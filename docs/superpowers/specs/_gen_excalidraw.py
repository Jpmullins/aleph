"""Generate three Excalidraw diagram files for the Aleph spec.

Outputs (next to this script):
- 2026-05-26-aleph-architecture.excalidraw   (topology + retrieval-flow inset)
- 2026-05-26-aleph-domain.excalidraw         (wiki at center; RKS upstream)
- 2026-05-26-aleph-ui.excalidraw             (slim left panel + 5-tab right)
"""

from __future__ import annotations

import json
import random
from pathlib import Path

OUT_DIR = Path(__file__).parent
random.seed(17)


def _seed() -> int:
    return random.randint(1, 10_000_000)


def rect(*, x, y, w, h, bg="transparent", stroke="#1e1e1e", stroke_width=2,
         stroke_style="solid", fill_style="solid", roundness=True, group=None, eid=None) -> dict:
    return {
        "id": eid or f"r{_seed()}",
        "type": "rectangle",
        "x": x, "y": y, "width": w, "height": h, "angle": 0,
        "strokeColor": stroke, "backgroundColor": bg, "fillStyle": fill_style,
        "strokeWidth": stroke_width, "strokeStyle": stroke_style,
        "roughness": 1, "opacity": 100, "groupIds": group or [],
        "frameId": None, "roundness": {"type": 3} if roundness else None,
        "seed": _seed(), "version": 1, "versionNonce": _seed(),
        "isDeleted": False, "boundElements": [], "updated": 1,
        "link": None, "locked": False,
    }


def ellipse(*, x, y, w, h, bg="transparent", stroke="#1e1e1e", stroke_width=2) -> dict:
    return {
        "id": f"e{_seed()}",
        "type": "ellipse",
        "x": x, "y": y, "width": w, "height": h, "angle": 0,
        "strokeColor": stroke, "backgroundColor": bg, "fillStyle": "solid",
        "strokeWidth": stroke_width, "strokeStyle": "solid",
        "roughness": 1, "opacity": 100, "groupIds": [],
        "frameId": None, "roundness": None,
        "seed": _seed(), "version": 1, "versionNonce": _seed(),
        "isDeleted": False, "boundElements": [], "updated": 1,
        "link": None, "locked": False,
    }


def text(*, x, y, text, font_size=16, width=None, height=None, color="#1e1e1e",
         align="left", group=None, eid=None) -> dict:
    lines = text.split("\n")
    longest = max(len(l) for l in lines)
    w = width if width is not None else int(longest * font_size * 0.6) + 10
    h = height if height is not None else int(len(lines) * font_size * 1.25) + 6
    return {
        "id": eid or f"t{_seed()}",
        "type": "text",
        "x": x, "y": y, "width": w, "height": h, "angle": 0,
        "strokeColor": color, "backgroundColor": "transparent", "fillStyle": "solid",
        "strokeWidth": 1, "strokeStyle": "solid",
        "roughness": 0, "opacity": 100, "groupIds": group or [],
        "frameId": None, "roundness": None,
        "seed": _seed(), "version": 1, "versionNonce": _seed(),
        "isDeleted": False, "boundElements": [], "updated": 1,
        "link": None, "locked": False,
        "fontSize": font_size, "fontFamily": 3, "text": text,
        "textAlign": align, "verticalAlign": "top",
        "baseline": int(font_size * 0.85),
        "containerId": None, "originalText": text,
        "lineHeight": 1.25, "autoResize": True,
    }


def arrow(*, x, y, dx, dy, stroke="#1e1e1e", stroke_width=2, dashed=False, end_arrow=True) -> dict:
    return {
        "id": f"a{_seed()}",
        "type": "arrow",
        "x": x, "y": y, "width": abs(dx), "height": abs(dy), "angle": 0,
        "strokeColor": stroke, "backgroundColor": "transparent", "fillStyle": "solid",
        "strokeWidth": stroke_width, "strokeStyle": "dashed" if dashed else "solid",
        "roughness": 1, "opacity": 100, "groupIds": [],
        "frameId": None, "roundness": {"type": 2},
        "seed": _seed(), "version": 1, "versionNonce": _seed(),
        "isDeleted": False, "boundElements": [], "updated": 1,
        "link": None, "locked": False,
        "points": [[0, 0], [dx, dy]], "lastCommittedPoint": None,
        "startBinding": None, "endBinding": None,
        "startArrowhead": None, "endArrowhead": "arrow" if end_arrow else None,
    }


def box(*, x, y, w, h, label, bg="#e7f5ff", font=16) -> list[dict]:
    return [rect(x=x, y=y, w=w, h=h, bg=bg), text(x=x + 12, y=y + 10, text=label, font_size=font)]


def title(x, y, t) -> dict:
    return text(x=x, y=y, text=t, font_size=28, color="#1e1e1e")


def subtitle(x, y, t) -> dict:
    return text(x=x, y=y, text=t, font_size=16, color="#5f6368")


def write_excalidraw(path: Path, elements: list[dict]) -> None:
    doc = {
        "type": "excalidraw", "version": 2,
        "source": "https://excalidraw.com",
        "elements": elements,
        "appState": {"gridSize": None, "viewBackgroundColor": "#ffffff"},
        "files": {},
    }
    path.write_text(json.dumps(doc, indent=2))


# ─── Diagram 1: Architecture with retrieval-flow inset ───────────────────────

def build_architecture() -> list[dict]:
    e: list[dict] = []
    e.append(title(40, 30, "Aleph — Runtime Architecture (wiki-first retrieval)"))
    e.append(subtitle(40, 70, "The wiki is the primary KB; RKS chunks reached only via intra-source descent"))

    # Browser
    e += box(x=40, y=130, w=820, h=90,
             label="Browser  React + @a2ui/react + CopilotKit (AG-UI)\n"
                   "3-panel shell: left (projects+sessions), center (chat), right (5 A2UI surfaces)",
             bg="#fff4e6")

    # API
    e += box(x=40, y=240, w=820, h=200,
             label=("aleph-api  (FastAPI + Pydantic)\n"
                    "• Typed services • Auth + project-scoping middleware\n"
                    "• Wiki retrieval router (page-selector LLM call) ← the primary path\n"
                    "• Action-ledger writer (hash-chained)  • Cost-ledger writer\n"
                    "• A2UI Python SDK  • Langfuse + OTEL trace context\n"
                    "• AIQ client (service token, async jobs, SSE consumer)"),
             bg="#e7f5ff")

    # Workers
    e += box(x=40, y=470, w=400, h=220,
             label=("aleph-workers\n"
                    "• Wiki agent (ingest, compile,\n"
                    "  index, alias extraction,\n"
                    "  SourcePage maintenance,\n"
                    "  synthesis drafts)\n"
                    "• MechanicalReviewer\n"
                    "• EditorialReviewer (DeepAgents)\n"
                    "• Builder\n"
                    "• Normalization\n"
                    "• Intra-source chunk + embed\n"
                    "• Playwright render"),
             bg="#e3fafc")

    # AIQ
    e += box(x=460, y=470, w=400, h=220,
             label=("aiq-server  (NeMo Agent Toolkit)\n"
                    "• Orchestrator  • ShallowResearcher\n"
                    "• DeepResearcher  • Clarifier\n"
                    "• data_source_registry (connectors)\n"
                    "• tokenomics → CostLedger adapter\n"
                    "• citation_verification → MechReviewer\n"
                    "• Custom LangChain middleware\n"
                    "\n"
                    "All LLM calls via LiteLLM gateway\n"
                    "(_type: openai, base_url=LITELLM_BASE_URL).\n"
                    "Output = synthesis proposal, never wiki write."),
             bg="#f3f0ff")

    # LiteLLM Gateway — the single LLM transport
    e += box(x=880, y=470, w=180, h=130,
             label=("Insights LiteLLM\nGateway\n"
                    "(OpenAI-compat)\n"
                    "• model routing\n"
                    "• quotas, audit\n"
                    "• provider swap"),
             bg="#fff0f6", font=12)
    # Langfuse
    e += box(x=880, y=610, w=180, h=80,
             label="Langfuse\ntraces + evals\n(OTEL ingest)", bg="#f3f0ff", font=13)

    # Data row
    e += box(x=40, y=720, w=380, h=160,
             label=("Postgres + pgvector\n"
                    "• wiki pages/revisions/index/links/aliases\n"
                    "• SourcePages + WikiClaim + Citation\n"
                    "• per-source DocumentChunk (pgvector)\n"
                    "• action ledger  • cost ledger\n"
                    "• domain rows"),
             bg="#ebfbee")
    e += box(x=440, y=720, w=300, h=160,
             label=("MinIO / S3\n"
                    "• raw assets\n"
                    "• normalized md cache\n"
                    "• artifacts\n"
                    "• rendered assets"),
             bg="#ebfbee")
    qdrant = rect(x=760, y=720, w=300, h=160, bg="#f8f9fa", stroke_style="dashed")
    e.append(qdrant)
    e.append(text(x=772, y=730,
                  text="Qdrant (optional)\nonly if pgvector\nceiling is hit for\nintra-source chunks",
                  font_size=14))

    # Arrows
    e.append(arrow(x=440, y=220, dx=0, dy=18))
    e.append(text(x=460, y=222, text="HTTP / SSE", font_size=13, color="#495057"))
    e.append(arrow(x=240, y=440, dx=0, dy=28))
    e.append(arrow(x=660, y=440, dx=0, dy=28))
    e.append(text(x=540, y=445, text="service token + correlation id", font_size=11, color="#495057"))

    # AIQ -> API (tool callbacks)
    e.append(arrow(x=620, y=690, dx=-300, dy=-240, dashed=True))
    e.append(text(x=320, y=455, text="connector tools re-enter aleph-api\nfor creds + scope + writes",
                  font_size=11, color="#495057"))

    # Workers + AIQ -> LiteLLM Gateway (every LLM call)
    e.append(arrow(x=440, y=520, dx=440, dy=10))
    e.append(arrow(x=860, y=540, dx=20, dy=0))
    e.append(text(x=550, y=505, text="LLM/embed via gateway", font_size=11, color="#7c4900"))
    # Workers + AIQ -> Langfuse
    e.append(arrow(x=440, y=600, dx=440, dy=30))
    e.append(arrow(x=860, y=620, dx=20, dy=20))

    # API -> Postgres
    e.append(arrow(x=200, y=440, dx=20, dy=280, dashed=True))
    # workers -> Postgres
    e.append(arrow(x=240, y=690, dx=-10, dy=28))

    # AIQ no direct DB
    block = rect(x=620, y=680, w=130, h=22, bg="#ffe3e3", stroke="#fa5252")
    e.append(block)
    e.append(text(x=630, y=685, text="✗ no direct DB", font_size=12, color="#c92a2a"))

    # ── Retrieval flow inset ──
    inset_x = 1100
    inset_y = 130
    e += box(x=inset_x, y=inset_y, w=480, h=560,
             label="Retrieval flow  (the load-bearing path)",
             bg="#fff8e1", font=18)
    steps = [
        ("1. WikiIndex page-selector LLM",
         "query → top-K wiki page IDs"),
        ("2. Load selected pages",
         "+ 1-hop wikilink expansion"),
        ("3. Answer composer LLM",
         "writes answer w/ [[wikilinks]] + [c12]"),
        ("4a. Descent (if coverage gap + cites SourcePage)",
         "load source's normalized text\n+ intra-source chunk retrieve"),
        ("4b. Synthesis (if coverage gap, no source)",
         "trigger AIQ research → wiki agent\n→ reviewer → approval → wiki page"),
    ]
    sy = inset_y + 50
    for hdr, body in steps:
        e.append(rect(x=inset_x + 16, y=sy, w=448, h=80, bg="#fffaf2", stroke="#fab005"))
        e.append(text(x=inset_x + 28, y=sy + 8, text=hdr, font_size=14, color="#7c4900"))
        e.append(text(x=inset_x + 28, y=sy + 32, text=body, font_size=12, color="#5f6368"))
        sy += 92
    e.append(text(x=inset_x + 16, y=sy + 4,
                  text="Embeddings appear ONLY at 4a (intra-source).\nPrimary path 1→3 is wiki page-selection.",
                  font_size=12, color="#c92a2a"))

    # Legend
    e.append(text(x=40, y=910,
                  text=("Solid = direct call.  Dashed = callback / persistence path.\n"
                        "AIQ never writes Postgres or S3; tool calls re-enter aleph-api for creds + writes."),
                  font_size=12, color="#5f6368"))
    return e


# ─── Diagram 2: Domain model with wiki at center ─────────────────────────────

def build_domain() -> list[dict]:
    e: list[dict] = []
    e.append(title(40, 30, "Aleph — Domain Model (wiki at center)"))
    e.append(subtitle(40, 70, "RKS upstream → Wiki primary KB → analyst-authored + governance + outputs around it"))

    # WIKI — at center
    e += box(x=480, y=200, w=420, h=300,
             label=("WIKI  (primary KB)\n"
                    "WikiPage / WikiRevision (immutable)\n"
                    "WikiSection\n"
                    "WikiLink  [[wikilinks]]\n"
                    "WikiClaim (graded confidence)\n"
                    "Citation  Claim → DocumentChunk[] | SourcePage\n"
                    "SourcePage  [[Source:X]]\n"
                    "Alias  surface_form → canonical_name\n"
                    "HandEditMark  (protects analyst edits)\n"
                    "RejectionFeedback (next-compile prompt)\n"
                    "WikiIndex  (page-selector retrieval idx)"),
             bg="#d0ebff")

    # RKS — upstream (left)
    e += box(x=40, y=240, w=380, h=220,
             label=("RKS  (upstream)\n"
                    "Source / SourceVersion / SourceAsset\n"
                    "Connector / ConnectorBinding\n"
                    "ConnectorCredential (encrypted)\n"
                    "NormalizedDocument\n"
                    "DocumentChunk  (pgvector + FTS)\n"
                    "  ↳ used for intra-source descent only\n"
                    "RetrievalIndexRecord (per source)"),
             bg="#d3f9d8")

    # ANALYST-AUTHORED (bottom-left)
    e += box(x=40, y=540, w=380, h=160,
             label=("ANALYST-AUTHORED\n"
                    "Hypothesis / HypothesisVersion\n"
                    "HypothesisEvidence (→ claims + chunks)\n"
                    "Note / NoteSection\n"
                    "  (free-form with [[wikilink]] affordances)"),
             bg="#f3d9fa")

    # GOVERNANCE (bottom-center)
    e += box(x=480, y=540, w=420, h=160,
             label=("GOVERNANCE\n"
                    "ReviewRun (mech | editorial)\n"
                    "ReviewFinding (severity, evidence_refs)\n"
                    "ApprovalRequest / ApprovalDecision\n"
                    "UserFeedback (claim/source/card target)"),
             bg="#ffe3e3")

    # INTERACTIVE A2UI (right)
    e += box(x=960, y=200, w=420, h=180,
             label=("INTERACTIVE  (A2UI)\n"
                    "Surface components: WikiSurface,\n"
                    "  ArtifactsSurface, NotesSurface,\n"
                    "  HypothesesSurface, BriefsSurface\n"
                    "InteractiveCard / Version\n"
                    "CardAction (ledgered)\n"
                    "RenderedAsset (PNG/SVG/PDF)"),
             bg="#ffe8cc")

    # DATASETS (right-middle)
    e += box(x=960, y=400, w=420, h=130,
             label=("DATASETS  (no tab — embedded)\n"
                    "Dataset / DatasetVersion (immutable)\n"
                    "Observation (row)\n"
                    "Render inside Wiki / Notes / Briefs via\n"
                    "ChartCard / TableCard / MapCard / GraphCard"),
             bg="#fff4e6")

    # ARTIFACTS (right-bottom)
    e += box(x=960, y=545, w=420, h=120,
             label=("ARTIFACTS  (output)\n"
                    "Artifact / ArtifactVersion\n"
                    "Built by Builder from approved\n"
                    "wiki + dataset snapshots."),
             bg="#ffe8cc")

    # OPERATIONS (top-right)
    e += box(x=960, y=70, w=420, h=120,
             label=("OPERATIONS\n"
                    "ModelProfile / ModelCall\n"
                    "CostLedgerEvent (per phase, per model)\n"
                    "Budget (soft / hard)\n"
                    "AgentRun / AgentEvent / AgentMemory"),
             bg="#e5dbff")

    # PROVENANCE (top-center)
    e += box(x=480, y=70, w=420, h=120,
             label=("PROVENANCE\n"
                    "ActionLedgerEvent (append-only, hash-chained)\n"
                    "Trace (Langfuse + OTEL pointer)\n"
                    "Every row: id, project_id, timestamps,\n"
                    "created_by, access_scope, trace_id?,\n"
                    "ledger_event_id?"),
             bg="#d0ebff")

    # QUALITY (top-left)
    e += box(x=40, y=70, w=380, h=120,
             label=("QUALITY\n"
                    "EvalDataset / EvalCase / EvalRun / EvalResult\n"
                    "Public bench: FreshQA, DeepResearch Bench\n"
                    "Aleph evals: Coverage, Page-Selection,\n"
                    "  Citations, Conflicts, Permissions, Descent"),
             bg="#e5dbff")

    # Arrows showing flow
    # RKS -> Wiki (compile)
    e.append(arrow(x=420, y=350, dx=60, dy=0))
    e.append(text(x=440, y=325, text="wiki agent\ncompiles", font_size=12, color="#5f6368"))
    # Wiki -> RKS (descent)
    e.append(arrow(x=480, y=380, dx=-60, dy=0))
    e.append(text(x=440, y=385, text="descent\n(intra-source)", font_size=12, color="#5f6368"))
    # Wiki -> Interactive (renders)
    e.append(arrow(x=900, y=300, dx=60, dy=0))
    e.append(text(x=900, y=275, text="renders via", font_size=12, color="#5f6368"))
    # Hypotheses -> Wiki (evidence)
    e.append(arrow(x=420, y=600, dx=60, dy=-100))
    e.append(text(x=440, y=560, text="evidence → claims", font_size=12, color="#5f6368"))
    # Governance -> Wiki (gates)
    e.append(arrow(x=690, y=540, dx=0, dy=-40))
    e.append(text(x=700, y=510, text="gates revisions", font_size=12, color="#5f6368"))

    e.append(text(x=40, y=720,
                  text=("The wiki is the spine. RKS is upstream; analyst-authored and outputs are around it.\n"
                        "Datasets are domain objects rendered as A2UI cards inside whichever surface they belong to."),
                  font_size=12, color="#5f6368"))
    return e


# ─── Diagram 3: UI wireframe — slim left + 5-tab right ───────────────────────

def build_ui() -> list[dict]:
    e: list[dict] = []
    e.append(title(40, 30, "Aleph — Project Workspace"))
    e.append(subtitle(40, 70, "Left: projects + sessions + bottom icons.  Right: 5 A2UI surfaces (Wiki | Artifacts | Notes | Hypotheses | Briefs)"))

    # App frame
    e.append(rect(x=40, y=120, w=1280, h=760, bg="transparent"))

    # Top bar
    e.append(rect(x=40, y=120, w=1280, h=44, bg="#f1f3f5"))
    e.append(text(x=56, y=132, text="Aleph", font_size=18))
    e.append(text(x=140, y=134, text="Project: OSINT — Regional Power Grids 2026", font_size=14, color="#495057"))
    e.append(text(x=900, y=134, text="Cost: $4.18 / $50.00    budget OK", font_size=13, color="#2f9e44"))
    e.append(text(x=1240, y=132, text="● jp", font_size=14))

    # LEFT PANEL — slim
    e.append(rect(x=40, y=164, w=220, h=716, bg="#f8f9fa"))
    e.append(text(x=56, y=180, text="◆ Projects", font_size=14))
    e.append(rect(x=48, y=204, w=204, h=24, bg="#d0ebff", stroke="#74c0fc", stroke_width=1))
    e.append(text(x=56, y=210, text="OSINT — Power Grids 2026", font_size=12))
    e.append(text(x=56, y=232, text="Academic — AI Safety lit", font_size=12, color="#868e96"))
    e.append(text(x=56, y=252, text="+ New project", font_size=12, color="#1971c2"))

    e.append(text(x=56, y=290, text="◆ Sessions", font_size=14))
    sessions = [
        ("#142  Grid resilience  ▸ now", True),
        ("#141  Transformer ratings  2h", False),
        ("#140  S3↔S7 conflict review  yesterday", False),
        ("#139  Hypothesis: capacity drift  Mon", False),
        ("#138  arXiv 2410 sweep  Mon", False),
        ("#137  Initial bootstrap  Sun", False),
    ]
    sy = 314
    for label, active in sessions:
        if active:
            e.append(rect(x=48, y=sy - 4, w=204, h=22, bg="#d0ebff", stroke="#74c0fc", stroke_width=1))
        e.append(text(x=56, y=sy, text=label, font_size=12))
        sy += 26

    # Bottom icon row
    e.append(rect(x=40, y=830, w=220, h=50, bg="#f1f3f5"))
    icons = [("⚙", 64, "settings"), ("▤", 116, "logs"), ("🔔", 168, "notifications"), ("●", 220, "profile")]
    for sym, ix, label in icons:
        e.append(text(x=ix, y=842, text=sym, font_size=18))

    # CENTER — chat + activity
    e.append(rect(x=260, y=164, w=600, h=716, bg="#ffffff"))
    e.append(rect(x=260, y=164, w=600, h=30, bg="#f1f3f5"))
    e.append(text(x=272, y=170, text="Assistant — session #142", font_size=14))

    # Message 1 (user)
    e.append(rect(x=276, y=210, w=560, h=60, bg="#e7f5ff"))
    e.append(text(x=288, y=220,
                  text="You\nSummarize known sources on grid resilience in Region X\nand flag any contradictions.",
                  font_size=13))

    # Message 2 (assistant) — wiki-first answer
    e.append(rect(x=276, y=282, w=560, h=140, bg="#f8f9fa"))
    e.append(text(x=288, y=292,
                  text=("Assistant\n"
                        "From the wiki page [[Region X / Infrastructure]]:\n"
                        "Three studies report transformer capacity at 1.4 GW [c12],\n"
                        "1.6 GW [c14], and 1.5 GW [c17] respectively.\n"
                        "The [[Region X / Infrastructure]] page notes Finding F12 — a\n"
                        "conflict between [[Source:S3]] and [[Source:S7]]. Open\n"
                        "the wiki tab or click any [[wikilink]] to navigate."),
                  font_size=13))
    e.append(text(x=288, y=412,
                  text="◀ trace · page-selection (1 page + 1-hop) · cost $0.01 · 0.8s",
                  font_size=11, color="#868e96"))

    # ApprovalCard
    e.append(rect(x=276, y=440, w=560, h=140, bg="#fff9db", stroke="#fab005"))
    e.append(text(x=288, y=450,
                  text=("ApprovalCard  (A2UI)\n"
                        "EditorialReviewer proposes wiki patch: rewrite\n"
                        "\"transformer capacity\" para on [[Region X / Infrastructure]]\n"
                        "to reflect the S3↔S7 conflict.\n"
                        "Severity: medium    Evidence: F12, [[Source:S3]], [[Source:S7]]"),
                  font_size=13))
    e.append(rect(x=288, y=540, w=120, h=30, bg="#ffe066", stroke="#fab005"))
    e.append(text(x=316, y=548, text="Approve", font_size=13))
    e.append(rect(x=418, y=540, w=120, h=30, bg="#f8f9fa"))
    e.append(text(x=446, y=548, text="Reject (reason…)", font_size=12))
    e.append(rect(x=548, y=540, w=120, h=30, bg="#f8f9fa"))
    e.append(text(x=566, y=548, text="View diff", font_size=13))

    # Input
    e.append(rect(x=276, y=600, w=560, h=44, bg="#f8f9fa"))
    e.append(text(x=288, y=613, text="Ask the assistant…", font_size=13, color="#868e96"))

    # Activity card
    e.append(rect(x=276, y=660, w=560, h=200, bg="#f1f3f5"))
    e.append(text(x=288, y=670, text="Activity", font_size=15))
    e.append(text(x=288, y=698,
                  text=("▶ Wiki agent — compiling [[Region X / Infrastructure]] (running)\n"
                        "▶ MechanicalReviewer — 12/14 claims validated         (running)\n"
                        "● EditorialReviewer — 3 findings need approval        (blocked → Briefs)\n"
                        "○ AIQ DeepResearcher — last --synthesize 14m ago      (idle)\n"
                        "\n"
                        "Cost this session: $0.42    Tokens: 38.2k / 12.4k cached"),
                  font_size=12))

    # RIGHT PANEL — 5 tabs
    e.append(rect(x=860, y=164, w=460, h=716, bg="#ffffff"))
    tabs = [("Wiki", True), ("Artifacts", False), ("Notes", False), ("Hypotheses", False), ("Briefs (3)", False)]
    tx = 868
    for label, active in tabs:
        w = 88
        e.append(rect(x=tx, y=170, w=w, h=28, bg="#d0ebff" if active else "#f1f3f5"))
        e.append(text(x=tx + 6, y=176, text=label, font_size=12))
        tx += w + 2

    # WikiSurface preview
    e.append(rect(x=868, y=210, w=444, h=660, bg="#ffffff"))

    # Surface header
    e.append(text(x=880, y=222, text="WikiSurface — A2UI", font_size=12, color="#868e96"))
    e.append(text(x=880, y=244, text="Region X › Infrastructure", font_size=18))
    e.append(text(x=880, y=270, text="Revision 14 · approved 2026-05-27 · hand-edit on intro §", font_size=11, color="#868e96"))

    # Graph mini view
    e.append(rect(x=880, y=294, w=420, h=140, bg="#f8f9fa"))
    e.append(text(x=890, y=300, text="graph view  ▸", font_size=11, color="#495057"))
    # Tiny graph
    e.append(ellipse(x=1050, y=320, w=40, h=40, bg="#d0ebff"))
    e.append(text(x=1056, y=332, text="Inf", font_size=11))
    e.append(ellipse(x=950, y=380, w=40, h=40, bg="#d3f9d8"))
    e.append(text(x=952, y=392, text="[[S3]]", font_size=10))
    e.append(ellipse(x=1150, y=380, w=40, h=40, bg="#d3f9d8"))
    e.append(text(x=1152, y=392, text="[[S7]]", font_size=10))
    e.append(ellipse(x=1050, y=380, w=40, h=40, bg="#fff4e6"))
    e.append(text(x=1058, y=392, text="F12", font_size=11))
    # graph edges
    e.append(arrow(x=1070, y=360, dx=-90, dy=20, stroke="#adb5bd", stroke_width=1, end_arrow=False))
    e.append(arrow(x=1070, y=360, dx=90, dy=20, stroke="#adb5bd", stroke_width=1, end_arrow=False))
    e.append(arrow(x=1070, y=360, dx=0, dy=20, stroke="#adb5bd", stroke_width=1, end_arrow=False))

    # Page body
    e.append(text(x=880, y=444,
                  text=("Transformer capacity in the regional grid\n"
                        "has been characterised in three independent\n"
                        "studies. [c12] reports 1.4 GW peak supply,\n"
                        "[c14] cites 1.6 GW, and [c17] derives 1.5 GW\n"
                        "from a different model.\n"
                        "\n"
                        "Cross-references: [[Region X / Power Demand]],\n"
                        "[[Transformer]], [[Source:S3]], [[Source:S7]].\n"
                        "\n"
                        "▲ Finding F12 — citation conflict between\n"
                        "[[Source:S3]] and [[Source:S7]]. See Briefs."),
                  font_size=13))

    # Inline ChartCard on the page
    e.append(rect(x=880, y=708, w=420, h=130, bg="#fff4e6", stroke="#fd7e14"))
    e.append(text(x=892, y=716, text="ChartCard (inline on wiki page)", font_size=11, color="#7c4900"))
    e.append(text(x=892, y=736, text="DatasetVersion: \"capacity-3-sources@v4\"", font_size=11, color="#495057"))
    # tiny bars
    e.append(rect(x=900, y=760, w=20, h=50, bg="#ffd8a8"))
    e.append(rect(x=930, y=750, w=20, h=60, bg="#ffd8a8"))
    e.append(rect(x=960, y=755, w=20, h=55, bg="#ffd8a8"))
    e.append(text(x=900, y=816, text="S3", font_size=10))
    e.append(text(x=930, y=816, text="S7", font_size=10))
    e.append(text(x=960, y=816, text="S17", font_size=10))

    # Footer note
    e.append(text(x=40, y=900,
                  text=("• Left = projects + sessions + bottom icons (gear/logs/notifications/profile).\n"
                        "• Right = 5 A2UI surfaces (Wiki shown). Sources via [[Source:X]] wiki pages. Datasets render as embedded cards.\n"
                        "• Reviews + assistant-proposed cards land in Briefs (badge count = unhandled)."),
                  font_size=12, color="#5f6368"))
    return e


def main() -> None:
    write_excalidraw(OUT_DIR / "2026-05-26-aleph-architecture.excalidraw", build_architecture())
    write_excalidraw(OUT_DIR / "2026-05-26-aleph-domain.excalidraw", build_domain())
    write_excalidraw(OUT_DIR / "2026-05-26-aleph-ui.excalidraw", build_ui())
    print("wrote 3 .excalidraw files into", OUT_DIR)


if __name__ == "__main__":
    main()
