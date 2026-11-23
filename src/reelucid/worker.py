"""An isolated compute process that handles one media job at a time."""

from __future__ import annotations

import asyncio
import gc
import multiprocessing as mp
import os
import queue
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from reelucid.domain import Cancelled, JsonObject, PipelineOptions, ReelucidError


class ComputeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    protocol_version: Literal[1] = 1
    job_id: str
    lease: str
    source: str
    output: str
    options: PipelineOptions
    cpu_threads: int | None = Field(default=None, ge=1)
    resume: dict[str, str]


class ComputeEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    protocol_version: Literal[1] = 1
    job_id: str
    lease: str
    generation: str
    type: Literal["stage", "progress", "chunk", "done", "error"]
    payload: JsonObject


def _work(
    commands: Any,
    events: Any,
    cancelled: Any,
    parent_pid: int,
    parent_started: float,
    generation: str,
) -> None:
    import psutil

    # Limit mel preprocessing to one BLAS thread before NumPy loads.
    os.environ["OPENBLAS_NUM_THREADS"] = "1"
    try:
        if os.name == "nt":
            psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
        elif hasattr(os, "nice"):
            os.nice(5)
    except OSError, psutil.Error, NotImplementedError:
        pass

    def watch_parent() -> None:
        while True:
            try:
                parent = psutil.Process(parent_pid)
                alive = abs(parent.create_time() - parent_started) < 0.1
            except psutil.Error:
                alive = False
            if not alive:
                for child in psutil.Process().children(recursive=True):
                    try:
                        child.kill()
                    except psutil.Error:
                        pass
                os._exit(0)
            time.sleep(1)

    threading.Thread(target=watch_parent, daemon=True, name="parent-watch").start()
    from reelucid.engines import FasterWhisperEngine
    from reelucid.pipeline import run_pipeline

    model: FasterWhisperEngine | None = None
    model_key: tuple[str, str, str, int, int, int] | None = None
    while message := commands.get():
        request = ComputeRequest.model_validate(message)
        job_id, lease = request.job_id, request.lease

        def emit(event: str, payload: JsonObject, job_id: str = job_id, lease: str = lease) -> None:
            events.put(
                {
                    "job_id": job_id,
                    "lease": lease,
                    "generation": generation,
                    "type": event,
                    "payload": payload,
                }
            )

        try:
            if cancelled.is_set():
                raise Cancelled()
            options = request.options
            cpu_threads = min(options.cpu_threads, request.cpu_threads or options.cpu_threads)
            model_file = Path(options.model_path) / "model.bin"
            stamp = model_file.stat() if model_file.is_file() else None
            key = (
                options.model_path,
                options.device,
                options.compute_type,
                cpu_threads,
                stamp.st_mtime_ns if stamp else 0,
                stamp.st_size if stamp else 0,
            )
            if model_key != key:
                model = None
                gc.collect()
                emit("stage", {"stage": "load_model"})
                model = FasterWhisperEngine(options.model_copy(update={"cpu_threads": cpu_threads}))
                model_key = key
            result = run_pipeline(
                Path(request.source),
                Path(request.output),
                options,
                emit,
                cancelled.is_set,
                engine=model,
                resume=request.resume,
                cpu_threads=cpu_threads,
            )
            emit("done", result)
        except ReelucidError as exc:
            emit("error", exc.public())
        except Exception:
            import traceback

            traceback.print_exc()
            emit(
                "error",
                {
                    "code": "COMPUTE_FAILED",
                    "message": "本地处理失败，请查看服务端诊断日志。",
                    "retryable": False,
                },
            )


class ComputeWorker:
    def __init__(self, generation: str | None = None) -> None:
        import uuid

        self.context = mp.get_context("spawn")
        self.commands: Any = self.context.Queue(maxsize=2)
        self.events: Any = self.context.Queue(maxsize=128)
        self.cancelled: Any = self.context.Event()
        self.generation = generation or uuid.uuid4().hex
        self.process: Any = None

    def start(self) -> None:
        import psutil

        self.process = self.context.Process(
            target=_work,
            args=(
                self.commands,
                self.events,
                self.cancelled,
                os.getpid(),
                psutil.Process().create_time(),
                self.generation,
            ),
            daemon=True,
            name="reelucid-asr",
        )
        self.process.start()

    def stop(self) -> None:
        import psutil

        self.cancelled.set()
        process = self.process
        if process is not None and process.pid is not None:
            try:
                self.commands.put_nowait(None)
            except queue.Full:
                pass
            process.join(timeout=4)
            if process.is_alive():
                try:
                    children = psutil.Process(process.pid).children(recursive=True)
                    for child in children:
                        try:
                            child.kill()
                        except psutil.Error:
                            pass
                except psutil.Error:
                    pass
                process.terminate()
                process.join(timeout=3)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=3)
        if process is not None:
            process.close()
            self.process = None
        for channel in (self.commands, self.events):
            channel.cancel_join_thread()
            channel.close()

    def poll(self) -> JsonObject | None:
        try:
            return ComputeEvent.model_validate(self.events.get(timeout=0.25)).model_dump()
        except queue.Empty:
            return None

    async def run(
        self, request: JsonObject, receive: Callable[[str, JsonObject], Awaitable[None]]
    ) -> JsonObject:
        if self.cancelled.is_set():
            raise Cancelled()
        self.commands.put_nowait(ComputeRequest.model_validate(request).model_dump())
        callback_error: Exception | None = None
        while True:
            message = await asyncio.to_thread(self.poll)
            if message is None:
                if not self.process.is_alive():
                    raise ReelucidError(
                        "WORKER_DIED", "计算进程意外退出，任务可重试。", retryable=True
                    )
                continue
            if (message["job_id"], message["lease"], message["generation"]) != (
                request["job_id"],
                request["lease"],
                self.generation,
            ):
                continue
            kind, payload = message["type"], message["payload"]
            if kind in {"done", "error"}:
                if callback_error:
                    raise callback_error
                if kind == "error":
                    if payload["code"] == "CANCELLED":
                        raise Cancelled()
                    raise ReelucidError(
                        payload["code"],
                        payload["message"],
                        retryable=payload.get("retryable", False),
                    )
                return payload
            if callback_error is None:
                try:
                    await receive(kind, payload)
                except Exception as exc:
                    callback_error = exc
                    self.cancelled.set()
