"""后端固定目标的网络诊断：每个目标执行一次 GET，不接受外部 URL。"""

import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from time import perf_counter

import requests

from config import Config, FANART_MOVIE_API_URL


@dataclass(frozen=True)
class _Target:
    name: str
    url: str
    proxy: bool = False
    json_field: str = ""


_TARGETS = (
    _Target("www.themoviedb.org", "https://www.themoviedb.org", True),
    _Target("api.themoviedb.org", "https://api.themoviedb.org/3/configuration", True, "images"),
    _Target("api.tmdb.org", "https://api.tmdb.org/3/configuration", True, "images"),
    _Target("image.tmdb.org", "https://image.tmdb.org/t/p/w500/wwemzKWzjKYJFfCeiB57q3r4Bcm.png", True),
    _Target("webservice.fanart.tv", FANART_MOVIE_API_URL % "550", True, "tmdb_id"),
    _Target("api.telegram.org", "https://api.telegram.org", True),
    _Target("qyapi.weixin.qq.com", "https://qyapi.weixin.qq.com/cgi-bin/gettoken"),
    _Target("www.opensubtitles.org", "https://www.opensubtitles.org"),
)


class NetworkTest:
    def __init__(self):
        config = Config()
        app = config.get_config("app")
        self._keys = [key.strip() for key in (app.get("rmt_tmdbkey") or "").split(";") if key.strip()]
        self._proxies = config.get_proxies()

    def run(self):
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="network-test") as executor:
            results = list(executor.map(self._check, _TARGETS))
        return {"results": results}

    def _safe_reason(self, reason):
        reason = str(reason)
        for key in self._keys:
            reason = reason.replace(key, "[已隐藏]")
        reason = re.sub(r"(?i)(api_key=)[^&\s)'\"<>]+", r"\1[已隐藏]", reason)
        reason = re.sub(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s@]+@", r"\1[已隐藏]@", reason)
        return reason[:1000]

    def _check(self, target):
        start = perf_counter()
        result = {"target": target.name, "res": False, "time": "", "reason": "", "status_code": None}
        params = {"api_key": self._keys[0]} if target.json_field == "images" and self._keys else None
        try:
            # 独立会话避免诊断请求共享业务 cookies；默认不重试，禁止跳转到其他 URL。
            with requests.Session() as session:
                response = session.get(
                    target.url,
                    params=params,
                    headers={"User-Agent": "NAS-Tools/network-test", "Accept": "*/*"},
                    proxies=self._proxies if target.proxy else None,
                    timeout=5,
                    allow_redirects=False,
                )
            if response is None:
                result["reason"] = "请求失败，未收到 HTTP 响应"
            else:
                status = response.status_code
                result["status_code"] = status
                result["res"] = 200 <= status < 400
                if status >= 300:
                    result["reason"] = f"HTTP {status} {response.reason}".strip()
                if target.json_field:
                    result["res"] = False
                    if 200 <= status < 300:
                        try:
                            payload = response.json()
                        except ValueError:
                            result["reason"] = "已收到 HTTP 响应，但返回内容不是有效的 API JSON 数据"
                        else:
                            if isinstance(payload, dict) and target.json_field in payload:
                                result["res"] = True
                            else:
                                result["reason"] = "已收到 HTTP 响应，但返回内容不是预期的 API 数据"
                    elif target.json_field == "images" and not self._keys:
                        result["reason"] += "；未配置 TMDB API Key"
        except requests.RequestException as error:
            result["reason"] = f"{type(error).__name__}: {error}"
        result["reason"] = self._safe_reason(result["reason"])
        result["time"] = f"{int((perf_counter() - start) * 1000)} 毫秒"
        return result
