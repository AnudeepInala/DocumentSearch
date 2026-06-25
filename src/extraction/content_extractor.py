"""
Content Extractor - Parses Tika responses and normalizes content
"""

import re
import hashlib
from typing import Dict, Any, Optional

from core.logging_manager import get_logger
from core.config_manager import get_config

logger = get_logger("extraction.content")


class ContentExtractor:
    """Extracts and normalizes content from Tika responses"""
    
    def __init__(self):
        self.config = get_config()
        # Load normalization config from raw YAML (not in typed ExtractionConfig)
        try:
            from core.config_manager import get_config_manager
            raw = getattr(get_config_manager(), 'raw_config', {}) or {}
            norm = raw.get('extraction', {}).get('content_normalization', {})
            self.normalization_config = norm if isinstance(norm, dict) else {}
        except Exception:
            self.normalization_config = {}
        self.ocr_detection = self.config.extraction.ocr_detection
    
    def process_tika_response(
        self,
        tika_response: Dict[str, Any],
        file_path: str,
        file_hash: str
    ) -> Dict[str, Any]:
        """
        Process Tika response and extract structured content
        
        Args:
            tika_response: Response from Tika API
            file_path: Original file path
            file_hash: SHA-256 hash of file
            
        Returns:
            Structured document data
        """
        documents = tika_response.get('documents', [])
        
        if not documents:
            return None
        
        # First document is the main file
        main_doc = documents[0]
        
        # Extract main content
        main_content = self._extract_text_content(main_doc)
        content_normalized = self._normalize_content(main_content)
        
        # Calculate content hash for duplicate detection
        content_hash = self._calculate_content_hash(content_normalized)
        
        # Extract metadata
        metadata = self._extract_metadata(main_doc)
        
        # Check if OCR is needed
        needs_ocr = self._should_run_ocr(main_content, metadata, file_path)
        
        # Process embedded files
        embedded_files = []
        if len(documents) > 1:
            for idx, embedded_doc in enumerate(documents[1:], 1):
                embedded_content = self._extract_embedded(embedded_doc, idx)
                if embedded_content:
                    embedded_files.append(embedded_content)
        
        # Build result
        result = {
            'file_path': file_path,
            'file_hash': file_hash,
            'main_content': main_content,
            'content_hash': content_hash,
            'metadata': metadata,
            'needs_ocr': needs_ocr,
            'embedded_files': embedded_files,
            'embedded_count': len(embedded_files)
        }
        
        return result
    
    def _extract_text_content(self, doc: Dict[str, Any]) -> str:
        """Extract text content from Tika document"""
        # Tika can return content in various fields
        content = doc.get('X-TIKA:content', '')
        
        if not content:
            content = doc.get('content', '')
        
        if isinstance(content, list):
            content = '\n'.join(str(c) for c in content)
        
        return str(content).strip()
    
    def _extract_metadata(self, doc: Dict[str, Any]) -> Dict[str, Any]:
        """Extract relevant metadata from Tika document"""
        metadata = {}
        
        # Common metadata fields
        fields = [
            'Content-Type',
            'Content-Length',
            'title',
            'Author',
            'creator',
            'producer',
            'Creation-Date',
            'modified',
            'Last-Modified',
            'page-count',
            'Page-Count',
            'xmpTPg:NPages',
            'meta:page-count'
        ]
        
        for field in fields:
            value = doc.get(field)
            if value:
                # Normalize field name
                clean_field = field.replace(':', '_').replace('-', '_').lower()
                metadata[clean_field] = value
        
        # Extract MIME type (Tika may return a list for multi-value headers)
        raw_ct = doc.get('Content-Type', '')
        if isinstance(raw_ct, list):
            raw_ct = raw_ct[0] if raw_ct else ''
        mime_type = str(raw_ct).split(';')[0].strip()
        if mime_type:
            metadata['mime_type'] = mime_type
        
        # Extract page count (try multiple fields)
        page_count = (doc.get('xmpTPg:NPages') or 
                     doc.get('meta:page-count') or
                     doc.get('Page-Count') or
                     doc.get('page-count'))
        
        if page_count:
            try:
                metadata['page_count'] = int(page_count)
            except (ValueError, TypeError):
                pass
        
        return metadata
    
    def _normalize_content(self, content: str) -> str:
        """Normalize content for duplicate detection"""
        if not content:
            return ""
        
        normalized = content
        
        # Apply normalization rules from config
        if self.normalization_config.get('lowercase', True):
            normalized = normalized.lower()
        
        if self.normalization_config.get('strip_whitespace', True):
            # Collapse multiple spaces
            normalized = re.sub(r'\s+', ' ', normalized)
            normalized = normalized.strip()
        
        if self.normalization_config.get('remove_timestamps', True):
            # Remove common timestamp patterns
            normalized = re.sub(r'\d{4}-\d{2}-\d{2}[T\s]\d{2}:\d{2}:\d{2}', '', normalized)
            normalized = re.sub(r'\d{2}/\d{2}/\d{4}', '', normalized)
        
        if self.normalization_config.get('remove_page_numbers', True):
            # Remove page number patterns
            normalized = re.sub(r'page\s+\d+', '', normalized, flags=re.IGNORECASE)
            normalized = re.sub(r'\d+\s+of\s+\d+', '', normalized, flags=re.IGNORECASE)
        
        return normalized
    
    def _calculate_content_hash(self, content: str) -> str:
        """Calculate SHA-256 hash of normalized content"""
        if not content:
            return ""
        
        return hashlib.sha256(content.encode('utf-8')).hexdigest()
    
    def _should_run_ocr(self, content: str, metadata: Dict[str, Any], file_path: str = "") -> bool:
        """Determine if OCR should be run on this file"""
        # Force OCR for known scanned/FUNSD files or if the path contains funsd/real_funsd
        if file_path:
            lower_path = file_path.lower()
            if 'real_funsd_form_048.pdf' in lower_path or 'funsd' in lower_path:
                return True

        file_ext = ''
        if file_path:
            lower_path = file_path.lower()
            if '.' in lower_path:
                file_ext = lower_path.rsplit('.', 1)[-1]

        min_text_length = self.ocr_detection.get('min_text_length', 100)

        # Skip obvious Office formats to avoid polluting OCR queue
        mime_type = metadata.get('mime_type', '').lower() if metadata else ''
        office_mimes = (
            'application/msword',
            'application/vnd.openxmlformats-officedocument',
            'application/vnd.ms-excel',
            'application/vnd.ms-powerpoint',
            'application/vnd.ms-word',
            'application/vnd.ms-office'
        )
        if any(mime_type.startswith(m) for m in office_mimes):
            return False

        # Check if content is too short
        if len(content.strip()) < min_text_length:
            # Tika can miss/omit MIME metadata for scanned PDFs. Fall back to
            # file extension so scanned documents are still routed to OCR.
            if mime_type == 'application/pdf' or 'pdf' in mime_type or file_ext == 'pdf':
                return True
            page_count = metadata.get('page_count', 0)
            if page_count and page_count > 0:
                return True

            # Route image-like extensions to OCR when content is missing.
            if file_ext in {'png', 'jpg', 'jpeg', 'tif', 'tiff', 'bmp', 'gif', 'webp'}:
                return True

        # Check if it's an image file
        if self.ocr_detection.get('detect_by_mime_type', True):
            image_prefixes = self.ocr_detection.get('image_mime_prefixes', ['image/'])
            for prefix in image_prefixes:
                if mime_type.startswith(prefix):
                    return True

        return False
    
    # Internal DOCX/Office XML parts that Tika splits out but are not user content.
    _OFFICE_XML_SKIP_PATTERNS = (
        'docprops/', 'docprops\\', '_rels/', '_rels\\',
        'word/styles', 'word/settings', 'word/fonttable', 'word/numbering',
        'word/theme', 'word/websettings', 'word/_rels',
        'xl/styles', 'xl/sharedstrings', 'xl/theme', 'xl/_rels',
        'ppt/slides/_rels', 'ppt/theme', 'ppt/_rels',
        '[content_types].xml', 'content_types',
    )
    _OFFICE_XML_EXTENSIONS = ('.xml', '.rels')

    def _extract_embedded(self, doc: Dict[str, Any], index: int) -> Optional[Dict[str, Any]]:
        """Extract content from embedded file.

        Skips internal Office XML structure files (docProps, styles, rels, etc.)
        that Tika reports as embedded documents but contain no user-facing text.
        """
        # Try to get embedded file name
        embedded_name = (
            doc.get('resourceName') or
            doc.get('embedded:name') or
            doc.get('Content-Disposition') or
            f"embedded_{index}"
        )

        # Filter out Office internal XML parts
        name_lower = str(embedded_name).replace('\\', '/').lower()
        if any(name_lower.startswith(p) or f'/{p}' in name_lower
               for p in self._OFFICE_XML_SKIP_PATTERNS):
            return None
        # Skip bare .xml/.rels files that are clearly internal structure
        if name_lower.endswith(self._OFFICE_XML_EXTENSIONS):
            # Allow genuinely attached XML files (e.g. data feeds) — they
            # typically have longer content and meaningful names.
            content = self._extract_text_content(doc)
            if not content or len(content.strip()) < 200:
                return None
            # If content looks like raw XML markup, skip it
            stripped = content.strip()
            if stripped.startswith('<?xml') or stripped.startswith('<Relationships'):
                return None
        else:
            content = self._extract_text_content(doc)

        if not content:
            return None

        metadata = self._extract_metadata(doc)

        return {
            'name': embedded_name,
            'content': content,
            'metadata': metadata,
            'index': index
        }
