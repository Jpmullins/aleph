"""Inline-card builders. One per catalog component."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4


def _card(type_name: str, *, card_id: str | None, props: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": type_name,
        "id": card_id or f"{type_name}-{uuid4().hex[:8]}",
        "props": props,
    }


# ---------------------------------------------------------------------------
# Dataclasses (handy for agent code that wants typing without dict-shuffling)
# ---------------------------------------------------------------------------


@dataclass
class ClaimCardProps:
    claim_id: UUID
    text: str
    confidence: str = "cited"
    citations: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class SourceCardProps:
    source_id: UUID
    short_id: str
    title: str
    url: str | None = None
    status: str = "indexed"


@dataclass
class ChartCardProps:
    dataset_version_id: UUID | None
    title: str = ""
    vega_lite_spec: dict[str, Any] = field(default_factory=dict)


@dataclass
class TableCardProps:
    dataset_version_id: UUID | None
    title: str = ""
    columns: list[dict[str, Any]] = field(default_factory=list)
    rows: list[Any] = field(default_factory=list)


@dataclass
class MapCardProps:
    dataset_version_id: UUID | None
    title: str = ""
    maplibre_style_url: str = "https://demotiles.maplibre.org/style.json"
    geo_features: list[Any] = field(default_factory=list)


@dataclass
class GraphCardProps:
    dataset_version_id: UUID | None
    title: str = ""
    nodes: list[Any] = field(default_factory=list)
    edges: list[Any] = field(default_factory=list)


@dataclass
class ApprovalCardProps:
    target_id: UUID
    target_kind: str
    title: str
    summary: str
    severity: str = "info"
    evidence_refs: list[dict[str, Any]] = field(default_factory=list)
    diff_card_id: str | None = None


@dataclass
class FindingCardProps:
    finding_id: UUID
    severity: str
    kind: str
    summary: str
    evidence_refs: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class HypothesisCardProps:
    hypothesis_id: UUID
    title: str
    confidence: str = "initial"
    evidence_count: int = 0


@dataclass
class NotebookCellCardProps:
    section_id: UUID
    body_md: str
    ordinal: int = 0


@dataclass
class FormCardProps:
    form_id: str
    title: str
    fields: list[dict[str, Any]]


@dataclass
class DiffCardProps:
    from_revision_id: UUID
    to_revision_id: UUID
    page_id: UUID


# ---------------------------------------------------------------------------
# Builder functions
# ---------------------------------------------------------------------------


def claim_card(p: ClaimCardProps, *, card_id: str | None = None) -> dict[str, Any]:
    return _card(
        "ClaimCard",
        card_id=card_id,
        props={
            "claim_id": str(p.claim_id),
            "text": p.text,
            "confidence": p.confidence,
            "citations": p.citations,
            "open_action": "open",
        },
    )


def source_card(p: SourceCardProps, *, card_id: str | None = None) -> dict[str, Any]:
    return _card(
        "SourceCard",
        card_id=card_id,
        props={
            "source_id": str(p.source_id),
            "short_id": p.short_id,
            "title": p.title,
            "url": p.url,
            "status": p.status,
            "open_action": "open",
            "navigate_wiki_action": "navigate_wiki",
        },
    )


def chart_card(p: ChartCardProps, *, card_id: str | None = None) -> dict[str, Any]:
    return _card(
        "ChartCard",
        card_id=card_id,
        props={
            "dataset_version_id": str(p.dataset_version_id) if p.dataset_version_id else None,
            "title": p.title,
            "vega_lite_spec": p.vega_lite_spec,
            "open_action": "open",
            # Placeholder only when there is nothing to render at all — an
            # inline vega_lite_spec renders without a dataset binding.
            "_placeholder": p.dataset_version_id is None and not p.vega_lite_spec,
        },
    )


def table_card(p: TableCardProps, *, card_id: str | None = None) -> dict[str, Any]:
    return _card(
        "TableCard",
        card_id=card_id,
        props={
            "dataset_version_id": str(p.dataset_version_id) if p.dataset_version_id else None,
            "title": p.title,
            "columns": p.columns,
            "rows": p.rows,
            "open_action": "open",
        },
    )


def map_card(p: MapCardProps, *, card_id: str | None = None) -> dict[str, Any]:
    return _card(
        "MapCard",
        card_id=card_id,
        props={
            "dataset_version_id": str(p.dataset_version_id) if p.dataset_version_id else None,
            "title": p.title,
            "maplibre_style_url": p.maplibre_style_url,
            "geo_features": p.geo_features,
        },
    )


def graph_card(p: GraphCardProps, *, card_id: str | None = None) -> dict[str, Any]:
    return _card(
        "GraphCard",
        card_id=card_id,
        props={
            "dataset_version_id": str(p.dataset_version_id) if p.dataset_version_id else None,
            "title": p.title,
            "nodes": p.nodes,
            "edges": p.edges,
        },
    )


def approval_card(p: ApprovalCardProps, *, card_id: str | None = None) -> dict[str, Any]:
    return _card(
        "ApprovalCard",
        card_id=card_id,
        props={
            "target_id": str(p.target_id),
            "target_kind": p.target_kind,
            "title": p.title,
            "summary": p.summary,
            "severity": p.severity,
            "evidence_refs": p.evidence_refs,
            "diff_card_id": p.diff_card_id,
            "approve_action": "approve",
            "reject_action": "reject",
            "view_diff_action": "open",
        },
    )


def finding_card(p: FindingCardProps, *, card_id: str | None = None) -> dict[str, Any]:
    return _card(
        "FindingCard",
        card_id=card_id,
        props={
            "finding_id": str(p.finding_id),
            "severity": p.severity,
            "kind": p.kind,
            "summary": p.summary,
            "evidence_refs": p.evidence_refs,
            "open_action": "open",
            "approve_action": "approve",
            "reject_action": "reject",
        },
    )


def hypothesis_card(p: HypothesisCardProps, *, card_id: str | None = None) -> dict[str, Any]:
    return _card(
        "HypothesisCard",
        card_id=card_id,
        props={
            "hypothesis_id": str(p.hypothesis_id),
            "title": p.title,
            "confidence": p.confidence,
            "evidence_count": p.evidence_count,
            "open_action": "open",
        },
    )


def notebook_cell_card(p: NotebookCellCardProps, *, card_id: str | None = None) -> dict[str, Any]:
    return _card(
        "NotebookCellCard",
        card_id=card_id,
        props={
            "section_id": str(p.section_id),
            "body_md": p.body_md,
            "ordinal": p.ordinal,
            "edit_action": "edit_note",
        },
    )


def form_card(p: FormCardProps, *, card_id: str | None = None) -> dict[str, Any]:
    return _card(
        "FormCard",
        card_id=card_id,
        props={
            "form_id": p.form_id,
            "title": p.title,
            "fields": p.fields,
            "submit_action": "submit_form",
        },
    )


def diff_card(p: DiffCardProps, *, card_id: str | None = None) -> dict[str, Any]:
    return _card(
        "DiffCard",
        card_id=card_id,
        props={
            "from_revision_id": str(p.from_revision_id),
            "to_revision_id": str(p.to_revision_id),
            "page_id": str(p.page_id),
            "open_action": "open",
        },
    )
