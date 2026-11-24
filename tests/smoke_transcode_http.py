"""Check a running release using a local speech video and an administrator token."""

import hashlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen

type JsonObject = dict[str, Any]


def main(source: Path) -> None:
    base = os.environ.get("REELUCID_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    authorization = f"Bearer {os.environ['REELUCID_TOKEN']}"

    def call(
        path: str,
        *,
        data: bytes | None = None,
        content_type: str = "application/json",
        byte_range: str | None = None,
        status: int = 200,
    ) -> bytes:
        headers = {"Authorization": authorization, "Content-Type": content_type}
        if byte_range:
            headers["Range"] = byte_range
        with urlopen(Request(base + path, data=data, headers=headers), timeout=15) as response:
            assert response.status == status, (path, response.status)
            return response.read()

    capabilities = json.loads(call("/api/v1/capabilities"))
    presets = {"audio_mp3": "mp3", "audio_wav": "wav", "video_mp4": "mp4"}
    assert "transcode" in capabilities["operations"]
    assert set(capabilities["transcode_presets"]) == presets.keys()
    boundary = "reelucid-release-verification"
    multipart = (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="speech.mkv"\r\n'
            "Content-Type: video/x-matroska\r\n\r\n"
        ).encode()
        + source.read_bytes()
        + f"\r\n--{boundary}--\r\n".encode()
    )
    asset_id = json.loads(
        call(
            "/api/v1/assets",
            data=multipart,
            content_type=f"multipart/form-data; boundary={boundary}",
            status=201,
        )
    )["asset_id"]

    def job(operations: list[JsonObject]) -> JsonObject:
        request = {
            "source": {"kind": "asset", "asset_id": asset_id},
            "operations": operations,
            "options": {"language": "en", "export_formats": ["json", "srt", "vtt"]},
        }
        identifier = json.loads(
            call("/api/v1/jobs", data=json.dumps(request).encode(), status=202)
        )["job_id"]
        path = f"/api/v1/jobs/{identifier}"
        deadline = time.monotonic() + 150
        while time.monotonic() < deadline:
            snapshot = json.loads(call(path))
            if snapshot["status"] == "succeeded":
                return json.loads(call(f"{path}/result?include=transcript"))
            if snapshot["status"] in {"failed", "cancelled", "partially_succeeded"}:
                raise AssertionError(json.loads(call(f"{path}/result")))
            time.sleep(0.5)
        raise TimeoutError(f"{path} did not finish")

    def download(artifact: JsonObject) -> bytes:
        identifier = artifact["artifact_id"]
        metadata = json.loads(call(f"/api/v1/artifacts/{identifier}"))
        path = f"/api/v1/artifacts/{identifier}/content"
        assert path == artifact["content_url"] == metadata["content_url"]
        data = call(path)
        assert len(data) == artifact["size_bytes"] == metadata["size_bytes"] and len(data) > 16
        assert hashlib.sha256(data).hexdigest() == artifact["sha256"] == metadata["sha256"]
        assert call(path, byte_range="bytes=0-15", status=206) == data[:16]
        return data

    for preset, extension in presets.items():
        result = job([{"id": "media", "type": "transcode", "options": {"preset": preset}}])
        assert "transcript" not in result
        (artifact,) = result["artifacts"]
        assert artifact["kind"] == "media" and artifact["format"] == extension
        assert artifact["artifact_id"] == result["outputs"][0]["artifact_id"]
        media = download(artifact)
        assert {
            "mp3": media.startswith(b"ID3"),
            "wav": media.startswith(b"RIFF"),
            "mp4": media[4:8] == b"ftyp",
        }[extension]
        print(f"{preset}: succeeded, verified {len(media)} bytes and HTTP Range")

    combined = job(
        [
            {"id": "transcript", "type": "transcribe"},
            {"id": "media", "type": "transcode", "options": {"preset": "audio_mp3"}},
        ]
    )
    segments = combined["transcript"]["segments"]
    text = " ".join(segment["text"].lower() for segment in segments)
    assert "meeting" in text and "report" in text, text
    artifacts = {artifact["format"]: artifact for artifact in combined["artifacts"]}
    assert artifacts["mp3"]["kind"] == "media" and download(artifacts["mp3"]).startswith(b"ID3")
    assert b"-->" in download(artifacts["srt"])
    assert download(artifacts["vtt"]).startswith(b"WEBVTT")
    print(
        f"transcribe + audio_mp3: {len(segments)} speech segments, verified SRT, VTT and MP3 downloads"
    )


if __name__ == "__main__":
    main(Path(sys.argv[1]))
