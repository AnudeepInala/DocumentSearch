"""
Content Extractor - Parses Tika responses and normalizes content
"""

import re
import math
import string
import hashlib
from collections import Counter
from pathlib import Path
from typing import Dict, Any, Optional

from core.logging_manager import get_logger
from core.config_manager import get_config


# Top ~400 English letter bigrams (covers >99% of bigram occurrences in English text).
# Any two-letter pair not in this set is considered implausible within a word.
# Non-alpha character pairs are always implausible (not in this set by definition).
_PLAUSIBLE_BIGRAMS = frozenset([
    "th", "he", "in", "er", "an", "re", "on", "en", "at", "nd",
    "ti", "es", "or", "te", "of", "ed", "is", "it", "al", "ar",
    "st", "to", "nt", "ng", "se", "ha", "as", "ou", "io", "le",
    "ve", "co", "me", "de", "hi", "ri", "ro", "ic", "ne", "ea",
    "ra", "ce", "li", "ch", "ll", "be", "ma", "si", "om", "ur",
    "ca", "el", "ta", "la", "ns", "di", "fo", "ho", "pe", "ec",
    "pr", "no", "ct", "us", "ac", "ot", "il", "tr", "ly", "nc",
    "et", "ut", "ss", "so", "rs", "un", "lo", "wa", "ge", "ie",
    "wh", "ee", "wi", "em", "ad", "ol", "rt", "po", "we", "na",
    "ul", "ni", "ts", "mo", "ow", "pa", "im", "mi", "ai", "sh",
    "ir", "su", "id", "os", "iv", "ia", "am", "fi", "ci", "vi",
    "pl", "ig", "tu", "ev", "ld", "ry", "mp", "fe", "bl", "ab",
    "gh", "ty", "op", "wo", "sa", "ay", "ex", "ke", "fr", "oo",
    "av", "ag", "if", "ap", "gr", "od", "bo", "sp", "rd", "do",
    "uc", "bu", "ei", "ov", "by", "rm", "ep", "tt", "oc", "fa",
    "ef", "cu", "rn", "sc", "gi", "da", "yo", "cr", "cl", "du",
    "ga", "qu", "ue", "ff", "ba", "ey", "ls", "va", "um", "pp",
    "ua", "up", "lu", "go", "ht", "ru", "ug", "ds", "lt", "pi",
    "rc", "rr", "eg", "au", "ck", "ew", "mu", "br", "bi", "pt",
    "ak", "pu", "ui", "rg", "ib", "tl", "ny", "ki", "rk", "oa",
    "rl", "sw", "mm", "ys", "oi", "ft", "tw", "my", "ox", "ob",
    "nu", "fl", "dr", "nn", "aw", "sl", "sm", "nk", "ks", "gy",
    "sk", "sn", "gu", "ph", "gl", "wn", "wr", "hy", "dg", "sy",
    "fy", "lf", "lk", "nf", "gn", "mg", "dd", "hn", "wl", "hr",
    "dn", "zy", "lm", "xt", "rf", "rb", "ms", "nj", "bt", "gm",
    "mn", "xi", "vy", "iy", "tk", "nb", "bb", "dv", "dl", "gg",
    "gt", "hm", "hb", "ln", "mt", "pm", "sb", "tn", "xp", "ya",
    "ye", "yi", "yo", "yu", "za", "ze", "zi", "zo", "zu", "bj",
    "bs", "cc", "dj", "dm", "fn", "fs", "ft", "gd", "gs", "gz",
    "hd", "hs", "hw", "hz", "iq", "iz", "ja", "je", "ji", "jo",
    "ju", "ka", "kn", "ko", "ky", "lb", "lc", "ld", "lg", "lp",
    "lt", "lv", "ly", "mb", "mc", "mp", "mw", "nc", "nd", "nf",
    "nm", "np", "nq", "nr", "ns", "nt", "nv", "nw", "nx", "nz",
    "oy", "oz", "py", "rb", "rp", "rv", "rw", "ry", "sf", "sq",
    "tc", "tf", "tm", "tp", "tv", "tz", "ub", "ud", "uf", "uh",
    "uk", "un", "uq", "ux", "uz", "vy", "wb", "wf", "wk", "wt",
    "xe", "xh", "xl", "xo", "xt", "xu", "yb", "yc", "yd", "yf",
    "yg", "yh", "yl", "ym", "yn", "yp", "yr", "ys", "yt", "yw",
])

_COMMON_SHORT_WORDS = frozenset([
    "a", "i", "an", "at", "am", "as", "be", "by", "do", "go",
    "he", "if", "in", "is", "it", "me", "my", "no", "of", "oh",
    "ok", "on", "or", "so", "to", "up", "us", "we",
])

_EXPECTED_CHARS = frozenset(
    string.ascii_letters + string.digits +
    " \t\n\r.,;:'-\"()[]!?/@#$%&*+=<>_"
)

_SCANNER_KEYWORDS = frozenset([
    "xerox", "canon", "ricoh", "konica", "minolta", "epson",
    "fujitsu", "kodak", "scanjet", "ghostscript", "scansnap",
    "imagerunner", "bizhub", "sharp", "kyocera",
])

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
        needs_ocr = self._should_run_ocr(main_content, metadata, file_path=file_path)
        is_corrupted = self._is_text_corrupted(main_content, metadata=metadata)
        
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
    
    # Tika /rmeta/text renders table cells as individual lines: \tCellValue
    # Rows are separated by blank lines.  This regex matches such a cell line.
    _TIKA_CELL_RE = re.compile(r'^\t[^\t]*$', re.MULTILINE)

    @staticmethod
    def _normalize_tika_tables(content: str) -> str:
        """Reassemble Tika's one-cell-per-line table format into tab-separated rows.

        Tika /rmeta/text renders DOCX/XLSX tables as:
            \\tCell1\\n\\tCell2\\n\\tCell3\\n\\n  (blank line = row boundary)

        We walk the lines, collect consecutive cell-lines into groups (blank line =
        row boundary), infer column count from the first group, and emit one
        tab-separated line per row — preserving any non-table text around it.
        """
        cell_re = re.compile(r'^\t[^\t]*$')
        lines = content.splitlines()
        if not any(cell_re.match(ln) for ln in lines):
            return content

        # Pre-normalize: first cell of a row-group sometimes lacks leading \t
        normalized_lines = []
        for i, ln in enumerate(lines):
            if (ln.strip() and not ln.startswith('\t')
                    and i + 1 < len(lines) and cell_re.match(lines[i + 1])
                    and (i == 0 or not lines[i - 1].strip())):
                normalized_lines.append('\t' + ln)
            else:
                normalized_lines.append(ln)
        lines = normalized_lines

        # First pass: collect all row-groups to determine column count
        row_groups: list = []
        current: list = []
        for ln in lines:
            if cell_re.match(ln):
                current.append(ln.lstrip('\t').strip())
            else:
                if current:
                    row_groups.append(current)
                    current = []
        if current:
            row_groups.append(current)

        if not row_groups:
            return content

        n_cols = len(row_groups[0])
        if n_cols < 2 or not all(len(g) == n_cols for g in row_groups):
            return content  # irregular — leave as-is

        # Second pass: rebuild output, replacing cell-line blocks with TSV rows
        out: list = []
        row_iter = iter(row_groups)
        current_group: list = []
        for ln in lines:
            if cell_re.match(ln):
                current_group.append(ln.lstrip('\t').strip())
            else:
                if current_group:
                    # Emit this completed row as a TSV line
                    out.append('\t'.join(current_group))
                    current_group = []
                # Skip blank lines that were just row separators inside the table
                # (keep non-blank non-cell lines)
                if ln.strip():
                    out.append(ln)
        if current_group:
            out.append('\t'.join(current_group))

        return '\n'.join(out).strip()

    def _extract_text_content(self, doc: Dict[str, Any]) -> str:
        """Extract text content from Tika document"""
        content = doc.get('X-TIKA:content', '')

        if not content:
            content = doc.get('content', '')

        if isinstance(content, list):
            content = '\n'.join(str(c) for c in content)

        content = str(content).strip()
        content = self._normalize_tika_tables(content)
        return content
    
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
    
    def _is_text_corrupted(self, content: str, metadata: Optional[Dict[str, Any]] = None) -> bool:
        """
        Statistical text quality scoring. Returns True if text quality is too
        low (indicating a bad OCR layer, encoding issues, or scanned content).
        Uses 5 independent signals combined with configurable weights.
        """
        try:
            check_config = self.ocr_detection.get('corruption_check', {})
            if not check_config.get('enabled', True):
                return False

            if not content or len(content.strip()) < 50:
                return False

            # Sample long documents for performance
            text = content.strip()
            if len(text) > 5000:
                text = text[:2500] + text[-2500:]

            weights = check_config.get('weights', {})
            w_entropy = weights.get('entropy_anomaly', 0.20)
            w_bigram = weights.get('bigram_plausibility', 0.30)
            w_alnum = weights.get('alphanumeric_density', 0.20)
            w_fragment = weights.get('word_fragment_ratio', 0.15)
            w_nonstandard = weights.get('nonstandard_symbol_density', 0.15)

            s_entropy = self._score_entropy(text)
            s_bigram = self._score_bigram_plausibility(text)
            s_alnum = self._score_alphanumeric_density(text)
            s_fragment = self._score_word_fragments(text)
            s_nonstandard = self._score_nonstandard_symbols(text)

            final_score = (
                w_entropy * s_entropy +
                w_bigram * s_bigram +
                w_alnum * s_alnum +
                w_fragment * s_fragment +
                w_nonstandard * s_nonstandard
            )

            # Boost score if PDF metadata indicates a scanner-produced document
            scanner_boost = check_config.get('scanner_boost', 1.15)
            if metadata and scanner_boost > 1.0:
                producer = str(metadata.get('producer', '')).lower()
                creator = str(metadata.get('creator', '')).lower()
                combined = producer + " " + creator
                if any(kw in combined for kw in _SCANNER_KEYWORDS):
                    final_score *= scanner_boost

            threshold = check_config.get('threshold', 0.45)

            logger.debug(
                f"Text quality scores: entropy={s_entropy:.3f} bigram={s_bigram:.3f} "
                f"alnum={s_alnum:.3f} fragment={s_fragment:.3f} nonstandard={s_nonstandard:.3f} "
                f"final={final_score:.3f} threshold={threshold}"
            )

            if final_score >= threshold:
                logger.info(f"Text flagged as corrupted: score={final_score:.3f} >= {threshold}")
                return True

            return False

        except Exception as e:
            logger.warning(f"Corrupt text check failed: {e}")
            return False

    def _score_entropy(self, text: str) -> float:
        """Shannon entropy of non-whitespace characters. Normal English ~4.0-4.5 bits."""
        chars = [c for c in text if not c.isspace()]
        if len(chars) < 20:
            return 0.0
        freq = Counter(chars)
        total = len(chars)
        entropy = -sum((count / total) * math.log2(count / total) for count in freq.values())
        if 3.5 <= entropy <= 5.0:
            return 0.0
        elif entropy > 5.0:
            return min(1.0, (entropy - 5.0) / 1.5)
        else:
            return min(1.0, (3.5 - entropy) / 1.5)

    def _score_bigram_plausibility(self, text: str) -> float:
        """
        Measures implausible bigrams both globally and per-token.
        A document with even a few badly-corrupted tokens scores high because
        symbol-inside-word is definitive evidence of a bad text layer.
        """
        tokens = text.split()
        total_bigrams = 0
        implausible_count = 0
        corrupted_tokens = 0
        scored_tokens = 0

        for token in tokens:
            if len(token) < 4:
                continue
            # Skip numeric tokens (decimals like 1234.56)
            stripped = token.replace('.', '').replace(',', '')
            if stripped.isdigit():
                continue
            # Skip URLs
            if '://' in token or token.startswith('www.'):
                continue
            # Skip phone-number-like patterns
            if stripped.replace('-', '').replace('(', '').replace(')', '').isdigit():
                continue

            lower_token = token.lower()
            token_bigrams = 0
            token_implausible = 0

            for i in range(len(lower_token) - 1):
                pair = lower_token[i:i+2]
                if pair[0].isalpha() and pair[1].isalpha():
                    token_bigrams += 1
                    total_bigrams += 1
                    if pair not in _PLAUSIBLE_BIGRAMS:
                        token_implausible += 1
                        implausible_count += 1
                elif pair[0].isalpha() or pair[1].isalpha():
                    # Letter adjacent to symbol inside a word token
                    token_bigrams += 1
                    token_implausible += 1
                    total_bigrams += 1
                    implausible_count += 1

            if token_bigrams >= 2:
                scored_tokens += 1
                # A token with >40% implausible bigrams is individually corrupted
                if token_implausible / token_bigrams > 0.40:
                    corrupted_tokens += 1

        if total_bigrams < 10:
            return 0.0

        # Global ratio component
        global_ratio = implausible_count / total_bigrams
        global_score = 0.0
        if global_ratio > 0.05:
            global_score = min(1.0, (global_ratio - 0.05) / 0.30)

        # Per-token corruption component (even 2-3 corrupted tokens in a doc is bad)
        token_score = 0.0
        if scored_tokens > 0 and corrupted_tokens > 0:
            token_ratio = corrupted_tokens / scored_tokens
            if token_ratio >= 0.02:
                token_score = min(1.0, token_ratio / 0.15)

        return max(global_score, token_score)

    def _score_alphanumeric_density(self, text: str) -> float:
        """Ratio of alnum chars to total non-whitespace. Normal text: 0.85-0.95."""
        non_ws = [c for c in text if not c.isspace()]
        if not non_ws:
            return 0.0
        alnum_count = sum(1 for c in non_ws if c.isalnum())
        ratio = alnum_count / len(non_ws)
        if ratio >= 0.82:
            return 0.0
        elif ratio <= 0.55:
            return 1.0
        return (0.82 - ratio) / 0.27

    def _score_word_fragments(self, text: str) -> float:
        """Ratio of unexplained short (1-2 char) word-like tokens."""
        tokens = text.split()
        word_tokens = [t for t in tokens if any(c.isalpha() for c in t)]
        if len(word_tokens) < 10:
            return 0.0

        fragment_count = 0
        for t in word_tokens:
            if len(t) <= 2 and t.lower() not in _COMMON_SHORT_WORDS:
                fragment_count += 1

        ratio = fragment_count / len(word_tokens)
        if ratio <= 0.10:
            return 0.0
        elif ratio >= 0.35:
            return 1.0
        return (ratio - 0.10) / 0.25

    def _score_nonstandard_symbols(self, text: str) -> float:
        """Density of characters outside the expected English charset."""
        if not text:
            return 0.0
        nonstandard = sum(1 for c in text if c not in _EXPECTED_CHARS)
        ratio = nonstandard / len(text)
        if ratio <= 0.005:
            return 0.0
        elif ratio >= 0.05:
            return 1.0
        return (ratio - 0.005) / 0.045

    def _should_run_ocr(self, content: str, metadata: Dict[str, Any], file_path: str = '') -> bool:
        """Determine if OCR should be run on this file"""
        min_text_length = self.ocr_detection.get('min_text_length', 100)

        # Skip archives — nothing to OCR
        ext = Path(file_path).suffix.lower() if file_path else ''
        if ext in ('.zip', '.tar', '.gz', '.rar', '.7z', '.bz2', '.xz', '.tgz'):
            return False

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
            page_count = metadata.get('page_count', 0)
            if page_count and page_count > 0:
                return True
            # Scanned PDFs often have no page_count and no MIME type from Tika.
            # If the file extension is PDF and there's no readable text, always OCR.
            if str(file_path).lower().endswith('.pdf'):
                return True

        # Check if text layer is corrupted (triggers OCR if True)
        if self._is_text_corrupted(content, metadata=metadata):
            return True

        # Check if it's an image file
        if self.ocr_detection.get('detect_by_mime_type', True):
            image_prefixes = self.ocr_detection.get('image_mime_prefixes', ['image/'])
            for prefix in image_prefixes:
                if mime_type.startswith(prefix):
                    return True

        return False
    
    # Internal Office/ZIP paths that Tika surfaces as sub-documents but contain no
    # useful text (thumbnails, XML relationship files, theme data, etc.)
    _OFFICE_INTERNAL_PREFIXES = (
        '/docProps/', 'docProps/',
        '/word/', '/xl/', '/ppt/',
        '/_rels/', '/customXml/',
        '/theme/', '/media/',
    )

    def _extract_embedded(self, doc: Dict[str, Any], index: int) -> Optional[Dict[str, Any]]:
        """Extract content from embedded file"""
        content = self._extract_text_content(doc)

        if not content:
            return None

        # Skip internal Office/ZIP metadata paths — not real embedded files
        resource_name = (
            doc.get('resourceName') or
            doc.get('embedded:name') or
            doc.get('Content-Disposition') or
            ''
        )
        if any(str(resource_name).startswith(p) for p in self._OFFICE_INTERNAL_PREFIXES):
            return None

        metadata = self._extract_metadata(doc)

        embedded_name = resource_name or f"embedded_{index}"

        return {
            'name': embedded_name,
            'content': content,
            'metadata': metadata,
            'index': index
        }
