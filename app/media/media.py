import random
import sys

import zhconv
import log
from app.helper import MetaHelper
from app.media.meta.metainfo import MetaInfo, _infer_recognition_source
from app.media.recognition.records import current_recorder, recognition_config, recognition_remaining_seconds, record_tmdb_call, recognition_abort_reason
from app.media.tmdbv3api import TMDb, Search, Movie, TV, Person, Find, Discover, Trending, Episode, Genre
from app.utils import EpisodeFormat, StringUtils
from app.utils.types import MediaType
from config import Config, TMDB_IMAGE_ORIGINAL_URL, DEFAULT_TMDB_PROXY, \
    TMDB_IMAGE_FACE_URL, TMDB_PEOPLE_PROFILE_URL, TMDB_IMAGE_W500_URL


class Media:
    # TheMovieDB
    tmdb = None
    search = None
    movie = None
    tv = None
    episode = None
    person = None
    find = None
    trending = None
    discover = None
    genre = None
    meta = None
    _tmdb_clients = None


    def _v3_coordinator(self):
        from app.media.recognition.coordinator import RecognitionCoordinator
        return RecognitionCoordinator(recognition_config())

    @staticmethod
    def __v3_source(default):
        caller = sys._getframe(2).f_globals.get("__name__", "")
        source = _infer_recognition_source(caller)
        return default if source == "unknown" else source

    def __init__(self):
        self.init_config()

    def init_config(self):
        self._tmdb_clients = []
        app = Config().get_config('app')
        laboratory = Config().get_config('laboratory')
        if app:
            if app.get('rmt_tmdbkey'):
                self.tmdb = TMDb()
                if laboratory.get('tmdb_proxy'):
                    self.tmdb.domain = DEFAULT_TMDB_PROXY
                else:
                    self.tmdb.domain = app.get("tmdb_domain")
                self.tmdb.cache = True
                self.tmdb.api_key = app.get('rmt_tmdbkey')
                self.tmdb.language = 'zh'
                self.tmdb.proxies = Config().get_proxies()
                self.tmdb.debug = True
                clients = [Search, Movie, TV, Episode, Find, Person, Trending, Discover, Genre]
                instances = []
                for client_type in clients:
                    client = client_type(session=self.tmdb._session)
                    client.api_key = app.get('rmt_tmdbkey')
                    client.domain = self.tmdb.domain
                    client.proxies = Config().get_proxies()
                    client.cache = True
                    instances.append(client)
                (self.search, self.movie, self.tv, self.episode, self.find,
                 self.person, self.trending, self.discover, self.genre) = instances
                self._tmdb_clients = [self.tmdb, *instances]
                self.meta = MetaHelper()

    def _set_tmdb_language(self, language):
        for client in self._tmdb_clients or []:
            client.language = language


    @record_tmdb_call
    def get_tmdb_info(self, mtype: MediaType,
                      tmdbid,
                      language=None,
                      append_to_response=None,
                      chinese=True):
        """
        给定TMDB号，查询一条媒体信息
        :param mtype: 类型：电影、电视剧、动漫，为空时都查（此时用不上年份）
        :param tmdbid: TMDB的ID，有tmdbid时优先使用tmdbid，否则使用年份和标题
        :param language: 语种
        :param append_to_response: 附加信息
        :param chinese: 是否转换中文标题
        """
        if not self.tmdb:
            log.error("【Meta】TMDB API Key 未设置！")
            return None
        if language:
            self._set_tmdb_language(language)
        else:
            self._set_tmdb_language('zh-CN')
        if mtype == MediaType.MOVIE:
            tmdb_info = self.__get_tmdb_movie_detail(tmdbid, append_to_response)
            if tmdb_info:
                tmdb_info['media_type'] = MediaType.MOVIE
        else:
            tmdb_info = self.__get_tmdb_tv_detail(tmdbid, append_to_response)
            if tmdb_info:
                tmdb_info['media_type'] = MediaType.TV
        if tmdb_info:
            # 转换genreid
            tmdb_info['genre_ids'] = self.__get_genre_ids_from_detail(tmdb_info.get('genres'))
            # 转换中文标题
            if chinese:
                tmdb_info = self.__update_tmdbinfo_cn_title(tmdb_info)

        return tmdb_info

    def __update_tmdbinfo_cn_title(self, tmdb_info):
        """
        更新TMDB信息中的中文名称
        """
        # 查找中文名
        org_title = tmdb_info.get("title") if tmdb_info.get("media_type") == MediaType.MOVIE else tmdb_info.get(
            "name")
        if not StringUtils.is_chinese(org_title) and (self.tmdb.language == 'zh-CN'):
            cn_title = self.__get_tmdb_chinese_title(tmdbinfo=tmdb_info)
            if cn_title and cn_title != org_title:
                if tmdb_info.get("media_type") == MediaType.MOVIE:
                    tmdb_info['title'] = cn_title
                else:
                    tmdb_info['name'] = cn_title
        return tmdb_info

    def get_tmdb_infos(self, title, year=None, mtype: MediaType = None, page=1):
        """
        查询名称中有关键字的所有的TMDB信息并返回
        """
        if not self.tmdb:
            log.error("【Meta】TMDB API Key 未设置！")
            return []
        if not title:
            return []
        if not mtype and not year:
            results = self.__search_multi_tmdbinfos(title)
        else:
            if not mtype:
                results = list(
                    set(self.__search_movie_tmdbinfos(title, year)).union(set(self.__search_tv_tmdbinfos(title, year))))
                # 组合结果的情况下要排序
                results = sorted(results,
                                 key=lambda x: x.get("release_date") or x.get("first_air_date") or "0000-00-00",
                                 reverse=True)
            elif mtype == MediaType.MOVIE:
                results = self.__search_movie_tmdbinfos(title, year)
            else:
                results = self.__search_tv_tmdbinfos(title, year)
        return results[(page - 1) * 20:page * 20]

    def __search_multi_tmdbinfos(self, title):
        """
        同时查询模糊匹配的电影、电视剧TMDB信息
        """
        if not title:
            return []
        ret_infos = []
        multis = self.search.multi({"query": title}) or []
        for multi in multis:
            if multi.get("media_type") in ["movie", "tv"]:
                multi['media_type'] = MediaType.MOVIE if multi.get("media_type") == "movie" else MediaType.TV
                ret_infos.append(multi)
        return ret_infos

    def __search_movie_tmdbinfos(self, title, year):
        """
        查询模糊匹配的所有电影TMDB信息
        """
        if not title:
            return []
        ret_infos = []
        if year:
            movies = self.search.movies({"query": title, "year": year}) or []
        else:
            movies = self.search.movies({"query": title}) or []
        for movie in movies:
            if title in movie.get("title"):
                movie['media_type'] = MediaType.MOVIE
                ret_infos.append(movie)
        return ret_infos

    def __search_tv_tmdbinfos(self, title, year):
        """
        查询模糊匹配的所有电视剧TMDB信息
        """
        if not title:
            return []
        ret_infos = []
        if year:
            tvs = self.search.tv_shows({"query": title, "first_air_date_year": year}) or []
        else:
            tvs = self.search.tv_shows({"query": title}) or []
        for tv in tvs:
            if title in tv.get("name"):
                tv['media_type'] = MediaType.TV
                ret_infos.append(tv)
        return ret_infos

    @staticmethod
    def __make_cache_key(meta_info):
        """
        生成缓存的key
        """
        if not meta_info:
            return None
        cache_name = meta_info.get_name()
        alternative_names = getattr(meta_info, "alternative_names", []) or []
        if isinstance(alternative_names, str):
            alternative_names = [alternative_names]
        if len(alternative_names) > 1:
            # 多别名标题此前只以最后一个别名建缓存；将完整候选写入新键，
            # 使历史的“未识别”缓存不会阻止本次别名重试。
            cache_name = "|".join(alternative_names)
        return f"[{meta_info.type.value}]{cache_name}-{meta_info.year}-{meta_info.begin_season}"

    def get_cache_info(self, meta_info):
        """
        根据名称查询是否已经有缓存
        """
        if not meta_info:
            return {}
        return self.meta.get_meta_data_by_key(self.__make_cache_key(meta_info))

    def get_media_info(self, title,
                       subtitle=None,
                       mtype=None,
                       strict=None,
                       cache=True,
                       chinese=True,
                       append_to_response=None,
                       pre_parsed=None):
        """Resolve a media title and persist its complete recognition trace."""
        return self._v3_coordinator().resolve(title, subtitle=subtitle, mtype=mtype, strict=strict,
            cache=cache, chinese=chinese, append_to_response=append_to_response,
            pre_parsed=pre_parsed, source=self.__v3_source("media.get_media_info")).meta


    def get_media_info_original_title(self, title,
                        name=None,
                        subtitle=None,
                        year=None,
                        season=None,
                        mtype=None,
                        strict=None,
                        cache=True,
                        chinese=True,
                        append_to_response=None,
                        pre_parsed=None):
        """Record and resolve a title while preserving its supplied name context."""
        return self._v3_coordinator().resolve(title, name=name, subtitle=subtitle, year=year,
            season=season, mtype=mtype, strict=strict, cache=cache, chinese=chinese,
            append_to_response=append_to_response, pre_parsed=pre_parsed,
            source=self.__v3_source("media.get_media_info_original_title")).meta


    def __insert_media_cache(self, media_key, file_media_info):
        """
        将TMDB信息插入缓存
        """
        recorder = current_recorder()
        remaining = recognition_remaining_seconds()
        if (recognition_abort_reason(recorder) or (remaining is not None and remaining <= 0)
                or (recorder is not None and recorder.context.get("decision_reason"))):
            return
        if file_media_info:
            # 缓存标题
            cache_title = file_media_info.get(
                "title") if file_media_info.get(
                "media_type") == MediaType.MOVIE else file_media_info.get("name")
            # 缓存年份
            cache_year = file_media_info.get('release_date') if file_media_info.get(
                "media_type") == MediaType.MOVIE else file_media_info.get('first_air_date')
            if cache_year:
                cache_year = cache_year[:4]
            self.meta.update_meta_data({
                media_key: {
                    "id": file_media_info.get("id"),
                    "type": file_media_info.get("media_type"),
                    "year": cache_year,
                    "title": cache_title,
                    "poster_path": file_media_info.get("poster_path"),
                    "backdrop_path": file_media_info.get("backdrop_path")
                }
            })
        else:
            self.meta.update_meta_data({media_key: {'id': 0}})

    def get_media_info_on_files(self,
                                file_list,
                                tmdb_info=None,
                                media_type=None,
                                season=None,
                                episode_format: EpisodeFormat = None,
                                chinese=True):
        """Create one complete recognition record for each file in a batch."""
        return self._v3_coordinator().resolve_files(file_list, provided=tmdb_info, mtype=media_type,
            season=season, episode_format=episode_format, chinese=chinese,
            source=self.__v3_source("media.get_media_info_on_files"))


    def search_media_info_force(self, meta_info):
        """Retry TMDB lookup and keep the retry trace as a recognition request."""
        if not meta_info:
            return None
        scope = getattr(meta_info, "recognition_identity_scope", None) or {}
        result = self._v3_coordinator().resolve(
            getattr(meta_info, "org_string", None) or meta_info.get_name(),
            name=scope.get("canonical_name", meta_info.get_name()), year=scope.get("year", meta_info.year), season=meta_info.begin_season,
            mtype=(None if scope.get("media_type") == "unknown" else scope.get("media_type", meta_info.type)),
            force=True, pre_parsed=meta_info, source=self.__v3_source("media.search_media_info_force"))
        return result.meta.tmdb_info if result.meta is not None and result.outcome.business_identity else None


    @staticmethod
    def __dict_tmdbinfos(infos, mtype=None):
        """
        TMDB电影信息转为字典
        """
        if not infos:
            return []
        ret_infos = []
        for info in infos:
            tmdbid = info.get("id")
            vote = round(float(info.get("vote_average")), 1) if info.get("vote_average") else 0
            image = TMDB_IMAGE_W500_URL % info.get("poster_path")
            overview = info.get("overview")
            if mtype:
                media_type = mtype.value
                year = info.get("release_date")[0:4] if info.get(
                    "release_date") and mtype == MediaType.MOVIE else info.get(
                    "first_air_date")[0:4] if info.get(
                    "first_air_date") else ""
                typestr = 'MOV' if mtype == MediaType.MOVIE else 'TV'
                title = info.get("title") if mtype == MediaType.MOVIE else info.get("name")
            else:
                media_type = MediaType.MOVIE.value if info.get(
                    "media_type") == "movie" else MediaType.TV.value
                year = info.get("release_date")[0:4] if info.get(
                    "release_date") and info.get(
                    "media_type") == "movie" else info.get(
                    "first_air_date")[0:4] if info.get(
                    "first_air_date") else ""
                typestr = 'MOV' if info.get("media_type") == "movie" else 'TV'
                title = info.get("title") if info.get("media_type") == "movie" else info.get("name")

            ret_infos.append({
                'id': tmdbid,
                'orgid': tmdbid,
                'tmdbid': tmdbid,
                'title': title,
                'type': typestr,
                'media_type': media_type,
                'year': year,
                'vote': vote,
                'image': image,
                'overview': overview
            })

        return ret_infos

    def get_tmdb_hot_movies(self, page):
        """
        获取热门电影
        :param page: 第几页
        :return: TMDB信息列表
        """
        if not self.movie:
            return []
        from app.utils import TmdbHotMoviesCache
        cache_key = f"hot_movies:{page}"
        cached = TmdbHotMoviesCache.get(cache_key)
        if cached is not None:
            return cached
        result = self.__dict_tmdbinfos(self.movie.popular(page), MediaType.MOVIE)
        TmdbHotMoviesCache.set(cache_key, result)
        return result

    def get_tmdb_hot_tvs(self, page):
        """
        获取热门电视剧
        :param page: 第几页
        :return: TMDB信息列表
        """
        if not self.tv:
            return []
        from app.utils import TmdbHotTvsCache
        cache_key = f"hot_tvs:{page}"
        cached = TmdbHotTvsCache.get(cache_key)
        if cached is not None:
            return cached
        result = self.__dict_tmdbinfos(self.tv.popular(page), MediaType.TV)
        TmdbHotTvsCache.set(cache_key, result)
        return result

    def get_tmdb_new_movies(self, page):
        """
        获取最新电影
        :param page: 第几页
        :return: TMDB信息列表
        """
        if not self.movie:
            return []
        from app.utils import TmdbNewMoviesCache
        cache_key = f"new_movies:{page}"
        cached = TmdbNewMoviesCache.get(cache_key)
        if cached is not None:
            return cached
        result = self.__dict_tmdbinfos(self.movie.now_playing(page), MediaType.MOVIE)
        TmdbNewMoviesCache.set(cache_key, result)
        return result

    def get_tmdb_new_tvs(self, page):
        """
        获取最新电视剧
        :param page: 第几页
        :return: TMDB信息列表
        """
        if not self.tv:
            return []
        from app.utils import TmdbNewTvsCache
        cache_key = f"new_tvs:{page}"
        cached = TmdbNewTvsCache.get(cache_key)
        if cached is not None:
            return cached
        result = self.__dict_tmdbinfos(self.tv.on_the_air(page), MediaType.TV)
        TmdbNewTvsCache.set(cache_key, result)
        return result

    def get_tmdb_upcoming_movies(self, page):
        """
        获取即将上映电影
        :param page: 第几页
        :return: TMDB信息列表
        """
        if not self.movie:
            return []
        from app.utils import TmdbUpcomingMoviesCache
        cache_key = f"upcoming_movies:{page}"
        cached = TmdbUpcomingMoviesCache.get(cache_key)
        if cached is not None:
            return cached
        result = self.__dict_tmdbinfos(self.movie.upcoming(page), MediaType.MOVIE)
        TmdbUpcomingMoviesCache.set(cache_key, result)
        return result

    def get_tmdb_trending_all_week(self, page=1):
        """
        获取即将上映电影
        :param page: 第几页
        :return: TMDB信息列表
        """
        if not self.movie:
            return []
        from app.utils import TmdbTrendingCache
        cache_key = f"trending_week:{page}"
        cached = TmdbTrendingCache.get(cache_key)
        if cached is not None:
            return cached
        result = self.__dict_tmdbinfos(self.trending.all_week(page=page))
        TmdbTrendingCache.set(cache_key, result)
        return result

    def __get_tmdb_movie_detail(self, tmdbid, append_to_response=None):
        """
        获取电影的详情
        :param tmdbid: TMDB ID
        :return: TMDB信息
        """
        """
        {
          "adult": false,
          "backdrop_path": "/r9PkFnRUIthgBp2JZZzD380MWZy.jpg",
          "belongs_to_collection": {
            "id": 94602,
            "name": "穿靴子的猫（系列）",
            "poster_path": "/anHwj9IupRoRZZ98WTBvHpTiE6A.jpg",
            "backdrop_path": "/feU1DWV5zMWxXUHJyAIk3dHRQ9c.jpg"
          },
          "budget": 90000000,
          "genres": [
            {
              "id": 16,
              "name": "动画"
            },
            {
              "id": 28,
              "name": "动作"
            },
            {
              "id": 12,
              "name": "冒险"
            },
            {
              "id": 35,
              "name": "喜剧"
            },
            {
              "id": 10751,
              "name": "家庭"
            },
            {
              "id": 14,
              "name": "奇幻"
            }
          ],
          "homepage": "",
          "id": 315162,
          "imdb_id": "tt3915174",
          "original_language": "en",
          "original_title": "Puss in Boots: The Last Wish",
          "overview": "时隔11年，臭屁自大又爱卖萌的猫大侠回来了！如今的猫大侠（安东尼奥·班德拉斯 配音），依旧幽默潇洒又不拘小节、数次“花式送命”后，九条命如今只剩一条，于是不得不请求自己的老搭档兼“宿敌”——迷人的软爪妞（萨尔玛·海耶克 配音）来施以援手来恢复自己的九条生命。",
          "popularity": 8842.129,
          "poster_path": "/rnn30OlNPiC3IOoWHKoKARGsBRK.jpg",
          "production_companies": [
            {
              "id": 33,
              "logo_path": "/8lvHyhjr8oUKOOy2dKXoALWKdp0.png",
              "name": "Universal Pictures",
              "origin_country": "US"
            },
            {
              "id": 521,
              "logo_path": "/kP7t6RwGz2AvvTkvnI1uteEwHet.png",
              "name": "DreamWorks Animation",
              "origin_country": "US"
            }
          ],
          "production_countries": [
            {
              "iso_3166_1": "US",
              "name": "United States of America"
            }
          ],
          "release_date": "2022-12-07",
          "revenue": 260725470,
          "runtime": 102,
          "spoken_languages": [
            {
              "english_name": "English",
              "iso_639_1": "en",
              "name": "English"
            },
            {
              "english_name": "Spanish",
              "iso_639_1": "es",
              "name": "Español"
            }
          ],
          "status": "Released",
          "tagline": "",
          "title": "穿靴子的猫2",
          "video": false,
          "vote_average": 8.614,
          "vote_count": 2291
        }
        """
        if not self.movie:
            return {}
        try:
            log.info("【Meta】正在查询TMDB电影：%s ..." % tmdbid)
            tmdbinfo = self.movie.details(tmdbid, append_to_response)
            return tmdbinfo or {}
        except Exception as e:
            print(str(e))
            return None

    def __get_tmdb_tv_detail(self, tmdbid, append_to_response=None):
        """
        获取电视剧的详情
        :param tmdbid: TMDB ID
        :return: TMDB信息
        """
        """
        {
          "adult": false,
          "backdrop_path": "/uDgy6hyPd82kOHh6I95FLtLnj6p.jpg",
          "created_by": [
            {
              "id": 35796,
              "credit_id": "5e84f06a3344c600153f6a57",
              "name": "Craig Mazin",
              "gender": 2,
              "profile_path": "/uEhna6qcMuyU5TP7irpTUZ2ZsZc.jpg"
            },
            {
              "id": 1295692,
              "credit_id": "5e84f03598f1f10016a985c0",
              "name": "Neil Druckmann",
              "gender": 2,
              "profile_path": "/bVUsM4aYiHbeSYE1xAw2H5Z1ANU.jpg"
            }
          ],
          "episode_run_time": [],
          "first_air_date": "2023-01-15",
          "genres": [
            {
              "id": 18,
              "name": "剧情"
            },
            {
              "id": 10765,
              "name": "Sci-Fi & Fantasy"
            },
            {
              "id": 10759,
              "name": "动作冒险"
            }
          ],
          "homepage": "https://www.hbo.com/the-last-of-us",
          "id": 100088,
          "in_production": true,
          "languages": [
            "en"
          ],
          "last_air_date": "2023-01-15",
          "last_episode_to_air": {
            "air_date": "2023-01-15",
            "episode_number": 1,
            "id": 2181581,
            "name": "当你迷失在黑暗中",
            "overview": "在一场全球性的流行病摧毁了文明之后，一个顽强的幸存者负责照顾一个 14 岁的小女孩，她可能是人类最后的希望。",
            "production_code": "",
            "runtime": 81,
            "season_number": 1,
            "show_id": 100088,
            "still_path": "/aRquEWm8wWF1dfa9uZ1TXLvVrKD.jpg",
            "vote_average": 8,
            "vote_count": 33
          },
          "name": "最后生还者",
          "next_episode_to_air": {
            "air_date": "2023-01-22",
            "episode_number": 2,
            "id": 4071039,
            "name": "虫草变异菌",
            "overview": "",
            "production_code": "",
            "runtime": 55,
            "season_number": 1,
            "show_id": 100088,
            "still_path": "/jkUtYTmeap6EvkHI4n0j5IRFrIr.jpg",
            "vote_average": 10,
            "vote_count": 1
          },
          "networks": [
            {
              "id": 49,
              "name": "HBO",
              "logo_path": "/tuomPhY2UtuPTqqFnKMVHvSb724.png",
              "origin_country": "US"
            }
          ],
          "number_of_episodes": 9,
          "number_of_seasons": 1,
          "origin_country": [
            "US"
          ],
          "original_language": "en",
          "original_name": "The Last of Us",
          "overview": "不明真菌疫情肆虐之后的美国，被真菌感染的人都变成了可怕的怪物，乔尔（Joel）为了换回武器答应将小女孩儿艾莉（Ellie）送到指定地点，由此开始了两人穿越美国的漫漫旅程。",
          "popularity": 5585.639,
          "poster_path": "/nOY3VBFO0VnlN9nlRombnMTztyh.jpg",
          "production_companies": [
            {
              "id": 3268,
              "logo_path": "/tuomPhY2UtuPTqqFnKMVHvSb724.png",
              "name": "HBO",
              "origin_country": "US"
            },
            {
              "id": 11073,
              "logo_path": "/aCbASRcI1MI7DXjPbSW9Fcv9uGR.png",
              "name": "Sony Pictures Television Studios",
              "origin_country": "US"
            },
            {
              "id": 23217,
              "logo_path": "/kXBZdQigEf6QiTLzo6TFLAa7jKD.png",
              "name": "Naughty Dog",
              "origin_country": "US"
            },
            {
              "id": 115241,
              "logo_path": null,
              "name": "The Mighty Mint",
              "origin_country": "US"
            },
            {
              "id": 119645,
              "logo_path": null,
              "name": "Word Games",
              "origin_country": "US"
            },
            {
              "id": 125281,
              "logo_path": "/3hV8pyxzAJgEjiSYVv1WZ0ZYayp.png",
              "name": "PlayStation Productions",
              "origin_country": "US"
            }
          ],
          "production_countries": [
            {
              "iso_3166_1": "US",
              "name": "United States of America"
            }
          ],
          "seasons": [
            {
              "air_date": "2023-01-15",
              "episode_count": 9,
              "id": 144593,
              "name": "第 1 季",
              "overview": "",
              "poster_path": "/aUQKIpZZ31KWbpdHMCmaV76u78T.jpg",
              "season_number": 1
            }
          ],
          "spoken_languages": [
            {
              "english_name": "English",
              "iso_639_1": "en",
              "name": "English"
            }
          ],
          "status": "Returning Series",
          "tagline": "",
          "type": "Scripted",
          "vote_average": 8.924,
          "vote_count": 601
        }
        """
        if not self.tv:
            return {}
        try:
            log.info("【Meta】正在查询TMDB电视剧：%s ..." % tmdbid)
            tmdbinfo = self.tv.details(tmdbid, append_to_response)
            return tmdbinfo or {}
        except Exception as e:
            print(str(e))
            return None

    def get_tmdb_tv_season_detail(self, tmdbid, season: int):
        """
        获取电视剧季的详情
        :param tmdbid: TMDB ID
        :param season: 季，数字
        :return: TMDB信息
        """
        if not self.tv:
            return {}
        from app.utils import TmdbSeasonDetailCache
        cache_key = f"season_detail:{tmdbid}:{season}"
        cached = TmdbSeasonDetailCache.get(cache_key)
        if cached is not None:
            return cached
        try:
            log.info("【Meta】正在查询TMDB电视剧：%s，季：%s ..." % (tmdbid, season))
            tmdbinfo = self.tv.season_details(tmdbid, season)
            if tmdbinfo:
                TmdbSeasonDetailCache.set(cache_key, tmdbinfo)
            return tmdbinfo or {}
        except Exception as e:
            print(str(e))
            return {}

    def get_tmdb_tv_seasons_byid(self, tmdbid):
        """
        根据TMDB查询TMDB电视剧的所有季
        """
        if not tmdbid:
            return []
        return self.get_tmdb_tv_seasons(
            tv_info=self.__get_tmdb_tv_detail(
                tmdbid=tmdbid
            )
        )

    @staticmethod
    def get_tmdb_tv_seasons(tv_info):
        """
        查询TMDB电视剧的所有季
        :param tv_info: TMDB 的季信息
        :return: 带有season_number、episode_count 的每季总集数的字典列表
        """
        """
        "seasons": [
            {
              "air_date": "2006-01-08",
              "episode_count": 11,
              "id": 3722,
              "name": "特别篇",
              "overview": "",
              "poster_path": "/snQYndfsEr3Sto2jOmkmsQuUXAQ.jpg",
              "season_number": 0
            },
            {
              "air_date": "2005-03-27",
              "episode_count": 9,
              "id": 3718,
              "name": "第 1 季",
              "overview": "",
              "poster_path": "/foM4ImvUXPrD2NvtkHyixq5vhPx.jpg",
              "season_number": 1
            }
        ]
        """
        if not tv_info:
            return []
        return tv_info.get("seasons") or []

    def get_tmdb_season_episodes(self, tmdbid, season: int):
        """
        :param: tmdbid: TMDB ID
        :param: season: 季号
        """
        """
        从TMDB的季集信息中获得某季的集信息
        """
        """
        "episodes": [
            {
              "air_date": "2023-01-15",
              "episode_number": 1,
              "id": 2181581,
              "name": "当你迷失在黑暗中",
              "overview": "在一场全球性的流行病摧毁了文明之后，一个顽强的幸存者负责照顾一个 14 岁的小女孩，她可能是人类最后的希望。",
              "production_code": "",
              "runtime": 81,
              "season_number": 1,
              "show_id": 100088,
              "still_path": "/aRquEWm8wWF1dfa9uZ1TXLvVrKD.jpg",
              "vote_average": 8,
              "vote_count": 33
            },
          ]
        """
        if not tmdbid:
            return []
        season_info = self.get_tmdb_tv_season_detail(tmdbid=tmdbid, season=season)
        if not season_info:
            return []
        return season_info.get("episodes") or []

    @staticmethod
    def get_tmdb_backdrops(tmdbinfo):
        """
        获取TMDB的背景图
        """
        """
        {
          "backdrops": [
            {
              "aspect_ratio": 1.778,
              "height": 2160,
              "iso_639_1": "en",
              "file_path": "/qUroDlCDUMwRWbkyjZGB9THkMgZ.jpg",
              "vote_average": 5.312,
              "vote_count": 1,
              "width": 3840
            },
            {
              "aspect_ratio": 1.778,
              "height": 2160,
              "iso_639_1": "en",
              "file_path": "/iyxvxEQIfQjzJJTfszZxmH5UV35.jpg",
              "vote_average": 0,
              "vote_count": 0,
              "width": 3840
            },
            {
              "aspect_ratio": 1.778,
              "height": 720,
              "iso_639_1": "en",
              "file_path": "/8SRY6IcMKO1E5p83w7bjvcqklp9.jpg",
              "vote_average": 0,
              "vote_count": 0,
              "width": 1280
            },
            {
              "aspect_ratio": 1.778,
              "height": 1080,
              "iso_639_1": "en",
              "file_path": "/erkJ7OxJWFdLBOcn2MvIdhTLHTu.jpg",
              "vote_average": 0,
              "vote_count": 0,
              "width": 1920
            }
          ]
        }
        """
        if not tmdbinfo:
            return []
        backdrops = tmdbinfo.get("images", {}).get("backdrops") or []
        result = [TMDB_IMAGE_ORIGINAL_URL % backdrop.get("file_path") for backdrop in backdrops]
        result.append(TMDB_IMAGE_ORIGINAL_URL % tmdbinfo.get("backdrop_path"))
        return result

    @staticmethod
    def get_tmdb_season_episodes_num(tv_info, season: int):
        """
        从TMDB的季信息中获得具体季有多少集
        :param season: 季号，数字
        :param tv_info: 已获取的TMDB季的信息
        :return: 该季的总集数
        """
        if not tv_info:
            return 0
        seasons = tv_info.get("seasons")
        if not seasons:
            return 0
        for sea in seasons:
            if sea.get("season_number") == int(season):
                return int(sea.get("episode_count"))
        return 0

    @staticmethod
    def __dict_media_crews(crews):
        """
        字典化媒体工作人员
        """
        return [{
            "id": crew.get("id"),
            "gender": crew.get("gender"),
            "known_for_department": crew.get("known_for_department"),
            "name": crew.get("name"),
            "original_name": crew.get("original_name"),
            "popularity": crew.get("popularity"),
            "image": TMDB_IMAGE_FACE_URL % crew.get("profile_path"),
            "credit_id": crew.get("credit_id"),
            "department": crew.get("department"),
            "job": crew.get("job"),
            "profile": TMDB_PEOPLE_PROFILE_URL % crew.get('id')
        } for crew in crews or []]

    @staticmethod
    def __dict_media_casts(casts):
        """
        字典化媒体演职人员
        """
        return [{
            "id": cast.get("id"),
            "gender": cast.get("gender"),
            "known_for_department": cast.get("known_for_department"),
            "name": cast.get("name"),
            "original_name": cast.get("original_name"),
            "popularity": cast.get("popularity"),
            "image": TMDB_IMAGE_FACE_URL % cast.get("profile_path"),
            "cast_id": cast.get("cast_id"),
            "role": cast.get("character"),
            "credit_id": cast.get("credit_id"),
            "order": cast.get("order"),
            "profile": TMDB_PEOPLE_PROFILE_URL % cast.get('id')
        } for cast in casts or []]

    def get_tmdb_directors_actors(self, tmdbinfo):
        """
        查询导演和演员
        :param tmdbinfo: TMDB元数据
        :return: 导演列表，演员列表
        """
        """
        "cast": [
          {
            "adult": false,
            "gender": 2,
            "id": 3131,
            "known_for_department": "Acting",
            "name": "Antonio Banderas",
            "original_name": "Antonio Banderas",
            "popularity": 60.896,
            "profile_path": "/iWIUEwgn2KW50MssR7tdPeFoRGW.jpg",
            "cast_id": 2,
            "character": "Puss in Boots (voice)",
            "credit_id": "6052480e197de4006bb47b9a",
            "order": 0
          }
        ],
        "crew": [
          {
            "adult": false,
            "gender": 2,
            "id": 5524,
            "known_for_department": "Production",
            "name": "Andrew Adamson",
            "original_name": "Andrew Adamson",
            "popularity": 9.322,
            "profile_path": "/qqIAVKAe5LHRbPyZUlptsqlo4Kb.jpg",
            "credit_id": "63b86b2224b33300a0585bf1",
            "department": "Production",
            "job": "Executive Producer"
          }
        ]
        """
        if not tmdbinfo:
            return [], []
        _credits = tmdbinfo.get("credits")
        if not _credits:
            return [], []
        directors = []
        actors = []
        for cast in self.__dict_media_casts(_credits.get("cast")):
            if cast.get("known_for_department") == "Acting":
                actors.append(cast)
        for crew in self.__dict_media_crews(_credits.get("crew")):
            if crew.get("job") == "Director":
                directors.append(crew)
        return directors, actors

    def get_tmdb_cats(self, mtype, tmdbid):
        """
        获取TMDB的演员列表
        :param: mtype: 媒体类型
        :param: tmdbid: TMDBID
        """
        try:
            if mtype == MediaType.MOVIE:
                if not self.movie:
                    return []
                return self.__dict_media_casts(self.movie.credits(tmdbid).get("cast"))
            else:
                if not self.tv:
                    return []
                return self.__dict_media_casts(self.tv.credits(tmdbid).get("cast"))
        except Exception as err:
            print(str(err))
        return []

    @staticmethod
    def get_tmdb_genres_names(tmdbinfo):
        """
        从TMDB数据中获取风格名称
        """
        """
        "genres": [
            {
              "id": 16,
              "name": "动画"
            },
            {
              "id": 28,
              "name": "动作"
            },
            {
              "id": 12,
              "name": "冒险"
            },
            {
              "id": 35,
              "name": "喜剧"
            },
            {
              "id": 10751,
              "name": "家庭"
            },
            {
              "id": 14,
              "name": "奇幻"
            }
          ]
        """
        if not tmdbinfo:
            return ""
        genres = tmdbinfo.get("genres") or []
        genres_list = [genre.get("name") for genre in genres]
        return ", ".join(genres_list) if genres_list else ""

    def get_tmdb_genres(self, mtype):
        """
        获取TMDB的风格列表
        :param: mtype: 媒体类型
        """
        if not self.genre:
            return []
        try:
            if mtype == MediaType.MOVIE:
                return self.genre.movie_list()
            else:
                return self.genre.tv_list()
        except Exception as err:
            print(str(err))
        return []

    @staticmethod
    def get_get_production_country_names(tmdbinfo):
        """
        从TMDB数据中获取制片国家名称
        """
        """
        "production_countries": [
            {
              "iso_3166_1": "US",
              "name": "美国"
            }
          ]
        """
        if not tmdbinfo:
            return ""
        countries = tmdbinfo.get("production_countries") or []
        countries_list = [country.get("name") for country in countries]
        return ", ".join(countries_list) if countries_list else ""

    @staticmethod
    def get_tmdb_production_company_names(tmdbinfo):
        """
        从TMDB数据中获取制片公司名称
        """
        """
        "production_companies": [
            {
              "id": 2,
              "logo_path": "/wdrCwmRnLFJhEoH8GSfymY85KHT.png",
              "name": "DreamWorks Animation",
              "origin_country": "US"
            }
          ]
        """
        if not tmdbinfo:
            return ""
        companies = tmdbinfo.get("production_companies") or []
        companies_list = [company.get("name") for company in companies]
        return ", ".join(companies_list) if companies_list else ""

    @staticmethod
    def get_tmdb_crews(tmdbinfo, nums=None):
        """
        从TMDB数据中获取制片人员
        """
        if not tmdbinfo:
            return ""
        crews = tmdbinfo.get("credits", {}).get("crew") or []
        result = [{crew.get("name"): crew.get("job")} for crew in crews]
        if nums:
            return result[:nums]
        else:
            return result

    def get_tmdb_en_title(self, media_info):
        """
        获取TMDB的英文名称
        """
        from app.utils import TmdbEnTitleCache
        cache_key = f"en_title:{media_info.type.value}:{media_info.tmdb_id}"
        cached = TmdbEnTitleCache.get(cache_key)
        if cached is not None:
            return cached
        en_info = self.get_tmdb_info(mtype=media_info.type,
                                     tmdbid=media_info.tmdb_id,
                                     language="en-US")
        if en_info:
            title = en_info.get("title") if media_info.type == MediaType.MOVIE else en_info.get("name")
            TmdbEnTitleCache.set(cache_key, title)
            return title
        return None

    def get_episode_title(self, media_info):
        """
        获取剧集的标题
        """
        if media_info.type == MediaType.MOVIE:
            return None
        if media_info.tmdb_id:
            if not media_info.begin_episode:
                return None
            episodes = self.get_tmdb_season_episodes(tmdbid=media_info.tmdb_id,
                                                     season=int(media_info.get_season_seq()))
            for episode in episodes:
                if episode.get("episode_number") == media_info.begin_episode:
                    return episode.get("name")
        return None

    def get_movie_discover(self, page=1):
        """
        发现电影
        """
        if not self.movie:
            return []
        try:
            movies = self.movie.discover(page)
            if movies:
                return movies.get("results")
        except Exception as e:
            print(str(e))
        return []

    def get_movie_similar(self, tmdbid, page=1):
        """
        查询类似电影
        """
        if not self.movie:
            return []
        try:
            movies = self.movie.similar(movie_id=tmdbid, page=page) or []
            return self.__dict_tmdbinfos(movies, MediaType.MOVIE)
        except Exception as e:
            print(str(e))
            return []

    def get_movie_recommendations(self, tmdbid, page=1):
        """
        查询电影关联推荐
        """
        if not self.movie:
            return []
        try:
            movies = self.movie.recommendations(movie_id=tmdbid, page=page) or []
            return self.__dict_tmdbinfos(movies, MediaType.MOVIE)
        except Exception as e:
            print(str(e))
            return []

    def get_tv_similar(self, tmdbid, page=1):
        """
        查询类似电视剧
        """
        if not self.tv:
            return []
        try:
            tvs = self.tv.similar(tv_id=tmdbid, page=page) or []
            return self.__dict_tmdbinfos(tvs, MediaType.TV)
        except Exception as e:
            print(str(e))
            return []

    def get_tv_recommendations(self, tmdbid, page=1):
        """
        查询电视剧关联推荐
        """
        if not self.tv:
            return []
        try:
            tvs = self.tv.recommendations(tv_id=tmdbid, page=page) or []
            return self.__dict_tmdbinfos(tvs, MediaType.TV)
        except Exception as e:
            print(str(e))
            return []

    def get_tmdb_discover(self, mtype, params=None, page=1):
        """
        浏览电影、电视剧（复杂过滤条件）
        """
        if not self.discover:
            return []
        try:
            if mtype == MediaType.MOVIE:
                movies = self.discover.discover_movies(params=params, page=page)
                return self.__dict_tmdbinfos(movies, mtype)
            elif mtype == MediaType.TV:
                tvs = self.discover.discover_tv_shows(params=params, page=page)
                return self.__dict_tmdbinfos(tvs, mtype)
        except Exception as e:
            print(str(e))
        return []

    def get_person_medias(self, personid, mtype, page=1):
        """
        查询人物相关影视作品
        """
        if not self.person:
            return []
        result = []
        try:
            if mtype == MediaType.MOVIE:
                movies = self.person.movie_credits(person_id=personid) or []
                result = self.__dict_tmdbinfos(movies, mtype)
            elif mtype == MediaType.TV:
                tvs = self.person.tv_credits(person_id=personid) or []
                result = self.__dict_tmdbinfos(tvs, mtype)
            return result[(page - 1) * 20: page * 20]
        except Exception as e:
            print(str(e))
        return []

    @staticmethod
    def __get_genre_ids_from_detail(genres):
        """
        从TMDB详情中获取genre_id列表
        """
        if not genres:
            return []
        genre_ids = []
        for genre in genres:
            genre_ids.append(genre.get('id'))
        return genre_ids

    @staticmethod
    def __get_tmdb_chinese_title(tmdbinfo):
        """
        从别名中获取中文标题
        """
        if not tmdbinfo:
            return None
        if tmdbinfo.get("media_type") == MediaType.MOVIE:
            alternative_titles = tmdbinfo.get("alternative_titles", {}).get("titles", [])
        else:
            alternative_titles = tmdbinfo.get("alternative_titles", {}).get("results", [])
        for alternative_title in alternative_titles:
            iso_3166_1 = alternative_title.get("iso_3166_1")
            if iso_3166_1 == "CN":
                title = alternative_title.get("title")
                if title and StringUtils.is_chinese(title) and zhconv.convert(title, "zh-hans") == title:
                    return title
        return tmdbinfo.get("title") if tmdbinfo.get("media_type") == MediaType.MOVIE else tmdbinfo.get("name")

    def get_tmdbperson_chinese_name(self, person_id):
        """
        查询TMDB人物中文名称
        """
        if not self.person:
            return ""
        alter_names = []
        name = ""
        try:
            aka_names = self.person.details(person_id).get("also_known_as", []) or []
        except Exception as err:
            print(str(err))
            return ""
        for aka_name in aka_names:
            if StringUtils.is_chinese(aka_name):
                alter_names.append(aka_name)
        if len(alter_names) == 1:
            name = alter_names[0]
        elif len(alter_names) > 1:
            for alter_name in alter_names:
                if alter_name == zhconv.convert(alter_name, 'zh-hans'):
                    name = alter_name
        return name

    def get_tmdbperson_aka_names(self, person_id):
        """
        查询人物又名
        """
        if not self.person:
            return []
        try:
            aka_names = self.person.details(person_id).get("also_known_as", []) or []
            return aka_names
        except Exception as err:
            print(str(err))
            return []

    def get_random_discover_backdrop(self):
        """
        获取TMDB热门电影随机一张背景图
        """
        movies = self.get_movie_discover()
        if movies:
            backdrops = [movie.get("backdrop_path") for movie in movies]
            return TMDB_IMAGE_ORIGINAL_URL % backdrops[round(random.uniform(0, len(backdrops) - 1))]
        return ""

    def save_rename_cache(self, file_name, cache_info):
        """
        将手动识别的信息加入缓存
        """
        if not file_name or not cache_info:
            return
        meta_info = MetaInfo(title=file_name, include_ai=False, record=False)
        self.__insert_media_cache(self.__make_cache_key(meta_info), cache_info)

    @staticmethod
    def merge_media_info(target, source):
        """
        将soruce中有效的信息合并到target中并返回
        """
        target.set_tmdb_info(source.tmdb_info)
        target.fanart_poster = source.get_poster_image()
        target.fanart_backdrop = source.get_backdrop_image()
        target.set_download_info(download_setting=source.download_setting,
                                 save_path=source.save_path)
        return target

    def get_tmdbid_by_imdbid(self, imdbid):
        """
        根据IMDBID查询TMDB信息
        """
        if not self.find:
            return None
        try:
            result = self.find.find_by_imdbid(imdbid) or {}
            tmdbinfo = result.get('movie_results') or result.get("tv_results")
            if tmdbinfo:
                tmdbinfo = tmdbinfo[0]
                return tmdbinfo.get("id")
        except Exception as err:
            print(str(err))
        return None

    @staticmethod
    def get_detail_url(mtype, tmdbid):
        """
        获取TMDB/豆瓣详情页地址
        """
        if not tmdbid:
            return ""
        if str(tmdbid).startswith("DB:"):
            return "https://movie.douban.com/subject/%s" % str(tmdbid).replace("DB:", "")
        elif mtype == MediaType.MOVIE:
            return "https://www.themoviedb.org/movie/%s" % tmdbid
        else:
            return "https://www.themoviedb.org/tv/%s" % tmdbid

    def get_episode_images(self, tv_id, season_id, episode_id, orginal=False):
        """
        获取剧集中某一集封面
        """
        if not self.episode:
            return ""
        res = self.episode.images(tv_id, season_id, episode_id)
        if res:
            if orginal:
                return TMDB_IMAGE_ORIGINAL_URL % res[0].get("file_path")
            else:
                return TMDB_IMAGE_W500_URL % res[0].get("file_path")
        else:
            return ""

    def get_tmdb_factinfo(self, media_info):
        """
        获取TMDB发布信息
        """
        result = []
        if media_info.vote_average:
            result.append({"评分": media_info.vote_average})
        if media_info.original_title:
            result.append({"原始标题": media_info.original_title})
        status = media_info.tmdb_info.get("status")
        if status:
            result.append({"状态": status})
        if media_info.release_date:
            result.append({"上映日期": media_info.release_date})
        revenue = media_info.tmdb_info.get("revenue")
        if revenue:
            result.append({"收入": StringUtils.str_amount(revenue)})
        budget = media_info.tmdb_info.get("budget")
        if budget:
            result.append({"成本": StringUtils.str_amount(budget)})
        if media_info.original_language:
            result.append({"原始语言": media_info.original_language})
        production_country = self.get_get_production_country_names(tmdbinfo=media_info.tmdb_info)
        if production_country:
            result.append({"出品国家": production_country})
        production_company = self.get_tmdb_production_company_names(tmdbinfo=media_info.tmdb_info)
        if production_company:
            result.append({"制作公司": production_company})

        return result
