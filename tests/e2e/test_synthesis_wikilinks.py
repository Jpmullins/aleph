"""E1.2 — synthesis-authored pages must commit with their wikilinks.

`_node_wikilink_resolve` parsed `[[Topic]]` out of the report body, resolved
each through the alias table, and returned `{"resolved_wikilinks": [...]}`.
`resolved_wikilinks` was not declared on `SynthesisState`, so LangGraph
**discarded the write silently** — no error, no warning. The next node read
`state.get("resolved_wikilinks") or []`, got an empty list, and committed every
synthesis page with **zero** wikilinks.

That guts rule #1. Wiki-first retrieval loads the selected pages *plus their
one-hop wikilinks*; a corpus of synthesis pages with no outbound links makes
that expansion a no-op, and the assistant answers from strictly less context
than the design assumes — while every step reports success.

**This test drives the compiled graph**, not the node. That matters: the defect
was not in `_node_wikilink_resolve`, which was always correct, but in the
channel between it and `_node_commit_revision`. Calling the node directly and
inspecting its return value passes on the broken tree.

No gateway is faked because none is needed — every node on this path is pure or
DB-backed. The assertion is against rows in Postgres, written by `WikiService`.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from aleph_core.ids import uuid7

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

BODY_MD = """# Chain-of-Thought Prompting

Chain-of-thought prompting elicits reasoning [c1]. It relates closely to
[[Topic A]] and is contrasted with [[Topic B]] in the literature.

## Scale

The benefit emerges only at scale, which [[Topic A]] also predicts.
"""


async def _project(http_client) -> str:
    resp = await http_client.post(
        "/v1/projects", json={"title": f"synth {uuid4().hex[:6]}", "description": ""}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


@pytest.fixture
async def auth_bypass(monkeypatch):
    from aleph_api.middleware import auth as auth_mod

    async def fake_verify(token, *, jwks_cache, issuer, audience, leeway_seconds=30):
        return {"sub": "synth", "email": "s@test.local", "name": "S"}

    monkeypatch.setattr(auth_mod, "verify_user_jwt", fake_verify)


async def _run_synthesis(asgi_app, project_id: str, body_md: str = BODY_MD):
    """Drive the REAL `SynthesisWorkflow` end to end."""
    from aleph_security.principal import Principal
    from aleph_wiki.synthesis_workflow import (
        ResearchClaim,
        ResearchReport,
        ResearchSourceRef,
        SynthesisWorkflow,
    )
    from tests.e2e.test_citation_provenance import _dev_user_id

    maker = asgi_app.state.session_maker
    principal = Principal(
        user_id=(await _dev_user_id(maker)),
        subject="dev@aleph.local",
        email="dev@aleph.local",
        actor_kind="user",
    )
    ref = ResearchSourceRef(source_short_id="S1", title="A Source", url="https://x.invalid")
    report = ResearchReport(
        topic="Chain-of-Thought Prompting",
        body_md=body_md,
        summary="CoT elicits reasoning.",
        sources=[ref],
        citations_by_marker={"c1": ref},
        claims=[
            ResearchClaim(
                text="Chain-of-thought prompting elicits reasoning.",
                citation_markers=["c1"],
                section_anchor=None,
            )
        ],
    )
    workflow = SynthesisWorkflow(
        session_maker=maker,
        litellm=asgi_app.state.litellm,
        principal=principal,
    )
    return await workflow.run(
        {
            "agent_run_id": uuid7(),
            "project_id": UUID(project_id),
            "topic": "Chain-of-Thought Prompting",
            "report": report,
            "profile_bindings": {},
        }
    )


async def _wikilinks_for(asgi_app, revision_id):
    from aleph_wiki.models import WikiLink

    async with asgi_app.state.session_maker() as session:
        return list(
            (await session.execute(select(WikiLink).where(WikiLink.src_revision_id == revision_id)))
            .scalars()
            .all()
        )


class TestWikilinksSurviveTheGraph:
    async def test_committed_revision_has_the_body_s_wikilinks(
        self, asgi_app, http_client, auth_bypass
    ):
        """The headline. Two distinct `[[targets]]` in, at least two rows out."""
        project_id = await _project(http_client)
        state = await _run_synthesis(asgi_app, project_id)

        revision_ids = state.get("committed_revision_ids") or []
        assert revision_ids, "the workflow committed no revision at all"
        links = await _wikilinks_for(asgi_app, revision_ids[0])

        assert len(links) >= 2, (
            f"the committed revision has {len(links)} wikilink rows; the body "
            f"contains [[Topic A]] and [[Topic B]]. LangGraph dropped the "
            f"`resolved_wikilinks` write, so one-hop expansion — the second "
            f"half of wiki-first retrieval — has nothing to expand through."
        )
        assert {link.dst_title for link in links} >= {"Topic A", "Topic B"}

    async def test_state_carries_the_channel_out_of_the_graph(
        self, asgi_app, http_client, auth_bypass
    ):
        """The channel itself, asserted on the graph's own output.

        If `resolved_wikilinks` is undeclared this is empty even though the node
        returned a populated list — which is exactly how the defect hid.
        """
        project_id = await _project(http_client)
        state = await _run_synthesis(asgi_app, project_id)
        resolved = state.get("resolved_wikilinks") or []
        assert len(resolved) >= 2, (
            f"the graph's final state carries {len(resolved)} resolved "
            f"wikilinks; the node produced them and the channel discarded them"
        )

    async def test_repeated_target_is_counted_not_duplicated(
        self, asgi_app, http_client, auth_bypass
    ):
        """`[[Topic A]]` appears twice in the body: one row, two occurrences."""
        project_id = await _project(http_client)
        state = await _run_synthesis(asgi_app, project_id)
        links = await _wikilinks_for(asgi_app, (state["committed_revision_ids"])[0])
        by_title = {link.dst_title: link for link in links}
        assert by_title["Topic A"].occurrences == 2, (
            f"expected 2 occurrences of [[Topic A]], got {by_title['Topic A'].occurrences}"
        )
        assert by_title["Topic B"].occurrences == 1

    async def test_body_without_wikilinks_commits_cleanly(self, asgi_app, http_client, auth_bypass):
        """Zero links must mean "the body had none", not "the write was lost".

        Without this the headline test could be satisfied by a writer that
        invents links, and an empty result would stay ambiguous.
        """
        project_id = await _project(http_client)
        state = await _run_synthesis(
            asgi_app, project_id, body_md="# Plain\n\nNo links here at all [c1].\n"
        )
        links = await _wikilinks_for(asgi_app, (state["committed_revision_ids"])[0])
        assert links == []
