"""Manage fixed Compose bundles on the operator's Docker host."""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import subprocess
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from uuid import uuid4

from filelock import FileLock, Timeout
from pydantic import BaseModel, ConfigDict, Field

from reelucid.component_jobs import COMPONENTS
from reelucid.component_jobs import ComponentId as Component
from reelucid.component_jobs import ComponentState as State
from reelucid.domain import JsonObject, ReelucidError
from reelucid.files import write_json

SERVICES = {
    "dtk": {"postgres", "redis", "api", "worker", "downloader", "migrate"},
    "xhs": {"xhs-resolver"},
}
VERSIONS = {"dtk": "5.1.13", "xhs": "2.8"}
ALIASES = {"dtk": "reelucid-managed-dtk", "xhs": "xhs-resolver"}


class Installation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    owner: str = Field(pattern=r"^[0-9a-f]{32}$")
    dtk_port: int = Field(ge=1, le=65535)
    secrets: dict[str, str]


def docker(*arguments: str, env: dict[str, str] | None = None, timeout: float = 30) -> str:
    try:
        result = subprocess.run(
            ["docker", *arguments],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
            env=env,
        )
    except OSError, subprocess.TimeoutExpired:
        raise ReelucidError(
            "COMPONENT_DOCKER_UNAVAILABLE", "无法连接 Docker 或命令已超时。"
        ) from None
    if result.returncode:
        # Compose and upstream container diagnostics can contain bootstrap tokens.
        raise ReelucidError("COMPONENT_DOCKER_ERROR", "Docker 命令失败，请在宿主检查服务状态。")
    return result.stdout.strip()


@dataclass(frozen=True)
class Manager:
    directory: Path
    core_project: str = "reelucid"
    dtk_port: int = 18080

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,40}", self.core_project):
            raise ReelucidError("COMPONENT_PROJECT_INVALID", "主 Compose 项目名格式错误。")
        if not 1 <= self.dtk_port <= 65535:
            raise ReelucidError("COMPONENT_PORT_INVALID", "DTK 端口须为 1 至 65535。")

    @property
    def root(self) -> Path:
        return self.directory / self.core_project

    @property
    def network(self) -> str:
        return f"{self.core_project}_default"

    def project(self, component: Component, state: Installation) -> str:
        return f"{self.core_project}-component-{component}-{state.owner[:12]}"

    def load(self, component: Component) -> Installation | None:
        path = self.root / f"{component}.json"
        try:
            value = Installation.model_validate_json(path.read_bytes())
            expected: set[str] = (
                {"SECRET", "PG", "REDIS", "DOWNLOADER"} if component == "dtk" else set()
            )
            if set(value.secrets) != expected or not all(
                re.fullmatch(r"[0-9a-f]{64}", s) for s in value.secrets.values()
            ):
                raise ValueError("invalid private configuration")
            return value
        except FileNotFoundError:
            return None
        except OSError, ValueError:
            raise ReelucidError(
                "COMPONENT_STATE_ERROR", "请检查或恢复该组件的宿主私有配置。"
            ) from None

    def _containers(self, component: Component, state: Installation) -> dict[str, str]:
        records = docker(
            "ps",
            "--all",
            "--filter",
            f"label=com.docker.compose.project={self.project(component, state)}",
            "--format",
            '{{.ID}}|{{.Label "com.docker.compose.service"}}|{{.Label "io.reelucid.owner"}}',
        )
        containers: dict[str, str] = {}
        for line in records.splitlines():
            identifier, service, owner = line.split("|")
            if owner != state.owner or service not in SERVICES[component]:
                raise ReelucidError(
                    "COMPONENT_CONFLICT", "同名 Docker 项目包含非本安装器管理的容器。"
                )
            containers[service] = identifier
        return containers

    def status(self, component: Component) -> State:
        state = self.load(component)
        if state is None:
            return "not_installed"
        containers = self._containers(component, state)
        if not containers:
            return "not_installed"
        rows = docker(
            "inspect",
            "--format",
            "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}|{{.State.ExitCode}}",
            *containers.values(),
        ).splitlines()
        statuses: set[str] = set()
        for service, line in zip(containers, rows, strict=True):
            run_state, health, exit_code = line.split("|")
            if service == "migrate" and run_state == "exited" and exit_code == "0":
                continue
            if run_state in {"dead", "exited", "paused"}:
                statuses.add("stopped" if exit_code == "0" else "unhealthy")
            elif run_state != "running" or health == "starting":
                statuses.add("starting")
            else:
                statuses.add("healthy" if health == "healthy" else "unhealthy")
        if "unhealthy" in statuses:
            return "unhealthy"
        if statuses == {"stopped"}:
            return "stopped"
        if set(containers) == SERVICES[component] and statuses == {"healthy"}:
            return "healthy"
        return "starting" if "stopped" not in statuses else "unhealthy"

    def states(self) -> dict[Component, State]:
        result: dict[Component, State] = {}
        for component in COMPONENTS:
            try:
                result[component] = self.status(component)
            except ReelucidError:
                result[component] = "unknown"
        return result

    def _check_network(self, component: Component, state: Installation) -> None:
        try:
            owner = docker(
                "network",
                "inspect",
                self.network,
                "--format",
                '{{index .Labels "com.docker.compose.project"}}',
            )
        except ReelucidError as exc:
            if exc.code == "COMPONENT_DOCKER_UNAVAILABLE":
                raise
            raise ReelucidError(
                "COMPONENT_NETWORK_MISSING", "请先启动主 Reelucid Compose 项目并确认项目名。"
            ) from None
        if owner != self.core_project:
            raise ReelucidError("COMPONENT_CONFLICT", "目标网络的 Compose 项目标签不匹配。")
        owned = set(self._containers(component, state).values())
        others = (
            set(
                docker(
                    "ps", "-a", "--filter", f"network={self.network}", "--format", "{{.ID}}"
                ).splitlines()
            )
            - owned
        )
        if others:
            fmt = '{{json (index .NetworkSettings.Networks "' + self.network + '").Aliases}}'
            for line in docker("inspect", "--format", fmt, *sorted(others)).splitlines():
                if ALIASES[component] in (json.loads(line) or []):
                    raise ReelucidError(
                        "COMPONENT_CONFLICT", "组件地址已由其他容器提供，请保留或手动配置该服务。"
                    )

    def install(self, component: Component) -> JsonObject:
        if component not in COMPONENTS:
            raise ReelucidError("COMPONENT_UNKNOWN", "未知组件。")
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            with FileLock(self.root / f"{component}.lock", timeout=1):
                return self._install(component)
        except Timeout:
            raise ReelucidError("COMPONENT_BUSY", "该组件已有宿主安装操作正在执行。") from None
        except OSError:
            raise ReelucidError("COMPONENT_STATE_ERROR", "无法读写宿主组件配置。") from None

    def _install(self, component: Component) -> JsonObject:
        state = self.load(component)
        fresh = state is None
        if state is None:
            keys = ("SECRET", "PG", "REDIS", "DOWNLOADER") if component == "dtk" else ()
            state = Installation(
                owner=uuid4().hex,
                dtk_port=self.dtk_port,
                secrets={k: secrets.token_hex(32) for k in keys},
            )
        self._check_network(component, state)
        if self.status(component) != "healthy":
            if component == "dtk" and "api" not in self._containers(component, state):
                with socket.socket() as probe:
                    try:
                        probe.bind(("127.0.0.1", state.dtk_port))
                    except OSError:
                        raise ReelucidError(
                            "COMPONENT_PORT_IN_USE", "DTK 宿主端口不可用，请检查已有监听服务。"
                        ) from None
            if fresh:
                write_json(self.root / f"{component}.json", state.model_dump())
            env = {
                k: v
                for k, v in os.environ.items()
                if not k.startswith(("COMPOSE_", "REELUCID_DTK_"))
            }
            env.update(
                COMPOSE_DISABLE_ENV_FILE="1",
                REELUCID_COMPONENT_PORT=str(state.dtk_port),
                REELUCID_COMPONENT_NETWORK=self.network,
                REELUCID_COMPONENT_OWNER=state.owner,
            )
            env.update({f"REELUCID_DTK_{k}": v for k, v in state.secrets.items()})
            bundle = resources.files("reelucid.component_manager") / f"{component}.yaml"
            with resources.as_file(bundle) as compose:
                try:
                    docker(
                        "compose",
                        "--project-directory",
                        str(self.root),
                        "-p",
                        self.project(component, state),
                        "-f",
                        str(compose),
                        "up",
                        "--detach",
                        "--no-build",
                        "--wait",
                        "--wait-timeout",
                        "240",
                        env=env,
                        timeout=1200,
                    )
                except ReelucidError:
                    raise ReelucidError(
                        "COMPONENT_INSTALL_FAILED",
                        "组件安装失败，请检查 Docker、镜像下载与组件健康状态。",
                    ) from None
            if self.status(component) != "healthy":
                raise ReelucidError(
                    "COMPONENT_INSTALL_FAILED", "组件尚未通过健康检查，可重复执行安装。"
                )
        result: JsonObject = {
            "component": component,
            "state": "healthy",
            "version": VERSIONS[component],
            "project": self.project(component, state),
            "network": self.network,
        }
        if component == "xhs":
            result.update(
                endpoint="http://xhs-resolver:5556",
                next_step="在小红书设置中选择外部服务并填入 endpoint。",
            )
        else:
            result.update(
                endpoint="http://reelucid-managed-dtk:8000",
                console=f"http://127.0.0.1:{state.dtk_port}",
                setup_log_command=f"docker logs {self.project(component, state)}-api-1",
                next_step="在宿主终端运行 setup_log_command 查看一次性初始化 Token，打开 console 创建 DTK 管理员、配置身份和 API Key，再在 Reelucid 后台设置 endpoint 与凭据。",
            )
        return result
