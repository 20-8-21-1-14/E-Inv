"""Vietnamese GDT e-invoice XML parser (schema 1.0.7 / HDDTGTT).

Handles namespace variations across issuer software — some omit prefixes,
some use 'inv:' or 'hd:'. Tag names are extracted without namespace prefix
so they match regardless of which prefix the issuer used.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any

import defusedxml.ElementTree as ET
import structlog

from pipeline.models import ExtractionData, LineItemData

logger = structlog.get_logger()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tag(element: ET.Element) -> str:
    """Return local tag name, stripping namespace prefix."""
    return element.tag.split("}")[-1] if "}" in element.tag else element.tag


def _find_text(node: ET.Element, *local_tags: str) -> str | None:
    """Search for any of the given local tag names under node (BFS)."""
    for tag in local_tags:
        for child in node.iter():
            if _tag(child) == tag and child.text and child.text.strip():
                return child.text.strip()
    return None


def _find_node(node: ET.Element, *local_tags: str) -> ET.Element | None:
    for tag in local_tags:
        for child in node.iter():
            if _tag(child) == tag:
                return child
    return None


def _find_all(node: ET.Element, local_tag: str) -> list[ET.Element]:
    return [child for child in node.iter() if _tag(child) == local_tag]


def _decimal(raw: str | None) -> Decimal | None:
    if not raw:
        return None
    try:
        # Replace comma thousands/decimal separators → dot, then strip non-numeric chars
        cleaned = re.sub(r"[^\d.\-]", "", raw.replace(",", "."))
        parts = cleaned.split(".")
        if len(parts) > 2:
            # "1.234.567" — all but last are thousands groups
            cleaned = "".join(parts[:-1]) + "." + parts[-1]
        elif len(parts) == 2 and len(parts[-1]) == 3 and len(parts[0]) <= 3:
            # "100.000" — ambiguous; treat as integer thousands separator
            cleaned = "".join(parts)
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _decimal_required(raw: str | None, fallback: Decimal = Decimal("0")) -> Decimal:
    return _decimal(raw) or fallback


def _parse_date(raw: str | None) -> str | None:
    """Normalise GDT date strings (YYYY-MM-DD, DD/MM/YYYY, YYYYMMDD) → ISO."""
    if not raw:
        return None
    raw = raw.strip()
    if re.match(r"^\d{4}-\d{2}-\d{2}", raw):
        return raw[:10]
    m = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", raw)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    if re.match(r"^\d{8}$", raw):
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    return raw


# ---------------------------------------------------------------------------
# Main parser
# ---------------------------------------------------------------------------

def parse_xml(content: bytes) -> ExtractionData:
    """Parse Vietnamese GDT XML and return a fully-populated ExtractionData.

    All monetary fields carry Decimal precision — no float rounding.
    Confidence for XML path is always 1.0 (canonical source).
    """
    tree = ET.fromstring(content)

    # ── Locate logical sections ──────────────────────────────────────────────
    # Some issuers nest under <HDon>, some put sections directly at root
    tt_chung = _find_node(tree, "TTChung")
    n_xuat   = _find_node(tree, "NXuat", "NBan")    # seller
    n_mua    = _find_node(tree, "NMua")              # buyer
    nd_hdon  = _find_node(tree, "NDHDon")            # content block
    ds_hhdv  = _find_node(tree, "DSHHDVu")          # line items list
    tt_oan   = _find_node(tree, "TToan")             # totals

    root_for_search = nd_hdon or tree

    data = ExtractionData()

    # ── Header ───────────────────────────────────────────────────────────────
    if tt_chung is not None:
        data.invoice_number = _find_text(tt_chung, "SHDon", "SoHDon")
        data.invoice_date   = _parse_date(_find_text(tt_chung, "NLap", "NKy", "NgayLap"))
        data.invoice_form   = _find_text(tt_chung, "MHSo", "MauSo")
        data.invoice_series = _find_text(tt_chung, "KHHDon", "KHMSHDon", "KyHieu")
        data.currency       = _find_text(tt_chung, "DVTTe", "LoaiTien") or "VND"
        data.payment_method = _find_text(tt_chung, "HTTToan", "PhuongThucTT")
    else:
        # Fallback: search from root
        data.invoice_number = _find_text(tree, "SHDon", "SoHDon")
        data.invoice_date   = _parse_date(_find_text(tree, "NLap", "NKy", "NgayLap"))
        data.invoice_form   = _find_text(tree, "MHSo", "MauSo")
        data.invoice_series = _find_text(tree, "KHHDon", "KHMSHDon", "KyHieu")
        data.currency       = _find_text(tree, "DVTTe", "LoaiTien") or "VND"
        data.payment_method = _find_text(tree, "HTTToan", "PhuongThucTT")

    # ── Seller ───────────────────────────────────────────────────────────────
    seller_node = n_xuat or _find_node(root_for_search, "NXuat", "NBan")
    if seller_node is not None:
        data.seller_name     = _find_text(seller_node, "Ten")
        data.seller_tax_code = _find_text(seller_node, "MST", "MaSoThue")
        data.seller_address  = _find_text(seller_node, "DChi", "DiaChi")
        data.seller_bank     = _find_text(seller_node, "STKNHang", "SoTaiKhoan")
    else:
        data.seller_name     = _find_text(root_for_search, "TenNBan", "Ten")
        data.seller_tax_code = _find_text(root_for_search, "MSTNBan")

    # ── Buyer ────────────────────────────────────────────────────────────────
    buyer_node = n_mua or _find_node(root_for_search, "NMua", "KHang")
    if buyer_node is not None:
        data.buyer_name     = _find_text(buyer_node, "Ten")
        data.buyer_tax_code = _find_text(buyer_node, "MST", "MaSoThue")
        data.buyer_address  = _find_text(buyer_node, "DChi", "DiaChi")
    else:
        data.buyer_name     = _find_text(root_for_search, "TenNMua")
        data.buyer_tax_code = _find_text(root_for_search, "MSTNMua")

    # ── Line items ───────────────────────────────────────────────────────────
    items_root = ds_hhdv or nd_hdon or tree
    raw_items: list[ET.Element] = _find_all(items_root, "HHDVu")

    if not raw_items:
        # Some issuers use <ChiTiet>, <Item>, <MatHang>
        for alt in ("ChiTiet", "Item", "MatHang", "HangHoa"):
            raw_items = _find_all(items_root, alt)
            if raw_items:
                break

    for idx, item_node in enumerate(raw_items, start=1):
        stt_text = _find_text(item_node, "STT")
        try:
            line_num = int(stt_text) if stt_text else idx
        except ValueError:
            line_num = idx

        item_name = (
            _find_text(item_node, "THHDVu", "TenHH", "MoTa", "TenDV", "Ten") or ""
        )
        item_code = _find_text(item_node, "MHH", "MaHang", "Ma")
        unit      = _find_text(item_node, "DVTinh", "DonVi", "DVT") or ""

        qty_raw   = _find_text(item_node, "SLuong", "SL", "SoLuong")
        price_raw = _find_text(item_node, "DGia", "DonGia", "GiaBan")
        amt_raw   = _find_text(item_node, "ThTien", "ThanhTien", "TienHang")

        disc_rate_raw = _find_text(item_node, "TLCKhau", "TyLeCKhau")
        disc_amt_raw  = _find_text(item_node, "STCKhau", "TienCKhau")

        tax_rate_raw  = _find_text(item_node, "TSuat", "ThueSuat", "TSuatGTGT")
        tax_amt_raw   = _find_text(item_node, "TienThue", "TienThuGTGT")
        total_raw     = _find_text(
            item_node, "ThTienTDTCVAT", "TongTien", "ThanhToanSauThue", "TienThanhToan"
        )

        quantity   = _decimal_required(qty_raw)
        unit_price = _decimal_required(price_raw)
        amount     = _decimal_required(amt_raw) or (quantity * unit_price)
        tax_rate   = _decimal_required(_clean_tax_rate(tax_rate_raw))
        tax_amount = _decimal_required(tax_amt_raw) or (amount * tax_rate / Decimal("100"))
        disc_rate  = _decimal(disc_rate_raw)
        disc_amt   = _decimal(disc_amt_raw)
        total_amount = _decimal_required(total_raw) or (amount - (disc_amt or Decimal("0")) + tax_amount)

        data.line_items.append(LineItemData(
            line_number=line_num,
            item_name=item_name,
            item_code=item_code,
            unit=unit,
            quantity=quantity,
            unit_price=unit_price,
            amount=amount,
            discount_rate=disc_rate,
            discount_amount=disc_amt,
            tax_rate=tax_rate,
            tax_amount=tax_amount,
            total_amount=total_amount,
        ))

    # ── Totals ───────────────────────────────────────────────────────────────
    totals_node = tt_oan or nd_hdon or tree
    data.subtotal        = _decimal(_find_text(totals_node, "TgTCThue", "TongTienHang", "TienHang"))
    data.total_discount  = _decimal(_find_text(totals_node, "TgTCKhau", "TongChietKhau"))
    data.total_tax       = _decimal(_find_text(totals_node, "TgTThue", "TongTienThue", "TongThue"))
    data.grand_total     = _decimal(_find_text(totals_node, "TgTTTBSo", "TongTienTT", "TongThanhToan"))
    data.amount_in_words = _find_text(totals_node, "TTBChu", "TongTienBangChu")

    logger.info(
        "xml_parser.done",
        invoice_number=data.invoice_number,
        line_items=len(data.line_items),
        grand_total=str(data.grand_total),
    )
    return data


def _clean_tax_rate(raw: str | None) -> str | None:
    """Normalise tax rate: '10%' → '10', 'KKKK' / '0%' → '0'."""
    if not raw:
        return None
    raw = raw.strip()
    # Non-taxable codes used by GDT
    if raw.upper() in ("KK", "KKKK", "KCT", "KHAC", "ÂM", "AM"):
        return "0"
    return raw.replace("%", "").strip()
