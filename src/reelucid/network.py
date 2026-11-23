"""Source-only HTTP transport. IP validation happens on the socket actually connected."""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import urllib.request
from http.cookiejar import CookieJar
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from reelucid.domain import ReelucidError

if TYPE_CHECKING:
    from reelucid.source_proxy import SourceProxy


def validate_url(url: str) -> None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ReelucidError("INVALID_URL", "URL 格式错误。") from exc
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise ReelucidError("INVALID_URL", "仅支持不含用户名密码的 HTTP(S) URL。")
    if port is not None and port not in {80, 443}:
        raise ReelucidError("NETWORK_POLICY_BLOCKED", "视频源仅允许标准 HTTP/HTTPS 端口。")


def public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        address = address.ipv4_mapped
    return address.is_global and not address.is_multicast and not address.is_unspecified


def checked_connection(
    address: tuple[str, int],
    timeout: float | object = 30.0,
    source_address: tuple[str, int] | None = None,
    **_kwargs: Any,
) -> socket.socket:
    host, port = address
    candidates = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not candidates or any(not public_ip(str(item[4][0])) for item in candidates):
        raise ReelucidError(
            "NETWORK_POLICY_BLOCKED", "视频源解析到内网、回环或保留地址，已阻止连接。"
        )
    last_error: OSError | None = None
    for family, kind, protocol, _name, endpoint in candidates:
        connection = socket.socket(family, kind, protocol)
        try:
            connection.settimeout(float(timeout) if isinstance(timeout, (int, float)) else 30.0)
            if source_address:
                connection.bind(source_address)
            connection.connect(endpoint)
            return connection
        except OSError as exc:
            last_error = exc
            connection.close()
    raise last_error or OSError("No reachable public address")


class CheckedHTTPConnection(http.client.HTTPConnection):
    _tunnel_host: str | None
    source_address: tuple[str, int] | None

    def __init__(self, *args: Any, proxy: SourceProxy | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.proxy = proxy

    def connect(self) -> None:
        if self._tunnel_host:
            raise ReelucidError("NETWORK_POLICY_UNSUPPORTED", "来源请求不允许隧道代理。")
        self.sock = (
            self.proxy.connect(self.host, self.port, float(self.timeout or 30))
            if self.proxy is not None
            else checked_connection((self.host, self.port), self.timeout, self.source_address)
        )


class CheckedHTTPSConnection(http.client.HTTPSConnection):
    _tunnel_host: str | None
    _context: ssl.SSLContext
    source_address: tuple[str, int] | None

    def __init__(self, *args: Any, proxy: SourceProxy | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.proxy = proxy

    def connect(self) -> None:
        if self._tunnel_host:
            raise ReelucidError("NETWORK_POLICY_UNSUPPORTED", "来源请求不允许隧道代理。")
        raw = (
            self.proxy.connect(self.host, self.port, float(self.timeout or 30))
            if self.proxy is not None
            else checked_connection((self.host, self.port), self.timeout, self.source_address)
        )
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except Exception:
            raw.close()
            raise


class HTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, proxy: SourceProxy | None = None) -> None:
        super().__init__()
        self.proxy = proxy

    def http_open(self, req: urllib.request.Request) -> Any:
        validate_url(req.full_url)
        return self.do_open(CheckedHTTPConnection, req, proxy=self.proxy)


class HTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, proxy: SourceProxy | None = None) -> None:
        super().__init__()
        self.proxy = proxy

    def https_open(self, req: urllib.request.Request) -> Any:
        validate_url(req.full_url)
        return self.do_open(
            CheckedHTTPSConnection, req, context=ssl.create_default_context(), proxy=self.proxy
        )


class RedirectHandler(urllib.request.HTTPRedirectHandler):
    max_redirections = 5

    def __init__(self, allowed_hosts: frozenset[str] | None = None) -> None:
        self.allowed_hosts = allowed_hosts

    def redirect_request(
        self, req: urllib.request.Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> urllib.request.Request | None:
        validate_url(newurl)
        if self.allowed_hosts is not None and urlsplit(newurl).hostname not in self.allowed_hosts:
            raise ReelucidError("UNSUPPORTED_SOURCE", "分享链接跳转到了其他网站。")
        result = super().redirect_request(req, fp, code, msg, headers, newurl)
        if result and urlsplit(req.full_url).hostname != urlsplit(newurl).hostname:
            for key in ("Authorization", "Cookie", "Proxy-authorization"):
                result.remove_header(key)
        return result


def source_opener(
    cookiejar: CookieJar | None = None,
    *,
    redirect_hosts: frozenset[str] | None = None,
    proxy: SourceProxy | None = None,
) -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        HTTPHandler(proxy),
        HTTPSHandler(proxy),
        RedirectHandler(redirect_hosts),
        urllib.request.HTTPCookieProcessor(cookiejar or CookieJar()),
    )
