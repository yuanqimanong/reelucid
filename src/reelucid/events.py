from __future__ import annotations

import asyncio
import time

from reelucid.database import Database
from reelucid.domain import JsonObject


class EventBroker:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.subscribers: dict[str, set[asyncio.Queue[JsonObject]]] = {}
        self.cursor = 0
        self.last_progress: dict[str, float] = {}
        self.closing = False

    def subscribe(self, job_id: str) -> asyncio.Queue[JsonObject]:
        channel: asyncio.Queue[JsonObject] = asyncio.Queue(maxsize=256)
        self.subscribers.setdefault(job_id, set()).add(channel)
        return channel

    def unsubscribe(self, job_id: str, channel: asyncio.Queue[JsonObject]) -> None:
        if job_id in self.subscribers:
            self.subscribers[job_id].discard(channel)
            if not self.subscribers[job_id]:
                del self.subscribers[job_id]

    def send(self, job_id: str, message: JsonObject) -> None:
        for channel in tuple(self.subscribers.get(job_id, ())):
            try:
                channel.put_nowait(message)
            except asyncio.QueueFull:
                while not channel.empty():
                    channel.get_nowait()
                channel.put_nowait({"type": "reset", "payload": {"job_id": job_id}})
                self.unsubscribe(job_id, channel)

    def progress(self, job_id: str, payload: JsonObject) -> None:
        now = time.monotonic()
        if now - self.last_progress.get(job_id, 0) >= 0.25:
            self.last_progress[job_id] = now
            self.send(job_id, {"type": "stage.progress", "payload": {"job_id": job_id, **payload}})

    async def run(self) -> None:
        while not self.closing:
            rows = await self.database.run(self.database.events_after, self.cursor)
            for row in rows:
                self.cursor = row["id"]
                self.send(row["job_id"], row)
            await asyncio.sleep(0.25 if self.subscribers else 0.75)
