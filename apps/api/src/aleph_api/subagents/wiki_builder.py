"""The `wiki_builder` subagent — ingests sources + promotes notes (Wave 3 T4).

Delegating "add a source" / "build wiki content" to this subagent keeps the
ingestion + promotion chatter (and any compile logs) in an isolated context, so
the orchestrator's thread just sees one short status line back. The subagent
reuses the exact ingest impl (`_ingest_source_impl`, rule #3 — self-calls the
tested `/sources/ingest-url` route, never raw DB) and self-calls the tested
note-promote route the same way (`POST /notes/{id}/promote`). Its LLM calls use
the cost-attributed subagent model (`subagent_model`, rule #5 — they write
`ModelCall` + `CostLedgerEvent` tagged `assistant.subagent.wiki_builder`).
"""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import (
    RunnableConfig,
)
from langchain_core.tools import tool


def build_wiki_builder_subagent(*, settings: Any) -> dict[str, Any]:
    """Build the wiki_builder subagent dict (a deepagents `SubAgent`).

    The imports from `aleph_api.copilot_agent` are function-local to avoid a
    circular import (copilot_agent does not import this module at top level; the
    orchestrator builder calls this function at startup).
    """
    from aleph_api.agent_middleware import AlephAgentMiddleware
    from aleph_api.copilot_agent import (
        _ingest_source_impl,  # pyright: ignore[reportPrivateUsage] — shared ingest body deliberately reused (DRY); module-private to the api
        _project_id_from_config,  # pyright: ignore[reportPrivateUsage] — shared scope resolver reused (DRY); module-private to the api
        subagent_model,
    )

    @tool
    async def ingest_source(url: str, config: RunnableConfig, title: str = "") -> str:
        """Ingest a web page or document URL into the project's knowledge store.

        Fetches the URL, normalizes, chunks+embeds, folds into the wiki. Render a
        SourceCard for the result. Use when the analyst shares a link or asks to
        add a source.
        """
        return await _ingest_source_impl(url, config, title)

    @tool
    async def promote_note(note_id: str, config: RunnableConfig) -> str:
        """Promote an analyst note to a draft wiki page + pending Briefs proposal.

        `note_id` is the note's UUID. Concatenates the note's sections into a
        draft wiki page and lands a pending proposal in the Briefs tab for
        approval. Use when the analyst asks to promote/publish a note to the wiki.
        """
        from uuid import UUID

        import httpx

        from aleph_api.copilot_agent import (
            _runtime,  # pyright: ignore[reportPrivateUsage] — shared runtime accessor (DRY); module-private to the api
            _self_headers,  # pyright: ignore[reportPrivateUsage] — shared self-call token minter (DRY); module-private to the api
        )

        settings_rt = _runtime.get("settings")
        project_id = await _project_id_from_config(config)
        if settings_rt is None or project_id is None:
            return "Cannot promote (no project scope)."
        try:
            note_uuid = UUID(note_id)
        except ValueError:
            return f"note_id must be a valid UUID (got '{note_id}')."
        base = settings_rt.aleph_self_url
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    f"{base}/v1/projects/{project_id}/notes/{note_uuid}/promote",
                    headers=await _self_headers(project_id, settings=settings_rt),
                )
        except Exception as exc:
            return f"Could not promote note {note_id}: {exc}"
        if resp.status_code >= 400:
            return f"Could not promote note {note_id} ({resp.status_code}): {resp.text[:200]}"
        b = resp.json()
        return (
            f"Promoted note {note_id} to a draft wiki page (page {b['page_id']}). "
            f"A pending proposal (proposal {b['proposal_id']}) is waiting in the Briefs "
            "tab for approval."
        )

    return {
        "name": "wiki_builder",
        "description": (
            "Ingests a source URL into the knowledge store and folds it into the wiki; "
            "can promote a note to a draft wiki page. Delegate when the analyst wants to "
            "add/build/revise wiki content."
        ),
        "system_prompt": (
            "You are Aleph's wiki builder. Ingest the source URL or promote the note as "
            "requested, then return ONE short line with the resulting source/page short_id "
            "+ status. Don't dump compile logs."
        ),
        "tools": [ingest_source, promote_note],
        # The same tool guard the orchestrator carries. deepagents lets a
        # subagent spec override the parent's middleware rather than extend it,
        # so "the orchestrator has it" is not "the subagents have it" —
        # scripts/check-agent-middleware.sh asserts all six do.
        "middleware": [AlephAgentMiddleware()],
        "model": subagent_model(settings, "wiki_builder"),
    }
