"""
通过request进行http下载的实现
新起一个进程 以避免和主进程间的竞争，通过队列进行通信
进程内通过线程池消费需要爬取的任务
"""
import multiprocessing
import time
from concurrent.futures import ThreadPoolExecutor, Future
from enum import Enum
from multiprocessing import Process, Queue
from multiprocessing.synchronize import Event
from os import cpu_count
from queue import Empty
from typing import Optional, NoReturn

from fake_useragent import UserAgent
from requests import Response as RequestsResponse, RequestException, get

from downloader.Downloader import AsyncHttpDownloader, BaseRequest, BaseResponse


class Request(BaseRequest):
    def __init__(self, url, retry_time=3):
        super().__init__(url)
        if retry_time < 1:
            raise AttributeError

        self.retry_time = retry_time


class State(Enum):
    SUCCESS = 1
    FALSE = 2


class Response(BaseResponse):
    def __init__(self, request: Request, result: Optional[RequestsResponse], state: State):
        super().__init__(request, result)
        self.state = state


class AsyncHttpRequestDownloader(AsyncHttpDownloader):
    def __init__(self):
        self._request_queue: Queue[Request] = Queue()
        self._result_queue: Queue[Response] = Queue()
        self._exit_sign: Event = multiprocessing.Event()

        self._child_process = AsyncHttpRequestDownloader.GetPageByMultiThreading(self._request_queue,
                                                                                 self._result_queue,
                                                                                 self._exit_sign)
        self._child_process.start()

    def summit(self, request: Request):
        self._request_queue.put(request)

    def not_next_result(self) -> bool:
        return self._result_queue.empty()

    def get_result(self) -> Optional[Response]:
        try:
            return self._result_queue.get_nowait()
        except Empty:
            return None

    def shutdown(self):
        self._request_queue.close()
        self._exit_sign.set()
        self._child_process.join()

    class GetPageByMultiThreading(Process):
        def __init__(self, request_queue: Queue[Request], result_queue: Queue[Response], exit_sign: Event):
            super().__init__()
            self._fake_ua = UserAgent()
            self._request_queue = request_queue
            self._result_queue = result_queue
            self._thread_max_workers = cpu_count() * 5
            self._executor = ThreadPoolExecutor(max_workers=self._thread_max_workers)
            self._future_list: list[Future] = list()
            self._exit_sign = exit_sign

        def get_page(self, request: Request) -> Response:
            header = {"User-Agent": self._fake_ua.random}
            try:
                page = get(request.url, headers=header, timeout=1)
                if not page.text:
                    # 反爬虫策略之 给你返回空白的 200结果
                    raise AttributeError
                return Response(request, page, State.SUCCESS)
            except (RequestException, AttributeError):
                return Response(request, None, State.FALSE)

        def run(self) -> NoReturn:
            while True:
                # 结束
                if self._exit_sign.is_set() and self._request_queue.empty() and not self._future_list:
                    self._executor.shutdown()
                    break

                # 处理结果
                new_future_list: list[Future] = list()
                for future in self._future_list:
                    if not future.done():
                        new_future_list.append(future)
                        continue

                    result: Response = future.result()
                    if result.state == State.FALSE and result.request.retry_time > 0:
                        # 失败重试
                        result.request.retry_time -= 1
                        self._request_queue.put(result.request)
                        continue
                    self._result_queue.put(result)
                self._future_list = new_future_list

                # 处理请求
                request_once_handle_max_num = self._thread_max_workers * 2 - len(self._future_list)
                while not self._request_queue.empty() and request_once_handle_max_num > 0:
                    request = self._request_queue.get()
                    self._future_list.append(self._executor.submit(self.get_page, request))
                    request_once_handle_max_num -= 1

                # 根据当前未完成的任务数量，休眠主线程，避免循环占用过多的cpu时间
                time.sleep(1000 * len(self._future_list) / self._thread_max_workers * 2)
