-- AIQ job-store schema for the `aiq_jobs` database (idempotent).
--
-- Vendored from the AI-Q Blueprint's deploy/compose/init-db.sql. The
-- aiq-agent image does NOT create these at runtime — only `job_events` is
-- auto-created. Without `job_info` (NAT's JobStore table) every
-- /v1/jobs/async/submit fails with "relation job_info does not exist".
-- bootstrap-local.sh applies this to the shared Postgres after creating the
-- aiq_jobs database. (GRANTs to a dedicated `aiq` role are omitted — Aleph's
-- shared Postgres uses a single role.)

CREATE TABLE IF NOT EXISTS job_info (
    job_id VARCHAR PRIMARY KEY,
    status VARCHAR NOT NULL,
    config_file VARCHAR,
    error VARCHAR,
    output_path VARCHAR,
    created_at TIMESTAMP WITH TIME ZONE,
    updated_at TIMESTAMP WITH TIME ZONE,
    expiry_seconds INTEGER,
    output VARCHAR,
    is_expired BOOLEAN DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS idx_job_info_status ON job_info(status);
CREATE INDEX IF NOT EXISTS idx_job_info_created_at ON job_info(created_at);

CREATE TABLE IF NOT EXISTS job_access (
    job_id VARCHAR PRIMARY KEY,
    owner_auth_type VARCHAR NOT NULL,
    owner_subject VARCHAR NOT NULL,
    owner_email VARCHAR,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_job_access_owner ON job_access(owner_auth_type, owner_subject);

CREATE TABLE IF NOT EXISTS job_events (
    id SERIAL PRIMARY KEY,
    job_id VARCHAR(64) NOT NULL,
    event_type VARCHAR(64) NOT NULL,
    event_data TEXT,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_job_events_job_id ON job_events(job_id);
CREATE INDEX IF NOT EXISTS idx_job_events_job_id_id ON job_events(job_id, id);

CREATE TABLE IF NOT EXISTS summaries (
    collection VARCHAR(256) NOT NULL,
    filename VARCHAR(512) NOT NULL,
    summary TEXT NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    PRIMARY KEY (collection, filename)
);
CREATE INDEX IF NOT EXISTS idx_summaries_collection ON summaries(collection);
