"""业务缓存接口适配，数据存储由 CacheLib 后端负责。"""

import hashlib
import pickle
import time
from collections import OrderedDict, namedtuple
from functools import wraps
from threading import RLock

from app.utils.cache_backend import CacheStore, STORE
from app.utils.cache_policy import cache_ttl

CacheInfo = namedtuple("CacheInfo", "hits misses maxsize currsize")
StatsInfo = namedtuple("StatsInfo", "hit_count miss_count")


class _Stats:
    def __init__(self):
        self.reset()

    def reset(self):
        self.hits = self.misses = 0

    def info(self):
        return StatsInfo(self.hits, self.misses)


class PersistentCache:
    """兼容旧接口；默认不限制容量，仅有限容量缓存维护淘汰顺序。"""

    def __init__(self, name, store=None, maxsize=0, ttl=0, timer=time.time,
                 default=None, enable_stats=False):
        self.name, self.maxsize, self._ttl = name, maxsize, ttl
        self.store = store if store is not None else STORE
        self.timer, self.default = timer, default
        self.stats = _Stats()
        self._lock = RLock()
        self._order = OrderedDict()
        self._revision = None

    @property
    def ttl(self):
        return cache_ttl(self.name, self._ttl)

    @ttl.setter
    def ttl(self, value):
        self._ttl = value

    def _sync(self):
        revision = self.store.revision
        if revision != self._revision:
            self._order = (OrderedDict((key, expires) for key, _, expires in self.store.load(self.name))
                           if self.maxsize else OrderedDict())
            self._revision = self.store.revision
            while self.maxsize and len(self._order) > self.maxsize:
                self.delete(next(iter(self._order)))

    def get(self, key, default=None):
        with self._lock:
            self._sync()
            record = self.store.get(self.name, key)
            if record is None:
                self._order.pop(key, None)
                self.stats.misses += 1
                return self.default if default is None else default
            self.stats.hits += 1
            if self.maxsize:
                self._order[key] = record[2]
                self._order.move_to_end(key)
            return record[1]

    def get_with_expiration(self, key):
        """返回缓存值和绝对过期时间，供格式迁移保留原 TTL。"""
        with self._lock:
            self._sync()
            record = self.store.get(self.name, key)
            return (record[1], record[2]) if record is not None else (None, None)

    def get_envelope(self, key, *, hit=True, allow_stale=False):
        return self.store.envelope_get(self.name, key, hit=hit, allow_stale=allow_stale)

    def compare_and_set_envelope(self, key, value, expected, **lease):
        return self.store.envelope_put(self.name, key, value, expected=expected, **lease)

    def set_with_expiration(self, key, value, expires):
        """按绝对过期时间写入，不因格式迁移重新授予 TTL。"""
        with self._lock:
            self._sync()
            self.store.put(self.name, key, value, expires)
            if self.maxsize:
                self._order[key] = expires
                self._order.move_to_end(key)
                while len(self._order) > self.maxsize:
                    self.delete(next(iter(self._order)))

    def set(self, key, value, ttl=None):
        with self._lock:
            self._sync()
            effective_ttl = cache_ttl(self.name, self._ttl if ttl is None else ttl)
            expires = time.time() + effective_ttl if effective_ttl else None
            if self.maxsize:
                for old, expiration in list(self._order.items()):
                    if expiration is not None and expiration <= time.time():
                        self.delete(old)
            if self.maxsize and key not in self._order:
                while self.maxsize and len(self._order) >= self.maxsize:
                    self.delete(next(iter(self._order)))
            self.store.put(self.name, key, value, expires)
            if self.maxsize:
                self._order[key] = expires
                self._order.move_to_end(key)

    def set_many(self, mapping, ttl=None):
        for key, value in mapping.items():
            self.set(key, value, ttl)

    def delete(self, key):
        with self._lock:
            self._order.pop(key, None)
            return int(bool(self.store.delete(self.name, key)))

    def clear(self):
        with self._lock:
            self.store.clear(self.name)
            self._order.clear()
            self._revision = self.store.revision

    def copy(self):
        with self._lock:
            self._sync()
            records = {key: (value, expires) for key, value, expires in self.store.load(self.name)}
            if not self.maxsize:
                return {key: value for key, (value, _) in records.items()}
            for key in list(self._order):
                if key not in records:
                    self._order.pop(key)
            for key, (_, expires) in records.items():
                self._order.setdefault(key, expires)
            return {key: records[key][0] for key in self._order if key in records}

    def expire_times(self):
        return {key: expires for key, _, expires in self.store.load(self.name) if expires is not None}

    def delete_expired(self):
        with self._lock:
            return self.store.prune_expired(self.name)

    def memory_info(self):
        with self._lock:
            self._sync()
        return self.store.memory_info(self.name)

    def __len__(self):
        return self.memory_info()["entries"]

    def __iter__(self):
        return iter(self.copy())

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


def persistent_memoize(name, maxsize=0, ttl=86400):
    cache = PersistentCache(name, maxsize=maxsize, ttl=ttl, enable_stats=True)
    FUNCTION_CACHES[name] = cache

    def decorate(function):
        missing = object()

        @wraps(function)
        def wrapped(*args, **kwargs):
            values = args[1:] if args and isinstance(args[0], type) else args
            key = hashlib.sha256(pickle.dumps(
                (function.__module__, function.__qualname__, values, sorted(kwargs.items())), protocol=4)).hexdigest()
            value = cache.get(key, missing)
            if value is missing:
                value = function(*args, **kwargs)
                if value is not None:
                    cache.set(key, value)
            return value

        def clear():
            cache.clear()
            cache.stats.reset()

        wrapped.cache_clear = clear
        wrapped.cache_info = lambda: CacheInfo(cache.stats.hits, cache.stats.misses, cache.maxsize, len(cache))
        wrapped.cache = cache
        return wrapped
    return decorate
