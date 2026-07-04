"""The `researcher` subagent — native research dispatch + verified scholarship (WP-2).

Delegating "research X" to this subagent keeps the dispatch logic (and the
fire-and-return chatter) in an isolated context, so the orchestrator's thread
just sees one short line back. The subagent reuses the exact dispatch impl
(`_start_research_impl`, rule #3 — self-calls the tested `/synthesize` route,
never raw DB) and the cost-attributed subagent model (`subagent_model`, rule #5
— its LLM calls write `ModelCall` + `CostLedgerEvent` tagged
`assistant.subagent.researcher`).

WP-2 adds the scholar tool suite, each self-calling the typed
`/scholar/*` + `/sources/ingest-url` routes the same way (rule #3):
`verify_dois`, `search_openalex`, `search_consensus`, `expand_citations`,
and `ingest_paper` (which refuses to ingest a DOI that fails verification —
fabricated citations never become Sources).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from langchain_core.runnables import (
    RunnableConfig,
)
from langchain_core.tools import tool

_SELF_AUTH_HEADERS = {"Authorization": "Bearer local-dev"}


def _format_work(w: dict[str, Any]) -> str:
    authors_raw: list[Any] = w.get("authors") or []
    authors = ", ".join(str(a) for a in authors_raw[:3]) or "unknown authors"
    year = w.get("year") or "n.d."
    doi = f" doi:{w['doi']}" if w.get("doi") else ""
    oa = f" openalex:{w['openalex_id']}" if w.get("openalex_id") else ""
    cited = w.get("cited_by_count")
    cited_s = f" (cited {cited}x)" if isinstance(cited, int) else ""
    oa_pdf = f"\n  open-access pdf: {w['pdf_url']}" if w.get("pdf_url") else ""
    return f"- {w.get('title', '?')} — {authors} ({year}){doi}{oa}{cited_s}{oa_pdf}"


def _format_verdict(v: dict[str, Any]) -> str:
    if v.get("ok") is True:
        state = "VERIFIED"
        if v.get("retracted") is True:
            state = "VERIFIED but RETRACTED — do not cite as valid evidence"
    elif v.get("ok") is False:
        state = "FABRICATED (authoritative 404 on both Crossref and OpenAlex) — do not cite"
    else:
        state = "UNVERIFIABLE right now (network) — cite only with an 'unverified' caveat"
    title = f" '{v['title']}'" if v.get("title") else ""
    return f"- {v.get('doi', '?')}: {state}{title} [checked via {v.get('checked_via', '?')}]"


def build_researcher_subagent(*, settings: Any) -> dict[str, Any]:
    """Build the researcher subagent dict (a deepagents `SubAgent`).

    The imports from `aleph_api.copilot_agent` are function-local to avoid a
    circular import (copilot_agent does not import this module at top level; the
    orchestrator builder calls this function at startup).
    """
    from aleph_api.copilot_agent import (
        _project_id_from_config,  # pyright: ignore[reportPrivateUsage] — shared scope resolver reused (DRY); module-private to the api
        _runtime,  # pyright: ignore[reportPrivateUsage] — shared runtime accessor (DRY); module-private to the api
        _start_research_impl,  # pyright: ignore[reportPrivateUsage] — shared dispatch body deliberately reused (DRY); module-private to the api
        subagent_model,
    )

    def _scope(config: RunnableConfig) -> tuple[str, UUID] | None:
        """Resolve (self base URL, project id) or None when unscoped."""
        settings_rt = _runtime.get("settings")
        project_id = _project_id_from_config(config)
        if settings_rt is None or project_id is None:
            return None
        return str(settings_rt.aleph_self_url), project_id

    async def _post_scholar(
        config: RunnableConfig, endpoint: str, payload: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Self-call a typed scholar/source route; returns (json, error-string)."""
        import httpx

        scope = _scope(config)
        if scope is None:
            return None, "Scholar tools are unavailable (no project scope on this run)."
        base, project_id = scope
        try:
            # Generous timeout: DOI verification fans out to rate-limited
            # upstreams (1 req/s polite pool) with retries.
            async with httpx.AsyncClient(timeout=120.0) as client:
                resp = await client.post(
                    f"{base}/v1/projects/{project_id}/{endpoint}",
                    json=payload,
                    headers=_SELF_AUTH_HEADERS,
                )
        except Exception as exc:
            return None, f"Could not reach {endpoint}: {exc}"
        if resp.status_code >= 400:
            return None, f"{endpoint} failed ({resp.status_code}): {resp.text[:300]}"
        data: dict[str, Any] = resp.json()
        return data, None

    @tool
    async def start_research(query: str, config: RunnableConfig, depth: str = "shallow") -> str:
        """Kick off background research on a topic; returns immediately.

        `depth` is 'shallow' (fast) or 'deep' (thorough).
        """
        return await _start_research_impl(query, config, depth)

    @tool
    async def verify_dois(dois: list[str], config: RunnableConfig) -> str:
        """Verify DOIs against Crossref + OpenAlex (tri-state, batched).

        Returns per-DOI verdicts: VERIFIED (resolves; includes retraction
        status), FABRICATED (authoritative 404 on both upstreams — never cite
        it), or UNVERIFIABLE (network trouble — do not flag, but caveat it).
        Verify EVERY DOI you are about to cite.
        """
        body, err = await _post_scholar(config, "scholar/verify-dois", {"dois": dois})
        if err or body is None:
            return err or "No response from verify-dois."
        verdicts: list[dict[str, Any]] = body.get("verdicts") or []
        if not verdicts:
            return "No verdicts returned."
        return "DOI verification:\n" + "\n".join(_format_verdict(v) for v in verdicts)

    @tool
    async def search_openalex(query: str, config: RunnableConfig, limit: int = 10) -> str:
        """Search OpenAlex for scholarly works (bulk discovery — unmetered).

        Use this for finding papers on a topic. Returns title, authors, year,
        DOI, OpenAlex id, citation count per hit.
        """
        body, err = await _post_scholar(
            config, "scholar/search", {"provider": "openalex", "query": query, "limit": limit}
        )
        if err or body is None:
            return err or "No response from scholar/search."
        works: list[dict[str, Any]] = body.get("works") or []
        if not works:
            return f"OpenAlex: no works found for '{query}'."
        return f"OpenAlex results for '{query}':\n" + "\n".join(_format_work(w) for w in works)

    @tool
    async def search_consensus(query: str, config: RunnableConfig) -> str:
        """Search Consensus for evidence on a screening/yes-no research question.

        QUOTA-METERED (monthly cap) — use it ONLY for evidence/screening
        questions ("does X improve Y?"), never for bulk discovery (use
        search_openalex for that). May report quota exhaustion or that the
        Consensus connection needs to be re-authorized.
        """
        import httpx

        scope = _scope(config)
        if scope is None:
            return "Scholar tools are unavailable (no project scope on this run)."
        base, project_id = scope
        try:
            async with httpx.AsyncClient(timeout=330.0) as client:
                resp = await client.post(
                    f"{base}/v1/projects/{project_id}/scholar/consensus-search",
                    json={"query": query},
                    headers=_SELF_AUTH_HEADERS,
                )
        except Exception as exc:
            return f"Could not reach Consensus: {exc}"
        if resp.status_code == 403:
            return (
                "Consensus is disabled for this project (connector binding off) — "
                "use search_openalex instead."
            )
        if resp.status_code == 409:
            return (
                "Consensus authorization needs to be re-established — tell the analyst to "
                "run scripts/connect-consensus.py. Use search_openalex meanwhile."
            )
        if resp.status_code >= 400:
            return f"Consensus search failed ({resp.status_code}): {resp.text[:300]}"
        body: dict[str, Any] = resp.json()
        if body.get("status") == "quota_exhausted":
            return str(
                body.get("message")
                or "Consensus monthly quota exhausted — use search_openalex instead."
            )
        hits: list[dict[str, Any]] = body.get("hits") or []
        if not hits:
            return f"Consensus: no evidence hits for '{query}'."
        lines: list[str] = []
        for h in hits:
            doi = f" doi:{h['doi']}" if h.get("doi") else ""
            snippet = f" — {str(h.get('snippet') or '')[:200]}" if h.get("snippet") else ""
            lines.append(f"- {h.get('title', '?')} ({h.get('url', '')}){doi}{snippet}")
        return f"Consensus evidence for '{query}':\n" + "\n".join(lines)

    @tool
    async def expand_citations(ref: str, config: RunnableConfig, direction: str = "both") -> str:
        """Walk the citation graph of a work (`ref` = DOI or OpenAlex id).

        `direction`: 'backward' (what it cites), 'forward' (what cites it),
        or 'both'. Use on a key paper to find foundations and follow-ups.
        """
        direction = direction if direction in ("backward", "forward", "both") else "both"
        body, err = await _post_scholar(
            config, "scholar/expand-citations", {"ref": ref, "direction": direction}
        )
        if err or body is None:
            return err or "No response from expand-citations."
        backward: list[dict[str, Any]] = body.get("backward") or []
        forward: list[dict[str, Any]] = body.get("forward") or []
        parts: list[str] = []
        if direction in ("backward", "both"):
            parts.append(
                f"Cites ({len(backward)}):\n" + "\n".join(_format_work(w) for w in backward)
                if backward
                else "Cites: none found."
            )
        if direction in ("forward", "both"):
            parts.append(
                f"Cited by ({len(forward)}):\n" + "\n".join(_format_work(w) for w in forward)
                if forward
                else "Cited by: none found."
            )
        return f"Citation graph for {ref}:\n" + "\n\n".join(parts)

    @tool
    async def ingest_paper(
        url: str,
        config: RunnableConfig,
        title: str = "",
        doi: str = "",
        openalex_id: str = "",
        connector_kind: str = "openalex",
    ) -> str:
        """Ingest a paper you RELY ON into the knowledge store, with provenance.

        Verifies the DOI first (verify-dois route) and REFUSES to ingest a
        fabricated DOI (404 on both Crossref and OpenAlex). An unverifiable
        DOI (network) is ingested with its verdict recorded as unverified.
        `connector_kind` is the real origin of the paper: 'openalex',
        'crossref', 'consensus', or 'arxiv'.

        Pass the OPEN-ACCESS pdf url from search results when one exists —
        publisher landing pages are usually paywalled and serve empty
        documents to non-browser fetchers, failing normalization.
        """
        verdict: dict[str, Any] | None = None
        if doi:
            body, err = await _post_scholar(config, "scholar/verify-dois", {"dois": [doi]})
            if err or body is None:
                return f"Cannot ingest before verifying the DOI: {err}"
            verdicts: list[dict[str, Any]] = body.get("verdicts") or []
            verdict = verdicts[0] if verdicts else None
            if verdict is not None and verdict.get("ok") is False:
                return (
                    f"REFUSING to ingest: DOI {doi} does not exist (authoritative 404 on both "
                    "Crossref and OpenAlex). This citation looks fabricated — do not present "
                    "it as real. Find the correct DOI via search_openalex and retry."
                )

        source_metadata: dict[str, Any] = {}
        if doi:
            source_metadata["doi"] = doi.strip().lower()
        if openalex_id:
            source_metadata["openalex_id"] = openalex_id
        if verdict is not None:
            source_metadata["doi_verdict"] = {
                "ok": verdict.get("ok"),
                "retracted": verdict.get("retracted"),
                "checked_via": verdict.get("checked_via"),
                "checked_at": datetime.now(UTC).isoformat(),
            }
            if not openalex_id and verdict.get("openalex_id"):
                source_metadata["openalex_id"] = verdict["openalex_id"]

        ingest_body, ingest_err = await _post_scholar(
            config,
            "sources/ingest-url",
            {
                "url": url,
                "title": title,
                "connector_kind": connector_kind,
                "source_metadata": source_metadata,
            },
        )
        if ingest_err or ingest_body is None:
            return f"Could not ingest {url}: {ingest_err}"
        b: dict[str, Any] = ingest_body
        caveat = ""
        if verdict is not None and verdict.get("ok") is None:
            caveat = " NOTE: the DOI could not be verified right now (recorded as unverified)."
        elif verdict is not None and verdict.get("retracted") is True:
            caveat = " WARNING: this paper is RETRACTED — treat its findings accordingly."
        return (
            f"Ingested paper {url} (source {b.get('source_id')}, status {b.get('status')}, "
            f"provenance {connector_kind}"
            + (f", doi {source_metadata.get('doi')}" if doi else "")
            + f").{caveat} Metadata recorded: {json.dumps(source_metadata)[:300]}"
        )

    return {
        "name": "researcher",
        "description": (
            "Research + verified scholarship. Kicks off background research on a topic (web "
            "search via the native research loop) that grows the wiki, and works the "
            "scholarly literature directly: "
            "OpenAlex discovery, Consensus evidence search, citation-graph expansion, DOI "
            "verification, and provenance-carrying paper ingestion. Delegate when the analyst "
            "asks to research/look into/synthesize a topic, or asks a literature question."
        ),
        "system_prompt": (
            "You are Aleph's research agent.\n"
            "For broad topic research: call start_research with a focused query (depth "
            "'shallow' unless the analyst asked for a thorough/deep dive), then return ONE "
            "short line naming the topic and stating results will appear in the Wiki/Briefs "
            "tabs. Do NOT wait for completion.\n"
            "For literature/scholarly questions, use the scholar tools:\n"
            "- search_openalex for discovery (unmetered); expand_citations to walk a key "
            "paper's references and citers.\n"
            "- search_consensus ONLY for screening/evidence questions ('does X affect Y?') — "
            "it is quota-metered per month; never use it for bulk discovery.\n"
            "- verify_dois on EVERY DOI you cite. Never present a fabricated or retracted "
            "DOI as valid evidence; flag unverifiable ones as unverified.\n"
            "- ingest_paper for each paper your answer actually relies on (it verifies the "
            "DOI, refuses fabricated ones, and records doi/openalex_id provenance).\n"
            "Keep your final reply short: findings + the papers you ingested."
        ),
        "tools": [
            start_research,
            verify_dois,
            search_openalex,
            search_consensus,
            expand_citations,
            ingest_paper,
        ],
        "model": subagent_model(settings, "researcher"),
    }
