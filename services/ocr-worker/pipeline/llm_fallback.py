"""LLM fallback extractor using Claude Vision.

Called when OCR confidence < threshold_done but ≥ threshold_llm.
Sends the raw invoice image (base64) to Claude and asks for structured JSON.
The returned JSON is merged with the existing OCR result — Claude fills in
fields where OCR confidence was low; high-confidence OCR fields are kept.

Claude model is configured via ANTHROPIC_MODEL env var (default: claude-haiku-4-5-20251001
for cost-effectiveness on routine fallbacks; override to claude-sonnet-4-6 for
complex documents).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
from decimal import Decimal, InvalidOperation

import httpx
import numpy as np
import structlog

from einv_common.exceptions import LLMError
from pipeline.models import ExtractionData, FieldData, LineItemData

logger = structlog.get_logger()

_API_URL   = "https://api.anthropic.com/v1/messages"
_MODEL     = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
_MAX_TOKENS = 4096
_TIMEOUT   = 30.0

_SYSTEM_PROMPT = (
    "You are an expert invoice data extractor specialised in Vietnamese e-invoices. "
    "Extract ALL data precisely. Return ONLY valid JSON — no markdown, no explanation."
)

_USER_PROMPT = """Extract all fields from this invoice image. Return JSON with this exact structure:
{
  "invoice_number": "string or null",
  "invoice_date": "YYYY-MM-DD or null",
  "invoice_form": "string or null",
  "invoice_series": "string or null",
  "seller_name": "string or null",
  "seller_tax_code": "string or null",
  "seller_address": "string or null",
  "seller_bank": "string or null",
  "buyer_name": "string or null",
  "buyer_tax_code": "string or null",
  "buyer_address": "string or null",
  "payment_method": "string or null",
  "currency": "string or null",
  "line_items": [
    {
      "line_number": 1,
      "item_name": "string",
      "item_code": "string or null",
      "unit": "string",
      "quantity": "decimal string",
      "unit_price": "decimal string",
      "amount": "decimal string",
      "discount_rate": "decimal string or null",
      "discount_amount": "decimal string or null",
      "tax_rate": "decimal string",
      "tax_amount": "decimal string",
      "total_amount": "decimal string"
    }
  ],
  "subtotal": "decimal string or null",
  "total_discount": "decimal string or null",
  "total_tax": "decimal string or null",
  "grand_total": "decimal string or null",
  "amount_in_words": "string or null"
}

Rules:
- All monetary amounts as decimal strings (no currency symbols, no thousands separators)
- Dates as YYYY-MM-DD
- Tax rate as number (e.g. "10" not "10%")
- If a field is not present, use null
- Include ALL line items — do not skip any rows"""


def _image_to_b64(image: np.ndarray) -> str:
    import cv2
    _, buf = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 90])
    return base64.b64encode(buf.tobytes()).decode()


def _parse_decimal(val: str | int | float | None) -> Decimal | None:
    if val is None:
        return None
    try:
        return Decimal(str(val).replace(",", ""))
    except InvalidOperation:
        return None


def _json_to_extraction(raw: dict) -> tuple[ExtractionData, list[FieldData]]:
    data = ExtractionData()
    confs: list[FieldData] = []
    confidence = 0.85  # LLM-extracted fields get 0.85 — lower than native XML but better than low-conf OCR

    # Header
    for field in (
        "invoice_number", "invoice_date", "invoice_form", "invoice_series",
        "seller_name", "seller_tax_code", "seller_address", "seller_bank",
        "buyer_name", "buyer_tax_code", "buyer_address",
        "payment_method", "currency", "amount_in_words",
    ):
        val = raw.get(field)
        if val:
            setattr(data, field, str(val))
            confs.append(FieldData(name=field, value=str(val), confidence=confidence))

    # Totals
    data.subtotal       = _parse_decimal(raw.get("subtotal"))
    data.total_discount = _parse_decimal(raw.get("total_discount"))
    data.total_tax      = _parse_decimal(raw.get("total_tax"))
    data.grand_total    = _parse_decimal(raw.get("grand_total"))

    # Line items
    for idx, item_raw in enumerate(raw.get("line_items") or [], start=1):
        try:
            item = LineItemData(
                line_number=int(item_raw.get("line_number", idx)),
                item_name=str(item_raw.get("item_name") or ""),
                item_code=item_raw.get("item_code"),
                unit=str(item_raw.get("unit") or ""),
                quantity=_parse_decimal(item_raw.get("quantity")) or Decimal("0"),
                unit_price=_parse_decimal(item_raw.get("unit_price")) or Decimal("0"),
                amount=_parse_decimal(item_raw.get("amount")) or Decimal("0"),
                discount_rate=_parse_decimal(item_raw.get("discount_rate")),
                discount_amount=_parse_decimal(item_raw.get("discount_amount")),
                tax_rate=_parse_decimal(item_raw.get("tax_rate")) or Decimal("0"),
                tax_amount=_parse_decimal(item_raw.get("tax_amount")) or Decimal("0"),
                total_amount=_parse_decimal(item_raw.get("total_amount")) or Decimal("0"),
                name_confidence=confidence,
                qty_confidence=confidence,
                unit_confidence=confidence,
                price_confidence=confidence,
            )
            data.line_items.append(item)
            confs.append(FieldData(
                name=f"line_{item.line_number}.item_name",
                value=item.item_name,
                confidence=confidence,
            ))
        except Exception as exc:
            logger.warning("llm_fallback.item_parse_error", row=idx, error=str(exc))

    return data, confs


async def run_llm_fallback(
    image: np.ndarray,
    prior_data: ExtractionData,
    prior_confs: list[FieldData],
    api_key: str,
) -> tuple[ExtractionData, list[FieldData]]:
    """Call Claude Vision and merge results with existing OCR output.

    Fields where prior OCR confidence ≥ 0.90 are kept as-is;
    low-confidence fields are overwritten by Claude's extraction.

    Raises:
        LLMError on API failure (retryable).
    """
    img_b64 = await asyncio.get_running_loop().run_in_executor(None, _image_to_b64, image)

    payload = {
        "model": _MODEL,
        "max_tokens": _MAX_TOKENS,
        "system": _SYSTEM_PROMPT,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/jpeg",
                            "data": img_b64,
                        },
                    },
                    {"type": "text", "text": _USER_PROMPT},
                ],
            }
        ],
    }

    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(_API_URL, json=payload, headers=headers)
            resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise LLMError(f"Claude API HTTP {exc.response.status_code}") from exc
    except Exception as exc:
        raise LLMError(str(exc)) from exc

    body = resp.json()
    raw_text = body.get("content", [{}])[0].get("text", "")

    # Strip markdown code fences if present
    json_match = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", raw_text)
    json_str = json_match.group(1) if json_match else raw_text.strip()

    try:
        extracted = json.loads(json_str)
    except json.JSONDecodeError as exc:
        raise LLMError(f"Claude returned invalid JSON: {exc}") from exc

    llm_data, llm_confs = _json_to_extraction(extracted)

    # ── Merge: keep high-confidence OCR fields, fill gaps with LLM ──────────
    high_conf_fields = {fd.name for fd in prior_confs if fd.confidence >= 0.90}

    merged = _merge(prior_data, llm_data, high_conf_fields)
    merged_confs = list(prior_confs)
    for c in llm_confs:
        if c.name not in high_conf_fields:
            merged_confs.append(c)

    logger.info(
        "llm_fallback.done",
        llm_items=len(llm_data.line_items),
        merged_items=len(merged.line_items),
        model=_MODEL,
    )
    return merged, merged_confs


def _merge(
    ocr: ExtractionData,
    llm: ExtractionData,
    keep_fields: set[str],
) -> ExtractionData:
    """Merge LLM extraction into OCR result, respecting keep_fields."""
    merged = ExtractionData()

    for attr in (
        "invoice_number", "invoice_date", "invoice_form", "invoice_series",
        "seller_name", "seller_tax_code", "seller_address", "seller_bank",
        "buyer_name", "buyer_tax_code", "buyer_address",
        "payment_method", "currency", "amount_in_words",
        "subtotal", "total_discount", "total_tax", "grand_total",
    ):
        if attr in keep_fields:
            setattr(merged, attr, getattr(ocr, attr))
        else:
            val = getattr(llm, attr) or getattr(ocr, attr)
            setattr(merged, attr, val)

    # Line items: if LLM found more items than OCR, prefer LLM (more complete)
    if len(llm.line_items) >= len(ocr.line_items):
        merged.line_items = llm.line_items
    else:
        merged.line_items = ocr.line_items

    return merged
