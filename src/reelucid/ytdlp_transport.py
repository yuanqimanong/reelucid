"""The pinned yt-dlp request and native-downloader integration points."""

from __future__ import annotations

import io
import urllib.error
import urllib.request
from functools import partial
from typing import Any, cast

from yt_dlp import YoutubeDL
from yt_dlp.downloader.hls import HlsFD
from yt_dlp.networking.common import Request, RequestHandler, Response
from yt_dlp.networking.exceptions import HTTPError, TransportError

from reelucid.domain import JsonObject, ReelucidError
from reelucid.network import source_opener, validate_url
from reelucid.source_common import DownloadDeadline
from reelucid.source_proxy import SourceProxy
from reelucid.source_response import adapt_source_response


class CheckedRH(RequestHandler):
    def __init__(
        self, *, deadline: DownloadDeadline, source_proxy: SourceProxy | None, **kwargs: Any
    ) -> None:
        super().__init__(**kwargs)
        self.deadline = deadline
        self.source_proxy = source_proxy

    @property
    def RH_NAME(self) -> str:
        return "ReelucidCheckedHTTP"

    _SUPPORTED_URL_SCHEMES = ("http", "https")
    _SUPPORTED_PROXY_SCHEMES: tuple[str, ...] = ()

    def _check_extensions(self, extensions: JsonObject) -> None:
        for key in ("cookiejar", "timeout", "keep_header_casing"):
            extensions.pop(key, None)

    def _send(self, request: Request) -> Response:
        self.deadline.check()
        validate_url(request.url)
        headers = {
            **dict(self.headers),
            **dict(request.headers),
            "Accept-Encoding": "identity",
        }
        native = urllib.request.Request(
            request.url, data=request.data, headers=headers, method=request.method
        )
        opener = source_opener(
            request.extensions.get("cookiejar") or self.cookiejar, proxy=self.source_proxy
        )
        try:
            response = opener.open(
                native,
                timeout=float(request.extensions.get("timeout") or self.timeout or 30),
            )
        except urllib.error.HTTPError as exc:
            raise HTTPError(
                Response(cast(io.IOBase, exc), exc.url, dict(exc.headers), exc.code)
            ) from exc
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise TransportError("Source connection failed") from exc
        return adapt_source_response(
            cast(io.IOBase, response),
            response.url,
            response.headers,
            response.status,
            method=request.method,
        )


class CheckedYoutubeDL(YoutubeDL):
    def __init__(
        self, params: JsonObject, *, deadline: DownloadDeadline, proxy: SourceProxy | None
    ) -> None:
        self.deadline = deadline
        self.source_proxy = proxy
        super().__init__(cast(Any, params))

    def build_request_director(self, handlers: Any, preferences: Any = None) -> Any:
        # The locked yt-dlp version accepts handler factories. Every request from this
        # downloader uses the same proxy and deadline, including playlist redirects.
        handler = partial(CheckedRH, deadline=self.deadline, source_proxy=self.source_proxy)
        return cast(Any, super()).build_request_director([handler])

    def dl(self, name: str, info: Any, subtitle: bool = False, test: bool = False) -> Any:
        # Also blocks native HLS's implicit fallback to FFmpeg networking.
        self.deadline.check()
        if info.get("protocol", "https") not in {
            "http",
            "https",
            "m3u8_native",
            "http_dash_segments",
        }:
            raise ReelucidError("NETWORK_POLICY_UNSUPPORTED", "该来源需要未纳管的下载协议。")
        if info.get("protocol") == "m3u8_native":
            manifest = info.get("hls_media_playlist_data")
            if not manifest:
                with self.urlopen(
                    cast(Any, Request(info["url"], headers=info.get("http_headers")))
                ) as response:
                    payload = response.read(8 * 1024**2 + 1)
                    if len(payload) > 8 * 1024**2:
                        raise ReelucidError("SIZE_LIMIT_EXCEEDED", "HLS 清单超过体积上限。")
                    manifest = payload.decode("utf-8", "replace")
                    info["url"] = response.url
            if "#EXT-X-ENDLIST" not in manifest:
                raise ReelucidError("LIVE_NOT_SUPPORTED", "仅接受已结束的 HLS 点播清单。")
            if not HlsFD.can_download(manifest, info):
                raise ReelucidError(
                    "NETWORK_POLICY_UNSUPPORTED",
                    "该 HLS 需要未纳管的外部下载器或受 DRM 保护。",
                )
            info["hls_media_playlist_data"] = manifest
        return cast(Any, super()).dl(name, info, subtitle=subtitle, test=test)
