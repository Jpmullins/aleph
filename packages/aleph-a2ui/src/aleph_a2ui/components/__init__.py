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
    GraphCardProps,
    HypothesisCardProps,
    MapCardProps,
    NotebookCellCardProps,
    SourceCardProps,
    TableCardProps,
    approval_card,
    chart_card,
    claim_card,
    diff_card,
    finding_card,
    form_card,
    graph_card,
    hypothesis_card,
    map_card,
    notebook_cell_card,
    source_card,
    table_card,
)
from aleph_a2ui.components.surfaces import (
    artifacts_surface,
    briefs_surface,
    hypotheses_surface,
    notes_surface,
    wiki_surface,
)

__all__ = [
    "ApprovalCardProps",
    "ChartCardProps",
    "ClaimCardProps",
    "DiffCardProps",
    "FindingCardProps",
    "FormCardProps",
    "GraphCardProps",
    "HypothesisCardProps",
    "MapCardProps",
    "NotebookCellCardProps",
    "SourceCardProps",
    "TableCardProps",
    "approval_card",
    "artifacts_surface",
    "briefs_surface",
    "chart_card",
    "claim_card",
    "diff_card",
    "finding_card",
    "form_card",
    "graph_card",
    "hypotheses_surface",
    "hypothesis_card",
    "map_card",
    "notebook_cell_card",
    "notes_surface",
    "source_card",
    "table_card",
    "wiki_surface",
]
