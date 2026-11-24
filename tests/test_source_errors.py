import io
import json
from urllib.error import URLError

import pytest
from yt_dlp.networking.common import Response
from yt_dlp.networking.exceptions import HTTPError, NoSupportingHandlers, TransportError
from yt_dlp.utils import DownloadError, ExtractorError, RegexNotFoundError, UnsupportedError

from reelucid.domain import ReelucidError
from reelucid.source_common import classify_http_error, classify_ytdlp_error

# Synthetic data exercises privacy in both extractor messages and HTTP response metadata.
PRIVATE_URL = "https://media.example/video?token=secret-marker"


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (401, "AUTH_REQUIRED", False),
        (403, "SOURCE_ACCESS_REQUIRED", False),
        (404, "SOURCE_NOT_FOUND", False),
        (412, "SOURCE_PRECONDITION_FAILED", False),
        (429, "RATE_LIMITED", True),
        (503, "DOWNLOAD_FAILED", True),
        (504, "DOWNLOAD_TIMEOUT", True),
    ],
)
def test_wrapped_http_error_beats_extractor_message(
    status: int, code: str, retryable: bool
) -> None:
    with Response(io.BytesIO(), PRIVATE_URL, {}, status, reason="secret-marker") as response:
        with pytest.raises(ExtractorError) as captured:
            raise ExtractorError("Unable to extract metadata", cause=HTTPError(response))
        wrapped = DownloadError(PRIVATE_URL, exc_info=(captured.type, captured.value, captured.tb))
        result = classify_ytdlp_error(wrapped)
    assert result.code == code and result.retryable == retryable
    assert "secret-marker" not in str(result.public())


@pytest.mark.parametrize(
    ("error", "code", "retryable"),
    [
        (TransportError(cause=TimeoutError(PRIVATE_URL)), "DOWNLOAD_TIMEOUT", True),
        (TransportError(PRIVATE_URL), "DOWNLOAD_FAILED", True),
        (UnsupportedError(PRIVATE_URL), "UNSUPPORTED_SOURCE", False),
        (RegexNotFoundError("metadata " + PRIVATE_URL), "EXTRACTOR_BROKEN", False),
        (
            ExtractorError("parse", cause=json.JSONDecodeError("bad", PRIVATE_URL, 0)),
            "EXTRACTOR_BROKEN",
            False,
        ),
        (
            ExtractorError("This video is only available for registered users"),
            "AUTH_REQUIRED",
            False,
        ),
        (
            ExtractorError("Fresh cookies (not necessarily logged in) are needed"),
            "SOURCE_ACCESS_REQUIRED",
            False,
        ),
        (
            ExtractorError("Unexpected response from webpage request"),
            "INVALID_SOURCE_RESPONSE",
            False,
        ),
        (
            DownloadError(
                "ERROR: [sample] id: unable to download video data: HTTP Error 429: Too Many Requests"
            ),
            "RATE_LIMITED",
            True,
        ),
        (
            DownloadError("ERROR: [sample] id: Failed to parse JSON: " + PRIVATE_URL),
            "EXTRACTOR_BROKEN",
            False,
        ),
        (ExtractorError("unavailable: " + PRIVATE_URL), "DOWNLOAD_FAILED", False),
        (
            DownloadError("unexpected upstream cookies/HTTP 403 " + PRIVATE_URL),
            "DOWNLOAD_FAILED",
            False,
        ),
    ],
)
def test_classification_returns_fixed_public_messages(
    error: Exception, code: str, retryable: bool
) -> None:
    result = classify_ytdlp_error(error)
    assert result.code == code and result.retryable == retryable
    assert "secret-marker" not in str(result.public())


def test_domain_error_survives_suppression_and_cyclic_wrappers() -> None:
    original = ReelucidError("NETWORK_POLICY_BLOCKED", "blocked")
    nested = NoSupportingHandlers([], [original])
    original.__cause__ = nested
    outer = DownloadError("Login required")
    outer.__context__ = nested
    outer.__suppress_context__ = True
    assert classify_ytdlp_error(outer) is original


def test_direct_and_extractor_preserve_wrapped_transport_errors() -> None:
    original = ReelucidError("NETWORK_POLICY_BLOCKED", "blocked")
    for error, code in (
        (URLError(original), "NETWORK_POLICY_BLOCKED"),
        (URLError(TimeoutError(PRIVATE_URL)), "DOWNLOAD_TIMEOUT"),
        (URLError(ConnectionError(PRIVATE_URL)), "DOWNLOAD_FAILED"),
    ):
        direct, extractor = classify_http_error(error), classify_ytdlp_error(error)
        assert direct.code == code and direct.public() == extractor.public()
        assert "secret-marker" not in str(direct.public())
