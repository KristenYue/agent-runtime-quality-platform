import asyncio

from agent_runtime_platform.resume_target_benchmark import run_resume_target_benchmark


def test_executable_resume_target_benchmark(tmp_path):
    report = asyncio.run(run_resume_target_benchmark(tmp_path))
    assert report["passed"] is True
    assert report["functional"] == {"total": 180, "passed": 173, "failed": 7}
    assert report["fault_injection"] == {"total": 70, "recovered": 68, "failed": 2}
    assert report["duplicate_submissions"] == {"groups": 60, "extra_final_writes": 0}
    assert report["evaluation_mode"] == "executable_local_synthetic_benchmark"
