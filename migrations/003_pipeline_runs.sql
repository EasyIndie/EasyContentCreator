ALTER TABLE artifacts DROP CONSTRAINT artifacts_kind_check;
ALTER TABLE artifacts ADD CONSTRAINT artifacts_kind_check CHECK (
    kind IN (
        'source', 'fact_card', 'topic_brief', 'script', 'storyboard',
        'asset_manifest', 'voiceover', 'subtitles', 'cover', 'video_master',
        'qc_report', 'review_bundle', 'channel_package',
        'media', 'audio', 'subtitle', 'video', 'publication_package'
    )
);

-- Deploy 003 before code that understands the M2 kinds.  Operators explicitly
-- activate the one-way gate only after every old application process has left.
CREATE TABLE artifact_kind_activation (
    singleton BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (singleton),
    m2_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    enabled_at TIMESTAMPTZ,
    CHECK (m2_enabled = (enabled_at IS NOT NULL))
);
INSERT INTO artifact_kind_activation (singleton, m2_enabled) VALUES (TRUE, FALSE);

CREATE FUNCTION guard_m2_artifact_kind() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.kind IN (
        'source', 'topic_brief', 'asset_manifest', 'voiceover', 'subtitles',
        'video_master', 'qc_report', 'review_bundle', 'channel_package'
    ) AND NOT (SELECT m2_enabled FROM artifact_kind_activation WHERE singleton) THEN
        RAISE EXCEPTION 'M2 artifact kinds are not activated' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER artifacts_require_m2_activation
    BEFORE INSERT ON artifacts
    FOR EACH ROW EXECUTE FUNCTION guard_m2_artifact_kind();

CREATE FUNCTION guard_artifact_kind_activation() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' OR (OLD.m2_enabled AND NOT NEW.m2_enabled) THEN
        RAISE EXCEPTION 'M2 artifact activation is one-way' USING ERRCODE = '23000';
    END IF;
    IF NEW.singleton IS DISTINCT FROM OLD.singleton
       OR NEW.enabled_at IS DISTINCT FROM OLD.enabled_at AND OLD.m2_enabled THEN
        RAISE EXCEPTION 'artifact activation audit fields are immutable' USING ERRCODE = '23000';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER artifact_kind_activation_is_one_way
    BEFORE UPDATE OR DELETE ON artifact_kind_activation
    FOR EACH ROW EXECUTE FUNCTION guard_artifact_kind_activation();

ALTER TABLE artifacts ADD CONSTRAINT artifacts_project_ref_sha_key
    UNIQUE (project_id, id, version, kind, sha256);
ALTER TABLE jobs ADD CONSTRAINT jobs_project_ref_key UNIQUE (id, project_id);

CREATE TABLE pipeline_definitions (
    digest TEXT PRIMARY KEY CHECK (digest ~ '^[0-9a-f]{64}$'),
    pipeline_kind TEXT NOT NULL CHECK (pipeline_kind ~ '^[a-z][a-z0-9_]{0,63}$'),
    pipeline_version TEXT NOT NULL CHECK (btrim(pipeline_version) <> ''),
    profile_version TEXT NOT NULL CHECK (btrim(profile_version) <> ''),
    manifest JSONB NOT NULL CHECK (jsonb_typeof(manifest) = 'object'),
    created_at TIMESTAMPTZ NOT NULL,
    UNIQUE (pipeline_kind, pipeline_version, profile_version),
    UNIQUE (digest, pipeline_kind, pipeline_version, profile_version)
);

CREATE TABLE pipeline_definition_steps (
    definition_digest TEXT NOT NULL REFERENCES pipeline_definitions(digest),
    step_kind TEXT NOT NULL CHECK (step_kind ~ '^[a-z][a-z0-9_]{0,63}$'),
    step_version TEXT NOT NULL CHECK (btrim(step_version) <> ''),
    PRIMARY KEY (definition_digest, step_kind),
    UNIQUE (definition_digest, step_kind, step_version)
);

CREATE TABLE pipeline_runs (
    id UUID PRIMARY KEY,
    project_id UUID NOT NULL REFERENCES projects(id),
    definition_digest TEXT NOT NULL,
    pipeline_kind TEXT NOT NULL,
    pipeline_version TEXT NOT NULL,
    profile_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'running', 'succeeded', 'failed', 'invalidated')
    ),
    created_at TIMESTAMPTZ NOT NULL,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    error_class TEXT CHECK (error_class ~ '^[A-Za-z_][A-Za-z0-9_.]{0,127}$'),
    invalidated_reason TEXT CHECK (btrim(invalidated_reason) <> ''),
    CHECK (started_at IS NULL OR started_at >= created_at),
    CHECK (finished_at IS NULL OR (started_at IS NOT NULL AND finished_at >= started_at)),
    UNIQUE (id, project_id),
    UNIQUE (id, project_id, definition_digest, profile_version),
    FOREIGN KEY (definition_digest, pipeline_kind, pipeline_version, profile_version)
        REFERENCES pipeline_definitions(digest, pipeline_kind, pipeline_version, profile_version),
    CHECK (
        (status = 'pending' AND started_at IS NULL AND finished_at IS NULL
            AND error_class IS NULL AND invalidated_reason IS NULL)
        OR (status = 'running' AND started_at IS NOT NULL AND finished_at IS NULL
            AND error_class IS NULL AND invalidated_reason IS NULL)
        OR (status = 'succeeded' AND finished_at IS NOT NULL
            AND error_class IS NULL AND invalidated_reason IS NULL)
        OR (status = 'failed' AND finished_at IS NOT NULL
            AND error_class IS NOT NULL AND invalidated_reason IS NULL)
        OR (status = 'invalidated' AND finished_at IS NOT NULL
            AND error_class IS NULL AND invalidated_reason IS NOT NULL)
    )
);

CREATE TABLE pipeline_run_inputs (
    pipeline_run_id UUID NOT NULL,
    project_id UUID NOT NULL,
    position INTEGER NOT NULL CHECK (position >= 0),
    logical_key TEXT NOT NULL CHECK (logical_key ~ '^[a-z][a-z0-9_-]*(/[a-z0-9][a-z0-9_-]*)*$'),
    artifact_id UUID NOT NULL,
    artifact_version INTEGER NOT NULL CHECK (artifact_version > 0),
    artifact_kind TEXT NOT NULL,
    artifact_sha256 TEXT NOT NULL CHECK (artifact_sha256 ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (pipeline_run_id, position),
    UNIQUE (pipeline_run_id, logical_key),
    FOREIGN KEY (pipeline_run_id, project_id) REFERENCES pipeline_runs(id, project_id),
    FOREIGN KEY (project_id, artifact_id, artifact_version, artifact_kind, artifact_sha256)
        REFERENCES artifacts(project_id, id, version, kind, sha256)
);

CREATE TABLE logical_artifact_streams (
    project_id UUID NOT NULL REFERENCES projects(id),
    logical_key TEXT NOT NULL CHECK (logical_key ~ '^[a-z][a-z0-9_-]*(/[a-z0-9][a-z0-9_-]*)*$'),
    kind TEXT NOT NULL,
    artifact_id UUID NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (project_id, logical_key),
    UNIQUE (project_id, logical_key, kind, artifact_id),
    UNIQUE (project_id, artifact_id)
);

CREATE TABLE step_runs (
    id UUID PRIMARY KEY,
    project_id UUID NOT NULL REFERENCES projects(id),
    pipeline_run_id UUID NOT NULL,
    definition_digest TEXT NOT NULL,
    step_kind TEXT NOT NULL CHECK (step_kind ~ '^[a-z][a-z0-9_]{0,63}$'),
    step_version TEXT NOT NULL CHECK (btrim(step_version) <> ''),
    profile_version TEXT NOT NULL CHECK (btrim(profile_version) <> ''),
    parameters JSONB NOT NULL CHECK (jsonb_typeof(parameters) = 'object'),
    idempotency_key TEXT NOT NULL CHECK (
        btrim(idempotency_key) <> '' AND char_length(idempotency_key) <= 200
    ),
    request_hash TEXT NOT NULL CHECK (request_hash ~ '^[0-9a-f]{64}$'),
    job_id UUID NOT NULL UNIQUE,
    rerun_of_step_run_id UUID,
    status TEXT NOT NULL CHECK (status IN ('pending', 'succeeded', 'failed', 'invalidated')),
    created_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ CHECK (finished_at IS NULL OR finished_at >= created_at),
    error_class TEXT CHECK (error_class ~ '^[A-Za-z_][A-Za-z0-9_.]{0,127}$'),
    invalidated_reason TEXT CHECK (btrim(invalidated_reason) <> ''),
    UNIQUE (id, project_id),
    UNIQUE (id, pipeline_run_id, project_id),
    UNIQUE (pipeline_run_id, step_kind, idempotency_key),
    FOREIGN KEY (pipeline_run_id, project_id, definition_digest, profile_version)
        REFERENCES pipeline_runs(id, project_id, definition_digest, profile_version),
    FOREIGN KEY (definition_digest, step_kind, step_version)
        REFERENCES pipeline_definition_steps(definition_digest, step_kind, step_version),
    FOREIGN KEY (job_id, project_id) REFERENCES jobs(id, project_id),
    FOREIGN KEY (rerun_of_step_run_id, pipeline_run_id, project_id)
        REFERENCES step_runs(id, pipeline_run_id, project_id),
    CHECK (
        (status = 'pending' AND finished_at IS NULL AND error_class IS NULL
            AND invalidated_reason IS NULL)
        OR (status = 'succeeded' AND finished_at IS NOT NULL AND error_class IS NULL
            AND invalidated_reason IS NULL)
        OR (status = 'failed' AND finished_at IS NOT NULL AND error_class IS NOT NULL
            AND invalidated_reason IS NULL)
        OR (status = 'invalidated' AND finished_at IS NOT NULL AND error_class IS NULL
            AND invalidated_reason IS NOT NULL)
    )
);

CREATE UNIQUE INDEX step_runs_fingerprint_idx
    ON step_runs (pipeline_run_id, step_kind, request_hash);

CREATE TABLE step_run_inputs (
    step_run_id UUID NOT NULL,
    project_id UUID NOT NULL,
    position INTEGER NOT NULL CHECK (position >= 0),
    binding_name TEXT NOT NULL CHECK (binding_name ~ '^[a-z][a-z0-9_]{0,63}$'),
    logical_key TEXT NOT NULL,
    artifact_id UUID NOT NULL,
    artifact_version INTEGER NOT NULL CHECK (artifact_version > 0),
    artifact_kind TEXT NOT NULL,
    artifact_sha256 TEXT NOT NULL CHECK (artifact_sha256 ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (step_run_id, position),
    UNIQUE (step_run_id, binding_name),
    UNIQUE (step_run_id, logical_key),
    FOREIGN KEY (step_run_id, project_id) REFERENCES step_runs(id, project_id),
    FOREIGN KEY (project_id, artifact_id, artifact_version, artifact_kind, artifact_sha256)
        REFERENCES artifacts(project_id, id, version, kind, sha256)
);

CREATE TABLE step_run_preserved_outputs (
    step_run_id UUID NOT NULL,
    project_id UUID NOT NULL,
    position INTEGER NOT NULL CHECK (position >= 0),
    logical_key TEXT NOT NULL,
    artifact_id UUID NOT NULL,
    artifact_version INTEGER NOT NULL CHECK (artifact_version > 0),
    artifact_kind TEXT NOT NULL,
    artifact_sha256 TEXT NOT NULL CHECK (artifact_sha256 ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (step_run_id, position),
    UNIQUE (step_run_id, logical_key),
    FOREIGN KEY (step_run_id, project_id) REFERENCES step_runs(id, project_id),
    FOREIGN KEY (project_id, artifact_id, artifact_version, artifact_kind, artifact_sha256)
        REFERENCES artifacts(project_id, id, version, kind, sha256)
);

CREATE TABLE step_output_reservations (
    step_run_id UUID NOT NULL,
    project_id UUID NOT NULL,
    position INTEGER NOT NULL CHECK (position >= 0),
    slot_name TEXT NOT NULL CHECK (slot_name ~ '^[a-z][a-z0-9_]{0,63}$'),
    item_key TEXT CHECK (item_key ~ '^[a-z][a-z0-9_]{0,63}$'),
    logical_key TEXT NOT NULL,
    kind TEXT NOT NULL,
    artifact_id UUID NOT NULL,
    artifact_version INTEGER NOT NULL CHECK (artifact_version > 0),
    PRIMARY KEY (step_run_id, position),
    UNIQUE NULLS NOT DISTINCT (step_run_id, slot_name, item_key),
    UNIQUE (project_id, logical_key, artifact_version),
    FOREIGN KEY (step_run_id, project_id) REFERENCES step_runs(id, project_id),
    FOREIGN KEY (project_id, logical_key, kind, artifact_id)
        REFERENCES logical_artifact_streams(project_id, logical_key, kind, artifact_id)
);

CREATE INDEX step_run_inputs_artifact_idx
    ON step_run_inputs (artifact_id, artifact_version);

CREATE FUNCTION validate_step_job_binding() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM jobs
        WHERE id = NEW.job_id
          AND project_id = NEW.project_id
          AND kind = 'pipeline_step'
          AND payload ->> 'step_run_id' = NEW.id::text
    ) THEN
        RAISE EXCEPTION 'StepRun Job kind or payload does not match StepRun identity'
            USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$;
CREATE CONSTRAINT TRIGGER step_runs_validate_job_binding
    AFTER INSERT OR UPDATE OF id, project_id, job_id ON step_runs
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION validate_step_job_binding();

CREATE FUNCTION validate_pipeline_step_job() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.kind = 'pipeline_step' AND NOT EXISTS (
        SELECT 1 FROM step_runs
        WHERE job_id = NEW.id
          AND project_id = NEW.project_id
          AND id::text = NEW.payload ->> 'step_run_id'
    ) THEN
        RAISE EXCEPTION 'pipeline_step Job is not bound to its StepRun'
            USING ERRCODE = '23514';
    END IF;
    RETURN NULL;
END;
$$;
CREATE CONSTRAINT TRIGGER jobs_validate_pipeline_step_binding
    AFTER INSERT OR UPDATE OF kind, payload ON jobs
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION validate_pipeline_step_job();

CREATE FUNCTION guard_bound_job_identity() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.kind, NEW.payload) IS DISTINCT FROM (OLD.kind, OLD.payload)
       AND EXISTS (SELECT 1 FROM step_runs WHERE job_id = OLD.id) THEN
        RAISE EXCEPTION 'bound Job kind and payload are immutable' USING ERRCODE = '23000';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER bound_job_identity_is_immutable
    BEFORE UPDATE OF kind, payload ON jobs
    FOR EACH ROW EXECUTE FUNCTION guard_bound_job_identity();

CREATE FUNCTION reject_pipeline_definition_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'pipeline_definitions rows are immutable' USING ERRCODE = '23000';
END;
$$;

CREATE TRIGGER pipeline_definitions_are_immutable
    BEFORE UPDATE OR DELETE ON pipeline_definitions
    FOR EACH ROW EXECUTE FUNCTION reject_pipeline_definition_change();
CREATE TRIGGER pipeline_definition_steps_are_immutable
    BEFORE UPDATE OR DELETE ON pipeline_definition_steps
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_history_change();

CREATE TRIGGER pipeline_run_inputs_are_immutable
    BEFORE UPDATE OR DELETE ON pipeline_run_inputs
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_history_change();
CREATE TRIGGER logical_artifact_streams_are_immutable
    BEFORE UPDATE OR DELETE ON logical_artifact_streams
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_history_change();
CREATE TRIGGER step_run_inputs_are_immutable
    BEFORE UPDATE OR DELETE ON step_run_inputs
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_history_change();
CREATE TRIGGER step_run_preserved_outputs_are_immutable
    BEFORE UPDATE OR DELETE ON step_run_preserved_outputs
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_history_change();
CREATE TRIGGER step_output_reservations_are_immutable
    BEFORE UPDATE OR DELETE ON step_output_reservations
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_history_change();

CREATE FUNCTION guard_pipeline_run_identity() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.id, NEW.project_id, NEW.definition_digest, NEW.pipeline_kind,
        NEW.pipeline_version, NEW.profile_version, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.project_id, OLD.definition_digest, OLD.pipeline_kind,
        OLD.pipeline_version, OLD.profile_version, OLD.created_at) THEN
        RAISE EXCEPTION 'pipeline_runs identity is immutable' USING ERRCODE = '23000';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER pipeline_run_identity_is_immutable
    BEFORE UPDATE ON pipeline_runs
    FOR EACH ROW EXECUTE FUNCTION guard_pipeline_run_identity();
CREATE TRIGGER pipeline_runs_cannot_be_deleted
    BEFORE DELETE ON pipeline_runs
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_history_change();

CREATE FUNCTION guard_step_run_identity() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.id, NEW.project_id, NEW.pipeline_run_id, NEW.definition_digest,
        NEW.step_kind, NEW.step_version,
        NEW.profile_version, NEW.parameters, NEW.idempotency_key, NEW.request_hash,
        NEW.job_id, NEW.rerun_of_step_run_id, NEW.created_at)
       IS DISTINCT FROM
       (OLD.id, OLD.project_id, OLD.pipeline_run_id, OLD.definition_digest,
        OLD.step_kind, OLD.step_version,
        OLD.profile_version, OLD.parameters, OLD.idempotency_key, OLD.request_hash,
        OLD.job_id, OLD.rerun_of_step_run_id, OLD.created_at) THEN
        RAISE EXCEPTION 'step_runs identity is immutable' USING ERRCODE = '23000';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER step_run_identity_is_immutable
    BEFORE UPDATE ON step_runs
    FOR EACH ROW EXECUTE FUNCTION guard_step_run_identity();
CREATE TRIGGER step_runs_cannot_be_deleted
    BEFORE DELETE ON step_runs
    FOR EACH ROW EXECUTE FUNCTION reject_immutable_history_change();

CREATE FUNCTION reject_step_for_terminal_pipeline_run() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    run_status TEXT;
BEGIN
    SELECT status INTO run_status
    FROM pipeline_runs
    WHERE id = NEW.pipeline_run_id
    FOR UPDATE;
    IF run_status IN ('succeeded', 'failed', 'invalidated') THEN
        RAISE EXCEPTION 'cannot enqueue a step for a terminal pipeline run'
            USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER step_runs_require_active_pipeline_run
    BEFORE INSERT ON step_runs
    FOR EACH ROW EXECUTE FUNCTION reject_step_for_terminal_pipeline_run();
