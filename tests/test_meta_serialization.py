"""媒体对象的 pickle、复制及 CacheLib 缓存回归验证。"""

import copy
import pickle

import pytest

from app.media.category import Category
from app.media.meta.metaanime import MetaAnime
from app.media.meta.metavideo import MetaVideo
from app.utils.cache_backend import CacheStore
from app.utils.persistent_cache import PersistentCache
from app.utils.types import MediaType


@pytest.fixture(params=[
    (MetaVideo, "Example.Show.2024.S02E03.1080p.WEB-DL.H.264-Group"),
    (MetaAnime, "[Fansub] Example Anime - 03 [1080p].mkv"),
])
def media(request):
    meta_class, title = request.param
    meta = meta_class(title, subtitle="第三集", fileflag=True)
    meta.set_tmdb_info({"id": 123, "media_type": MediaType.TV, "name": "示例剧集",
                        "first_air_date": "2024-01-01", "genre_ids": [18]})
    meta.recognition_request_id = "recognition-example"
    meta.note = {"source": ["test"]}
    meta.fanart._images = {"tvposter": "https://example.com/poster.jpg"}
    return meta


def assert_restored_media(restored, original):
    assert type(restored) is type(original)
    assert restored is not original
    for field in ("org_string", "subtitle", "fileflag", "type", "title", "year",
                  "tmdb_id", "tmdb_info", "begin_season", "begin_episode",
                  "resource_pix", "recognition_request_id", "note"):
        assert getattr(restored, field) == getattr(original, field)
    assert restored.fanart._images == original.fanart._images
    assert restored.category_handler is Category()
    assert original.category_handler is Category()


@pytest.mark.parametrize("protocol", [4, pickle.HIGHEST_PROTOCOL])
def test_pickle_round_trip_preserves_media_and_restores_category(media, protocol, monkeypatch):
    payload = pickle.dumps(media, protocol=protocol)
    monkeypatch.setattr(Category(), "_tv_categorys", {"当前分类": {}})
    restored = pickle.loads(payload)

    assert_restored_media(restored, media)
    restored.set_tmdb_info(dict(media.tmdb_info))
    assert restored.category == "当前分类"


@pytest.mark.parametrize("backend", ["memory", "memory_disk"])
def test_cache_round_trip_preserves_media_objects(media, backend, tmp_path):
    path = str(tmp_path / "cache")
    store = CacheStore(path, settings={"backend": backend})
    cache = PersistentCache("media", store=store)
    cache.set("detail", media)
    cache.set("search", [media])
    assert_restored_media(cache.get("detail"), media)
    assert_restored_media(cache.get("search")[0], media)

    if backend == "memory_disk":
        restored_store = CacheStore(path, settings={"backend": backend})
        restored_cache = PersistentCache("media", store=restored_store)
        assert_restored_media(restored_cache.get("detail"), media)
        assert_restored_media(restored_cache.get("search")[0], media)


@pytest.mark.parametrize("copier", [copy.copy, copy.deepcopy])
def test_copy_preserves_media_and_category_singleton(media, copier):
    restored = copier(media)
    assert_restored_media(restored, media)

    if copier is copy.deepcopy:
        restored.note["source"].append("copied")
        assert media.note == {"source": ["test"]}
