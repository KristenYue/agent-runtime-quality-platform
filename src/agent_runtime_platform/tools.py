"""Explicit tool registry and permission policy."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import inspect
from typing import Any, Literal


ToolHandler = Callable[..., Any] | Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class ToolExecutionContext:
    idempotency_key: str
    run_id: str
    step_no: int
    tool_index: int
    lease_owner: str
    fencing_token: int


@dataclass(frozen=True)
class ToolSpec:
    name: str
    handler: ToolHandler
    permission: Literal["read", "compute", "write", "dangerous"] = "read"
    replay_safe: bool = False
    inject_context: bool = False


class ToolRegistry:
    def __init__(self, tools: list[ToolSpec]) -> None:
        self._tools = {tool.name: tool for tool in tools}
        if len(self._tools) != len(tools):
            raise ValueError("tool names must be unique")

    def resolve(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ValueError(f"tool is not registered: {name}") from exc

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        human_confirmed: bool = False,
        runtime_context: ToolExecutionContext | None = None,
    ) -> Any:
        tool = self.resolve(name)
        if tool.permission == "dangerous" and not human_confirmed:
            raise PermissionError(f"tool requires human confirmation: {name}")
        call_arguments = dict(arguments)
        if tool.inject_context:
            if runtime_context is None:
                raise ValueError(f"runtime context is required by tool: {name}")
            if "runtime_context" in call_arguments:
                raise ValueError("tool arguments must not override runtime_context")
            call_arguments["runtime_context"] = runtime_context
        value = tool.handler(**call_arguments)
        return await value if inspect.isawaitable(value) else value

    def requires_confirmation(self, name: str) -> bool:
        return self.resolve(name).permission == "dangerous"

    def is_replay_safe(self, name: str) -> bool:
        tool = self.resolve(name)
        return tool.permission == "read" or tool.replay_safe
