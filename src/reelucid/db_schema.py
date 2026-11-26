"""SQLAlchemy mappings shared by persistence and Alembic metadata checks."""

from __future__ import annotations

import time

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Index, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from reelucid.domain import JsonObject


class Base(DeclarativeBase):
    pass


class AssetRow(Base):
    __tablename__ = "assets"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    kind: Mapped[str]
    name: Mapped[str]
    source: Mapped[JsonObject] = mapped_column(JSON, default=dict)
    resources: Mapped[JsonObject] = mapped_column(JSON, default=dict)
    media: Mapped[JsonObject] = mapped_column(JSON, default=dict)
    revision: Mapped[int] = mapped_column(default=1)
    created: Mapped[float] = mapped_column(default=time.time)


class JobRow(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    asset_id: Mapped[str | None] = mapped_column(ForeignKey("assets.id"))
    kind: Mapped[str] = mapped_column(default="media")
    status: Mapped[str] = mapped_column(index=True, default="queued")
    request: Mapped[JsonObject] = mapped_column(JSON)
    snapshot: Mapped[JsonObject] = mapped_column(JSON)
    attempt: Mapped[int] = mapped_column(default=1)
    stage: Mapped[str] = mapped_column(default="queued")
    progress: Mapped[JsonObject] = mapped_column(JSON, default=dict)
    error: Mapped[JsonObject | None] = mapped_column(JSON)
    result: Mapped[JsonObject | None] = mapped_column(JSON)
    warnings: Mapped[list[str]] = mapped_column(JSON, default=list)
    idem_key: Mapped[str | None] = mapped_column(String, unique=True)
    request_hash: Mapped[str] = mapped_column(default="")
    created: Mapped[float] = mapped_column(index=True, default=time.time)
    started: Mapped[float | None] = mapped_column(Float)
    finished: Mapped[float | None] = mapped_column(Float)
    deleted: Mapped[bool] = mapped_column(Boolean, default=False)


class StageRow(Base):
    __tablename__ = "job_stages"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    name: Mapped[str]
    status: Mapped[str] = mapped_column(default="pending")
    lease: Mapped[str | None] = mapped_column(String)
    generation: Mapped[str | None] = mapped_column(String)
    lease_until: Mapped[float | None] = mapped_column(Float)
    progress: Mapped[JsonObject] = mapped_column(JSON, default=dict)


class ChunkRow(Base):
    __tablename__ = "asr_chunks"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    chunk_index: Mapped[int]
    cache_key: Mapped[str]
    path: Mapped[str]
    sha256: Mapped[str]
    metadata_json: Mapped[JsonObject] = mapped_column(JSON)


class ArtifactRow(Base):
    __tablename__ = "artifacts"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    asset_id: Mapped[str | None] = mapped_column(ForeignKey("assets.id"), index=True)
    kind: Mapped[str]
    format: Mapped[str]
    path: Mapped[str]
    sha256: Mapped[str]
    size: Mapped[int] = mapped_column(Integer)
    created: Mapped[float] = mapped_column(default=time.time)


class EventRow(Base):
    __tablename__ = "events"
    __table_args__ = {"sqlite_autoincrement": True}
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("jobs.id"), index=True)
    type: Mapped[str]
    payload: Mapped[JsonObject] = mapped_column(JSON)
    created: Mapped[float] = mapped_column(default=time.time)


class SettingsRow(Base):
    __tablename__ = "system_settings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    value: Mapped[JsonObject] = mapped_column(JSON)


Index("ix_chunks_job_cache", ChunkRow.job_id, ChunkRow.chunk_index, ChunkRow.cache_key, unique=True)
