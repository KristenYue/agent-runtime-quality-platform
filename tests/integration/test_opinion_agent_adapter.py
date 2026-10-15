"""Optional cross-repository smoke test for the real opinion LangGraph."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import sys

import pytest

from agent_runtime_platform import RunStatus
from agent_runtime_platform.adapters import OpinionAgentRuntimeAdapter


pytestmark = pytest.mark.integration


def test_real_opinion_graph_runs_through_runtime_adapter(tmp_path):
    project_value = os.getenv("OPINION_AGENT_PROJECT", "").strip()
    if not project_value:
        pytest.skip("set OPINION_AGENT_PROJECT to run cross-repository integration")
    project = Path(project_value).resolve()
    if not (project / "src" / "opinion_agent").is_dir():
        pytest.fail(f"invalid OPINION_AGENT_PROJECT: {project}")
    sys.path.insert(0, str(project / "src"))

    from opinion_agent.agent.graph import build_opinion_graph
    from opinion_agent.sentiment.classifier import Prediction

    class StubClassifier:
        def predict(self, text: str) -> Prediction:
            return Prediction("Positive", 0.8, {"Positive": 0.8}, text)

    graph = build_opinion_graph(StubClassifier())
    adapter = OpinionAgentRuntimeAdapter(
        graph_provider=lambda: graph,
        checkpoint_path=tmp_path / "real-opinion-adapter.sqlite3",
    )

    result = asyncio.run(
        adapter.analyze(
            {
                "event_id": "integration-event",
                "query": "生成事件分析",
                "comments": [{"sample_id": "sample-1", "text": "整体反馈很好"}],
            }
        )
    )

    assert result.status == RunStatus.COMPLETED
    assert result.result["aggregate_stats"]["counts"] == {"Positive": 1}
    assert result.result["analysis_report"]["review_status"] == "not_required"
    assert [trace["node"] for trace in result.result["tool_traces"]] == [
        "sentiment_classifier",
        "sentiment_aggregator",
        "briefing_composer",
    ]
