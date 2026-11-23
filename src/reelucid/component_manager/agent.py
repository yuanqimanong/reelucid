"""Poll the local Reelucid admin API; only component IDs cross the install boundary."""

from __future__ import annotations

import ipaddress
import re
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import uuid4

import httpx2
from pydantic import ValidationError

from reelucid.component_jobs import ComponentId, ComponentState, InstallRequest, PollResult
from reelucid.component_manager.install import Manager, docker
from reelucid.domain import JsonObject, ReelucidError

_INSTALL_ERRORS = {
    "COMPONENT_BUSY",
    "COMPONENT_CONFLICT",
    "COMPONENT_DOCKER_ERROR",
    "COMPONENT_DOCKER_UNAVAILABLE",
    "COMPONENT_INSTALL_FAILED",
    "COMPONENT_NETWORK_MISSING",
    "COMPONENT_PORT_IN_USE",
    "COMPONENT_STATE_ERROR",
}


def local_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if not host or parsed.scheme not in {"http", "https"} or parsed.path not in {"", "/"}:
            raise ValueError
        if (
            parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
        addr = ipaddress.ip_address("127.0.0.1" if host == "localhost" else host)
        if not addr.is_loopback or "%" in host:
            raise ValueError
        authority = f"[{addr}]" if addr.version == 6 else str(addr)
        if (port := parsed.port) is not None:
            authority += f":{port}"
        return f"{parsed.scheme}://{authority}"
    except ValueError:
        raise ReelucidError(
            "COMPONENT_AGENT_ENDPOINT_INVALID", "Agent 只接受本机回环 API 根地址。"
        ) from None


def admin_token(path: Path | None, project: str) -> str:
    if path is None:
        ids = docker(
            "ps",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--filter",
            "label=com.docker.compose.service=reelucid",
            "--format",
            "{{.ID}}",
        ).splitlines()
        if len(ids) != 1:
            raise ReelucidError(
                "COMPONENT_AGENT_TOKEN_MISSING", "无法定位唯一主容器，请使用 --token-file。"
            )
        token = docker("exec", ids[0], "cat", "/data/secrets/admin_token")
    else:
        try:
            with path.open(encoding="utf-8") as handle:
                token = handle.read(1024).strip()
        except OSError, UnicodeError:
            raise ReelucidError(
                "COMPONENT_AGENT_TOKEN_MISSING", "无法读取管理员 Token 文件。"
            ) from None
    if not re.fullmatch(r"[!-~]{1,512}", token):
        raise ReelucidError("COMPONENT_AGENT_TOKEN_INVALID", "管理员 Token 文件格式错误。")
    return token


class Agent:
    def __init__(self, manager: Manager, client: httpx2.Client, executor: ThreadPoolExecutor):
        self.manager = manager
        self.client = client
        self.executor = executor
        self.agent_id = uuid4().hex
        self.active: InstallRequest | None = None
        self.future: Future[JsonObject] | None = None
        self.state_time = -float("inf")
        self.states: dict[ComponentId, ComponentState] = {"dtk": "unknown", "xhs": "unknown"}
        self.state_future: Future[dict[ComponentId, ComponentState]] | None = None

    def poll(self) -> None:
        now = time.monotonic()
        if self.state_future is not None and self.state_future.done():
            self.states = self.state_future.result()
            self.state_future = None
            self.state_time = now
        if self.state_future is None and now - self.state_time >= 8:
            # A slow Docker daemon must not hold up an active installation's lease heartbeat.
            self.state_future = self.executor.submit(self.manager.states)
        payload: JsonObject = {"agent_id": self.agent_id, "components": self.states}
        if self.future is not None and self.future.done() and self.active is not None:
            status: Literal["succeeded", "failed"] = "succeeded"
            code: str | None = None
            try:
                self.future.result()
            except ReelucidError as exc:
                status = "failed"
                code = exc.code if exc.code in _INSTALL_ERRORS else "COMPONENT_INSTALL_FAILED"
            except Exception:
                status, code = "failed", "COMPONENT_INSTALL_FAILED"
            else:
                self.states[self.active.component] = "healthy"
            payload["completion"] = {"id": self.active.id, "status": status, "error_code": code}
        try:
            response = self.client.post("/api/v1/admin/components/agent/poll", json=payload)
            if response.status_code in {401, 403}:
                raise ReelucidError(
                    "COMPONENT_AGENT_UNAUTHORIZED", "Agent 凭据已失效，请检查管理员 Token。"
                )
            response.raise_for_status()
            requested = PollResult.model_validate_json(response.content).request
        except httpx2.HTTPError, ValidationError:
            raise ReelucidError(
                "COMPONENT_AGENT_POLL_FAILED",
                "未收到本机 API 的有效确认，稍后重试。",
                retryable=True,
            ) from None
        if self.active is not None:
            if "completion" not in payload or (
                requested is not None and requested.id == self.active.id
            ):
                return
            self.active, self.future = None, None
            self.state_time = -float("inf")
        if requested is not None:
            self.active = requested
            self.future = self.executor.submit(self.manager.install, requested.component)


def serve(manager: Manager, api_url: str, token_file: Path | None) -> None:
    url = local_url(api_url)
    token = admin_token(token_file, manager.core_project)
    with (
        httpx2.Client(
            base_url=url,
            headers={"Authorization": f"Bearer {token}"},
            trust_env=False,
            follow_redirects=False,
            timeout=20,
        ) as client,
        ThreadPoolExecutor(max_workers=2, thread_name_prefix="reelucid-components") as executor,
    ):
        agent = Agent(manager, client, executor)
        previous: str | None = None
        print(f"宿主组件 Agent 正在轮询 {url}（Ctrl+C 结束）。", flush=True)
        while True:
            try:
                agent.poll()
            except ReelucidError as exc:
                if not exc.retryable:
                    raise
                if previous != exc.code:
                    print(exc.message, flush=True)
                previous = exc.code
            else:
                previous = None
            time.sleep(2.5)
