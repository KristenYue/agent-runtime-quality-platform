import asyncio

from agent_runtime_platform import RunStatus
from agent_runtime_platform.adapters import (
    OpinionAgentRuntimeAdapter,
    OpinionAnalysisRequest,
)


class FakeOpinionGraph:
    def __init__(self):
        self.states = []

    def invoke(self, state):
        self.states.append(state)
        return {
            **state,
            "analysis_report": {
                "event_id": state["event_id"],
                "review_status": "not_required",
            },
            "final_report": "analysis complete",
        }


def test_adapter_maps_request_to_real_graph_state_contract(tmp_path):
    graph = FakeOpinionGraph()
    adapter = OpinionAgentRuntimeAdapter(
        graph_provider=lambda: graph,
        checkpoint_path=tmp_path / "adapter.sqlite3",
    )
    request = OpinionAnalysisRequest.model_validate(
        {
            "event_id": "事件A",
            "query": "分析公众反馈",
            "comments": [
                {"sample_id": "1", "text": "支持", "context": None},
            ],
        }
    )

    result = asyncio.run(adapter.analyze(request))

    assert result.status == RunStatus.COMPLETED
    assert result.task_id == "事件A"
    assert result.result["final_report"] == "analysis complete"
    assert graph.states[0]["event_id"] == "事件A"
    assert graph.states[0]["comments"] == [{"sample_id": "1", "text": "支持"}]
    assert graph.states[0]["tool_traces"] == []
    assert graph.states[0]["errors"] == []


def test_adapter_runs_injected_security_transforms(tmp_path):
    graph = FakeOpinionGraph()
    calls = []

    def input_transform(payload):
        calls.append("input")
        return {**payload, "query": "[redacted]"}

    def output_transform(result):
        calls.append("output")
        return {**result, "final_report": "[safe]"}

    adapter = OpinionAgentRuntimeAdapter(
        graph_provider=lambda: graph,
        checkpoint_path=tmp_path / "adapter.sqlite3",
        input_transform=input_transform,
        output_transform=output_transform,
    )

    result = asyncio.run(
        adapter.analyze(
            {
                "event_id": "event-a",
                "query": "secret",
                "comments": [{"sample_id": "1", "text": "comment"}],
            }
        )
    )

    assert result.status == RunStatus.COMPLETED
    assert graph.states[0]["query"] == "[redacted]"
    assert result.result["final_report"] == "[safe]"
    assert calls == ["input", "output"]


def test_adapter_fails_closed_on_output_contract_violation(tmp_path):
    class InvalidGraph:
        def invoke(self, state):
            return {**state, "final_report": "missing structured report"}

    adapter = OpinionAgentRuntimeAdapter(
        graph_provider=InvalidGraph,
        checkpoint_path=tmp_path / "adapter.sqlite3",
    )

    result = asyncio.run(
        adapter.analyze(
            {
                "event_id": "event-a",
                "query": "analyze",
                "comments": [{"sample_id": "1", "text": "comment"}],
            }
        )
    )

    assert result.status == RunStatus.FAILED_CLOSED
    assert result.errors[0].code == "tool_execution_failed"
    assert result.steps[0].tool_calls[0].error_type == "ValueError"
