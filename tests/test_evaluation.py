import pytest

from agent_runtime_platform.evaluation import (
    GateThresholds,
    RunRecord,
    paired_regression_gate,
)


def record(
    case_id,
    category,
    succeeded=True,
    quality=0.9,
    errors=0,
    calls=1,
    latency_ms=50,
    cost_usd=0.01,
):
    return RunRecord(
        case_id=case_id,
        category=category,
        succeeded=succeeded,
        quality_score=quality,
        error_count=errors,
        tool_call_count=calls,
        latency_ms=latency_ms,
        estimated_cost_usd=cost_usd,
    )


def test_paired_gate_passes_non_regression_and_reports_categories():
    baseline = [record("a", "search", quality=0.8), record("b", "write", quality=0.9)]
    candidate = [record("a", "search", quality=0.82), record("b", "write", quality=0.9)]

    report = paired_regression_gate(baseline, candidate)

    assert report.passed is True
    assert report.paired_cases == 2
    assert report.deltas["mean_quality"] == pytest.approx(0.01)
    assert set(report.category_deltas) == {"search", "write"}


def test_paired_gate_fails_on_success_quality_and_error_regressions():
    baseline = [record("a", "search"), record("b", "search")]
    candidate = [
        record("a", "search", succeeded=False, quality=0.3, errors=1),
        record("b", "search", quality=0.8),
    ]

    report = paired_regression_gate(
        baseline,
        candidate,
        thresholds=GateThresholds(
            max_success_rate_drop=0,
            max_quality_drop=0.02,
            max_error_increase=0,
        ),
    )

    assert report.passed is False
    assert set(report.failures) == {
        "success_rate_regression",
        "quality_regression",
        "error_rate_regression",
    }


def test_paired_gate_rejects_unpaired_cases():
    with pytest.raises(ValueError, match="paired case mismatch"):
        paired_regression_gate([record("a", "search")], [record("b", "search")])


def test_paired_gate_fails_on_latency_and_cost_regressions():
    baseline = [record("a", "opinion", latency_ms=100, cost_usd=0.01)]
    candidate = [record("a", "opinion", latency_ms=151, cost_usd=0.016)]

    report = paired_regression_gate(
        baseline,
        candidate,
        thresholds=GateThresholds(
            max_latency_increase_ms=50,
            max_cost_increase_usd=0.005,
        ),
    )

    assert report.passed is False
    assert set(report.failures) == {"latency_regression", "cost_regression"}
    assert report.deltas["mean_latency_ms"] == 51
    assert report.deltas["mean_cost_usd"] == pytest.approx(0.006)


def test_paired_gate_reports_latency_and_cost_by_category():
    baseline = [record("a", "standard", latency_ms=80, cost_usd=0.02)]
    candidate = [record("a", "standard", latency_ms=90, cost_usd=0.018)]

    report = paired_regression_gate(baseline, candidate)

    assert report.passed is True
    assert report.baseline["mean_latency_ms"] == 80
    assert report.candidate["mean_cost_usd"] == pytest.approx(0.018)
    assert report.category_deltas["standard"]["mean_latency_ms"] == 10
    assert report.category_deltas["standard"]["mean_cost_usd"] == pytest.approx(
        -0.002
    )
