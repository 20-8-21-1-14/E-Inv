"""Prepare PaddleOCR recognition finetuning dataset from HITL-corrected records.

PaddleOCR rec finetuning requires image-text pairs in the format:
    crops/abc123.jpg\tcorrected_text

This script:
  1. Queries FieldConfidence records where is_corrected=True AND bbox IS NOT NULL
  2. Downloads the source document image from MinIO (minio_bucket_raw)
  3. Crops the bbox region from the image
  4. Saves crops to <output_dir>/crops/
  5. Writes train.txt / val.txt / test.txt split files

Usage:
    python training/scripts/prepare_dataset.py \
        --output-dir training/data/splits \
        --val-ratio 0.1 --test-ratio 0.1 \
        --max-samples 10000

After running, finetune with:
    python training/scripts/generate_finetune_config.py \
        --data-dir training/data/splits \
        --output configs/rec_vi_finetune.yml

    python /path/to/PaddleOCR/tools/train.py \
        -c configs/rec_vi_finetune.yml

Then export the trained checkpoint:
    python /path/to/PaddleOCR/tools/export_model.py \
        -c configs/rec_vi_finetune.yml \
        -o Global.pretrained_model=training/output/rec_vi/best_accuracy \
           Global.save_inference_dir=training/output/rec_vi/inference

Finally push to the model registry:
    python training/scripts/push_model.py \
        --model-type rec --model-dir training/output/rec_vi/inference \
        --version v1.1.0 --promote
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "common"))

import cv2
import numpy as np
import structlog

from einv_common.config import settings
from einv_common.db import session_factory
from einv_common.models.document import Document
from einv_common.models.extraction import ExtractionResult, FieldConfidence
from einv_common.storage import get_storage_client
from sqlalchemy import select

logger = structlog.get_logger()

_SEED = 42
_MIN_CROP_HEIGHT = 8   # discard crops too small to be meaningful
_JPEG_QUALITY    = 90


# ---------------------------------------------------------------------------
# Data transfer object — carries scalar values extracted while session is open,
# preventing DetachedInstanceError when used outside the session context.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _FieldRecord:
    bbox: dict            # {"x1": int, "y1": int, "x2": int, "y2": int}
    corrected_value: str
    file_path: str        # MinIO key in minio_bucket_raw


# ---------------------------------------------------------------------------
# DB query
# ---------------------------------------------------------------------------

async def fetch_corrected_fields(max_samples: int) -> list[_FieldRecord]:
    """Return lightweight records for all corrected field regions with bboxes.

    Scalar values are extracted while the session is still open, so no ORM
    objects escape the session boundary (avoids DetachedInstanceError).
    """
    async with session_factory() as session:
        result = await session.execute(
            select(FieldConfidence, Document.file_path)
            .join(ExtractionResult, FieldConfidence.result_id == ExtractionResult.id)
            .join(Document, ExtractionResult.document_id == Document.id)
            .where(FieldConfidence.is_corrected == True)          # noqa: E712
            .where(FieldConfidence.corrected_value.is_not(None))
            .where(FieldConfidence.bbox.is_not(None))
            .limit(max_samples)
        )
        return [
            _FieldRecord(
                bbox=fc.bbox,
                corrected_value=fc.corrected_value,
                file_path=file_path,
            )
            for fc, file_path in result.all()
        ]


# ---------------------------------------------------------------------------
# Image handling
# ---------------------------------------------------------------------------

def _bytes_to_bgr(content: bytes) -> np.ndarray | None:
    arr = np.frombuffer(content, dtype=np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def _pdf_first_page_to_bgr(content: bytes, dpi: int = 300) -> np.ndarray | None:
    try:
        from pdf2image import convert_from_bytes  # type: ignore
        pil_images = convert_from_bytes(content, dpi=dpi, fmt="jpeg", first_page=1, last_page=1)
        if not pil_images:
            return None
        img = np.array(pil_images[0].convert("RGB"))
        return cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    except Exception as exc:
        logger.warning("prepare_dataset.pdf_render_failed", error=str(exc))
        return None


def _crop(img: np.ndarray, bbox: dict) -> np.ndarray | None:
    """Crop bbox {x1,y1,x2,y2} from image with a small padding."""
    h, w = img.shape[:2]
    pad = 4
    x1 = max(0, bbox["x1"] - pad)
    y1 = max(0, bbox["y1"] - pad)
    x2 = min(w, bbox["x2"] + pad)
    y2 = min(h, bbox["y2"] + pad)
    if x2 <= x1 or y2 <= y1:
        return None
    crop = img[y1:y2, x1:x2]
    return crop if crop.shape[0] >= _MIN_CROP_HEIGHT else None


async def _fetch_image(file_path: str) -> bytes | None:
    try:
        return await get_storage_client().download(
            bucket=settings.minio_bucket_raw, key=file_path
        )
    except Exception as exc:
        logger.warning("prepare_dataset.download_failed", file_path=file_path, error=str(exc))
        return None


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

async def build_pairs(
    records: list[_FieldRecord],
    crops_dir: Path,
) -> list[tuple[str, str]]:
    """Download each source document once, crop all its field regions, write jpegs.

    Returns a list of (relative_crop_path, corrected_text) pairs ready for
    PaddleOCR train/val/test split files.
    """
    # Group by document so each is downloaded exactly once
    by_doc: dict[str, list[_FieldRecord]] = {}
    for rec in records:
        by_doc.setdefault(rec.file_path, []).append(rec)

    crops_dir.mkdir(parents=True, exist_ok=True)
    pairs: list[tuple[str, str]] = []
    processed_docs = 0

    for file_path, doc_records in by_doc.items():
        content = await _fetch_image(file_path)
        if content is None:
            continue

        is_pdf = file_path.lower().endswith(".pdf") or content[:4] == b"%PDF"
        img = _pdf_first_page_to_bgr(content) if is_pdf else _bytes_to_bgr(content)

        if img is None:
            logger.warning("prepare_dataset.decode_failed", file_path=file_path)
            continue

        processed_docs += 1
        for rec in doc_records:
            crop = _crop(img, rec.bbox)
            if crop is None:
                continue
            crop_name = f"{uuid.uuid4().hex}.jpg"
            cv2.imwrite(
                str(crops_dir / crop_name),
                crop,
                [cv2.IMWRITE_JPEG_QUALITY, _JPEG_QUALITY],
            )
            # Relative path so the dataset is portable across machines
            rel_path = f"crops/{crop_name}"
            pairs.append((rel_path, rec.corrected_value))

        if processed_docs % 50 == 0:
            logger.info(
                "prepare_dataset.progress",
                docs=processed_docs,
                total_docs=len(by_doc),
                crops=len(pairs),
            )

    return pairs


def split_dataset(
    pairs: list[tuple[str, str]],
    val_ratio: float,
    test_ratio: float,
    seed: int = _SEED,
) -> tuple[list, list, list]:
    random.seed(seed)
    shuffled = list(pairs)
    random.shuffle(shuffled)
    n = len(shuffled)
    n_test = max(1, int(n * test_ratio))
    n_val  = max(1, int(n * val_ratio))
    return shuffled[n_test + n_val:], shuffled[n_test:n_test + n_val], shuffled[:n_test]


def write_split(pairs: list[tuple[str, str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for img_path, text in pairs:
            f.write(f"{img_path}\t{text}\n")


async def run(args: argparse.Namespace) -> None:
    out = Path(args.output_dir)
    crops_dir = out / "crops"

    logger.info("prepare_dataset.querying_db")
    records = await fetch_corrected_fields(args.max_samples)
    logger.info("prepare_dataset.records_found", count=len(records))

    if not records:
        logger.warning(
            "prepare_dataset.no_records",
            hint=(
                "Ensure: (1) HITL reviewers have corrected OCR errors in admin UI, "
                "(2) OCR pipeline ran after bbox storage was added, "
                "(3) FieldConfidence.bbox column exists (run Alembic migration)"
            ),
        )
        return

    logger.info("prepare_dataset.generating_crops", crops_dir=str(crops_dir))
    pairs = await build_pairs(records, crops_dir)
    logger.info("prepare_dataset.crops_generated", count=len(pairs))

    if not pairs:
        logger.warning(
            "prepare_dataset.no_crops",
            hint="Check that MinIO documents are accessible and bboxes are non-empty.",
        )
        return

    train, val, test = split_dataset(pairs, args.val_ratio, args.test_ratio)
    write_split(train, out / "train.txt")
    write_split(val,   out / "val.txt")
    write_split(test,  out / "test.txt")

    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "total_records_queried": len(records),
        "total_crops": len(pairs),
        "train": len(train),
        "val":   len(val),
        "test":  len(test),
        "val_ratio":  args.val_ratio,
        "test_ratio": args.test_ratio,
        "seed": _SEED,
        "crops_dir": str(crops_dir),
        "note": (
            "PaddleOCR rec format: each line is 'crops/uuid.jpg<TAB>label'. "
            "data_dir in the YAML config must point to the parent of crops/."
        ),
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    logger.info(
        "prepare_dataset.done",
        output_dir=str(out),
        train=len(train),
        val=len(val),
        test=len(test),
        next_step=f"python training/scripts/generate_finetune_config.py --data-dir {out}",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Prepare PaddleOCR rec finetuning dataset from HITL corrections"
    )
    parser.add_argument(
        "--output-dir", default="training/data/splits",
        help="Directory to write train.txt / val.txt / test.txt and crops/",
    )
    parser.add_argument("--val-ratio",   type=float, default=0.1)
    parser.add_argument("--test-ratio",  type=float, default=0.1)
    parser.add_argument(
        "--max-samples", type=int, default=20_000,
        help="Maximum number of corrected records to include",
    )
    args = parser.parse_args()
    asyncio.run(run(args))
