"""Authenticated upload, media playback and immutable artifact downloads."""

from __future__ import annotations

import errno
import mimetypes
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from reelucid.api.runtime import ConfigDep, DatabaseDep
from reelucid.api.uploads import store_upload
from reelucid.database import new_id
from reelucid.domain import JsonObject, ReelucidError
from reelucid.files import contained
from reelucid.job_config import settings_value

asset_router = APIRouter()
artifact_router = APIRouter()
file_responses: dict[int | str, JsonObject] = {
    200: {
        "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}
    },
    206: {"description": "Partial Content"},
}


@asset_router.post(
    "/assets",
    status_code=201,
    tags=["Assets"],
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["file"],
                        "properties": {"file": {"type": "string", "format": "binary"}},
                    }
                }
            },
        }
    },
)
async def upload(db: DatabaseDep, runtime: ConfigDep, request: Request) -> JsonObject:
    current = settings_value(await db.run(db.settings))
    if not current.adapters.get("upload", False):
        raise ReelucidError("ADAPTER_DISABLED", "文件上传已禁用。")
    limit = min(runtime.max_bytes, current.max_upload_mb * 1024**2)
    identifier = new_id("ast")
    directory = runtime.data_dir / "assets" / identifier
    try:
        result = await store_upload(request, db, directory, identifier, limit)
    except OSError as exc:
        if exc.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}:
            raise ReelucidError(
                "STORAGE_EXHAUSTED", "数据盘剩余空间不足。", retryable=True
            ) from exc
        raise
    return {
        "asset_id": identifier,
        "name": result.name,
        "status": "uploaded",
        "size_bytes": result.size,
    }


@asset_router.get("/assets/{identifier}", tags=["Assets"])
async def get_asset(db: DatabaseDep, identifier: str) -> JsonObject:
    value = await db.run(db.asset, identifier)
    return {k: v for k, v in value.items() if k not in {"source", "resources"}} | {
        "media_url": f"/api/v1/assets/{identifier}/media",
        "has_source": bool(value["resources"]),
    }


@asset_router.get(
    "/assets/{identifier}/media",
    tags=["Assets"],
    response_class=FileResponse,
    responses=file_responses,
)
async def media(db: DatabaseDep, runtime: ConfigDep, identifier: str) -> FileResponse:
    value = await db.run(db.asset, identifier)
    resource = value["resources"].get("source")
    if not resource:
        raise HTTPException(404, "原始媒体不可用。")
    path = contained(runtime.data_dir / "assets", Path(resource["path"]))
    if not path.is_file():
        raise HTTPException(404, "原始媒体文件已移除。")
    return FileResponse(
        path,
        media_type=mimetypes.guess_type(str(path))[0]
        or mimetypes.guess_type(resource.get("name", value["name"]))[0]
        or "application/octet-stream",
        headers={"Cache-Control": "private, no-store"},
    )


@artifact_router.get("/artifacts/{identifier}", tags=["Artifacts"])
async def artifact(db: DatabaseDep, identifier: str) -> JsonObject:
    value = await db.run(db.artifact, identifier)
    return {k: v for k, v in value.items() if k != "path"}


@artifact_router.get(
    "/artifacts/{identifier}/content",
    tags=["Artifacts"],
    response_class=FileResponse,
    responses=file_responses,
)
async def content(db: DatabaseDep, runtime: ConfigDep, identifier: str) -> FileResponse:
    value = await db.run(db.artifact, identifier)
    path = contained(runtime.data_dir / "artifacts", Path(value["path"]))
    if not path.is_file():
        raise HTTPException(404, "产物文件已移除，请检查数据卷。")
    return FileResponse(
        path,
        filename=f"{value['kind']}.{value['format']}",
        headers={"Cache-Control": "private, no-store"},
    )
