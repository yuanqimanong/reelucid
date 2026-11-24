"""Conservative admission for jobs that share one host and SQLite writer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from reelucid.domain import JsonObject
from reelucid.resources import ResourceSample

MIB = 1024**2
type Phase = Literal["download", "asr", "transcode", "ai", "install"]
MODEL_MEMORY_MIB = {
    "tiny": 640,
    "base": 768,
    "small": 1280,
    "medium": 2304,
    "large-v3": 4096,
    "large-v3-turbo": 3072,
}


@dataclass(frozen=True, slots=True)
class Workload:
    memory: int
    phase: Phase
    exclusive: bool = False

    @classmethod
    def for_stage(cls, job: JsonObject, phase: Phase) -> Workload:
        if phase == "install":
            return cls(512 * MIB, phase)
        if phase == "transcode":
            presets = [
                operation.get("options", {}).get("preset")
                for operation in job["request"].get("operations", [])
                if operation["type"] == "transcode"
            ]
            audio_only = bool(presets) and all(p in {"audio_mp3", "audio_wav"} for p in presets)
            return cls((256 if audio_only else 768) * MIB, phase)
        if phase in {"download", "ai"}:
            return cls(128 * MIB, phase)
        request, settings = job["request"], job["snapshot"]
        requested = request.get("processing_profile_id", "local-default")
        profiles: list[JsonObject] = settings.get("processing_profiles", [])
        profile = next((item for item in profiles if item["id"] == requested), None)
        if profile is None:
            return cls(MODEL_MEMORY_MIB["small"] * MIB, phase)
        gpu = profile.get("device") == "cuda"
        memory = MODEL_MEMORY_MIB.get(profile.get("model_id", "small"), 1280)
        if not gpu and profile.get("compute_type") in {"float32", "auto", "default"}:
            memory *= 2
        # CUDA takes an exclusive compute slot because the sampler only measures system RAM.
        return cls(memory * MIB, phase, exclusive=gpu)


@dataclass(frozen=True, slots=True)
class Allocation:
    workload: Workload
    cpu_threads: int
    impossible: bool = False


@dataclass(frozen=True, slots=True)
class Budget:
    sample: ResourceSample
    max_jobs: int
    active: tuple[Allocation, ...] = ()
    active_resident: int = 0

    @property
    def cpu_threads(self) -> int:
        return max(1, int(self.sample.cpu_cores * 0.75))

    @property
    def slots(self) -> int:
        return min(self.max_jobs, (self.cpu_threads + 1) // 2)

    @property
    def memory_reserve(self) -> int:
        return max(384 * MIB, self.sample.memory_limit // 5)

    @property
    def ready(self) -> bool:
        load = self.sample.cpu_usage
        if load is None:
            return not self.active
        return load < 0.80 and len(self.active) < self.max_jobs

    def stage_limit(self, phase: Phase) -> int:
        if phase == "install":
            return 1
        if phase in {"asr", "transcode"}:
            return self.slots
        return min(2, self.slots)

    def allocate(self, job: JsonObject, phase: Phase) -> Allocation | None:
        if not self.ready:
            return None
        workload = Workload.for_stage(job, phase)
        capacity = self.sample.memory_limit - self.memory_reserve
        if workload.memory > capacity:
            return Allocation(workload, 1, impossible=True)
        if sum(item.workload.phase == phase for item in self.active) >= self.stage_limit(phase):
            return None
        compute = [item for item in self.active if item.cpu_threads]
        if phase in {"asr", "install", "transcode"} and (
            len(compute) >= self.slots
            or (workload.exclusive and compute)
            or any(item.workload.exclusive for item in compute)
        ):
            return None
        # Running jobs may still be loading their models. Reserve their remaining
        # allocations and count private pages seen by the sampler only once.
        reserved = sum(item.workload.memory for item in self.active)
        if workload.memory + reserved > capacity:
            return None
        pending = max(0, reserved - self.active_resident)
        if workload.memory + pending > self.sample.memory_available - self.memory_reserve:
            return None
        if phase in {"download", "ai"}:
            return Allocation(workload, 0)
        free_threads = self.cpu_threads - sum(item.cpu_threads for item in compute)
        base, remainder = divmod(self.cpu_threads, self.slots)
        per_job = base + int(len(compute) < remainder)
        threads = min(free_threads, per_job, 32)
        return Allocation(workload, threads) if threads > 0 else None


def first_phase(job: JsonObject) -> Phase:
    """Classify queued work for admission. The runner reserves each actual stage separately."""
    if job["kind"] == "model":
        return "install"
    request = job["request"]
    options = request.get("options", {})
    operations = request.get("operations", [])
    if operations and all(op["type"] == "transcode" for op in operations):
        return "transcode" if job["has_local_source"] else "download"
    legacy_reuse = (
        "transcript_artifact_id" not in options
        and request.get("source", {}).get("kind") == "asset"
        and not any(op["type"] == "transcribe" for op in operations)
    )
    if options.get("transcript_artifact_id") or legacy_reuse:
        return "ai"
    return "asr" if job["has_local_source"] else "download"
