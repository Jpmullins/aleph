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
