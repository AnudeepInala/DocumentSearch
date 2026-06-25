"""
Accuracy Analyzer — Measures extraction accuracy per document.

Tier 1: OpenCV + PaddleOCR (always available)
Tier 2: + YOLOv8n object detection (optional)
Tier 3: + DocTR ground-truth OCR (optional)

Outputs per-document:
  - extraction_accuracy (0-100%)
  - text_area_pct / non_text_area_pct
  - accuracy_loss_json (breakdown of WHERE accuracy was lost)
  - pipeline_type (text_extraction | ocr)
  - raw_char_count / processed_char_count
  - preprocessing_gain_pct
  - page_metrics_json (per-page breakdown)
  - accuracy_tier (tier1 | tier2 | tier3)
"""

from __future__ import annotations

import io
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np
from PIL import Image

from core.logging_manager import get_logger

logger = get_logger("extraction.accuracy")

# ---------- Lazy optional imports ----------


try:
    from ultralytics import YOLO
    _HAS_YOLO = True
except ImportError:
    _HAS_YOLO = False

try:
    from doctr.io import DocumentFile
    from doctr.models import ocr_predictor
    _HAS_DOCTR = True
except ImportError:
    _HAS_DOCTR = False

# Tesseract removed (using PaddleOCR for coverage)


def _empty_metrics(pipeline_type: str = "text_extraction", tier: str = "tier1") -> Dict[str, Any]:
    """Return a metrics dict with all fields defaulted."""
    return {
        "pipeline_type": pipeline_type,
        "extraction_accuracy": None,
        "text_area_pct": None,
        "non_text_area_pct": None,
        "raw_char_count": None,
        "processed_char_count": None,
        "preprocessing_gain_pct": None,
        "accuracy_loss_json": "{}",
        "page_metrics_json": "[]",
        "accuracy_tier": tier,
    }


def _normalize_partition_to_100(parts: Dict[str, float]) -> Dict[str, float]:
    """Ensure the partition percentages sum to exactly 100.0.
    Adjusts the whitespace/margin percentage to absorb rounding errors.
    """
    total = sum(parts.values())
    if abs(total - 100.0) > 0.001:
        diff = 100.0 - total
        ws_keys = [k for k in ["whitespace_pct", "whitespace_margins_pct"] if k in parts]
        if ws_keys:
            parts[ws_keys[0]] = max(0.0, parts[ws_keys[0]] + diff)
    
    for k in parts:
        parts[k] = round(parts[k], 2)
        
    total = sum(parts.values())
    if abs(total - 100.0) > 0.001:
        ws_keys = [k for k in ["whitespace_pct", "whitespace_margins_pct"] if k in parts]
        if ws_keys:
            parts[ws_keys[0]] = round(parts[ws_keys[0]] + (100.0 - total), 2)
            
    return parts


# =============================================================================
# AccuracyAnalyzer
# =============================================================================
class AccuracyAnalyzer:
    """Tiered document extraction accuracy analyzer."""

    def __init__(self, enable_yolo: bool = True, enable_doctr: bool = True):
        self.tier = "tier1"
        self._yolo = None
        self._yolo_net = None

        # Tier 2: YOLOv8 Layout (ONNX natively via OpenCV DNN or fallback to ultralytics)
        _repo_root = Path(__file__).resolve().parents[2]
        if enable_yolo:
            onnx_path = str(_repo_root / "models" / "yolov8_layout.onnx")
            if os.path.exists(onnx_path):
                try:
                    self._yolo_net = cv2.dnn.readNetFromONNX(onnx_path)
                    self._yolo_net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
                    self._yolo_net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
                    self.tier = "tier2"
                    logger.info("AccuracyAnalyzer: yolov8_layout.onnx loaded natively via OpenCV DNN (Tier 2)")
                except Exception as exc:
                    logger.warning("AccuracyAnalyzer: OpenCV YOLO ONNX load failed: %s", exc)

            if self._yolo_net is None and _HAS_YOLO:
                try:
                    # Use a pre-trained YOLO model for document layout
                    # We'll use yolov8n (nano) for CPU efficiency
                    self._yolo = YOLO(str(_repo_root / "yolov8n.pt"))
                    self.tier = "tier2"
                    logger.info("AccuracyAnalyzer: YOLOv8n loaded (Tier 2)")
                except Exception as exc:
                    logger.warning("AccuracyAnalyzer: YOLO load failed — staying Tier 1: %s", exc)

        # Tier 3: DocTR
        self._doctr = None
        if enable_doctr and _HAS_DOCTR:
            try:
                self._doctr = ocr_predictor(
                    det_arch="db_mobilenet_v3_large",
                    reco_arch="crnn_mobilenet_v3_small",
                    pretrained=True,
                )
                if self.tier == "tier2":
                    self.tier = "tier3"
                # else: keep current tier — DocTR alone without YOLO doesn't qualify for tier3
                logger.info("AccuracyAnalyzer: DocTR loaded (Tier 3)")
            except Exception as exc:
                logger.warning("AccuracyAnalyzer: DocTR load failed: %s", exc)

        logger.info("AccuracyAnalyzer initialized — active tier: %s", self.tier)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def analyze(
        self,
        file_path: str,
        extracted_text: str,
        tika_response: Optional[Dict] = None,
    ) -> Dict[str, Any]:
        """Analyze extraction accuracy for a non-OCR file.

        Routes to the appropriate per-format analyzer.
        """
        ext = Path(file_path).suffix.lower()
        try:
            if ext in {".txt", ".csv", ".md"}:
                return self._analyze_pure_text(file_path, extracted_text, ext)
            elif ext in {".json", ".xml"}:
                return self._analyze_structured_text(file_path, extracted_text, ext)
            elif ext == ".html":
                return self._analyze_html(file_path, extracted_text)
            elif ext == ".docx":
                return self._analyze_docx(file_path, extracted_text)
            elif ext == ".xlsx":
                return self._analyze_xlsx(file_path, extracted_text)
            elif ext == ".pdf":
                return self._analyze_text_pdf(file_path, extracted_text, tika_response)
            else:
                return self._analyze_generic(file_path, extracted_text, ext)
        except Exception as exc:
            logger.warning("Accuracy analysis failed for %s: %s", file_path, exc)
            m = _empty_metrics("text_extraction", self.tier)
            m["accuracy_loss_json"] = json.dumps({"error": str(exc)})
            return m

    def analyze_ocr_page(
        self,
        raw_image_bytes: bytes,
        preprocessed_image_bytes: bytes,
        smart_id: Optional[str] = None,
        page_num: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Analyze OCR accuracy for a single page image.

        Args:
            raw_image_bytes: Original image before any preprocessing.
            preprocessed_image_bytes: Image after best preprocessing strategy.
            smart_id: Optional document ID.
            page_num: Optional page number.

        Returns:
            Per-page accuracy metrics dict.
        """
        try:
            # Step 1: Zone segmentation
            zone_metrics = self._segment_page_opencv(raw_image_bytes)
            zone_metrics = self._refine_zones_yolo(raw_image_bytes, zone_metrics)

            # Step 1b: Detect faded text bboxes and merge them
            faded_regions = self._detect_faded_text_regions(raw_image_bytes)
            if "bboxes" not in zone_metrics:
                zone_metrics["bboxes"] = []
            zone_metrics["bboxes"].extend(faded_regions)

            # Step 2: PaddleOCR char-level comparison
            paddle_metrics = self._measure_paddle_coverage(
                raw_image_bytes, preprocessed_image_bytes
            )

            # Step 3: DocTR ground truth (Tier 3 only)
            doctr_metrics = None
            if self._doctr:
                doctr_metrics = self._measure_doctr_ground_truth(
                    raw_image_bytes, preprocessed_image_bytes
                )

            # Step 4: Estimate total chars in text zone
            estimated_total = self._estimate_total_chars(
                raw_image_bytes, zone_metrics["text_area_pct"]
            )

            # Step 5: Build loss breakdown
            loss = self._build_loss_breakdown(
                zone_metrics, paddle_metrics, doctr_metrics, estimated_total
            )

            # Step 6: Write page segmentation breakdown if smart_id/page_num provided
            if smart_id is not None and page_num is not None:
                from core.reporting_manager import write_page_segmentation_breakdown
                from PIL import Image
                img = Image.open(io.BytesIO(raw_image_bytes))
                pw, ph = img.width, img.height
                
                clean_text_pct = loss["accuracy_loss_breakdown"].get("text_read_pct", 0.0)
                faded_text_pct = loss["accuracy_loss_breakdown"].get("unreadable_text_pct", 0.0)
                logo_pct = loss["accuracy_loss_breakdown"].get("logos_images_pct", 0.0)
                stamp_pct = loss["accuracy_loss_breakdown"].get("stamps_seals_pct", 0.0)
                handwritten_pct = loss["accuracy_loss_breakdown"].get("handwritten_pct", 0.0)
                whitespace_pct = loss["accuracy_loss_breakdown"].get("whitespace_margins_pct", 0.0)
                noise_pct = loss["accuracy_loss_breakdown"].get("noise_artifacts_pct", 0.0)
                
                content_area = max(0.1, 100.0 - whitespace_pct)
                baseline = round((clean_text_pct / content_area) * 100, 2)
                
                write_page_segmentation_breakdown(
                    smart_id=smart_id,
                    page_num=page_num,
                    clean_text_pct=clean_text_pct,
                    faded_text_pct=faded_text_pct,
                    logo_pct=logo_pct,
                    stamp_pct=stamp_pct,
                    handwritten_pct=handwritten_pct,
                    whitespace_pct=whitespace_pct,
                    noise_pct=noise_pct,
                    content_area_pct=content_area,
                    baseline_accuracy=baseline,
                    page_width_px=pw,
                    page_height_px=ph,
                    analyzer_tier=self.tier,
                )

            return {
                "pipeline_type": "ocr",
                "extraction_accuracy": loss["extraction_accuracy"],
                "text_area_pct": zone_metrics["text_area_pct"],
                "non_text_area_pct": round(
                    100 - zone_metrics["text_area_pct"] - zone_metrics["whitespace_pct"], 2
                ),
                "raw_char_count": paddle_metrics["raw_char_count"],
                "processed_char_count": paddle_metrics["processed_char_count"],
                "preprocessing_gain_pct": paddle_metrics["preprocessing_gain_pct"],
                "accuracy_loss_json": json.dumps(loss["accuracy_loss_breakdown"]),
                "page_metrics_json": "",  # Caller aggregates pages
                "accuracy_tier": self.tier,
            }
        except Exception as exc:
            logger.warning("OCR accuracy analysis failed: %s", exc)
            return _empty_metrics("ocr", self.tier)

    # ------------------------------------------------------------------
    # Layer 1: Pure text (TXT, CSV, MD)
    # ------------------------------------------------------------------
    def _analyze_pure_text(
        self, file_path: str, extracted_text: str, ext: str
    ) -> Dict[str, Any]:
        file_size = os.path.getsize(file_path)
        if file_size == 0:
            m = _empty_metrics("text_extraction", self.tier)
            m["extraction_accuracy"] = 0.0
            m["text_area_pct"] = 0.0
            m["non_text_area_pct"] = 0.0
            return m

        extracted_bytes = len(extracted_text.encode("utf-8"))
        # Text formats: Tika may add/remove whitespace but content is fully extractable
        accuracy = min(extracted_bytes / max(file_size, 1) * 100, 100.0)
        # For CSV, small overhead from delimiters; for TXT/MD essentially 0
        overhead = {".csv": 5.0, ".md": 3.0, ".txt": 0.0}.get(ext, 0.0)

        return {
            "pipeline_type": "text_extraction",
            "extraction_accuracy": round(accuracy, 2),
            "text_area_pct": round(100.0 - overhead, 2),
            "non_text_area_pct": round(overhead, 2),
            "raw_char_count": len(extracted_text),
            "processed_char_count": len(extracted_text),
            "preprocessing_gain_pct": 0.0,
            "accuracy_loss_json": json.dumps(
                {"syntax_overhead_pct": overhead, "format": ext.lstrip(".")}
            ),
            "page_metrics_json": json.dumps(
                [{"page": 1, "text_area_pct": round(100 - overhead, 2), "extraction_accuracy": round(accuracy, 2)}]
            ),
            "accuracy_tier": self.tier,
        }

    # ------------------------------------------------------------------
    # Layer 1b: Structured text (JSON, XML)
    # ------------------------------------------------------------------
    def _analyze_structured_text(
        self, file_path: str, extracted_text: str, ext: str
    ) -> Dict[str, Any]:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            raw = f.read()

        if not raw:
            m = _empty_metrics("text_extraction", self.tier)
            m["extraction_accuracy"] = 0.0
            return m

        # Discount structural syntax
        if ext == ".json":
            # Count structural chars: {}[]",:
            struct_chars = sum(1 for c in raw if c in '{}[]",:')
            overhead_pct = struct_chars / max(len(raw), 1) * 100
        else:  # XML
            tags = re.findall(r"<[^>]+>", raw)
            tag_chars = sum(len(t) for t in tags)
            overhead_pct = tag_chars / max(len(raw), 1) * 100

        # Expected text = raw minus structure
        expected_text_chars = len(raw) - int(len(raw) * overhead_pct / 100)
        accuracy = len(extracted_text) / max(expected_text_chars, 1) * 100

        return {
            "pipeline_type": "text_extraction",
            "extraction_accuracy": round(min(accuracy, 100.0), 2),
            "text_area_pct": round(100 - overhead_pct, 2),
            "non_text_area_pct": round(overhead_pct, 2),
            "raw_char_count": expected_text_chars,
            "processed_char_count": len(extracted_text),
            "preprocessing_gain_pct": 0.0,
            "accuracy_loss_json": json.dumps(
                {"syntax_overhead_pct": round(overhead_pct, 2), "format": ext.lstrip(".")}
            ),
            "page_metrics_json": json.dumps(
                [{"page": 1, "text_area_pct": round(100 - overhead_pct, 2), "extraction_accuracy": round(min(accuracy, 100.0), 2)}]
            ),
            "accuracy_tier": self.tier,
        }

    # ------------------------------------------------------------------
    # Layer 2: HTML
    # ------------------------------------------------------------------
    def _analyze_html(self, file_path: str, extracted_text: str) -> Dict[str, Any]:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            raw_html = f.read()

        if not raw_html:
            m = _empty_metrics("text_extraction", self.tier)
            m["extraction_accuracy"] = 0.0
            return m

        # Strip all tags to get expected text
        tags = re.findall(r"<[^>]+>", raw_html)
        tag_chars = sum(len(t) for t in tags)
        total_chars = len(raw_html)
        text_area_pct = (total_chars - tag_chars) / max(total_chars, 1) * 100

        # Count embedded images
        img_count = len(re.findall(r"<img[^>]*>", raw_html, re.IGNORECASE))
        image_area_estimate = min(img_count * 5.0, 25.0)

        # Expected text after stripping tags
        expected_text = re.sub(r"<[^>]+>", " ", raw_html)
        expected_text = re.sub(r"\s+", " ", expected_text).strip()
        accuracy = len(extracted_text) / max(len(expected_text), 1) * 100

        return {
            "pipeline_type": "text_extraction",
            "extraction_accuracy": round(min(accuracy, 100.0), 2),
            "text_area_pct": round(text_area_pct, 2),
            "non_text_area_pct": round(100 - text_area_pct, 2),
            "raw_char_count": len(expected_text),
            "processed_char_count": len(extracted_text),
            "preprocessing_gain_pct": 0.0,
            "accuracy_loss_json": json.dumps({
                "html_tags_pct": round(100 - text_area_pct, 2),
                "embedded_images": img_count,
                "image_area_estimate_pct": round(image_area_estimate, 2),
            }),
            "page_metrics_json": json.dumps(
                [{"page": 1, "text_area_pct": round(text_area_pct, 2), "extraction_accuracy": round(min(accuracy, 100.0), 2)}]
            ),
            "accuracy_tier": self.tier,
        }

    # ------------------------------------------------------------------
    # Layer 3a: DOCX
    # ------------------------------------------------------------------
    def _analyze_docx(self, file_path: str, extracted_text: str) -> Dict[str, Any]:
        try:
            from docx import Document as DocxDocument
        except ImportError:
            logger.warning("python-docx not installed — falling back to generic analysis")
            return self._analyze_generic(file_path, extracted_text, ".docx")

        try:
            doc = DocxDocument(file_path)
        except Exception as exc:
            logger.warning("Cannot open DOCX %s: %s", file_path, exc)
            return self._analyze_generic(file_path, extracted_text, ".docx")

        # Count text from paragraphs
        para_chars = sum(len(p.text) for p in doc.paragraphs)

        # Count text from tables
        table_chars = 0
        for table in doc.tables:
            for row in table.rows:
                for cell in row.cells:
                    table_chars += len(cell.text)

        total_expected = para_chars + table_chars

        # Count images
        image_count = 0
        try:
            image_count = len(doc.inline_shapes)
        except Exception:
            pass
        # Also count from relationships
        try:
            image_rels = [r for r in doc.part.rels.values()
                          if "image" in str(getattr(r, 'reltype', '')).lower()]
            image_count = max(image_count, len(image_rels))
        except Exception:
            pass

        # Count headers/footers
        hf_chars = 0
        try:
            for section in doc.sections:
                if section.header and section.header.paragraphs:
                    hf_chars += sum(len(p.text) for p in section.header.paragraphs)
                if section.footer and section.footer.paragraphs:
                    hf_chars += sum(len(p.text) for p in section.footer.paragraphs)
        except Exception:
            pass

        total_expected += hf_chars

        # Estimate composition
        if total_expected + image_count > 0:
            text_area_pct = total_expected / (total_expected + image_count * 60) * 100
        else:
            text_area_pct = 100.0

        if total_expected > 0:
            accuracy = min(len(extracted_text) / total_expected * 100, 100.0)
        else:
            accuracy = 100.0 if not extracted_text else 0.0

        return {
            "pipeline_type": "text_extraction",
            "extraction_accuracy": round(accuracy, 2),
            "text_area_pct": round(text_area_pct, 2),
            "non_text_area_pct": round(100 - text_area_pct, 2),
            "raw_char_count": total_expected,
            "processed_char_count": len(extracted_text),
            "preprocessing_gain_pct": 0.0,
            "accuracy_loss_json": json.dumps({
                "paragraphs": len(doc.paragraphs),
                "tables": len(doc.tables),
                "table_chars": table_chars,
                "images": image_count,
                "header_footer_chars": hf_chars,
                "image_area_pct": round(100 - text_area_pct, 2),
            }),
            "page_metrics_json": json.dumps(
                [{"page": 1, "text_area_pct": round(text_area_pct, 2),
                  "image_area_pct": round(100 - text_area_pct, 2),
                  "extraction_accuracy": round(accuracy, 2)}]
            ),
            "accuracy_tier": self.tier,
        }

    # ------------------------------------------------------------------
    # Layer 3b: XLSX
    # ------------------------------------------------------------------
    def _analyze_xlsx(self, file_path: str, extracted_text: str) -> Dict[str, Any]:
        try:
            from openpyxl import load_workbook
        except ImportError:
            logger.warning("openpyxl not installed — falling back to generic analysis")
            return self._analyze_generic(file_path, extracted_text, ".xlsx")

        try:
            wb = load_workbook(file_path, data_only=True, read_only=True)
        except Exception as exc:
            logger.warning("Cannot open XLSX %s: %s", file_path, exc)
            return self._analyze_generic(file_path, extracted_text, ".xlsx")

        total_cells = 0
        text_cells = 0
        number_cells = 0
        empty_cells = 0
        text_char_count = 0

        try:
            for ws in wb.worksheets:
                for row in ws.iter_rows():
                    for cell in row:
                        total_cells += 1
                        val = cell.value
                        if val is None or str(val).strip() == "":
                            empty_cells += 1
                        elif isinstance(val, str):
                            text_cells += 1
                            text_char_count += len(val)
                        elif isinstance(val, (int, float)):
                            number_cells += 1
                            text_char_count += len(str(val))
        except Exception as exc:
            logger.warning("Error reading XLSX cells: %s", exc)
        finally:
            try:
                wb.close()
            except Exception:
                pass

        extractable_cells = text_cells + number_cells
        text_area_pct = extractable_cells / max(total_cells, 1) * 100
        accuracy = len(extracted_text) / max(text_char_count, 1) * 100

        return {
            "pipeline_type": "text_extraction",
            "extraction_accuracy": round(min(accuracy, 100.0), 2),
            "text_area_pct": round(text_area_pct, 2),
            "non_text_area_pct": round(100 - text_area_pct, 2),
            "raw_char_count": text_char_count,
            "processed_char_count": len(extracted_text),
            "preprocessing_gain_pct": 0.0,
            "accuracy_loss_json": json.dumps({
                "total_cells": total_cells,
                "text_cells": text_cells,
                "number_cells": number_cells,
                "empty_cells": empty_cells,
                "empty_cell_pct": round(empty_cells / max(total_cells, 1) * 100, 2),
            }),
            "page_metrics_json": json.dumps(
                [{"page": 1, "text_area_pct": round(text_area_pct, 2),
                  "extraction_accuracy": round(min(accuracy, 100.0), 2)}]
            ),
            "accuracy_tier": self.tier,
        }

    # ------------------------------------------------------------------
    # Layer 4a: Text-based PDF
    # ------------------------------------------------------------------
    def _analyze_text_pdf(
        self, file_path: str, extracted_text: str, tika_response: Optional[Dict] = None
    ) -> Dict[str, Any]:
        # Try to get page count from Tika metadata
        page_count = 1
        if tika_response:
            docs = tika_response.get("documents", [])
            if docs:
                meta = docs[0] if isinstance(docs, list) else docs
                page_count = int(
                    meta.get("xmpTPg:NPages", 0) or meta.get("meta:page-count", 0) or 1
                )
        page_count = max(page_count, 1)

        chars_per_page = len(extracted_text) / page_count
        # A typical A4 page has ~2000-3000 chars of text
        EXPECTED_CHARS = 2500
        text_density = min(chars_per_page / EXPECTED_CHARS, 1.0)
        text_area_pct = text_density * 85  # Text max ~85% of page (margins etc)
        non_text_pct = 100 - text_area_pct

        # If very low text density, the PDF might be scanned
        accuracy = min(text_density * 100, 100.0)

        page_metrics = []
        for p in range(1, page_count + 1):
            page_metrics.append({
                "page": p,
                "text_area_pct": round(text_area_pct, 2),
                "non_text_area_pct": round(non_text_pct, 2),
                "extraction_accuracy": round(accuracy, 2),
            })

        return {
            "pipeline_type": "text_extraction",
            "extraction_accuracy": round(accuracy, 2),
            "text_area_pct": round(text_area_pct, 2),
            "non_text_area_pct": round(non_text_pct, 2),
            "raw_char_count": len(extracted_text),
            "processed_char_count": len(extracted_text),
            "preprocessing_gain_pct": 0.0,
            "accuracy_loss_json": json.dumps({
                "page_count": page_count,
                "chars_per_page": round(chars_per_page, 0),
                "text_density": round(text_density, 4),
                "low_text_warning": chars_per_page < 100,
            }),
            "page_metrics_json": json.dumps(page_metrics),
            "accuracy_tier": self.tier,
        }

    # ------------------------------------------------------------------
    # Generic fallback
    # ------------------------------------------------------------------
    def _analyze_generic(
        self, file_path: str, extracted_text: str, ext: str
    ) -> Dict[str, Any]:
        file_size = os.path.getsize(file_path)
        extracted_bytes = len(extracted_text.encode("utf-8"))
        accuracy = min(extracted_bytes / max(file_size, 1) * 100, 100.0) if file_size > 0 else 0.0
        return {
            "pipeline_type": "text_extraction",
            "extraction_accuracy": round(accuracy, 2),
            "text_area_pct": 95.0,
            "non_text_area_pct": 5.0,
            "raw_char_count": len(extracted_text),
            "processed_char_count": len(extracted_text),
            "preprocessing_gain_pct": 0.0,
            "accuracy_loss_json": json.dumps({"format": ext.lstrip("."), "method": "generic_byte_ratio"}),
            "page_metrics_json": "[]",
            "accuracy_tier": self.tier,
        }

    # ==================================================================
    # OCR Internal Methods
    # ==================================================================
    def _segment_page_opencv(self, image_bytes: bytes) -> Dict[str, float]:
        """Segment a page image into text/image/signature/stamp/noise/whitespace zones."""
        nparr = np.frombuffer(image_bytes, np.uint8)
        img = cv2.imdecode(nparr, cv2.IMREAD_GRAYSCALE)
        if img is None:
            return {
                "text_area_pct": 50.0, "image_area_pct": 0.0,
                "signature_area_pct": 0.0, "stamp_area_pct": 0.0,
                "noise_area_pct": 0.0, "whitespace_pct": 50.0,
            }

        h, w = img.shape
        total_pixels = h * w

        # Binarize
        _, binary = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

        # ── Pass 1: coarse kernels for text / stamp / logo blocks ─────────────
        # (25,1)+(1,15) merges entire lines together — good for text layout but
        # too aggressive for signatures: the "By:" label + cursive strokes +
        # printed name below all merge into one tall block that fails the
        # height_ratio gate.  Use this pass ONLY for non-signature categories.
        h_kernel_coarse = cv2.getStructuringElement(cv2.MORPH_RECT, (25, 1))
        h_dilated_coarse = cv2.dilate(binary, h_kernel_coarse, iterations=1)
        v_kernel_coarse = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 15))
        text_blocks = cv2.dilate(h_dilated_coarse, v_kernel_coarse, iterations=1)

        coarse_contours, _ = cv2.findContours(
            text_blocks, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )

        # ── Pass 2: tight kernels to isolate signature rows ───────────────────
        # (10,1) bridges the short gaps within a single cursive stroke cluster
        # without pulling in text rows above/below.  (1,4) allows slight vertical
        # variance in ascending/descending loops without merging adjacent lines.
        h_kernel_tight = cv2.getStructuringElement(cv2.MORPH_RECT, (10, 1))
        h_dilated_tight = cv2.dilate(binary, h_kernel_tight, iterations=1)
        v_kernel_tight = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 4))
        sig_blocks = cv2.dilate(h_dilated_tight, v_kernel_tight, iterations=1)

        tight_contours, _ = cv2.findContours(
            sig_blocks, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )

        text_px = 0
        image_px = 0
        sig_px = 0
        stamp_px = 0
        noise_px = 0
        bboxes = []

        # Helper: IoU overlap between two bboxes [x1,y1,x2,y2]
        def _iou(a, b):
            ix1 = max(a[0], b[0]); iy1 = max(a[1], b[1])
            ix2 = min(a[2], b[2]); iy2 = min(a[3], b[3])
            iw = max(0, ix2 - ix1); ih = max(0, iy2 - iy1)
            inter = iw * ih
            if inter == 0:
                return 0.0
            ua = (a[2]-a[0])*(a[3]-a[1]) + (b[2]-b[0])*(b[3]-b[1]) - inter
            return inter / max(ua, 1)

        # ── Process coarse pass — stamp / logo / text (skip signature attempt) ─
        for cnt in coarse_contours:
            x, y, cw, ch = cv2.boundingRect(cnt)
            area = cw * ch
            if area == 0:
                continue
                
            # Ignore full-page border artifacts
            if cw > w * 0.95 and ch > h * 0.95:
                continue
                
            aspect = cw / max(ch, 1)
            roi = binary[y : y + ch, x : x + cw]
            ink_density = np.count_nonzero(roi) / max(area, 1)

            if area < total_pixels * 0.0005:
                noise_px += area
                continue

            # Stamp/Seal
            if 0.6 < aspect < 1.6 and area > total_pixels * 0.005:
                perimeter = cv2.arcLength(cnt, True)
                if perimeter > 0:
                    circularity = 4 * np.pi * cv2.contourArea(cnt) / (perimeter ** 2)
                    if circularity > 0.4:
                        stamp_px += area
                        bboxes.append({
                            "type": "stamp",
                            "bbox": [int(x), int(y), int(x + cw), int(y + ch)],
                            "impact": round(area / total_pixels * 100, 2)
                        })
                        continue

            # Image/Logo
            # Prevent giant page-sized artifacts from being classified as logos
            if total_pixels * 0.02 < area < total_pixels * 0.40 and ink_density > 0.5:
                image_px += area
                bboxes.append({
                    "type": "logo",
                    "bbox": [int(x), int(y), int(x + cw), int(y + ch)],
                    "impact": round(area / total_pixels * 100, 2)
                })
                continue

            text_px += area

        # ── Process tight pass — signature candidates only ────────────────────
        # Rules are intentionally relaxed to capture diverse signature styles:
        #   - position: anywhere in lower 70% (catches bottom-right corners)
        #   - aspect: 1.0–30  (loopy signatures can be nearly square, wide initials)
        #   - height: 0.001–0.25  (thin line signatures to multi-line blocks)
        #   - width: 0.02–0.95  (full-width to single-column signatures)
        #   - ink_density: 0.002–0.50  (faint to dense cursive strokes)
        for cnt in tight_contours:
            x, y, cw, ch = cv2.boundingRect(cnt)
            area = cw * ch
            if area == 0:
                continue
            if area < total_pixels * 0.0005:
                continue

            aspect = cw / max(ch, 1)
            width_ratio = cw / max(w, 1)
            height_ratio = ch / max(h, 1)
            roi = binary[y : y + ch, x : x + cw]
            ink_density = np.count_nonzero(roi) / area

            if not (
                (y + ch) > h * 0.3  # Relaxed: catches signatures in lower 70% including bottom edge
                and 1.0 < aspect < 30.0  # Relaxed: wider range for diverse signature morphology
                and 0.001 < height_ratio < 0.25  # Relaxed: catches thin line signatures and multi-line blocks
                and 0.02 < width_ratio < 0.95  # Relaxed: catches full-width or narrow column signatures
                and 0.002 < ink_density < 0.50  # Relaxed: captures faint signatures and dense cursive
            ):
                continue

            candidate_bbox = [int(x), int(y), int(x + cw), int(y + ch)]

            # Skip if this box overlaps heavily with an already-found stamp/logo
            overlap = any(
                _iou(candidate_bbox, b["bbox"]) > 0.4
                for b in bboxes
                if b["type"] in ("stamp", "logo")
            )
            if overlap:
                continue

            # Deduplicate against already-accepted signature bboxes
            dup = any(
                _iou(candidate_bbox, b["bbox"]) > 0.5
                for b in bboxes
                if b["type"] == "signature"
            )
            if dup:
                continue

            sig_px += area
            bboxes.append({
                "type": "signature",
                "bbox": candidate_bbox,
                "impact": round(area / total_pixels * 100, 2)
            })

        covered = text_px + image_px + sig_px + stamp_px + noise_px
        ws_px = max(0, total_pixels - covered)

        # Validate whitespace in grid blocks
        suspect_ws_px = 0
        grid_w, grid_h = 100, 100
        covered_mask = np.zeros((h, w), dtype=np.uint8)
        for b in bboxes:
            bx1, by1, bx2, by2 = b["bbox"]
            bx1, bx2 = max(0, min(w, bx1)), max(0, min(w, bx2))
            by1, by2 = max(0, min(h, by1)), max(0, min(h, by2))
            covered_mask[by1:by2, bx1:bx2] = 255
        
        # Merge text blocks
        covered_mask = cv2.bitwise_or(covered_mask, text_blocks)
        
        for gy in range(0, h, grid_h):
            for gx in range(0, w, grid_w):
                gx2 = min(w, gx + grid_w)
                gy2 = min(h, gy + grid_h)
                block_area = (gx2 - gx) * (gy2 - gy)
                if block_area == 0:
                    continue
                if np.count_nonzero(covered_mask[gy:gy2, gx:gx2]) == 0:
                    if not self._validate_whitespace_region(img, (gx, gy, gx2, gy2)):
                        suspect_ws_px += block_area

        suspect_ws_px = min(suspect_ws_px, ws_px)
        validated_ws_px = ws_px - suspect_ws_px
        
        # Add suspect whitespace to noise
        noise_px += suspect_ws_px

        validated_ws_pct = round(validated_ws_px / total_pixels * 100, 2)
        suspect_ws_pct = round(suspect_ws_px / total_pixels * 100, 2)

        return {
            "text_area_pct": round(text_px / total_pixels * 100, 2),
            "image_area_pct": round(image_px / total_pixels * 100, 2),
            "signature_area_pct": round(sig_px / total_pixels * 100, 2),
            "stamp_area_pct": round(stamp_px / total_pixels * 100, 2),
            "noise_area_pct": round(noise_px / total_pixels * 100, 2),
            "whitespace_pct": validated_ws_pct,
            "validated_whitespace_pct": validated_ws_pct,
            "suspect_whitespace_pct": suspect_ws_pct,
            "bboxes": bboxes,
        }

    def _refine_zones_yolo(self, image_bytes: bytes, opencv_zones: dict) -> dict:
        """Refine zone detection using YOLO object detection.
        Falls back to opencv_zones if YOLO is not available.
        """
        if getattr(self, "_yolo_net", None) is None and getattr(self, "_yolo", None) is None:
            return opencv_zones

        try:
            # 1. Use OpenCV DNN ONNX if loaded
            if getattr(self, "_yolo_net", None) is not None:
                nparr = np.frombuffer(image_bytes, np.uint8)
                img_cv = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                h, w, _ = img_cv.shape
                total_pixels = w * h

                # Prepare input blob (640x640)
                blob = cv2.dnn.blobFromImage(
                    img_cv, 
                    scalefactor=1.0/255.0, 
                    size=(640, 640), 
                    mean=(0, 0, 0), 
                    swapRB=True, 
                    crop=False
                )
                self._yolo_net.setInput(blob)
                outputs = self._yolo_net.forward()  # Shape: (1, 84, 8400) or similar
                
                output = outputs[0]
                num_classes = output.shape[0] - 4
                num_candidates = output.shape[1]
                
                boxes = []
                confidences = []
                class_ids = []
                
                for col in range(num_candidates):
                    cx, cy, wb_box, hb_box = output[0:4, col]
                    classes_scores = output[4:, col]
                    class_id = np.argmax(classes_scores)
                    confidence = float(classes_scores[class_id])
                    
                    if confidence >= 0.25:
                        x = int((cx - wb_box / 2.0) * w / 640.0)
                        y = int((cy - hb_box / 2.0) * h / 640.0)
                        width = int(wb_box * w / 640.0)
                        height = int(hb_box * h / 640.0)
                        
                        boxes.append([x, y, width, height])
                        confidences.append(confidence)
                        class_ids.append(int(class_id))
                
                indices = cv2.dnn.NMSBoxes(boxes, confidences, score_threshold=0.25, nms_threshold=0.45)
                
                zones = {
                    'text_area_pct': 0, 'image_area_pct': 0, 'signature_area_pct': 0,
                    'stamp_area_pct': 0, 'table_area_pct': 0, 'noise_area_pct': 0,
                }
                yolo_bboxes = []
                
                if len(indices) > 0:
                    flat_indices = np.array(indices).flatten()
                    for idx in flat_indices:
                        x, y, wb, hb = boxes[idx]
                        cls_id = class_ids[idx]
                        area = wb * hb
                        
                        x1 = max(0, min(w, x))
                        y1 = max(0, min(h, y))
                        x2 = max(0, min(w, x + wb))
                        y2 = max(0, min(h, y + hb))
                        
                        # Ignore full-page border artifacts
                        if wb > w * 0.95 and hb > h * 0.95:
                            continue
                            
                        # Custom layout vs standard COCO classes
                        if num_classes < 10:
                            s_type = "logo"
                            if cls_id == 0:
                                s_type = "signature"
                            elif cls_id == 1:
                                s_type = "stamp"
                            elif cls_id == 2:
                                s_type = "logo"
                            elif cls_id == 3:
                                s_type = "handwritten"
                            elif cls_id == 4:
                                s_type = "table"
                        else:
                            s_type = "logo"  # Map everything to logo/image for COCO
                            
                        if s_type == "signature":
                            zones['signature_area_pct'] += area
                        elif s_type == "stamp":
                            zones['stamp_area_pct'] += area
                        elif s_type == "logo":
                            if area > total_pixels * 0.40:
                                continue
                            zones['image_area_pct'] += area
                        elif s_type == "handwritten":
                            zones['noise_area_pct'] += area
                        elif s_type == "table":
                            zones['table_area_pct'] += area
                            
                        yolo_bboxes.append({
                            "type": s_type,
                            "bbox": [int(x1), int(y1), int(x2), int(y2)],
                            "impact": round(area / total_pixels * 100, 2)
                        })

                for key in zones:
                    zones[key] = round(zones[key] / total_pixels * 100, 2)

                refined = dict(opencv_zones)
                if "bboxes" not in refined:
                    refined["bboxes"] = []

                if zones['image_area_pct'] > 0 or zones['signature_area_pct'] > 0 or zones['stamp_area_pct'] > 0:
                    refined['image_area_pct'] = max(opencv_zones.get('image_area_pct', 0), zones['image_area_pct'])
                    refined['signature_area_pct'] = max(opencv_zones.get('signature_area_pct', 0), zones['signature_area_pct'])
                    refined['stamp_area_pct'] = max(opencv_zones.get('stamp_area_pct', 0), zones['stamp_area_pct'])
                    
                    for yb in yolo_bboxes:
                        refined["bboxes"].append(yb)
                        
                    total_non_text = refined['image_area_pct'] + refined.get('signature_area_pct', 0) + refined.get('stamp_area_pct', 0) + refined.get('noise_area_pct', 0)
                    refined['text_area_pct'] = max(0.0, 100.0 - total_non_text - refined.get('whitespace_pct', 0.0))

                refined['detection_method'] = 'opencv+yolo'
                return refined

            # 2. Fallback to Ultralytics YOLO
            img = Image.open(io.BytesIO(image_bytes))
            w, h = img.size
            total_pixels = w * h

            results = self._yolo(img, conf=0.25, iou=0.45, verbose=False)

            zones = {
                'text_area_pct': 0, 'image_area_pct': 0, 'signature_area_pct': 0,
                'stamp_area_pct': 0, 'table_area_pct': 0, 'noise_area_pct': 0,
            }

            yolo_bboxes = []
            for box in results[0].boxes:
                cls_id = int(box.cls)
                x1, y1, x2, y2 = box.xyxy[0].tolist()
                
                wb = x2 - x1
                hb = y2 - y1
                area = wb * hb
                
                # Ignore full-page border artifacts
                if wb > w * 0.95 and hb > h * 0.95:
                    continue
                    
                # Prevent giant artifacts
                if area > total_pixels * 0.40:
                    continue
                
                zones['image_area_pct'] += area
                yolo_bboxes.append({
                    "type": "logo",
                    "bbox": [int(x1), int(y1), int(x2), int(y2)],
                    "impact": round(area / total_pixels * 100, 2)
                })

            for key in zones:
                zones[key] = round(zones[key] / total_pixels * 100, 2)

            refined = dict(opencv_zones)
            if "bboxes" not in refined:
                refined["bboxes"] = []

            if zones['image_area_pct'] > 0:
                refined['image_area_pct'] = max(opencv_zones.get('image_area_pct', 0), zones['image_area_pct'])
                for yb in yolo_bboxes:
                    refined["bboxes"].append(yb)
                total_non_text = refined['image_area_pct'] + refined.get('signature_area_pct', 0) + refined.get('stamp_area_pct', 0) + refined.get('noise_area_pct', 0)
                refined['text_area_pct'] = max(0, 100 - total_non_text - refined.get('whitespace_pct', 0))

            refined['detection_method'] = 'opencv+yolo'
            return refined

        except Exception as exc:
            logger.debug("YOLO refinement failed, using OpenCV zones: %s", exc)
            return opencv_zones

    def _measure_paddle_coverage(
        self, raw_bytes: bytes, processed_bytes: bytes
    ) -> Dict[str, Any]:
        """Run PaddleOCR on raw vs preprocessed image."""
        try:
            if not hasattr(self, "_paddle_wrapper") or self._paddle_wrapper is None:
                from ocr.paddle_wrapper import PaddleWrapper
                self._paddle_wrapper = PaddleWrapper()
            paddle = self._paddle_wrapper
        except ImportError:
            return {
                "raw_char_count": 0, "processed_char_count": 0,
                "preprocessing_gain_pct": 0.0,
            }

        def _count_chars(img_bytes: bytes) -> Tuple[int, int]:
            import tempfile
            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                    tmp.write(img_bytes)
                    tmp_path = tmp.name
                
                res = paddle.extract_text(tmp_path)
                if not res:
                    return 0, 0
                text, conf = res
                total = len(text)
                high = total if conf > 30.0 else 0
                return high, total
            except Exception:
                return 0, 0
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass

        raw_high, raw_total = _count_chars(raw_bytes)
        proc_high, proc_total = _count_chars(processed_bytes)

        gain = (proc_high - raw_high) / max(raw_high, 1) * 100

        return {
            "raw_char_count": raw_high,
            "raw_total_chars": raw_total,
            "processed_char_count": proc_high,
            "processed_total_chars": proc_total,
            "preprocessing_gain_pct": round(gain, 2),
            "chars_recovered": proc_high - raw_high,
        }

    def _measure_doctr_ground_truth(
        self, raw_bytes: bytes, processed_bytes: bytes
    ) -> Optional[Dict[str, int]]:
        """Use DocTR as ground-truth OCR engine."""
        if not self._doctr:
            return None

        def _count_doctr(img_bytes: bytes) -> int:
            try:
                doc = DocumentFile.from_images([img_bytes])
                result = self._doctr(doc)
                chars = 0
                for page in result.pages:
                    for block in page.blocks:
                        for line in block.lines:
                            for word in line.words:
                                if word.confidence > 0.3:
                                    chars += len(word.value)
                return chars
            except Exception as exc:
                logger.debug("DocTR analysis failed: %s", exc)
                return 0

        raw_chars = _count_doctr(raw_bytes)
        proc_chars = _count_doctr(processed_bytes)

        return {
            "doctr_raw_chars": raw_chars,
            "doctr_processed_chars": proc_chars,
            "doctr_ground_truth": max(raw_chars, proc_chars),
        }

    def _estimate_total_chars(self, image_bytes: bytes, text_area_pct: float) -> int:
        """Estimate total character capacity in the text zone."""
        img = Image.open(io.BytesIO(image_bytes))
        total_px = img.width * img.height
        text_zone_px = total_px * (text_area_pct / 100)
        # At 300 DPI: avg char ~12x18=216px², with spacing ~420px²
        EFFECTIVE_CHAR_AREA = 420
        return max(int(text_zone_px / EFFECTIVE_CHAR_AREA), 1)

    def _build_loss_breakdown(
        self,
        zones: Dict[str, float],
        paddle: Dict[str, Any],
        doctr: Optional[Dict[str, int]],
        estimated_total: int,
    ) -> Dict[str, Any]:
        """Build 100%-sum accuracy loss breakdown."""
        text_area = zones["text_area_pct"]

        # Best available ground truth
        if doctr and doctr.get("doctr_ground_truth", 0) > 0:
            ground_truth = doctr["doctr_ground_truth"]
        else:
            ground_truth = estimated_total

        processed_chars = paddle.get("processed_char_count", 0)
        read_fraction = min(processed_chars / max(ground_truth, 1), 1.0)

        text_read = text_area * read_fraction
        text_lost = text_area * (1 - read_fraction)

        img_pct = zones.get("image_area_pct", 0)
        sig_pct = zones.get("signature_area_pct", 0)
        stamp_pct = zones.get("stamp_area_pct", 0)
        noise_pct = zones.get("noise_area_pct", 0)
        ws_pct = zones.get("whitespace_pct", 0)

        parts = {
            "text_read_pct": text_read,
            "unreadable_text_pct": text_lost,
            "logos_images_pct": img_pct,
            "signatures_pct": sig_pct,
            "stamps_seals_pct": stamp_pct,
            "noise_artifacts_pct": noise_pct,
            "whitespace_margins_pct": ws_pct,
        }
        parts = _normalize_partition_to_100(parts)

        # Include snippets in the returned dictionary
        parts_with_snippets = parts.copy()
        parts_with_snippets["snippets"] = zones.get("bboxes", [])

        return {
            "extraction_accuracy": round(read_fraction * 100, 2),
            "accuracy_loss_breakdown": parts_with_snippets,
            "total_accounted": 100.0,
        }

    def _validate_whitespace_region(
        self, image_binary: np.ndarray, region: Tuple[int, int, int, int]
    ) -> bool:
        """Verify if region is genuine whitespace (no hidden content)."""
        x1, y1, x2, y2 = region
        h, w = image_binary.shape
        x1 = max(0, min(w, x1))
        x2 = max(0, min(w, x2))
        y1 = max(0, min(h, y1))
        y2 = max(0, min(h, y2))
        if x1 >= x2 or y1 >= y2:
            return True

        roi = image_binary[y1:y2, x1:x2]
        rh, rw = roi.shape
        patch_size = 32

        for py in range(0, rh, patch_size):
            for px in range(0, rw, patch_size):
                patch = roi[py:py+patch_size, px:px+patch_size]
                if patch.size == 0:
                    continue
                # Std deviation of grayscale: < 3.0 = truly blank
                if np.std(patch.astype(float)) >= 3.0:
                    return False
                # Sobel edges: any strong edge = possible faded text
                sobel = cv2.Sobel(patch, cv2.CV_64F, 1, 0, ksize=3)
                if np.max(np.abs(sobel)) > 10.0:
                    return False
                # If mean intensity is below 240, it is not white
                if np.mean(patch) < 240:
                    return False
        return True

    def _detect_faded_text_regions(self, image_bytes: bytes) -> List[Dict[str, Any]]:
        """Detect regions where printed text exists but is too faint for Otsu binarization."""
        nparr = np.frombuffer(image_bytes, np.uint8)
        img_gray = cv2.imdecode(nparr, cv2.IMREAD_GRAYSCALE)
        if img_gray is None:
            return []

        h, w = img_gray.shape
        total_px = h * w

        # Pass 1: Otsu
        otsu_thresh, otsu_mask = cv2.threshold(img_gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
        if otsu_thresh > 180:
            _, otsu_mask = cv2.threshold(img_gray, 180, 255, cv2.THRESH_BINARY_INV)

        # Pass 2: Adaptive
        adaptive_mask = cv2.adaptiveThreshold(
            img_gray, 255,
            cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV,
            15, 8
        )

        # Faded = visible in adaptive but NOT in Otsu
        faded_only = cv2.bitwise_and(adaptive_mask, cv2.bitwise_not(otsu_mask))

        # Merge letters into line blobs
        h_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (40, 1))
        line_blobs = cv2.dilate(faded_only, h_kernel, iterations=1)
        v_kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (1, 8))
        line_blobs = cv2.dilate(line_blobs, v_kernel, iterations=1)

        contours, _ = cv2.findContours(line_blobs, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        faded_bboxes = []
        for cnt in contours:
            x, y, cw, ch = cv2.boundingRect(cnt)
            area = cw * ch
            if area < total_px * 0.0005:
                continue

            roi_faded = faded_only[y:y+ch, x:x+cw]
            ink_density = np.count_nonzero(roi_faded) / max(area, 1)
            if ink_density < 0.003:
                continue

            impact = round(area / total_px * 100, 3)
            faded_bboxes.append({
                "type": "faded_text",
                "bbox": [int(x), int(y), int(x+cw), int(y+ch)],
                "impact": impact,
                "ink_density": round(ink_density, 4),
                "needs_paddle_confirm": True,
            })

        return faded_bboxes
