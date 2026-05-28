# MechanicalReviewer

LangGraph workflow that runs on every `wiki_service.commit_revision`
success. Deterministic by default; one LLM-judged check
(`citation_match`) wraps the AIQ-compatible
`aleph_wiki.citation_verification` contract.

## Checks (Inc 5)

| Node | Kind emitted | Severity | LLM? |
|---|---|---|---|
| `citation_match` | `citation_match_failure` | high | none — pattern match against Citation rows |
| `broken_links` | `broken_wikilink` | medium | no |
| `stale_sources` | `stale_source` | low | no |
| `duplicate_sources` | `duplicate_source` | low | no |

Each finding writes a `ReviewFinding` row pointing at the offending
page / revision / source. Findings with `auto_resolvable=true` (e.g.
a missing alias the system can derive) are eligible for the auto-fix
pass — that pass lands in Inc 5 only as a hook; deciding which finding
kinds may auto-resolve is a project-level setting introduced when
reviewer policy lands in Inc 8.

## Trigger

The `wiki_ingest_job` enqueues `mechanical_review_job` after every
successful revision commit. Manual triggering is via
`POST /v1/projects/{id}/reviews/runs` (owner/editor; not yet exposed —
follow-on).

## Failure semantics

If a check raises, the workflow records the partial findings and
finalizes the `ReviewRun` as `failed`. The wiki revision is NOT
rolled back; the analyst sees the finding pile and can act.
