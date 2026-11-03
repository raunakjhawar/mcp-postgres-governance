import time
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from app.auth import LOCAL_OBJECT_ID, TokenVerifier
from app.config import Settings
from app.database import Database
from app.server import create_app

TENANT = "11111111-1111-1111-1111-111111111111"
AUDIENCE = "44444444-4444-4444-4444-444444444444"
SETTINGS = Settings(
    mode="azure",
    tenant_id=TENANT,
    audience=AUDIENCE,
    public_url="https://test.example",
    db_host="unused",
    db_name="postgres",
    db_user="runtime",
    identity_client_id="unused",
    local_password="",
)


class FakeDatabase:
    def __init__(self):
        self.seen = []
        self.created = {}
        self.starts = 0
        self.closes = 0

    async def start(self):
        self.starts += 1

    async def close(self):
        self.closes += 1

    async def profile(self, principal):
        self.seen.append(principal)
        return {
            "display_name": "Synthetic Employee",
            "department": "Demo Engineering",
            "job_title": "Demo Analyst",
            "office_location": "Demo Office",
        }

    async def requests(self, principal):
        self.seen.append(principal)
        return []

    async def create_request(self, principal, request_id, category, summary):
        self.seen.append(principal)
        key = (principal.tenant_id, principal.object_id, request_id)
        if key in self.created and self.created[key] != (category, summary):
            raise ValueError("Idempotency key already belongs to different request parameters")
        created = key not in self.created
        self.created[key] = (category, summary)
        return {"request_id": str(request_id), "created": created}


@pytest.fixture(scope="module")
def signing_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def verifier(signing_key):
    result = TokenVerifier(SETTINGS)
    result.keys = SimpleNamespace(
        get_signing_key_from_jwt=lambda _token: SimpleNamespace(key=signing_key.public_key())
    )
    return result


def token(signing_key, **overrides):
    claims = {
        "aud": AUDIENCE,
        "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0",
        "tid": TENANT,
        "oid": LOCAL_OBJECT_ID,
        "iat": int(time.time()) - 10,
        "nbf": int(time.time()) - 10,
        "exp": int(time.time()) + 600,
        "scp": "access_as_user",
        "roles": ["Reader"],
    }
    claims.update(overrides)
    return jwt.encode(claims, signing_key, algorithm="RS256")


@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "wrong"},
        {"iss": "https://untrusted.example"},
        {"exp": 1},
        {"tid": "55555555-5555-5555-5555-555555555555"},
        {"scp": "unrelated"},
        {"roles": []},
        {"oid": "not-a-uuid"},
    ],
)
def test_invalid_claims_rejected(signing_key, overrides):
    with pytest.raises(jwt.PyJWTError):
        verifier(signing_key).verify(token(signing_key, **overrides))


def test_valid_token(signing_key):
    principal = verifier(signing_key).verify(token(signing_key))
    assert principal.object_id == LOCAL_OBJECT_ID
    assert principal.roles == frozenset({"Reader"})


async def test_health_metadata_and_anonymous_denial(signing_key):
    app = create_app(SETTINGS, FakeDatabase(), verifier(signing_key))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url=SETTINGS.public_url
    ) as client:
        assert (await client.get("/health")).json()["mode"] == "azure"
        metadata = (await client.get("/.well-known/oauth-protected-resource")).json()
        assert metadata["resource"] == SETTINGS.public_url + "/mcp"
        response = await client.get("/mcp")
        assert response.status_code == 401
        assert "resource_metadata=" in response.headers["www-authenticate"]
        response = await client.get("/mcp", headers={"Authorization": "Bearer invalid"})
        assert response.status_code == 401


@pytest.mark.parametrize("role", ["Reader", "Operator"])
@pytest.mark.parametrize("origin", [None, "https://gateway.example"])
async def test_real_mcp_protocol_and_tool_permissions(signing_key, role, origin):
    db = FakeDatabase()
    app = create_app(
        replace(SETTINGS, gateway_url="https://gateway.example"), db, verifier(signing_key)
    )
    headers = {"Authorization": "Bearer " + token(signing_key, roles=[role])}
    if origin:
        headers["Origin"] = origin
    async with app.app.router.lifespan_context(app.app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            headers=headers,
        ) as client:
            async with streamable_http_client(SETTINGS.public_url + "/mcp", http_client=client) as (
                read,
                write,
                _,
            ):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    assert db.starts == 1
                    assert db.closes == 0
                    tools = await session.list_tools()
                    assert len(tools.tools) == 3
                    profile = await session.call_tool(
                        "get_my_employee_profile", {"object_id": str(uuid4())}
                    )
                    assert not profile.isError
                    assert profile.structuredContent["display_name"] == "Synthetic Employee"
                    assert db.seen[-1].object_id == LOCAL_OBJECT_ID
                    assert "private_salary" not in profile.structuredContent
                    args = {
                        "request_id": str(uuid4()),
                        "category": "IT",
                        "summary": "Synthetic test request",
                    }
                    write_result = await session.call_tool("create_support_request", args)
                    assert write_result.isError == (role == "Reader")
                    if role == "Reader":
                        assert not db.created
                    else:
                        assert write_result.structuredContent["created"]
                        duplicate = await session.call_tool("create_support_request", args)
                        assert duplicate.structuredContent["created"] is False
                        changed = await session.call_tool(
                            "create_support_request", {**args, "summary": "Different test request"}
                        )
                        assert changed.isError
                    oversized = await session.call_tool(
                        "create_support_request", {**args, "summary": "x" * 201}
                    )
                    assert oversized.isError
                    assert db.closes == 0
    assert db.closes == 1


async def test_rate_limit(signing_key):
    app = create_app(
        replace(SETTINGS, requests_per_minute=1), FakeDatabase(), verifier(signing_key)
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url=SETTINGS.public_url,
        headers={"Authorization": "Bearer " + token(signing_key)},
    ) as client:
        await client.get("/unknown")
        response = await client.get("/unknown")
        assert response.status_code == 429
        assert response.headers["Retry-After"] == "60"


async def test_expired_rate_buckets_are_reclaimed(signing_key):
    app = create_app(SETTINGS, FakeDatabase(), verifier(signing_key))
    app.buckets = {str(index): (-1, 1) for index in range(10000)}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app),
        base_url=SETTINGS.public_url,
        headers={"Authorization": "Bearer " + token(signing_key)},
    ) as client:
        assert (await client.get("/unknown")).status_code == 404
        assert len(app.buckets) == 1


def test_local_auth_forbidden_on_azure(monkeypatch):
    monkeypatch.setenv("APP_MODE", "local")
    monkeypatch.setenv("WEBSITE_INSTANCE_ID", "azure-instance")
    with pytest.raises(ValueError, match="forbidden"):
        Settings.from_env()


def test_local_mode_requires_loopback(monkeypatch):
    monkeypatch.setenv("APP_MODE", "local")
    monkeypatch.delenv("WEBSITE_INSTANCE_ID", raising=False)
    monkeypatch.setenv("PUBLIC_URL", "https://public.example")
    with pytest.raises(ValueError, match="loopback"):
        Settings.from_env()


async def test_azure_database_credential_initializes(monkeypatch):
    monkeypatch.setenv("IDENTITY_ENDPOINT", "http://127.0.0.1:12345/token")
    monkeypatch.setenv("IDENTITY_HEADER", "synthetic-test-header")
    database = Database(SETTINGS)
    await database.close()


async def test_unapproved_origin_rejected(signing_key):
    app = create_app(
        replace(SETTINGS, gateway_url="https://gateway.example"),
        FakeDatabase(),
        verifier(signing_key),
    )
    async with app.app.router.lifespan_context(app.app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url=SETTINGS.public_url,
            headers={
                "Authorization": "Bearer " + token(signing_key),
                "Origin": "https://untrusted.example",
            },
        ) as client:
            assert (await client.get("/mcp")).status_code == 403
