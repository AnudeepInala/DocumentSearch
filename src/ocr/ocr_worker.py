"""
OCR Worker - Processes images/scanned documents with PaddleOCR
Includes NLP text correction after OCR
"""

import io
import json
import os
import time
import tempfile
import threading
import re
import numpy as np
from typing import Dict, Any, Optional, Tuple, List
from pathlib import Path
from datetime import datetime
import shutil

from core.logging_manager import get_logger
from core.config_manager import get_config
from core.queue_manager import get_queue_manager
from core.constants import ErrorType
from core.reporting_manager import (
    AuditEvent,
    FileStateRow,
    build_smart_id,
    derive_file_key,
    normalize_file_type,
    record_event,
    upsert_file_state,
    update_accuracy_metrics,
    create_snippet_review,
    update_snippet_review_status,
    get_approved_features_for_doc,
)

from .image_preprocessor_advanced import ImagePreprocessor
from .paddle_wrapper import PaddleWrapper

try:
    import cv2 as cv2
except ImportError:
    cv2 = None

# Import indexing client for updating documents
import sys
sys.path.append(str(Path(__file__).parent.parent))
from indexing.opensearch_client import OpenSearchClient
from indexing.document_builder import DocumentBuilder

# Try to import PDF support
try:
    from pdf2image import convert_from_path
    PDF_SUPPORT = True
except ImportError:
    PDF_SUPPORT = False

try:
    import fitz
    FITZ_SUPPORT = True
except ImportError:
    FITZ_SUPPORT = False

try:
    import pdfplumber
    PDFPLUMBER_SUPPORT = True
except ImportError:
    PDFPLUMBER_SUPPORT = False

# Try to import NLP corrector
try:
    from nlp.text_corrector import get_text_corrector
    NLP_AVAILABLE = True
except ImportError:
    NLP_AVAILABLE = False

logger = get_logger("ocr.worker")

OCR_CACHE_VERSION = "v2"


class OCRWorker:
    """OCR worker - processes scanned documents and images with NLP correction"""
    
    def __init__(self, worker_id: str):
        self.worker_id = worker_id
        
        self.config = get_config()
        self.queue_manager = get_queue_manager()
        
        # Initialize OCR components
        self.preprocessor = ImagePreprocessor()
        self.paddle = PaddleWrapper()
        

        # Initialize NLP text corrector for OCR text
        self.text_corrector = None
        if NLP_AVAILABLE:
            try:
                self.text_corrector = get_text_corrector()
                logger.info(f"Worker {worker_id}: NLP text corrector initialized for OCR")
            except Exception as e:
                logger.warning(f"Worker {worker_id}: Could not initialize NLP corrector: {e}")
        else:
            logger.warning(f"Worker {worker_id}: NLP module not available for OCR text corrections")

        # Inject Poppler into PATH BEFORE checking availability
        poppler_path = getattr(self.config.ocr, 'poppler_path', '')
        if poppler_path:
            poppler_path = str(poppler_path)
            # Only inject when the directory actually exists; injecting a
            # non-existent path can trigger Windows shell "Could not find files
            # for the given pattern(s)." noise from subsequent glob-based calls.
            if os.path.isdir(poppler_path):
                current_path = os.environ.get("PATH", "")
                if poppler_path not in current_path:
                    os.environ["PATH"] = poppler_path + os.pathsep + current_path
                    logger.info("Worker %s: Injected Poppler into PATH: %s", self.worker_id, poppler_path)
                os.environ.setdefault("POPPLER_PATH", poppler_path)
            else:
                logger.info(
                    "Worker %s: Configured poppler_path does not exist (%s) — will use PyMuPDF for PDF rasterisation.",
                    self.worker_id, poppler_path,
                )
        
        # NOW check if poppler is available (after PATH injection)
        self.poppler_available = PDF_SUPPORT and self._check_poppler_tools()
        
        # Initialize OpenSearch client for updates
        try:
            self.os_client = OpenSearchClient()
            startup_timeout = getattr(
                self.config.indexing.opensearch,
                'startup_timeout_seconds',
                120
            )

            if not self.os_client.wait_for_availability(timeout_seconds=startup_timeout):
                logger.warning(
                    "Worker %s: OpenSearch unavailable after %ss; OCR updates will be deferred",
                    worker_id,
                    startup_timeout
                )
                self.os_client = None
        except Exception as exc:
            logger.warning(
                "Worker %s: Could not initialize OpenSearch client for OCR updates: %s",
                worker_id,
                exc
            )
            self.os_client = None

        self.document_builder = DocumentBuilder()
        
        # Initialize accuracy analyzer
        self.accuracy_analyzer = None
        self._empty_metrics_fn = lambda pipeline_type="text_extraction", tier="tier1": {
            "pipeline_type": pipeline_type,
            "extraction_accuracy": 0.0,
            "text_area_pct": 0.0,
            "non_text_area_pct": 0.0,
            "raw_char_count": 0,
            "processed_char_count": 0,
            "preprocessing_gain_pct": 0.0,
            "accuracy_loss_json": "{}",
            "page_metrics_json": "[]",
            "accuracy_tier": tier,
        }
        try:
            from extraction.accuracy_analyzer import AccuracyAnalyzer, _empty_metrics
            self.accuracy_analyzer = AccuracyAnalyzer(
                enable_yolo=True, enable_doctr=True
            )
            self._empty_metrics_fn = _empty_metrics
            logger.info(f"Worker {worker_id}: Accuracy analyzer initialized (tier: {self.accuracy_analyzer.tier})")
        except Exception as exc:
            logger.warning(f"Worker {worker_id}: Accuracy analyzer unavailable: {exc}")

        # Initialize visual memory engine
        self.visual_memory = None
        try:
            from ocr.visual_memory import VisualMemoryEngine
            self.visual_memory = VisualMemoryEngine()
            logger.info(f"Worker {worker_id}: Visual memory engine initialized")
        except Exception as exc:
            logger.warning(f"Worker {worker_id}: Visual memory engine unavailable: {exc}")

        # Quality thresholds
        self.quality_config = self.config.ocr.quality
        self.min_confidence = self.quality_config.get('min_confidence', 25)
        self.good_confidence = self.quality_config.get('good_confidence', 70)
        
        # Pending OCR updates for batch processing
        self.pending_updates: List[Dict[str, Any]] = []
        
        # Statistics
        self.files_processed = 0
        self.files_failed = 0
        self.low_confidence_count = 0
        self.total_confidence = 0
        self.nlp_corrections_applied = 0
        self.start_time = None
        self.running = False
        self._poppler_warned = False
    
    def _check_poppler_tools(self) -> bool:
        """Check if poppler tools are available in PATH after injection"""
        tools = ("pdftoppm", "pdfinfo")
        available = all(shutil.which(t) for t in tools)
        
        if not available:
            logger.warning(
                "Worker %s: Poppler tools not found in PATH even after injection. "
                "PDF OCR will be skipped. Verify poppler_path in config.",
                self.worker_id
            )
        else:
            logger.info("Worker %s: Poppler tools detected and ready", self.worker_id)
        
        return available
    
    def run(self) -> None:
        """Main worker loop"""
        self.running = True
        self.start_time = time.time()
        
        logger.info(f"Worker {self.worker_id}: Starting OCR processing")

        # Start heartbeat thread
        heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        heartbeat_thread.start()
        
        # Warn if PDF support is not available
        if not PDF_SUPPORT:
            logger.warning(f"Worker {self.worker_id}: PDF support not available - install pdf2image and poppler for PDF OCR")
        elif not self.poppler_available:
            if FITZ_SUPPORT:
                logger.info(
                    "Worker %s: Poppler not found - using PyMuPDF (fitz) for PDF rasterisation.",
                    self.worker_id
                )
            else:
                logger.warning(
                    "Worker %s: Poppler not installed or not in PATH and PyMuPDF unavailable - PDF OCR will be skipped. "
                    "Install poppler-utils: Windows users can download from https://github.com/oschwartz10612/poppler-windows/releases/",
                    self.worker_id
                )
        
        # Critical: check PaddleOCR is actually installed
        if not self.paddle.health_check():
            logger.critical(
                "Worker %s: PaddleOCR is NOT installed or failed to initialise. "
                "ALL OCR jobs will fail. Run: pip install paddleocr paddlepaddle",
                self.worker_id
            )
        else:
            # Pre-warm the primary OCR engine so first-file latency is absorbed at startup.
            self.paddle.prewarm()
        
        consecutive_empty = 0
        max_empty_polls = 10
        
        try:
            while self.running:
                # ---- Memory management ----
                if self.files_processed > 0 and self.files_processed % 50 == 0:
                    import gc
                    gc.collect()
                    try:
                        import psutil as _psutil
                        proc = _psutil.Process()
                        mem_mb = proc.memory_info().rss / 1024 / 1024
                        if mem_mb > 4096:
                            logger.warning(
                                f"Worker {self.worker_id}: {mem_mb:.0f} MB RSS exceeds 4 GB. "
                                "Self-terminating for clean restart."
                            )
                            break
                        sys_mem = _psutil.virtual_memory()
                        if sys_mem.percent > 85:
                            logger.warning(
                                f"Worker {self.worker_id}: System memory {sys_mem.percent:.0f}%. Pausing 10s."
                            )
                            time.sleep(10)
                            gc.collect()
                    except Exception:
                        pass

                # Claim work from OCR queue (1 item at a time for fair distribution)
                work_items = self.queue_manager.claim_ocr_work(
                    worker_id=self.worker_id,
                    batch_size=1
                )
                
                if not work_items:
                    consecutive_empty += 1

                    if consecutive_empty >= max_empty_polls:
                        logger.debug(f"Worker {self.worker_id}: No work available, idling...")
                        time.sleep(5)
                        consecutive_empty = 0
                    else:
                        time.sleep(0.5)
                    continue
                
                consecutive_empty = 0
                
                # Process work item
                for work_item in work_items:
                    if not self.running:
                        break
                    
                    self._process_file(work_item)
                    
                    # Log progress periodically
                    if self.files_processed % 50 == 0:
                        self._log_progress()
            
            # Final flush
            if self.pending_updates:
                self._flush_updates()
            
            self._log_final_stats()
            
        except (KeyboardInterrupt, SystemExit):
            logger.info(f"Worker {self.worker_id}: Shutdown signal received, exiting gracefully")
        except Exception as e:
            logger.error(f"Worker {self.worker_id}: Fatal error: {e}", exc_info=True)
        finally:
            self.running = False
    
    def _process_file(self, work_item: Dict[str, Any]) -> None:
        """Process a single file for OCR with comprehensive error handling"""
        queue_id = work_item.get('id')
        file_id = work_item.get('file_id')
        file_path = work_item.get('file_path')
        if not all([queue_id is not None, file_id is not None, file_path]):
            logger.error(
                "Worker %s: Malformed work_item (missing id/file_id/file_path): %s",
                self.worker_id, work_item
            )
            self.files_failed += 1
            return
        file_hash = self._get_file_hash(file_id)
        file_key = derive_file_key(file_hash=file_hash, file_id=file_id, file_path=file_path)
        processed_on = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        smart_id = build_smart_id(file_key=file_key, when_iso=processed_on)
        file_size = 0
        try:
            file_info = self.queue_manager.get_file_info(file_id) or {}
            file_size = int(file_info.get('file_size', 0) or 0)
        except Exception:
            pass
        
        start_time = time.time()
        self._emit_stage_audit(
            stage="ocr",
            status="processing",
            file_id=file_id,
            file_path=file_path,
            file_hash=file_hash,
            file_size=file_size,
            state_status="processing",
            state_stage="ocr",
            processed_on=processed_on,
            smart_id=smart_id,
        )
        
        try:
            # Determine file type and validate
            file_ext = str(file_path).lower().rsplit('.', 1)[-1] if '.' in str(file_path) else ''
            
            # Define supported formats
            image_exts = {'jpg', 'jpeg', 'png', 'tif', 'tiff', 'bmp', 'gif', 'webp'}
            pdf_ext = {'pdf'}
            office_exts = {'doc', 'docx', 'xls', 'xlsx', 'ppt', 'pptx'}
            
            # Skip office documents - they shouldn't be flagged for OCR
            if file_ext in office_exts:
                logger.warning(f"Skipping Office document (should not be in OCR queue): {file_path}")
                self.queue_manager.complete_ocr(queue_id, 0.0, 0, self.worker_id)
                self.files_processed += 1
                self._emit_stage_audit(
                    stage="ocr",
                    status="skipped",
                    file_id=file_id,
                    file_path=file_path,
                    file_hash=file_hash,
                    file_size=file_size,
                    state_status="completed",
                    state_stage="ocr",
                    error_message="Skipped OCR for office document",
                    processed_on=processed_on,
                    smart_id=smart_id,
                )
                return
            
            # Process based on file type
            if file_ext in image_exts:
                result = self._process_image_file(file_path)
            elif file_ext in pdf_ext:
                smart_routing = getattr(self.config.ocr, 'smart_routing', {})
                is_rich = False
                extracted_text = ""
                if smart_routing.get('enabled', False):
                    min_chars = smart_routing.get('min_text_chars_per_page', 200)
                    use_pymupdf = smart_routing.get('use_pymupdf', True)
                    is_rich, extracted_text = self._is_text_rich_pdf(file_path, min_chars, use_pymupdf)
                
                if is_rich:
                    logger.info(f"Worker {self.worker_id}: Smart Routing - {file_path} is text-rich. Skipping OCR.")
                    result = (extracted_text, 100.0, [])
                else:
                    # Skip PDFs only when BOTH Poppler and PyMuPDF are unavailable.
                    # PyMuPDF (fitz) is used as a zero-dependency fallback rasteriser.
                    if not self.poppler_available and not FITZ_SUPPORT:
                        processing_time_ms = int((time.time() - start_time) * 1000)
                        logger.warning(
                            "Worker %s: Skipping PDF OCR for %s — neither Poppler nor PyMuPDF available",
                            self.worker_id,
                            file_path
                        )
                        self.queue_manager.complete_ocr(queue_id, 0.0, processing_time_ms, self.worker_id)
                        self.files_processed += 1
                        self._emit_stage_audit(
                            stage="ocr",
                            status="skipped",
                            file_id=file_id,
                            file_path=file_path,
                            file_hash=file_hash,
                            file_size=file_size,
                            state_status="completed",
                            state_stage="ocr",
                            payload={"processing_time_ms": processing_time_ms},
                            error_message="Skipped PDF OCR: Poppler and PyMuPDF both unavailable",
                            processed_on=processed_on,
                            smart_id=smart_id,
                        )
                        return
                    result = self._process_pdf_file(file_path, smart_id=smart_id)
            else:
                logger.warning(f"Unsupported file type .{file_ext} for OCR: {file_path}")
                self.queue_manager.complete_ocr(queue_id, 0.0, 0, self.worker_id)
                self.files_processed += 1
                self._emit_stage_audit(
                    stage="ocr",
                    status="skipped",
                    file_id=file_id,
                    file_path=file_path,
                    file_hash=file_hash,
                    file_size=file_size,
                    state_status="completed",
                    state_stage="ocr",
                    error_message=f"Unsupported OCR type: .{file_ext}",
                )
                return
            
            if not result or not result[0].strip():
                self._handle_failure(
                    queue_id=queue_id,
                    file_id=file_id,
                    file_path=file_path,
                    error_message=f"OCR processing produced no text content for {Path(file_path).name}"
                )
                return
            
            ocr_text, confidence = result[:2]
            
            # --- Accuracy analysis ---
            if self.accuracy_analyzer:
                try:
                    acc_metrics = None
                    if file_ext in image_exts:
                        prep_bytes = result[2]
                        with open(file_path, "rb") as f:
                            original_bytes = f.read()
                        acc_metrics = self.accuracy_analyzer.analyze_ocr_page(original_bytes, prep_bytes, smart_id=smart_id, page_num=1)

                        # Full-page validation snippet: only queue for human review when
                        # OCR confidence is below the 'good' threshold so we don't flood
                        # the snippet portal with pages the OCR already handled well.
                        _good_conf = float(getattr(getattr(self.config, 'ocr', None) and getattr(self.config.ocr, 'quality', None), 'good_confidence', 70) if False else 70)
                        try:
                            _good_conf = float(self.config.ocr.quality.good_confidence)
                        except Exception:
                            _good_conf = 70.0
                        if float(confidence) < _good_conf:
                            try:
                                working_root = Path(self.config.paths.working_root)
                                review_snippets_root = working_root / "review_snippets"
                                full_page_dir = review_snippets_root / smart_id
                                full_page_dir.mkdir(parents=True, exist_ok=True)
                                full_path = full_page_dir / "page_1_full_page_validation.png"
                                with open(full_path, "wb") as f_out:
                                    f_out.write(original_bytes)
                                review_id = f"{smart_id}_p1_full_page_validation"
                                from core.reporting_manager import create_snippet_review, update_snippet_review_status
                                create_snippet_review(
                                    review_id=review_id,
                                    smart_id=smart_id,
                                    page_num=1,
                                    snippet_type="full_page",
                                    snippet_path=str(full_path),
                                    bounding_box=[0, 0, 9999, 9999],
                                    accuracy_impact=0.0,
                                    reviewer_role="Document Reviewer"
                                )
                                update_snippet_review_status(review_id, status="pending")
                            except Exception as val_e:
                                logger.error(f"Failed to create full page validation: {val_e}")

                        if acc_metrics and "accuracy_loss_json" in acc_metrics:
                            self._process_visual_snippets(
                                smart_id=smart_id,
                                page_num=1,
                                page_bytes=original_bytes,
                                accuracy_loss_json_str=acc_metrics["accuracy_loss_json"],
                                file_path=file_path,
                            )
                        if acc_metrics is not None:
                            acc_metrics["page_metrics_json"] = json.dumps([{
                                "page": 1,
                                "extraction_accuracy": acc_metrics.get("extraction_accuracy"),
                                "text_area_pct": acc_metrics.get("text_area_pct"),
                                "non_text_area_pct": acc_metrics.get("non_text_area_pct"),
                            }])
                    elif file_ext in pdf_ext:
                        page_metrics_list = result[2]
                        acc_metrics = self._aggregate_ocr_metrics(page_metrics_list)
                    
                    if acc_metrics:
                        acc_file_key = derive_file_key(file_hash=file_hash, file_id=file_id, file_path=file_path)
                        update_accuracy_metrics(acc_file_key, acc_metrics)
                        logger.debug(
                            f"Worker {self.worker_id}: Saved OCR accuracy metrics for {file_path}: "
                            f"{acc_metrics.get('extraction_accuracy', 0.0):.1f}%"
                        )
                except Exception as acc_exc:
                    logger.warning(f"Worker {self.worker_id}: Failed to save OCR accuracy metrics: {acc_exc}")
            
            # Check confidence threshold - skip very low quality OCR
            if confidence < self.min_confidence:
                self.low_confidence_count += 1
                logger.warning(
                    f"Low confidence OCR ({confidence:.1f}% < {self.min_confidence}%) - skipping indexing for {file_path}"
                )
                # Mark OCR stage complete for this worker but don't count as successful extraction
                processing_time_ms = int((time.time() - start_time) * 1000)
                self.queue_manager.complete_ocr(queue_id, confidence, processing_time_ms, self.worker_id)
                self.files_processed += 1
                self._emit_stage_audit(
                    stage="ocr",
                    status="skipped",
                    file_id=file_id,
                    file_path=file_path,
                    file_hash=file_hash,
                    file_size=file_size,
                    state_status="completed",
                    state_stage="ocr",
                    payload={"ocr_confidence": confidence, "processing_time_ms": processing_time_ms},
                    error_message="Low confidence OCR skipped for indexing",
                )
                return
            
            # Apply NLP text corrections to OCR text
            if self.text_corrector:
                try:
                    corrected_text, corrections = self.text_corrector.correct(ocr_text)
                    if corrections > 0:
                        ocr_text = corrected_text
                        self.nlp_corrections_applied += corrections
                        logger.debug(f"Applied {corrections} NLP corrections to OCR text for {file_path}")
                except Exception as e:
                    logger.warning(f"NLP correction failed for OCR text {file_path}: {e}")
            
            # Update document in OpenSearch FIRST, then mark complete (Fix #1: prevent data loss)
            processing_time_ms = int((time.time() - start_time) * 1000)
            file_hash = self._get_file_hash(file_id)
            if not file_hash:
                logger.warning(f"No file hash found for file_id {file_id}, cannot update document")
                # Still mark complete — no hash means we can't persist anyway
                self.queue_manager.complete_ocr(queue_id, confidence, processing_time_ms, self.worker_id)
            elif not self.os_client:
                logger.warning(
                    "Worker %s: OpenSearch client unavailable; OCR update skipped for %s",
                    self.worker_id,
                    file_path
                )
                # Still mark complete but persist to Redis retry queue (Fix #2)
                self._persist_pending_update(file_id, str(file_hash), ocr_text, confidence)
                self.queue_manager.complete_ocr(queue_id, confidence, processing_time_ms, self.worker_id)
            else:
                doc_id = str(file_hash)
                
                # Retrieve metrics safely
                metrics_to_store = locals().get('acc_metrics', None)
                success = self.os_client.update_document_ocr(doc_id, ocr_text, confidence, metrics=metrics_to_store)
                
                if success:
                    # Mark OCR complete ONLY after OpenSearch confirms persistence
                    self.queue_manager.complete_ocr(queue_id, confidence, processing_time_ms, self.worker_id)
                    logger.info(f"Worker {self.worker_id}: Updated OCR for {file_path} (confidence={confidence:.1f}%)")
                else:
                    # Persist to Redis for crash recovery (Fix #2), then mark complete
                    self._persist_pending_update(file_id, doc_id, ocr_text, confidence)
                    self.queue_manager.complete_ocr(queue_id, confidence, processing_time_ms, self.worker_id)
                    self.pending_updates.append({
                        'file_id': file_id,
                        'file_hash': doc_id,
                        'update': {
                            'ocr_content': ocr_text,
                            'ocr_confidence': confidence
                        }
                    })
                    logger.debug(f"OCR update queued for retry: {file_path}")
                    batch_limit = max(1, int(getattr(self.config.ocr, 'update_batch_size', 20)))
                    if len(self.pending_updates) >= batch_limit:
                        self._flush_updates()
            
            self.files_processed += 1
            self.total_confidence += confidence
            self._emit_stage_audit(
                stage="ocr",
                status="completed",
                file_id=file_id,
                file_path=file_path,
                file_hash=file_hash,
                file_size=file_size,
                state_status="completed",
                state_stage="ocr",
                payload={"ocr_confidence": confidence, "processing_time_ms": processing_time_ms},
            )
            
        except FileNotFoundError:
            logger.error(f"File not found: {file_path}")
            self._handle_failure(queue_id, file_id, file_path, "File not found")
            
        except PermissionError:
            logger.error(f"Permission denied accessing: {file_path}")
            self._handle_failure(queue_id, file_id, file_path, "Permission denied")
            
        except Exception as e:
            logger.error(f"Error processing {file_path}: {e}", exc_info=True)
            error_detail = f"{type(e).__name__}: {str(e)}"
            self._handle_failure(queue_id, file_id, file_path, error_detail)
    
    def _process_image_file(self, file_path: str) -> Optional[Tuple[str, float, bytes]]:
        """Process an image file with Smart OCR strategies
        
        Returns:
            Tuple of (text, confidence, preprocessed_bytes) or None on failure
        """
        tmp_path = None
        
        try:
            # Check if smart retries are enabled
            smart_config = getattr(self.config.ocr, 'smart_retries', {})
            use_smart = smart_config.get('enabled', False)
            target_conf = smart_config.get('min_confidence_threshold', 80.0)
            
            # Read image file
            with open(file_path, 'rb') as f:
                original_bytes = f.read()
                
            if not use_smart:
                # Legacy simple mode
                res = self._run_ocr_attempt(original_bytes, "Standard", file_path)
                if res:
                    result, prep_bytes = res
                    if result:
                        text, conf = result
                        return text, conf, prep_bytes
                return None
            
            # --- Smart Strategy Loop ---
            
            # Define strategies: (Name, PreprocessingFunc, DummyPSM)
            # Each strategy targets a different type of image/content:
            #   PSM 3 = Auto page segmentation
            #   PSM 4 = Single column
            #   PSM 6 = Single uniform block of text
            #   PSM 11 = Sparse text
            #   PSM 12 = Sparse text + OSD
            strategies = [
                ("1. Standard", lambda b: self.preprocessor.preprocess(b), "3"),      # Full pipeline
                ("2. Dense Block", lambda b: self.preprocessor.apply_clahe_only(b), "6"), # Block text
                ("3. Sparse/Messy", lambda b: self.preprocessor.apply_binarization(b), "11"), # Sparse
                ("4. High Res", lambda b: self.preprocessor.resize_image(b, 2.0), "3"),   # Upscale 2x
                ("5. Table/Column", lambda b: self.preprocessor.apply_clahe_only(b), "4"), # Column layout
                ("6. Color BG Remove", lambda b: self.preprocessor.remove_color_background_aggressive(b), "6"),  # Colored bg
                ("7. Inverted/Dark", lambda b: self.preprocessor.invert_and_enhance(b), "6"), # Light-on-dark
                ("8. Raw Fallback", lambda b: b, "12"),                                 # Sparse+OSD
                # --- Rotation Fallbacks (Brute Force) ---
                ("9. Rotate 90", lambda b: self.preprocessor.rotate_image(b, 90), "6"),
                ("10. Rotate 180", lambda b: self.preprocessor.rotate_image(b, 180), "6"),
                ("11. Rotate 270", lambda b: self.preprocessor.rotate_image(b, 270), "6"),
                # --- Extreme upscale for tiny images ---
                ("12. Extreme Upscale", lambda b: self.preprocessor.resize_image(b, 3.0), "6"), # 3x upscale
            ]
            
            best_result = None
            best_conf = -1.0
            best_prep_bytes = original_bytes
            original_psm = getattr(self.paddle, 'psm', '3')  # Save original PSM
            
            logger.info(f"Starting Smart OCR for {Path(file_path).name}")
            
            for name, prep_func, psm in strategies:
                try:
                    self.paddle.psm = psm 
                    
                    # Run attempt
                    prep_bytes = prep_func(original_bytes)
                    if not prep_bytes: prep_bytes = original_bytes
                    
                    # Save temp
                    with tempfile.NamedTemporaryFile(mode='wb', suffix='.png', delete=False) as tmp_file:
                        tmp_file.write(prep_bytes)
                        current_tmp = tmp_file.name
                        
                    try:
                        result = self.paddle.extract_text(current_tmp)
                    finally:
                        if os.path.exists(current_tmp):
                            os.unlink(current_tmp)
                    
                    if result:
                        text, conf = result
                        
                        # Logging
                        has_text = len(text.strip()) > 0
                        logger.debug(f"  Strategy '{name}': Conf={conf:.1f}%, TextLen={len(text)}")
                        
                        if has_text:
                            # Keep if it's the best so far (purely by confidence for fallback)
                            if conf > best_conf:
                                best_result = result
                                best_conf = conf
                                best_prep_bytes = prep_bytes
                                
                            # Validate result quality using heuristics
                            is_valid, reason = self._validate_ocr_result(text, conf, target_conf)
                            
                            if is_valid:
                                logger.info(f"  Strategy '{name}' accepted: {reason}")
                                self.paddle.psm = original_psm
                                return text, conf, prep_bytes
                                
                except Exception as strat_error:
                    logger.warning(f"Strategy '{name}' failed: {strat_error}")
            
            # Restore original PSM after smart OCR loop
            self.paddle.psm = original_psm
            
            # Return best found if it has some content, even if it didn't pass strict validation
            if best_result:
                logger.info(f"Smart OCR finished. No perfect match, returning best result: {best_conf:.1f}%")
                text, conf = best_result
                return text, conf, best_prep_bytes
                
            logger.warning(f"All Smart OCR strategies failed for {file_path}")
            return None
            
        except Exception as e:
            logger.error(f"Error processing image {file_path}: {e}", exc_info=True)
            return None
            
    def _validate_ocr_result(self, text: str, conf: float, target_conf: float) -> Tuple[bool, str]:
        """
        Validate OCR result using heuristics to avoid 'high confidence garbage'.
        
        Args:
            text: Extracted text
            conf: Average confidence score
            target_conf: Target confidence threshold
            
        Returns:
            (is_valid, reason_string)
        """
        # 1. Check Confidence
        if conf < target_conf:
             return False, f"Confidence {conf:.1f}% < {target_conf}%"
             
        # 2. Check Text Length (Ignore tiny noise)
        clean_text = text.strip()
        if len(clean_text) < 3:
            return False, "Text too short (<3 chars)"
            
        # 3. Check Word-character Ratio (Avoid pure symbol garbage like ".,;..")
        # Use Unicode-aware \w so non-Latin scripts (Telugu, Devanagari, CJK, etc.)
        # are counted as valid word characters, not stripped to zero.
        import re
        word_char_count = len(re.sub(r'\W', '', clean_text, flags=re.UNICODE))
        total_count = len(clean_text.replace(" ", "").replace("\n", ""))  # Ignore whitespace
        
        if total_count == 0:
            return False, "Empty text"
            
        ratio = word_char_count / total_count
        
        # We expect at least 50% of non-whitespace characters to be word characters.
        # This allows for some punctuation but rejects "......" or "| | |"
        if ratio < 0.5:
            return False, f"Low word-character ratio ({ratio:.2f} < 0.5) - likely noise"
            
        return True, f"Valid (Conf={conf:.1f}%, Ratio={ratio:.2f})"

    def _run_ocr_attempt(self, image_data: bytes, strategy_name: str, file_path: str) -> Optional[Tuple[Tuple[str, float], bytes]]:
        """Legacy helper for simple single-pass"""
        tmp_path = None
        try:
            preprocessed_data = self.preprocessor.preprocess(image_data)
            if not preprocessed_data:
                preprocessed_data = image_data
                
            with tempfile.NamedTemporaryFile(mode='wb', suffix='.png', delete=False) as tmp_file:
                tmp_file.write(preprocessed_data)
                tmp_path = tmp_file.name
                
            result = self.paddle.extract_text(tmp_path)
            return result, preprocessed_data
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)
    
    def _is_text_rich_pdf(self, file_path: str, min_chars_per_page: int, use_pymupdf: bool = True) -> Tuple[bool, str]:
        """Check if PDF has embedded text and extract it if rich enough."""
        text_content = []
        try:
            if use_pymupdf and FITZ_SUPPORT:
                with fitz.open(file_path) as doc:
                    num_pages = len(doc)
                    if num_pages == 0:
                        return False, ""
                    for page in doc:
                        text_content.append(page.get_text())
            elif PDFPLUMBER_SUPPORT:
                import pdfplumber
                with pdfplumber.open(file_path) as doc:
                    num_pages = len(doc.pages)
                    if num_pages == 0:
                        return False, ""
                    for page in doc.pages:
                        page_text = page.extract_text()
                        if page_text:
                            text_content.append(page_text)
            else:
                return False, ""
                
            full_text = "\n".join(text_content).strip()
            if not full_text:
                return False, ""
                
            # Use same word-character ratio heuristic to avoid counting garbage
            import re
            word_char_count = len(re.sub(r'\W', '', full_text, flags=re.UNICODE))
            avg_chars_per_page = word_char_count / num_pages
            
            if avg_chars_per_page >= min_chars_per_page:
                return True, full_text
                
        except Exception as e:
            logger.warning(f"Worker {self.worker_id}: Error checking PDF text density: {e}")
            
        return False, ""

    def _get_page_digital_text(self, file_path: str, page_num: int, min_chars: int = 50, use_pymupdf: bool = True) -> Optional[str]:
        """Extract digital text for a specific page using PyMuPDF or pdfplumber."""
        try:
            if use_pymupdf and FITZ_SUPPORT:
                with fitz.open(file_path) as doc:
                    if 1 <= page_num <= len(doc):
                        page = doc[page_num - 1]
                        text = page.get_text()
                        import re
                        word_chars = len(re.sub(r'\W', '', text, flags=re.UNICODE))
                        if word_chars >= min_chars:
                            return text.strip()
            elif PDFPLUMBER_SUPPORT:
                import pdfplumber
                with pdfplumber.open(file_path) as doc:
                    if 1 <= page_num <= len(doc.pages):
                        page = doc.pages[page_num - 1]
                        text = page.extract_text()
                        if text:
                            import re
                            word_chars = len(re.sub(r'\W', '', text, flags=re.UNICODE))
                            if word_chars >= min_chars:
                                return text.strip()
        except Exception as e:
            pass
        return None

    def _pdf_pages_to_pil(self, file_path: str, first_page: int, last_page: int,
                            target_dpi: int, poppler_path: Optional[str],
                            chunk_size: int):
        """Render PDF pages to a list of PIL Images.

        Tries pdf2image/Poppler first; falls back to PyMuPDF (fitz) automatically
        so PDF OCR works without Poppler installed.

        Args:
            file_path:    Path to the PDF.
            first_page:   1-based first page to render.
            last_page:    1-based last page to render (inclusive).
            target_dpi:   Render resolution.
            poppler_path: Path injected into pdftoppm (may be None).
            chunk_size:   Thread-count hint for pdf2image.

        Returns:
            List[PIL.Image.Image] — may be empty when the page range is out of bounds.

        Raises:
            RuntimeError when both rasterisers fail.
        """
        # --- Poppler path ---
        if self.poppler_available and PDF_SUPPORT:
            try:
                kwargs = dict(
                    dpi=target_dpi,
                    first_page=first_page,
                    last_page=last_page,
                    thread_count=chunk_size,
                )
                if poppler_path:
                    kwargs["poppler_path"] = str(poppler_path)
                return convert_from_path(str(file_path), **kwargs)
            except Exception as pdf_error:
                err = str(pdf_error).lower()
                if 'poppler' in err or 'unable to get page count' in err:
                    self.poppler_available = False
                    logger.warning(
                        "Worker %s: Poppler failed (%s) — switching to PyMuPDF fallback.",
                        self.worker_id, pdf_error,
                    )
                else:
                    raise

        # --- PyMuPDF (fitz) fallback ---
        if not FITZ_SUPPORT:
            raise RuntimeError("Neither Poppler nor PyMuPDF available for PDF rasterisation.")

        from PIL import Image as _PILImage
        images = []
        with fitz.open(str(file_path)) as doc:
            n_pages = len(doc)
            for page_idx in range(first_page - 1, min(last_page, n_pages)):
                pix = doc[page_idx].get_pixmap(dpi=target_dpi)
                img = _PILImage.frombytes("RGB", [pix.width, pix.height], pix.samples)
                images.append(img)
        return images

    def _process_pdf_file(self, file_path: str, smart_id: str) -> Optional[Tuple[str, float, List[Dict[str, Any]]]]:
        """Process a PDF file (potentially scanned document) with OCR.

        Uses pdf2image/Poppler when available; falls back to PyMuPDF (fitz)
        automatically so PDF OCR works without Poppler installed.

        Returns:
            Tuple of (text, confidence, page_metrics_list) or None on failure.
        """
        if not self.poppler_available and not FITZ_SUPPORT:
            if not self._poppler_warned:
                logger.error(
                    "Worker %s: Cannot rasterise PDF — neither Poppler nor PyMuPDF available. "
                    "Install poppler-utils or run: pip install pymupdf",
                    self.worker_id,
                )
                self._poppler_warned = True
            return None

        if not self.poppler_available and not self._poppler_warned:
            logger.info(
                "Worker %s: Poppler not found — using PyMuPDF for PDF rasterisation.",
                self.worker_id,
            )
            self._poppler_warned = True

        temp_dir = None
        try:
            # Convert PDF to images in chunks to reduce memory spikes
            poppler_path = getattr(self.config.ocr, 'poppler_path', None)
            target_dpi = getattr(self.config.ocr.preprocessing, 'target_dpi', 200)
            max_pages = getattr(self.config.ocr, 'max_pages_per_pdf', 50)
            chunk_size = max(1, getattr(getattr(self.config.ocr, 'multipage', None), 'max_parallel_pages', 4))

            # Process each page and combine results
            all_text = []
            total_confidence = 0.0
            processed_pages = 0
            total_pages_converted = 0
            page_start = 1
            page_metrics_list = []
            while page_start <= max_pages:
                page_end = min(page_start + chunk_size - 1, max_pages)
                try:
                    images = self._pdf_pages_to_pil(
                        file_path, page_start, page_end,
                        target_dpi, poppler_path, chunk_size
                    )
                except Exception as pdf_error:
                    logger.error(
                        "Worker %s: Failed to rasterise PDF pages %s-%s of %s: %s",
                        self.worker_id, page_start, page_end, file_path, pdf_error,
                    )
                    return None

                if not images:
                    if page_start == 1:
                        logger.warning(f"PDF conversion produced no images: {file_path}")
                    break
                total_pages_converted += len(images)

                for offset, image in enumerate(images):
                    page_num = page_start + offset
                    try:
                        # Convert PIL image to bytes for preprocessing pipeline
                        buf = io.BytesIO()
                        image.save(buf, format='PNG')
                        page_bytes = buf.getvalue()
                        buf.close()

                        # Free PIL image memory immediately
                        image.close()

                        import hashlib
                        page_hash = hashlib.sha256(page_bytes).hexdigest()
                        
                        r = getattr(self.queue_manager, 'client', None)
                        cached_result = None
                        if r:
                            try:
                                cached_json = r.get(f"docsearch:ocr_cache:{page_hash}")
                                if cached_json:
                                    cached_data = json.loads(cached_json)
                                    cache_version = str(cached_data.get('version', ''))
                                    cached_text = str(cached_data.get('text', '') or '').strip()
                                    cached_conf = float(cached_data.get('conf', 0.0) or 0.0)
                                    cached_word_chars = int(cached_data.get('word_chars', 0) or 0)
                                    min_cached_word_chars = int(getattr(self.config.ocr, 'min_cached_word_chars', 20))

                                    # Recompute old cache entries (no version) and low-quality
                                    # snippets so stale bad first-page OCR does not persist.
                                    if (
                                        cache_version == OCR_CACHE_VERSION
                                        and cached_text
                                        and cached_word_chars >= min_cached_word_chars
                                    ):
                                        cached_result = ((cached_text, cached_conf), page_bytes)
                                        logger.info(f"Worker {self.worker_id}: OCR cache hit for page {page_num}")
                                    else:
                                        logger.info(
                                            f"Worker {self.worker_id}: Ignoring stale/low-quality OCR cache for page {page_num} "
                                            f"(version={cache_version or 'none'}, word_chars={cached_word_chars})"
                                        )
                            except Exception as e:
                                pass

                        digital_text = self._get_page_digital_text(file_path, page_num)
                        
                        if digital_text:
                            # Skip OCR! Use the digital text directly.
                            logger.info(f"Worker {self.worker_id}: Page {page_num} of {file_path} contains rich digital text. Skipping OCR.")
                            page_result = ((digital_text, 100.0), page_bytes)
                        elif cached_result:
                            page_result = cached_result
                        else:
                            # --- Smart OCR for each PDF page (same as images) ---
                            page_result = self._ocr_page_smart(page_bytes, page_num, file_path)
                            if page_result and r:
                                try:
                                    text, conf = page_result[0]
                                    word_chars = len(re.sub(r'\W', '', text or '', flags=re.UNICODE))
                                    r.setex(
                                        f"docsearch:ocr_cache:{page_hash}",
                                        604800,
                                        json.dumps({
                                            'version': OCR_CACHE_VERSION,
                                            'text': text,
                                            'conf': conf,
                                            'word_chars': word_chars,
                                        })
                                    )
                                except Exception as e:
                                    pass

                        if page_result:
                            (page_text, page_confidence), prep_bytes = page_result
                            all_text.append(f"\n--- Page {page_num} ---\n{page_text}")
                            total_confidence += page_confidence
                            processed_pages += 1

                            # Full-page validation snippet: only queue when OCR confidence
                            # is below the 'good' threshold.  High-confidence pages are
                            # already handled well and don't need human review.
                            try:
                                _good_conf_pdf = float(self.config.ocr.quality.good_confidence)
                            except Exception:
                                _good_conf_pdf = 70.0
                            if float(page_confidence) < _good_conf_pdf:
                                try:
                                    working_root = Path(self.config.paths.working_root)
                                    review_snippets_root = working_root / "review_snippets"
                                    full_page_dir = review_snippets_root / smart_id
                                    full_page_dir.mkdir(parents=True, exist_ok=True)
                                    full_path = full_page_dir / f"page_{page_num}_full_page_validation.png"
                                    with open(full_path, "wb") as f_out:
                                        f_out.write(page_bytes)
                                    review_id = f"{smart_id}_p{page_num}_full_page_validation"
                                    from core.reporting_manager import create_snippet_review, update_snippet_review_status
                                    create_snippet_review(
                                        review_id=review_id,
                                        smart_id=smart_id,
                                        page_num=page_num,
                                        snippet_type="full_page",
                                        snippet_path=str(full_path),
                                        bounding_box=[0, 0, 9999, 9999],
                                        accuracy_impact=0.0,
                                        reviewer_role="Document Reviewer"
                                    )
                                    update_snippet_review_status(review_id, status="pending")
                                except Exception as val_e:
                                    logger.error(f"Failed to create full page validation: {val_e}")

                            # Run page-level accuracy analysis
                            if self.accuracy_analyzer:
                                try:
                                    metrics = self.accuracy_analyzer.analyze_ocr_page(page_bytes, prep_bytes, smart_id=smart_id, page_num=page_num)
                                    metrics["page"] = page_num
                                    metrics["page_confidence"] = page_confidence
                                    page_metrics_list.append(metrics)
                                    
                                    # Trigger snippet extraction and visual memory routing for each page
                                    if metrics and "accuracy_loss_json" in metrics:
                                        self._process_visual_snippets(
                                            smart_id=smart_id,
                                            page_num=page_num,
                                            page_bytes=page_bytes,
                                            accuracy_loss_json_str=metrics["accuracy_loss_json"],
                                            file_path=file_path,
                                        )
                                except Exception as e:
                                    logger.warning(f"Failed page {page_num} accuracy analysis: {e}")
                        else:
                            logger.warning(f"No OCR result for page {page_num} of {file_path}")

                    except MemoryError:
                        logger.error(f"MemoryError processing page {page_num} of {file_path}")
                        continue
                    except Exception as page_error:
                        logger.error(f"Error processing page {page_num} of {file_path}: {page_error}")
                        continue
                
                # Free the images list after processing each chunk
                del images

                page_start = page_end + 1
            
            if processed_pages == 0:
                logger.warning(f"No pages successfully processed in PDF: {file_path}")
                return None
            
            # Combine all text and average confidence
            combined_text = "\n".join(all_text)
            avg_confidence = total_confidence / processed_pages
            
            logger.info(
                "Successfully processed %s/%s pages of PDF: %s",
                processed_pages,
                total_pages_converted,
                file_path
            )
            
            return (combined_text, avg_confidence, page_metrics_list)
            
        except Exception as e:
            logger.error(f"Error processing PDF {file_path}: {e}", exc_info=True)
            return None

    def _ocr_page_smart(self, page_bytes: bytes, page_num: int, file_path: str) -> Optional[Tuple[Tuple[str, float], bytes]]:
        """Apply smart OCR strategies to a single PDF page image.
        
        Preprocesses the page, runs PaddleOCR, and retries with
        alternative strategies if confidence is below threshold.
        """
        smart_config = getattr(self.config.ocr, 'smart_retries', {})
        target_conf = smart_config.get('min_confidence_threshold', 80.0)

        # Page-level strategies ordered by speed: fastest first, heavy last.
        # The fast-path exits as soon as we get good confidence + text coverage,
        # so parent files with clean pages typically only run 1-2 strategies.
        strategies = [
            ("Preprocess",  lambda b: self.preprocessor.preprocess(b),  "3"),
            ("RawFallback", lambda b: b, "3"),
            ("CLAHE+Block", lambda b: self.preprocessor.apply_clahe_only(b), "6"),
            ("Binarize",    lambda b: self.preprocessor.apply_binarization(b), "6"),
            ("Upscale2x",   lambda b: self.preprocessor.resize_image(b, 2.0), "3"),
            ("ColorBGRm",   lambda b: self.preprocessor.remove_color_background_aggressive(b), "6"),
        ]

        best_result = None
        best_conf = -1.0
        best_score = -1.0
        best_word_chars = 0
        best_prep_bytes = page_bytes
        original_psm = getattr(self.paddle, 'psm', '3')

        for name, prep_func, psm in strategies:
            tmp_path = None
            try:
                self.paddle.psm = psm
                prep_bytes = prep_func(page_bytes)
                if not prep_bytes:
                    prep_bytes = page_bytes

                with tempfile.NamedTemporaryFile(mode='wb', suffix='.png', delete=False) as tmp_file:
                    tmp_file.write(prep_bytes)
                    tmp_path = tmp_file.name

                result = self.paddle.extract_text(tmp_path)
                if result:
                    text, conf = result
                    if text.strip():
                        # Prefer candidates that balance confidence and text coverage.
                        # This avoids selecting over-cropped center-only text with high
                        # confidence when another strategy captures the full page.
                        word_chars = len(re.sub(r'\W', '', text, flags=re.UNICODE))
                        text_bonus = min(30.0, word_chars * 0.05)
                        score = float(conf) + text_bonus

                        if score > best_score or (score == best_score and word_chars > best_word_chars):
                            best_result = result
                            best_conf = conf
                            best_score = score
                            best_word_chars = word_chars
                            best_prep_bytes = prep_bytes

                        # Fast-path: once we have sufficiently confident and
                        # non-trivial text, stop trying heavier strategies.
                        if float(conf) >= float(target_conf) and word_chars >= 20:
                            return result, prep_bytes
            except Exception as e:
                logger.debug(f"PDF page {page_num} strategy '{name}' failed: {e}")
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.unlink(tmp_path)
                    except Exception:
                        pass

        self.paddle.psm = original_psm
        if best_result:
            return best_result, best_prep_bytes
        return None

    def _process_visual_snippets(
        self,
        smart_id: str,
        page_num: int,
        page_bytes: bytes,
        accuracy_loss_json_str: str,
        file_path: str = "",
    ) -> None:
        """Parse accuracy_loss_json, crop visual snippets (signatures, seals, logos),
        assign simple static reviewer roles, calculate CNN embedding feature vectors,
        run visual template matching, and insert pending reviews into SQLite.
        """
        if not accuracy_loss_json_str:
            return

        try:
            loss_data = json.loads(accuracy_loss_json_str)
            if not isinstance(loss_data, dict):
                return
            
            snippets = loss_data.get("snippets", []) or []

            raw_snippets: List[Dict[str, Any]] = list(snippets)

            # Optional strict visual policy (config-driven):
            # - allow only selected snippet types
            # - require minimum impact by type
            # - keep only top-N by impact per page/type
            preprocessing_cfg = dict(getattr(self.config.ocr, "preprocessing", {}) or {})

            # Optional static per-file overrides.
            # Example config:
            # visual_pdf_overrides:
            #   - match_substring: "PNC-GECC Fiscal Agency"
            #     visual_allowed_types: ["signature", "stamp", "logo"]
            #     signature_min_impact: 0.05
            overrides = preprocessing_cfg.get("visual_pdf_overrides") or []
            matched_override: Optional[Dict[str, Any]] = None
            normalized_path = str(file_path or "").replace("\\", "/").lower()
            if isinstance(overrides, list) and normalized_path:
                for item in overrides:
                    if not isinstance(item, dict):
                        continue
                    match_sub = str(item.get("match_substring", "") or "").strip().lower()
                    if match_sub and match_sub in normalized_path:
                        matched_override = item
                        for key in (
                            "visual_allowed_types",
                            "signature_min_impact",
                            "logo_min_impact",
                            "stamp_min_impact",
                            "text_anomaly_min_impact",
                            "max_per_page_per_type",
                        ):
                            if key in item:
                                preprocessing_cfg[key] = item[key]
                        break
            allowed_types = {
                str(t).strip().lower()
                for t in (preprocessing_cfg.get("visual_allowed_types") or [])
                if str(t).strip()
            }
            if allowed_types:
                snippets = [
                    s for s in snippets
                    if str((s or {}).get("type", "logo")).lower() in allowed_types
                ]

            min_impact_by_type = {
                "signature": float(preprocessing_cfg.get("signature_min_impact", 0.0) or 0.0),
                "logo": float(preprocessing_cfg.get("logo_min_impact", 0.0) or 0.0),
                "stamp": float(preprocessing_cfg.get("stamp_min_impact", 0.0) or 0.0),
                "noise": float(preprocessing_cfg.get("noise_min_impact", 0.0) or 0.0),
                "faded_text": float(preprocessing_cfg.get("faded_text_min_impact", 0.0) or 0.0),
                "handwritten": float(preprocessing_cfg.get("handwritten_min_impact", 0.0) or 0.0),
                "text_anomaly": float(preprocessing_cfg.get("text_anomaly_min_impact", 0.0) or 0.0),
            }

            impact_filtered: List[Dict[str, Any]] = []
            for s in snippets:
                s_type = str((s or {}).get("type", "logo")).lower()
                s_impact = float((s or {}).get("impact", 0.0) or 0.0)
                if s_impact >= min_impact_by_type.get(s_type, 0.0):
                    impact_filtered.append(s)
            snippets = impact_filtered

            max_per_type_cfg = preprocessing_cfg.get("max_per_page_per_type") or {}
            if isinstance(max_per_type_cfg, dict) and snippets:
                grouped: Dict[str, List[Dict[str, Any]]] = {}
                for s in snippets:
                    s_type = str((s or {}).get("type", "logo")).lower()
                    grouped.setdefault(s_type, []).append(s)

                topk_snippets: List[Dict[str, Any]] = []
                for s_type, items in grouped.items():
                    items_sorted = sorted(
                        items,
                        key=lambda it: float((it or {}).get("impact", 0.0) or 0.0),
                        reverse=True,
                    )
                    try:
                        limit = int(max_per_type_cfg.get(s_type, 0) or 0)
                    except Exception:
                        limit = 0
                    if limit > 0:
                        items_sorted = items_sorted[:limit]
                    topk_snippets.extend(items_sorted)
                snippets = topk_snippets

            # Safety fallback: if strict policy filtered everything, keep the
            # highest-impact candidate per allowed type from raw snippets.
            if not snippets and raw_snippets:
                fallback_source = raw_snippets
                if allowed_types:
                    fallback_source = [
                        s for s in raw_snippets
                        if str((s or {}).get("type", "logo")).lower() in allowed_types
                    ]
                grouped_fb: Dict[str, List[Dict[str, Any]]] = {}
                for s in fallback_source:
                    t = str((s or {}).get("type", "logo")).lower()
                    grouped_fb.setdefault(t, []).append(s)

                fallback_keep: List[Dict[str, Any]] = []
                for t, items in grouped_fb.items():
                    items_sorted = sorted(
                        items,
                        key=lambda it: float((it or {}).get("impact", 0.0) or 0.0),
                        reverse=True,
                    )
                    fallback_keep.append(items_sorted[0])
                snippets = fallback_keep

            from PIL import Image
            import io
            import uuid
            
            # Load the page image once for cropping
            img = Image.open(io.BytesIO(page_bytes)).convert("RGB")
            w, h = img.size

            # Store visual artifacts under <app_root>/data to avoid machine-specific paths.
            app_root = Path(self.config.paths.working_root).parent
            review_snippets_root = app_root / "data" / "review_snippets"
            approved_vectors_root = app_root / "data" / "visual_memory"

            # Check if there are any previously approved visual templates for this document
            # Approved templates are stored as .npy feature vector files in data/visual_memory/<smart_id>/
            approved_vectors_dir = approved_vectors_root / smart_id

            # Manual file-specific signature boxes (static override):
            # allows exact signature crops to be queued even if detector misses them.
            manual_added = 0
            if isinstance(matched_override, dict):
                manual_boxes = matched_override.get("manual_signature_bboxes") or []
                if isinstance(manual_boxes, list):
                    for mb in manual_boxes:
                        if not isinstance(mb, dict):
                            continue
                        try:
                            target_page = int(mb.get("page", 1) or 1)
                        except Exception:
                            target_page = 1
                        if target_page != int(page_num):
                            continue

                        bbox = None
                        bbox_norm = mb.get("bbox_norm")
                        bbox_abs = mb.get("bbox")

                        if isinstance(bbox_norm, list) and len(bbox_norm) == 4:
                            try:
                                nx1, ny1, nx2, ny2 = [float(v) for v in bbox_norm]
                                x1 = int(nx1 * w)
                                y1 = int(ny1 * h)
                                x2 = int(nx2 * w)
                                y2 = int(ny2 * h)
                                bbox = [x1, y1, x2, y2]
                            except Exception:
                                bbox = None
                        elif isinstance(bbox_abs, list) and len(bbox_abs) == 4:
                            try:
                                bbox = [int(v) for v in bbox_abs]
                            except Exception:
                                bbox = None

                        if not bbox:
                            continue

                        snippets.append({
                            "type": "signature",
                            "bbox": bbox,
                            "impact": float(mb.get("impact", 0.99) or 0.99),
                            "force_keep": True,
                        })
                        manual_added += 1

            if manual_added > 0:
                logger.info(
                    "Worker %s: Injected %s manual signature boxes for %s page %s",
                    self.worker_id,
                    manual_added,
                    smart_id,
                    page_num,
                )

            if not snippets:
                return
            
            for idx, snippet in enumerate(snippets, 1):
                snippet_type = snippet.get("type", "logo")
                bbox = snippet.get("bbox", [])
                impact = snippet.get("impact", 0.0)
                force_keep = bool(snippet.get("force_keep", False))
                if len(bbox) != 4 or impact <= 0.0:
                    continue

                # Hard suppression: only skip truly microscopic impacts (dust/single pixels).
                # Real cursive signatures on large pages can have low area ratios.
                if (not force_keep) and snippet_type == "signature" and float(impact) <= 0.03:
                    continue

                # Crop coordinates safety check (bounding box inside page area)
                x1, y1, x2, y2 = bbox
                x1 = max(0, min(int(x1), w - 1))
                y1 = max(0, min(int(y1), h - 1))
                x2 = max(x1 + 1, min(int(x2), w))
                y2 = max(y1 + 1, min(int(y2), h))

                box_w = max(1, x2 - x1)
                box_h = max(1, y2 - y1)
                width_ratio = box_w / max(1, w)
                height_ratio = box_h / max(1, h)
                aspect_ratio = box_w / max(1, box_h)

                # Enhanced artifact detection guardrail: Filter common false positives
                # (lines, dots, noise) that are incorrectly classified as signatures
                is_likely_artifact = False

                # Rule 1: Obvious horizontal/vertical lines (extreme aspect ratios)
                if snippet_type == "signature":
                    if height_ratio < 0.002 or width_ratio > 0.95 or aspect_ratio > 40.0:
                        is_likely_artifact = True  # Line artifact
                    # Rule 2: Tiny dots/specs (too small to be meaningful)
                    elif box_h < 4 or box_w < 4:
                        is_likely_artifact = True  # Noise/dust
                    # Rule 3: Very small box area
                    elif (box_w * box_h) < 16:
                        is_likely_artifact = True  # Too tiny

                if (not force_keep) and is_likely_artifact:
                    continue

                cropped = img.crop((x1, y1, x2, y2))

                # Skip near-blank signature regions before saving — a crop with
                # almost no ink is not a real signature, it's a detection artifact.
                if snippet_type == "signature" and not force_keep:
                    if self._is_sparse_noise_visual_snippet(cropped, snippet_type):
                        logger.debug(
                            "Worker %s: Skipping sparse/blank signature crop on page %s",
                            self.worker_id, page_num,
                        )
                        continue

                # Setup snippet directories
                snippet_dir = review_snippets_root / smart_id
                snippet_dir.mkdir(parents=True, exist_ok=True)
                snippet_path = snippet_dir / f"page_{page_num}_{snippet_type}_{idx}.png"
                
                # Save cropped image on disk
                cropped.save(str(snippet_path), format="PNG")

                # Suppress snippets whose content is clearly OCR-readable printed text.
                # Logos, stamps, faded_text, handwritten, and noise are ALWAYS kept
                # — these are accuracy-depreciating elements for human review.
                # Signatures are kept unless they're a misclassified printed-text block.
                # text_anomaly regions are suppressed when OCR can cleanly read them.
                _visual_type_always_keep = snippet_type in {"logo", "stamp", "faded_text", "handwritten", "noise"}
                if not force_keep and not _visual_type_always_keep and self._is_text_like_visual_snippet(str(snippet_path), snippet_type):
                    try:
                        snippet_path.unlink()
                    except Exception:
                        pass
                    logger.debug(
                        "Worker %s: Suppressed text-like snippet %s (type=%s) — already in OCR output",
                        self.worker_id, snippet_path.name, snippet_type,
                    )
                    continue

                # Generate a unique review ID
                review_id = f"{smart_id}_p{page_num}_{snippet_type}_{idx}"
                
                # Check for approved templates using Cosine Similarity matching
                is_auto_accepted = False
                matched_vector_path = None
                if self.visual_memory and approved_vectors_dir.exists():
                    try:
                        # Cosine similarity matching against previously approved snippets of this type
                        # If a match is found (similarity > 0.88), automatically approve it
                        is_match, matched_path = self.visual_memory.match_snippet(
                            candidate_image_path=str(snippet_path),
                            approved_vectors_dir=str(approved_vectors_dir),
                            threshold=0.88
                        )
                        if is_match:
                            is_auto_accepted = True
                            matched_vector_path = matched_path
                            logger.info(f"VisualMemoryEngine: Snippet {review_id} matches approved template! Auto-accepting.")
                    except Exception as e:
                        logger.error(f"VisualMemoryEngine: Error during template matching for {review_id}: {e}")

                if is_auto_accepted:
                    # Register and automatically accept the review
                    if force_keep and snippet_type == "signature":
                        snippet_type_refined = "signature"
                        reviewer_role = self._get_reviewer_role("signature")
                    else:
                        snippet_type_refined, reviewer_role = self.classify_snippet_deficit(
                            cropped_pil_img=cropped,
                            bbox=bbox,
                            page_dims=(w, h),
                            initial_type=snippet_type,
                            snippet_path=str(snippet_path)
                        )
                    create_snippet_review(
                        review_id=review_id,
                        smart_id=smart_id,
                        page_num=page_num,
                        snippet_type=snippet_type_refined,
                        snippet_path=str(snippet_path),
                        bounding_box=bbox,
                        accuracy_impact=impact,
                        reviewer_role=reviewer_role
                    )
                    update_snippet_review_status(review_id, status="accepted", feature_vector_path=matched_vector_path)
                else:
                    # Standard pending human-in-the-loop review
                    # Classify snippet with enhanced categorization
                    if force_keep and snippet_type == "signature":
                        snippet_type_refined = "signature"
                        reviewer_role = self._get_reviewer_role("signature")
                    else:
                        snippet_type_refined, reviewer_role = self.classify_snippet_deficit(
                            cropped_pil_img=cropped,
                            bbox=bbox,
                            page_dims=(w, h),
                            initial_type=snippet_type,
                            snippet_path=str(snippet_path)
                        )
                    create_snippet_review(
                        review_id=review_id,
                        smart_id=smart_id,
                        page_num=page_num,
                        snippet_type=snippet_type_refined,
                        snippet_path=str(snippet_path),
                        bounding_box=bbox,
                        accuracy_impact=impact,
                        reviewer_role=reviewer_role
                    )
                    update_snippet_review_status(review_id, status="pending")

            img.close()
        except Exception as exc:
            logger.error(f"Failed to process visual snippets for {smart_id}: {exc}")

    def classify_snippet_deficit(
        self,
        cropped_pil_img,
        bbox: List[int],
        page_dims: Tuple[int, int],
        initial_type: str,
        snippet_path: Optional[str] = None,
    ) -> Tuple[str, str]:
        """Classify visual snippet deficit type using ML models and heuristics."""
        roles = {
            "signature": "Contract Auditor",
            "stamp": "Compliance Officer",
            "logo": "Brand Integrity Reviewer",
            "handwritten": "Transcription Auditor",
            "faded_text": "Faded Text Specialist",
            "text_anomaly": "Text Specialist",
            "noise": "Quality Control",
            "unknown": "Document Reviewer",
        }
        def _get_role(category: str) -> str:
            return roles.get(category, roles.get("unknown", "Document Reviewer"))

        # Check if pre-trained SVM snippet classifier exists
        classifier_model_path = Path("models/snippet_classifier.pkl")
        if getattr(self, "_snippet_classifier", None) is None:
            if classifier_model_path.exists():
                try:
                    import pickle
                    with open(classifier_model_path, "rb") as f:
                        self._snippet_classifier = pickle.load(f)
                    logger.info("OCRWorker: Loaded trained SVM snippet classifier model")
                except Exception as exc:
                    logger.warning(f"OCRWorker: Failed to load SVM snippet classifier: {exc}")
                    self._snippet_classifier = None

        # Run SVM model prediction on the extracted feature vector
        if getattr(self, "_snippet_classifier", None) is not None and getattr(self, "visual_memory", None) and snippet_path:
            try:
                vector = self.visual_memory.extract_vector(snippet_path)
                if vector is not None:
                    probs = self._snippet_classifier.predict_proba(vector.reshape(1, -1))[0]
                    classes = self._snippet_classifier.classes_
                    best_idx = np.argmax(probs)
                    best_prob = probs[best_idx]
                    best_class = str(classes[best_idx])
                    
                    if best_prob >= 0.65:
                        logger.info(f"OCRWorker: Snippet {snippet_path} classified as '{best_class}' via SVM with probability {best_prob:.2f}")
                        return best_class, _get_role(best_class)
            except Exception as exc:
                logger.warning(f"OCRWorker: SVM prediction failed: {exc}")

        # Basic aspect ratio heuristics (fallback)
        if not bbox or len(bbox) != 4:
            return initial_type, _get_role(initial_type)

        x1, y1, x2, y2 = bbox
        w, h = page_dims
        box_w = max(1, x2 - x1)
        box_h = max(1, y2 - y1)
        width_ratio = box_w / max(1, w)
        height_ratio = box_h / max(1, h)
        aspect_ratio = box_w / max(1, box_h)

        if initial_type == "signature" and aspect_ratio < 2.0 and height_ratio > 0.02:
            return "text_anomaly", _get_role("text_anomaly")

        return initial_type, _get_role(initial_type)

    @staticmethod
    def _get_reviewer_role(snippet_type: str) -> str:
        """Map snippet types to reviewer roles (extended categories)"""
        if snippet_type == "signature":
            return "Contract Auditor"
        elif snippet_type == "stamp":
            return "Compliance Officer"
        elif snippet_type == "logo":
            return "Brand Integrity Reviewer"
        elif snippet_type == "handwritten":
            return "Transcription Auditor"
        elif snippet_type == "faded_text":
            return "Faded Text Specialist"
        elif snippet_type == "text_anomaly":
            return "Text Specialist"
        elif snippet_type == "noise":
            return "Quality Control"
        else:
            return "Document Reviewer"

    def _is_text_like_visual_snippet(self, snippet_path: str, snippet_type: str) -> bool:
        """Return True when a visual snippet should be suppressed from the review queue.

        Logos and stamps are never suppressed — their visual identity matters even
        if they contain readable text.  Signatures are suppressed only when OCR
        reads them back as a clean multi-word printed sentence (misclassification
        by the layout model).  Genuine cursive/handwritten signatures produce
        garbled, low-confidence OCR output and are always kept.
        text_anomaly regions are suppressed when OCR can read them cleanly at
        high confidence (meaning they are ordinary printed text, not anomalies).
        """
        # Logos and stamps always go to review regardless of text content
        if snippet_type in {"logo", "stamp"}:
            return False
        if snippet_type not in {"signature", "text_anomaly"}:
            return False

        try:
            ocr_result = self.paddle.extract_text(snippet_path)
            if not ocr_result:
                return False

            text, confidence = ocr_result
            if not text:
                return False

            cleaned = re.sub(r"\s+", " ", text.strip())
            alnum = re.sub(r"[^A-Za-z0-9]", "", cleaned)
            alpha_only = re.sub(r"[^A-Za-z]", "", cleaned)

            conf = float(confidence or 0.0)

            if snippet_type == "signature":
                # Real handwritten signatures: OCR returns garbled output at low confidence.
                # Misclassified printed-text blocks: multiple clean English words, high
                # confidence.  Suppress only the latter.
                words = [w for w in cleaned.split() if len(w) >= 2]
                alpha_ratio = (len(alpha_only) / max(1, len(alnum))) if alnum else 0.0
                is_printed_sentence = (
                    len(words) >= 2        # at least two words → sentence fragment
                    and len(alnum) >= 8    # enough characters to be meaningful
                    and conf >= 45.0       # OCR confident it is real text
                )
                # Single long clean word at very high confidence is also not a signature
                is_printed_word = (
                    len(alpha_only) >= 8
                    and alpha_ratio >= 0.85
                    and conf >= 65.0
                )
                if is_printed_sentence or is_printed_word:
                    logger.info(
                        "Worker %s: Suppressing printed-text signature snippet %s "
                        "(words=%d, conf=%.1f, text='%s')",
                        self.worker_id, Path(snippet_path).name,
                        len(words), conf, cleaned[:50],
                    )
                    return True
                return False

            # text_anomaly: suppress only when OCR reads it back with high
            # confidence as clean printed text. Use a high floor so faded,
            # handwritten, or barely-legible regions are always kept for review.
            has_meaningful_text = len(alnum) >= 6 and conf >= 55.0
            has_long_token = len(alnum) >= 10 and conf >= 40.0

            if has_meaningful_text or has_long_token:
                logger.info(
                    "Worker %s: Skipping visual review for text-like snippet %s (type=%s, conf=%.2f, text='%s')",
                    self.worker_id,
                    Path(snippet_path).name,
                    snippet_type,
                    conf,
                    alnum[:30],
                )
                return True

            return False
        except Exception as exc:
            logger.debug("Text-like snippet check failed for %s: %s", snippet_path, exc)
            return False

    def _is_sparse_noise_visual_snippet(self, cropped_img: Any, snippet_type: str) -> bool:
        """Return True if crop is mostly sparse blobs/dots and should not be reviewed as visual signature."""
        if snippet_type not in {"signature", "text_anomaly"}:
            return False

        try:
            arr = np.array(cropped_img.convert("L"))
            if arr.ndim != 2:
                return False

            h, w = arr.shape
            area = max(1, h * w)

            # Dark pixels as "ink" on light paper.
            ink_mask = arr < 200
            ink_px = int(np.count_nonzero(ink_mask))
            if ink_px == 0:
                return True

            ink_ratio = ink_px / area
            row_coverage = float(np.count_nonzero(np.any(ink_mask, axis=1)) / max(1, h))
            col_coverage = float(np.count_nonzero(np.any(ink_mask, axis=0)) / max(1, w))

            # Very sparse marks, pepper noise, or tiny disconnected blobs.
            if ink_ratio < 0.004:
                return True
            if ink_ratio < 0.010 and row_coverage < 0.35 and col_coverage < 0.35:
                return True
            if area > 1500 and ink_px < 60:
                return True

            return False
        except Exception as exc:
            logger.debug("Sparse-noise snippet check failed: %s", exc)
            return False

    def _is_printed_font_snippet(self, cropped_img: Any) -> bool:
        """Return True when the crop looks like a printed/digital font rather than handwriting.

        Printed text has highly uniform stroke widths (low coefficient of variation)
        and smooth, straight edges.  Cursive handwriting has wildly variable stroke
        widths and irregular contour shapes.

        Three independent signals — all must agree before we suppress:
          1. Stroke-width uniformity (distance-transform CV)
          2. Edge straightness (proportion of Canny edges that are near-horizontal/vertical)
          3. Contour regularity (convexity-defect ratio)
        """
        if cv2 is None:
            return False
        try:
            arr = np.array(cropped_img.convert("L"))
            if arr.ndim != 2 or arr.size == 0:
                return False

            h, w = arr.shape
            if h < 8 or w < 8:
                return False

            # --- Binarize -------------------------------------------------
            _, bw = cv2.threshold(arr, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)

            ink_px = int(np.count_nonzero(bw))
            if ink_px < 20:
                return False

            # ── Signal 1: stroke-width uniformity via distance transform ──
            # Distance transform on the ink mask gives local stroke half-width at each pixel.
            dist = cv2.distanceTransform(bw, cv2.DIST_L2, 5)
            stroke_vals = dist[bw > 0]
            if len(stroke_vals) < 20:
                return False
            sw_mean = float(np.mean(stroke_vals))
            sw_std = float(np.std(stroke_vals))
            sw_cv = sw_std / max(sw_mean, 0.001)  # coefficient of variation
            # Printed fonts: very uniform strokes → CV < 0.40
            # Handwriting: highly variable → CV > 0.55
            is_uniform_stroke = sw_cv < 0.40

            # ── Signal 2: edge straightness ────────────────────────────────
            edges = cv2.Canny(arr, 50, 150)
            edge_px = int(np.count_nonzero(edges))
            if edge_px > 10:
                # Sobel gradients to measure edge orientation
                gx = cv2.Sobel(arr, cv2.CV_64F, 1, 0, ksize=3)
                gy = cv2.Sobel(arr, cv2.CV_64F, 0, 1, ksize=3)
                angles = np.arctan2(np.abs(gy[edges > 0]), np.abs(gx[edges > 0])) * 180 / np.pi
                # Near-horizontal (0-20°) or near-vertical (70-90°) edges → typical of printed glyphs
                straight = float(np.sum((angles < 20) | (angles > 70)) / max(len(angles), 1))
            else:
                straight = 0.5
            is_straight_edges = straight > 0.55

            # ── Signal 3: contour regularity ───────────────────────────────
            contours, _ = cv2.findContours(bw, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            defect_ratios = []
            for cnt in contours:
                if cv2.contourArea(cnt) < 10:
                    continue
                hull = cv2.convexHull(cnt, returnPoints=False)
                if hull is None or len(hull) < 3 or len(cnt) < 4:
                    continue
                try:
                    defects = cv2.convexityDefects(cnt, hull)
                    if defects is None:
                        defect_ratios.append(0.0)
                        continue
                    # depth of defects relative to contour perimeter
                    perimeter = cv2.arcLength(cnt, True)
                    total_depth = float(np.sum(defects[:, 0, 3]) / 256.0)
                    defect_ratios.append(total_depth / max(perimeter, 1.0))
                except cv2.error:
                    continue

            if defect_ratios:
                avg_defect = float(np.mean(defect_ratios))
                # Printed glyphs: smooth convex hulls → low defect ratio
                # Handwriting: loops, curls → high defect ratio
                is_regular_contour = avg_defect < 0.08
            else:
                is_regular_contour = False

            # Suppress when at least 2 of 3 signals agree it's printed text.
            # Requiring all 3 is too strict for degraded scans where one signal
            # degrades (e.g. noisy edges break the straightness measure).
            signals_fired = sum([is_uniform_stroke, is_straight_edges, is_regular_contour])
            if signals_fired >= 2:
                return True

            # Additional fast-path: very high OCR confidence on the crop almost
            # certainly means it is machine-readable printed text
            try:
                tmp_path = None
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                    cropped_img.save(tmp.name, format="PNG")
                    tmp_path = tmp.name
                ocr_result = self.paddle.extract_text(tmp_path)
                if ocr_result:
                    _, conf = ocr_result
                    if float(conf or 0) >= 60.0:
                        return True
            except Exception:
                pass
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass

            return False

        except Exception as exc:
            logger.debug("Printed-font snippet check failed: %s", exc)
            return False

    def _aggregate_ocr_metrics(self, page_metrics: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Aggregate per-page OCR metrics into a single document-level metrics dict."""
        if not page_metrics:
            return self._empty_metrics_fn("ocr", self.accuracy_analyzer.tier if self.accuracy_analyzer else "tier1")

        n_pages = len(page_metrics)
        
        # Simple averages
        avg_accuracy = sum(m.get("extraction_accuracy") or 0.0 for m in page_metrics) / n_pages
        avg_text_area = sum(m.get("text_area_pct") or 0.0 for m in page_metrics) / n_pages
        avg_non_text_area = sum(m.get("non_text_area_pct") or 0.0 for m in page_metrics) / n_pages
        avg_gain = sum(m.get("preprocessing_gain_pct") or 0.0 for m in page_metrics) / n_pages
        
        # Average OCR Confidence if present
        avg_confidence = 100.0
        conf_values = [m.get("page_confidence") for m in page_metrics if m.get("page_confidence") is not None]
        if conf_values:
            avg_confidence = sum(conf_values) / len(conf_values)
            
        # Determine Verification Status based on new confidence bounds
        if avg_confidence >= 95.0:
            verification_status = "High Confidence"
        elif avg_confidence >= 80.0:
            verification_status = "Review Recommended"
        elif avg_confidence >= 60.0:
            verification_status = "Visual Verification Required"
        else:
            verification_status = "Human Review Required"
            
        # Sums
        sum_raw_char = sum(m.get("raw_char_count") or 0 for m in page_metrics)
        sum_proc_char = sum(m.get("processed_char_count") or 0 for m in page_metrics)
        
        # Average loss breakdown fields
        loss_fields = [
            "text_read_pct", "unreadable_text_pct", "logos_images_pct",
            "signatures_pct", "stamps_seals_pct", "noise_artifacts_pct",
            "whitespace_margins_pct"
        ]
        avg_loss = {}
        for field in loss_fields:
            sum_field = 0.0
            for m in page_metrics:
                loss_json = m.get("accuracy_loss_json", "")
                if loss_json:
                    try:
                        loss_dict = json.loads(loss_json)
                        sum_field += float(loss_dict.get(field, 0.0))
                    except Exception:
                        pass
            avg_loss[field] = round(sum_field / n_pages, 2)
            
        # Compile page breakdown
        clean_page_metrics = []
        for m in page_metrics:
            clean_page_metrics.append({
                "page": m.get("page", 1),
                "extraction_accuracy": m.get("extraction_accuracy"),
                "text_area_pct": m.get("text_area_pct"),
                "non_text_area_pct": m.get("non_text_area_pct"),
            })

        tier = page_metrics[0].get("accuracy_tier", "tier1")

        return {
            "pipeline_type": "ocr",
            "extraction_accuracy": round(avg_accuracy, 2),
            "text_area_pct": round(avg_text_area, 2),
            "non_text_area_pct": round(avg_non_text_area, 2),
            "raw_char_count": sum_raw_char,
            "processed_char_count": sum_proc_char,
            "preprocessing_gain_pct": round(avg_gain, 2),
            "accuracy_loss_json": json.dumps(avg_loss),
            "page_metrics_json": json.dumps(clean_page_metrics),
            "accuracy_tier": tier,
            "verification_status": verification_status,
            "overall_risk_score": round(100.0 - avg_confidence, 2),
            "has_signature": avg_loss.get("signatures_pct", 0.0) > 0,
            "has_stamp": avg_loss.get("stamps_seals_pct", 0.0) > 0,
            "has_logo": avg_loss.get("logos_images_pct", 0.0) > 0,
            "has_handwritten": avg_loss.get("noise_artifacts_pct", 0.0) > 0
        }
    
    def _get_file_hash(self, file_id: int) -> str:
        """Get file hash from database"""
        try:
            file_info = self.queue_manager.get_file_info(file_id)
            return file_info.get('file_hash', '')
        except Exception as e:
            logger.warning(f"Could not get file hash for file_id {file_id}: {e}")
            return ''
    
    def _persist_pending_update(self, file_id: int, doc_id: str, ocr_text: str, confidence: float) -> None:
        """Persist failed OCR update to Redis for crash recovery (Fix #2)."""
        try:
            import json
            if hasattr(self.queue_manager, 'client'):
                self.queue_manager.client.lpush(
                    "ds:ocr:pending_updates",
                    json.dumps({
                        'file_id': file_id,
                        'file_hash': doc_id,
                        'ocr_content': ocr_text[:50000],  # Cap to avoid Redis memory issue
                        'ocr_confidence': confidence,
                        'worker_id': self.worker_id,
                        'timestamp': time.time(),
                    })
                )
        except Exception as exc:
            logger.debug("Worker %s: Could not persist pending update to Redis: %s", self.worker_id, exc)

    def _flush_updates(self) -> None:
        """Flush pending OCR updates to OpenSearch"""
        if not self.pending_updates:
            return
        if not self.os_client:
            logger.debug("Worker %s: OpenSearch unavailable; keeping %s OCR updates queued", self.worker_id, len(self.pending_updates))
            return
        
        logger.info(f"Worker {self.worker_id}: Flushing {len(self.pending_updates)} OCR updates")
        
        success_count = 0
        remaining_updates: List[Dict[str, Any]] = []
        for item in self.pending_updates:
            try:
                # Update document in OpenSearch
                # Use file_hash as document ID
                doc_id = item['file_hash']
                
                if not doc_id:
                    logger.warning("Skipping update - no file hash available")
                    continue
                
                success = self.os_client.update_document_ocr(doc_id, item['update'].get('ocr_content', ''), item['update'].get('ocr_confidence', 0.0))
                
                if success:
                    success_count += 1
                else:
                    logger.debug(f"Document {doc_id} may not exist in index yet")
                    remaining_updates.append(item)
                    
            except Exception as e:
                error_msg = str(e)
                # Don't log as error if document simply doesn't exist yet
                if 'document_missing' in error_msg or 'NotFoundError' in error_msg:
                    logger.debug(f"Document not yet indexed, will retry later: {item.get('file_id')}")
                    remaining_updates.append(item)
                else:
                    logger.error(f"Error updating document: {e}")
                    remaining_updates.append(item)
        
        if success_count > 0:
            logger.info(f"Worker {self.worker_id}: Successfully updated {success_count}/{len(self.pending_updates)} documents")
        
        self.pending_updates = remaining_updates
    
    def _handle_failure(
        self,
        queue_id: int,
        file_id: int,
        file_path: str,
        error_message: str
    ) -> None:
        """Handle OCR failure"""
        self.files_failed += 1
        
        self.queue_manager.mark_file_failed(
            file_id=file_id,
            stage='ocr',
            error_type=ErrorType.OCR_ERROR,
            error_message=error_message,
            file_path=file_path
        )
        
        # Remove from OCR queue
        try:
            self.queue_manager.complete_ocr(queue_id, 0.0, 0, self.worker_id)
        except Exception as e:
            logger.debug(f"Could not complete OCR queue for failed file: {e}")
        try:
            file_info = self.queue_manager.get_file_info(file_id) or {}
            file_hash = file_info.get('file_hash', '')
            file_size = int(file_info.get('file_size', 0) or 0)
        except Exception:
            file_hash = ''
            file_size = 0
        self._emit_stage_audit(
            stage="ocr",
            status="failed",
            file_id=file_id,
            file_path=file_path,
            file_hash=file_hash,
            file_size=file_size,
            state_status="failed",
            state_stage="ocr",
            error_type=str(ErrorType.OCR_ERROR.value),
            error_message=error_message,
        )

    def _emit_stage_audit(
        self,
        *,
        stage: str,
        status: str,
        file_id: Any,
        file_path: str,
        file_hash: str = "",
        file_size: int = 0,
        state_status: Optional[str] = None,
        state_stage: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
        error_type: str = "",
        error_message: str = "",
        processed_on: Optional[str] = None,
        smart_id: Optional[str] = None,
    ) -> None:
        """Emit OCR stage event + file state upsert."""
        try:
            file_name = Path(file_path).name if file_path else ""
            file_key = derive_file_key(file_hash=file_hash, file_id=file_id, file_path=file_path)
            file_type = normalize_file_type("", file_name=file_name, file_path=file_path)
            if not processed_on:
                processed_on = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
            if not smart_id:
                smart_id = build_smart_id(file_key=file_key, when_iso=processed_on)

            record_event(
                AuditEvent(
                    event_time=processed_on,
                    file_key=file_key,
                    smart_id=smart_id,
                    file_name=file_name,
                    file_path=file_path,
                    stage=stage,
                    status=status,
                    worker_id=self.worker_id,
                    file_type=file_type,
                    error_type=error_type,
                    error_message=error_message,
                    payload_json=payload or {},
                )
            )

            if state_status is not None:
                upsert_file_state(
                    FileStateRow(
                        file_key=file_key,
                        smart_id=smart_id,
                        file_name=file_name,
                        current_status=state_status,
                        processed_on=processed_on,
                        file_type=file_type,
                        file_size=int(file_size or 0),
                        file_path=file_path,
                        updated_at=processed_on,
                        source_stage=state_stage or stage,
                        worker_id=self.worker_id,
                    )
                )
        except Exception as exc:
            logger.debug("Worker %s: OCR audit write failed: %s", self.worker_id, exc)
    
    def _log_progress(self) -> None:
        """Log progress statistics with clear formatting"""
        elapsed = time.time() - self.start_time
        rate = self.files_processed / elapsed if elapsed > 0 else 0
        avg_confidence = self.total_confidence / self.files_processed if self.files_processed > 0 else 0
        
        # Get pending count for ETA
        try:
            # Use proper method that works for both SQLite and Redis
            stats = self.queue_manager.get_queue_stats()
            pending = stats.get('ocr', {}).get('pending', 0)
        except Exception as e:
            logger.warning(f"Could not get pending count for ETA: {e}")
            pending = 0
        
        # Calculate ETA
        eta_str = ""
        if pending > 0 and rate > 0:
            eta_seconds = pending / rate
            if eta_seconds > 3600:
                eta_str = f" | ETA: {eta_seconds/3600:.1f}h"
            elif eta_seconds > 60:
                eta_str = f" | ETA: {eta_seconds/60:.0f}m"
            else:
                eta_str = f" | ETA: {eta_seconds:.0f}s"
        
        nlp_status = "✓NLP" if self.text_corrector else ""
        
        logger.info(
            f"[{self.worker_id}] "
            f"Done: {self.files_processed:,} | "
            f"Pending: {pending:,} | "
            f"Fail: {self.files_failed} | "
            f"Conf: {avg_confidence:.0f}% | "
            f"Rate: {rate:.1f}/s{eta_str} {nlp_status}"
        )
    
    def _log_final_stats(self) -> None:
        """Log final statistics with clear formatting"""
        elapsed = time.time() - self.start_time
        avg_rate = self.files_processed / elapsed if elapsed > 0 else 0
        avg_confidence = self.total_confidence / self.files_processed if self.files_processed > 0 else 0
        
        
        paddle_stats = self.paddle.get_stats()
        paddle_pages = paddle_stats.get('pages_processed', 0)
        
        nlp_status = "ENABLED" if self.text_corrector else "DISABLED"
        
        logger.info(
            f"\n{'='*60}\n"
            f"[{self.worker_id}] OCR Complete\n"
            f"{'='*60}\n"
            f"  Files Processed:    {self.files_processed:,}\n"
            f"  Files Failed:       {self.files_failed}\n"
            f"  Low Confidence:     {self.low_confidence_count}\n"
            f"  NLP Corrections:    {self.nlp_corrections_applied:,}\n"
            f"  Average Confidence: {avg_confidence:.1f}%\n"
            f"  Total Time:         {elapsed:.1f}s\n"
            f"  Average Rate:       {avg_rate:.1f} files/sec\n"
            f"  Paddle Pages:       {paddle_pages}\n"
            f"  NLP Status:         {nlp_status}\n"
            f"{'='*60}"
        )
    
    def stop(self) -> None:
        """Stop the worker gracefully"""
        logger.info(f"Worker {self.worker_id}: Stop requested")
        self.running = False
        
        # Remove heartbeat from Redis
        try:
            if hasattr(self.queue_manager, 'remove_worker_heartbeat'):
                self.queue_manager.remove_worker_heartbeat(self.worker_id)
        except Exception as e:
            logger.debug(f"Error removing heartbeat: {e}")
            
        logger.info(f"Worker {self.worker_id}: Shutdown complete")

    def _heartbeat_loop(self) -> None:
        """Send heartbeat periodically"""
        while self.running:
            try:
                self.queue_manager.update_worker_heartbeat(self.worker_id)
            except Exception:
                pass
            time.sleep(10)
        
    def get_stats(self) -> Dict[str, Any]:
        """Get current statistics"""
        elapsed = time.time() - self.start_time if self.start_time else 0
        avg_confidence = self.total_confidence / self.files_processed if self.files_processed > 0 else 0
        
        return {
            'worker_id': self.worker_id,
            'running': self.running,
            'files_processed': self.files_processed,
            'files_failed': self.files_failed,
            'low_confidence_count': self.low_confidence_count,
            'average_confidence': avg_confidence,
            'elapsed_seconds': elapsed,
            'rate_per_second': self.files_processed / elapsed if elapsed > 0 else 0
        }
