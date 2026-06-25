"""
Query Builder - Constructs OpenSearch query DSL
ACCURATE SEARCH - No fuzzy/phonetic matching for 100% accuracy
Enhanced with exact numeric/value matching using .keyword subfields
"""

import re
from typing import List, Dict, Any, Optional

from core.config_manager import get_config
from core.logging_manager import get_logger

logger = get_logger("api.query_builder")


class QueryBuilder:
    """
    Builds OpenSearch query DSL for ACCURATE search
    
    Features:
    - Exact phrase matching for quoted queries
    - Exact numeric/formatted value matching (e.g., "2,480,821.04")
    - Boolean operators (AND, OR, NOT)
    - Field-specific boosting
    - NO fuzzy matching (for accuracy)
    - NO phonetic matching (removed)
    - Configurable minimum match threshold
    """
    
    # Pattern to detect numeric values with formatting (commas, decimals, etc.)
    # Must contain at least one digit to avoid matching lone '.' or ','
    NUMERIC_PATTERN = re.compile(r'^[\d,.\s$€£¥]+$')
    NUMERIC_HAS_DIGIT = re.compile(r'\d')
    
    def __init__(self):
        self.config = get_config()
        self.mapping = self.config.indexing.mapping
        self.field_boost = self.mapping.get('field_boost', {})
        
        # Default field boosts for relevance ranking
        self.default_boosts = {
            'file_name': 3.0,
            'main_content': 2.0,
            'ocr_content': 1.5,
            'embedded_content': 1.0
        }
    
    def _is_numeric_query(self, query_text: str) -> bool:
        """Detect if query looks like a formatted number or numeric value.
        Must contain at least one digit — a lone '.' or ',' is not numeric."""
        clean = query_text.strip().strip('"')
        return bool(self.NUMERIC_PATTERN.match(clean) and self.NUMERIC_HAS_DIGIT.search(clean))

    def _parse_slash_command(self, query_text: str) -> Optional[Dict[str, str]]:
        """Parse slash search commands — only if backslash is followed by a
        recognized command keyword.  Bare backslash or backslash followed by
        unknown text is treated as a literal search (e.g., file paths)."""
        query_text = (query_text or "").strip()
        if not query_text.startswith("\\"):
            return None
        body = query_text[1:].strip()
        if not body:
            return None

        # --- Only recognize explicit command prefixes ---
        uid_match = re.match(r"^uid(?:[:=\s]+(.+))?$", body, flags=re.IGNORECASE)
        if uid_match:
            return {"mode": "uid", "value": (uid_match.group(1) or "").strip()}

        ext_match = re.match(r"^ext(?:[:=\s]+(.+))?$", body, flags=re.IGNORECASE)
        if ext_match:
            return {"mode": "ext", "value": (ext_match.group(1) or "").strip().lstrip(".")}

        # Smart-ID pattern  (e.g. \DOC-20260211-ABCD)
        if re.match(r"^[A-Za-z]{2,8}-\d{8}-[A-Za-z0-9]{4,}$", body):
            return {"mode": "uid", "value": body}

        # Shortcut for known file extensions ONLY when preceded by \
        if " " not in body:
            token = body.lower().lstrip(".")
            known_ext = {
                "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
                "txt", "csv", "json", "xml", "html", "htm",
                "jpg", "jpeg", "png", "gif", "bmp", "tiff", "zip",
            }
            if token in known_ext:
                return {"mode": "ext", "value": token}

        # Tag search ONLY for single alphanumeric words (no spaces, no path chars)
        if re.match(r"^[A-Za-z][A-Za-z0-9_-]{1,30}$", body):
            return {"mode": "tag", "value": body}

        # If none of above matched, this is NOT a command — return None so the
        # query is treated as a literal search (e.g., paths with backslashes).
        return None

    def _build_slash_query(self, slash_cmd: Dict[str, str]) -> Dict[str, Any]:
        """Build strict slash-command query."""
        mode = slash_cmd.get("mode", "")
        value = (slash_cmd.get("value") or "").strip()
        if not value:
            return {"query": {"match_none": {}}}

        if mode == "uid":
            return {
                "query": {
                    "bool": {
                        "should": [
                            {"term": {"smart_id": {"value": value, "boost": 20}}},
                            {"term": {"smart_id.text": {"value": value, "boost": 5}}},
                        ],
                        "minimum_should_match": 1,
                    }
                }
            }
        if mode == "ext":
            ext = value.lower().lstrip(".")
            mime_by_ext = {
                "pdf": "application/pdf",
                "doc": "application/msword",
                "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "xls": "application/vnd.ms-excel",
                "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "ppt": "application/vnd.ms-powerpoint",
                "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                "txt": "text/plain",
                "csv": "text/csv",
                "json": "application/json",
                "xml": "application/xml",
                "png": "image/png",
                "jpg": "image/jpeg",
                "jpeg": "image/jpeg",
            }
            return {
                "query": {
                    "bool": {
                        "should": [
                            {"term": {"file_type": {"value": ext, "boost": 20}}},
                            {"term": {"mime_type": {"value": mime_by_ext.get(ext, ext), "boost": 10}}},
                            {"wildcard": {"file_name.keyword": {"value": f"*.{ext}", "boost": 5}}},
                        ],
                        "minimum_should_match": 1,
                    }
                }
            }

        variants = list({value, value.lower(), value.title(), value.upper()})
        return {
            "query": {
                "bool": {
                    "should": [
                        {"terms": {"category": variants, "boost": 15}},
                        {"terms": {"department": variants, "boost": 15}},
                        {"terms": {"purpose": variants, "boost": 15}},
                        {"terms": {"dynamic_subtags": variants, "boost": 12}},
                        {"term": {"category.text": {"value": value, "boost": 5}}},
                        {"term": {"department.text": {"value": value, "boost": 5}}},
                        {"term": {"purpose.text": {"value": value, "boost": 5}}},
                        # Search in extended metadata (flattened field supports wildcard queries)
                        {"query_string": {
                            "query": value,
                            "fields": ["extended_metadata.*"],
                            "default_operator": "OR",
                            "boost": 10
                        }},
                    ],
                    "minimum_should_match": 1,
                }
            }
        }
    
    def build_search_query(
        self,
        query_text: str,
        fields: List[str],
        page: int = 1,
        size: int = 20,
        exact_match: bool = False,
        minimum_should_match: str = "75%"
    ) -> Dict[str, Any]:
        """
        Build ACCURATE search query - NO fuzzy/phonetic matching
        
        Args:
            query_text: Search query string
            fields: Fields to search
            page: Page number
            size: Results per page
            exact_match: If True, require exact phrase match
            minimum_should_match: Minimum % of terms that must match
            
        Returns:
            OpenSearch query DSL
        """
        # Check if query is quoted (exact phrase) or numeric value
        slash_cmd = self._parse_slash_command(query_text)
        if slash_cmd:
            query = self._build_slash_query(slash_cmd)
        else:
            # Detect if query contains path separators — search both original
            # and normalized (forward-slash) variants for better recall
            has_path_sep = '\\' in query_text or '/' in query_text

            is_phrase_query = (
                query_text.startswith('"') and query_text.endswith('"')
            ) or exact_match

            is_numeric = self._is_numeric_query(query_text)

            if is_phrase_query:
                # Exact phrase matching
                clean_query = query_text.strip('"')
                query = self._build_phrase_query(clean_query, fields)
            elif is_numeric:
                # Numeric/formatted value - use exact + analyzed matching
                clean_query = query_text.strip()
                query = self._build_numeric_query(clean_query, fields)
            elif has_path_sep:
                # Path-aware search: query with both backslash and forward-slash variants
                query = self._build_path_query(query_text, fields, minimum_should_match)
            else:
                # Multi-match with high accuracy settings
                query = self._build_accurate_query(query_text, fields, minimum_should_match)
        
        # Add highlighting
        query["highlight"] = {
            "pre_tags": ["<mark>"],
            "post_tags": ["</mark>"],
            "fields": {
                "main_content": {
                    "fragment_size": 200,
                    "number_of_fragments": 3,
                    "type": "unified"
                },
                "embedded_content": {
                    "fragment_size": 150,
                    "number_of_fragments": 2,
                    "type": "unified"
                },
                "ocr_content": {
                    "fragment_size": 150,
                    "number_of_fragments": 2,
                    "type": "unified"
                },
                "file_name": {
                    "fragment_size": 100,
                    "number_of_fragments": 1,
                    "type": "unified"
                }
            }
        }
        
        return query
    
    def _build_phrase_query(self, query_text: str, fields: List[str]) -> Dict[str, Any]:
        """Build exact phrase match query"""
        should_clauses = []
        
        for field in fields:
            boost = self.field_boost.get(field, self.default_boosts.get(field, 1.0))
            should_clauses.append({
                "match_phrase": {
                    field: {
                        "query": query_text,
                        "boost": boost,
                        "slop": 0  # Exact phrase, no word gaps
                    }
                }
            })
        
        return {
            "query": {
                "bool": {
                    "should": should_clauses,
                    "minimum_should_match": 1
                }
            }
        }
    
    def _build_numeric_query(self, query_text: str, fields: List[str]) -> Dict[str, Any]:
        """
        Build query for numeric/formatted values with exact matching.
        Uses .keyword subfield for exact match (high boost) + analyzed field for flexibility.
        Also searches dot/comma-swapped variants for number format normalization.
        """
        should_clauses = []

        # Generate normalized variants (dot<->comma swap, stripped)
        variants = [query_text]
        if '.' in query_text:
            variants.append(query_text.replace('.', ','))
        if ',' in query_text:
            variants.append(query_text.replace(',', '.'))
        stripped = re.sub(r'(?<=\d)[.,](?=\d)', '', query_text)
        if stripped != query_text:
            variants.append(stripped)

        for variant in variants:
            boost_factor = 1.0 if variant == query_text else 0.8
            for field in fields:
                base_boost = self.field_boost.get(field, self.default_boosts.get(field, 1.0))

                if field in ['main_content', 'embedded_content', 'ocr_content']:
                    should_clauses.append({
                        "term": {
                            f"{field}.keyword": {
                                "value": variant,
                                "boost": base_boost * 10.0 * boost_factor
                            }
                        }
                    })

                should_clauses.append({
                    "match_phrase": {
                        field: {
                            "query": variant,
                            "boost": base_boost * boost_factor,
                            "slop": 0
                        }
                    }
                })

        return {
            "query": {
                "bool": {
                    "should": should_clauses,
                    "minimum_should_match": 1
                }
            }
        }
    
    def _build_path_query(
        self,
        query_text: str,
        fields: List[str],
        minimum_should_match: str = "75%"
    ) -> Dict[str, Any]:
        """Build a search query for path-like strings containing backslashes.

        EXCLUDES content fields — searches ONLY tagged/metadata fields:
        file_path, file_name, category, department, purpose, file_type, smart_id

        Searches:
        1. Exact keyword match on file_path (highest boost)
        2. Wildcard on file_path / file_name
        3. Tag field matches (category, department, purpose, etc.)
        """
        should_clauses = []
        fwd = query_text.replace("\\", "/")  # normalised variant

        # --- Exact keyword match on file_path ---
        should_clauses.append({
            "term": {"file_path": {"value": query_text, "boost": 20.0}}
        })
        should_clauses.append({
            "wildcard": {"file_path": {"value": f"*{query_text.replace(chr(92), chr(92)*2)}*", "boost": 10.0}}
        })
        # Wildcard on file_name
        fname_part = query_text.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        if fname_part:
            should_clauses.append({
                "wildcard": {"file_name.keyword": {"value": f"*{fname_part}*", "boost": 8.0}}
            })

        # --- ONLY search tagged/metadata fields, EXCLUDE content fields ---
        # Filter out content fields to match only tags/metadata
        metadata_fields = [
            'file_name', 'file_path', 'category', 'department', 
            'purpose', 'file_type', 'smart_id', 'dynamic_subtags'
        ]
        tag_fields = [f for f in fields if f in metadata_fields]

        # Search tag fields with both backslash and forward-slash variants
        for variant in {query_text, fwd}:
            for field in tag_fields:
                boost = self.field_boost.get(field, 1.0)
                should_clauses.append({
                    "match_phrase": {
                        field: {
                            "query": variant,
                            "boost": boost * 2.0,  # Boost tag matches
                            "slop": 1
                        }
                    }
                })
                # Also try as simple match (tokens may be split on slashes)
                should_clauses.append({
                    "match": {
                        field: {
                            "query": variant,
                            "boost": boost,
                            "operator": "and"
                        }
                    }
                })

        return {
            "query": {
                "bool": {
                    "should": should_clauses,
                    "minimum_should_match": 1
                }
            }
        }

    def _build_accurate_query(
        self,
        query_text: str,
        fields: List[str],
        minimum_should_match: str = "75%"
    ) -> Dict[str, Any]:
        """
        Build accurate multi-field query WITHOUT fuzzy matching
        FIXED: Exclude nested fields to prevent query errors
        FIXED: Handle long query text safely
        """
        # FIXED: Filter out nested fields (they cause "nested items" errors)
        # Nested fields require special query syntax and shouldn't be in multi_match
        excluded_fields = ['embedded_files', 'metadata']
        safe_fields = [f for f in fields if not any(f.startswith(ex) for ex in excluded_fields)]
        
        # FIXED: Truncate very long queries to prevent parsing errors
        # OpenSearch has limits on query length (~32KB)
        max_query_length = 5000  # Safe limit for query text
        if len(query_text) > max_query_length:
            query_text = query_text[:max_query_length]
            logger.warning(f"Query text truncated to {max_query_length} characters")
        
        # Apply field boosts
        boosted_fields = []
        for field in safe_fields:
            boost = self.field_boost.get(field, self.default_boosts.get(field, 1.0))
            if boost != 1.0:
                boosted_fields.append(f"{field}^{boost}")
            else:
                boosted_fields.append(field)
        
        # Use cross_fields for better multi-word matching across fields
        # NO fuzziness for accuracy
        return {
            "query": {
                "bool": {
                    "must": [
                        {
                            "multi_match": {
                                "query": query_text,
                                "fields": boosted_fields,
                                "type": "cross_fields",
                                "operator": "or",
                                "minimum_should_match": minimum_should_match
                                # NO fuzziness - exact word matching only
                            }
                        }
                    ],
                    "should": [
                        # Boost exact phrase matches
                        {
                            "multi_match": {
                                "query": query_text,
                                "fields": boosted_fields,
                                "type": "phrase",
                                "boost": 2.0
                            }
                        },
                        # Search in extended metadata (flattened field)
                        {
                            "query_string": {
                                "query": query_text,
                                "fields": ["extended_metadata.*"],
                                "default_operator": "OR",
                                "boost": 1.5
                            }
                        }
                    ]
                }
            }
        }
    
    def build_filter_query(
        self,
        query_text: str,
        filters: Dict[str, Any],
        fields: List[str],
        page: int = 1,
        size: int = 20
    ) -> Dict[str, Any]:
        """Build query with filters - ACCURATE matching"""
        # Apply field boosts
        boosted_fields = []
        for field in fields:
            boost = self.field_boost.get(field, self.default_boosts.get(field, 1.0))
            if boost != 1.0:
                boosted_fields.append(f"{field}^{boost}")
            else:
                boosted_fields.append(field)
        
        must_clauses = [
            {
                "multi_match": {
                    "query": query_text,
                    "fields": boosted_fields,
                    "type": "cross_fields",
                    "operator": "or",
                    "minimum_should_match": "75%"
                    # NO fuzziness for accuracy
                }
            }
        ]
        
        filter_clauses = []
        
        # Add filters
        for field, value in filters.items():
            if isinstance(value, list):
                filter_clauses.append({"terms": {field: value}})
            else:
                filter_clauses.append({"term": {field: value}})
        
        query = {
            "query": {
                "bool": {
                    "must": must_clauses,
                    "filter": filter_clauses
                }
            },
            "highlight": {
                "pre_tags": ["<mark>"],
                "post_tags": ["</mark>"],
                "fields": {
                    "main_content": {"fragment_size": 200, "number_of_fragments": 3},
                    "embedded_content": {"fragment_size": 150, "number_of_fragments": 2},
                    "ocr_content": {"fragment_size": 150, "number_of_fragments": 2}
                }
            }
        }
        
        return query
