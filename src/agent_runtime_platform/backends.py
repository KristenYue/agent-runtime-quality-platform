"""Async persistence boundary for runtime state and coordination."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from .checkpoint import SQLiteCheckpointStore


@dataclass(frozen=True)
class LeaseGrant:
    owner_id: str
    fencing_token: int


class StaleLeaseWriteError(RuntimeError):
    """A state write was rejected because its lease grant is stale."""


@runtime_checkable
class RuntimeBackend(Protocol):
    async def save_checkpoint(
        self,
        run_id: str,
        step_no: int,
        state: dict[str, Any],
        *,
        lease_grant: LeaseGrant,
    ) -> str: ...

    async def load_latest(self, run_id: str) -> dict[str, Any] | None: ...

    async def acquire_lease(
        self, run_id: str, owner_id: str, *, ttl_seconds: float
    ) -> LeaseGrant | None: ...

    async def renew_lease(
        self,
        run_id: str,
        owner_id: str,
        *,
        ttl_seconds: float,
        fencing_token: int,
    ) -> bool: ...

    async def release_lease(
        self, run_id: str, owner_id: str, *, fencing_token: int
    ) -> bool: ...

    async def save_tool_trace(
        self,
        idempotency_key: str,
        trace: dict[str, Any],
        *,
        run_id: str,
        lease_grant: LeaseGrant,
    ) -> None: ...

    async def load_tool_trace(
        self, idempotency_key: str
    ) -> dict[str, Any] | None: ...


class AsyncSQLiteBackend:
    """Offload the synchronous stdlib SQLite store from the event loop."""

    def __init__(self, store: SQLiteCheckpointStore) -> None:
        self.store = store

    async def save_checkpoint(
        self,
        run_id: str,
        step_no: int,
        state: dict[str, Any],
        *,
        lease_grant: LeaseGrant,
    ) -> str:
        digest = await asyncio.to_thread(
            self.store.save_fenced,
            run_id,
            step_no,
            state,
            owner_id=lease_grant.owner_id,
            fencing_token=lease_grant.fencing_token,
        )
        if digest is None:
            raise StaleLeaseWriteError(f"stale checkpoint write rejected: {run_id}")
        return digest

    async def load_latest(self, run_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.store.load_latest, run_id)

    async def acquire_lease(
        self, run_id: str, owner_id: str, *, ttl_seconds: float
    ) -> LeaseGrant | None:
        raw = await asyncio.to_thread(
            self.store.acquire_lease_grant,
            run_id,
            owner_id,
            ttl_seconds=ttl_seconds,
        )
        return None if raw is None else LeaseGrant(*raw)

    async def renew_lease(
        self,
        run_id: str,
        owner_id: str,
        *,
        ttl_seconds: float,
        fencing_token: int,
    ) -> bool:
        return await asyncio.to_thread(
            self.store.renew_lease,
            run_id,
            owner_id,
            ttl_seconds=ttl_seconds,
            fencing_token=fencing_token,
        )

    async def release_lease(
        self, run_id: str, owner_id: str, *, fencing_token: int
    ) -> bool:
        return await asyncio.to_thread(
            self.store.release_lease,
            run_id,
            owner_id,
            fencing_token=fencing_token,
        )

    async def save_tool_trace(
        self,
        idempotency_key: str,
        trace: dict[str, Any],
        *,
        run_id: str,
        lease_grant: LeaseGrant,
    ) -> None:
        saved = await asyncio.to_thread(
            self.store.save_tool_trace_fenced,
            idempotency_key,
            trace,
            run_id=run_id,
            owner_id=lease_grant.owner_id,
            fencing_token=lease_grant.fencing_token,
        )
        if not saved:
            raise StaleLeaseWriteError(f"stale tool trace write rejected: {run_id}")

    async def load_tool_trace(
        self, idempotency_key: str
    ) -> dict[str, Any] | None:
        return await asyncio.to_thread(self.store.load_tool_trace, idempotency_key)
