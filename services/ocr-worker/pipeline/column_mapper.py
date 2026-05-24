"""Map OCR-detected column headers to canonical LineItemData field names.

Two-pass strategy:
  1. Exact match (case-insensitive, after normalising whitespace)
  2. Token overlap ratio — handles partial OCR errors ("Đơngiá" vs "Đơn giá")

Schema hot-reload:
  The alias dictionary is refreshed from the DB (SchemaVersion table) every
  OCR_SCHEMA_TTL seconds (default 900 = 15 min). Falls back to label_schema.json
  on disk when the DB is unavailable. This means adding a new column alias in the
  admin panel takes effect in the next worker TTL window — no restart required.

Unmatched header collection:
  `map_headers()` returns a second value: the list of raw headers that could not
  be matched. The orchestrator writes these to ColumnAliasProposal so admins can
  review and approve new aliases without diving into DB logs.
"""

from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

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

_SCHEMA_TTL: float = float(os.environ.get("OCR_SCHEMA_TTL", "900"))   # 15 min default

# Fallback aliases baked in — always available if DB / file unreachable
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


# ---------------------------------------------------------------------------
# TTL-based schema cache (replaces @lru_cache to allow hot-reload)
# ---------------------------------------------------------------------------

@dataclass
class _SchemaCache:
    aliases: dict[str, list[str]] = field(default_factory=lambda: dict(_BUILTIN_ALIASES))
    lookup: dict[str, str] = field(default_factory=dict)   # normalised alias → canonical field
    loaded_at: float = 0.0
    schema_version: str = "builtin"

    def is_stale(self) -> bool:
        return (time.monotonic() - self.loaded_at) > _SCHEMA_TTL


_cache = _SchemaCache()


def _load_aliases_from_file() -> dict[str, list[str]]:
    try:
        schema = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))
        aliases = schema.get("doc_types", {}).get("vat_invoice", {}).get("column_aliases", {})
        if aliases:
            merged = dict(_BUILTIN_ALIASES)
            for f, extra in aliases.items():
                merged.setdefault(f, [])
                for v in extra:
                    if v not in merged[f]:
                        merged[f].append(v)
            return merged
    except Exception:
        pass
    return dict(_BUILTIN_ALIASES)


def _load_aliases_from_db() -> tuple[dict[str, list[str]], str] | None:
    """Try to load the active SchemaVersion from DB synchronously (for use in sync context).

    Returns (aliases_dict, version_str) or None if unavailable.
    The DB call uses a short timeout so it never blocks OCR processing.
    """
    try:
        import asyncio
        from einv_common.db import session_factory
        from einv_common.models.training import SchemaVersion
        from sqlalchemy import select

        async def _fetch():
            async with session_factory() as session:
                result = await session.execute(
                    select(SchemaVersion)
                    .where(SchemaVersion.is_active == True)  # noqa: E712
                    .limit(1)
                )
                return result.scalar_one_or_none()

        # Run in a fresh event loop — this is called from sync context (ThreadPoolExecutor).
        # If there is already a running loop in this thread, we cannot block it.
        try:
            asyncio.get_running_loop()
            return None  # Running loop detected — fall back to file
        except RuntimeError:
            pass  # No running loop in this thread — safe to call asyncio.run()

        sv = asyncio.run(asyncio.wait_for(_fetch(), timeout=2.0))
        if sv is None:
            return None

        content = sv.content
        aliases = content.get("doc_types", {}).get("vat_invoice", {}).get("column_aliases", {})
        merged = dict(_BUILTIN_ALIASES)
        for f, extra in aliases.items():
            merged.setdefault(f, [])
            for v in extra:
                if v not in merged[f]:
                    merged[f].append(v)
        return merged, sv.version

    except Exception as exc:
        logger.warning("column_mapper.db_schema_unavailable", error=str(exc))
        return None


def _build_lookup(aliases: dict[str, list[str]]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for f, alias_list in aliases.items():
        for alias in alias_list:
            lookup[_normalise(alias)] = f
        lookup[_normalise(f)] = f
    return lookup


def _refresh_cache() -> None:
    """Reload aliases from DB → file → builtin (in priority order)."""
    result = _load_aliases_from_db()
    if result:
        aliases, version = result
        _cache.aliases = aliases
        _cache.schema_version = version
    else:
        aliases = _load_aliases_from_file()
        _cache.aliases = aliases
        _cache.schema_version = "file"

    _cache.lookup = _build_lookup(_cache.aliases)
    _cache.loaded_at = time.monotonic()
    logger.debug("column_mapper.schema_refreshed", version=_cache.schema_version,
                 fields=len(_cache.aliases))


def _get_lookup() -> dict[str, str]:
    """Return the current alias lookup, refreshing if stale."""
    if _cache.is_stale() or not _cache.lookup:
        _refresh_cache()
    return _cache.lookup


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

def _normalise(text: str) -> str:
    text = unicodedata.normalize("NFC", text)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def _tokens(text: str) -> set[str]:
    return set(re.split(r"[\s,./\-]+", _normalise(text))) - {""}


def _overlap_ratio(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(len(ta), len(tb))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def map_header(raw_header: str, threshold: float = 0.45) -> str | None:
    """Return canonical field name for a raw OCR column header, or None.

    Args:
        raw_header: Raw OCR text of the column header cell.
        threshold:  Minimum token overlap ratio for fuzzy match (0–1).
    """
    if not raw_header or not raw_header.strip():
        return None

    lookup = _get_lookup()
    norm = _normalise(raw_header)

    # Pass 1: exact
    if norm in lookup:
        return lookup[norm]

    # Pass 2: fuzzy token overlap against all known aliases
    best_field: str | None = None
    best_score = 0.0
    for alias_norm, f in lookup.items():
        score = _overlap_ratio(norm, alias_norm)
        if score > best_score:
            best_score = score
            best_field = f

    if best_score >= threshold:
        logger.debug("column_mapper.fuzzy_match",
                     raw=raw_header, mapped=best_field, score=round(best_score, 3))
        return best_field

    return None


def map_headers(raw_headers: list[str]) -> tuple[dict[int, str], list[str]]:
    """Map a list of raw header strings.

    Returns:
        ({col_index: canonical_field}, [unmatched_raw_headers])

    Unmatched headers are returned so the orchestrator can record them
    as ColumnAliasProposal records for admin review.
    """
    result: dict[int, str] = {}
    unmatched: list[str] = []
    seen: set[str] = set()

    for idx, raw in enumerate(raw_headers):
        field = map_header(raw)
        if field and field not in seen:
            result[idx] = field
            seen.add(field)
        elif field:
            # Duplicate column for same canonical field — skip silently (not unmatched)
            pass
        elif raw and raw.strip():
            unmatched.append(raw.strip())

    return result, unmatched
