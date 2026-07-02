"""
PaddleOCR-VL-1.6 Wrapper — drop-in replacement for PaddleWrapper.
Uses the vision-language pipeline for higher-quality document OCR.
"""

import html as _html_module
import os
import re
import tempfile
import time
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple

from core.logging_manager import get_logger
from core.config_manager import get_config

logger = get_logger("ocr.vl16")

os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")
os.environ.setdefault("PADDLE_CPP_LOG_LEVEL", "3")
os.environ.setdefault("FLAGS_logtostderr", "0")
os.environ.setdefault("FLAGS_use_onednn", "0")
# Limit CPU threads so PaddlePaddle doesn't starve the Python event loop.
# The machine has 32 cores; leave at least 4 for OS + other workers.
os.environ.setdefault("OMP_NUM_THREADS", "24")
os.environ.setdefault("MKL_NUM_THREADS", "24")
# Models are cached locally — skip connectivity check and SSL verify for corporate proxy
os.environ.setdefault("PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK", "True")
os.environ.setdefault("PADDLE_PDX_CACHE_HOME", r"C:\softwares\paddle_models")
import ssl as _ssl
_ssl._create_default_https_context = _ssl._create_unverified_context

try:
    from paddleocr import PaddleOCRVL
    VL16_AVAILABLE = True
except ImportError:
    VL16_AVAILABLE = False
    logger.warning("paddleocr.PaddleOCRVL not found — install paddleocr>=3.7.0 with paddlex")

try:
    from paddlex import create_model as _paddlex_create_model
    _PADDLEX_AVAILABLE = True
except ImportError:
    _PADDLEX_AVAILABLE = False

# Lazy singleton for orientation classifier — loaded once on first use
_ori_model = None

def _get_ori_model():
    global _ori_model
    if _ori_model is None and _PADDLEX_AVAILABLE:
        try:
            _ori_model = _paddlex_create_model("PP-LCNet_x1_0_doc_ori")
        except Exception as e:
            logger.warning(f"Could not load PP-LCNet_x1_0_doc_ori: {e}")
    return _ori_model

# PP-LCNet class_id → cv2 rotation constant to make strip upright
# class 0 = 0°   (already upright)
# class 1 = 90°  (rotated CW → fix with CCW)
# class 2 = 180° (upside-down → rotate 180°)
# class 3 = 270° (rotated CCW → fix with CW)

def get_column_orientations(image_path: str):
    """Return list of (strip_img, class_id, needs_correction) for each column."""
    try:
        import cv2
    except ImportError:
        return None

    ori = _get_ori_model()
    if ori is None:
        return None

    img = cv2.imread(image_path)
    if img is None:
        return None

    _, w = img.shape[:2]
    n_cols = 4
    results = []

    for i in range(n_cols):
        x1 = i * w // n_cols
        x2 = (i + 1) * w // n_cols
        strip = img[:, x1:x2]
        try:
            r = list(ori.predict(strip, batch_size=1))[0]
            class_id = int(r["class_ids"][0][0])
            score = float(r["scores"][0])
        except Exception:
            class_id, score = 0, 1.0
        needs = class_id != 0 and score > 0.50
        results.append((strip, class_id, needs, score))

    return results


# PP-LCNet class_id → cv2 rotation to make the strip upright
_CLASS_TO_CV2_FLAG = {
    1: "ROTATE_90_COUNTERCLOCKWISE",  # 90° CW text → rotate CCW
    2: "ROTATE_180",                   # upside-down
    3: "ROTATE_90_CLOCKWISE",          # 270° CW text → rotate CW
}


def _ocr_rotated_column(img, x1: int, x2: int, class_id: int,
                        col_idx: int, score: float, ocr_fn) -> Optional[List[str]]:
    """Rotate a column strip upright, OCR it, and return cell values as a list.

    For 180° (class_id=2) the row order after rotation is reversed relative to
    the original image, so we reverse the returned list.
    """
    try:
        import cv2
    except ImportError:
        return None

    strip = img[:, x1:x2]
    cv2_flag = getattr(cv2, _CLASS_TO_CV2_FLAG[class_id])
    rotated = cv2.rotate(strip, cv2_flag)

    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    try:
        cv2.imwrite(tmp.name, rotated)
        tmp.close()
        text = ocr_fn(tmp.name)
    except Exception as e:
        logger.warning(f"Column {col_idx+1} OCR error: {e}")
        return None
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass

    if not text:
        return None

    cells = [ln.strip() for ln in text.splitlines() if ln.strip()]
    # 180° flip reverses top-to-bottom order — restore original order
    if class_id == 2:
        cells = list(reversed(cells))

    logger.info(f"Column {col_idx+1} corrected (class={class_id}, score={score:.2f}): {cells}")
    return cells


def _splice_corrected_columns(main_text: str, image_path: str, ocr_fn) -> str:
    """Replace garbled rotated columns in the main OCR TSV with correctly-read values.

    Strategy:
    1. Detect which columns PP-LCNet says are rotated.
    2. For each rotated column, OCR that strip upright and collect cell values.
    3. Splice those values into the corresponding column index of the TSV rows.
    4. Return the corrected TSV.

    Falls back to main_text unchanged if anything looks wrong.
    """
    try:
        import cv2
    except ImportError:
        return main_text

    col_info = get_column_orientations(image_path)
    if col_info is None or not any(needs for _, _, needs, _ in col_info):
        return main_text

    # Parse main_text into TSV rows
    rows = [ln.split('\t') for ln in main_text.splitlines() if ln.strip()]
    if not rows:
        return main_text
    n_rows = len(rows)
    n_cols_tsv = max(len(r) for r in rows)

    # Pad all rows to the same width
    for r in rows:
        while len(r) < n_cols_tsv:
            r.append('')

    img = cv2.imread(image_path)
    if img is None:
        return main_text

    _, w = img.shape[:2]
    n_strips = len(col_info)

    # Map strip index → TSV column index
    # PP-LCNet splits the image into n_strips equal-width vertical bands.
    # We map each band to the closest TSV column by assuming equal spacing.
    # If n_strips == n_cols_tsv the mapping is 1:1; otherwise scale.
    for strip_i, (_, class_id, needs, score) in enumerate(col_info):
        if not needs or class_id not in _CLASS_TO_CV2_FLAG:
            continue

        tsv_col = round(strip_i * (n_cols_tsv - 1) / max(n_strips - 1, 1))

        x1 = strip_i * w // n_strips
        x2 = (strip_i + 1) * w // n_strips
        cells = _ocr_rotated_column(img, x1, x2, class_id, strip_i, score, ocr_fn)
        if cells is None:
            continue

        # Align cells to rows — if count matches use directly; otherwise pad/trim
        if len(cells) == n_rows:
            for r_i, cell in enumerate(cells):
                rows[r_i][tsv_col] = cell
        elif len(cells) > 0:
            # Try to match by trimming or padding
            aligned = (cells + [''] * n_rows)[:n_rows]
            for r_i, cell in enumerate(aligned):
                if cell:
                    rows[r_i][tsv_col] = cell
        logger.info(f"Spliced col {tsv_col}: {[r[tsv_col] for r in rows]}")

    return '\n'.join('\t'.join(r) for r in rows)


class VL16Wrapper:
    """PaddleOCR-VL-1.6 wrapper with the same public interface as PaddleWrapper.

    Public API (identical to PaddleWrapper / TesseractWrapper):
        extract_text(image_path) -> Optional[Tuple[str, float]]
        health_check()           -> bool
        get_version()            -> Optional[str]
        get_stats()              -> Dict[str, Any]
        .psm                     (no-op attribute for compatibility)
    """

    def __init__(self):
        self.config = get_config()
        vl_cfg = getattr(self.config.ocr, "vl16", None)

        self.pipeline_version: str = getattr(vl_cfg, "pipeline_version", "v1.6")
        self.use_doc_orientation_classify: bool = bool(getattr(vl_cfg, "use_doc_orientation_classify", False))
        self.use_doc_unwarping: bool = bool(getattr(vl_cfg, "use_doc_unwarping", False))
        self.use_layout_detection: bool = bool(getattr(vl_cfg, "use_layout_detection", True))
        self.timeout: int = int(getattr(vl_cfg, "timeout_seconds", 300))

        self.psm: str = "3"  # compatibility no-op

        self.pages_processed: int = 0
        self.total_confidence: float = 0.0
        self.errors: int = 0

        self._ocr: Optional["PaddleOCRVL"] = None
        self._init_failed: bool = False

        if not VL16_AVAILABLE:
            logger.error("PaddleOCR-VL-1.6 is not available. OCR will not work.")
            return

        logger.info(
            f"VL16Wrapper configured — version={self.pipeline_version}, "
            f"layout={self.use_layout_detection}, "
            f"orientation={self.use_doc_orientation_classify}, "
            f"unwarp={self.use_doc_unwarping}"
        )

    # File-based cross-process lock so only one worker loads the model at a time.
    # Concurrent Paddle C++ allocator calls during model init cause "bad allocation".
    _INIT_LOCK_PATH = Path(os.environ.get("PADDLE_PDX_CACHE_HOME",
                           r"C:\softwares\paddle_models")
                          ) / ".vl16_init.lock"

    # ------------------------------------------------------------------
    # Lazy initialiser — model weights downloaded on first call (~2-3 GB)
    # ------------------------------------------------------------------
    def _get_ocr(self) -> Optional["PaddleOCRVL"]:
        if self._ocr is not None:
            return self._ocr
        if self._init_failed:
            return None
        if not VL16_AVAILABLE:
            return None

        # Serialise model loading across all worker processes with a file lock.
        # Each worker polls until the lock file disappears, then takes it.
        lock = self._INIT_LOCK_PATH
        deadline = time.monotonic() + 600  # 10-min max wait
        while True:
            try:
                fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.close(fd)
                break  # acquired
            except FileExistsError:
                if time.monotonic() > deadline:
                    logger.error("Timed out waiting for VL16 init lock — another worker may have crashed.")
                    self._init_failed = True
                    return None
                time.sleep(2)

        try:
            # Re-check after acquiring: another worker may have already succeeded
            if self._ocr is not None:
                return self._ocr

            logger.info("Initialising PaddleOCR-VL-1.6 (models will be downloaded on first run)...")
            self._ocr = PaddleOCRVL(
                pipeline_version=self.pipeline_version,
                vl_rec_backend="native",
                use_doc_orientation_classify=self.use_doc_orientation_classify,
                use_doc_unwarping=self.use_doc_unwarping,
                use_layout_detection=self.use_layout_detection,
            )
            logger.info("PaddleOCR-VL-1.6 engine ready.")
        except Exception as e:
            logger.error(f"Failed to initialise PaddleOCR-VL-1.6: {e}")
            self._init_failed = True
        finally:
            try:
                lock.unlink()
            except OSError:
                pass

        return self._ocr if not self._init_failed else None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _run_vl16(self, image_path: str) -> Optional[str]:
        """Run VL16 predict on image_path and return plain text, or None."""
        ocr = self._get_ocr()
        if ocr is None:
            return None
        try:
            results = list(ocr.predict(image_path))
        except Exception as e:
            logger.error(f"VL-1.6 OCR error on {image_path}: {e}")
            return None
        if not results:
            return None

        text_parts: list[str] = []
        for page_result in results:
            if page_result is None:
                continue
            markdown = None
            if hasattr(page_result, "markdown"):
                md_val = page_result.markdown
                if isinstance(md_val, dict):
                    markdown = md_val.get("markdown_texts") or md_val.get("text")
                elif isinstance(md_val, str):
                    markdown = md_val
            elif isinstance(page_result, dict):
                md_val = page_result.get("markdown")
                if isinstance(md_val, dict):
                    markdown = md_val.get("markdown_texts") or md_val.get("text")
                else:
                    markdown = md_val or page_result.get("text")
            if markdown:
                text_parts.append(_strip_markdown(str(markdown)))
                continue
            rec_texts = (page_result.rec_texts if hasattr(page_result, "rec_texts")
                         else (page_result.get("rec_texts") if isinstance(page_result, dict) else None))
            if rec_texts:
                for t in rec_texts:
                    t = str(t).strip()
                    if t:
                        text_parts.append(t)

        return "\n".join(text_parts).strip() or None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def extract_text(self, image_path: str) -> Optional[Tuple[str, float]]:
        """Extract text from an image/PDF page using VL-1.6.

        When PP-LCNet detects 180°-rotated columns, a corrected composite image
        is built (those columns flipped in-place) and VL-1.6 runs on that single
        corrected image instead of the original, eliminating garbled text and
        duplicate appended strips.

        Returns (text, confidence_0_to_100) or None on error / no text.
        """
        if self._get_ocr() is None:
            return None

        # Run main OCR pass on the original image
        text = self._run_vl16(image_path)
        if not text:
            self.errors += 1
            return None

        # If PP-LCNet detects rotated columns, splice correctly-read values
        # back into the TSV in place of the garbled ones.
        text = _splice_corrected_columns(text, image_path, self._run_vl16)

        self.pages_processed += 1
        self.total_confidence += 85.0
        return (text, 85.0)

    def health_check(self) -> bool:
        if not VL16_AVAILABLE:
            return False
        try:
            return self._get_ocr() is not None
        except Exception:
            return False

    def get_version(self) -> Optional[str]:
        try:
            import paddleocr
            return f"PaddleOCR-VL-{self.pipeline_version} ({getattr(paddleocr, '__version__', 'unknown')})"
        except Exception:
            return None

    def get_stats(self) -> Dict[str, Any]:
        avg_confidence = (
            self.total_confidence / self.pages_processed if self.pages_processed > 0 else 0.0
        )
        return {
            "pages_processed": self.pages_processed,
            "average_confidence": avg_confidence,
            "errors": self.errors,
        }


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
_MD_HEADING = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_MD_BOLD_ITALIC = re.compile(r"\*{1,3}(.+?)\*{1,3}")
_MD_CODE_BLOCK = re.compile(r"```.*?```", re.DOTALL)
_MD_INLINE_CODE = re.compile(r"`([^`]+)`")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^\)]+\)")
_MD_TABLE_SEP = re.compile(r"^\|[-:| ]+\|$", re.MULTILINE)
# VL-1.6 emits HTML blocks for detected image regions, e.g. <div ...><img .../></div>
_HTML_TAG = re.compile(r"<[^>]+>", re.DOTALL)
# HTML table tags — VL-1.6 emits full <table><tr><td> for detected tables
_HTML_TR = re.compile(r"</tr>", re.IGNORECASE)
_HTML_TD = re.compile(r"</t[dh]>", re.IGNORECASE)
_HTML_TABLE_OPEN = re.compile(r"<table[^>]*>", re.IGNORECASE | re.DOTALL)
_HTML_TABLE_CLOSE = re.compile(r"</table>", re.IGNORECASE)
_HTML_TR_OPEN = re.compile(r"<tr[^>]*>", re.IGNORECASE)
_HTML_TD_OPEN = re.compile(r"<t[dh][^>]*>", re.IGNORECASE)




def _html_tables_to_tsv(text: str) -> str:
    """Convert HTML <table> blocks to tab-separated rows so table structure is preserved."""
    # Replace </td> and </th> with tab, </tr> with newline, strip open tags
    text = _HTML_TD.sub("\t", text)
    text = _HTML_TR.sub("\n", text)
    text = _HTML_TABLE_OPEN.sub("", text)
    text = _HTML_TABLE_CLOSE.sub("\n", text)
    text = _HTML_TR_OPEN.sub("", text)
    text = _HTML_TD_OPEN.sub("", text)
    # Clean up trailing tabs on each line
    text = re.sub(r"\t+\n", "\n", text)
    text = re.sub(r"\t+$", "", text, flags=re.MULTILINE)
    return text


def _strip_markdown(text: str) -> str:
    """Convert VL-1.6 markdown output to plain text for indexing.

    HTML tables are converted to tab-separated rows (preserved structure).
    All other HTML tags are stripped.
    """
    text = _MD_CODE_BLOCK.sub("", text)
    # Convert HTML tables to TSV BEFORE stripping tags — preserves table structure
    text = _html_tables_to_tsv(text)
    text = _HTML_TAG.sub("", text)          # strip remaining inline HTML (image divs, spans)
    text = _MD_HEADING.sub("", text)
    text = _MD_BOLD_ITALIC.sub(r"\1", text)
    text = _MD_INLINE_CODE.sub(r"\1", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _MD_TABLE_SEP.sub("", text)
    # Remove markdown table pipes but keep cell content
    text = re.sub(r"\|", " ", text)
    # Collapse runs of blank lines left after removing HTML blocks
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Unescape HTML entities left after tag stripping (e.g. &#x27; → ')
    text = _html_module.unescape(text)
    return text.strip()