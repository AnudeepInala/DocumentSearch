"""
Document Builder - Constructs OpenSearch documents from extraction data
"""

import json
import redis
from typing import Dict, Any, Optional, Tuple
from datetime import datetime
from pathlib import Path

from core.logging_manager import get_logger
from core.config_manager import get_config

logger = get_logger("indexing.builder")

# Maximum content lengths to prevent HTTP 413 errors
MAX_MAIN_CONTENT_CHARS = 500_000  # 500K chars (~500KB)
MAX_EMBEDDED_CONTENT_CHARS = 200_000  # 200K chars (~200KB)
MAX_OCR_CONTENT_CHARS = 200_000  # 200K chars (~200KB)
MAX_EMBEDDED_FILES_ENTRIES = 200  # Max embedded file metadata entries to keep
MAX_EMBEDDED_FILE_CONTENT_CHARS = 5_000  # Max content per embedded entry in the array


class DocumentBuilder:
    """Builds OpenSearch documents from extracted content"""
    
    def __init__(self):
        """Initialize document builder with config"""
        try:
            self.config = get_config()
            # Use config value if available, otherwise use defaults
            # Bug fix: config.tika doesn't exist; correct path is config.extraction.tika
            extraction_tika = getattr(self.config, 'extraction', None)
            if extraction_tika:
                extraction_tika = getattr(extraction_tika, 'tika', None)
            self.max_text_length = getattr(extraction_tika, 'max_text_length', MAX_MAIN_CONTENT_CHARS) if extraction_tika else MAX_MAIN_CONTENT_CHARS
        except Exception:
            self.max_text_length = MAX_MAIN_CONTENT_CHARS

        # Lazy Redis connection for parent-child lookups
        self._redis: Optional[redis.Redis] = None

    def _get_redis(self) -> Optional[redis.Redis]:
        """Lazy-init a Redis connection for parent-hash lookups"""
        if self._redis is None:
            try:
                cfg = get_config()
                redis_cfg = getattr(cfg, 'redis', None)
                url = getattr(redis_cfg, 'url', 'redis://localhost:6379/0') if redis_cfg else 'redis://localhost:6379/0'
                self._redis = redis.from_url(url, decode_responses=True, protocol=2)
            except Exception:
                return None
        return self._redis

    def _lookup_parent_metadata(self, child_hash: str) -> Optional[Dict[str, str]]:
        """Look up parent metadata (hash, path, name) for an embedded (child) file"""
        if not child_hash:
            return None
        try:
            r = self._get_redis()
            if r:
                val = r.hget('docsearch:parent_map', child_hash)
                if val:
                    try:
                        import json
                        return json.loads(val)
                    except Exception:
                        return {
                            'parent_hash': val,
                            'parent_path': '',
                            'parent_name': ''
                        }
        except Exception:
            pass
        return None
    
    def _truncate_content(self, content: str, max_chars: int, field_name: str = "") -> Tuple[str, bool, int]:
        """Truncate content to max characters, preserving word boundaries.
        
        Returns:
            tuple: (truncated_content, was_truncated, original_length)
        """
        if not content or len(content) <= max_chars:
            return content or '', False, len(content) if content else 0
        
        original_length = len(content)
        
        # Truncate to max_chars, try to end at word boundary
        truncated = content[:max_chars]
        last_space = truncated.rfind(' ')
        if last_space > max_chars * 0.8:  # Don't go back more than 20%
            truncated = truncated[:last_space]
        
        if field_name:
            logger.warning(f"Truncated {field_name} from {original_length:,} to {len(truncated):,} chars")
        
        return truncated + " [content truncated]", True, original_length
    
    def build_document(self, document_json: str, existing_ocr: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        Build OpenSearch document from extraction data
        
        Args:
            document_json: JSON string from extraction stage
            
        Returns:
            Document ready for OpenSearch indexing
        """
        try:
            # Parse extraction data
            extracted = json.loads(document_json)
            
            # Extract file name from path
            file_path = extracted.get('file_path', '')
            file_name = Path(file_path).name if file_path else ''
            
            embedded_files_raw = extracted.get('embedded_files', [])
            if not isinstance(embedded_files_raw, list):
                logger.warning("Embedded files payload is not a list for %s; defaulting to empty", file_path)
                embedded_files = []
            else:
                embedded_files = embedded_files_raw

            # -----------------------------------------------------------------
            # Sanitise the embedded_files array to prevent HTTP 413 errors.
            # Each entry can carry huge Tika metadata blobs; we keep only the
            # fields the dashboard / search actually needs and cap the list.
            # -----------------------------------------------------------------
            if len(embedded_files) > MAX_EMBEDDED_FILES_ENTRIES:
                logger.warning(
                    "Truncating embedded_files from %d to %d entries for %s",
                    len(embedded_files), MAX_EMBEDDED_FILES_ENTRIES, file_path)
                embedded_files = embedded_files[:MAX_EMBEDDED_FILES_ENTRIES]

            sanitised_embedded = []
            for emb in embedded_files:
                sanitised = {
                    'name': emb.get('name', emb.get('resourceName', '')),
                    'index': emb.get('index', 0),
                    'metadata': {},  # drop heavy raw Tika metadata
                }
                # Keep content but cap it
                content = emb.get('content', '')
                if content and len(content) > MAX_EMBEDDED_FILE_CONTENT_CHARS:
                    content = content[:MAX_EMBEDDED_FILE_CONTENT_CHARS] + ' [truncated]'
                sanitised['content'] = content
                sanitised_embedded.append(sanitised)
            embedded_files = sanitised_embedded

            embedded_count = extracted.get('embedded_count')
            if not isinstance(embedded_count, int):
                embedded_count = len(embedded_files)

            # Build base document with content truncation to prevent 413 errors
            main_content = extracted.get('main_content', '')
            main_content, was_truncated, original_length = self._truncate_content(
                main_content, MAX_MAIN_CONTENT_CHARS, f"main_content for {file_path}"
            )
            
            document = {
                'file_path': file_path,
                'file_name': file_name,
                'file_hash': extracted.get('file_hash', ''),
                'content_hash': extracted.get('content_hash', ''),
                'main_content': main_content,
                # M5: Truncation metadata so consumers know if full content was indexed
                'content_truncated': was_truncated,
                'content_original_length': original_length,
                'metadata': extracted.get('metadata', {}),
                'embedded_files': embedded_files,
                'embedded_count': embedded_count,
                'needs_ocr': extracted.get('needs_ocr', False),
                'tika_content': main_content if extracted.get('needs_ocr', False) else '',
                'ocr_completed': False,
                'indexed_at': datetime.now().isoformat(),
                'extraction_time_ms': extracted.get('extraction_time_ms', 0)
            }

            # Preserve semantic tagging fields when provided by extraction stage.
            dynamic_subtags = extracted.get('dynamic_subtags', [])
            if isinstance(dynamic_subtags, str):
                dynamic_subtags = [
                    tag.strip()
                    for tag in dynamic_subtags.replace(";", ",").replace("|", ",").split(",")
                    if tag.strip()
                ]
            elif not isinstance(dynamic_subtags, list):
                dynamic_subtags = []

            key_names = extracted.get('key_names', [])
            if isinstance(key_names, str):
                key_names = [name.strip() for name in key_names.replace(";", ",").split(",") if name.strip()]
            elif not isinstance(key_names, list):
                key_names = []

            important_dates = extracted.get('important_dates', [])
            if isinstance(important_dates, str):
                important_dates = [d.strip() for d in important_dates.replace(";", ",").split(",") if d.strip()]
            elif not isinstance(important_dates, list):
                important_dates = []

            locations = extracted.get('location_mentioned', [])
            if isinstance(locations, str):
                locations = [loc.strip() for loc in locations.replace(";", ",").split(",") if loc.strip()]
            elif not isinstance(locations, list):
                locations = []

            file_type = extracted.get('file_type', '')
            if not file_type:
                file_type = Path(file_path).suffix.lower().lstrip('.') if file_path else ''

            document.update({
                'smart_id': str(extracted.get('smart_id', '') or ''),
                'category': str(extracted.get('category', '') or ''),
                'department': str(extracted.get('department', '') or ''),
                'purpose': str(extracted.get('purpose', '') or ''),
                'dynamic_subtags': dynamic_subtags,
                'key_names': key_names,
                'amount_found': str(extracted.get('amount_found', '') or ''),
                'important_dates': important_dates,
                'location_mentioned': locations,
                'confidentiality': str(extracted.get('confidentiality', '') or ''),
                'file_type': file_type,
            })

            # -----------------------------------------------------------------
            # Parent-child tagging: if this file was deep-extracted from a
            # parent Office document, tag it so the UI can show the lineage.
            # The extraction worker stores the mapping in Redis:
            #   docsearch:parent_map   <child_hash> → <parent_meta_json>
            # Uses the existing 'parent_file' keyword field in the mapping.
            # -----------------------------------------------------------------
            file_hash = extracted.get('file_hash', '')
            parent_meta = self._lookup_parent_metadata(file_hash)
            if parent_meta:
                document['parent_file'] = parent_meta.get('parent_hash', '')
                document['parent_path'] = parent_meta.get('parent_path', '')
                document['parent_name'] = parent_meta.get('parent_name', '')
                document['is_embedded'] = True
            else:
                document['parent_file'] = ''
                document['parent_path'] = ''
                document['parent_name'] = ''
                document['is_embedded'] = False
            
            # Add file size if available
            if 'file_size' in extracted:
                document['file_size'] = extracted['file_size']
            
            # Handle embedded content - always set this field
            embedded_content = []
            if document['embedded_files']:
                for embedded in document['embedded_files']:
                    content = embedded.get('content', '')
                    if content:
                        embedded_content.append(content)
            
            # Always set embedded_content field (with truncation)
            raw_embedded = '\n\n'.join(embedded_content) if embedded_content else ''
            emb_truncated, _, _ = self._truncate_content(raw_embedded, MAX_EMBEDDED_CONTENT_CHARS, f"embedded_content for {file_path}")
            document['embedded_content'] = emb_truncated
            
            # Preserve OCR content if already computed; otherwise blank for OCR worker to fill later.
            if existing_ocr and existing_ocr.get('ocr_content'):
                document['ocr_content'] = existing_ocr['ocr_content']
                document['ocr_confidence'] = float(existing_ocr.get('ocr_confidence') or 0.0)
                document['ocr_completed'] = bool(existing_ocr.get('ocr_completed', True))
            else:
                document['ocr_content'] = ''
                document['ocr_confidence'] = 0.0
            
            return document
            
        except json.JSONDecodeError as e:
            logger.error(f"Error parsing document JSON: {e}")
            return None
        except Exception as e:
            logger.error(f"Error building document: {e}", exc_info=True)
            return None
    
    def build_ocr_update(self, ocr_text: str, confidence: float) -> Dict[str, Any]:
        """
        Build OCR update for existing document
        
        Args:
            ocr_text: Extracted OCR text
            confidence: OCR confidence score
            
        Returns:
            Update dictionary for OpenSearch
        """
        # Truncate OCR content to prevent 413 errors
        truncated_ocr, _, _ = self._truncate_content(ocr_text, MAX_OCR_CONTENT_CHARS, "ocr_content")
        
        return {
            'ocr_content': truncated_ocr,
            'ocr_confidence': confidence,
            'ocr_completed': True,
            'ocr_updated_at': datetime.now().isoformat()
        }
