"""aiq_synthesis_poll_job: AIQ research report → wiki synthesis proposal.

`/synthesize` dispatches an AIQ research job and enqueues this job. It polls
the AIQ job to completion, fetches the report + citation sources, parses them
into the `AIQReport` the synthesis workflow expects, and runs that workflow —
which lands a draft wiki page and a pending `SynthesisProposal` (surfaced in
Briefs for approval). The AgentRun status is updated throughout.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

from sqlalchemy import select

from aleph_aiq.client import AIQClient, AIQJobStatus
from aleph_core.time import utcnow
from aleph_db.models.agent import AgentRun
from aleph_observability.tracing import start_span
from aleph_security.agent_token import verify_agent_token
from aleph_security.principal import Principal
from aleph_wiki.synthesis_workflow import (
    AIQReport,
    AIQReportSourceRef,
    SynthesisWorkflow,
)

# Long-job polling without tying up a worker: each invocation checks the AIQ
# job once and, if it's still running, re-enqueues itself after a delay. Deep
# research can run many minutes, so a sleep-loop bounded by arq's job_timeout
# isn't enough — re-enqueue has no total-time ceiling (capped only by
# _MAX_ATTEMPTS as a runaway backstop: 30 attempts x 20s ≈ 10 min default,
# extended for deep below).
_DEFER_S = 20
_MAX_ATTEMPTS = 90  # 90 x 20s = 30 min hard ceiling
_TERMINAL = {
    AIQJobStatus.SUCCEEDED,
    AIQJobStatus.FAILED,
    AIQJobStatus.CANCELLED,
}
_NUMERIC_MARKER_RE = re.compile(r"\[(\d+)\]")


def _parse_report(
    *, topic: str, report_md: str, artifacts: dict[str, Any]
) -> AIQReport:
    """Build an AIQReport from AIQ's report markdown + citation artifacts.

    AIQ emits numeric `[1]` citation markers and a list of `citation_source`
    artifacts (in citation order). We map each source to a `cN` marker and
    rewrite `[N]` → `[cN]` for the sources we actually have, so Aleph's
    citation verification links them. Markers beyond the captured source count
    are left numeric (and simply ignored by verification — never blocking).
    """
    outputs = artifacts.get("outputs") or []
    cite_outputs = [
        o for o in outputs if isinstance(o, dict) and o.get("type") == "citation_source"
    ]
    sources: list[AIQReportSourceRef] = []
    citations_by_marker: dict[str, AIQReportSourceRef] = {}
    for i, o in enumerate(cite_outputs, start=1):
        url = o.get("url") or o.get("content")
        name = o.get("name") or url or f"Source {i}"
        ref = AIQReportSourceRef(
            source_short_id=f"S{i}", title=str(name)[:240], url=str(url) if url else None
        )
        sources.append(ref)
        citations_by_marker[f"c{i}"] = ref

    n = len(sources)

    def _remap(m: re.Match[str]) -> str:
        k = int(m.group(1))
        return f"[c{k}]" if 1 <= k <= n else m.group(0)

    body_md = _NUMERIC_MARKER_RE.sub(_remap, report_md)

    # Summary: first non-heading paragraph, trimmed.
    summary = topic
    for block in report_md.split("\n\n"):
        t = block.strip()
        if t and not t.startswith("#"):
            summary = t.replace("\n", " ")[:2048]
            break

    return AIQReport(
        topic=topic,
        body_md=body_md,
        summary=summary,
        sources=sources,
        citations_by_marker=citations_by_marker,
        claims=[],
    )


async def _set_run_status(
    maker: Any, agent_run_id: UUID, status: str, error: str | None = None
) -> None:
    async with maker() as session:
        run = (
            await session.execute(select(AgentRun).where(AgentRun.id == agent_run_id))
        ).scalar_one_or_none()
        if run is None:
            return
        run.status = status
        if status in ("succeeded", "failed", "cancelled"):
            run.completed_at = utcnow()
        if error:
            run.error_text = error[:4096]
        await session.commit()


async def aiq_synthesis_poll_job(
    ctx: dict[str, Any],
    agent_run_id_str: str,
    aiq_job_id: str,
    project_id_str: str,
    topic: str,
    agent_token: str,
    attempt: int = 0,
) -> dict[str, Any]:
    secret: str = ctx["agent_token_secret"]
    claims = verify_agent_token(agent_token, secret=secret)
    principal = Principal(
        user_id=claims.user_id,
        subject="agent",
        email="",
        actor_kind=claims.actor_kind,
        agent_run_id=claims.agent_run_id,
        correlation_id=claims.correlation_id,
    )
    agent_run_id = UUID(agent_run_id_str)
    project_id = UUID(project_id_str)
    maker = ctx["session_maker"]
    litellm = ctx["litellm_client"]
    settings = ctx["settings"]

    with start_span(
        "worker.aiq_synthesis_poll",
        **{
            "aleph.agent_run_id": str(agent_run_id),
            "aleph.project_id": str(project_id),
            "aleph.aiq_job_id": aiq_job_id,
            "aleph.attempt": attempt,
        },
    ):
        if attempt == 0:
            await _set_run_status(maker, agent_run_id, "running")
        client = AIQClient(base_url=settings.aiq_base_url, service_token=agent_token)

        # One status check; re-enqueue ourselves if the job is still running so
        # we never hold a worker through a multi-minute deep-research run.
        job = await client.get_job(aiq_job_id)
        status = job.status
        if status not in _TERMINAL:
            if attempt + 1 >= _MAX_ATTEMPTS:
                await _set_run_status(
                    maker,
                    agent_run_id,
                    "failed",
                    error=f"AIQ job still {status} after {_MAX_ATTEMPTS} polls",
                )
                return {"status": "failed", "reason": "poll_timeout"}
            await ctx["redis_pool"].enqueue_job(
                "aiq_synthesis_poll_job",
                agent_run_id_str,
                aiq_job_id,
                project_id_str,
                topic,
                agent_token,
                attempt + 1,
                _defer_by=_DEFER_S,
            )
            return {"status": "polling", "attempt": attempt, "aiq_status": str(status)}

        if status is not AIQJobStatus.SUCCEEDED:
            await _set_run_status(
                maker, agent_run_id, "failed", error=f"AIQ job {status}"
            )
            return {"status": "failed", "aiq_status": str(status)}

        report_md = await client.get_report(aiq_job_id)
        if not report_md:
            await _set_run_status(
                maker, agent_run_id, "failed", error="AIQ job produced no report"
            )
            return {"status": "failed", "reason": "no_report"}
        artifacts = await client.get_state(aiq_job_id)

        report = _parse_report(topic=topic, report_md=report_md, artifacts=artifacts)

        workflow = SynthesisWorkflow(
            session_maker=maker, litellm=litellm, principal=principal
        )
        try:
            out = await workflow.run(
                {
                    "agent_run_id": agent_run_id,
                    "project_id": project_id,
                    "topic": topic,
                    "aiq_report": report,
                }
            )
        except Exception as exc:
            await _set_run_status(maker, agent_run_id, "failed", error=str(exc))
            raise

        proposal_ids = out.get("proposal_ids") or []
        await _set_run_status(maker, agent_run_id, "succeeded")
        return {
            "status": "succeeded",
            "proposal_ids": [str(p) for p in proposal_ids],
            "source_count": len(report.sources),
        }
