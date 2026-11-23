"""Local transport/assembly contracts, distinct from anonymous live-platform tests."""

from __future__ import annotations

import json
import socket
import threading
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from yt_dlp import YoutubeDL
from yt_dlp.networking.exceptions import NoSupportingHandlers

from reelucid.domain import Cancelled, JsonObject, ReelucidError
from reelucid.media import probe, run_tool, tool
from reelucid.resolver import DtkAdapter
from reelucid.settings import SourceResolver, SystemSettings
from reelucid.source_common import DownloadDeadline, source_error
from reelucid.sources import SourceRegistry, YtDlpAdapter
from reelucid.xiaohongshu import XhsAdapter
from reelucid.ytdlp_transport import CheckedYoutubeDL


@pytest.fixture
def source_server(
    monkeypatch: pytest.MonkeyPatch,
) -> Generator[tuple[str, dict[str, bytes], list[tuple[str, dict[str, str]]]]]:
    files: dict[str, bytes] = {}
    seen: list[tuple[str, dict[str, str]]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append((self.path, dict(self.headers)))
            content = files[self.path]
            self.send_response(200)
            self.send_header(
                "Content-Type", "application/json" if self.path.startswith("/api/") else "video/mp4"
            )
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length)
            json.loads(body)
            files["__posted_json"] = body
            self.do_GET()

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def connect(address: tuple[str, int], *_args: Any, **_kwargs: Any) -> socket.socket:
        assert address == ("media.example", 80)
        return socket.create_connection(("127.0.0.1", server.server_port), timeout=5)

    # Redirect only the source socket to the fixture; production policy is tested separately.
    monkeypatch.setattr("reelucid.network.checked_connection", connect)
    try:
        yield f"http://127.0.0.1:{server.server_port}", files, seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def tracks(tmp_path: Path) -> tuple[bytes, bytes]:
    video, audio = tmp_path / "video.mp4", tmp_path / "audio.m4a"
    for path, source, codec in (
        (video, "color=c=blue:s=64x64:d=2", "mpeg4"),
        (audio, "sine=frequency=400:duration=2", "aac"),
    ):
        run_tool(
            [
                tool("ffmpeg"),
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                source,
                "-c",
                codec,
                str(path),
            ],
            lambda: False,
        )
    return video.read_bytes(), audio.read_bytes()


def quiet_progress(_event: str, _payload: JsonObject) -> None:
    pass


def selected_info(protocol: str = "http") -> JsonObject:
    return {
        "id": "fixture",
        "title": "Fixture",
        "duration": 2,
        "ext": "mp4",
        "protocol": protocol,
        "url": "http://media.example/video",
        "http_headers": {},
    }


def test_real_split_download_merge_and_combined_budget(
    tmp_path: Path, source_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _endpoint, files, _seen = source_server
    video, audio = tracks(tmp_path)
    files.update({"/video": video, "/audio": audio})

    def extract(self: Any, *_args: Any, **_kwargs: Any) -> JsonObject:
        assert "bestvideo" in self.params["format"]
        return {
            **selected_info(),
            "requested_formats": [
                selected_info(),
                {**selected_info(), "url": "http://media.example/audio", "ext": "m4a"},
            ],
        }

    monkeypatch.setattr(YoutubeDL, "extract_info", extract)
    result = YtDlpAdapter().acquire(
        "http://media.example/watch", tmp_path / "ok", 1024**2, True, quiet_progress, lambda: False
    )
    output = Path(result["path"])
    assert probe(output).has_audio and probe(output).has_video
    assert not (output.parent / "track-0.media").exists()
    with pytest.raises(ReelucidError) as error:
        YtDlpAdapter().acquire(
            "http://media.example/watch",
            tmp_path / "limited",
            max(len(video), len(audio)) + 1,
            True,
            quiet_progress,
            lambda: False,
        )
    assert error.value.code == "SIZE_LIMIT_EXCEEDED"


def test_hls_live_and_unsafe_fallback_fail_closed(
    tmp_path: Path, source_server: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _endpoint, files, _seen = source_server

    def extract(*_args: Any, **_kwargs: Any) -> JsonObject:
        return selected_info("m3u8_native")

    monkeypatch.setattr(YoutubeDL, "extract_info", extract)
    for manifest, expected in (
        (b"#EXTM3U\n#EXTINF:1\nsegment.ts\n", "LIVE_NOT_SUPPORTED"),
        (
            b'#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI="key"\n#EXT-X-ENDLIST',
            "NETWORK_POLICY_UNSUPPORTED",
        ),
    ):
        files["/video"] = manifest
        with pytest.raises(ReelucidError) as error:
            YtDlpAdapter().acquire(
                "http://media.example/watch",
                tmp_path,
                1024**2,
                True,
                quiet_progress,
                lambda: False,
            )
        assert error.value.code == expected


def test_nested_policy_error_is_not_internal_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = ReelucidError("NETWORK_POLICY_BLOCKED", "blocked")

    def extract(*_args: Any, **_kwargs: Any) -> Any:
        raise NoSupportingHandlers([], [original])

    monkeypatch.setattr(YoutubeDL, "extract_info", extract)
    with pytest.raises(ReelucidError) as error:
        YtDlpAdapter().acquire(
            "https://example.com/watch", tmp_path, 1000, True, quiet_progress, lambda: False
        )
    assert error.value is original


def test_yt_dlp_request_handlers_keep_cancellation_scoped_to_one_job(source_server: Any) -> None:
    _, files, seen = source_server
    files["/sample"] = b"sample"
    url = "http://media.example/sample"
    with (
        CheckedYoutubeDL(
            {"quiet": True, "proxy": ""}, deadline=DownloadDeadline(lambda: True), proxy=None
        ) as stopped,
        CheckedYoutubeDL(
            {"quiet": True, "proxy": ""}, deadline=DownloadDeadline(lambda: False), proxy=None
        ) as active,
    ):
        with pytest.raises(NoSupportingHandlers) as error:
            stopped.urlopen(url)
        assert isinstance(source_error(error.value), Cancelled)
        with active.urlopen(url) as response:
            assert response.read() == b"sample"
    assert [path for path, _ in seen] == ["/sample"]


def test_dtk_async_contract_and_credential_boundary(tmp_path: Path, source_server: Any) -> None:
    endpoint, files, seen = source_server
    files["/api/v1/parse"] = json.dumps(
        {"success": True, "data": {"task_id": "fixture", "state": "queued"}}
    ).encode()
    files["/api/v1/tasks/fixture"] = json.dumps(
        {
            "success": True,
            "data": {
                "state": "done",
                "data": {
                    "kind": "video",
                    "content_id": "123",
                    "title": "Example",
                    "media": {"video": {"url": "http://media.example/video"}},
                },
            },
        }
    ).encode()
    files["/video"] = b"media bytes"
    adapter = DtkAdapter(SourceResolver(enabled=True, base_url=endpoint), "resolver-secret")
    result = adapter.acquire(
        "https://www.douyin.com/video/123", tmp_path, 1024, True, quiet_progress, lambda: False
    )
    assert result["adapter_id"] == "dtk"
    assert seen[0][1]["X-Api-Key"] == "resolver-secret"
    assert all(
        "X-Api-Key" not in headers and "Authorization" not in headers
        for path, headers in seen
        if path == "/video"
    )
    cdn_headers = next(headers for path, headers in seen if path == "/video")
    assert cdn_headers["Referer"] == "https://www.douyin.com/"
    assert "Mozilla" in cdn_headers["User-Agent"]
    with pytest.raises(Cancelled):
        adapter.acquire(
            "https://www.douyin.com/video/123", tmp_path, 1024, True, quiet_progress, lambda: True
        )


def test_new_routes_and_existing_disabled_settings_are_preserved() -> None:
    registry = SourceRegistry()
    assert registry.select("https://b23.tv/abc").id == "bilibili"
    assert registry.select("https://www.bilibili.com/video/BV123?p=2").id == "bilibili"
    assert registry.select("https://v.douyin.com/abc", prefer_dtk=True).id == "dtk"
    assert (
        registry.select("https://www.tiktok.com/@a/video/123", "yt_dlp", prefer_dtk=True).id
        == "yt_dlp"
    )
    settings = SystemSettings.model_validate({"adapters": {"yt_dlp": False}})
    assert settings.adapters["yt_dlp"] is False and settings.adapters["bilibili"] is True
    assert registry.select("https://xhslink.cn/o/example", prefer_xhs=True).id == "xhs"


def test_xhs_reuses_resolver_without_remote_download_or_cookie_leak(
    tmp_path: Path,
    source_server: Any,
) -> None:
    endpoint, files, seen = source_server
    files["/xhs/detail"] = json.dumps(
        {
            "data": {
                "作品ID": "fixture",
                "作品标题": "Example",
                "作品类型": "视频",
                "下载地址": ["http://media.example/video"],
            },
            "params": {"cookie": "must-not-be-forwarded"},
        }
    ).encode()
    files["/video"] = b"media bytes"
    cookie = tmp_path / "cookies.txt"
    cookie.write_text(
        "# Netscape HTTP Cookie File\n.xiaohongshu.com\tTRUE\t/\tTRUE\t0\tweb_session\txhs-only\n.example.com\tTRUE\t/\tTRUE\t0\tsession\tunrelated-secret\n",
        encoding="utf-8",
    )
    adapter = XhsAdapter(SourceResolver(enabled=True, base_url=endpoint))
    result = adapter.acquire(
        "https://xhslink.com/o/test",
        tmp_path / "media",
        1024,
        True,
        quiet_progress,
        lambda: False,
        cookie,
    )
    assert result["adapter_id"] == "xhs"
    posted = json.loads(files["__posted_json"])
    assert posted["download"] is False and posted["check_record"] is False
    assert "xhs-only" in posted["cookie"] and "unrelated-secret" not in posted["cookie"]
    assert all(
        "Cookie" not in headers and "X-Api-Key" not in headers
        for path, headers in seen
        if path == "/video"
    )


def test_xhs_rejects_unavailable_posts_and_image_albums(tmp_path: Path, source_server: Any) -> None:
    endpoint, files, _seen = source_server
    adapter = XhsAdapter(SourceResolver(enabled=True, base_url=endpoint))
    for data, expected in (
        (None, "SOURCE_ACCESS_REQUIRED"),
        (
            {
                "作品ID": "photo",
                "作品标题": "Album",
                "作品类型": "图文",
                "下载地址": ["https://cdn.example/a.jpg", "https://cdn.example/b.jpg"],
            },
            "UNSUPPORTED_SOURCE",
        ),
    ):
        files["/xhs/detail"] = json.dumps(
            {"message": "HTTP 200 is not extraction success", "data": data}
        ).encode()
        with pytest.raises(ReelucidError) as error:
            adapter.acquire(
                "https://www.xiaohongshu.com/explore/test",
                tmp_path,
                1024,
                True,
                quiet_progress,
                lambda: False,
            )
        assert error.value.code == expected
