import asyncio
from pathlib import Path

import pytest
import agent_runtime_platform.postgres as postgres_module

from agent_runtime_platform import (
    LeaseGrant,
    PostgresRuntimeBackend,
    RuntimeBackend,
    StaleLeaseWriteError,
)
from agent_runtime_platform.postgres import _canonical_json


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeCursor:
    def __init__(self, *, row=None, rowcount=1):
        self.row = row
        self.rowcount = rowcount
        self.executions = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def execute(self, sql, parameters):
        self.executions.append((sql, parameters))

    async def fetchone(self):
        return self.row


class FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    def cursor(self):
        return self._cursor

    async def commit(self):
        self.commits += 1


def attach_connection(backend, connection):
    async def connect():
        return connection

    backend._connect = connect


def test_postgres_migration_owns_only_runtime_namespace():
    sql = (PROJECT_ROOT / "migrations" / "0001_postgres_runtime.sql").read_text(
        encoding="utf-8"
    )

    assert "CREATE TABLE IF NOT EXISTS runtime_checkpoints" in sql
    assert "CREATE TABLE IF NOT EXISTS runtime_run_leases" in sql
    assert "CREATE TABLE IF NOT EXISTS runtime_tool_results" in sql
    assert "fencing_token BIGINT" in sql
    assert "ALTER TABLE analysis_jobs" not in sql
    assert "CREATE TABLE analysis_jobs" not in sql


def test_postgres_backend_is_structurally_runtime_backend():
    backend = PostgresRuntimeBackend("postgresql://example.invalid/runtime")

    assert isinstance(backend, RuntimeBackend)


def test_postgres_backend_rejects_blank_dsn():
    with pytest.raises(ValueError, match="must not be blank"):
        PostgresRuntimeBackend("  ")


@pytest.mark.parametrize(
    ("options", "message"),
    [
        ({"pool_min_size": -1}, "pool_min_size"),
        ({"pool_min_size": 2, "pool_max_size": 1}, "pool_max_size"),
        ({"pool_timeout_seconds": 0}, "pool_timeout_seconds"),
    ],
)
def test_postgres_backend_rejects_invalid_pool_configuration(options, message):
    with pytest.raises(ValueError, match=message):
        PostgresRuntimeBackend("postgresql://example.invalid/runtime", **options)


def test_postgres_backend_explicit_pool_lifecycle(monkeypatch):
    created = []

    class FakePool:
        def __init__(self, conninfo, **kwargs):
            self.conninfo = conninfo
            self.kwargs = kwargs
            self.open_calls = []
            self.close_calls = 0
            self.acquire_calls = 0
            created.append(self)

        async def open(self, *, wait, timeout):
            self.open_calls.append((wait, timeout))

        async def close(self):
            self.close_calls += 1

        def connection(self):
            self.acquire_calls += 1
            return FakeConnection(FakeCursor(row=None))

    monkeypatch.setattr(postgres_module, "_load_psycopg_pool", lambda: FakePool)
    backend = PostgresRuntimeBackend(
        "postgresql://example.invalid/runtime",
        pool_min_size=2,
        pool_max_size=5,
        pool_timeout_seconds=7,
    )

    async def exercise():
        async with backend as active:
            assert active is backend
            assert await backend.load_latest("run-1") is None

    asyncio.run(exercise())

    pool = created[0]
    assert pool.conninfo == backend.dsn
    assert pool.kwargs["min_size"] == 2
    assert pool.kwargs["max_size"] == 5
    assert pool.kwargs["timeout"] == 7
    assert pool.kwargs["open"] is False
    assert pool.open_calls == [(True, 7)]
    assert pool.acquire_calls == 1
    assert pool.close_calls == 1
    assert backend._pool is None


def test_postgres_checkpoint_save_and_load_contract():
    backend = PostgresRuntimeBackend("postgresql://example.invalid/runtime")
    state = {"run_id": "00000000-0000-0000-0000-000000000001", "status": "running"}
    _, digest = _canonical_json(state)

    save_cursor = FakeCursor(row={"state_sha256": digest})
    save_connection = FakeConnection(save_cursor)
    attach_connection(backend, save_connection)
    saved_digest = asyncio.run(
        backend.save_checkpoint(
            state["run_id"],
            -1,
            state,
            lease_grant=LeaseGrant("worker-a", 3),
        )
    )

    assert saved_digest == digest
    assert "runtime_checkpoints" in save_cursor.executions[0][0]
    assert "fencing_token" in save_cursor.executions[0][0]
    assert save_cursor.executions[0][1][-1] == 3
    assert save_connection.commits == 1

    load_cursor = FakeCursor(row={"state": state, "state_sha256": digest})
    attach_connection(backend, FakeConnection(load_cursor))
    loaded = asyncio.run(backend.load_latest(state["run_id"]))

    assert loaded == state


def test_postgres_lease_returns_fencing_grant_and_uses_token_on_renewal():
    backend = PostgresRuntimeBackend("postgresql://example.invalid/runtime")
    acquire_cursor = FakeCursor(row={"owner_id": "worker-a", "fencing_token": 7})
    attach_connection(backend, FakeConnection(acquire_cursor))

    grant = asyncio.run(
        backend.acquire_lease("00000000-0000-0000-0000-000000000001", "worker-a", ttl_seconds=30)
    )

    assert grant is not None
    assert grant.fencing_token == 7
    assert "runtime_run_leases" in acquire_cursor.executions[0][0]
    assert "fencing_token" in acquire_cursor.executions[0][0]

    renew_cursor = FakeCursor(rowcount=1)
    attach_connection(backend, FakeConnection(renew_cursor))
    renewed = asyncio.run(
        backend.renew_lease(
            "00000000-0000-0000-0000-000000000001",
            "worker-a",
            ttl_seconds=30,
            fencing_token=7,
        )
    )

    assert renewed is True
    assert renew_cursor.executions[0][1][-1] == 7


def test_postgres_checkpoint_rejects_stale_lease():
    backend = PostgresRuntimeBackend("postgresql://example.invalid/runtime")
    cursor = FakeCursor(row=None)
    attach_connection(backend, FakeConnection(cursor))

    with pytest.raises(StaleLeaseWriteError, match="stale checkpoint"):
        asyncio.run(
            backend.save_checkpoint(
                "00000000-0000-0000-0000-000000000001",
                0,
                {"status": "running"},
                lease_grant=LeaseGrant("old-worker", 1),
            )
        )


def test_postgres_tool_trace_uses_generation_fence_without_wall_clock_expiry():
    backend = PostgresRuntimeBackend("postgresql://example.invalid/runtime")
    cursor = FakeCursor(row={"idempotency_key": "run-1:0:0"})
    connection = FakeConnection(cursor)
    attach_connection(backend, connection)

    asyncio.run(
        backend.save_tool_trace(
            "run-1:0:0",
            {"status": "succeeded"},
            run_id="run-1",
            lease_grant=LeaseGrant("worker-a", 4),
        )
    )

    sql, parameters = cursor.executions[0]
    fence_clause = sql.split("WHERE EXISTS", 1)[1].split("ON CONFLICT", 1)[0]
    assert "owner_id = %s" in fence_clause
    assert "fencing_token = %s" in fence_clause
    assert "expires_at" not in fence_clause
    assert parameters[-2:] == ("worker-a", 4)
    assert connection.commits == 1


def test_postgres_tool_trace_rejects_stale_lease():
    backend = PostgresRuntimeBackend("postgresql://example.invalid/runtime")
    cursor = FakeCursor(row=None)
    attach_connection(backend, FakeConnection(cursor))

    with pytest.raises(StaleLeaseWriteError, match="stale tool trace"):
        asyncio.run(
            backend.save_tool_trace(
                "run-1:0:0",
                {"status": "succeeded"},
                run_id="run-1",
                lease_grant=LeaseGrant("old-worker", 1),
            )
        )
