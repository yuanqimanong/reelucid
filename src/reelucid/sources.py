"""Source adapter contracts, trusted plugin registration, and URL routing."""

from __future__ import annotations

import importlib
import re
from pathlib import Path
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

from reelucid.domain import JsonObject, ReelucidError
from reelucid.http_media import HttpMediaAdapter
from reelucid.ports import Cancellation, Progress
from reelucid.resolver import DtkAdapter
from reelucid.source_proxy import SourceProxy
from reelucid.xhs_local import XHS_HOSTS
from reelucid.xiaohongshu import XhsAdapter
from reelucid.ytdlp_source import BilibiliAdapter, YtDlpAdapter

__all__ = [
    "BilibiliAdapter",
    "DtkAdapter",
    "HttpMediaAdapter",
    "SourceAdapter",
    "SourceRegistry",
    "XhsAdapter",
    "YtDlpAdapter",
    "normalize_source_url",
]


def normalize_source_url(value: str) -> str:
    """Accept a single pasted share URL without guessing between multiple links."""
    stripped = value.strip()
    links = re.findall(r"https?://[^\s<>\"'，。！？；、]+", stripped)
    if len(links) != 1:
        raise ValueError("请输入一条 HTTP(S) 链接，或只包含一条链接的分享文案。")
    return links[0] if links[0] == stripped else links[0].rstrip("：）》】,.!;)]}")


@runtime_checkable
class SourceAdapter(Protocol):
    """Trusted adapters write an acquired file to the job directory and return its metadata."""

    id: str

    def acquire(
        self,
        url: str,
        directory: Path,
        limit: int,
        keep_video: bool,
        progress: Progress,
        cancelled: Cancellation,
        cookie_file: Path | None = None,
    ) -> JsonObject: ...


class SourceRegistry:
    """Route jobs to built-in adapters or plugins installed by the administrator."""

    def __init__(
        self,
        adapters: list[SourceAdapter] | None = None,
        plugins: tuple[str, ...] = (),
        *,
        proxy: SourceProxy | None = None,
    ) -> None:
        self.adapters: dict[str, SourceAdapter] = {}
        self.plugin_ids: set[str] = set()
        instances: list[SourceAdapter] = (
            list(adapters)
            if adapters is not None
            else [
                HttpMediaAdapter(proxy=proxy),
                YtDlpAdapter(proxy=proxy),
                BilibiliAdapter(proxy=proxy),
                DtkAdapter(),
                XhsAdapter(),
            ]
        )
        for reference in plugins:
            if not re.fullmatch(r"[a-zA-Z_][\w.]*:[a-zA-Z_]\w*", reference):
                raise ReelucidError("INVALID_ADAPTER_PLUGIN", "适配器入口应为 module:ClassName。")
            module, name = reference.split(":")
            factory = getattr(importlib.import_module(module), name)
            instance: object = factory()
            if not isinstance(instance, SourceAdapter):
                raise ReelucidError("INVALID_ADAPTER_PLUGIN", "适配器必须实现 SourceAdapter。")
            instances.append(instance)
            self.plugin_ids.add(instance.id)
        for adapter in instances:
            if (
                not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", adapter.id)
                or adapter.id in self.adapters
                or adapter.id == "upload"
            ):
                raise ReelucidError("INVALID_ADAPTER_PLUGIN", "适配器标识无效或重复。")
            self.adapters[adapter.id] = adapter

    def select(
        self,
        url: str,
        adapter_id: str | None = None,
        *,
        prefer_dtk: bool = False,
        prefer_xhs: bool = False,
    ) -> SourceAdapter:
        if adapter_id is None:
            parts = urlsplit(url)
            host = (parts.hostname or "").lower()
            suffix = Path(parts.path).suffix.lower()
            if prefer_xhs and host in XHS_HOSTS:
                adapter_id = "xhs"
            elif prefer_dtk and any(
                host == d or host.endswith("." + d)
                for d in ("douyin.com", "iesdouyin.com", "tiktok.com")
            ):
                adapter_id = "dtk"
            elif host in {"www.bilibili.com", "m.bilibili.com", "bilibili.com", "b23.tv"} and (
                "/video/" in parts.path or host == "b23.tv"
            ):
                adapter_id = "bilibili"
            elif suffix in {".mp4", ".webm", ".mkv", ".mp3", ".wav", ".m4a", ".flac", ".ogg"}:
                adapter_id = "http_media"
            else:
                adapter_id = "yt_dlp"
        if adapter_id not in self.adapters:
            raise ReelucidError("UNSUPPORTED_SOURCE", "此来源适配器不可用。")
        return self.adapters[adapter_id]
