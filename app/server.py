import logging
from contextlib import asynccontextmanager
from typing import Annotated, Literal
from urllib.parse import urlparse
from uuid import UUID

import psycopg
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field
from starlette.responses import JSONResponse
from starlette.routing import Route

from app.auth import SecurityMiddleware, TokenVerifier, current_principal
from app.config import Settings
from app.database import Database

logger = logging.getLogger("mcp.audit")


def create_app(settings: Settings | None = None, database=None, verifier=None):
    settings = settings or Settings.from_env()
    database = database or Database(settings)
    hostname = urlparse(settings.public_url).hostname

    mcp = FastMCP(
        "Azure PostgreSQL Security Demo",
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[hostname, f"{hostname}:443", "127.0.0.1:*", "localhost:*"],
            allowed_origins=[settings.public_url]
            + ([settings.gateway_url] if settings.gateway_url else []),
        ),
    )

    async def run_query(operation):
        try:
            return await operation
        except (psycopg.Error, TimeoutError) as error:
            logger.error(
                "database_failure error_type=%s sqlstate=%s",
                type(error).__name__,
                getattr(error, "sqlstate", None),
            )
            raise ToolError("Database unavailable or query budget exceeded") from error

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def get_my_employee_profile() -> dict[str, str]:
        """Return your synthetic profile. Identity comes from authentication, not arguments."""
        profile = await run_query(database.profile(current_principal.get()))
        if profile is None:
            raise ToolError("No demo profile provisioned for this identity")
        return profile

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    async def list_my_requests() -> list[dict[str, str]]:
        """Return at most 20 synthetic support requests belonging to the caller."""
        return await run_query(database.requests(current_principal.get()))

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False})
    async def create_support_request(
        request_id: UUID,
        category: Literal["IT", "Facilities"],
        summary: Annotated[str, Field(min_length=5, max_length=200)],
    ) -> dict[str, str | bool]:
        """Create a synthetic request. Requires Operator role and an idempotency UUID."""
        principal = current_principal.get()
        if "Operator" not in principal.roles:
            logger.warning(
                "tool_denied tool=create_support_request subject=%s", principal.object_id
            )
            raise ToolError("Operator role required")
        try:
            return await run_query(
                database.create_request(principal, request_id, category, summary)
            )
        except ValueError as error:
            raise ToolError(str(error)) from error

    async def health(_request):
        return JSONResponse({"status": "alive", "mode": settings.mode})

    async def metadata(_request):
        return JSONResponse(
            {
                "resource": f"{settings.public_url}/mcp",
                "authorization_servers": [
                    f"https://login.microsoftonline.com/{settings.tenant_id}/v2.0"
                ],
                "scopes_supported": [f"api://{settings.audience}/access_as_user"],
                "bearer_methods_supported": ["header"],
            }
        )

    app = mcp.streamable_http_app()
    transport_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def application_lifespan(starlette_app):
        try:
            await database.start()
            async with transport_lifespan(starlette_app):
                yield
        finally:
            await database.close()

    app.router.lifespan_context = application_lifespan
    app.routes.insert(0, Route("/health", health))
    app.routes.insert(0, Route("/.well-known/oauth-protected-resource", metadata))
    return SecurityMiddleware(app, settings, verifier or TokenVerifier(settings))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    logging.getLogger("azure").setLevel(logging.WARNING)
    settings = Settings.from_env()
    uvicorn.run(
        create_app(settings),
        host="127.0.0.1" if settings.mode == "local" else "0.0.0.0",
        port=8000,
        access_log=False,
    )
