#!/bin/sh
# Runs ONCE, the first time Postgres initializes an empty data directory —
# i.e. on a brand-new volume or after `docker compose down -v`. The stock
# postgres entrypoint executes everything in /docker-entrypoint-initdb.d in
# lexical order while the server is still bound to a local socket only (the
# TCP port + healthcheck come up afterward). So by the time the postgres
# healthcheck passes, these databases exist and dependents can rely on them.
#
# Responsibilities:
#   - create the auxiliary logical databases that share Aleph's Postgres
#     instance (langfuse + the three AIQ stores)
#   - apply the AIQ job-store + checkpoint schema, which the aiq-agent image
#     does NOT self-create (only job_events is auto-created)
#
# The main `aleph` database is created by the postgres entrypoint from
# POSTGRES_DB; its schema is applied by the `aleph-migrate` service (Alembic),
# not here. CREATE DATABASE is unguarded because an init-time run is always
# against an empty cluster.
#
# This is the in-container twin of the steps scripts/bootstrap-local.sh used
# to run host-side. Keep the two in sync.
set -e

for db in langfuse aiq_jobs aiq_checkpoints aiq_summary; do
  echo "  creating database $db"
  psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname postgres \
    -c "CREATE DATABASE $db"
done

echo "  applying AIQ job-store schema -> aiq_jobs"
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname aiq_jobs \
  -f /aleph-init/aiq-init-jobs.sql

echo "  applying AIQ checkpoint schema -> aiq_checkpoints"
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname aiq_checkpoints \
  -f /aleph-init/aiq-init-checkpoints.sql
