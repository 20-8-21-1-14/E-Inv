"""Confidence scoring and routing decisions.

Computes a single document-level confidence score from:
  1. Per-field OCR confidence values (weighted by field importance)
  2. Validation error penalty (-5% per error, -2% per warning)
  3. Completeness bonus/penalty (missing required fields reduce score)

Routing thresholds are read from environment / tenant config:
  - score ≥ threshold_done   → "done"     (auto-accepted)
  - score ≥ threshold_llm    → "llm"      (send to LLM fallback)
  - score <  threshold_llm   → "hitl"     (send to human review queue)
"""

from __future__ import annotations

import os

import structlog

from pipeline.models import ExtractionData, FieldData, PipelineResult, ValidationError

logger = structlog.get_logger()

# Default thresholds — overridden per tenant at runtime
_THRESHOLD_DONE = float(os.environ.get("OCR_THRESHOLD_DONE", "0.92"))
_THRESHOLD_LLM  = float(os.environ.get("OCR_THRESHOLD_LLM", "0.75"))

# Field weights: critical charge fields get higher weight
_FIELD_WEIGHTS: dict[str, float] = {
    "item_name":       2.0,
    "quantity":        2.0,
    "unit_price":      2.0,
    "amount":          1.5,
    "tax_rate":        1.0,
    "tax_amount":      1.0,
    "total_amount":    1.5,
    "unit":            0.5,
    "invoice_number":  1.5,
    "invoice_date":    1.0,
    "seller_tax_code": 1.0,
    "buyer_tax_code":  0.5,
    "grand_total":     1.5,
}

_REQUIRED_FIELDS = (
    "invoice_number",
    "invoice_date",
    "seller_name",
    "buyer_name",
    "grand_total",
)

_ERROR_PENALTY   = 0.05   # per validation error
_WARNING_PENALTY = 0.02   # per validation warning


def compute_score(
    data: ExtractionData,
    field_confidences: list[FieldData],
    validation_errors: list[ValidationError],
    threshold_done: float = _THRESHOLD_DONE,
    threshold_llm: float = _THRESHOLD_LLM,
) -> tuple[float, str]:
    """Compute overall confidence score and routing decision.

    Returns:
        (score, routing) where routing ∈ {"done", "llm", "hitl"}
    """
    score = _weighted_avg(field_confidences)
    score = _apply_validation_penalty(score, validation_errors)
    score = _apply_completeness_penalty(score, data)

    score = max(0.0, min(1.0, round(score, 4)))

    if score >= threshold_done:
        routing = "done"
    elif score >= threshold_llm:
        routing = "llm"
    else:
        routing = "hitl"

    logger.info(
        "scorer.result",
        score=score,
        routing=routing,
        line_items=len(data.line_items),
        errors=len([e for e in validation_errors if e.severity == "error"]),
        warnings=len([e for e in validation_errors if e.severity == "warning"]),
    )
    return score, routing


def _weighted_avg(field_confidences: list[FieldData]) -> float:
    if not field_confidences:
        return 0.5   # Neutral when no OCR confidence data available

    total_weight = 0.0
    weighted_sum = 0.0

    for fd in field_confidences:
        # Extract base field name (strip line_N. prefix)
        base = fd.name.split(".")[-1] if "." in fd.name else fd.name
        w = _FIELD_WEIGHTS.get(base, 1.0)
        weighted_sum += fd.confidence * w
        total_weight += w

    return weighted_sum / total_weight if total_weight else 0.5


def _apply_validation_penalty(score: float, errors: list[ValidationError]) -> float:
    for err in errors:
        if err.severity == "error":
            score -= _ERROR_PENALTY
        else:
            score -= _WARNING_PENALTY
    return score


def _apply_completeness_penalty(score: float, data: ExtractionData) -> float:
    missing = sum(1 for f in _REQUIRED_FIELDS if not getattr(data, f, None))
    if missing > 0:
        # Deduct 3% per missing required field
        score -= missing * 0.03

    if not data.line_items:
        score -= 0.20   # Large penalty — charge lines are the primary output

    return score
