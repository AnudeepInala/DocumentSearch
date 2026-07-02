"""
Tiered OCR Router — routes each image to the fastest engine that can handle it.

Tier 1: Tika          (<1s)  — digital docs (.docx/.pdf/.xlsx) — handled upstream
Tier 2: PP-OCRv4      (2-4s) — clean, sharp, high-contrast printed images
Tier 3: VL-1.6        (65s)  — degraded, rotated, handwritten, complex layouts

Quality thresholds (tunable via config.yaml ocr.tiered section):
  brightness: mean pixel value of grayscale image
  contrast:   std deviation of pixel values
  sharpness:  Laplacian variance (edge sharpness)
  pp_ocr_min_confidence: minimum confidence from PP-OCRv4 to accept result
                         if below this, escalate to VL-1.6
"""

import os
import tempfile
from typing import Optional, Tuple

from core.logging_manager import get_logger

logger = get_logger("ocr.tier_router")

# Default quality thresholds — override in config.yaml under ocr.tiered
_DEFAULT_THRESHOLDS = {
    "min_contrast":     30.0,   # std < 30  → faded/degraded → VL1.6
    "min_sharpness":    80.0,   # laplacian var < 80 → blurry → VL1.6
    "min_brightness":   40.0,   # mean < 40 → too dark → VL1.6
    "max_brightness":  230.0,   # mean > 230 → over-exposed (allow, usually fine)
    "pp_min_confidence": 70.0,  # if PP-OCRv4 returns conf < 70% → escalate to VL1.6
}


def _analyze_image_bytes(image_bytes: bytes) -> Tuple[float, float, float]:
    """Returns (brightness, contrast, sharpness) from raw image bytes."""
    try:
        import numpy as np
        import cv2
        arr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return 128.0, 50.0, 200.0
        brightness = float(img.mean())
        contrast   = float(img.std())
        sharpness  = float(cv2.Laplacian(img, cv2.CV_64F).var())
        return brightness, contrast, sharpness
    except Exception:
        return 128.0, 50.0, 200.0


def _analyze_image_path(image_path: str) -> Tuple[float, float, float]:
    """Returns (brightness, contrast, sharpness) from an image file path."""
    try:
        import numpy as np
        import cv2
        img = cv2.imread(image_path, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return 128.0, 50.0, 200.0
        brightness = float(img.mean())
        contrast   = float(img.std())
        sharpness  = float(cv2.Laplacian(img, cv2.CV_64F).var())
        return brightness, contrast, sharpness
    except Exception:
        return 128.0, 50.0, 200.0


def is_clean_image(image_path: str, thresholds: dict = None) -> Tuple[bool, str]:
    """Return (is_clean, reason) — True means PP-OCRv4 is sufficient."""
    t = {**_DEFAULT_THRESHOLDS, **(thresholds or {})}
    brightness, contrast, sharpness = _analyze_image_path(image_path)

    if brightness < t["min_brightness"]:
        return False, f"too dark (brightness={brightness:.0f} < {t['min_brightness']})"
    if contrast < t["min_contrast"]:
        return False, f"low contrast (contrast={contrast:.0f} < {t['min_contrast']})"
    if sharpness < t["min_sharpness"]:
        return False, f"blurry (sharpness={sharpness:.0f} < {t['min_sharpness']})"

    return True, f"clean (brightness={brightness:.0f}, contrast={contrast:.0f}, sharpness={sharpness:.0f})"


class TieredOCREngine:
    """Drop-in replacement for VL16Wrapper / PaddleWrapper.

    Routes each image:
      - Clean images  → PP-OCRv4 (fast, 2-4s)
      - Degraded      → VL-1.6   (accurate, 65s)

    If PP-OCRv4 returns low confidence it automatically escalates to VL-1.6.
    Public interface is identical to VL16Wrapper.
    """

    def __init__(self):
        from core.config_manager import get_config
        cfg = get_config()
        tiered_cfg = getattr(cfg.ocr, "tiered", {}) or {}
        if not isinstance(tiered_cfg, dict):
            tiered_cfg = {}

        self.thresholds = {**_DEFAULT_THRESHOLDS, **tiered_cfg}
        self.pp_min_conf = float(self.thresholds.get("pp_min_confidence", 70.0))

        self.psm = "3"  # compatibility no-op

        self.pages_processed = 0
        self.total_confidence = 0.0
        self.errors = 0
        self.tier2_count = 0
        self.tier3_count = 0

        # Lazy-loaded engines
        self._pp_engine  = None
        self._vl_engine  = None

        logger.info(
            f"TieredOCREngine ready — "
            f"min_contrast={self.thresholds['min_contrast']}, "
            f"min_sharpness={self.thresholds['min_sharpness']}, "
            f"pp_min_conf={self.pp_min_conf}%"
        )

    def _get_pp(self):
        if self._pp_engine is None:
            from .paddle_wrapper import PaddleWrapper
            self._pp_engine = PaddleWrapper()
        return self._pp_engine

    def _get_vl(self):
        if self._vl_engine is None:
            from .vl16_wrapper import VL16Wrapper
            self._vl_engine = VL16Wrapper()
        return self._vl_engine

    def extract_text(self, image_path: str) -> Optional[Tuple[str, float]]:
        clean, reason = is_clean_image(image_path, self.thresholds)

        if clean:
            result = self._get_pp().extract_text(image_path)
            if result:
                text, conf = result
                if conf >= self.pp_min_conf and text.strip():
                    logger.debug(f"Tier2 PP-OCRv4 OK ({conf:.0f}%): {os.path.basename(image_path)}")
                    self.tier2_count += 1
                    self.pages_processed += 1
                    self.total_confidence += conf
                    return result
                # Low confidence from PP-OCRv4 — escalate
                logger.info(
                    f"Tier2→3 escalation: PP-OCRv4 conf={conf:.0f}% < {self.pp_min_conf}% "
                    f"on {os.path.basename(image_path)}"
                )
            else:
                logger.info(f"Tier2→3 escalation: PP-OCRv4 returned no result on {os.path.basename(image_path)}")
        else:
            logger.info(f"Tier3 VL-1.6 (degraded — {reason}): {os.path.basename(image_path)}")

        # Tier 3 — VL-1.6
        result = self._get_vl().extract_text(image_path)
        if result:
            self.tier3_count += 1
            self.pages_processed += 1
            self.total_confidence += result[1]
        else:
            self.errors += 1
        return result

    def health_check(self) -> bool:
        try:
            return self._get_pp().health_check() or self._get_vl().health_check()
        except Exception:
            return False

    def get_version(self) -> Optional[str]:
        return "TieredOCR (PP-OCRv4 + VL-1.6)"

    def get_stats(self) -> dict:
        avg_conf = self.total_confidence / self.pages_processed if self.pages_processed else 0.0
        return {
            "pages_processed": self.pages_processed,
            "average_confidence": avg_conf,
            "errors": self.errors,
            "tier2_pp_ocr": self.tier2_count,
            "tier3_vl16": self.tier3_count,
        }
