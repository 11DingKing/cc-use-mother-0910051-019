import threading
from contextlib import contextmanager

class KeyedLockPool:
    """按 key 串行化临界区的进程内锁池。

    SQLite 不支持 SELECT ... FOR UPDATE，多请求并发时用它在应用层
    把"读取-校验-写入-提交"整段包成互斥区，配合单事务提交保证
    数量守恒（不超分、不超发）与原子性（失败整体回滚）。
    """

    def __init__(self):
        self._guard = threading.Lock()
        self._locks = {}

    @contextmanager
    def acquire(self, key: str):
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        lock.acquire()
        try:
            yield
        finally:
            lock.release()

lock_pool = KeyedLockPool()
