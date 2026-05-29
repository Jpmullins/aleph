"""EditorialReviewer — scheduled + threshold-triggered Deep Agents-style workflow.

Inc 5 ships the LangGraph workflow with subagents in series. The
Deep-Agents harness (subagent + planning + HITL) can be swapped in
when the `deepagents` PyPI package lands a stable release that matches
our framework set; the workflow contract here is the same.
"""

from aleph_reviewer.editorial.workflow import (
    EditorialReviewerWorkflow,
    EditorialReviewState,
)

__all__ = ["EditorialReviewState", "EditorialReviewerWorkflow"]
