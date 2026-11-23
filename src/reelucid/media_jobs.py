"""Execute a claimed media job and publish its artifacts under the active lease."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Callable, Coroutine
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from reelucid.ai import analyze, local_endpoint
from reelucid.cancellation import JobCancellation
from reelucid.database import Database, new_id
from reelucid.domain import (
    Cancelled,
    ChunkStrategy,
    JsonObject,
    PipelineOptions,
    ReelucidError,
    Transcript,
)
from reelucid.exports import write_exports
from reelucid.files import contained, digest_file, digest_json, write_json
from reelucid.job_config import find_profile, settings_value
from reelucid.models import model_path
from reelucid.ports import Progress
from reelucid.resolver import DtkAdapter
from reelucid.scheduling import Phase
from reelucid.security import Security
from reelucid.settings import AppConfig, ProcessingProfile, SystemSettings
from reelucid.sources import SourceRegistry
from reelucid.worker import ComputeWorker
from reelucid.xiaohongshu import XhsAdapter

log = logging.getLogger(__name__)

type ReportProgress = Callable[[JsonObject], Coroutine[Any, Any, None]]
type ReserveStage = Callable[[Phase], AbstractAsyncContextManager[tuple[ComputeWorker | None, int]]]

DEFAULT_FORMATS = ["json", "srt", "vtt", "txt"]


@dataclass(frozen=True, slots=True)
class MediaJob:
    id: str
    lease: str
    attempt: int
    work: Path
    snapshot: JsonObject
    settings: SystemSettings
    profile: ProcessingProfile
    operations: list[JsonObject]
    options: JsonObject
    chunk_strategy: ChunkStrategy
    cancelled: JobCancellation
    report: ReportProgress
    reserve: ReserveStage

    def check_cancelled(self) -> None:
        if self.cancelled.is_set():
            raise Cancelled()


class MediaJobRunner:
    def __init__(
        self, config: AppConfig, database: Database, security: Security, sources: SourceRegistry
    ) -> None:
        self.config = config
        self.db = database
        self.security = security
        self.sources = sources
        self.plugin_locks = {identifier: asyncio.Lock() for identifier in sources.plugin_ids}

    async def run(
        self,
        claim: JsonObject,
        reserve: ReserveStage,
        cancelled: JobCancellation,
        report: ReportProgress,
    ) -> None:
        request = claim["request"]
        saved = claim["snapshot"]
        settings = settings_value(
            {key: value for key, value in saved.items() if key != "secret_fingerprints"}
        )
        job = MediaJob(
            id=claim["job_id"],
            lease=claim["lease"],
            attempt=claim["attempt"],
            work=self.config.data_dir / "work" / claim["job_id"],
            snapshot=saved,
            settings=settings,
            profile=find_profile(settings, request.get("processing_profile_id", "local-default")),
            operations=request["operations"],
            options=request.get("options", {}),
            chunk_strategy=request.get("asr_chunk_strategy", "silence"),
            cancelled=cancelled,
            report=report,
            reserve=reserve,
        )
        await asyncio.to_thread(job.work.mkdir, parents=True, exist_ok=True)
        asset = await self.db.run(self.db.asset, claim["asset_id"])
        job.check_cancelled()
        raw = await self._transcript(job, asset)
        artifacts = await asyncio.to_thread(self._prepare_artifacts, job, raw["artifacts"])
        if artifacts:
            await self.db.run(self.db.publish_artifacts, job.id, job.lease, artifacts)
        result: JsonObject = {
            "schema_version": "1.0",
            "job_id": job.id,
            "asset_id": asset["asset_id"],
            "status": "succeeded",
            "transcript": raw["transcript"],
            "source_video_available": raw["media"].get("has_video", False),
            "artifacts": [_public_artifact(artifact) for artifact in artifacts],
            "evidence": [],
            "outputs": [],
            "warnings": list(raw["warnings"]),
        }
        await self._operations(job, result, artifacts)
        await self._finish(job, result, artifacts)

    async def _credential(self, identifier: str | None, saved: JsonObject) -> str | None:
        if not identifier:
            return None
        current = await self.db.run(self.db.settings)
        encrypted = current.get("secrets", {}).get(identifier)
        if not encrypted:
            raise ReelucidError("CREDENTIAL_REVOKED", "所需凭据已移除。")
        if digest_json(encrypted) != saved.get("secret_fingerprints", {}).get(identifier):
            raise ReelucidError("CREDENTIAL_CHANGED", "凭据已改变，请使用新配置创建任务。")
        return self.security.decrypt(encrypted)

    async def _source(self, job: MediaJob, asset: JsonObject) -> Path:
        resource = asset["resources"].get("source")
        if resource is None:
            async with job.reserve("download"):
                resource = await self._download(job, asset)
            await self.db.run(self.db.update_asset, asset["asset_id"], {"source": resource})
        return contained(self.config.data_dir / "assets", Path(resource["path"]))

    async def _download(self, job: MediaJob, asset: JsonObject) -> JsonObject:
        if self.config.offline:
            raise ReelucidError("OFFLINE_MODE", "离线模式不允许下载视频。")
        await self.db.run(self.db.stage, job.id, job.lease, "acquire", {})
        config = job.settings
        url = self.security.decrypt(asset["source"]["url"])
        adapter = self.sources.select(
            url,
            asset["source"].get("adapter_id"),
            prefer_dtk=config.source_resolver.enabled,
            prefer_xhs=config.xhs_resolver.enabled,
        )
        if not config.adapters.get(adapter.id, False):
            raise ReelucidError("ADAPTER_DISABLED", "该下载适配器已禁用。")
        if isinstance(adapter, DtkAdapter):
            key = await self._credential(config.source_resolver.credential_ref, job.snapshot)
            adapter = DtkAdapter(config.source_resolver, key, proxy=self.config.source_proxy)
        elif isinstance(adapter, XhsAdapter):
            key = await self._credential(config.xhs_resolver.credential_ref, job.snapshot)
            adapter = XhsAdapter(
                config.xhs_resolver,
                key,
                local_command=self.config.xhs_command,
                proxy=self.config.source_proxy,
            )
        cookie = await self._credential(asset["source"].get("credential_profile_id"), job.snapshot)
        receive = _download_progress(job)

        def acquire() -> JsonObject:
            cookie_file = job.work / "cookies.txt" if cookie else None
            # The download thread owns the cookie file through adapter shutdown.
            try:
                if cookie_file:
                    cookie_file.write_text(cookie or "", encoding="utf-8")
                    cookie_file.chmod(0o600)
                return adapter.acquire(
                    url,
                    self.config.data_dir / "assets" / asset["asset_id"],
                    min(self.config.max_bytes, config.max_upload_mb * 1024**2),
                    job.options.get("keep_source", True),
                    receive,
                    job.cancelled.is_set,
                    cookie_file,
                )
            finally:
                if cookie_file:
                    cookie_file.unlink(missing_ok=True)

        if lock := self.plugin_locks.get(adapter.id):
            # Serialize calls to the shared plugin instance.
            async with lock:
                job.check_cancelled()
                return await asyncio.to_thread(acquire)
        return await asyncio.to_thread(acquire)

    async def _transcript(
        self,
        job: MediaJob,
        asset: JsonObject,
    ) -> JsonObject:
        pinned = job.options.get("transcript_artifact_id")
        # Older jobs lack a pinned version and use the latest available transcript.
        legacy_reuse = "transcript_artifact_id" not in job.options and not any(
            operation["type"] == "transcribe" for operation in job.operations
        )
        if pinned is not None or legacy_reuse:
            existing = await self.db.run(self.db.transcript_for, asset["asset_id"], pinned)
            if existing:
                async with job.reserve("ai"):
                    return await self._reuse_transcript(job, asset, existing)
        source = await self._source(job, asset)
        profile = job.profile
        options = PipelineOptions(
            model_path=str(model_path(self.config.models_dir, profile.model_id)),
            model_id=profile.model_id,
            device=profile.device,
            compute_type=profile.compute_type,
            language=job.options.get("language", profile.language),
            glossary=profile.glossary,
            chunk_strategy=job.chunk_strategy,
            cpu_threads=job.settings.cpu_threads,
            max_duration_seconds=job.settings.max_duration_seconds,
            formats=job.options.get("export_formats", DEFAULT_FORMATS),
        )

        async def compute_event(kind: str, payload: JsonObject) -> None:
            if kind == "stage":
                await self.db.run(self.db.stage, job.id, job.lease, payload["stage"], payload)
            elif kind == "progress":
                await job.report(payload)
            elif kind == "chunk":
                path = contained(job.work, Path(payload["path"]))
                if await asyncio.to_thread(digest_file, path) != payload["sha256"]:
                    raise ReelucidError("CHECKPOINT_CORRUPT", "分块检查点校验失败。")
                await self.db.run(self.db.save_chunk, job.id, job.lease, payload)

        async with job.reserve("asr") as (worker, cpu_threads):
            assert worker is not None
            raw = await worker.run(
                {
                    "job_id": job.id,
                    "lease": job.lease,
                    "source": str(source),
                    "output": str(job.work / "output"),
                    "options": options.model_dump(),
                    "cpu_threads": cpu_threads,
                    "resume": await self.db.run(self.db.resume_chunks, job.id),
                },
                compute_event,
            )
        await self.db.run(self.db.update_asset, asset["asset_id"], {}, raw["media"])
        return raw

    async def _reuse_transcript(
        self, job: MediaJob, asset: JsonObject, existing: JsonObject
    ) -> JsonObject:
        path = contained(self.config.data_dir / "artifacts", Path(existing["path"]))

        def read_transcript() -> Transcript:
            try:
                data = path.read_bytes()
            except FileNotFoundError as exc:
                raise ReelucidError("ARTIFACT_CORRUPT", "文稿文件缺失，请检查数据卷。") from exc
            if hashlib.sha256(data).hexdigest() != existing["sha256"]:
                raise ReelucidError("ARTIFACT_CORRUPT", "缓存文稿校验失败。")
            return Transcript.model_validate_json(data)

        transcript = await asyncio.to_thread(read_transcript)
        return {
            "transcript": transcript.model_dump(),
            "artifacts": await asyncio.to_thread(
                write_exports,
                transcript,
                job.work / "output",
                job.options.get("export_formats", DEFAULT_FORMATS),
            ),
            "media": asset["media"],
            "warnings": transcript.warnings,
        }

    async def _operations(
        self, job: MediaJob, result: JsonObject, artifacts: list[JsonObject]
    ) -> None:
        for operation in job.operations:
            job.check_cancelled()
            operation_type = operation["type"]
            output = {"id": operation["id"], "type": operation_type}
            if operation_type == "transcribe":
                result["outputs"].append({**output, "status": "succeeded"})
                continue
            try:
                async with job.reserve("ai"):
                    await self.db.run(self.db.stage, job.id, job.lease, operation_type, {})
                    analysis = await self._analyze(job, operation, result)
                path = job.work / f"analysis-{operation['id']}.json"
                await asyncio.to_thread(write_json, path, analysis)
                generated = await asyncio.to_thread(
                    self._prepare_artifacts,
                    job,
                    [{"path": str(path), "format": "json", "kind": "analysis"}],
                )
                await self.db.run(self.db.publish_artifacts, job.id, job.lease, generated)
                artifacts.extend(generated)
                result["artifacts"].extend(_public_artifact(item) for item in generated)
                result["evidence"].extend(analysis["evidence"])
                result["outputs"].append(
                    {**output, "status": "succeeded", "claims": analysis["claims"]}
                )
                result["warnings"].extend(analysis["warnings"])
                await self.db.run(
                    self.db.complete_stage, job.id, job.lease, operation_type, "succeeded"
                )
            except Cancelled:
                raise
            except ReelucidError as exc:
                await self.db.run(
                    self.db.complete_stage, job.id, job.lease, operation_type, "failed"
                )
                result["outputs"].append({**output, "status": "failed", "error": exc.public()})
                result["warnings"].append(f"{operation['id']}:{exc.code}")
                if operation.get("required", True):
                    result["status"] = "partially_succeeded"

    async def _analyze(
        self, job: MediaJob, operation: JsonObject, result: JsonObject
    ) -> JsonObject:
        if operation["type"] == "visual_analyze":
            raise ReelucidError("CAPABILITY_UNAVAILABLE", "当前版本未提供视觉分析。")
        identifier = job.profile.text_binding_id
        if not identifier or identifier not in job.settings.model_bindings:
            raise ReelucidError("AI_NOT_CONFIGURED", "请先为处理预设配置文本模型。")
        binding = job.settings.model_bindings[identifier]
        provider = next(
            (item for item in job.settings.providers if item.id == binding.provider_id), None
        )
        if provider is None:
            raise ReelucidError("PROVIDER_NOT_FOUND", "模型提供方不存在。")
        if not local_endpoint(provider.base_url) and (
            self.config.offline or not job.profile.allow_remote_ai
        ):
            raise ReelucidError("REMOTE_AI_DISABLED", "该预设未允许远程 AI。")
        secret = await self._credential(provider.credential_ref, job.snapshot)
        analysis = await analyze(
            Transcript.model_validate(result["transcript"]),
            operation,
            provider,
            binding,
            secret,
            cancellation=job.cancelled,
        )
        for item in analysis["evidence"]:
            item["asset_id"] = result["asset_id"]
        return analysis

    async def _finish(self, job: MediaJob, result: JsonObject, artifacts: list[JsonObject]) -> None:
        job.check_cancelled()
        manifest = job.work / f"result-attempt-{job.attempt}.json"
        await asyncio.to_thread(write_json, manifest, result)
        manifests = await asyncio.to_thread(
            self._prepare_artifacts,
            job,
            [{"path": str(manifest), "format": "json", "kind": "manifest"}],
        )
        artifacts.extend(manifests)
        # Segment and word arrays belong in the immutable transcript artifact.
        stored = {
            **result,
            "transcript": {
                key: item for key, item in result["transcript"].items() if key != "segments"
            },
        }
        await self.db.run(self.db.finish, job.id, job.lease, stored, artifacts, result["status"])
        if result["status"] == "succeeded":
            await asyncio.to_thread(_release_pcm, job)

    def _prepare_artifacts(self, job: MediaJob, entries: list[JsonObject]) -> list[JsonObject]:
        result: list[JsonObject] = []
        for entry in entries:
            source = contained(job.work, Path(entry["path"]))
            format = entry["format"]
            if format not in {"json", "srt", "vtt", "txt", "md"}:
                raise ReelucidError("INVALID_ARTIFACT", "产物格式无效。")
            identifier = new_id("art")
            destination = self.config.data_dir / "artifacts" / job.id / f"{identifier}.{format}"
            destination.parent.mkdir(parents=True, exist_ok=True)
            checksum, size = digest_file(source), source.stat().st_size
            # Move the file into place before SQLite records its path.
            source.replace(destination)
            result.append(
                {
                    "id": identifier,
                    "kind": entry["kind"],
                    "format": format,
                    "path": str(destination),
                    "sha256": checksum,
                    "size": size,
                }
            )
        return result


def _download_progress(job: MediaJob) -> Progress:
    loop = asyncio.get_running_loop()
    last_report = 0.0

    def receive(event: str, payload: JsonObject) -> None:
        nonlocal last_report
        job.check_cancelled()
        if event == "progress" and time.monotonic() - last_report >= 0.3:
            last_report = time.monotonic()
            asyncio.run_coroutine_threadsafe(job.report(payload), loop).result(timeout=15)

    return receive


def _public_artifact(artifact: JsonObject) -> JsonObject:
    return {
        "artifact_id": artifact["id"],
        "kind": artifact["kind"],
        "format": artifact["format"],
        "sha256": artifact["sha256"],
        "size_bytes": artifact["size"],
        "content_url": f"/api/v1/artifacts/{artifact['id']}/content",
    }


def _release_pcm(job: MediaJob) -> None:
    for wav in (job.work / "output/checkpoints").glob("*/normalized.wav"):
        try:
            contained(job.work, wav).unlink(missing_ok=True)
        except OSError:
            log.warning("work_cleanup_failed job=%s", job.id)
