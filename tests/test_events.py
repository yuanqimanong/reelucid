import asyncio
from pathlib import Path

from reelucid.database import Database
from reelucid.events import EventBroker


async def test_slow_sse_consumer_receives_reset_without_blocking_other_clients(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "events.db")
    broker = EventBroker(db)
    slow, fast = broker.subscribe("job"), broker.subscribe("job")
    for index in range(257):
        broker.send("job", {"type": "stage.started", "id": index})
        fast.get_nowait()
    assert (await asyncio.wait_for(slow.get(), 1))["type"] == "reset"
    assert slow not in broker.subscribers["job"]
    assert fast in broker.subscribers["job"]
    db.close()
