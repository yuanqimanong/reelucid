from collections.abc import Generator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from reelucid.database import Database, NewUrlAsset
from reelucid.db_schema import ArtifactRow, AssetRow, Base, EventRow, JobRow, StageRow
from reelucid.domain import Cancelled, ReelucidError


@pytest.fixture
def db(tmp_path: Path) -> Generator[Database]:
    database = Database(tmp_path / "state.db")
    database.initialize()
    yield database
    database.close()


def test_idempotency_is_durable_and_rejects_different_request(db: Database) -> None:
    first = db.create_job({}, {}, None, idem_key="request", request_hash="a")
    assert (
        db.create_job({}, {}, None, idem_key="request", request_hash="a")["job_id"]
        == first["job_id"]
    )
    with pytest.raises(ReelucidError, match="幂等"):
        db.create_job({}, {}, None, idem_key="request", request_hash="b")


def test_concurrent_url_replays_create_one_asset_and_job(db: Database) -> None:
    def submit() -> tuple[str, str]:
        job = db.create_job(
            {},
            {},
            None,
            idem_key="same",
            request_hash="a",
            limit=1,
            url_asset=NewUrlAsset("example.org", "encrypted-url", None, None),
        )
        return job["job_id"], job["asset_id"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(submit), pool.submit(submit)
        job_id, asset_id = first.result()
        assert second.result() == (job_id, asset_id)
    with Session(db.engine) as session:
        assert session.scalars(select(AssetRow.id)).all() == [asset_id]
        assert session.scalars(select(JobRow.id)).all() == [job_id]


def test_pyp_purge_cleans_job_files_and_keeps_shared_assets(db: Database, tmp_path: Path) -> None:
    source = "ast_" + "a" * 32
    first_key = "pyp-" + "1" * 32
    last_key = "pyp-" + "2" * 32
    db.add_asset(source, "upload", "media", {})
    first = db.create_job({}, {}, source, idem_key=first_key)["job_id"]
    last = db.create_job({}, {}, source, idem_key=last_key)["job_id"]
    db.cancel(first)
    originals = tmp_path / "assets" / source
    artifacts = tmp_path / "artifacts" / first
    work = tmp_path / "work" / first
    for directory in (originals, artifacts, work):
        directory.mkdir(parents=True)
        (directory / "output.bin").write_bytes(b"owned")
    with db.transaction() as session:
        session.add(
            ArtifactRow(
                id="artifact_fixture",
                job_id=first,
                asset_id=source,
                kind="subtitle",
                format="srt",
                path=str(artifacts / "output.bin"),
                sha256="checksum",
                size=5,
            )
        )
    with pytest.raises(ReelucidError, match="正在使用"):
        db.purge_pyp_job(tmp_path, first_key, True, source)
    db.cancel(last)
    high = db.event_high()
    assert db.purge_pyp_job(tmp_path, first_key, True, source) == {
        "removed_job": True,
        "removed_assets": 0,
    }
    assert originals.is_dir()
    assert not artifacts.exists()
    assert not work.exists()
    with Session(db.engine) as session:
        assert session.get(ArtifactRow, "artifact_fixture") is None
        assert session.get(JobRow, first) is None
        assert session.get(JobRow, last) is not None
    assert db.purge_pyp_job(tmp_path, last_key, True, source)["removed_assets"] == 1
    assert not originals.exists()
    assert db.purge_pyp_job(tmp_path, last_key, True, source)["removed_job"] is False
    assert db.event_high() == high
    db.create_job({}, {}, None)
    assert db.event_high() > high


def test_events_keep_existing_ids_during_upgrade(tmp_path: Path) -> None:
    database = Database(tmp_path / "legacy.db")
    try:
        with database.engine.connect() as connection:
            config = Config()
            config.set_main_option(
                "script_location",
                str(Path(__file__).resolve().parents[1] / "src/reelucid/migrations"),
            )
            config.attributes["connection"] = connection
            command.upgrade(config, "0001")
        task = database.create_job({}, {}, None, idem_key="pyp-" + "3" * 32)["job_id"]
        with Session(database.engine) as session:
            before = session.scalars(select(EventRow.id).order_by(EventRow.id)).all()
        database.initialize()
        assert database.event_high() == before[-1]
        assert [event["id"] for event in database.job_events(task, 0, before[-1])] == before
    finally:
        database.close()


def test_url_asset_is_rolled_back_when_job_insert_fails(db: Database) -> None:
    with db.engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TRIGGER reject_job BEFORE INSERT ON jobs "
            "BEGIN SELECT RAISE(ABORT, 'test job failure'); END"
        )
    with pytest.raises(IntegrityError, match="test job failure"):
        db.create_job({}, {}, None, url_asset=NewUrlAsset("example.org", "encrypted", None, None))
    with Session(db.engine) as session:
        assert session.scalar(select(AssetRow.id)) is None


def test_cancel_wins_against_late_success(db: Database) -> None:
    job = db.create_job({}, {}, None)
    claim = db.claim("generation-a")
    assert claim is not None
    db.cancel(job["job_id"])
    with pytest.raises(Cancelled):
        db.finish(job["job_id"], claim["lease"], {}, [])
    db.fail(job["job_id"], claim["lease"], Cancelled().public())
    assert db.job(job["job_id"])["status"] == "cancelled"


def test_claim_skips_active_jobs_and_assets_without_blocking_other_work(db: Database) -> None:
    db.add_asset("shared", "url", "shared", {})
    first = db.create_job({}, {}, "shared")
    second = db.create_job({}, {}, "shared")
    independent = db.create_job({}, {}, None)
    claim = db.claim("primary")
    assert claim and claim["job_id"] == first["job_id"]
    db.fail(first["job_id"], claim["lease"], {"code": "TEMPORARY_FAILURE"})
    db.retry(first["job_id"])
    later = db.claim("helper", active_jobs=(first["job_id"],), active_assets=("shared",))
    assert later and later["job_id"] == independent["job_id"]
    assert db.claim("helper", active_assets=("shared",)) is None
    available = db.claim("primary", active_jobs=(first["job_id"],))
    assert available and available["job_id"] == second["job_id"]


def test_recovery_preserves_chunks_and_rejects_old_lease(db: Database) -> None:
    job = db.create_job({}, {}, None)
    first = db.claim("old-generation")
    assert first is not None
    db.save_chunk(
        job["job_id"],
        first["lease"],
        {"index": 0, "cache_key": "cache", "path": "chunk.json", "sha256": "hash", "chunk": {}},
    )
    db.initialize()
    second = db.claim("new-generation")
    assert second is not None and first["lease"] != second["lease"]
    assert db.resume_chunks(job["job_id"]) == {"chunk.json": "hash"}
    with pytest.raises(ReelucidError, match="租约"):
        db.finish(job["job_id"], first["lease"], {}, [])
    db.finish(job["job_id"], second["lease"], {"warnings": []}, [])
    assert db.job(job["job_id"])["status"] == "succeeded"


def test_expired_lease_rejects_progress_and_artifacts(db: Database) -> None:
    job = db.create_job({}, {}, None)
    claim = db.claim("generation")
    assert claim is not None
    with db.transaction() as session:
        stage = session.get(StageRow, f"{job['job_id']}:workflow")
        assert stage is not None
        stage.lease_until = 1
    with pytest.raises(ReelucidError, match="租约"):
        db.progress(job["job_id"], claim["lease"], {"percent": 100})
    with pytest.raises(ReelucidError, match="租约"):
        db.renew(job["job_id"], claim["lease"])


def test_progress_does_not_append_events_and_configuration_uses_cas(db: Database) -> None:
    job = db.create_job({}, {}, None)
    claim = db.claim("g")
    assert claim is not None
    before = db.event_high()
    for count in range(20):
        db.progress(job["job_id"], claim["lease"], {"percent": count})
    assert db.event_high() == before
    config = db.settings()
    db.save_settings(config, config["revision"])
    with pytest.raises(ReelucidError, match="设置"):
        db.save_settings(config, config["revision"])


def test_sqlite_pragmas_and_queued_cancel(db: Database) -> None:
    with db.engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA journal_mode").scalar() == "wal"
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
        assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar() == 5000
    job = db.create_job({}, {}, None)
    assert db.cancel(job["job_id"])["status"] == "cancelled"
    assert db.claim("g") is None
    with Session(db.engine) as session:
        assert session.scalar(select(JobRow)) is not None


def test_migrations_create_the_current_orm_schema(db: Database) -> None:
    with db.engine.connect() as connection:
        assert compare_metadata(MigrationContext.configure(connection), Base.metadata) == []
