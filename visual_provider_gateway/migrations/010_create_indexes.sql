CREATE UNIQUE INDEX IF NOT EXISTS ux_visual_jobs_client_scope_idempotency
ON visual_jobs(client_id, scope_id, idempotency_key)
WHERE idempotency_key <> '';

CREATE INDEX IF NOT EXISTS ix_visual_jobs_client_created
ON visual_jobs(client_id, created_at);
