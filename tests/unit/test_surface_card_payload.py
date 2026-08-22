"""The pinned code_runner card, and why its component name is written out.

`render_code_artifact_job` is the ONLY producer of `ImageCard` and
`HtmlFrameCard` anywhere in Aleph. It used to assemble the pin as
`{"type": card_kind, "id": ..., "props": props}` with both halves held in
variables, which meant `scripts/check-surface-bindings.sh` — which reads
emission sites statically — could not tell which props reached which zod
schema, and so never compared either card at all.
`ChartCard.artifact_version_id` shipped from here and was read by nothing.

Writing the names out is only safe while the branches cover `_KIND_MAP`, so
that is what these pin: the map and the payload builder agree, for every entry,
and an entry with no branch is a loud failure rather than an HTML frame
pointing at PNG bytes.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from aleph_workers.jobs.render_code import _KIND_MAP, card_payload

_CARD_ID = UUID("01920000-0000-7000-8000-000000000001")
_ASSET = "/v1/projects/p/assets/artifact-version/v"


@pytest.mark.parametrize("output_kind", sorted(_KIND_MAP))
def test_every_kind_the_job_accepts_pins_the_card_that_kind_names(output_kind: str) -> None:
    """The duplication between `_KIND_MAP` and the literal branches cannot drift.

    A literal component name at the emission site is what makes the prop
    contract statically checkable, and the cost of that is saying the name
    twice. This is the check that makes the second copy free: add an output kind
    to `_KIND_MAP` without a branch and this goes red, rather than the pin
    quietly becoming whatever the last branch was.
    """
    card_kind = _KIND_MAP[output_kind][3]
    payload = card_payload(
        card_kind,
        card_id=_CARD_ID,
        title="t",
        asset_uri=_ASSET,
        inline_spec={"mark": "bar"},
    )
    assert payload["type"] == card_kind


def test_an_unmapped_kind_raises_instead_of_becoming_an_html_frame() -> None:
    """The old `else: # HtmlFrameCard` was a fall-through, not a decision.

    Anything that was not a chart or an image was pinned as an interactive HTML
    frame whose `src` streamed bytes of some other type — into an iframe with
    `allow-scripts`.
    """
    with pytest.raises(ValueError, match="no card payload for 'PdfCard'"):
        card_payload("PdfCard", card_id=_CARD_ID, title="t", asset_uri=_ASSET, inline_spec=None)


def test_the_chart_pin_no_longer_carries_a_prop_the_browser_drops() -> None:
    """`artifact_version_id` was written here, streamed, and dropped by the binder.

    No zod schema declared it and no view read it; provenance now rides on the
    `card.pin` ledger row, which is append-only and actually read. If it comes
    back into the props, `check-surface-bindings.sh` fails too — this asserts it
    at the producer so the reason survives next to the code.
    """
    payload = card_payload(
        "ChartCard", card_id=_CARD_ID, title="t", asset_uri=_ASSET, inline_spec={"mark": "bar"}
    )
    assert set(payload["props"]) == {"title", "vega_lite_spec"}


def test_an_image_pin_carries_the_streaming_route_and_an_alt() -> None:
    """`ImageCard` refuses to render a `src` that is not an asset route."""
    payload = card_payload(
        "ImageCard", card_id=_CARD_ID, title="Fig 1", asset_uri=_ASSET, inline_spec=None
    )
    assert payload == {
        "type": "ImageCard",
        "id": f"pinned-{_CARD_ID}",
        "props": {"title": "Fig 1", "src": _ASSET, "alt": "Fig 1"},
    }
