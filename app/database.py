import asyncio
import logging
import time
from contextlib import asynccontextmanager
from uuid import UUID

import certifi
import psycopg
from azure.identity.aio import ManagedIdentityCredential
from psycopg.rows import dict_row

from app.auth import Principal
from app.config import Settings

PG_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"
logger = logging.getLogger("mcp.audit")


class Database:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.capacity = asyncio.Semaphore(4)
        self.credential = (
            ManagedIdentityCredential(client_id=settings.identity_client_id)
            if settings.mode == "azure"
            else None
        )

    async def start(self):
        if self.credential:
            started = time.monotonic()
            async with asyncio.timeout(60):
                await self.credential.get_token(PG_SCOPE)
            logger.info(
                "database_identity_ready duration_ms=%.0f", (time.monotonic() - started) * 1000
            )

    async def close(self):
        if self.credential:
            await self.credential.close()

    @asynccontextmanager
    async def connection(self, principal: Principal):
        async with asyncio.timeout(12), self.capacity:
            password = (
                (await self.credential.get_token(PG_SCOPE)).token
                if self.credential
                else self.settings.local_password
            )
            conn = await psycopg.AsyncConnection.connect(
                host=self.settings.db_host,
                dbname=self.settings.db_name,
                user=self.settings.db_user,
                password=password,
                sslmode="verify-full" if self.settings.mode == "azure" else "disable",
                sslrootcert=certifi.where() if self.settings.mode == "azure" else None,
                connect_timeout=5,
                row_factory=dict_row,
            )
            async with conn:
                await conn.execute("SELECT set_config('statement_timeout', '5000', true)")
                await conn.execute("SELECT set_config('lock_timeout', '2000', true)")
                await conn.execute(
                    "SELECT set_config('mcp.tenant_id', %s, true), "
                    "set_config('mcp.object_id', %s, true)",
                    (principal.tenant_id, principal.object_id),
                )
                yield conn

    async def profile(self, principal: Principal):
        async with self.connection(principal) as conn:
            cursor = await conn.execute(
                "SELECT display_name, department, job_title, office_location "
                "FROM demo.employees WHERE tenant_id = %s AND object_id = %s",
                (principal.tenant_id, principal.object_id),
            )
            return await cursor.fetchone()

    async def requests(self, principal: Principal):
        async with self.connection(principal) as conn:
            cursor = await conn.execute(
                "SELECT request_id, category, summary, status FROM demo.requests "
                "WHERE tenant_id = %s AND object_id = %s ORDER BY created_at DESC LIMIT 20",
                (principal.tenant_id, principal.object_id),
            )
            rows = await cursor.fetchall()
            return [{**row, "request_id": str(row["request_id"])} for row in rows]

    async def create_request(
        self, principal: Principal, request_id: UUID, category: str, summary: str
    ):
        async with self.connection(principal) as conn:
            cursor = await conn.execute(
                "INSERT INTO demo.requests "
                "(tenant_id, object_id, request_id, category, summary) "
                "VALUES (%s, %s, %s, %s, %s) "
                "ON CONFLICT (tenant_id, object_id, request_id) DO NOTHING "
                "RETURNING request_id",
                (principal.tenant_id, principal.object_id, request_id, category, summary),
            )
            inserted = await cursor.fetchone()
            cursor = await conn.execute(
                "SELECT category, summary FROM demo.requests "
                "WHERE tenant_id = %s AND object_id = %s AND request_id = %s",
                (principal.tenant_id, principal.object_id, request_id),
            )
            existing = await cursor.fetchone()
            if not existing or existing["category"] != category or existing["summary"] != summary:
                raise ValueError("Idempotency key already belongs to different request parameters")
            return {"request_id": str(request_id), "created": inserted is not None}
