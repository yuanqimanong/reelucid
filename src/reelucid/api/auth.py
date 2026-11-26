"""Bearer and browser-session authentication at the HTTP boundary."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from reelucid.api.runtime import SecurityDep
from reelucid.api.schemas import Login

login_router = APIRouter()
session_router = APIRouter()

bearer = HTTPBearer(auto_error=False)


async def require_bearer(
    security: SecurityDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> None:
    if credentials is None or not security.valid_token(credentials.credentials):
        raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Bearer"})


async def authenticated(
    security: SecurityDep,
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> None:
    if (
        credentials
        and credentials.scheme.lower() == "bearer"
        and security.valid_token(credentials.credentials)
    ):
        return
    cookie = request.cookies.get("reelucid_session", "")
    if cookie and security.valid_session(cookie):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin != f"{request.url.scheme}://{request.url.netloc}":
                raise HTTPException(403, "Origin verification failed")
        return
    raise HTTPException(401, "Authentication required", headers={"WWW-Authenticate": "Bearer"})


@login_router.post("/auth/session", tags=["Authentication"])
async def login(security: SecurityDep, body: Login, request: Request) -> JSONResponse:
    if request.headers.get("origin") not in {
        None,
        f"{request.url.scheme}://{request.url.netloc}",
    }:
        raise HTTPException(403, "Origin verification failed")
    if not security.valid_token(body.token):
        raise HTTPException(401, "Token 无效。")
    response = JSONResponse({"authenticated": True})
    response.set_cookie(
        "reelucid_session",
        security.session(),
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        max_age=43200,
    )
    return response


@session_router.delete("/auth/session", tags=["Authentication"])
async def logout() -> Response:
    response = JSONResponse({"authenticated": False})
    response.delete_cookie("reelucid_session")
    return response
