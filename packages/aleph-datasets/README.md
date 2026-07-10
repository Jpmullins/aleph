# aleph-datasets — PARKED (built ahead, not yet wired)

**Status:** the ORM models (`Dataset` / `DatasetVersion` / `Observation`) and the
service code (`dataset_service`, `vega_compile`, `schema_inference`) are complete but
**have no runtime caller**. This is intentional pre-build: per `GOAL.md`, the
figure-composition stack is the *next* goal and datasets are its foundation.

**Do not treat this package as live.** Nothing in `apps/` drives it; the
`dataset_rows` connector path (`artificialanalysis` → `extract_rows` →
`commit_version`) is not connected to the research loop. The models are kept (and
their Alembic migration stands) so there is no destructive down-migration, but the
service functions here are quarantined until the datasets goal is picked up.

**To wire it** (future work — see `docs/future-work.md`): branch the research loop on
`output_kind == "dataset_rows"` to call `extract_rows` → `commit_version`, and expose
a datasets route + A2UI surface card.

The short-id allocation was hardened to use a transaction-scoped advisory lock (matching
`aleph_rks.source_service`) so that when this package *is* wired, concurrent creators
cannot collide on the globally-unique `Dataset.short_id`.
