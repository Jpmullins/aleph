"""An archived project can still take a copy of its own wiki.

`_assert_project_writable` refuses every non-GET on an archived or deleted
project. The vault export is a POST, because it returns a zip — so the person
most likely to want their knowledge out, someone who has already archived the
project, was the one refused, with a 409 telling them to restore it first. Reads
stay open on an archived project deliberately; this route was on the wrong side
of the METHOD check, not the wrong side of the rule.

Tested against the rule itself rather than through a route. The integration
tests for the export route override `project_scope_dep` to inject a principal,
which means the writable check never runs there — a test driven through that app
would report a pass no matter what this rule said.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from aleph_api.middleware.project_scope import _NON_MUTATING_POSTS, _assert_project_writable
from aleph_core.errors import Conflict

EXPORT_ROUTE = "/v1/projects/{project_id}/export/vault"
PROJECT = uuid.uuid4()


def _request(method: str, route_path: str) -> Any:
    """The two things `_assert_project_writable` reads off a request."""
    return SimpleNamespace(method=method, scope={"route": SimpleNamespace(path=route_path)})


@pytest.mark.parametrize("status", ["archived", "deleted"])
def test_the_vault_export_is_allowed_on_a_project_that_takes_no_writes(status: str) -> None:
    _assert_project_writable(_request("POST", EXPORT_ROUTE), PROJECT, status)


@pytest.mark.parametrize("status", ["archived", "deleted"])
def test_an_ordinary_post_is_still_refused(status: str) -> None:
    """The exemption must be one route, not a hole.

    Without this, the test above passes just as well if somebody widens the rule
    to "POSTs are fine on archived projects", which is the opposite of what the
    rule is for.
    """
    with pytest.raises(Conflict, match="accepts no writes"):
        _assert_project_writable(
            _request("POST", "/v1/projects/{project_id}/wiki/hubs/sync"), PROJECT, status
        )


def test_an_active_project_is_unaffected() -> None:
    _assert_project_writable(
        _request("POST", "/v1/projects/{project_id}/wiki/hubs/sync"), PROJECT, "active"
    )


def test_reads_stay_open_as_they_did() -> None:
    _assert_project_writable(
        _request("GET", "/v1/projects/{project_id}/wiki/pages"), PROJECT, "archived"
    )


def test_the_restore_path_is_still_exempt() -> None:
    """The one request that can bring a project back. Asserted here because the
    exemption list gained a second member, and a second member is when a list
    starts being able to shadow the first."""
    _assert_project_writable(_request("PATCH", "/v1/projects/{project_id}"), PROJECT, "archived")


def test_the_exemption_names_a_route_that_exists() -> None:
    """An exemption for a route template that does not exist is either a typo
    that quietly does nothing, or a rename that quietly reopened the refusal."""
    import os

    os.environ.setdefault("ALEPH_CORS_ORIGINS", "http://localhost:5173")
    from aleph_api.main import create_app

    paths = {getattr(route, "path", "") for route in create_app().routes}
    for template in _NON_MUTATING_POSTS:
        assert template in paths, f"{template} is exempted and is not a route"
