import uuid
from datetime import datetime
from pydantic import BaseModel, ConfigDict


class HitlItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    document_id: uuid.UUID
    tenant_id: uuid.UUID
    reason: str
    status: str
    assigned_to: str | None
    notes: str | None
    created_at: datetime
    resolved_at: datetime | None


class FieldCorrectionIn(BaseModel):
    field_name: str
    corrected_value: str


class LineItemCorrectionIn(BaseModel):
    line_item_id: uuid.UUID
    item_name: str | None = None
    item_code: str | None = None
    unit: str | None = None
    quantity: float | None = None
    unit_price: float | None = None
    tax_rate: float | None = None


class HitlCorrectionIn(BaseModel):
    field_corrections: list[FieldCorrectionIn] = []
    line_item_corrections: list[LineItemCorrectionIn] = []
