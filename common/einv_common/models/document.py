import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from einv_common.db.base import Base


class Document(Base):
    __tablename__ = "documents"
    __table_args__ = (
        Index("ix_documents_tenant_created", "tenant_id", "created_at"),
        Index("ix_documents_tenant_status", "tenant_id", "status"),
        Index("ix_documents_file_hash_tenant", "file_hash", "tenant_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
    )
    # vat_invoice | freight_invoice | bol | packing_list | pod
    doc_type: Mapped[str] = mapped_column(String(50), nullable=False)
    # xml | pdf | image
    source_format: Mapped[str] = mapped_column(String(20), nullable=False)
    # queued | processing | done | hitl | error
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")
    file_path: Mapped[str] = mapped_column(String(500), nullable=False)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    task_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    extraction_result: Mapped["ExtractionResult | None"] = relationship(  # noqa: F821
        "ExtractionResult", back_populates="document", uselist=False
    )
