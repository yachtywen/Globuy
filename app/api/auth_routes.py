"""Registration, login, logout and current-user endpoints."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request, Response

from app.api.errors import ApiError
from app.api.schemas import LoginRequest, RegisterRequest
from app.auth.dependencies import csrf_user, current_user
from app.auth.service import AuthService, IssuedSession, Principal


def _service(request: Request) -> AuthService:
    service = getattr(request.app.state, "auth_service", None)
    if service is None:
        raise ApiError(503, "DATABASE_NOT_CONFIGURED", "MySQL 尚未配置")
    return service


def _public(principal: Principal) -> dict[str, str]:
    return {
        "user_id": principal.user_id,
        "email": principal.email,
        "display_name": principal.display_name,
    }


def _set_session(response: Response, request: Request, issued: IssuedSession) -> None:
    settings = request.app.state.settings
    max_age = settings.auth_session_days * 24 * 60 * 60
    response.set_cookie(
        settings.auth_cookie_name,
        issued.cookie_value,
        max_age=max_age,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        path="/",
    )
    response.set_cookie(
        settings.auth_csrf_cookie_name,
        issued.csrf_token,
        max_age=max_age,
        httponly=False,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        path="/",
    )


router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", status_code=201)
async def register(
    payload: RegisterRequest,
    request: Request,
    response: Response,
    idempotency_key: Annotated[
        str, Header(alias="Idempotency-Key", min_length=8, max_length=128)
    ],
) -> dict:
    issued = await _service(request).register(
        payload.email, payload.password, payload.display_name, idempotency_key
    )
    _set_session(response, request, issued)
    return {"user": _public(issued.principal), "csrf_token": issued.csrf_token}


@router.post("/login")
async def login(payload: LoginRequest, request: Request, response: Response) -> dict:
    issued = await _service(request).login(payload.email, payload.password)
    _set_session(response, request, issued)
    return {"user": _public(issued.principal), "csrf_token": issued.csrf_token}


@router.post("/logout", status_code=204)
async def logout(
    request: Request,
    response: Response,
    principal: Annotated[Principal, Depends(csrf_user)],
) -> None:
    await _service(request).logout(principal)
    settings = request.app.state.settings
    response.delete_cookie(settings.auth_cookie_name, path="/")
    response.delete_cookie(settings.auth_csrf_cookie_name, path="/")


@router.get("/me")
async def me(principal: Annotated[Principal, Depends(current_user)]) -> dict:
    return {"user": _public(principal)}
