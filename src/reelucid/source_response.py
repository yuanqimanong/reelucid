"""Adapt checked HTTP responses to yt-dlp, including servers that ignore identity."""

from __future__ import annotations

import gzip
import io
import zlib
from collections.abc import Mapping
from copy import copy
from email.message import Message
from typing import cast

from yt_dlp.networking.common import Response

from reelucid.domain import ReelucidError

_METADATA_LIMIT = 8 * 1024**2
_METADATA_TYPES = frozenset(
    {
        "application/ecmascript",
        "application/javascript",
        "application/json",
        "application/mpegurl",
        "application/vnd.apple.mpegurl",
        "application/x-mpegurl",
        "application/xml",
        "audio/mpegurl",
        "audio/x-mpegurl",
    }
)


def adapt_source_response(
    body: io.IOBase, url: str, headers: Message, status: int, *, method: str = "GET"
) -> Response:
    """Transfer ownership of the native body to a Response, or close it on failure."""
    encoding = ",".join(headers.get_all("Content-Encoding", [])).strip().lower()
    if encoding in {"", "identity"} or method == "HEAD" or status in {204, 205, 304}:
        # Response only requires .items(). Message also preserves repeated fields.
        return Response(body, url, cast(Mapping[str, str], headers), status)

    with body:
        media_type = headers.get("Content-Type", "").partition(";")[0].strip().lower()
        is_metadata = (
            media_type.startswith("text/")
            or (media_type.startswith("application/") and media_type.endswith(("+json", "+xml")))
            or media_type in _METADATA_TYPES
        )
        if encoding not in {"gzip", "x-gzip"} or not is_metadata or status == 206:
            raise ReelucidError(
                "SOURCE_ENCODING_UNSUPPORTED", "来源返回了不支持的内容编码或压缩媒体分片。"
            )
        compressed: bytes = body.read(_METADATA_LIMIT + 1)
        if len(compressed) > _METADATA_LIMIT:
            raise ReelucidError("SIZE_LIMIT_EXCEEDED", "压缩元数据超过 8 MiB。")
        try:
            if not compressed.startswith(b"\x1f\x8b"):
                raise gzip.BadGzipFile("Missing gzip header")
            with gzip.GzipFile(fileobj=io.BytesIO(compressed)) as archive:
                decoded = archive.read(_METADATA_LIMIT + 1)
        except (gzip.BadGzipFile, EOFError, zlib.error) as exc:
            raise ReelucidError(
                "INVALID_SOURCE_RESPONSE", "来源返回的 gzip 元数据损坏或不完整。"
            ) from exc
        if len(decoded) > _METADATA_LIMIT:
            raise ReelucidError("SIZE_LIMIT_EXCEEDED", "解压后的元数据超过 8 MiB。")

    decoded_headers = copy(headers)
    del decoded_headers["Content-Encoding"]
    del decoded_headers["Content-Length"]
    decoded_headers["Content-Length"] = str(len(decoded))
    return Response(io.BytesIO(decoded), url, cast(Mapping[str, str], decoded_headers), status)
