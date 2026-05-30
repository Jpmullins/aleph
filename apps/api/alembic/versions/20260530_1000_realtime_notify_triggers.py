"""realtime: pg_notify triggers for the LISTEN/NOTIFY push layer.

Adds `AFTER INSERT/UPDATE` triggers that `pg_notify('aleph_changes', …)` on the
three mutation surfaces the SSE streams consume:

  * action_ledger_events (INSERT)  — every state mutation (rule #4) → committed signal
  * agent_events         (INSERT)  — per-phase progress; project resolved via agent_runs
  * assistant_messages   (UPDATE)  — token/status changes for the assistant stream

Triggers fire AFTER the row is written and are delivered on COMMIT, so subscribers
never observe uncommitted data. Payloads carry identity only (well under the ~8000-byte
NOTIFY cap); the stream does a small cursor query to fetch the actual rows.

No table/column changes — the agent_events trigger does a read-only PK lookup on
agent_runs to resolve project_id.

Revision ID: realtime_notify_triggers
Revises: reconcile_budgets_cost_ledger
Create Date: 2026-05-30
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "realtime_notify_triggers"
down_revision: str | None = "reconcile_budgets_cost_ledger"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# asyncpg's extended query protocol permits ONE statement per execute, so each
# CREATE FUNCTION / CREATE TRIGGER is issued as its own op.execute(). (A dollar-
# quoted function body with internal semicolons is still a single statement.)
_UPGRADE_STATEMENTS = [
    """
CREATE OR REPLACE FUNCTION aleph_notify_ledger() RETURNS trigger AS $$
BEGIN
  IF NEW.project_id IS NOT NULL THEN
    PERFORM pg_notify('aleph_changes', json_build_object(
      'src', 'ledger',
      'project_id', NEW.project_id,
      'action_kind', NEW.action_kind,
      'target_id', NEW.target_id,
      'ts', extract(epoch from NEW.timestamp)
    )::text);
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
""",
    """
CREATE TRIGGER trg_aleph_notify_ledger
AFTER INSERT ON action_ledger_events
FOR EACH ROW EXECUTE FUNCTION aleph_notify_ledger();
""",
    """
CREATE OR REPLACE FUNCTION aleph_notify_agent_event() RETURNS trigger AS $$
DECLARE
  pid uuid;
BEGIN
  SELECT project_id INTO pid FROM agent_runs WHERE id = NEW.agent_run_id;
  IF pid IS NOT NULL THEN
    PERFORM pg_notify('aleph_changes', json_build_object(
      'src', 'agent_event',
      'project_id', pid,
      'agent_run_id', NEW.agent_run_id,
      'event_kind', NEW.event_kind,
      'ts', extract(epoch from NEW.timestamp)
    )::text);
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
""",
    """
CREATE TRIGGER trg_aleph_notify_agent_event
AFTER INSERT ON agent_events
FOR EACH ROW EXECUTE FUNCTION aleph_notify_agent_event();
""",
    """
CREATE OR REPLACE FUNCTION aleph_notify_assistant() RETURNS trigger AS $$
BEGIN
  IF NEW.project_id IS NOT NULL
     AND (NEW.body_md IS DISTINCT FROM OLD.body_md OR NEW.status IS DISTINCT FROM OLD.status)
  THEN
    PERFORM pg_notify('aleph_changes', json_build_object(
      'src', 'assistant',
      'project_id', NEW.project_id,
      'message_id', NEW.id,
      'status', NEW.status,
      'ts', extract(epoch from now())
    )::text);
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;
""",
    """
CREATE TRIGGER trg_aleph_notify_assistant
AFTER UPDATE ON assistant_messages
FOR EACH ROW EXECUTE FUNCTION aleph_notify_assistant();
""",
]

_DOWNGRADE_STATEMENTS = [
    "DROP TRIGGER IF EXISTS trg_aleph_notify_assistant ON assistant_messages;",
    "DROP FUNCTION IF EXISTS aleph_notify_assistant();",
    "DROP TRIGGER IF EXISTS trg_aleph_notify_agent_event ON agent_events;",
    "DROP FUNCTION IF EXISTS aleph_notify_agent_event();",
    "DROP TRIGGER IF EXISTS trg_aleph_notify_ledger ON action_ledger_events;",
    "DROP FUNCTION IF EXISTS aleph_notify_ledger();",
]


def upgrade() -> None:
    for stmt in _UPGRADE_STATEMENTS:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in _DOWNGRADE_STATEMENTS:
        op.execute(stmt)
