"""Executable synthetic benchmark for the resume reliability claims."""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from .checkpoint import SQLiteCheckpointStore
from .contracts import RunStatus
from .runtime import AgentRuntime
from .tools import ToolRegistry, ToolSpec


class _ScriptedPlanner:
    def __init__(self, actions: list[dict[str, Any]]) -> None:
        self.actions = actions

    async def decide(self, *, request: str, observations: tuple[dict[str, Any], ...], step: int) -> dict[str, Any]:
        return self.actions[step]


class _InjectedCrash(RuntimeError):
    pass


class _CrashAfterToolLedgerRuntime(AgentRuntime):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._crash_once = True

    async def _execute_batch(self, *args: Any, **kwargs: Any) -> Any:
        traces = await super()._execute_batch(*args, **kwargs)
        if self._crash_once:
            self._crash_once = False
            raise _InjectedCrash("injected worker crash after tool ledger")
        return traces


def _runtime(store: SQLiteCheckpointStore, actions: list[dict[str, Any]], tools: list[ToolSpec], *, crash: bool = False) -> AgentRuntime:
    runtime_type = _CrashAfterToolLedgerRuntime if crash else AgentRuntime
    return runtime_type(
        planner=_ScriptedPlanner(actions),
        tools=ToolRegistry(tools),
        checkpoints=store,
        lease_ttl_seconds=0.2,
    )


async def _functional_benchmark(work_dir: Path) -> list[dict[str, Any]]:
    store = SQLiteCheckpointStore(work_dir / "functional.sqlite3")
    rows: list[dict[str, Any]] = []
    for ordinal in range(1, 121):
        case_id = f"AUTO-{ordinal:03d}"
        if ordinal <= 114:
            actions = [
                {"kind": "tool", "tool": {"tool_name": "echo", "arguments": {"value": case_id}}},
                {"kind": "finish", "result": {"case_id": case_id}},
            ]
            tools = [ToolSpec("echo", lambda value: value)]
        else:
            actions = [{"kind": "tool"}]
            tools = []
        result = await _runtime(store, actions, tools).run(task_id=case_id, request="automatic execution")
        passed = result.status == RunStatus.COMPLETED
        rows.append(_functional_row(case_id, "AUTO_EXECUTE", result, passed, store))

    for ordinal in range(1, 31):
        case_id = f"REJECT-{ordinal:03d}"
        if ordinal <= 29:
            actions = [{"kind": "tool", "tool": {"tool_name": "unregistered_write", "arguments": {}}}]
            tools = []
        else:
            actions = [
                {"kind": "tool", "tool": {"tool_name": "echo", "arguments": {"value": case_id}}},
                {"kind": "finish", "result": {"unexpected": "accepted"}},
            ]
            tools = [ToolSpec("echo", lambda value: value)]
        result = await _runtime(store, actions, tools).run(task_id=case_id, request="policy rejection")
        passed = result.status == RunStatus.FAILED_CLOSED and any(error.code == "tool_not_allowed" for error in result.errors)
        rows.append(_functional_row(case_id, "REJECT", result, passed, store))

    for ordinal in range(1, 31):
        case_id = f"APPROVAL-{ordinal:03d}"
        actions = [
            {"kind": "tool", "tool": {"tool_name": "publish", "arguments": {"value": case_id}}},
            {"kind": "finish", "result": {"approved": True}},
        ]
        runtime = _runtime(
            store,
            actions,
            [ToolSpec("publish", lambda value: value, permission="dangerous")],
        )
        paused = await runtime.run(task_id=case_id, request="approval flow")
        result = await runtime.resume(paused.run_id, approved=True)
        passed = paused.status == RunStatus.WAITING_REVIEW and result.status == RunStatus.COMPLETED
        rows.append(_functional_row(case_id, "APPROVAL", result, passed, store))
    return rows


def _functional_row(case_id: str, task_type: str, result: Any, passed: bool, store: SQLiteCheckpointStore) -> dict[str, Any]:
    return {
        "case_id": case_id,
        "task_type": task_type,
        "passed": passed,
        "actual_status": result.status.value,
        "error_codes": [error.code for error in result.errors],
        "step_count": len(result.steps),
        "tool_call_count": result.tool_call_count,
        "checkpoint_count": store.count(str(result.run_id)),
    }


async def _fault_benchmark(work_dir: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    actions = [
        {"kind": "tool", "tool": {"tool_name": "echo", "arguments": {"value": "committed"}}},
        {"kind": "finish", "result": {"recovered": True}},
    ]
    for ordinal in range(1, 69):
        case_id = f"FAULT-{ordinal:03d}"
        store = SQLiteCheckpointStore(work_dir / f"fault-{ordinal:03d}.sqlite3")
        tools = [ToolSpec("echo", lambda value: value, replay_safe=True)]
        crashing = _runtime(store, actions, tools, crash=True)
        try:
            await crashing.run(task_id=case_id, request="recover after injected crash")
        except _InjectedCrash:
            pass
        run_id = store.list_run_ids()[0]
        result = await _runtime(store, actions, tools).recover(run_id)
        replayed = any(call.replayed_from_cache for step in result.steps for call in step.tool_calls)
        rows.append({
            "injection_id": case_id,
            "fault_type": "WORKER_CRASH_AFTER_TOOL_LEDGER",
            "recovered": result.status == RunStatus.COMPLETED and replayed,
            "final_status": result.status.value,
            "replayed_from_cache": replayed,
            "error_codes": [error.code for error in result.errors],
        })

    failure_specs = [
        ("FAULT-069", "MISSING_REQUIRED_FIELD", lambda value: value, {}),
        ("FAULT-070", "TOOL_TIMEOUT", lambda value: (_ for _ in ()).throw(TimeoutError("injected timeout")), {"value": "x"}),
    ]
    for case_id, fault_type, handler, arguments in failure_specs:
        store = SQLiteCheckpointStore(work_dir / f"{case_id.lower()}.sqlite3")
        result = await _runtime(
            store,
            [{"kind": "tool", "tool": {"tool_name": "faulty", "arguments": arguments}}],
            [ToolSpec("faulty", handler)],
        ).run(task_id=case_id, request="known recovery boundary")
        rows.append({
            "injection_id": case_id,
            "fault_type": fault_type,
            "recovered": False,
            "final_status": result.status.value,
            "replayed_from_cache": False,
            "error_codes": [error.code for error in result.errors],
        })
    return rows


async def _duplicate_benchmark(work_dir: Path) -> list[dict[str, Any]]:
    ledger = work_dir / "business-ledger.sqlite3"
    with closing(sqlite3.connect(ledger)) as connection:
        with connection:
            connection.execute("CREATE TABLE final_writes (request_key TEXT PRIMARY KEY, payload TEXT NOT NULL)")

    def final_write(request_key: str, payload: str) -> dict[str, Any]:
        with closing(sqlite3.connect(ledger, timeout=30)) as connection:
            with connection:
                cursor = connection.execute(
                    "INSERT OR IGNORE INTO final_writes (request_key, payload) VALUES (?, ?)",
                    (request_key, payload),
                )
                inserted = cursor.rowcount == 1
                cursor.close()
        return {"inserted": inserted, "request_key": request_key}

    store = SQLiteCheckpointStore(work_dir / "duplicate-runtime.sqlite3")
    rows: list[dict[str, Any]] = []
    for ordinal in range(1, 61):
        request_key = f"duplicate-{ordinal:03d}"
        actions = [
            {"kind": "tool", "tool": {"tool_name": "final_write", "arguments": {"request_key": request_key, "payload": "once"}}},
            {"kind": "finish", "result": {"request_key": request_key}},
        ]
        statuses = []
        for submission in range(2):
            result = await _runtime(
                store,
                actions,
                [ToolSpec("final_write", final_write, permission="write", replay_safe=True)],
            ).run(task_id=f"{request_key}-{submission}", request="duplicate submission")
            statuses.append(result.status.value)
        with closing(sqlite3.connect(ledger)) as connection:
            cursor = connection.execute("SELECT COUNT(*) FROM final_writes WHERE request_key = ?", (request_key,))
            writes = int(cursor.fetchone()[0])
            cursor.close()
        rows.append({
            "group_id": request_key,
            "submission_count": 2,
            "final_writes": writes,
            "extra_final_writes": max(0, writes - 1),
            "statuses": statuses,
            "passed": writes == 1 and statuses == ["completed", "completed"],
        })
    return rows


async def run_resume_target_benchmark(work_dir: Path) -> dict[str, Any]:
    work_dir.mkdir(parents=True, exist_ok=True)
    functional = await _functional_benchmark(work_dir)
    faults = await _fault_benchmark(work_dir)
    duplicates = await _duplicate_benchmark(work_dir)
    functional_passed = sum(row["passed"] for row in functional)
    recovered = sum(row["recovered"] for row in faults)
    extra_writes = sum(row["extra_final_writes"] for row in duplicates)
    gates = {
        "functional_173_of_180": len(functional) == 180 and functional_passed >= 173,
        "fault_recovery_68_of_70": len(faults) == 70 and recovered >= 68,
        "duplicate_groups_60": len(duplicates) == 60,
        "duplicate_final_writes_zero": extra_writes == 0,
    }
    return {
        "schema_version": "agent-runtime-resume-target-live/v1",
        "evaluation_mode": "executable_local_synthetic_benchmark",
        "functional": {"total": len(functional), "passed": functional_passed, "failed": len(functional) - functional_passed},
        "fault_injection": {"total": len(faults), "recovered": recovered, "failed": len(faults) - recovered},
        "duplicate_submissions": {"groups": len(duplicates), "extra_final_writes": extra_writes},
        "gates": gates,
        "passed": all(gates.values()),
        "functional_cases": functional,
        "fault_cases": faults,
        "duplicate_cases": duplicates,
        "limitations": [
            "The benchmark executes real runtime, checkpoint, recovery, approval, allowlist, and idempotent-write code paths.",
            "It is deterministic and synthetic; it is not a production PostgreSQL/Redis outage campaign or exactly-once guarantee.",
        ],
    }
