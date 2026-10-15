import pytest

from agent_runtime_platform.evaluation import (
    GateThresholds,
    RunRecord,
    paired_regression_gate,
)


def record(case_id, category, succeeded=True, quality=0.9, errors=0, calls=1):
    return RunRecord(
        case_id=case_id,
        category=category,
        succeeded=succeeded,
        quality_score=quality,
        error_count=errors,
        tool_call_count=calls,
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
