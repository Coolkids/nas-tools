"""CacheLib 后端选择、故障降级及旧缓存接口适配。"""

import hashlib
import heapq
import json
import logging
import math
import os
import pickle
import sqlite3
import stat
import struct
import sys
import time
import uuid
from contextlib import contextmanager, nullcontext
from pathlib import Path
from threading import RLock, local

import redis
import valkey
from cachelib import FileSystemCache, RedisCache, SimpleCache, ValkeyCache
from app.utils.storage_jobs import STORAGE_JOBS
from app.utils.cache_atomic import AtomicCacheMixin, V3_NAMESPACES

logger = logging.getLogger(__name__)
REMOTE_ERRORS = (redis.exceptions.RedisError, valkey.exceptions.ValkeyError, OSError)
CONNECT_ERRORS = REMOTE_ERRORS + (ValueError, TypeError)
DEFAULT_SETTINGS = {"backend": "memory_disk", "host": "127.0.0.1", "port": 6379,
                    "db": 0, "auth_mode": "none", "username": "", "password": "",
                    "connect_timeout": 0.5, "retry_interval": 30, "storage_generation": ""}
BACKENDS = {"memory", "memory_disk", "redis", "valkey"}
PREFIX = "nastool:cache:v2:"
_LEGACY_SOURCE_RETENTION_SECONDS = 30 * 24 * 60 * 60
CACHE_NAMESPACE_REGISTRY = (
    "tmdb_supply", "TmdbWebSearchCache", "TorznabCache", "WebSearchCache",
    "WebMediaInfoCache", "TmdbEnTitleCache", "TmdbHotMoviesCache", "TmdbHotTvsCache",
    "TmdbNewMoviesCache", "TmdbNewTvsCache", "TmdbUpcomingMoviesCache", "TmdbTrendingCache",
    "TmdbSeasonDetailCache", "SubtitleCache", "tmdb_metadata", "bangumi", "douban_api",
    "douban_detail", "douban_user", "fanart", "web_request", "wallpaper", "tmdb_request",
    "recognition_tmdb", "recognition_decision", "recognition_parse",
) + V3_NAMESPACES


def validate_settings(settings):
    backend = settings.get("backend", "memory_disk")
    auth_mode = settings.get("auth_mode", "none")
    if not isinstance(backend, str) or backend not in BACKENDS:
        raise ValueError("缓存保存方式无效")
    if not isinstance(auth_mode, str) or auth_mode not in {"none", "password"}:
        raise ValueError("缓存认证方式无效")
    if backend in {"redis", "valkey"} and auth_mode == "password" and not settings.get("password"):
        raise ValueError("缓存密码认证模式必须填写密码")
    generation = settings.get("storage_generation", "")
    if (not isinstance(generation, str) or len(generation) > 128
            or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
                   for char in generation)):
        raise ValueError("缓存存储代次无效")
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

    def records(self, strict=False):
        for filename in self._list_dir():
            try:
                with open(filename, "rb") as stream:
                    expires = struct.unpack("I", stream.read(4))[0]
                    record = self.serializer.load(stream)
                if isinstance(record, tuple) and len(record) == 3:
                    yield record
                elif strict:
                    raise ValueError("旧文件缓存包含不支持的条目")
            except Exception as error:
                if strict:
                    raise ValueError("旧文件缓存包含无法读取的条目") from error
                logger.warning("忽略损坏的缓存文件")

    def _prune(self):
        # 文件缓存不参与容量淘汰；旧来源在安全退休前保持不变。
        return None


class _UnboundedSimpleCache(SimpleCache):
    """沿用 CacheLib 序列化，禁用容量淘汰并用到期堆分批回收。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._expiry_heap = []
        self._expiry_versions = {}
        self._expiry_sequence = 0
        self._estimated_sizes = {}
        self._estimated_memory = 0

    @staticmethod
    def _estimate_item_size(key, item):
        expires, payload = item
        return sys.getsizeof(key) + sys.getsizeof(expires) + sys.getsizeof(payload) + 72

    def _prune(self):
        now = time.time()
        removed = 0
        processed = 0
        while (self._expiry_heap and self._expiry_heap[0][0] <= now
               and processed < 500):
            expires, key, sequence = heapq.heappop(self._expiry_heap)
            processed += 1
            if self._expiry_versions.get(key) == sequence:
                self._expiry_versions.pop(key, None)
                if super().delete(key):
                    self._estimated_memory -= self._estimated_sizes.pop(key, 0)
                    removed += 1
        return removed

    def _compact_expiry_heap(self):
        if len(self._expiry_heap) <= max(1000, len(self._expiry_versions) * 2):
            return
        self._expiry_heap = [
            (expires, key, sequence)
            for expires, key, sequence in self._expiry_heap
            if self._expiry_versions.get(key) == sequence]
        heapq.heapify(self._expiry_heap)

    def set(self, key, value, timeout=None):
        with self._lock:
            self._prune()
            result = super().set(key, value, timeout=timeout)
            if result:
                item = self._cache[key]
                expires = item[0]
                old_size = self._estimated_sizes.get(key, 0)
                new_size = self._estimate_item_size(key, item)
                self._estimated_sizes[key] = new_size
                self._estimated_memory += new_size - old_size
                if expires:
                    self._expiry_sequence += 1
                    sequence = self._expiry_sequence
                    self._expiry_versions[key] = sequence
                    heapq.heappush(self._expiry_heap, (expires, key, sequence))
                    self._compact_expiry_heap()
                else:
                    self._expiry_versions.pop(key, None)
            return result

    def delete(self, key):
        with self._lock:
            self._expiry_versions.pop(key, None)
            removed = super().delete(key)
            if removed:
                self._estimated_memory -= self._estimated_sizes.pop(key, 0)
            self._compact_expiry_heap()
            return removed

    def clear(self):
        with self._lock:
            self._expiry_heap.clear()
            self._expiry_versions.clear()
            self._estimated_sizes.clear()
            self._estimated_memory = 0
            return super().clear()

    def records(self, strict=False):
        """枚举未过期记录，不触发 CacheLib 的读时清理。"""
        now = time.time()
        with self._lock:
            for key, item in self._cache.items():
                try:
                    expires, payload = item
                    if expires and expires <= now:
                        continue
                    yield self.serializer.loads(payload)
                except Exception as error:
                    if strict:
                        raise ValueError("内存缓存包含无法读取的条目") from error
                    logger.warning("忽略损坏的内存缓存条目")


class _SQLiteCache:
    """本地持久化缓存；单条访问使用短连接和短事务。"""

    FORMAT_VERSION = 1
    CLEANUP_INTERVAL = 60

    def __init__(self, path, namespace):
        self._path = path
        self.namespace = namespace
        self._last_cleanup = 0
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("""CREATE TABLE IF NOT EXISTS cache_entries_v2 (
                namespace TEXT NOT NULL,
                cache_key TEXT NOT NULL,
                payload BLOB NOT NULL,
                expires REAL,
                PRIMARY KEY(namespace, cache_key)
            )""")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_cache_entries_v2_expires "
                               "ON cache_entries_v2(expires)")
            connection.execute("""CREATE TABLE IF NOT EXISTS cache_metadata_v2 (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )""")
            connection.execute("INSERT OR IGNORE INTO cache_metadata_v2(key, value) VALUES (?, ?)",
                               ("format_version", str(self.FORMAT_VERSION)))
            version = connection.execute(
                "SELECT value FROM cache_metadata_v2 WHERE key=?", ("format_version",)).fetchone()
            if not version or version[0] != str(self.FORMAT_VERSION):
                raise sqlite3.DatabaseError("缓存 SQLite 结构版本不受支持")
            # Install V3 cache-only tables before publishing disk stamps. Doing
            # DDL during a later snapshot read would change the source revision.
            connection.execute("""CREATE TABLE IF NOT EXISTS recognition_cache_v3 (
                namespace TEXT NOT NULL, cache_key TEXT NOT NULL, data TEXT NOT NULL,
                hits INTEGER NOT NULL, last_access REAL,
                PRIMARY KEY(namespace, cache_key))""")
            connection.execute("""CREATE TABLE IF NOT EXISTS recognition_cache_lease_v3 (
                namespace TEXT NOT NULL, cache_key TEXT NOT NULL, owner TEXT,
                expires REAL NOT NULL, fence INTEGER NOT NULL,
                PRIMARY KEY(namespace, cache_key))""")
            from app.utils.cache_rank import ensure_disk_index
            ensure_disk_index(connection)
            connection.execute("""CREATE TABLE IF NOT EXISTS recognition_cache_reads_v3 (
                namespace TEXT PRIMARY KEY, hits INTEGER NOT NULL, miss INTEGER NOT NULL,
                stale_reads INTEGER NOT NULL, since REAL NOT NULL)""")

    def _connect(self):
        connection = sqlite3.connect(self._path, timeout=10, isolation_level=None)
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    @contextmanager
    def _connection(self):
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @staticmethod
    def _encode(record):
        return struct.pack(">I", _SQLiteCache.FORMAT_VERSION) + pickle.dumps(record, protocol=4)

    @staticmethod
    def _decode(payload):
        if len(payload) < 4 or struct.unpack(">I", payload[:4])[0] != _SQLiteCache.FORMAT_VERSION:
            raise ValueError("缓存载荷版本无效")
        record = pickle.loads(payload[4:])
        if not isinstance(record, tuple) or len(record) != 3:
            raise ValueError("缓存载荷结构无效")
        return record

    def get(self, key, cleanup=True):
        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload, expires FROM cache_entries_v2 WHERE namespace=? AND cache_key=?",
                (self.namespace, key)).fetchone()
            if row is None:
                return None
            if row[1] is not None and row[1] <= time.time():
                if cleanup:
                    connection.execute("DELETE FROM cache_entries_v2 WHERE namespace=? AND cache_key=?",
                                       (self.namespace, key))
                return None
            try:
                return self._decode(row[0])
            except Exception:
                logger.warning("忽略损坏的 SQLite 缓存条目：%s", self.namespace)
                connection.execute("DELETE FROM cache_entries_v2 WHERE namespace=? AND cache_key=?",
                                   (self.namespace, key))
                return None

    def set(self, key, record, timeout=None):
        self._cleanup_expired()
        expires = record[2]
        with self._connection() as connection:
            connection.execute("""INSERT INTO cache_entries_v2(namespace, cache_key, payload, expires)
                VALUES (?, ?, ?, ?) ON CONFLICT(namespace, cache_key) DO UPDATE SET
                payload=excluded.payload, expires=excluded.expires""",
                               (self.namespace, key, self._encode(record), expires))
        return True

    def set_many(self, records):
        values = [(self.namespace, key, self._encode(record), record[2])
                  for key, record in records]
        if not values:
            return 0
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.executemany("""INSERT INTO cache_entries_v2
                    (namespace, cache_key, payload, expires) VALUES (?, ?, ?, ?)
                    ON CONFLICT(namespace, cache_key) DO UPDATE SET
                    payload=excluded.payload, expires=excluded.expires""", values)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return len(values)

    def delete(self, key):
        with self._connection() as connection:
            cursor = connection.execute(
                "DELETE FROM cache_entries_v2 WHERE namespace=? AND cache_key=?",
                (self.namespace, key))
            return cursor.rowcount > 0

    def clear(self):
        with self._connection() as connection:
            connection.execute("DELETE FROM cache_entries_v2 WHERE namespace=?", (self.namespace,))
        return True

    def records(self, strict=False):
        now = time.time()
        with self._connection() as connection:
            cursor = connection.execute(
                "SELECT cache_key, payload FROM cache_entries_v2 WHERE namespace=? "
                "AND (expires IS NULL OR expires>?) ORDER BY rowid", (self.namespace, now))
            while True:
                rows = cursor.fetchmany(500)
                if not rows:
                    break
                for _, payload in rows:
                    try:
                        yield self._decode(payload)
                    except Exception as error:
                        if strict:
                            raise sqlite3.DatabaseError("SQLite 缓存包含无法读取的载荷") from error
                        logger.warning("忽略损坏的 SQLite 缓存条目：%s", self.namespace)

    def stats(self):
        now = time.time()
        with self._connection() as connection:
            return connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(length(payload)), 0) FROM cache_entries_v2 "
                "WHERE namespace=? AND (expires IS NULL OR expires>?)",
                (self.namespace, now)).fetchone()

    def prune_expired(self):
        with self._connection() as connection:
            cursor = connection.execute("""DELETE FROM cache_entries_v2 WHERE rowid IN (
                SELECT rowid FROM cache_entries_v2 WHERE namespace=? AND expires IS NOT NULL
                AND expires<=? LIMIT 500
            )""", (self.namespace, time.time()))
            return max(0, cursor.rowcount)

    def _cleanup_expired(self):
        now_mono = time.monotonic()
        if now_mono - self._last_cleanup < self.CLEANUP_INTERVAL:
            return
        self._last_cleanup = now_mono
        with self._connection() as connection:
            connection.execute("""DELETE FROM cache_entries_v2 WHERE rowid IN (
                SELECT rowid FROM cache_entries_v2 WHERE expires IS NOT NULL AND expires<=?
                LIMIT 500
            )""", (time.time(),))


class CacheStore(AtomicCacheMixin):
    def __init__(self, path=None, settings=None, maintenance=True):
        self.path = path
        self._settings_override = settings
        self._maintenance_enabled = maintenance
        self._lock = RLock()
        self._layout_lock_state = local()
        self._signature = None
        self._settings = dict(DEFAULT_SETTINGS)
        self._pending_settings = None
        self._backends = {}
        self._disk_stamps = {}
        self._client = None
        self._actual = "memory"
        self._reason = ""
        self._invalid = False
        self._retry_at = 0
        self._dirty = set()
        self._revision = 0
        self._storage_ready = False
        self._legacy_sqlite_paths = []
        self._frozen = False
        self._instance_id = None

    @staticmethod
    def _key(key):
        return hashlib.sha256(pickle.dumps(key, protocol=4)).hexdigest()

    def _prefix(self, namespace):
        generation = str(self._settings.get("storage_generation") or "").strip()
        if not generation:
            # 保持未迁移旧配置的现有远端命名空间可读。
            return PREFIX + namespace + ":"
        return PREFIX + self._remote_instance_id() + f":{generation}:" + namespace + ":"

    def _remote_instance_id(self):
        if self._instance_id:
            return self._instance_id
        maintenance = os.path.join(os.path.dirname(self._directory()), ".maintenance")
        os.makedirs(maintenance, exist_ok=True)
        path = os.path.join(maintenance, "cache-instance-id")
        if not os.path.exists(path):
            temporary = f"{path}.{uuid.uuid4().hex}.tmp"
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                os.write(descriptor, uuid.uuid4().hex.encode("ascii"))
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            linked = False
            try:
                os.link(temporary, path)
                linked = True
            except FileExistsError:
                pass
            finally:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
            if linked:
                try:
                    directory_fd = os.open(maintenance, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                except OSError:
                    pass
        for _ in range(20):
            try:
                with open(path, "r", encoding="ascii") as stream:
                    value = stream.read().strip()
                if len(value) == 32 and all(char in "0123456789abcdef" for char in value):
                    self._instance_id = value
                    return value
            except OSError:
                pass
            time.sleep(0.01)
        raise OSError("缓存实例标识不可用")

    def _configuration(self):
        if self._settings_override is not None:
            return {**DEFAULT_SETTINGS, **self._settings_override}
        from config import Config
        configured = Config().get_config("cache") or {}
        return {**DEFAULT_SETTINGS, **configured} if isinstance(configured, dict) else dict(DEFAULT_SETTINGS)

    def _sync(self):
        settings = self._configuration()
        signature = tuple((key, str(settings.get(key))) for key in DEFAULT_SETTINGS)
        if self._signature is None:
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
        else:
            self._pending_settings = None if signature == self._signature else settings
        if self._frozen or (self._maintenance_enabled and STORAGE_JOBS.maintenance_gate("cache")
                            and not STORAGE_JOBS.owns_exclusive_lock()):
            return
        if self._settings.get("backend") in ("redis", "valkey") and self._client is None:
            if self._invalid or time.monotonic() < self._retry_at:
                return
            try:
                self._connect_remote()
                # 降级期间的删除/清空不能让旧远程值复活；本地新写入随后补回。
                for namespace in self._dirty:
                    backend = self._backends.get(namespace)
                    records = list(backend[0].records(strict=True)) if backend else []
                    self._clear_remote(namespace)
                    if backend:
                        cache_class = ValkeyCache if self._settings["backend"] == "valkey" else RedisCache
                        remote_cache = cache_class(
                            self._client, key_prefix=self._prefix(namespace), default_timeout=0)
                        for offset in range(0, len(records), 500):
                            self._set_remote_records(remote_cache, records[offset:offset + 500])
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
        self._cancel_recognition_refreshes()
        # 不向日志、接口暴露包含密码/地址的异常文本。
        self._reason = f"{self._settings['backend']} 连接不可用（{type(error).__name__}）"
        self._retry_at = time.monotonic() + float(self._settings["retry_interval"])
        if was_remote:
            logger.warning("%s，缓存降级到内存", self._reason)

    def _mark_dirty(self, namespace):
        if self._settings.get("backend") in ("redis", "valkey") and self._actual == "memory":
            self._dirty.add(namespace)

    @property
    def revision(self):
        if not self._maintenance_enabled:
            with self._lock:
                self._sync()
                return self._revision
        if self._is_frozen() and not STORAGE_JOBS.owns_exclusive_lock():
            with self._lock:
                return self._revision
        with STORAGE_JOBS.maintenance_lock(exclusive=False):
            with self._lock:
                self._sync()
                return self._revision

    def status(self):
        if not self._maintenance_enabled:
            with self._lock:
                return self._status_locked(sync=True)
        if self._is_frozen() and not STORAGE_JOBS.owns_exclusive_lock():
            with self._lock:
                return self._status_locked(sync=False)
        with STORAGE_JOBS.maintenance_lock(exclusive=False):
            with self._lock:
                return self._status_locked(sync=True)

    def _status_locked(self, sync=True):
        if sync:
            self._sync()
        configured_settings = self._configuration()
        configured = str(configured_settings.get("backend", "memory_disk"))
        path = os.path.join(self._directory(), "system.sqlite3") \
            if configured == "memory_disk" or self._actual == "memory_disk" else None
        configured_signature = tuple((key, str(configured_settings.get(key)))
                                     for key in DEFAULT_SETTINGS)
        pending_settings = self._pending_settings is not None or (
            self._signature is not None and configured_signature != self._signature)
        pending = configured if pending_settings else None
        configured_generation = configured_settings.get("storage_generation") or None
        active_generation = self._settings.get("storage_generation") or None
        connection = {key: configured_settings.get(key, DEFAULT_SETTINGS.get(key))
                      for key in ("host", "port", "db", "auth_mode",
                                  "connect_timeout", "retry_interval")}
        connection["username_set"] = bool(configured_settings.get("username"))
        connection["password_set"] = bool(configured_settings.get("password"))
        return {"configured_backend": configured, "backend": self._actual,
                "active_backend": self._actual, "pending_backend": pending,
                "restart_required": pending_settings,
                "generation": self._revision,
                "config_generation": active_generation,
                "active_generation": active_generation,
                "configured_generation": configured_generation,
                "pending_generation": configured_generation if pending_settings else None,
                "store_path": path, "memory_scope": "remote" if self._actual in {"redis", "valkey"}
                else "local", "connection": connection,
                "maintenance": self._is_frozen(),
                "fallback": bool(self._reason), "reason": self._reason}

    def _is_frozen(self):
        return self._frozen or (self._maintenance_enabled
                                and bool(STORAGE_JOBS.maintenance_gate("cache")))

    def freeze(self):
        with self._lock:
            self._frozen = True
            self._cancel_recognition_refreshes()

    def _cancel_recognition_refreshes(self):
        module = sys.modules.get("app.media.recognition.refresh_worker")
        if module is not None:
            module.cancel_refreshes(self)

    def unfreeze(self):
        with self._lock:
            self._frozen = False
            module = sys.modules.get("app.media.recognition.lease")
            if module is not None:
                module.resume_refreshes(self)

    def initialize(self):
        self._initialize_storage()
        self.rebuild_recognition_indexes()

    def _initialize_storage(self):
        """在后台服务启动前完成本地缓存格式升级和已知命名空间导入。"""
        with STORAGE_JOBS.maintenance_lock(exclusive=True):
            if STORAGE_JOBS.maintenance_gate("cache"):
                return
            with self._lock, self._layout_guard():
                self._sync()
                if self._invalid:
                    raise ValueError("缓存后端配置无效，无法升级缓存存储")
                self._prepare_storage()
                self._cleanup_retired_legacy_sources()
                path = os.path.join(self._directory(), "system.sqlite3")
                configured_backend = self._settings.get("backend")
                if configured_backend == "memory_disk" and self._actual == "memory_disk":
                    for namespace in CACHE_NAMESPACE_REGISTRY:
                        self._migrate(namespace, _SQLiteCache(path, namespace))
                    self._retire_migration_markers()
                    return

                state_path = self._layout_state_path()
                state = self._read_layout_state(state_path)
                imports = state.setdefault("imports", {})
                if not isinstance(imports, dict):
                    raise sqlite3.DatabaseError("缓存迁移记录中的命名空间状态无效")
                current_sqlite = self._is_current_sqlite(path)
                if state.get("legacy_sources") == "retired" and not current_sqlite:
                    return
                has_legacy_sources = self._has_legacy_sources()
                needs_legacy_import = state.get("legacy_sources") != "retired" and has_legacy_sources and any(
                    imports.get(namespace) not in {"imported", "empty"}
                    for namespace in CACHE_NAMESPACE_REGISTRY)
                if not current_sqlite and not has_legacy_sources:
                    for namespace in CACHE_NAMESPACE_REGISTRY:
                        imports.setdefault(namespace, "empty")
                    self._write_layout_state(state_path, state)
                    self._retire_migration_markers()
                    return
                staging_path = os.path.join(self._directory(), ".staging", "legacy-import.sqlite3")
                self._remove_staging_database(staging_path)
                fallback_remote = (configured_backend in {"redis", "valkey"}
                                   and self._actual != configured_backend)
                original_retry_at = self._retry_at
                if fallback_remote:
                    # 让这次启动导入保持在同一降级代次，不在批次中途重连并丢弃内存写入。
                    self._retry_at = float("inf")
                completed_imports = {}
                try:
                    for namespace in CACHE_NAMESPACE_REGISTRY:
                        legacy = _SQLiteCache(staging_path, namespace)
                        if needs_legacy_import:
                            self._migrate(namespace, legacy, mark_imported=False)
                        if current_sqlite:
                            batch = []
                            for record in _SQLiteCache(path, namespace).records(strict=True):
                                if record[2] is None or record[2] > time.time():
                                    batch.append((self._key(record[0]), record))
                                if len(batch) == 500:
                                    legacy.set_many(batch)
                                    batch.clear()
                            if batch:
                                legacy.set_many(batch)
                        if namespace == "wallpaper":
                            best = None
                            for record in legacy.records(strict=True):
                                if best is None or (record[2] or 0, self._key(record[0])) > (
                                        best[2] or 0, self._key(best[0])):
                                    best = record
                            legacy.clear()
                            if best is not None:
                                legacy.set_many([(self._key(best[0]), best)])
                        imported_any = False
                        for key, value, expires in legacy.records(strict=True):
                            if expires is not None and expires <= time.time():
                                continue
                            imported_any = True
                            if configured_backend in {"redis", "valkey"} and self._actual == configured_backend:
                                if self.get(namespace, key) is None:
                                    self.put(namespace, key, value, expires)
                            else:
                                self.put(namespace, key, value, expires)
                        completed_imports[namespace] = "imported" if imported_any else "empty"
                finally:
                    if fallback_remote:
                        self._retry_at = original_retry_at

                if fallback_remote or (configured_backend in {"redis", "valkey"}
                                       and self._actual != configured_backend):
                    self._remove_staging_database(staging_path)
                    return

                imports.update(completed_imports)
                state["legacy_namespace_states"] = {
                    namespace: imports.get(namespace, "pending")
                    for namespace in CACHE_NAMESPACE_REGISTRY
                }
                self._write_layout_state(state_path, state)
                if current_sqlite and os.path.exists(path):
                    self._archive_current_database(path)
                self._retire_migration_markers()
                self._remove_staging_database(staging_path)

    def _has_legacy_sources(self):
        directory = self._directory()
        if any(os.path.isfile(path) for path in self._legacy_sqlite_paths):
            return True
        if os.path.isfile(os.path.join(os.path.dirname(directory), "tmdb.dat")):
            return True
        for namespace in CACHE_NAMESPACE_REGISTRY:
            if os.path.isdir(os.path.join(directory, "files", self._key(namespace))):
                return True
        markers = os.path.join(directory, "migrations")
        if os.path.isdir(markers):
            with os.scandir(markers) as entries:
                return next(entries, None) is not None
        return False

    @staticmethod
    def _remove_staging_database(path):
        for filename in (path, path + "-wal", path + "-shm"):
            try:
                os.unlink(filename)
            except FileNotFoundError:
                pass

    def _archive_current_database(self, path):
        staging = os.path.join(self._directory(), ".staging")
        os.makedirs(staging, exist_ok=True)
        archived = os.path.join(staging, "legacy-current-system.sqlite3")
        if os.path.exists(archived):
            archived = os.path.join(staging, f"legacy-current-system-{time.time_ns()}.sqlite3")
        os.replace(path, archived)
        for suffix in ("-wal", "-shm"):
            sidecar = path + suffix
            if os.path.exists(sidecar):
                os.replace(sidecar, archived + suffix)

    def _directory(self):
        if self.path is None:
            from config import Config
            self.path = os.path.join(Config().get_config_path(), "cache")
        return self.path

    @contextmanager
    def _backend_layout_guard(self):
        if self._actual != "memory_disk":
            yield
            return
        guard = self._layout_guard()
        try:
            guard.__enter__()
        except OSError as error:
            self._reason = f"磁盘缓存不可用（{type(error).__name__}）"
            logger.warning("%s，使用内存缓存", self._reason)
            self._actual = "memory"
            self._backends.clear()
            self._revision += 1
            yield
        else:
            try:
                yield
            finally:
                guard.__exit__(None, None, None)

    def _backend(self, namespace):
        self._sync()
        if namespace not in self._backends:
            with self._backend_layout_guard():
                memory = _UnboundedSimpleCache(threshold=0, default_timeout=0)
                disk = None
                if self._actual == "memory_disk":
                    try:
                        self._prepare_storage()
                        disk = _SQLiteCache(os.path.join(self._directory(), "system.sqlite3"), namespace)
                    except (OSError, sqlite3.DatabaseError) as error:
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
                    except (OSError, sqlite3.DatabaseError, ValueError):
                        logger.warning("旧缓存导入失败：%s", namespace)
        memory, disk = self._backends[namespace]
        if disk is not None:
            stamp = self._disk_stamp(disk._path)
            previous = self._disk_stamps.get(namespace)
            if previous is not None and previous != stamp:
                # SQLite 可由其他进程写入；丢弃本进程的副本后再从持久层读取。
                memory.clear()
                self._revision += 1
            self._disk_stamps[namespace] = stamp
        return self._backends[namespace]

    @staticmethod
    def _disk_stamp(path):
        stamp = []
        for filename in (path, path + "-wal"):
            try:
                item = os.stat(filename)
                stamp.append((item.st_mtime_ns, item.st_size))
            except FileNotFoundError:
                stamp.append(None)
        return tuple(stamp)

    def _migrate(self, namespace, disk, mark_imported=True):
        """导入旧文件/SQLite 缓存；完成标记保存在清理缓存不会删除的目录。"""
        state_path = self._layout_state_path()
        state = self._read_layout_state(state_path)
        imports = state.setdefault("imports", {})
        if not isinstance(imports, dict):
            raise sqlite3.DatabaseError("缓存迁移记录中的命名空间状态无效")
        if imports.get(namespace) in {"imported", "empty"}:
            return
        if state.get("legacy_sources") == "retired":
            imports[namespace] = "empty"
            self._write_layout_state(state_path, state)
            return
        root = os.path.dirname(self._directory())
        old_marker = os.path.join(self._directory(), "migrations", self._key(namespace))
        had_import_marker = os.path.exists(old_marker)
        staging = os.path.join(self._directory(), ".staging")
        os.makedirs(staging, exist_ok=True)
        conflict_path = os.path.join(staging, f"legacy-conflicts-{uuid.uuid4().hex}.sqlite3")
        conflict_connection = sqlite3.connect(conflict_path, timeout=10)
        conflict_connection.execute("""CREATE TABLE legacy_import_records (
            namespace TEXT NOT NULL,
            cache_key TEXT NOT NULL,
            record BLOB NOT NULL,
            PRIMARY KEY(namespace, cache_key)
        )""")
        conflict_connection.execute("PRAGMA synchronous=NORMAL")

        def add_record(key, value, expires, source):
            wire_key = self._key(key)
            record = (key, value, expires)
            encoded = pickle.dumps(record, protocol=4)
            previous = conflict_connection.execute(
                "SELECT record FROM legacy_import_records WHERE namespace=? AND cache_key=?",
                (namespace, wire_key)).fetchone()
            if previous is not None and previous[0] != encoded:
                raise ValueError(f"缓存命名空间 {namespace} 在多个旧来源中存在冲突：{source}")
            conflict_connection.execute(
                "INSERT OR IGNORE INTO legacy_import_records(namespace, cache_key, record) VALUES (?, ?, ?)",
                (namespace, wire_key, encoded))

        try:
            files_directory = os.path.join(self._directory(), "files", self._key(namespace))
            if os.path.isdir(files_directory):
                try:
                    for key, value, expires in _DiskCache(
                            files_directory, threshold=0, default_timeout=0).records(strict=True):
                        add_record(key, value, expires, "cache/files")
                except (OSError, ValueError) as error:
                    raise ValueError(f"无法完整读取旧文件缓存：{namespace}") from error
            if not had_import_marker:
                for filename in self._legacy_sqlite_paths:
                    if not os.path.isfile(filename):
                        continue
                    connection = sqlite3.connect(self._readonly_uri(filename), uri=True, timeout=1)
                    try:
                        for payload, expires in connection.execute(
                                "SELECT payload, expires FROM cache_entries WHERE namespace=? ORDER BY rowid",
                                (namespace,)):
                            try:
                                key, value = pickle.loads(payload)
                            except Exception as error:
                                raise sqlite3.DatabaseError(
                                    f"旧缓存命名空间 {namespace} 包含损坏载荷") from error
                            add_record(key, value, expires, filename)
                    except sqlite3.DatabaseError as error:
                        raise sqlite3.DatabaseError(f"无法读取旧缓存数据库：{filename}") from error
                    finally:
                        connection.close()
                metadata = os.path.join(root, "tmdb.dat")
                if namespace == "tmdb_metadata" and os.path.isfile(metadata):
                    try:
                        with open(metadata, "rb") as stream:
                            values = pickle.load(stream)
                        if not isinstance(values, dict):
                            raise ValueError("旧 TMDB 缓存结构无效")
                        for key, value in values.items():
                            if not isinstance(value, dict):
                                raise ValueError("旧 TMDB 缓存条目结构无效")
                            if str(value.get("id")) != "0":
                                add_record(key, value, None, "tmdb.dat")
                    except Exception as error:
                        raise ValueError("旧 TMDB 缓存读取或结构校验失败") from error
            conflict_connection.commit()
            row = conflict_connection.execute(
                "SELECT COUNT(*) FROM legacy_import_records WHERE namespace=?", (namespace,)).fetchone()
            record_count = row[0] if row else 0
            cursor = conflict_connection.execute(
                "SELECT cache_key, record FROM legacy_import_records WHERE namespace=? ORDER BY cache_key",
                (namespace,))
            batch = []
            while True:
                rows = cursor.fetchmany(500)
                if not rows:
                    break
                for wire_key, payload in rows:
                    try:
                        key, value, expires = pickle.loads(payload)
                    except Exception as error:
                        raise sqlite3.DatabaseError("旧缓存冲突索引载荷损坏") from error
                    if expires is None or expires > time.time():
                        batch.append((wire_key, (key, value, expires)))
                if len(batch) >= 500:
                    disk.set_many(batch)
                    batch.clear()
            if batch:
                disk.set_many(batch)
            if mark_imported:
                imports[namespace] = "imported" if record_count else "empty"
                state["legacy_namespace_states"] = {
                    name: imports.get(name, "pending") for name in CACHE_NAMESPACE_REGISTRY
                }
                self._write_layout_state(state_path, state)
        finally:
            conflict_connection.close()
            self._remove_staging_database(conflict_path)

    def _layout_state_path(self):
        return os.path.join(os.path.dirname(self._directory()), ".maintenance", "cache-layout.json")

    @staticmethod
    def _legacy_file_identity(path):
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode):
            return None
        return {"device": info.st_dev, "inode": info.st_ino,
                "size": info.st_size, "mtime_ns": info.st_mtime_ns}

    def _legacy_retirement_records(self):
        """登记已知旧缓存来源及身份，避免清理后来替换的新文件。"""
        root = os.path.realpath(os.path.dirname(self._directory()))
        cache_directory = os.path.realpath(self._directory())
        candidates = {
            os.path.join(root, "system.sqlite3"),
            os.path.join(root, "tmdb.dat"),
        }
        for path in getattr(self, "_legacy_sqlite_paths", ()):
            candidates.add(path)

        known_names = {self._key(namespace) for namespace in CACHE_NAMESPACE_REGISTRY}
        files_directory = os.path.join(cache_directory, "files")
        if os.path.isdir(files_directory) and not os.path.islink(files_directory):
            for namespace in known_names:
                namespace_directory = os.path.join(files_directory, namespace)
                if not os.path.isdir(namespace_directory) or os.path.islink(namespace_directory):
                    continue
                for current, directories, filenames in os.walk(namespace_directory, followlinks=False):
                    directories[:] = [name for name in directories
                                      if not os.path.islink(os.path.join(current, name))]
                    candidates.update(os.path.join(current, name) for name in filenames
                                      if not os.path.islink(os.path.join(current, name)))

        migrations = os.path.join(cache_directory, "migrations")
        if os.path.isdir(migrations) and not os.path.islink(migrations):
            try:
                for entry in os.scandir(migrations):
                    if entry.name in known_names and entry.is_file(follow_symlinks=False):
                        candidates.add(entry.path)
            except OSError:
                logger.warning("无法完整枚举旧缓存标记目录：%s", migrations)

        staging = os.path.join(cache_directory, ".staging")
        if os.path.isdir(staging) and not os.path.islink(staging):
            try:
                for entry in os.scandir(staging):
                    if (entry.is_file(follow_symlinks=False)
                            and (entry.name in {"legacy-system.sqlite3", "legacy-root-system.sqlite3"}
                                 or entry.name.startswith("legacy-system.original")
                                 or entry.name.startswith("legacy-current-system"))):
                        candidates.add(entry.path)
            except OSError:
                logger.warning("无法完整枚举旧缓存快照目录：%s", staging)

        records = []
        for path in sorted(candidates):
            try:
                identity = self._legacy_file_identity(path)
            except OSError as error:
                logger.warning("无法记录旧缓存来源身份：%s：%s", path, error)
                continue
            if identity is None:
                continue
            absolute = os.path.abspath(path)
            try:
                contained = os.path.commonpath((root, absolute)) == root
            except ValueError:
                contained = False
            if not contained:
                continue
            records.append({"path": os.path.relpath(absolute, root),
                            "identity": identity, "status": "pending"})
        return records

    def _record_legacy_layout(self):
        """持久记录转换前可识别的来源身份和每个命名空间的导入状态。"""
        state_path = self._layout_state_path()
        state = self._read_layout_state(state_path)
        if state.get("legacy_sources") == "retired":
            return
        imports = state.get("imports") if isinstance(state.get("imports"), dict) else {}
        inventory = state.get("legacy_source_inventory")
        if not isinstance(inventory, list):
            inventory = []
        known = {record.get("path"): record for record in inventory
                 if isinstance(record, dict) and isinstance(record.get("path"), str)}
        for record in self._legacy_retirement_records():
            previous = known.get(record["path"])
            if previous is None:
                inventory.append(record)
                known[record["path"]] = record
            elif previous.get("identity") != record.get("identity"):
                changes = state.setdefault("legacy_source_changes", [])
                changes.append({"path": record["path"],
                                "previous": previous.get("identity"),
                                "observed": record.get("identity"),
                                "detected_at": time.time()})
        if not state.get("legacy_sources_detected_at"):
            state["legacy_sources_detected_at"] = time.time()
        state["legacy_source_inventory"] = inventory
        state["legacy_namespace_states"] = {
            name: imports.get(name, "pending") for name in CACHE_NAMESPACE_REGISTRY
        }
        state["layout_version"] = max(2, int(state.get("layout_version") or 0))
        self._write_layout_state(state_path, state)

    def _cleanup_retired_legacy_sources(self, now=None):
        state_path = self._layout_state_path()
        state = self._read_layout_state(state_path)
        if state.get("legacy_sources") != "retired":
            return 0
        retired_at = float(state.get("legacy_sources_retired_at") or 0)
        current_time = time.time() if now is None else float(now)
        if not retired_at or current_time - retired_at < _LEGACY_SOURCE_RETENTION_SECONDS:
            return 0

        root = os.path.realpath(os.path.dirname(self._directory()))
        records = state.get("retired_legacy_files") or []
        removed = 0
        changed = False
        for record in records:
            if record.get("status") != "pending":
                continue
            relative = record.get("path")
            if not isinstance(relative, str) or os.path.isabs(relative):
                record["status"] = "preserved_invalid_path"
                changed = True
                continue
            path = os.path.abspath(os.path.join(root, relative))
            try:
                contained = os.path.commonpath((root, path)) == root
            except ValueError:
                contained = False
            real_parent = os.path.realpath(os.path.dirname(path))
            try:
                parent_contained = os.path.commonpath((root, real_parent)) == root
            except ValueError:
                parent_contained = False
            if not contained or not parent_contained or os.path.islink(path):
                record["status"] = "preserved_invalid_path"
                changed = True
                continue
            try:
                current_identity = self._legacy_file_identity(path)
            except OSError as error:
                record["last_error"] = str(error)[:500]
                logger.warning("旧缓存来源暂时不可访问，清理待重试：%s：%s", path, error)
                changed = True
                continue
            if current_identity is None:
                record["status"] = ("preserved_changed" if os.path.lexists(path)
                                     else "removed")
                changed = True
                continue
            if current_identity != record.get("identity"):
                record["status"] = "preserved_changed"
                changed = True
                continue
            try:
                os.unlink(path)
                record["status"] = "removed"
                removed += 1
                changed = True
                self._cleanup_empty_legacy_parents(path, root)
            except OSError as error:
                record["last_error"] = str(error)[:500]
                logger.warning("旧缓存来源清理待重试：%s：%s", path, error)
                changed = True

        if changed:
            state["retired_legacy_files"] = records
            self._write_layout_state(state_path, state)
        return removed

    def _cleanup_empty_legacy_parents(self, path, root):
        cache_directory = os.path.realpath(self._directory())
        safe_roots = (os.path.join(cache_directory, "files"),
                      os.path.join(cache_directory, ".staging"))
        parent = os.path.dirname(path)
        while parent not in safe_roots:
            try:
                if os.path.commonpath((root, parent)) != root:
                    return
                safe_root = next((candidate for candidate in safe_roots
                                  if os.path.commonpath((candidate, parent)) == candidate), None)
            except ValueError:
                return
            if safe_root is None or parent == safe_root or os.path.islink(parent):
                return
            try:
                os.rmdir(parent)
            except OSError:
                return
            parent = os.path.dirname(parent)
        try:
            os.rmdir(parent)
        except OSError:
            pass

    @staticmethod
    def _read_layout_state(path):
        try:
            with open(path, "r", encoding="utf-8") as stream:
                state = json.load(stream)
            return state if isinstance(state, dict) else {"version": 1, "imports": {}}
        except FileNotFoundError:
            return {"version": 1, "imports": {}}
        except (OSError, ValueError):
            raise sqlite3.DatabaseError("缓存迁移记录损坏")

    @staticmethod
    def _write_layout_state(path, state):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temporary = f"{path}.tmp"
        with open(temporary, "w", encoding="utf-8") as stream:
            json.dump(state, stream, ensure_ascii=False, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)

    def _retire_migration_markers(self):
        directory = os.path.join(self._directory(), "migrations")
        state_path = self._layout_state_path()
        state = self._read_layout_state(state_path)
        imports = state.setdefault("imports", {})
        if not isinstance(imports, dict):
            raise sqlite3.DatabaseError("缓存迁移记录中的命名空间状态无效")
        if not all(imports.get(namespace) in {"imported", "empty"}
                   for namespace in CACHE_NAMESPACE_REGISTRY):
            return
        known = {self._key(namespace) for namespace in CACHE_NAMESPACE_REGISTRY}
        retiring_legacy_sources = state.get("legacy_sources") != "retired"
        retired_files = (self._legacy_retirement_records()
                         if retiring_legacy_sources else None)
        unknown = []
        if os.path.isdir(directory):
            with os.scandir(directory) as entries:
                for entry in entries:
                    if entry.name not in known or not entry.is_file(follow_symlinks=False):
                        unknown.append(entry.name)
                        continue
                    os.unlink(entry.path)
        if os.path.isdir(directory) and not unknown:
            os.rmdir(directory)
        if not unknown:
            state["migration_markers"] = "cleaned"
        else:
            logger.warning("缓存迁移目录包含未知内容，保留：%s", ", ".join(sorted(unknown)))
            state["migration_markers"] = "unknown_preserved"
        state["legacy_sources"] = "retired"
        if retiring_legacy_sources:
            state["legacy_sources_retired_at"] = time.time()
            state["retired_legacy_files"] = retired_files
        self._write_layout_state(state_path, state)

    @contextmanager
    def _layout_guard(self):
        """Publish a complete cache schema before another process inspects it."""
        if getattr(self._layout_lock_state, "depth", 0):
            self._layout_lock_state.depth += 1
            try:
                yield
            finally:
                self._layout_lock_state.depth -= 1
            return
        from app.utils.storage_jobs import fcntl, msvcrt
        directory = self._directory()
        os.makedirs(directory, exist_ok=True)
        descriptor = os.open(os.path.join(directory, ".layout.lock"), os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(descriptor, "a+b") as stream:
            deadline = time.monotonic()+10
            while True:
                try:
                    if fcntl is not None:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    elif msvcrt is not None:  # pragma: no cover - Windows
                        stream.seek(0)
                        if not stream.read(1):
                            stream.write(b"0")
                            stream.flush()
                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:  # pragma: no cover - unsupported platform
                        raise RuntimeError("cache layout locking unavailable")
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise OSError("cache layout lock unavailable") from None
                    time.sleep(.01)
            try:
                self._layout_lock_state.depth = 1
                yield
            finally:
                self._layout_lock_state.depth = 0
                if fcntl is not None:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
                else:  # pragma: no cover - Windows
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)

    def _prepare_storage(self):
        if self._storage_ready:
            return
        with self._layout_guard():
            if self._storage_ready:
                return
            try:
                self._prepare_storage_layout()
                if self._actual == "memory_disk":
                    # Opening SQLite creates a file before CREATE/format_version
                    # finish. Keep that entire interval behind the same lock.
                    _SQLiteCache(os.path.join(self._directory(), "system.sqlite3"), "__layout__")
            except BaseException:
                self._storage_ready = False
                raise

    def _prepare_storage_layout(self):
        if self._storage_ready:
            return
        directory = self._directory()
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, "system.sqlite3")
        root = os.path.dirname(directory)
        self._legacy_sqlite_paths = []
        if os.path.isfile(path) and not self._is_current_sqlite(path):
            staging = os.path.join(directory, ".staging")
            os.makedirs(staging, exist_ok=True)
            legacy = os.path.join(staging, "legacy-system.sqlite3")
            if not self._valid_sqlite_file(legacy):
                # backup() 把已提交 WAL 内容合入只读快照，再隔离原文件。
                source = sqlite3.connect(self._readonly_uri(path), uri=True, timeout=5)
                temporary = legacy + ".tmp"
                snapshot = sqlite3.connect(temporary, timeout=5)
                try:
                    source.backup(snapshot)
                finally:
                    snapshot.close()
                    source.close()
                if not self._valid_sqlite_file(temporary):
                    raise sqlite3.DatabaseError("旧缓存 SQLite 快照完整性检查失败")
                os.replace(temporary, legacy)
            if os.path.exists(path):
                original = os.path.join(staging, "legacy-system.original.sqlite3")
                if os.path.exists(original):
                    original = os.path.join(staging, f"legacy-system.original-{time.time_ns()}.sqlite3")
                os.replace(path, original)
                for suffix in ("-wal", "-shm"):
                    sidecar = path + suffix
                    if os.path.exists(sidecar):
                        os.replace(sidecar, original + suffix)
            self._legacy_sqlite_paths.insert(0, legacy)
        root_database = os.path.join(root, "system.sqlite3")
        state = self._read_layout_state(self._layout_state_path())
        imports = state.get("imports", {}) if isinstance(state.get("imports"), dict) else {}
        needs_import = (state.get("legacy_sources") != "retired"
                        and any(imports.get(namespace) not in {"imported", "empty"}
                                for namespace in CACHE_NAMESPACE_REGISTRY))
        if needs_import and os.path.isfile(root_database):
            staging = os.path.join(directory, ".staging")
            os.makedirs(staging, exist_ok=True)
            root_snapshot = os.path.join(staging, "legacy-root-system.sqlite3")
            if not self._valid_sqlite_file(root_snapshot):
                source = sqlite3.connect(self._readonly_uri(root_database), uri=True, timeout=5)
                temporary = root_snapshot + ".tmp"
                snapshot = sqlite3.connect(temporary, timeout=5)
                try:
                    source.backup(snapshot)
                finally:
                    snapshot.close()
                    source.close()
                if not self._valid_sqlite_file(temporary):
                    os.unlink(temporary)
                    raise sqlite3.DatabaseError("根目录旧缓存 SQLite 快照完整性检查失败")
                os.replace(temporary, root_snapshot)
            if not self._valid_sqlite_file(root_snapshot):
                raise sqlite3.DatabaseError("根目录旧缓存 SQLite 快照完整性检查失败")
            self._legacy_sqlite_paths.append(root_snapshot)
        self._record_legacy_layout()
        self._storage_ready = True

    @staticmethod
    def _is_current_sqlite(path):
        try:
            connection = sqlite3.connect(CacheStore._readonly_uri(path), uri=True, timeout=1)
            try:
                row = connection.execute(
                    "SELECT value FROM cache_metadata_v2 WHERE key='format_version'").fetchone()
                return bool(row and row[0] == str(_SQLiteCache.FORMAT_VERSION))
            finally:
                connection.close()
        except sqlite3.DatabaseError:
            return False

    @staticmethod
    def _valid_sqlite_file(path):
        if not os.path.isfile(path):
            return False
        try:
            connection = sqlite3.connect(CacheStore._readonly_uri(path), uri=True, timeout=1)
            try:
                return connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
            finally:
                connection.close()
        except sqlite3.DatabaseError:
            return False

    @staticmethod
    def _readonly_uri(path):
        return f"{Path(path).resolve().as_uri()}?mode=ro"

    @staticmethod
    def _timeout(expires):
        return max(1, math.ceil(expires - time.time()) + 1) if expires is not None else 0

    def _call(self, namespace, operation, blocked=None):
        def invoke(memory, disk):
            result = operation(memory, disk)
            if disk is not None:
                self._disk_stamps[namespace] = self._disk_stamp(disk._path)
            return result

        if not self._maintenance_enabled:
            with self._lock:
                self._sync()
                memory, disk = self._backend(namespace)
                try:
                    return invoke(memory, disk)
                except REMOTE_ERRORS as error:
                    if self._actual not in {"redis", "valkey"}:
                        raise
                    self._fallback(error)
                    memory, disk = self._backend(namespace)
                    return invoke(memory, disk)
        if self._is_frozen() and not STORAGE_JOBS.owns_exclusive_lock():
            return blocked
        with STORAGE_JOBS.maintenance_lock(exclusive=False):
            with self._lock:
                self._sync()
                if self._is_frozen() and not STORAGE_JOBS.owns_exclusive_lock():
                    return blocked
                memory, disk = self._backend(namespace)
                try:
                    return invoke(memory, disk)
                except REMOTE_ERRORS as error:
                    if self._actual not in {"redis", "valkey"}:
                        raise
                    self._fallback(error)
                    memory, disk = self._backend(namespace)
                    return invoke(memory, disk)

    def get(self, namespace, key):
        if namespace in V3_NAMESPACES:
            value = self.envelope_get(namespace, key)
            return (key, value, value["stale_until"]) if value is not None else None
        wire_key = self._key(key)

        def read(memory, disk):
            record = memory.get(wire_key)
            if record is None and disk is not None:
                record = disk.get(wire_key, cleanup=not self._is_frozen())
                if record is not None:
                    memory.set(wire_key, record, timeout=self._timeout(record[2]))
            if record is not None and record[2] is not None and record[2] <= time.time():
                if self._is_frozen():
                    return None
                if disk is not None:
                    disk.delete(wire_key)
                if memory.delete(wire_key) and disk is None:
                    self._mark_dirty(namespace)
                return None
            return record
        return self._call(namespace, read)

    def put(self, namespace, key, value, expires=None):
        if namespace in V3_NAMESPACES:
            return self.envelope_restore(namespace, key, value)
        record, wire_key = (key, value, expires), self._key(key)

        def write(memory, disk):
            if self._is_frozen() and (self._frozen or not STORAGE_JOBS.owns_exclusive_lock()):
                return False
            if namespace == "wallpaper":
                if self._actual in {"redis", "valkey"}:
                    self._clear_remote(namespace)
                elif disk is not None:
                    disk.clear()
                    memory.clear()
                else:
                    memory.clear()
                    self._mark_dirty(namespace)
            if disk is not None:
                if not disk.set(wire_key, record, timeout=self._timeout(expires)):
                    raise OSError("缓存写入失败")
                if not memory.set(wire_key, record, timeout=self._timeout(expires)):
                    raise OSError("缓存内存层写入失败")
            else:
                self._mark_dirty(namespace)
                if not memory.set(wire_key, record, timeout=self._timeout(expires)):
                    raise OSError("缓存写入失败")
        self._call(namespace, write, blocked=False)

    def put_many(self, namespace, records):
        if namespace in V3_NAMESPACES:
            return sum(bool(self.envelope_restore(namespace, key, value)) for key, value, _expires in records)
        records = list(records)
        if namespace == "wallpaper" and len(records) > 1:
            records = [max(records, key=lambda record: (record[2] or 0,
                                                         self._key(record[0])))]

        def write(memory, disk):
            if self._is_frozen() and (self._frozen or not STORAGE_JOBS.owns_exclusive_lock()):
                return 0
            if namespace == "wallpaper":
                if self._actual in {"redis", "valkey"}:
                    self._clear_remote(namespace)
                elif disk is not None:
                    disk.clear()
                    memory.clear()
                else:
                    memory.clear()
                    self._mark_dirty(namespace)
            written = 0
            for offset in range(0, len(records), 500):
                batch = records[offset:offset + 500]
                if disk is not None:
                    written += disk.set_many([
                        (self._key(key), (key, value, expires))
                        for key, value, expires in batch])
                    for key, value, expires in batch:
                        if not memory.set(self._key(key), (key, value, expires),
                                          timeout=self._timeout(expires)):
                            raise OSError("缓存内存层批量写入失败")
                elif self._actual in {"redis", "valkey"}:
                    written += self._remote_set_many(memory, batch)
                else:
                    for key, value, expires in batch:
                        self._mark_dirty(namespace)
                        if memory.set(self._key(key), (key, value, expires),
                                      timeout=self._timeout(expires)):
                            written += 1
                        else:
                            raise OSError("缓存批量写入失败")
            return written
        return self._call(namespace, write, blocked=0)

    def checkpoint(self):
        """将活动本地缓存的 WAL 截断，发布固定数据库文件前调用。"""
        with self._lock:
            for _, disk in self._backends.values():
                if disk is None:
                    continue
                with disk._connection() as connection:
                    result = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                    if result and result[0]:
                        raise sqlite3.OperationalError("缓存 SQLite WAL 检查点未完成")

    def delete(self, namespace, key):
        if namespace in V3_NAMESPACES:
            return self.envelope_delete(namespace, key)
        def remove(memory, disk):
            if self._is_frozen() and (self._frozen or not STORAGE_JOBS.owns_exclusive_lock()):
                return False
            if disk is not None:
                result = disk.delete(self._key(key))
                return memory.delete(self._key(key)) or result
            result = memory.delete(self._key(key))
            if self._actual == "memory":
                self._mark_dirty(namespace)
            return result
        return self._call(namespace, remove, blocked=False)

    def _clear_remote(self, namespace):
        batch = []
        for key in self._client.scan_iter(match=self._prefix(namespace) + "*", count=500):
            batch.append(key)
            if len(batch) == 500:
                self._client.delete(*batch)
                batch.clear()
        if batch:
            self._client.delete(*batch)

    def _remote_set_many(self, memory, records):
        return self._set_remote_records(memory, records)

    def _set_remote_records(self, cache, records):
        pipeline = cache._write_client.pipeline(transaction=False)
        prefix = cache._get_prefix()
        for key, value, expires in records:
            timeout = cache._normalize_timeout(self._timeout(expires))
            pipeline.set(name=f"{prefix}{self._key(key)}",
                         value=cache.serializer.dumps((key, value, expires)),
                         ex=timeout if timeout != -1 else None)
        results = pipeline.execute()
        if not all(results):
            raise OSError("远程缓存批量写入失败")
        return len(results)

    def clear(self, namespace):
        if namespace in V3_NAMESPACES:
            return self.envelope_clear(namespace)
        def remove(memory, disk):
            if self._is_frozen() and (self._frozen or not STORAGE_JOBS.owns_exclusive_lock()):
                return False
            if self._actual in {"redis", "valkey"}:
                self._clear_remote(namespace)
            elif disk is not None:
                if not disk.clear():
                    raise OSError("清理磁盘缓存失败")
                memory.clear()
            else:
                memory.clear()
                self._mark_dirty(namespace)
        self._call(namespace, remove, blocked=False)

    def iter_records(self, namespace, strict=False):
        """在独占维护锁内流式读取快照，不触发过期清理或壁纸修剪。"""
        if not STORAGE_JOBS.owns_exclusive_lock():
            raise RuntimeError("流式缓存读取需要独占维护锁")
        if namespace in V3_NAMESPACES:
            yield from self.envelope_records(namespace)
            return
        with self._lock:
            self._sync()
            memory, disk = self._backend(namespace)
            actual, client = self._actual, self._client
            prefix = self._prefix(namespace) if actual in {"redis", "valkey"} else None

        if actual in {"redis", "valkey"}:
            def remote_records():
                keys = []
                for wire_key in client.scan_iter(match=prefix + "*", count=500):
                    keys.append(wire_key.decode()[len(prefix):])
                    if len(keys) == 500:
                        values = memory.get_many(*keys)
                        if strict and any(value is None for value in values):
                            raise ValueError(f"远程缓存命名空间 {namespace} 包含无法读取的条目")
                        yield from values
                        keys.clear()
                if keys:
                    values = memory.get_many(*keys)
                    if strict and any(value is None for value in values):
                        raise ValueError(f"远程缓存命名空间 {namespace} 包含无法读取的条目")
                    yield from values
            records = remote_records()
        elif disk is not None:
            records = disk.records(strict=strict)
        else:
            records = memory.records(strict=strict)

        now = time.time()
        for record in records:
            if not isinstance(record, tuple) or len(record) != 3:
                if strict:
                    raise ValueError(f"缓存命名空间 {namespace} 包含不支持的载荷")
                continue
            key, value, expires = record
            if expires is None or expires > now:
                yield record

    def load(self, namespace, strict=False):
        if namespace in V3_NAMESPACES:
            return self.envelope_records(namespace)
        def read(memory, disk):
            if self._actual in {"redis", "valkey"}:
                prefix = self._prefix(namespace)
                keys = [key.decode()[len(prefix):] for key in
                        self._client.scan_iter(match=prefix + "*", count=500)]
                records = memory.get_many(*keys) if keys else []
                if strict and any(record is None for record in records):
                    raise ValueError(f"远程缓存命名空间 {namespace} 包含无法读取的条目")
            elif disk is not None:
                records = list(disk.records(strict=strict))
            else:
                records = list(memory.records(strict=strict))
            result = {}
            for record in records:
                if not isinstance(record, tuple) or len(record) != 3:
                    if strict:
                        raise ValueError(f"缓存命名空间 {namespace} 包含不支持的载荷")
                    continue
                key, value, expires = record
                if expires is not None and expires <= time.time():
                    if self._is_frozen():
                        continue
                    if disk is not None:
                        disk.delete(self._key(key))
                    else:
                        if memory.delete(self._key(key)):
                            self._mark_dirty(namespace)
                else:
                    result[self._key(key)] = (key, value, expires)
            if namespace == "wallpaper" and len(result) > 1 and not self._is_frozen():
                keep_key, keep_record = max(
                    result.items(), key=lambda item: (item[1][2] or 0, item[0]))
                target = disk if disk is not None else memory
                for wire_key in result:
                    if wire_key != keep_key:
                        target.delete(wire_key)
                result = {keep_key: keep_record}
            return list(result.values())
        return self._call(namespace, read, blocked=[])

    def memory_info(self, namespace):
        if namespace in V3_NAMESPACES:
            records = self.envelope_records(namespace)
            size = sum(len(json.dumps(value).encode()) for _, value, _ in records)
            actual = self.capabilities()["actual_backend"]
            persistent_size = None
            if actual == "memory_disk":
                path = os.path.join(self._directory(), "system.sqlite3")
                persistent_size = sum(os.path.getsize(filename) for filename in
                                      (path, path + "-wal", path + "-shm")
                                      if os.path.exists(filename))
            # 远程缓存数据计入服务端内存，本地文件占用只用于 SQLite。
            return {"entries": len(records), "bytes": size,
                    "memory_bytes": size if actual != "memory_disk" else 0,
                    "store_bytes": persistent_size,
                    "backend": actual, "persistent": actual == "memory_disk"}
        def measure(memory, disk, actual, client, prefix):
            if disk is not None:
                entries, size = disk.stats()
                with memory._lock:
                    if not self._is_frozen():
                        memory._prune()
                    local_size = memory._estimated_memory
                persistent_size = sum(os.path.getsize(filename) for filename in
                                      (disk._path, disk._path + "-wal", disk._path + "-shm")
                                      if os.path.exists(filename))
            elif actual in {"redis", "valkey"}:
                entries = 0
                size = 0
                pipeline = client.pipeline(transaction=False)
                pending = []
                measure_memory = True
                for wire_key in client.scan_iter(match=prefix + "*", count=500):
                    entries += 1
                    if not measure_memory:
                        continue
                    pipeline.memory_usage(wire_key)
                    pending.append(wire_key)
                    if len(pending) == 500:
                        try:
                            values = pipeline.execute()
                            size += sum(value or 0 for value in values)
                        except (redis.exceptions.ResponseError, valkey.exceptions.ResponseError):
                            size = None
                            measure_memory = False
                        pending.clear()
                if pending and measure_memory:
                    try:
                        values = pipeline.execute()
                        size += sum(value or 0 for value in values)
                    except (redis.exceptions.ResponseError, valkey.exceptions.ResponseError):
                        size = None
                local_size, persistent_size = 0, None
            else:
                with memory._lock:
                    if not self._is_frozen():
                        memory._prune()
                    entries = len(memory._cache)
                    local_size = memory._estimated_memory
                size, persistent_size = local_size, None
            actual = "memory_disk" if disk is not None else actual
            if actual == "memory_disk" and disk is None:
                actual = "memory"
            memory_bytes = size if actual in {"redis", "valkey"} else local_size
            return {"entries": entries, "memory_bytes": memory_bytes,
                    "bytes": size if disk is not None or size is not None else None,
                    "store_bytes": persistent_size,
                    "backend": actual, "persistent": actual == "memory_disk"}

        blocked = {"entries": 0, "memory_bytes": 0, "bytes": None, "store_bytes": None,
                   "backend": self._actual, "persistent": self._actual == "memory_disk"}
        if self._maintenance_enabled and self._is_frozen() and not STORAGE_JOBS.owns_exclusive_lock():
            return blocked
        lock = STORAGE_JOBS.maintenance_lock(exclusive=False) \
            if self._maintenance_enabled else nullcontext()
        with lock:
            if self._maintenance_enabled and self._is_frozen() and not STORAGE_JOBS.owns_exclusive_lock():
                return blocked
            with self._lock:
                self._sync()
                if self._maintenance_enabled and self._is_frozen() \
                        and not STORAGE_JOBS.owns_exclusive_lock():
                    return blocked
                memory, disk = self._backend(namespace)
                actual, client = self._actual, self._client
                prefix = self._prefix(namespace) if actual in {"redis", "valkey"} else None
            try:
                return measure(memory, disk, actual, client, prefix)
            except REMOTE_ERRORS as error:
                if actual not in {"redis", "valkey"}:
                    raise
                with self._lock:
                    if self._actual in {"redis", "valkey"}:
                        self._fallback(error)
                    memory, disk = self._backend(namespace)
                    actual, client = self._actual, self._client
                    prefix = self._prefix(namespace) if actual in {"redis", "valkey"} else None
                return measure(memory, disk, actual, client, prefix)

    def prune_expired(self, namespace):
        if namespace in V3_NAMESPACES:
            return self.envelope_prune(namespace)
        def prune(memory, disk):
            if self._is_frozen() and (self._frozen or not STORAGE_JOBS.owns_exclusive_lock()):
                return 0
            if disk is not None:
                return disk.prune_expired()
            if self._actual in {"redis", "valkey"}:
                return 0
            with memory._lock:
                removed = memory._prune()
            if removed:
                self._mark_dirty(namespace)
            return removed
        return self._call(namespace, prune, blocked=0)


STORE = CacheStore()
