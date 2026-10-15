import asyncio

import pytest

from agent_runtime_platform import (
    AgentRuntime,
    AsyncSQLiteBackend,
    RunStatus,
    SQLiteCheckpointStore,
    ToolRegistry,
)


class FinishPlanner:
    async def decide(self, *, request, observations, step):
        return {"kind": "finish", "result": {"request": request}}


class FalsySQLiteBackend(AsyncSQLiteBackend):
    def __bool__(self):
        return False


def test_runtime_accepts_async_backend_without_direct_checkpoint_store(tmp_path):
    store = SQLiteCheckpointStore(tmp_path / "backend.sqlite3")
    backend = AsyncSQLiteBackend(store)
    runtime = AgentRuntime(
        planner=FinishPlanner(),
        tools=ToolRegistry([]),
        backend=backend,
    )

    result = asyncio.run(runtime.run(task_id="backend-1", request="hello"))
    loaded = asyncio.run(runtime.get(result.run_id))

    assert result.status == RunStatus.COMPLETED
    assert loaded == result
    assert store.count(str(result.run_id)) == 2


def test_runtime_requires_exactly_one_persistence_entrypoint(tmp_path):
    store = SQLiteCheckpointStore(tmp_path / "backend.sqlite3")
    backend = AsyncSQLiteBackend(store)

    with pytest.raises(ValueError, match="exactly one"):
        AgentRuntime(planner=FinishPlanner(), tools=ToolRegistry([]))
    with pytest.raises(ValueError, match="exactly one"):
        AgentRuntime(
            planner=FinishPlanner(),
            tools=ToolRegistry([]),
            checkpoints=store,
            backend=backend,
        )


def test_explicit_falsy_backend_is_not_replaced(tmp_path):
    backend = FalsySQLiteBackend(
        SQLiteCheckpointStore(tmp_path / "falsy-backend.sqlite3")
    )
    runtime = AgentRuntime(
        planner=FinishPlanner(),
        tools=ToolRegistry([]),
        backend=backend,
    )

    result = asyncio.run(runtime.run(task_id="backend-falsy", request="kept"))

    assert runtime.backend is backend
    assert result.status == RunStatus.COMPLETED
