"""Scholar error taxonomy.

Two buckets only: transient upstream trouble (retry later, verdicts become
``None``) and the one authoritative OAuth failure that requires the user to
re-run the Consensus connect bootstrap.
"""

from __future__ import annotations


class ScholarUpstreamError(Exception):
    """Transient upstream failure (network, timeout, 5xx after retries).

    Consumers must treat this as "unverifiable", never as "does not exist".
    """


class ConsensusReconnectRequired(Exception):
    """The stored Consensus OAuth grant is dead (invalid_grant/invalid_client).

    Raised only on an authoritative AS error — never on network failure.
    The remedy is `scripts/connect-consensus.py`.
    """
