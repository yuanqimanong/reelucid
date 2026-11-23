"""Stream the multipart file into the asset directory and publish it after validation."""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import Coroutine
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from fastapi import HTTPException, Request
from python_multipart import MultipartParser
from python_multipart.exceptions import FormParserError
from python_multipart.multipart import parse_options_header
from starlette.requests import ClientDisconnect

from reelucid.database import Database
from reelucid.domain import ReelucidError

MEDIA_EXTENSIONS = frozenset(
    {
        ".mp4",
        ".mkv",
        ".webm",
        ".mov",
        ".mp3",
        ".m4a",
        ".wav",
        ".flac",
        ".ogg",
        ".opus",
        ".aac",
        ".avi",
    }
)
PLAYLIST_PREFIXES = (b"#EXTM3U", b"<MPD", b"<?xml")
READ_BLOCK = 256 * 1024


@dataclass(frozen=True, slots=True)
class UploadedMedia:
    name: str
    size: int
    sha256: str


class MediaPart:
    def __init__(self, stream: BinaryIO, limit: int, charset: str) -> None:
        self.stream = stream
        self.limit = limit
        self.charset = charset
        self.name: str | None = None
        self.size = 0
        self.checksum = hashlib.sha256()
        self.ended = False
        self._file_part = False
        self._fields = 0
        self._field_size = 0
        self._header_size = 0
        self._header_name = bytearray()
        self._header_value = bytearray()
        self._disposition = b""
        self._prefix = b""
        self._checked_prefix = False

    def part_begin(self) -> None:
        self._disposition = b""
        self._header_size = self._field_size = 0
        self._file_part = False

    def _append_header(self, buffer: bytearray, data: bytes, start: int, end: int) -> None:
        self._header_size += end - start
        if self._header_size > 32 * 1024:
            raise HTTPException(400, "上传表单字段头过大。")
        buffer.extend(data[start:end])

    def header_name(self, data: bytes, start: int, end: int) -> None:
        self._append_header(self._header_name, data, start, end)

    def header_value(self, data: bytes, start: int, end: int) -> None:
        self._append_header(self._header_value, data, start, end)

    def header_end(self) -> None:
        if bytes(self._header_name).lower() == b"content-disposition":
            self._disposition = bytes(self._header_value)
        self._header_name.clear()
        self._header_value.clear()

    def headers_finished(self) -> None:
        disposition, params = parse_options_header(self._disposition)
        field_name = params.get(b"name")
        if disposition != b"form-data" or field_name is None:
            raise HTTPException(400, "上传表单缺少有效的字段名。")
        filename = params.get(b"filename")
        if filename is None:
            self._fields += 1
            if self._fields > 4:
                raise HTTPException(400, "上传表单的附加字段过多。")
            return
        if self.name is not None:
            raise HTTPException(400, "每次只允许上传一个文件。")
        if not filename or field_name != b"file":
            raise HTTPException(422, "需要 multipart file 字段。")
        try:
            decoded = filename.decode(self.charset)
        except LookupError, UnicodeError:
            decoded = filename.decode("latin-1")
        name = decoded.replace("\\", "/").rsplit("/", 1)[-1][:250]
        if Path(name).suffix.lower() not in MEDIA_EXTENSIONS:
            raise HTTPException(422, "不支持该文件扩展名。")
        self.name = name
        self._file_part = True

    def part_data(self, data: bytes, start: int, end: int) -> None:
        size = end - start
        if not self._file_part:
            self._field_size += size
            if self._field_size > 1024**2:
                raise HTTPException(400, "上传表单字段过大。")
            return
        if self.size + size > self.limit:
            raise ReelucidError("SIZE_LIMIT_EXCEEDED", "上传文件超过体积限制。")
        block = data[start:end]
        if not self._checked_prefix:
            candidate = block if self._prefix else block.lstrip()
            self._prefix += candidate[: 7 - len(self._prefix)]
            if self._prefix.startswith(PLAYLIST_PREFIXES):
                raise HTTPException(422, "不能上传外部资源播放清单。")
            self._checked_prefix = not any(p.startswith(self._prefix) for p in PLAYLIST_PREFIXES)
        self.stream.write(block)
        self.checksum.update(block)
        self.size += size

    def end(self) -> None:
        self.ended = True

    def finish(self, parser: MultipartParser) -> UploadedMedia:
        parser.finalize()
        if not self.ended:
            raise HTTPException(400, "上传表单未完整结束。")
        if self.name is None:
            raise HTTPException(422, "需要 multipart file 字段。")
        if not self.size:
            raise HTTPException(422, "文件为空。")
        self.stream.flush()
        os.fsync(self.stream.fileno())
        return UploadedMedia(self.name, self.size, self.checksum.hexdigest())


async def _complete[T](work: Coroutine[object, object, T]) -> T:
    # An in-flight thread or commit still owns the file after request cancellation.
    task = asyncio.create_task(work)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise


async def _receive(request: Request, staging: Path, limit: int) -> UploadedMedia:
    content_type, params = parse_options_header(request.headers.get("content-type"))
    if content_type != b"multipart/form-data":
        raise HTTPException(422, "需要 multipart file 字段。")
    boundary = params.get(b"boundary", b"")
    if not boundary:
        raise HTTPException(400, "multipart 表单缺少边界。")
    with staging.open("xb", buffering=64 * 1024) as stream:
        part = MediaPart(stream, limit, params.get(b"charset", b"utf-8").decode("latin-1"))
        try:
            parser = MultipartParser(
                boundary,
                {
                    "on_part_begin": part.part_begin,
                    "on_header_field": part.header_name,
                    "on_header_value": part.header_value,
                    "on_header_end": part.header_end,
                    "on_headers_finished": part.headers_finished,
                    "on_part_data": part.part_data,
                    "on_end": part.end,
                },
            )
            async for block in request.stream():
                for offset in range(0, len(block), READ_BLOCK):
                    await _complete(
                        asyncio.to_thread(parser.write, block[offset : offset + READ_BLOCK])
                    )
            return await _complete(asyncio.to_thread(part.finish, parser))
        except FormParserError as exc:
            raise HTTPException(400, "上传表单格式无效。") from exc
        except ClientDisconnect as exc:
            raise HTTPException(400, "上传连接已中断。") from exc


async def store_upload(
    request: Request, db: Database, directory: Path, identifier: str, limit: int
) -> UploadedMedia:
    staging = directory / ".source.partial"
    created = False
    published = False

    def prepare() -> None:
        nonlocal created
        directory.mkdir(parents=True)
        created = True

    def discard() -> None:
        staging.unlink(missing_ok=True)
        directory.rmdir()

    try:
        await _complete(asyncio.to_thread(prepare))
        result = await _receive(request, staging, limit)
        destination = directory / f"source{Path(result.name).suffix.lower()}"

        def publish() -> None:
            nonlocal published
            staging.replace(destination)
            try:
                db.add_asset(
                    identifier,
                    "upload",
                    result.name,
                    {
                        "source": {
                            "path": str(destination),
                            "sha256": result.sha256,
                            "size_bytes": result.size,
                        }
                    },
                )
            except Exception:
                destination.unlink(missing_ok=True)
                raise
            published = True

        await _complete(db.run(publish))
        return result
    finally:
        if created and not published:
            await _complete(asyncio.to_thread(discard))
