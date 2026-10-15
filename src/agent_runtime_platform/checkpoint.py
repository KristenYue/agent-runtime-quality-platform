"""SQLite checkpoint ledger with state-integrity hashes."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path
from typing import Any
import json
import sqlite3
import time


def _lease_grant(owner_id: str, fencing_token: int) -> tuple[str, int]:
    return owner_id, fencing_token


class SQLiteCheckpointStore:
    def __init__(self, path: str | Path) -> None:
        resolved = Path(path)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        self.path = str(resolved)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS checkpoints (
                    run_id TEXT NOT NULL,
                    step_no INTEGER NOT NULL,
                    state_json TEXT NOT NULL,
                    state_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (run_id, step_no)
                );

                CREATE TABLE IF NOT EXISTS run_leases (
                    run_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    fencing_token INTEGER NOT NULL DEFAULT 0,
                    expires_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS tool_results (
                    idempotency_key TEXT PRIMARY KEY,
                    trace_json TEXT NOT NULL,
                    trace_sha256 TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
            columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(run_leases)").fetchall()
            }
            if "fencing_token" not in columns:
                connection.execute(
                    "ALTER TABLE run_leases ADD COLUMN fencing_token INTEGER NOT NULL DEFAULT 0"
                )

    def save(self, run_id: str, step_no: int, state: dict[str, Any]) -> str:
        payload = json.dumps(
            state,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        digest = sha256(payload.encode("utf-8")).hexdigest()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO checkpoints (run_id, step_no, state_json, state_sha256)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(run_id, step_no) DO UPDATE SET
                    state_json = excluded.state_json,
                    state_sha256 = excluded.state_sha256,
                    created_at = CURRENT_TIMESTAMP
                """,
                (run_id, step_no, payload, digest),
            )
        return digest

    def save_fenced(
        self,
        run_id: str,
        step_no: int,
        state: dict[str, Any],
        *,
        owner_id: str,
        fencing_token: int,
        now: float | None = None,
    ) -> str | None:
        payload = json.dumps(
            state,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        digest = sha256(payload.encode("utf-8")).hexdigest()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                """
                SELECT 1 FROM run_leases
                WHERE run_id = ? AND owner_id = ? AND fencing_token = ?
                """,
                (run_id, owner_id, fencing_token),
            ).fetchone()
            if active is None:
                return None
            connection.execute(
                """
                INSERT INTO checkpoints (run_id, step_no, state_json, state_sha256)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(run_id, step_no) DO UPDATE SET
                    state_json = excluded.state_json,
                    state_sha256 = excluded.state_sha256,
                    created_at = CURRENT_TIMESTAMP
                """,
                (run_id, step_no, payload, digest),
            )
        return digest

    def load_latest(self, run_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT step_no, state_json, state_sha256
                FROM checkpoints WHERE run_id = ?
                ORDER BY step_no DESC LIMIT 1
                """,
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        actual = sha256(row["state_json"].encode("utf-8")).hexdigest()
        if actual != row["state_sha256"]:
            raise ValueError("checkpoint integrity verification failed")
        return json.loads(row["state_json"])

    def count(self, run_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS total FROM checkpoints WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        return int(row["total"])

    def list_run_ids(self) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT run_id FROM checkpoints ORDER BY run_id"
            ).fetchall()
        return [str(row["run_id"]) for row in rows]

    def acquire_lease(
        self,
        run_id: str,
        owner_id: str,
        *,
        ttl_seconds: float,
        now: float | None = None,
    ) -> bool:
        return self.acquire_lease_grant(
            run_id,
            owner_id,
            ttl_seconds=ttl_seconds,
            now=now,
        ) is not None

    def acquire_lease_grant(
        self,
        run_id: str,
        owner_id: str,
        *,
        ttl_seconds: float,
        now: float | None = None,
    ) -> tuple[str, int] | None:
        if ttl_seconds <= 0:
            raise ValueError("lease ttl must be positive")
        current = time.time() if now is None else now
        expires_at = current + ttl_seconds
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT owner_id, fencing_token, expires_at FROM run_leases WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if (
                row is not None
                and row["owner_id"] != owner_id
                and float(row["expires_at"]) > current
            ):
                return None
            if row is None:
                fencing_token = 1
            elif row["owner_id"] == owner_id and float(row["expires_at"]) > current:
                fencing_token = int(row["fencing_token"])
            else:
                fencing_token = int(row["fencing_token"]) + 1
            connection.execute(
                """
                INSERT INTO run_leases (
                    run_id, owner_id, fencing_token, expires_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    owner_id = excluded.owner_id,
                    fencing_token = excluded.fencing_token,
                    expires_at = excluded.expires_at,
                    updated_at = excluded.updated_at
                """,
                (run_id, owner_id, fencing_token, expires_at, current),
            )
        return _lease_grant(owner_id, fencing_token)

    def renew_lease(
        self,
        run_id: str,
        owner_id: str,
        *,
        ttl_seconds: float,
        fencing_token: int | None = None,
        now: float | None = None,
    ) -> bool:
        if ttl_seconds <= 0:
            raise ValueError("lease ttl must be positive")
        current = time.time() if now is None else now
        sql = """
            UPDATE run_leases
            SET expires_at = ?, updated_at = ?
            WHERE run_id = ? AND owner_id = ?
        """
        parameters: tuple[Any, ...] = (
            current + ttl_seconds,
            current,
            run_id,
            owner_id,
        )
        if fencing_token is not None:
            sql += " AND fencing_token = ?"
            parameters += (fencing_token,)
        with self._connect() as connection:
            cursor = connection.execute(sql, parameters)
        return cursor.rowcount == 1

    def release_lease(
        self, run_id: str, owner_id: str, *, fencing_token: int | None = None
    ) -> bool:
        sql = """
            UPDATE run_leases SET expires_at = 0, updated_at = ?
            WHERE run_id = ? AND owner_id = ?
        """
        parameters: tuple[Any, ...] = (time.time(), run_id, owner_id)
        if fencing_token is not None:
            sql += " AND fencing_token = ?"
            parameters += (fencing_token,)
        with self._connect() as connection:
            cursor = connection.execute(sql, parameters)
        return cursor.rowcount == 1

    def save_tool_trace(self, idempotency_key: str, trace: dict[str, Any]) -> None:
        payload = json.dumps(
            trace,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        digest = sha256(payload.encode("utf-8")).hexdigest()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO tool_results (idempotency_key, trace_json, trace_sha256)
                VALUES (?, ?, ?)
                ON CONFLICT(idempotency_key) DO NOTHING
                """,
                (idempotency_key, payload, digest),
            )

    def save_tool_trace_fenced(
        self,
        idempotency_key: str,
        trace: dict[str, Any],
        *,
        run_id: str,
        owner_id: str,
        fencing_token: int,
        now: float | None = None,
    ) -> bool:
        payload = json.dumps(
            trace,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        digest = sha256(payload.encode("utf-8")).hexdigest()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                """
                SELECT 1 FROM run_leases
                WHERE run_id = ? AND owner_id = ? AND fencing_token = ?
                """,
                (run_id, owner_id, fencing_token),
            ).fetchone()
            if active is None:
                return False
            connection.execute(
                """
                INSERT INTO tool_results (idempotency_key, trace_json, trace_sha256)
                VALUES (?, ?, ?)
                ON CONFLICT(idempotency_key) DO NOTHING
                """,
                (idempotency_key, payload, digest),
            )
        return True

    def load_tool_trace(self, idempotency_key: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT trace_json, trace_sha256 FROM tool_results
                WHERE idempotency_key = ?
                """,
                (idempotency_key,),
            ).fetchone()
        if row is None:
            return None
        actual = sha256(row["trace_json"].encode("utf-8")).hexdigest()
        if actual != row["trace_sha256"]:
            raise ValueError("tool trace integrity verification failed")
        return json.loads(row["trace_json"])
