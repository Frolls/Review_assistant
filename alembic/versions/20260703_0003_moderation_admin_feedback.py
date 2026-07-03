"""moderation admin feedback

Revision ID: 20260703_0003
Revises: 20260701_0002
Create Date: 2026-07-03 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "20260703_0003"
down_revision: str | None = "20260701_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("chat_messages", sa.Column("latency_ms", sa.Integer(), nullable=True))

    op.create_table(
        "moderation_incidents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "chat_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chats.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("direction", sa.Text(), nullable=False),
        sa.Column("categories", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reasons", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("blocked_by", sa.Text(), nullable=False),
        sa.Column("text_hash", sa.Text(), nullable=False),
        sa.Column("text_preview", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_moderation_incidents_created",
        "moderation_incidents",
        [sa.text("created_at DESC")],
    )

    op.create_table(
        "message_feedback",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "message_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("chat_messages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("owner_external_id", sa.Text(), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "owner_external_id",
            "message_id",
            name="uq_message_feedback_owner_message",
        ),
    )
    op.create_index(
        "ix_message_feedback_created",
        "message_feedback",
        [sa.text("created_at DESC")],
    )

    op.create_table(
        "broadcast_queue",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("interface", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default="pending", nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index(
        "ix_broadcast_queue_status_interface",
        "broadcast_queue",
        ["status", "interface"],
    )


def downgrade() -> None:
    op.drop_index("ix_broadcast_queue_status_interface", table_name="broadcast_queue")
    op.drop_table("broadcast_queue")
    op.drop_index("ix_message_feedback_created", table_name="message_feedback")
    op.drop_table("message_feedback")
    op.drop_index("ix_moderation_incidents_created", table_name="moderation_incidents")
    op.drop_table("moderation_incidents")
    op.drop_column("chat_messages", "latency_ms")
