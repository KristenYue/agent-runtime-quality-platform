"""Run and persist the executable resume-target benchmark."""

import asyncio
import json
import tempfile
from pathlib import Path

from agent_runtime_platform.resume_target_benchmark import run_resume_target_benchmark


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data" / "evaluation" / "resume_target_live_v1" / "report.json"
WORK_ROOT = ROOT / ".pytest-tmp-codex"
WORK_ROOT.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(
    prefix="agent-runtime-resume-target-", dir=WORK_ROOT, ignore_cleanup_errors=True
) as directory:
    report = asyncio.run(run_resume_target_benchmark(Path(directory)))
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
print(json.dumps({key: report[key] for key in ("functional", "fault_injection", "duplicate_submissions", "passed")}, indent=2))
