import asyncio

import pytest

from agent_runtime_platform.tools import ToolExecutionContext, ToolRegistry, ToolSpec


def test_registry_supports_sync_and_async_handlers():
    async def double(value):
        await asyncio.sleep(0)
        return value * 2

    registry = ToolRegistry(
        [
            ToolSpec("add", lambda left, right: left + right),
            ToolSpec("double", double),
        ]
    )

    assert asyncio.run(registry.execute("add", {"left": 2, "right": 3})) == 5
    assert asyncio.run(registry.execute("double", {"value": 4})) == 8


def test_dangerous_tool_requires_confirmation():
    registry = ToolRegistry(
        [ToolSpec("delete_record", lambda: "deleted", permission="dangerous")]
    )

    with pytest.raises(PermissionError, match="human confirmation"):
        asyncio.run(registry.execute("delete_record", {}))
    assert (
        asyncio.run(
            registry.execute("delete_record", {}, human_confirmed=True)
        )
        == "deleted"
    )


def test_registry_rejects_duplicate_names():
    with pytest.raises(ValueError, match="unique"):
        ToolRegistry([ToolSpec("same", lambda: 1), ToolSpec("same", lambda: 2)])


def test_registry_injects_runtime_context_without_mutating_arguments():
    received = []

    def external_write(value, runtime_context):
        received.append(runtime_context)
        return value

    registry = ToolRegistry(
        [ToolSpec("write", external_write, permission="write", inject_context=True)]
    )
    context = ToolExecutionContext(
        idempotency_key="run:0:0",
        run_id="run",
        step_no=0,
        tool_index=0,
        lease_owner="worker-a",
        fencing_token=1,
    )
    arguments = {"value": "saved"}

    output = asyncio.run(
        registry.execute("write", arguments, runtime_context=context)
    )

    assert output == "saved"
    assert arguments == {"value": "saved"}
    assert received == [context]


def test_tool_arguments_cannot_override_runtime_context():
    registry = ToolRegistry(
        [ToolSpec("write", lambda **_: None, inject_context=True)]
    )
    context = ToolExecutionContext("key", "run", 0, 0, "worker", 1)

    with pytest.raises(ValueError, match="must not override"):
        asyncio.run(
            registry.execute(
                "write",
                {"runtime_context": "forged"},
                runtime_context=context,
            )
        )
