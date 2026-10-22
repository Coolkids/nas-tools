"""Cookie-session browser access, independent of token-only client APIs."""

from functools import wraps
from urllib.parse import urlparse

from flask import request
from flask_login import current_user


def is_same_origin_request(strict=False):
    source = request.headers.get("Origin") or request.headers.get("Referer")
    if not source:
        return False
    try:
        supplied, expected = urlparse(source), urlparse(request.host_url)
        if supplied.scheme not in {"http", "https"} or not supplied.hostname or supplied.username or supplied.password:
            return False
        supplied_port = supplied.port or (443 if supplied.scheme == "https" else 80)
        expected_port = request.headers.get("X-Forwarded-Port") or expected.port
        if strict:
            scheme = request.headers.get("X-Forwarded-Proto", expected.scheme)
            if scheme not in {"http", "https"} or supplied.scheme != scheme:
                return False
            expected_port = expected_port or (443 if scheme == "https" else 80)
        if expected_port is not None and supplied_port != int(expected_port):
            return False
        if expected_port is None and supplied.port is not None and supplied_port not in {80, 443}:
            return False
        return supplied.hostname.lower() == (expected.hostname or "").lower()
    except ValueError:
        return False


class BrowserAPIError(Exception):
    def __init__(self, code, message, status=400, details=None):
        self.code, self.message, self.status, self.details = code, message, status, details
        super().__init__(code)


def session_route(administrator=False):
    def decorate(method):
        @wraps(method)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                raise BrowserAPIError("AUTHENTICATION_REQUIRED", "请先登录", 401)
            if administrator and str(current_user.get_id()) != "0":
                raise BrowserAPIError("ADMINISTRATOR_REQUIRED", "仅管理员可以执行此操作", 403)
            supplied = request.headers.get("Origin") or request.headers.get("Referer")
            if ((request.method not in {"GET", "HEAD", "OPTIONS"} or supplied)
                    and not is_same_origin_request(strict=True)) or request.headers.get("Sec-Fetch-Site") == "cross-site":
                raise BrowserAPIError("ORIGIN_NOT_ALLOWED", "请求来源与当前站点不一致", 403)
            if request.content_length and request.content_length > 128*1024:
                raise BrowserAPIError("PAYLOAD_TOO_LARGE", "请求内容过大", 413)
            try:
                return method(*args, **kwargs)
            finally:
                # In particular, do not retain a user-loader ORM session for SSE.
                from app.db import close_db
                close_db()
        return wrapped
    return decorate
