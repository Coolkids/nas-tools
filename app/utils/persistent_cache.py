"""本地缓存持久化；保留绝对过期时间，不延长重启后的 TTL。"""

import hashlib
import logging
import os
import pickle
import sqlite3
import tempfile
import time
from collections import namedtuple
from functools import wraps
from threading import RLock

from cacheout import LRUCache


class CacheStore:
    def __init__(self, path=None):
        self.path = path
        self._connection = None
        self._lock = RLock()

    def _connect(self):
        if self._connection is None:
            if self.path is None:
                from config import Config
                self.path = os.path.join(Config().get_config_path(), "system.sqlite3")
                self._migrate_legacy_cache()
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            self._connection = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute(
                "CREATE TABLE IF NOT EXISTS cache_entries ("
                "namespace TEXT, key_hash TEXT, payload BLOB, expires REAL, "
                "PRIMARY KEY (namespace, key_hash))")
            self._connection.commit()
        return self._connection

    def _migrate_legacy_cache(self):
        legacy_path = os.path.join(os.path.dirname(self.path), "cache", "system.sqlite3")
        if os.path.exists(self.path) or not os.path.isfile(legacy_path):
            return
        fd, temporary = tempfile.mkstemp(dir=os.path.dirname(self.path), suffix=".sqlite3.tmp")
        os.close(fd)
        try:
            source = sqlite3.connect(legacy_path, timeout=10)
            try:
                destination = sqlite3.connect(temporary, timeout=10)
                try:
                    # backup 会同时读取已提交的 WAL 数据；不能只复制主数据库文件。
                    source.backup(destination)
                finally:
                    destination.close()
            finally:
                source.close()
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def _key(key):
        return hashlib.sha256(pickle.dumps(key, protocol=4)).hexdigest()

    def load(self, namespace):
        with self._lock:
            connection = self._connect()
            with connection:
                connection.execute("DELETE FROM cache_entries WHERE namespace=? "
                                   "AND expires IS NOT NULL AND expires<=?", (namespace, time.time()))
            rows = connection.execute("SELECT key_hash, payload, expires FROM cache_entries "
                                      "WHERE namespace=? ORDER BY rowid", (namespace,)).fetchall()
            entries = []
            for key_hash, payload, expires in rows:
                try:
                    key, value = pickle.loads(payload)
                    entries.append((key, value, expires))
                except Exception:
                    logging.getLogger(__name__).warning("忽略损坏的缓存条目：%s", namespace)
                    with connection:
                        connection.execute("DELETE FROM cache_entries WHERE namespace=? AND key_hash=?",
                                           (namespace, key_hash))
            return entries

    def put(self, namespace, key, value, expires=None):
        payload = pickle.dumps((key, value), protocol=pickle.HIGHEST_PROTOCOL)
        with self._lock, self._connect() as connection:
            connection.execute("INSERT OR REPLACE INTO cache_entries VALUES (?, ?, ?, ?)",
                               (namespace, self._key(key), payload, expires))

    def delete(self, namespace, key):
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM cache_entries WHERE namespace=? AND key_hash=?",
                               (namespace, self._key(key)))

    def clear(self, namespace):
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM cache_entries WHERE namespace=?", (namespace,))


STORE = CacheStore()


class PersistentCache(LRUCache):
    def __init__(self, name, store=None, **kwargs):
        self.name = name
        self.store = store if store is not None else STORE
        self._loaded = False
        super().__init__(**kwargs)

    def _load(self):
        if self._loaded:
            return
        entries = self.store.load(self.name)
        self._loaded = True
        for key, value, expires in entries:
            if expires is not None and expires <= time.time():
                continue
            self._cache[key] = value
            if expires is not None:
                self._expire_times[key] = self.timer() + max(0, expires - time.time())
        while self.maxsize and len(self._cache) > self.maxsize:
            self._delete(next(iter(self._cache)))

    def _get(self, key, default=None):
        self._load()
        return super()._get(key, default)

    def _set(self, key, value, ttl=None):
        self._load()
        super()._set(key, value, ttl)
        expires = self._expire_times.get(key)
        if expires is not None:
            expires = time.time() + expires - self.timer()
        try:
            self.store.put(self.name, key, value, expires)
        except (pickle.PickleError, TypeError, AttributeError):
            # 部分临时对象不可序列化，仍保留内存缓存。
            logging.getLogger(__name__).warning("缓存条目无法持久化：%s", self.name)

    def _delete(self, key, cause=None):
        self._load()
        count = super()._delete(key, cause)
        if count:
            self.store.delete(self.name, key)
        return count

    def _clear(self):
        self.store.clear(self.name)
        self._loaded = True
        super()._clear()

    def __len__(self):
        with self._lock:
            self._load()
            return super().__len__()

    def copy(self):
        with self._lock:
            self._load()
            self._delete_expired()
            return super().copy()

    def _delete_expired(self):
        self._load()
        return super()._delete_expired()

    def __getitem__(self, key):
        missing = object()
        value = self.get(key, missing)
        if value is missing:
            raise KeyError(key)
        return value

    def __setitem__(self, key, value):
        self.set(key, value)

    def pop(self, key, default=None):
        with self._lock:
            value = self.get(key, default)
            self.delete(key)
            return value


FUNCTION_CACHES = {}
CacheInfo = namedtuple("CacheInfo", "hits misses maxsize currsize")


def persistent_memoize(name, maxsize=1024, ttl=86400):
    """替代 lru_cache，兼容已有 cache_clear/cache_info 调用。"""
    cache = PersistentCache(name, maxsize=maxsize, ttl=ttl, enable_stats=True)
    FUNCTION_CACHES[name] = cache

    def decorate(function):
        missing = object()

        @wraps(function)
        def wrapped(*args, **kwargs):
            # cls 的对象地址不会跨重启稳定；使用函数限定名标识方法。
            values = args[1:] if args and isinstance(args[0], type) else args
            key = hashlib.sha256(pickle.dumps(
                (function.__module__, function.__qualname__, values, sorted(kwargs.items())),
                protocol=4)).hexdigest()
            value = cache.get(key, missing)
            if value is missing:
                value = function(*args, **kwargs)
                if value is not None:
                    cache.set(key, value)
            return value

        def clear():
            cache.clear()
            cache.stats.reset()

        def info():
            with cache._lock:
                cache.delete_expired()
                stats = cache.stats.info()
                return CacheInfo(stats.hit_count, stats.miss_count, cache.maxsize, len(cache))

        wrapped.cache_clear = clear
        wrapped.cache_info = info
        wrapped.cache = cache
        return wrapped

    return decorate
