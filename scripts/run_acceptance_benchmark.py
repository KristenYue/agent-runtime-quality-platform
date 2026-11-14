"""Run and persist the executable Runtime acceptance benchmark."""

import asyncio
import json
import tempfile
from pathlib import Path

from agent_runtime_platform.acceptance_benchmark import run_acceptance_benchmark


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "data" / "evaluation" / "acceptance_v1" / "report.json"
WORK_ROOT = ROOT / ".pytest-tmp-codex"
WORK_ROOT.mkdir(parents=True, exist_ok=True)
with tempfile.TemporaryDirectory(
    prefix="agent-runtime-acceptance-", dir=WORK_ROOT, ignore_cleanup_errors=True
) as directory:
    report = asyncio.run(run_acceptance_benchmark(Path(directory)))
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
print(json.dumps({key: report[key] for key in ("functional", "fault_injection", "duplicate_submissions", "passed")}, indent=2))
raise SystemExit(0 if report["passed"] else 1)
