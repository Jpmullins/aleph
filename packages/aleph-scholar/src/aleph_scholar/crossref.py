"""Crossref works API client (DOI lookup, bibliographic search, retraction).

Crossref corroborates OpenAlex: a DOI is only declared fabricated when both
upstreams 404. Retraction corroboration reads the `update-to` /
`updated-by` relations for entries of type `retraction`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from aleph_scholar._json import as_dict, as_list
from aleph_scholar.http import ScholarHttp, ensure_ok
from aleph_scholar.types import WorkRef

_API = "https://api.crossref.org"
_RETRACTION_TYPES = {"retraction", "retracted", "withdrawal", "removal"}


@dataclass(frozen=True)
class CrossrefWork:
    """A parsed Crossref work: the shareable ref + retraction corroboration."""

    ref: WorkRef
    retracted: bool


def _first(values: object) -> str | None:
    items = as_list(values)
    return str(items[0]) if items else None


def _year(message: dict[str, Any]) -> int | None:
    for key in ("issued", "published-print", "published-online", "created"):
        date_parts = as_list(as_dict(message.get(key)).get("date-parts"))
        first = as_list(date_parts[0]) if date_parts else []
        if first and first[0] is not None:
            return int(first[0])
    return None


def _authors(message: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for raw_author in as_list(message.get("author")):
        author = as_dict(raw_author)
        family = author.get("family")
        given = author.get("given")
        name = author.get("name")
        if family and given:
            out.append(f"{given} {family}")
        elif family or given or name:
            out.append(str(family or given or name))
    return out


def _is_retracted(message: dict[str, Any]) -> bool:
    """Detect `update-to`/`updated-by` relations of type retraction."""
    for key in ("update-to", "updated-by"):
        for update in as_list(message.get(key)):
            if str(as_dict(update).get("type", "")).lower() in _RETRACTION_TYPES:
                return True
    return False


def parse_work(message: dict[str, Any]) -> CrossrefWork:
    """Parse one Crossref `message` (work) object into a `CrossrefWork`."""
    doi = str(message.get("DOI") or "").lower() or None
    ref = WorkRef(
        doi=doi,
        openalex_id=None,
        title=_first(message.get("title")) or "",
        year=_year(message),
        venue=_first(message.get("container-title")),
        authors=_authors(message),
        cited_by_count=message.get("is-referenced-by-count"),
    )
    return CrossrefWork(ref=ref, retracted=_is_retracted(message))


class CrossrefClient:
    """Thin typed client over the Crossref `/works` endpoints."""

    def __init__(self, http: ScholarHttp) -> None:
        self._http = http

    async def get_work(self, doi: str) -> CrossrefWork | None:
        """Look up one DOI. Returns None on an authoritative 404."""
        response = await self._http.get(f"{_API}/works/{doi}")
        if response.status_code == 404:
            return None
        ensure_ok(response)
        body: dict[str, Any] = response.json()
        return parse_work(as_dict(body.get("message")))

    async def search_bibliographic(
        self, query: str, *, rows: int = 10, deadline_s: float | None = None
    ) -> list[WorkRef]:
        """Bibliographic search (`query.bibliographic=`).

        `deadline_s` is the caller's wall-clock budget for the whole attempt
        sequence — see `ScholarHttp.get`.
        """
        response = await self._http.get(
            f"{_API}/works",
            params={"query.bibliographic": query, "rows": str(rows)},
            deadline_s=deadline_s,
        )
        ensure_ok(response)
        body: dict[str, Any] = response.json()
        items = as_list(as_dict(body.get("message")).get("items"))
        return [parse_work(as_dict(item)).ref for item in items]
