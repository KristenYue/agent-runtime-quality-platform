-- Agent Runtime Quality Platform PostgreSQL backend.
-- These runtime_* tables are intentionally separate from opinion-agent V2
-- analysis_jobs, agent_runs, run_checkpoints, and tool_calls.

CREATE TABLE IF NOT EXISTS runtime_checkpoints (
    run_id UUID NOT NULL,
    step_no INTEGER NOT NULL CHECK (step_no >= -1),
    state JSONB NOT NULL,
    state_sha256 CHAR(64) NOT NULL CHECK (state_sha256 ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (run_id, step_no)
);

CREATE TABLE IF NOT EXISTS runtime_run_leases (
    run_id UUID PRIMARY KEY,
    owner_id VARCHAR(200) NOT NULL,
    fencing_token BIGINT NOT NULL CHECK (fencing_token >= 1),
    expires_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_runtime_run_leases_expiry
    ON runtime_run_leases(expires_at);

CREATE TABLE IF NOT EXISTS runtime_tool_results (
    idempotency_key VARCHAR(300) PRIMARY KEY,
    trace JSONB NOT NULL,
    trace_sha256 CHAR(64) NOT NULL CHECK (trace_sha256 ~ '^[0-9a-f]{64}$'),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

COMMENT ON TABLE runtime_checkpoints IS
    'Provider-neutral Runtime state; not opinion-agent V2 replay checkpoints.';
COMMENT ON COLUMN runtime_run_leases.fencing_token IS
    'Monotonic per-run token; downstream writers should reject stale tokens.';
