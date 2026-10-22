"""Small backend-owned ranking/control metadata; payloads never enter scans."""

import json


CONTROL_FIELDS = ("created_at", "next_retry_at", "refresh_failures", "refresh_state", "refresh_error", "needs_revalidation")


def positive(value):
    payload = value.get("payload")
    if value.get("needs_revalidation") or not isinstance(payload, dict):
        return False
    if value["layer"] == "search":
        return isinstance(payload.get("results"), list) and bool(payload["results"])
    identity = payload.get("tmdb_id") if value["layer"] == "identity" else payload.get("id")
    return type(identity) is int and identity > 0 and (value["layer"] != "identity" or payload.get("verification_level") == "verified")


def ensure_disk_index(connection):
    existed = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='recognition_cache_rank_v3'").fetchone()
    connection.execute("""CREATE TABLE IF NOT EXISTS recognition_cache_rank_v3 (
        namespace TEXT NOT NULL, cache_key TEXT NOT NULL, hits INTEGER NOT NULL,
        fresh REAL, positive INTEGER NOT NULL, version INTEGER NOT NULL, cycle TEXT NOT NULL,
        generation TEXT NOT NULL, created_at REAL, next_retry_at REAL NOT NULL DEFAULT 0,
        refresh_failures INTEGER NOT NULL DEFAULT 0, refresh_state TEXT NOT NULL DEFAULT 'idle',
        refresh_error TEXT, needs_revalidation INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(namespace,cache_key))""")
    connection.execute("""CREATE INDEX IF NOT EXISTS recognition_cache_rank_v3_order
        ON recognition_cache_rank_v3(namespace,positive,hits DESC,cache_key)""")
    connection.execute("""CREATE INDEX IF NOT EXISTS recognition_cache_rank_v3_expiry
        ON recognition_cache_rank_v3(namespace,fresh)""")
    if existed and connection.execute("SELECT value FROM cache_metadata_v2 WHERE key='recognition_rank_version'").fetchone() == ("1",):
        return
    # A one-time cache format upgrade, also valid for old snapshots. This is
    # called during storage initialization, never by a ranking scan.
    rows = connection.execute("""SELECT e.namespace,e.data,e.hits,e.last_access FROM recognition_cache_v3 e
        LEFT JOIN recognition_cache_rank_v3 r USING(namespace,cache_key) WHERE r.cache_key IS NULL""")
    while batch := rows.fetchmany(128):
        for namespace, body, hits, last in batch:
            value = json.loads(body)
            value.update(hit_count=hits, last_access=last)
            disk_index_write(connection, namespace, value)
    connection.execute("INSERT OR REPLACE INTO cache_metadata_v2(key,value) VALUES ('recognition_rank_version','1')")


def disk_index_write(connection, namespace, value):
    connection.execute("""INSERT INTO recognition_cache_rank_v3 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(namespace,cache_key) DO UPDATE SET hits=excluded.hits,fresh=excluded.fresh,
        positive=excluded.positive,version=excluded.version,cycle=excluded.cycle,generation=excluded.generation,
        created_at=excluded.created_at,next_retry_at=excluded.next_retry_at,refresh_failures=excluded.refresh_failures,
        refresh_state=excluded.refresh_state,refresh_error=excluded.refresh_error,needs_revalidation=excluded.needs_revalidation""",
        (namespace, value["cache_key"], value["hit_count"], value["fresh_until"], int(positive(value)),
         value["payload_version"], value["cycle_id"], value["storage_generation"], value.get("created_at"),
         value.get("next_retry_at", 0), value.get("refresh_failures", 0), value.get("refresh_state", "idle"),
         value.get("refresh_error"), int(value.get("needs_revalidation", False))))


def disk_overlay(connection, namespace, value):
    if value is not None:
        row = connection.execute("""SELECT created_at,next_retry_at,refresh_failures,refresh_state,refresh_error,needs_revalidation
            FROM recognition_cache_rank_v3 WHERE namespace=? AND cache_key=?""", (namespace, value["cache_key"])).fetchone()
        if row:
            value.update(zip(CONTROL_FIELDS, row))
            value["needs_revalidation"] = bool(value["needs_revalidation"])
    return value


REMOTE_INDEX = r"""
local function index_entry(e, hits)
  local params = type(e.request_params) == 'table' and e.request_params or {}
  local payload = type(e.payload) == 'table' and e.payload or {}
  local label = params.query or (type(params.scope) == 'table' and params.scope.canonical_name) or payload.title or payload.name or params.path or ''
  redis.call('HSET', KEYS[1], 'label', type(label) == 'string' and label or '',
    'source_trace', type(e.source_trace_id) == 'string' and e.source_trace_id or '')
  local positive = type(e.payload) == 'table' and e.needs_revalidation ~= true
  if e.layer == 'search' then
    positive = positive and type(e.payload.results) == 'table' and #e.payload.results > 0
  elseif e.layer == 'identity' then
    positive = positive and type(e.payload.tmdb_id) == 'number' and e.payload.tmdb_id > 0 and e.payload.verification_level == 'verified'
  else
    positive = positive and type(e.payload.id) == 'number' and e.payload.id > 0
  end
  local fresh = e.fresh_until == cjson.null and 0 or e.fresh_until
  redis.call('HSET', KEYS[1], 'cache_key', e.cache_key, 'positive', positive and 1 or 0,
    'created', e.created_at and e.created_at ~= cjson.null and e.created_at or '',
    'retry', e.next_retry_at or 0, 'failures', e.refresh_failures or 0,
    'refresh_state', e.refresh_state or 'idle', 'refresh_error', e.refresh_error and e.refresh_error ~= cjson.null and e.refresh_error or '',
    'needs', e.needs_revalidation == true and 1 or 0)
  if positive and (fresh == 0 or fresh > tonumber(ARGV[2])) then
    redis.call('ZADD', KEYS[3], -hits, e.cache_key)
    redis.call('HSET', KEYS[5], e.cache_key, KEYS[1])
    if fresh > 0 then redis.call('ZADD', KEYS[4], fresh, e.cache_key) else redis.call('ZREM', KEYS[4], e.cache_key) end
  else
    redis.call('ZREM', KEYS[3], e.cache_key); redis.call('ZREM', KEYS[4], e.cache_key); redis.call('HDEL', KEYS[5], e.cache_key)
  end
end
local function remove_index(key)
  if key then redis.call('ZREM', KEYS[3], key); redis.call('ZREM', KEYS[4], key); redis.call('HDEL', KEYS[5], key) end
end
"""


REMOTE_RANK = r"""
local now, offset, limit = tonumber(ARGV[1]), tonumber(ARGV[2]), tonumber(ARGV[3])
-- Removing stale members changes neither their payloads nor their stale TTL.
local expired = redis.call('ZRANGEBYSCORE', KEYS[2], '-inf', now)
for _, key in ipairs(expired) do
  redis.call('ZREM', KEYS[1], key); redis.call('ZREM', KEYS[2], key); redis.call('HDEL', KEYS[3], key)
end
local total, items = redis.call('ZCARD', KEYS[1]), {}
local keys = redis.call('ZRANGE', KEYS[1], offset, offset+limit-1, 'WITHSCORES')
for i=1,#keys,2 do
  local key, entry = keys[i], redis.call('HGET', KEYS[3], keys[i])
  if entry then
    local m = redis.call('HMGET', entry, 'version','cycle','generation','fresh','created','retry','failures','refresh_state','refresh_error')
    table.insert(items, {cache_key=key, hit_count=-tonumber(keys[i+1]), rank=offset+(i-1)/2,
      payload_version=tonumber(m[1]), cycle_id=m[2], storage_generation=m[3],
      fresh_until=m[4] and tonumber(m[4]) ~= 0 and tonumber(m[4]) or cjson.null,
      created_at=m[5] and tonumber(m[5]) or cjson.null, next_retry_at=tonumber(m[6]) or 0,
      refresh_failures=tonumber(m[7]) or 0, refresh_state=m[8] or 'idle', refresh_error=m[9] or ''})
  end
end
return cjson.encode({total=total,items=items})
"""
