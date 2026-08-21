"""Agent-action approval gating (Wave 6 B).

Consequential agent tools (`build_artifact`, `set_connector_enabled`) do
not execute directly. Instead they self-call
`POST /v1/projects/{id}/agent-actions/request`, which creates a *pending*
`ApprovalRequest` (`target_kind="agent_action"`) whose `target_id` is the
request's own id, persisting the action + args server-side in
`proposed_patch_jsonb`. The tool then instructs the agent to render an
`ApprovalCard` pointing at that request.

When the analyst clicks Approve, `/cards/actions` → `ActionRouter._approve`
loads the stored action, validates the tool against a fixed allowlist, and
executes the real effect by self-calling the underlying route. This keeps
rule #8 intact: args are server-persisted and dispatch goes through a fixed
switch, never agent-emitted executable code.

Native `create_deep_agent(interrupt_on=...)` was probed and does NOT surface
an approval UI in this CopilotKit v2 + ag-ui-langgraph stack (the gated tool
executed anyway), so this ApprovalRequest + ApprovalCard path is the gating
mechanism.
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Body, status
from pydantic import BaseModel, Field

from aleph_api.deps import LedgerDep, PrincipalDep, SessionDep
from aleph_api.middleware.project_scope import ProjectScopeDep
from aleph_core.errors import ValidationFailed
from aleph_core.ids import uuid7
from aleph_observability.tracing import current_trace_id
from aleph_reviewer.models import ApprovalRequest
from aleph_security.roles import ProjectRole, require_at_least

router = APIRouter(prefix="/v1/projects", tags=["agent-actions"])

# The only tools an agent-action approval may execute on approve. The approve
# handler dispatches via this allowlist, never via arbitrary agent input.
ALLOWED_AGENT_ACTION_TOOLS = ("build_artifact", "set_connector_enabled")


class AgentActionRequestIn(BaseModel):
    tool: str = Field(min_length=1, max_length=64)
    args: dict[str, Any] = Field(default_factory=dict)
    title: str = Field(min_length=1, max_length=255)
    summary: str = Field(min_length=1, max_length=4096)


class AgentActionRequestOut(BaseModel):
    request_id: UUID
    status: str


@router.post(
    "/{project_id}/agent-actions/request",
    status_code=status.HTTP_201_CREATED,
    response_model=AgentActionRequestOut,
)
async def request_agent_action(
    project_id: ProjectScopeDep,
    body: Annotated[AgentActionRequestIn, Body()],
    session: SessionDep,
    ledger: LedgerDep,
    principal: PrincipalDep,
) -> AgentActionRequestOut:
    """Create a pending approval for a consequential agent action.

    The action (tool + args) is persisted server-side in
    `proposed_patch_jsonb`; the request's `target_id` equals its own id so the
    rendered `ApprovalCard` can address it directly.
    """
    require_at_least(principal, project_id, at_least=ProjectRole.EDITOR)
    if body.tool not in ALLOWED_AGENT_ACTION_TOOLS:
        # Refuse to gate a tool we cannot later execute via the allowlist.
        msg = f"tool {body.tool!r} is not an approvable agent action"
        raise ValidationFailed(msg)

    request_id = uuid7()
    req = ApprovalRequest(
        id=request_id,
        project_id=project_id,
        target_kind="agent_action",
        # target_id == the request's own id so the ApprovalCard addresses it.
        target_id=request_id,
        title=body.title[:255],
        summary=body.summary,
        severity="medium",
        proposed_patch_jsonb={"tool": body.tool, "args": body.args},
        evidence_refs_jsonb=[],
        requested_by_kind=principal.actor_kind,
        requested_by_id=principal.user_id,
        status="pending",
        created_by=principal.user_id,
    )
    session.add(req)
    await session.flush()
    await ledger.append(
        project_id=project_id,
        actor_id=principal.user_id,
        actor_kind=principal.actor_kind,
        action_kind="approval_request.create",
        target_id=request_id,
        target_kind="approval_request",
        payload={
            "target_kind": "agent_action",
            "tool": body.tool,
            "title": body.title,
        },
        trace_id=current_trace_id(),
    )
    return AgentActionRequestOut(request_id=request_id, status="pending")
