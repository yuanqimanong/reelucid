"""Administrator configuration, model installation, credentials and provider probes."""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, HTTPException, Response

from reelucid.ai import chat, local_endpoint
from reelucid.api.runtime import ConfigDep, DatabaseDep, SecurityDep
from reelucid.api.schemas import Credential
from reelucid.domain import JsonObject, ReelucidError
from reelucid.job_config import settings_value, snapshot
from reelucid.models import MODEL_IDS, catalog, model_path
from reelucid.settings import SystemSettings

router = APIRouter()


@router.get("/admin/system-config", tags=["Administration"])
async def get_config(db: DatabaseDep) -> JsonObject:
    value = await db.run(db.settings)
    return {
        "config": settings_value(value).model_dump(),
        "credential_ids": list(value.get("secrets", {})),
    }


@router.put("/admin/system-config", tags=["Administration"])
async def save_config(db: DatabaseDep, runtime: ConfigDep, value: SystemSettings) -> JsonObject:
    xhs = value.xhs_resolver
    if xhs.enabled and xhs.mode == "local" and runtime.xhs_command is None:
        previous = settings_value(await db.run(db.settings)).xhs_resolver
        if not previous.enabled or previous.mode != "local":
            raise HTTPException(422, "当前部署缺少小红书插件，请安装插件或选择外部服务。")
    ids = [p.id for p in value.processing_profiles]
    provider_ids = [p.id for p in value.providers]
    if len(set(ids)) != len(ids) or len(set(provider_ids)) != len(provider_ids) or not ids:
        raise HTTPException(422, "预设和 Provider 标识必须唯一，且至少保留一个预设。")
    for profile in value.processing_profiles:
        model_path(runtime.models_dir, profile.model_id)
        if profile.text_binding_id and profile.text_binding_id not in value.model_bindings:
            raise HTTPException(422, "文本模型绑定不存在。")
    for binding in value.model_bindings.values():
        if binding.provider_id not in provider_ids:
            raise HTTPException(422, "模型绑定引用的 Provider 不存在。")
    result = await db.run(db.save_settings, value.model_dump(), value.revision)
    return {"config": settings_value(result).model_dump()}


@router.get("/admin/asr-models", tags=["Administration"])
async def asr_models(runtime: ConfigDep) -> JsonObject:
    return {"items": await asyncio.to_thread(catalog, runtime.models_dir)}


@router.post("/admin/asr-models/{identifier}/install", status_code=202, tags=["Administration"])
async def install(db: DatabaseDep, identifier: str) -> JsonObject:
    if identifier not in MODEL_IDS:
        raise HTTPException(422, "未知模型。")
    current = await db.run(db.settings)
    return await db.run(
        db.create_job, {"model_id": identifier}, snapshot(current), None, kind="model"
    )


@router.put("/admin/credentials/{identifier}", tags=["Administration"])
async def credential(
    db: DatabaseDep, security: SecurityDep, identifier: str, value: Credential
) -> JsonObject:
    if not identifier.replace("-", "").replace("_", "").isalnum() or len(identifier) > 64:
        raise HTTPException(422, "凭据标识无效。")
    await db.run(db.put_secret, identifier, security.encrypt(value.value))
    return {"id": identifier, "configured": True}


@router.delete("/admin/credentials/{identifier}", status_code=204, tags=["Administration"])
async def delete_credential(db: DatabaseDep, identifier: str) -> Response:
    await db.run(db.put_secret, identifier, None)
    return Response(status_code=204)


@router.post("/admin/providers/{identifier}/test", tags=["Administration"])
async def test_provider(
    db: DatabaseDep, security: SecurityDep, runtime: ConfigDep, identifier: str
) -> JsonObject:
    raw = await db.run(db.settings)
    current = settings_value(raw)
    provider = next((p for p in current.providers if p.id == identifier), None)
    binding = next(
        (b for b in current.model_bindings.values() if b.provider_id == identifier), None
    )
    if provider is None or binding is None:
        raise HTTPException(422, "先保存 Provider 和至少一个模型绑定。")
    if runtime.offline and not local_endpoint(provider.base_url):
        raise ReelucidError("OFFLINE_MODE", "离线模式不允许测试远程 AI。")
    secret_value = (
        raw.get("secrets", {}).get(provider.credential_ref) if provider.credential_ref else None
    )
    secret = security.decrypt(secret_value) if secret_value else None
    started = time.monotonic()
    result = await chat(provider, binding, secret, [{"role": "user", "content": "Reply with OK."}])
    return {
        "ok": True,
        "model": binding.model,
        "elapsed_ms": round((time.monotonic() - started) * 1000),
        "reply": result[:100],
    }
