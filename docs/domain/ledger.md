# Action ledger

Append-only hash-chained Postgres table of every state-changing operation.

## Schema

`action_ledger_events`:

| column | purpose |
|---|---|
| `id` | UUIDv7 |
| `project_id` | null only for global ops (`user.create`) |
| `actor_id` | the `User` who initiated (or the system principal) |
| `actor_kind` | `user` / `aleph_agent` / `aiq_agent` / `system` |
| `action_kind` | dotted taxonomy, e.g. `project.create` |
| `target_id` | the row being mutated |
| `target_kind` | the table name of the target |
| `payload_jsonb` | the *intent* of the change, sufficient to audit and to replay |
| `trace_id` | OTEL trace id (32-hex) |
| `timestamp` | UTC, server-set |
| `prev_event_id` | chain pointer |
| `chain_hash` | sha256 over prev + action + target + payload + timestamp |

`ledger_chain_heads`:

One row per project + a single null-project row. Holds the current
head event id and chain hash. The `LedgerWriter` takes a `SELECT FOR
UPDATE` lock on the matching head row when appending, serializing
chain extension and preventing hash races.

## Immutability

Two Postgres triggers (`ledger_no_update`, `ledger_no_delete`) raise
on `UPDATE` and `DELETE` against `action_ledger_events`. Service-layer
discipline keeps writes flowing through `LedgerWriter.append(...)`;
the trigger is defense in depth.

There is no admin override. Compaction is by archival to cold storage —
never by deletion of the live table.

## action_kind taxonomy (Inc 0)

- `user.create`
- `project.create`
- `project.update`
- `project_member.add`
- `project_member.remove`
- `project_member.role_change`
- `budget.set`
- `model_profile.create`
- `model_profile.update`
- `model_profile.copy_from_template`
- `agent_run.create`

Each later increment introduces its own kinds (e.g. `wiki.revision.commit`,
`approval.decide`, `dataset.version.create`, ...). All go through the
same writer.

## Querying

`GET /v1/projects/{id}/ledger` returns events newest-first with
filters `since`, `until`, `actor_kind`, `action_kind`.

The Logs icon in the left panel will open a UI view of the ledger
(Inc 4 surface work).

## Verifying the chain

A maintenance script (lands when first needed in ops) reads events in
order and re-computes each `chain_hash`; a mismatch is a tamper signal.
