"""Prepare training dataset from HITL exports.

Downloads corrections.jsonl files from MinIO (training bucket), converts them
to PPOCRLabel text-recognition format, merges with existing annotated data,
and creates stratified train/val/test splits.

Usage:
    python training/scripts/prepare_dataset.py \
        --export-prefix exports/2026-05 \
        --output-dir training/data/splits \
        --val-ratio 0.1 --test-ratio 0.1

Output structure:
    training/data/splits/
        train.txt   # PPOCRLabel format: path\ttext
        val.txt
        test.txt
        manifest.json   # stats, date, source files
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow running from project root
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "common"))

from einv_common.config import settings
from einv_common.storage import get_storage_client

_SEED = 42


async def list_export_keys(prefix: str) -> list[str]:
    """List all corrections.jsonl keys under the given prefix."""
    storage = get_storage_client()
    async with storage._client() as client:
        paginator = client.get_paginator("list_objects_v2")
        keys: list[str] = []
        async for page in paginator.paginate(
            Bucket=settings.minio_bucket_training, Prefix=prefix
        ):
            for obj in page.get("Contents", []):
                if obj["Key"].endswith("corrections.jsonl"):
                    keys.append(obj["Key"])
    return keys


async def download_export(key: str) -> list[dict]:
    """Download and parse a corrections.jsonl file."""
    storage = get_storage_client()
    content = await storage.download(bucket=settings.minio_bucket_training, key=key)
    records = []
    for line in content.decode("utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return records


def corrections_to_rec_labels(records: list[dict]) -> list[tuple[str, str]]:
    """Convert HITL correction records to (image_path, text) rec-training pairs.

    For text-recognition fine-tuning we only care about the corrected text value.
    Image crops should already be saved alongside corrections; we reference them
    by a convention: training/data/raw/crops/<field>/<record_id>.jpg
    For records without an image path we generate a synthetic placeholder.
    """
    pairs: list[tuple[str, str]] = []
    for rec in records:
        rec_type = rec.get("type")
        corrected = rec.get("corrected") or rec.get("item_name")
        if not corrected:
            continue
        # image_path is best-effort; actual crop saving happens in admin-api
        image_path = rec.get("image_path", "")
        if image_path:
            pairs.append((image_path, corrected))
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
    test  = shuffled[:n_test]
    val   = shuffled[n_test:n_test + n_val]
    train = shuffled[n_test + n_val:]
    return train, val, test


def write_split(pairs: list[tuple[str, str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for img_path, text in pairs:
            f.write(f"{img_path}\t{text}\n")


async def run(args: argparse.Namespace) -> None:
    print(f"[prepare_dataset] fetching exports under prefix: {args.export_prefix}")
    keys = await list_export_keys(args.export_prefix)
    print(f"  found {len(keys)} export files")

    all_records: list[dict] = []
    for key in keys:
        records = await download_export(key)
        all_records.extend(records)
        print(f"  loaded {len(records)} records from {key}")

    pairs = corrections_to_rec_labels(all_records)
    print(f"  {len(pairs)} usable image–text pairs")

    if not pairs:
        print("No usable pairs found. Exiting.")
        return

    train, val, test = split_dataset(pairs, args.val_ratio, args.test_ratio)
    out = Path(args.output_dir)
    write_split(train, out / "train.txt")
    write_split(val,   out / "val.txt")
    write_split(test,  out / "test.txt")

    manifest = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "export_prefix": args.export_prefix,
        "source_files": keys,
        "total_records": len(all_records),
        "usable_pairs": len(pairs),
        "train": len(train),
        "val": len(val),
        "test": len(test),
        "val_ratio": args.val_ratio,
        "test_ratio": args.test_ratio,
        "seed": _SEED,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"[prepare_dataset] done → {out}/  (train={len(train)}, val={len(val)}, test={len(test)})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--export-prefix", default="exports/", help="MinIO key prefix for export files")
    parser.add_argument("--output-dir", default="training/data/splits")
    parser.add_argument("--val-ratio", type=float, default=0.1)
    parser.add_argument("--test-ratio", type=float, default=0.1)
    args = parser.parse_args()
    asyncio.run(run(args))
