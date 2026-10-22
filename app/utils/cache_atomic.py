"""Backend-owned V3 envelopes and fenced leases, separate from legacy values.

The SQLite here is the existing cache/system.sqlite3, never the business DB.
Remote metadata is modified by Lua without rewriting the JSON payload on hits.
"""

from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
import sqlite3
import time
from app.utils.cache_rank import (
    CONTROL_FIELDS, REMOTE_INDEX, REMOTE_RANK, disk_index_write, disk_overlay, positive,
)



V3_NAMESPACES = tuple(f"recognition_{layer}" for layer in ("identity", "search", "detail", "episode"))

REMOTE_OPERATION = REMOTE_INDEX + r"""
local op, now = ARGV[1], tonumber(ARGV[2])
local value = redis.call('HGET', KEYS[1], 'data')
if not value then remove_index(ARGV[#ARGV]) end
local leaseOwner = redis.call('HGET', KEYS[2], 'owner')
local leaseUntil = tonumber(redis.call('HGET', KEYS[2], 'until') or '0')
local fence = tonumber(redis.call('HGET', KEYS[2], 'fence') or '0')
if op == 'lease' then
  if leaseUntil > now then return 0 end
  fence = fence + 1
  redis.call('HSET', KEYS[2], 'owner', ARGV[3], 'until', now+tonumber(ARGV[4]), 'fence', fence)
  return fence
elseif op == 'renew' or op == 'release' then
  if leaseOwner ~= ARGV[3] or fence ~= tonumber(ARGV[4]) or leaseUntil <= now then return 0 end
  redis.call('HSET', KEYS[2], 'until', op == 'release' and 0 or now+tonumber(ARGV[5]))
  return 1
end
if value and tonumber(redis.call('HGET', KEYS[1], 'stale') or '0') > 0
    and tonumber(redis.call('HGET', KEYS[1], 'stale')) <= now then
  remove_index(redis.call('HGET', KEYS[1], 'cache_key')); redis.call('DEL', KEYS[1]); value = false
end
if op == 'index' then
  if value then
    local e = cjson.decode(value)
    local m = redis.call('HMGET', KEYS[1], 'retry','failures','refresh_state','refresh_error','needs')
    e.next_retry_at=tonumber(m[1]) or e.next_retry_at or 0; e.refresh_failures=tonumber(m[2]) or e.refresh_failures or 0
    e.refresh_state=m[3] or e.refresh_state; e.refresh_error=m[4] or e.refresh_error
    e.needs_revalidation=m[5] == '1' or e.needs_revalidation == true
    index_entry(e, tonumber(redis.call('HGET', KEYS[1], 'hits')) or e.hit_count)
    return 1
  end
  return 0
end
if op == 'get' then
  if ARGV[3] == '1' then
    redis.call('HSETNX', KEYS[6], 'since', now)
    local fresh = value and tonumber(redis.call('HGET', KEYS[1], 'fresh') or '0')
    redis.call('HINCRBY', KEYS[6], value and (fresh == 0 or fresh > now) and 'hits' or 'miss', 1)
    if value and fresh > 0 and fresh <= now then redis.call('HINCRBY', KEYS[6], 'stale_reads', 1) end
  end
  if not value then return {} end
  local fresh = tonumber(redis.call('HGET', KEYS[1], 'fresh') or '0')
  if fresh > 0 and fresh <= now and ARGV[4] ~= '1' then return {} end
  if ARGV[3] == '1' and (fresh == 0 or fresh > now) then
    local hits = redis.call('HINCRBY', KEYS[1], 'hits', 1)
    redis.call('HSET', KEYS[1], 'last', now)
    local e = cjson.decode(value)
    if redis.call('HGET', KEYS[1], 'needs') ~= '1' then
      e.next_retry_at=tonumber(redis.call('HGET', KEYS[1], 'retry')) or 0
      e.refresh_failures=tonumber(redis.call('HGET', KEYS[1], 'failures')) or 0
      e.refresh_state=redis.call('HGET', KEYS[1], 'refresh_state') or 'idle'
      e.refresh_error=redis.call('HGET', KEYS[1], 'refresh_error') or ''
      index_entry(e, hits)
    end
  end
  return {value, redis.call('HGET', KEYS[1], 'hits'), redis.call('HGET', KEYS[1], 'last') or '', redis.call('HGET', KEYS[1], 'version'),
    redis.call('HGET', KEYS[1], 'created') or '', redis.call('HGET', KEYS[1], 'retry') or '0',
    redis.call('HGET', KEYS[1], 'failures') or '0', redis.call('HGET', KEYS[1], 'refresh_state') or 'idle',
    redis.call('HGET', KEYS[1], 'refresh_error') or '', redis.call('HGET', KEYS[1], 'needs') or '0'}
end
if op == 'delete' then
  if ARGV[3] ~= '' and (not value or redis.call('HGET', KEYS[1], 'version') ~= ARGV[3]
      or redis.call('HGET', KEYS[1], 'cycle') ~= ARGV[4] or redis.call('HGET', KEYS[1], 'generation') ~= ARGV[5]) then return 0 end
  remove_index(redis.call('HGET', KEYS[1], 'cache_key'))
  redis.call('HSET', KEYS[2], 'owner', '', 'until', 0, 'fence', fence+1)
  return redis.call('DEL', KEYS[1])
end
if op == 'failure' then
  if not value or redis.call('HGET', KEYS[1], 'version') ~= ARGV[3]
    or redis.call('HGET', KEYS[1], 'cycle') ~= ARGV[4] or redis.call('HGET', KEYS[1], 'generation') ~= ARGV[5]
    or leaseOwner ~= ARGV[6] or fence ~= tonumber(ARGV[7]) or leaseUntil <= now then return 0 end
  redis.call('HSET', KEYS[1], 'retry', now+tonumber(ARGV[8]), 'refresh_state', 'retry', 'refresh_error', ARGV[9])
  redis.call('HINCRBY', KEYS[1], 'failures', 1)
  return 1
end
if op ~= 'set' then return redis.error_reply('invalid V3 operation') end
local expected = ARGV[4]
if expected == 'absent' and value then return 0 end
if expected ~= '' and expected ~= 'absent' then
  if not value or redis.call('HGET', KEYS[1], 'version') ~= expected
    or redis.call('HGET', KEYS[1], 'cycle') ~= ARGV[5]
    or redis.call('HGET', KEYS[1], 'generation') ~= ARGV[6] then return 0 end
end
if ARGV[7] ~= '' and (leaseOwner ~= ARGV[7] or fence ~= tonumber(ARGV[8]) or leaseUntil <= now) then return 0 end
local e = cjson.decode(ARGV[3])
if e.stale_until ~= cjson.null and e.stale_until <= now then return 0 end
local version = e.payload_version
if ARGV[9] ~= '1' and value then version = tonumber(redis.call('HGET', KEYS[1], 'version'))+1 end
local stale = e.stale_until == cjson.null and 0 or e.stale_until
local fresh = e.fresh_until == cjson.null and 0 or e.fresh_until
redis.call('HSET', KEYS[1], 'data', ARGV[3], 'version', version,
  'cycle', e.cycle_id, 'generation', e.storage_generation, 'hits', e.hit_count,
  'last', e.last_access == cjson.null and '' or e.last_access, 'fresh', ARGV[10], 'stale', ARGV[11])
if stale > 0 then redis.call('PEXPIREAT', KEYS[1], math.ceil(stale*1000)) else redis.call('PERSIST', KEYS[1]) end
index_entry(e, e.hit_count)
return 1
"""


def _decode(data, hits=None, last=None, version=None, created=None, retry=None, failures=None, state=None, error=None, needs=None):
    result = json.loads(data)
    if hits is not None:
        result["hit_count"] = int(hits)
        result["last_access"] = float(last) if last not in (None, b"", "") else None
    if version is not None:
        result["payload_version"] = int(version)
    if retry is not None:
        result.update(created_at=float(created) if created not in (None, b"", "") else result.get("created_at"),
            next_retry_at=float(retry), refresh_failures=int(failures),
            refresh_state=state.decode() if isinstance(state, bytes) else state,
            refresh_error=(error.decode() if isinstance(error, bytes) else error) or None,
            needs_revalidation=needs in (b"1", "1", 1))
    return result


class AtomicCacheMixin:
    def envelope_read_stats(self, namespace):
        """Touch-enabled read counters since initialization; management peeks excluded."""
        if namespace not in V3_NAMESPACES:
            raise ValueError("not a V3 cache namespace")
        def read(_memory, disk):
            if self._actual in {"redis", "valkey"}:
                values = self._client.hmget(self._v3_prefix(namespace)+"read-stats", "hits", "miss", "stale_reads", "since")
            elif disk is not None:
                with self._v3_disk(disk) as connection:
                    values = connection.execute("SELECT hits,miss,stale_reads,since FROM recognition_cache_reads_v3 WHERE namespace=?", (namespace,)).fetchone() or (0, 0, 0, None)
            else:
                self._v3_memory()
                values = self._v3_read_stats.get(namespace, (0, 0, 0, None))
            return dict(hits=int(values[0] or 0), miss=int(values[1] or 0), stale_reads=int(values[2] or 0),
                        since=float(values[3]) if values[3] is not None else None)
        return self._call(namespace, read)

    def rebuild_recognition_indexes(self):
        """One-time remote cache-format upgrade at startup, never a refresh scan."""
        if self.capabilities()["actual_backend"] not in {"redis", "valkey"}:
            return
        for namespace in V3_NAMESPACES:
            def rebuild(_memory, _disk):
                prefix = self._v3_prefix(namespace)
                if self._client.get(prefix+"rank-ready:1") and self._client.get(prefix+"metadata-ready:1"):
                    return
                # SCAN belongs to controlled format initialization. Each Lua
                # call indexes current atomic metadata without replacing it.
                for entry in self._client.scan_iter(match=prefix+"entry:*", count=128):
                    entry = entry.decode() if isinstance(entry, bytes) else entry
                    suffix = entry.rsplit(":", 1)[-1]
                    self._client.eval(REMOTE_OPERATION, 5, entry, prefix+"lease:"+suffix,
                        prefix+"rank", prefix+"fresh", prefix+"members", "index", time.time())
                self._client.set(prefix+"rank-ready:1", "1")
                self._client.set(prefix+"metadata-ready:1", "1")
            self._call(namespace, rebuild)

    def rank_snapshot(self, namespace, *, now=None, offset=0, limit=128):
        if namespace not in V3_NAMESPACES or type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 512:
            raise ValueError("invalid rank page")
        now = time.time() if now is None else now
        def read(_memory, disk):
            if self._actual in {"redis", "valkey"}:
                prefix = self._v3_prefix(namespace)
                result = json.loads(self._client.eval(REMOTE_RANK, 3, prefix+"rank", prefix+"fresh", prefix+"members", now, offset, limit))
                if not result["items"]:
                    result["items"] = []
            elif disk is not None:
                with self._v3_disk(disk) as connection:
                    total = connection.execute("""SELECT count(*) FROM recognition_cache_rank_v3
                        WHERE namespace=? AND positive=1 AND (fresh IS NULL OR fresh>?)""", (namespace, now)).fetchone()[0]
                    rows = connection.execute("""SELECT cache_key,hits,version,cycle,generation,fresh,created_at,
                        next_retry_at,refresh_failures,refresh_state,refresh_error
                        FROM recognition_cache_rank_v3 WHERE namespace=? AND positive=1 AND (fresh IS NULL OR fresh>?)
                        ORDER BY hits DESC,cache_key ASC LIMIT ? OFFSET ?""", (namespace, now, limit, offset))
                    fields = ("cache_key", "hit_count", "payload_version", "cycle_id", "storage_generation", "fresh_until",
                              "created_at", "next_retry_at", "refresh_failures", "refresh_state", "refresh_error")
                    result = {"total": total, "items": [{**dict(zip(fields, row)), "rank": offset+i} for i, row in enumerate(rows)]}
            else:
                entries, _leases = self._v3_memory()
                valid = sorted((value for (name, _key), value in entries.items() if name == namespace and positive(value)
                    and (value["fresh_until"] is None or value["fresh_until"] > now)), key=lambda x: (-x["hit_count"], x["cache_key"]))
                result = {"total": len(valid), "items": [{key: value.get(key) for key in (
                    "cache_key", "hit_count", "payload_version", "cycle_id", "storage_generation", "fresh_until", *CONTROL_FIELDS)}
                    | {"rank": offset+i} for i, value in enumerate(valid[offset:offset+limit])]}
            result["snapshot_now"] = now
            for row in result["items"]:
                row["hot"] = row["hit_count"] > 1 and row["rank"] < (result["total"]+1)//2
            return result
        return self._call(namespace, read, blocked={"total": 0, "items": [], "snapshot_now": now})

    def envelope_refresh_failure(self, namespace, key, expected, *, owner, fence, delay, code):
        if type(delay) not in (int, float) or not 0 <= delay <= 36500*86400 or not isinstance(code, str) or len(code) > 128:
            raise ValueError("invalid refresh failure")
        return bool(self._v3_apply(namespace, key, "failure", (str(expected["payload_version"]), expected["cycle_id"],
            expected["storage_generation"], owner, fence, delay, code)))

    def capabilities(self):
        state = self.status()
        actual = state["backend"]
        return {"configured_backend": state["configured_backend"], "actual_backend": actual,
                "hot_refresh_supported": actual != "memory", "shared": actual != "memory",
                "atomic_hit": True, "cas": True, "fencing": True,
                "storage_generation": self._settings.get("storage_generation") or "v3",
                "reason": state.get("reason", ""), "maintenance": state.get("maintenance", False),
                "restart_required": state.get("restart_required", False)}

    def _v3_prefix(self, namespace):
        return self._prefix(namespace) + "atomic:" + self._remote_instance_id() + ":"

    def _v3_memory(self):
        if not hasattr(self, "_v3_state") or self._v3_state[0] != self._revision:
            self._v3_state = (self._revision, {}, {})
            self._v3_read_stats = {}
        return self._v3_state[1:]

    @contextmanager
    def _v3_disk(self, disk):
        with disk._connection() as connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS recognition_cache_v3 (
                namespace TEXT NOT NULL, cache_key TEXT NOT NULL, data TEXT NOT NULL,
                hits INTEGER NOT NULL, last_access REAL,
                PRIMARY KEY(namespace, cache_key))""")
            connection.execute("""CREATE TABLE IF NOT EXISTS recognition_cache_lease_v3 (
                namespace TEXT NOT NULL, cache_key TEXT NOT NULL, owner TEXT,
                expires REAL NOT NULL, fence INTEGER NOT NULL,
                PRIMARY KEY(namespace, cache_key))""")
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def _v3_apply(self, namespace, key, op, args=(), now=None):
        if namespace not in V3_NAMESPACES:
            raise ValueError("not a V3 cache namespace")
        if not isinstance(key, str) or len(key) > 1024:
            raise ValueError("invalid V3 cache key")
        now = time.time() if now is None else now

        def execute(_memory, disk):
            # _call owns the injected maintenance gate. A frozen source may be
            # read by its snapshot owner, but may never receive mutations.
            if self._frozen and op != "get":
                return None
            if self._actual in {"redis", "valkey"}:
                prefix = self._v3_prefix(namespace)
                digest = hashlib.sha256(key.encode()).hexdigest()
                remote_args = args
                if op == "set":
                    envelope = json.loads(args[0])
                    remote_args = (*args, repr(envelope["fresh_until"]) if envelope["fresh_until"] is not None else "0",
                                   repr(envelope["stale_until"]) if envelope["stale_until"] is not None else "0")
                result = self._client.eval(REMOTE_OPERATION, 6, prefix + "entry:" + digest,
                    prefix + "lease:" + digest, prefix+"rank", prefix+"fresh", prefix+"members", prefix+"read-stats", op, now, *remote_args, key)
                if op == "get":
                    return _decode(*result) if result else None
                return int(result) or None
            if disk is not None:
                with self._v3_disk(disk) as connection:
                    row = connection.execute("SELECT data,hits,last_access FROM recognition_cache_v3 WHERE namespace=? AND cache_key=?",
                                             (namespace, key)).fetchone()
                    value = disk_overlay(connection, namespace, _decode(*row)) if row else None
                    if op == "get" and args[0] == "1":
                        hit = bool(value and (value["fresh_until"] is None or value["fresh_until"] > now))
                        stale = bool(value and value["fresh_until"] is not None and value["fresh_until"] <= now
                                     and (value["stale_until"] is None or value["stale_until"] > now))
                        connection.execute("""INSERT INTO recognition_cache_reads_v3 VALUES (?,?,?,?,?)
                            ON CONFLICT(namespace) DO UPDATE SET hits=hits+excluded.hits,
                            miss=miss+excluded.miss,stale_reads=stale_reads+excluded.stale_reads""",
                            (namespace, int(hit), int(not hit), int(stale), now))
                    row = connection.execute("SELECT owner,expires,fence FROM recognition_cache_lease_v3 WHERE namespace=? AND cache_key=?",
                                             (namespace, key)).fetchone()
                    lease = {"owner": row[0], "until": row[1], "fence": row[2]} if row else {"until": 0, "fence": 0}
                    result, updated, lease_changed = self._v3_mutate(op, value, lease, args, now)
                    if updated == "delete":
                        connection.execute("DELETE FROM recognition_cache_v3 WHERE namespace=? AND cache_key=?", (namespace, key))
                        connection.execute("DELETE FROM recognition_cache_rank_v3 WHERE namespace=? AND cache_key=?", (namespace, key))
                    elif updated == "hit":
                        connection.execute("UPDATE recognition_cache_v3 SET hits=?,last_access=? WHERE namespace=? AND cache_key=?",
                                           (value["hit_count"], value["last_access"], namespace, key))
                        connection.execute("UPDATE recognition_cache_rank_v3 SET hits=? WHERE namespace=? AND cache_key=?",
                                           (value["hit_count"], namespace, key))
                    elif updated == "control":
                        connection.execute("""UPDATE recognition_cache_rank_v3 SET next_retry_at=?,refresh_failures=?,
                            refresh_state=?,refresh_error=? WHERE namespace=? AND cache_key=?""",
                            (value["next_retry_at"], value["refresh_failures"], value["refresh_state"], value["refresh_error"], namespace, key))
                    elif isinstance(updated, dict):
                        connection.execute("""INSERT INTO recognition_cache_v3(namespace,cache_key,data,hits,last_access)
                            VALUES (?,?,?,?,?) ON CONFLICT(namespace,cache_key) DO UPDATE SET
                            data=excluded.data,hits=excluded.hits,last_access=excluded.last_access""",
                            (namespace, key, json.dumps(updated), updated["hit_count"], updated["last_access"]))
                        disk_index_write(connection, namespace, updated)
                    if lease_changed:
                        connection.execute("""INSERT INTO recognition_cache_lease_v3(namespace,cache_key,owner,expires,fence)
                            VALUES (?,?,?,?,?) ON CONFLICT(namespace,cache_key) DO UPDATE SET
                            owner=excluded.owner,expires=excluded.expires,fence=excluded.fence""",
                            (namespace, key, lease.get("owner"), lease["until"], lease["fence"]))
                    return result
            entries, leases = self._v3_memory()
            entry_key = (namespace, key)
            value = entries.get(entry_key)
            if op == "get" and args[0] == "1":
                counts = self._v3_read_stats.setdefault(namespace, [0, 0, 0, now])
                hit = bool(value and (value["fresh_until"] is None or value["fresh_until"] > now))
                stale = bool(value and value["fresh_until"] is not None and value["fresh_until"] <= now
                             and (value["stale_until"] is None or value["stale_until"] > now))
                counts[0 if hit else 1] += 1
                counts[2] += int(stale)
            lease = leases.setdefault(entry_key, {"until": 0, "fence": 0})
            result, updated, _changed = self._v3_mutate(op, value, lease, args, now)
            if updated == "delete":
                entries.pop(entry_key, None)
            elif isinstance(updated, dict):
                entries[entry_key] = updated
            if updated or op in {"lease", "renew", "release"}:
                self._mark_dirty(namespace)
            return deepcopy(result)
        return self._call(namespace, execute)

    @staticmethod
    def _v3_mutate(op, value, lease, args, now):
        if op == "lease":
            if lease["until"] > now:
                return None, None, False
            lease.update(owner=args[0], until=now + float(args[1]), fence=lease["fence"] + 1)
            return lease["fence"], None, True
        if op in {"renew", "release"}:
            if lease.get("owner") != args[0] or lease["fence"] != int(args[1]) or lease["until"] <= now:
                return None, None, False
            lease["until"] = 0 if op == "release" else now + float(args[2])
            return True, None, True
        expired = value and value["stale_until"] is not None and value["stale_until"] <= now
        if expired:
            value = None
        if op == "get":
            if value is None:
                return None, "delete" if expired else None, False
            fresh = value["fresh_until"] is None or value["fresh_until"] > now
            if not fresh and args[1] != "1":
                return None, None, False
            if fresh and args[0] == "1":
                value["hit_count"] += 1
                value["last_access"] = now
            return value, "hit" if fresh and args[0] == "1" else None, False
        if op == "delete":
            if args and args[0] and (value is None or str(value["payload_version"]) != str(args[0])
                    or value["cycle_id"] != args[1] or value["storage_generation"] != args[2]):
                return False, None, False
            lease.update(owner=None, until=0, fence=lease["fence"]+1)
            return bool(value), "delete", True
        if op == "failure":
            version, cycle, generation, owner, fence, delay, code = args
            if (value is None or (value["payload_version"], value["cycle_id"], value["storage_generation"]) != (int(version), cycle, generation)
                    or lease.get("owner") != owner or lease["fence"] != int(fence) or lease["until"] <= now):
                return None, None, False
            value.update(next_retry_at=now+float(delay), refresh_failures=value.get("refresh_failures", 0)+1,
                         refresh_state="retry", refresh_error=code)
            return True, "control", False
        data, expected, cycle, generation, owner, fence, preserve = args
        if expected == "absent" and value is not None:
            return None, None, False
        if expected not in ("", "absent") and (value is None or
                (value["payload_version"], value["cycle_id"], value["storage_generation"]) != (int(expected), cycle, generation)):
            return None, None, False
        if owner and (lease.get("owner") != owner or lease["fence"] != int(fence) or lease["until"] <= now):
            return None, None, False
        updated = json.loads(data)
        if updated["stale_until"] is not None and updated["stale_until"] <= now:
            return None, None, False
        if preserve != "1" and value is not None:
            updated["payload_version"] = value["payload_version"] + 1
        return True, updated, False

    def envelope_get(self, namespace, key, *, hit=True, allow_stale=False):
        return self._v3_apply(namespace, key, "get", ("1" if hit else "0", "1" if allow_stale else "0"))

    def envelope_put(self, namespace, key, envelope, *, expected=None, absent=False,
                     owner=None, fence=None, preserve_version=False):
        from app.media.recognition.contracts import CacheEnvelope
        value = asdict(envelope) if isinstance(envelope, CacheEnvelope) else deepcopy(envelope)
        value = asdict(CacheEnvelope(**value))
        if value["cache_key"] != key or namespace != "recognition_" + value["layer"]:
            raise ValueError("envelope key/layer mismatch")
        expected = expected or {}
        return bool(self._v3_apply(namespace, key, "set", (
            json.dumps(value, ensure_ascii=False, allow_nan=False),
            "absent" if absent else str(expected.get("payload_version", "")),
            expected.get("cycle_id", ""), expected.get("storage_generation", ""),
            owner or "", fence or 0, "1" if preserve_version else "0")))

    def envelope_restore(self, namespace, key, envelope):
        value = deepcopy(envelope)
        value["storage_generation"] = self.capabilities()["storage_generation"]
        return self.envelope_put(namespace, key, value, preserve_version=True)

    def envelope_delete(self, namespace, key, *, expected=None):
        expected = expected or {}
        return bool(self._v3_apply(namespace, key, "delete", (
            str(expected.get("payload_version", "")), expected.get("cycle_id", ""), expected.get("storage_generation", ""))))

    def lease_acquire(self, namespace, key, owner, ttl=30):
        if not owner or not isinstance(owner, str) or type(ttl) not in (int, float) or not 0 < ttl <= 3600:
            raise ValueError("invalid lease")
        return self._v3_apply(namespace, key, "lease", (owner, ttl))

    def lease_renew(self, namespace, key, owner, fence, ttl=30):
        if type(ttl) not in (int, float) or not 0 < ttl <= 3600:
            raise ValueError("invalid lease")
        return bool(self._v3_apply(namespace, key, "renew", (owner, fence, ttl)))

    def lease_release(self, namespace, key, owner, fence):
        return bool(self._v3_apply(namespace, key, "release", (owner, fence, 0)))

    def envelope_records(self, namespace, include_expired=False):
        def read(_memory, disk):
            if self._actual in {"redis", "valkey"}:
                values = []
                for key in self._client.scan_iter(match=self._v3_prefix(namespace) + "entry:*", count=500):
                    row = self._client.hmget(key, "data", "hits", "last", "version", "created", "retry", "failures", "refresh_state", "refresh_error", "needs")
                    if row[0]:
                        if row[5] is None:  # pre-index snapshots keep their original defaults
                            row = row[:4]
                        values.append(_decode(*row))
            elif disk is not None:
                with self._v3_disk(disk) as connection:
                    values = [disk_overlay(connection, namespace, _decode(*row)) for row in connection.execute(
                        "SELECT data,hits,last_access FROM recognition_cache_v3 WHERE namespace=?", (namespace,))]
            else:
                entries, _leases = self._v3_memory()
                values = [deepcopy(value) for (name, _key), value in entries.items() if name == namespace]
            now = time.time()
            return [(e["cache_key"], e, e["stale_until"]) for e in values
                    if include_expired or e["stale_until"] is None or e["stale_until"] > now]
        return self._call(namespace, read, blocked=[]) or []

    def envelope_metadata(self, namespace, *, now=None):
        """Administrative metadata only; never transfer raw response JSON."""
        now = time.time() if now is None else now
        fields = ("cache_key", "hit_count", "payload_version", "cycle_id", "storage_generation", "fresh_until", "stale_until",
                  "created_at", "next_retry_at", "refresh_failures", "refresh_state", "refresh_error", "needs_revalidation", "source_trace_id", "label")
        def read(_memory, disk):
            if self._actual in {"redis", "valkey"}:
                names = ("cache_key", "hits", "version", "cycle", "generation", "fresh", "stale", "created", "retry", "failures",
                         "refresh_state", "refresh_error", "needs", "source_trace", "label")
                result, keys = [], []
                def page(keys):
                    with self._client.pipeline(transaction=False) as pipeline:
                        for key in keys:
                            pipeline.hmget(key, names)
                        return pipeline.execute()
                for key in self._client.scan_iter(match=self._v3_prefix(namespace)+"entry:*", count=128):
                    keys.append(key)
                    if len(keys) >= 128:
                        result.extend(page(keys))
                        keys = []
                if keys:
                    result.extend(page(keys))
                items = []
                for row in result:
                    if not row[0]:
                        continue
                    values = [value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value for value in row]
                    item = dict(zip(fields, values))
                    for field in ("hit_count", "payload_version", "refresh_failures"):
                        item[field] = int(item[field] or 0)
                    for field in ("fresh_until", "stale_until", "created_at", "next_retry_at"):
                        item[field] = float(item[field]) if item[field] else None
                    item["fresh_until"] = item["fresh_until"] or None
                    item["stale_until"] = item["stale_until"] or None
                    item["needs_revalidation"] = item["needs_revalidation"] == "1"
                    item["source_trace_id"] = item["source_trace_id"] or None
                    items.append(item)
            elif disk is not None:
                with self._v3_disk(disk) as connection:
                    rows = connection.execute("""SELECT r.cache_key,r.hits,r.version,r.cycle,r.generation,r.fresh,
                        json_extract(e.data,'$.stale_until'),r.created_at,r.next_retry_at,r.refresh_failures,r.refresh_state,
                        r.refresh_error,r.needs_revalidation,json_extract(e.data,'$.source_trace_id'),
                        coalesce(json_extract(e.data,'$.request_params.query'),json_extract(e.data,'$.request_params.scope.canonical_name'),
                          json_extract(e.data,'$.payload.title'),json_extract(e.data,'$.payload.name'),json_extract(e.data,'$.request_params.path'),'')
                        FROM recognition_cache_rank_v3 r JOIN recognition_cache_v3 e USING(namespace,cache_key) WHERE r.namespace=?""", (namespace,))
                    items = [dict(zip(fields, row)) for row in rows]
            else:
                entries, _leases = self._v3_memory()
                items = []
                for (name, key), value in entries.items():
                    if name != namespace:
                        continue
                    item = {field: value.get(field) for field in fields}
                    params, payload = value.get("request_params") or {}, value.get("payload") or {}
                    item["label"] = params.get("query") or (params.get("scope") or {}).get("canonical_name") or payload.get("title") or payload.get("name") or params.get("path") or ""
                    items.append(item)
            return [item for item in items if item["stale_until"] is None or item["stale_until"] > now]
        return self._call(namespace, read, blocked=[]) or []

    def envelope_clear(self, namespace):
        def clear(memory, disk):
            if self._frozen:
                return False
            if self._actual in {"redis", "valkey"}:
                self._clear_remote(namespace)
            elif disk is not None:
                with self._v3_disk(disk) as connection:
                    connection.execute("DELETE FROM recognition_cache_v3 WHERE namespace=?", (namespace,))
                    connection.execute("DELETE FROM recognition_cache_lease_v3 WHERE namespace=?", (namespace,))
                    connection.execute("DELETE FROM recognition_cache_rank_v3 WHERE namespace=?", (namespace,))
                    connection.execute("DELETE FROM recognition_cache_reads_v3 WHERE namespace=?", (namespace,))
                memory.clear()
            else:
                entries, leases = self._v3_memory()
                self._v3_read_stats.pop(namespace, None)
                for collection in (entries, leases):
                    for key in list(collection):
                        if key[0] == namespace:
                            del collection[key]
                self._mark_dirty(namespace)
            return True
        return self._call(namespace, clear, blocked=False)

    def envelope_prune(self, namespace):
        now = time.time()
        expired = [key for key, _value, expires in self.envelope_records(namespace, include_expired=True)
                   if expires is not None and expires <= now]
        for key in expired:
            self.envelope_delete(namespace, key)
        return len(expired)
