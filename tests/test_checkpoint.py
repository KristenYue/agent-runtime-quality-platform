import sqlite3

import pytest

from agent_runtime_platform.checkpoint import SQLiteCheckpointStore


def test_checkpoint_round_trip_and_count(tmp_path):
    store = SQLiteCheckpointStore(tmp_path / "nested" / "runs.sqlite3")
    state = {"status": "running", "message": "中文状态"}

    digest = store.save("run-1", 0, state)

    assert len(digest) == 64
    assert store.load_latest("run-1") == state
    assert store.count("run-1") == 1


def test_checkpoint_detects_tampering(tmp_path):
    path = tmp_path / "runs.sqlite3"
    store = SQLiteCheckpointStore(path)
    store.save("run-1", 0, {"status": "running"})
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE checkpoints SET state_json = ? WHERE run_id = ?",
            ('{"status":"completed"}', "run-1"),
        )

    with pytest.raises(ValueError, match="integrity"):
        store.load_latest("run-1")


def test_lease_excludes_other_owner_until_expiry(tmp_path):
    store = SQLiteCheckpointStore(tmp_path / "runs.sqlite3")

    assert store.acquire_lease("run-1", "worker-a", ttl_seconds=10, now=100)
    assert not store.acquire_lease("run-1", "worker-b", ttl_seconds=10, now=105)
    assert store.renew_lease("run-1", "worker-a", ttl_seconds=10, now=106)
    assert not store.release_lease("run-1", "worker-b")
    assert store.acquire_lease("run-1", "worker-b", ttl_seconds=10, now=117)
    assert not store.renew_lease("run-1", "worker-a", ttl_seconds=10, now=118)
    assert store.release_lease("run-1", "worker-b")


def test_same_owner_can_renew_after_scheduler_delay_when_not_taken_over(tmp_path):
    store = SQLiteCheckpointStore(tmp_path / "runs.sqlite3")

    assert store.acquire_lease("run-1", "worker-a", ttl_seconds=1, now=100)
    assert store.renew_lease("run-1", "worker-a", ttl_seconds=1, now=102)
    assert not store.acquire_lease("run-1", "worker-b", ttl_seconds=1, now=102.5)


def test_fencing_token_increments_after_release_and_rejects_stale_owner(tmp_path):
    store = SQLiteCheckpointStore(tmp_path / "runs.sqlite3")

    first = store.acquire_lease_grant("run-1", "worker-a", ttl_seconds=10, now=100)
    assert first == ("worker-a", 1)
    assert store.release_lease("run-1", "worker-a", fencing_token=1)
    second = store.acquire_lease_grant("run-1", "worker-b", ttl_seconds=10, now=101)
    assert second == ("worker-b", 2)
    assert not store.renew_lease(
        "run-1", "worker-a", ttl_seconds=10, fencing_token=1, now=102
    )
    assert not store.release_lease("run-1", "worker-a", fencing_token=1)


def test_fenced_checkpoint_and_tool_trace_reject_stale_token(tmp_path):
    store = SQLiteCheckpointStore(tmp_path / "runs.sqlite3")
    first = store.acquire_lease_grant("run-1", "worker-a", ttl_seconds=10, now=100)
    assert first == ("worker-a", 1)
    assert store.save_fenced(
        "run-1",
        0,
        {"status": "running"},
        owner_id="worker-a",
        fencing_token=1,
        now=101,
    )
    assert store.save_tool_trace_fenced(
        "run-1:0:0",
        {"status": "succeeded"},
        run_id="run-1",
        owner_id="worker-a",
        fencing_token=1,
        now=101,
    )
    assert store.release_lease("run-1", "worker-a", fencing_token=1)
    assert store.acquire_lease_grant("run-1", "worker-b", ttl_seconds=10, now=102)

    assert (
        store.save_fenced(
            "run-1",
            1,
            {"status": "stale"},
            owner_id="worker-a",
            fencing_token=1,
            now=103,
        )
        is None
    )
    assert not store.save_tool_trace_fenced(
        "run-1:1:0",
        {"status": "stale"},
        run_id="run-1",
        owner_id="worker-a",
        fencing_token=1,
        now=103,
    )


def test_tool_trace_round_trip_and_integrity(tmp_path):
    path = tmp_path / "runs.sqlite3"
    store = SQLiteCheckpointStore(path)
    trace = {"tool_name": "search", "status": "succeeded", "output": [1, 2]}
    store.save_tool_trace("run:0:0", trace)

    assert store.load_tool_trace("run:0:0") == trace
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE tool_results SET trace_json = ? WHERE idempotency_key = ?",
            ('{"status":"tampered"}', "run:0:0"),
        )
    with pytest.raises(ValueError, match="integrity"):
        store.load_tool_trace("run:0:0")
