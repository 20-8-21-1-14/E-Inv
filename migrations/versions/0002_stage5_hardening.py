"""Stage 5 hardening: webhook_deliveries table, quota columns, retry tracking.

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-19
"""
from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── tenants: quota + webhook secret ─────────────────────────────────────
    op.add_column("tenants", sa.Column("webhook_secret", sa.String(64), nullable=True))
    op.add_column("tenants", sa.Column("quota_max_docs", sa.Integer(), nullable=True))
    op.add_column("tenants", sa.Column("quota_window_seconds", sa.Integer(), nullable=True))

    # ── documents: retry tracking ────────────────────────────────────────────
    op.add_column(
        "documents",
        sa.Column("processing_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column("documents", sa.Column("last_retry_at", sa.DateTime(timezone=True), nullable=True))

    # ── webhook_deliveries ───────────────────────────────────────────────────
    op.create_table(
        "webhook_deliveries",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_id", sa.String(36), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("response_body_truncated", sa.Text(), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.CheckConstraint("attempt > 0", name="ck_webhook_deliveries_attempt_positive"),
        sa.CheckConstraint(
            "http_status IS NULL OR (http_status >= 100 AND http_status <= 599)",
            name="ck_webhook_deliveries_http_status_range",
        ),
    )
    op.create_index(
        "ix_webhook_deliveries_tenant_doc", "webhook_deliveries", ["tenant_id", "document_id"]
    )
    op.create_index(
        "ix_webhook_deliveries_doc_attempt", "webhook_deliveries", ["document_id", "attempt"]
    )
    op.create_index(
        "ix_webhook_deliveries_status_retry", "webhook_deliveries", ["status", "next_retry_at"]
    )


def downgrade() -> None:
    op.drop_table("webhook_deliveries")
    op.drop_column("documents", "last_retry_at")
    op.drop_column("documents", "processing_attempts")
    op.drop_column("tenants", "quota_window_seconds")
    op.drop_column("tenants", "quota_max_docs")
    op.drop_column("tenants", "webhook_secret")
