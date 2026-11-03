import os
from uuid import uuid4

import psycopg
import pytest

from app.bootstrap import provision


def test_postgres_permissions_and_rls(monkeypatch):
    dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("Set TEST_POSTGRES_DSN to an empty disposable PostgreSQL 16 database")
    password = uuid4().hex
    role = "test_mcp_" + uuid4().hex[:12]
    monkeypatch.setenv("LOCAL_RUNTIME_PASSWORD", password)
    tenant = "11111111-1111-1111-1111-111111111111"
    user = "22222222-2222-2222-2222-222222222222"
    provision(psycopg.connect(dsn), tenant, user, [{"name": role}], local=True)
    with psycopg.connect(dsn, user=role, password=password) as conn:
        assert conn.execute("SELECT count(*) FROM demo.employees").fetchone()[0] == 0
        conn.execute(
            "SELECT set_config('mcp.tenant_id',%s,true), set_config('mcp.object_id',%s,true)",
            (tenant, user),
        )
        assert conn.execute("SELECT count(*) FROM demo.employees").fetchone()[0] == 1
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute("SELECT private_salary FROM demo.employees")
        request = str(uuid4())
        conn.execute(
            "INSERT INTO demo.requests (tenant_id,object_id,request_id,category,summary) "
            "VALUES (%s,%s,%s,'IT','Synthetic request')",
            (tenant, user, request),
        )
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute(
                "INSERT INTO demo.requests (tenant_id,object_id,request_id,category,summary) "
                "VALUES (%s,%s,%s,'IT','Other synthetic request')",
                (tenant, "33333333-3333-3333-3333-333333333333", str(uuid4())),
            )
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            conn.execute("DELETE FROM demo.employees")
