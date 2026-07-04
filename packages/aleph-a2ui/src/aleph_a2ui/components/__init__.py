"""Typed Python builders for A2UI surfaces and inline cards.

Use these from agent code instead of hand-building dicts — the builders
encode the catalog's required props and emit JSON that round-trips
through `validate_surface` cleanly.
"""

from aleph_a2ui.components.cards import (
    ApprovalCardProps,
    ChartCardProps,
    ClaimCardProps,
    DiffCardProps,
    FindingCardProps,
    FormCardProps,
    HypothesisCardProps,
    SourceCardProps,
    TableCardProps,
    approval_card,
    chart_card,
    claim_card,
    diff_card,
    finding_card,
    form_card,
    hypothesis_card,
    source_card,
    table_card,
)
from aleph_a2ui.components.surfaces import (
    artifacts_surface_v09,
    briefs_surface_v09,
    hypotheses_surface_v09,
    notes_surface_v09,
    wiki_surface_v09,
)

__all__ = [
    "ApprovalCardProps",
    "ChartCardProps",
    "ClaimCardProps",
    "DiffCardProps",
    "FindingCardProps",
    "FormCardProps",
    "HypothesisCardProps",
    "SourceCardProps",
    "TableCardProps",
    "approval_card",
    "artifacts_surface_v09",
    "briefs_surface_v09",
    "chart_card",
    "claim_card",
    "diff_card",
    "finding_card",
    "form_card",
    "hypotheses_surface_v09",
    "hypothesis_card",
    "notes_surface_v09",
    "source_card",
    "table_card",
    "wiki_surface_v09",
]
