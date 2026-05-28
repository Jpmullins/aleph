"""UserFeedback model.

Inline analyst signals on claims, sources, charts, findings,
hypotheses, assistant messages, and wiki pages. High-signal feedback
(`signal=marked_wrong` with a `note`) becomes a regression eval case
via the Inc 8 UserFeedback → EvalCase pipeline.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from aleph_db.base import Base, CommonColumns


class UserFeedback(CommonColumns, Base):
    __tablename__ = "user_feedback"

    project_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    target_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    target_id: Mapped[UUID] = mapped_column(nullable=False, index=True)
    signal: Mapped[str] = mapped_column(String(32), nullable=False)
    # thumbs_up | thumbs_down | marked_wrong | misleading | false_positive |
    # excellent | note_only
    note: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    severity: Mapped[str | None] = mapped_column(String(16), nullable=True)
    context_jsonb: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    # surface-specific extras: e.g. which claim citation was wrong, which
    # source seemed misattributed, the message_id when target_kind=claim
    promoted_to_eval_case_id: Mapped[UUID | None] = mapped_column(nullable=True)
