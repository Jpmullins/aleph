"""Two global CSS guarantees the workspace must never ship without.

Neither existed before 2026-07-26, and neither is the kind of thing a typecheck,
a linter, or a React test can see — `tsc` cannot read a stylesheet, and Aleph has
no frontend test runner at all. So they are guarded here, against the built
bundle as well as the source: a build that tree-shakes or reorders them away
would otherwise pass every gate.

* **`:focus-visible`** — the workspace is dense and almost entirely buttons
  (surface tabs, card actions, wikilink chips, claim pills). Tailwind's preflight
  suppresses the browser's default focus ring, and nothing replaced it, so
  keyboard and screen-reader users had no position indicator anywhere.
* **`prefers-reduced-motion`** — Aleph animates streaming cursors, pulsing
  activity dots and surface transitions. Ignoring the OS preference is actively
  harmful for users with vestibular disorders.
"""

from __future__ import annotations

import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
STYLES = REPO_ROOT / "apps" / "web" / "src" / "styles.css"
TOKENS = REPO_ROOT / "apps" / "web" / "src" / "styles" / "tokens.css"
INDEX_HTML = REPO_ROOT / "apps" / "web" / "index.html"
DIST = REPO_ROOT / "apps" / "web" / "dist"


def test_styles_source_exists() -> None:
    assert STYLES.is_file(), f"stylesheet not found at {STYLES}"


@pytest.mark.parametrize(
    ("rule", "why"),
    [
        (":focus-visible", "keyboard users have no visible focus indicator"),
        (
            "prefers-reduced-motion",
            "animations play regardless of the user's OS accessibility setting",
        ),
    ],
)
def test_source_declares_rule(rule: str, why: str) -> None:
    assert rule in STYLES.read_text(encoding="utf-8"), f"{rule} missing from styles.css — {why}"


@pytest.mark.parametrize("rule", [":focus-visible", "prefers-reduced-motion"])
def test_built_bundle_keeps_rule(rule: str) -> None:
    """Source is not enough — assert it survives into the shipped CSS.

    Skips when `dist/` has not been built, so this never fails spuriously in a
    clean checkout; CI builds the web app, so it is enforced there.
    """
    if not DIST.is_dir():
        pytest.skip("apps/web/dist not built")
    css = list(DIST.rglob("*.css"))
    if not css:
        pytest.skip("no built CSS found")
    combined = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in css)
    assert rule in combined, (
        f"{rule} is in styles.css but absent from the built bundle — it was "
        f"stripped somewhere in the build."
    )


def test_focus_ring_uses_a_theme_token() -> None:
    """A hardcoded colour would be invisible in one of the two themes."""
    src = STYLES.read_text(encoding="utf-8")
    block = src[src.index(":focus-visible") : src.index(":focus-visible") + 220]
    assert "var(--" in block, (
        f"the focus ring must use a theme token so it is visible in both light "
        f"and dark; got:\n{block}"
    )


def test_theme_is_applied_before_first_paint() -> None:
    """A post-mount theme apply flashes the wrong theme on every load."""
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert "data-theme" in html, (
        "index.html does not set data-theme inline; the app will flash the "
        "default theme before React mounts and applies the stored preference."
    )


def _declaration_block(css: str, selector: str) -> str:
    """The body of the first real `selector { ... }` rule (not a comment)."""
    import re

    m = re.search(re.escape(selector) + r"\s*\{(.*?)\n\}", css, re.S)
    assert m, f"no declaration block found for {selector}"
    return m.group(1)


def test_both_themes_define_the_accent_token() -> None:
    """The focus ring resolves through `--accent`; if a theme omits it the ring
    silently disappears in that theme only — the hardest kind of a11y bug to
    notice, because it is invisible exactly where you are not looking."""
    tokens = TOKENS.read_text(encoding="utf-8")
    assert "--accent:" in _declaration_block(tokens, ":root"), (
        "light theme does not define --accent"
    )
    assert "--accent:" in _declaration_block(tokens, '[data-theme="dark"]'), (
        "dark theme does not define --accent"
    )


class TestSemanticTokensOnly:
    """E5.1 — one token set, no `!important` overrides fighting a framework.

    Two hacks used to live in the stylesheets, both with the same shape: a rule
    that wins by force rather than by cascade, and whose failure mode is silent.

    * A 26-rule `!important` shim in `tokens.css` remapping `.bg-white` /
      `.text-slate-*`, which existed only because no Tailwind theme defined
      `bg-surface` to migrate *to*.
    * A CopilotKit override in `styles.css` matching elements by the literal
      text of their inline `style` attribute — `[style*="rgb(250, 250, 250)"]`.
      That stops working the moment upstream changes a colour by one digit, and
      the result is an unreadable dark theme rather than a build error.

    Both are gone. `@theme inline` supplies the semantic names, and the
    `--cpk-color-*` variables theme the chat through its supported mechanism.
    """

    def test_no_inline_style_attribute_selectors(self) -> None:
        """Styling by matching a colour literal is a rule waiting to break."""
        for path in (STYLES, TOKENS):
            if not path.is_file():
                continue
            body = path.read_text(encoding="utf-8")
            for line in body.splitlines():
                stripped = line.strip()
                if stripped.startswith("*") or stripped.startswith("/*"):
                    continue  # the comment explaining why it was removed
                assert "[style*=" not in line, (
                    f"{path.name} styles elements by their inline style text: "
                    f"{line.strip()!r} — a one-digit upstream colour change "
                    f"silently disables it"
                )

    def test_important_is_confined_to_reduced_motion(self) -> None:
        """`prefers-reduced-motion` is the one place `!important` is correct.

        Overriding animation for users who asked for less motion must beat
        component styles; everything else should win on specificity.
        """
        body = STYLES.read_text(encoding="utf-8")
        offenders = [
            line.strip()
            for line in body.splitlines()
            if "!important" in line
            and not line.strip().startswith(("*", "/*"))
            and not any(
                prop in line
                for prop in (
                    "animation-duration",
                    "animation-iteration-count",
                    "transition-duration",
                    "scroll-behavior",
                )
            )
        ]
        assert offenders == [], (
            f"`!important` outside the reduced-motion block: {offenders}. Each "
            f"one is a rule that wins by force, and the shim this replaced grew "
            f"to 26 of them."
        )

    def test_theme_defines_the_semantic_names(self) -> None:
        """The shim's removal is only safe if `bg-surface` etc. actually exist."""
        body = STYLES.read_text(encoding="utf-8")
        assert "@theme inline" in body, (
            "no Tailwind theme block — without it there is no `bg-surface` to "
            "migrate to, which is precisely why the !important shim existed"
        )
        for token in ("--color-surface", "--color-ink", "--color-canvas"):
            assert token in body, f"{token} missing from the theme block"
