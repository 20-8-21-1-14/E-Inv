"""Image preprocessing pipeline for scanned invoices.

Stage order (CV-validated):
  1. DPI detection / normalisation
  2. Super-resolution  (ESRGAN 4x, only when effective DPI < 150)
  3. Orientation correction  (PaddleOCR angle classifier)
  4. Border crop  (remove blank margins)
  5. Perspective correction  (four-corner unwarp)
  6. Deskew  (Hough-line angle estimation)
  7. Denoise  (fastNlMeansDenoisingColored)
  8. CLAHE contrast enhancement

Output is always a continuous BGR uint8 image — PaddleOCR models are trained
on continuous BGR input and must NOT receive a binarized single-channel image.
Each step is optional and logged. CPU-bound — call via run_in_executor.
"""

from __future__ import annotations

import math
from typing import NamedTuple

import cv2
import numpy as np
import structlog

logger = structlog.get_logger()

_MIN_DPI_FOR_SR = 150   # below this, apply super-resolution
_TARGET_DPI     = 300   # normalise all inputs to this equivalent


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

class PreprocessResult(NamedTuple):
    image: np.ndarray       # BGR uint8
    steps_applied: list[str]
    effective_dpi: int


def preprocess(image: np.ndarray, dpi_hint: int = 0) -> PreprocessResult:
    """Run the full preprocessing pipeline on a BGR or RGB image array.

    Args:
        image:    Input image as numpy uint8 array (H×W×3, BGR preferred).
        dpi_hint: DPI reported by the source (0 = unknown).

    Returns:
        PreprocessResult with corrected image and list of applied step names.
    """
    steps: list[str] = []
    img = image.copy()

    # ── 1. DPI normalisation ─────────────────────────────────────────────────
    effective_dpi = dpi_hint if dpi_hint > 0 else _estimate_dpi(img)
    if effective_dpi > 0 and effective_dpi < _TARGET_DPI:
        scale = _TARGET_DPI / effective_dpi
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
        steps.append(f"dpi_scale_{effective_dpi}→{_TARGET_DPI}")
    effective_dpi = max(effective_dpi, _TARGET_DPI)

    # ── 2. Super-resolution ──────────────────────────────────────────────────
    if dpi_hint > 0 and dpi_hint < _MIN_DPI_FOR_SR:
        img, applied = _super_resolve(img)
        if applied:
            steps.append("super_resolution")

    # ── 3. Orientation correction ────────────────────────────────────────────
    img, rotated = _correct_orientation(img)
    if rotated:
        steps.append("orientation_fix")

    # ── 4. Border crop ───────────────────────────────────────────────────────
    img, cropped = _border_crop(img)
    if cropped:
        steps.append("border_crop")

    # ── 5. Perspective correction ────────────────────────────────────────────
    img, warped = _perspective_correct(img)
    if warped:
        steps.append("perspective_warp")

    # ── 6. Deskew ────────────────────────────────────────────────────────────
    img, angle = _deskew(img)
    if abs(angle) > 0.3:
        steps.append(f"deskew_{angle:.1f}deg")

    # ── 7. Denoise ───────────────────────────────────────────────────────────
    img = _denoise(img)
    steps.append("denoise")

    # ── 8. CLAHE ─────────────────────────────────────────────────────────────
    img = _clahe(img)
    steps.append("clahe")

    logger.debug("preprocess.done", steps=steps, effective_dpi=effective_dpi,
                 shape=img.shape)
    return PreprocessResult(image=img, steps_applied=steps, effective_dpi=effective_dpi)


# ---------------------------------------------------------------------------
# Step implementations
# ---------------------------------------------------------------------------

def _estimate_dpi(img: np.ndarray) -> int:
    """Heuristic: assume A4 portrait; estimate DPI from image height."""
    h = img.shape[0]
    # A4 at 300 DPI → 3508 px tall; at 150 DPI → 1754 px
    if h >= 3000:
        return 300
    if h >= 1700:
        return 150
    return 96


def _super_resolve(img: np.ndarray) -> tuple[np.ndarray, bool]:
    """Apply ESRGAN 4× if model is available, else bicubic 2× fallback."""
    try:
        from basicsr.archs.rrdbnet_arch import RRDBNet  # type: ignore
        from realesrgan import RealESRGANer               # type: ignore

        model = RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64,
                        num_block=23, num_grow_ch=32, scale=4)
        upsampler = RealESRGANer(scale=4, model_path="", model=model,
                                  tile=0, tile_pad=10, pre_pad=0, half=False)
        out, _ = upsampler.enhance(img, outscale=4)
        return out, True
    except Exception:
        # Bicubic 2× fallback — always available
        out = cv2.resize(img, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
        return out, True


def _correct_orientation(img: np.ndarray) -> tuple[np.ndarray, bool]:
    """Rotate to upright orientation using PaddleOCR angle classifier."""
    try:
        from paddleocr import PaddleOCR  # type: ignore
        ocr = PaddleOCR(use_angle_cls=True, show_log=False, lang="vi")
        result = ocr.ocr(img, det=False, rec=False, cls=True)
        if result and result[0]:
            angle_label = result[0][0][0]  # '0' or '180'
            if angle_label == "180":
                img = cv2.rotate(img, cv2.ROTATE_180)
                return img, True
    except Exception:
        pass
    return img, False


def _border_crop(img: np.ndarray, threshold: int = 250, min_border: int = 10) -> tuple[np.ndarray, bool]:
    """Remove white/near-white borders."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    mask = gray < threshold
    rows = np.any(mask, axis=1)
    cols = np.any(mask, axis=0)
    if not rows.any():
        return img, False
    rmin, rmax = np.where(rows)[0][[0, -1]]
    cmin, cmax = np.where(cols)[0][[0, -1]]
    h, w = img.shape[:2]
    if (rmin < min_border and rmax > h - min_border and
            cmin < min_border and cmax > w - min_border):
        return img, False
    rmin = max(0, rmin - min_border)
    rmax = min(h, rmax + min_border)
    cmin = max(0, cmin - min_border)
    cmax = min(w, cmax + min_border)
    return img[rmin:rmax, cmin:cmax], True


def _perspective_correct(img: np.ndarray) -> tuple[np.ndarray, bool]:
    """Find and unwarp document quadrilateral, if clearly non-rectangular."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blurred, 50, 150)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return img, False

    largest = max(contours, key=cv2.contourArea)
    peri = cv2.arcLength(largest, True)
    approx = cv2.approxPolyDP(largest, 0.02 * peri, True)

    if len(approx) != 4:
        return img, False

    h, w = img.shape[:2]
    # Only warp if quad significantly differs from image bounds
    area_ratio = cv2.contourArea(largest) / (h * w)
    if area_ratio > 0.95:
        return img, False

    pts = approx.reshape(4, 2).astype(np.float32)
    pts = _order_points(pts)
    dst = np.array([[0, 0], [w - 1, 0], [w - 1, h - 1], [0, h - 1]], dtype=np.float32)
    M = cv2.getPerspectiveTransform(pts, dst)
    warped = cv2.warpPerspective(img, M, (w, h), flags=cv2.INTER_LINEAR)
    return warped, True


def _order_points(pts: np.ndarray) -> np.ndarray:
    rect = np.zeros((4, 2), dtype=np.float32)
    s = pts.sum(axis=1)
    rect[0] = pts[np.argmin(s)]   # top-left
    rect[2] = pts[np.argmax(s)]   # bottom-right
    diff = np.diff(pts, axis=1)
    rect[1] = pts[np.argmin(diff)]  # top-right
    rect[3] = pts[np.argmax(diff)]  # bottom-left
    return rect


def _deskew(img: np.ndarray, max_angle: float = 10.0) -> tuple[np.ndarray, float]:
    """Correct small rotation using Hough line detection."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    edges = cv2.Canny(gray, 50, 150, apertureSize=3)
    lines = cv2.HoughLines(edges, 1, np.pi / 180, threshold=100)
    if lines is None:
        return img, 0.0

    angles = []
    for line in lines[:50]:
        theta = line[0][1]
        angle = (theta * 180 / np.pi) - 90
        if abs(angle) <= max_angle:
            angles.append(angle)

    if not angles:
        return img, 0.0

    median_angle = float(np.median(angles))
    if abs(median_angle) < 0.3:
        return img, 0.0

    h, w = img.shape[:2]
    M = cv2.getRotationMatrix2D((w / 2, h / 2), median_angle, 1.0)
    rotated = cv2.warpAffine(img, M, (w, h),
                              flags=cv2.INTER_LINEAR,
                              borderMode=cv2.BORDER_REPLICATE)
    return rotated, median_angle


def _denoise(img: np.ndarray) -> np.ndarray:
    if img.ndim == 3:
        return cv2.fastNlMeansDenoisingColored(img, None, h=10, hColor=10,
                                               templateWindowSize=7, searchWindowSize=21)
    return cv2.fastNlMeansDenoising(img, None, h=10, templateWindowSize=7, searchWindowSize=21)


def _clahe(img: np.ndarray) -> np.ndarray:
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    if img.ndim == 3:
        lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
        lab[:, :, 0] = clahe.apply(lab[:, :, 0])
        return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
    return clahe.apply(img)


