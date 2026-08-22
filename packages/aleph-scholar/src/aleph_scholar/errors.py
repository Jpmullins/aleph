"""Scholar error taxonomy.

Three buckets. The first two both mean "the upstream did not give us data",
but they mean opposite things to whoever has to fix it, and collapsing them
is the defect this file exists to prevent:

- ``ScholarClientError`` — the upstream *rejected the request Aleph sent*.
  A 400 from OpenAlex because Aleph built a filter with a stray comma in it
  is not an outage; reporting it as one tells an operator the internet is
  down when the actual message is "your filter syntax is wrong".
- ``ScholarUnavailable`` — 429 or 5xx that outlived the request's retry
  budget, or a transport failure. Retrying later is the remedy, and the
  upstream's own ``Retry-After`` is carried so the caller can say when.
- ``ConsensusReconnectRequired`` — the one authoritative OAuth failure.

Both upstream buckets subclass ``ScholarUpstreamError`` deliberately, so the
tri-state consumers that already fold *any* upstream trouble to ``ok=None``
(``verify_dois``, the reviewer's ``doi_verification`` node) keep behaving
exactly as before: an upstream fault must never be read as "this DOI does
not exist", whichever bucket it landed in.
"""

from __future__ import annotations

#: How much of an upstream error body is echoed back to our caller. Enough to
#: carry a real reason ("Invalid query parameters: filter"), short enough that
#: an upstream HTML error page cannot become the response body.
_REASON_MAX_CHARS = 300


class ScholarUpstreamError(Exception):
    """Base: the scholarly upstream did not return usable data.

    Consumers must treat this as "unverifiable", never as "does not exist".
    """

    #: The upstream HTTP status, when there was a response at all.
    status_code: int | None = None
    #: Seconds the upstream asked us to wait, when it said.
    retry_after: float | None = None


class ScholarClientError(ScholarUpstreamError):
    """The upstream rejected the request Aleph sent (a 4xx that is not 429).

    Not retryable: sending the same bad request again produces the same 4xx.
    ``reason`` is the upstream's own explanation, truncated — it is the piece
    that makes the failure actionable instead of mysterious.
    """

    def __init__(self, message: str, *, status_code: int, reason: str = "") -> None:
        super().__init__(message)
        self.status_code = status_code
        self.reason = reason[:_REASON_MAX_CHARS]


class ScholarUnavailable(ScholarUpstreamError):
    """429 or 5xx that survived the request's deadline, or transport failure.

    ``retry_after`` is the upstream's own header when it sent one, so the API
    layer can pass a real number to its caller rather than inventing one.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retry_after = retry_after


class ConsensusReconnectRequired(Exception):
    """The stored Consensus OAuth grant is dead (invalid_grant/invalid_client).

    Raised only on an authoritative AS error — never on network failure.
    The remedy is `scripts/connect-consensus.py`.
    """
