from pathlib import Path

import pytest

from reelucid.domain import JsonObject, ReelucidError
from reelucid.resolver import DtkAdapter
from reelucid.resolver_http import ResolverClient
from reelucid.settings import SourceResolver
from reelucid.sources import HttpMediaAdapter


@pytest.mark.parametrize(
    ("access", "error_code"),
    [
        ({"session_bound": True}, "RESOLVER_DOWNLOAD_REQUIRED"),
        ({"session_bound": []}, "RESOLVER_INVALID_RESPONSE"),
    ],
)
def test_dtk_checks_media_access_before_reading_direct_link(
    access: JsonObject, error_code: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def request(_client: ResolverClient, path: str, body: JsonObject | None = None) -> JsonObject:
        if path == "/api/v1/parse":
            return {"success": True, "data": {"task_id": "example"}}
        assert path == "/api/v1/tasks/example"
        return {
            "success": True,
            "data": {
                "state": "done",
                "data": {
                    "kind": "video",
                    "content_id": "123",
                    "title": "Example",
                    "media": {
                        "video": {"url": "https://cdn.example/media.mp4"},
                        "access": access,
                    },
                },
            },
        }

    def unexpected_fetch(*_args: object, **_kwargs: object) -> None:
        pytest.fail("attempted a direct media request")

    monkeypatch.setattr(ResolverClient, "request", request)
    monkeypatch.setattr(HttpMediaAdapter, "acquire", unexpected_fetch)
    adapter = DtkAdapter(SourceResolver(enabled=True, base_url="https://dtk.example"))
    with pytest.raises(ReelucidError) as exc:
        adapter.acquire(
            "https://www.tiktok.com/@example/video/123",
            tmp_path,
            1024,
            True,
            lambda _stage, _data: None,
            lambda: False,
        )
    assert exc.value.code == error_code
    assert not exc.value.retryable
