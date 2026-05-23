"""Map OCR-detected column headers to canonical LineItemData field names.

Uses a two-pass strategy:
  1. Exact match (case-insensitive, after stripping whitespace)
  2. Token overlap ratio — handles partial OCR errors like "Đơn giá" → "Đơngiá"

The alias dictionary is loaded from label_schema.json once at import time and
cached so repeated calls within a worker process are free.
"""

from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

import structlog

logger = structlog.get_logger()

# Canonical names that appear as keys in LineItemData
CANONICAL_FIELDS = (
    "line_number",
    "item_name",
    "item_code",
    "unit",
    "quantity",
    "unit_price",
    "amount",
    "discount_rate",
    "discount_amount",
    "tax_rate",
    "tax_amount",
    "total_amount",
)

_SCHEMA_PATH = Path(__file__).parent.parent.parent.parent / "training" / "annotation" / "label_schema.json"

# Fallback aliases baked in — used when the schema file is unavailable
_BUILTIN_ALIASES: dict[str, list[str]] = {
    "item_name":       ["Tên hàng hóa", "Tên hàng hóa, dịch vụ", "Diễn giải", "Tên dịch vụ", "Name"],
    "item_code":       ["Mã hàng", "Mã HH", "Mã số", "Code"],
    "unit":            ["Đơn vị tính", "ĐVT", "Đơn vị", "Unit"],
    "quantity":        ["Số lượng", "SL", "Qty", "Quantity"],
    "unit_price":      ["Đơn giá", "Giá bán", "Unit Price", "Giá"],
    "amount":          ["Thành tiền", "Tiền hàng", "Amount", "Tổng tiền hàng"],
    "discount_rate":   ["Chiết khấu %", "CK %", "Discount %"],
    "discount_amount": ["Tiền chiết khấu", "CK", "Discount"],
    "tax_rate":        ["Thuế suất", "Thuế GTGT %", "VAT %", "Tax Rate"],
    "tax_amount":      ["Tiền thuế GTGT", "Tiền thuế", "VAT Amount", "Tax"],
    "total_amount":    ["Tổng cộng", "Total", "Tổng thanh toán"],
    "line_number":     ["STT", "TT", "No.", "#"],
}


@lru_cache(maxsize=1)
def _load_aliases() -> dict[str, list[str]]:
    try:
        schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
        aliases = schema.get("doc_types", {}).get("vat_invoice", {}).get("column_aliases", {})
        if aliases:
            merged = dict(_BUILTIN_ALIASES)
            for field, extra in aliases.items():
                merged.setdefault(field, [])
                for v in extra:
                    if v not in merged[field]:
                        merged[field].append(v)
            return merged
    except Exception:
        pass
    return _BUILTIN_ALIASES


def _normalise(text: str) -> str:
    """Lowercase, remove combining diacritics noise, collapse spaces."""
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def _tokens(text: str) -> set[str]:
    return set(re.split(r"[\s,./\-]+", _normalise(text))) - {""}


def _overlap_ratio(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    inter = ta & tb
    return len(inter) / max(len(ta), len(tb))


# Pre-built lookup: normalised alias → canonical field
@lru_cache(maxsize=1)
def _exact_lookup() -> dict[str, str]:
    lookup: dict[str, str] = {}
    for field, aliases in _load_aliases().items():
        for alias in aliases:
            lookup[_normalise(alias)] = field
        # Also register the canonical name itself
        lookup[_normalise(field)] = field
    return lookup


def map_header(raw_header: str, threshold: float = 0.45) -> str | None:
    """Return canonical field name for a raw OCR column header, or None.

    Args:
        raw_header: Raw OCR text of the column header cell.
        threshold:  Minimum token overlap ratio for fuzzy match (0–1).
    """
    if not raw_header or not raw_header.strip():
        return None

    norm = _normalise(raw_header)
    lookup = _exact_lookup()

    # Pass 1: exact
    if norm in lookup:
        return lookup[norm]

    # Pass 2: fuzzy token overlap
    best_field: str | None = None
    best_score = 0.0
    for alias_norm, field in lookup.items():
        score = _overlap_ratio(norm, alias_norm)
        if score > best_score:
            best_score = score
            best_field = field

    if best_score >= threshold:
        logger.debug(
            "column_mapper.fuzzy_match",
            raw=raw_header,
            mapped=best_field,
            score=round(best_score, 3),
        )
        return best_field

    logger.warning("column_mapper.no_match", raw=raw_header, best_score=round(best_score, 3))
    return None


def map_headers(raw_headers: list[str]) -> dict[int, str]:
    """Map a list of raw header strings → {col_index: canonical_field}.

    Only columns that successfully map are included in the result.
    """
    result: dict[int, str] = {}
    seen: set[str] = set()

    for idx, raw in enumerate(raw_headers):
        field = map_header(raw)
        if field and field not in seen:
            result[idx] = field
            seen.add(field)

    return result
