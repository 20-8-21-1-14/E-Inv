"""PaddleOCR PPStructure async wrapper.

Loads the model once per process (lazy singleton) and offloads CPU-bound
inference to a dedicated ThreadPoolExecutor. Callers use the async API;
the sync internals are safe to call concurrently from the same thread pool
because PaddleOCR releases the GIL during inference.

PPStructure returns a list of region dicts. We extract:
  - type == "table"  → SLANet HTML table → parsed into row/column cell grid
  - type == "text"   → plain text blocks → assembled into logical paragraphs
"""

from __future__ import annotations

import asyncio
import html
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import structlog

logger = structlog.get_logger()

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ocr_engine")

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

@dataclass
class TableCell:
    row: int
    col: int
    text: str
    confidence: float = 1.0


@dataclass
class TableRegion:
    """One detected table with its cell grid."""
    bbox: tuple[int, int, int, int]     # x1, y1, x2, y2
    cells: list[TableCell] = field(default_factory=list)
    html_raw: str = ""


@dataclass
class TextBlock:
    bbox: tuple[int, int, int, int]
    text: str
    confidence: float


@dataclass
class OCRPageResult:
    tables: list[TableRegion]
    text_blocks: list[TextBlock]


# ---------------------------------------------------------------------------
# Singleton model loader
# ---------------------------------------------------------------------------

_structure_model: Any = None
_model_lock = asyncio.Lock()


def _load_model_sync() -> Any:
    global _structure_model
    if _structure_model is not None:
        return _structure_model

    from paddleocr import PPStructure  # type: ignore

    _structure_model = PPStructure(
        lang="vi",
        use_gpu=False,
        show_log=False,
        table=True,
        ocr=True,
        layout=True,
        # Use SLANet for table structure — higher accuracy than default
        structure_version="PP-StructureV2",
    )
    logger.info("ocr_engine.model_loaded")
    return _structure_model


async def _get_model() -> Any:
    global _structure_model
    if _structure_model is not None:
        return _structure_model
    async with _model_lock:
        if _structure_model is None:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(_executor, _load_model_sync)
    return _structure_model


# ---------------------------------------------------------------------------
# Sync inference (runs in executor thread)
# ---------------------------------------------------------------------------

def _run_inference_sync(model: Any, image: np.ndarray) -> list[dict]:
    return model(image)


def _parse_html_table(html_str: str) -> list[TableCell]:
    """Extract cell text from SLANet HTML output preserving row/col indices."""
    cells: list[TableCell] = []
    html_str = html.unescape(html_str)

    # Strip table wrapper tags
    body_match = re.search(r"<tbody>(.*?)</tbody>", html_str, re.DOTALL | re.IGNORECASE)
    body = body_match.group(1) if body_match else html_str

    row_idx = 0
    for row_match in re.finditer(r"<tr[^>]*>(.*?)</tr>", body, re.DOTALL | re.IGNORECASE):
        row_html = row_match.group(1)
        col_idx = 0
        for cell_match in re.finditer(r"<t[dh][^>]*>(.*?)</t[dh]>", row_html, re.DOTALL | re.IGNORECASE):
            inner = re.sub(r"<[^>]+>", " ", cell_match.group(1))
            text = re.sub(r"\s+", " ", inner).strip()
            cells.append(TableCell(row=row_idx, col=col_idx, text=text))
            col_idx += 1
        if col_idx > 0:
            row_idx += 1

    return cells


def _bbox_to_tuple(bbox: list | Any) -> tuple[int, int, int, int]:
    """Normalise PPStructure bbox (nested list or flat) to (x1,y1,x2,y2)."""
    if isinstance(bbox, (list, tuple)):
        flat = [v for sub in bbox for v in (sub if isinstance(sub, (list, tuple)) else [sub])]
        if len(flat) >= 4:
            xs = flat[0::2]
            ys = flat[1::2]
            return int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys))
    return (0, 0, 0, 0)


def _parse_structure_result(regions: list[dict]) -> OCRPageResult:
    tables: list[TableRegion] = []
    text_blocks: list[TextBlock] = []

    for region in regions:
        region_type = region.get("type", "").lower()
        bbox = _bbox_to_tuple(region.get("bbox", []))

        if region_type == "table":
            html_str = region.get("res", {}).get("html", "")
            cells = _parse_html_table(html_str)
            tables.append(TableRegion(bbox=bbox, cells=cells, html_raw=html_str))

        else:
            # Text / title / figure_caption regions
            res = region.get("res", [])
            if not isinstance(res, list):
                continue
            lines: list[str] = []
            confs: list[float] = []
            for line_result in res:
                if isinstance(line_result, (list, tuple)) and len(line_result) >= 2:
                    txt_conf = line_result[1]
                    if isinstance(txt_conf, (list, tuple)) and len(txt_conf) >= 2:
                        lines.append(str(txt_conf[0]))
                        confs.append(float(txt_conf[1]))
            if lines:
                text_blocks.append(TextBlock(
                    bbox=bbox,
                    text=" ".join(lines),
                    confidence=float(sum(confs) / len(confs)),
                ))

    return OCRPageResult(tables=tables, text_blocks=text_blocks)


# ---------------------------------------------------------------------------
# Public async API
# ---------------------------------------------------------------------------

async def run_ocr(image: np.ndarray) -> OCRPageResult:
    """Run PPStructure on one image and return structured OCR results."""
    model = await _get_model()
    loop = asyncio.get_event_loop()
    regions: list[dict] = await loop.run_in_executor(
        _executor, _run_inference_sync, model, image
    )
    result = _parse_structure_result(regions)
    logger.debug(
        "ocr_engine.done",
        tables=len(result.tables),
        text_blocks=len(result.text_blocks),
    )
    return result


async def run_ocr_pages(images: list[np.ndarray]) -> list[OCRPageResult]:
    """Run OCR on multiple pages concurrently (bounded by executor workers)."""
    tasks = [run_ocr(img) for img in images]
    return await asyncio.gather(*tasks)
