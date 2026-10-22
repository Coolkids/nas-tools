"""向 TMDB 缓存管理界面展示识别引擎正在使用的缓存封装。

页面读取和编辑的就是引擎使用的同一批记录。管理读取既不计为缓存命中，也不受缓存开关影响。
"""

from copy import deepcopy
import re

from app.media.recognition.cache_repository import LAYERS
from app.utils.cache_backend import STORE


class RecognitionTmdbCache:
    visible_layers = ("identity", "detail")
    labels = {"identity": "标题 → TMDB ID", "detail": "TMDB 作品详情"}

    def __init__(self, store=None):
        self.store = STORE if store is None else store

    @classmethod
    def parse_key(cls, key):
        match = re.fullmatch(r"recognition:(identity|detail):([a-f0-9]{64})", key or "")
        return match.groups() if match else None

    def records(self):
        for layer in self.visible_layers:
            for key, envelope, _expires in self.store.envelope_records("recognition_" + layer):
                yield layer, key, envelope

    @staticmethod
    def entity(layer, envelope):
        payload = envelope.get("payload") or {}
        if layer == "identity":
            return payload.get("media_type"), payload.get("tmdb_id")
        path = (envelope.get("request_params") or {}).get("path", "")
        match = re.fullmatch(r"/(movie|tv)/(\d+)", path)
        return (match.group(1), payload.get("id")) if match else (None, None)

    @classmethod
    def detail_scope(cls, layer, envelope):
        params = envelope.get("request_params") or {}
        return (*cls.entity(layer, envelope), params.get("origin"), params.get("language"))

    def entries(self, search=""):
        records = list(self.records())
        details = {}
        for layer, _key, envelope in records:
            if layer == "detail":
                entity = self.detail_scope(layer, envelope)
                current = details.get(entity)
                if current is None or (envelope.get("created_at") or 0) > (current.get("created_at") or 0):
                    details[entity] = envelope
        rows = []
        for layer, key, envelope in records:
            kind, tmdb_id = self.entity(layer, envelope)
            if kind not in {"movie", "tv"} or type(tmdb_id) is not int or tmdb_id <= 0:
                continue
            payload = envelope["payload"]
            params = envelope.get("request_params") or {}
            detail = details.get(self.detail_scope(layer, envelope), {}).get("payload", {}) if layer == "identity" else payload
            title = detail.get("title") or detail.get("name") or payload.get("title") or str(tmdb_id)
            scope = params.get("scope") or {}
            index = scope.get("canonical_name") or params.get("path") or title
            date = detail.get("release_date" if kind == "movie" else "first_air_date") or ""
            info = {"id": tmdb_id, "title": title, "year": date[:4] or payload.get("year"),
                    "media_type": "电影" if kind == "movie" else "电视剧",
                    "poster_path": detail.get("poster_path"), "backdrop_path": detail.get("backdrop_path"),
                    "cache_layer": layer, "cache_label": self.labels[layer],
                    "fresh_until": envelope.get("fresh_until"), "language": params.get("language")}
            if search.casefold() in f"{index} {title} {tmdb_id} {info['cache_label']}".casefold():
                rows.append((f"recognition:{layer}:{key}", info, index))
        return rows

    def delete(self, key):
        layer, digest = self.parse_key(key)
        # 删除操作也会通过栅栏阻止当前正在发布此记录的刷新任务。
        return self.store.envelope_delete("recognition_" + layer, digest)

    def modify(self, key, title):
        layer, digest = self.parse_key(key)
        target = self.store.envelope_get("recognition_" + layer, digest, hit=False, allow_stale=True)
        if target is None:
            return False
        entity = self.detail_scope(layer, target)
        # 身份缓存命中时会从详情记录读取标题。编辑标题时应保持两个视图
        # 一致，同时不延长有效期或更改 ID。
        changed = False
        for selected, cache_key, envelope in self.records():
            if self.detail_scope(selected, envelope) != entity:
                continue
            updated = deepcopy(envelope)
            field = "name" if selected == "detail" and entity[0] == "tv" else "title"
            updated["payload"][field] = title
            if not self.store.envelope_put("recognition_" + selected, cache_key, updated, expected=envelope):
                return False
            changed = True
        return changed

    def delete_by_tmdb_id(self, tmdb_id):
        for layer, key, envelope in self.records():
            if str(self.entity(layer, envelope)[1]) == str(tmdb_id):
                self.store.envelope_delete("recognition_" + layer, key)

    def clear(self):
        for layer in LAYERS:
            self.store.envelope_clear("recognition_" + layer)
