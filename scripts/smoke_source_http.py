"""Black-box source URL / HTTP 412 checks against a running release container."""

import argparse
import json
import os
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

SOURCE_URL = "http://93.184.216.34/reelucid-smoke.mp4?access=release-smoke-secret"
MARKER = b"release-smoke-secret"
API = "/api/v1"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("phase", choices=("create", "verify"))
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--fixture", type=Path)
    args = parser.parse_args()
    base = os.environ.get("REELUCID_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    token = os.environ["REELUCID_TOKEN"]
    opener = build_opener(ProxyHandler({}))

    def call(path, *, data=None, auth=True, headers=None, status=200):
        request_headers = {"Content-Type": "application/json", **(headers or {})}
        if auth is True:
            request_headers["Authorization"] = f"Bearer {token}"
        elif isinstance(auth, str):
            request_headers["Authorization"] = f"Bearer {auth}"
        try:
            response = opener.open(Request(base + path, data=data, headers=request_headers), timeout=15)
        except HTTPError as error:
            response = error
        with response:
            payload = response.read()
            assert response.code == status, (path, response.code, status)
            return payload, response.headers

    def public(path):
        payload, _ = call(path)
        assert MARKER not in payload, f"Sensitive source URL leaked from {path}"
        return json.loads(payload)

    assert json.loads(call(API + "/health/live", auth=False)[0])["version"] == args.version
    assert json.loads(call(API + "/health/ready", auth=False)[0])["ready"] is True
    assert public(API + "/capabilities")["version"] == args.version
    schema = json.loads(call("/openapi.json", auth=False)[0])
    assert schema["info"]["version"] == args.version
    route = schema["paths"][API + "/assets/{identifier}/source-url"]["get"]
    assert {"HTTPBearer": []} in route["security"]

    def verify(state):
        path = API + "/assets/" + state["asset_id"]
        job_path = API + "/jobs/" + state["job_id"]
        for auth in (False, "invalid-release-smoke-token"):
            payload, _ = call(path + "/source-url", auth=auth, status=401)
            assert MARKER not in payload
        public(path)
        payload, response_headers = call(path + "/source-url")
        assert json.loads(payload) == {"url": SOURCE_URL}
        assert response_headers["Cache-Control"] == "private, no-store"
        for asset in (state["upload_id"], "ast_missing_release_smoke"):
            payload, _ = call(API + "/assets/" + asset + "/source-url", status=404)
            assert json.loads(payload)["error"]["code"] == "NOT_FOUND"
            assert MARKER not in payload
        for snapshot in (public(job_path), public(job_path + "/result")):
            assert snapshot["status"] == "failed"
            assert snapshot["error"]["code"] == "SOURCE_PRECONDITION_FAILED", snapshot["error"]["code"]
            assert snapshot["error"]["retryable"] is False
        assert MARKER not in call(API + "/jobs")[0]
        print(f"source URL and HTTP 412 verified ({args.phase})")

    if args.phase == "verify":
        verify(json.loads(args.state.read_text(encoding="utf-8")))
        return
    if args.fixture is None:
        parser.error("--fixture is required for the create phase")
    boundary = "reelucid-source-smoke"
    multipart = (
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="speech.mkv"\r\n'
        "Content-Type: video/x-matroska\r\n\r\n"
    ).encode() + args.fixture.read_bytes() + f"\r\n--{boundary}--\r\n".encode()
    upload = json.loads(call(API + "/assets", data=multipart, headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}"
    }, status=201)[0])
    data = json.dumps({
        "source": {"kind": "url", "url": SOURCE_URL, "adapter_id": "http_media"},
        "operations": [{"id": "media", "type": "transcode", "options": {"preset": "audio_mp3"}}],
    }).encode()
    request_headers = {"Idempotency-Key": "release-source-412"}
    payload, _ = call(API + "/jobs", data=data, headers=request_headers, status=202)
    assert MARKER not in payload
    job = json.loads(payload)
    replay = json.loads(call(API + "/jobs", data=data, headers=request_headers, status=202)[0])
    assert (replay["job_id"], replay["asset_id"]) == (job["job_id"], job["asset_id"])
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        snapshot = public(API + "/jobs/" + job["job_id"])
        if snapshot["status"] in {"failed", "succeeded", "cancelled", "partially_succeeded"}:
            break
        time.sleep(0.5)
    else:
        raise TimeoutError("Synthetic HTTP 412 job did not finish")
    state = {"asset_id": job["asset_id"], "job_id": job["job_id"], "upload_id": upload["asset_id"]}
    verify(state)
    args.state.write_text(json.dumps(state), encoding="utf-8")


if __name__ == "__main__":
    main()
