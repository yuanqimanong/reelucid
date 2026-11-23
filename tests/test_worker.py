from pytest import raises

from reelucid.domain import Cancelled, JsonObject
from reelucid.worker import ComputeWorker


async def test_worker_preserves_cancellation_before_dispatch() -> None:
    worker = ComputeWorker()

    async def receive(kind: str, payload: JsonObject) -> None:
        raise AssertionError("cancelled work must not dispatch")

    try:
        worker.cancelled.set()
        with raises(Cancelled):
            await worker.run({}, receive)
        assert worker.cancelled.is_set()
    finally:
        worker.stop()
