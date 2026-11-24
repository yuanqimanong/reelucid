"""Schedule persisted jobs and supervise their isolated compute processes."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from functools import partial
from uuid import uuid4

from reelucid.cancellation import JobCancellation
from reelucid.database import Database
from reelucid.domain import Cancelled, JsonObject, ReelucidError
from reelucid.events import EventBroker
from reelucid.media_jobs import MediaJobRunner
from reelucid.models import prepare_model
from reelucid.resources import ResourceMonitor, ResourceSample, private_memory
from reelucid.scheduling import MIB, MODEL_MEMORY_MIB, Allocation, Budget, Phase, first_phase
from reelucid.security import Security
from reelucid.settings import AppConfig
from reelucid.sources import SourceRegistry
from reelucid.worker import ComputeWorker

log = logging.getLogger(__name__)


@dataclass(slots=True)
class Execution:
    asset_id: str | None
    lease: str
    phase: Phase | None
    cancelled: JobCancellation
    allocation: Allocation | None = None
    worker: ComputeWorker | None = None

    def cancel(self) -> None:
        self.cancelled.set()
        if self.worker is not None:
            self.worker.cancelled.set()


class Service:
    def __init__(self, config: AppConfig, database: Database, security: Security) -> None:
        self.config = config
        self.db = database
        self.broker = EventBroker(database)
        self.sources = SourceRegistry(plugins=config.adapter_plugins, proxy=config.source_proxy)
        self.media = MediaJobRunner(config, database, security, self.sources)
        self.monitor = ResourceMonitor()
        self.sample: ResourceSample | None = None
        self.worker_memory: dict[str, int] = {}
        self.running: dict[str, tuple[Execution, asyncio.Task[None]]] = {}
        self.idle_worker: ComputeWorker | None = None
        self.idle_since = 0.0
        self.closing = False
        self.last_write: dict[str, float] = {}
        self.tasks: list[asyncio.Task[None]] = []
        self.capacity_changed = asyncio.Event()

    def start(self) -> None:
        self.tasks = [
            asyncio.create_task(self.broker.run(), name="event-fanout"),
            asyncio.create_task(self.run(), name="job-scheduler"),
        ]

    async def stop(self) -> None:
        self.closing = self.broker.closing = True
        self.capacity_changed.set()
        for execution, _ in self.running.values():
            execution.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        running = [task for _, task in self.running.values()]
        if running:
            _, pending = await asyncio.wait(running, timeout=8)
            for task in pending:
                task.cancel()
            await asyncio.gather(*running, return_exceptions=True)
        workers = [execution.worker for execution, _ in self.running.values()]
        await asyncio.gather(
            *(asyncio.to_thread(worker.stop) for worker in [self.idle_worker, *workers] if worker)
        )
        self.running.clear()
        self.idle_worker = None

    def cancel(self, job_id: str) -> None:
        if running := self.running.get(job_id):
            running[0].cancel()
            self.capacity_changed.set()

    @property
    def ready(self) -> bool:
        return not self.closing and bool(self.tasks) and all(not task.done() for task in self.tasks)

    def budget(self, sample: ResourceSample) -> Budget:
        executions = [execution for execution, _ in self.running.values()]
        return Budget(
            sample,
            self.config.max_concurrent_jobs,
            tuple(e.allocation for e in executions if e.allocation),
            sum(
                min(
                    execution.allocation.workload.memory, self.worker_memory.get(execution.lease, 0)
                )
                for execution in executions
                if execution.allocation and execution.worker
            ),
        )

    def scheduling_status(self) -> JsonObject:
        if self.sample is None:
            return {"state": "sampling"}
        sample = self.sample
        budget = self.budget(sample)
        return {
            "cpu_cores": sample.cpu_cores,
            "cpu_usage": sample.cpu_usage,
            "cpu_thread_budget": budget.cpu_threads,
            "cpu_fallback": sample.cpu_fallback,
            "memory_limit_mib": sample.memory_limit // MIB,
            "memory_available_mib": sample.memory_available // MIB,
            "memory_reserved_mib": budget.memory_reserve // MIB,
            "memory_fallback": sample.memory_fallback,
            "cpu_job_slots": budget.slots,
            "active_jobs": len(self.running),
            "active_stages": dict(Counter(a.workload.phase for a in budget.active)),
            "download_slots": budget.stage_limit("download"),
            "transcode_slots": budget.stage_limit("transcode"),
            "ai_slots": budget.stage_limit("ai"),
        }

    async def run(self) -> None:
        sampled_at = -float("inf")
        while not self.closing:
            self._reap_jobs()
            if self.sample is None or time.monotonic() - sampled_at >= 1:
                await self._sample_resources()
                sampled_at = time.monotonic()
            if self.closing:
                return
            sample = self.sample
            assert sample is not None
            budget = self.budget(sample)
            if self.idle_worker and (
                not self.idle_worker.process.is_alive()
                or time.monotonic() - self.idle_since >= 30
                or sample.memory_available < budget.memory_reserve + MODEL_MEMORY_MIB["small"] * MIB
            ):
                worker, self.idle_worker = self.idle_worker, None
                await asyncio.to_thread(worker.stop)
                sampled_at = -float("inf")
                continue
            workflow_limit = self.config.max_concurrent_jobs if sample.cpu_usage is not None else 1
            if len(self.running) < workflow_limit and budget.ready and await self._dispatch(budget):
                continue
            await asyncio.sleep(0.25)

    async def _sample_resources(self) -> None:
        workers: dict[str, int] = {}
        for execution, _ in self.running.values():
            worker = execution.worker
            if worker is not None and worker.process is not None:
                pid = worker.process.pid
                if pid is not None:
                    workers[execution.lease] = pid

        def measure() -> tuple[ResourceSample, dict[str, int]]:
            resident = {lease: private_memory(pid) for lease, pid in workers.items()}
            return self.monitor.read(), resident

        self.sample, self.worker_memory = await asyncio.to_thread(measure)
        self.capacity_changed.set()

    async def _dispatch(self, budget: Budget) -> bool:
        assets = tuple(
            execution.asset_id for execution, _ in self.running.values() if execution.asset_id
        )

        phases = Counter(e.phase for e, _ in self.running.values())

        def eligible(candidate: JsonObject) -> bool:
            phase = first_phase(candidate)
            return (
                phases[phase] < budget.stage_limit(phase)
                and budget.allocate(candidate, phase) is not None
            )

        job = await self.db.run(
            self.db.claim,
            uuid4().hex,
            active_jobs=tuple(self.running),
            active_assets=assets,
            eligible=eligible,
        )
        if job is None or self.closing:
            # On shutdown the persisted lease is recovered by Database.initialize.
            return False
        execution = Execution(job["asset_id"], job["lease"], first_phase(job), JobCancellation())
        self.running[job["job_id"]] = (
            execution,
            asyncio.create_task(self._run_claim(job, execution), name=f"job-{job['job_id']}"),
        )
        return True

    def _reap_jobs(self) -> None:
        for job_id, (_, task) in list(self.running.items()):
            if not task.done():
                continue
            del self.running[job_id]
            try:
                task.result()
            except Exception as exc:
                log.error("job_cleanup_failed job=%s exception=%s", job_id, type(exc).__name__)

    @asynccontextmanager
    async def _stage(
        self, job: JsonObject, execution: Execution, phase: Phase
    ) -> AsyncGenerator[tuple[ComputeWorker | None, int]]:
        execution.phase = phase
        announced = False
        while True:
            self.capacity_changed.clear()
            execution.cancelled.check()
            sample = self.sample
            allocation = self.budget(sample).allocate(job, phase) if sample else None
            if allocation:
                break
            if not announced:
                await self.db.run(
                    self.db.stage, job["job_id"], execution.lease, f"wait_{phase}", {}
                )
                announced = True
                continue
            await self.capacity_changed.wait()
        if allocation.impossible:
            raise ReelucidError(
                "RESOURCE_LIMIT",
                "当前内存额度不足以运行此阶段，请调整容器内存或模型后重试。",
                retryable=True,
            )
        execution.allocation = allocation
        worker = None
        completed = False
        try:
            if phase == "asr":
                worker, self.idle_worker = self.idle_worker or ComputeWorker(), None
                execution.worker = worker
                worker.cancelled.clear()
                await self.db.run(
                    self.db.bind_worker, job["job_id"], execution.lease, worker.generation
                )
                execution.cancelled.check()
                if worker.process is None:
                    worker.start()
            yield worker, allocation.cpu_threads
            completed = True
        finally:
            try:
                if worker:
                    if (
                        completed
                        and not self.closing
                        and self.idle_worker is None
                        and worker.process
                        and worker.process.is_alive()
                    ):
                        self.idle_worker, self.idle_since = worker, time.monotonic()
                    else:
                        await asyncio.to_thread(worker.stop)
            finally:
                execution.phase = execution.allocation = execution.worker = None
                if worker:
                    # RSS and model ownership can change when a native process becomes idle.
                    self.sample = None
                self.capacity_changed.set()

    async def _run_claim(self, job: JsonObject, execution: Execution) -> None:
        job_id, lease = job["job_id"], job["lease"]
        heartbeat = asyncio.create_task(self.heartbeat(job_id, lease), name=f"lease-{job_id}")
        try:
            if execution.cancelled.is_set():
                raise Cancelled()
            if job["kind"] == "model":
                async with self._stage(job, execution, "install"):
                    await self._install_model(job, execution.cancelled)
            else:
                await self.media.run(
                    job,
                    partial(self._stage, job, execution),
                    execution.cancelled,
                    partial(self.progress, job_id, lease, execution.cancelled),
                )
        except ReelucidError as exc:
            if not self.closing:
                log.warning("job_failed job=%s code=%s", job_id, exc.code)
                await self.db.run(self.db.fail, job_id, lease, exc.public())
        except asyncio.CancelledError:
            execution.cancel()
            raise
        except Exception as exc:
            # Provider responses and signed URLs can appear in exception messages.
            log.error("unexpected_job_failure job=%s exception=%s", job_id, type(exc).__name__)
            if not self.closing:
                await self.db.run(
                    self.db.fail,
                    job_id,
                    lease,
                    {
                        "code": "INTERNAL_ERROR",
                        "message": "处理失败，请查看服务端诊断。",
                        "retryable": True,
                    },
                )
        finally:
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            self.last_write.pop(job_id, None)
            self.broker.last_progress.pop(job_id, None)

    async def _install_model(self, job: JsonObject, cancelled: JobCancellation) -> None:
        job_id, lease = job["job_id"], job["lease"]
        await self.db.run(self.db.stage, job_id, lease, "install_model", {})
        if self.config.offline:
            raise ReelucidError("OFFLINE_MODE", "离线模式不允许下载模型。")
        loop = asyncio.get_running_loop()

        def install() -> None:
            with prepare_model(
                self.config.models_dir, job["request"]["model_id"], check_cancelled=cancelled.check
            ) as prepared:
                cancelled.check()
                result: JsonObject = {
                    "schema_version": "1.0",
                    "job_id": job_id,
                    "status": "succeeded",
                    "model": prepared.manifest,
                    "warnings": [],
                }
                # The dedicated SQLite writer orders the final rename against cancel().
                commit = self.db.run(
                    self.db.finish, job_id, lease, result, [], before_commit=prepared.publish
                )
                asyncio.run_coroutine_threadsafe(commit, loop).result()

        await asyncio.to_thread(install)

    async def heartbeat(self, job_id: str, lease: str) -> None:
        while True:
            await asyncio.sleep(10)
            await self.db.run(self.db.renew, job_id, lease)

    async def progress(
        self, job_id: str, lease: str, cancelled: JobCancellation, payload: JsonObject
    ) -> None:
        if cancelled.is_set():
            raise Cancelled()
        self.broker.progress(job_id, payload)
        now = time.monotonic()
        if now - self.last_write.get(job_id, 0) >= 2:
            self.last_write[job_id] = now
            await self.db.run(self.db.progress, job_id, lease, payload)
