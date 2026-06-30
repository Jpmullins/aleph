# Audit Remediation — Step 1: Ledger Holes + Chain Verify — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every wiki-side state mutation write an `ActionLedgerEvent` in its own transaction (close rule-#4 holes in `AliasService`, `handedit_service`, `feedback_service`), and add a runtime hash-chain verification endpoint.

**Architecture:** Thread a `LedgerWriter` into the three offending services and append in-transaction with new `action_kind`s. Add a pure `verify_event_chain()` function (unit-testable) plus a DB-loading `verify_project_chain()` wrapper exposed at `GET /v1/projects/{id}/ledger/verify`. No schema change.

**Tech Stack:** Python 3.13, SQLAlchemy async, FastAPI, pytest (unit + `@pytest.mark.integration`).

## Global Constraints

- **Rule #4:** every state mutation writes an `ActionLedgerEvent` in the SAME transaction as the mutation.
- **Action-kind naming:** `<entity>.<verb>` — new kinds: `wiki.alias.upsert`, `wiki.links.repair`, `wiki.handedit.mark`, `wiki.handedit.clear`, `wiki.feedback.write`. `actor_kind` is `String(16)` — use `"user"` (route principals) or `"agent"` (curator).
- **DAG:** `aleph-wiki` may import `aleph-db` (`LedgerWriter` from `aleph_db.repos.ledger`) — it already does in `wiki_service.py`. `aleph-wiki` may import `aleph_observability.current_trace_id`.
- **Tests:** pure logic → unit test in the owning package's `tests/`. DB-touching → `@pytest.mark.integration` in `tests/e2e/` using the `http_client` fixture.
- **Gates before merge:** `uv run ruff check . && uv run ruff format --check . && uv run pyright`, `cd apps/api && uv run alembic check` (must stay clean — no migration in this step), `uv run pytest -m "not integration" -q`.
- **Ledger immutability triggers** block UPDATE/DELETE on `action_ledger_events` — do NOT write a tamper test that mutates a stored row; tamper detection is tested on the pure function with hand-built events.
- Genesis chain hash is `"0" * 64` (`LedgerChainHead.head_chain_hash` server default).

---

### Task 1: Ledger chain verification (pure function + DB wrapper + endpoint)

**Files:**
- Modify: `packages/aleph-db/src/aleph_db/repos/ledger.py` (add `ChainVerification`, `Divergence`, `verify_event_chain`, `verify_project_chain`)
- Modify: `apps/api/src/aleph_api/routes/ledger.py` (add the verify route)
- Test: `packages/aleph-db/tests/test_chain_verify.py` (new, unit)
- Test: `tests/e2e/test_ledger_verify.py` (new, integration)

**Interfaces:**
- Consumes: `_compute_chain_hash(...)` (existing private fn in `ledger.py`), `ActionLedgerEvent` model.
- Produces:
  - `verify_event_chain(events: Sequence[ChainLink], *, genesis_hash: str = "0"*64) -> ChainVerification`
  - `verify_project_chain(session: AsyncSession, project_id: UUID) -> ChainVerification`
  - `ChainVerification(ok: bool, count: int, first_divergence: Divergence | None)`
  - `Divergence(event_id: UUID, expected: str, actual: str)`
  - where `ChainLink` is any object exposing `.id, .action_kind, .target_id, .payload_jsonb, .timestamp, .chain_hash`.

- [ ] **Step 1: Write the failing unit test**

Create `packages/aleph-db/tests/test_chain_verify.py`:

```python
"""verify_event_chain — pure recompute over hand-built events."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from uuid import uuid4

from aleph_db.repos.ledger import _compute_chain_hash, verify_event_chain


@dataclass
class _Link:
    id: object
    action_kind: str
    target_id: object
    payload_jsonb: dict
    timestamp: datetime
    chain_hash: str


def _build_chain(n: int) -> list[_Link]:
    links: list[_Link] = []
    prev = "0" * 64
    for i in range(n):
        ts = datetime(2026, 6, 30, 12, 0, i, tzinfo=timezone.utc)
        tid = uuid4()
        payload = {"i": i}
        ch = _compute_chain_hash(
            prev_hash=prev,
            action_kind="test.event",
            target_id=tid,
            payload=payload,
            timestamp_iso=ts.isoformat(),
        )
        links.append(_Link(uuid4(), "test.event", tid, payload, ts, ch))
        prev = ch
    return links


def test_intact_chain_verifies_ok() -> None:
    result = verify_event_chain(_build_chain(3))
    assert result.ok is True
    assert result.count == 3
    assert result.first_divergence is None


def test_tampered_chain_reports_first_divergence() -> None:
    links = _build_chain(3)
    # Simulate a tampered payload: the stored chain_hash no longer matches.
    links[1].payload_jsonb = {"i": 999}
    result = verify_event_chain(links)
    assert result.ok is False
    assert result.first_divergence is not None
    assert result.first_divergence.event_id == links[1].id


def test_empty_chain_is_ok() -> None:
    result = verify_event_chain([])
    assert result.ok is True
    assert result.count == 0
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest packages/aleph-db/tests/test_chain_verify.py -q`
Expected: FAIL — `ImportError: cannot import name 'verify_event_chain'`.

- [ ] **Step 3: Implement the verify functions**

Append to `packages/aleph-db/src/aleph_db/repos/ledger.py` (add `dataclass` + `Sequence` imports at top; the file already imports `select`, `ActionLedgerEvent`):

```python
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Divergence:
    event_id: UUID
    expected: str
    actual: str


@dataclass(frozen=True)
class ChainVerification:
    ok: bool
    count: int
    first_divergence: Divergence | None


def verify_event_chain(
    events: Sequence[Any],
    *,
    genesis_hash: str = "0" * 64,
) -> ChainVerification:
    """Recompute the hash chain over `events` (in chain order) and report
    the first event whose stored `chain_hash` does not match the recomputation.
    Each event must expose: id, action_kind, target_id, payload_jsonb,
    timestamp, chain_hash.
    """
    prev = genesis_hash
    for e in events:
        expected = _compute_chain_hash(
            prev_hash=prev,
            action_kind=e.action_kind,
            target_id=e.target_id,
            payload=e.payload_jsonb,
            timestamp_iso=e.timestamp.isoformat(),
        )
        if expected != e.chain_hash:
            return ChainVerification(
                ok=False,
                count=len(events),
                first_divergence=Divergence(event_id=e.id, expected=expected, actual=e.chain_hash),
            )
        prev = e.chain_hash
    return ChainVerification(ok=True, count=len(events), first_divergence=None)


async def verify_project_chain(
    session: AsyncSession, project_id: UUID
) -> ChainVerification:
    rows = list(
        (
            await session.execute(
                select(ActionLedgerEvent)
                .where(ActionLedgerEvent.project_id == project_id)
                .order_by(ActionLedgerEvent.timestamp.asc(), ActionLedgerEvent.id.asc())
            )
        )
        .scalars()
        .all()
    )
    return verify_event_chain(rows)
```

Note: `AsyncSession` is already imported under `TYPE_CHECKING`; move it to a runtime import (the function signature uses it only as an annotation, so the existing `TYPE_CHECKING` import suffices with `from __future__ import annotations` already at the top — no change needed).

- [ ] **Step 4: Run the unit test to verify it passes**

Run: `uv run pytest packages/aleph-db/tests/test_chain_verify.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Add the verify route**

In `apps/api/src/aleph_api/routes/ledger.py`, add imports and a route:

```python
from uuid import UUID  # add if absent

from pydantic import BaseModel

from aleph_db.repos.ledger import verify_project_chain


class ChainVerifyOut(BaseModel):
    ok: bool
    count: int
    first_divergence_event_id: UUID | None = None


@router.get("/{project_id}/ledger/verify", response_model=ChainVerifyOut)
async def verify_ledger(
    project_id: ProjectScopeDep,
    session: SessionDep,
) -> ChainVerifyOut:
    result = await verify_project_chain(session, project_id)
    return ChainVerifyOut(
        ok=result.ok,
        count=result.count,
        first_divergence_event_id=(
            result.first_divergence.event_id if result.first_divergence else None
        ),
    )
```

- [ ] **Step 6: Write the integration test (happy path)**

Create `tests/e2e/test_ledger_verify.py`:

```python
"""GET /ledger/verify returns ok for a real project chain."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


async def test_ledger_verify_ok_after_mutations(http_client) -> None:
    # Creating a project writes ledger events; verify the chain holds.
    resp = await http_client.post(
        "/v1/projects", json={"title": "Verify", "description": "chain test"}
    )
    assert resp.status_code == 201
    pid = resp.json()["id"]

    v = await http_client.get(f"/v1/projects/{pid}/ledger/verify")
    assert v.status_code == 200
    body = v.json()
    assert body["ok"] is True
    assert body["count"] >= 1
    assert body["first_divergence_event_id"] is None
```

- [ ] **Step 7: Run gates + commit**

Run: `uv run pytest packages/aleph-db/tests/test_chain_verify.py -q && uv run ruff check packages/aleph-db apps/api/src/aleph_api/routes/ledger.py && uv run ruff format packages/aleph-db apps/api/src/aleph_api/routes/ledger.py tests/e2e/test_ledger_verify.py`
Expected: tests pass; ruff clean.

```bash
git add packages/aleph-db/src/aleph_db/repos/ledger.py \
        packages/aleph-db/tests/test_chain_verify.py \
        apps/api/src/aleph_api/routes/ledger.py \
        tests/e2e/test_ledger_verify.py
git commit -m "feat(ledger): runtime hash-chain verification endpoint + pure verify fn"
```

---

### Task 2: Ledger the AliasService mutations (`upsert`, `repair_broken_links`)

**Files:**
- Modify: `packages/aleph-wiki/src/aleph_wiki/alias_service.py`
- Modify: `packages/aleph-wiki/src/aleph_wiki/curator_service.py` (pass ledger + actor)
- Modify: `apps/workers/src/aleph_workers/jobs/curate.py` (construct CuratorService with ledger + actor)
- Modify: `apps/api/src/aleph_api/routes/aliases.py` (construct `AliasService(session, LedgerWriter(session))`)
- Modify: all other callers of `repair_broken_links` / `AliasService(...).upsert` (find via grep, Step 1)
- Test: `tests/e2e/test_alias_ledger.py` (new, integration)

**Interfaces:**
- Consumes: `LedgerWriter` (Task-independent, existing), `current_trace_id`.
- Produces:
  - `AliasService.__init__(self, session, ledger: LedgerWriter | None = None)`
  - `AliasService.upsert(..., created_by: UUID, actor_kind: str = "user") -> Alias` (now ledgers when `ledger` set)
  - `AliasService.repair_broken_links(self, *, project_id: UUID, actor_id: UUID, actor_kind: str = "agent") -> int` (signature CHANGED — adds `actor_id`, `actor_kind`)
  - `CuratorService.__init__(self, session, ledger: LedgerWriter | None = None, actor_id: UUID | None = None)`

- [ ] **Step 1: Enumerate callers that must be updated**

Run:
```bash
grep -rn "repair_broken_links\|AliasService(" packages apps --include=*.py
```
Expected callers to update (verify against output): `routes/aliases.py` (add_alias, repair_links), `curator_service.py` (`_register_aliases`, `_repair_links`), `aleph_wiki/agent/workflow.py` (inline repair after ingest commit). The retrieval router constructs `AliasService` read-only (no mutation) — leave as-is (ledger defaults to None). Record the exact list before editing.

- [ ] **Step 2: Write the failing integration test**

Create `tests/e2e/test_alias_ledger.py`:

```python
"""Alias upsert + repair-links each write an ActionLedgerEvent."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


async def _ledger_kinds(http_client, pid: str) -> list[str]:
    r = await http_client.get(f"/v1/projects/{pid}/ledger?limit=200")
    assert r.status_code == 200
    return [e["action_kind"] for e in r.json()]


async def test_alias_upsert_and_repair_write_ledger(http_client) -> None:
    resp = await http_client.post(
        "/v1/projects", json={"title": "AliasLedger", "description": "x"}
    )
    pid = resp.json()["id"]

    a = await http_client.post(
        f"/v1/projects/{pid}/wiki/aliases",
        json={"surface_form": "PC", "canonical_name": "Program Counter"},
    )
    assert a.status_code == 201

    r = await http_client.post(f"/v1/projects/{pid}/wiki/aliases/repair-links")
    assert r.status_code == 200

    kinds = await _ledger_kinds(http_client, pid)
    assert "wiki.alias.upsert" in kinds
    # repair-links with zero repaired writes no event; this project has no
    # broken links, so we assert the upsert event and that verify still holds.
    v = await http_client.get(f"/v1/projects/{pid}/ledger/verify")
    assert v.json()["ok"] is True
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `uv run pytest tests/e2e/test_alias_ledger.py -q -m integration`
Expected: FAIL — `wiki.alias.upsert` not in `kinds` (no ledger written yet). (Requires the compose stack + migrations; if env is absent the test skips — run it against the running stack.)

- [ ] **Step 4: Add ledger writes to `AliasService`**

Edit `packages/aleph-wiki/src/aleph_wiki/alias_service.py`. Add imports:

```python
from aleph_db.repos.ledger import LedgerWriter
from aleph_observability import current_trace_id
```

Change `__init__`:

```python
    def __init__(self, session: AsyncSession, ledger: LedgerWriter | None = None) -> None:
        self._session = session
        self._ledger = ledger
```

In `upsert`, add `actor_kind: str = "user"` to the signature, and replace the two `return existing` / `return a` tails so a single ledger append runs before returning. Concretely, after resolving `result` (existing or new) and flushing, append:

```python
    async def upsert(
        self,
        *,
        project_id: UUID,
        surface_form: str,
        canonical_name: str,
        canonical_page_id: UUID | None = None,
        confidence: float = 1.0,
        created_by: UUID,
        actor_kind: str = "user",
    ) -> Alias:
        existing = (
            await self._session.execute(
                select(Alias).where(
                    Alias.project_id == project_id,
                    Alias.surface_form == surface_form,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.canonical_name = canonical_name
            existing.canonical_page_id = canonical_page_id
            existing.confidence = max(existing.confidence, confidence)
            await self._session.flush()
            result = existing
        else:
            result = Alias(
                id=uuid7(),
                project_id=project_id,
                surface_form=surface_form[:512],
                canonical_name=canonical_name[:512],
                canonical_page_id=canonical_page_id,
                confidence=confidence,
                created_by=created_by,
                access_scope="project",
            )
            self._session.add(result)
            await self._session.flush()
        if self._ledger is not None:
            await self._ledger.append(
                project_id=project_id,
                actor_id=created_by,
                actor_kind=actor_kind,
                action_kind="wiki.alias.upsert",
                target_id=result.id,
                target_kind="wiki_alias",
                payload={
                    "surface_form": result.surface_form,
                    "canonical_name": result.canonical_name,
                    "canonical_page_id": str(result.canonical_page_id)
                    if result.canonical_page_id
                    else None,
                },
                trace_id=current_trace_id(),
            )
        return result
```

Change `repair_broken_links` to take `actor_id`/`actor_kind` and ledger only when it actually repaired:

```python
    async def repair_broken_links(
        self, *, project_id: UUID, actor_id: UUID, actor_kind: str = "agent"
    ) -> int:
        rows = list(
            (
                await self._session.execute(
                    select(WikiLink).where(
                        WikiLink.project_id == project_id,
                        WikiLink.dst_page_id.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        n_repaired = 0
        repaired_ids: list[str] = []
        for link in rows:
            r = await self.resolve(project_id=project_id, surface_form=link.dst_title)
            if r and r.canonical_page_id:
                link.dst_page_id = r.canonical_page_id
                n_repaired += 1
                repaired_ids.append(str(link.id))
        await self._session.flush()
        if n_repaired and self._ledger is not None:
            await self._ledger.append(
                project_id=project_id,
                actor_id=actor_id,
                actor_kind=actor_kind,
                action_kind="wiki.links.repair",
                target_id=None,
                target_kind="wiki_links",
                payload={"repaired": n_repaired, "link_ids": repaired_ids[:200]},
                trace_id=current_trace_id(),
            )
        return n_repaired
```

- [ ] **Step 5: Update `CuratorService` to pass ledger + actor**

Edit `packages/aleph-wiki/src/aleph_wiki/curator_service.py`. Add `from aleph_db.repos.ledger import LedgerWriter` (top). Change `__init__` and the two helper call sites:

```python
    def __init__(
        self,
        session: AsyncSession,
        ledger: LedgerWriter | None = None,
        actor_id: UUID | None = None,
    ) -> None:
        self._session = session
        self._ledger = ledger
        self._actor_id = actor_id
        self._aliases = AliasService(session, ledger=ledger)
```

In `_register_aliases`, pass `actor_kind="agent"`:

```python
            await self._aliases.upsert(
                project_id=project_id,
                surface_form=page.title,
                canonical_name=page.title,
                canonical_page_id=page.id,
                created_by=page.created_by,
                actor_kind="agent",
            )
```

In `_repair_links`, pass actor:

```python
    async def _repair_links(self, *, project_id: UUID) -> int:
        with start_span("wiki.curate.repair_links", **{"aleph.project_id": str(project_id)}):
            return await self._aliases.repair_broken_links(
                project_id=project_id,
                actor_id=self._actor_id or _NIL_ACTOR,
                actor_kind="agent",
            )
```

Add a module-level fallback actor near the top (used only if the job didn't supply one — should not happen in production):

```python
from uuid import UUID
_NIL_ACTOR = UUID(int=0)
```

- [ ] **Step 6: Update `curate.py` to construct CuratorService with ledger + actor**

In `apps/workers/src/aleph_workers/jobs/curate.py`, the deterministic-knit block (`CuratorService(session).curate(...)`) becomes — using the `owner` already resolved in the job and a `LedgerWriter` on the same session:

```python
from aleph_db.repos.ledger import LedgerWriter  # add at top
...
            result = await CuratorService(
                session, ledger=LedgerWriter(session), actor_id=owner
            ).curate(project_id=pid, page_id=page)
            await session.commit()
```

(`owner = project.created_by` is already in scope from the run-setup block.)

- [ ] **Step 7: Update `routes/aliases.py`**

Add `from aleph_db.repos.ledger import LedgerWriter`. In `add_alias`:

```python
    svc = AliasService(session, LedgerWriter(session))
    a = await svc.upsert(
        project_id=project_id,
        surface_form=body.surface_form,
        canonical_name=body.canonical_name,
        canonical_page_id=body.canonical_page_id,
        confidence=body.confidence,
        created_by=principal.user_id,
        actor_kind=principal.actor_kind,
    )
```

In `repair_links`:

```python
    svc = AliasService(session, LedgerWriter(session))
    n = await svc.repair_broken_links(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
    )
```

- [ ] **Step 8: Update remaining `repair_broken_links` callers found in Step 1**

For the inline repair in `packages/aleph-wiki/src/aleph_wiki/agent/workflow.py` (ingest), construct the `AliasService` with a `LedgerWriter(session)` and pass `actor_id` (the ingest principal's user id, already in the workflow state) + `actor_kind="agent"`. Mirror the Step-7 pattern. If the workflow constructs `AliasService(session)` without a ledger, change to `AliasService(session, LedgerWriter(session))` and update the `repair_broken_links(...)` call to pass `actor_id`/`actor_kind`.

- [ ] **Step 9: Run unit gates + the integration test**

Run: `uv run pytest -m "not integration" -q packages/aleph-wiki && uv run ruff check packages/aleph-wiki apps/api apps/workers && uv run pyright packages/aleph-wiki/src/aleph_wiki/alias_service.py packages/aleph-wiki/src/aleph_wiki/curator_service.py`
Then against the running stack: `uv run pytest tests/e2e/test_alias_ledger.py -q -m integration`
Expected: unit pass, ruff clean, pyright clean on the edited files, integration `wiki.alias.upsert` present + verify ok.

- [ ] **Step 10: Commit**

```bash
git add packages/aleph-wiki/src/aleph_wiki/alias_service.py \
        packages/aleph-wiki/src/aleph_wiki/curator_service.py \
        apps/workers/src/aleph_workers/jobs/curate.py \
        apps/api/src/aleph_api/routes/aliases.py \
        packages/aleph-wiki/src/aleph_wiki/agent/workflow.py \
        tests/e2e/test_alias_ledger.py
git commit -m "feat(ledger): AliasService upsert + repair_broken_links write ActionLedgerEvent"
```

---

### Task 3: Ledger the hand-edit mutations (`mark_section`, `clear_section`)

**Files:**
- Modify: `packages/aleph-wiki/src/aleph_wiki/handedit_service.py` (functions gain `ledger` + `actor_kind`)
- Modify: `apps/api/src/aleph_api/routes/handedits.py`
- Modify: any non-route caller of `mark_section`/`clear_section` (grep, Step 1)
- Test: `tests/e2e/test_handedit_ledger.py` (new, integration)

**Interfaces:**
- Produces:
  - `mark_section(session, *, project_id, page_id, section_anchor, applied_by, ledger: LedgerWriter | None = None, actor_kind: str = "user") -> HandEditMark`
  - `clear_section(session, *, project_id, page_id, section_anchor, cleared_by, ledger: LedgerWriter | None = None, actor_kind: str = "user") -> int`

- [ ] **Step 1: Enumerate callers**

Run: `grep -rn "mark_section\|clear_section" packages apps --include=*.py`
Record callers. Expected: `routes/handedits.py` only (plus the function defs + `list_active_for_page` which is read-only). Update each mutating caller.

- [ ] **Step 2: Write the failing integration test**

Create `tests/e2e/test_handedit_ledger.py`:

```python
"""mark/clear hand-edit each write an ActionLedgerEvent."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


async def test_handedit_mark_clear_write_ledger(http_client) -> None:
    p = await http_client.post(
        "/v1/projects", json={"title": "HandeditLedger", "description": "x"}
    )
    pid = p.json()["id"]

    # Create a wiki page to mark. Use the manual page-create path.
    pg = await http_client.post(
        f"/v1/projects/{pid}/wiki/pages",
        json={"title": "Topic", "body_md": "# Topic\n\nbody"},
    )
    assert pg.status_code in (200, 201)
    page_id = pg.json()["id"]

    m = await http_client.post(
        f"/v1/projects/{pid}/wiki/pages/{page_id}/sections/content/handedit"
    )
    assert m.status_code == 201
    c = await http_client.delete(
        f"/v1/projects/{pid}/wiki/pages/{page_id}/sections/content/handedit"
    )
    assert c.status_code == 204

    r = await http_client.get(f"/v1/projects/{pid}/ledger?limit=200")
    kinds = [e["action_kind"] for e in r.json()]
    assert "wiki.handedit.mark" in kinds
    assert "wiki.handedit.clear" in kinds
    v = await http_client.get(f"/v1/projects/{pid}/ledger/verify")
    assert v.json()["ok"] is True
```

Note: confirm the wiki page-create route path/payload in Step 1's grep of `routes/wiki*.py`; adjust the `pg` call to the actual create endpoint if different (e.g. a notes-promote or a direct page POST). The assertion logic is unchanged.

- [ ] **Step 3: Run to verify it fails**

Run: `uv run pytest tests/e2e/test_handedit_ledger.py -q -m integration`
Expected: FAIL — kinds missing.

- [ ] **Step 4: Add ledger writes to the functions**

Edit `packages/aleph-wiki/src/aleph_wiki/handedit_service.py`. Add imports:

```python
from aleph_db.repos.ledger import LedgerWriter
from aleph_observability import current_trace_id
```

In `mark_section`, add params and append after `session.add(mark); await session.flush()`:

```python
async def mark_section(
    session: AsyncSession,
    *,
    project_id: UUID,
    page_id: UUID,
    section_anchor: str | None,
    applied_by: UUID,
    ledger: LedgerWriter | None = None,
    actor_kind: str = "user",
) -> HandEditMark:
    ...
    session.add(mark)
    await session.flush()
    if ledger is not None:
        await ledger.append(
            project_id=project_id,
            actor_id=applied_by,
            actor_kind=actor_kind,
            action_kind="wiki.handedit.mark",
            target_id=page_id,
            target_kind="wiki_page",
            payload={"page_id": str(page_id), "section_anchor": section_anchor},
            trace_id=current_trace_id(),
        )
    return mark
```

In `clear_section`, add params and append after the flush (only when something was cleared):

```python
async def clear_section(
    session: AsyncSession,
    *,
    project_id: UUID,
    page_id: UUID,
    section_anchor: str | None,
    cleared_by: UUID,
    ledger: LedgerWriter | None = None,
    actor_kind: str = "user",
) -> int:
    ...
    await session.flush()
    if rows and ledger is not None:
        await ledger.append(
            project_id=project_id,
            actor_id=cleared_by,
            actor_kind=actor_kind,
            action_kind="wiki.handedit.clear",
            target_id=page_id,
            target_kind="wiki_page",
            payload={"page_id": str(page_id), "section_anchor": section_anchor, "cleared": len(rows)},
            trace_id=current_trace_id(),
        )
    return len(rows)
```

- [ ] **Step 5: Update `routes/handedits.py`**

Add `from aleph_db.repos.ledger import LedgerWriter`. Pass `ledger=LedgerWriter(session), actor_kind=principal.actor_kind` to both `mark_section(...)` and `clear_section(...)` calls.

- [ ] **Step 6: Run gates + integration + commit**

Run: `uv run pytest -m "not integration" -q packages/aleph-wiki && uv run ruff check packages/aleph-wiki apps/api && uv run pytest tests/e2e/test_handedit_ledger.py -q -m integration`
Expected: pass.

```bash
git add packages/aleph-wiki/src/aleph_wiki/handedit_service.py \
        apps/api/src/aleph_api/routes/handedits.py \
        tests/e2e/test_handedit_ledger.py
git commit -m "feat(ledger): hand-edit mark/clear write ActionLedgerEvent"
```

---

### Task 4: Ledger the rejection-feedback mutation (`write_feedback`)

**Files:**
- Modify: `packages/aleph-wiki/src/aleph_wiki/feedback_service.py`
- Modify: `apps/api/src/aleph_api/routes/feedback.py`
- Modify: any non-route caller of `write_feedback` (grep, Step 1)
- Test: `tests/e2e/test_feedback_ledger.py` (new, integration)

**Interfaces:**
- Produces: `write_feedback(session, *, project_id, page_id, concept_name, rejected_revision_id, reason, rejected_by, ledger: LedgerWriter | None = None, actor_kind: str = "user") -> RejectionFeedback`

- [ ] **Step 1: Enumerate callers**

Run: `grep -rn "write_feedback" packages apps --include=*.py`
Record callers. Expected: `routes/feedback.py`. Update each.

- [ ] **Step 2: Write the failing integration test**

Create `tests/e2e/test_feedback_ledger.py`:

```python
"""write_feedback writes an ActionLedgerEvent."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


async def test_feedback_writes_ledger(http_client) -> None:
    p = await http_client.post(
        "/v1/projects", json={"title": "FeedbackLedger", "description": "x"}
    )
    pid = p.json()["id"]

    f = await http_client.post(
        f"/v1/projects/{pid}/wiki/feedback/rejection",
        json={"concept_name": "Topic", "reason": "wrong"},
    )
    assert f.status_code == 201

    r = await http_client.get(f"/v1/projects/{pid}/ledger?limit=200")
    kinds = [e["action_kind"] for e in r.json()]
    assert "wiki.feedback.write" in kinds
    v = await http_client.get(f"/v1/projects/{pid}/ledger/verify")
    assert v.json()["ok"] is True
```

- [ ] **Step 3: Run to verify it fails**

Run: `uv run pytest tests/e2e/test_feedback_ledger.py -q -m integration`
Expected: FAIL — `wiki.feedback.write` missing.

- [ ] **Step 4: Add the ledger write**

Edit `packages/aleph-wiki/src/aleph_wiki/feedback_service.py`. Add imports (`LedgerWriter`, `current_trace_id`). Add params to `write_feedback` and append after `session.add(fb); await session.flush()`:

```python
async def write_feedback(
    session: AsyncSession,
    *,
    project_id: UUID,
    page_id: UUID | None,
    concept_name: str,
    rejected_revision_id: UUID | None,
    reason: str,
    rejected_by: UUID,
    ledger: LedgerWriter | None = None,
    actor_kind: str = "user",
) -> RejectionFeedback:
    ...
    session.add(fb)
    await session.flush()
    if ledger is not None:
        await ledger.append(
            project_id=project_id,
            actor_id=rejected_by,
            actor_kind=actor_kind,
            action_kind="wiki.feedback.write",
            target_id=fb.id,
            target_kind="rejection_feedback",
            payload={"concept_name": fb.concept_name, "page_id": str(page_id) if page_id else None},
            trace_id=current_trace_id(),
        )
    return fb
```

- [ ] **Step 5: Update `routes/feedback.py`**

Add `from aleph_db.repos.ledger import LedgerWriter`. Pass `ledger=LedgerWriter(session), actor_kind=principal.actor_kind` to the `write_feedback(...)` call. (`principal` is already a dep on `post_feedback`.)

- [ ] **Step 6: Run gates + integration + commit**

Run: `uv run pytest -m "not integration" -q packages/aleph-wiki && uv run ruff check packages/aleph-wiki apps/api && uv run pytest tests/e2e/test_feedback_ledger.py -q -m integration`
Expected: pass.

```bash
git add packages/aleph-wiki/src/aleph_wiki/feedback_service.py \
        apps/api/src/aleph_api/routes/feedback.py \
        tests/e2e/test_feedback_ledger.py
git commit -m "feat(ledger): rejection-feedback write_feedback writes ActionLedgerEvent"
```

---

### Task 5: Full-suite gate + docs note

**Files:**
- Modify: `docs/implementation-log.md` (append a short entry)

- [ ] **Step 1: Run the full unit suite + hygiene gates**

Run:
```bash
uv run ruff check . && uv run ruff format --check . \
  && uv run pytest -m "not integration" -q \
  && (cd apps/api && uv run alembic check)
```
Expected: ruff clean, all unit tests pass, `alembic check` reports no new operations (this step adds no migration).

- [ ] **Step 2: Append the implementation-log entry**

Add to `docs/implementation-log.md` a dated subsection "Audit remediation — Step 1 (ledger holes + chain verify)" listing: the four newly-ledgered mutations + their `action_kind`s, the `verify_event_chain`/`verify_project_chain` functions, and the `GET /v1/projects/{id}/ledger/verify` endpoint. Note that hand-edits now write a ledger event (closing the prior "hand-edits not pushed live" honest-limit).

- [ ] **Step 3: Commit**

```bash
git add docs/implementation-log.md
git commit -m "docs: log audit-remediation step 1 (ledger holes + chain verify)"
```

---

## Self-Review (completed during authoring)

- **Spec coverage (WS-C.1, WS-C.2):** §3.1 ledger holes → Tasks 2–4 (alias/handedit/feedback, all four `action_kind`s); §3.2 chain verify → Task 1. ✓
- **Placeholder scan:** every code step shows complete code; the one conditional ("confirm the wiki page-create route path") is a verification instruction with an explicit grep, not a code placeholder. ✓
- **Type consistency:** `LedgerWriter`, `current_trace_id`, `verify_event_chain`/`verify_project_chain`/`ChainVerification`/`Divergence` used consistently; `AliasService.__init__(session, ledger=None)` and the changed `repair_broken_links(..., actor_id, actor_kind)` signature are referenced identically in callers (curator, routes). ✓
- **Migration:** none — `alembic check` must stay clean (asserted in Task 5). ✓
```
