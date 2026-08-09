CREATE TABLE projects (
    id UUID PRIMARY KEY,
    title TEXT NOT NULL CHECK (btrim(title) <> ''),
    status TEXT NOT NULL CHECK (
        status IN ('draft', 'generating', 'review_required', 'approved', 'publishing', 'published', 'failed')
    ),
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL CHECK (updated_at >= created_at),
    revision INTEGER NOT NULL CHECK (revision > 0),
    failed_stage TEXT CHECK (failed_stage IN ('generation', 'publication')),
    CHECK ((status = 'failed') = (failed_stage IS NOT NULL))
);

CREATE TABLE sources (
    id UUID PRIMARY KEY,
    kind TEXT NOT NULL CHECK (
        kind IN ('official_documentation', 'paper', 'institution', 'trusted_media')
    ),
    title TEXT NOT NULL CHECK (btrim(title) <> ''),
    uri TEXT NOT NULL CHECK (btrim(uri) <> ''),
    retrieved_at TIMESTAMPTZ NOT NULL,
    sha256 TEXT NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    summary TEXT NOT NULL CHECK (btrim(summary) <> '')
);

CREATE TABLE source_excerpts (
    source_id UUID NOT NULL REFERENCES sources(id),
    id UUID NOT NULL,
    position INTEGER NOT NULL CHECK (position >= 0),
    text TEXT NOT NULL CHECK (btrim(text) <> ''),
    locator TEXT,
    PRIMARY KEY (source_id, position),
    UNIQUE (id),
    UNIQUE (source_id, id)
);

CREATE TABLE artifacts (
    id UUID NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    kind TEXT NOT NULL CHECK (
        kind IN ('fact_card', 'script', 'storyboard', 'media', 'audio', 'subtitle', 'cover', 'video', 'publication_package')
    ),
    sha256 TEXT NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    project_id UUID NOT NULL REFERENCES projects(id),
    storage_path TEXT NOT NULL CHECK (
        storage_path <> '' AND storage_path !~ '^/' AND storage_path !~ '(^|/)\.\.(/|$)'
    ),
    created_at TIMESTAMPTZ NOT NULL,
    created_by TEXT NOT NULL CHECK (btrim(created_by) <> ''),
    adapter TEXT NOT NULL CHECK (btrim(adapter) <> ''),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(metadata) = 'object'),
    PRIMARY KEY (id, version),
    UNIQUE (project_id, id, version),
    UNIQUE (project_id, id, version, kind)
);

CREATE TABLE artifact_upstream (
    project_id UUID NOT NULL,
    artifact_id UUID NOT NULL,
    artifact_version INTEGER NOT NULL,
    position INTEGER NOT NULL CHECK (position >= 0),
    upstream_id UUID NOT NULL,
    upstream_version INTEGER NOT NULL,
    PRIMARY KEY (artifact_id, artifact_version, position),
    UNIQUE (artifact_id, artifact_version, upstream_id, upstream_version),
    FOREIGN KEY (project_id, artifact_id, artifact_version)
        REFERENCES artifacts(project_id, id, version),
    FOREIGN KEY (project_id, upstream_id, upstream_version)
        REFERENCES artifacts(project_id, id, version)
);

CREATE TABLE artifact_citations (
    artifact_id UUID NOT NULL,
    artifact_version INTEGER NOT NULL,
    position INTEGER NOT NULL CHECK (position >= 0),
    source_id UUID NOT NULL,
    excerpt_id UUID NOT NULL,
    source_sha256 TEXT NOT NULL CHECK (source_sha256 ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (artifact_id, artifact_version, position),
    UNIQUE (artifact_id, artifact_version, source_id, excerpt_id),
    FOREIGN KEY (artifact_id, artifact_version) REFERENCES artifacts(id, version),
    FOREIGN KEY (source_id, excerpt_id) REFERENCES source_excerpts(source_id, id)
);

CREATE TABLE project_current_artifacts (
    project_id UUID NOT NULL REFERENCES projects(id),
    kind TEXT NOT NULL CHECK (
        kind IN ('fact_card', 'script', 'storyboard', 'media', 'audio', 'subtitle', 'cover', 'video', 'publication_package')
    ),
    artifact_id UUID NOT NULL,
    artifact_version INTEGER NOT NULL,
    PRIMARY KEY (project_id, kind),
    FOREIGN KEY (project_id, artifact_id, artifact_version, kind)
        REFERENCES artifacts(project_id, id, version, kind)
);

CREATE FUNCTION reject_immutable_history_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% rows are immutable', TG_TABLE_NAME USING ERRCODE = '23000';
END;
$$;

CREATE TRIGGER sources_are_immutable
    BEFORE UPDATE OR DELETE ON sources
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_history_change();
CREATE TRIGGER source_excerpts_are_immutable
    BEFORE UPDATE OR DELETE ON source_excerpts
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_history_change();
CREATE TRIGGER artifacts_are_immutable
    BEFORE UPDATE OR DELETE ON artifacts
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_history_change();
CREATE TRIGGER artifact_upstream_is_immutable
    BEFORE UPDATE OR DELETE ON artifact_upstream
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_history_change();
CREATE TRIGGER artifact_citations_are_immutable
    BEFORE UPDATE OR DELETE ON artifact_citations
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_history_change();

CREATE TABLE jobs (
    id UUID PRIMARY KEY,
    project_id UUID NOT NULL REFERENCES projects(id),
    kind TEXT NOT NULL CHECK (btrim(kind) <> ''),
    payload JSONB NOT NULL DEFAULT '{}'::jsonb CHECK (jsonb_typeof(payload) = 'object'),
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed')),
    attempt INTEGER NOT NULL DEFAULT 0 CHECK (attempt >= 0),
    max_attempts INTEGER NOT NULL CHECK (max_attempts > 0),
    available_at TIMESTAMPTZ NOT NULL,
    lease_owner TEXT,
    lease_expires_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL CHECK (updated_at >= created_at),
    CHECK ((lease_owner IS NULL) = (lease_expires_at IS NULL))
);

CREATE INDEX jobs_claim_idx ON jobs (available_at, created_at) WHERE status = 'queued';
CREATE INDEX jobs_lease_idx ON jobs (lease_expires_at) WHERE status = 'running';

CREATE TABLE reviews (
    id UUID PRIMARY KEY,
    project_id UUID NOT NULL REFERENCES projects(id),
    decision TEXT NOT NULL CHECK (decision IN ('approve', 'reject')),
    note TEXT NOT NULL CHECK (btrim(note) <> '' AND char_length(note) <= 2000),
    project_revision INTEGER NOT NULL CHECK (project_revision > 0),
    created_at TIMESTAMPTZ NOT NULL
);
