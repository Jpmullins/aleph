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


# ---- WP-5 A3: honest artifact kinds (implement-or-400) ---------------------


@pytest.mark.parametrize("kind", ["report_docx", "deck_pdf"])
async def test_unexported_report_kinds_rejected_by_service(kind: str) -> None:
    # No docx/deck exporter exists — the service must reject rather than emit a
    # mislabeled markdown bundle.
    with pytest.raises(ValidationFailed):
        await artifact_service.create_artifact(
            MagicMock(),
            principal=_principal(),
            project_id=uuid7(),
            title="x",
            artifact_kind=kind,
        )


@pytest.mark.parametrize("kind", ["report_docx", "deck_pdf"])
def test_unexported_report_kinds_rejected_by_route_model(kind: str) -> None:
    # Route-level validation: the BuildIn pattern must reject these kinds
    # (FastAPI turns the pydantic ValidationError into a 422).
    from pydantic import ValidationError

    from aleph_api.routes.artifacts import BuildIn

    with pytest.raises(ValidationError):
        BuildIn(title="x", artifact_kind=kind)


@pytest.mark.parametrize("kind", ["report_pdf", "report_markdown_bundle", "source_pack"])
def test_builder_report_kinds_accepted_by_route_model(kind: str) -> None:
    from aleph_api.routes.artifacts import BuildIn

    body = BuildIn(title="x", artifact_kind=kind)
    assert body.artifact_kind == kind


class _FakeStored:
    def __init__(self, uri: str) -> None:
        self.storage_uri = uri


class _FakeAssetStore:
    def __init__(self) -> None:
        self.last_key: str | None = None

    def put_bytes(self, *, key: str, data: bytes, mime_type: str) -> _FakeStored:
        del data, mime_type
        self.last_key = key
        return _FakeStored(f"s3://bucket/{key}")


@pytest.mark.parametrize(
    ("kind", "ext", "magic"),
    [
        ("report_pdf", "pdf", b"%PDF"),
        ("report_markdown_bundle", "zip", b"PK"),
        ("source_pack", "zip", b"PK"),
    ],
)
async def test_node_package_produces_real_format_per_kind(
    monkeypatch: pytest.MonkeyPatch, kind: str, ext: str, magic: bytes
) -> None:
    from aleph_artifacts.builder import workflow

    # Avoid pulling weasyprint into the unit run — assert the pdf branch is
    # selected and produces PDF-shaped bytes.
    monkeypatch.setattr(workflow, "markdown_to_pdf_bytes", lambda **_kw: b"%PDF-1.7 fake")

    store = _FakeAssetStore()
    ctx = workflow._Ctx(
        session_maker=MagicMock(),  # unused: _node_package has no agent_run_id
        asset_store=store,  # type: ignore[arg-type]
        principal=_principal(),
    )
    # The builder context lives in a ContextVar now: concurrent arq jobs used to
    # clobber the module global and clear each other's context mid-run.
    workflow._active_ctx_var.set(ctx)

    state = {
        "project_id": uuid7(),
        "artifact_id": uuid7(),
        "artifact_kind": kind,
        "composed_markdown": "# body",
        "bibliography_markdown": "",
        "version_no": 1,
    }
    out = await workflow._node_package(state)

    assert out["artifact_bytes"].startswith(magic)
    assert store.last_key is not None
    assert store.last_key.endswith(f".{ext}")


async def test_node_package_rejects_unexpected_kind(monkeypatch: pytest.MonkeyPatch) -> None:
    from aleph_artifacts.builder import workflow

    ctx = workflow._Ctx(
        session_maker=MagicMock(),
        asset_store=_FakeAssetStore(),  # type: ignore[arg-type]
        principal=_principal(),
    )
    # The builder context lives in a ContextVar now: concurrent arq jobs used to
    # clobber the module global and clear each other's context mid-run.
    workflow._active_ctx_var.set(ctx)

    state = {
        "project_id": uuid7(),
        "artifact_id": uuid7(),
        "artifact_kind": "report_docx",  # no exporter → must not silently bundle
        "composed_markdown": "# body",
        "bibliography_markdown": "",
        "version_no": 1,
    }
    with pytest.raises(ValueError, match="unsupported artifact_kind"):
        await workflow._node_package(state)
