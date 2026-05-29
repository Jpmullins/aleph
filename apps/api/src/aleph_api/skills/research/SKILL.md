---
name: research
description: How to grow the wiki by delegating to the researcher subagent — when to research vs. read the wiki, shallow vs. deep, how to frame a focused query, and how findings become wiki pages plus a Briefs approval proposal.
---

# Research methodology

Use this when the wiki does not yet cover a question and the analyst wants it
investigated.

## When to research (vs. read the wiki)

- First check coverage with `search_wiki`. If relevant pages exist, delegate to
  the `retriever` subagent instead — do not research what the wiki already knows.
- Research only when `search_wiki` returns nothing relevant, OR the analyst
  explicitly asks to research / look into / synthesize a topic.
- Confirm intent before kicking off a deep run (it is slow and consequential).

## Shallow vs. deep

- `depth="shallow"` — fast single pass (~1 min). Default. Use for a focused
  fact, a quick scan of a narrow topic, or to test whether a topic is worth a
  deep run.
- `depth="deep"` — thorough multi-loop (several minutes). Use only when the
  analyst wants comprehensive coverage of a broad topic and accepts the wait.

## How to frame a focused query

- One topic per research run. Split multi-part asks into separate runs.
- Be specific and scoped: name the entity, the time window, and the angle
  (e.g. "2024 EU AI Act enforcement timeline" not "AI regulation").
- State the question, not a command — the researcher turns it into searches.

## What happens after you delegate

1. Delegate to the `researcher` subagent via the `task` tool with the framed
   query and depth.
2. It runs in the background; you return immediately.
3. When it finishes it lands a **draft wiki page** plus an **approval proposal
   in the Briefs tab** — research is never published directly.
4. Tell the analyst what you kicked off and that the proposal will appear in
   Briefs for them to review and approve.
