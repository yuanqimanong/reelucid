from __future__ import annotations

import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from reelucid.domain import ChunkPolicy
from reelucid.source_proxy import SourceProxy


class Provider(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    name: str = "AI Provider"
    protocol: Literal["openai_compatible"] = "openai_compatible"
    base_url: str = Field(max_length=2048)
    credential_ref: str | None = None
    timeout_seconds: int = Field(default=120, ge=5, le=600)

    @field_validator("base_url")
    @classmethod
    def valid_endpoint(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "Provider requires an HTTP(S) base URL without credentials, query or fragment"
            )
        _ = parsed.port
        return value.rstrip("/")


class ModelBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider_id: str
    model: str = Field(min_length=1, max_length=200)
    capabilities: list[Literal["text", "vision"]] = Field(default_factory=lambda: ["text"])


class ProcessingProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    model_id: str = "small"
    language: str = "auto"
    device: Literal["cpu", "cuda"] = "cpu"
    compute_type: str = "int8"
    glossary: str = Field(default="", max_length=2000)
    text_binding_id: str | None = None
    vision_binding_id: str | None = None
    allow_remote_ai: bool = False


class SourceResolver(BaseModel):
    """Administrator-owned resolver endpoint, separate from untrusted media URLs."""

    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    base_url: str = Field(default="", max_length=2048)
    credential_ref: str | None = None
    timeout_seconds: int = Field(default=120, ge=5, le=600)

    @field_validator("base_url")
    @classmethod
    def endpoint(cls, value: str) -> str:
        return Provider.valid_endpoint(value) if value else ""

    def _validate_location(self) -> None:
        if self.enabled and not self.base_url:
            raise ValueError("Source resolver requires a base_url when enabled")

    @model_validator(mode="after")
    def configured(self) -> SourceResolver:
        self._validate_location()
        return self


class DtkResolver(SourceResolver):
    download_session_bound: bool = Field(
        default=False,
        strict=True,
        description="允许 DTK 在其存储中下载绑定会话的视频，并由 Reelucid 取回。DTK Key 需具备 media:write 和 media:read 权限。",
    )


class XhsResolver(SourceResolver):
    # Preserve the original HTTP resolver for settings and jobs saved without a mode.
    mode: Literal["local", "remote"] = Field(
        default="remote", description="local 在容器内运行插件，remote 连接 base_url。"
    )

    def _validate_location(self) -> None:
        if self.mode == "remote":
            super()._validate_location()
        elif self.base_url or self.credential_ref is not None:
            raise ValueError("Local XHS does not use a resolver endpoint or API key")


class SystemSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: int = 1
    providers: list[Provider] = Field(default_factory=lambda: list[Provider]())
    model_bindings: dict[str, ModelBinding] = Field(default_factory=dict)
    processing_profiles: list[ProcessingProfile] = Field(
        default_factory=lambda: [ProcessingProfile(id="local-default")]
    )
    max_duration_seconds: int = Field(default=14400, ge=10, le=86400)
    max_upload_mb: int = Field(default=2048, ge=1, le=8192)
    queue_limit: int = Field(default=100, ge=1, le=1000)
    cpu_threads: int = Field(default=4, ge=1, le=32)
    adapters: dict[str, bool] = Field(
        default_factory=lambda: {
            "upload": True,
            "http_media": True,
            "yt_dlp": True,
            "bilibili": True,
            "dtk": True,
            "xhs": True,
        }
    )
    source_credentials: dict[str, str] = Field(default_factory=dict)
    source_resolver: DtkResolver = Field(default_factory=DtkResolver)
    xhs_resolver: XhsResolver = Field(default_factory=XhsResolver)

    @field_validator("adapters")
    @classmethod
    def upgrade_adapters(cls, value: dict[str, bool]) -> dict[str, bool]:
        return {"bilibili": True, "dtk": True, "xhs": True, **value}


class AppConfig:
    """Deployment paths and hard limits, independent of editable database settings."""

    def __init__(self, data_dir: Path | None = None) -> None:
        command = Path("/opt/reelucid-xhs/resolve")
        self.xhs_command: Path | None = (
            command if command.is_file() and os.access(command, os.X_OK) else None
        )
        self.data_dir = (data_dir or Path(os.environ.get("REELUCID_DATA_DIR", ".data"))).resolve()
        self.models_dir = Path(
            os.environ.get("REELUCID_MODELS_DIR", str(self.data_dir / "models"))
        ).resolve()
        self.max_bytes = int(os.environ.get("REELUCID_MAX_UPLOAD_BYTES", str(2 * 1024**3)))
        strategy = os.environ.get("REELUCID_ASR_CHUNK_STRATEGY", "auto")
        if strategy not in ("auto", "adjacent", "silence"):
            raise ValueError("REELUCID_ASR_CHUNK_STRATEGY must be auto, adjacent or silence")
        self.asr_chunk_policy: ChunkPolicy = strategy
        self.max_concurrent_jobs = int(os.environ.get("REELUCID_MAX_CONCURRENT_JOBS", "4"))
        if not 1 <= self.max_concurrent_jobs <= 16:
            raise ValueError("REELUCID_MAX_CONCURRENT_JOBS must be between 1 and 16")
        self.admin_token_file = Path(
            os.environ.get("REELUCID_ADMIN_TOKEN_FILE", str(self.data_dir / "secrets/admin_token"))
        )
        self.master_key_file = Path(
            os.environ.get("REELUCID_MASTER_KEY_FILE", str(self.data_dir / "secrets/master_key"))
        )
        self.offline = os.environ.get("REELUCID_OFFLINE", "false").lower() == "true"
        self.source_proxy = SourceProxy.configured(os.environ.get("REELUCID_SOURCE_PROXY", ""))
        # Administrator-controlled code extensions; never supplied by an API request.
        self.adapter_plugins = tuple(
            filter(None, os.environ.get("REELUCID_ADAPTER_PLUGINS", "").split(","))
        )
        self.web_dir = Path(
            os.environ.get("REELUCID_WEB_DIR", str(Path(__file__).parent / "static"))
        )
        if not self.web_dir.is_dir():
            self.web_dir = Path(__file__).resolve().parents[2] / "web/dist"

    def initial_settings(self) -> SystemSettings:
        if self.xhs_command is not None:
            return SystemSettings(xhs_resolver=XhsResolver(enabled=True, mode="local"))
        return SystemSettings()

    def prepare(self) -> None:
        import tempfile

        for name in ("db", "assets", "artifacts", "work", "work/tmp", "secrets"):
            (self.data_dir / name).mkdir(parents=True, exist_ok=True)
        self.models_dir.mkdir(parents=True, exist_ok=True)
        temporary = str(self.data_dir / "work/tmp")
        for key in ("TMPDIR", "TEMP", "TMP"):
            os.environ[key] = temporary
        tempfile.tempdir = temporary
