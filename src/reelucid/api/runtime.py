"""Own the single API writer and worker lifecycle; inject resources per app instance."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Annotated, cast

from fastapi import Depends, FastAPI, Request
from filelock import FileLock, Timeout

from reelucid.database import Database
from reelucid.security import Security
from reelucid.service import Service
from reelucid.settings import AppConfig


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    config = cast(AppConfig, app.state.config)
    config.prepare()
    lock = FileLock(str(config.data_dir / "db/api.lock"))
    try:
        lock.acquire(timeout=0)
    except Timeout as exc:
        raise RuntimeError("该数据目录已有 API 实例运行；请勿启动多个 Uvicorn worker。") from exc
    async with AsyncExitStack() as resources:
        resources.callback(lock.release)
        database = Database(config.data_dir / "db/reelucid.db")
        resources.push_async_callback(asyncio.to_thread, database.close)
        security = Security(config)
        await database.run(database.initialize, config.initial_settings())
        service = Service(config, database, security)
        resources.push_async_callback(service.stop)
        app.state.db, app.state.security, app.state.service = database, security, service
        service.start()
        yield


async def database_for(request: Request) -> Database:
    return cast(Database, request.app.state.db)


async def security_for(request: Request) -> Security:
    return cast(Security, request.app.state.security)


async def service_for(request: Request) -> Service:
    return cast(Service, request.app.state.service)


async def config_for(request: Request) -> AppConfig:
    return cast(AppConfig, request.app.state.config)


DatabaseDep = Annotated[Database, Depends(database_for)]
SecurityDep = Annotated[Security, Depends(security_for)]
ServiceDep = Annotated[Service, Depends(service_for)]
ConfigDep = Annotated[AppConfig, Depends(config_for)]
