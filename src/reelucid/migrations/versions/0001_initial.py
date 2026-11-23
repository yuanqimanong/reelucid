"""Initial seven application tables; frozen independently of ORM evolution."""

import sqlalchemy as sa
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "assets",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("source", sa.JSON(), nullable=False),
        sa.Column("resources", sa.JSON(), nullable=False),
        sa.Column("media", sa.JSON(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created", sa.Double[float](), nullable=False),
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("asset_id", sa.String(), sa.ForeignKey("assets.id")),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("stage", sa.String(), nullable=False),
        sa.Column("progress", sa.JSON(), nullable=False),
        sa.Column("error", sa.JSON()),
        sa.Column("result", sa.JSON()),
        sa.Column("warnings", sa.JSON(), nullable=False),
        sa.Column("idem_key", sa.String(), unique=True),
        sa.Column("request_hash", sa.String(), nullable=False),
        sa.Column("created", sa.Double[float](), nullable=False),
        sa.Column("started", sa.Float()),
        sa.Column("finished", sa.Float()),
        sa.Column("deleted", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_jobs_status", "jobs", ["status"])
    op.create_index("ix_jobs_created", "jobs", ["created"])
    op.create_table(
        "job_stages",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("job_id", sa.String(), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("lease", sa.String()),
        sa.Column("generation", sa.String()),
        sa.Column("lease_until", sa.Float()),
        sa.Column("progress", sa.JSON(), nullable=False),
    )
    op.create_index("ix_job_stages_job_id", "job_stages", ["job_id"])
    op.create_table(
        "asr_chunks",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("job_id", sa.String(), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("cache_key", sa.String(), nullable=False),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("sha256", sa.String(), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
    )
    op.create_index("ix_asr_chunks_job_id", "asr_chunks", ["job_id"])
    op.create_index(
        "ix_chunks_job_cache", "asr_chunks", ["job_id", "chunk_index", "cache_key"], unique=True
    )
    op.create_table(
        "artifacts",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("job_id", sa.String(), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("asset_id", sa.String(), sa.ForeignKey("assets.id")),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("format", sa.String(), nullable=False),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("sha256", sa.String(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("created", sa.Double[float](), nullable=False),
    )
    op.create_index("ix_artifacts_job_id", "artifacts", ["job_id"])
    op.create_index("ix_artifacts_asset_id", "artifacts", ["asset_id"])
    op.create_table(
        "events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("job_id", sa.String(), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created", sa.Double[float](), nullable=False),
    )
    op.create_index("ix_events_job_id", "events", ["job_id"])
    op.create_table(
        "system_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("value", sa.JSON(), nullable=False),
    )


def downgrade() -> None:
    for name in (
        "system_settings",
        "events",
        "artifacts",
        "asr_chunks",
        "job_stages",
        "jobs",
        "assets",
    ):
        op.drop_table(name)
