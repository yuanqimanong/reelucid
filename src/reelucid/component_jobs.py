"""Coordinate fixed host component installs. Only the host agent runs Docker."""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from threading import Lock
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import Field, TypeAdapter

from reelucid.domain import ReelucidError, Value
from reelucid.files import write_json

type ComponentId = Literal["dtk", "xhs"]
type ComponentState = Literal[
    "unknown", "not_installed", "stopped", "starting", "healthy", "unhealthy"
]
type InstallState = Literal["idle", "queued", "running", "succeeded", "failed"]
type AgentId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
type RequestId = Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]
type ErrorCode = Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")]

COMPONENTS: tuple[ComponentId, ...] = ("dtk", "xhs")
HEARTBEAT_TTL = 15


class ReportedStates(Value):
    dtk: ComponentState
    xhs: ComponentState


class Completion(Value):
    id: RequestId
    status: Literal["succeeded", "failed"]
    error_code: ErrorCode | None = None


class AgentPoll(Value):
    agent_id: AgentId
    components: ReportedStates
    completion: Completion | None = None


class InstallRequest(Value):
    id: RequestId
    component: ComponentId


class PollResult(Value):
    request: InstallRequest | None


class ComponentInfo(Value):
    id: ComponentId
    state: ComponentState
    install_state: InstallState
    error_code: ErrorCode | None


class ComponentInventory(Value):
    agent_connected: bool
    components: list[ComponentInfo]


class _Install(InstallRequest):
    agent_id: AgentId
    status: Literal["queued", "running", "succeeded", "failed"] = "queued"
    error_code: ErrorCode | None = None

    def public_request(self) -> InstallRequest:
        return InstallRequest(id=self.id, component=self.component)


_journal = TypeAdapter(list[_Install])


class ComponentJobs:
    """A single API writer persists transitions; ephemeral heartbeats never write to disk."""

    def __init__(self, path: Path, clock: Callable[[], float] = time.monotonic):
        self._path = path
        self._clock = clock
        self._lock = Lock()
        self._agent: str | None = None
        self._seen = float("-inf")
        self._states = ReportedStates(dtk="unknown", xhs="unknown")

    def _read(self) -> list[_Install]:
        try:
            return _journal.validate_json(self._path.read_bytes())
        except FileNotFoundError:
            return []

    def _write(self, jobs: list[_Install]) -> None:
        write_json(self._path, _journal.dump_python(jobs, mode="json"))

    def _connected(self) -> bool:
        return self._clock() - self._seen < HEARTBEAT_TTL

    def inventory(self) -> ComponentInventory:
        with self._lock:
            connected = self._connected()
            latest = {job.component: job for job in self._read()}
            components: list[ComponentInfo] = []
            for component in COMPONENTS:
                job = latest.get(component)
                components.append(
                    ComponentInfo(
                        id=component,
                        state=getattr(self._states, component) if connected else "unknown",
                        install_state=job.status if job else "idle",
                        error_code=job.error_code if job else None,
                    )
                )
            return ComponentInventory(agent_connected=connected, components=components)

    def enqueue(self, component: ComponentId) -> InstallRequest:
        with self._lock:
            if self._agent is None or not self._connected():
                raise ReelucidError("INVALID_TRANSITION", "宿主管理代理未连接。")
            jobs = self._read()
            for job in jobs:
                if job.component == component and job.status in ("queued", "running"):
                    return job.public_request()
            request = _Install(id=uuid4().hex, component=component, agent_id=self._agent)
            # Keep the last receipt per component even when an administrator immediately retries.
            terminal: dict[ComponentId, _Install] = {}
            pending: list[_Install] = []
            for job in jobs:
                if job.status in ("succeeded", "failed"):
                    terminal[job.component] = job
                else:
                    pending.append(job)
            self._write([*terminal.values(), *pending, request])
            return request.public_request()

    def poll(self, poll: AgentPoll) -> PollResult:
        with self._lock:
            if self._agent != poll.agent_id and self._connected():
                raise ReelucidError("INVALID_TRANSITION", "已有另一个宿主管理代理在线。")
            original = self._read()
            jobs = list(original)
            if poll.completion is not None:
                self._complete(jobs, poll.agent_id, poll.completion)
            # A different host must never automatically execute a previous host's work.
            for i, job in enumerate(jobs):
                if job.agent_id != poll.agent_id and job.status in ("queued", "running"):
                    jobs[i] = job.model_copy(
                        update={"status": "failed", "error_code": "AGENT_CHANGED"}
                    )
            request = self._claim(jobs)
            if jobs != original:
                self._write(jobs)
            self._agent, self._states, self._seen = poll.agent_id, poll.components, self._clock()
            return PollResult(request=request)

    @staticmethod
    def _complete(jobs: list[_Install], agent_id: str, completion: Completion) -> None:
        for i, job in enumerate(jobs):
            if job.id != completion.id or job.agent_id != agent_id:
                continue
            if job.status == completion.status and job.error_code == completion.error_code:
                return
            if job.status == "running":
                jobs[i] = job.model_copy(
                    update={"status": completion.status, "error_code": completion.error_code}
                )
                return
            break
        raise ReelucidError("INVALID_TRANSITION", "回执与代理当前领取的安装请求不符。")

    @staticmethod
    def _claim(jobs: list[_Install]) -> InstallRequest | None:
        for job in jobs:
            if job.status == "running":
                # The response may have been lost. Redeliver the same id to the same host.
                return job.public_request()
        for i, job in enumerate(jobs):
            if job.status == "queued":
                jobs[i] = job.model_copy(update={"status": "running"})
                return job.public_request()
        return None
