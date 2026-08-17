"""Building a Postgres ``tsquery`` that a real question can match.

Postgres offers three parsers and **all three conjoin unquoted terms**:

* ``plainto_tsquery('a b c')``      -> ``a & b & c``
* ``websearch_to_tsquery('a b c')`` -> ``a & b & c`` (adds quoted phrases,
  ``OR``, and ``-`` negation, but the default is still AND)
* ``to_tsquery``                    -> you write the operators yourself

So a natural-language question must have *every* content word present for the
row to match at all. That is fine for a search box where the user expects to
narrow, and wrong for "find me the page about this", where a question phrased in
different words than the document should still retrieve it and simply rank
lower. It is half of why Aleph's wiki retrieval returned nothing for most
questions — the other half being that the index did not cover page bodies.

:func:`or_tsquery` gets OR semantics without hand-parsing user text: let
Postgres parse the query normally, then rewrite the conjunctions to
disjunctions in the parsed ``tsquery``, and cast back.

    websearch_to_tsquery('english', 'sediment record environment')
      -> 'sediment' & 'record' & 'environ'
      -> 'sediment' | 'record' | 'environ'

This is safe in a way that string-munging the raw input is not: the rewrite
happens on an already-parsed ``tsquery``, so stemming, stop-word removal and
quoting are Postgres's job and the user's text never reaches the query as
operators. Phrase operators (``<->``) are left alone, so a quoted "wave base"
stays a phrase.

Ranking is unaffected: ``ts_rank`` scores by how much of the query matched and
with what weight, so a row matching every term still outranks one matching a
single term. OR widens the candidate set; it does not flatten the order.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from sqlalchemy import Text, cast, func
from sqlalchemy.dialects.postgresql import TSQUERY

if TYPE_CHECKING:
    from sqlalchemy.sql.elements import ColumnElement

__all__ = ["or_tsquery"]


# Returns ColumnElement[Any]: SQLAlchemy types TSQUERY's Python side as `str`,
# so a precise annotation would claim the expression yields a Python string.
def or_tsquery(query: str, *, regconfig: str = "english") -> ColumnElement[Any]:
    """A ``tsquery`` whose terms are ORed rather than ANDed.

    Use for retrieval, where a partial match should surface and rank lower. Use
    ``plainto_tsquery`` directly where a caller genuinely means "all of these".
    """
    parsed = func.websearch_to_tsquery(regconfig, query)
    ored = func.replace(cast(parsed, Text), "&", "|")
    return cast(ored, TSQUERY)
