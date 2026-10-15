"""Small, deterministic paired-regression gate for runtime releases."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .contracts import RunResult, RunStatus


class RunRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    category: str
    succeeded: bool
    quality_score: float = Field(ge=0, le=1)
    error_count: int = Field(ge=0)
    tool_call_count: int = Field(ge=0)
    latency_ms: float = Field(default=0.0, ge=0)
    estimated_cost_usd: float = Field(default=0.0, ge=0)


class GateThresholds(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_success_rate_drop: float = Field(default=0.0, ge=0, le=1)
    max_quality_drop: float = Field(default=0.02, ge=0, le=1)
    max_error_increase: float = Field(default=0.0, ge=0)
    max_latency_increase_ms: float = Field(default=100.0, ge=0)
    max_cost_increase_usd: float = Field(default=0.01, ge=0)


class GateReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    passed: bool
    paired_cases: int
    baseline: dict[str, float]
    candidate: dict[str, float]
    deltas: dict[str, float]
    failures: list[str]
    category_deltas: dict[str, dict[str, float]]


def record_from_result(
    *,
    case_id: str,
    category: str,
    result: RunResult,
    quality_score: float,
    latency_ms: float = 0.0,
    estimated_cost_usd: float = 0.0,
) -> RunRecord:
    return RunRecord(
        case_id=case_id,
        category=category,
        succeeded=result.status == RunStatus.COMPLETED,
        quality_score=quality_score,
        error_count=len(result.errors),
        tool_call_count=result.tool_call_count,
        latency_ms=latency_ms,
        estimated_cost_usd=estimated_cost_usd,
    )


def paired_regression_gate(
    baseline: list[RunRecord],
    candidate: list[RunRecord],
    *,
    thresholds: GateThresholds | None = None,
) -> GateReport:
    limits = thresholds or GateThresholds()
    baseline_by_id = _unique_by_case(baseline, "baseline")
    candidate_by_id = _unique_by_case(candidate, "candidate")
    if baseline_by_id.keys() != candidate_by_id.keys():
        missing = sorted(baseline_by_id.keys() - candidate_by_id.keys())
        extra = sorted(candidate_by_id.keys() - baseline_by_id.keys())
        raise ValueError(f"paired case mismatch; missing={missing}, extra={extra}")
    if not baseline_by_id:
        raise ValueError("paired evaluation requires at least one case")

    ordered_ids = sorted(baseline_by_id)
    for case_id in ordered_ids:
        if baseline_by_id[case_id].category != candidate_by_id[case_id].category:
            raise ValueError(f"category mismatch for case: {case_id}")

    base_summary = _summary([baseline_by_id[key] for key in ordered_ids])
    candidate_summary = _summary([candidate_by_id[key] for key in ordered_ids])
    deltas = {
        key: candidate_summary[key] - base_summary[key]
        for key in base_summary
    }
    failures: list[str] = []
    if deltas["success_rate"] < -limits.max_success_rate_drop:
        failures.append("success_rate_regression")
    if deltas["mean_quality"] < -limits.max_quality_drop:
        failures.append("quality_regression")
    if deltas["mean_errors"] > limits.max_error_increase:
        failures.append("error_rate_regression")
    if deltas["mean_latency_ms"] > limits.max_latency_increase_ms:
        failures.append("latency_regression")
    if deltas["mean_cost_usd"] > limits.max_cost_increase_usd:
        failures.append("cost_regression")

    grouped: dict[str, list[str]] = defaultdict(list)
    for case_id in ordered_ids:
        grouped[baseline_by_id[case_id].category].append(case_id)
    category_deltas: dict[str, dict[str, float]] = {}
    for category, case_ids in sorted(grouped.items()):
        base = _summary([baseline_by_id[key] for key in case_ids])
        cand = _summary([candidate_by_id[key] for key in case_ids])
        category_deltas[category] = {key: cand[key] - base[key] for key in base}

    return GateReport(
        passed=not failures,
        paired_cases=len(ordered_ids),
        baseline=base_summary,
        candidate=candidate_summary,
        deltas=deltas,
        failures=failures,
        category_deltas=category_deltas,
    )


def _unique_by_case(records: list[RunRecord], label: str) -> dict[str, RunRecord]:
    result: dict[str, RunRecord] = {}
    for record in records:
        if record.case_id in result:
            raise ValueError(f"duplicate {label} case_id: {record.case_id}")
        result[record.case_id] = record
    return result


def _summary(records: list[RunRecord]) -> dict[str, float]:
    total = len(records)
    return {
        "success_rate": sum(record.succeeded for record in records) / total,
        "mean_quality": sum(record.quality_score for record in records) / total,
        "mean_errors": sum(record.error_count for record in records) / total,
        "mean_tool_calls": sum(record.tool_call_count for record in records) / total,
        "mean_latency_ms": sum(record.latency_ms for record in records) / total,
        "mean_cost_usd": sum(record.estimated_cost_usd for record in records) / total,
    }
