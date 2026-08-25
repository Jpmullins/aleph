"""The two status vocabularies are different sizes, so the map has to be total.

`AsyncSubAgentMiddleware._build_check_result` keys its entire result off
`run["status"]` and compares against six literals. A value outside that set
falls through every branch: the supervisor is told the task exists and never
that it finished, so a delegated run completes and the conversation never learns.

That makes the fallback the load-bearing part of this module, and it is the
thing worth testing — not the six happy pairs.
"""

from __future__ import annotations

import pytest

from aleph_db.agent_protocol import (
    PROTOCOL_STATUSES,
    TERMINAL_PROTOCOL_STATUSES,
    is_terminal,
    to_protocol_status,
)


@pytest.mark.parametrize(
    ("aleph", "protocol"),
    [
        ("pending", "pending"),
        ("running", "running"),
        ("succeeded", "success"),
        ("completed", "success"),
        ("failed", "error"),
        ("cancelled", "cancelled"),
    ],
)
def test_every_aleph_status_maps(aleph: str, protocol: str) -> None:
    assert to_protocol_status(aleph) == protocol


def test_succeeded_and_completed_both_mean_success() -> None:
    """Both appear in the live table. Picking one would drop the other's runs.

    This duplication is pre-existing and deliberately NOT resolved here — a
    migration collapsing them is a separate change with its own blast radius.
    """
    assert to_protocol_status("succeeded") == to_protocol_status("completed") == "success"


def test_an_unknown_status_reads_as_running_not_error() -> None:
    """The fallback, and the reason for it.

    If Aleph grows a run state this map has not been taught, the run is still
    live as far as anyone knows. Reporting `error` would tell the supervisor to
    abandon work that is proceeding — a worse failure than reporting it as
    in-flight for one more poll.
    """
    assert to_protocol_status("quiesced") == "running"
    assert to_protocol_status("") == "running"


def test_every_mapped_value_is_one_the_middleware_understands() -> None:
    """The whole point. A value outside this set is invisible to the supervisor."""
    for aleph in ("pending", "running", "succeeded", "completed", "failed", "cancelled", "?"):
        assert to_protocol_status(aleph) in PROTOCOL_STATUSES


def test_terminal_set_is_what_the_middleware_caches() -> None:
    """Terminal statuses stop the supervisor asking the server again.

    `running` and `pending` must NOT be terminal or a live task would be cached
    as finished; `interrupted` and `timeout` are not terminal either, because
    `update_async_task` can revive an interrupted run on the same thread.
    """
    assert {"success", "error", "cancelled"} == TERMINAL_PROTOCOL_STATUSES
    for live in ("pending", "running", "interrupted", "timeout"):
        assert not is_terminal(live)
