from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_ci_separates_unit_and_real_postgres_gates():
    workflow = PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
    text = workflow.read_text(encoding="utf-8")

    assert "unit-and-contract:" in text
    assert 'python -m pytest -m "not integration"' in text
    assert "postgres-integration:" in text
    assert "image: postgres:16-alpine" in text
    assert "RUNTIME_DATABASE_URL:" in text
    assert 'python -m pip install -e ".[dev,postgres]"' in text
    assert "tests/integration/test_postgres_runtime_backend.py" in text
    assert "actions/upload-artifact@v4" in text
    assert "OPINION_AGENT_PROJECT" not in text


def test_postgres_ci_cannot_silently_run_without_its_dsn():
    text = (
        PROJECT_ROOT / ".github" / "workflows" / "ci.yml"
    ).read_text(encoding="utf-8")
    postgres_job = text.split("  postgres-integration:", 1)[1]

    assert "RUNTIME_DATABASE_URL:" in postgres_job
    assert "postgresql://runtime_test:integration-only@localhost:5432/runtime_test" in postgres_job
    assert "test_postgres_runtime_backend.py" in postgres_job
