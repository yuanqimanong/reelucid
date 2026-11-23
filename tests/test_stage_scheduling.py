"""Exercise the mixed download/ASR/QA path with controlled external services."""

import asyncio
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest

from reelucid.database import Database
from reelucid.domain import JsonObject, ReelucidError, Transcript
from reelucid.files import digest_file
from reelucid.media_jobs import MediaJob
from reelucid.resources import ResourceSample
from reelucid.scheduling import MIB
from reelucid.security import Security
from reelucid.service import Service
from reelucid.settings import AppConfig


async def test_slow_download_and_ai_leave_one_cpu_slot_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = AppConfig(tmp_path)
    config.prepare()
    db = Database(tmp_path / "db/reelucid.db")
    db.initialize()
    service = Service(config, db, Security(config))
    gates = {name: asyncio.Event() for name in ("download", "downloaded", "asr", "ai", "finish")}

    async def download(job: MediaJob, _asset: JsonObject) -> JsonObject:
        gates["download"].set()
        await job.cancelled.run(gates["downloaded"].wait)
        return {"path": str(tmp_path / "assets/downloaded.wav")}

    class Worker:
        generation = "test"

        def __init__(self) -> None:
            self.cancelled = Event()
            self.process: SimpleNamespace | None = None

        def start(self) -> None:
            self.process = SimpleNamespace(is_alive=lambda: True, pid=None)

        def stop(self) -> None:
            self.process = None

        async def run(self, request: JsonObject, _receive: object) -> JsonObject:
            assert request["cpu_threads"] == 1
            gates["asr"].set()
            await gates["finish"].wait()
            raise ReelucidError("WORKER_DIED", "test")

    async def analyze(job: MediaJob, _op: JsonObject, result: JsonObject) -> JsonObject:
        assert result["transcript"]["version_id"] == "tr_saved"
        gates["ai"].set()
        await job.cancelled.run(gates["finish"].wait)
        return {"claims": [], "evidence": [], "warnings": []}

    sample = ResourceSample(1, 2048 * MIB, 2048 * MIB, 0.1)
    monkeypatch.setattr(service.monitor, "read", lambda: sample)
    monkeypatch.setattr(service.media, "_download", download)
    monkeypatch.setattr(service.media, "_analyze", analyze)
    monkeypatch.setattr("reelucid.service.ComputeWorker", Worker)
    config.max_concurrent_jobs = 4
    snapshot = db.settings()
    for asset in ("url", "second-url", "local", "saved"):
        resources = (
            {"source": {"path": str(tmp_path / "assets/local.wav")}} if asset == "local" else {}
        )
        db.add_asset(asset, "upload" if asset == "local" else "url", asset, resources)
    db.create_job({}, snapshot, "saved")
    owner = db.claim("seed")
    assert owner
    path = tmp_path / "artifacts/saved.json"
    path.write_text(
        Transcript(version_id="tr_saved", segments=[]).model_dump_json(), encoding="utf-8"
    )
    db.finish(
        owner["job_id"],
        owner["lease"],
        {},
        [
            {
                "id": "art_saved",
                "kind": "transcript",
                "format": "json",
                "path": str(path),
                "sha256": digest_file(path),
                "size": path.stat().st_size,
            }
        ],
    )

    def add(asset: str, source: str, operation: str) -> str:
        req: JsonObject = {
            "source": {"kind": source},
            "operations": [{"id": "op", "type": operation}],
        }
        if operation == "qa":
            req["options"] = {"transcript_artifact_id": "art_saved"}
        return db.create_job(req, snapshot, asset)["job_id"]

    slow = add("url", "url", "transcribe")
    waiting = add("second-url", "url", "transcribe")
    cpu = add("local", "asset", "transcribe")
    qa = add("saved", "asset", "qa")
    try:
        service.start()
        async with asyncio.timeout(12):
            await asyncio.gather(*(gates[name].wait() for name in ("download", "asr", "ai")))
            assert service.scheduling_status()["active_stages"] == {
                "download": 1,
                "asr": 1,
                "ai": 1,
            }
            assert (await db.run(db.job, waiting))["status"] == "queued"
            download_task, asr_task, ai_task = (service.running[j][1] for j in (slow, cpu, qa))
            await db.run(db.cancel, waiting)
            channel = service.broker.subscribe(slow)
            try:
                gates["downloaded"].set()
                while True:
                    event = await channel.get()
                    if event["type"] == "stage.started" and event["payload"]["stage"] == "wait_asr":
                        break
            finally:
                service.broker.unsubscribe(slow, channel)
            await db.run(db.cancel, slow)
            service.cancel(slow)
            await download_task
            assert (await db.run(db.job, slow))["status"] == "cancelled"
            assert service.scheduling_status()["active_stages"] == {"asr": 1, "ai": 1}
            gates["finish"].set()
            await asyncio.gather(asr_task, ai_task)
            assert (await db.run(db.job, qa))["status"] == "succeeded"
            assert (await db.run(db.job, cpu))["error"]["code"] == "WORKER_DIED"
            assert all(e.allocation is None for e, _ in service.running.values())
    finally:
        gates["finish"].set()
        await service.stop()
        db.close()
