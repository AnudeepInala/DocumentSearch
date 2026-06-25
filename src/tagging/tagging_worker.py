"""
Tagging Worker - Async semantic tagging with guaranteed core-field coverage.
"""

from __future__ import annotations

import gc
import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from opensearchpy.exceptions import NotFoundError

from core.config_manager import get_config
from core.constants import ErrorType, QueueStatus
from core.logging_manager import get_logger
from core.queue_manager import get_queue_manager
from core.reporting_manager import (
    AuditEvent,
    FileStateRow,
    build_smart_id,
    derive_file_key,
    normalize_file_type,
    record_event,
    upsert_file_state,
)
from indexing.opensearch_client import OpenSearchClient

from .tagging_engine import TaggingEngine
from .tagging_models import TaggingRequest

logger = get_logger("tagging.worker")


class TaggingWorker:
    """Consumes tagging queue and updates OpenSearch + audit state."""

    def __init__(self, worker_id: str):
        self.worker_id = worker_id
        self.config = get_config()
        self.queue_manager = get_queue_manager()
        self.engine = TaggingEngine()
        self.max_retries = int(getattr(self.config.tagging, "max_retries", 3) or 3)
        self.batch_size = int(getattr(self.config.tagging, "batch_size", 8) or 8)
        self.running = False
        self.start_time = None

        self.files_processed = 0
        self.files_failed = 0
        self.files_retried = 0

        try:
            self.os_client = OpenSearchClient()
            startup_timeout = int(getattr(self.config.indexing.opensearch, "startup_timeout_seconds", 120) or 120)
            if not self.os_client.wait_for_availability(timeout_seconds=startup_timeout):
                logger.warning("Worker %s: OpenSearch unavailable at startup; tagging updates may fail", self.worker_id)
        except Exception as exc:
            logger.warning("Worker %s: Could not initialize OpenSearch client: %s", self.worker_id, exc)
            self.os_client = None

    def run(self) -> None:
        self.running = True
        self.start_time = time.time()
        heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        heartbeat_thread.start()

        logger.info("Worker %s: Tagging worker started", self.worker_id)
        empty_polls = 0
        items_since_gc = 0

        try:
            while self.running:
                work_items = self.queue_manager.claim_tagging_work(
                    worker_id=self.worker_id,
                    batch_size=self.batch_size,
                )

                if not work_items:
                    empty_polls += 1
                    time.sleep(2 if empty_polls > 5 else 0.5)
                    # GC during idle to free memory
                    if empty_polls % 10 == 0:
                        gc.collect()
                    continue

                empty_polls = 0
                for item in work_items:
                    if not self.running:
                        break
                    self._process_item(item)
                    items_since_gc += 1
                
                # Periodic garbage collection to prevent memory bloat
                if items_since_gc >= 50:
                    gc.collect()
                    items_since_gc = 0
        except (KeyboardInterrupt, SystemExit):
            logger.info("Worker %s: Shutdown signal received, exiting gracefully", self.worker_id)
        except Exception as exc:
            logger.error("Worker %s: Fatal error: %s", self.worker_id, exc, exc_info=True)
        finally:
            self.running = False
            logger.info(
                "Worker %s: stopped processed=%s failed=%s retried=%s",
                self.worker_id,
                self.files_processed,
                self.files_failed,
                self.files_retried,
            )

    def stop(self) -> None:
        self.running = False

    def _process_item(self, item: Dict[str, Any]) -> None:
        queue_id = int(item.get("id", 0) or 0)
        file_id = int(item.get("file_id", 0) or 0)
        file_path = str(item.get("file_path", "") or "")
        file_hash = str(item.get("file_hash", "") or "")
        retry_count = int(item.get("retry_count", 0) or 0)
        start_ts = time.time()

        file_name = Path(file_path).name if file_path else ""
        file_key = derive_file_key(file_hash=file_hash, file_id=file_id, file_path=file_path)
        self._emit_stage_audit(
            stage="tagging",
            status="processing",
            file_key=file_key,
            file_id=file_id,
            file_name=file_name,
            file_path=file_path,
            file_hash=file_hash,
            state_status="processing",
            state_stage="tagging",
        )

        try:
            source_doc, doc_id = self._load_document(item)
            req = TaggingRequest(
                file_id=file_id,
                file_path=file_path or str(source_doc.get("file_path", "") or ""),
                file_name=file_name or str(source_doc.get("file_name", "") or ""),
                file_hash=file_hash or str(source_doc.get("file_hash", "") or ""),
                doc_id=doc_id,
                file_type=str(source_doc.get("file_type", "") or ""),
                mime_type=str(source_doc.get("mime_type", "") or ""),
                main_content=str(source_doc.get("main_content", "") or ""),
                ocr_content=str(source_doc.get("ocr_content", "") or ""),
                embedded_content=str(source_doc.get("embedded_content", "") or ""),
                metadata=source_doc.get("metadata", {}) if isinstance(source_doc.get("metadata"), dict) else {},
            )

            result = self.engine.tag(req)
            update_fields = result.to_document_update()
            existing_smart_id = str(source_doc.get("smart_id", "") or "")
            if existing_smart_id:
                update_fields["smart_id"] = existing_smart_id
            source_file_size = int(source_doc.get("file_size", 0) or 0)

            if self.os_client and doc_id:
                self.os_client.update_document(doc_id=doc_id, updates=update_fields)

            duration_ms = int((time.time() - start_ts) * 1000)
            self.queue_manager.complete_tagging(
                queue_id=queue_id,
                processing_time_ms=duration_ms,
                worker_id=self.worker_id,
                status=QueueStatus.COMPLETED.value,
            )

            self._emit_stage_audit(
                stage="tagging",
                status="completed",
                file_key=file_key,
                file_id=file_id,
                file_name=file_name,
                file_path=file_path,
                file_hash=file_hash,
                file_size=source_file_size,
                state_status="completed",
                state_stage="tagging",
                state_fields=update_fields,
                payload={"doc_id": doc_id, "processing_time_ms": duration_ms},
            )
            self.files_processed += 1
        except Exception as exc:
            if retry_count < self.max_retries:
                self.queue_manager.requeue_tagging(queue_id=queue_id, reason=str(exc))
                self._emit_stage_audit(
                    stage="tagging",
                    status="retried",
                    file_key=file_key,
                    file_id=file_id,
                    file_name=file_name,
                    file_path=file_path,
                    file_hash=file_hash,
                    file_size=0,
                    state_status="pending",
                    state_stage="tagging",
                    error_message=str(exc),
                )
                self.files_retried += 1
                return

            try:
                self.queue_manager.complete_tagging(
                    queue_id=queue_id,
                    processing_time_ms=int((time.time() - start_ts) * 1000),
                    worker_id=self.worker_id,
                    status=QueueStatus.FAILED.value,
                )
            except Exception:
                pass

            try:
                self.queue_manager.mark_file_failed(
                    file_id=file_id,
                    file_path=file_path,
                    stage="tagging",
                    error_type=ErrorType.TAGGING_ERROR,
                    error_message=str(exc),
                )
            except Exception:
                pass

            self._emit_stage_audit(
                stage="tagging",
                status="failed",
                file_key=file_key,
                file_id=file_id,
                file_name=file_name,
                file_path=file_path,
                file_hash=file_hash,
                file_size=0,
                state_status="failed",
                state_stage="tagging",
                error_type=str(ErrorType.TAGGING_ERROR.value),
                error_message=str(exc),
            )
            self.files_failed += 1
            logger.error("Worker %s: Tagging failed for file_id=%s: %s", self.worker_id, file_id, exc)

    _TAGGING_SOURCE_FIELDS = [
        "file_name", "file_path", "file_hash", "file_type", "mime_type",
        "main_content", "ocr_content", "embedded_content", "metadata",
        "smart_id", "file_size",
    ]

    def _load_document(self, item: Dict[str, Any]) -> tuple[Dict[str, Any], str]:
        """Load source document from OpenSearch; returns source and doc_id."""
        file_hash = str(item.get("file_hash", "") or "")
        file_id = int(item.get("file_id", 0) or 0)
        doc_id = str(item.get("doc_id", "") or file_hash or f"file-{file_id}")
        if not self.os_client:
            # T9: Don't silently return empty — the tagging result would be meaningless
            raise RuntimeError("OpenSearch client unavailable; cannot load document for tagging")

        candidates = [doc_id]
        if file_hash and file_hash not in candidates:
            candidates.append(file_hash)
        if file_id:
            fid = f"file-{file_id}"
            if fid not in candidates:
                candidates.append(fid)

        for candidate in candidates:
            try:
                response = self.os_client.client.get(
                    index=self.os_client.index_name,
                    id=candidate,
                    _source=self._TAGGING_SOURCE_FIELDS,
                )
                source = response.get("_source", {}) or {}
                # T9: Verify we actually have content to tag
                has_content = any(
                    source.get(f)
                    for f in ("main_content", "ocr_content", "embedded_content")
                )
                if not has_content:
                    # For files with no content (binary, corrupt, unsupported format),
                    # we still want to tag based on filename and metadata.
                    # Log at debug level to avoid log spam.
                    logger.debug(
                        "Worker %s: Document %s has no content fields; will tag from metadata only",
                        self.worker_id, candidate,
                    )
                return source, candidate
            except NotFoundError:
                continue
            except Exception:
                continue
        raise RuntimeError(f"Document not found in OpenSearch for any candidate ID: {candidates}")

    def _emit_stage_audit(
        self,
        *,
        stage: str,
        status: str,
        file_key: str,
        file_id: int,
        file_name: str,
        file_path: str,
        file_hash: str,
        file_size: int = 0,
        state_status: Optional[str] = None,
        state_stage: Optional[str] = None,
        state_fields: Optional[Dict[str, Any]] = None,
        payload: Optional[Dict[str, Any]] = None,
        error_type: str = "",
        error_message: str = "",
    ) -> None:
        try:
            processed_on = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
            state_fields = state_fields or {}
            file_type = normalize_file_type(str(state_fields.get("file_type", "") or ""), file_name=file_name, file_path=file_path)
            smart_id = str(
                state_fields.get("smart_id")
                or build_smart_id(
                    file_key=file_key,
                    department=str(state_fields.get("department", "") or ""),
                    when_iso=processed_on,
                )
            )

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
                confidence_json = state_fields.get("tag_confidence_by_field", {})
                if not isinstance(confidence_json, str):
                    try:
                        confidence_json = json.dumps(confidence_json, ensure_ascii=False)
                    except Exception:
                        confidence_json = "{}"

                extended_metadata_json = state_fields.get("extended_metadata", {})
                if not isinstance(extended_metadata_json, str):
                    try:
                        extended_metadata_json = json.dumps(extended_metadata_json, ensure_ascii=False)
                    except Exception:
                        extended_metadata_json = "{}"

                original_labels = state_fields.get("original_labels", {})
                if not isinstance(original_labels, str):
                    try:
                        original_labels = json.dumps(original_labels, ensure_ascii=False)
                    except Exception:
                        original_labels = "{}"

                match_modes = state_fields.get("match_mode", {})
                if not isinstance(match_modes, str):
                    try:
                        match_modes = json.dumps(match_modes, ensure_ascii=False)
                    except Exception:
                        match_modes = "{}"

                original_score = 0.0
                orig_scores = state_fields.get("original_scores", {})
                if orig_scores:
                    if isinstance(orig_scores, dict):
                        original_score = float(orig_scores.get("category", sum(orig_scores.values()) / max(len(orig_scores), 1)))
                    else:
                        original_score = float(orig_scores)

                upsert_file_state(
                    FileStateRow(
                        file_key=file_key,
                        smart_id=smart_id,
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
                        tag_confidence=float(state_fields.get("tag_confidence_overall", state_fields.get("tag_confidence", 0.0)) or 0.0),
                        source_stage=state_stage or stage,
                        worker_id=self.worker_id,
                        tagging_status=str(state_fields.get("tagging_status", "") or ""),
                        review_required=bool(state_fields.get("review_required", False)),
                        tagger_version=str(state_fields.get("tagger_version", "") or ""),
                        taxonomy_version=str(state_fields.get("taxonomy_version", "") or ""),
                        confidence_json=str(confidence_json or ""),
                        extended_metadata_json=str(extended_metadata_json or ""),
                        constraint_source=str(state_fields.get("constraint_source", "") or ""),
                        forced_flag=bool(state_fields.get("forced_flag", False)),
                        original_label=str(original_labels or ""),
                        original_score=float(original_score),
                        match_mode=str(match_modes or ""),
                        constraint_version=str(state_fields.get("constraint_version", "") or ""),
                        # 12 Taxonomy Dimensions
                        metadata_level_code=str(state_fields.get("metadata_level_code", "File") or "File"),
                        record_class_name=str(state_fields.get("record_class_name", "Undefined") or "Undefined"),
                        record_category_name_functional=str(state_fields.get("record_category_name_functional", "") or ""),
                        record_category_name_transactional=str(state_fields.get("record_category_name_transactional", "") or ""),
                        record_type_code=str(state_fields.get("record_type_code", "") or ""),
                        business_unit_name=str(state_fields.get("business_unit_name", "") or ""),
                        sub_business_unit_name=str(state_fields.get("sub_business_unit_name", "") or ""),
                        iso_country_code=str(state_fields.get("iso_country_code", "") or ""),
                        record_format_name=str(state_fields.get("record_format_name", "Electronic") or "Electronic"),
                        original_record_location_type_name=str(state_fields.get("original_record_location_type_name", "Shared Drive") or "Shared Drive"),
                        data_classification_name=str(state_fields.get("data_classification_name", "GE Internal") or "GE Internal"),
                        divestiture_deal_name=str(state_fields.get("divestiture_deal_name", "") or ""),
                        dynamic_subtags=state_fields.get("dynamic_subtags", []),
                    )
                )
        except Exception as exc:
            logger.warning("Worker %s: tagging audit write failed: %s", self.worker_id, exc)

    def _heartbeat_loop(self) -> None:
        while self.running:
            try:
                self.queue_manager.update_worker_heartbeat(self.worker_id)
            except Exception:
                pass
            time.sleep(10)
