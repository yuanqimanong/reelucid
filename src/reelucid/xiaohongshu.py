"""Acquire one XHS video from the bundled parser or an administrator's resolver."""

from __future__ import annotations

import time
import urllib.request
from http.cookiejar import LoadError, MozillaCookieJar
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, ValidationError

from reelucid.domain import JsonObject, ReelucidError
from reelucid.network import validate_url
from reelucid.ports import Cancellation, Progress
from reelucid.resolver_http import ResolverClient
from reelucid.settings import SourceResolver, XhsResolver
from reelucid.source_proxy import SourceProxy
from reelucid.xhs_local import XHS_HOSTS, fetch_page, parse_page


class XhsVideo(BaseModel):
    source_id: str = Field(alias="作品ID")
    title: str = Field(default="小红书视频", alias="作品标题")
    kind: str = Field(alias="作品类型")
    urls: list[str] = Field(alias="下载地址", max_length=100)


class XhsAdapter:
    id = "xhs"

    def __init__(
        self,
        config: SourceResolver | None = None,
        api_key: str | None = None,
        *,
        local_command: Path | None = None,
        proxy: SourceProxy | None = None,
    ) -> None:
        self.config, self.api_key = config or SourceResolver(), api_key
        self.local_command = local_command
        self.proxy = proxy

    def acquire(
        self,
        url: str,
        directory: Path,
        limit: int,
        keep_video: bool,
        progress: Progress,
        cancelled: Cancellation,
        cookie_file: Path | None = None,
    ) -> JsonObject:
        from reelucid.sources import HttpMediaAdapter

        if not self.config.enabled:
            raise ReelucidError(
                "RESOLVER_NOT_CONFIGURED", "请先在后台启用并配置 XHS-Downloader 服务。"
            )
        validate_url(url)
        host = (urlsplit(url).hostname or "").lower()
        if host not in XHS_HOSTS:
            raise ReelucidError("UNSUPPORTED_SOURCE", "小红书适配器仅接收作品或分享链接。")
        jar = None
        if cookie_file is not None:
            jar = MozillaCookieJar(str(cookie_file))
            try:
                jar.load(ignore_discard=True)
            except (LoadError, OSError) as exc:
                raise ReelucidError(
                    "INVALID_SOURCE_CREDENTIAL", "站点凭据必须是有效的 Netscape cookies.txt。"
                ) from exc
        if isinstance(self.config, XhsResolver) and self.config.mode == "local":
            if self.local_command is None:
                raise ReelucidError("RESOLVER_UNAVAILABLE", "当前安装未包含小红书本地插件。")
            deadline = time.monotonic() + self.config.timeout_seconds
            page = fetch_page(url, jar, deadline, cancelled, proxy=self.proxy)
            payload = parse_page(self.local_command, page, deadline, cancelled)
        else:
            body: JsonObject = {"url": url, "download": False, "check_record": False}
            if jar is not None:
                origin = "www.rednote.com" if host == "www.rednote.com" else "www.xiaohongshu.com"
                cookie_request = urllib.request.Request(f"https://{origin}/")
                jar.add_cookie_header(cookie_request)
                body["cookie"] = cookie_request.get_header("Cookie") or ""
            payload = ResolverClient(self.config, self.api_key, cancelled).request(
                "/xhs/detail", body
            )
        if not payload.get("data"):
            raise ReelucidError(
                "SOURCE_ACCESS_REQUIRED",
                "小红书未返回作品数据，请检查分享链接是否有效、Cookie、网络或站点风控。",
                retryable=True,
            )
        try:
            video = XhsVideo.model_validate(payload["data"])
        except ValidationError as exc:
            raise ReelucidError(
                "RESOLVER_INVALID_RESPONSE", "小红书解析结果缺少单条视频字段或接口格式已改变。"
            ) from exc
        if video.kind not in {"视频", "Video", "video"}:
            raise ReelucidError(
                "UNSUPPORTED_SOURCE", "小红书转录只接受单条视频，不接受图文、图集或直播。"
            )
        if len(video.urls) != 1:
            raise ReelucidError("RESOLVER_INVALID_RESPONSE", "解析器必须提供一条完整视频直链。")
        result = HttpMediaAdapter(proxy=self.proxy).acquire(
            video.urls[0], directory, limit, keep_video, progress, cancelled
        )
        return {
            **result,
            "adapter_id": self.id,
            "source_id": video.source_id,
            "name": video.title[:300],
        }
