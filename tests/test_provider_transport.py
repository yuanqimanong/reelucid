import asyncio
import json

import pytest
from aiohttp import web

from reelucid.ai import analyze, chat
from reelucid.cancellation import JobCancellation
from reelucid.domain import Cancelled, Segment, Transcript
from reelucid.settings import ModelBinding, Provider


async def test_compatible_provider_consumes_chunked_response_and_sends_auth() -> None:
    async def endpoint(request: web.Request) -> web.StreamResponse:
        assert request.headers["Authorization"] == "Bearer test-private-key"
        body = await request.json()
        assert body["model"] == "protocol-fixture"
        response = web.StreamResponse(headers={"Content-Type": "application/json"})
        await response.prepare(request)
        raw = json.dumps({"choices": [{"message": {"content": "verified reply"}}]}).encode()
        await response.write(raw[:12])
        await response.write(raw[12:])
        await response.write_eof()
        return response

    app = web.Application()
    app.router.add_post("/v1/chat/completions", endpoint)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    try:
        port = runner.addresses[0][1]
        provider = Provider(id="test", base_url=f"http://127.0.0.1:{port}/v1")
        binding = ModelBinding(provider_id="test", model="protocol-fixture")
        assert (
            await chat(
                provider, binding, "test-private-key", [{"role": "user", "content": "hello"}]
            )
            == "verified reply"
        )
    finally:
        await runner.cleanup()


async def test_cancelling_first_batch_closes_connection_and_stops_later_calls() -> None:
    entered, disconnected, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    requests = 0

    async def delayed(_request: web.Request) -> web.Response:
        nonlocal requests
        requests += 1
        entered.set()
        try:
            await release.wait()
        finally:
            disconnected.set()
        return web.json_response({"choices": [{"message": {"content": "invalid JSON"}}]})

    app = web.Application()
    app.router.add_post("/v1/chat/completions", delayed)
    runner = web.AppRunner(app, handler_cancellation=True)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    try:
        provider = Provider(id="test", base_url=f"http://127.0.0.1:{runner.addresses[0][1]}/v1")
        binding = ModelBinding(provider_id="test", model="delayed")
        transcript = Transcript(
            version_id="two-batches",
            segments=[Segment(start_ms=i, end_ms=i + 1, text="x" * 8000) for i in range(2)],
        )
        token = JobCancellation()
        task = asyncio.create_task(
            analyze(transcript, {"type": "summarize"}, provider, binding, None, cancellation=token)
        )
        await asyncio.wait_for(entered.wait(), timeout=5)
        token.set()
        with pytest.raises(Cancelled):
            await asyncio.wait_for(task, timeout=5)
        await asyncio.wait_for(disconnected.wait(), timeout=5)
        assert requests == 1
    finally:
        release.set()
        await runner.cleanup()
