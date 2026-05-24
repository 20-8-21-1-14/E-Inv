"""Initial schema — all tables.

Revision ID: 0001
Revises:
Create Date: 2026-05-24 00:00:00.000000
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── tenants ──────────────────────────────────────────────────────────────
    op.create_table(
        "tenants",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(100), nullable=False),
        sa.Column("confidence_threshold", sa.Float(), nullable=False, server_default="0.95"),
        sa.Column("webhook_url", sa.String(500), nullable=True),
        sa.Column("validation_rules", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )

    # ── admin_users ───────────────────────────────────────────────────────────
    op.create_table(
        "admin_users",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("role", sa.String(30), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
    )
    op.create_index("ix_admin_users_tenant_id", "admin_users", ["tenant_id"])

    # ── api_keys ──────────────────────────────────────────────────────────────
    op.create_table(
        "api_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key_hash", sa.String(64), nullable=False),
        sa.Column("label", sa.String(255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("key_hash"),
    )
    op.create_index("ix_api_keys_tenant_id", "api_keys", ["tenant_id"])

    # ── documents ─────────────────────────────────────────────────────────────
    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("doc_type", sa.String(50), nullable=False),
        sa.Column("source_format", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("file_path", sa.String(500), nullable=False),
        sa.Column("file_hash", sa.String(64), nullable=False),
        sa.Column("task_id", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_documents_tenant_created", "documents", ["tenant_id", "created_at"])
    op.create_index("ix_documents_tenant_status",  "documents", ["tenant_id", "status"])
    op.create_index("ix_documents_file_hash_tenant", "documents", ["file_hash", "tenant_id"])

    # ── hitl_queue ────────────────────────────────────────────────────────────
    op.create_table(
        "hitl_queue",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reason", sa.String(50), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("assigned_to", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"],   ["tenants.id"],   ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["assigned_to"], ["admin_users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_hitl_queue_document_id", "hitl_queue", ["document_id"])
    op.create_index(
        "ix_hitl_tenant_status_created", "hitl_queue",
        ["tenant_id", "status", "created_at"],
    )

    # ── extraction_results ────────────────────────────────────────────────────
    op.create_table(
        "extraction_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("confidence_score", sa.Float(), nullable=False),
        sa.Column("subtotal",   sa.Numeric(15, 2), nullable=True),
        sa.Column("total_tax",  sa.Numeric(15, 2), nullable=True),
        sa.Column("grand_total", sa.Numeric(15, 2), nullable=True),
        sa.Column("raw_fields",        postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("validated_fields",  postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("validation_errors", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("ocr_engine", sa.String(50), nullable=False),
        sa.Column("processing_time_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"],   ["tenants.id"],   ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_extraction_results_document_id", "extraction_results", ["document_id"])
    op.create_index(
        "ix_extraction_results_tenant_created", "extraction_results",
        ["tenant_id", "created_at"],
    )

    # ── invoice_line_items ────────────────────────────────────────────────────
    op.create_table(
        "invoice_line_items",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("result_id",   postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id",   postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("line_number", sa.Integer(), nullable=False),
        sa.Column("item_name",  sa.String(500), nullable=False),
        sa.Column("item_code",  sa.String(50),  nullable=True),
        sa.Column("unit",       sa.String(50),  nullable=False),
        sa.Column("quantity",    sa.Numeric(15, 4), nullable=False),
        sa.Column("unit_price",  sa.Numeric(15, 2), nullable=False),
        sa.Column("amount",      sa.Numeric(15, 2), nullable=False),
        sa.Column("discount_rate",   sa.Numeric(5, 2),  nullable=True),
        sa.Column("discount_amount", sa.Numeric(15, 2), nullable=True),
        sa.Column("tax_rate",    sa.Numeric(5, 2),  nullable=False),
        sa.Column("tax_amount",  sa.Numeric(15, 2), nullable=False),
        sa.Column("total_amount", sa.Numeric(15, 2), nullable=False),
        sa.Column("name_confidence",  sa.Float(), nullable=True),
        sa.Column("qty_confidence",   sa.Float(), nullable=True),
        sa.Column("unit_confidence",  sa.Float(), nullable=True),
        sa.Column("price_confidence", sa.Float(), nullable=True),
        sa.Column("is_corrected",  sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("corrected_by",  sa.String(255), nullable=True),
        sa.Column("corrected_at",  sa.DateTime(timezone=True), nullable=True),
        sa.Column("exported", sa.Boolean(), nullable=False, server_default="false"),
        sa.ForeignKeyConstraint(["result_id"],   ["extraction_results.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"],          ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"],   ["tenants.id"],            ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_line_items_result_id", "invoice_line_items", ["result_id"])
    op.create_index(
        "ix_line_items_tenant_created", "invoice_line_items",
        ["tenant_id", "result_id"],
    )

    # ── field_confidences ─────────────────────────────────────────────────────
    op.create_table(
        "field_confidences",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("result_id",  postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id",  postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("field_name",      sa.String(100), nullable=False),
        sa.Column("raw_value",       sa.Text(),   nullable=True),
        sa.Column("confidence",      sa.Float(),  nullable=False),
        sa.Column("is_corrected",    sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("corrected_value", sa.Text(),   nullable=True),
        sa.Column("corrected_by",    sa.String(255), nullable=True),
        sa.Column("corrected_at",    sa.DateTime(timezone=True), nullable=True),
        sa.Column("exported", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("bbox", postgresql.JSONB(), nullable=True),
        sa.ForeignKeyConstraint(["result_id"], ["extraction_results.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"],            ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_field_confidences_result_id", "field_confidences", ["result_id"])
    op.create_index(
        "ix_field_confidences_tenant_exported", "field_confidences",
        ["tenant_id", "exported"],
    )

    # ── audit_log ─────────────────────────────────────────────────────────────
    op.create_table(
        "audit_log",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("tenant_id",   postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.String(50),  nullable=False),
        sa.Column("actor",  sa.String(255), nullable=False),
        sa.Column("details", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["tenant_id"],   ["tenants.id"],   ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_log_tenant_created", "audit_log", ["tenant_id", "created_at"])
    op.create_index("ix_audit_log_document_id",    "audit_log", ["document_id"])

    # ── column_alias_proposals ────────────────────────────────────────────────
    op.create_table(
        "column_alias_proposals",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("unmatched_header", sa.String(500), nullable=False),
        sa.Column("doc_type", sa.String(50), nullable=False, server_default="vat_invoice"),
        sa.Column("seen_count",    sa.Integer(), nullable=False, server_default="1"),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("last_seen_at",  sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("suggested_field", sa.String(100), nullable=True),
        sa.Column("reviewed_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["reviewed_by"], ["admin_users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("unmatched_header", "doc_type", name="uq_proposal_header_doctype"),
    )
    op.create_index(
        "ix_proposals_status_created", "column_alias_proposals",
        ["status", "first_seen_at"],
    )

    # ── schema_versions ───────────────────────────────────────────────────────
    op.create_table(
        "schema_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version",   sa.String(20),  nullable=False),
        sa.Column("content",   postgresql.JSONB(), nullable=False),
        sa.Column("changelog", sa.Text(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["created_by"], ["admin_users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("version"),
    )
    op.create_index("ix_schema_versions_active", "schema_versions", ["is_active"])

    # ── model_versions ────────────────────────────────────────────────────────
    op.create_table(
        "model_versions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("model_type", sa.String(20),  nullable=False),
        sa.Column("version",    sa.String(100), nullable=False),
        sa.Column("minio_key",  sa.String(500), nullable=False),
        sa.Column("metrics", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("training_samples", sa.Integer(), nullable=True),
        sa.Column("mlflow_run_id",    sa.String(255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("now()")),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("promoted_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.ForeignKeyConstraint(["promoted_by"], ["admin_users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_model_versions_type_active", "model_versions",
        ["model_type", "is_active"],
    )


def downgrade() -> None:
    op.drop_table("model_versions")
    op.drop_table("schema_versions")
    op.drop_table("column_alias_proposals")
    op.drop_table("audit_log")
    op.drop_table("field_confidences")
    op.drop_table("invoice_line_items")
    op.drop_table("extraction_results")
    op.drop_table("hitl_queue")
    op.drop_table("documents")
    op.drop_table("api_keys")
    op.drop_table("admin_users")
    op.drop_table("tenants")
