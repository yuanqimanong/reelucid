"""Administrator and host-agent interfaces for optional Docker components."""

from __future__ import annotations

from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials

from reelucid.api.auth import bearer
from reelucid.api.runtime import SecurityDep
from reelucid.component_jobs import (
    AgentPoll,
    ComponentId,
    ComponentInventory,
    ComponentJobs,
    InstallRequest,
    PollResult,
)
from reelucid.domain import Value

router = APIRouter(prefix="/admin/components", tags=["Administration"])
agent_router = APIRouter(prefix="/admin/components/agent", tags=["Administration"])


async def _coordinator(request: Request) -> ComponentJobs:
    return cast(ComponentJobs, request.app.state.component_jobs)


async def _admin_bearer(
    security: SecurityDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> None:
    if not credentials or not security.valid_token(credentials.credentials):
        raise HTTPException(
            401, "需要管理员 Bearer Token。", headers={"WWW-Authenticate": "Bearer"}
        )


async def _no_query(request: Request) -> None:
    if request.query_params:
        raise HTTPException(422, "此操作不接受查询参数。")


class InstallOptions(Value):
    """The component is selected exclusively by the path."""


ComponentJobsDep = Annotated[ComponentJobs, Depends(_coordinator)]


@router.get("")
def get_components(jobs: ComponentJobsDep) -> ComponentInventory:
    return jobs.inventory()


@router.post("/{component}/install", status_code=202, dependencies=[Depends(_no_query)])
def install_component(
    component: ComponentId, jobs: ComponentJobsDep, options: InstallOptions | None = None
) -> InstallRequest:
    return jobs.enqueue(component)


@agent_router.post("/poll", dependencies=[Depends(_admin_bearer), Depends(_no_query)])
def poll_components(body: AgentPoll, jobs: ComponentJobsDep) -> PollResult:
    return jobs.poll(body)
