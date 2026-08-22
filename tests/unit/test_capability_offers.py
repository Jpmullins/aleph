"""The capability sweep must model reality, and its parsers must see the trap.

`rerank` was offered in the Settings picker, had a `CAPABILITY_POLICIES` entry,
had a help string — and nothing in Aleph resolves that capability, so every
`autoconfigure` run reported it permanently unbound and a model bound to it
would never have been called.

The obvious check was ``grep -rn '"rerank"' | grep -v tests | wc -l == 1``. It
would have gone green with the orphan help text still shipping, because
`CAPABILITY_HELP` spells the key **unquoted** (``rerank: "Reorders retrieved
chunks",``) and the grep never matched it. `test_the_help_parser_sees_an_unquoted_key`
is that specific defect, pinned.

`KNOWN_UNRESOLVED` is asserted to be exactly right in both directions: an entry
that is no longer needed fails as loudly as a missing one, so it cannot quietly
become the place orphans go to live.
"""

from __future__ import annotations

import pathlib

from capability_offers import (
    KNOWN_UNRESOLVED,
    help_capabilities,
    offered_capabilities,
    policy_capabilities,
    run,
)

ROOT = pathlib.Path(__file__).resolve().parents[2]


def test_every_offered_capability_has_a_policy_and_a_caller() -> None:
    problems = run(ROOT, exempt=KNOWN_UNRESOLVED)
    assert not problems, (
        "the Settings picker, CAPABILITY_POLICIES and the call sites disagree:\n"
        + "\n".join(f"  [{p.kind}] {p}" for p in problems)
    )


def test_the_exemptions_are_all_still_needed() -> None:
    """An exemption list nobody prunes is how a sweep stops meaning anything."""
    unresolved = {p.capability for p in run(ROOT) if p.kind == "no-call-site"}
    stale = sorted(KNOWN_UNRESOLVED - unresolved)
    assert not stale, (
        f"{stale} now has a resolving call site (or no policy). Delete the entry from "
        f"KNOWN_UNRESOLVED so the next orphan cannot hide behind it."
    )


def test_rerank_is_gone_from_all_three_lists() -> None:
    """The defect this sweep was written for, pinned by name."""
    drawers = (ROOT / "apps/web/src/components/Drawers.tsx").read_text(encoding="utf-8")
    discovery = (ROOT / "packages/aleph-models/src/aleph_models/discovery.py").read_text(
        encoding="utf-8"
    )
    members = {"RERANK": "rerank"}
    assert "rerank" not in offered_capabilities(drawers)
    assert "rerank" not in help_capabilities(drawers)
    assert "rerank" not in policy_capabilities(discovery, members)


# --- the parsers, against synthetic sources -------------------------------

_DRAWERS = """
const CAPABILITIES = [
  "synthesis",
  "rerank",
] as const;

const CAPABILITY_HELP: Record<string, string> = {
  synthesis: "Composes briefs and wiki pages",
  rerank: "Reorders retrieved chunks",
};
"""


def test_the_offer_parser_reads_the_array() -> None:
    assert offered_capabilities(_DRAWERS) == ["synthesis", "rerank"]


def test_the_help_parser_sees_an_unquoted_key() -> None:
    """The exact reason the grep in the original criterion could not fail."""
    assert help_capabilities(_DRAWERS) == ["synthesis", "rerank"]
    help_block = _DRAWERS.split("CAPABILITY_HELP", 1)[1]
    assert '"rerank"' not in help_block, (
        "the trap is that the help key is NOT quoted — if this sample quotes it, "
        "the test is no longer reproducing the defect"
    )


def test_the_help_parser_also_reads_a_quoted_key() -> None:
    source = """
const CAPABILITY_HELP: Record<string, string> = {
  "synthesis": "Composes briefs",
};
"""
    assert help_capabilities(source) == ["synthesis"]


def test_the_policy_parser_reads_enum_attribute_keys() -> None:
    source = """
CAPABILITY_POLICIES: dict[Capability, CapabilityPolicy] = {
    Capability.SYNTHESIS: CapabilityPolicy(mode="chat", tier="heavy"),
    Capability.EMBEDDING: CapabilityPolicy(mode="embedding", tier="light"),
}
"""
    members = {"SYNTHESIS": "synthesis", "EMBEDDING": "embedding", "RERANK": "rerank"}
    assert policy_capabilities(source, members) == {"synthesis", "embedding"}
