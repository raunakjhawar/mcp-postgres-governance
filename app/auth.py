import asyncio
import logging
import time
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import UUID, uuid4

import jwt
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import Settings

logger = logging.getLogger("mcp.audit")


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    object_id: str
    roles: frozenset[str]


current_principal: ContextVar[Principal] = ContextVar("principal")
LOCAL_OBJECT_ID = "22222222-2222-2222-2222-222222222222"


class TokenVerifier:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.issuer = f"https://login.microsoftonline.com/{settings.tenant_id}/v2.0"
        self.keys = jwt.PyJWKClient(
            f"https://login.microsoftonline.com/{settings.tenant_id}/discovery/v2.0/keys",
            timeout=5,
        )

    def verify(self, token: str) -> Principal:
        key = self.keys.get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            key.key,
            algorithms=["RS256"],
            audience=self.settings.audience,
            issuer=self.issuer,
            options={"require": ["exp", "iat", "nbf", "aud", "iss", "tid", "oid"]},
        )
        if claims["tid"] != self.settings.tenant_id:
            raise jwt.InvalidTokenError("Tenant mismatch")
        if "access_as_user" not in claims.get("scp", "").split():
            raise jwt.InvalidTokenError("Delegated scope required")
        roles = frozenset(claims.get("roles", []))
        if not roles.intersection({"Reader", "Operator"}):
            raise jwt.InvalidTokenError("Assigned application role required")
        try:
            return Principal(str(UUID(claims["tid"])), str(UUID(claims["oid"])), roles)
        except (ValueError, TypeError, AttributeError) as error:
            raise jwt.InvalidTokenError("Invalid identity claims") from error


class SecurityMiddleware:
    def __init__(self, app: ASGIApp, settings: Settings, verifier: TokenVerifier):
        self.app, self.settings, self.verifier = app, settings, verifier
        self.buckets: dict[str, tuple[int, int]] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        request = Request(scope)
        correlation = str(uuid4())
        path = request.url.path
        if path in {"/health", "/.well-known/oauth-protected-resource"}:
            await self.app(scope, receive, send)
            return
        if self.settings.mode == "local":
            principal = Principal(
                self.settings.tenant_id, LOCAL_OBJECT_ID, frozenset({"Reader", "Operator"})
            )
        else:
            scheme, _, token = request.headers.get("authorization", "").partition(" ")
            try:
                if scheme.lower() != "bearer" or not token:
                    raise jwt.InvalidTokenError("Missing bearer token")
                principal = await asyncio.to_thread(self.verifier.verify, token)
            except jwt.PyJWTError as error:
                logger.warning(
                    "authentication_denied correlation=%s reason=%s",
                    correlation,
                    type(error).__name__,
                )
                response = JSONResponse(
                    {"error": "unauthorized", "correlation_id": correlation},
                    status_code=401,
                    headers={
                        "WWW-Authenticate": (
                            'Bearer resource_metadata="'
                            f'{self.settings.public_url}/.well-known/oauth-protected-resource"'
                        )
                    },
                )
                await response(scope, receive, send)
                return
        minute = int(time.monotonic() // 60)
        if len(self.buckets) >= 10000:
            self.buckets = {k: v for k, v in self.buckets.items() if v[0] == minute}
        key = f"{principal.tenant_id}:{principal.object_id}"
        old_minute, count = self.buckets.get(key, (minute, 0))
        count = count + 1 if old_minute == minute else 1
        if count > self.settings.requests_per_minute or (
            key not in self.buckets and len(self.buckets) >= 10000
        ):
            logger.warning("rate_limit correlation=%s subject=%s", correlation, key)
            await JSONResponse(
                {"error": "rate_limited"}, status_code=429, headers={"Retry-After": "60"}
            )(scope, receive, send)
            return
        self.buckets[key] = (minute, count)
        context = current_principal.set(principal)
        started = time.monotonic()
        try:
            await self.app(scope, receive, send)
        finally:
            current_principal.reset(context)
            logger.info(
                "request correlation=%s subject=%s path=%s duration_ms=%.0f",
                correlation,
                key,
                path,
                (time.monotonic() - started) * 1000,
            )
