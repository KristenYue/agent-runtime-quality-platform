"""Optional real PostgreSQL lifecycle test for a dedicated test database."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest

from agent_runtime_platform import (
    AgentRuntime,
    PostgresRuntimeBackend,
    RunStatus,
    StaleLeaseWriteError,
    ToolRegistry,
)


pytestmark = pytest.mark.integration
PROJECT_ROOT = Path(__file__).resolve().parents[2]


class FinishPlanner:
    async def decide(self, *, request, observations, step):
        return {"kind": "finish", "result": {"request": request}}


def test_real_postgres_runtime_lifecycle():
    dsn = os.getenv("RUNTIME_DATABASE_URL", "").strip()
    if not dsn:
        pytest.skip("set RUNTIME_DATABASE_URL for real PostgreSQL integration")
    asyncio.run(_exercise(dsn))


async def _exercise(dsn: str) -> None:
    import psycopg

    migration = (PROJECT_ROOT / "migrations" / "0001_postgres_runtime.sql").read_text(
        encoding="utf-8"
    )
    connection = await psycopg.AsyncConnection.connect(dsn)
    async with connection:
        async with connection.cursor() as cursor:
            await cursor.execute(migration, prepare=False)
        await connection.commit()

    backend_a = PostgresRuntimeBackend(dsn, pool_min_size=1, pool_max_size=2)
    backend_b = PostgresRuntimeBackend(dsn, pool_min_size=1, pool_max_size=2)
    takeover_run_id = str(uuid4())
    async with backend_a, backend_b:
        runtime = AgentRuntime(
            planner=FinishPlanner(),
            tools=ToolRegistry([]),
            backend=backend_a,
        )
        result = await runtime.run(task_id="postgres-integration", request="hello")
        loaded = await runtime.get(result.run_id)
        assert result.status == RunStatus.COMPLETED
        assert loaded == result

        old_grant = await backend_a.acquire_lease(
            takeover_run_id, "worker-old", ttl_seconds=30
        )
        assert old_grant is not None
        assert await backend_a.release_lease(
            takeover_run_id,
            old_grant.owner_id,
            fencing_token=old_grant.fencing_token,
        )

        grants = await asyncio.gather(
            backend_a.acquire_lease(
                takeover_run_id, "worker-new-a", ttl_seconds=30
            ),
            backend_b.acquire_lease(
                takeover_run_id, "worker-new-b", ttl_seconds=30
            ),
        )
        winners = [grant for grant in grants if grant is not None]
        assert len(winners) == 1
        winner = winners[0]
        assert winner.fencing_token > old_grant.fencing_token

        with pytest.raises(StaleLeaseWriteError):
            await backend_a.save_checkpoint(
                takeover_run_id,
                0,
                {"status": "stale"},
                lease_grant=old_grant,
            )

        winner_backend = backend_a if grants[0] is winner else backend_b
        await winner_backend.save_checkpoint(
            takeover_run_id,
            0,
            {"status": "winner", "owner_id": winner.owner_id},
            lease_grant=winner,
        )
        assert await winner_backend.load_latest(takeover_run_id) == {
            "status": "winner",
            "owner_id": winner.owner_id,
        }

    connection = await psycopg.AsyncConnection.connect(dsn)
    async with connection:
        async with connection.cursor() as cursor:
            await cursor.execute(
                "DELETE FROM runtime_tool_results WHERE idempotency_key LIKE %s",
                (f"{result.run_id}:%",),
            )
            await cursor.execute(
                "DELETE FROM runtime_checkpoints WHERE run_id = %s",
                (result.run_id,),
            )
            await cursor.execute(
                "DELETE FROM runtime_run_leases WHERE run_id = %s",
                (result.run_id,),
            )
            await cursor.execute(
                "DELETE FROM runtime_checkpoints WHERE run_id = %s",
                (takeover_run_id,),
            )
            await cursor.execute(
                "DELETE FROM runtime_run_leases WHERE run_id = %s",
                (takeover_run_id,),
            )
        await connection.commit()
