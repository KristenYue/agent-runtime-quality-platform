"""Provider-neutral runtime and trace contracts."""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RunStatus(StrEnum):
    RUNNING = "running"
    WAITING_REVIEW = "waiting_review"
    COMPLETED = "completed"
    FAILED_CLOSED = "failed_closed"


class ToolCallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool_name: str = Field(min_length=1, max_length=100)
    arguments: dict[str, Any] = Field(default_factory=dict)


class PlannerAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["tool", "tool_batch", "pause_for_review", "finish"]
    tool: ToolCallRequest | None = None
    tools: list[ToolCallRequest] = Field(default_factory=list, max_length=16)
    message: str | None = Field(default=None, max_length=2000)
    result: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> "PlannerAction":
        if self.kind == "tool" and self.tool is None:
            raise ValueError("tool action requires tool")
        if self.kind == "tool_batch" and not self.tools:
            raise ValueError("tool_batch action requires tools")
        if self.kind == "finish" and self.result is None:
            raise ValueError("finish action requires result")
        return self


class Planner(Protocol):
    async def decide(
        self,
        *,
        request: str,
        observations: tuple[dict[str, Any], ...],
        step: int,
    ) -> PlannerAction | dict[str, Any]: ...


class ToolCallTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    call_id: UUID = Field(default_factory=uuid4)
    idempotency_key: str | None = None
    tool_name: str
    arguments: dict[str, Any]
    status: Literal["succeeded", "failed_closed"]
    output: Any = None
    error_type: str | None = None
    duration_ms: float = Field(ge=0)
    replayed_from_cache: bool = False


class StepTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_no: int = Field(ge=0)
    action_kind: str
    tool_calls: list[ToolCallTrace] = Field(default_factory=list)


class RuntimeErrorTrace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str
    step_no: int
    recoverable: bool


class RunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: UUID
    task_id: str
    status: RunStatus
    request: str
    steps: list[StepTrace]
    errors: list[RuntimeErrorTrace]
    result: dict[str, Any] | None = None
    pending_action: PlannerAction | None = None
    tool_call_count: int = Field(ge=0)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
