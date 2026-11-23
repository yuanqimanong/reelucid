"""Signal cancellation from the API loop to worker threads and async requests."""

import asyncio
from collections.abc import Callable, Coroutine
from threading import Event
from typing import Any

from reelucid.domain import Cancelled


class JobCancellation:
    def __init__(self) -> None:
        self._flag = Event()
        self._pending: set[asyncio.Future[Any]] = set()

    def is_set(self) -> bool:
        return self._flag.is_set()

    def check(self) -> None:
        if self.is_set():
            raise Cancelled()

    def set(self) -> None:
        # Only the API loop signals cancellation. Download threads just read the flag.
        self._flag.set()
        for task in self._pending:
            task.cancel()

    async def run[T](self, start: Callable[[], Coroutine[Any, Any, T]]) -> T:
        self.check()
        request = asyncio.create_task(start())
        self._pending.add(request)
        try:
            return await request
        except asyncio.CancelledError:
            parent = asyncio.current_task()
            if parent is not None and not parent.cancelling():
                self.check()
            raise
        finally:
            self._pending.discard(request)
