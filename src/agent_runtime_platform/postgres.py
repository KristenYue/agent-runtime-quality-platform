"""PostgreSQL implementation of the async runtime backend."""

from __future__ import annotations

from contextlib import asynccontextmanager
from hashlib import sha256
import json
from typing import Any

from .backends import LeaseGrant, StaleLeaseWriteError


def _load_psycopg():
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError(
            "PostgreSQL support requires: pip install -e '.[postgres]'"
        ) from exc
    return psycopg, dict_row


def _load_psycopg_pool():
    try:
        from psycopg_pool import AsyncConnectionPool
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError(
            "PostgreSQL pooling requires: pip install -e '.[postgres]'"
        ) from exc
    return AsyncConnectionPool


def _canonical_json(value: dict[str, Any]) -> tuple[str, str]:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return payload, sha256(payload.encode("utf-8")).hexdigest()


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("PostgreSQL JSON payload must be an object")
    return value


class PostgresRuntimeBackend:
    """Own only the ``runtime_*`` tables from this project's migration."""

    def __init__(
        self,
        dsn: str,
        *,
        pool_min_size: int = 1,
        pool_max_size: int = 10,
        pool_timeout_seconds: float = 30.0,
    ) -> None:
        if not dsn.strip():
            raise ValueError("database DSN must not be blank")
        if pool_min_size < 0:
            raise ValueError("pool_min_size must not be negative")
        if pool_max_size < 1 or pool_max_size < pool_min_size:
            raise ValueError("pool_max_size must be positive and >= pool_min_size")
        if pool_timeout_seconds <= 0:
            raise ValueError("pool_timeout_seconds must be positive")
        self.dsn = dsn
        self.pool_min_size = pool_min_size
        self.pool_max_size = pool_max_size
        self.pool_timeout_seconds = pool_timeout_seconds
        self._pool = None

    async def open(self) -> None:
        """Open and verify an explicit async connection pool."""
        if self._pool is not None:
            return
        _, dict_row = _load_psycopg()
        pool_class = _load_psycopg_pool()
        pool = pool_class(
            self.dsn,
            min_size=self.pool_min_size,
            max_size=self.pool_max_size,
            timeout=self.pool_timeout_seconds,
            kwargs={"row_factory": dict_row},
            open=False,
        )
        try:
            await pool.open(wait=True, timeout=self.pool_timeout_seconds)
        except BaseException:
            await pool.close()
            raise
        self._pool = pool

    async def close(self) -> None:
        """Close the pool; direct per-operation mode has nothing to close."""
        pool, self._pool = self._pool, None
        if pool is not None:
            await pool.close()

    async def __aenter__(self):
        await self.open()
        return self

    async def __aexit__(self, *_):
        await self.close()
        return False

    async def _connect(self):
        psycopg, dict_row = _load_psycopg()
        return await psycopg.AsyncConnection.connect(self.dsn, row_factory=dict_row)

    @asynccontextmanager
    async def _connection(self):
        if self._pool is not None:
            async with self._pool.connection() as connection:
                yield connection
            return
        connection = await self._connect()
        async with connection:
            yield connection

    async def save_checkpoint(
        self,
        run_id: str,
        step_no: int,
        state: dict[str, Any],
        *,
        lease_grant: LeaseGrant,
    ) -> str:
        payload, digest = _canonical_json(state)
        async with self._connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO runtime_checkpoints (
                        run_id, step_no, state, state_sha256
                    )
                    SELECT %s, %s, %s::jsonb, %s
                    WHERE EXISTS (
                        SELECT 1 FROM runtime_run_leases
                        WHERE run_id = %s AND owner_id = %s
                          AND fencing_token = %s
                    )
                    ON CONFLICT (run_id, step_no) DO UPDATE SET
                        state = EXCLUDED.state,
                        state_sha256 = EXCLUDED.state_sha256,
                        created_at = CURRENT_TIMESTAMP
                    RETURNING state_sha256
                    """,
                    (
                        run_id,
                        step_no,
                        payload,
                        digest,
                        run_id,
                        lease_grant.owner_id,
                        lease_grant.fencing_token,
                    ),
                )
                row = await cursor.fetchone()
            await connection.commit()
        if row is None:
            raise StaleLeaseWriteError(f"stale checkpoint write rejected: {run_id}")
        return digest

    async def load_latest(self, run_id: str) -> dict[str, Any] | None:
        async with self._connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    SELECT state, state_sha256 FROM runtime_checkpoints
                    WHERE run_id = %s ORDER BY step_no DESC LIMIT 1
                    """,
                    (run_id,),
                )
                row = await cursor.fetchone()
        if row is None:
            return None
        state = _as_dict(row["state"])
        _, digest = _canonical_json(state)
        if digest != row["state_sha256"]:
            raise ValueError("checkpoint integrity verification failed")
        return state

    async def acquire_lease(
        self, run_id: str, owner_id: str, *, ttl_seconds: float
    ) -> LeaseGrant | None:
        if ttl_seconds <= 0:
            raise ValueError("lease ttl must be positive")
        async with self._connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO runtime_run_leases (
                        run_id, owner_id, fencing_token, expires_at, updated_at
                    ) VALUES (
                        %s, %s, 1,
                        CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'),
                        CURRENT_TIMESTAMP
                    )
                    ON CONFLICT (run_id) DO UPDATE SET
                        owner_id = EXCLUDED.owner_id,
                        fencing_token = CASE
                            WHEN runtime_run_leases.owner_id = EXCLUDED.owner_id
                             AND runtime_run_leases.expires_at > CURRENT_TIMESTAMP
                            THEN runtime_run_leases.fencing_token
                            ELSE runtime_run_leases.fencing_token + 1
                        END,
                        expires_at = EXCLUDED.expires_at,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE runtime_run_leases.owner_id = EXCLUDED.owner_id
                       OR runtime_run_leases.expires_at <= CURRENT_TIMESTAMP
                    RETURNING owner_id, fencing_token
                    """,
                    (run_id, owner_id, ttl_seconds),
                )
                row = await cursor.fetchone()
            await connection.commit()
        if row is None:
            return None
        return LeaseGrant(
            owner_id=str(row["owner_id"]),
            fencing_token=int(row["fencing_token"]),
        )

    async def renew_lease(
        self,
        run_id: str,
        owner_id: str,
        *,
        ttl_seconds: float,
        fencing_token: int,
    ) -> bool:
        if ttl_seconds <= 0:
            raise ValueError("lease ttl must be positive")
        async with self._connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE runtime_run_leases
                    SET expires_at = CURRENT_TIMESTAMP + (%s * INTERVAL '1 second'),
                        updated_at = CURRENT_TIMESTAMP
                    WHERE run_id = %s AND owner_id = %s AND fencing_token = %s
                    """,
                    (ttl_seconds, run_id, owner_id, fencing_token),
                )
                renewed = cursor.rowcount == 1
            await connection.commit()
        return renewed

    async def release_lease(
        self, run_id: str, owner_id: str, *, fencing_token: int
    ) -> bool:
        async with self._connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    UPDATE runtime_run_leases
                    SET expires_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                    WHERE run_id = %s AND owner_id = %s AND fencing_token = %s
                    """,
                    (run_id, owner_id, fencing_token),
                )
                released = cursor.rowcount == 1
            await connection.commit()
        return released

    async def save_tool_trace(
        self,
        idempotency_key: str,
        trace: dict[str, Any],
        *,
        run_id: str,
        lease_grant: LeaseGrant,
    ) -> None:
        payload, digest = _canonical_json(trace)
        async with self._connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    INSERT INTO runtime_tool_results (
                        idempotency_key, trace, trace_sha256
                    )
                    SELECT %s, %s::jsonb, %s
                    WHERE EXISTS (
                        SELECT 1 FROM runtime_run_leases
                        WHERE run_id = %s AND owner_id = %s
                          AND fencing_token = %s
                    )
                    ON CONFLICT (idempotency_key) DO UPDATE SET
                        idempotency_key = EXCLUDED.idempotency_key
                    RETURNING idempotency_key
                    """,
                    (
                        idempotency_key,
                        payload,
                        digest,
                        run_id,
                        lease_grant.owner_id,
                        lease_grant.fencing_token,
                    ),
                )
                row = await cursor.fetchone()
            await connection.commit()
        if row is None:
            raise StaleLeaseWriteError(f"stale tool trace write rejected: {run_id}")

    async def load_tool_trace(
        self, idempotency_key: str
    ) -> dict[str, Any] | None:
        async with self._connection() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    """
                    SELECT trace, trace_sha256 FROM runtime_tool_results
                    WHERE idempotency_key = %s
                    """,
                    (idempotency_key,),
                )
                row = await cursor.fetchone()
        if row is None:
            return None
        trace = _as_dict(row["trace"])
        _, digest = _canonical_json(trace)
        if digest != row["trace_sha256"]:
            raise ValueError("tool trace integrity verification failed")
        return trace
