"""Read session-bound TikTok media from the configured DTK instance's file API."""

import re
import time
from pathlib import Path
from typing import cast
from urllib.parse import quote
from uuid import UUID

from pydantic import BaseModel, Field, StrictBool, StrictInt, ValidationError

from reelucid.domain import JsonObject, ReelucidError
from reelucid.ports import Progress
from reelucid.resolver_http import ResolverClient


class StoredFile(BaseModel):
    name: str
    kind: str
    state: str
    bytes: StrictInt
    sha256: str | None = Field(default=None, pattern=r"^[a-fA-F0-9]{64}$")


class StoredDownload(BaseModel):
    id: UUID
    platform: str
    content_id: str
    on_disk: StrictBool
    files: list[StoredFile] = Field(max_length=64)


def dtk_data(client: ResolverClient, path: str, body: JsonObject | None = None) -> JsonObject:
    result = client.request(path, body)
    data = result.get("data")
    if result.get("success") is not True or not isinstance(data, dict):
        raise ReelucidError("RESOLVER_FAILED", "DTK 返回失败或不兼容的响应。", retryable=True)
    return cast(JsonObject, data)


def dtk_task_error(data: JsonObject, *, downloading: bool = False) -> ReelucidError:
    detail = data.get("error")
    if (
        isinstance(detail, dict)
        and cast(JsonObject, detail).get("code") == "IDENTITY_POOL_EXHAUSTED"
    ):
        return ReelucidError(
            "RESOLVER_IDENTITY_UNAVAILABLE", "DTK 没有可访问该平台的可用会话，请检查 DTK 设置。"
        )
    if downloading:
        return ReelucidError(
            "RESOLVER_DOWNLOAD_FAILED", "DTK 未能保存视频，请检查 DTK 的下载任务。"
        )
    return ReelucidError(
        "SOURCE_ACCESS_REQUIRED", "DTK 未能解析作品，请检查会话、访问权限与网络。", retryable=True
    )


def acquire_stored_tiktok(
    client: ResolverClient,
    content_id: str,
    title: str,
    directory: Path,
    limit: int,
    progress: Progress,
) -> JsonObject:
    if not re.fullmatch(r"[0-9]{1,64}", content_id):
        raise ReelucidError("RESOLVER_INVALID_RESPONSE", "DTK 返回了无效的 TikTok 作品 ID。")
    data = dtk_data(
        client,
        "/api/v1/downloads",
        {"platform": "tiktok", "content_id": content_id, "skip_existing": True},
    )
    download_id = data.get("download_id")
    try:
        if not isinstance(download_id, str):
            raise ValueError
        identifier = UUID(download_id)
    except ValueError as exc:
        raise ReelucidError("RESOLVER_INVALID_RESPONSE", "DTK 未返回有效的下载 ID。") from exc
    path = f"/api/v1/downloads/{identifier}"
    while True:
        data = dtk_data(client, path)
        state = data.get("state")
        if state in ("done", "partial"):
            break
        if state in ("failed", "cancelled"):
            raise dtk_task_error(data, downloading=True)
        if state not in ("queued", "running"):
            raise ReelucidError("RESOLVER_INVALID_RESPONSE", "DTK 下载任务状态不兼容。")
        time.sleep(min(0.5, client.remaining()))
    try:
        download = StoredDownload.model_validate(data)
    except ValidationError as exc:
        raise ReelucidError("RESOLVER_INVALID_RESPONSE", "DTK 下载记录字段无效。") from exc
    if (download.id, download.platform, download.content_id) != (identifier, "tiktok", content_id):
        raise ReelucidError("RESOLVER_INVALID_RESPONSE", "DTK 返回了其他下载任务的数据。")
    if not download.on_disk:
        raise ReelucidError("RESOLVER_FILE_MISSING", "DTK 已存视频不存在或已被清理。")
    files = [item for item in download.files if item.kind == "video" and item.state == "done"]
    if len(files) != 1:
        raise ReelucidError("RESOLVER_DOWNLOAD_FAILED", "DTK 未保存完整的单条视频。")
    video = files[0]
    if video.bytes <= 0 or not re.fullmatch(r"video\.(mp4|m4v|mov|webm)", video.name):
        raise ReelucidError("RESOLVER_INVALID_RESPONSE", "DTK 视频文件名称或体积无效。")
    destination = directory / f"source{Path(video.name).suffix}"
    size, digest = client.download_video(
        f"{path}/files/{quote(video.name, safe='')}",
        destination,
        video.bytes,
        video.sha256.lower() if video.sha256 else None,
        limit,
        progress,
    )
    return {
        "path": str(destination),
        "sha256": digest,
        "size_bytes": size,
        "adapter_id": "dtk",
        "source_id": content_id,
        "name": (title or video.name)[:300],
    }
