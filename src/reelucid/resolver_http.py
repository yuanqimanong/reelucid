"""Bounded HTTP for administrator-configured resolvers, separate from media transport."""

from __future__ import annotations

import hashlib
import http.client
import json
import time
import urllib.error
import urllib.request
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from reelucid.domain import Cancelled, JsonObject, ReelucidError
from reelucid.ports import Cancellation, Progress
from reelucid.settings import SourceResolver


class NoResolverRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        raise ReelucidError("RESOLVER_REDIRECT_BLOCKED", "解析服务发生重定向，请配置最终服务地址。")


class ResolverClient:
    def __init__(
        self, config: SourceResolver, api_key: str | None, cancelled: Cancellation
    ) -> None:
        self.config, self.api_key, self.cancelled = config, api_key, cancelled
        self.deadline = time.monotonic() + config.timeout_seconds
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoResolverRedirects()
        )

    def remaining(self) -> float:
        if self.cancelled():
            raise Cancelled()
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ReelucidError("RESOLVER_TIMEOUT", "解析服务超时。", retryable=True)
        return remaining

    @contextmanager
    def _response(
        self, path: str, accept: str, body: JsonObject | None = None
    ) -> Generator[http.client.HTTPResponse]:
        headers = {"Accept": accept, "Accept-Encoding": "identity"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        req = urllib.request.Request(
            self.config.base_url + path,
            headers=headers,
            data=json.dumps(body).encode() if body is not None else None,
        )
        try:
            response = cast(
                http.client.HTTPResponse, self.opener.open(req, timeout=min(30, self.remaining()))
            )
        except urllib.error.HTTPError as exc:
            exc.close()
            code = "RESOLVER_AUTH_REQUIRED" if exc.code in {401, 403} else "RESOLVER_FAILED"
            if exc.code == 501 and path.startswith("/api/v1/downloads"):
                raise ReelucidError(
                    "RESOLVER_DOWNLOAD_UNAVAILABLE", "DTK 下载组件未启用或当前版本不支持下载。"
                ) from exc
            if exc.code == 404 and "/files/" in path:
                raise ReelucidError(
                    "RESOLVER_FILE_MISSING", "DTK 已存视频不存在或已被清理。"
                ) from exc
            raise ReelucidError(
                code, "解析服务拒绝请求，请检查 Key、权限与服务日志。", retryable=exc.code >= 500
            ) from exc
        except TimeoutError as exc:
            raise ReelucidError("RESOLVER_TIMEOUT", "解析服务超时。", retryable=True) from exc
        except (OSError, http.client.HTTPException) as exc:
            raise ReelucidError(
                "RESOLVER_UNREACHABLE", "无法连接解析服务。", retryable=True
            ) from exc
        with response:
            yield response

    def _read(self, response: http.client.HTTPResponse, size: int, *, streaming: bool) -> bytes:
        try:
            return response.read1(size) if streaming else response.read(size)
        except TimeoutError as exc:
            raise ReelucidError("RESOLVER_TIMEOUT", "读取 DTK 响应超时。", retryable=True) from exc
        except (OSError, http.client.HTTPException) as exc:
            raise ReelucidError(
                "RESOLVER_UNREACHABLE", "读取解析服务响应失败。", retryable=True
            ) from exc

    def request(self, path: str, body: JsonObject | None = None) -> JsonObject:
        with self._response(path, "application/json", body) as response:
            payload = self._read(response, 2 * 1024**2 + 1, streaming=False)
        if len(payload) > 2 * 1024**2:
            raise ReelucidError("RESOLVER_INVALID_RESPONSE", "解析响应超过体积上限。")
        try:
            result: Any = json.loads(payload)
        except ValueError as exc:
            raise ReelucidError("RESOLVER_INVALID_RESPONSE", "解析服务未返回有效 JSON。") from exc
        if not isinstance(result, dict):
            raise ReelucidError("RESOLVER_INVALID_RESPONSE", "解析响应必须为 JSON 对象。")
        return cast(JsonObject, result)

    def download_video(
        self,
        path: str,
        destination: Path,
        expected_size: int,
        expected_hash: str | None,
        limit: int,
        progress: Progress,
    ) -> tuple[int, str]:
        if expected_size > limit:
            raise ReelucidError("SIZE_LIMIT_EXCEEDED", "DTK 视频超过下载体积上限。")
        destination.parent.mkdir(parents=True, exist_ok=True)
        pending = destination.with_suffix(".pending")
        written = 0
        checksum = hashlib.sha256()
        try:
            with (
                self._response(path, "video/*, application/octet-stream") as response,
                pending.open("wb") as stream,
            ):
                media_type = response.headers.get_content_type()
                length = response.getheader("Content-Length")
                if (
                    response.status != 200
                    or not (
                        media_type.startswith("video/") or media_type == "application/octet-stream"
                    )
                    or response.getheader("Content-Encoding", "identity").lower() != "identity"
                    or length is not None
                    and (not length.isdecimal() or int(length) != expected_size)
                ):
                    raise ReelucidError(
                        "RESOLVER_INVALID_RESPONSE", "DTK 返回的文件类型或长度与下载记录不符。"
                    )
                while True:
                    self.remaining()
                    block = self._read(response, 256 * 1024, streaming=True)
                    if not block:
                        break
                    written += len(block)
                    if written > limit:
                        raise ReelucidError("SIZE_LIMIT_EXCEEDED", "DTK 视频超过下载体积上限。")
                    if written > expected_size:
                        raise ReelucidError(
                            "RESOLVER_INVALID_RESPONSE", "DTK 返回的文件比记录中的视频更大。"
                        )
                    stream.write(block)
                    checksum.update(block)
                    progress(
                        "progress",
                        {
                            "stage": "acquire",
                            "completed_bytes": written,
                            "total_bytes": expected_size,
                        },
                    )
            self.remaining()
            digest = checksum.hexdigest()
            if not written or written != expected_size or expected_hash not in {None, digest}:
                raise ReelucidError("DOWNLOAD_INCOMPLETE", "DTK 文件不完整或哈希校验失败。")
            pending.replace(destination)
            return written, digest
        finally:
            pending.unlink(missing_ok=True)
