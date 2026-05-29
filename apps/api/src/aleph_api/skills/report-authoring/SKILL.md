---
name: report-authoring
description: Compose a report, deck, or export artifact from approved wiki pages — how to select pages, choose artifact_kind and csl_style, delegate to the viz_builder subagent (approval-gated build), and where the finished artifact lands (Artifacts tab).
---

# Report / artifact authoring

Use this when the analyst wants a deliverable — a report, deck, or source pack —
built from the wiki.

## 1. Pick the pages

- Build only from **approved wiki content**. Use `search_wiki` to find the
  relevant pages, confirm the set with the analyst, and collect their page ids.
- A report should be grounded in pages that already exist; if a needed topic is
  missing, research it first (see the `research` skill) before authoring.

## 2. Choose the shape

- `artifact_kind`:
  - `report_markdown_bundle` — a cited written report (default).
  - deck / source-pack kinds for slides or a bundle of underlying sources.
- `csl_style` — the citation style for the bibliography (e.g. `apa-7`).
  Default to `apa-7` unless the analyst names another.

## 3. Delegate the build (approval-gated)

- Delegate to the `viz_builder` subagent via the `task` tool with the title,
  `artifact_kind`, `csl_style`, and the selected `wiki_page_ids`.
- For quick inline figures the subagent returns a ChartCard render instruction —
  render it directly.
- A full report/deck/export is **consequential**: the subagent returns an
  instruction to render an **ApprovalCard** instead of building immediately.
  Render that ApprovalCard exactly and tell the analyst the build runs once they
  approve.

## 4. Where it lands

- After the analyst approves, the build runs and the finished artifact appears
  in the **Artifacts tab**. Point them there to view, re-render, or export it.
