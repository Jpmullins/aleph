"""A large upload is refused, and refused WITHOUT being buffered first.

`upload_source` was `data = await file.read()` — the whole request body into
memory, unbounded, before anything looked at it. One large POST is enough to
take the API out, and the container now carries a hard `mem_limit`, so "out"
means OOM-killed rather than slow.

The subtle half is the second clause. Reading the body and then checking its
length still buffers the body, so the machine is already gone by the time the
413 is written. And checking `Content-Length` instead would be cheaper and
wrong twice: a chunked request carries no length at all, and a header is a
claim by the caller — the entire point is not to trust the size the client says
it is sending.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from aleph_api.routes.sources import _read_bounded


class _Body:
    """An UploadFile stand-in that records how much was actually read.

    A real `UploadFile` over a large body would make this test slow and prove
    the same thing. What matters is the READ PATTERN, which this can observe
    and a real one cannot.
    """

    def __init__(self, total: int, chunk_reads: list[int]) -> None:
        self._remaining = total
        self._reads = chunk_reads

    async def read(self, size: int = -1) -> bytes:
        if size < 0:
            # The unbounded call this whole change exists to remove. If the
            # implementation ever goes back to it, this makes the test say so
            # rather than quietly passing on a small fixture.
            msg = "read() was called with no size — the body is being buffered whole"
            raise AssertionError(msg)
        n = min(size, self._remaining)
        self._remaining -= n
        self._reads.append(n)
        return b"x" * n


async def test_an_oversized_upload_is_refused_with_413() -> None:
    reads: list[int] = []
    body = _Body(total=10 * 1024 * 1024, chunk_reads=reads)
    with pytest.raises(HTTPException) as caught:
        await _read_bounded(body, limit=1024 * 1024)  # ty: ignore[invalid-argument-type]
    assert caught.value.status_code == 413
    assert "limit" in str(caught.value.detail)


async def test_it_stops_reading_instead_of_draining_the_body() -> None:
    """The point of the limit: refusing after buffering 10 GiB refuses nothing.

    Bounded at 1 MiB against a 100 MiB body, it must read a couple of chunks
    and stop — not a hundred.
    """
    reads: list[int] = []
    body = _Body(total=100 * 1024 * 1024, chunk_reads=reads)
    with pytest.raises(HTTPException):
        await _read_bounded(body, limit=1024 * 1024)  # ty: ignore[invalid-argument-type]
    assert sum(reads) <= 4 * 1024 * 1024, f"read {sum(reads)} bytes to refuse 1 MiB"


async def test_an_upload_within_the_limit_is_returned_whole() -> None:
    """The other half. A limit that also rejects legitimate uploads is not a fix."""
    reads: list[int] = []
    body = _Body(total=3 * 1024 * 1024, chunk_reads=reads)
    data = await _read_bounded(body, limit=8 * 1024 * 1024)  # ty: ignore[invalid-argument-type]
    assert len(data) == 3 * 1024 * 1024


async def test_an_empty_upload_is_not_an_error() -> None:
    data = await _read_bounded(_Body(total=0, chunk_reads=[]), limit=1024)  # ty: ignore[invalid-argument-type]
    assert data == b""


def test_the_limit_is_configuration_not_a_literal() -> None:
    from aleph_api.settings import Settings

    field = Settings.model_fields["aleph_max_upload_bytes"]
    assert field.default == 64 * 1024 * 1024


def test_the_route_reads_through_the_bounded_helper() -> None:
    """`_read_bounded` with no caller would be the usual defect.

    An AST check on the route, because a `grep` counts the definition, the
    import and the docstring — three hits for zero call sites.
    """
    import ast
    import pathlib

    src = pathlib.Path("apps/api/src/aleph_api/routes/sources.py")
    tree = ast.parse(src.read_text())
    bounded = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_read_bounded"
    ]
    assert len(bounded) == 1, "the upload route does not go through the bounded read"

    # And no argument-less `file.read()` survives ANYWHERE in the module.
    #
    # AST, not a substring search. The first version of this check was
    # `"await file.read()" not in source` and it failed against the docstring
    # explaining why that call was removed — a check that cannot tell a comment
    # from code, which is the same defect Part 4 corrections #21 and #22 exist
    # to remove from this plan.
    unbounded = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "read"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "file"
        and not node.args
        and not node.keywords
    ]
    assert not unbounded, (
        f"file.read() with no size at line(s) {[n.lineno for n in unbounded]} — "
        "the body is buffered whole before anything checks it"
    )
