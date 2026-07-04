"""Artifact-kind allowlist honesty (F5): implemented kinds accepted, unknown 400s.

WP-4c adds `image` / `chart` / `html_frame` (the code_runner outputs). The
allowlist guard runs before any session use, so the reject path is a pure unit
check; the accept path is asserted with a stubbed `_next_short_id`.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from aleph_artifacts import artifact_service
from aleph_core.errors import ValidationFailed
from aleph_core.ids import uuid7
from aleph_security.principal import Principal


def _principal() -> Principal:
    return Principal(user_id=uuid7(), subject="u", email="u@t", actor_kind="user")


async def test_unknown_kind_rejected() -> None:
    with pytest.raises(ValidationFailed):
        await artifact_service.create_artifact(
            MagicMock(),  # session never touched — guard raises first
            principal=_principal(),
            project_id=uuid7(),
            title="x",
            artifact_kind="totally_bogus_kind",
        )


@pytest.mark.parametrize("kind", ["image", "chart", "html_frame"])
async def test_viz_kinds_accepted(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    monkeypatch.setattr(artifact_service, "_next_short_id", AsyncMock(return_value="A0001"))
    session = MagicMock()
    session.flush = AsyncMock()
    artifact = await artifact_service.create_artifact(
        session,
        principal=_principal(),
        project_id=uuid7(),
        title="x",
        artifact_kind=kind,
    )
    assert artifact.artifact_kind == kind
    session.add.assert_called_once()
