import json
import uuid
from datetime import datetime
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from app.db.models import RECOGNITIONATTEMPT, RECOGNITIONREQUEST
from app.helper.db_helper import DbHelper
from app.indexer.client._base import _IIndexClient
from app.media.media import Media
from app.rsschecker import RssChecker as RssCheckerSingleton
from app.utils.types import MediaType


class _MediaInfo:
    def __init__(self, request_id="recognition-request", tmdb_id=7):
        self.recognition_request_id = request_id
        self.tmdb_id = tmdb_id
        self.tmdb_info = {"id": tmdb_id, "media_type": "tv", "name": "Example Show"}
        self.type = MediaType.TV
        self.title = "Example Show"
        self.year = "2024"
        self.org_string = "Example.Show.S01E01"
        self.site = "Example Index"
        self.imdb_id = None
        self.over_edition = False
        self.res_order = None
        self.download_setting = None
        self.save_path = None

    def get_name(self):
        return self.title

    def get_title_string(self):
        return self.title

    def get_episode_list(self):
        return [1]

    def get_season_episode_string(self):
        return "S01E01"

    def get_season_string(self):
        return "S01"

    def set_torrent_info(self, **_kwargs):
        pass

    def set_tmdb_info(self, value):
        self.tmdb_info = value
        self.tmdb_id = value.get("id") if value else None


class _Indexer(_IIndexClient):
    def __init__(self):
        pass

    def match(self, *_args, **_kwargs):
        return False

    def get_status(self):
        return None

    def get_indexers(self):
        return []

    def search(self, *_args, **_kwargs):
        return []


class BusinessShortCircuitTest(TestCase):
    def test_custom_rss_resolve_reuses_local_parse_and_skips_ai_on_media_cache_hit(self):
        rss_checker_type = RssCheckerSingleton.__closure__[0].cell_contents
        for cache_hit in (True, False):
            with self.subTest(cache_hit=cache_hit):
                checker = object.__new__(rss_checker_type)
                taskinfo = {
                    "uses": "D", "recognization": "Y", "name": "Fixture RSS",
                    "include": "", "exclude": "", "filter": None,
                    "save_path": "", "download_setting": "", "sites": {},
                }
                checker.get_rsstask_info = Mock(return_value=taskinfo)
                local_parse = _MediaInfo(request_id=None)
                local_parse.tmdb_info = {"id": 7, "media_type": "movie", "title": "Example Show"}
                local_parse.type = MediaType.MOVIE
                local_parse.download_setting = None
                resolved = _MediaInfo()
                resolved.type = MediaType.MOVIE
                resolved.download_setting = None
                checker.media = Mock()
                checker.media.get_cache_info.return_value = (
                    {"id": 7, "type": "movie", "title": "Example Show", "year": "2024"}
                    if cache_hit else {})
                checker.media.get_media_info.return_value = resolved
                checker.downloader = Mock()
                checker.downloader.check_exists_medias.return_value = (False, None, None)
                checker.downloader.download.return_value = (True, None)
                checker.downloader.get_default_client_type.return_value = SimpleNamespace(value="test")
                checker.filter = Mock()
                checker.filter.check_torrent_filter.return_value = (True, 0, [])
                checker.dbhelper = Mock()
                checker.dbhelper.is_userrss_finished.return_value = False
                checker.message = Mock()

                with patch.object(rss_checker_type, "_RssChecker__parse_userrss_result",
                                  return_value=[{"title": "Example Show S01E01", "enclosure": "torrent-url"}]), \
                        patch("app.rsschecker.MetaInfo", return_value=local_parse) as parse_title, \
                        patch("app.rsschecker.record_deferred_parse_result") as deferred:
                    checker.check_task_rss(9)

                parse_title.assert_called_once_with(
                    title="Example Show S01E01", mtype=None, include_ai=False, record=False)
                checker.media.get_cache_info.assert_called_once_with(local_parse)
                if cache_hit:
                    checker.media.get_media_info.assert_not_called()
                    deferred.assert_called_once()
                    self.assertEqual("rsschecker_media_cache_hit", deferred.call_args.args[4])
                else:
                    checker.media.get_media_info.assert_called_once_with(
                        title="Example Show S01E01", mtype=None, pre_parsed=local_parse)
                    deferred.assert_not_called()

    def test_indexer_tmdb_id_mismatch_is_recorded_on_successful_parse(self):
        client = _Indexer()
        client.index_type = "public_indexer"
        client._reverse_title_sites = []
        client.filter = Mock()
        client.filter.check_torrent_filter.return_value = (True, 10, "accepted")
        client.progress = Mock()
        client.media = Mock()
        client.media.get_cache_info.return_value = {}
        resolved = _MediaInfo(tmdb_id=8)
        client.media.get_media_info.return_value = resolved
        match_media = _MediaInfo(tmdb_id=7)
        indexer = Mock(id="indexer-id", public=True, name="Example Index")
        item = {"title": "Example.Show.S01E01", "description": "", "seeders": 4,
                "peers": 5, "uploadvolumefactor": 1, "downloadvolumefactor": 1}
        annotate = Mock()

        with patch("app.indexer.client._base.MetaInfo", return_value=_MediaInfo()), \
                patch("app.indexer.client._base.annotate_business_short_circuit", annotate):
            results = client.filter_search_results(
                [item], 1, indexer, {}, match_media, datetime.now())

        self.assertEqual([], results)
        client.media.get_media_info.assert_called_once()
        annotate.assert_called_once_with(
            resolved, "prefilter_rejected", "indexer_tmdb_id_mismatch",
            {"recognized_tmdb_id": 8, "matched_tmdb_id": 7,
             "indexer": "public_indexer"})

    def test_indexer_tmdb_mismatch_persists_on_original_recognition_record(self):
        helper = DbHelper()
        request_id = str(uuid.uuid4())
        attempt_id = str(uuid.uuid4())
        helper.insert_recognition_record({
            "request_id": request_id,
            "original_name": "Example.Show.S01E01",
            "source": "indexer",
            "stage": "parse_only",
            "actions": [],
            "provider_results": [{
                "attempt_id": attempt_id,
                "provider_id": "anitopy_ml",
                "status": "skipped",
                "error": "ai_deferred_to_indexer_filter_or_resolution",
            }],
            "overall_result": {"status": "success", "tmdb_status": "not_requested"},
            "tmdb_results": [],
        })
        try:
            client = _Indexer()
            client.index_type = "public_indexer"
            client._reverse_title_sites = []
            client.filter = Mock()
            client.filter.check_torrent_filter.return_value = (True, 10, "accepted")
            client.progress = Mock()
            client.media = Mock()
            client.media.get_cache_info.return_value = {}
            resolved = _MediaInfo(request_id=request_id, tmdb_id=8)
            client.media.get_media_info.return_value = resolved
            matched_media = _MediaInfo(tmdb_id=7)
            indexer = Mock(id="indexer-id", public=True, name="Example Index")
            item = {"title": "Example.Show.S01E01", "description": "", "seeders": 4,
                    "peers": 5, "uploadvolumefactor": 1, "downloadvolumefactor": 1}

            with patch("app.indexer.client._base.MetaInfo", return_value=_MediaInfo()):
                results = client.filter_search_results(
                    [item], 1, indexer, {}, matched_media, datetime.now())

            self.assertEqual([], results)
            request, attempts = helper.get_recognition_record(request_id)
            overall = json.loads(request.OVERALL_RESULT)
            self.assertEqual("indexer_tmdb_id_mismatch",
                             overall["business_result"]["reason"])
            action = json.loads(request.ACTIONS)[-1]
            self.assertEqual(attempt_id, action["attempt_id"])
            self.assertEqual(8, action["output"]["recognized_tmdb_id"])
            self.assertEqual(7, action["output"]["matched_tmdb_id"])
            attempt = next(row for row in attempts if row.ATTEMPT_ID == attempt_id)
            self.assertEqual("indexer_tmdb_id_mismatch", attempt.ERROR)
        finally:
            helper._db.query(RECOGNITIONATTEMPT).filter(
                RECOGNITIONATTEMPT.REQUEST_ID == request_id).delete()
            helper._db.query(RECOGNITIONREQUEST).filter(
                RECOGNITIONREQUEST.REQUEST_ID == request_id).delete()
            helper._db.commit()

    def test_indexer_missing_resolved_media_info_is_recorded(self):
        client = _Indexer()
        client.index_type = "public_indexer"
        client._reverse_title_sites = []
        client.filter = Mock()
        client.filter.check_torrent_filter.return_value = (True, 10, "accepted")
        client.progress = Mock()
        client.media = Mock()
        client.media.get_cache_info.return_value = {}
        client.media.get_media_info.return_value = None
        indexer = Mock(id="indexer-id", public=True, name="Example Index")
        item = {"title": "Example.Show.S01E01", "description": "", "seeders": 4,
                "peers": 5, "uploadvolumefactor": 1, "downloadvolumefactor": 1}
        record_skip = Mock()

        with patch("app.indexer.client._base.MetaInfo", return_value=_MediaInfo()), \
                patch("app.indexer.client._base.record_business_short_circuit", record_skip):
            results = client.filter_search_results(
                [item], 1, indexer, {}, _MediaInfo(tmdb_id=7), datetime.now())

        self.assertEqual([], results)
        record_skip.assert_called_once_with(
            "Example.Show.S01E01", "indexer", "indexer_resolution_failed",
            "indexer_media_info_unavailable", {"indexer": "public_indexer"},
            business_status="error")

    def test_indexer_missing_title_is_recorded_without_attempting_ai(self):
        client = _Indexer()
        client.index_type = "public_indexer"
        client._reverse_title_sites = []
        client.progress = Mock()
        indexer = Mock(id="indexer-id", name="Example Index")
        record_skip = Mock()

        with patch("app.indexer.client._base.record_business_short_circuit", record_skip):
            results = client.filter_search_results(
                [{"title": None, "description": "has no source title"}],
                1, indexer, {}, None, datetime.now())

        self.assertEqual([], results)
        record_skip.assert_called_once_with(
            "has no source title", "indexer", "prefilter_rejected",
            "indexer_title_missing",
            {"description": "has no source title", "indexer": "public_indexer"},
            business_status="error")
