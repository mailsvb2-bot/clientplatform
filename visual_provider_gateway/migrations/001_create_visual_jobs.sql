CREATE TABLE IF NOT EXISTS visual_jobs (
    id TEXT PRIMARY KEY,
    client_id TEXT NOT NULL DEFAULT 'legacy',
    scope_id TEXT NOT NULL DEFAULT 'global',
    idempotency_key TEXT NOT NULL DEFAULT '',
    request_fingerprint TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL,
    kind TEXT NOT NULL,
    status TEXT NOT NULL,
    provider_job_id TEXT NOT NULL,
    model TEXT NOT NULL,
    mime_type TEXT NOT NULL,
    asset_path TEXT NOT NULL,
    error_code TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
