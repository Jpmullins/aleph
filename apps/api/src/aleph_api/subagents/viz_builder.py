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
    from aleph_api.agent_middleware import AlephAgentMiddleware
    from aleph_api.copilot_agent import (
        _build_artifact_impl,  # pyright: ignore[reportPrivateUsage] — shared build body deliberately reused (DRY); module-private to the api
        _pin_to_briefs_impl,  # pyright: ignore[reportPrivateUsage]
        _render_code_via_runner_impl,  # pyright: ignore[reportPrivateUsage]
        subagent_model,
    )
    from aleph_core.schemas.model_profile import Capability

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
            "$schema": "https://vega.github.io/schema/vega-lite/v6.json",
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
    async def render_chart_via_code(
        python_code: str,
        config: RunnableConfig,
        output_kind: str = "png",
        title: str = "Sandbox chart",
        pin: bool = True,
    ) -> str:
        """Render a chart by running Python in the isolated sandbox, then pin it.

        Use this when a chart needs real computation (matplotlib/pandas/numpy) or
        a data transform — anything beyond a trivial inline Vega-Lite spec. Write
        self-contained Python that produces ONE output:
          - output_kind='png' or 'svg': draw with matplotlib (the sandbox saves
            the current figure, or write to the path in the `OUTPUT_PATH` global);
          - output_kind='vega': define a top-level `spec` dict (a Vega-Lite spec)
            or an Altair chart named `spec`;
          - output_kind='html': define a top-level `html` string (self-contained).
        The code runs with NO network and NO credentials (amended rule 8); its
        output becomes a versioned artifact pinned to Briefs as an ImageCard /
        ChartCard / HtmlFrameCard. For a trivial bar chart from inline points,
        prefer make_chart instead.
        """
        return await _render_code_via_runner_impl(python_code, output_kind, config, title, pin)

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
            "(make_chart → ChartCard), computed charts via the sandbox "
            "(render_chart_via_code → code_runner → pinned artifact card), and "
            "full reports/decks/source-packs (build_artifact, approval-gated). "
            "Delegate when the analyst wants a chart, report, export, or "
            "visualization."
        ),
        "system_prompt": (
            "You are Aleph's viz builder. For a trivial chart from inline points, "
            "call make_chart. For a chart needing real computation "
            "(matplotlib/pandas) or a data transform, call render_chart_via_code "
            "(it runs your Python in the isolated sandbox and pins the artifact). "
            "For a report/deck/export, call build_artifact (it surfaces an "
            "approval card). Return concise render instructions, never raw specs "
            "as prose."
        ),
        "tools": [make_chart, render_chart_via_code, build_artifact],
        # The same tool guard the orchestrator carries. deepagents lets a
        # subagent spec override the parent's middleware rather than extend it,
        # so "the orchestrator has it" is not "the subagents have it" —
        # scripts/check-agent-middleware.sh asserts all six do.
        "middleware": [AlephAgentMiddleware()],
        "model": subagent_model(settings, "viz_builder", capability=Capability.CODE),
    }
