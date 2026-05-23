import uuid
from datetime import datetime
from pydantic import BaseModel, ConfigDict


class DocumentCreate(BaseModel):
    doc_type: str = "vat_invoice"


class DocumentStatus(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    tenant_id: uuid.UUID
    doc_type: str
    source_format: str
    status: str
    task_id: str | None
    created_at: datetime
    processed_at: datetime | None


class DocumentOut(DocumentStatus):
    file_hash: str
