"""Exercise FFmpeg through the authenticated job and artifact API."""

import hashlib
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from reelucid.api import create_app
from reelucid.domain import TERMINAL, JsonObject
from reelucid.media import run_tool, tool
from reelucid.resources import ResourceMonitor, ResourceSample
from reelucid.scheduling import MIB
from reelucid.settings import AppConfig


def _wait(client: TestClient, job_id: str) -> JsonObject:
    deadline = time.monotonic() + 30
    current: JsonObject = {}
    while time.monotonic() < deadline:
        current = client.get(f"/api/v1/jobs/{job_id}").json()
        if current["status"] in TERMINAL:
            return current
        time.sleep(0.1)
    raise AssertionError(f"Job {job_id} did not terminate: {current}")


def test_transcode_presets_publish_downloadable_media_without_an_asr_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The API and encoder are real. Keep admission independent of host memory pressure.
    sample = ResourceSample(4, 4096 * MIB, 4096 * MIB, 0.1)

    def read_resources(_monitor: ResourceMonitor) -> ResourceSample:
        return sample

    monkeypatch.setattr(ResourceMonitor, "read", read_resources)
    source = tmp_path / "source.mkv"
    run_tool(
        [
            tool("ffmpeg"),
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=s=48x32:r=5:d=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-c:v",
            "ffv1",
            "-c:a",
            "pcm_s16le",
            str(source),
        ],
        lambda: False,
    )
    config = AppConfig(tmp_path / "data")
    with TestClient(create_app(config)) as client:
        assert client.get("/api/v1/capabilities").status_code == 401
        client.headers["Authorization"] = f"Bearer {config.admin_token_file.read_text().strip()}"
        capabilities = client.get("/api/v1/capabilities").json()
        assert "transcode" in capabilities["operations"]
        assert capabilities["transcode_presets"] == ["audio_mp3", "audio_wav", "video_mp4"]
        settings = client.get("/api/v1/admin/system-config").json()["config"]
        settings["processing_profiles"][0]["id"] = "custom"
        assert client.put("/api/v1/admin/system-config", json=settings).status_code == 200
        uploaded = client.post("/api/v1/assets", files={"file": (source.name, source.read_bytes())})
        assert uploaded.status_code == 201
        body = {"source": {"kind": "asset", "asset_id": uploaded.json()["asset_id"]}}
        for preset, extension, codecs in (
            ("audio_mp3", "mp3", ["mp3"]),
            ("audio_wav", "wav", ["pcm_s16le"]),
            ("video_mp4", "mp4", ["aac", "h264"]),
        ):
            operation = {"id": "media", "type": "transcode", "options": {"preset": preset}}
            response = client.post("/api/v1/jobs", json={**body, "operations": [operation]})
            assert response.status_code == 202, response.text
            job_id = response.json()["job_id"]
            job = _wait(client, job_id)
            assert job["status"] == "succeeded", job
            assert {stage["name"] for stage in job["stages"]} == {"transcode"}
            result = client.get(f"/api/v1/jobs/{job_id}/result?include=transcript").json()
            assert "transcript" not in result
            (artifact,) = result["artifacts"]
            assert artifact["kind"] == "media" and artifact["format"] == extension
            assert result["outputs"][0]["artifact_id"] == artifact["artifact_id"]
            download = client.get(artifact["content_url"])
            assert download.status_code == 200
            assert len(download.content) == artifact["size_bytes"]
            assert hashlib.sha256(download.content).hexdigest() == artifact["sha256"]
            partial = client.get(artifact["content_url"], headers={"Range": "bytes=0-15"})
            assert partial.status_code == 206 and partial.content == download.content[:16]
            output = tmp_path / f"result.{extension}"
            output.write_bytes(download.content)
            streams = json.loads(
                run_tool(
                    [
                        tool("ffprobe"),
                        "-v",
                        "error",
                        "-show_entries",
                        "stream=codec_name",
                        "-of",
                        "json",
                        str(output),
                    ],
                    lambda: False,
                )
            )["streams"]
            assert sorted(stream["codec_name"] for stream in streams) == codecs

        invalid = {"id": "media", "type": "transcode", "options": {"preset": "custom"}}
        assert (
            client.post("/api/v1/jobs", json={**body, "operations": [invalid]}).status_code == 422
        )
        audio = client.post(
            "/api/v1/assets", files={"file": ("audio.wav", (tmp_path / "result.wav").read_bytes())}
        )
        job_id = client.post(
            "/api/v1/jobs",
            json={
                "source": {"kind": "asset", "asset_id": audio.json()["asset_id"]},
                "operations": [
                    {"id": "media", "type": "transcode", "options": {"preset": "video_mp4"}}
                ],
            },
        ).json()["job_id"]
        failed = _wait(client, job_id)
        assert failed["status"] == "failed" and failed["error"]["code"] == "NO_VIDEO_TRACK"
        assert failed["artifacts"] == []
