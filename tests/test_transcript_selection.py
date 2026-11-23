from pathlib import Path
from time import monotonic, sleep
from typing import cast

import pytest
from fastapi.testclient import TestClient

from reelucid import ai
from reelucid.api import create_app
from reelucid.api.schemas import JobRequest
from reelucid.database import Database
from reelucid.domain import TERMINAL, JsonObject, Segment, Transcript
from reelucid.files import digest_file
from reelucid.resources import ResourceMonitor, ResourceSample
from reelucid.service import Service
from reelucid.settings import AppConfig


def publish(db: Database, root: Path, asset_id: str, version: str) -> str:
    job_id = db.create_job({}, {}, asset_id)["job_id"]
    claim = db.claim("fixture", eligible=lambda job: job["job_id"] == job_id)
    assert claim
    transcript = Transcript(
        version_id=version,
        segments=[Segment(segment_id="seg_1", start_ms=10, end_ms=1000, text=version)],
    )
    path = root / "artifacts" / f"{version}.json"
    path.write_text(transcript.model_dump_json(), encoding="utf-8")
    artifact = {
        "id": version,
        "kind": "transcript",
        "format": "json",
        "path": str(path),
        "size": path.stat().st_size,
        "sha256": digest_file(path),
    }
    db.finish(
        job_id,
        claim["lease"],
        {},
        [artifact, {**artifact, "id": f"other_{version}", "kind": "analysis"}],
    )
    return job_id


def test_queued_qa_keeps_reader_transcript_across_restart_and_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = AppConfig(tmp_path)
    app = create_app(config)

    def pause_scheduler(_service: Service) -> None:
        pass

    with monkeypatch.context() as paused:
        paused.setattr(Service, "start", pause_scheduler)
        with TestClient(app) as http:
            token = config.admin_token_file.read_text().strip()
            http.headers["Authorization"] = f"Bearer {token}"
            db = cast(Database, app.state.db)
            asset = http.post("/api/v1/assets", files={"file": ("sample.wav", b"sample")}).json()[
                "asset_id"
            ]
            second = http.post("/api/v1/assets", files={"file": ("other.wav", b"other")}).json()[
                "asset_id"
            ]
            first_job = publish(db, tmp_path, asset, "first")
            second_job = publish(db, tmp_path, asset, "second")
            settings = http.get("/api/v1/admin/system-config").json()["config"]
            settings["providers"] = [{"id": "local", "base_url": "http://localhost/v1"}]
            settings["model_bindings"] = {"text": {"provider_id": "local", "model": "fixture"}}
            settings["processing_profiles"][0]["text_binding_id"] = "text"
            assert http.put("/api/v1/admin/system-config", json=settings).status_code == 200

            def question(asset_id: str, pin: str | None = None) -> JsonObject:
                source = {"kind": "asset", "asset_id": asset_id}
                if pin:
                    source["transcript_artifact_id"] = pin
                return {
                    "source": source,
                    "operations": [
                        {"id": "q", "type": "qa", "options": {"question": "Which version?"}}
                    ],
                }

            body = question(asset, "first")
            idem = {"Idempotency-Key": "question-about-first"}
            pinned = http.post("/api/v1/jobs", json=body, headers=idem)
            assert pinned.status_code == 202, pinned.text
            pinned_id = pinned.json()["job_id"]
            automatic = http.post("/api/v1/jobs", json=question(asset))
            assert automatic.status_code == 202, automatic.text
            automatic_id = automatic.json()["job_id"]
            third_job = publish(db, tmp_path, asset, "third")
            removed_job = publish(db, tmp_path, asset, "removed")
            doomed = http.post("/api/v1/jobs", json=question(asset, "removed"))
            assert doomed.status_code == 202
            doomed_id = doomed.json()["job_id"]
            assert http.delete(f"/api/v1/jobs/{removed_job}").status_code == 204
            assert http.post("/api/v1/jobs", json=body, headers=idem).json()["job_id"] == pinned_id
            assert (
                http.post("/api/v1/jobs", json=question(asset, "third"), headers=idem).status_code
                == 409
            )
            assert http.post("/api/v1/jobs", json=question(second, "first")).status_code == 404
            assert http.post("/api/v1/jobs", json=question(asset, "other_first")).status_code == 404
            assert http.post("/api/v1/jobs", json={"source": body["source"]}).status_code == 422
            assert http.post(f"/api/v1/jobs/{pinned_id}/cancel").status_code == 200
            assert http.post(f"/api/v1/jobs/{pinned_id}/retry").status_code == 202

    async def anchored_answer(*_args: object) -> str:
        return '{"claims":[{"text":"answer","refs":["S1"]}]}'

    def idle_machine(_monitor: ResourceMonitor) -> ResourceSample:
        return ResourceSample(4, 4 << 30, 3 << 30, 0.1)

    monkeypatch.setattr(ai, "chat", anchored_answer)
    monkeypatch.setattr(ResourceMonitor, "read", idle_machine)
    restarted = create_app(config)
    with TestClient(restarted) as http:
        http.headers["Authorization"] = f"Bearer {token}"
        for job_id, version in [(pinned_id, "first"), (automatic_id, "second"), (doomed_id, None)]:
            deadline = monotonic() + 12
            while http.get(f"/api/v1/jobs/{job_id}").json()["status"] not in TERMINAL:
                assert monotonic() < deadline
                sleep(0.05)
            result = http.get(f"/api/v1/jobs/{job_id}/result").json()
            if version is None:
                assert result["status"] == "failed" and result["error"]["code"] == "NOT_FOUND"
                continue
            assert result["status"] == "succeeded", result
            assert result["evidence"][0]["transcript_version_id"] == version
            assert result["evidence"][0]["quotes"] == [version]
        db = cast(Database, restarted.state.db)
        latest = db.transcript_for(asset)
        assert latest and latest["artifact_id"] == "third"
        assert http.delete(f"/api/v1/jobs/{first_job}").status_code == 204
        assert http.delete(f"/api/v1/jobs/{second_job}").status_code == 204
        assert http.delete(f"/api/v1/jobs/{third_job}").status_code == 204
        fallback = db.transcript_for(asset)
        assert fallback and fallback["job_id"] in (pinned_id, automatic_id)
        assert http.post("/api/v1/jobs", json=body).status_code == 404
        assert (
            http.get(f"/api/v1/jobs/{pinned_id}/result?include=transcript").json()["transcript"][
                "version_id"
            ]
            == "first"
        )


def test_unpinned_requests_keep_the_pre_upgrade_idempotency_payload() -> None:
    source = {"kind": "asset", "asset_id": "file"}
    for request_source in (source, {**source, "transcript_artifact_id": None}):
        assert (
            JobRequest.model_validate({"source": request_source}).model_dump()["source"] == source
        )
