import gzip
import io
from email.message import Message

import pytest

from reelucid.domain import ReelucidError
from reelucid.source_response import adapt_source_response

URL = "https://example.org/video"


def headers_for(content_type: str, size: int, encoding: str | None = "gzip") -> Message:
    headers = Message()
    if content_type:
        headers["Content-Type"] = content_type
    headers["Content-Length"] = str(size)
    if encoding is not None:
        headers["Content-Encoding"] = encoding
    return headers


@pytest.mark.parametrize("mime", ["text/html; charset=utf-8", "application/problem+json"])
def test_metadata_ignored_identity_decodes_and_rewrites_headers(mime: str) -> None:
    page = "window.__INITIAL_STATE__ = 帧闻录".encode()
    compressed = gzip.compress(page)
    stream = io.BytesIO(compressed)
    headers = headers_for(mime, len(compressed))
    headers["Vary"] = "Accept-Encoding"
    with adapt_source_response(stream, URL, headers, 200) as response:
        assert stream.closed
        assert response.read() == page
        assert "Content-Encoding" not in response.headers
        assert response.headers["Content-Length"] == str(len(page))
        assert response.headers["Vary"] == "Accept-Encoding"
    assert response.closed
    assert headers["Content-Encoding"] == "gzip"
    assert headers["Content-Length"] == str(len(compressed))


@pytest.mark.parametrize("encoding", [None, "identity"])
def test_unencoded_media_stays_streaming(encoding: str | None) -> None:
    stream = io.BytesIO(b"video")
    with adapt_source_response(stream, URL, headers_for("video/mp4", 5, encoding), 200) as response:
        assert not stream.closed
        assert stream.tell() == 0
        assert response.read(2) == b"vi"
    assert stream.closed


def test_header_only_response_does_not_read_or_decode_body() -> None:
    stream = io.BytesIO()
    with adapt_source_response(
        stream, URL, headers_for("video/mp4", 123), 200, method="HEAD"
    ) as response:
        assert response.headers["Content-Length"] == "123"
        assert response.read() == b""
    assert stream.closed


@pytest.mark.parametrize(
    ("data", "mime", "encoding", "status", "error"),
    [
        (b"not gzip", "text/html", "gzip", 200, "INVALID_SOURCE_RESPONSE"),
        (gzip.compress(b"abc")[:-2], "application/json", "gzip", 200, "INVALID_SOURCE_RESPONSE"),
        (b"abc", "video/mp4", "gzip", 200, "SOURCE_ENCODING_UNSUPPORTED"),
        (b"abc", "", "gzip", 200, "SOURCE_ENCODING_UNSUPPORTED"),
        (b"abc", "text/html", "gzip", 206, "SOURCE_ENCODING_UNSUPPORTED"),
        (b"abc", "text/html", "br", 200, "SOURCE_ENCODING_UNSUPPORTED"),
        (b"abc", "text/html", "gzip, gzip", 200, "SOURCE_ENCODING_UNSUPPORTED"),
    ],
)
def test_faults_report_domain_error_and_close(
    data: bytes, mime: str, encoding: str, status: int, error: str
) -> None:
    stream = io.BytesIO(data)
    with pytest.raises(ReelucidError) as caught:
        adapt_source_response(stream, URL, headers_for(mime, len(data), encoding), status)
    assert caught.value.code == error
    assert stream.closed


@pytest.mark.parametrize("wire_limit", [False, True])
def test_metadata_byte_limit_checks_both_sizes(wire_limit: bool) -> None:
    oversized = b"0" * (8 * 1024**2 + 1)
    stream = io.BytesIO(oversized if wire_limit else gzip.compress(oversized))
    headers = headers_for("application/json", 1)
    with pytest.raises(ReelucidError) as caught:
        adapt_source_response(stream, URL, headers, 200)
    assert caught.value.code == "SIZE_LIMIT_EXCEEDED"
    assert stream.closed
