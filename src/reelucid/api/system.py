"""Public liveness probes and authenticated system capabilities."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse

from reelucid import __version__
from reelucid.api.runtime import ConfigDep, DatabaseDep, ServiceDep
from reelucid.domain import JsonObject
from reelucid.job_config import settings_value
from reelucid.models import catalog
from reelucid.source_catalogue import source_catalogue

health_router = APIRouter()
system_router = APIRouter()


@health_router.get("/health/live", tags=["Health"])
async def live() -> JsonObject:
    return {"status": "ok", "version": __version__}


@health_router.get("/health/ready", tags=["Health"])
async def ready(service: ServiceDep) -> Response:
    return JSONResponse({"ready": service.ready}, status_code=200 if service.ready else 503)


@system_router.get("/capabilities", tags=["System"])
async def capabilities(db: DatabaseDep, service: ServiceDep, runtime: ConfigDep) -> JsonObject:
    """xhs_resolver.local_available 表示本机已安装插件。configured 表示所选方式已配置，外部服务的连通性在处理任务时检查。"""
    current = settings_value(await db.run(db.settings))
    return {
        "version": __version__,
        "asr": await asyncio.to_thread(catalog, runtime.models_dir),
        "asr_chunk_policy": runtime.asr_chunk_policy,
        "scheduler": service.scheduling_status(),
        "adapters": current.adapters,
        "available_adapters": list(service.sources.adapters),
        "source_resolver": {
            "adapter_id": "dtk",
            "configured": current.source_resolver.enabled,
            "enabled": current.adapters.get("dtk", False),
            "download_session_bound": current.source_resolver.download_session_bound,
        },
        "xhs_resolver": {
            "adapter_id": "xhs",
            "mode": current.xhs_resolver.mode,
            "local_available": runtime.xhs_command is not None,
            "configured": current.xhs_resolver.enabled
            and (current.xhs_resolver.mode == "remote" or runtime.xhs_command is not None),
            "enabled": current.adapters.get("xhs", False),
        },
        "source_catalogue_url": "/api/v1/source-platforms",
        "source_proxy_enabled": runtime.source_proxy is not None,
        "operations": ["transcribe", "summarize", "translate", "extract", "qa"],
        "visual": False,
        "offline": runtime.offline,
        "max_upload_bytes": min(runtime.max_bytes, current.max_upload_mb * 1024**2),
    }


@system_router.get("/source-platforms", tags=["System"])
async def platforms() -> JsonObject:
    return await asyncio.to_thread(source_catalogue)
