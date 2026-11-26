"""HTTP body limits, public error envelopes, and the Web application's static files."""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from reelucid.database import new_id
from reelucid.domain import ReelucidError
from reelucid.settings import AppConfig

ERROR_STATUS = {
    "NOT_FOUND": 404,
    "MODEL_NOT_READY": 409,
    "AI_NOT_CONFIGURED": 409,
    "CONFIG_CONFLICT": 409,
    "PROFILE_NOT_FOUND": 404,
    "IDEMPOTENCY_CONFLICT": 409,
    "JOB_ACTIVE": 409,
    "INVALID_TRANSITION": 409,
    "RESULT_NOT_READY": 409,
    "EVENT_CURSOR_EXPIRED": 409,
    "QUEUE_FULL": 429,
    "SIZE_LIMIT_EXCEEDED": 413,
    "STORAGE_EXHAUSTED": 507,
    "STORAGE_CLEANUP_FAILED": 503,
    "CAPABILITY_UNAVAILABLE": 422,
}


class BodyLimit:
    def __init__(self, app: ASGIApp, config: AppConfig) -> None:
        self.app, self.config = app, config

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self.config.max_bytes + 1024**2 if scope["path"] == "/api/v1/assets" else 1024**2
        headers = dict(scope["headers"])
        try:
            length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            length = limit + 1
        if length > limit:
            await JSONResponse(
                {"error": {"code": "SIZE_LIMIT_EXCEEDED", "message": "请求体超过实例上限。"}},
                status_code=413,
            )(scope, receive, send)
            return
        size = 0

        async def bounded() -> Message:
            nonlocal size
            message = await receive()
            size += len(message.get("body", b""))
            if size > limit:
                raise HTTPException(413, "请求体超过实例上限。")
            return message

        async def secure_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                extra = [
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"same-origin"),
                    (
                        b"content-security-policy",
                        b"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'",
                    ),
                ]
                message["headers"] = list(message.get("headers", [])) + extra
            await send(message)

        await self.app(scope, bounded, secure_send)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ReelucidError)
    async def domain_error(_request: Request, error: ReelucidError) -> JSONResponse:
        return JSONResponse(
            {"error": {**error.public(), "trace_id": new_id("trace")}},
            status_code=ERROR_STATUS.get(error.code, 400),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, error: StarletteHTTPException) -> JSONResponse:
        return JSONResponse(
            {
                "error": {
                    "code": "SIZE_LIMIT_EXCEEDED"
                    if error.status_code == 413
                    else f"HTTP_{error.status_code}",
                    "message": str(error.detail),
                }
            },
            status_code=error.status_code,
            headers=error.headers,
        )

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Request, _error: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            {
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": "请求参数无效，请检查 OpenAPI 中的类型、范围和必填字段。",
                }
            },
            status_code=422,
        )


def mount_web(app: FastAPI, web_dir: Path) -> None:
    if not web_dir.is_dir():
        return
    assets = web_dir / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="web-assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def web(path: str) -> FileResponse:
        if path.startswith("api/"):
            raise HTTPException(404, "API route not found")
        return FileResponse(web_dir / "index.html", headers={"Cache-Control": "no-cache"})
