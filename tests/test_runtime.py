import asyncio

from agent_runtime_platform import (
    AgentRuntime,
    RunStatus,
    RuntimeLeaseBusy,
    RuntimeLeaseLost,
    SQLiteCheckpointStore,
    ToolRegistry,
    ToolSpec,
)
import pytest


class ScriptedPlanner:
    def __init__(self, actions):
        self.actions = actions
        self.calls = []

    async def decide(self, *, request, observations, step):
        self.calls.append(
            {"request": request, "observations": observations, "step": step}
        )
        return self.actions[step]


class SimulatedCrash(BaseException):
    pass


class CrashAfterBatchRuntime(AgentRuntime):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.crash_once = True

    async def _execute_batch(self, *args, **kwargs):
        traces = await super()._execute_batch(*args, **kwargs)
        if self.crash_once:
            self.crash_once = False
            raise SimulatedCrash("after tool ledger, before step checkpoint")
        return traces


def make_runtime(tmp_path, planner, tools, *, max_tool_calls=8, max_steps=12):
    return AgentRuntime(
        planner=planner,
        tools=ToolRegistry(tools),
        checkpoints=SQLiteCheckpointStore(tmp_path / "runtime.sqlite3"),
        max_tool_calls=max_tool_calls,
        max_steps=max_steps,
    )


def test_runtime_executes_tool_then_finishes_with_trace(tmp_path):
    planner = ScriptedPlanner(
        [
            {
                "kind": "tool",
                "tool": {"tool_name": "add", "arguments": {"left": 2, "right": 5}},
            },
            {"kind": "finish", "result": {"answer": 7}},
        ]
    )
    runtime = make_runtime(
        tmp_path, planner, [ToolSpec("add", lambda left, right: left + right)]
    )

    result = asyncio.run(runtime.run(task_id="math-1", request="2 + 5"))

    assert result.status == RunStatus.COMPLETED
    assert result.result == {"answer": 7}
    assert result.tool_call_count == 1
    assert result.steps[0].tool_calls[0].output == 7
    assert planner.calls[1]["observations"][0]["output"] == 7


def test_tool_batch_runs_concurrently(tmp_path):
    active = 0
    maximum_active = 0

    async def overlap(value):
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return value

    planner = ScriptedPlanner(
        [
            {
                "kind": "tool_batch",
                "tools": [
                    {"tool_name": "overlap", "arguments": {"value": "a"}},
                    {"tool_name": "overlap", "arguments": {"value": "b"}},
                ],
            },
            {"kind": "finish", "result": {"ok": True}},
        ]
    )
    runtime = make_runtime(tmp_path, planner, [ToolSpec("overlap", overlap)])

    result = asyncio.run(runtime.run(task_id="batch-1", request="run both"))

    assert result.status == RunStatus.COMPLETED
    assert maximum_active == 2
    assert [trace.output for trace in result.steps[0].tool_calls] == ["a", "b"]


def test_budget_excess_fails_closed_without_executing_tools(tmp_path):
    executions = []
    planner = ScriptedPlanner(
        [
            {
                "kind": "tool_batch",
                "tools": [
                    {"tool_name": "record", "arguments": {"value": 1}},
                    {"tool_name": "record", "arguments": {"value": 2}},
                ],
            }
        ]
    )
    runtime = make_runtime(
        tmp_path,
        planner,
        [ToolSpec("record", lambda value: executions.append(value))],
        max_tool_calls=1,
    )

    result = asyncio.run(runtime.run(task_id="budget-1", request="too many"))

    assert result.status == RunStatus.FAILED_CLOSED
    assert result.errors[0].code == "tool_budget_exceeded"
    assert result.tool_call_count == 0
    assert executions == []


def test_explicit_pause_can_resume_in_new_runtime_instance(tmp_path):
    actions = [
        {"kind": "pause_for_review", "message": "approve the plan"},
        {"kind": "finish", "result": {"reviewed": True}},
    ]
    first_runtime = make_runtime(tmp_path, ScriptedPlanner(actions), [])
    paused = asyncio.run(
        first_runtime.run(task_id="review-1", request="prepare action")
    )
    assert paused.status == RunStatus.WAITING_REVIEW

    restarted_runtime = make_runtime(tmp_path, ScriptedPlanner(actions), [])
    completed = asyncio.run(
        restarted_runtime.resume(
            paused.run_id, approved=True, review_message="approved by operator"
        )
    )

    assert completed.status == RunStatus.COMPLETED
    assert completed.result == {"reviewed": True}
    assert restarted_runtime.checkpoints.count(str(paused.run_id)) == 3


def test_dangerous_tool_waits_for_review_then_executes_once(tmp_path):
    executions = []
    actions = [
        {
            "kind": "tool",
            "tool": {"tool_name": "publish", "arguments": {"message": "hello"}},
        },
        {"kind": "finish", "result": {"published": True}},
    ]
    runtime = make_runtime(
        tmp_path,
        ScriptedPlanner(actions),
        [
            ToolSpec(
                "publish",
                lambda message: executions.append(message) or "published",
                permission="dangerous",
            )
        ],
    )

    paused = asyncio.run(runtime.run(task_id="publish-1", request="publish"))
    assert paused.status == RunStatus.WAITING_REVIEW
    assert paused.tool_call_count == 1
    assert executions == []

    completed = asyncio.run(runtime.resume(paused.run_id, approved=True))
    assert completed.status == RunStatus.COMPLETED
    assert executions == ["hello"]
    assert completed.steps[0].tool_calls[0].status == "succeeded"


def test_rejected_review_fails_closed(tmp_path):
    planner = ScriptedPlanner(
        [
            {"kind": "pause_for_review", "message": "check"},
            {"kind": "finish", "result": {"should_not": "run"}},
        ]
    )
    runtime = make_runtime(tmp_path, planner, [])
    paused = asyncio.run(runtime.run(task_id="review-2", request="review"))

    rejected = asyncio.run(
        runtime.resume(paused.run_id, approved=False, review_message="policy violation")
    )

    assert rejected.status == RunStatus.FAILED_CLOSED
    assert rejected.errors[0].code == "human_rejected"
    assert rejected.errors[0].message == "policy violation"
    assert len(planner.calls) == 1


def test_tool_error_is_traced_and_fails_closed(tmp_path):
    def fail():
        raise RuntimeError("service unavailable")

    planner = ScriptedPlanner(
        [{"kind": "tool", "tool": {"tool_name": "fail", "arguments": {}}}]
    )
    runtime = make_runtime(tmp_path, planner, [ToolSpec("fail", fail)])

    result = asyncio.run(runtime.run(task_id="failure-1", request="call service"))

    assert result.status == RunStatus.FAILED_CLOSED
    assert result.steps[0].tool_calls[0].error_type == "RuntimeError"
    assert result.errors[0].code == "tool_execution_failed"


def test_unregistered_tool_fails_closed_before_execution(tmp_path):
    planner = ScriptedPlanner(
        [{"kind": "tool", "tool": {"tool_name": "unknown", "arguments": {}}}]
    )
    runtime = make_runtime(tmp_path, planner, [])

    result = asyncio.run(runtime.run(task_id="allowlist-1", request="unknown"))

    assert result.status == RunStatus.FAILED_CLOSED
    assert result.errors[0].code == "tool_not_allowed"


def test_step_budget_stops_a_non_terminating_planner(tmp_path):
    planner = ScriptedPlanner(
        [
            {"kind": "tool", "tool": {"tool_name": "noop", "arguments": {}}},
            {"kind": "tool", "tool": {"tool_name": "noop", "arguments": {}}},
        ]
    )
    runtime = make_runtime(
        tmp_path,
        planner,
        [ToolSpec("noop", lambda: None)],
        max_steps=2,
    )

    result = asyncio.run(runtime.run(task_id="loop-1", request="never finish"))

    assert result.status == RunStatus.FAILED_CLOSED
    assert result.errors[0].code == "step_budget_exceeded"
    assert result.tool_call_count == 2


def test_recovery_reuses_cached_tool_result_without_duplicate_execution(tmp_path):
    executions = []
    actions = [
        {"kind": "tool", "tool": {"tool_name": "lookup", "arguments": {}}},
        {"kind": "finish", "result": {"ok": True}},
    ]
    store = SQLiteCheckpointStore(tmp_path / "runtime.sqlite3")
    crashing = CrashAfterBatchRuntime(
        planner=ScriptedPlanner(actions),
        tools=ToolRegistry(
            [ToolSpec("lookup", lambda: executions.append("called") or "value")]
        ),
        checkpoints=store,
    )

    with pytest.raises(SimulatedCrash):
        asyncio.run(crashing.run(task_id="recovery-1", request="lookup"))
    run_id = store.list_run_ids()[0]

    restarted = AgentRuntime(
        planner=ScriptedPlanner(actions),
        tools=ToolRegistry(
            [ToolSpec("lookup", lambda: executions.append("called") or "value")]
        ),
        checkpoints=store,
    )
    result = asyncio.run(restarted.recover(run_id))

    assert result.status == RunStatus.COMPLETED
    assert executions == ["called"]
    assert result.steps[0].tool_calls[0].replayed_from_cache is True


def test_recovery_blocks_uncertain_write_replay(tmp_path):
    executions = []

    def write_then_crash():
        executions.append("written")
        raise SimulatedCrash("external write may have happened")

    actions = [
        {"kind": "tool", "tool": {"tool_name": "write", "arguments": {}}},
        {"kind": "finish", "result": {"ok": True}},
    ]
    store = SQLiteCheckpointStore(tmp_path / "runtime.sqlite3")
    crashing = AgentRuntime(
        planner=ScriptedPlanner(actions),
        tools=ToolRegistry([ToolSpec("write", write_then_crash, permission="write")]),
        checkpoints=store,
    )

    with pytest.raises(SimulatedCrash):
        asyncio.run(crashing.run(task_id="recovery-2", request="write"))
    run_id = store.list_run_ids()[0]
    restarted = AgentRuntime(
        planner=ScriptedPlanner(actions),
        tools=ToolRegistry([ToolSpec("write", write_then_crash, permission="write")]),
        checkpoints=store,
    )

    result = asyncio.run(restarted.recover(run_id))

    assert result.status == RunStatus.FAILED_CLOSED
    assert result.errors[0].code == "unsafe_replay_blocked"
    assert executions == ["written"]


def test_active_lease_blocks_recovery_by_another_worker(tmp_path):
    store = SQLiteCheckpointStore(tmp_path / "runtime.sqlite3")
    state = {
        "run_id": "leased-run",
        "task_id": "lease-1",
        "status": "running",
        "request": "continue",
        "observations": [],
        "steps": [],
        "errors": [],
        "result": None,
        "pending_action": None,
        "inflight_action": None,
        "inflight_human_confirmed": False,
        "tool_call_count": 0,
        "created_at": "2026-10-15T00:00:00+00:00",
        "updated_at": "2026-10-15T00:00:00+00:00",
    }
    store.save("leased-run", -1, state)
    assert store.acquire_lease("leased-run", "worker-a", ttl_seconds=60)
    runtime = AgentRuntime(
        planner=ScriptedPlanner([{"kind": "finish", "result": {"ok": True}}]),
        tools=ToolRegistry([]),
        checkpoints=store,
        owner_id="worker-b",
    )

    with pytest.raises(RuntimeLeaseBusy):
        asyncio.run(runtime.recover("leased-run"))


def test_lease_heartbeat_keeps_long_tool_execution_owned(tmp_path):
    async def slow_tool():
        await asyncio.sleep(0.08)
        return "done"

    planner = ScriptedPlanner(
        [
            {"kind": "tool", "tool": {"tool_name": "slow", "arguments": {}}},
            {"kind": "finish", "result": {"ok": True}},
        ]
    )
    runtime = AgentRuntime(
        planner=planner,
        tools=ToolRegistry([ToolSpec("slow", slow_tool)]),
        checkpoints=SQLiteCheckpointStore(tmp_path / "runtime.sqlite3"),
        lease_ttl_seconds=0.03,
    )

    result = asyncio.run(runtime.run(task_id="heartbeat-1", request="wait"))

    assert result.status == RunStatus.COMPLETED


def test_recovery_fails_closed_when_inflight_tool_was_removed(tmp_path):
    store = SQLiteCheckpointStore(tmp_path / "runtime.sqlite3")
    run_id = "00000000-0000-0000-0000-000000000024"
    state = {
        "run_id": run_id,
        "task_id": "drift-1",
        "status": "running",
        "request": "continue",
        "observations": [],
        "steps": [{"step_no": 0, "action_kind": "tool", "tool_calls": []}],
        "errors": [],
        "result": None,
        "pending_action": None,
        "inflight_action": {
            "kind": "tool",
            "tool": {"tool_name": "removed_tool", "arguments": {}},
            "tools": [],
            "message": None,
            "result": None,
        },
        "inflight_human_confirmed": False,
        "tool_call_count": 1,
        "created_at": "2026-10-15T00:00:00+00:00",
        "updated_at": "2026-10-15T00:00:00+00:00",
    }
    store.save(run_id, 0, state)
    runtime = AgentRuntime(
        planner=ScriptedPlanner([{"kind": "finish", "result": {"ok": True}}]),
        tools=ToolRegistry([]),
        checkpoints=store,
    )

    result = asyncio.run(runtime.recover(run_id))

    assert result.status == RunStatus.FAILED_CLOSED
    assert result.errors[0].code == "tool_not_allowed"


def test_external_idempotency_key_prevents_duplicate_side_effect_after_crash(tmp_path):
    external_effects = {}
    handler_calls = []

    def idempotent_write(value, runtime_context):
        key = runtime_context.idempotency_key
        handler_calls.append(key)
        if key not in external_effects:
            external_effects[key] = value
            raise SimulatedCrash("crash after remote commit, before local ledger")
        return {"stored": external_effects[key], "deduplicated": True}

    actions = [
        {
            "kind": "tool",
            "tool": {"tool_name": "idempotent_write", "arguments": {"value": "once"}},
        },
        {"kind": "finish", "result": {"ok": True}},
    ]
    store = SQLiteCheckpointStore(tmp_path / "runtime.sqlite3")
    tools = [
        ToolSpec(
            "idempotent_write",
            idempotent_write,
            permission="write",
            replay_safe=True,
            inject_context=True,
        )
    ]
    crashing = AgentRuntime(
        planner=ScriptedPlanner(actions),
        tools=ToolRegistry(tools),
        checkpoints=store,
    )

    with pytest.raises(SimulatedCrash):
        asyncio.run(crashing.run(task_id="external-idempotency", request="write"))
    run_id = store.list_run_ids()[0]
    restarted = AgentRuntime(
        planner=ScriptedPlanner(actions),
        tools=ToolRegistry(tools),
        checkpoints=store,
    )

    result = asyncio.run(restarted.recover(run_id))

    expected_key = f"{run_id}:0:0"
    assert result.status == RunStatus.COMPLETED
    assert external_effects == {expected_key: "once"}
    assert handler_calls == [expected_key, expected_key]
    assert result.steps[0].tool_calls[0].idempotency_key == expected_key
    assert result.steps[0].tool_calls[0].output["deduplicated"] is True


def test_tool_context_exposes_positive_fencing_token(tmp_path):
    contexts = []

    def inspect(runtime_context):
        contexts.append(runtime_context)
        return "ok"

    planner = ScriptedPlanner(
        [
            {"kind": "tool", "tool": {"tool_name": "inspect", "arguments": {}}},
            {"kind": "finish", "result": {"ok": True}},
        ]
    )
    runtime = make_runtime(
        tmp_path,
        planner,
        [ToolSpec("inspect", inspect, inject_context=True)],
    )

    result = asyncio.run(runtime.run(task_id="fencing-1", request="inspect"))

    assert result.status == RunStatus.COMPLETED
    assert len(contexts) == 1
    assert contexts[0].fencing_token == 1


def test_stale_worker_cannot_write_tool_trace_after_takeover(tmp_path):
    store = SQLiteCheckpointStore(tmp_path / "runtime.sqlite3")

    def force_takeover(runtime_context):
        assert store.release_lease(
            runtime_context.run_id,
            runtime_context.lease_owner,
            fencing_token=runtime_context.fencing_token,
        )
        takeover = store.acquire_lease_grant(
            runtime_context.run_id,
            "new-worker",
            ttl_seconds=60,
        )
        assert takeover is not None
        assert takeover[1] > runtime_context.fencing_token
        return "old worker finished too late"

    planner = ScriptedPlanner(
        [
            {"kind": "tool", "tool": {"tool_name": "takeover", "arguments": {}}},
            {"kind": "finish", "result": {"ok": True}},
        ]
    )
    runtime = AgentRuntime(
        planner=planner,
        tools=ToolRegistry(
            [ToolSpec("takeover", force_takeover, inject_context=True)]
        ),
        checkpoints=store,
        owner_id="old-worker",
    )

    with pytest.raises(RuntimeLeaseLost, match="stale tool trace"):
        asyncio.run(runtime.run(task_id="stale-write", request="take over"))

    run_id = store.list_run_ids()[0]
    assert store.load_tool_trace(f"{run_id}:0:0") is None
