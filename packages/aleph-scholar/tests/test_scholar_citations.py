"""expand_citations both directions, on fixture JSON over a mocked transport."""

from __future__ import annotations

from collections.abc import Callable

import httpx

from aleph_scholar.http import ScholarHttp
from aleph_scholar.service import ScholarService

MakeHttp = Callable[[Callable[[httpx.Request], httpx.Response]], ScholarHttp]

BASE_DOI = "10.1234/base"


def _work(short_id: str, title: str, doi: str | None = None) -> dict[str, object]:
    return {
        "id": f"https://openalex.org/{short_id}",
        "doi": f"https://doi.org/{doi}" if doi else None,
        "display_name": title,
        "publication_year": 2019,
        "cited_by_count": 7,
        "is_retracted": False,
        "primary_location": {"source": {"display_name": "Fixture Journal"}},
        "authorships": [{"author": {"display_name": "Grace Hopper"}}],
    }


def _citation_handler(requests: list[httpx.Request]) -> Callable[[httpx.Request], httpx.Response]:
    base = _work("W1", "Base work", BASE_DOI)
    base["referenced_works"] = [
        "https://openalex.org/W2",
        "https://openalex.org/W3",
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.host == "api.openalex.org"
        path = request.url.path
        if path == f"/works/doi:{BASE_DOI}":
            return httpx.Response(200, json=base)
        assert path == "/works"
        filter_param = request.url.params.get("filter", "")
        if filter_param.startswith("openalex_id:"):
            return httpx.Response(
                200,
                json={
                    "results": [
                        _work("W2", "Referenced one", "10.1234/ref1"),
                        _work("W3", "Referenced two", "10.1234/ref2"),
                    ]
                },
            )
        if filter_param.startswith("cites:"):
            assert filter_param == "cites:W1"
            return httpx.Response(
                200, json={"results": [_work("W4", "Citing work", "10.1234/cite1")]}
            )
        raise AssertionError(f"unexpected request: {request.url}")

    return handler


async def test_expand_citations_both_directions(make_http: MakeHttp) -> None:
    requests: list[httpx.Request] = []
    service = ScholarService(mailto="test@aleph.local", http=make_http(_citation_handler(requests)))
    expansion = await service.expand_citations(BASE_DOI, direction="both", limit=25)
    assert [w.title for w in expansion.backward] == ["Referenced one", "Referenced two"]
    assert [w.title for w in expansion.forward] == ["Citing work"]
    ref = expansion.backward[0]
    assert ref.openalex_id == "W2"
    assert ref.doi == "10.1234/ref1"
    assert ref.venue == "Fixture Journal"
    assert ref.authors == ["Grace Hopper"]
    assert ref.year == 2019
    assert ref.cited_by_count == 7


async def test_expand_citations_backward_only(make_http: MakeHttp) -> None:
    requests: list[httpx.Request] = []
    service = ScholarService(mailto="test@aleph.local", http=make_http(_citation_handler(requests)))
    expansion = await service.expand_citations(BASE_DOI, direction="backward")
    assert len(expansion.backward) == 2
    assert expansion.forward == []
    assert all("cites:" not in str(r.url) for r in requests)


async def test_expand_citations_forward_only(make_http: MakeHttp) -> None:
    requests: list[httpx.Request] = []
    service = ScholarService(mailto="test@aleph.local", http=make_http(_citation_handler(requests)))
    expansion = await service.expand_citations(BASE_DOI, direction="forward")
    assert expansion.backward == []
    assert [w.openalex_id for w in expansion.forward] == ["W4"]


async def test_expand_citations_respects_limit(make_http: MakeHttp) -> None:
    requests: list[httpx.Request] = []
    service = ScholarService(mailto="test@aleph.local", http=make_http(_citation_handler(requests)))
    expansion = await service.expand_citations(BASE_DOI, direction="backward", limit=1)
    assert len(expansion.backward) == 1


async def test_expand_citations_unknown_ref_is_empty(make_http: MakeHttp) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    service = ScholarService(mailto="test@aleph.local", http=make_http(handler))
    expansion = await service.expand_citations("10.9999/nope", direction="both")
    assert expansion.backward == []
    assert expansion.forward == []
