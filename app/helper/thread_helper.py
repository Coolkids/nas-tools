from concurrent.futures import ThreadPoolExecutor

from app.db import close_db
from app.utils.commons import singleton


@singleton
class ThreadHelper:
    _thread_num = 50
    executor = None

    def __init__(self):
        self.executor = ThreadPoolExecutor(max_workers=self._thread_num)

    def init_config(self):
        pass

    def start_thread(self, func, kwargs):
        def _wrapped():
            try:
                return func(*kwargs)
            finally:
                # 任务结束后释放当前线程的 scoped_session，归还连接到连接池
                close_db()

        self.executor.submit(_wrapped)
