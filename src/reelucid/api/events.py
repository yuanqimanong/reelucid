"""Replay persisted job events before continuing from the live SSE subscription."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import StreamingResponse

from reelucid.api.runtime import DatabaseDep, ServiceDep
from reelucid.domain import JsonObject, ReelucidError

router = APIRouter()


@dataclass(frozen=True)
class Subscription:
    channel: asyncio.Queue[JsonObject]
    history: list[JsonObject]
    high_watermark: int


async def subscribe(
    db: DatabaseDep,
    service: ServiceDep,
    identifier: str,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> AsyncIterator[Subscription]:
    await db.run(db.job, identifier)
    try:
        after = int(last_event_id or "0")
    except ValueError as exc:
        raise HTTPException(400, "事件游标必须是整数。") from exc
    # Subscribe before reading the watermark so commits during replay reach the live queue.
    channel = service.broker.subscribe(identifier)
    try:
        high = await db.run(db.event_high)
        history = await db.run(db.job_events, identifier, after, high, 513)
        if len(history) > 512 or after > high or after < 0:
            raise ReelucidError("EVENT_CURSOR_EXPIRED", "请重新读取任务快照后订阅。")
        yield Subscription(channel, history, high)
    finally:
        service.broker.unsubscribe(identifier, channel)


def encode(event: JsonObject) -> str:
    cursor = f"id: {event['id']}\n" if event.get("id") else ""
    data = json.dumps(event["payload"], ensure_ascii=False)
    return f"{cursor}event: {event['type']}\ndata: {data}\n\n"


@router.get("/jobs/{identifier}/events", tags=["Jobs"])
async def events(
    identifier: str,
    request: Request,
    db: DatabaseDep,
    subscription: Annotated[Subscription, Depends(subscribe, scope="request")],
) -> StreamingResponse:
    async def stream() -> AsyncIterator[str]:
        yield encode({"type": "snapshot", "payload": await db.run(db.job, identifier)})
        for entry in subscription.history:
            yield encode(entry)
        while not await request.is_disconnected():
            try:
                entry = await asyncio.wait_for(subscription.channel.get(), timeout=12)
            except TimeoutError:
                yield ": heartbeat\n\n"
                continue
            if entry.get("id", subscription.high_watermark + 1) <= subscription.high_watermark:
                continue
            yield encode(entry)
            if entry["type"] == "reset":
                break

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
