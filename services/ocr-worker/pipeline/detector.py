"""Format detection and PDF → image conversion."""

import asyncio
from concurrent.futures import ThreadPoolExecutor

import structlog

logger = structlog.get_logger()

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="detector")


def _detect_format_sync(content: bytes, declared_format: str) -> str:
    """Confirm format from magic bytes, overriding declared format if needed."""
    if content[:5] in (b"<?xml", b"<e-in") or (len(content) > 1 and content[0:1] == b"<"):
        return "xml"
    if content[:4] == b"%PDF":
        return "pdf"
    if (
        content[:2] == b"\xff\xd8"       # JPEG
        or content[:4] == b"\x89PNG"     # PNG
        or content[:2] in (b"II", b"MM") # TIFF
        or content[:4] == b"RIFF"        # WebP container
    ):
        return "image"
    # Trust declared format as fallback
    return declared_format


async def detect_format(content: bytes, declared_format: str) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, _detect_format_sync, content, declared_format)


def _pdf_to_images_sync(content: bytes, dpi: int = 300) -> list:
    """Convert PDF bytes → list of numpy arrays, one per page."""
    import numpy as np
    from pdf2image import convert_from_bytes

    pil_images = convert_from_bytes(content, dpi=dpi, fmt="jpeg")
    return [np.array(img.convert("RGB")) for img in pil_images]


async def pdf_to_images(content: bytes, dpi: int = 300) -> list:
    """Returns list[np.ndarray], one image per PDF page."""
    loop = asyncio.get_running_loop()
    images = await loop.run_in_executor(_executor, _pdf_to_images_sync, content, dpi)
    logger.debug("pdf_to_images", pages=len(images), dpi=dpi)
    return images


def _bytes_to_image_sync(content: bytes) -> object:
    import numpy as np
    from PIL import Image
    import io

    img = Image.open(io.BytesIO(content)).convert("RGB")
    return np.array(img)


async def bytes_to_image(content: bytes):
    """Convert raw image bytes → np.ndarray."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_executor, _bytes_to_image_sync, content)


def merge_page_results(page_extractions: list) -> object:
    """Merge multi-page pipeline results into a single ExtractionData.

    Strategy:
      - Header fields: first non-None value (page 1 preferred)
      - Line items: concatenated across all pages, line_number re-sequenced
      - Totals: last non-None value (usually on the final page)
    """
    from pipeline.models import ExtractionData

    if not page_extractions:
        return ExtractionData()
    if len(page_extractions) == 1:
        return page_extractions[0]

    merged = ExtractionData()
    line_counter = 1

    for page_data in page_extractions:
        # Header: take first non-None value
        for attr in (
            "invoice_number", "invoice_date", "invoice_form", "invoice_series",
            "seller_name", "seller_tax_code", "seller_address", "seller_bank",
            "buyer_name", "buyer_tax_code", "buyer_address",
            "payment_method", "currency",
        ):
            if getattr(merged, attr) is None and getattr(page_data, attr) is not None:
                setattr(merged, attr, getattr(page_data, attr))

        # Line items: append, renumber globally
        for item in page_data.line_items:
            item.line_number = line_counter
            merged.line_items.append(item)
            line_counter += 1

        # Totals: overwrite with each page (last page wins)
        for attr in ("subtotal", "total_discount", "total_tax", "grand_total", "amount_in_words"):
            if getattr(page_data, attr) is not None:
                setattr(merged, attr, getattr(page_data, attr))

    return merged
