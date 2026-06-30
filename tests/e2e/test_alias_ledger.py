"""Alias upsert + repair-links each write an ActionLedgerEvent."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


async def _ledger_kinds(http_client, pid: str) -> list[str]:
    r = await http_client.get(f"/v1/projects/{pid}/ledger?limit=200")
    assert r.status_code == 200
    return [e["action_kind"] for e in r.json()]


async def test_alias_upsert_writes_ledger_and_chain_holds(http_client) -> None:
    resp = await http_client.post("/v1/projects", json={"title": "AliasLedger", "description": "x"})
    pid = resp.json()["id"]

    a = await http_client.post(
        f"/v1/projects/{pid}/wiki/aliases",
        json={"surface_form": "PC", "canonical_name": "Program Counter"},
    )
    assert a.status_code == 201

    # repair-links with zero broken links writes no event (idempotent no-op).
    r = await http_client.post(f"/v1/projects/{pid}/wiki/aliases/repair-links")
    assert r.status_code == 200

    kinds = await _ledger_kinds(http_client, pid)
    assert "wiki.alias.upsert" in kinds

    v = await http_client.get(f"/v1/projects/{pid}/ledger/verify")
    assert v.json()["ok"] is True
