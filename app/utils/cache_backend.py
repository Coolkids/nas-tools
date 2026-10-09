"""CacheLib 后端选择、故障降级及旧缓存接口适配。"""

import hashlib
import logging
import math
import os
import pickle
import sqlite3
import struct
import time
from threading import RLock

import redis
import valkey
from cachelib import FileSystemCache, RedisCache, SimpleCache, ValkeyCache

logger = logging.getLogger(__name__)
REMOTE_ERRORS = (redis.exceptions.RedisError, valkey.exceptions.ValkeyError, OSError)
CONNECT_ERRORS = REMOTE_ERRORS + (ValueError, TypeError)
DEFAULT_SETTINGS = {"backend": "memory_disk", "host": "127.0.0.1", "port": 6379,
                    "db": 0, "auth_mode": "none", "username": "", "password": "",
                    "connect_timeout": 0.5, "retry_interval": 30}
BACKENDS = {"memory", "memory_disk", "redis", "valkey"}
PREFIX = "nastool:cache:v2:"


def validate_settings(settings):
    backend = settings.get("backend", "memory_disk")
    auth_mode = settings.get("auth_mode", "none")
    if not isinstance(backend, str) or backend not in BACKENDS:
        raise ValueError("缓存保存方式无效")
    if not isinstance(auth_mode, str) or auth_mode not in {"none", "password"}:
        raise ValueError("缓存认证方式无效")
    if backend in {"redis", "valkey"} and auth_mode == "password" and not settings.get("password"):
        raise ValueError("缓存密码认证模式必须填写密码")
    for key, minimum, maximum in (("port", 1, 65535), ("db", 0, 15),
                                  ("connect_timeout", 0.1, 10), ("retry_interval", 1, 3600)):
        try:
            value = float(settings.get(key, DEFAULT_SETTINGS[key]))
            if not math.isfinite(value) or not minimum <= value <= maximum:
                raise ValueError
            if key in {"port", "db"} and not value.is_integer():
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError(f"缓存 {key} 配置无效") from None


class _DiskCache(FileSystemCache):
    """枚举 CacheLib 文件，每个命名空间独占一个目录。"""

    def records(self):
        for filename in self._list_dir():
            try:
                with open(filename, "rb") as stream:
                    expires = struct.unpack("I", stream.read(4))[0]
                    record = self.serializer.load(stream)
                if expires and expires <= time.time():
                    os.unlink(filename)
                elif isinstance(record, tuple) and len(record) == 3:
                    yield record
            except (OSError, EOFError, struct.error, pickle.UnpicklingError):
                logger.warning("忽略损坏的缓存文件")


class CacheStore:
    def __init__(self, path=None, settings=None):
        self.path = path
        self._settings_override = settings
        self._lock = RLock()
        self._signature = None
        self._settings = dict(DEFAULT_SETTINGS)
        self._backends = {}
        self._disk_stamps = {}
        self._client = None
        self._actual = "memory"
        self._reason = ""
        self._invalid = False
        self._retry_at = 0
        self._dirty = set()
        self._revision = 0

    @staticmethod
    def _key(key):
        return hashlib.sha256(pickle.dumps(key, protocol=4)).hexdigest()

    @staticmethod
    def _prefix(namespace):
        return PREFIX + namespace + ":"

    def _configuration(self):
        if self._settings_override is not None:
            return {**DEFAULT_SETTINGS, **self._settings_override}
        from config import Config
        configured = Config().get_config("cache") or {}
        return {**DEFAULT_SETTINGS, **configured} if isinstance(configured, dict) else dict(DEFAULT_SETTINGS)

    def _sync(self):
        settings = self._configuration()
        signature = tuple((key, str(settings.get(key))) for key in DEFAULT_SETTINGS)
        if signature != self._signature:
            self._signature = signature
            self._settings = settings
            self._backends.clear()
            self._disk_stamps.clear()
            self.close()
            self._revision += 1
            self._reason = ""
            self._invalid = False
            self._retry_at = 0
            self._actual = settings.get("backend", "memory_disk")
            try:
                validate_settings(settings)
            except ValueError as error:
                self._actual = "memory"
                self._reason = str(error)
                self._invalid = True
                logger.warning("%s，缓存降级到内存", self._reason)
        if self._settings.get("backend") in ("redis", "valkey") and self._client is None:
            if self._invalid or time.monotonic() < self._retry_at:
                return
            try:
                self._connect_remote()
                # 降级期间的写入/清理不能被恢复后的旧远程数据覆盖。
                for namespace in self._dirty:
                    self._clear_remote(namespace)
                recovered = bool(self._reason)
                self._dirty.clear()
                self._backends.clear()
                self._actual = self._settings["backend"]
                self._reason = ""
                self._revision += 1
                if recovered:
                    logger.info("远程缓存连接恢复，已切回 %s", self._actual)
            except CONNECT_ERRORS as error:
                self._fallback(error)

    def _connect_remote(self):
        settings = self._settings
        authenticated = settings["auth_mode"] == "password"
        options = dict(host=str(settings["host"]).strip(), port=int(settings["port"]),
                       db=int(settings["db"]), password=settings["password"] if authenticated else None,
                       socket_connect_timeout=float(settings["connect_timeout"]),
                       socket_timeout=float(settings["connect_timeout"]), decode_responses=False)
        if authenticated and settings.get("username"):
            options["username"] = settings["username"]
        client_class = valkey.Valkey if settings["backend"] == "valkey" else redis.Redis
        if settings["backend"] == "valkey":
            from valkey.backoff import NoBackoff
            from valkey.retry import Retry
        else:
            from redis.backoff import NoBackoff
            from redis.retry import Retry
        # 应用层负责冷却和探测，避免客户端重试额外延长缓存请求。
        options["retry"] = Retry(NoBackoff(), 0)
        client = client_class(**options)
        try:
            client.ping()
        except Exception:
            client.connection_pool.disconnect()
            raise
        self._client = client

    def close(self):
        if self._client is not None:
            self._client.connection_pool.disconnect()
        self._client = None

    def _fallback(self, error):
        was_remote = self._actual != "memory"
        self.close()
        if was_remote:
            self._backends.clear()
            self._revision += 1
        self._actual = "memory"
        # 不向日志、接口暴露包含密码/地址的异常文本。
        self._reason = f"{self._settings['backend']} 连接不可用（{type(error).__name__}）"
        self._retry_at = time.monotonic() + float(self._settings["retry_interval"])
        if was_remote:
            logger.warning("%s，缓存降级到内存", self._reason)

    @property
    def revision(self):
        with self._lock:
            self._sync()
            return self._revision

    def status(self):
        with self._lock:
            self._sync()
            return {"configured_backend": str(self._settings["backend"]), "backend": self._actual,
                    "fallback": bool(self._reason), "reason": self._reason}

    def _directory(self):
        if self.path is None:
            from config import Config
            self.path = os.path.join(Config().get_config_path(), "cache")
        return self.path

    def _backend(self, namespace):
        self._sync()
        if namespace not in self._backends:
            memory = SimpleCache(threshold=1_000_000, default_timeout=0)
            disk = None
            if self._actual == "memory_disk":
                directory = os.path.join(self._directory(), "files", self._key(namespace))
                try:
                    disk = _DiskCache(directory, threshold=0, default_timeout=0)
                except OSError as error:
                    self._reason = f"磁盘缓存不可用（{type(error).__name__}）"
                    logger.warning("%s，使用内存缓存", self._reason)
                    self._actual = "memory"
                    self._backends.clear()
                    self._revision += 1
            elif self._actual in {"redis", "valkey"}:
                cache_class = ValkeyCache if self._actual == "valkey" else RedisCache
                memory = cache_class(self._client, key_prefix=self._prefix(namespace), default_timeout=0)
            self._backends[namespace] = (memory, disk)
            if disk is not None:
                try:
                    self._migrate(namespace, disk)
                except (OSError, sqlite3.DatabaseError):
                    logger.warning("旧缓存导入失败：%s", namespace)
        if self._settings["backend"] in ("redis", "valkey") and self._actual == "memory":
            self._dirty.add(namespace)
        memory, disk = self._backends[namespace]
        if disk is not None:
            stamp = os.stat(disk._path).st_mtime_ns
            if stamp != self._disk_stamps.get(namespace):
                memory.clear()
                self._disk_stamps[namespace] = stamp
        return self._backends[namespace]

    def _migrate(self, namespace, disk):
        """只读导入旧 SQLite/tmdb.dat，导入标记不会随清理缓存删除。"""
        marker = os.path.join(self._directory(), "migrations", self._key(namespace))
        if os.path.exists(marker):
            return
        root = os.path.dirname(self._directory())
        records = []
        for filename in (os.path.join(root, "system.sqlite3"), os.path.join(self._directory(), "system.sqlite3")):
            if not os.path.isfile(filename):
                continue
            connection = sqlite3.connect(filename, timeout=1)
            try:
                for payload, expires in connection.execute(
                        "SELECT payload, expires FROM cache_entries WHERE namespace=? ORDER BY rowid", (namespace,)):
                    try:
                        key, value = pickle.loads(payload)
                        records.append((key, value, expires))
                    except (pickle.PickleError, ValueError, TypeError, EOFError):
                        logger.warning("忽略损坏的旧缓存条目：%s", namespace)
            except sqlite3.DatabaseError:
                logger.warning("无法读取旧缓存数据库：%s", namespace)
            finally:
                connection.close()
            break
        metadata = os.path.join(root, "tmdb.dat")
        if namespace == "tmdb_metadata" and os.path.isfile(metadata):
            try:
                with open(metadata, "rb") as stream:
                    values = pickle.load(stream)
                records.extend((key, value, None) for key, value in values.items() if str(value.get("id")) != "0")
            except (OSError, pickle.PickleError, EOFError, AttributeError):
                logger.warning("无法读取旧 TMDB 缓存")
        for key, value, expires in records:
            if expires is not None and expires <= time.time():
                continue
            if disk.get(self._key(key)) is None:
                if not disk.set(self._key(key), (key, value, expires), timeout=self._timeout(expires)):
                    return
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "w", encoding="utf-8") as stream:
            stream.write("1")

    @staticmethod
    def _timeout(expires):
        return max(1, math.ceil(expires - time.time()) + 1) if expires is not None else 0

    def _call(self, namespace, operation):
        with self._lock:
            memory, disk = self._backend(namespace)
            try:
                result = operation(memory, disk)
                if disk is not None:
                    self._disk_stamps[namespace] = os.stat(disk._path).st_mtime_ns
                return result
            except REMOTE_ERRORS as error:
                if self._actual not in {"redis", "valkey"}:
                    raise
                self._fallback(error)
                memory, disk = self._backend(namespace)
                return operation(memory, disk)

    def get(self, namespace, key):
        wire_key = self._key(key)

        def read(memory, disk):
            record = memory.get(wire_key)
            if record is None and disk is not None:
                record = disk.get(wire_key)
                if record is not None:
                    memory.set(wire_key, record, timeout=self._timeout(record[2]))
            if record is not None and record[2] is not None and record[2] <= time.time():
                memory.delete(wire_key)
                if disk is not None:
                    disk.delete(wire_key)
                return None
            return record
        return self._call(namespace, read)

    def put(self, namespace, key, value, expires=None):
        record, wire_key = (key, value, expires), self._key(key)

        def write(memory, disk):
            if not memory.set(wire_key, record, timeout=self._timeout(expires)):
                raise OSError("缓存写入失败")
            if disk is not None and not disk.set(wire_key, record, timeout=self._timeout(expires)):
                logger.warning("缓存写入磁盘失败：%s", namespace)
        self._call(namespace, write)

    def delete(self, namespace, key):
        def remove(memory, disk):
            result = memory.delete(self._key(key))
            if disk is not None:
                result = disk.delete(self._key(key)) or result
            return result
        return self._call(namespace, remove)

    def _clear_remote(self, namespace):
        batch = []
        for key in self._client.scan_iter(match=self._prefix(namespace) + "*", count=500):
            batch.append(key)
            if len(batch) == 500:
                self._client.delete(*batch)
                batch.clear()
        if batch:
            self._client.delete(*batch)

    def clear(self, namespace):
        def remove(memory, disk):
            if self._actual in {"redis", "valkey"}:
                self._clear_remote(namespace)
            else:
                memory.clear()
            if disk is not None and not disk.clear():
                raise OSError("清理磁盘缓存失败")
        self._call(namespace, remove)

    def load(self, namespace):
        def read(memory, disk):
            if self._actual in {"redis", "valkey"}:
                prefix = self._prefix(namespace)
                keys = [key.decode()[len(prefix):] for key in
                        self._client.scan_iter(match=prefix + "*", count=500)]
                records = memory.get_many(*keys) if keys else []
            else:
                records = [memory.get(key) for key in list(memory._cache)]
                if disk is not None:
                    records.extend(disk.records())
            result = {}
            for record in records:
                if not isinstance(record, tuple) or len(record) != 3:
                    continue
                key, value, expires = record
                if expires is not None and expires <= time.time():
                    memory.delete(self._key(key))
                    if disk is not None:
                        disk.delete(self._key(key))
                else:
                    result[self._key(key)] = (key, value, expires)
            return list(result.values())
        return self._call(namespace, read)

    def memory_info(self, namespace):
        from app.utils.cache_memory import estimate_memory

        def measure(memory, disk):
            records = self.load(namespace)
            memory, disk = self._backend(namespace)
            if self._actual in {"redis", "valkey"}:
                pipeline = self._client.pipeline(transaction=False)
                for key, _, _ in records:
                    pipeline.memory_usage(self._prefix(namespace) + self._key(key))
                try:
                    size = sum(value or 0 for value in pipeline.execute()) if records else 0
                except (redis.exceptions.ResponseError, valkey.exceptions.ResponseError):
                    # debug 统计权限不足不应影响正常缓存或触发后端切换。
                    size = None
            else:
                size = estimate_memory(memory._cache) if memory._cache else 0
            actual = "memory_disk" if disk is not None else self._actual
            if actual == "memory_disk" and disk is None:
                actual = "memory"
            return {"entries": len(records), "memory_bytes": size, "backend": actual,
                    "persistent": actual == "memory_disk"}
        return self._call(namespace, measure)


STORE = CacheStore()
