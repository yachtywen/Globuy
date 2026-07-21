"""Password authentication and revocable server-side sessions."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.errors import ApiError
from app.config import Settings
from app.database.models import AuthSession, IdempotencyKey, User, Wishlist
from app.database.session import Database


def utc_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def normalize_email(value: str) -> str:
    return value.strip().casefold()


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class Principal:
    user_id: str
    email: str
    display_name: str
    session_id: str
    csrf_token_hash: str


@dataclass(frozen=True)
class IssuedSession:
    principal: Principal
    cookie_value: str
    csrf_token: str
    expires_at: datetime


class AuthService:
    def __init__(self, database: Database, settings: Settings) -> None:
        self.database = database
        self.settings = settings
        self.passwords = PasswordHasher()

    def _new_session(self, user: User) -> tuple[AuthSession, IssuedSession]:
        now = utc_naive()
        session_id = uuid4().hex
        secret = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        expires_at = now + timedelta(days=self.settings.auth_session_days)
        record = AuthSession(
            session_id=session_id,
            user_id=user.user_id,
            token_hash=_digest(secret),
            csrf_token_hash=_digest(csrf),
            created_at=now,
            expires_at=expires_at,
            last_seen_at=now,
        )
        principal = Principal(
            user_id=user.user_id,
            email=user.email_normalized,
            display_name=user.display_name,
            session_id=session_id,
            csrf_token_hash=record.csrf_token_hash,
        )
        return record, IssuedSession(principal, f"{session_id}.{secret}", csrf, expires_at)

    async def register(
        self,
        email: str,
        password: str,
        display_name: str,
        client_request_id: str,
    ) -> IssuedSession:
        normalized = normalize_email(email)
        normalized_name = display_name.strip()
        request_hash = _digest(f"{normalized}\n{normalized_name}")
        now = utc_naive()
        user = User(
            user_id=uuid4().hex,
            email_normalized=normalized,
            password_hash=self.passwords.hash(password),
            display_name=normalized_name,
            status="active",
            version=1,
            created_at=now,
            updated_at=now,
            last_login_at=now,
        )
        auth_session, issued = self._new_session(user)
        try:
            async with self.database.sessions.begin() as session:
                session.add(user)
                # The registration records reference users through foreign keys, but
                # there are no ORM relationships between these models.  MySQL can
                # therefore receive a child INSERT before the parent unless the user
                # row is flushed explicitly first.
                await session.flush()
                session.add_all(
                    [
                        Wishlist(
                            wishlist_id=uuid4().hex,
                            user_id=user.user_id,
                            name="我的心愿库",
                            is_default=True,
                            default_slot=1,
                            created_at=now,
                            updated_at=now,
                        ),
                        auth_session,
                        IdempotencyKey(
                            user_id=user.user_id,
                            client_request_id=client_request_id,
                            operation="register",
                            request_hash=request_hash,
                            response_json={"user_id": user.user_id},
                            response_status=201,
                            created_at=now,
                        ),
                    ]
                )
        except IntegrityError as exc:
            return await self._recover_registration(
                normalized,
                password,
                client_request_id,
                request_hash,
                exc,
            )
        return issued

    async def _recover_registration(
        self,
        normalized_email: str,
        password: str,
        client_request_id: str,
        request_hash: str,
        original_error: IntegrityError,
    ) -> IssuedSession:
        async with self.database.sessions.begin() as session:
            user = await session.scalar(
                select(User).where(User.email_normalized == normalized_email).with_for_update()
            )
            if user is None:
                raise ApiError(
                    500,
                    "REGISTRATION_FAILED",
                    "注册失败，请稍后重试",
                    retryable=True,
                ) from original_error
            idem = await session.get(
                IdempotencyKey,
                {
                    "user_id": user.user_id,
                    "client_request_id": client_request_id,
                    "operation": "register",
                },
            )
            if idem is None or idem.request_hash != request_hash:
                raise ApiError(
                    409, "EMAIL_ALREADY_REGISTERED", "该邮箱已经注册"
                ) from original_error
            try:
                self.passwords.verify(user.password_hash, password)
            except (VerifyMismatchError, InvalidHashError):
                raise ApiError(
                    409, "IDEMPOTENCY_KEY_REUSED", "该注册幂等键与原请求不一致"
                ) from None
            auth_session, issued = self._new_session(user)
            session.add(auth_session)
            user.last_login_at = utc_naive()
            user.updated_at = user.last_login_at
            return issued

    async def login(self, email: str, password: str) -> IssuedSession:
        normalized = normalize_email(email)
        await self._ensure_login_allowed(normalized)
        async with self.database.sessions.begin() as session:
            user = await session.scalar(
                select(User).where(User.email_normalized == normalized).with_for_update()
            )
            if user is None or user.status != "active":
                await self._record_failed_login(normalized)
                raise ApiError(401, "INVALID_CREDENTIALS", "邮箱或密码错误")
            try:
                self.passwords.verify(user.password_hash, password)
            except (VerifyMismatchError, InvalidHashError):
                await self._record_failed_login(normalized)
                raise ApiError(401, "INVALID_CREDENTIALS", "邮箱或密码错误") from None
            if self.passwords.check_needs_rehash(user.password_hash):
                user.password_hash = self.passwords.hash(password)
            user.last_login_at = utc_naive()
            user.updated_at = user.last_login_at
            auth_session, issued = self._new_session(user)
            session.add(auth_session)
        await self._clear_failed_logins(normalized)
        return issued

    def _login_key(self, normalized_email: str) -> str:
        return f"globuy:auth:failed:{_digest(normalized_email)}"

    async def _ensure_login_allowed(self, normalized_email: str) -> None:
        if not self.settings.redis_url:
            return
        try:
            from redis.asyncio import Redis

            async with Redis.from_url(self.settings.redis_url, decode_responses=True) as client:
                attempts = int(await client.get(self._login_key(normalized_email)) or 0)
        except Exception:
            return
        if attempts >= self.settings.auth_login_max_attempts:
            raise ApiError(
                429,
                "LOGIN_RATE_LIMITED",
                "登录尝试过于频繁，请稍后再试",
                retryable=True,
            )

    async def _record_failed_login(self, normalized_email: str) -> None:
        if not self.settings.redis_url:
            return
        try:
            from redis.asyncio import Redis

            async with Redis.from_url(self.settings.redis_url, decode_responses=True) as client:
                key = self._login_key(normalized_email)
                attempts = await client.incr(key)
                if attempts == 1:
                    await client.expire(key, self.settings.auth_login_window_seconds)
        except Exception:
            return

    async def _clear_failed_logins(self, normalized_email: str) -> None:
        if not self.settings.redis_url:
            return
        try:
            from redis.asyncio import Redis

            async with Redis.from_url(self.settings.redis_url) as client:
                await client.delete(self._login_key(normalized_email))
        except Exception:
            return

    async def authenticate(self, cookie_value: str | None) -> Principal:
        if not cookie_value or "." not in cookie_value:
            raise ApiError(401, "AUTH_REQUIRED", "请先登录")
        session_id, secret = cookie_value.split(".", 1)
        if not session_id or not secret:
            raise ApiError(401, "AUTH_REQUIRED", "登录会话无效")
        now = utc_naive()
        async with self.database.sessions.begin() as session:
            row = (
                await session.execute(
                    select(AuthSession, User)
                    .join(User, User.user_id == AuthSession.user_id)
                    .where(AuthSession.session_id == session_id)
                    .with_for_update()
                )
            ).one_or_none()
            if row is None:
                raise ApiError(401, "AUTH_REQUIRED", "登录会话无效")
            auth_session, user = row
            if (
                auth_session.revoked_at is not None
                or auth_session.expires_at <= now
                or user.status != "active"
                or not hmac.compare_digest(auth_session.token_hash, _digest(secret))
            ):
                raise ApiError(401, "AUTH_REQUIRED", "登录会话已失效")
            auth_session.last_seen_at = now
            return Principal(
                user_id=user.user_id,
                email=user.email_normalized,
                display_name=user.display_name,
                session_id=auth_session.session_id,
                csrf_token_hash=auth_session.csrf_token_hash,
            )

    async def logout(self, principal: Principal) -> None:
        async with self.database.sessions.begin() as session:
            auth_session = await session.get(AuthSession, principal.session_id)
            if auth_session is not None and auth_session.revoked_at is None:
                auth_session.revoked_at = utc_naive()

    @staticmethod
    def verify_csrf(
        principal: Principal, cookie_token: str | None, header_token: str | None
    ) -> None:
        if (
            not cookie_token
            or not header_token
            or not hmac.compare_digest(cookie_token, header_token)
            or not hmac.compare_digest(principal.csrf_token_hash, _digest(header_token))
        ):
            raise ApiError(403, "CSRF_FAILED", "请求安全令牌无效")
