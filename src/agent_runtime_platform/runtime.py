"""Durable, provider-neutral runtime loop."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from datetime import datetime, timezone
from time import perf_counter
from typing import Any
from uuid import UUID, uuid4

from .backends import (
    AsyncSQLiteBackend,
    LeaseGrant,
    RuntimeBackend,
    StaleLeaseWriteError,
)
from .checkpoint import SQLiteCheckpointStore
from .contracts import (
    Planner,
    PlannerAction,
    RunResult,
    RunStatus,
    RuntimeErrorTrace,
    StepTrace,
    ToolCallRequest,
    ToolCallTrace,
)
from .tools import ToolExecutionContext, ToolRegistry


_ACTIVE_LEASE: ContextVar[LeaseGrant | None] = ContextVar(
    "agent_runtime_active_lease", default=None
)


class RuntimeLeaseBusy(RuntimeError):
    """Another worker currently owns the run lease."""


class RuntimeLeaseLost(RuntimeError):
    """The worker could not renew its run lease."""


class AgentRuntime:
    """Execute planner actions with budgets, checkpoints, and human review."""

    def __init__(
        self,
        *,
        planner: Planner,
        tools: ToolRegistry,
        checkpoints: SQLiteCheckpointStore | None = None,
        backend: RuntimeBackend | None = None,
        max_tool_calls: int = 8,
        max_steps: int = 12,
        lease_ttl_seconds: float = 30.0,
        owner_id: str | None = None,
    ) -> None:
        if max_tool_calls < 1:
            raise ValueError("max_tool_calls must be positive")
        if max_steps < 1:
            raise ValueError("max_steps must be positive")
        if lease_ttl_seconds <= 0:
            raise ValueError("lease_ttl_seconds must be positive")
        if (checkpoints is None) == (backend is None):
            raise ValueError("provide exactly one of checkpoints or backend")
        self.planner = planner
        self.tools = tools
        self.checkpoints = checkpoints
        if backend is not None:
            self.backend = backend
        else:
            assert checkpoints is not None
            self.backend = AsyncSQLiteBackend(checkpoints)
        self.max_tool_calls = max_tool_calls
        self.max_steps = max_steps
        self.lease_ttl_seconds = lease_ttl_seconds
        self.owner_id = owner_id or str(uuid4())

    async def run(self, *, task_id: str, request: str) -> RunResult:
        now = datetime.now(timezone.utc)
        run_id = str(uuid4())
        state: dict[str, Any] = {
            "run_id": run_id,
            "task_id": task_id,
            "status": RunStatus.RUNNING.value,
            "request": request,
            "observations": [],
            "steps": [],
            "errors": [],
            "result": None,
            "pending_action": None,
            "inflight_action": None,
            "inflight_human_confirmed": False,
            "tool_call_count": 0,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }
        async def execute(
            lease_lost: asyncio.Event, lease_grant: LeaseGrant
        ) -> RunResult:
            await self._save(state, -1)
            return await self._drive(
                state, lease_lost=lease_lost, lease_grant=lease_grant
            )

        return await self._with_lease(run_id, execute)

    async def resume(
        self,
        run_id: UUID | str,
        *,
        approved: bool,
        review_message: str | None = None,
    ) -> RunResult:
        run_id_text = str(run_id)

        async def execute(
            lease_lost: asyncio.Event, lease_grant: LeaseGrant
        ) -> RunResult:
            state = await self.backend.load_latest(run_id_text)
            if state is None:
                raise LookupError(f"run checkpoint not found: {run_id}")
            return await self._resume_state(
                state,
                approved=approved,
                review_message=review_message,
                lease_lost=lease_lost,
                lease_grant=lease_grant,
            )

        return await self._with_lease(run_id_text, execute)

    async def _resume_state(
        self,
        state: dict[str, Any],
        *,
        approved: bool,
        review_message: str | None,
        lease_lost: asyncio.Event,
        lease_grant: LeaseGrant,
    ) -> RunResult:
        if state is None:
            raise LookupError("run checkpoint not found")
        if state["status"] != RunStatus.WAITING_REVIEW.value:
            raise ValueError("only a run waiting for review can be resumed")

        action = PlannerAction.model_validate(state["pending_action"])
        step_no = len(state["steps"]) - 1
        if not approved:
            state["status"] = RunStatus.FAILED_CLOSED.value
            state["pending_action"] = None
            state["errors"].append(
                RuntimeErrorTrace(
                    code="human_rejected",
                    message=review_message or "human reviewer rejected the pending action",
                    step_no=step_no,
                    recoverable=False,
                ).model_dump(mode="json")
            )
            await self._save(state, step_no)
            return self._result(state)

        state["status"] = RunStatus.RUNNING.value
        state["pending_action"] = None
        if action.kind in {"tool", "tool_batch"}:
            requests = self._action_tools(action)
            state["inflight_action"] = action.model_dump(mode="json")
            state["inflight_human_confirmed"] = True
            await self._save(state, step_no)
            traces = await self._execute_batch(
                requests,
                run_id=state["run_id"],
                step_no=step_no,
                human_confirmed=True,
                lease_grant=lease_grant,
            )
            self._ensure_lease(lease_lost)
            state["steps"][-1]["tool_calls"] = [
                trace.model_dump(mode="json") for trace in traces
            ]
            self._append_observations(state, traces)
            state["inflight_action"] = None
            state["inflight_human_confirmed"] = False
            if any(trace.status == "failed_closed" for trace in traces):
                self._fail_from_tool_traces(state, traces, step_no)
                await self._save(state, step_no)
                return self._result(state)
        else:
            state["observations"].append(
                {
                    "kind": "human_review",
                    "approved": True,
                    "message": review_message,
                }
            )
        await self._save(state, step_no)
        return await self._drive(
            state, lease_lost=lease_lost, lease_grant=lease_grant
        )

    async def recover(self, run_id: UUID | str) -> RunResult:
        """Continue a checkpointed running run after its previous lease expired."""

        run_id_text = str(run_id)

        async def execute(
            lease_lost: asyncio.Event, lease_grant: LeaseGrant
        ) -> RunResult:
            state = await self.backend.load_latest(run_id_text)
            if state is None:
                raise LookupError(f"run checkpoint not found: {run_id}")
            if state["status"] != RunStatus.RUNNING.value:
                raise ValueError("only a running checkpoint can be recovered")
            if state.get("inflight_action") is not None:
                recovered = await self._recover_inflight(
                    state, lease_lost=lease_lost, lease_grant=lease_grant
                )
                if recovered is not None:
                    return recovered
            return await self._drive(
                state, lease_lost=lease_lost, lease_grant=lease_grant
            )

        return await self._with_lease(run_id_text, execute)

    async def get(self, run_id: UUID | str) -> RunResult:
        state = await self.backend.load_latest(str(run_id))
        if state is None:
            raise LookupError(f"run checkpoint not found: {run_id}")
        return self._result(state)

    async def _with_lease(self, run_id: str, operation: Any) -> RunResult:
        lease_grant = await self.backend.acquire_lease(
            run_id,
            self.owner_id,
            ttl_seconds=self.lease_ttl_seconds,
        )
        if lease_grant is None:
            raise RuntimeLeaseBusy(f"run lease is held by another worker: {run_id}")

        stop = asyncio.Event()
        lease_lost = asyncio.Event()
        context_token = _ACTIVE_LEASE.set(lease_grant)

        async def heartbeat() -> None:
            interval = max(self.lease_ttl_seconds / 3, 0.001)
            while True:
                try:
                    await asyncio.wait_for(stop.wait(), timeout=interval)
                    return
                except TimeoutError:
                    try:
                        renewed = await self.backend.renew_lease(
                            run_id,
                            self.owner_id,
                            ttl_seconds=self.lease_ttl_seconds,
                            fencing_token=lease_grant.fencing_token,
                        )
                    except Exception:
                        lease_lost.set()
                        return
                    if not renewed:
                        lease_lost.set()
                        return

        heartbeat_task = asyncio.create_task(heartbeat())
        try:
            result = await operation(lease_lost, lease_grant)
            self._ensure_lease(lease_lost)
            return result
        finally:
            stop.set()
            await heartbeat_task
            try:
                await self.backend.release_lease(
                    run_id,
                    self.owner_id,
                    fencing_token=lease_grant.fencing_token,
                )
            finally:
                _ACTIVE_LEASE.reset(context_token)

    async def _recover_inflight(
        self,
        state: dict[str, Any],
        *,
        lease_lost: asyncio.Event,
        lease_grant: LeaseGrant,
    ) -> RunResult | None:
        action = PlannerAction.model_validate(state["inflight_action"])
        requests = self._action_tools(action)
        step_no = len(state["steps"]) - 1
        unsafe_missing: list[str] = []
        try:
            for index, call in enumerate(requests):
                key = f"{state['run_id']}:{step_no}:{index}"
                if (
                    await self.backend.load_tool_trace(key) is None
                    and not self.tools.is_replay_safe(call.tool_name)
                ):
                    unsafe_missing.append(call.tool_name)
        except ValueError as exc:
            self._fail(
                state,
                code="tool_not_allowed",
                message=str(exc),
                step_no=step_no,
            )
            state["inflight_action"] = None
            state["inflight_human_confirmed"] = False
            await self._save(state, step_no)
            return self._result(state)
        if unsafe_missing:
            self._fail(
                state,
                code="unsafe_replay_blocked",
                message=(
                    "automatic replay blocked for non-replay-safe tool(s): "
                    + ", ".join(unsafe_missing)
                ),
                step_no=step_no,
            )
            state["inflight_action"] = None
            state["inflight_human_confirmed"] = False
            await self._save(state, step_no)
            return self._result(state)

        traces = await self._execute_batch(
            requests,
            run_id=state["run_id"],
            step_no=step_no,
            human_confirmed=bool(state.get("inflight_human_confirmed", False)),
            lease_grant=lease_grant,
        )
        self._ensure_lease(lease_lost)
        state["steps"][-1]["tool_calls"] = [
            trace.model_dump(mode="json") for trace in traces
        ]
        self._append_observations(state, traces)
        state["inflight_action"] = None
        state["inflight_human_confirmed"] = False
        if any(trace.status == "failed_closed" for trace in traces):
            self._fail_from_tool_traces(state, traces, step_no)
            await self._save(state, step_no)
            return self._result(state)
        await self._save(state, step_no)
        return None

    @staticmethod
    def _ensure_lease(lease_lost: asyncio.Event) -> None:
        if lease_lost.is_set():
            raise RuntimeLeaseLost("run lease heartbeat was lost")

    async def _drive(
        self,
        state: dict[str, Any],
        *,
        lease_lost: asyncio.Event,
        lease_grant: LeaseGrant,
    ) -> RunResult:
        while state["status"] == RunStatus.RUNNING.value:
            self._ensure_lease(lease_lost)
            step_no = len(state["steps"])
            if step_no >= self.max_steps:
                self._fail(
                    state,
                    code="step_budget_exceeded",
                    message=f"step budget {self.max_steps} has been exhausted",
                    step_no=step_no,
                )
                await self._save(state, step_no)
                break
            try:
                raw_action = await self.planner.decide(
                    request=state["request"],
                    observations=tuple(state["observations"]),
                    step=step_no,
                )
                action = PlannerAction.model_validate(raw_action)
            except Exception as exc:
                self._fail(
                    state,
                    code="planner_error",
                    message=f"{type(exc).__name__}: {exc}",
                    step_no=step_no,
                )
                await self._save(state, step_no)
                break

            state["steps"].append(
                StepTrace(step_no=step_no, action_kind=action.kind).model_dump(mode="json")
            )

            if action.kind == "finish":
                state["status"] = RunStatus.COMPLETED.value
                state["result"] = action.result
                await self._save(state, step_no)
                break

            if action.kind == "pause_for_review":
                await self._pause(state, action, step_no)
                break

            requests = self._action_tools(action)
            if state["tool_call_count"] + len(requests) > self.max_tool_calls:
                self._fail(
                    state,
                    code="tool_budget_exceeded",
                    message=(
                        f"tool budget {self.max_tool_calls} would be exceeded by "
                        f"{len(requests)} requested call(s)"
                    ),
                    step_no=step_no,
                )
                await self._save(state, step_no)
                break

            try:
                needs_review = any(
                    self.tools.requires_confirmation(call.tool_name) for call in requests
                )
            except ValueError as exc:
                self._fail(
                    state,
                    code="tool_not_allowed",
                    message=str(exc),
                    step_no=step_no,
                )
                await self._save(state, step_no)
                break

            state["tool_call_count"] += len(requests)
            if needs_review:
                await self._pause(state, action, step_no)
                break

            state["inflight_action"] = action.model_dump(mode="json")
            state["inflight_human_confirmed"] = False
            await self._save(state, step_no)
            traces = await self._execute_batch(
                requests,
                run_id=state["run_id"],
                step_no=step_no,
                human_confirmed=False,
                lease_grant=lease_grant,
            )
            self._ensure_lease(lease_lost)
            state["steps"][-1]["tool_calls"] = [
                trace.model_dump(mode="json") for trace in traces
            ]
            self._append_observations(state, traces)
            state["inflight_action"] = None
            state["inflight_human_confirmed"] = False
            if any(trace.status == "failed_closed" for trace in traces):
                self._fail_from_tool_traces(state, traces, step_no)
                await self._save(state, step_no)
                break
            await self._save(state, step_no)

        return self._result(state)

    async def _pause(
        self, state: dict[str, Any], action: PlannerAction, step_no: int
    ) -> None:
        state["status"] = RunStatus.WAITING_REVIEW.value
        state["pending_action"] = action.model_dump(mode="json")
        await self._save(state, step_no)

    async def _execute_batch(
        self,
        requests: list[ToolCallRequest],
        *,
        run_id: str,
        step_no: int,
        human_confirmed: bool,
        lease_grant: LeaseGrant,
    ) -> list[ToolCallTrace]:
        return list(
            await asyncio.gather(
                *(
                    self._execute_one(
                        call,
                        idempotency_key=f"{run_id}:{step_no}:{index}",
                        run_id=run_id,
                        step_no=step_no,
                        tool_index=index,
                        human_confirmed=human_confirmed,
                        lease_grant=lease_grant,
                    )
                    for index, call in enumerate(requests)
                )
            )
        )

    async def _execute_one(
        self,
        call: ToolCallRequest,
        *,
        idempotency_key: str,
        run_id: str,
        step_no: int,
        tool_index: int,
        human_confirmed: bool,
        lease_grant: LeaseGrant,
    ) -> ToolCallTrace:
        cached = await self.backend.load_tool_trace(idempotency_key)
        if cached is not None:
            cached["replayed_from_cache"] = True
            return ToolCallTrace.model_validate(cached)
        started = perf_counter()
        try:
            output = await self.tools.execute(
                call.tool_name,
                call.arguments,
                human_confirmed=human_confirmed,
                runtime_context=ToolExecutionContext(
                    idempotency_key=idempotency_key,
                    run_id=run_id,
                    step_no=step_no,
                    tool_index=tool_index,
                    lease_owner=self.owner_id,
                    fencing_token=lease_grant.fencing_token,
                ),
            )
            trace = ToolCallTrace(
                idempotency_key=idempotency_key,
                tool_name=call.tool_name,
                arguments=call.arguments,
                status="succeeded",
                output=output,
                duration_ms=(perf_counter() - started) * 1000,
            )
        except Exception as exc:
            trace = ToolCallTrace(
                idempotency_key=idempotency_key,
                tool_name=call.tool_name,
                arguments=call.arguments,
                status="failed_closed",
                error_type=type(exc).__name__,
                output={"error": str(exc)},
                duration_ms=(perf_counter() - started) * 1000,
            )
        try:
            await self.backend.save_tool_trace(
                idempotency_key,
                trace.model_dump(mode="json"),
                run_id=run_id,
                lease_grant=lease_grant,
            )
        except StaleLeaseWriteError as exc:
            raise RuntimeLeaseLost(str(exc)) from exc
        return trace

    @staticmethod
    def _action_tools(action: PlannerAction) -> list[ToolCallRequest]:
        if action.kind == "tool" and action.tool is not None:
            return [action.tool]
        if action.kind == "tool_batch":
            return action.tools
        return []

    @staticmethod
    def _append_observations(
        state: dict[str, Any], traces: list[ToolCallTrace]
    ) -> None:
        state["observations"].extend(
            {
                "kind": "tool_result",
                "tool_name": trace.tool_name,
                "status": trace.status,
                "output": trace.output,
            }
            for trace in traces
        )

    @staticmethod
    def _fail_from_tool_traces(
        state: dict[str, Any], traces: list[ToolCallTrace], step_no: int
    ) -> None:
        failed = [trace for trace in traces if trace.status == "failed_closed"]
        state["status"] = RunStatus.FAILED_CLOSED.value
        state["errors"].extend(
            RuntimeErrorTrace(
                code="tool_execution_failed",
                message=f"{trace.tool_name}: {trace.error_type}",
                step_no=step_no,
                recoverable=False,
            ).model_dump(mode="json")
            for trace in failed
        )

    @staticmethod
    def _fail(
        state: dict[str, Any], *, code: str, message: str, step_no: int
    ) -> None:
        state["status"] = RunStatus.FAILED_CLOSED.value
        state["errors"].append(
            RuntimeErrorTrace(
                code=code,
                message=message,
                step_no=step_no,
                recoverable=False,
            ).model_dump(mode="json")
        )

    async def _save(self, state: dict[str, Any], step_no: int) -> str:
        state["updated_at"] = datetime.now(timezone.utc).isoformat()
        lease_grant = _ACTIVE_LEASE.get()
        if lease_grant is None:
            raise RuntimeError("checkpoint write requires an active lease")
        try:
            return await self.backend.save_checkpoint(
                state["run_id"],
                step_no,
                state,
                lease_grant=lease_grant,
            )
        except StaleLeaseWriteError as exc:
            raise RuntimeLeaseLost(str(exc)) from exc

    @staticmethod
    def _result(state: dict[str, Any]) -> RunResult:
        internal = {"observations", "inflight_action", "inflight_human_confirmed"}
        public = {key: value for key, value in state.items() if key not in internal}
        return RunResult.model_validate(public)
