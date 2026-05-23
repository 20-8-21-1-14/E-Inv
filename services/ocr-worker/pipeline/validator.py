"""Charge math validator.

Validates:
  - Per line: qty × unit_price ≈ amount  (within rounding tolerance)
  - Per line: amount - discount + tax ≈ total_amount
  - Per line: amount × (tax_rate / 100) ≈ tax_amount
  - Cross-line: Σ total_amount ≈ grand_total
  - Cross-line: Σ tax_amount ≈ total_tax

All comparisons use Decimal arithmetic. Tolerance is set per-field:
  - Unit_price / amount: 1 VND (Vietnamese invoices round to integer VND)
  - Totals: 2 VND (some issuers apply rounding at the total level)
"""

from __future__ import annotations

from decimal import Decimal

import structlog

from pipeline.models import ExtractionData, ValidationError

logger = structlog.get_logger()

_LINE_TOLERANCE  = Decimal("1")    # 1 VND per line field
_TOTAL_TOLERANCE = Decimal("2")    # 2 VND for cross-line totals


def _approx(a: Decimal, b: Decimal, tol: Decimal = _LINE_TOLERANCE) -> bool:
    return abs(a - b) <= tol


def validate(data: ExtractionData) -> list[ValidationError]:
    """Return a list of ValidationError. Empty list means all checks passed."""
    errors: list[ValidationError] = []

    computed_subtotal   = Decimal("0")
    computed_total_tax  = Decimal("0")
    computed_grand      = Decimal("0")

    for item in data.line_items:
        ln = item.line_number

        # ── qty × price ≈ amount ─────────────────────────────────────────────
        if item.quantity != Decimal("0") and item.unit_price != Decimal("0"):
            expected_amount = (item.quantity * item.unit_price).quantize(Decimal("1"))
            if not _approx(item.amount, expected_amount):
                errors.append(ValidationError(
                    field=f"line_{ln}.amount",
                    message=(
                        f"qty×price mismatch: {item.quantity}×{item.unit_price}"
                        f"={expected_amount}, recorded={item.amount}"
                    ),
                ))

        # ── tax_amount ≈ amount × (tax_rate / 100) ──────────────────────────
        if item.tax_rate > Decimal("0"):
            expected_tax = (item.amount * item.tax_rate / Decimal("100")).quantize(Decimal("1"))
            if not _approx(item.tax_amount, expected_tax):
                errors.append(ValidationError(
                    field=f"line_{ln}.tax_amount",
                    message=(
                        f"tax mismatch: {item.amount}×{item.tax_rate}%"
                        f"={expected_tax}, recorded={item.tax_amount}"
                    ),
                    severity="warning",   # Tax may be pre-rounded by issuer
                ))

        # ── total_amount ≈ amount - discount + tax ──────────────────────────
        disc = item.discount_amount or Decimal("0")
        expected_total = (item.amount - disc + item.tax_amount).quantize(Decimal("1"))
        if item.total_amount != Decimal("0") and not _approx(item.total_amount, expected_total):
            errors.append(ValidationError(
                field=f"line_{ln}.total_amount",
                message=(
                    f"total mismatch: amount={item.amount} - disc={disc}"
                    f" + tax={item.tax_amount} = {expected_total},"
                    f" recorded={item.total_amount}"
                ),
            ))

        computed_subtotal  += item.amount
        computed_total_tax += item.tax_amount
        computed_grand     += item.total_amount

    # ── Cross-line total checks ───────────────────────────────────────────────
    if data.grand_total is not None and computed_grand != Decimal("0"):
        if not _approx(computed_grand, data.grand_total, _TOTAL_TOLERANCE):
            errors.append(ValidationError(
                field="grand_total",
                message=(
                    f"Σtotal_amount={computed_grand} ≠ grand_total={data.grand_total}"
                ),
            ))

    if data.total_tax is not None and computed_total_tax != Decimal("0"):
        if not _approx(computed_total_tax, data.total_tax, _TOTAL_TOLERANCE):
            errors.append(ValidationError(
                field="total_tax",
                message=(
                    f"Σtax_amount={computed_total_tax} ≠ total_tax={data.total_tax}"
                ),
                severity="warning",
            ))

    if data.subtotal is not None and computed_subtotal != Decimal("0"):
        if not _approx(computed_subtotal, data.subtotal, _TOTAL_TOLERANCE):
            errors.append(ValidationError(
                field="subtotal",
                message=(
                    f"Σamount={computed_subtotal} ≠ subtotal={data.subtotal}"
                ),
                severity="warning",
            ))

    # Fill in computed totals when document didn't provide them
    if data.grand_total is None and computed_grand != Decimal("0"):
        data.grand_total = computed_grand
    if data.total_tax is None and computed_total_tax != Decimal("0"):
        data.total_tax = computed_total_tax
    if data.subtotal is None and computed_subtotal != Decimal("0"):
        data.subtotal = computed_subtotal

    if errors:
        logger.warning("validator.errors", count=len(errors),
                       errors=[e.field for e in errors])
    else:
        logger.info("validator.ok", line_items=len(data.line_items))

    return errors
