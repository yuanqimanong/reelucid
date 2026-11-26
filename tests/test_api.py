import os
import sqlite3
import time
from collections.abc import Generator
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from reelucid.api import create_app
from reelucid.database import Database
from reelucid.job_config import settings_value
from reelucid.service import Service
from reelucid.settings import AppConfig


def test_url_admission_registers_only_accepted_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def hold_queue(_service: Service) -> None:
        pass

    def model_available(_path: Path) -> bool:
        return True

    monkeypatch.setattr(Service, "start", hold_queue)
    monkeypatch.setattr("reelucid.api.jobs.model_ready", model_available)
    config = AppConfig(tmp_path)
    with TestClient(create_app(config)) as http:
        http.headers["Authorization"] = f"Bearer {config.admin_token_file.read_text().strip()}"
        settings_url = "/api/v1/admin/system-config"
        settings = http.get(settings_url).json()["config"]
        settings["queue_limit"] = 1
        assert http.put(settings_url, json=settings).status_code == 200
        url = "https://example.org/sample.mp4?access=test-key"
        body = {"source": {"kind": "url", "url": url}}
        headers = {"Idempotency-Key": "test-url-admission"}
        accepted = http.post("/api/v1/jobs", json=body, headers=headers)
        assert accepted.status_code == 202
        path = f"/api/v1/assets/{accepted.json()['asset_id']}"
        assert url not in http.get(path).text
        auth = http.headers.pop("Authorization")
        assert http.get(path + "/source-url").status_code == 401
        http.headers["Authorization"] = auth
        source = http.get(path + "/source-url")
        assert source.json() == {"url": url}
        assert source.headers["Cache-Control"] == "private, no-store"
        replay = http.post("/api/v1/jobs", json=body, headers=headers)
        assert replay.json()["asset_id"] == accepted.json()["asset_id"]
        assert replay.json()["job_id"] == accepted.json()["job_id"]
        different = {"source": {"kind": "url", "url": "https://example.org/other.mp4"}}
        assert http.post("/api/v1/jobs", json=different, headers=headers).status_code == 409
        assert http.post("/api/v1/jobs", json=different).status_code == 429
        with closing(sqlite3.connect(tmp_path / "db/reelucid.db")) as db:
            assert db.execute("SELECT id FROM assets").fetchall() == [
                (accepted.json()["asset_id"],)
            ]
            assert db.execute("SELECT count(*) FROM jobs").fetchone() == (1,)
            assert url not in db.execute("SELECT source FROM assets").fetchone()[0]


@pytest.fixture
def client(tmp_path: Path) -> Generator[TestClient]:
    with TestClient(create_app(AppConfig(tmp_path))) as http:
        token = (tmp_path / "secrets/admin_token").read_text().strip()
        http.headers["Authorization"] = f"Bearer {token}"
        yield http


def test_pyp_purge_requires_a_bearer_and_is_idempotent(client: TestClient) -> None:
    endpoint = "/api/v1/maintenance/pyp/purge-job"
    body = {"task_id": str(uuid4())}
    auth = client.headers.pop("Authorization")
    assert client.post(endpoint, json=body).status_code == 401
    client.headers["Authorization"] = auth
    result = client.post(endpoint, json=body)
    assert result.status_code == 200
    assert result.json() == {"removed_job": False, "removed_assets": 0}


def test_web_fallback_keeps_the_api_namespace_and_asset_mount(tmp_path: Path) -> None:
    config = AppConfig(tmp_path / "data")
    config.web_dir = tmp_path / "web"
    assets = config.web_dir / "assets"
    assets.mkdir(parents=True)
    (config.web_dir / "index.html").write_text("<title>reader</title>")
    (assets / "app.js").write_text("const ready = true")
    with TestClient(create_app(config)) as client:
        assert client.get("/jobs/from-a-bookmark").text == "<title>reader</title>"
        assert client.get("/assets/app.js").text == "const ready = true"
        unknown = client.get("/api/v1/missing")
        assert unknown.status_code == 404 and unknown.json()["error"]["code"] == "HTTP_404"
        assert client.get("/api/v1/health/live").json()["status"] == "ok"


def test_auth_config_cas_upload_and_media_range(client: TestClient) -> None:
    token = client.headers.pop("Authorization")
    assert client.get("/api/v1/jobs").status_code == 401
    assert client.post("/api/v1/assets", content=b"unauthorized").status_code == 401
    client.headers["Authorization"] = token
    settings = client.get("/api/v1/admin/system-config").json()["config"]
    assert client.put("/api/v1/admin/system-config", json=settings).status_code == 200
    assert client.put("/api/v1/admin/system-config", json=settings).status_code == 409
    upload = client.post(
        "/api/v1/assets", files={"file": ("example.wav", b"fake-media-range-test", "audio/wav")}
    )
    assert upload.status_code == 201
    asset = upload.json()["asset_id"]
    assert client.get(f"/api/v1/assets/{asset}/source-url").status_code == 404
    media = client.get(f"/api/v1/assets/{asset}/media", headers={"Range": "bytes=0-3"})
    assert media.status_code == 206 and media.content == b"fake"
    assert (
        client.post(
            "/api/v1/assets", files={"file": ("playlist.mp4", b"#EXTM3U\nhttp://127.0.0.1/secret")}
        ).status_code
        == 422
    )
    settings = client.get("/api/v1/admin/system-config").json()["config"]
    settings["adapters"]["upload"] = False
    client.put("/api/v1/admin/system-config", json=settings)
    assert (
        client.post("/api/v1/assets", files={"file": ("example.wav", b"data")}).status_code == 400
    )


def test_source_inventory_and_optional_resolver_configuration(client: TestClient) -> None:
    capabilities = client.get("/api/v1/capabilities").json()
    assert {"bilibili", "yt_dlp", "dtk", "http_media", "xhs"} <= set(
        capabilities["available_adapters"]
    )
    assert not capabilities["source_resolver"]["configured"]
    xhs = capabilities["xhs_resolver"]
    assert xhs["configured"] == xhs["local_available"]
    assert xhs["mode"] == ("local" if xhs["local_available"] else "remote")
    inventory = client.get(capabilities["source_catalogue_url"]).json()
    assert inventory["extractor_count"] == len(inventory["extractors"])
    assert inventory["extractor_count"] > 100
    settings = client.get("/api/v1/admin/system-config").json()["config"]
    settings["source_resolver"] = {"enabled": True, "base_url": "", "timeout_seconds": 10}
    assert client.put("/api/v1/admin/system-config", json=settings).status_code == 422
    settings["adapters"] = []
    assert client.put("/api/v1/admin/system-config", json=settings).status_code == 422


def test_dtk_session_bound_downloads_require_an_explicit_boolean(client: TestClient) -> None:
    settings = client.get("/api/v1/admin/system-config").json()["config"]
    resolver = settings["source_resolver"]
    assert resolver["download_session_bound"] is False
    assert "download_session_bound" not in settings["xhs_resolver"]
    resolver.update(enabled=True, base_url="https://dtk.example")
    for invalid in (1, 0, "false", "true", None):
        resolver["download_session_bound"] = invalid
        assert client.put("/api/v1/admin/system-config", json=settings).status_code == 422
    assert (
        client.get("/api/v1/capabilities").json()["source_resolver"]["download_session_bound"]
        is False
    )
    resolver["download_session_bound"] = True
    assert client.put("/api/v1/admin/system-config", json=settings).status_code == 200
    assert (
        client.get("/api/v1/capabilities").json()["source_resolver"]["download_session_bound"]
        is True
    )


def test_fresh_install_with_xhs_plugin_defaults_to_local(tmp_path: Path) -> None:
    runtime = AppConfig(tmp_path)
    runtime.xhs_command = tmp_path / "test-resolver"
    with TestClient(create_app(runtime)) as client:
        token = runtime.admin_token_file.read_text().strip()
        client.headers["Authorization"] = f"Bearer {token}"
        config = client.get("/api/v1/admin/system-config").json()["config"]
        xhs = config["xhs_resolver"]
        assert xhs["enabled"] and xhs["mode"] == "local"
        assert not xhs["base_url"] and xhs["credential_ref"] is None
        assert not config["source_resolver"]["enabled"]
        capabilities = client.get("/api/v1/capabilities").json()["xhs_resolver"]
        assert capabilities["configured"] and capabilities["local_available"]

        for invalid in (
            {"mode": "local", "base_url": "https://resolver.example"},
            {"mode": "local", "credential_ref": "old-remote-key"},
            {"mode": "remote", "base_url": ""},
        ):
            config["xhs_resolver"] = {**xhs, **invalid}
            assert client.put("/api/v1/admin/system-config", json=config).status_code == 422

    # Restarting without the plugin leaves the administrator's saved mode intact.
    runtime.xhs_command = None
    with TestClient(create_app(runtime)) as client:
        client.headers["Authorization"] = f"Bearer {token}"
        capabilities = client.get("/api/v1/capabilities").json()["xhs_resolver"]
        assert capabilities["mode"] == "local"
        assert not capabilities["configured"] and not capabilities["local_available"]
        config = client.get("/api/v1/admin/system-config").json()["config"]
        config["cpu_threads"] += 1
        assert client.put("/api/v1/admin/system-config", json=config).status_code == 200
        config["revision"] += 1
        config["xhs_resolver"]["enabled"] = False
        assert client.put("/api/v1/admin/system-config", json=config).status_code == 200
        config["revision"] += 1
        config["xhs_resolver"]["enabled"] = True
        assert client.put("/api/v1/admin/system-config", json=config).status_code == 422


@pytest.mark.parametrize("was_enabled", [True, False])
def test_installing_plugin_preserves_legacy_settings_and_job_snapshots(
    tmp_path: Path, was_enabled: bool
) -> None:
    runtime = AppConfig(tmp_path)
    runtime.xhs_command = tmp_path / "test-resolver"
    db = Database(tmp_path / "db/reelucid.db")
    try:
        db.initialize()
        old = db.settings()
        old["source_resolver"] = {"enabled": True, "base_url": "https://dtk.example"}
        old["xhs_resolver"] = {
            "enabled": was_enabled,
            "base_url": "http://old-resolver:5556",
            "credential_ref": "old-key",
        }
        db.save_settings(old, old["revision"])
        legacy_snapshot = {k: v for k, v in old.items() if k != "secrets"}
        job = db.create_job({}, legacy_snapshot, None)
        db.cancel(job["job_id"])
    finally:
        db.close()

    with TestClient(create_app(runtime)) as client:
        token = runtime.admin_token_file.read_text().strip()
        client.headers["Authorization"] = f"Bearer {token}"
        config = client.get("/api/v1/admin/system-config").json()["config"]
        assert config["source_resolver"]["download_session_bound"] is False
        xhs = config["xhs_resolver"]
        assert xhs["enabled"] == was_enabled and xhs["mode"] == "remote"
        assert xhs["base_url"] == "http://old-resolver:5556" and xhs["credential_ref"] == "old-key"
        config["xhs_resolver"] = {"enabled": True, "mode": "local"}
        config["source_resolver"]["download_session_bound"] = True
        assert client.put("/api/v1/admin/system-config", json=config).status_code == 200

    db = Database(tmp_path / "db/reelucid.db")
    try:
        saved = db.job(job["job_id"], private=True)["snapshot"]
        assert saved == legacy_snapshot
        assert settings_value(saved).xhs_resolver.mode == "remote"
        assert settings_value(saved).source_resolver.download_session_bound is False
    finally:
        db.close()


def test_browser_cookie_requires_matching_origin(client: TestClient, tmp_path: Path) -> None:
    token = client.headers.pop("Authorization").removeprefix("Bearer ")
    assert client.post("/api/v1/auth/session", json={"token": token}).status_code == 200
    assert client.get("/api/v1/jobs").status_code == 200
    assert client.delete("/api/v1/auth/session").status_code == 403
    assert (
        client.delete("/api/v1/auth/session", headers={"Origin": "http://evil.test"}).status_code
        == 403
    )
    assert (
        client.delete("/api/v1/auth/session", headers={"Origin": "http://testserver"}).status_code
        == 200
    )
    assert client.get("/api/v1/jobs").status_code == 401


@pytest.mark.model
def test_real_upload_to_worker_and_transcript(client: TestClient) -> None:
    fixture = os.environ.get("REELUCID_TEST_MEDIA")
    if not fixture:
        pytest.skip("Set REELUCID_TEST_MEDIA to a real speech fixture and REELUCID_MODELS_DIR")
    settings = client.get("/api/v1/admin/system-config").json()["config"]
    settings["processing_profiles"][0]["model_id"] = "tiny"
    assert client.put("/api/v1/admin/system-config", json=settings).status_code == 200
    source = Path(fixture)
    upload = client.post("/api/v1/assets", files={"file": (source.name, source.read_bytes())})
    assert upload.status_code == 201, upload.text
    body = {
        "source": {"kind": "asset", "asset_id": upload.json()["asset_id"]},
        "options": {"language": "en"},
    }
    response = client.post("/api/v1/jobs", json=body, headers={"Idempotency-Key": "real-speech"})
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    assert (
        client.post("/api/v1/jobs", json=body, headers={"Idempotency-Key": "real-speech"}).json()[
            "job_id"
        ]
        == job_id
    )
    deadline = time.monotonic() + 120
    job = response.json()
    while time.monotonic() < deadline:
        job = client.get(f"/api/v1/jobs/{job_id}").json()
        if job["status"] in {"succeeded", "failed", "partially_succeeded", "cancelled"}:
            break
        time.sleep(0.2)
    assert job["status"] == "succeeded", job
    result = client.get(f"/api/v1/jobs/{job_id}/result?include=transcript").json()
    transcript = result["transcript"]
    assert transcript["segments"] and transcript["time_unit"] == "ms"
    text = " ".join(s["text"] for s in transcript["segments"]).lower()
    assert "meeting" in text and "budget" in text, text
    srt = next(a for a in job["artifacts"] if a["format"] == "srt")
    assert "-->" in client.get(srt["content_url"]).text
    reused = client.post(
        "/api/v1/jobs",
        json={
            "source": body["source"],
            "operations": [{"id": "optional-summary", "type": "summarize", "required": False}],
        },
    )
    assert reused.status_code == 202, reused.text
    reused_id = reused.json()["job_id"]
    reused_job = reused.json()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        reused_job = client.get(f"/api/v1/jobs/{reused_id}").json()
        if reused_job["status"] in {"succeeded", "failed", "partially_succeeded"}:
            break
        time.sleep(0.1)
    assert reused_job["status"] == "succeeded", reused_job
    assert not any(stage["name"] == "asr" for stage in reused_job["stages"])
    assert (
        next(stage for stage in reused_job["stages"] if stage["name"] == "summarize")["status"]
        == "failed"
    )
    assert client.delete(f"/api/v1/jobs/{job_id}").status_code == 204
    after_delete = client.get(f"/api/v1/jobs/{reused_id}/result?include=transcript")
    assert after_delete.status_code == 200 and after_delete.json()["transcript"]["segments"]
