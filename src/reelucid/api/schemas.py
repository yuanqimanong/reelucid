"""Validated HTTP request bodies shared by the API endpoints."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from reelucid.sources import normalize_source_url


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UrlSource(Strict):
    kind: Literal["url"]
    url: str = Field(max_length=8192)
    adapter_id: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    credential_profile_id: str | None = None

    @field_validator("url")
    @classmethod
    def share_url(cls, value: str) -> str:
        return normalize_source_url(value)


class AssetSource(Strict):
    kind: Literal["asset"]
    asset_id: str
    transcript_artifact_id: str | None = Field(
        default=None,
        min_length=1,
        max_length=128,
        exclude_if=lambda value: value is None,
        description="从此素材已发布的指定文稿生成结果，仅适用于不含 transcribe 的任务。",
    )


class OperationOptions(Strict):
    instructions: str = Field(default="", max_length=4000)
    output_language: str = Field(default="zh", max_length=32)
    question: str = Field(default="", max_length=2000)
    use_visual: bool = False
    max_frames: int = Field(default=60, ge=1, le=120)
    focus: list[str] = Field(default_factory=list, max_length=10)


class Operation(Strict):
    id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    type: Literal["transcribe", "summarize", "translate", "extract", "qa", "visual_analyze"]
    required: bool = True
    options: OperationOptions = Field(default_factory=OperationOptions)


class JobOptions(Strict):
    language: str = Field(default="auto", max_length=32)
    transcript_mode: Literal["local_asr"] = "local_asr"
    keep_source: bool = True
    export_formats: list[Literal["json", "srt", "vtt", "txt", "md"]] = Field(
        default_factory=lambda: ["json", "srt", "vtt", "txt"], min_length=1, max_length=5
    )


class JobRequest(Strict):
    source: Annotated[UrlSource | AssetSource, Field(discriminator="kind")]
    processing_profile_id: str = "local-default"
    operations: list[Operation] = Field(
        default_factory=lambda: [Operation(id="transcript", type="transcribe")],
        min_length=1,
        max_length=8,
    )
    options: JobOptions = Field(default_factory=JobOptions)

    @model_validator(mode="after")
    def unique_operations(self) -> Self:
        if not self.options.keep_source:
            raise ValueError("source retention is required for playback")
        if len({op.id for op in self.operations}) != len(self.operations):
            raise ValueError("operation ids must be unique")
        for op in self.operations:
            if (
                op.type == "transcribe"
                and isinstance(self.source, AssetSource)
                and self.source.transcript_artifact_id is not None
            ):
                raise ValueError("transcribe cannot use an existing transcript_artifact_id")
            if op.type == "qa" and not op.options.question.strip():
                raise ValueError("qa requires a question")
        return self


class Login(Strict):
    token: str = Field(max_length=512)


class Credential(Strict):
    value: str = Field(min_length=1, max_length=65536)
