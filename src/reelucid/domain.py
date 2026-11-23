"""Validated media values and public result contracts; no I/O dependencies."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

type JsonObject = dict[str, Any]
type ChunkStrategy = Literal["adjacent", "silence"]
type ChunkPolicy = Literal["auto", "adjacent", "silence"]
CHUNK_POLICIES = ("auto", "adjacent", "silence")


class ReelucidError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable

    def public(self) -> JsonObject:
        return {"code": self.code, "message": self.message, "retryable": self.retryable}


class Cancelled(ReelucidError):
    def __init__(self) -> None:
        super().__init__("CANCELLED", "处理已取消。")


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    CANCELLED = "cancelled"
    SUCCEEDED = "succeeded"
    PARTIAL = "partially_succeeded"
    FAILED = "failed"


TERMINAL = frozenset(
    {JobStatus.CANCELLED, JobStatus.SUCCEEDED, JobStatus.PARTIAL, JobStatus.FAILED}
)
TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    JobStatus.QUEUED: frozenset({JobStatus.RUNNING, JobStatus.CANCELLED}),
    JobStatus.RUNNING: frozenset(
        {
            JobStatus.CANCELLING,
            JobStatus.SUCCEEDED,
            JobStatus.PARTIAL,
            JobStatus.FAILED,
            JobStatus.QUEUED,
        }
    ),
    JobStatus.CANCELLING: frozenset({JobStatus.CANCELLED}),
    JobStatus.CANCELLED: frozenset({JobStatus.QUEUED}),
    JobStatus.FAILED: frozenset({JobStatus.QUEUED}),
    JobStatus.PARTIAL: frozenset({JobStatus.QUEUED}),
    JobStatus.SUCCEEDED: frozenset(),
}


def check_transition(before: str, after: str) -> None:
    if JobStatus(after) not in TRANSITIONS[JobStatus(before)]:
        raise ReelucidError("INVALID_TRANSITION", f"不能从 {before} 转换到 {after}。")


class Value(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Word(Value):
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    text: str

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.end_ms < self.start_ms:
            raise ValueError("word end precedes start")
        return self


class Segment(Value):
    segment_id: str = ""
    start_ms: int = Field(ge=0)
    end_ms: int = Field(ge=0)
    text: str
    words: list[Word] = Field(default_factory=lambda: list[Word]())
    speaker_id: str | None = None
    timing_quality: Literal["estimated", "segment"] = "estimated"
    needs_review: bool = False

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.end_ms < self.start_ms:
            raise ValueError("segment end precedes start")
        return self


class Transcript(Value):
    version_id: str
    source: Literal["local_asr", "subtitle"] = "local_asr"
    time_unit: Literal["ms"] = "ms"
    timeline: Literal["source"] = "source"
    language: str = "unknown"
    duration_ms: int = 0
    segments: list[Segment] = Field(default_factory=lambda: list[Segment]())
    warnings: list[str] = Field(default_factory=list)


class SpeechRegion(Value):
    start: int = Field(ge=0)
    end: int = Field(ge=0)


class Chunk(Value):
    index: int
    core_start: int
    core_end: int
    context_start: int
    context_end: int
    cut_kind: Literal["silence", "hard"]


class MediaInfo(Value):
    duration_ms: int
    has_audio: bool
    has_video: bool
    format_name: str
    source_origin_seconds: float = 0.0
    audio_start_seconds: float = 0.0


class PipelineOptions(Value):
    model_path: str
    model_id: str = "small"
    device: Literal["cpu", "cuda"] = "cpu"
    compute_type: str = "int8"
    language: str = "auto"
    glossary: str = Field(default="", max_length=2000)
    chunk_seconds: int = Field(default=30, ge=5, le=30)
    chunk_strategy: ChunkStrategy = "silence"
    context_prompt: bool = True
    vad: bool = True
    vad_model_path: str | None = None
    cpu_threads: int = Field(default=4, ge=1, le=32)
    max_duration_seconds: int = 14400
    formats: list[Literal["json", "srt", "vtt", "txt", "md"]] = Field(
        default_factory=lambda: ["json", "srt", "vtt", "txt"]
    )


def choose_chunk_strategy(policy: ChunkPolicy, language: str, model_id: str) -> ChunkStrategy:
    if policy != "auto":
        return policy
    return "adjacent" if language == "en" and model_id == "small" else "silence"


def make_chunks(
    regions: list[SpeechRegion],
    total: int,
    seconds: int = 30,
    rate: int = 16000,
    *,
    strategy: ChunkStrategy = "silence",
) -> list[Chunk]:
    """Only hard cuts borrow acoustic context; all core intervals are half open."""
    if strategy == "adjacent":
        regions = _group_speech(regions, min(seconds, 20) * rate, 6 * rate // 5)
    chunks: list[Chunk] = []
    for region in regions:
        start = max(0, region.start)
        end = min(total, region.end)
        while start < end:
            stop = min(end, start + seconds * rate)
            left = start > region.start
            right = stop < end
            chunks.append(
                Chunk(
                    index=len(chunks),
                    core_start=start,
                    core_end=stop,
                    context_start=max(0, start - rate // 2) if left else start,
                    context_end=min(total, stop + rate // 2) if right else stop,
                    cut_kind="hard" if left or right else "silence",
                )
            )
            start = stop
    return chunks


def _group_speech(regions: list[SpeechRegion], window: int, gap: int) -> list[SpeechRegion]:
    # Keep the silence inside the original PCM window so timestamps stay on the source timeline.
    grouped: list[SpeechRegion] = []
    for region in regions:
        previous = grouped[-1] if grouped else None
        if (
            previous
            and region.start - previous.end <= gap
            and max(previous.end, region.end) - previous.start <= window
        ):
            grouped[-1] = SpeechRegion(start=previous.start, end=max(previous.end, region.end))
        else:
            grouped.append(region)
    return grouped


def own_segments(parts: list[Segment], chunk: Chunk, rate: int = 16000) -> list[Segment]:
    offset = chunk.context_start * 1000 // rate
    low, high = chunk.core_start * 1000 // rate, chunk.core_end * 1000 // rate
    output: list[Segment] = []
    for part in parts:
        words = [
            w.model_copy(update={"start_ms": w.start_ms + offset, "end_ms": w.end_ms + offset})
            for w in part.words
        ]
        owned = [w for w in words if low <= (w.start_ms + w.end_ms) / 2 < high]
        if words:
            if owned:
                output.append(
                    Segment(
                        start_ms=max(low, owned[0].start_ms),
                        end_ms=min(high, owned[-1].end_ms),
                        text="".join(w.text for w in owned).strip(),
                        words=owned,
                        needs_review=part.needs_review,
                    )
                )
        elif low <= (part.start_ms + part.end_ms) / 2 + offset < high:
            output.append(
                part.model_copy(
                    update={
                        "start_ms": max(low, part.start_ms + offset),
                        "end_ms": min(high, part.end_ms + offset),
                        "timing_quality": "segment",
                        "needs_review": True,
                    }
                )
            )
    return output
