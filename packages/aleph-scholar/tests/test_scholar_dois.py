"""extract_dois edge cases + the four verify_dois tri-state outcomes + batching."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from aleph_scholar.crossref import CrossrefClient
from aleph_scholar.dois import extract_dois, normalize_doi, verify_dois
from aleph_scholar.http import ScholarHttp
from aleph_scholar.openalex import OpenAlexClient

RETRACTED_DOI = "10.1016/s0140-6736(97)11096-0"

MakeHttp = Callable[[Callable[[httpx.Request], httpx.Response]], ScholarHttp]


# ---------------------------------------------------------------- extract_dois


def test_extract_plain_doi() -> None:
    assert extract_dois("see 10.1234/abc-DEF for details") == ["10.1234/abc-def"]


def test_extract_strips_doi_org_urls() -> None:
    text = "available at https://doi.org/10.5555/12345678 and http://dx.doi.org/10.5555/Xyz"
    assert extract_dois(text) == ["10.5555/12345678", "10.5555/xyz"]


def test_extract_trims_trailing_punctuation() -> None:
    assert extract_dois("cited (10.1234/abc). Also 10.1234/def; and 10.1234/ghi,") == [
        "10.1234/abc",
        "10.1234/def",
        "10.1234/ghi",
    ]


def test_extract_keeps_balanced_parentheses() -> None:
    # The canonical retracted Lancet DOI contains balanced parens — it must
    # survive extraction whole (the spec's regex would truncate at the paren,
    # so trimming is balance-aware instead).
    assert extract_dois(f"the retracted paper ({RETRACTED_DOI}).") == [RETRACTED_DOI]


def test_extract_dedupes_preserving_order() -> None:
    text = "10.1234/b then 10.1234/a then https://doi.org/10.1234/B again"
    assert extract_dois(text) == ["10.1234/b", "10.1234/a"]


def test_extract_no_match() -> None:
    assert extract_dois("no identifiers here, 10.12/  nope") == []


def test_normalize_doi() -> None:
    assert normalize_doi("https://doi.org/10.1234/AbC") == "10.1234/abc"
    assert normalize_doi("doi:10.1234/abc") == "10.1234/abc"


# ---------------------------------------------------------------- verify_dois


def _openalex_work(doi: str, *, retracted: bool = False, n: int = 1) -> dict[str, object]:
    return {
        "id": f"https://openalex.org/W{n}",
        "doi": f"https://doi.org/{doi}",
        "display_name": f"Work {n}",
        "publication_year": 2020,
        "is_retracted": retracted,
        "cited_by_count": 42,
        "primary_location": {"source": {"display_name": "Journal of Tests"}},
        "authorships": [{"author": {"display_name": "Ada Lovelace"}}],
    }


def _verify_handler(
    openalex_works: list[dict[str, object]],
    crossref_by_doi: dict[str, dict[str, object]],
    requests: list[httpx.Request],
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.host == "api.openalex.org":
            return httpx.Response(200, json={"results": openalex_works})
        if request.url.host == "api.crossref.org":
            doi = request.url.path.removeprefix("/works/")
            message = crossref_by_doi.get(doi)
            if message is None:
                return httpx.Response(404, text="Resource not found.")
            return httpx.Response(200, json={"status": "ok", "message": message})
        raise AssertionError(f"unexpected host {request.url.host}")

    return handler


async def test_verify_good_doi(make_http: MakeHttp) -> None:
    requests: list[httpx.Request] = []
    http = make_http(_verify_handler([_openalex_work("10.1234/good")], {}, requests))
    verdicts = await verify_dois(
        ["10.1234/GOOD"], openalex=OpenAlexClient(http), crossref=CrossrefClient(http)
    )
    assert len(verdicts) == 1
    v = verdicts[0]
    assert v.doi == "10.1234/good"
    assert v.ok is True
    assert v.retracted is False
    assert v.title == "Work 1"
    assert v.year == 2020
    assert v.openalex_id == "W1"
    assert v.checked_via == "openalex"


async def test_verify_fabricated_doi_requires_404_on_both(make_http: MakeHttp) -> None:
    requests: list[httpx.Request] = []
    http = make_http(_verify_handler([], {}, requests))  # OpenAlex empty, Crossref 404
    verdicts = await verify_dois(
        ["10.9999/fabricated"], openalex=OpenAlexClient(http), crossref=CrossrefClient(http)
    )
    v = verdicts[0]
    assert v.ok is False
    assert v.retracted is None
    assert v.checked_via == "crossref+openalex"


async def test_verify_retracted_doi_via_openalex(make_http: MakeHttp) -> None:
    requests: list[httpx.Request] = []
    http = make_http(_verify_handler([_openalex_work(RETRACTED_DOI, retracted=True)], {}, requests))
    verdicts = await verify_dois(
        [RETRACTED_DOI], openalex=OpenAlexClient(http), crossref=CrossrefClient(http)
    )
    v = verdicts[0]
    assert v.ok is True
    assert v.retracted is True


async def test_verify_retraction_corroborated_by_crossref(make_http: MakeHttp) -> None:
    # OpenAlex miss; Crossref resolves it and carries an update-to retraction.
    message: dict[str, object] = {
        "DOI": RETRACTED_DOI,
        "title": ["Ileal-lymphoid-nodular hyperplasia..."],
        "issued": {"date-parts": [[1998, 2, 28]]},
        "container-title": ["The Lancet"],
        "update-to": [{"type": "retraction", "DOI": RETRACTED_DOI}],
    }
    requests: list[httpx.Request] = []
    http = make_http(_verify_handler([], {RETRACTED_DOI: message}, requests))
    verdicts = await verify_dois(
        [RETRACTED_DOI], openalex=OpenAlexClient(http), crossref=CrossrefClient(http)
    )
    v = verdicts[0]
    assert v.ok is True
    assert v.retracted is True
    assert v.checked_via == "crossref+openalex"
    assert v.year == 1998


async def test_verify_timeout_is_unverifiable_not_missing(make_http: MakeHttp) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("simulated timeout", request=request)

    http = make_http(handler)
    verdicts = await verify_dois(
        ["10.1234/unreachable"], openalex=OpenAlexClient(http), crossref=CrossrefClient(http)
    )
    v = verdicts[0]
    assert v.ok is None
    assert v.retracted is None


async def test_verify_batch_of_50_uses_at_most_two_requests(make_http: MakeHttp) -> None:
    dois = [f"10.1234/x{i}" for i in range(50)]
    works = [_openalex_work(doi, n=i) for i, doi in enumerate(dois)]
    requests: list[httpx.Request] = []
    http = make_http(_verify_handler(works, {}, requests))
    verdicts = await verify_dois(dois, openalex=OpenAlexClient(http), crossref=CrossrefClient(http))
    assert len(verdicts) == 50
    assert all(v.ok is True for v in verdicts)
    assert len(requests) <= 2
    assert len(requests) == 1  # all resolved in one OpenAlex batch


async def test_verify_crossref_404_with_openalex_down_is_unverifiable(
    make_http: MakeHttp,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.openalex.org":
            raise httpx.ConnectError("openalex down", request=request)
        return httpx.Response(404, text="Resource not found.")

    http = make_http(handler)
    verdicts = await verify_dois(
        ["10.1234/limbo"], openalex=OpenAlexClient(http), crossref=CrossrefClient(http)
    )
    assert verdicts[0].ok is None  # one upstream never answered — not authoritative


async def test_verify_empty_input() -> None:
    http = ScholarHttp(
        mailto="test@aleph.local",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: pytest.fail("no request expected"))
        ),
    )
    assert await verify_dois([], openalex=OpenAlexClient(http), crossref=CrossrefClient(http)) == []


async def test_unexpected_4xx_folds_to_unverifiable_not_crash(make_http: MakeHttp) -> None:
    """A 403 (e.g. comma-corrupted batch filter) must yield ok=None for the
    batch, never escape as HTTPStatusError — one bad DOI string in a page
    must not crash the reviewer run or 500 the route."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": "invalid filter"})

    http = make_http(handler)
    verdicts = await verify_dois(
        ["10.1234/abc,def", "10.1038/nature14539"],
        openalex=OpenAlexClient(http),
        crossref=CrossrefClient(http),
    )
    assert len(verdicts) == 2
    assert all(v.ok is None for v in verdicts)
    assert all(v.retracted is None for v in verdicts)
