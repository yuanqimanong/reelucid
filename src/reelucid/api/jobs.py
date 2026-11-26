"""Job submission, transitions and artifact-backed results."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Header, Query, Response
from fastapi.responses import JSONResponse

from reelucid.api.auth import require_bearer
from reelucid.api.runtime import ConfigDep, DatabaseDep, SecurityDep, ServiceDep
from reelucid.api.schemas import AssetSource, JobRequest, JobResult, PurgePypJob, UrlSource
from reelucid.database import NewUrlAsset
from reelucid.domain import TERMINAL, JsonObject, ReelucidError, choose_chunk_strategy
from reelucid.files import contained, digest_json
from reelucid.job_config import find_profile, settings_value, snapshot
from reelucid.media import available_transcode_presets
from reelucid.models import model_path, model_ready
from reelucid.network import validate_url

router = APIRouter()


@router.post("/jobs", status_code=202, tags=["Jobs"])
async def create_job(
    db: DatabaseDep,
    security: SecurityDep,
    runtime: ConfigDep,
    body: JobRequest,
    response: Response,
    idempotency_key: Annotated[str | None, Header(max_length=128)] = None,
) -> JsonObject:
    request = body.model_dump()
    checksum = digest_json(request)
    if idempotency_key and (old := await db.run(db.idempotent, idempotency_key, checksum)):
        response.headers["Location"] = old["links"]["self"]
        return old
    raw_settings = await db.run(db.settings)
    current = settings_value(raw_settings)
    needs_transcript = any(op.type != "transcode" for op in body.operations)
    profile = find_profile(current, body.processing_profile_id) if needs_transcript else None
    presets = {op.options.preset for op in body.operations if op.type == "transcode"}
    if presets and not presets.issubset(await asyncio.to_thread(available_transcode_presets)):
        raise ReelucidError("CAPABILITY_UNAVAILABLE", "当前 FFmpeg 安装缺少所选转码预设。")
    for operation in body.operations:
        if operation.type == "transcode":
            continue
        assert profile is not None
        if (
            operation.type == "visual_analyze" or operation.options.use_visual
        ) and operation.required:
            raise ReelucidError("CAPABILITY_UNAVAILABLE", "当前版本尚未提供视觉分析。")
        if (
            operation.type not in {"transcribe", "visual_analyze"}
            and operation.required
            and not profile.text_binding_id
        ):
            raise ReelucidError(
                "AI_NOT_CONFIGURED", "请先为预设绑定文本模型，或将该 AI 操作设为可选。"
            )
    reuse = None
    if isinstance(body.source, AssetSource):
        asset = await db.run(db.asset, body.source.asset_id)
        if needs_transcript and not any(op.type == "transcribe" for op in body.operations):
            reuse = await db.run(
                db.transcript_for, asset["asset_id"], body.source.transcript_artifact_id
            )
        asset_id = asset["asset_id"]
    else:
        validate_url(body.source.url)
        asset_id = None
    # Pin the resolved transcript without changing the client request hash.
    request["options"]["transcript_artifact_id"] = reuse["artifact_id"] if reuse else None
    if (
        profile is not None
        and not reuse
        and not await asyncio.to_thread(
            model_ready, model_path(runtime.models_dir, profile.model_id)
        )
    ):
        raise ReelucidError(
            "MODEL_NOT_READY", "本地 ASR 模型未安装，请在设置中安装并选择默认模型。"
        )
    url_asset = None
    if isinstance(body.source, UrlSource):
        if runtime.offline:
            raise ReelucidError("OFFLINE_MODE", "离线模式不接受 URL 下载。")
        url_asset = NewUrlAsset(
            host=urlsplit(body.source.url).hostname or "URL",
            encrypted_url=security.encrypt(body.source.url),
            adapter_id=body.source.adapter_id,
            credential_profile_id=body.source.credential_profile_id,
        )
        request["source"]["url"] = "[stored encrypted in asset]"
    if profile is not None:
        request["asr_chunk_strategy"] = choose_chunk_strategy(
            runtime.asr_chunk_policy, body.options.language, profile.model_id
        )
    job = await db.run(
        db.create_job,
        request,
        snapshot(raw_settings),
        asset_id,
        idem_key=idempotency_key,
        request_hash=checksum,
        limit=current.queue_limit,
        url_asset=url_asset,
    )
    response.headers["Location"] = job["links"]["self"]
    return job


@router.get("/jobs", tags=["Jobs"])
async def list_jobs(
    db: DatabaseDep,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: float | None = None,
    status: str | None = None,
    created_before: datetime | None = None,
    created_after: datetime | None = None,
) -> JsonObject:
    return await db.run(
        db.jobs,
        limit,
        cursor,
        status,
        created_before.timestamp() if created_before else None,
        created_after.timestamp() if created_after else None,
    )


@router.get("/jobs/{identifier}", tags=["Jobs"])
async def get_job(db: DatabaseDep, identifier: str) -> JsonObject:
    return await db.run(db.job, identifier)


@router.post("/jobs/{identifier}/cancel", status_code=202, tags=["Jobs"])
async def cancel_job(
    db: DatabaseDep, service: ServiceDep, identifier: str, response: Response
) -> JsonObject:
    result = await db.run(db.cancel, identifier)
    service.cancel(identifier)
    if result["status"] in TERMINAL:
        response.status_code = 200
    return result


@router.post("/jobs/{identifier}/retry", status_code=202, tags=["Jobs"])
async def retry_job(db: DatabaseDep, identifier: str) -> JsonObject:
    return await db.run(db.retry, identifier)


@router.delete("/jobs/{identifier}", status_code=204, tags=["Jobs"])
async def delete_job(db: DatabaseDep, identifier: str) -> Response:
    await db.run(db.delete, identifier)
    return Response(status_code=204)


@router.post("/maintenance/pyp/purge-job", tags=["Jobs"], dependencies=[Depends(require_bearer)])
async def purge_pyp_job(db: DatabaseDep, runtime: ConfigDep, body: PurgePypJob) -> JsonObject:
    return await db.run(
        db.purge_pyp_job,
        runtime.data_dir,
        "pyp-" + body.task_id.hex,
        body.delete_source,
        body.asset_id,
    )


@router.get("/jobs/{identifier}/result", tags=["Jobs"], response_model=JobResult)
async def get_result(
    db: DatabaseDep, runtime: ConfigDep, identifier: str, include: str = ""
) -> JSONResponse:
    job = await db.run(db.job, identifier, private=True)
    if job["status"] not in TERMINAL:
        raise ReelucidError("RESULT_NOT_READY", "任务尚未完成，已就绪产物可从任务详情单独下载。")
    result: JsonObject = job.get("result") or {
        "schema_version": "1.0",
        "job_id": identifier,
        "status": job["status"],
        "error": job["error"],
        "artifacts": job["artifacts"],
    }
    if include != "transcript" and "transcript" in result:
        result["transcript"] = {k: v for k, v in result["transcript"].items() if k != "segments"}
    elif (
        include == "transcript"
        and "transcript" in result
        and "segments" not in result["transcript"]
    ):
        artifact = next((a for a in result.get("artifacts", []) if a["kind"] == "transcript"), None)
        if artifact:
            stored = await db.run(db.artifact, artifact["artifact_id"])
            path = contained(runtime.data_dir / "artifacts", Path(stored["path"]))
            result["transcript"] = json.loads(
                await asyncio.to_thread(path.read_text, encoding="utf-8")
            )
    return JSONResponse(result)
