"""Thin adapter for the existing opinion-analysis LangGraph contract."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field

from ..checkpoint import SQLiteCheckpointStore
from ..contracts import PlannerAction, RunResult
from ..runtime import AgentRuntime
from ..tools import ToolRegistry, ToolSpec


class OpinionComment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_id: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=5000)
    context: str | None = Field(default=None, max_length=10000)
    source_url: str | None = Field(default=None, max_length=2000)


class OpinionAnalysisRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=200)
    query: str = Field(min_length=1, max_length=2000)
    comments: list[OpinionComment] = Field(min_length=1, max_length=500)


class OpinionGraph(Protocol):
    def invoke(self, state: dict[str, Any]) -> dict[str, Any]: ...


PayloadTransform = Callable[[dict[str, Any]], dict[str, Any]]


class _OpinionPlanner:
    async def decide(
        self,
        *,
        request: str,
        observations: tuple[dict[str, Any], ...],
        step: int,
    ) -> PlannerAction:
        if step == 0:
            return PlannerAction(
                kind="tool",
                tool={
                    "tool_name": "analyze_opinion",
                    "arguments": {"payload": json.loads(request)},
                },
            )
        if step == 1 and len(observations) == 1:
            observation = observations[0]
            output = observation.get("output")
            if observation.get("status") != "succeeded" or not isinstance(output, dict):
                raise ValueError("opinion analysis tool did not return a successful object")
            return PlannerAction(kind="finish", result=output)
        raise ValueError(f"unexpected opinion adapter state at step {step}")


class OpinionAgentRuntimeAdapter:
    """Expose an existing synchronous opinion graph through the durable runtime.

    The graph is classified as ``compute`` rather than ``read``: it may call a
    paid or nondeterministic model, so an uncertain crash is not replayed unless
    its completed trace already reached the idempotency ledger.
    """

    def __init__(
        self,
        *,
        graph_provider: Callable[[], OpinionGraph],
        checkpoint_path: str | Path,
        input_transform: PayloadTransform | None = None,
        output_transform: PayloadTransform | None = None,
        lease_ttl_seconds: float = 60.0,
        owner_id: str | None = None,
    ) -> None:
        self.graph_provider = graph_provider
        self.input_transform = input_transform or (lambda value: value)
        self.output_transform = output_transform or (lambda value: value)
        checkpoints = SQLiteCheckpointStore(checkpoint_path)
        self.runtime = AgentRuntime(
            planner=_OpinionPlanner(),
            tools=ToolRegistry(
                [
                    ToolSpec(
                        "analyze_opinion",
                        self._invoke_graph,
                        permission="compute",
                        replay_safe=False,
                    )
                ]
            ),
            checkpoints=checkpoints,
            max_tool_calls=1,
            max_steps=2,
            lease_ttl_seconds=lease_ttl_seconds,
            owner_id=owner_id,
        )

    async def analyze(
        self,
        request: OpinionAnalysisRequest | dict[str, Any],
        *,
        task_id: str | None = None,
    ) -> RunResult:
        validated = OpinionAnalysisRequest.model_validate(request)
        payload = validated.model_dump(mode="json", exclude_none=True)
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return await self.runtime.run(
            task_id=task_id or validated.event_id,
            request=serialized,
        )

    async def recover(self, run_id: UUID | str) -> RunResult:
        return await self.runtime.recover(run_id)

    async def _invoke_graph(self, payload: dict[str, Any]) -> dict[str, Any]:
        safe_payload = self.input_transform(payload)
        if not isinstance(safe_payload, dict):
            raise TypeError("input_transform must return a dictionary")
        state = {
            "request_id": str(uuid4()),
            "event_id": safe_payload["event_id"],
            "query": safe_payload["query"],
            "comments": safe_payload["comments"],
            "tool_traces": [],
            "errors": [],
        }
        graph = await asyncio.to_thread(self.graph_provider)
        invoke = getattr(graph, "invoke", None)
        if not callable(invoke):
            raise TypeError("graph_provider must return an object with invoke(state)")
        raw_result = await asyncio.to_thread(invoke, state)
        if not isinstance(raw_result, dict):
            raise TypeError("opinion graph result must be a dictionary")
        result = self.output_transform(raw_result)
        self._validate_result(result, expected_event_id=safe_payload["event_id"])
        return result

    @staticmethod
    def _validate_result(result: Any, *, expected_event_id: str) -> None:
        if not isinstance(result, dict):
            raise TypeError("output_transform must return a dictionary")
        if result.get("event_id") != expected_event_id:
            raise ValueError("opinion graph result event_id does not match request")
        if not isinstance(result.get("analysis_report"), dict):
            raise ValueError("opinion graph result is missing analysis_report")
        if not isinstance(result.get("tool_traces"), list):
            raise ValueError("opinion graph result is missing tool_traces")
        if not isinstance(result.get("errors"), list):
            raise ValueError("opinion graph result is missing errors")
