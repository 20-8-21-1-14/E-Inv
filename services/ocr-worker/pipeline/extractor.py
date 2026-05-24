"""Structured field extractor.

Converts OCRPageResult (tables + text blocks) into ExtractionData by:
  1. Identifying the charge table (the one with item/qty/price columns)
  2. Mapping column headers via column_mapper
  3. Parsing each data row into LineItemData
  4. Extracting header fields from surrounding text blocks
  5. Extracting totals from footer text blocks

All numeric parsing is Decimal-safe.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import NamedTuple

import structlog

from pipeline.column_mapper import map_headers
from pipeline.models import ExtractionData, FieldData, LineItemData
from pipeline.ocr_engine import OCRPageResult, TableCell, TableRegion, TextBlock

logger = structlog.get_logger()

# Minimum columns a table must map to be considered the charge table
_MIN_CHARGE_COLS = 2
# Required canonical fields — at least one must be present
_CHARGE_ANCHORS = {"item_name", "unit_price", "amount", "total_amount"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _to_decimal(text: str | None) -> Decimal | None:
    if not text:
        return None
    cleaned = re.sub(r"[^\d.\-,]", "", text).replace(",", ".")
    # Handle Vietnamese thousands separator (dot): "1.234.567" → "1234567"
    parts = cleaned.split(".")
    if len(parts) > 2:
        # All but last are thousands groups
        cleaned = "".join(parts[:-1]) + "." + parts[-1]
    elif len(parts) == 2 and len(parts[-1]) == 3 and len(parts[0]) <= 3:
        # Ambiguous: "100.000" — treat as integer (thousands separator)
        cleaned = "".join(parts)
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _to_decimal_req(text: str | None, fallback: Decimal = Decimal("0")) -> Decimal:
    return _to_decimal(text) or fallback


def _clean_tax_rate(raw: str | None) -> Decimal:
    if not raw:
        return Decimal("0")
    raw = raw.strip().replace("%", "").upper()
    if raw in ("KK", "KKKK", "KCT", "KHAC"):
        return Decimal("0")
    return _to_decimal_req(raw)


def _cell_text(cells: list[TableCell], row: int, col: int) -> str:
    for c in cells:
        if c.row == row and c.col == col:
            return c.text.strip()
    return ""


def _cell_conf(cells: list[TableCell], row: int, col: int) -> float:
    for c in cells:
        if c.row == row and c.col == col:
            return c.confidence
    return 1.0


# ---------------------------------------------------------------------------
# Charge table detection
# ---------------------------------------------------------------------------

class _ColMap(NamedTuple):
    mapping: dict[int, str]   # col_index → canonical field
    header_row: int
    unmatched: list[str]      # raw headers that failed to map


def _find_charge_table(tables: list[TableRegion]) -> tuple[TableRegion, _ColMap] | None:
    """Return the table most likely to be the charge/line-item table."""
    best: tuple[TableRegion, _ColMap] | None = None
    best_score = 0

    for table in tables:
        if not table.cells:
            continue
        max_row = max(c.row for c in table.cells)
        # Try rows 0, 1, and 2 as candidate header rows (some invoices have a merged title row)
        for hrow in range(min(3, max_row + 1)):
            header_cells = [c for c in table.cells if c.row == hrow]
            header_texts = [c.text for c in sorted(header_cells, key=lambda c: c.col)]
            if not header_texts:
                continue
            mapping, unmatched = map_headers(header_texts)
            if len(mapping) < _MIN_CHARGE_COLS:
                continue
            if not (_CHARGE_ANCHORS & set(mapping.values())):
                continue
            score = len(mapping)
            if score > best_score:
                best_score = score
                best = (table, _ColMap(mapping=mapping, header_row=hrow, unmatched=unmatched))

    return best


# ---------------------------------------------------------------------------
# Row parsing
# ---------------------------------------------------------------------------

def _parse_charge_rows(
    table: TableRegion,
    col_map: _ColMap,
) -> tuple[list[LineItemData], list[FieldData]]:
    """Parse data rows of the charge table into LineItemData list."""
    items: list[LineItemData] = []
    confidences: list[FieldData] = []

    max_row = max(c.row for c in table.cells)
    line_counter = 1

    for row_idx in range(col_map.header_row + 1, max_row + 1):
        row_cells = {c.col: c for c in table.cells if c.row == row_idx}
        if not row_cells:
            continue

        def txt(field: str) -> str:
            col = _find_col(col_map.mapping, field)
            return row_cells[col].text.strip() if col is not None and col in row_cells else ""

        def conf(field: str) -> float:
            col = _find_col(col_map.mapping, field)
            return row_cells[col].confidence if col is not None and col in row_cells else 1.0

        item_name = txt("item_name")
        if not item_name:
            continue  # Skip empty / summary rows

        try:
            line_num_raw = txt("line_number")
            line_num = int(line_num_raw) if line_num_raw else line_counter
        except ValueError:
            line_num = line_counter

        qty   = _to_decimal_req(txt("quantity"))
        price = _to_decimal_req(txt("unit_price"))
        amt   = _to_decimal_req(txt("amount")) or (qty * price)
        tax_r = _clean_tax_rate(txt("tax_rate"))
        tax_a = _to_decimal_req(txt("tax_amount")) or (amt * tax_r / Decimal("100"))
        disc_r = _to_decimal(txt("discount_rate"))
        disc_a = _to_decimal(txt("discount_amount"))
        total  = _to_decimal_req(txt("total_amount")) or (
            amt - (disc_a or Decimal("0")) + tax_a
        )

        name_c  = conf("item_name")
        qty_c   = conf("quantity")
        unit_c  = conf("unit")
        price_c = conf("unit_price")

        item = LineItemData(
            line_number=line_num,
            item_name=item_name,
            item_code=txt("item_code") or None,
            unit=txt("unit"),
            quantity=qty,
            unit_price=price,
            amount=amt,
            discount_rate=disc_r,
            discount_amount=disc_a,
            tax_rate=tax_r,
            tax_amount=tax_a,
            total_amount=total,
            name_confidence=name_c,
            qty_confidence=qty_c,
            unit_confidence=unit_c,
            price_confidence=price_c,
        )
        items.append(item)
        line_counter += 1

        # Record per-field confidences for scoring
        for field_name, c_val in [
            ("item_name", name_c), ("quantity", qty_c),
            ("unit", unit_c), ("unit_price", price_c),
        ]:
            confidences.append(FieldData(
                name=f"line_{line_num}.{field_name}",
                value=str(getattr(item, field_name.replace(".", "_"))),
                confidence=c_val,
            ))

    return items, confidences


def _find_col(mapping: dict[int, str], field: str) -> int | None:
    for col, f in mapping.items():
        if f == field:
            return col
    return None


# ---------------------------------------------------------------------------
# Header / footer extraction from text blocks
# ---------------------------------------------------------------------------

_HEADER_PATTERNS: dict[str, list[str]] = {
    "invoice_number": [
        r"(?:số|s[oô])\s*[:：]?\s*(\w[\w\-/]+)",
        r"invoice\s*(?:no|number)\s*[:：]?\s*(\w[\w\-/]+)",
    ],
    "invoice_date": [
        r"ngày\s+(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4})",
        r"date\s*[:：]\s*(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4})",
    ],
    "seller_name": [
        r"(?:đơn vị bán|người bán|công ty|seller)\s*[:：]\s*(.+)",
    ],
    "seller_tax_code": [
        r"(?:mã số thuế|mst|tax\s*code)\s*[:：]\s*([\d\-]+)",
    ],
    "buyer_name": [
        r"(?:đơn vị mua|người mua|buyer)\s*[:：]\s*(.+)",
    ],
    "buyer_tax_code": [
        r"(?:mã số thuế.*mua|buyer.*tax)\s*[:：]\s*([\d\-]+)",
    ],
    "grand_total": [
        r"(?:tổng\s+cộng|tổng\s+thanh\s+toán|total)\s*[:：]\s*([\d,\.]+)",
    ],
    "amount_in_words": [
        r"(?:số tiền bằng chữ|bằng chữ|in words)\s*[:：]\s*(.+)",
    ],
}


def _extract_from_text(text_blocks: list[TextBlock]) -> tuple[ExtractionData, list[FieldData]]:
    all_text = "\n".join(b.text for b in text_blocks)
    avg_conf = (sum(b.confidence for b in text_blocks) / len(text_blocks)) if text_blocks else 1.0

    data = ExtractionData()
    confs: list[FieldData] = []

    for field, patterns in _HEADER_PATTERNS.items():
        for pat in patterns:
            m = re.search(pat, all_text, re.IGNORECASE)
            if m:
                value = m.group(1).strip()
                setattr(data, field, value)
                confs.append(FieldData(name=field, value=value, confidence=avg_conf))
                break

    return data, confs


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def extract(ocr_result: OCRPageResult) -> tuple[ExtractionData, list[FieldData], list[str]]:
    """Extract structured invoice data from one page's OCR result.

    Returns:
        (ExtractionData, list[FieldData], list[str])
        — extraction, per-field confidences, unmatched column headers.

    Unmatched column headers should be passed to the orchestrator so they
    can be recorded as ColumnAliasProposal records for admin review.
    """
    data, text_confs = _extract_from_text(ocr_result.text_blocks)
    all_confs = list(text_confs)
    all_unmatched: list[str] = []

    found = _find_charge_table(ocr_result.tables)
    if found:
        table, col_map = found
        items, item_confs = _parse_charge_rows(table, col_map)
        data.line_items = items
        all_confs.extend(item_confs)
        all_unmatched.extend(col_map.unmatched)
        logger.info(
            "extractor.table_found",
            line_items=len(items),
            mapped_cols=list(col_map.mapping.values()),
            unmatched=col_map.unmatched,
        )
    else:
        logger.warning("extractor.no_charge_table", tables=len(ocr_result.tables))

    return data, all_confs, all_unmatched
