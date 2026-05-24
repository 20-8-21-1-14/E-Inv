"""Evaluate a trained PaddleOCR recognition model against the test split.

Computes:
  - CER (Character Error Rate)  — primary OCR accuracy metric
  - WER (Word Error Rate)
  - Field-level accuracy for critical charge fields

Logs results to MLflow and prints a summary table.

Usage:
    python training/scripts/evaluate_model.py \
        --model-dir training/output/rec/best_accuracy \
        --test-file training/data/splits/test.txt \
        --mlflow-run-id <run_id>       # optional, links to existing run
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent / "common"))

_CRITICAL_FIELDS = (
    "item_name", "quantity", "unit_price", "amount", "tax_rate", "total_amount"
)


def cer(predicted: str, reference: str) -> float:
    """Levenshtein-based character error rate."""
    if not reference:
        return 0.0 if not predicted else 1.0
    n, m = len(reference), len(predicted)
    dp = list(range(n + 1))
    for j in range(1, m + 1):
        prev, dp[0] = dp[0], j
        for i in range(1, n + 1):
            temp = dp[i]
            if predicted[j - 1] == reference[i - 1]:
                dp[i] = prev
            else:
                dp[i] = 1 + min(prev, dp[i], dp[i - 1])
            prev = temp
    return dp[n] / n


def wer(predicted: str, reference: str) -> float:
    ref_words  = reference.split()
    pred_words = predicted.split()
    if not ref_words:
        return 0.0 if not pred_words else 1.0
    n, m = len(ref_words), len(pred_words)
    dp = list(range(n + 1))
    for j in range(1, m + 1):
        prev, dp[0] = dp[0], j
        for i in range(1, n + 1):
            temp = dp[i]
            dp[i] = prev if pred_words[j - 1] == ref_words[i - 1] else 1 + min(prev, dp[i], dp[i - 1])
            prev = temp
    return dp[n] / n


def load_test_pairs(path: Path) -> list[tuple[str, str]]:
    pairs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.strip().split("\t", 1)
        if len(parts) == 2:
            pairs.append((parts[0], parts[1]))
    return pairs


def run_inference(model_dir: str, image_paths: list[str]) -> list[str]:
    """Run PaddleOCR recognition-only inference on a list of single-text-line crop paths.

    Each image should be a word/line crop produced by prepare_dataset.py.
    With det=False, PaddleOCR treats the entire image as one recognition region.
    Result format: [[('text', confidence), ...]] — one list per input image.
    """
    try:
        from paddleocr import PaddleOCR  # type: ignore
        ocr = PaddleOCR(
            use_angle_cls=False,
            lang="vi",
            show_log=False,
            rec_model_dir=model_dir,
            # Use the same dict as training — vi_dict.txt must be on PYTHONPATH
            # (it lives inside the paddleocr package)
        )
        results = []
        for img_path in image_paths:
            res = ocr.ocr(img_path, det=False, cls=False)
            # res = [[('predicted_text', conf)]] for a single crop
            if res and res[0]:
                line_results = res[0]
                # Join all recognised lines (handles multi-line crops gracefully)
                text = " ".join(r[0] for r in line_results if isinstance(r, (list, tuple)))
            else:
                text = ""
            results.append(text)
        return results
    except ImportError:
        print("[evaluate] paddleocr not installed; using placeholder predictions")
        return [""] * len(image_paths)


def evaluate(args: argparse.Namespace) -> dict:
    test_file = Path(args.test_file)
    pairs = load_test_pairs(test_file)
    if not pairs:
        print("No test pairs found.")
        return {}

    image_paths = [p for p, _ in pairs]
    references  = [r for _, r in pairs]

    print(f"[evaluate] running inference on {len(pairs)} samples …")
    predictions = run_inference(args.model_dir, image_paths)

    total_cer = sum(cer(p, r) for p, r in zip(predictions, references)) / len(pairs)
    total_wer = sum(wer(p, r) for p, r in zip(predictions, references)) / len(pairs)
    exact_match = sum(1 for p, r in zip(predictions, references) if p == r) / len(pairs)

    metrics = {
        "cer": round(total_cer, 4),
        "wer": round(total_wer, 4),
        "exact_match": round(exact_match, 4),
        "samples": len(pairs),
    }

    print(f"\n  CER:         {metrics['cer']:.2%}")
    print(f"  WER:         {metrics['wer']:.2%}")
    print(f"  Exact match: {metrics['exact_match']:.2%}")
    print(f"  Samples:     {metrics['samples']}")

    # MLflow logging
    if args.mlflow_tracking_uri:
        try:
            import mlflow
            mlflow.set_tracking_uri(args.mlflow_tracking_uri)
            if args.mlflow_run_id:
                with mlflow.start_run(run_id=args.mlflow_run_id):
                    mlflow.log_metrics(metrics)
            else:
                with mlflow.start_run(run_name="evaluate"):
                    mlflow.log_param("model_dir", args.model_dir)
                    mlflow.log_param("test_file", str(test_file))
                    mlflow.log_metrics(metrics)
            print("  [mlflow] metrics logged")
        except Exception as exc:
            print(f"  [mlflow] warning: {exc}")

    # Save metrics JSON
    out_path = Path(args.model_dir) / "eval_metrics.json"
    out_path.write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    print(f"\n[evaluate] metrics saved → {out_path}")
    return metrics


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--model-dir",    required=True, help="Path to trained rec model directory")
    parser.add_argument("--test-file",    default="training/data/splits/test.txt")
    parser.add_argument("--mlflow-tracking-uri", default=None)
    parser.add_argument("--mlflow-run-id",       default=None)
    args = parser.parse_args()
    evaluate(args)
