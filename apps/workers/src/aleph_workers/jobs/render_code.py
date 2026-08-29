"""render_code_artifact_job — the privileged persistence side of WP-4c.

The sandbox (`aleph-code-runner`) executes agent Python and returns only bytes +
metadata; it never touches Postgres or the asset store. THIS job — which runs in
`aleph-workers` and holds the store + DB — takes that result and materializes it
as a versioned artifact:

  1. enqueue the code job onto the dedicated `code_runner` queue and await it;
  2. on success, persist an `Artifact` + `ArtifactVersion` whose `lineage_jsonb`
     captures the exact `producing_code` (+ its sha256), plus the output sha256
     and `builder_agent_run_id` — every mutation ledgered in the same txn;
  3. pin a catalog card (`ImageCard`/`ChartCard`/`HtmlFrameCard`) that references
     the artifact bytes by streaming-route URI, so it lands in Briefs.

No agent code executes in this credentialed process — `exec` happens only in the
network-less, credential-less sandbox.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Any
from uuid import UUID

from aleph_a2ui.card_service import pin_card
from aleph_artifacts.artifact_service import create_artifact
from aleph_artifacts.models import ArtifactVersion
from aleph_core.ids import uuid7
from aleph_core.time import utcnow
from aleph_db.models.agent import AgentRun
from aleph_db.repos.ledger import LedgerWriter
from aleph_security.agent_token import verify_agent_token
from aleph_security.principal import Principal
from aleph_workers.project_guard import refuse_if_project_is_gone

# Must match aleph_code_runner.executor.CODE_RUNNER_QUEUE. Hardcoded (not
# imported) because the isolated runner package is not installed here.
_CODE_RUNNER_QUEUE = "arq:queue:code_runner"

# output_kind -> (artifact_kind, file extension, storage mime, catalog card)
_KIND_MAP: dict[str, tuple[str, str, str, str]] = {
    "png": ("image", "png", "image/png", "ImageCard"),
    "svg": ("image", "svg", "image/svg+xml", "ImageCard"),
    "vega": ("chart", "json", "application/json", "ChartCard"),
    "html": ("html_frame", "html", "text/html; charset=utf-8", "HtmlFrameCard"),
}


def card_payload(
    card_kind: str,
    *,
    card_id: UUID,
    title: str,
    asset_uri: str,
    inline_spec: dict[str, Any] | None,
) -> dict[str, Any]:
    """The A2UI payload for a pinned code_runner artifact.

    Every branch writes its component name and its props as LITERALS. The
    payload used to be assembled as `{"type": card_kind, "props": props}` with
    both halves held in variables, which is invisible to
    `scripts/check-surface-bindings.sh`: that sweep reads emission sites
    statically, so a runtime component name tells it nothing about which props
    reach which zod schema. This worker is the only producer of `ImageCard` and
    `HtmlFrameCard` anywhere in Aleph, and neither card was ever compared
    against the schema that has to declare its props for the binder to resolve
    them. `ChartCard.artifact_version_id` was shipped here and read by nothing.

    Unknown kinds raise rather than falling through to `HtmlFrameCard`, which is
    what the old `else` did: a new entry in `_KIND_MAP` would have been pinned
    as an interactive HTML frame pointing at bytes of some other type.
    """
    ref = f"pinned-{card_id}"
    if card_kind == "ChartCard":
        return {
            "type": "ChartCard",
            "id": ref,
            "props": {"title": title, "vega_lite_spec": inline_spec or {}},
        }
    if card_kind == "ImageCard":
        return {
            "type": "ImageCard",
            "id": ref,
            "props": {"title": title, "src": asset_uri, "alt": title},
        }
    if card_kind == "HtmlFrameCard":
        return {
            "type": "HtmlFrameCard",
            "id": ref,
            "props": {"title": title, "src": asset_uri},
        }
    msg = f"no card payload for {card_kind!r}"
    raise ValueError(msg)


async def _finish_run(maker: Any, run_id: UUID, *, status: str, error_text: str | None) -> None:
    async with maker() as session:
        run = await session.get(AgentRun, run_id)
        if run is not None:
            run.status = status
            run.completed_at = utcnow()
            run.error_text = error_text
        await session.commit()


async def render_code_artifact_job(
    ctx: dict[str, Any],
    project_id_str: str,
    code: str,
    output_kind: str,
    title: str,
    pin: bool,
    agent_token: str,
    timeout_s: int = 30,
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
    agent_run_id = claims.agent_run_id
    project_id = UUID(project_id_str)
    maker = ctx["session_maker"]

    # Before anything that costs money. See `project_guard`: a deleted
    # project's queued work kept running AND kept enqueueing more.
    refusal = await refuse_if_project_is_gone(maker, project_id)
    if refusal is not None:
        return refusal
    asset_store = ctx["asset_store"]
    # Dedicated code-job Redis (isolated bus the sandbox shares) — NOT the
    # platform pool (which carries tokens + privileged queues the sandbox
    # must never reach). See WP-4c §6 amendment.
    code_runner_pool = ctx["code_runner_pool"]

    if output_kind not in _KIND_MAP:
        await _finish_run(
            maker, agent_run_id, status="failed", error_text=f"bad output_kind: {output_kind}"
        )
        return {"ok": False, "error": f"bad output_kind: {output_kind}"}

    async with maker() as session:
        run = await session.get(AgentRun, agent_run_id)
        if run is not None:
            run.status = "running"
            run.started_at = utcnow()
        await session.commit()

    # --- 1. dispatch to the isolated sandbox and await the result -------------
    try:
        job = await code_runner_pool.enqueue_job(
            "run_code_job", code, output_kind, timeout_s, _queue_name=_CODE_RUNNER_QUEUE
        )
        if job is None:
            raise RuntimeError("code_runner enqueue returned no job")
        result: dict[str, Any] = await job.result(timeout=timeout_s + 30)
    except Exception as exc:
        await _finish_run(maker, agent_run_id, status="failed", error_text=str(exc)[:2048])
        return {"ok": False, "error": str(exc)[:2048]}

    if not result.get("ok"):
        err = str(result.get("error", "code_runner reported failure"))[:2048]
        await _finish_run(maker, agent_run_id, status="failed", error_text=err)
        return {"ok": False, "error": err}

    artifact_kind, ext, mime, card_kind = _KIND_MAP[output_kind]

    # Materialize the bytes the sandbox produced.
    inline_spec: dict[str, Any] | None = None
    if output_kind == "vega":
        inline_spec = result.get("spec_json") or {}
        data = json.dumps(inline_spec, sort_keys=True).encode("utf-8")
    else:
        data = base64.b64decode(result["bytes_b64"])

    producing_code_sha = hashlib.sha256(code.encode("utf-8")).hexdigest()
    sha = hashlib.sha256(data).hexdigest()

    # --- 2. persist as a versioned artifact (ledgered) ------------------------
    key = f"projects/{project_id}/code_runner/{sha[:16]}.{ext}"
    stored = asset_store.put_bytes(key=key, data=data, mime_type=mime)

    async with maker() as session:
        ledger = LedgerWriter(session)
        artifact = await create_artifact(
            session,
            principal=principal,
            project_id=project_id,
            title=title[:512] or "Sandbox artifact",
            artifact_kind=artifact_kind,
            description="code_runner-produced artifact",
        )
        lineage = {
            "producing_code": code,
            "producing_code_sha256": producing_code_sha,
            "output_kind": output_kind,
            "source": "code_runner",
            "commit_message": f"code_runner render {artifact.short_id} v1",
        }
        version_event = await ledger.append(
            project_id=project_id,
            actor_id=principal.user_id,
            actor_kind=principal.actor_kind,
            action_kind="artifact.version.create",
            target_id=artifact.id,
            target_kind="artifact",
            payload={
                "version_no": 1,
                "artifact_kind": artifact_kind,
                "sha256": sha,
                "producing_code_sha256": producing_code_sha,
            },
            trace_id=None,
        )
        version = ArtifactVersion(
            id=uuid7(),
            project_id=project_id,
            artifact_id=artifact.id,
            version_no=1,
            storage_uri=stored.storage_uri,
            bytes_size=len(data),
            sha256=sha,
            parent_version_id=None,
            lineage_jsonb=lineage,
            template_name="code_runner",
            csl_style="",
            builder_agent_run_id=agent_run_id,
            author_kind=principal.actor_kind,
            author_id=principal.user_id,
            trace_id=None,
            ledger_event_id=version_event.id,
        )
        session.add(version)
        await session.flush()
        artifact.current_version_id = version.id

        card_id: UUID | None = None
        if pin:
            card_id = uuid7()
            payload = card_payload(
                card_kind,
                card_id=card_id,
                title=title,
                asset_uri=f"/v1/projects/{project_id}/assets/artifact-version/{version.id}",
                inline_spec=inline_spec,
            )
            pin_event = await ledger.append(
                project_id=project_id,
                actor_id=principal.user_id,
                actor_kind=principal.actor_kind,
                action_kind="card.pin",
                target_id=card_id,
                target_kind="interactive_card",
                payload={
                    "card_kind": card_kind,
                    "title": title,
                    "pinned_to": "briefs",
                    # Provenance lives HERE, not in the card props. It used to
                    # ride along as `ChartCard.artifact_version_id`, which the
                    # A2UI binder drops (no zod declaration reads it) and no
                    # view has ever rendered — a link written into a payload
                    # whose only consumer is the browser, and dropped there.
                    # The ledger is append-only and the Inspector reads it.
                    "artifact_version_id": str(version.id),
                },
                trace_id=None,
            )
            await pin_card(
                session,
                project_id=project_id,
                card_id=card_id,
                card_kind=card_kind,
                title=title,
                payload=payload,
                author_id=principal.user_id,
                author_kind=principal.actor_kind,
                ledger_event_id=pin_event.id,
            )
        await session.commit()
        version_id = str(version.id)
        pinned_card_id = str(card_id) if card_id is not None else None

    await _finish_run(maker, agent_run_id, status="succeeded", error_text=None)
    return {
        "ok": True,
        "artifact_version_id": version_id,
        "card_id": pinned_card_id,
        "card_kind": card_kind,
    }
