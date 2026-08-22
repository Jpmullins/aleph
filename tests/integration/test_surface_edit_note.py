"""A note with no section is editable, and the edit lands in the database.

WS-UI-4 c6, server half. `_notes_messages` binds `section_id: None` for any note
that has no sections — a promoted or imported one — and `_edit_note` required
one, so the only way to edit such a note was an action the catalog schema
refused. The browser never even sent it: `NoteEditorCard` guarded the dispatch
on the same null and reported "Saved" anyway. Two layers agreeing to discard the
analyst's typing, and neither raising.

Everything here runs inside one rolled-back transaction (the `session` fixture),
because the notes tables are not in the integration teardown list and a test
that commits into them leaves rows behind.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aleph_a2ui.action_router import CardActionRequest
from aleph_a2ui.catalog import CATALOG
from aleph_api.a2ui_handlers import _edit_note
from aleph_core.errors import NotFound, ValidationFailed
from aleph_notes.models import Note, NoteSection
from aleph_notes.note_service import create_note
from aleph_security.principal import Principal

pytestmark = pytest.mark.integration

ACTOR = uuid.UUID("00000000-0000-0000-0000-0000000000aa")


def _principal() -> Principal:
    return Principal(user_id=ACTOR, subject="notes", email="n@example.test", actor_kind="user")


def _request(params: dict[str, object]) -> CardActionRequest:
    return CardActionRequest(
        action_kind="edit_note",
        surface_kind="notesSurface",
        card_id=None,
        target_id=None,
        target_kind=None,
        params=params,
    )


async def _sections(session: AsyncSession, note_id: uuid.UUID) -> list[NoteSection]:
    return list(
        (
            await session.execute(
                select(NoteSection)
                .where(NoteSection.note_id == note_id)
                .order_by(NoteSection.ordinal)
            )
        )
        .scalars()
        .all()
    )


async def test_editing_a_sectionless_note_creates_the_section_and_stores_the_body(
    session: AsyncSession,
) -> None:
    project_id = uuid.uuid4()
    note = await create_note(session, project_id=project_id, title="Promoted", created_by=ACTOR)
    assert await _sections(session, note.id) == []

    out = await _edit_note(
        session=session,
        principal=_principal(),
        project_id=project_id,
        request=_request({"note_id": str(note.id), "body_md": "the analyst's typing"}),
    )

    assert out["created"] is True
    rows = await _sections(session, note.id)
    assert [r.body_md for r in rows] == ["the analyst's typing"]
    assert str(rows[0].id) == out["section_id"]


async def test_a_second_edit_updates_that_section_rather_than_stacking_new_ones(
    session: AsyncSession,
) -> None:
    """Otherwise every keystroke burst after the debounce appends a section and
    the note grows a copy of itself per save."""
    project_id = uuid.uuid4()
    note = await create_note(session, project_id=project_id, title="Promoted", created_by=ACTOR)

    first = await _edit_note(
        session=session,
        principal=_principal(),
        project_id=project_id,
        request=_request({"note_id": str(note.id), "body_md": "one"}),
    )
    # The card re-binds from the surface: the next save carries the section id
    # the server just handed back.
    await _edit_note(
        session=session,
        principal=_principal(),
        project_id=project_id,
        request=_request({"section_id": first["section_id"], "body_md": "two"}),
    )

    rows = await _sections(session, note.id)
    assert [r.body_md for r in rows] == ["two"]


async def test_an_edit_naming_neither_is_refused_rather_than_dropped(
    session: AsyncSession,
) -> None:
    with pytest.raises(ValidationFailed):
        await _edit_note(
            session=session,
            principal=_principal(),
            project_id=uuid.uuid4(),
            request=_request({"body_md": "nowhere to go"}),
        )


def test_the_catalog_accepts_a_note_id_and_still_demands_one_of_the_two() -> None:
    """The router validates params against this schema BEFORE the handler runs.

    A handler that accepts `note_id` behind a schema that requires `section_id`
    is unreachable, and the failure is a 422 that names a field the card is not
    supposed to have.
    """
    from jsonschema import ValidationError, validate

    schema = CATALOG["actions"]["edit_note"]["params"]
    validate({"note_id": str(uuid.uuid4()), "body_md": "x"}, schema)
    validate({"section_id": str(uuid.uuid4()), "body_md": "x"}, schema)
    with pytest.raises(ValidationError):
        validate({"body_md": "x"}, schema)


async def test_a_note_from_another_project_is_not_editable(session: AsyncSession) -> None:
    """`create_section` takes the project id from the CALLER, so a cross-project
    note id would otherwise graft a section onto someone else's note."""
    owner = uuid.uuid4()
    intruder = uuid.uuid4()
    note = await create_note(session, project_id=owner, title="Theirs", created_by=ACTOR)

    with pytest.raises(NotFound):
        await _edit_note(
            session=session,
            principal=_principal(),
            project_id=intruder,
            request=_request({"note_id": str(note.id), "body_md": "not yours"}),
        )

    # And no section was grafted onto it.
    assert await _sections(session, note.id) == []
    kept = (await session.execute(select(Note).where(Note.id == note.id))).scalar_one()
    assert kept.project_id == owner
