"""The `viz_builder` subagent — quick charts + approval-gated artifacts (Wave 3 T5).

Delegating "make a chart" / "build a report" to this subagent keeps the
visualization + build chatter in an isolated context, so the orchestrator's
thread just sees one short render instruction back. Two tools:

- `make_chart` — a convenience viz tool that builds a minimal Vega-Lite spec
  from inline data points and returns a ChartCard render instruction (no
  approval needed; the W4 catalog renders it via `render_a2ui`).
- `build_artifact` — the full Builder path for reports/decks/source-packs. It
  reuses the exact approval-gated impl (`_build_artifact_impl`, rule #3 —
  self-calls the agent-actions/request route, never raw DB) so the Wave-6
  approval gate is preserved: the build only runs after the analyst approves.

Its LLM calls use the cost-attributed subagent model (`subagent_model`, rule #5
— they write `ModelCall` + `CostLedgerEvent` tagged `assistant.subagent.viz_builder`).
"""

from __future__ import annotations

import json
from typing import Any

from langchain_core.runnables import (
    RunnableConfig,
)
from langchain_core.tools import tool


def build_viz_builder_subagent(*, settings: Any) -> dict[str, Any]:
    """Build the viz_builder subagent dict (a deepagents `SubAgent`).

    The imports from `aleph_api.copilot_agent` are function-local to avoid a
    circular import (copilot_agent does not import this module at top level; the
    orchestrator builder calls this function at startup).
    """
    from aleph_api.copilot_agent import (
        _build_artifact_impl,  # pyright: ignore[reportPrivateUsage] — shared build body deliberately reused (DRY); module-private to the api
        _pin_to_briefs_impl,  # pyright: ignore[reportPrivateUsage]
        subagent_model,
    )

    @tool
    async def make_chart(
        title: str,
        x_field: str,
        y_field: str,
        points: list[dict[str, Any]],
        config: RunnableConfig,
        pin: bool = True,
    ) -> str:
        """Make a quick chart from inline data points (no approval needed).

        Builds a minimal Vega-Lite spec (bar chart) over `points` (a list of
        objects, each carrying the `x_field` and `y_field` keys) and returns a
        ChartCard render instruction. By default the chart is also pinned to
        the Briefs tab so it survives the conversation (pass pin=false for a
        throwaway inline chart). For a full report/deck/export, use
        build_artifact.
        """
        spec: dict[str, Any] = {
            "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
            "title": title,
            "data": {"values": points},
            "mark": "bar",
            "encoding": {
                "x": {"field": x_field, "type": "nominal"},
                "y": {"field": y_field, "type": "quantitative"},
            },
        }
        pinned = ""
        if pin:
            pinned = await _pin_to_briefs_impl(
                "ChartCard",
                title,
                {
                    "dataset_version_id": None,
                    "title": title,
                    "vega_lite_spec": spec,
                    "open_action": "open",
                    "_placeholder": True,
                },
                config,
            )
            pinned = f" {pinned}"
        return (
            f"Render a ChartCard with title='{title}' and "
            f"vega_lite_spec={json.dumps(spec)}.{pinned}"
        )

    @tool
    async def build_artifact(
        title: str,
        config: RunnableConfig,
        artifact_kind: str = "report_markdown_bundle",
        wiki_page_ids: list[str] | None = None,
        csl_style: str = "apa-7",
    ) -> str:
        """Build a product artifact (report/deck/source-pack) from approved wiki pages.

        This is a consequential action, so it is **approval-gated**: instead of
        building immediately, it creates a pending approval and asks you to
        render an ApprovalCard. The build only runs after the analyst clicks
        Approve. `artifact_kind` is one of report_pdf, report_docx,
        report_markdown_bundle, source_pack, deck_pdf. Use when the analyst asks
        to draft/build a report, deck, or export.
        """
        return await _build_artifact_impl(title, config, artifact_kind, wiki_page_ids, csl_style)

    return {
        "name": "viz_builder",
        "description": (
            "Builds visualizations and product artifacts: quick charts "
            "(make_chart → ChartCard) and full reports/decks/source-packs "
            "(build_artifact, which is approval-gated). Delegate when the analyst "
            "wants a chart, report, export, or visualization."
        ),
        "system_prompt": (
            "You are Aleph's viz builder. For a quick chart, call make_chart and "
            "return the ChartCard render instruction. For a report/deck/export, "
            "call build_artifact (it will surface an approval card). Return "
            "concise render instructions, never raw specs as prose."
        ),
        "tools": [make_chart, build_artifact],
        "model": subagent_model(settings, "viz_builder"),
    }
