"""Internal data structures passed between pipeline stages.

These are NOT ORM models — they live only in memory during processing.
"""

from dataclasses import dataclass, field
from decimal import Decimal


@dataclass
class LineItemData:
    """One charge line on the invoice."""
    line_number: int
    item_name: str
    item_code: str | None = None
    unit: str = ""
    quantity: Decimal = Decimal("0")
    unit_price: Decimal = Decimal("0")
    amount: Decimal = Decimal("0")            # qty × unit_price (before discount)
    discount_rate: Decimal | None = None      # %
    discount_amount: Decimal | None = None
    tax_rate: Decimal = Decimal("0")          # e.g. 10 (not 0.10)
    tax_amount: Decimal = Decimal("0")
    total_amount: Decimal = Decimal("0")      # after discount + tax
    # Per-field OCR confidence (1.0 for XML bypass path)
    name_confidence: float = 1.0
    qty_confidence: float = 1.0
    unit_confidence: float = 1.0
    price_confidence: float = 1.0


@dataclass
class FieldData:
    """Single extracted field with its confidence score."""
    name: str
    value: str | None
    confidence: float = 1.0
    raw_value: str | None = None   # pre-normalisation value for HITL
    # Pixel bounding box of the OCR region (x1, y1, x2, y2). Stored in
    # FieldConfidence so the training pipeline can crop images without re-running OCR.
    bbox: tuple[int, int, int, int] | None = None


@dataclass
class ExtractionData:
    """All structured data extracted from one document (or all pages of a PDF)."""
    # ── Header ────────────────────────────────────────────────────────────────
    invoice_number: str | None = None
    invoice_date: str | None = None
    invoice_form: str | None = None    # mẫu số
    invoice_series: str | None = None  # ký hiệu
    seller_name: str | None = None
    seller_tax_code: str | None = None
    seller_address: str | None = None
    seller_bank: str | None = None
    buyer_name: str | None = None
    buyer_tax_code: str | None = None
    buyer_address: str | None = None
    payment_method: str | None = None
    currency: str | None = None

    # ── Charges (line items) ──────────────────────────────────────────────────
    line_items: list[LineItemData] = field(default_factory=list)

    # ── Totals ────────────────────────────────────────────────────────────────
    subtotal: Decimal | None = None       # Σ amount before tax
    total_discount: Decimal | None = None
    total_tax: Decimal | None = None
    grand_total: Decimal | None = None    # final payable amount
    amount_in_words: str | None = None


@dataclass
class ValidationError:
    field: str
    message: str
    severity: str = "error"   # error | warning


@dataclass
class PipelineResult:
    extraction: ExtractionData
    field_confidences: list[FieldData]
    validation_errors: list[ValidationError]
    confidence_score: float
    ocr_engine: str                     # xml_bypass | paddleocr | llm_fallback
    processing_time_ms: int
    preprocess_steps: list[str] = field(default_factory=list)

    @property
    def low_confidence_fields(self) -> list[FieldData]:
        return [f for f in self.field_confidences if f.confidence < 0.95]
