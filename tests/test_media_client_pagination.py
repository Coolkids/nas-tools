"""媒体服务器全量同步适配器使用有界分页读取条目。"""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from app.mediaserver.client.emby import Emby
from app.mediaserver.client.jellyfin import Jellyfin
from app.mediaserver.client.plex import Plex


class MediaClientPaginationTest(TestCase):
    @staticmethod
    def _page_response(items, total):
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"Items": items, "TotalRecordCount": total},
        )

    def _assert_http_client_pages(self, client_type, host, page_size=500):
        client = object.__new__(client_type)
        client._host = host
        client._user = "user"
        client._apikey = "api-key"
        client.server_type = client_type.server_type
        first_page = [{"Type": "Episode"} for _ in range(page_size)]
        second_page = [{"Id": "movie-1", "Type": "Movie", "Name": "电影",
                        "ParentId": "movies", "ProductionYear": 2024,
                        "ProviderIds": {"Tmdb": "1", "Imdb": "tt1"},
                        "Path": "/movies/film.mkv"}]
        response = Mock()
        response.get_res.side_effect = [
            self._page_response(first_page, page_size + 1),
            self._page_response(second_page, page_size + 1),
        ]

        with patch(f"{client_type.__module__}.RequestUtils", return_value=response):
            items = list(client.get_items("movies"))

        self.assertEqual(1, len(items))
        self.assertEqual({"id": "movie-1", "library": "movies", "type": "Movie",
                          "title": "电影", "originalTitle": None, "year": 2024,
                          "tmdbid": "1", "imdbid": "tt1", "path": "/movies/film.mkv"},
                         items[0])
        self.assertEqual(2, response.get_res.call_count)
        first_query = parse_qs(urlparse(response.get_res.call_args_list[0].args[0]).query)
        second_query = parse_qs(urlparse(response.get_res.call_args_list[1].args[0]).query)
        self.assertEqual(["0"], first_query["StartIndex"])
        self.assertEqual([str(page_size)], first_query["Limit"])
        self.assertEqual([str(page_size)], second_query["StartIndex"])

    def test_emby_reads_multiple_bounded_pages_and_uses_list_fields(self):
        self._assert_http_client_pages(Emby, "http://emby/")

    def test_jellyfin_reads_multiple_bounded_pages_and_uses_list_fields(self):
        self._assert_http_client_pages(Jellyfin, "http://jellyfin/")

    def test_http_pagination_failure_is_propagated(self):
        client = object.__new__(Emby)
        client._host = "http://emby/"
        client._user = "user"
        client._apikey = "api-key"
        client.server_type = Emby.server_type
        response = Mock()
        response.get_res.return_value = SimpleNamespace(status_code=503)

        with patch("app.mediaserver.client.emby.RequestUtils", return_value=response), \
                patch("app.mediaserver.client._base.time.sleep"):
            with self.assertRaisesRegex(RuntimeError, "分页请求失败"):
                list(client.get_items("movies"))
        self.assertEqual(3, response.get_res.call_count)

    def test_http_page_retries_transient_server_failure_and_continues_pagination(self):
        for client_type, host in ((Emby, "http://emby/"), (Jellyfin, "http://jellyfin/")):
            with self.subTest(client=client_type.__name__):
                client = object.__new__(client_type)
                client._host = host
                client._user = "user"
                client._apikey = "api-key"
                client.server_type = client_type.server_type
                result = {"Id": "movie-1", "Type": "Movie", "Name": "电影",
                          "ParentId": "movies", "ProductionYear": 2024}
                response = Mock()
                response.get_res.side_effect = [
                    SimpleNamespace(status_code=503),
                    self._page_response([result], 1),
                ]
                with patch(f"{client_type.__module__}.RequestUtils", return_value=response), \
                        patch("app.mediaserver.client._base.time.sleep"):
                    items = list(client.get_items("movies"))

                self.assertEqual(1, len(items))
                self.assertEqual("movie-1", items[0]["id"])
                self.assertEqual(2, response.get_res.call_count)

    def test_plex_reads_multiple_bounded_pages(self):
        client = object.__new__(Plex)
        page = [SimpleNamespace(key=str(number), librarySectionID=1, type="movie",
                                title=f"电影 {number}", year=2024)
                for number in range(500)]
        final_page = [SimpleNamespace(key="last", librarySectionID=1, type="movie",
                                      title="最后一部", year=2024)]
        section = Mock()
        section.all.side_effect = [page, final_page]
        plex = SimpleNamespace(library=SimpleNamespace(sectionByID=Mock(return_value=section)))
        client._plex = plex
        client.server_type = Plex.server_type

        items = list(client.get_items(1))

        self.assertEqual(501, len(items))
        self.assertEqual([0, 500], [call.kwargs["container_start"]
                                    for call in section.all.call_args_list])
        self.assertEqual([500, 500], [call.kwargs["container_size"]
                                      for call in section.all.call_args_list])
        self.assertEqual("last", items[-1]["id"])

    def test_plex_normalizes_string_section_id_from_library_list(self):
        client = object.__new__(Plex)
        result = SimpleNamespace(key="movie-1", librarySectionID=14, type="movie",
                                 title="电影", year=2024)
        section = Mock()
        section.all.return_value = [result]
        library = SimpleNamespace(sectionByID=Mock(return_value=section))
        client._plex = SimpleNamespace(library=library)

        items = list(client.get_items("14"))

        self.assertEqual(1, len(items))
        library.sectionByID.assert_called_once_with(14)

    def test_plex_page_retries_transient_remote_failure(self):
        client = object.__new__(Plex)
        result = SimpleNamespace(key="movie-1", librarySectionID=1, type="movie",
                                 title="电影", year=2024)
        section = Mock()
        section.all.side_effect = [RuntimeError("temporary server failure"), [result]]
        client._plex = SimpleNamespace(
            library=SimpleNamespace(sectionByID=Mock(return_value=section)))

        with patch("app.mediaserver.client.plex.time.sleep"):
            items = list(client.get_items(1))

        self.assertEqual(1, len(items))
        self.assertEqual("movie-1", items[0]["id"])
        self.assertEqual(2, section.all.call_count)
