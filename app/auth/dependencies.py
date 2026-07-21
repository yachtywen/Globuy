"""FastAPI dependencies for authenticated and CSRF-protected routes."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request

from app.auth.service import AuthService, Principal


def auth_service(request: Request) -> AuthService:
    return request.app.state.auth_service


async def current_user(
    request: Request,
    service: Annotated[AuthService, Depends(auth_service)],
) -> Principal:
    cookie_name = request.app.state.settings.auth_cookie_name
    return await service.authenticate(request.cookies.get(cookie_name))


async def csrf_user(
    request: Request,
    principal: Annotated[Principal, Depends(current_user)],
    service: Annotated[AuthService, Depends(auth_service)],
) -> Principal:
    settings = request.app.state.settings
    service.verify_csrf(
        principal,
        request.cookies.get(settings.auth_csrf_cookie_name),
        request.headers.get("X-CSRF-Token"),
    )
    return principal
