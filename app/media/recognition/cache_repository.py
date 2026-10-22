"""使用选定的 CacheStore 实现四层 V3 缓存，不执行业务 SQL。"""

from dataclasses import asdict, is_dataclass
import hashlib
import json
import time
import uuid
from threading import Event, Thread
from contextlib import contextmanager
from contextvars import ContextVar

from app.media.recognition.contracts import CacheEnvelope, IdentityEntry
from app.media.recognition.records import recognition_abort_reason, recognition_config, recognition_remaining_seconds
from app.utils.cache_backend import STORE


LAYERS = ("identity", "search", "detail", "episode")
DEFAULT_TTLS = {"identity": 0, "search": 86400, "detail": 604800, "episode": 259200}
_hit_scope = ContextVar("recognition_v3_cache_hits", default=None)


@contextmanager
def cache_hit_scope():
    token = _hit_scope.set(set())
    try:
        yield
    finally:
        _hit_scope.reset(token)


class CacheRepository:
    def __init__(self, store=None, settings=None, clock=None):
        self.store = STORE if store is None else store
        self.settings = settings
        self.clock = time.time if clock is None else clock

    def _config(self):
        return self.settings if self.settings is not None else recognition_config()

    def enabled(self):
        return (self._config().get("recognition", {}).get("cache") or {}).get("enabled", True) is True

    @staticmethod
    def namespace(layer):
        if layer not in LAYERS:
            raise ValueError("unknown cache layer")
        return "recognition_" + layer

    @staticmethod
    def key(layer, parameters):
        if layer not in LAYERS or not isinstance(parameters, dict):
            raise ValueError("invalid cache key parameters")
        if layer == "identity" and (set(parameters) != {"title"} or
                not isinstance(parameters["title"], str) or not parameters["title"].strip()):
            raise ValueError("identity keys must contain only the work title")
        wire = json.dumps({"schema": 3, "layer": layer, "params": parameters},
                          sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
        return hashlib.sha256(wire.encode()).hexdigest()

    def get(self, layer, key, *, allow_stale=False, hit=True):
        remaining = recognition_remaining_seconds()
        if not self.enabled() or recognition_abort_reason() or remaining is not None and remaining <= 0:
            return None
        seen = _hit_scope.get()
        identity = (id(self.store), layer, key)
        touch = hit and (seen is None or identity not in seen)
        value = self.store.envelope_get(self.namespace(layer), key, allow_stale=allow_stale, hit=touch)
        if touch and seen is not None:
            seen.add(identity)
        if value is None:
            return None
        result = CacheEnvelope(**value)
        if result.storage_generation != self.store.capabilities()["storage_generation"]:
            return None
        return result

    def put(self, layer, key, payload, *, ttl=None, source_trace_id=None, override_version=0,
            request_params=None, expected=None, owner=None, fence=None):
        remaining = recognition_remaining_seconds()
        if not self.enabled() or recognition_abort_reason() or remaining is not None and remaining <= 0:
            return False
        config = self._config()
        if layer == "identity":
            if isinstance(payload, dict):
                payload = IdentityEntry(**payload)
            if not isinstance(payload, IdentityEntry) or payload.verification_level != "verified":
                raise ValueError("identity cache requires a verified identity")
        payload = asdict(payload) if is_dataclass(payload) else payload
        ttl = ((config.get("cache", {}).get("ttl_seconds") or {}).get("recognition_" + layer, DEFAULT_TTLS[layer])
               if ttl is None else ttl)
        if type(ttl) is not int or not 0 <= ttl <= 36500 * 86400:
            raise ValueError("TTL must be nonnegative integer seconds")
        grace = ((config.get("recognition", {}).get("cache_v3") or {}).get("stale_grace_seconds", 3600))
        if type(grace) is not int or grace < 0:
            raise ValueError("invalid stale grace")
        created_at = self.clock()
        fresh = created_at + ttl if ttl else None
        value = CacheEnvelope(layer, key, payload, uuid.uuid4().hex,
            self.store.capabilities()["storage_generation"], fresh_until=fresh,
            stale_until=fresh + grace if fresh is not None else None,
            source_trace_id=source_trace_id, override_version=override_version, request_params=request_params,
            created_at=created_at)
        return self.store.envelope_put(self.namespace(layer), key, value,
            expected=asdict(expected) if isinstance(expected, CacheEnvelope) else expected, owner=owner, fence=fence)

    def load(self, layer, key, loader, *, cancellation=None, budget=None, lease_seconds=30,
             ttl_for_payload=None, validator=None, **put_options):
        """共享存储支持跨进程 singleflight；内存缓存仅限当前进程。"""
        if cancellation is not None:
            cancellation.check()
        if budget is not None:
            budget.check()
        if not self.enabled():
            return loader()
        namespace, owner = self.namespace(layer), uuid.uuid4().hex
        def usable(cached):
            return (cached is not None and not cached.needs_revalidation and
                    (validator is None or validator(cached.payload)))
        waited = False
        while True:
            if cancellation is not None:
                cancellation.check()
            if budget is not None:
                budget.check()
            cached = self.get(layer, key, hit=not waited)
            if usable(cached):
                return cached.payload
            fence = self.store.lease_acquire(namespace, key, owner, lease_seconds)
            if fence:
                stopped = Event()
                def heartbeat():
                    while not stopped.wait(max(0.01, lease_seconds/3)):
                        try:
                            if not self.store.lease_renew(namespace, key, owner, fence, lease_seconds):
                                return
                        except Exception:
                            return
                renewal = Thread(target=heartbeat, name="recognition-cache-lease", daemon=True)
                renewal.start()
                try:
                    # 在 get 和 acquire 之间，可能已有值被发布。
                    cached = self.get(layer, key, hit=False)
                    if usable(cached):
                        return cached.payload
                    payload = loader()
                    if cancellation is not None:
                        cancellation.check()
                    if budget is not None:
                        budget.check()
                    options = dict(put_options)
                    if ttl_for_payload is not None:
                        options["ttl"] = ttl_for_payload(payload)
                    # 通过同一栅栏替换无效数据，但不
                    # 删除数据（删除操作也会使租约失效）。旧值
                    # 可能在 I/O 期间过期；有效栅栏仍允许我们
                    # 发布结果。较新的所有者始终具有优先权。
                    self.put(layer, key, payload, owner=owner, fence=fence, **options)
                    return payload
                finally:
                    stopped.set()
                    renewal.join(timeout=1)
                    self.store.lease_release(namespace, key, owner, fence)
            time.sleep(0.02)
            waited = True

    def clear(self, layer):
        return self.store.envelope_clear(self.namespace(layer))

    def snapshot(self):
        return {layer: self.store.envelope_records(self.namespace(layer)) for layer in LAYERS}

    def restore(self, snapshot):
        """绝对过期时间、版本、周期和命中次数都会保留；租约不会迁移。"""
        restored = 0
        for layer, records in snapshot.items():
            namespace = self.namespace(layer)
            for key, envelope, expires in records:
                if expires is not None and expires <= self.clock():
                    continue
                restored += bool(self.store.envelope_restore(namespace, key, envelope))
        return restored
