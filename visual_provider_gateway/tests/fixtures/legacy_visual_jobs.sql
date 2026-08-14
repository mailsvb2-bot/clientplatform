CREATE TABLE visual_jobs (
    id TEXT PRIMARY KEY,
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

INSERT INTO visual_jobs(
    id, provider, kind, status, provider_job_id, model,
    mime_type, asset_path, error_code, created_at, updated_at
) VALUES (
    'legacy-job', 'yandexart', 'image', 'succeeded', 'op-1',
    'art://folder/yandex-art/latest', 'image/jpeg', '/data/output/a.jpg',
    '', 1, 2
);
