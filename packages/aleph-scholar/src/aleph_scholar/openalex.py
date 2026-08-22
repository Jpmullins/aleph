"""OpenAlex works API client (search, batched DOI/ID resolution, citations).

OpenAlex is the primary upstream for DOI verification (`is_retracted` is
Retraction-Watch-backed) and the only one used for citation-graph
expansion. Requests carry `mailto=` for the polite pool when the configured
address is contactable — see `ScholarHttp.mailto_params`. Batch
endpoints are preferred: `filter=doi:a|b|c` resolves up to 50 DOIs per
request, `filter=openalex_id:W1|W2|...` resolves W-ids.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aleph_scholar._json import as_dict, as_list
from aleph_scholar.http import ScholarHttp, ensure_ok
from aleph_scholar.types import WorkRef

_API = "https://api.openalex.org"
OPENALEX_DOI_BATCH_SIZE = 50
_MAX_PER_PAGE = 200
#: Ceiling on how many referenced-work ids one backward walk will resolve.
#: Applied to the id list before any request, so cost stays bounded (4 batches)
#: no matter how long the bibliography is.
_MAX_EXPANSION_IDS = 200


def _by_influence(refs: list[WorkRef]) -> list[WorkRef]:
    """Most-cited first; unknown counts sort last, ties keep source order."""
    return sorted(refs, key=lambda r: (r.cited_by_count is None, -(r.cited_by_count or 0)))


@dataclass(frozen=True)
class OpenAlexWork:
    """A parsed OpenAlex work: the shareable ref + verdict-relevant flags."""

    ref: WorkRef
    is_retracted: bool


def _short_id(value: str | None) -> str | None:
    """`https://openalex.org/W123` → `W123` (already-short ids pass through)."""
    if not value:
        return None
    return value.rsplit("/", 1)[-1]


def _bare_doi(value: str | None) -> str | None:
    """`https://doi.org/10.1/x` → `10.1/x`, lowercased."""
    if not value:
        return None
    doi = value.strip().lower()
    for prefix in ("https://doi.org/", "http://doi.org/", "http://dx.doi.org/"):
        doi = doi.removeprefix(prefix)
    return doi or None


def parse_work(raw: dict[str, Any]) -> OpenAlexWork:
    """Parse one OpenAlex work JSON object into an `OpenAlexWork`."""
    authors: list[str] = []
    for authorship in as_list(raw.get("authorships")):
        name = as_dict(as_dict(authorship).get("author")).get("display_name")
        if name:
            authors.append(str(name))
    primary = as_dict(raw.get("primary_location"))
    source = as_dict(primary.get("source"))
    venue = source.get("display_name")
    best_oa = as_dict(raw.get("best_oa_location"))
    pdf_url = best_oa.get("pdf_url") or primary.get("pdf_url")
    landing_url = best_oa.get("landing_page_url") or primary.get("landing_page_url")
    ref = WorkRef(
        doi=_bare_doi(raw.get("doi")),
        openalex_id=_short_id(raw.get("id")),
        title=str(raw.get("display_name") or raw.get("title") or ""),
        year=raw.get("publication_year"),
        venue=str(venue) if venue else None,
        authors=authors,
        cited_by_count=raw.get("cited_by_count"),
        pdf_url=str(pdf_url) if pdf_url else None,
        landing_url=str(landing_url) if landing_url else None,
    )
    return OpenAlexWork(ref=ref, is_retracted=bool(raw.get("is_retracted", False)))


class OpenAlexClient:
    """Thin typed client over the OpenAlex `/works` endpoints."""

    def __init__(self, http: ScholarHttp) -> None:
        self._http = http

    async def _list_works(
        self, params: dict[str, str], *, deadline_s: float | None = None
    ) -> list[OpenAlexWork]:
        params = {**params, **self._http.mailto_params()}
        response = await self._http.get(f"{_API}/works", params=params, deadline_s=deadline_s)
        ensure_ok(response)
        body: dict[str, Any] = response.json()
        return [parse_work(as_dict(raw)) for raw in as_list(body.get("results"))]

    async def search(
        self, query: str, *, per_page: int = 10, deadline_s: float | None = None
    ) -> list[WorkRef]:
        """Full-text relevance search over works.

        `deadline_s` is the caller's wall-clock budget for the whole attempt
        sequence — see `ScholarHttp.get`. Discovery search is the one call site
        where a caller genuinely knows how long an answer is still worth
        waiting for, so it is plumbed through rather than fixed here.
        """
        works = await self._list_works(
            {"search": query, "per-page": str(min(per_page, _MAX_PER_PAGE))},
            deadline_s=deadline_s,
        )
        return [work.ref for work in works]

    async def works_by_dois(self, dois: list[str]) -> dict[str, OpenAlexWork]:
        """Batch-resolve up to 50 DOIs in one request, keyed by bare lowercase DOI."""
        if not dois:
            return {}
        if len(dois) > OPENALEX_DOI_BATCH_SIZE:
            msg = f"works_by_dois takes at most {OPENALEX_DOI_BATCH_SIZE} DOIs, got {len(dois)}"
            raise ValueError(msg)
        works = await self._list_works(
            {"filter": "doi:" + "|".join(dois), "per-page": str(OPENALEX_DOI_BATCH_SIZE)}
        )
        return {work.ref.doi: work for work in works if work.ref.doi}

    async def works_by_ids(self, openalex_ids: list[str]) -> list[OpenAlexWork]:
        """Batch-resolve OpenAlex W-ids (short or URL form)."""
        short_ids = [sid for sid in (_short_id(i) for i in openalex_ids) if sid]
        if not short_ids:
            return []
        if len(short_ids) > OPENALEX_DOI_BATCH_SIZE:
            msg = f"works_by_ids takes at most {OPENALEX_DOI_BATCH_SIZE} ids, got {len(short_ids)}"
            raise ValueError(msg)
        return await self._list_works(
            {
                "filter": "openalex_id:" + "|".join(short_ids),
                "per-page": str(OPENALEX_DOI_BATCH_SIZE),
            }
        )

    async def get_work(self, ref: str) -> OpenAlexWork | None:
        """Fetch one work by OpenAlex id or DOI. Returns None on 404."""
        raw = await self._raw_work(ref)
        return parse_work(raw) if raw is not None else None

    async def referenced_works(self, ref: str, *, limit: int = 25) -> list[WorkRef]:
        """Backward citations: the works `ref` cites, most-cited first.

        `referenced_works` comes back in OpenAlex's own storage order, which
        carries no meaning. Slicing it to `limit` before resolving therefore
        returned an arbitrary subset of a paper's bibliography — for a paper
        with 80 references, an arbitrary 25 — which is precisely the opposite
        of a backward citation walk, whose whole purpose is to surface the
        foundational work a field is built on.

        So: resolve up to `_MAX_EXPANSION_IDS` references, rank the resolved
        works by citation count, and return the top `limit`. The cap is a real
        limit and is applied to the id list before any network call, so a paper
        with a very long bibliography costs a bounded number of requests.
        """
        work_raw = await self._raw_work(ref)
        if work_raw is None:
            return []
        w_ids = [str(w) for w in as_list(work_raw.get("referenced_works"))[:_MAX_EXPANSION_IDS]]
        if not w_ids:
            return []
        out: list[WorkRef] = []
        for start in range(0, len(w_ids), OPENALEX_DOI_BATCH_SIZE):
            batch = w_ids[start : start + OPENALEX_DOI_BATCH_SIZE]
            out.extend(work.ref for work in await self.works_by_ids(batch))
        return _by_influence(out)[:limit]

    async def citing_works(self, ref: str, *, limit: int = 25) -> list[WorkRef]:
        """Forward citations: works citing `ref` (`filter=cites:W...`), most-cited first.

        Without an explicit `sort` OpenAlex returns its default ordering, so a
        forward walk over a well-cited paper surfaced an arbitrary slice of its
        citing literature rather than the work that actually took it up.
        """
        openalex_id = await self._resolve_id(ref)
        if openalex_id is None:
            return []
        works = await self._list_works(
            {
                "filter": f"cites:{openalex_id}",
                "sort": "cited_by_count:desc",
                "per-page": str(min(limit, _MAX_PER_PAGE)),
            }
        )
        return [work.ref for work in works[:limit]]

    async def _raw_work(self, ref: str) -> dict[str, Any] | None:
        ident = f"doi:{ref}" if ref.startswith("10.") else (_short_id(ref) or ref)
        response = await self._http.get(f"{_API}/works/{ident}", params=self._http.mailto_params())
        if response.status_code == 404:
            return None
        ensure_ok(response)
        body: dict[str, Any] = response.json()
        return body

    async def _resolve_id(self, ref: str) -> str | None:
        if not ref.startswith("10."):
            return _short_id(ref)
        work = await self.get_work(ref)
        return work.ref.openalex_id if work else None
