from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from hashlib import sha256
from pathlib import Path
from typing import cast

from httpx2 import ASGITransport, AsyncClient

from reelucid.api import create_app
from reelucid.database import Database
from reelucid.settings import AppConfig

BOUNDARY = "test-boundary"
HEAD = (
    f"--{BOUNDARY}\r\n"
    'Content-Disposition: form-data; name="file"; filename="voice.wav"\r\n'
    "Content-Type: audio/wav\r\n\r\n"
).encode()
END = f"\r\n--{BOUNDARY}--\r\n".encode()
HEADERS = {"Content-Type": f"multipart/form-data; boundary={BOUNDARY}"}


@asynccontextmanager
async def uploads(data: Path, limit: int) -> AsyncGenerator[tuple[AsyncClient, Database]]:
    config = AppConfig(data)
    config.max_bytes = limit
    app = create_app(config)
    async with app.router.lifespan_context(app):
        token = config.admin_token_file.read_text().strip()
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://reelucid.test",
            headers={"Authorization": f"Bearer {token}"},
        ) as client:
            yield client, cast(Database, app.state.db)


async def test_upload_writes_once_before_final_boundary_and_registers_digest(
    tmp_path: Path,
) -> None:
    payload = b"RIFF" + bytes(range(256)) * 512
    assets = tmp_path / "assets"

    async def body() -> AsyncIterator[bytes]:
        first = 96 * 1024
        yield HEAD[:27]
        yield HEAD[27:] + payload[:first]
        files = list(assets.glob("*/*"))
        assert len(files) == 1 and files[0].name == ".source.partial"
        assert files[0].stat().st_size >= 60 * 1024
        assert list((tmp_path / "work" / "tmp").iterdir()) == []
        yield payload[first:] + END[:5]
        yield END[5:]

    async with uploads(tmp_path, len(payload)) as (client, db):
        response = await client.post("/api/v1/assets", content=body(), headers=HEADERS)
        assert response.status_code == 201
        asset = response.json()
        assert asset["size_bytes"] == len(payload)
        source = (await db.run(db.asset, asset["asset_id"]))["resources"]["source"]
        assert source["sha256"] == sha256(payload).hexdigest()
        assert [p.name for p in assets.glob("*/*")] == ["source.wav"]
        media = await client.get(
            f"/api/v1/assets/{asset['asset_id']}/media", headers={"Range": "bytes=0-3"}
        )
        assert media.status_code == 206 and media.content == b"RIFF"


async def test_oversized_stream_stops_reading_and_cleans_pending_upload(tmp_path: Path) -> None:
    limit = 65536

    async def body() -> AsyncIterator[bytes]:
        yield HEAD + b"media" * 14000
        raise AssertionError(
            "The file limit should be enforced before reading the rest of the body"
        )

    async with uploads(tmp_path, limit) as (client, _db):
        response = await client.post("/api/v1/assets", content=body(), headers=HEADERS)
        assert response.status_code == 413
        assert response.json()["error"]["code"] == "SIZE_LIMIT_EXCEEDED"
        assert list((tmp_path / "assets").iterdir()) == []


async def test_incomplete_form_and_split_playlist_leave_no_asset(tmp_path: Path) -> None:
    async def playlist() -> AsyncIterator[bytes]:
        yield HEAD + b" \n#EX"
        yield b"TM3U\n" + END

    async with uploads(tmp_path, 1024**2) as (client, _db):
        incomplete = await client.post("/api/v1/assets", content=HEAD + b"RIFF", headers=HEADERS)
        assert incomplete.status_code == 400
        rejected = await client.post("/api/v1/assets", content=playlist(), headers=HEADERS)
        assert rejected.status_code == 422
        assert list((tmp_path / "assets").iterdir()) == []
