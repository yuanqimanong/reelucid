"""One serialized SQLite writer. Native workers never receive an engine/session."""

from __future__ import annotations

import asyncio
import shutil
import time
import uuid
from collections.abc import Callable, Generator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, delete, event, func, or_, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from reelucid.db_schema import (
    ArtifactRow,
    AssetRow,
    ChunkRow,
    EventRow,
    JobRow,
    SettingsRow,
    StageRow,
)
from reelucid.domain import (
    TERMINAL,
    Cancelled,
    JsonObject,
    ReelucidError,
    check_transition,
)
from reelucid.files import contained
from reelucid.settings import SystemSettings


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def iso(value: float | None) -> str | None:
    return datetime.fromtimestamp(value, UTC).isoformat() if value is not None else None


@dataclass(frozen=True, slots=True)
class NewUrlAsset:
    host: str
    encrypted_url: str
    adapter_id: str | None
    credential_profile_id: str | None


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="reelucid-db")
        self.engine: Engine = create_engine(
            f"sqlite:///{path.as_posix()}",
            pool_size=1,
            max_overflow=0,
            connect_args={"check_same_thread": False},
        )

        @event.listens_for(self.engine, "connect")
        def configure(connection: Any, _record: Any) -> None:
            cursor = connection.cursor()
            for sql in (
                "PRAGMA busy_timeout=5000",
                "PRAGMA foreign_keys=ON",
                "PRAGMA synchronous=NORMAL",
            ):
                cursor.execute(sql)
            cursor.close()

    async def run[T](self, operation: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        from functools import partial

        return await asyncio.get_running_loop().run_in_executor(
            self.executor, partial(operation, *args, **kwargs)
        )

    def initialize(self, initial_settings: SystemSettings | None = None) -> None:
        from alembic import command
        from alembic.config import Config

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA journal_mode=WAL")
            connection.commit()
            config = Config()
            config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
        with self.transaction() as session:
            if session.get(SettingsRow, 1) is None:
                defaults = initial_settings or SystemSettings()
                session.add(SettingsRow(id=1, value={**defaults.model_dump(), "secrets": {}}))
            for job in session.scalars(
                select(JobRow).where(JobRow.status.in_(["running", "cancelling"]))
            ):
                before = job.status
                job.status = "cancelled" if before == "cancelling" else "queued"
                job.stage = job.status
                if job.status == "cancelled":
                    job.finished = time.time()
                self.emit(
                    session, job.id, "job.recovered", {"status": job.status, "attempt": job.attempt}
                )
            for stage in session.scalars(select(StageRow).where(StageRow.status == "running")):
                stage.status, stage.lease, stage.lease_until = "pending", None, None

    @contextmanager
    def transaction(self) -> Generator[Session]:
        with Session(self.engine) as session:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    def close(self) -> None:
        self.executor.shutdown(wait=True)
        self.engine.dispose()

    @staticmethod
    def emit(session: Session, job_id: str, name: str, payload: JsonObject) -> None:
        session.add(EventRow(job_id=job_id, type=name, payload={"job_id": job_id, **payload}))

    def settings(self) -> JsonObject:
        with Session(self.engine) as session:
            row = session.get(SettingsRow, 1)
            assert row is not None
            return row.value.copy()

    def save_settings(self, value: JsonObject, revision: int) -> JsonObject:
        with self.transaction() as session:
            row = session.get(SettingsRow, 1)
            assert row is not None
            if row.value["revision"] != revision:
                raise ReelucidError("CONFIG_CONFLICT", "设置已被更新，请刷新后重试。")
            row.value = {**value, "secrets": row.value.get("secrets", {}), "revision": revision + 1}
            return row.value.copy()

    def put_secret(self, identifier: str, encrypted: str | None) -> None:
        with self.transaction() as session:
            row = session.get(SettingsRow, 1)
            assert row is not None
            value = row.value.copy()
            secrets = dict(value.get("secrets", {}))
            if encrypted is None:
                secrets.pop(identifier, None)
            else:
                secrets[identifier] = encrypted
            row.value = {**value, "secrets": secrets, "revision": int(value["revision"]) + 1}

    def add_asset(
        self,
        identifier: str,
        kind: str,
        name: str,
        resources: JsonObject,
        source: JsonObject | None = None,
    ) -> JsonObject:
        with self.transaction() as session:
            row = AssetRow(
                id=identifier,
                kind=kind,
                name=name[:300],
                resources=resources,
                source=source or {},
                media={},
            )
            session.add(row)
            session.flush()
            return self.asset_dict(row)

    @staticmethod
    def asset_dict(row: AssetRow) -> JsonObject:
        return {
            "asset_id": row.id,
            "kind": row.kind,
            "name": row.name,
            "resources": row.resources,
            "source": row.source,
            "media": row.media,
            "revision": row.revision,
            "created_at": iso(row.created),
        }

    def asset(self, identifier: str) -> JsonObject:
        with Session(self.engine) as session:
            row = session.get(AssetRow, identifier)
            if row is None:
                raise ReelucidError("NOT_FOUND", "素材不存在。")
            return self.asset_dict(row)

    def update_asset(
        self, identifier: str, resources: JsonObject, media: JsonObject | None = None
    ) -> None:
        with self.transaction() as session:
            row = session.get(AssetRow, identifier)
            if row is None:
                raise ReelucidError("NOT_FOUND", "素材不存在。")
            row.resources = {**row.resources, **resources}
            if resources.get("source", {}).get("name"):
                row.name = str(resources["source"]["name"])[:300]
            if media is not None:
                row.media = media
            row.revision += 1

    def create_job(
        self,
        request: JsonObject,
        snapshot: JsonObject,
        asset_id: str | None,
        *,
        idem_key: str | None = None,
        request_hash: str = "",
        kind: str = "media",
        limit: int = 100,
        url_asset: NewUrlAsset | None = None,
    ) -> JsonObject:
        with self.transaction() as session:
            if idem_key:
                old = session.scalar(select(JobRow).where(JobRow.idem_key == idem_key))
                if old and old.created < time.time() - 86400:
                    old.idem_key = None
                    session.flush()
                    old = None
                if old:
                    if old.request_hash != request_hash:
                        raise ReelucidError("IDEMPOTENCY_CONFLICT", "该幂等键已用于不同请求。")
                    return self.job_dict(old)
            count = (
                session.scalar(
                    select(func.count())
                    .select_from(JobRow)
                    .where(JobRow.status.in_(["queued", "running", "cancelling"]))
                )
                or 0
            )
            if count >= limit:
                raise ReelucidError("QUEUE_FULL", "任务队列已满，请稍后重试。", retryable=True)
            if url_asset is not None:
                if asset_id is not None:
                    raise ValueError("A job can reference either an existing asset or a new URL")
                asset_id = new_id("ast")
                session.add(
                    AssetRow(
                        id=asset_id,
                        kind="url",
                        name=url_asset.host[:300],
                        source={
                            "url": url_asset.encrypted_url,
                            "adapter_id": url_asset.adapter_id,
                            "credential_profile_id": url_asset.credential_profile_id,
                        },
                    )
                )
                session.flush()
            row = JobRow(
                id=new_id("job"),
                request=request,
                snapshot=snapshot,
                asset_id=asset_id,
                kind=kind,
                idem_key=idem_key,
                request_hash=request_hash,
            )
            session.add(row)
            session.flush()
            self.emit(session, row.id, "job.queued", {"status": "queued", "attempt": 1})
            return self.job_dict(row)

    def idempotent(self, key: str, request_hash: str) -> JsonObject | None:
        with Session(self.engine) as session:
            row = session.scalar(select(JobRow).where(JobRow.idem_key == key))
            if row is None or row.created < time.time() - 86400:
                return None
            if row.request_hash != request_hash:
                raise ReelucidError("IDEMPOTENCY_CONFLICT", "该幂等键已用于不同请求。")
            return self.job_dict(row)

    @staticmethod
    def job_dict(row: JobRow, *, private: bool = False) -> JsonObject:
        value: JsonObject = {
            "job_id": row.id,
            "asset_id": row.asset_id,
            "kind": row.kind,
            "status": row.status,
            "attempt": row.attempt,
            "current_stage": row.stage,
            "progress": row.progress,
            "config_revision": row.snapshot.get("revision", 1),
            "error": row.error,
            "warnings": row.warnings,
            "created_at": iso(row.created),
            "started_at": iso(row.started),
            "finished_at": iso(row.finished),
            "effective_plan": row.request.get("operations", []),
            "links": {
                "self": f"/api/v1/jobs/{row.id}",
                "events": f"/api/v1/jobs/{row.id}/events",
                "result": f"/api/v1/jobs/{row.id}/result",
            },
        }
        if private:
            value.update(request=row.request, snapshot=row.snapshot, result=row.result)
        return value

    def job(self, identifier: str, *, private: bool = False) -> JsonObject:
        with Session(self.engine) as session:
            row = session.get(JobRow, identifier)
            if row is None or row.deleted:
                raise ReelucidError("NOT_FOUND", "任务不存在。")
            result = self.job_dict(row, private=private)
            result["stages"] = [
                {"name": s.name, "status": s.status, "progress": s.progress}
                for s in session.scalars(
                    select(StageRow).where(
                        StageRow.job_id == identifier, StageRow.name != "workflow"
                    )
                )
            ]
            result["artifacts"] = [
                self.artifact_dict(a)
                for a in session.scalars(
                    select(ArtifactRow).where(ArtifactRow.job_id == identifier)
                )
            ]
            return result

    def jobs(
        self,
        limit: int = 50,
        cursor: float | None = None,
        status: str | None = None,
        created_before: float | None = None,
        created_after: float | None = None,
    ) -> JsonObject:
        with Session(self.engine) as session:
            query = select(JobRow).where(JobRow.deleted.is_(False))
            if cursor is not None:
                query = query.where(JobRow.created < cursor)
            if created_before is not None:
                query = query.where(JobRow.created < created_before)
            if created_after is not None:
                query = query.where(JobRow.created > created_after)
            if status:
                query = query.where(JobRow.status.in_(status.split(",")))
            rows = list(session.scalars(query.order_by(JobRow.created.desc()).limit(limit + 1)))
            return {
                "items": [self.job_dict(row) for row in rows[:limit]],
                "next_cursor": rows[limit - 1].created if len(rows) > limit else None,
            }

    def claim(
        self,
        generation: str,
        *,
        active_jobs: tuple[str, ...] = (),
        active_assets: tuple[str, ...] = (),
        eligible: Callable[[JsonObject], bool] | None = None,
    ) -> JsonObject | None:
        with self.transaction() as session:
            queued = (
                select(JobRow, AssetRow.resources["source"]["path"].as_string().is_not(None))
                .outerjoin(AssetRow, JobRow.asset_id == AssetRow.id)
                .where(JobRow.status == "queued", JobRow.deleted.is_(False))
                .order_by(JobRow.created)
            )
            if active_jobs:
                queued = queued.where(JobRow.id.not_in(active_jobs))
            if active_assets:
                queued = queued.where(
                    or_(JobRow.asset_id.is_(None), JobRow.asset_id.not_in(active_assets))
                )
            if eligible is None:
                queued = queued.limit(1)
            for row, has_source in session.execute(queued):
                if eligible is None or eligible(
                    {**self.job_dict(row, private=True), "has_local_source": has_source}
                ):
                    break
            else:
                return None
            check_transition(row.status, "running")
            row.status, row.started, row.stage = "running", time.time(), "starting"
            lease = new_id("lease")
            root = session.get(StageRow, f"{row.id}:workflow")
            if root is None:
                root = StageRow(id=f"{row.id}:workflow", job_id=row.id, name="workflow")
                session.add(root)
            root.status, root.lease, root.generation, root.lease_until = (
                "running",
                lease,
                generation,
                time.time() + 60,
            )
            self.emit(session, row.id, "job.running", {"status": "running", "attempt": row.attempt})
            session.flush()
            return {
                **self.job_dict(row, private=True),
                "lease": lease,
                "has_local_source": has_source,
            }

    @staticmethod
    def guarded(session: Session, job_id: str, lease: str) -> JobRow:
        job = session.get(JobRow, job_id)
        stage = session.get(StageRow, f"{job_id}:workflow")
        if (
            job is None
            or stage is None
            or stage.lease != lease
            or not stage.lease_until
            or stage.lease_until < time.time()
        ):
            raise ReelucidError(
                "LEASE_EXPIRED", "阶段租约已失效，迟到结果不能提交。", retryable=True
            )
        if job.status == "cancelling":
            raise Cancelled()
        if job.status != "running":
            raise ReelucidError("LEASE_EXPIRED", "任务已离开运行状态。")
        return job

    def bind_worker(self, job_id: str, lease: str, generation: str) -> None:
        with self.transaction() as session:
            self.guarded(session, job_id, lease)
            root = session.get(StageRow, f"{job_id}:workflow")
            assert root is not None
            root.generation = generation

    def renew(self, job_id: str, lease: str) -> None:
        with self.transaction() as session:
            root = session.get(StageRow, f"{job_id}:workflow")
            if (
                not root
                or root.lease != lease
                or not root.lease_until
                or root.lease_until < time.time()
            ):
                raise ReelucidError("LEASE_EXPIRED", "已失效租约不能通过续期复活。", retryable=True)
            root.lease_until = time.time() + 60

    def stage(self, job_id: str, lease: str, name: str, progress: JsonObject) -> None:
        with self.transaction() as session:
            job = self.guarded(session, job_id, lease)
            for prior in session.scalars(
                select(StageRow).where(
                    StageRow.job_id == job_id,
                    StageRow.name != "workflow",
                    StageRow.status == "running",
                )
            ):
                if prior.name != name:
                    prior.status = "succeeded"
            row = session.get(StageRow, f"{job_id}:{name}")
            if row is None:
                row = StageRow(id=f"{job_id}:{name}", job_id=job_id, name=name)
                session.add(row)
            row.status, row.progress = "running", progress
            job.stage, job.progress = name, progress
            self.emit(session, job_id, "stage.started", {"stage": name, "attempt": job.attempt})

    def progress(self, job_id: str, lease: str, payload: JsonObject) -> None:
        with self.transaction() as session:
            job = self.guarded(session, job_id, lease)
            job.progress = payload
            stage = session.get(StageRow, f"{job_id}:{job.stage}")
            if stage:
                stage.progress = payload

    def complete_stage(self, job_id: str, lease: str, name: str, status: str) -> None:
        if status not in {"succeeded", "failed"}:
            raise ValueError("Invalid completed stage status")
        with self.transaction() as session:
            self.guarded(session, job_id, lease)
            stage = session.get(StageRow, f"{job_id}:{name}")
            if stage:
                stage.status = status
                self.emit(session, job_id, f"stage.{status}", {"stage": name, "status": status})

    def save_chunk(self, job_id: str, lease: str, payload: JsonObject) -> None:
        with self.transaction() as session:
            self.guarded(session, job_id, lease)
            identifier = f"{job_id}:{payload['index']}:{payload['cache_key']}"
            row = session.get(ChunkRow, identifier)
            if row is None:
                session.add(
                    ChunkRow(
                        id=identifier,
                        job_id=job_id,
                        chunk_index=payload["index"],
                        cache_key=payload["cache_key"],
                        path=payload["path"],
                        sha256=payload["sha256"],
                        metadata_json=payload["chunk"],
                    )
                )

    def resume_chunks(self, job_id: str) -> dict[str, str]:
        with Session(self.engine) as session:
            return {
                r.path: r.sha256
                for r in session.scalars(select(ChunkRow).where(ChunkRow.job_id == job_id))
            }

    def finish(
        self,
        job_id: str,
        lease: str,
        result: JsonObject,
        artifacts: list[JsonObject],
        status: str = "succeeded",
        *,
        before_commit: Callable[[], None] | None = None,
    ) -> None:
        with self.transaction() as session:
            job = self.guarded(session, job_id, lease)
            check_transition(job.status, status)
            for artifact in artifacts:
                if session.get(ArtifactRow, artifact["id"]) is None:
                    session.add(ArtifactRow(**artifact, job_id=job_id, asset_id=job.asset_id))
                    self.emit(
                        session,
                        job_id,
                        "artifact.ready",
                        {"artifact_id": artifact["id"], "kind": artifact["kind"]},
                    )
            job.status, job.finished, job.result = status, time.time(), result
            job.stage = "completed"
            job.warnings, job.progress = (
                result.get("warnings", []),
                {"stage": "completed", "percent": 100},
            )
            for stage in session.scalars(
                select(StageRow).where(StageRow.job_id == job_id, StageRow.status == "running")
            ):
                stage.status, stage.lease_until = "succeeded", None
            self.emit(session, job_id, f"job.{status}", {"status": status, "attempt": job.attempt})

            if before_commit is not None:
                session.flush()
                before_commit()

    def publish_artifacts(self, job_id: str, lease: str, artifacts: list[JsonObject]) -> None:
        with self.transaction() as session:
            job = self.guarded(session, job_id, lease)
            for artifact in artifacts:
                session.add(ArtifactRow(**artifact, job_id=job_id, asset_id=job.asset_id))
                self.emit(
                    session,
                    job_id,
                    "artifact.ready",
                    {"artifact_id": artifact["id"], "kind": artifact["kind"]},
                )

    def job_events(self, job_id: str, after: int, high: int, limit: int = 512) -> list[JsonObject]:
        with Session(self.engine) as session:
            return [
                {"id": e.id, "job_id": e.job_id, "type": e.type, "payload": e.payload}
                for e in session.scalars(
                    select(EventRow)
                    .where(EventRow.job_id == job_id, EventRow.id > after, EventRow.id <= high)
                    .order_by(EventRow.id)
                    .limit(limit)
                )
            ]

    def event_high(self) -> int:
        with Session(self.engine) as session:
            return int(
                session.scalar(text("SELECT seq FROM sqlite_sequence WHERE name = 'events'")) or 0
            )

    def fail(self, job_id: str, lease: str, error: JsonObject) -> None:
        with self.transaction() as session:
            job = session.get(JobRow, job_id)
            root = session.get(StageRow, f"{job_id}:workflow")
            if (
                not job
                or not root
                or root.lease != lease
                or job.status not in {"running", "cancelling"}
            ):
                return
            status = (
                "cancelled"
                if job.status == "cancelling" or error["code"] == "CANCELLED"
                else "failed"
            )
            job.status, job.finished, job.error = status, time.time(), error
            for stage in session.scalars(
                select(StageRow).where(StageRow.job_id == job_id, StageRow.status == "running")
            ):
                stage.status, stage.lease_until = status, None
            self.emit(
                session,
                job_id,
                f"job.{status}",
                {"status": status, "error": error, "attempt": job.attempt},
            )

    def cancel(self, job_id: str) -> JsonObject:
        with self.transaction() as session:
            row = session.get(JobRow, job_id)
            if row is None or row.deleted:
                raise ReelucidError("NOT_FOUND", "任务不存在。")
            if row.status == "queued":
                row.status, row.finished = "cancelled", time.time()
            elif row.status == "running":
                row.status = "cancelling"
            else:
                return self.job_dict(row)
            self.emit(
                session, job_id, f"job.{row.status}", {"status": row.status, "attempt": row.attempt}
            )
            return self.job_dict(row)

    def retry(self, job_id: str) -> JsonObject:
        with self.transaction() as session:
            row = session.get(JobRow, job_id)
            if row is None or row.deleted:
                raise ReelucidError("NOT_FOUND", "任务不存在。")
            if row.status in {"queued", "running", "cancelling"}:
                return self.job_dict(row)
            check_transition(row.status, "queued")
            row.status, row.attempt, row.error, row.finished, row.stage = (
                "queued",
                row.attempt + 1,
                None,
                None,
                "queued",
            )
            row.result, row.warnings, row.progress = None, [], {}
            self.emit(session, job_id, "job.queued", {"status": "queued", "attempt": row.attempt})
            return self.job_dict(row)

    def delete(self, job_id: str) -> None:
        with self.transaction() as session:
            row = session.get(JobRow, job_id)
            if row is None or row.deleted:
                raise ReelucidError("NOT_FOUND", "任务不存在。")
            if row.status not in TERMINAL:
                raise ReelucidError("JOB_ACTIVE", "请先取消任务，再删除记录。")
            row.deleted = True

    @staticmethod
    def _remove_directory(root: Path, identifier: str) -> None:
        target = root / identifier
        if not identifier or target.parent != root or contained(root, target) != target:
            raise ReelucidError("INVALID_PATH", "待清理目录不在当前任务的存储范围内。")
        try:
            shutil.rmtree(target)
        except FileNotFoundError:
            if target.exists():
                raise

    def purge_pyp_job(
        self, data_dir: Path, key: str, delete_source: bool, uploaded_asset_id: str | None
    ) -> JsonObject:
        lookup = select(JobRow).where(JobRow.idem_key == key)
        with self.transaction() as session:
            job = session.scalar(lookup)
            if job is not None:
                busy_peer = job.asset_id and session.scalar(
                    select(JobRow.id)
                    .where(
                        JobRow.asset_id == job.asset_id,
                        JobRow.id != job.id,
                        JobRow.status.not_in(TERMINAL),
                    )
                    .limit(1)
                )
                if job.status not in TERMINAL or busy_peer:
                    raise ReelucidError(
                        "JOB_ACTIVE", "有任务正在使用此素材，结束后再清理。", retryable=True
                    )
                # Persist before touching files so a failed cleanup stays hidden and can be retried.
                job.deleted = True
        try:
            with self.transaction() as session:
                job = session.scalar(lookup)
                assets = {uploaded_asset_id}
                if job is not None:
                    assets.add(job.asset_id)
                    for directory in ("artifacts", "work"):
                        self._remove_directory(data_dir / directory, job.id)
                    for model in (ArtifactRow, ChunkRow, EventRow, StageRow):
                        session.execute(delete(model).where(model.job_id == job.id))
                    session.delete(job)
                    session.flush()
                removed_assets = 0
                if delete_source:
                    for identifier in assets - {None}:
                        asset = session.get(AssetRow, identifier)
                        if asset is None or session.scalar(
                            select(JobRow.id).where(JobRow.asset_id == asset.id).limit(1)
                        ):
                            continue
                        self._remove_directory(data_dir / "assets", asset.id)
                        session.delete(asset)
                        removed_assets += 1
                return {"removed_job": job is not None, "removed_assets": removed_assets}
        except OSError as exc:
            raise ReelucidError(
                "STORAGE_CLEANUP_FAILED", "文件暂时无法清理。", retryable=True
            ) from exc

    @staticmethod
    def artifact_dict(row: ArtifactRow, *, private: bool = False) -> JsonObject:
        value = {
            "artifact_id": row.id,
            "job_id": row.job_id,
            "asset_id": row.asset_id,
            "kind": row.kind,
            "format": row.format,
            "sha256": row.sha256,
            "size_bytes": row.size,
            "content_url": f"/api/v1/artifacts/{row.id}/content",
        }
        if private:
            value["path"] = row.path
        return value

    def artifact(self, identifier: str) -> JsonObject:
        with Session(self.engine) as session:
            row = session.get(ArtifactRow, identifier)
            if row is None:
                raise ReelucidError("NOT_FOUND", "产物不存在。")
            job = session.get(JobRow, row.job_id)
            if job is None or job.deleted:
                raise ReelucidError("NOT_FOUND", "产物不存在。")
            return self.artifact_dict(row, private=True)

    def transcript_for(self, asset_id: str, artifact_id: str | None = None) -> JsonObject | None:
        with Session(self.engine) as session:
            query = (
                select(ArtifactRow)
                .join(JobRow, JobRow.id == ArtifactRow.job_id)
                .where(
                    ArtifactRow.asset_id == asset_id,
                    ArtifactRow.kind == "transcript",
                    ArtifactRow.format == "json",
                    JobRow.deleted.is_(False),
                )
            )
            if artifact_id is not None:
                # A reader can select an already-published transcript while analysis is running.
                row = session.scalar(query.where(ArtifactRow.id == artifact_id))
                if row is None:
                    raise ReelucidError("NOT_FOUND", "指定文稿在当前素材中不存在或已删除。")
            else:
                # Prefer original transcriptions. Use answer exports only when no original
                # transcript is available.
                reused_from = JobRow.request[("options", "transcript_artifact_id")].as_string()
                row = session.scalar(
                    query.where(JobRow.status.in_(["succeeded", "partially_succeeded"]))
                    .order_by(
                        reused_from.is_not(None), ArtifactRow.created.desc(), ArtifactRow.id.desc()
                    )
                    .limit(1)
                )
            return self.artifact_dict(row, private=True) if row else None

    def events_after(self, after: int, limit: int = 512) -> list[JsonObject]:
        with Session(self.engine) as session:
            return [
                {"id": e.id, "job_id": e.job_id, "type": e.type, "payload": e.payload}
                for e in session.scalars(
                    select(EventRow).where(EventRow.id > after).order_by(EventRow.id).limit(limit)
                )
            ]
