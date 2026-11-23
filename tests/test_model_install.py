import asyncio
import threading
import time
from pathlib import Path
from unittest.mock import patch

import httpx2
import pytest
from fastapi.testclient import TestClient
from huggingface_hub.errors import HfHubHTTPError

from reelucid import models
from reelucid.api import create_app
from reelucid.domain import ReelucidError
from reelucid.models import catalog, install_model, model_path, model_ready, prepare_model
from reelucid.resources import ResourceMonitor, ResourceSample
from reelucid.settings import AppConfig


def test_bundled_small_is_available_with_an_existing_empty_model_volume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundled = tmp_path / "image/small"
    bundled.mkdir(parents=True)
    for name in ("config.json", "model.bin", "tokenizer.json"):
        (bundled / name).touch()
    (bundled / "reelucid-model.json").write_text(
        '{"id":"small","revision":"pinned"}', encoding="utf-8"
    )
    monkeypatch.setattr(models, "BUNDLED_SMALL", bundled)
    volume = tmp_path / "volume"

    assert model_path(volume, "small") == bundled
    assert install_model(volume, "small") == {"id": "small", "revision": "pinned"}
    assert not volume.exists()
    assert [item["id"] for item in catalog(volume) if item["state"] == "ready"] == ["small"]

    override = volume / "asr/small"
    override.mkdir(parents=True)
    for name in ("config.json", "model.bin", "tokenizer.json"):
        (override / name).touch()
    assert model_path(volume, "small") == override


@pytest.mark.parametrize(
    "failure",
    [
        httpx2.ConnectError("connection unavailable"),
        httpx2.ReadTimeout("read timeout"),
        HfHubHTTPError(
            "access denied",
            response=httpx2.Response(401, request=httpx2.Request("GET", "https://example.org")),
        ),
    ],
    ids=["connection", "timeout", "unauthorized"],
)
def test_hub_errors_preserve_model_download_failure(tmp_path: Path, failure: Exception) -> None:
    with (
        patch("huggingface_hub.HfApi.model_info", side_effect=failure),
        pytest.raises(ReelucidError) as caught,
    ):
        install_model(tmp_path, "tiny")

    assert caught.value.code == "MODEL_DOWNLOAD_FAILED" and caught.value.retryable
    assert caught.value.__cause__ is failure
    assert not model_ready(tmp_path / "asr/tiny")


def test_hub_cancellation_propagates(tmp_path: Path) -> None:
    with (
        patch("huggingface_hub.HfApi.model_info", side_effect=asyncio.CancelledError()),
        pytest.raises(asyncio.CancelledError),
    ):
        install_model(tmp_path, "tiny")


def _write_staged_model(local_dir: str, **_kwargs: object) -> None:
    for filename in ("config.json", "model.bin", "tokenizer.json"):
        (Path(local_dir) / filename).write_bytes(b"fixture")


def test_promotion_restores_manual_import_when_commit_fails(tmp_path: Path) -> None:
    target = tmp_path / "asr/tiny"
    target.mkdir(parents=True)
    (target / "manual.txt").write_text("previous")
    with (
        patch("huggingface_hub.HfApi.model_info") as info,
        patch("huggingface_hub.snapshot_download", side_effect=_write_staged_model),
    ):
        info.return_value.sha = "test-revision"
        with (
            pytest.raises(RuntimeError, match="commit failure"),
            prepare_model(tmp_path, "tiny") as prepared,
        ):
            prepared.publish()
            assert model_ready(target)
            raise RuntimeError("commit failure")
        assert not model_ready(target)
        assert (target / "manual.txt").read_text() == "previous"
        result = install_model(tmp_path, "tiny")
        assert model_ready(target) and result["revision"] == "test-revision"
        assert "reelucid-model.json" not in result["files"]


def test_api_install_cancel_during_download_and_retry(tmp_path: Path) -> None:
    runtime = AppConfig(tmp_path)
    runtime.models_dir = tmp_path / "models"
    downloading, release = threading.Event(), threading.Event()

    def download(local_dir: str, **_kwargs: object) -> None:
        _write_staged_model(local_dir)
        downloading.set()
        assert release.wait(10)

    def wait_for_status(client: TestClient, url: str, expected: str) -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if client.get(url).json()["status"] == expected:
                return
            time.sleep(0.05)
        pytest.fail(f"Expected {expected}, got {client.get(url).json()['status']}")

    target = runtime.models_dir / "asr/tiny"
    target.mkdir(parents=True)
    (target / "manual.txt").touch()
    with (
        patch.object(
            ResourceMonitor, "read", return_value=ResourceSample(4, 4 * 1024**3, 3 * 1024**3, 0.1)
        ),
        patch("huggingface_hub.HfApi.model_info") as info,
        patch("huggingface_hub.snapshot_download", side_effect=download),
        TestClient(create_app(runtime)) as client,
    ):
        info.return_value.sha = "test-revision"
        client.headers["Authorization"] = "Bearer " + runtime.admin_token_file.read_text().strip()
        accepted = client.post("/api/v1/admin/asr-models/tiny/install")
        assert accepted.status_code == 202
        url = accepted.json()["links"]["self"]
        try:
            assert downloading.wait(10)
            assert client.post(url + "/cancel").json()["status"] == "cancelling"
        finally:
            release.set()
        wait_for_status(client, url, "cancelled")
        assert (target / "manual.txt").is_file() and not model_ready(target)

        assert client.post(url + "/retry").status_code == 202
        wait_for_status(client, url, "succeeded")
        assert model_ready(target)
