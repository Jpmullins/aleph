"""ProjectRole + require_at_least tests."""

from __future__ import annotations

from uuid import uuid4

import pytest

from aleph_core.errors import PermissionDenied
from aleph_security.principal import Principal
from aleph_security.roles import ProjectRole, rank, require_at_least


def _principal() -> Principal:
    return Principal(
        user_id=uuid4(),
        subject="sub",
        email="u@example.com",
        actor_kind="user",
    )


def test_rank_order() -> None:
    assert rank("owner") > rank("editor") > rank("viewer") > rank("unknown")


def test_owner_passes_editor_gate() -> None:
    p = _principal()
    project_id = uuid4()
    p.cache_role(project_id, ProjectRole.OWNER.value)
    require_at_least(p, project_id, at_least=ProjectRole.EDITOR)


def test_viewer_fails_editor_gate() -> None:
    p = _principal()
    project_id = uuid4()
    p.cache_role(project_id, ProjectRole.VIEWER.value)
    with pytest.raises(PermissionDenied):
        require_at_least(p, project_id, at_least=ProjectRole.EDITOR)


def test_non_member_fails() -> None:
    p = _principal()
    with pytest.raises(PermissionDenied):
        require_at_least(p, uuid4(), at_least=ProjectRole.VIEWER)
