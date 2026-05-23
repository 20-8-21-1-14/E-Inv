import uuid
from datetime import datetime
from decimal import Decimal
from pydantic import BaseModel, ConfigDict


class LineItemOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    line_number: int
    item_name: str
    item_code: str | None
    unit: str
    quantity: Decimal
    unit_price: Decimal
    amount: Decimal
    discount_rate: Decimal | None
    discount_amount: Decimal | None
    tax_rate: Decimal
    tax_amount: Decimal
    total_amount: Decimal
    name_confidence: float | None
    qty_confidence: float | None
    unit_confidence: float | None
    price_confidence: float | None
    is_corrected: bool


class FieldConfidenceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    field_name: str
    raw_value: str | None
    confidence: float
    is_corrected: bool
    corrected_value: str | None


class ExtractionResultOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    document_id: uuid.UUID
    confidence_score: float
    subtotal: Decimal | None
    total_tax: Decimal | None
    grand_total: Decimal | None
    validated_fields: dict
    validation_errors: list
    ocr_engine: str
    processing_time_ms: int | None
    created_at: datetime
    line_items: list[LineItemOut]
    field_confidences: list[FieldConfidenceOut]
