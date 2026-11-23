from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from reelucid.api import create_app
from reelucid.domain import JsonObject, ReelucidError
from reelucid.resolver import DtkAdapter
from reelucid.settings import AppConfig, DtkResolver

DOWNLOAD_ID = "a286e988-1329-4c17-9f75-31b47c751231"
DETAIL = f"/api/v1/downloads/{DOWNLOAD_ID}"
FILE = DETAIL + "/files/video.mp4"
SAMPLE = "https://www.tiktok.com/@example/video/123"
MEDIA = b"stored video fixture"
KEY = "dtk-fixture-key"


@pytest.fixture
def resolver_server() -> Generator[tuple[str, JsonObject, list[tuple[str, str, str | None]]]]:
    data: JsonObject = {
        "detail": {
            "id": DOWNLOAD_ID,
            "platform": "tiktok",
            "content_id": "123",
            "state": "partial",
            "on_disk": True,
            "files": [
                {"name": "cover.jpg", "kind": "cover", "state": "failed", "bytes": 0},
                {
                    "name": "video.mp4",
                    "export_name": "dtk-tiktok-export.mp4",
                    "kind": "video",
                    "state": "done",
                    "bytes": len(MEDIA),
                    "sha256": hashlib.sha256(MEDIA).hexdigest(),
                },
            ],
        },
    }
    seen: list[tuple[str, str, str | None]] = []

    class Handler(BaseHTTPRequestHandler):
        def json_response(self, response: JsonObject, status: int = 200) -> None:
            self.reply(
                json.dumps({"success": status < 400, "data": response}).encode(),
                "application/json",
                status,
            )

        def reply(self, payload: bytes, mime: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:
            seen.append(("GET", self.path, self.headers.get("X-API-Key")))
            if self.path == "/api/v1/tasks/parse-1":
                self.json_response(
                    data.get("parse")
                    or {
                        "state": "done",
                        "data": {
                            "kind": "video",
                            "content_id": "123",
                            "title": "Public sample",
                            "media": {"access": {"session_bound": True}},
                        },
                    }
                )
            elif self.path == DETAIL:
                if data.get("queued_polls", 0) > 0:
                    data["queued_polls"] -= 1
                    self.json_response({"state": "queued"})
                else:
                    self.json_response(data["detail"])
            elif self.path == FILE and (redirect := data.get("redirect")):
                self.send_response(302)
                self.send_header("Location", redirect)
                self.end_headers()
            elif self.path == FILE:
                self.reply(data.get("media", MEDIA), "video/mp4")
            else:
                self.json_response({}, 404)

        def do_POST(self) -> None:
            seen.append(("POST", self.path, self.headers.get("X-API-Key")))
            payload = self.rfile.read(int(self.headers["Content-Length"]))
            data[self.path] = json.loads(payload)
            if self.path == "/api/v1/parse":
                self.json_response({"task_id": "parse-1"}, 202)
            elif self.path == "/api/v1/downloads":
                self.json_response(
                    {"download_id": DOWNLOAD_ID, "reused": "already_stored"},
                    data.get("post_status", 200),
                )
            else:
                self.json_response({}, 404)

        def log_message(self, format: str, *args: object) -> None:
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}", data, seen
        finally:
            server.shutdown()
            thread.join(timeout=3)


def adapter(base_url: str) -> DtkAdapter:
    return DtkAdapter(
        DtkResolver(enabled=True, base_url=base_url, download_session_bound=True), KEY
    )


def acquire(base_url: str, folder: Path, limit: int = 1024) -> JsonObject:
    return adapter(base_url).acquire(
        SAMPLE, folder, limit, True, lambda _stage, _data: None, lambda: False
    )


def test_official_api_reuses_download_and_reads_completed_video_from_partial_record(
    resolver_server: tuple[str, JsonObject, list[tuple[str, str, str | None]]], tmp_path: Path
) -> None:
    endpoint, data, seen = resolver_server
    result = acquire(endpoint, tmp_path)
    assert Path(result["path"]).read_bytes() == MEDIA
    assert result["sha256"] == hashlib.sha256(MEDIA).hexdigest()
    assert data["/api/v1/downloads"] == {
        "platform": "tiktok",
        "content_id": "123",
        "skip_existing": True,
    }
    assert seen[-3:] == [
        ("POST", "/api/v1/downloads", KEY),
        ("GET", DETAIL, KEY),
        ("GET", FILE, KEY),
    ]
    assert not (tmp_path / "source.pending").exists()


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ("redirect", "RESOLVER_REDIRECT_BLOCKED"),
        ("hash", "DOWNLOAD_INCOMPLETE"),
        ("path", "RESOLVER_INVALID_RESPONSE"),
        ("other_post", "RESOLVER_INVALID_RESPONSE"),
        ("limit", "SIZE_LIMIT_EXCEEDED"),
        ("not_configured", "RESOLVER_DOWNLOAD_UNAVAILABLE"),
        ("identity", "RESOLVER_IDENTITY_UNAVAILABLE"),
    ],
)
def test_stored_download_fails_closed(
    resolver_server: tuple[str, JsonObject, list[tuple[str, str, str | None]]],
    tmp_path: Path,
    change: str,
    expected: str,
) -> None:
    endpoint, data, seen = resolver_server
    limit = 1024
    if change == "redirect":
        data["redirect"] = "/unrelated"
    elif change == "hash":
        data["media"] = b"x" * len(MEDIA)
    elif change == "path":
        data["detail"]["files"][1]["name"] = "../video.mp4"
    elif change == "other_post":
        data["detail"]["content_id"] = "124"
    elif change == "limit":
        limit = len(MEDIA) - 1
    elif change == "not_configured":
        data["post_status"] = 501
    elif change == "identity":
        data["parse"] = {"state": "failed", "error": {"code": "IDENTITY_POOL_EXHAUSTED"}}
    with pytest.raises(ReelucidError) as exc:
        acquire(endpoint, tmp_path, limit)
    assert exc.value.code == expected
    assert all(path != "/unrelated" for _, path, _ in seen)
    if change in {"path", "other_post", "limit", "not_configured", "identity"}:
        assert all(path != FILE for _, path, _ in seen)
    assert not list(tmp_path.iterdir())


@pytest.mark.model
def test_managed_download_job_creates_real_subtitles(
    resolver_server: tuple[str, JsonObject, list[tuple[str, str, str | None]]], tmp_path: Path
) -> None:
    fixture = os.environ.get("REELUCID_TEST_MEDIA")
    if not fixture:
        pytest.skip("REELUCID_TEST_MEDIA is not set")
    endpoint, data, seen = resolver_server
    media = Path(fixture).read_bytes()
    data["media"] = media
    data["detail"]["files"][1].update(bytes=len(media), sha256=hashlib.sha256(media).hexdigest())
    data["post_status"] = 202
    data["queued_polls"] = 1
    with TestClient(create_app(AppConfig(tmp_path))) as client:
        token = (tmp_path / "secrets/admin_token").read_text().strip()
        client.headers["Authorization"] = f"Bearer {token}"
        client.put("/api/v1/admin/credentials/resolver-dtk", json={"value": KEY}).raise_for_status()
        config = client.get("/api/v1/admin/system-config").raise_for_status().json()["config"]
        config["processing_profiles"][0]["model_id"] = "tiny"
        config["source_resolver"] = {
            "enabled": True,
            "base_url": endpoint,
            "credential_ref": "resolver-dtk",
            "download_session_bound": True,
        }
        client.put("/api/v1/admin/system-config", json=config).raise_for_status()
        created = (
            client.post(
                "/api/v1/jobs",
                json={"source": {"kind": "url", "url": SAMPLE, "adapter_id": "dtk"}},
            )
            .raise_for_status()
            .json()
        )
        path = f"/api/v1/jobs/{created['job_id']}"
        deadline = time.monotonic() + 60
        while True:
            job = client.get(path).raise_for_status().json()
            if job["status"] in {"succeeded", "failed", "cancelled", "partially_succeeded"}:
                break
            assert time.monotonic() < deadline
            time.sleep(0.3)
        assert job["status"] == "succeeded", job.get("error")
        result = client.get(path + "/result?include=transcript").raise_for_status().json()
        assert len(result["transcript"]["segments"]) > 0
        subtitle = next(item for item in job["artifacts"] if item["format"] == "srt")
        body = client.get(subtitle["content_url"]).raise_for_status().content
        assert b" --> " in body and hashlib.sha256(body).hexdigest() == subtitle["sha256"]
        assert [request_path for method, request_path, _ in seen if method == "GET"].count(
            DETAIL
        ) == 2
