"""The `reviewer` subagent — runs the editorial reviewer on the wiki (Wave 3 T6).

Delegating "review/critique this page" to this subagent keeps the review
dispatch in an isolated context, so the orchestrator's thread just sees one
short status line back. One tool, `review_wiki_page`, which self-calls the
tested `POST /reviews/editorial` route (Bearer local-dev, the same self-call
pattern as `wiki_builder`'s `promote_note` — rule #3: it never touches the
worker/DB directly; the route mints the agent token and enqueues
`editorial_review_job`, which owns the ReviewRun + findings). The editorial
reviewer is project-scoped (it scans the project's pages for contradictions,
weak/missing sources, and coverage gaps), so the named page is carried as the
`trigger` label recorded on the ReviewRun. Its LLM calls use the
cost-attributed subagent model (`subagent_model`, rule #5 — they write
`ModelCall` + `CostLedgerEvent` tagged `assistant.subagent.reviewer`).
"""

from __future__ import annotations

from typing import Any

from langchain_core.runnables import (
    RunnableConfig,
)
from langchain_core.tools import tool


def build_reviewer_subagent(*, settings: Any) -> dict[str, Any]:
    """Build the reviewer subagent dict (a deepagents `SubAgent`).

    The imports from `aleph_api.copilot_agent` are function-local to avoid a
    circular import (copilot_agent does not import this module at top level; the
    orchestrator builder calls this function at startup).
    """
    from aleph_api.copilot_agent import (
        _project_id_from_config,  # pyright: ignore[reportPrivateUsage] — shared scope resolver reused (DRY); module-private to the api
        subagent_model,
    )

    @tool
    async def review_wiki_page(page_title: str, config: RunnableConfig) -> str:
        """Start the editorial reviewer over the project wiki, anchored on a page.

        Runs contradiction / weak-source / coverage-gap checks across the
        project's pages; the named `page_title` is recorded as the trigger so the
        review is anchored on what the analyst asked about. Runs in the
        background — findings appear as approval requests / findings in the
        Reviews flow. Don't wait.
        """
        import httpx

        from aleph_api.copilot_agent import (
            _runtime,  # pyright: ignore[reportPrivateUsage] — shared runtime accessor (DRY); module-private to the api
        )

        settings_rt = _runtime.get("settings")
        project_id = _project_id_from_config(config)
        if settings_rt is None or project_id is None:
            return "Cannot start a review (no project scope on this run)."
        base = settings_rt.aleph_self_url
        trigger = f"assistant: review [[{page_title}]]" if page_title else "assistant"
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    f"{base}/v1/projects/{project_id}/reviews/editorial",
                    headers={"Authorization": "Bearer local-dev"},
                    json={"trigger": trigger},
                )
        except Exception as exc:
            return f"Could not start the editorial review: {exc}"
        if resp.status_code >= 400:
            return f"Could not start the editorial review ({resp.status_code}): {resp.text[:200]}"
        return (
            f"Editorial review started for [[{page_title}]]. Findings will appear "
            "as approval requests / findings in the Reviews flow."
        )

    return {
        "name": "reviewer",
        "description": (
            "Runs the editorial reviewer on a wiki page — contradiction/weak-source/"
            "coverage-gap checks. Delegate when the analyst asks to review/critique a page."
        ),
        "system_prompt": (
            "You are Aleph's reviewer. Start an editorial review for the named page, "
            "then return ONE short line that the review is running and findings will "
            "appear as approval requests/findings. Don't wait."
        ),
        "tools": [review_wiki_page],
        "model": subagent_model(settings, "reviewer"),
    }
