"""Download a single direct-media response using the configured source transport."""

import http.client
import urllib.error
import urllib.request
from http.cookiejar import LoadError, MozillaCookieJar
from pathlib import Path
from urllib.parse import urlsplit

from reelucid import __version__
from reelucid.domain import JsonObject, ReelucidError
from reelucid.files import digest_file
from reelucid.network import source_opener, validate_url
from reelucid.ports import Cancellation, Progress
from reelucid.source_common import DownloadDeadline, classify_http_error
from reelucid.source_proxy import SourceProxy


class HttpMediaAdapter:
    id = "http_media"

    def __init__(
        self, headers: dict[str, str] | None = None, *, proxy: SourceProxy | None = None
    ) -> None:
        # Only trusted adapters construct headers; never accepted from a job request.
        self.headers = {
            "User-Agent": f"Reelucid/{__version__}",
            **(headers or {}),
            "Accept-Encoding": "identity",
        }
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
        validate_url(url)
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / "source.media"
        request = urllib.request.Request(url, headers=self.headers)
        jar = MozillaCookieJar(str(cookie_file)) if cookie_file else None
        if jar is not None:
            try:
                jar.load(ignore_discard=True)
            except (LoadError, OSError) as exc:
                raise ReelucidError(
                    "INVALID_SOURCE_CREDENTIAL", "站点凭据必须是有效的 Netscape cookies.txt。"
                ) from exc
        deadline = DownloadDeadline(cancelled)
        try:
            with source_opener(jar, proxy=self.proxy).open(request, timeout=30) as response:
                media_type = response.headers.get_content_type()
                if not (
                    media_type.startswith(("audio/", "video/"))
                    or media_type in {"application/octet-stream", "application/ogg"}
                ):
                    raise ReelucidError(
                        "UNSUPPORTED_SOURCE", "此地址不是音视频直链，请使用站点适配器。"
                    )
                total = int(response.headers.get("Content-Length") or 0)
                if total > limit:
                    raise ReelucidError("SIZE_LIMIT_EXCEEDED", "媒体超过下载体积上限。")
                size = 0
                with destination.open("wb") as stream:
                    while block := response.read(256 * 1024):
                        deadline.check()
                        size += len(block)
                        if size > limit:
                            raise ReelucidError("SIZE_LIMIT_EXCEEDED", "媒体超过下载体积上限。")
                        stream.write(block)
                        progress(
                            "progress",
                            {
                                "stage": "acquire",
                                "completed_bytes": size,
                                "total_bytes": total or None,
                            },
                        )
                if not size or (total and size != total):
                    raise ReelucidError("DOWNLOAD_INCOMPLETE", "媒体下载不完整。", retryable=True)
        except ReelucidError:
            destination.unlink(missing_ok=True)
            raise
        except urllib.error.URLError as exc:
            destination.unlink(missing_ok=True)
            raise classify_http_error(exc) from exc
        except (TimeoutError, OSError, http.client.HTTPException) as exc:
            destination.unlink(missing_ok=True)
            raise ReelucidError(
                "DOWNLOAD_FAILED", "直链下载失败，请检查地址、有效期和网络。", retryable=True
            ) from exc
        return {
            "path": str(destination),
            "sha256": digest_file(destination),
            "adapter_id": self.id,
            "name": Path(urlsplit(url).path).name or "在线媒体",
        }
