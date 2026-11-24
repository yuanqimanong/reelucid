"""Shared media assembly and public download errors."""

import json
import re
import ssl
import time
import urllib.error
from collections import deque
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

from reelucid.domain import Cancelled, ReelucidError
from reelucid.media import run_tool, tool
from reelucid.ports import Cancellation


class DownloadDeadline:
    """Share one monotonic deadline across a job's extraction, transfers, and local merge."""

    def __init__(self, cancelled: Cancellation) -> None:
        self._cancelled = cancelled
        self._expires_at = time.monotonic() + 3600

    def remaining_seconds(self) -> float:
        return self._expires_at - time.monotonic()

    def check(self) -> None:
        if self._cancelled():
            raise Cancelled()
        if self.remaining_seconds() < 0:
            raise ReelucidError("DOWNLOAD_TIMEOUT", "下载超过实例时间上限。", retryable=True)


def merge_tracks(
    tracks: list[Path], destination: Path, cancelled: Cancellation, timeout: float = 300
) -> Path:
    if len(tracks) != 2:
        raise ReelucidError("UNSUPPORTED_SOURCE", "仅支持一条视频轨和一条音频轨。")
    arguments = [tool("ffmpeg"), "-v", "error", "-nostdin", "-y"]
    for track in tracks:
        arguments.extend(["-protocol_whitelist", "file,pipe", "-threads", "1", "-i", str(track)])
    arguments.extend(["-map", "0:v:0", "-map", "1:a:0", "-c", "copy", str(destination)])
    try:
        run_tool(arguments, cancelled, timeout=max(1, timeout))
    except ReelucidError:
        destination.unlink(missing_ok=True)
        raise
    for track in tracks:
        track.unlink(missing_ok=True)
    return destination


def _causes(error: BaseException, *, include_suppressed: bool = False) -> Iterator[BaseException]:
    """yt-dlp also stores causes outside Python's standard exception chain."""
    pending = deque([error])
    visited: set[int] = set()
    while pending:
        item = pending.popleft()
        if id(item) in visited:
            continue
        visited.add(id(item))
        yield item
        for cause in (
            item.__cause__,
            getattr(item, "cause", None),
            getattr(item, "reason", None),
            item.__context__ if include_suppressed or not item.__suppress_context__ else None,
        ):
            if isinstance(cause, BaseException):
                pending.append(cause)
        for nested in getattr(item, "unexpected_errors", ()):
            if isinstance(nested, BaseException):
                pending.append(nested)
        exc_info: Any = getattr(item, "exc_info", None)
        if (
            isinstance(exc_info, tuple)
            and len(cast(tuple[Any, ...], exc_info)) > 1
            and isinstance(exc_info[1], BaseException)
        ):
            pending.append(exc_info[1])


def source_error(error: BaseException) -> ReelucidError | None:
    return next(
        (
            item
            for item in _causes(error, include_suppressed=True)
            if isinstance(item, ReelucidError)
        ),
        None,
    )


def _http_error(status: int) -> ReelucidError:
    if status == 401:
        return ReelucidError("AUTH_REQUIRED", "来源服务要求身份验证。")
    if status == 403:
        return ReelucidError("SOURCE_ACCESS_REQUIRED", "来源服务拒绝了访问请求（HTTP 403）。")
    if status == 412:
        return ReelucidError("SOURCE_PRECONDITION_FAILED", "来源服务拒绝了当前请求（HTTP 412）。")
    if status in {404, 410}:
        return ReelucidError("SOURCE_NOT_FOUND", "来源服务未找到所请求的资源。")
    if status == 429:
        return ReelucidError("RATE_LIMITED", "来源服务限制了请求频率，请稍后重试。", retryable=True)
    if status in {408, 504}:
        return ReelucidError("DOWNLOAD_TIMEOUT", "来源服务响应超时。", retryable=True)
    return ReelucidError(
        "DOWNLOAD_FAILED", "来源服务返回了 HTTP 错误。", retryable=500 <= status < 600
    )


def _connection_error(
    causes: list[BaseException],
    *,
    certificate_errors: tuple[type[BaseException], ...] = (),
    transport_errors: tuple[type[BaseException], ...] = (),
) -> ReelucidError | None:
    if any(
        isinstance(cause, (ssl.SSLCertVerificationError, *certificate_errors)) for cause in causes
    ):
        return ReelucidError("DOWNLOAD_FAILED", "来源服务的 TLS 证书验证失败。")
    if any(isinstance(cause, TimeoutError) for cause in causes):
        return ReelucidError("DOWNLOAD_TIMEOUT", "连接或读取来源超时。", retryable=True)
    if any(
        isinstance(cause, (urllib.error.URLError, ConnectionError, *transport_errors))
        for cause in causes
    ):
        return ReelucidError("DOWNLOAD_FAILED", "连接来源服务失败。", retryable=True)
    return None


def classify_http_error(error: urllib.error.URLError) -> ReelucidError:
    """Direct HTTP failures can be classified without loading the extractor engine."""
    if domain := source_error(error):
        return domain
    causes = list(_causes(error))
    for cause in causes:
        if isinstance(cause, urllib.error.HTTPError):
            return _http_error(cause.code)
    return _connection_error(causes) or ReelucidError(
        "DOWNLOAD_FAILED", "连接来源服务失败。", retryable=True
    )


def _extractor_diagnostic(message: str) -> ReelucidError | None:
    # Text-only DownloadError may include yt-dlp's prefix and video ID before the diagnostic.
    text = re.sub(
        r"^(?:error:\s*)?(?:\[[\w:.-]+\]\s*)?(?:[\w.-]+:\s+)?",
        "",
        message[:1024].lower(),
    )
    status = re.match(
        r"(?:unable to download (?:webpage|video data): )?http error ([45]\d\d):", text
    )
    if status:
        return _http_error(int(status[1]))
    if text.startswith("fresh cookies (not necessarily logged in) are needed"):
        return ReelucidError("SOURCE_ACCESS_REQUIRED", "来源服务要求有效的站点会话。")
    if text.startswith("unexpected response from webpage request"):
        return ReelucidError("INVALID_SOURCE_RESPONSE", "来源返回的页面与提取器预期不符。")
    if text.startswith(
        (
            "login required",
            "login is required",
            "please log in",
            "please sign in",
            "sign in to confirm",
            "this video is only available for registered users",
            "tiktok is requiring login",
        )
    ):
        return ReelucidError("AUTH_REQUIRED", "来源服务要求登录。")
    if text.startswith(("unable to extract ", "failed to parse json", "invalid json in ")):
        return ReelucidError("EXTRACTOR_BROKEN", "提取器无法解析来源返回的数据。")
    return None


def classify_ytdlp_error(error: BaseException) -> ReelucidError:
    """Classify a downloader failure without publishing upstream text, URLs or cookies."""
    from yt_dlp.networking.exceptions import CertificateVerifyError, HTTPError, TransportError
    from yt_dlp.utils import (
        DownloadError,
        ExtractorError,
        GeoRestrictedError,
        RegexNotFoundError,
        UnsupportedError,
    )

    if domain := source_error(error):
        return domain
    causes = list(_causes(error))
    for cause in causes:
        if isinstance(cause, HTTPError):
            return _http_error(cause.status)
        if isinstance(cause, urllib.error.HTTPError):
            return _http_error(cause.code)
        if isinstance(cause, GeoRestrictedError):
            return ReelucidError("SOURCE_ACCESS_REQUIRED", "来源服务限制了当前地区的访问。")
        if isinstance(cause, UnsupportedError):
            return ReelucidError("UNSUPPORTED_SOURCE", "提取器不支持此链接。")
    if connection := _connection_error(
        causes, certificate_errors=(CertificateVerifyError,), transport_errors=(TransportError,)
    ):
        return connection
    if any(isinstance(cause, RegexNotFoundError) for cause in causes):
        return ReelucidError("EXTRACTOR_BROKEN", "提取器无法解析来源返回的数据。")
    extractors = [cause for cause in causes if isinstance(cause, ExtractorError)]
    for extractor in extractors:
        if diagnostic := _extractor_diagnostic(str(extractor.orig_msg)):
            return diagnostic
    if extractors and any(isinstance(cause, json.JSONDecodeError) for cause in causes):
        return ReelucidError("EXTRACTOR_BROKEN", "提取器无法解析来源返回的数据。")
    if isinstance(error, DownloadError) and (diagnostic := _extractor_diagnostic(str(error))):
        return diagnostic
    return ReelucidError("DOWNLOAD_FAILED", "下载器未获取媒体，无法确定具体原因。")
