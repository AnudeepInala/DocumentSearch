"""
PaddleOCR Wrapper - Integration with PaddleOCR engine (replaces Tesseract)
"""

import os
import tempfile
from pathlib import Path
from typing import Optional, Dict, Any, Tuple, List, Set

# Inject Windows certificate store into Python's SSL so Zscaler-signed certs
# are trusted during PaddleX model downloads from ModelScope / HuggingFace.
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

from core.logging_manager import get_logger
from core.config_manager import get_config

logger = get_logger("ocr.paddle")

# Suppress excessive PaddleOCR / PaddlePaddle logging at import time
os.environ.setdefault("PADDLE_CPP_LOG_LEVEL", "3")   # ERROR only
os.environ.setdefault("FLAGS_logtostderr", "0")       # Don't flood stderr
os.environ.setdefault("FLAGS_use_onednn", "0")        # Disable OneDNN to bypass pir::ArrayAttribute bug on Windows CPU
os.environ.setdefault("FLAGS_use_mkldnn", "0")        # Ensure mkldnn is disabled
os.environ.setdefault("PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT", "0")
os.environ.setdefault("OMP_NUM_THREADS", "1")         # Windows-safe: bypass multi-process OpenMP conflicts
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("VECLIB_MAXIMUM_THREADS", "1")
os.environ.setdefault("NUMEXPR_NUM_THREADS", "1")
# Skip the model-hoster connectivity pre-check so corporate firewalls don't block startup.
# Models will still be downloaded from the first reachable source (ModelScope, BOS, etc.).
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")

try:
    from paddleocr import PaddleOCR
    PADDLEOCR_AVAILABLE = True
except ImportError:
    PADDLEOCR_AVAILABLE = False
    logger.warning("paddleocr package not found. Install with: pip install paddleocr paddlepaddle")


class PaddleWrapper:
    """Wrapper for PaddleOCR with confidence scoring.

    Drop-in replacement for TesseractWrapper.  The public interface is identical:
        extract_text(image_path) -> Optional[Tuple[str, float]]
        health_check()           -> bool
        get_version()            -> Optional[str]
        get_stats()              -> Dict[str, Any]
        .psm                     (accepted attribute, no-op — PaddleOCR handles layout internally)
    """

    def __init__(self):
        self.config = get_config()

        # Read paddle sub-section — config_manager always provides a PaddleConfig
        # dataclass, so use attribute access (not dict .get())
        paddle_cfg = getattr(self.config.ocr, "paddle", None)

        self.use_gpu: bool = bool(getattr(paddle_cfg, "use_gpu", False))  # kept for reference only
        self.det: bool = bool(getattr(paddle_cfg, "det", True))
        self.rec: bool = bool(getattr(paddle_cfg, "rec", True))
        self.cls: bool = bool(getattr(paddle_cfg, "cls", True))
        self.timeout: int = int(getattr(paddle_cfg, "timeout_seconds", 120))

        # Scaling/Resizing limit settings for PaddleOCR text detection
        self.det_limit_side_len: Optional[int] = getattr(paddle_cfg, "det_limit_side_len", None)
        if self.det_limit_side_len is not None:
            self.det_limit_side_len = int(self.det_limit_side_len)

        self.det_limit_type: Optional[str] = getattr(paddle_cfg, "det_limit_type", None)
        if self.det_limit_type is not None:
            self.det_limit_type = str(self.det_limit_type)

        self.max_side_limit: Optional[int] = getattr(paddle_cfg, "max_side_limit", None)
        if self.max_side_limit is not None:
            self.max_side_limit = int(self.max_side_limit)

        # Handle list, comma-separated string, or single string for langs config
        raw_lang = getattr(paddle_cfg, "lang", "en")
        langs = []
        if isinstance(raw_lang, list):
            langs = [str(l).strip() for l in raw_lang if l]
        elif isinstance(raw_lang, str):
            langs = [str(l).strip() for l in raw_lang.split(",") if l.strip()]
        else:
            langs = ["en"]
            
        if not langs:
            langs = ["en"]

        # CRITICAL: Restrict self.langs to only the first language on Windows CPU
        # to prevent segmentation faults (0xC0000005) from multiple concurrently
        # loaded deep learning engines.
        self.langs = [langs[0]]
        self.lang = self.langs[0]

        # No-op PSM attribute — accepted to keep the OCR worker strategy loop compatible
        self.psm: str = "3"

        # Statistics
        self.pages_processed: int = 0
        self.total_confidence: float = 0.0
        self.errors: int = 0

        # Lazy-init: we create one PaddleOCR instance per language when needed
        self._ocrs: Dict[str, "PaddleOCR"] = {}
        self._init_failed_langs: Set[str] = set()

        if not PADDLEOCR_AVAILABLE:
            logger.error("PaddleOCR is not installed. OCR will not work.")
            return

        logger.info(
            f"PaddleWrapper configured — langs={self.langs}, "
            f"det={self.det}, rec={self.rec}, cls={self.cls}"
        )

    # ------------------------------------------------------------------
    # Lazy initialiser — PaddleOCR downloads models on first instantiation
    # ------------------------------------------------------------------
    def _get_ocr_for_lang(self, lang: str) -> Optional["PaddleOCR"]:
        """Return (and lazily initialise) the PaddleOCR engine for a specific language."""
        if lang in self._ocrs:
            return self._ocrs[lang]

        # Don't retry after a permanent init failure for this language
        if lang in self._init_failed_langs:
            return None

        if not PADDLEOCR_AVAILABLE:
            return None

        try:
            logger.info(f"Initialising PaddleOCR engine for lang='{lang}' (models may be downloaded on first run)...")
            # Note: use_gpu is NOT a constructor parameter in PaddleOCR >= 2.7.
            # Newer PaddleX-based PaddleOCR does not accept: 'det', 'rec', or 'show_log'.
            # We try the new constructor parameters first:
            try:
                kwargs = {
                    "lang": lang,
                    "enable_mkldnn": False,
                    "use_textline_orientation": self.cls,
                }
                if self.det_limit_side_len is not None:
                    kwargs["det_limit_side_len"] = self.det_limit_side_len
                if self.det_limit_type is not None:
                    kwargs["det_limit_type"] = self.det_limit_type
                if self.max_side_limit is not None:
                    kwargs["max_side_limit"] = self.max_side_limit

                ocr_inst = PaddleOCR(**kwargs)
                logger.info(f"PaddleOCR engine for lang='{lang}' ready (modern PaddleX interface).")
            except ValueError as ve:
                if "Unknown argument" in str(ve):
                    logger.info(f"Falling back to legacy PaddleOCR constructor parameters for lang='{lang}'...")
                    kwargs = {
                        "use_angle_cls": self.cls,
                        "lang": lang,
                        "det": self.det,
                        "rec": self.rec,
                        "show_log": False,
                        "enable_mkldnn": False,
                    }
                    if self.det_limit_side_len is not None:
                        kwargs["det_limit_side_len"] = self.det_limit_side_len
                    if self.det_limit_type is not None:
                        kwargs["det_limit_type"] = self.det_limit_type
                    if self.max_side_limit is not None:
                        kwargs["max_side_limit"] = self.max_side_limit
                    ocr_inst = PaddleOCR(**kwargs)
                    logger.info(f"PaddleOCR engine for lang='{lang}' ready (legacy interface).")
                else:
                    raise ve
            self._ocrs[lang] = ocr_inst
            return ocr_inst
        except Exception as e:
            logger.error(f"Failed to initialise PaddleOCR for lang='{lang}': {e}")
            self._init_failed_langs.add(lang)
            return None

    def prewarm(self) -> bool:
        """Eagerly initialise the primary OCR engine so the first real file
        is not penalised by model-load latency.  Called at worker startup.

        Returns True if the engine initialised successfully.
        """
        if not PADDLEOCR_AVAILABLE or not self.langs:
            return False
        primary = self.langs[0]
        logger.info(f"Pre-warming PaddleOCR engine for lang='{primary}' …")
        engine = self._get_ocr_for_lang(primary)
        if engine is not None:
            logger.info(f"PaddleOCR pre-warm complete for lang='{primary}'.")
            return True
        logger.warning(f"PaddleOCR pre-warm failed for lang='{primary}'.")
        return False

    # ------------------------------------------------------------------
    # Public API (mirrors TesseractWrapper)
    # ------------------------------------------------------------------
    def extract_text(self, image_path: str) -> Optional[Tuple[str, float]]:
        """Extract text from an image file using PaddleOCR.
        It will try all configured languages and return the result with the highest confidence.

        Args:
            image_path: Absolute path to the image file.

        Returns:
            (text, confidence_0_to_100) or None on error / no text.
        """
        if not PADDLEOCR_AVAILABLE:
            return None

        best_text = ""
        best_conf = -1.0
        success = False

        for lang in self.langs:
            ocr = self._get_ocr_for_lang(lang)
            if ocr is None:
                continue

            try:
                # PaddleOCR 3.x uses predict(), older uses ocr()
                if hasattr(ocr, "predict"):
                    result = ocr.predict(image_path)
                else:
                    try:
                        result = ocr.ocr(image_path, use_textline_orientation=self.cls)
                    except TypeError:
                        result = ocr.ocr(image_path, cls=self.cls)

                if not result or result == [None]:
                    # If this language got no results, check the next one
                    continue

                lines: list[str] = []
                confidences: list[float] = []

                for page in result:
                    if not page:
                        continue

                    # PaddleOCR 3.x predict() result (object with rec_texts property)
                    if hasattr(page, "rec_texts"):
                        rec_texts = page.rec_texts
                        rec_scores = getattr(page, "rec_scores", [])
                        if not rec_scores or len(rec_scores) != len(rec_texts):
                            rec_scores = [1.0] * len(rec_texts)
                        for text, conf in zip(rec_texts, rec_scores):
                            if isinstance(text, tuple):
                                text = text[0]
                            text = str(text).strip()
                            if text:
                                lines.append(text)
                                confidences.append(float(conf))
                    # Check if it is the newer PaddleX-based OCRResult (dict-like with "rec_texts")
                    elif isinstance(page, dict) and "rec_texts" in page:
                        rec_texts = page.get("rec_texts", [])
                        rec_scores = page.get("rec_scores", [])
                        for text, conf in zip(rec_texts, rec_scores):
                            if isinstance(text, tuple):
                                text = text[0]
                            text = str(text).strip()
                            if text:
                                lines.append(text)
                                # newer version rec_scores are in [0, 1.0]. We want [0, 1.0] for the loop.
                                confidences.append(float(conf))
                    else:
                        # Old style list of box results: box_result = [bbox_points, (text, confidence)]
                        for box_result in page:
                            try:
                                text_conf = box_result[1]
                                text = str(text_conf[0]).strip()
                                conf = float(text_conf[1])        # 0.0 – 1.0
                                if text:
                                    lines.append(text)
                                    confidences.append(conf)
                            except (IndexError, TypeError, ValueError):
                                continue

                combined_text = "\n".join(lines)
                avg_conf = (
                    (sum(confidences) / len(confidences)) * 100.0
                    if confidences
                    else 0.0
                )
                
                success = True
                # Select the result that has the highest non-zero confidence.
                # Regional script models (like Telugu, Hindi, etc.) will yield higher confidence
                # on matching documents compared to running the English model on them.
                if avg_conf > best_conf:
                    best_conf = avg_conf
                    best_text = combined_text

            except Exception as e:
                logger.error(f"PaddleOCR error on {image_path} with lang='{lang}': {e}")

        if not success:
            self.errors += 1
            return None

        self.pages_processed += 1
        self.total_confidence += best_conf

        return (best_text, best_conf)

    def health_check(self) -> bool:
        """Return True if PaddleOCR is installed and at least one engine can be initialised."""
        if not PADDLEOCR_AVAILABLE:
            return False
        try:
            if not self.langs:
                return False
            return self._get_ocr_for_lang(self.langs[0]) is not None
        except Exception:
            return False

    def get_version(self) -> Optional[str]:
        """Return the installed PaddleOCR version string."""
        try:
            import paddleocr
            return f"PaddleOCR {getattr(paddleocr, '__version__', 'unknown')}"
        except Exception:
            return None

    def get_stats(self) -> Dict[str, Any]:
        """Return processing statistics (same shape as TesseractWrapper.get_stats)."""
        avg_confidence = (
            self.total_confidence / self.pages_processed
            if self.pages_processed > 0
            else 0.0
        )
        return {
            "pages_processed": self.pages_processed,
            "average_confidence": avg_confidence,
            "errors": self.errors,
        }
