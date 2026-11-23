"""Select and retrieve one video through Reelucid's yt-dlp transport."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from reelucid.domain import Cancelled, JsonObject, ReelucidError
from reelucid.files import digest_file
from reelucid.network import validate_url
from reelucid.ports import Cancellation, Progress
from reelucid.source_common import DownloadDeadline, classify_ytdlp_error, merge_tracks
from reelucid.source_proxy import SourceProxy

if TYPE_CHECKING:
    from reelucid.ytdlp_transport import CheckedYoutubeDL


class _QuietLogger:
    """Upstream diagnostics may contain signed media URLs; public errors are classified separately."""

    def debug(self, message: str) -> None:
        pass

    def warning(self, message: str) -> None:
        pass

    def error(self, message: str) -> None:
        pass


def _options(
    directory: Path,
    limit: int,
    keep_video: bool,
    progress: Progress,
    deadline: DownloadDeadline,
    cookie_file: Path | None,
) -> JsonObject:
    downloaded: dict[str, int] = {}

    def hook(data: JsonObject) -> None:
        deadline.check()
        key = str(data.get("filename", "source"))
        downloaded[key] = max(downloaded.get(key, 0), int(data.get("downloaded_bytes") or 0))
        size = sum(downloaded.values())
        if size > limit:
            raise ReelucidError("SIZE_LIMIT_EXCEEDED", "音视频轨合计超过实例体积上限。")
        progress(
            "progress",
            {
                "stage": "acquire",
                "completed_bytes": size,
                "total_bytes": None,
            },
        )

    parameters: JsonObject = {
        "format": "bestvideo[height<=720]+bestaudio/best[height<=720]/best"
        if keep_video
        else "bestaudio/best",
        "outtmpl": str(directory / "source.%(ext)s"),
        "paths": {"temp": str(directory)},
        "noplaylist": True,
        "playlist_items": "1",
        "max_filesize": limit,
        "progress_hooks": [hook],
        "logger": _QuietLogger(),
        "quiet": True,
        "socket_timeout": 30,
        "retries": 2,
        "fragment_retries": 2,
        "skip_unavailable_fragments": False,
        "fixup": "never",
        # HlsFD may invoke FFmpegFD directly; never give it a network-capable binary.
        "ffmpeg_location": str(directory / "disabled-network-tools"),
        "proxy": "",
        "enable_file_urls": False,
        "hls_prefer_native": True,
        "external_downloader": {"default": "native"},
        "postprocessors": [],
        "remote_components": [],
        "plugin_dirs": [],
        "cachedir": False,
    }
    if cookie_file:
        parameters["cookiefile"] = str(cookie_file)
    if wrapper := shutil.which("reelucid-deno"):
        parameters["js_runtimes"] = {"deno": {"path": wrapper}}
    else:
        parameters["js_runtimes"] = {}
    return parameters


def _download_tracks(
    downloader: CheckedYoutubeDL,
    info: JsonObject,
    directory: Path,
    limit: int,
    cancelled: Cancellation,
    deadline: DownloadDeadline,
) -> Path:
    if not info or info.get("_type") in {"playlist", "multi_video"}:
        raise ReelucidError("COLLECTION_NOT_SUPPORTED", "首版仅接受单条视频链接。")
    if info.get("is_live"):
        raise ReelucidError("LIVE_NOT_SUPPORTED", "首版不接受正在进行的直播。")
    if float(info.get("duration") or 0) > 14400:
        raise ReelucidError("MEDIA_DURATION_LIMIT", "媒体超过允许时长。")
    formats = info.get("requested_formats") or [info]
    if len(formats) > 2:
        raise ReelucidError("UNSUPPORTED_SOURCE", "该视频需要多于两条媒体轨。")
    paths: list[Path] = []
    for index, selected in enumerate(formats):
        track = {**info, **selected}
        track.pop("requested_formats", None)
        track.pop("requested_downloads", None)
        path = directory / f"track-{index}.media"
        success, _ = downloader.dl(str(path), track)
        if not success or not path.is_file():
            raise ReelucidError("DOWNLOAD_FAILED", "下载器未生成完整媒体轨。")
        paths.append(path)
        if sum(p.stat().st_size for p in paths) > limit:
            raise ReelucidError("SIZE_LIMIT_EXCEEDED", "音视频轨合计超过体积上限。")
    deadline.check()
    path = (
        merge_tracks(
            paths,
            directory / "source.mkv",
            cancelled,
            min(300, deadline.remaining_seconds()),
        )
        if len(paths) == 2
        else paths[0]
    )
    if path.stat().st_size > limit:
        raise ReelucidError("SIZE_LIMIT_EXCEEDED", "媒体超过体积上限。")
    return path


class YtDlpAdapter:
    id = "yt_dlp"
    extractor_key: str | None = None

    def __init__(self, *, proxy: SourceProxy | None = None) -> None:
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
        from yt_dlp.networking.exceptions import RequestError
        from yt_dlp.utils import DownloadError, ExtractorError

        from reelucid.ytdlp_transport import CheckedYoutubeDL

        validate_url(url)
        directory.mkdir(parents=True, exist_ok=True)
        deadline = DownloadDeadline(cancelled)
        params = _options(directory, limit, keep_video, progress, deadline, cookie_file)
        try:
            with CheckedYoutubeDL(params, deadline=deadline, proxy=self.proxy) as downloader:
                if self.extractor_key:
                    from reelucid.bilibili import BilibiliPublicIE

                    downloader.add_info_extractor(BilibiliPublicIE())
                info: Any = downloader.extract_info(url, download=False, ie_key=self.extractor_key)
                path = _download_tracks(downloader, info, directory, limit, cancelled, deadline)
                return {
                    "path": str(path),
                    "sha256": digest_file(path),
                    "adapter_id": self.id,
                    "source_id": str(info.get("id", "")),
                    "name": str(info.get("title", "在线媒体"))[:300],
                }
        except (DownloadError, ExtractorError, RequestError) as exc:
            if cancelled():
                raise Cancelled() from exc
            raise classify_ytdlp_error(exc) from exc


class BilibiliAdapter(YtDlpAdapter):
    id = "bilibili"
    extractor_key = "BilibiliPublic"
