"""Training lifecycle DB models.

Three tables track the feedback loop from production → retraining:

  ColumnAliasProposal — column headers that failed to map in production,
      collected for admin review so they can be added to label_schema.

  SchemaVersion — immutable snapshots of label_schema.json with a version tag.
      Active version drives column_mapper hot-reload.

  ModelVersion — versioned OCR model binaries stored in MinIO.
      Tracks accuracy metrics and which version is currently serving.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean, DateTime, Float, ForeignKey, Index,
    Integer, String, Text, UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from einv_common.db.base import Base


class ColumnAliasProposal(Base):
    """Unmatched column header seen in production — awaits admin approval.

    When column_mapper fails to resolve a header, the raw text is recorded here.
    An admin can then assign a canonical field name and the text is promoted to
    label_schema (triggering a new SchemaVersion).
    """
    __tablename__ = "column_alias_proposals"
    __table_args__ = (
        UniqueConstraint("unmatched_header", "doc_type", name="uq_proposal_header_doctype"),
        Index("ix_proposals_status_created", "status", "first_seen_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Raw text exactly as OCR returned it
    unmatched_header: Mapped[str] = mapped_column(String(500), nullable=False)
    # Which document type it appeared in (vat_invoice, freight_invoice, …)
    doc_type: Mapped[str] = mapped_column(String(50), nullable=False, default="vat_invoice")
    # How many times this exact header was seen
    seen_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    # pending | approved | rejected
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    # Canonical field name chosen by admin (item_name, unit_price, …)
    suggested_field: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # FK to admin_users.id — who approved or rejected
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class SchemaVersion(Base):
    """Immutable snapshot of label_schema.json.

    Each admin-approved alias addition creates a new version.
    Only one version is active at a time (is_active=True).
    The column_mapper hot-reload fetches the current active version
    and refreshes its in-process cache every OCR_SCHEMA_TTL seconds.
    """
    __tablename__ = "schema_versions"
    __table_args__ = (
        Index("ix_schema_versions_active", "is_active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Semantic version string: "1.0", "1.1", "2.0" …
    version: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    # Full label_schema content (same structure as label_schema.json)
    content: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # Human-readable change summary
    changelog: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ModelVersion(Base):
    """Versioned OCR model stored in MinIO.

    Model types: det (text detection), rec (text recognition),
    table (SLANet table structure), layout (PP-Layout).

    One version per type is active (is_active=True).
    The ocr_worker loads the active version at startup; hot-reload
    is triggered by a Celery beat task that polls this table.
    """
    __tablename__ = "model_versions"
    __table_args__ = (
        Index("ix_model_versions_type_active", "model_type", "is_active"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # det | rec | table | layout | sr
    model_type: Mapped[str] = mapped_column(String(20), nullable=False)
    # e.g., "v1.0.0", "v1.1.0-rec-finetune-2026-05-24"
    version: Mapped[str] = mapped_column(String(100), nullable=False)
    # MinIO key: e-invoice-models/rec/v1.1.0/inference.pdmodel
    minio_key: Mapped[str] = mapped_column(String(500), nullable=False)
    # Accuracy metrics logged at evaluation time
    metrics: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # Number of training samples used
    training_samples: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # MLflow run ID for experiment tracking
    mlflow_run_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    promoted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    promoted_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )
