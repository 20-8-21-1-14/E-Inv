import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, Numeric, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from einv_common.db.base import Base


class ExtractionResult(Base):
    __tablename__ = "extraction_results"
    __table_args__ = (
        Index("ix_extraction_results_tenant_created", "tenant_id", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    confidence_score: Mapped[float] = mapped_column(Float, nullable=False)
    # Document-level totals — validated against line-item math
    subtotal: Mapped[Decimal | None] = mapped_column(Numeric(15, 2), nullable=True)
    total_tax: Mapped[Decimal | None] = mapped_column(Numeric(15, 2), nullable=True)
    grand_total: Mapped[Decimal | None] = mapped_column(Numeric(15, 2), nullable=True)
    # Raw OCR output and clean validated output
    raw_fields: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    validated_fields: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    validation_errors: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # xml_bypass | paddleocr | llm_fallback
    ocr_engine: Mapped[str] = mapped_column(String(50), nullable=False)
    processing_time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    document: Mapped["Document"] = relationship("Document", back_populates="extraction_result")  # noqa: F821
    line_items: Mapped[list["InvoiceLineItem"]] = relationship(
        "InvoiceLineItem", back_populates="result", cascade="all, delete-orphan"
    )
    field_confidences: Mapped[list["FieldConfidence"]] = relationship(
        "FieldConfidence", back_populates="result", cascade="all, delete-orphan"
    )


class InvoiceLineItem(Base):
    """One row per charge line on the invoice."""
    __tablename__ = "invoice_line_items"
    __table_args__ = (
        Index("ix_line_items_result_id", "result_id"),
        Index("ix_line_items_tenant_created", "tenant_id", "result_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    result_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("extraction_results.id", ondelete="CASCADE"), nullable=False
    )
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    line_number: Mapped[int] = mapped_column(Integer, nullable=False)

    # Core charge fields
    item_name: Mapped[str] = mapped_column(String(500), nullable=False)
    item_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # đơn vị tính: cái, chiếc, kg, m², lít, thùng, chuyến …
    unit: Mapped[str] = mapped_column(String(50), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(15, 4), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    discount_rate: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    discount_amount: Mapped[Decimal | None] = mapped_column(Numeric(15, 2), nullable=True)
    # thuế suất: 0 | 5 | 8 | 10
    tax_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    tax_amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(15, 2), nullable=False)

    # Per-field OCR confidence scores
    name_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    qty_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    price_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)

    # HITL correction tracking
    is_corrected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    corrected_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    corrected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Tracks whether this line item has been exported to training data
    exported: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    result: Mapped["ExtractionResult"] = relationship("ExtractionResult", back_populates="line_items")


class FieldConfidence(Base):
    """Per-field confidence score and HITL correction for non-line-item fields."""
    __tablename__ = "field_confidences"
    __table_args__ = (
        Index("ix_field_confidences_result_id", "result_id"),
        Index("ix_field_confidences_tenant_exported", "tenant_id", "exported"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    result_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("extraction_results.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    field_name: Mapped[str] = mapped_column(String(100), nullable=False)
    raw_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    is_corrected: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    corrected_value: Mapped[str | None] = mapped_column(Text, nullable=True)
    corrected_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    corrected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Tracks whether this correction has been exported to training data
    exported: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    result: Mapped["ExtractionResult"] = relationship(
        "ExtractionResult", back_populates="field_confidences"
    )
