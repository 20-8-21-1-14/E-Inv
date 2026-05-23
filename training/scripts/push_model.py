"""Version and publish a trained model to the MinIO model registry.

After training and evaluation, run this script to:
  1. Read eval_metrics.json from the model directory
  2. Upload model files to MinIO under e-invoice-models/<type>/<version>/
  3. Create a ModelVersion record in the database
  4. Optionally promote the new version to active (replacing the current one)

Usage:
    python training/scripts/push_model.py \
        --model-type rec \
        --model-dir training/output/rec/best_accuracy \
        --version v1.1.0 \
        --promote       # immediately set as active serving version
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "common"))

from einv_common.config import settings
from einv_common.db import session_factory
from einv_common.models.training import ModelVersion
from einv_common.storage import get_storage_client
from sqlalchemy import select, update

_MODEL_FILES = {
    "rec":    ["inference.pdmodel", "inference.pdiparams"],
    "det":    ["inference.pdmodel", "inference.pdiparams"],
    "table":  ["inference.pdmodel", "inference.pdiparams"],
    "layout": ["inference.pdmodel", "inference.pdiparams"],
    "sr":     ["espcn.onnx"],
}


async def upload_model_files(model_type: str, version: str, model_dir: Path) -> list[str]:
    """Upload model binary files to MinIO. Returns list of uploaded keys."""
    storage = get_storage_client()
    expected = _MODEL_FILES.get(model_type, [])
    uploaded: list[str] = []

    for filename in expected:
        local_path = model_dir / filename
        if not local_path.exists():
            print(f"  [warn] {filename} not found in {model_dir}, skipping")
            continue
        key = f"{model_type}/{version}/{filename}"
        content = local_path.read_bytes()
        await storage.upload(
            bucket=settings.minio_bucket_models,
            key=key,
            data=content,
            content_type="application/octet-stream",
        )
        print(f"  uploaded {filename} → s3://{settings.minio_bucket_models}/{key}")
        uploaded.append(key)

    return uploaded


async def create_model_version(
    model_type: str,
    version: str,
    minio_key: str,
    metrics: dict,
    training_samples: int | None,
    mlflow_run_id: str | None,
    promote: bool,
) -> None:
    async with session_factory() as session:
        if promote:
            # Deactivate current active version of this type
            await session.execute(
                update(ModelVersion)
                .where(ModelVersion.model_type == model_type)
                .where(ModelVersion.is_active == True)    # noqa: E712
                .values(is_active=False)
            )

        mv = ModelVersion(
            model_type=model_type,
            version=version,
            minio_key=minio_key,
            metrics=metrics,
            training_samples=training_samples,
            mlflow_run_id=mlflow_run_id,
            is_active=promote,
            promoted_at=datetime.now(timezone.utc) if promote else None,
        )
        session.add(mv)
        await session.commit()
        print(f"  ModelVersion record created: {mv.id} (active={promote})")


async def run(args: argparse.Namespace) -> None:
    model_dir = Path(args.model_dir)
    if not model_dir.exists():
        print(f"[error] model directory not found: {model_dir}")
        sys.exit(1)

    # Load metrics
    metrics: dict = {}
    metrics_path = model_dir / "eval_metrics.json"
    if metrics_path.exists():
        metrics = json.loads(metrics_path.read_text())
        print(f"[push_model] metrics: {metrics}")
    else:
        print("[push_model] no eval_metrics.json found — metrics will be empty")

    print(f"[push_model] uploading {args.model_type} v{args.version} …")
    keys = await upload_model_files(args.model_type, args.version, model_dir)

    if not keys:
        print("[error] no model files uploaded")
        sys.exit(1)

    # Use first uploaded key as the primary minio_key
    primary_key = keys[0]

    await create_model_version(
        model_type=args.model_type,
        version=args.version,
        minio_key=primary_key,
        metrics=metrics,
        training_samples=args.training_samples,
        mlflow_run_id=args.mlflow_run_id,
        promote=args.promote,
    )

    print(f"\n[push_model] done")
    if args.promote:
        print(f"  ** {args.model_type} v{args.version} is now the active serving version **")
    else:
        print(f"  Run with --promote to set as active")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-type",   required=True, choices=["rec", "det", "table", "layout", "sr"])
    parser.add_argument("--model-dir",    required=True)
    parser.add_argument("--version",      required=True, help="e.g. v1.1.0")
    parser.add_argument("--promote",      action="store_true", help="Set as active serving version")
    parser.add_argument("--training-samples", type=int, default=None)
    parser.add_argument("--mlflow-run-id", default=None)
    args = parser.parse_args()
    asyncio.run(run(args))
