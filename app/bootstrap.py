import json
import logging
import os
import time
from uuid import UUID

import certifi
import psycopg
from azure.identity import ManagedIdentityCredential
from psycopg import sql

from app.database import PG_SCOPE

logger = logging.getLogger("bootstrap")

SCHEMA = """
CREATE SCHEMA IF NOT EXISTS demo;
REVOKE ALL ON SCHEMA demo FROM PUBLIC;
CREATE TABLE IF NOT EXISTS demo.employees (
    tenant_id uuid NOT NULL,
    object_id uuid NOT NULL,
    display_name text NOT NULL,
    department text NOT NULL,
    job_title text NOT NULL,
    office_location text NOT NULL,
    private_salary integer NOT NULL,
    PRIMARY KEY (tenant_id, object_id)
);
CREATE TABLE IF NOT EXISTS demo.requests (
    tenant_id uuid NOT NULL,
    object_id uuid NOT NULL,
    request_id uuid NOT NULL,
    category text NOT NULL CHECK (category IN ('IT', 'Facilities')),
    summary text NOT NULL CHECK (length(summary) BETWEEN 5 AND 200),
    status text NOT NULL DEFAULT 'Open',
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, object_id, request_id)
);
ALTER TABLE demo.employees ENABLE ROW LEVEL SECURITY;
ALTER TABLE demo.employees FORCE ROW LEVEL SECURITY;
ALTER TABLE demo.requests ENABLE ROW LEVEL SECURITY;
ALTER TABLE demo.requests FORCE ROW LEVEL SECURITY;
"""

POLICY = """
tenant_id = NULLIF(current_setting('mcp.tenant_id', true), '')::uuid
AND object_id = NULLIF(current_setting('mcp.object_id', true), '')::uuid
"""


def provision(conn, tenant: str, user: str, principals: list[dict], local: bool = False):
    with conn:
        conn.execute(SCHEMA)
        # Bootstrap owns the schema, not the runtime. Allow the owner to seed synthetic rows.
        for table in ("employees", "requests"):
            conn.execute(
                sql.SQL("DROP POLICY IF EXISTS bootstrap_owner ON demo.{}").format(
                    sql.Identifier(table)
                )
            )
            conn.execute(
                sql.SQL(
                    "CREATE POLICY bootstrap_owner ON demo.{} TO {} USING (true) WITH CHECK (true)"
                ).format(sql.Identifier(table), sql.Identifier(conn.info.user))
            )
            conn.execute(
                sql.SQL("DROP POLICY IF EXISTS caller_isolation ON demo.{}").format(
                    sql.Identifier(table)
                )
            )
            conn.execute(
                sql.SQL(
                    "CREATE POLICY caller_isolation ON demo.{} USING ({}) WITH CHECK ({})"
                ).format(sql.Identifier(table), sql.SQL(POLICY), sql.SQL(POLICY))
            )
        conn.execute(
            "INSERT INTO demo.employees VALUES (%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (tenant_id,object_id) DO NOTHING",
            (
                tenant,
                user,
                "Synthetic Employee",
                "Demo Engineering",
                "Demo Analyst",
                "Demo Office",
                100000,
            ),
        )
        conn.execute(
            "INSERT INTO demo.employees VALUES (%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (tenant_id,object_id) DO NOTHING",
            (
                tenant,
                "33333333-3333-3333-3333-333333333333",
                "Other Synthetic Employee",
                "Demo Finance",
                "Demo Analyst",
                "Other Office",
                200000,
            ),
        )
        for principal in principals:
            name = principal["name"]
            exists = conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (name,)).fetchone()
            if not exists:
                if local:
                    conn.execute(
                        sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                            sql.Identifier(name), sql.Literal(os.environ["LOCAL_RUNTIME_PASSWORD"])
                        )
                    )
                else:
                    conn.execute(
                        "SELECT * FROM pgaadauth_create_principal_with_oid(%s,%s,"
                        "'service',false,false)",
                        (name, str(UUID(principal["oid"]))),
                    )
            conn.execute(sql.SQL("GRANT USAGE ON SCHEMA demo TO {}").format(sql.Identifier(name)))
            conn.execute(
                sql.SQL(
                    "GRANT SELECT (tenant_id,object_id,display_name,department,job_title,"
                    "office_location) ON demo.employees TO {}"
                ).format(sql.Identifier(name))
            )
            conn.execute(
                sql.SQL("GRANT SELECT, INSERT ON demo.requests TO {}").format(sql.Identifier(name))
            )
            conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(name)))
            conn.execute(
                "SELECT set_config('mcp.tenant_id','',true), set_config('mcp.object_id','',true)"
            )
            count = conn.execute("SELECT count(*) FROM demo.employees").fetchone()[0]
            if count != 0:
                raise RuntimeError(f"RLS failed closed-context check for {name}")
            conn.execute(
                "SELECT set_config('mcp.tenant_id',%s,true), set_config('mcp.object_id',%s,true)",
                (tenant, user),
            )
            count = conn.execute("SELECT count(*) FROM demo.employees").fetchone()[0]
            if count != 1:
                raise RuntimeError(f"RLS verification failed for {name}: visible count={count}")
            hidden = conn.execute(
                "SELECT count(*) FROM demo.employees WHERE object_id=%s",
                ("33333333-3333-3333-3333-333333333333",),
            ).fetchone()[0]
            if hidden != 0:
                raise RuntimeError(f"Cross-employee RLS verification failed for {name}")
            logger.info("PASS missing-context and cross-employee RLS for %s", name)
            try:
                with conn.transaction():
                    conn.execute("SELECT private_salary FROM demo.employees")
            except psycopg.errors.InsufficientPrivilege:
                logger.info("PASS salary column denied for %s", name)
            else:
                raise RuntimeError("Sensitive column unexpectedly accessible")
            conn.execute("RESET ROLE")


def main():
    logging.basicConfig(level=logging.INFO)
    logging.getLogger("azure").setLevel(logging.WARNING)
    tenant = str(UUID(os.environ["TENANT_ID"]))
    user = str(UUID(os.environ["DEMO_USER_OID"]))
    local = os.environ.get("APP_MODE") == "local"
    credential = (
        None if local else ManagedIdentityCredential(client_id=os.environ["AZURE_CLIENT_ID"])
    )
    try:
        password = os.environ["PGPASSWORD"] if local else credential.get_token(PG_SCOPE).token
        for attempt in range(12):
            try:
                conn = psycopg.connect(
                    host=os.environ["PGHOST"],
                    dbname=os.environ.get("PGDATABASE", "postgres"),
                    user=os.environ["PGUSER"],
                    password=password,
                    connect_timeout=10,
                    sslmode="disable" if local else "verify-full",
                    sslrootcert=None if local else certifi.where(),
                )
                break
            except psycopg.OperationalError:
                logger.warning("Database not ready; connection attempt %s/12", attempt + 1)
                if attempt == 11:
                    raise
                time.sleep(10)
        provision(conn, tenant, user, json.loads(os.environ["RUNTIME_PRINCIPALS"]), local)
        logger.info("Bootstrap completed")
    finally:
        if credential:
            credential.close()


if __name__ == "__main__":
    main()
