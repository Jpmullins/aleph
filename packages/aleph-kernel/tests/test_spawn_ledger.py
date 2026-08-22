"""The agent family tree and its brakes (D4).

From prime-agent's "supervisor-owned rlm spawn ledger as family authority".
The ownership is the idea: agents are recorded in the ledger, never authors of
it, so the tree is a fact about the system rather than a claim by a participant.
"""

from __future__ import annotations

import pytest

from aleph_kernel.spawn_ledger import SpawnDenied, SpawnLedger, SpawnState


def ledger(**kw) -> SpawnLedger:
    return SpawnLedger(**kw)


# -- lineage ------------------------------------------------------------------


def test_lineage_answers_who_asked_for_this() -> None:
    lg = ledger()
    root = lg.open_root("investigate", budget=100)
    child = lg.spawn(root.id, "search", budget=40)
    grandchild = lg.spawn(child.id, "read", budget=10)

    assert lg.lineage(grandchild.id) == [root.id, child.id, grandchild.id]


def test_descendants_are_breadth_first_and_complete() -> None:
    lg = ledger()
    root = lg.open_root("r", budget=100)
    a = lg.spawn(root.id, "a", budget=30)
    b = lg.spawn(root.id, "b", budget=30)
    a1 = lg.spawn(a.id, "a1", budget=10)

    assert set(lg.descendants(root.id)) == {a.id, b.id, a1.id}
    assert lg.descendants(root.id)[:2] == [a.id, b.id]


# -- the brakes ---------------------------------------------------------------


def test_depth_is_capped() -> None:
    lg = ledger(max_depth=2)
    current = lg.open_root("r", budget=100)
    current = lg.spawn(current.id, "d1", budget=50)
    current = lg.spawn(current.id, "d2", budget=20)

    with pytest.raises(SpawnDenied, match="exceeds the limit"):
        lg.spawn(current.id, "d3", budget=5)


def test_a_depth_refusal_names_the_lineage() -> None:
    """A refusal an operator cannot trace is a refusal they cannot act on."""
    lg = ledger(max_depth=1)
    root = lg.open_root("r", budget=100)
    child = lg.spawn(root.id, "c", budget=50)
    with pytest.raises(SpawnDenied) as exc:
        lg.spawn(child.id, "g", budget=10)
    assert "lineage" in str(exc.value)


def test_fan_out_is_capped() -> None:
    lg = ledger(max_children=2)
    root = lg.open_root("r", budget=100)
    lg.spawn(root.id, "a", budget=10)
    lg.spawn(root.id, "b", budget=10)
    with pytest.raises(SpawnDenied, match="limit 2"):
        lg.spawn(root.id, "c", budget=10)


def test_a_subtree_cannot_outspend_its_root() -> None:
    """THE brake that actually bounds cost. Depth and fan-out do not."""
    lg = ledger()
    root = lg.open_root("r", budget=100)
    lg.spawn(root.id, "a", budget=60)
    lg.spawn(root.id, "b", budget=40)

    with pytest.raises(SpawnDenied, match="cannot outspend its root"):
        lg.spawn(root.id, "c", budget=1)


def test_budget_is_reserved_at_spawn_not_at_spend() -> None:
    """Deferring the deduction would let a parent promise the same budget twice."""
    lg = ledger()
    root = lg.open_root("r", budget=100)
    lg.spawn(root.id, "a", budget=100)
    assert root.remaining == 0

    with pytest.raises(SpawnDenied):
        lg.spawn(root.id, "b", budget=1)


def test_a_zero_budget_child_is_refused() -> None:
    lg = ledger()
    root = lg.open_root("r", budget=10)
    with pytest.raises(SpawnDenied, match="positive budget"):
        lg.spawn(root.id, "a", budget=0)


def test_overspending_is_refused_not_merely_reported() -> None:
    lg = ledger()
    root = lg.open_root("r", budget=10)
    lg.record_spend(root.id, 6)
    with pytest.raises(SpawnDenied, match="would spend"):
        lg.record_spend(root.id, 6)
    assert root.spent == 6


def test_a_finished_agent_cannot_spawn() -> None:
    lg = ledger()
    root = lg.open_root("r", budget=100)
    lg.close(root.id)
    with pytest.raises(SpawnDenied, match="cannot spawn"):
        lg.spawn(root.id, "late", budget=10)


# -- closing ------------------------------------------------------------------


def test_closing_a_parent_kills_its_descendants() -> None:
    """An orphan with nobody to return to keeps spending against an unwatched budget."""
    lg = ledger()
    root = lg.open_root("r", budget=100)
    child = lg.spawn(root.id, "c", budget=50)
    grandchild = lg.spawn(child.id, "g", budget=10)

    lg.close(root.id)

    assert lg.get(child.id).state is SpawnState.KILLED
    assert lg.get(grandchild.id).state is SpawnState.KILLED
    assert lg.get(root.id).state is SpawnState.FINISHED
    assert lg.live() == []


def test_subtree_spend_aggregates() -> None:
    lg = ledger()
    root = lg.open_root("r", budget=100)
    child = lg.spawn(root.id, "c", budget=50)
    lg.record_spend(child.id, 20)

    # The root reserved 50 for the child; the child has spent 20 of it.
    assert lg.subtree_spent(root.id) == 70


def test_an_unknown_spawn_is_refused_not_silently_created() -> None:
    import uuid

    with pytest.raises(SpawnDenied, match="unknown spawn"):
        ledger().get(uuid.uuid4())


def test_a_negative_spend_is_refused_rather_than_crediting_the_budget() -> None:
    """`record_spend(id, -5)` would UNDO spend, which is a way to mint budget.

    Found by mutation: deleting the negative-amount guard left every kernel
    test green. `test_overspending_is_refused_not_merely_reported` covers the
    ceiling and nothing covered the floor, so the one brake that actually
    bounds cost — "a subtree can never spend more than its root was given" —
    could be walked backwards by any caller that computes an amount and gets
    the sign wrong.

    It is not only fraud. A refund, a retry that reverses a charge, a token
    count subtracted twice: every one of those arrives as a negative here, and
    silently accepting it makes `subtree_spent` a number nobody can rely on.
    """
    lg = ledger()
    root = lg.open_root("r", budget=100)
    lg.record_spend(root.id, 60)

    with pytest.raises(SpawnDenied, match="negative"):
        lg.record_spend(root.id, -50)

    assert lg.get(root.id).spent == 60, "the refused spend changed the ledger anyway"
    assert lg.get(root.id).remaining == 40
