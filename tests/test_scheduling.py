from dataclasses import replace

from reelucid.domain import JsonObject
from reelucid.resources import FALLBACK_MEMORY, ResourceSample
from reelucid.scheduling import MIB, Budget


def media_job(model: str = "small", *, device: str = "cpu") -> JsonObject:
    return {
        "kind": "media",
        "request": {},
        "snapshot": {
            "processing_profiles": [{"id": "local-default", "model_id": model, "device": device}]
        },
    }


def test_four_cores_split_three_threads_across_two_jobs() -> None:
    sample = ResourceSample(4, 6144 * MIB, 5600 * MIB, 0.1)
    budget = Budget(sample, 4)
    first = budget.allocate(media_job(), "asr")
    assert first and first.cpu_threads == 2
    second = replace(budget, active=(first,)).allocate(media_job(), "asr")
    assert second and second.cpu_threads == 1
    assert replace(budget, active=(first, second)).allocate(media_job(), "asr") is None
    assert budget.memory_reserve == sample.memory_limit // 5
    serial = Budget(sample, 1).allocate(media_job(), "asr")
    assert serial and serial.cpu_threads == 3


def test_missing_capacity_allows_one_default_job_with_one_thread() -> None:
    sample = ResourceSample(1, FALLBACK_MEMORY, FALLBACK_MEMORY, None, True, True)
    budget = Budget(sample, 4)
    first = budget.allocate(media_job(), "asr")
    assert first and first.cpu_threads == 1
    assert replace(budget, active=(first,)).allocate(media_job(), "asr") is None


def test_current_pressure_and_running_reservations_defer_new_jobs() -> None:
    sample = ResourceSample(4, 6144 * MIB, 5600 * MIB, 0.1)
    budget = Budget(sample, 4)
    first = budget.allocate(media_job(), "asr")
    assert first
    assert (
        replace(budget, sample=replace(sample, cpu_usage=0.8)).allocate(media_job(), "asr") is None
    )
    assert (
        replace(budget, sample=replace(sample, memory_available=2400 * MIB)).allocate(
            media_job(), "asr"
        )
        is None
    )
    with_job = replace(budget, active=(first,), sample=replace(sample, memory_available=3500 * MIB))
    assert with_job.allocate(media_job(), "asr") is None
    assert replace(with_job, active_resident=600 * MIB).allocate(media_job(), "asr") is not None
    assert (
        replace(budget, sample=replace(sample, cpu_usage=None), active=(first,)).allocate(
            media_job(), "asr"
        )
        is None
    )


def test_impossible_model_is_distinct_from_temporary_pressure() -> None:
    sample = ResourceSample(4, FALLBACK_MEMORY, FALLBACK_MEMORY, 0.1)
    oversized = Budget(sample, 4).allocate(media_job("large-v3"), "asr")
    assert oversized and oversized.impossible


def test_gpu_and_model_install_concurrency_is_bounded() -> None:
    budget = Budget(ResourceSample(8, 8192 * MIB, 8192 * MIB, 0.1), 4)
    gpu = budget.allocate(media_job(device="cuda"), "asr")
    assert gpu and gpu.workload.exclusive
    assert replace(budget, active=(gpu,)).allocate(media_job(), "asr") is None
    installer = budget.allocate({"kind": "model"}, "install")
    assert installer and installer.workload.phase == "install"
    with_install = replace(budget, active=(installer,))
    assert with_install.allocate(media_job(), "asr") is not None
    assert with_install.allocate({"kind": "model"}, "install") is None


def test_pinned_qa_needs_only_lightweight_resources_on_a_small_host() -> None:
    sample = ResourceSample(1, 1536 * MIB, 1400 * MIB, 0.1)
    job = media_job()
    budget = Budget(sample, 4)
    asr = budget.allocate(job, "asr")
    assert asr and asr.impossible
    qa = budget.allocate(job, "ai")
    assert qa and not qa.impossible and qa.cpu_threads == 0
    assert replace(budget, active=(qa,)).allocate(job, "ai") is None
    assert replace(budget, active=(qa,)).allocate(job, "download") is not None
