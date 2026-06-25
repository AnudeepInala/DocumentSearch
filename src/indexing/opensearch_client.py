"""
OpenSearch Client - Bulk indexing with adaptive batching and circuit breaker
"""

import time
from typing import List, Dict, Any, Optional
from opensearchpy import OpenSearch, helpers
from opensearchpy.exceptions import ConnectionError, TransportError, NotFoundError, ConflictError

from core.logging_manager import get_logger
from core.config_manager import get_config



logger = get_logger("indexing.opensearch")


class OpenSearchClient:
    """OpenSearch client with bulk indexing and adaptive batching"""
    
    def __init__(self):
        self.config = get_config()
        self.os_config = self.config.indexing.opensearch
        
        # Create client
        self.client = self._create_client()
        
        # Index configuration
        self.index_name = self.os_config.index_name
        self.mapping = self.config.indexing.mapping
        
        # Batch configuration
        self.current_batch_size = self.os_config.initial_batch_size
        self.min_batch_size = self.os_config.min_batch_size
        self.max_batch_size = self.os_config.max_batch_size
        self.batch_adjustment_step = self.os_config.batch_adjustment_step
        self.target_batch_time = self.os_config.target_batch_time_seconds
        self.fast_threshold = self.os_config.fast_batch_threshold_seconds
        self.slow_threshold = self.os_config.slow_batch_threshold_seconds
        
        # Statistics
        self.documents_indexed = 0
        self.batches_sent = 0
        self.total_batch_time = 0
        self.errors = 0
        
        # Circuit breaker state
        self.consecutive_failures = 0
        self.max_consecutive_failures = 5
        self.circuit_open = False
        self.circuit_retry_time = None
    
    def _create_client(self) -> OpenSearch:
        """Create OpenSearch client"""
        # Parse hosts
        hosts = []
        for host_str in self.os_config.hosts:
            if '://' in host_str:
                # Full URL provided
                hosts.append(host_str)
            else:
                # Just host:port
                hosts.append({'host': host_str.split(':')[0], 'port': int(host_str.split(':')[1]) if ':' in host_str else 9200})
        
        # Authentication
        auth = None
        if self.os_config.username and self.os_config.password:
            auth = (self.os_config.username, self.os_config.password)
        
        client = OpenSearch(
            hosts=hosts,
            http_auth=auth,
            use_ssl=self.os_config.use_ssl,
            verify_certs=self.os_config.verify_certs,
            ssl_show_warn=False,
            timeout=self.os_config.timeout_seconds,
            max_retries=self.os_config.max_retries,
            retry_on_timeout=True
        )
        
        return client
    
    def wait_for_availability(self, timeout_seconds: int = 120) -> bool:
        """Wait until OpenSearch responds and cluster health is at least yellow"""
        deadline = time.time() + max(timeout_seconds, 1)
        attempt = 0
        last_error: Optional[Exception] = None

        while time.time() < deadline:
            attempt += 1
            try:
                if not self.client.ping():
                    raise ConnectionError("ping returned False")
                self.client.cluster.health(
                    wait_for_status='yellow',
                    request_timeout=max(5, self.os_config.timeout_seconds // 2)
                )
                if attempt > 1:
                    logger.info("OpenSearch became available after %s attempts", attempt)
                return True
            except Exception as exc:  # pylint: disable=broad-except
                last_error = exc
                sleep_seconds = min(5 * attempt, 15)
                logger.warning(
                    "OpenSearch unavailable (attempt %s): %s; retrying in %ss",
                    attempt,
                    exc,
                    sleep_seconds
                )
                time.sleep(sleep_seconds)

        logger.error(
            "OpenSearch not available after %s attempts (last error: %s)",
            attempt,
            last_error
        )
        return False

    def ensure_index(self, retries: int = 3) -> bool:
        """Create index if it doesn't exist"""
        attempts = max(1, retries)
        for attempt in range(1, attempts + 1):
            try:
                if self.client.indices.exists(index=self.index_name):
                    logger.info(f"Index {self.index_name} already exists")
                    self._ensure_additional_fields()
                    return True

                # Create index with enhanced mapping for high search accuracy
                index_body = {
                    'settings': {
                        'number_of_shards': self.mapping.get('shards', 5),
                        'number_of_replicas': self.mapping.get('replicas', 1),
                        'refresh_interval': self.os_config.bulk_refresh_interval,
                        # Custom analyzers for improved search accuracy
                        'analysis': {
                            'char_filter': {
                                'comma_stripper': {
                                    'type': 'pattern_replace',
                                    'pattern': r'(?<=\d),(?=\d)',
                                    'replacement': ''
                                }
                            },
                            'filter': {
                                # Edge n-gram for partial word matching (typo tolerance)
                                'edge_ngram_filter': {
                                    'type': 'edge_ngram',
                                    'min_gram': 3,
                                    'max_gram': 15
                                },
                                # Synonym filter for common business terms
                                'business_synonyms': {
                                    'type': 'synonym',
                                    'synonyms': [
                                        'contract, agreement, deal',
                                        'invoice, bill, receipt',
                                        'revenue, income, earnings',
                                        'expense, cost, expenditure',
                                        'employee, staff, worker, personnel',
                                        'client, customer, account',
                                        'terminate, cancel, end, discontinue',
                                        'budget, forecast, allocation',
                                        'memo, memorandum, note',
                                        'report, analysis, summary',
                                        'meeting, conference, discussion'
                                    ]
                                },
                                # Word delimiter for compound words and special chars
                                'word_delimiter_filter': {
                                    'type': 'word_delimiter_graph',
                                    'preserve_original': True,
                                    'split_on_numerics': False,
                                    'generate_word_parts': True,
                                    'generate_number_parts': True,
                                    'catenate_words': True
                                }
                            },
                            'analyzer': {
                                # Enhanced English analyzer with synonyms
                                'english_enhanced': {
                                    'type': 'custom',
                                    'char_filter': ['comma_stripper'],
                                    'tokenizer': 'standard',
                                    'filter': [
                                        'lowercase',
                                        'apostrophe',
                                        # Removed 'stop' filter to allow searching for exact phrases containing stop words
                                        'business_synonyms',
                                        'word_delimiter_filter',
                                        'porter_stem'
                                    ]
                                },
                                # OCR-specific analyzer — matches english_enhanced for consistent search
                                'ocr_analyzer': {
                                    'type': 'custom',
                                    'char_filter': ['comma_stripper'],
                                    'tokenizer': 'standard',
                                    'filter': [
                                        'lowercase',
                                        'apostrophe',
                                        'business_synonyms',
                                        'word_delimiter_filter',
                                        'porter_stem'
                                    ]
                                },
                                # Autocomplete analyzer with edge n-grams
                                'autocomplete': {
                                    'type': 'custom',
                                    'char_filter': ['comma_stripper'],
                                    'tokenizer': 'standard',
                                    'filter': [
                                        'lowercase',
                                        'edge_ngram_filter'
                                    ]
                                },
                                # Search analyzer (no edge n-gram, for queries)
                                'autocomplete_search': {
                                    'type': 'custom',
                                    'char_filter': ['comma_stripper'],
                                    'tokenizer': 'standard',
                                    'filter': ['lowercase']
                                }
                            }
                        }
                    },
                    'mappings': {
                        'properties': {
                            'file_path': {'type': 'keyword'},
                            'file_name': {
                                'type': 'text',
                                'analyzer': 'autocomplete',
                                'search_analyzer': 'autocomplete_search',
                                'fields': {
                                    'keyword': {'type': 'keyword'},
                                    'english': {'type': 'text', 'analyzer': 'english'}
                                }
                            },
                            'file_hash': {'type': 'keyword'},
                            'content_hash': {'type': 'keyword'},
                            'main_content': {
                                'type': 'text',
                                'analyzer': 'english_enhanced',
                                'fields': {
                                    'standard': {'type': 'text', 'analyzer': 'standard'},
                                    # FIXED: Increased from 256 to 8192 for long Excel cells
                                    'keyword': {'type': 'keyword', 'ignore_above': 8192}
                                }
                            },
                            'embedded_content': {
                                'type': 'text',
                                'analyzer': 'english_enhanced',
                                'fields': {
                                    # FIXED: Increased from 256 to 8192 for long content
                                    'keyword': {'type': 'keyword', 'ignore_above': 8192}
                                }
                            },
                            'ocr_content': {
                                'type': 'text',
                                'analyzer': 'ocr_analyzer',
                                'fields': {
                                    'standard': {'type': 'text', 'analyzer': 'standard'},
                                    'autocomplete': {'type': 'text', 'analyzer': 'autocomplete', 'search_analyzer': 'autocomplete_search'},
                                    # FIXED: Increased from 256 to 8192 for long OCR text
                                    'keyword': {'type': 'keyword', 'ignore_above': 8192}
                                }
                            },
                            # FIXED: Changed from 'object' to allow flexible metadata
                            'metadata': {'type': 'object', 'enabled': True},
                            # FIXED: Changed from 'nested' to 'object' to prevent query errors
                            # Nested fields require special query syntax and cause "nested items" errors
                            'embedded_files': {'type': 'object', 'enabled': True},
                            'embedded_count': {'type': 'integer'},
                            'needs_ocr': {'type': 'boolean'},
                            'ocr_completed': {'type': 'boolean'},
                            'ocr_confidence': {'type': 'float'},
                            'is_duplicate': {'type': 'boolean'},
                            'duplicate_of': {'type': 'keyword'},
                            'is_embedded': {'type': 'boolean'},
                            'parent_file': {'type': 'keyword'},
                            'parent_path': {'type': 'keyword'},
                            'parent_name': {'type': 'keyword'},
                            'smart_id': {
                                'type': 'keyword',
                                'fields': {
                                    'text': {'type': 'text', 'analyzer': 'standard'}
                                }
                            },
                            'category': {
                                'type': 'keyword',
                                'fields': {
                                    'text': {'type': 'text', 'analyzer': 'standard'}
                                }
                            },
                            'department': {
                                'type': 'keyword',
                                'fields': {
                                    'text': {'type': 'text', 'analyzer': 'standard'}
                                }
                            },
                            'purpose': {
                                'type': 'keyword',
                                'fields': {
                                    'text': {'type': 'text', 'analyzer': 'standard'}
                                }
                            },
                            'dynamic_subtags': {'type': 'keyword'},
                            'key_names': {'type': 'keyword'},
                            'amount_found': {'type': 'keyword'},
                            'important_dates': {'type': 'keyword'},
                            'location_mentioned': {
                                'type': 'keyword',
                                'fields': {
                                    'text': {'type': 'text', 'analyzer': 'standard'}
                                }
                            },
                            'confidentiality': {'type': 'keyword'},
                            'tagging_status': {'type': 'keyword'},
                            'review_required': {'type': 'boolean'},
                            'tagger_version': {'type': 'keyword'},
                            'taxonomy_version': {'type': 'keyword'},
                            'tag_confidence_overall': {'type': 'float'},
                            'tag_confidence': {'type': 'float'},
                            'tag_confidence_by_field': {'type': 'object', 'enabled': True},
                            'extended_metadata': {'type': 'object', 'enabled': True},
                            'file_type': {'type': 'keyword'},
                            'indexed_at': {'type': 'date'},
                            'extraction_time_ms': {'type': 'integer'},
                            'file_size': {'type': 'long'},
                            'mime_type': {'type': 'keyword'},
                        }
                    }
                }

                self.client.indices.create(index=self.index_name, body=index_body)
                logger.info(f"Created index {self.index_name} with enhanced analyzers")
                return True

            except Exception as exc:  # pylint: disable=broad-except
                if "already_exists" in str(exc).lower():
                    logger.info(f"Index {self.index_name} already exists (concurrent creation)")
                    return True

                if attempt < attempts:
                    sleep_seconds = min(2 ** attempt, 15)
                    logger.warning(
                        "Error creating index (attempt %s/%s): %s; retrying in %ss",
                        attempt,
                        attempts,
                        exc,
                        sleep_seconds
                    )
                    time.sleep(sleep_seconds)
                    continue

                logger.error(f"Error creating index: {exc}")
                return False

        return False

    def _ensure_additional_fields(self) -> None:
        """Best-effort mapping updates for Smart ID and layered tag fields."""
        try:
            self.client.indices.put_mapping(
                index=self.index_name,
                body={
                    "properties": {
                        "smart_id": {
                            "type": "keyword",
                            "fields": {"text": {"type": "text", "analyzer": "standard"}},
                        },
                        "category": {
                            "type": "keyword",
                            "fields": {"text": {"type": "text", "analyzer": "standard"}},
                        },
                        "department": {
                            "type": "keyword",
                            "fields": {"text": {"type": "text", "analyzer": "standard"}},
                        },
                        "purpose": {
                            "type": "keyword",
                            "fields": {"text": {"type": "text", "analyzer": "standard"}},
                        },
                        "dynamic_subtags": {"type": "keyword"},
                        "key_names": {"type": "keyword"},
                        "amount_found": {"type": "keyword"},
                        "important_dates": {"type": "keyword"},
                        "location_mentioned": {
                            "type": "keyword",
                            "fields": {"text": {"type": "text", "analyzer": "standard"}},
                        },
                        "confidentiality": {"type": "keyword"},
                        "tagging_status": {"type": "keyword"},
                        "review_required": {"type": "boolean"},
                        "tagger_version": {"type": "keyword"},
                        "taxonomy_version": {"type": "keyword"},
                        "tag_confidence_overall": {"type": "float"},
                        "tag_confidence": {"type": "float"},
                        "tag_confidence_by_field": {"type": "object", "enabled": True},
                        "extended_metadata": {"type": "object", "enabled": True},
                        "file_type": {"type": "keyword"},
                        "parent_path": {"type": "keyword"},
                        "parent_name": {"type": "keyword"},
                        # Searchable field written on snippet acceptance —
                        # stores the reviewer-typed label (e.g. "Scott A. Reich signature").
                        "reviewed_content": {
                            "type": "text",
                            "analyzer": "standard",
                            "fields": {
                                "english": {"type": "text", "analyzer": "english"}
                            },
                        },
                    }
                },
            )
        except Exception as exc:
            logger.debug("Could not apply optional mapping updates: %s", exc)
    
    def index_document_direct(self, doc_id: str, document: Dict[str, Any]) -> bool:
        """Index single document immediately (express lane - no batching)
        
        Args:
            doc_id: Document ID
            document: Document to index
            
        Returns:
            True if indexed successfully, False otherwise
        """
        try:
            response = self.client.index(
                index=self.index_name,
                id=doc_id,
                body=document,
                refresh=False  # Let bulk refresh interval handle visibility for speed
            )
            
            result = response.get('result', '')
            success = result in ['created', 'updated']
            
            if success:
                self.documents_indexed += 1
                logger.debug(f"Express indexed doc {doc_id}: {result}")
            else:
                logger.warning(f"Unexpected index result for {doc_id}: {result}")
            
            return success
            
        except Exception as e:
            logger.error(f"Direct index failed for {doc_id}: {e}")
            return False
    
    def update_document_ocr(self, doc_id: str, ocr_content: str, ocr_confidence: float, metrics: Optional[Dict[str, Any]] = None, max_retries: int = 3) -> bool:
        """Update existing document with OCR content (partial update)
        
        Args:
            doc_id: Document ID to update
            ocr_content: OCR extracted text
            ocr_confidence: OCR confidence score (0-100)
            metrics: Optional dict containing verification metrics (verification_status, overall_risk_score)
            max_retries: Maximum retry attempts for conflict errors
            
        Returns:
            True if updated successfully, False otherwise
        """
        for attempt in range(max_retries):
            try:
                painless_script = """
                    if (ctx._source.containsKey('main_content') == false || ctx._source.main_content == null || ctx._source.main_content == '') {
                        ctx._source.main_content = params.ocr_content;
                    }
                    ctx._source.ocr_content = params.ocr_content;
                    ctx._source.ocr_confidence = params.ocr_confidence;
                    ctx._source.ocr_completed = true;
                    if (params.metrics != null) {
                        if (ctx._source.metadata == null) {
                            ctx._source.metadata = new HashMap();
                        }
                        if (params.metrics.containsKey('verification_status')) {
                            ctx._source.metadata.verification_status = params.metrics.verification_status;
                        }
                        if (params.metrics.containsKey('overall_risk_score')) {
                            ctx._source.metadata.overall_risk_score = params.metrics.overall_risk_score;
                        }
                        if (params.metrics.containsKey('has_signature')) {
                            ctx._source.metadata.has_signature = params.metrics.has_signature;
                        }
                        if (params.metrics.containsKey('has_stamp')) {
                            ctx._source.metadata.has_stamp = params.metrics.has_stamp;
                        }
                        if (params.metrics.containsKey('has_logo')) {
                            ctx._source.metadata.has_logo = params.metrics.has_logo;
                        }
                        if (params.metrics.containsKey('has_handwritten')) {
                            ctx._source.metadata.has_handwritten = params.metrics.has_handwritten;
                        }
                    }
                """
                upsert_doc = {
                    'ocr_content': ocr_content,
                    'main_content': ocr_content,
                    'ocr_confidence': ocr_confidence,
                    'ocr_completed': True,
                }
                response = self.client.update(
                    index=self.index_name,
                    id=doc_id,
                    body={
                        'script': {
                            'source': painless_script,
                            'lang': 'painless',
                            'params': {
                                'ocr_content': ocr_content,
                                'ocr_confidence': ocr_confidence,
                                'metrics': metrics,
                            }
                        },
                        'upsert': upsert_doc,
                    },
                    refresh=False,
                    retry_on_conflict=3,
                )
                result = response.get('result', '')
                success = result in ['updated', 'noop']
                
                if success:
                    logger.debug(f"OCR updated doc {doc_id}: confidence={ocr_confidence:.2f}")
                else:
                    logger.warning(f"Unexpected OCR update result for {doc_id}: {result}")
                
                return success
                
            except ConflictError:
                if attempt < max_retries - 1:
                    # Retry with exponential backoff
                    import time
                    backoff = 0.1 * (2 ** attempt)  # 0.1s, 0.2s, 0.4s
                    logger.debug(f"OCR update conflict for {doc_id}, retrying in {backoff}s (attempt {attempt + 1}/{max_retries})")
                    time.sleep(backoff)
                    continue
                else:
                    logger.warning(f"OCR update failed after {max_retries} attempts due to conflicts: {doc_id}")
                    return False
                
            except NotFoundError:
                logger.warning(
                    "OCR update skipped for %s: document missing (likely not indexed yet)",
                    doc_id
                )
                return False
            except Exception as e:
                logger.error(f"OCR update failed for {doc_id}: {e}")
                return False
        
        return False

    # ------------------------------------------------------------------
    # Semantic / k-NN search
    # ------------------------------------------------------------------

    def bulk_index(self, bulk_items: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Bulk index documents with adaptive batching
        
        Args:
            bulk_items: List of action dictionaries and metadata for indexing
            
        Returns:
            Dictionary with indexing results
        """
        if self.circuit_open:
            # Check if should retry
            if time.time() < self.circuit_retry_time:
                return {'success': False, 'error': 'Circuit breaker open'}
            else:
                # Try to close circuit - but verify OpenSearch is healthy first
                logger.info("Attempting to close circuit breaker...")
                if self.health_check():
                    self.circuit_open = False
                    self.consecutive_failures = 0
                    logger.info("Circuit breaker closed - OpenSearch is healthy")
                else:
                    # OpenSearch still unhealthy, extend circuit open time
                    self.circuit_retry_time = time.time() + 60
                    logger.warning("Circuit breaker remains open - OpenSearch still unhealthy")
                    return {'success': False, 'error': 'Circuit breaker open - OpenSearch unhealthy'}
        
        if not bulk_items:
            return {
                'success': True,
                'indexed_items': [],
                'failed_items': [],
                'batch_time_ms': 0,
                'current_batch_size': self.current_batch_size
            }

        backoffs = self.os_config.retry_backoff_seconds or [1, 2, 4]
        max_attempts = max(1, len(backoffs) + 1)
        attempt = 0
        last_error: Optional[str] = None
        actions = [item['action'] for item in bulk_items]
        id_to_item = {item['doc_id']: item for item in bulk_items}
        doc_ids = set(id_to_item.keys())

        while attempt < max_attempts:
            attempt += 1
            start_time = time.time()

            try:
                success_count, error_entries = helpers.bulk(
                    self.client,
                    actions,
                    raise_on_error=False,
                    raise_on_exception=False
                )

                elapsed = time.time() - start_time

                # Update statistics on any attempt
                self.documents_indexed += success_count
                self.batches_sent += 1
                self.total_batch_time += elapsed

                failed_items: List[Dict[str, Any]] = []
                transient_failures = 0

                if error_entries:
                    self.errors += len(error_entries)

                    for error_entry in error_entries:
                        operation, payload = next(iter(error_entry.items()))
                        status = payload.get('status')
                        doc_id = payload.get('_id')
                        error_info = payload.get('error')
                        reason = ''
                        if isinstance(error_info, dict):
                            reason = error_info.get('reason', '')
                            error_type = error_info.get('type', '')
                        else:
                            reason = str(error_info)
                            error_type = ''

                        metadata = id_to_item.get(doc_id)
                        failure_record = {
                            'doc_id': doc_id,
                            'queue_id': metadata['queue_id'] if metadata else None,
                            'file_id': metadata['file_id'] if metadata else None,
                            'retry_count': metadata['retry_count'] if metadata else None,
                            'status': status,
                            'error': reason,
                            'error_type': error_type,
                            'operation': operation,
                            'transient': status in {429, 500, 502, 503, 504, 408, None}
                        }

                        if failure_record['transient']:
                            transient_failures += 1

                        failed_items.append(failure_record)

                    if transient_failures:
                        self.consecutive_failures += 1
                        self._reduce_batch_size_on_failure()
                    else:
                        self.consecutive_failures = 0

                    if self.consecutive_failures >= self.max_consecutive_failures:
                        self._open_circuit()
                else:
                    self.consecutive_failures = 0

                # Adapt batch size only when there were no transient failures
                if not error_entries or transient_failures == 0:
                    self._adapt_batch_size(elapsed, len(actions))

                failed_ids = {failure['doc_id'] for failure in failed_items if failure.get('doc_id') is not None}
                succeeded_ids = list(doc_ids - failed_ids)
                succeeded_items = [id_to_item[doc_id] for doc_id in succeeded_ids if doc_id in id_to_item]

                return {
                    'success': not failed_items or len(succeeded_items) > 0,
                    'indexed_items': succeeded_items,
                    'failed_items': failed_items,
                    'batch_time_ms': int(elapsed * 1000),
                    'current_batch_size': self.current_batch_size,
                    'transient_error': transient_failures > 0
                }

            except (ConnectionError, TransportError) as exc:
                last_error = str(exc)

                # ----------------------------------------------------------
                # Handle HTTP 413 (Payload Too Large) – split the batch in
                # half and retry each sub-batch independently.
                # ----------------------------------------------------------
                is_413 = '413' in last_error
                if is_413 and len(actions) > 1:
                    logger.warning(
                        "OpenSearch 413 Payload Too Large on %d docs – splitting batch in half and retrying",
                        len(actions),
                    )
                    mid = len(actions) // 2
                    items_a = bulk_items[:mid]
                    items_b = bulk_items[mid:]
                    result_a = self.bulk_index(items_a)
                    result_b = self.bulk_index(items_b)
                    # Merge results
                    merged = {
                        'success': result_a.get('success', False) or result_b.get('success', False),
                        'indexed_items': result_a.get('indexed_items', []) + result_b.get('indexed_items', []),
                        'failed_items': result_a.get('failed_items', []) + result_b.get('failed_items', []),
                        'batch_time_ms': result_a.get('batch_time_ms', 0) + result_b.get('batch_time_ms', 0),
                        'current_batch_size': max(1, self.current_batch_size // 2),
                    }
                    return merged

                self.consecutive_failures += 1
                self._reduce_batch_size_on_failure()
                if self.consecutive_failures >= self.max_consecutive_failures:
                    self._open_circuit()

                if attempt < max_attempts:
                    backoff = backoffs[attempt - 1] if attempt - 1 < len(backoffs) else backoffs[-1]
                    logger.warning(
                        "OpenSearch connection issue (attempt %s/%s): %s. Retrying in %ss",
                        attempt,
                        max_attempts,
                        exc,
                        backoff
                    )
                    time.sleep(backoff)
                    continue

                logger.error(f"OpenSearch connection error: {exc}")
                break

            except Exception as exc:  # pylint: disable=broad-except
                last_error = str(exc)
                logger.error(f"Error in bulk indexing: {exc}", exc_info=True)
                break

        return {
            'success': False,
            'error': last_error or 'Unknown OpenSearch error',
            'transient_error': True,
            'indexed_items': [],
            'failed_items': [
                {
                    'doc_id': item['doc_id'],
                    'queue_id': item['queue_id'],
                    'file_id': item['file_id'],
                    'transient': True
                }
                for item in bulk_items
            ],
            'batch_time_ms': 0,
            'current_batch_size': self.current_batch_size
        }
    
    def _adapt_batch_size(self, elapsed: float, docs_in_batch: int) -> None:
        """Adapt batch size based on performance"""
        if elapsed < self.fast_threshold:
            # Batch was too fast, increase size
            new_size = min(
                self.current_batch_size + self.batch_adjustment_step,
                self.max_batch_size
            )
            if new_size != self.current_batch_size:
                logger.debug(f"Increasing batch size: {self.current_batch_size} → {new_size}")
                self.current_batch_size = new_size
        
        elif elapsed > self.slow_threshold:
            # Batch was too slow, decrease size
            new_size = max(
                self.current_batch_size - self.batch_adjustment_step,
                self.min_batch_size
            )
            if new_size != self.current_batch_size:
                logger.debug(f"Decreasing batch size: {self.current_batch_size} → {new_size}")
                self.current_batch_size = new_size

    def _reduce_batch_size_on_failure(self) -> None:
        """Aggressively reduce batch size after errors"""
        new_size = max(self.current_batch_size // 2, self.min_batch_size)
        if new_size < self.current_batch_size:
            logger.debug(
                "Reducing batch size due to failure: %s → %s",
                self.current_batch_size,
                new_size
            )
            self.current_batch_size = new_size
    
    def _open_circuit(self) -> None:
        """Open circuit breaker"""
        self.circuit_open = True
        self.circuit_retry_time = time.time() + 60  # Wait 60 seconds
        logger.error(f"Circuit breaker OPEN after {self.consecutive_failures} consecutive failures")
    
    def update_document(self, doc_id: str, updates: Dict[str, Any]) -> bool:
        """Update specific fields in a document"""
        try:
            self.client.update(
                index=self.index_name,
                id=doc_id,
                body={'doc': updates}
            )
            return True
        except Exception as e:
            logger.error(f"Error updating document {doc_id}: {e}")
            return False
    
    def set_refresh_interval(self, interval: str) -> None:
        """Set index refresh interval"""
        try:
            self.client.indices.put_settings(
                index=self.index_name,
                body={'index': {'refresh_interval': interval}}
            )
            logger.info(f"Set refresh interval to {interval}")
        except Exception as e:
            logger.warning(f"Error setting refresh interval: {e}")
    
    def health_check(self) -> bool:
        """Check OpenSearch cluster health"""
        try:
            health = self.client.cluster.health()
            return health['status'] in ['green', 'yellow']
        except Exception:
            return False
    
    def close(self):
        """Close the OpenSearch client connection"""
        try:
            if hasattr(self, 'client') and self.client:
                self.client.close()
        except Exception as e:
            logger.warning(f"Error closing OpenSearch client: {e}")

    def get_stats(self) -> Dict[str, Any]:
        """Get client statistics"""
        avg_batch_time = (self.total_batch_time / self.batches_sent 
                         if self.batches_sent > 0 else 0)
        
        return {
            'documents_indexed': self.documents_indexed,
            'batches_sent': self.batches_sent,
            'errors': self.errors,
            'average_batch_time_ms': int(avg_batch_time * 1000),
            'current_batch_size': self.current_batch_size,
            'circuit_open': self.circuit_open,
            'consecutive_failures': self.consecutive_failures
        }