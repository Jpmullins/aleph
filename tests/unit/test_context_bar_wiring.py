"""The context bar must stay mounted, and must stay a *mirror*.

Its whole value is that it shows the analyst the exact payload the agent
receives — so two things can quietly ruin it:

1. **Unmounting it.** A component that exists but is not rendered is this
   codebase's signature failure; nothing else in the gate suite would notice.
2. **Giving it its own data source.** The moment it fetches, it can disagree
   with what the agent was actually sent, which is worse than showing nothing:
   the analyst would trust a number that is wrong.

Nothing else guards (2) for anything under `components/` — the sweep that once
covered `a2ui/components/` was deleted with the rest of them. These tests close
both.
"""

from __future__ import annotations

import pathlib
import re

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
WEB = REPO_ROOT / "apps" / "web" / "src"
BAR = WEB / "components" / "ContextBar.tsx"
WORKSPACE = WEB / "components" / "ProjectWorkspace.tsx"
CHAT = WEB / "components" / "CopilotChatSurface.tsx"


def test_component_exists() -> None:
    assert BAR.is_file(), f"context bar not found at {BAR}"


def test_it_is_actually_mounted() -> None:
    """Defined-but-unrendered is indistinguishable from absent to the user."""
    src = WORKSPACE.read_text(encoding="utf-8")
    assert "ContextBar" in src, "ContextBar is never imported by the workspace"
    assert re.search(r"<ContextBar\b", src), (
        "ContextBar is imported but never rendered — the analyst still cannot "
        "see what the assistant sees."
    )


@pytest.mark.parametrize(
    "pattern",
    [r"useQuery", r"useMutation", r"refetchInterval", r"EventSource", r"\bfetch\(", r"api\."],
)
def test_it_does_not_fetch(pattern: str) -> None:
    """It must mirror shared state, never source its own.

    A second source of truth can disagree with what was actually sent to the
    agent, and the analyst has no way to tell which is right.
    """
    src = BAR.read_text(encoding="utf-8")
    assert not re.search(pattern, src), (
        f"ContextBar contains {pattern!r}; it must render only from "
        f"WorkspaceUIProvider so it cannot drift from the agent's context."
    )


def test_it_reads_the_same_state_the_agent_is_sent() -> None:
    """Every field in the agent payload should be reflected somewhere.

    If the two lists diverge, the bar is showing a *different* context than the
    one the assistant acts on — the exact confusion it exists to remove.
    """
    bar = BAR.read_text(encoding="utf-8")
    for field in ("activeSurface", "openPageTitle", "selection"):
        assert field in bar, f"ContextBar does not surface {field!r}"

    chat = CHAT.read_text(encoding="utf-8")
    for field in ("active_tab", "open_page_title", "selection"):
        assert field in chat, (
            f"{field!r} is no longer in the agent context payload — the bar and "
            f"the agent have drifted apart."
        )


def test_it_uses_theme_tokens_not_raw_palette() -> None:
    """New chrome must not add to the `!important` dark-mode shim's debt."""
    src = BAR.read_text(encoding="utf-8")
    raw = re.findall(r"\b(?:bg|text|border)-slate-\d{2,3}\b", src)
    assert not raw, (
        f"ContextBar uses raw palette classes {sorted(set(raw))}; these only "
        f"render correctly in dark mode via the `!important` shim in "
        f"tokens.css. Use var(--…) tokens."
    )
