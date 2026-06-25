"""
Extraction Worker - Pulls from extraction queue and processes with Tika
Includes NLP text correction before indexing
"""

import gc
import time
import json
import threading
from typing import Dict, Any, Optional
from datetime import datetime
import zipfile
import shutil
from pathlib import Path
import hashlib
import email
from email import policy
try:
    import extract_msg
except ImportError:
    extract_msg = None

from core.logging_manager import get_logger
from core.config_manager import get_config
from core.queue_manager import get_queue_manager
from core.constants import SizeCategory, Priority, ErrorType
from core.reporting_manager import (
    AuditEvent,
    FileStateRow,
    build_smart_id,
    derive_file_key,
    normalize_file_type,
    record_event,
    upsert_file_state,
    update_accuracy_metrics,
)


from .tika_client import TikaClient
from .content_extractor import ContentExtractor

# Import OpenSearch client for express lane indexing

# Import NLP corrector for text enhancement
try:
    from nlp.text_corrector import get_text_corrector
    NLP_AVAILABLE = True
except ImportError:
    NLP_AVAILABLE = False

logger = get_logger("extraction.worker")


class ExtractionWorker:
    """Extraction worker - processes files with Tika and applies NLP corrections"""
    
    def __init__(self, worker_id: str, pool_type: str, size_category: SizeCategory, tika_port: int):
        self.worker_id = worker_id
        self.pool_type = pool_type
        self.size_category = size_category
        
        self.config = get_config()
        self.queue_manager = get_queue_manager()
        
        # Initialize Tika client
        tika_instance = None
        for instance in self.config.extraction.tika.instances:
            if instance.port == tika_port:
                tika_instance = instance
                break
        
        if not tika_instance:
            raise ValueError(f"No Tika instance configured for port {tika_port}")
        
        self.tika_client = TikaClient(tika_instance.host, tika_instance.port)
        self.content_extractor = ContentExtractor()
        
        # Initialize NLP text corrector only if enabled in config
        self.text_corrector = None
        if self.config.nlp.enabled and NLP_AVAILABLE:
            try:
                self.text_corrector = get_text_corrector()
                logger.info(f"[{worker_id}] NLP text corrector ENABLED and initialized")
            except Exception as e:
                logger.warning(f"[{worker_id}] NLP enabled but failed to initialize: {e}")
        else:
            if not self.config.nlp.enabled:
                # NLP disabled for extraction workers is normal - they process already-clean text
                logger.debug(f"[{worker_id}] NLP disabled (config.nlp.enabled=false)")
            elif not NLP_AVAILABLE:
                logger.warning(f"[{worker_id}] NLP module not available in environment")
        
        # OpenSearch client is not needed in ExtractionWorker - indexing happens in IndexingWorker
        self.os_client = None

        # Initialize accuracy analyzer
        self.accuracy_analyzer = None
        try:
            from extraction.accuracy_analyzer import AccuracyAnalyzer
            self.accuracy_analyzer = AccuracyAnalyzer(
                enable_yolo=True, enable_doctr=True
            )
            logger.info(f"[{worker_id}] Accuracy analyzer ENABLED (tier: {self.accuracy_analyzer.tier})")
        except Exception as exc:
            logger.warning(f"[{worker_id}] Accuracy analyzer unavailable: {exc}")

        
        # Worker configuration - run continuously (no restart threshold)
        # Memory management via periodic garbage collection instead
        self.gc_interval = 100  # Run GC every 100 files
        
        # Statistics
        self.files_processed = 0
        self.nlp_corrections_applied = 0
        self.files_failed = 0
        self.files_with_ocr = 0
        self.total_processing_time = 0
        self.start_time = None
        self.running = False
    
    def run(self) -> None:
        """Main worker loop - runs continuously with memory management"""
        self.running = True
        self.start_time = time.time()

        # Start heartbeat thread
        heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        heartbeat_thread.start()
        
        logger.info(f"Worker {self.worker_id} ({self.pool_type}): Starting for {self.size_category.value} files")
        
        consecutive_empty = 0
        max_empty_polls = 10
        
        try:
            while self.running:
                # Periodic garbage collection for memory management
                if self.files_processed > 0 and self.files_processed % self.gc_interval == 0:
                    gc.collect()
                    # Monitor memory usage to detect leaks
                    try:
                        import psutil
                        process = psutil.Process()
                        mem_mb = process.memory_info().rss / 1024 / 1024
                        logger.debug(f"Worker {self.worker_id}: Memory usage: {mem_mb:.1f} MB after {self.files_processed} files")
                        
                        # Self-terminate if this worker exceeds 4 GB RSS
                        if mem_mb > 4096:
                            logger.warning(
                                f"Worker {self.worker_id}: Memory usage {mem_mb:.0f} MB exceeds 4 GB limit. "
                                f"Self-terminating for restart with clean memory."
                            )
                            break
                        
                        # Also check system-wide memory – pause if > 80%
                        sys_mem = psutil.virtual_memory()
                        if sys_mem.percent > 85:
                            logger.warning(
                                f"Worker {self.worker_id}: System memory at {sys_mem.percent:.0f}%. "
                                f"Pausing 10s to let other processes finish."
                            )
                            time.sleep(10)
                            gc.collect()
                    except Exception as e:
                        logger.warning(f"Could not get memory info: {e}")
                
                # Claim work from queue
                work_items = self.queue_manager.claim_extraction_work(
                    size_category=self.size_category,
                    worker_id=self.worker_id,
                    batch_size=1
                )
                
                if not work_items:
                    consecutive_empty += 1
                    
                    if consecutive_empty >= max_empty_polls:
                        logger.debug(f"Worker {self.worker_id}: No work available, idling...")
                        time.sleep(2)
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
                    if self.files_processed % 100 == 0:
                        self._log_progress()
            self._log_final_stats()
            
        except (KeyboardInterrupt, SystemExit):
            logger.info(f"Worker {self.worker_id}: Shutdown signal received, exiting gracefully")
        except Exception as e:
            logger.error(f"Worker {self.worker_id}: Fatal error: {e}", exc_info=True)
        finally:
            self.running = False
            self.tika_client.close()
    
    def _process_file(self, work_item: Dict[str, Any]) -> None:
        """Process a single file"""
        queue_id = work_item['id']
        file_id = work_item['file_id']
        file_path = work_item['file_path']
        file_size = int(work_item.get('file_size', 0) or 0)
        file_hash = self._get_file_hash(file_id)
        
        start_time = time.time()
        self._emit_stage_audit(
            stage="extraction",
            status="processing",
            file_id=file_id,
            file_path=file_path,
            file_hash=file_hash,
            file_size=file_size,
            state_status="processing",
            state_stage="extraction",
        )
        
        try:
            # Extract with Tika
            tika_response = self.tika_client.extract(file_path)
            
            if not tika_response or not tika_response.get('success'):
                # Extraction failed
                self._handle_failure(
                    queue_id=queue_id,
                    file_id=file_id,
                    file_path=file_path,
                    error_type=ErrorType.EXTRACTION_FAILED,
                    error_message="Tika extraction failed or returned no data"
                )
                return
            
            # Process extraction results
            extracted_data = self.content_extractor.process_tika_response(
                tika_response=tika_response,
                file_path=file_path,
                file_hash=file_hash
            )
            
            if not extracted_data:
                self._handle_failure(
                    queue_id=queue_id,
                    file_id=file_id,
                    file_path=file_path,
                    error_type=ErrorType.EXTRACTION_FAILED,
                    error_message="Content extraction returned no data"
                )
                return
            
            # Store parent metadata in Redis for search dashboard lineage
            r = getattr(self.queue_manager, 'client', None)
            if r and file_hash:
                try:
                    r.hset('docsearch:parent_info', file_hash, json.dumps({
                        'file_path': file_path,
                        'file_name': Path(file_path).name
                    }))
                except Exception as redis_err:
                    logger.warning("Failed to save parent metadata in Redis: %s", redis_err)

            # Deep scan for embedded content (Deep Extraction Strategy)
            self._extract_embedded_content(file_path, file_hash)
            
            # Mark extraction complete
            processing_time_ms = int((time.time() - start_time) * 1000)
            self.queue_manager.complete_extraction(queue_id, processing_time_ms, self.worker_id)
            
            # Build document for indexing
            document = self._build_document(extracted_data, tika_response, file_size)
            document_json = json.dumps(document)
            state_fields = self._extract_state_fields(document)

            # --- Accuracy analysis ---
            if self.accuracy_analyzer:
                try:
                    file_path_str = extracted_data.get('file_path', '')
                    main_text = document.get('main_content', '')
                    accuracy_metrics = self.accuracy_analyzer.analyze(
                        file_path_str, main_text, tika_response
                    )
                    # Save accuracy metrics to database
                    file_hash_for_key = extracted_data.get('file_hash', '')
                    acc_file_key = derive_file_key(
                        file_hash=file_hash_for_key,
                        file_id=file_id,
                        file_path=file_path_str
                    )
                    update_accuracy_metrics(acc_file_key, accuracy_metrics)
                    logger.debug(
                        f"Accuracy: {file_path_str} -> "
                        f"{accuracy_metrics.get('extraction_accuracy', 0):.1f}% "
                        f"({accuracy_metrics.get('pipeline_type', 'unknown')})"
                    )
                except Exception as acc_exc:
                    logger.debug(f"Accuracy analysis skipped for {file_path}: {acc_exc}")

            self._emit_stage_audit(
                stage="extraction",
                status="completed",
                file_id=file_id,
                file_path=file_path,
                file_hash=file_hash,
                file_size=file_size,
                state_status="processing",
                state_stage="extraction",
                state_fields=state_fields,
                payload={"processing_time_ms": processing_time_ms},
            )
            
            # Route to express lane or batch indexing
            priority = work_item.get('priority', 5)
            
            # Content Size Limit Check (50MB) - Prevent Redis/OpenSearch overload
            doc_size = len(document_json.encode('utf-8'))
            if doc_size > 65 * 1024 * 1024:
                logger.warning(f"Document too large for indexing ({doc_size/1024/1024:.2f}MB). Skipping: {file_path}")
                self._handle_failure(
                    queue_id=queue_id,
                    file_id=file_id, 
                    file_path=file_path,
                    error_type=ErrorType.EXTRACTION_FAILED,
                    error_message=f"Document exceeds size limit (50MB): {doc_size} bytes"
                )
                return

            # For throughput and fewer OpenSearch refreshes, always use batch queue
            self.queue_manager.add_to_indexing_queue(
                file_id=file_id,
                document_json=document_json
            )
            self._emit_stage_audit(
                stage="indexing",
                status="pending",
                file_id=file_id,
                file_path=file_path,
                file_hash=file_hash,
                file_size=file_size,
                state_status="pending",
                state_stage="indexing",
                state_fields=state_fields,
            )
            
            # Queue OCR in parallel (doesn't block indexing)
            # Parent files get the original priority; embedded children are deprioritized
            # so parent OCR completes first.
            if extracted_data.get('needs_ocr', False):
                _is_embedded = '/data/embedded/' in file_path.replace('\\', '/')
                ocr_priority = Priority.LOW if _is_embedded else Priority(work_item.get('priority', 5))
                self.queue_manager.add_to_ocr_queue(
                    file_id=file_id,
                    file_path=file_path,
                    priority=ocr_priority
                )
                self.files_with_ocr += 1
                self._emit_stage_audit(
                    stage="ocr",
                    status="pending",
                    file_id=file_id,
                    file_path=file_path,
                    file_hash=file_hash,
                    file_size=file_size,
                    state_status="pending",
                    state_stage="ocr",
                    state_fields=state_fields,
                )
            
            self.files_processed += 1
            self.total_processing_time += (time.time() - start_time)
            
        except Exception as e:
            logger.error(f"Worker {self.worker_id}: Error processing {file_path}: {e}", exc_info=True)
            self._handle_failure(
                queue_id=queue_id,
                file_id=file_id,
                file_path=file_path,
                error_type=ErrorType.EXTRACTION_FAILED,
                error_message=str(e)
            )
    
    def _get_file_hash(self, file_id: int) -> str:
        """Get file hash from database"""
        try:
            file_info = self.queue_manager.get_file_info(file_id)
            return file_info.get('file_hash', '')
        except Exception as e:
            logger.warning(f"Could not get file hash for file_id {file_id}: {e}")
            return ''
    
    def _build_document(self, extracted_data: Dict[str, Any], tika_response: Dict[str, Any], file_size: int = 0) -> Dict[str, Any]:
        """Build document structure for indexing with NLP text corrections"""
        main_content = extracted_data['main_content']
        nlp_corrections = 0
        
        # Apply NLP text corrections if available
        if self.text_corrector and main_content:
            try:
                corrected_content, corrections = self.text_corrector.correct(main_content)
                if corrections > 0:
                    main_content = corrected_content
                    nlp_corrections = corrections
                    self.nlp_corrections_applied += corrections
                    logger.debug(f"Applied {corrections} NLP corrections to {extracted_data['file_path']}")
            except Exception as e:
                logger.warning(f"NLP correction failed for {extracted_data['file_path']}: {e}")

        metadata = extracted_data.get('metadata', {}) or {}
        file_hash = extracted_data.get('file_hash', '')
        file_path = extracted_data.get('file_path', '')
        file_name = Path(file_path).name if file_path else ''
        file_type = normalize_file_type(metadata.get('file_type', ''), file_name=file_name, file_path=file_path)

        dynamic_subtags = metadata.get('dynamic_subtags', metadata.get('tags', []))
        if isinstance(dynamic_subtags, str):
            dynamic_subtags = [
                tag.strip()
                for tag in dynamic_subtags.replace(";", ",").replace("|", ",").split(",")
                if tag.strip()
            ]
        elif not isinstance(dynamic_subtags, list):
            dynamic_subtags = []

        key_names = metadata.get('key_names', metadata.get('persons', []))
        if isinstance(key_names, str):
            key_names = [name.strip() for name in key_names.replace(";", ",").split(",") if name.strip()]
        elif not isinstance(key_names, list):
            key_names = []

        important_dates = metadata.get('important_dates', [])
        if isinstance(important_dates, str):
            important_dates = [d.strip() for d in important_dates.replace(";", ",").split(",") if d.strip()]
        elif not isinstance(important_dates, list):
            important_dates = []

        location_mentioned = metadata.get('location_mentioned', [])
        if isinstance(location_mentioned, str):
            location_mentioned = [loc.strip() for loc in location_mentioned.replace(";", ",").split(",") if loc.strip()]
        elif not isinstance(location_mentioned, list):
            location_mentioned = []

        department = str(metadata.get('department', '') or metadata.get('domain', '') or '')
        smart_id = str(
            metadata.get('smart_id')
            or build_smart_id(
                file_key=derive_file_key(file_hash=file_hash, file_path=file_path),
                department=department,
                when_iso=extracted_data.get('extracted_at', ''),
            )
        )
        
        document = {
            'file_path': extracted_data['file_path'],
            'file_hash': extracted_data['file_hash'],
            'file_size': file_size,  # Include file size for search and display
            'main_content': main_content,  # Use corrected content
            'content_hash': extracted_data['content_hash'],
            'metadata': extracted_data['metadata'],
            'embedded_files': extracted_data['embedded_files'],
            'embedded_count': extracted_data['embedded_count'],
            'needs_ocr': extracted_data['needs_ocr'],
            'extraction_time_ms': tika_response.get('response_time_ms', 0),
            'nlp_corrections': nlp_corrections,
            'extracted_at': datetime.now().isoformat(),
            'smart_id': smart_id,
            'category': str(metadata.get('category', metadata.get('document_type', '')) or ''),
            'department': department,
            'purpose': str(metadata.get('purpose', metadata.get('intent', '')) or ''),
            'dynamic_subtags': dynamic_subtags,
            'key_names': key_names,
            'amount_found': str(metadata.get('amount_found', '') or ''),
            'important_dates': important_dates,
            'location_mentioned': location_mentioned,
            'confidentiality': str(metadata.get('confidentiality', '') or ''),
            'file_type': file_type,
        }
        
        return document
    
    def _handle_failure(
        self,
        queue_id: int,
        file_id: int,
        file_path: str,
        error_type: ErrorType,
        error_message: str
    ) -> None:
        """Handle extraction failure"""
        self.files_failed += 1
        
        self.queue_manager.mark_file_failed(
            file_id=file_id,
            stage='extraction',
            error_type=error_type,
            error_message=error_message,
            file_path=file_path
        )
        
        # Remove from extraction queue
        try:
            self.queue_manager.complete_extraction(queue_id, 0, self.worker_id)
        except Exception as e:
            logger.debug(f"Could not complete extraction queue for failed file: {e}")

        try:
            file_info = self.queue_manager.get_file_info(file_id) or {}
            file_hash = file_info.get('file_hash', '')
            file_size = int(file_info.get('file_size', 0) or 0)
        except Exception:
            file_hash = ''
            file_size = 0

        self._emit_stage_audit(
            stage="extraction",
            status="failed",
            file_id=file_id,
            file_path=file_path,
            file_hash=file_hash,
            file_size=file_size,
            state_status="failed",
            state_stage="extraction",
            error_type=str(error_type.value if hasattr(error_type, "value") else error_type),
            error_message=error_message,
        )

    def _extract_state_fields(self, document: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize state export fields from document payload."""
        return {
            "smart_id": str(document.get("smart_id", "") or ""),
            "category": str(document.get("category", "") or ""),
            "department": str(document.get("department", "") or ""),
            "purpose": str(document.get("purpose", "") or ""),
            "key_names": document.get("key_names", []),
            "amount_found": str(document.get("amount_found", "") or ""),
            "important_dates": document.get("important_dates", []),
            "location_mentioned": document.get("location_mentioned", []),
            "confidentiality": str(document.get("confidentiality", "") or ""),
            "tag_confidence": float(document.get("tag_confidence", 0.0) or 0.0),
        }

    def _emit_stage_audit(
        self,
        *,
        stage: str,
        status: str,
        file_id: int,
        file_path: str,
        file_hash: str = "",
        file_size: int = 0,
        state_status: Optional[str] = None,
        state_stage: Optional[str] = None,
        state_fields: Optional[Dict[str, Any]] = None,
        payload: Optional[Dict[str, Any]] = None,
        error_type: str = "",
        error_message: str = "",
    ) -> None:
        """Emit audit event + one-row file state upsert."""
        try:
            file_name = Path(file_path).name if file_path else ""
            file_key = derive_file_key(file_hash=file_hash, file_id=file_id, file_path=file_path)
            file_type = normalize_file_type("", file_name=file_name, file_path=file_path)
            processed_on = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
            state_fields = state_fields or {}
            smart_id = state_fields.get("smart_id") or build_smart_id(
                file_key=file_key,
                department=str(state_fields.get("department", "") or ""),
                when_iso=processed_on,
            )

            record_event(
                AuditEvent(
                    event_time=processed_on,
                    file_key=file_key,
                    smart_id=str(smart_id),
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
                        smart_id=str(smart_id),
                        file_name=file_name,
                        category=str(state_fields.get("category", "") or ""),
                        department=str(state_fields.get("department", "") or ""),
                        purpose=str(state_fields.get("purpose", "") or ""),
                        key_names=state_fields.get("key_names", []),
                        amount_found=str(state_fields.get("amount_found", "") or ""),
                        important_dates=state_fields.get("important_dates", []),
                        location_mentioned=state_fields.get("location_mentioned", []),
                        confidentiality=str(state_fields.get("confidentiality", "") or ""),
                        current_status=state_status,
                        processed_on=processed_on,
                        file_type=file_type,
                        file_size=int(file_size or 0),
                        file_path=file_path,
                        updated_at=processed_on,
                        tag_confidence=float(state_fields.get("tag_confidence", 0.0) or 0.0),
                        source_stage=state_stage or stage,
                        worker_id=self.worker_id,
                    )
                )
        except Exception as exc:
            logger.debug("Worker %s: extraction audit write failed: %s", self.worker_id, exc)
    
    def _log_progress(self) -> None:
        """Log progress statistics with clear formatting"""
        elapsed = time.time() - self.start_time
        # Require minimum elapsed time for meaningful rate (avoid misleading rates like 10000 files/sec)
        rate = self.files_processed / elapsed if elapsed >= 1.0 else 0
        avg_time = self.total_processing_time / self.files_processed if self.files_processed > 0 else 0
        
        # Get queue pending count for ETA calculation
        try:
            stats = self.queue_manager.get_queue_stats()
            extraction_stats = stats.get('extraction', {})
            pending = extraction_stats.get('pending', 0)
        except Exception as e:
            logger.warning(f"Could not get queue size for ETA: {e}")
            pending = 0
        
        # Calculate ETA if we have pending files and a rate
        eta_str = ""
        if pending > 0 and rate > 0:
            eta_seconds = pending / rate
            if eta_seconds > 3600:
                eta_str = f" | ETA: {eta_seconds/3600:.1f}h"
            elif eta_seconds > 60:
                eta_str = f" | ETA: {eta_seconds/60:.0f}m"
            else:
                eta_str = f" | ETA: {eta_seconds:.0f}s"
        
        logger.info(
            f"[{self.worker_id}] "
            f"Done: {self.files_processed:,} | "
            f"Fail: {self.files_failed} | "
            f"OCR: {self.files_with_ocr} | "
            f"Rate: {rate:.1f}/s | "
            f"Pending: {pending:,}{eta_str}"
        )
    
    def _log_final_stats(self) -> None:
        """Log final statistics with clear formatting"""
        elapsed = time.time() - self.start_time
        avg_rate = self.files_processed / elapsed if elapsed > 0 else 0
        avg_time = self.total_processing_time / self.files_processed if self.files_processed > 0 else 0
        
        tika_stats = self.tika_client.get_stats()
        
        nlp_status = "ENABLED" if self.text_corrector else "DISABLED"
        
        logger.info(
            f"\n{'='*60}\n"
            f"[{self.worker_id}] Extraction Complete ({self.pool_type})\n"
            f"{'='*60}\n"
            f"  Files Processed:    {self.files_processed:,}\n"
            f"  Files Failed:       {self.files_failed}\n"
            f"  Files for OCR:      {self.files_with_ocr}\n"
            f"  Total Time:         {elapsed:.1f}s\n"
            f"  Average Rate:       {avg_rate:.1f} files/sec\n"
            f"  Avg Processing:     {avg_time:.2f}s/file\n"
            f"  Tika Success:       {tika_stats['success_rate']:.1%}\n"
            f"  NLP Status:         {nlp_status}\n"
            f"{'='*60}"
        )
    
    def stop(self) -> None:
        """Stop the worker gracefully"""
        logger.info(f"Worker {self.worker_id}: Stop requested")
        self.running = False
    
    def get_stats(self) -> Dict[str, Any]:
        """Get current statistics"""
        elapsed = time.time() - self.start_time if self.start_time else 0
        
        return {
            'worker_id': self.worker_id,
            'pool_type': self.pool_type,
            'size_category': self.size_category.value,
            'running': self.running,
            'files_processed': self.files_processed,
            'files_failed': self.files_failed,
            'files_with_ocr': self.files_with_ocr,
            'elapsed_seconds': elapsed,
            'rate_per_second': self.files_processed / elapsed if elapsed > 0 else 0
        }

    def _extract_embedded_content(self, file_path: str, file_hash: str) -> int:
        """Deep extraction of embedded files from ZIP, EML, and MSG documents"""
        try:
            suffix = Path(file_path).suffix.lower()
            if suffix == '.zip' or zipfile.is_zipfile(file_path):
                return self._extract_zip_embedded(file_path, file_hash)
            elif suffix == '.eml':
                return self._extract_eml_embedded(file_path, file_hash)
            elif suffix == '.msg':
                return self._extract_msg_embedded(file_path, file_hash)
        except Exception as e:
            logger.debug("Deep extraction skipped for %s: %s", file_path, e)
        return 0

    def _extract_zip_embedded(self, file_path: str, file_hash: str) -> int:
        """Deep extraction of embedded files from ZIP archive"""
        extracted_count = 0
        SEARCHABLE_EXTENSIONS = {
            # Images for OCR
            '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.webp', '.svg',
            # Documents for Tika
            '.pdf', '.txt', '.html', '.htm', '.xml', '.csv', '.rtf',
            # Internal office structures
            '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx',
            # Archives
            '.zip', '.tar', '.gz'
        }

        # Office formats (docx/xlsx/pptx) are ZIP containers whose internal
        # XML files are structural markup, not user-facing documents.
        # Skip deep extraction entirely for these to avoid flooding the
        # pipeline with thousands of internal XML parts.
        OFFICE_ZIP_EXTENSIONS = {'.docx', '.xlsx', '.pptx', '.odt', '.ods', '.odp'}
        if Path(file_path).suffix.lower() in OFFICE_ZIP_EXTENSIONS:
            return 0

        try:
            config_paths = self.config.paths
            embedded_root = Path(config_paths.working_root) / 'data' / 'embedded'
            embedded_dir = embedded_root / file_hash
            embedded_dir.mkdir(parents=True, exist_ok=True)
            
            # ZIP BOMB PROTECTION
            MAX_TOTAL_SIZE = 1 * 1024 * 1024 * 1024  # 1 GB limit per parent file
            total_extracted_bytes = 0

            with zipfile.ZipFile(file_path, 'r') as zf:
                for name in zf.namelist():
                    if name.endswith('/'): continue
                    
                    suffix = Path(name).suffix.lower()
                    if suffix not in SEARCHABLE_EXTENSIONS:
                        continue
                    
                    try:
                        info = zf.getinfo(name)
                        if info.file_size > 500 * 1024 * 1024:
                             logger.warning("Skipping embedded file %s: Too large (%s bytes)", name, info.file_size)
                             continue
                        
                        if total_extracted_bytes + info.file_size > MAX_TOTAL_SIZE:
                            logger.warning("Zip extraction stopped for %s: Total limit %s bytes exceeded", Path(file_path).name, MAX_TOTAL_SIZE)
                            break
                    except Exception:
                         pass

                    filename = Path(name).name
                    if not filename: continue
                    
                    # Flatten path for safety
                    safe_name = name.replace('/', '_').replace('\\', '_')
                    target_path = embedded_dir / safe_name
                    
                    # Copy to disk
                    with zf.open(name) as source, open(target_path, 'wb') as target:
                        shutil.copyfileobj(source, target)
                    
                    total_extracted_bytes += info.file_size

                    # Inject into pipeline
                    self._inject_file(target_path, file_hash)
                    extracted_count += 1
                        
            if extracted_count > 0:
                logger.info("Worker %s: Deep-extracted %s child files from ZIP %s", self.worker_id, extracted_count, Path(file_path).name)
            return extracted_count
            
        except Exception as e:
            logger.warning("Failed to extract zip embedded files from %s: %s", file_path, e)
            return 0

    def _extract_eml_embedded(self, file_path: str, file_hash: str) -> int:
        """Extract attachments from .eml files"""
        extracted_count = 0
        try:
            with open(file_path, 'rb') as f:
                msg = email.message_from_binary_file(f, policy=policy.default)
            
            config_paths = self.config.paths
            embedded_root = Path(config_paths.working_root) / 'data' / 'embedded'
            embedded_dir = embedded_root / file_hash
            embedded_dir.mkdir(parents=True, exist_ok=True)
            
            for part in msg.walk():
                if part.is_multipart():
                    continue
                
                filename = part.get_filename()
                if not filename:
                    cid = part.get('Content-ID')
                    if cid:
                        filename = cid.strip('<>')
                    else:
                        continue
                
                # Flatten filename for safety
                safe_name = filename.replace('/', '_').replace('\\', '_')
                target_path = embedded_dir / safe_name
                
                payload = part.get_payload(decode=True)
                if payload:
                    with open(target_path, 'wb') as out_f:
                        out_f.write(payload)
                    
                    self._inject_file(target_path, file_hash)
                    extracted_count += 1
            
            if extracted_count > 0:
                logger.info("Worker %s: Deep-extracted %s attachments from EML %s", self.worker_id, extracted_count, Path(file_path).name)
            return extracted_count
        except Exception as e:
            logger.warning("Failed to extract eml attachments from %s: %s", file_path, e)
            return 0

    def _extract_msg_embedded(self, file_path: str, file_hash: str) -> int:
        """Extract attachments from .msg files"""
        if extract_msg is None:
            logger.warning("extract_msg library not installed; MSG attachments extraction skipped for %s", file_path)
            return 0
        
        extracted_count = 0
        try:
            msg = extract_msg.Message(file_path)
            
            config_paths = self.config.paths
            embedded_root = Path(config_paths.working_root) / 'data' / 'embedded'
            embedded_dir = embedded_root / file_hash
            embedded_dir.mkdir(parents=True, exist_ok=True)
            
            for attachment in msg.attachments:
                filename = attachment.longFilename or attachment.filename or f"attachment_{attachment.index}"
                if not filename:
                    continue
                
                # Flatten filename for safety
                safe_name = filename.replace('/', '_').replace('\\', '_')
                target_path = embedded_dir / safe_name
                
                if attachment.data:
                    with open(target_path, 'wb') as out_f:
                        out_f.write(attachment.data)
                    
                    self._inject_file(target_path, file_hash)
                    extracted_count += 1
            
            msg.close()
            if extracted_count > 0:
                logger.info("Worker %s: Deep-extracted %s attachments from MSG %s", self.worker_id, extracted_count, Path(file_path).name)
            return extracted_count
        except Exception as e:
            logger.warning("Failed to extract msg attachments from %s: %s", file_path, e)
            return 0

    def _inject_file(self, file_path: Path, parent_hash: str) -> None:
        """Inject extracted file into pipeline as a new discovered file."""
        try:
            # Calculate hash
            sha256 = hashlib.sha256()
            with open(file_path, 'rb') as f:
                while True:
                    data = f.read(65536)
                    if not data: break
                    sha256.update(data)
            file_hash = sha256.hexdigest()
            
            # File info
            stats = file_path.stat()
            file_size = stats.st_size
            last_modified = stats.st_mtime
            created = stats.st_ctime
            
            # Determine size category
            cat = SizeCategory.TINY
            if file_size > 50 * 1024 * 1024: cat = SizeCategory.LARGE
            elif file_size > 10 * 1024 * 1024: cat = SizeCategory.MEDIUM
            
            # Add to discovery queue as a new file (registers in Redis)
            file_id = self.queue_manager.add_discovered_file(
                file_path=str(file_path),
                file_name=file_path.name,
                file_size=file_size,
                file_extension=file_path.suffix.lower().lstrip('.'),
                file_hash=file_hash,
                last_modified=last_modified,
                created=created,
                size_category=cat,
                priority=Priority.NORMAL
            )
            
            # Skip if duplicate or failed
            if file_id is None:
                return

            # -----------------------------------------------------------------
            # Store parent-child relationship in Redis so the indexing stage
            # can tag the OpenSearch document with its parent hash.
            # Key: docsearch:parent_map:<child_hash>  →  <parent_hash>
            # -----------------------------------------------------------------
            try:
                r = getattr(self.queue_manager, 'client', None)
                if r:
                    r.hset('docsearch:parent_map', file_hash, parent_hash)
            except Exception:
                pass  # non-critical; best-effort

            # CRITICAL: Also add to extraction queue so it actually gets processed!
            self.queue_manager.add_to_extraction_queue(
                file_id=file_id,
                file_path=str(file_path),
                file_size=file_size,
                size_category=cat,
                priority=Priority.NORMAL
            )
            
        except Exception as e:
            logger.info(f"Worker {self.worker_id}: Injection failed for {file_path}: {e}")
    def _heartbeat_loop(self) -> None:
        """Send heartbeat periodically"""
        while self.running:
            try:
                self.queue_manager.update_worker_heartbeat(self.worker_id)
            except Exception:
                pass
            time.sleep(10)
