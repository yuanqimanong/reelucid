"""Optional DTK v5 resolver integration. Only the administrator selects its endpoint."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote, urlsplit

from pydantic import BaseModel, Field, StrictBool, ValidationError

from reelucid.domain import Cancelled, JsonObject, ReelucidError
from reelucid.dtk_media import acquire_stored_tiktok, dtk_data, dtk_task_error
from reelucid.ports import Cancellation, Progress
from reelucid.resolver_http import ResolverClient
from reelucid.settings import DtkResolver, SourceResolver
from reelucid.source_proxy import SourceProxy


class ResolvedVideo(BaseModel):
    url: str


class MediaAccess(BaseModel):
    session_bound: StrictBool = False


class ResolvedMedia(BaseModel):
    video: ResolvedVideo | None = None
    # DTK 5.1.11 predates the media access field.
    access: MediaAccess = Field(default_factory=MediaAccess)


class ResolvedContent(BaseModel):
    kind: str
    content_id: str
    title: str
    duration_ms: int | None = None
    is_private: bool = False
    is_deleted: bool = False
    media: ResolvedMedia


class DtkAdapter:
    id = "dtk"

    def __init__(
        self,
        config: SourceResolver | None = None,
        api_key: str | None = None,
        *,
        proxy: SourceProxy | None = None,
    ) -> None:
        self.config = config or SourceResolver()
        self.api_key = api_key
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
        from reelucid.network import validate_url
        from reelucid.sources import HttpMediaAdapter

        if not self.config.enabled:
            raise ReelucidError(
                "RESOLVER_NOT_CONFIGURED", "请先在后台启用并配置自托管 DTK v5 服务。"
            )
        validate_url(url)
        host = (urlsplit(url).hostname or "").lower()
        if not any(
            host == domain or host.endswith("." + domain)
            for domain in ("douyin.com", "iesdouyin.com", "tiktok.com", "bilibili.com", "b23.tv")
        ):
            raise ReelucidError(
                "UNSUPPORTED_SOURCE", "DTK 适配器仅接收抖音、TikTok 与 Bilibili 链接。"
            )
        client = ResolverClient(self.config, self.api_key, cancelled)

        task = dtk_data(client, "/api/v1/parse", {"url": url})
        task_id = task.get("task_id")
        if not isinstance(task_id, str) or not task_id or len(task_id) > 200:
            raise ReelucidError("RESOLVER_INVALID_RESPONSE", "解析响应缺少 task_id。")
        while True:
            task = dtk_data(client, "/api/v1/tasks/" + quote(task_id, safe=""))
            state = task.get("state")
            if state == "done":
                break
            if state == "failed":
                raise dtk_task_error(task)
            if state not in {"queued", "running"}:
                raise ReelucidError("RESOLVER_INVALID_RESPONSE", "解析任务状态不兼容。")
            for _ in range(5):
                if cancelled():
                    raise Cancelled()
                time.sleep(0.1)
        raw_content: Any = task.get("data")
        if (
            not isinstance(raw_content, dict)
            or cast(JsonObject, raw_content).get("kind") != "video"
        ):
            raise ReelucidError(
                "UNSUPPORTED_SOURCE", "解析结果必须为单条点播视频，不接受图集或直播。"
            )
        try:
            content = ResolvedContent.model_validate(raw_content)
        except ValidationError as exc:
            raise ReelucidError(
                "RESOLVER_INVALID_RESPONSE", "解析结果没有完整视频直链或字段格式不兼容。"
            ) from exc
        if content.is_private or content.is_deleted:
            raise ReelucidError("SOURCE_ACCESS_REQUIRED", "视频不可访问或已删除。")
        if (content.duration_ms or 0) > 14400_000:
            raise ReelucidError("MEDIA_DURATION_LIMIT", "媒体超过允许时长。")
        if content.media.access.session_bound:
            if (
                (host == "tiktok.com" or host.endswith(".tiktok.com"))
                and isinstance(self.config, DtkResolver)
                and self.config.download_session_bound
            ):
                return acquire_stored_tiktok(
                    client, content.content_id, content.title, directory, limit, progress
                )
            raise ReelucidError(
                "RESOLVER_DOWNLOAD_REQUIRED",
                "此视频仅允许通过解析它的 DTK 会话下载。请在该 DTK 实例中保存视频，"
                "然后将文件上传到 Reelucid。",
            )
        media_url = content.media.video.url if content.media.video is not None else None
        if not media_url:
            raise ReelucidError("RESOLVER_INVALID_RESPONSE", "解析结果没有完整视频直链。")
        # Resolver Key and source cookies never leave the resolver trust boundary.
        site = (
            "www.tiktok.com"
            if host.endswith("tiktok.com")
            else "www.bilibili.com"
            if host.endswith(("bilibili.com", "b23.tv"))
            else "www.douyin.com"
        )
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
            "Referer": f"https://{site}/",
        }
        result = HttpMediaAdapter(headers, proxy=self.proxy).acquire(
            media_url, directory, limit, keep_video, progress, cancelled
        )
        return {
            **result,
            "adapter_id": self.id,
            "source_id": content.content_id,
            "name": (content.title or "在线媒体")[:300],
        }
