CREATE TABLE generation_requests (
    project_id UUID NOT NULL REFERENCES projects(id),
    idempotency_key TEXT NOT NULL CHECK (
        btrim(idempotency_key) <> '' AND char_length(idempotency_key) <= 200
    ),
    request_hash TEXT NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    job_id UUID NOT NULL UNIQUE REFERENCES jobs(id),
    artifact_id UUID NOT NULL,
    artifact_version INTEGER NOT NULL CHECK (artifact_version > 0),
    source_ids UUID[] NOT NULL CHECK (cardinality(source_ids) > 0),
    template_version TEXT NOT NULL CHECK (btrim(template_version) <> ''),
    budget_units INTEGER NOT NULL CHECK (budget_units > 0),
    project_revision INTEGER NOT NULL CHECK (project_revision > 0),
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (project_id, idempotency_key),
    UNIQUE (artifact_id, artifact_version)
);

CREATE INDEX generation_requests_project_artifact_idx
    ON generation_requests (project_id, artifact_id, artifact_version);
