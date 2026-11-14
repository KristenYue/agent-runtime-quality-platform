import asyncio

import pytest

from agent_runtime_platform.acceptance_benchmark import run_acceptance_benchmark


@pytest.mark.acceptance
def test_executable_acceptance_benchmark(tmp_path):
    report = asyncio.run(run_acceptance_benchmark(tmp_path))
    assert report["passed"] is True
    assert report["functional"] == {"total": 180, "passed": 180, "failed": 0}
    assert report["fault_injection"] == {
        "total": 70,
        "handled": 70,
        "recovered": 68,
        "failed_closed": 2,
        "unhandled": 0,
    }
    assert report["duplicate_submissions"] == {"groups": 60, "extra_final_writes": 0}
    assert report["evaluation_mode"] == "executable_local_synthetic_benchmark"
