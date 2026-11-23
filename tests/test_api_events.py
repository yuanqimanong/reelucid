import asyncio
from pathlib import Path
from typing import cast
from unittest.mock import patch

from fastapi.testclient import TestClient

from reelucid.api import create_app
from reelucid.database import Database
from reelucid.domain import JsonObject, ReelucidError
from reelucid.service import Service
from reelucid.settings import AppConfig


def test_event_stream_replays_once_and_always_releases_subscription(tmp_path: Path) -> None:
    config = AppConfig(tmp_path)
    database = Database(config.data_dir / "db/reelucid.db")
    try:
        database.initialize()
        job_id = database.create_job({}, {}, None)["job_id"]
        database.cancel(job_id)
        high = database.event_high()
    finally:
        database.close()

    app = create_app(config)
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {config.admin_token_file.read_text().strip()}"
        url = f"/api/v1/jobs/{job_id}/events"
        broker = cast(Service, app.state.service).broker
        db = cast(Database, app.state.db)
        subscribe = broker.subscribe

        def queued_while_replaying(identifier: str) -> asyncio.Queue[JsonObject]:
            queue = subscribe(identifier)
            queue.put_nowait({"id": high, "type": "duplicate", "payload": {}})
            queue.put_nowait({"type": "reset", "payload": {}})
            return queue

        with patch.object(broker, "subscribe", side_effect=queued_while_replaying):
            response = client.get(url)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.text.startswith("event: snapshot\n")
        assert response.text.count("event: job.queued\n") == 1
        assert "event: duplicate\n" not in response.text
        assert response.text.endswith("event: reset\ndata: {}\n\n")
        assert not broker.subscribers

        expired = client.get(url, headers={"Last-Event-ID": str(high + 1)})
        assert expired.status_code == 409
        assert expired.json()["error"]["code"] == "EVENT_CURSOR_EXPIRED"
        assert not broker.subscribers

        with patch.object(db, "job_events", side_effect=ReelucidError("NOT_FOUND", "gone")):
            assert client.get(url).status_code == 404
        assert not broker.subscribers
