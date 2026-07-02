"""
Local hybrid tagging engine (rules + taxonomy semantics + optional spaCy NER).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from core.config_manager import get_config
from core.logging_manager import get_logger
from core.reporting_manager import normalize_file_type

from .metadata_manager import get_metadata_manager
from .tagging_models import FieldConfidence, TaggingRequest, TaggingResult, TaxonomyRow
from .taxonomy_manager import get_taxonomy_manager

logger = get_logger("tagging.engine")

_TOKEN_RE = re.compile(r"[a-z0-9]{2,}")
_MONEY_RE = re.compile(r"(?i)(?:INR|Rs\.?|USD|EUR|GBP|₹|\$|€|£)\s?\d[\d,]*(?:\.\d+)?")
_DATE_RE = re.compile(
    r"(?i)\b(?:\d{4}[-/]\d{1,2}[-/]\d{1,2}|\d{1,2}[-/]\d{1,2}[-/]\d{2,4}|"
    r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2},?\s+\d{2,4})\b"
)
# Filter: file system paths / extensions that should NOT be NER entities
_PATH_FRAGMENT_RE = re.compile(
    r"[a-z]:\\|[\\/]users[\\/]|[\\/]home[\\/]|\\\\|[\\/]{2,}"
    r"|\.(?:exe|dll|py|txt|pdf|docx|xlsx|csv|json|xml|html|htm|pptx|ppt|doc|xls)\b",
    re.IGNORECASE,
)
# T1: Word-boundary patterns for confidentiality — avoids "not confidential" / "unrestricted"
_CONFIDENTIAL_RE = re.compile(r"\b(?:strictly\s+)?confidential\b", re.IGNORECASE)
_INTERNAL_RE = re.compile(r"\b(?:internal\s+(?:use|only)|restricted)\b", re.IGNORECASE)
_NOT_PREFIX_RE = re.compile(r"\bnot\s+confidential\b", re.IGNORECASE)
_UNRESTRICTED_RE = re.compile(r"\bunrestricted\b", re.IGNORECASE)

# T2: Maximum theoretical raw score for normalization
# Fix #8: SpaCy is now primary brain with higher weights
_MAX_RAW_SCORE = 2.0  # semantic(0.55) + entity_align(0.20) + noun_chunk(0.40) + alias(0.15) + keyword(0.10) + label_overlap(0.15) + feedback(0.20)
_EXT_TO_MIME = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "doc": "application/msword",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "xls": "application/vnd.ms-excel",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "ppt": "application/vnd.ms-powerpoint",
    "txt": "text/plain",
    "csv": "text/csv",
    "json": "application/json",
    "xml": "application/xml",
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
}
# T5: Explicit reverse map — dict comprehension lost "jpg" since both jpg/jpeg map to image/jpeg
_MIME_TO_EXT = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-excel": "xls",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "application/vnd.ms-powerpoint": "ppt",
    "text/plain": "txt",
    "text/csv": "csv",
    "application/json": "json",
    "application/xml": "xml",
    "image/png": "png",
    "image/jpeg": "jpg",
}


def _norm(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


FIELD_CONSTRAINT_MAP = {
    "category":        "record_category_name_functional",
    "department":      "business_unit_name",
    "confidentiality": "data_classification_name",
    # 12-Dimension direct mappings
    "metadata_level_code": "metadata_level_code",
    "record_class_name": "record_class_name",
    "record_category_name_functional": "record_category_name_functional",
    "record_category_name_transactional": "record_category_name_transactional",
    "record_type_code": "record_type_code",
    "business_unit_name": "business_unit_name",
    "sub_business_unit_name": "sub_business_unit_name",
    "iso_country_code": "iso_country_code",
    "record_format_name": "record_format_name",
    "original_record_location_type_name": "original_record_location_type_name",
    "data_classification_name": "data_classification_name",
    "divestiture_deal_name": "divestiture_deal_name",
}

# ===== 12-Dimension Lookup Tables =====

_COUNTRY_NAME_TO_ISO: Dict[str, str] = {
    "united states": "USA", "usa": "USA", "u.s.a.": "USA", "u.s.": "USA", "america": "USA",
    "united kingdom": "GBR", "great britain": "GBR", "uk": "GBR", "england": "GBR",
    "germany": "DEU", "deutschland": "DEU", "france": "FRA", "india": "IND", "china": "CHN",
    "japan": "JPN", "canada": "CAN", "australia": "AUS", "brazil": "BRA", "mexico": "MEX",
    "italy": "ITA", "spain": "ESP", "netherlands": "NLD", "holland": "NLD",
    "switzerland": "CHE", "sweden": "SWE", "norway": "NOR", "denmark": "DNK", "finland": "FIN",
    "ireland": "IRL", "singapore": "SGP", "hong kong": "HKG",
    "south korea": "KOR", "korea": "KOR", "taiwan": "TWN", "indonesia": "IDN",
    "malaysia": "MYS", "thailand": "THA", "philippines": "PHL", "vietnam": "VNM",
    "south africa": "ZAF", "nigeria": "NGA", "egypt": "EGY", "saudi arabia": "SAU",
    "uae": "ARE", "united arab emirates": "ARE", "israel": "ISR", "turkey": "TUR",
    "russia": "RUS", "poland": "POL", "belgium": "BEL", "austria": "AUT", "portugal": "PRT",
    "czech republic": "CZE", "czechia": "CZE", "romania": "ROU", "hungary": "HUN",
    "greece": "GRC", "argentina": "ARG", "chile": "CHL", "colombia": "COL", "peru": "PER",
    "new zealand": "NZL", "luxembourg": "LUX", "bermuda": "BMU", "cayman islands": "CYM",
    "puerto rico": "PRI", "scotland": "GBR", "wales": "GBR",
}

_CURRENCY_TO_COUNTRY: Dict[str, str] = {
    "USD": "USA", "$": "USA", "GBP": "GBR", "£": "GBR", "INR": "IND", "₹": "IND",
    "JPY": "JPN", "¥": "JPN", "CAD": "CAN", "AUD": "AUS", "CHF": "CHE",
    "SGD": "SGP", "HKD": "HKG", "BRL": "BRA", "MXN": "MEX", "ZAR": "ZAF",
    "KRW": "KOR", "SEK": "SWE", "NOK": "NOR", "DKK": "DNK", "PLN": "POL",
}

_BU_KEYWORD_MAP: Dict[str, List[str]] = {
    "Treasury": ["treasury", "cash management", "liquidity", "fx", "foreign exchange",
                  "hedging", "debt", "capital markets", "funding", "interest rate swap",
                  "money market", "bond", "fixed income", "debt issuance"],
    "Americas": ["americas", "north america", "south america", "latin america",
                  "latam", "us operations", "american operations", "north american"],
    "Real Estate": ["real estate", "real estate property", "physical property", "commercial real estate", "cre",
                     "lease", "tenant", "mortgage", "appraisal", "landlord",
                     "building management", "property management", "real property"],
    "GE Capital International": ["ge capital international", "geci", "international operations",
                                  "emea", "apac", "asia pacific", "european operations",
                                  "global operations", "international lending"],
    "GECC HQ": ["gecc", "ge capital", "headquarters", "corporate", "enterprise",
                 "general electric capital", "ge corporate", "corporate headquarters"],
}

_RECORD_TYPE_CONCEPT_MAP: Dict[str, List[str]] = {
    "FIN210": ["ledger", "general ledger", "chart of accounts", "trial balance", "accounting"],
    "FIN200": ["journal entry", "journal entries", "posting"],
    "FIN100": ["accounts payable", "ap ", "invoice payable", "payable invoice"],
    "FIN110": ["accounts receivable", "ar ", "invoice receivable", "receivable invoice"],
    "TAX140": ["tax", "tax return", "vat", "gst", "withholding", "1099", "w-2", "tax filing", "excise", "duty", "tariff"],
    "HR120": ["employee file", "personnel file", "employee record"],
    "HR160": ["payroll", "salary", "compensation planning", "wages"],
    "HR180": ["employee", "recruitment", "termination", "onboarding", "performance review", "human resources", "hr "],
    "LEG120": ["contract", "agreement", "nda", "memorandum of understanding", "mou", "service agreement", "master agreement", "covenant"],
    "AUD110": ["audit", "internal audit", "sox", "control testing", "audit finding", "audit observation", "remediation", "sox audit"],
    "INS120": ["insurance", "policy", "claim", "premium", "underwriting", "actuarial", "loss ratio", "reinsurance"],
    "FIN180": ["treasury", "cash management", "liquidity", "hedging", "debt management", "capital markets", "fx ", "foreign exchange", "money market"],
    "LEG160": ["legal advice", "legal opinion", "attorney-client", "legal memorandum", "legal memo", "counsel opinion"],
    "REG140": ["regulatory", "compliance", "examination", "supervisory", "consent order", "regulatory filing"],
    "RSK120": ["risk assessment", "credit risk", "operational risk", "risk management", "exposure", "mitigation"],
    "FIN160": ["financial report", "financial statement", "balance sheet", "income statement", "profit and loss", "p&l"],
    "CAD150": ["correspondence", "letter", "memo", "memorandum", "communication"],
    "CAD180": ["policy", "procedure", "guideline", "standard operating", "sop"],
    "CAD100": ["report", "analysis", "summary", "review", "findings"],
    "CAD120": ["meeting", "minutes", "agenda", "action items", "board meeting"],
    "CAD190": ["project", "project plan", "milestone", "deliverable", "timeline", "project proposal"],
    "SOU110": ["vendor", "supplier", "procurement", "purchase order", "rfp", "request for proposal"],
    "CAD230": ["customer", "client", "account holder", "borrower", "counterparty"],
    "MKT100": ["marketing", "campaign", "brand", "advertising", "market research"],
    "TEC100": ["information technology", "system", "application", "server", "database", "network", "software", "infrastructure", "it "],
    "FM110": ["facility", "building", "maintenance", "hvac", "office space", "space planning", "security"],
    "RIM100": ["record", "retention", "disposition", "archive", "records management", "information governance"],
    "CAD110": ["budget", "budget proposal", "budgeting", "forecast", "spending"],
    "TEC120": ["technical specification", "tech spec", "user manual", "training materials", "software development", "sdlc", "specification"],
    "BDV110": ["product development", "business strategy", "roadmap", "strategic planning"],
    "CAD210": ["training records", "course materials", "training guide"],
}

# Map Record Type Code prefixes to their parent functional category
_RECORD_TYPE_TO_CATEGORY: Dict[str, str] = {
    "FIN": "Finance & Accounting",
    "TAX": "Tax",
    "HR": "Human Resources",
    "LEG": "Legal",
    "AUD": "Audit & Controls",
    "INS": "Insurance",
    "REG": "Regulatory & Compliance",
    "RSK": "Risk Management",
    "CAD": "Corporate Administration",
    "SOU": "Sourcing & Procurement",
    "MKT": "Marketing",
    "TEC": "Information Technology",
    "FM": "Facilities Management",
    "RIM": "Records & Information Management",
    "BDV": "Business Development",
}

# Pre-compile data classification regexes for performance
_SPII_RE = re.compile(r"\bSPII\b|\bsensitive\s+personal", re.IGNORECASE)
_GE_RESTRICTED_RE = re.compile(r"\brestricted\b(?!\s+(?:to|from|access))", re.IGNORECASE)
_GE_CONFIDENTIAL_RE = re.compile(r"\b(?:strictly\s+)?confidential\b|\bGE\s+confidential\b", re.IGNORECASE)
_GE_INTERNAL_RE = re.compile(r"\b(?:internal\s+(?:use|only))\b|\bGE\s+internal\b", re.IGNORECASE)
_GE_PUBLIC_RE = re.compile(r"\bpublic\b|\bunrestricted\b", re.IGNORECASE)
_EMAIL_HEADER_RE = re.compile(r"(?:^|\n)\s*(?:From|To|Subject|Date|Sent|Cc)\s*:", re.IGNORECASE)

class TaggingEngine:
    def __init__(self) -> None:
        cfg = get_config()
        self.cfg = getattr(cfg, "tagging", None)
        self._nlp_cfg = getattr(cfg, "nlp", None)
        self._max_text_length = int(getattr(self._nlp_cfg, "max_text_length", 500000) or 500000)
        self.review_threshold = float(getattr(self.cfg, "review_threshold", 0.75) or 0.75)
        self.tagger_version = str(getattr(self.cfg, "tagger_version", "local-hybrid-v1") or "local-hybrid-v1")
        self.metadata_mode_enabled = bool(getattr(self.cfg, "metadata_mode_enabled", True))
        self.strict_spacy_when_no_metadata = bool(getattr(self.cfg, "strict_spacy_when_no_metadata", True))
        self.taxonomy = get_taxonomy_manager()
        self.metadata = get_metadata_manager()
        self._spacy_nlp = self._try_load_spacy()
        self._label_vector_cache: Dict[str, Any] = {}

    @staticmethod
    def _try_load_spacy():
        try:
            import spacy

            cfg = get_config()
            model_name = getattr(getattr(cfg, "nlp", None), "model_path", "en_core_web_sm")
            return spacy.load(model_name)
        except Exception:
            return None

    @staticmethod
    def _tokens(text: str) -> Set[str]:
        return set(_TOKEN_RE.findall((text or "").lower()))

    @staticmethod
    def _contains_phrase(full_text: str, phrase: str) -> bool:
        p = _norm(phrase)
        if not p:
            return False
        # Expand common separators (underscores, hyphens, path seps) to spaces
        # so word boundaries work correctly for filenames like "budget_operations"
        expanded = full_text.replace('_', ' ').replace('\\', ' ').replace('/', ' ')
        expanded = ' '.join(expanded.split())
        return bool(re.search(r'\b' + re.escape(p) + r'\b', expanded))

    def _file_type(self, req: TaggingRequest) -> str:
        ext = normalize_file_type(req.file_type, req.file_name, req.file_path)
        if ext:
            return ext
        mime = (req.mime_type or "").strip().lower()
        if mime in _MIME_TO_EXT:
            return _MIME_TO_EXT[mime]
        if "/" in mime:
            guessed = mime.split("/")[-1]
            if guessed:
                return guessed
        return "unknown"

    def _get_spacy_doc(self, text: str, max_chars: int = 0) -> Any:
        """Get or create a spaCy doc, truncating to max_chars (default: config limit)."""
        if not self._spacy_nlp or not text:
            return None
        limit = max_chars if max_chars > 0 else self._max_text_length
        try:
            return self._spacy_nlp(text[:limit])
        except Exception:
            return None

    def _semantic_similarity(self, doc_text: str, label_text: str, *, precomputed_doc: Any = None) -> float:
        if not self._spacy_nlp or not doc_text or not label_text:
            return 0.0
        try:
            doc = precomputed_doc if precomputed_doc is not None else self._get_spacy_doc(doc_text)
            if doc is None:
                return 0.0
            cache_key = label_text[:512]
            if cache_key not in self._label_vector_cache:
                self._label_vector_cache[cache_key] = self._spacy_nlp(label_text)
            label_doc = self._label_vector_cache[cache_key]
            score = float(doc.similarity(label_doc))
            return max(0.0, min(score, 1.0))
        except Exception:
            return 0.0

    def _score_field(
        self,
        *,
        field: str,
        rows: List[TaxonomyRow],
        alias_map: Dict[str, str],
        full_text: str,
        file_context: str,
        tokens: Set[str],
        spacy_doc: Any = None,
    ) -> FieldConfidence:
        candidates: Dict[str, Dict[str, Any]] = {}
        for row in rows:
            if not row.active:
                continue
            candidates[row.label] = {
                "score": 0.0,
                "priority": row.priority,
                "reasons": [],
                "keywords": row.keywords,
                "aliases": row.aliases,
            }

        has_spacy = self._spacy_nlp is not None and spacy_doc is not None

        # ===== PRIMARY SIGNAL: spaCy Rich Semantic Similarity (Fix #8A) =====
        for row in rows:
            if not row.active or row.label not in candidates:
                continue
            # Build FULL semantic description (not truncated)
            semantic_text = (
                f"{row.label}. Related terms: {', '.join(row.aliases)}. "
                f"Keywords: {', '.join(row.keywords)}."
            )
            sim = self._semantic_similarity(full_text, semantic_text, precomputed_doc=spacy_doc)
            if sim > 0:
                weight = 0.30 if has_spacy else 0.20  # Semantic is noisy; keyword matching is primary
                candidates[row.label]["score"] += weight * sim
                candidates[row.label]["reasons"].append(f"semantic:{sim:.2f}")

        # ===== PRIMARY SIGNAL: spaCy Contextualized Noun-Chunk Similarity (Fix #8B) =====
        if spacy_doc is not None:
            try:
                doc_chunks = list(spacy_doc.noun_chunks)
                for row in rows:
                    if not row.active or row.label not in candidates:
                        continue
                    # Use SpaCy vector similarity between chunks and label (not substring)
                    cache_key = f"label:{row.label}"
                    if cache_key not in self._label_vector_cache:
                        self._label_vector_cache[cache_key] = self._spacy_nlp(row.label)
                    label_doc = self._label_vector_cache[cache_key]
                    chunk_hits = 0
                    for chunk in doc_chunks:
                        if len(chunk.text.strip()) < 3:
                            continue
                        try:
                            sim = chunk.similarity(label_doc)
                            if sim > 0.70:  # Tighter semantic threshold
                                chunk_hits += 1
                        except Exception:
                            pass
                    if chunk_hits > 0:
                        chunk_score = min(chunk_hits * 0.08, 0.25)
                        candidates[row.label]["score"] += chunk_score
                        candidates[row.label]["reasons"].append(f"semantic_chunks:{chunk_hits}")
            except Exception:
                pass

        # ===== PRIMARY SIGNAL: SpaCy Entity-Category Alignment (Fix #8C) =====
        if spacy_doc is not None:
            try:
                entity_labels = {ent.label_ for ent in spacy_doc.ents}
                if entity_labels:
                    # Entity-category alignment: use DISCRIMINATIVE entity sets
                    # that are specific to a department, not generic (ORG/PERSON/DATE
                    # appear in ALL documents and must not boost any department).
                    entity_profile: Dict[str, Set[str]] = {
                        "Finance": {"MONEY", "PERCENT"},
                        "Legal": {"LAW"},
                        "Compliance": {"LAW"},
                    }
                    for category, relevant_ents in entity_profile.items():
                        overlap = entity_labels & relevant_ents
                        if overlap and category in candidates:
                            boost = min(len(overlap) * 0.10, 0.20)
                            candidates[category]["score"] += boost
                            candidates[category]["reasons"].append(
                                f"entity_alignment:{','.join(sorted(overlap))}"
                            )
            except Exception:
                pass

        # ===== PRIMARY: alias/label phrase matching (strongest signal) =====
        alias_weight_full = 0.30  # Alias phrases in doc body are high-quality signals
        alias_weight_path = 0.20  # Alias phrases in file path are also strong
        _alias_full_hit: set = set()
        _alias_path_hit: set = set()
        for alias, label in alias_map.items():
            if not label or label not in candidates:
                continue
            if label not in _alias_full_hit and self._contains_phrase(full_text, alias):
                candidates[label]["score"] += alias_weight_full
                candidates[label]["reasons"].append(f"alias:{alias}")
                _alias_full_hit.add(label)
            if label not in _alias_path_hit and self._contains_phrase(file_context, alias):
                candidates[label]["score"] += alias_weight_path
                candidates[label]["reasons"].append(f"path_alias:{alias}")
                _alias_path_hit.add(label)

        # ===== PRIMARY: keyword token matching (strongest classification signal) =====
        kw_weight = 0.35  # Keywords are the most reliable classification signal
        for row in rows:
            if not row.active or row.label not in candidates:
                continue
            if not row.keywords:
                continue
            hit_count = 0
            for keyword in row.keywords:
                if len(keyword.strip()) < 3:
                    continue
                if self._contains_phrase(full_text, keyword):
                    hit_count += 1
            if hit_count:
                coverage = hit_count / max(len(row.keywords), 1)
                candidates[row.label]["score"] += kw_weight * coverage
                candidates[row.label]["reasons"].append(f"keyword_hits:{hit_count}")

        # ===== FALLBACK: label token overlap =====
        for row in rows:
            if not row.active or row.label not in candidates:
                continue
            label_tokens = self._tokens(row.label)
            if label_tokens and label_tokens.intersection(tokens):
                overlap = len(label_tokens.intersection(tokens)) / len(label_tokens)
                candidates[row.label]["score"] += 0.15 * overlap
                candidates[row.label]["reasons"].append("label_token_overlap")

        # ===== BOOST: feedback from prior accepted corrections =====
        for row in rows:
            if not row.active or row.label not in candidates:
                continue
            fb = self.taxonomy.get_feedback_boost(field, row.label)
            if fb > 0:
                candidates[row.label]["score"] += min(fb, 0.20)
                candidates[row.label]["reasons"].append(f"feedback_boost:{fb:.2f}")

        if not candidates:
            return FieldConfidence(label="", score=0.0, reasons=["no_candidates"])

        ranked = sorted(
            candidates.items(),
            key=lambda kv: (-kv[1]["score"], -int(kv[1]["priority"]), kv[0].lower()),
        )
        best_label, best_meta = ranked[0]
        raw = float(best_meta["score"])

        # Fix #8D: Anti-hallucination gap check
        if len(ranked) > 1:
            runner_up_score = ranked[1][1]["score"]
            gap = raw - runner_up_score
            if gap < 0.05 and raw < 0.30:
                best_meta["reasons"].append("ambiguous_tie")
                # Force low score so tag() method triggers fallback
                raw = min(raw, 0.15)

        # Fix #8G: Cap confidence when SpaCy is unavailable
        if self._spacy_nlp is None:
            raw = min(raw, 1.20)  # Cap to ~60% of _MAX_RAW_SCORE
            best_meta["reasons"].append("no_spacy_degraded")

        score = float(max(0.0, min(raw / _MAX_RAW_SCORE, 0.99)))
        return FieldConfidence(label=best_label, score=score, reasons=best_meta["reasons"])

    @staticmethod
    def _is_garbage_entity(val: str) -> bool:
        """Filter out garbage entities: financial codes, column headers, numeric IDs, etc."""
        if not val or len(val) < 2:
            return True
        # Too long — likely a sentence fragment or multi-column header
        if len(val) > 80:
            return True
        # Mostly digits/special chars — not a real entity
        alpha_chars = sum(1 for c in val if c.isalpha())
        if alpha_chars < 2:
            return True
        # Ratio check: if more than 60% non-alpha, it's likely a code/ID
        if len(val) > 4 and alpha_chars / len(val) < 0.4:
            return True
        # Pure uppercase short codes (e.g., "DFL", "MLP", "TLM", "NCA", "GL", "COA")
        stripped = val.strip()
        if len(stripped) <= 4 and stripped.isupper() and stripped.isalpha():
            return True
        # Financial/accounting codes pattern (e.g., "BSEE00B31P", "CCAC8CFC6P")
        if re.match(r'^[A-Z0-9]{6,}$', stripped):
            return True
        # Negative numbers or pure numbers with dots/dashes/commas
        if re.match(r'^-?\d[\d.,\-]*$', stripped):
            return True
        # Comma-formatted large numbers used as fake entities (e.g., "74,564,570", "48,242,305")
        if re.match(r'^\d{1,3}(?:,\d{3})+$', stripped):
            return True
        # Scientific notation numbers (e.g., "1.30793E11", "2.45E-09")
        if re.match(r'^-?\d+\.?\d*[Ee][+\-]?\d+$', stripped):
            return True
        # Excel/spreadsheet cell references (e.g., "col_start=$B$24:$B$24", "A1:D10")
        if re.match(r'^(?:col|row)_|\$[A-Z]+\$?\d+|^[A-Z]+\d+:[A-Z]+\d+$', stripped):
            return True
        # Long strings with '=' or ':' signs (Excel/formula fragments)
        if '=' in stripped and sum(1 for c in stripped if c.isdigit()) > 3:
            return True
        # Column header garbage — extended list
        _COLUMN_HEADERS = re.compile(
            r'^(?:List|Text|Debit|Credit|Balance|Total|Amount|Date|Type|Rate|'
            r'Line|Description|Context|Messages|Conversion|Ledger|Journal|'
            r'Account|Accounts|Transaction|Source|Trans|Pages|Document|'
            r'Record|Entry|Entries|Posting|Period|Fiscal|Budget|Forecast|'
            r'Actual|Variance|Currency|Exchange|Adjustment|Accrual|'
            r'Invoice|Receipt|Payment|Deposit|Withdrawal|Transfer|'
            r'Principal|Interest|Fee|Charge|Discount|Rebate|'
            r'Opening|Closing|Beginning|Ending|Sub|Total|Grand|'
            r'Net|Gross|Tax|Rate|Code|Ref|Reference|Number|No\.|'
            r'Colomn|Column|Row|Sheet|Tab|Page|Name|Value|Field)\b',
            re.IGNORECASE
        )
        if _COLUMN_HEADERS.match(stripped):
            return True
        # Strings that are multi-word column headers (all uppercase multi-word)
        words = stripped.split()
        if len(words) >= 3 and all(w.isupper() or not w[0].isalpha() for w in words if len(w) > 1):
            return True
        # Web page / URL fragments
        if re.search(r'\bWeb\s*Page\b|\bwww\.\b|\.com\b|\.org\b|\.net\b', stripped, re.IGNORECASE):
            return True
        # Path fragments (file system paths)
        if _PATH_FRAGMENT_RE.search(val):
            return True
        return False

    def _extract_entities(self, text: str, *, spacy_doc: Any = None) -> Tuple[List[str], str, List[str], List[str]]:
        """Extract key_names, amount_found, important_dates, locations.

        Uses spaCy NER as primary source + noun-chunk heuristics + regex fallbacks.
        Heavy filtering applied to avoid garbage from financial tables/spreadsheets.
        """
        key_names: List[str] = []
        amount_found = ""
        important_dates: List[str] = []
        locations: List[str] = []

        text = text or ""
        doc = spacy_doc if spacy_doc is not None else self._get_spacy_doc(text)

        # ---------- spaCy NER (primary) ----------
        if doc is not None:
            try:
                for ent in doc.ents:
                    val = ent.text.strip()
                    if not val:
                        continue
                    if ent.label_ in {"PERSON", "ORG", "NORP"}:
                        if self._is_garbage_entity(val):
                            continue
                        key_names.append(val)
                    elif ent.label_ == "MONEY":
                        # Extract the cleanest money value
                        clean_match = _MONEY_RE.search(val)
                        if clean_match:
                            candidate = clean_match.group(0)
                            # Prefer the largest/first valid amount
                            if not amount_found:
                                amount_found = candidate
                            else:
                                # Keep the one with a currency symbol
                                if re.search(r'[\$€£₹]', candidate) and not re.search(r'[\$€£₹]', amount_found):
                                    amount_found = candidate
                    elif ent.label_ == "DATE":
                        if _DATE_RE.search(val):
                            # Filter out bare years or very short date fragments
                            if len(val) >= 6:
                                important_dates.append(val)
                    elif ent.label_ in {"GPE", "LOC", "FAC"}:
                        if self._is_garbage_entity(val):
                            continue
                        # Additional location-specific filtering
                        # Common false positives in financial docs
                        loc_skip = {"debit", "credit", "balance", "total", "amount",
                                    "ledger", "journal", "account", "query", "support",
                                    "expense", "revenue", "accrual", "invoice"}
                        if val.lower() in loc_skip:
                            continue
                        locations.append(val)
            except Exception:
                pass

            # ---------- spaCy noun-chunk heuristics for names ----------
            try:
                _seen_names = {_norm(n) for n in key_names}
                for chunk in doc.noun_chunks:
                    chunk_text = chunk.text.strip()
                    if len(chunk_text) < 3 or self._is_garbage_entity(chunk_text):
                        continue
                    # Proper noun phrases (e.g., "John Smith", "Acme Corp")
                    root_pos = chunk.root.pos_
                    if root_pos == "PROPN" or (root_pos == "NOUN" and any(t.pos_ == "PROPN" for t in chunk)):
                        norm_chunk = _norm(chunk_text)
                        if norm_chunk not in _seen_names:
                            # Must have at least one capitalized word (not all-caps codes)
                            words = [w for w in chunk_text.split() if w]
                            has_proper_cap = any(
                                w[0].isupper() and not w.isupper() and len(w) > 2
                                for w in words
                            )
                            if has_proper_cap:
                                key_names.append(chunk_text)
                                _seen_names.add(norm_chunk)
            except Exception:
                pass

        # ---------- Regex & Scanner fallbacks ----------
        # Names: "Mr./Mrs./Dr./Prof. Firstname Lastname" pattern
        _name_re = re.compile(
            r"\b(?:Mr\.?|Mrs\.?|Ms\.?|Dr\.?|Prof\.?)\s+"
            r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3})\b"
        )
        for m in _name_re.finditer(text):
            name = m.group(1).strip()
            if name and _norm(name) not in {_norm(n) for n in key_names}:
                if not self._is_garbage_entity(name):
                    key_names.append(name)

        # Custom proper noun chunk scanner fallback for general key names
        _proper_re = re.compile(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,2})\b')
        _proper_skip = {
            "The", "This", "That", "Their", "These", "Those", "There", "Here",
            "Please", "Thank", "Note", "See", "Per", "Above", "Below",
            "Dear", "Our", "Your", "Page", "Section", "Item", "Total",
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
            "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday",
            "All", "Any", "Every", "Some", "No", "Not", "Only", "Also", "Just"
        }
        for m in _proper_re.finditer(text):
            proper = m.group(1).strip()
            first_word = proper.split()[0]
            if first_word not in _proper_skip and len(proper) >= 4:
                if _norm(proper) not in {_norm(n) for n in key_names}:
                    if not self._is_garbage_entity(proper):
                        key_names.append(proper)

        # Locations: common location patterns — "in <City>", "at <Place>", "from <Location>"
        _loc_re = re.compile(
            r"\b(?:in|at|from|near|to)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})\b"
        )
        _seen_locs = {_norm(l) for l in locations}
        _loc_skip_words = {
            "the", "this", "that", "their", "these", "those", "there", "here",
            "please", "thank", "note", "see", "per", "above", "below",
            "dear", "our", "your", "page", "section", "item", "total",
            "general", "order", "process", "detail", "accordance", "addition",
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
            "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
        }
        for m in _loc_re.finditer(text):
            loc = m.group(1).strip()
            if loc and _norm(loc) not in _seen_locs and len(loc) >= 3:
                if loc.lower().split()[0] not in _loc_skip_words:
                    if not self._is_garbage_entity(loc):
                        locations.append(loc)
                        _seen_locs.add(_norm(loc))

        # Custom dictionary-based country & city fallback scanner
        text_lower = text.lower()
        countries_list = sorted(list(_COUNTRY_NAME_TO_ISO.keys()), key=len, reverse=True)
        for country_name in countries_list:
            if len(country_name) >= 3 and re.search(r'\b' + re.escape(country_name) + r'\b', text_lower):
                cap_country = " ".join(w.capitalize() for w in country_name.split())
                if _norm(cap_country) not in _seen_locs:
                    locations.append(cap_country)
                    _seen_locs.add(_norm(cap_country))

        common_locs = {
            "london": "London", "tokyo": "Tokyo", "singapore": "Singapore", 
            "new york": "New York", "paris": "Paris", "berlin": "Berlin", 
            "hong kong": "Hong Kong", "chicago": "Chicago", "sydney": "Sydney",
            "uk": "United Kingdom", "usa": "United States", "us": "United States"
        }
        for city_lower, city_cap in common_locs.items():
            if re.search(r'\b' + re.escape(city_lower) + r'\b', text_lower):
                if _norm(city_cap) not in _seen_locs:
                    locations.append(city_cap)
                    _seen_locs.add(_norm(city_cap))

        # Money regex fallback — find ALL money patterns and pick the best one
        if not amount_found:
            money_matches = _MONEY_RE.findall(text)
            if money_matches:
                # Prefer amounts with explicit currency symbols over plain numbers
                for m in money_matches:
                    if re.search(r'[\$€£₹]', m):
                        amount_found = m
                        break
                if not amount_found:
                    amount_found = money_matches[0]

        # Date regex — always supplement
        regex_dates = _DATE_RE.findall(text)[:10]
        for d in regex_dates:
            if len(d) >= 6 and _norm(d) not in {_norm(x) for x in important_dates}:
                important_dates.append(d)

        # Cleanup + dedupe
        def _dedupe(items: List[str], limit: int = 10) -> List[str]:
            seen: set = set()
            out: List[str] = []
            for item in items:
                norm = _norm(item)
                if not norm or norm in seen:
                    continue
                # Final garbage filter
                if len(norm) < 2:
                    continue
                seen.add(norm)
                out.append(item.strip())
                if len(out) >= limit:
                    break
            return out

        return (
            _dedupe(key_names, limit=10),
            amount_found,
            _dedupe(important_dates, limit=5),
            _dedupe(locations, limit=5),
        )

    def _derive_confidentiality(self, full_text: str) -> str:
        # T1: Word-boundary regex avoids false-positives like "not confidential"
        text = full_text or ""
        # Check for negations first
        has_not_confidential = bool(_NOT_PREFIX_RE.search(text))
        has_unrestricted = bool(_UNRESTRICTED_RE.search(text))

        if _CONFIDENTIAL_RE.search(text) and not has_not_confidential:
            return "Confidential"
        if _INTERNAL_RE.search(text) and not has_unrestricted:
            return "Internal"
        return "Public"

    def _enforce_sheet3_constraint(
        self,
        field: str,
        candidate_label: str,
        candidate_score: float,
        allowed_set: Set[str],
    ) -> Tuple[str, float, str, bool, str]:
        """
        Enforce Sheet 3 allowed-values constraint on a candidate tag value.
        
        Returns:
            (final_label, final_score, constraint_source, forced_flag, match_mode)
        """
        from .metadata_manager import Sheet3ValidValuesRegistry
        
        if not candidate_label:
            return candidate_label, candidate_score, "sheet3_empty", False, "none"
            
        # 1. Exact match (case-insensitive) against the allowed set
        for allowed in allowed_set:
            if allowed.strip().lower() == candidate_label.strip().lower():
                return allowed, candidate_score, "sheet3_pass", False, "exact"
                
        # 2. Not in allowed set -> try to find the best match
        best_match = Sheet3ValidValuesRegistry.find_best_allowed_value(candidate_label, allowed_set)
        if best_match:
            # We forced it to a value in the allowed set!
            final_score = min(candidate_score, 0.75)
            return best_match, final_score, "sheet3_forced_best_match", True, "partial"
            
        # 3. No match found in allowed set -> we keep candidate, but it's not compliant
        return candidate_label, candidate_score, "sheet3_no_match", False, "none"

    # ===== 12-Dimension Content-Driven Classification Methods =====

    def _classify_data_classification(self, full_text: str) -> str:
        """GE-prefixed 5-level data classification from content. Highest sensitivity wins."""
        if _NOT_PREFIX_RE.search(full_text):
            pass  # "not confidential" — don't auto-classify as confidential
        if _SPII_RE.search(full_text):
            return "GE Confidential with SPII"
        if _GE_RESTRICTED_RE.search(full_text) and not _UNRESTRICTED_RE.search(full_text):
            return "GE Restricted"
        if _GE_CONFIDENTIAL_RE.search(full_text) and not _NOT_PREFIX_RE.search(full_text):
            return "GE Confidential"
        if _GE_INTERNAL_RE.search(full_text):
            return "GE Internal"
        if _GE_PUBLIC_RE.search(full_text):
            return "Public"
        return "GE Internal"  # conservative default

    def _classify_country(self, full_text: str, spacy_doc: Any = None) -> str:
        """Multi-layer country detection from content. Returns comma-separated ISO-2 codes."""
        from collections import Counter
        country_counts: Counter = Counter()

        # Layer 1: spaCy NER — GPE entities
        if spacy_doc is not None:
            try:
                for ent in spacy_doc.ents:
                    if ent.label_ == "GPE":
                        ent_text = ent.text.strip().lower()
                        iso = _COUNTRY_NAME_TO_ISO.get(ent_text, "")
                        if iso:
                            country_counts[iso] += 2  # NER gets double weight
            except Exception:
                pass

        # Layer 2: Explicit country name scanning
        text_lower = full_text.lower() if full_text else ""
        for name, iso in _COUNTRY_NAME_TO_ISO.items():
            # Word boundary match to avoid substring false positives
            try:
                if re.search(r'\b' + re.escape(name) + r'\b', text_lower):
                    country_counts[iso] += 1
            except Exception:
                pass

        # Layer 3: Currency-to-country inference
        for currency, iso in _CURRENCY_TO_COUNTRY.items():
            if len(currency) >= 3 and currency.upper() in full_text.upper():
                country_counts[iso] += 1

        if not country_counts:
            return "USA"

        # Return top countries (comma-separated if multiple strong signals)
        sorted_countries = country_counts.most_common(3)
        # Only include countries with significant mention count
        top_score = sorted_countries[0][1]
        significant = [code for code, count in sorted_countries if count >= max(top_score * 0.5, 1)]
        return ", ".join(significant[:3])

    def _classify_divestiture_deal(self, full_text: str, registry: Optional[Dict] = None) -> str:
        """Regex scan for divestiture deal names from Sheet 3 registry."""
        deal_names: List[str] = []
        if registry and "divestiture_deal_name" in registry:
            deal_names = list(registry["divestiture_deal_name"])

        if not deal_names:
            return ""

        matched_deals: List[str] = []
        text = full_text or ""
        for deal in deal_names:
            if not deal or len(deal) < 2:
                continue
            try:
                pattern = re.compile(r'\b' + re.escape(deal) + r'\b', re.IGNORECASE)
                if pattern.search(text):
                    matched_deals.append(deal)
            except Exception:
                if deal.lower() in text.lower():
                    matched_deals.append(deal)

        if not matched_deals:
            return ""
        return ", ".join(matched_deals[:3])

    def _classify_record_format(self, req: TaggingRequest) -> str:
        """Determine if record is Electronic or Physical based on file intrinsic properties."""
        has_main = bool((req.main_content or "").strip())
        has_ocr = bool((req.ocr_content or "").strip())

        # If only OCR content and no main text → likely a scanned document
        if has_ocr and not has_main:
            return "Physical"

        # Mime type hints
        mime = (req.mime_type or "").lower()
        if "image" in mime and has_ocr:
            return "Physical"

        return "Electronic"

    def _classify_location_type(self, full_text: str, file_path: str = "") -> str:
        """Content-based detection of original record location type."""
        # Email headers in content
        if _EMAIL_HEADER_RE.search(full_text or ""):
            return "Email"

        text_lower = (full_text or "").lower()
        path_lower = (file_path or "").lower()

        if "sharepoint" in text_lower or "sharepoint" in path_lower:
            return "SharePoint"
        if "box.com" in text_lower or "box.com" in path_lower:
            return "Box"
        # UNC path pattern (intrinsic file property, not folder default)
        if path_lower.startswith("\\\\") or "\\\\" in path_lower:
            return "Shared Drive"
        if "application" in text_lower and ("system" in text_lower or "platform" in text_lower):
            return "Application"

        return "Shared Drive"  # safe default for file-based systems

    def _classify_record_type_code(self, full_text: str) -> str:
        """Concept-to-code mapping for record type classification."""
        # Replace underscores with spaces so word boundary matches work on filenames
        text_lower = (full_text or "").lower().replace('_', ' ')
        code_scores: Dict[str, int] = {}

        for code, keywords in _RECORD_TYPE_CONCEPT_MAP.items():
            hits = 0
            for kw in keywords:
                kw_clean = kw.strip().lower()
                if not kw_clean:
                    continue
                # Enforce word boundary regex to prevent false matches (e.g. 'it' inside 'initial')
                pattern = r'\b' + re.escape(kw_clean) + r'\b'
                if re.search(pattern, text_lower):
                    hits += 1
            if hits > 0:
                code_scores[code] = hits

        if not code_scores:
            return ""

        # Return code with the most keyword hits
        best_code = max(code_scores, key=lambda k: code_scores[k])
        # Require at least 2 keyword hits for confidence
        if code_scores[best_code] >= 2:
            return best_code
        # With only 1 hit, still return but it may be less reliable
        return best_code

    def _classify_business_unit(self, full_text: str) -> str:
        """Classify business unit from content using weighted keyword scoring."""
        # Replace underscores with spaces so word boundary matches work on filenames
        text_lower = (full_text or "").lower().replace('_', ' ')
        bu_scores: Dict[str, float] = {}

        for bu, keywords in _BU_KEYWORD_MAP.items():
            score = 0.0
            for kw in keywords:
                kw_clean = kw.strip().lower()
                if not kw_clean:
                    continue
                # Enforce word boundary regex to prevent false matches (e.g. 'cre' inside 'increase')
                pattern = r'\b' + re.escape(kw_clean) + r'\b'
                if re.search(pattern, text_lower):
                    # Weight by keyword specificity (longer phrases = more specific)
                    specificity_weight = 1.0 + (len(kw_clean.split()) - 1) * 0.5
                    score += specificity_weight
            if score > 0:
                bu_scores[bu] = score

        if not bu_scores:
            return "GECC HQ"  # default only when NO content signals

        return max(bu_scores, key=lambda k: bu_scores[k])

    def _classify_sub_business_unit(self, full_text: str, parent_bu: str, registry: Optional[Dict] = None) -> str:
        """Hierarchical sub-BU matching scoped to parent BU."""
        if not registry or "sub_business_unit_name" not in registry:
            return ""

        all_sub_bus = list(registry["sub_business_unit_name"])
        if not all_sub_bus:
            return ""

        text_lower = (full_text or "").lower()
        matched: List[str] = []

        # Scoped sub-BUs for the parent BU
        pbu_clean = str(parent_bu or "").strip()
        
        # Helper to check if a sub-BU belongs to this parent BU
        def belongs_to_pbu(sub: str, pbu: str) -> bool:
            sub_lower = sub.lower()
            pbu_lower = pbu.lower()
            if pbu_lower == "gecc hq" and ("gecchq" in sub_lower or "corporate" in sub_lower or "hq" in sub_lower):
                return True
            if pbu_lower == "treasury" and "treasury" in sub_lower:
                return True
            if pbu_lower == "real estate" and "cre" in sub_lower:
                return True
            if pbu_lower == "americas" and ("geca-" in sub_lower or "americas" in sub_lower):
                return True
            if pbu_lower == "ge capital international" and ("geci" in sub_lower or "ge capital international" in sub_lower or "se asia" in sub_lower or "western europe" in sub_lower or "germany" in sub_lower or "france" in sub_lower or "italy" in sub_lower or "poland" in sub_lower or "uk" in sub_lower or "india" in sub_lower or "korea" in sub_lower or "japan" in sub_lower or "russia" in sub_lower or "romania" in sub_lower or "hungary" in sub_lower or "anz-" in sub_lower or "mubadala" in sub_lower):
                return True
            return False

        pbu_sub_bus = [s for s in all_sub_bus if belongs_to_pbu(s, pbu_clean)]
        if not pbu_sub_bus:
            # Fallback to all if scoping fails
            pbu_sub_bus = all_sub_bus

        for sub_bu in pbu_sub_bus:
            if not sub_bu or len(sub_bu) < 2:
                continue
            # Scan content for sub-BU name matches
            try:
                # If hyphenated, split and check main part
                clean_name = sub_bu
                if "-" in clean_name:
                    clean_name = clean_name.split("-")[-1].strip()
                if "Business Unit =" in clean_name or "Business Unite =" in clean_name:
                    continue
                if re.search(r'\b' + re.escape(clean_name.lower()) + r'\b', text_lower):
                    matched.append(sub_bu)
            except Exception:
                if sub_bu.lower() in text_lower:
                    matched.append(sub_bu)

        if matched:
            return matched[0]

        # Dynamic heuristic mapping if direct scan yielded no matches
        pbu = pbu_clean.lower()
        choice = ""
        if "gecc hq" in pbu:
            if any(w in text_lower for w in ["finance", "accounting", "fin & acct", "ledger", "journal", "p&l", "balance sheet"]):
                choice = "GECCHQ-Finance"
            elif any(w in text_lower for w in ["technology", "it", "software", "network", "server", "database", "infrastructure", "system"]):
                choice = "GECCHQ-IT"
            elif any(w in text_lower for w in ["legal", "litigation", "lawsuit", "counsel", "attorney", "contract", "agreement", "nda"]):
                choice = "GECCHQ-Legal"
            elif any(w in text_lower for w in ["sourcing", "procurement", "vendor", "supplier", "purchase order"]):
                choice = "GECCHQ-Sourcing"
            elif any(w in text_lower for w in ["security", "physical security", "cybersecurity", "threat"]):
                choice = "GECCHQ-Security"
            elif any(w in text_lower for w in ["hr", "human resources", "employee", "payroll", "personnel", "compensation"]):
                choice = "GECCHQ-Human Resources"
            elif "tax" in text_lower:
                choice = "GECCHQ-Tax"
            elif "audit" in text_lower or "sox" in text_lower:
                choice = "GECCHQ-Audit"
            elif "compliance" in text_lower or "regulatory" in text_lower:
                choice = "GECCHQ-Compliance"
            elif any(w in text_lower for w in ["risk", "credit risk", "market risk"]):
                choice = "GECCHQ-Risk Management"
            elif any(w in text_lower for w in ["facility", "facilities", "building", "office space"]):
                choice = "GECCHQ-Facilities Management"
            else:
                choice = "GECCHQ-Multiple"

        elif "treasury" in pbu:
            if any(w in text_lower for w in ["bank", "capital bank"]):
                choice = "Treasury-GE Capital Bank"
            else:
                choice = "Treasury-Multiple"

        elif "real estate" in pbu:
            choice = "CRE-CORE"

        elif "americas" in pbu:
            if "rail" in text_lower:
                choice = "GECA-Rail"
            elif "fleet" in text_lower:
                choice = "GECA-Fleet"
            elif "canada" in text_lower:
                choice = "GECA-Canada"
            elif "franchise" in text_lower:
                choice = "GECA-Franchise"
            elif "cdf" in text_lower:
                choice = "GECA-CDF"
            else:
                choice = "GECA-Multiple"

        elif "ge capital international" in pbu:
            if any(w in text_lower for w in ["france", "factocic"]):
                choice = "GE Capital France-FACTOCIC"
            elif "germany" in text_lower:
                choice = "GE Capital Germany-EF Germany"
            elif "italy" in text_lower:
                choice = "GE Capital Italy-EF Italy"
            elif "uk" in text_lower:
                choice = "GE Capital UK-EF UK"
            elif "poland" in text_lower:
                choice = "GE Capital Poland-Poland"
            elif "singapore" in text_lower:
                choice = "SE Asia-GCF Singapore"
            elif "japan" in text_lower:
                choice = "Japan-GCF Japan"
            elif "india" in text_lower:
                choice = "India-GCF India"
            else:
                choice = "GECI-Multiple"

        # Verify that our choice is actually in the allowed sub-BUs list!
        if choice and choice in all_sub_bus:
            return choice

        # Generic safe fallback scoped to parent BU
        for sub in all_sub_bus:
            if belongs_to_pbu(sub, pbu_clean):
                sub_lower = sub.lower()
                if "multiple" in sub_lower:
                    return sub
        
        # Second try: any sub-BU for this parent
        for sub in all_sub_bus:
            if belongs_to_pbu(sub, pbu_clean):
                return sub

        return pbu_sub_bus[0] if pbu_sub_bus else ""

    def _classify_12_dimensions(
        self,
        req: TaggingRequest,
        full_text: str,
        content_text: str,
        spacy_doc: Any,
        content_doc: Any,
        registry: Optional[Dict],
        selected: Dict[str, str],
    ) -> Dict[str, str]:
        """Orchestrator: classifies all 12 dimensions from content."""
        dims: Dict[str, str] = {}

        # Dim 1: metadata_level_code — always "File"
        dims["metadata_level_code"] = "File"

        # Dim 9: record_format_name
        dims["record_format_name"] = self._classify_record_format(req)

        # Dim 10: original_record_location_type_name
        dims["original_record_location_type_name"] = self._classify_location_type(
            full_text, req.file_path or ""
        )

        # Dim 11: data_classification_name
        dims["data_classification_name"] = self._classify_data_classification(full_text)

        # Dim 8: iso_country_code
        dims["iso_country_code"] = self._classify_country(full_text, spacy_doc=spacy_doc)

        # Dim 12: divestiture_deal_name
        dims["divestiture_deal_name"] = self._classify_divestiture_deal(full_text, registry)

        # Dim 5: record_type_code
        dims["record_type_code"] = self._classify_record_type_code(full_text)

        # Dim 6: business_unit_name
        dims["business_unit_name"] = self._classify_business_unit(full_text)

        # Dim 7: sub_business_unit_name (hierarchical, depends on dim 6)
        dims["sub_business_unit_name"] = self._classify_sub_business_unit(
            full_text, dims["business_unit_name"], registry
        )

        # Dims 3 & 4: record category — derive from Record Type Code, NOT from constraint-forced selected["category"]
        record_type = dims.get("record_type_code", "")
        functional_cat = ""
        if record_type:
            # Extract the alpha prefix (e.g., "FIN" from "FIN210", "HR" from "HR180")
            prefix = re.match(r'^([A-Z]+)', record_type)
            if prefix:
                functional_cat = _RECORD_TYPE_TO_CATEGORY.get(prefix.group(1), "")
        if not functional_cat:
            # Fallback: use the raw (pre-constraint) label if available
            functional_cat = selected.get("category", "Unclassified")
        dims["record_category_name_functional"] = functional_cat
        dims["record_category_name_transactional"] = ""

        # Dim 2: record_class_name — derived from which category won
        if functional_cat and functional_cat not in {"Unclassified", "General", ""}:
            dims["record_class_name"] = "Functional"
        else:
            dims["record_class_name"] = "Undefined"

        # Validate against Sheet 3 registry if available
        if registry:
            for dim_field, dim_value in dims.items():
                if not dim_value:
                    continue
                registry_key = FIELD_CONSTRAINT_MAP.get(dim_field, dim_field)
                if registry_key in registry:
                    allowed = registry[registry_key]
                    # For comma-separated values (country, deal), validate each part
                    if ", " in dim_value:
                        parts = [p.strip() for p in dim_value.split(",")]
                        valid_parts = [p for p in parts if p in allowed]
                        dims[dim_field] = ", ".join(valid_parts) if valid_parts else dim_value
                    else:
                        if dim_value not in allowed:
                            # Try case-insensitive match
                            match = next((v for v in allowed if v.lower() == dim_value.lower()), None)
                            if match:
                                dims[dim_field] = match

        return dims

    def tag(self, req: TaggingRequest) -> TaggingResult:
        snap = self.taxonomy.get_snapshot()
        metadata_ctx = self.metadata.resolve_tags(req) if self.metadata_mode_enabled else {
            "active": False,
            "mode": "spacy_only_mode",
            "source": "",
            "matched": False,
            "explicit": {},
            "derived": {},
            "reasons": ["metadata_mode_disabled"],
        }
        metadata_active = bool(metadata_ctx.get("active", False))
        file_name = req.file_name or Path(req.file_path or "").name
        file_context = _norm(f"{file_name} {req.file_path}")
        full_text = _norm(
            " ".join(
                [
                    file_name,
                    req.file_path or "",
                    req.main_content or "",
                    req.ocr_content or "",
                    req.embedded_content or "",
                ]
            )
        )
        tokens = self._tokens(full_text)

        # Fix #8H: Insufficient content guard — don't hallucinate tags for empty docs
        total_content_len = (
            len(req.main_content or "")
            + len(req.ocr_content or "")
            + len(req.embedded_content or "")
        )
        if total_content_len < 50 and not metadata_active:
            file_type = self._file_type(req) or "unknown"
            return TaggingResult(
                category="Unclassified",
                department="Unclassified",
                purpose="Unclassified",
                file_type=file_type,
                # 12-Dimension defaults for insufficient content
                metadata_level_code="File",
                record_class_name="Undefined",
                record_category_name_functional="Unclassified",
                record_category_name_transactional="",
                record_type_code=self._classify_record_type_code(full_text),
                business_unit_name=self._classify_business_unit(full_text) or "GECC HQ",
                sub_business_unit_name="",
                iso_country_code="",
                record_format_name=self._classify_record_format(req),
                original_record_location_type_name="Shared Drive",
                data_classification_name="GE Internal",
                divestiture_deal_name="",
                tagging_status="insufficient_content",
                review_required=True,
                tagger_version=self.tagger_version,
                taxonomy_version=snap.version_id,
                tag_confidence_overall=0.0,
                tag_confidence_by_field={
                    f: {
                        "label": "Unclassified" if f != "file_type" else file_type,
                        "score": 0.0,
                        "reasons": ["insufficient_content"],
                        "used_fallback": True,
                        "source": "deterministic_default",
                    }
                    for f in ("category", "department", "purpose", "file_type")
                },
                metadata_mode=str(metadata_ctx.get("mode", "spacy_only_mode") or "spacy_only_mode"),
                metadata_source=str(metadata_ctx.get("source", "") or ""),
                extended_metadata=dict(metadata_ctx.get("extended", {}) or {}),
            )

        # Pre-compute spaCy doc ONCE for reuse across scoring + entity extraction
        spacy_doc = self._get_spacy_doc(full_text)

        # FOR ENTITY EXTRACTION: content-only text (NO file path/name)
        # Prevents file paths from being misidentified as PERSON/DATE/GPE/MONEY
        # IMPORTANT: Do NOT use _norm() here — it lowercases, killing NER capitalization
        content_text = " ".join(
            (
                " ".join(
                    [
                        req.main_content or "",
                        req.ocr_content or "",
                        req.embedded_content or "",
                    ]
                )
            ).split()
        )
        content_doc = self._get_spacy_doc(content_text) if content_text else None

        confidences: Dict[str, FieldConfidence] = {}
        review_required = False
        selected: Dict[str, str] = {}
        strict_spacy_degraded = (
            (not metadata_active)
            and self.strict_spacy_when_no_metadata
            and self._spacy_nlp is None
        )
        if strict_spacy_degraded:
            review_required = True

        orig_labels: Dict[str, str] = {}
        orig_scores: Dict[str, float] = {}
        match_modes: Dict[str, str] = {}
        forced_any = False
        constraint_sources: List[str] = []

        snapshot_metadata = self.metadata.ensure_loaded()
        constraint_version = snapshot_metadata.checksum if snapshot_metadata else ""
        registry = snapshot_metadata.sheet3_allowed_values if snapshot_metadata else None

        for field in ("category", "department", "purpose"):
            score: Optional[FieldConfidence] = None

            # Priority 1: explicit metadata label
            explicit_label = str(metadata_ctx.get("explicit", {}).get(field, "") or "")
            canonical_explicit = self.taxonomy.canonicalize_label(field, explicit_label) if explicit_label else ""
            if canonical_explicit:
                score = FieldConfidence(
                    label=canonical_explicit,
                    score=0.99,
                    reasons=["metadata_explicit"],
                    used_fallback=False,
                    source="metadata_explicit",
                )
            elif explicit_label:
                score = FieldConfidence(
                    label=explicit_label,
                    score=0.97,
                    reasons=["metadata_explicit_raw"],
                    used_fallback=False,
                    source="metadata_explicit",
                )

            # Priority 2: derived metadata label
            if score is None:
                derived_label = str(metadata_ctx.get("derived", {}).get(field, "") or "")
                canonical_derived = self.taxonomy.canonicalize_label(field, derived_label) if derived_label else ""
                if canonical_derived:
                    score = FieldConfidence(
                        label=canonical_derived,
                        score=0.86,
                        reasons=["metadata_derived"],
                        used_fallback=False,
                        source="metadata_derived",
                    )
                elif derived_label:
                    score = FieldConfidence(
                        label=derived_label,
                        score=0.78,
                        reasons=["metadata_derived_raw"],
                        used_fallback=False,
                        source="metadata_derived",
                    )

            # Priority 3: model/spaCy classification
            if score is None:
                score = self._score_field(
                    field=field,
                    rows=snap.rows_by_field.get(field, []),
                    alias_map=snap.alias_map.get(field, {}),
                    full_text=full_text,
                    file_context=file_context,
                    tokens=tokens,
                    spacy_doc=spacy_doc,
                )
                score.source = "spacy_content_strict" if (not metadata_active and self._spacy_nlp is not None) else "model"

            if score.score < self.review_threshold or not score.label:
                if score.score > 0.10 and score.label:
                    # Has a signal but low confidence — keep best guess, flag for review
                    score.reasons.append("low_confidence_kept")
                    if strict_spacy_degraded:
                        score.reasons.append("spacy_unavailable_strict_mode")
                    review_required = True
                else:
                    # Truly no signal — use fallback
                    fallback = snap.defaults.get(field, "General")
                    score.used_fallback = True
                    score.reasons.append("fallback_default")
                    score.label = fallback
                    # T3: Set to fallback floor — don't inflate with the failed raw score
                    score.score = 0.35
                    score.source = "deterministic_default"
                    if strict_spacy_degraded:
                        score.reasons.append("spacy_unavailable_strict_mode")
                    review_required = True

            # Save pre-constraint values for audit trail
            pre_constraint_label = score.label
            pre_constraint_score = score.score

            # Enforce Sheet 3 constraint on selected field
            if registry and field in FIELD_CONSTRAINT_MAP:
                field_registry_key = FIELD_CONSTRAINT_MAP[field]
                if field_registry_key in registry:
                    allowed_set = registry[field_registry_key]
                    orig_labels[field] = pre_constraint_label
                    orig_scores[field] = pre_constraint_score
                    
                    final_label, final_score, constraint_src, forced, mm = self._enforce_sheet3_constraint(
                        field, pre_constraint_label, pre_constraint_score, allowed_set
                    )
                    score.label = final_label
                    score.score = final_score
                    match_modes[field] = mm
                    constraint_sources.append(constraint_src)
                    if forced:
                        forced_any = True
                        review_required = True
                        score.reasons.append("sheet3_forced_best_match")
                    score.reasons.append(f"sheet3_constraint:{constraint_src}")

            selected[field] = score.label
            confidences[field] = score

        file_type = self._file_type(req)
        if not file_type:
            file_type = "unknown"
            review_required = True
        confidences["file_type"] = FieldConfidence(
            label=file_type,
            score=0.99 if file_type != "unknown" else 0.3,
            reasons=["deterministic_file_type" if file_type != "unknown" else "file_type_unknown"],
            used_fallback=(file_type == "unknown"),
            source="deterministic_file_type" if file_type != "unknown" else "deterministic_default",
        )
        if file_type == "unknown":
            review_required = True

        # optional enrichment — use content-only text (no file path/name contamination)
        key_names, amount_found, important_dates, locations = self._extract_entities(content_text, spacy_doc=content_doc)

        # Fix #8E: Cross-field consistency check
        consistency_issues = self._check_cross_field_consistency(selected, confidences)
        if consistency_issues:
            review_required = True
            weakest = min(
                (f for f in ("category", "department", "purpose") if f in confidences),
                key=lambda f: confidences[f].score,
                default=None,
            )
            if weakest:
                for issue in consistency_issues:
                    confidences[weakest].reasons.append(f"consistency:{issue}")

        # Fix #8D: Dynamic subtags via SpaCy + taxonomy (replaces hardcoded 7-word list)
        subtags: Set[str] = set()
        # 1. Pull matched taxonomy keywords/aliases from scoring results
        for field, confidence in confidences.items():
            if field == "file_type":
                continue
            for reason in confidence.reasons:
                if reason.startswith("alias:"):
                    subtags.add(reason.split(":", 1)[1].strip().lower())
        # 2. Pull taxonomy keywords that appear in the document content
        for field_rows in snap.rows_by_field.values():
            for row in field_rows:
                if not row.active:
                    continue
                for kw in row.keywords:
                    if len(subtags) >= 15:
                        break
                    if len(kw) >= 3 and self._contains_phrase(content_text, kw):
                        subtags.add(kw.lower())
        # 3. Pull SpaCy noun-chunks that are domain-relevant
        if content_doc is not None:
            try:
                for chunk in content_doc.noun_chunks:
                    if len(subtags) >= 15:
                        break
                    chunk_text = chunk.text.strip().lower()
                    if 3 <= len(chunk_text) <= 40 and chunk.root.pos_ in {"NOUN", "PROPN"}:
                        # Only add if chunk is semantically close to ANY cached label
                        for label_doc in self._label_vector_cache.values():
                            try:
                                if chunk.similarity(label_doc) > 0.50:
                                    subtags.add(chunk_text)
                                    break
                            except Exception:
                                pass
            except Exception:
                pass

        field_scores = [confidences["category"].score, confidences["department"].score, confidences["purpose"].score]
        overall = float(sum(field_scores) / max(len(field_scores), 1))
        tagging_status = "review_required" if review_required else "final"

        # Derive confidentiality and enforce Sheet 3 constraint
        derived_confidentiality = self._derive_confidentiality(full_text)
        final_confidentiality = derived_confidentiality
        
        if registry and "confidentiality" in FIELD_CONSTRAINT_MAP:
            field_registry_key = FIELD_CONSTRAINT_MAP["confidentiality"]
            if field_registry_key in registry:
                allowed_set = registry[field_registry_key]
                orig_labels["confidentiality"] = derived_confidentiality
                orig_scores["confidentiality"] = 0.90
                
                final_conf, final_score_conf, constraint_src_conf, forced_conf, mm_conf = self._enforce_sheet3_constraint(
                    "confidentiality", derived_confidentiality, 0.90, allowed_set
                )
                final_confidentiality = final_conf
                match_modes["confidentiality"] = mm_conf
                constraint_sources.append(constraint_src_conf)
                if forced_conf:
                    forced_any = True
                    review_required = True
                    tagging_status = "review_required"

        # Determine overall constraint source
        if forced_any:
            if "sheet3_no_match" in constraint_sources:
                overall_constraint_source = "sheet3_no_match"
            else:
                overall_constraint_source = "sheet3_forced_best_match"
        elif "sheet3_pass" in constraint_sources:
            overall_constraint_source = "sheet3_pass"
        else:
            overall_constraint_source = "no_constraint"
        # ===== 12-Dimension Content Classification =====
        dims = self._classify_12_dimensions(
            req=req,
            full_text=full_text,
            content_text=content_text,
            spacy_doc=spacy_doc,
            content_doc=content_doc,
            registry=registry,
            selected=selected,
        )

        # Sync backward-compatible legacy fields with new dimensions
        final_category = dims.get("record_category_name_functional") or selected["category"]
        final_department = dims.get("business_unit_name") or selected["department"]

        # Check if deal was found — flag for review if multiple deals
        deal_value = dims.get("divestiture_deal_name", "")
        if ", " in deal_value:
            review_required = True
            tagging_status = "review_required"

        return TaggingResult(
            # Legacy backward-compatible fields
            category=final_category,
            department=final_department,
            purpose=selected["purpose"],
            file_type=file_type,
            # 12 Core Taxonomy Dimensions
            metadata_level_code=dims.get("metadata_level_code", "File"),
            record_class_name=dims.get("record_class_name", "Undefined"),
            record_category_name_functional=dims.get("record_category_name_functional", ""),
            record_category_name_transactional=dims.get("record_category_name_transactional", ""),
            record_type_code=dims.get("record_type_code", ""),
            business_unit_name=dims.get("business_unit_name", ""),
            sub_business_unit_name=dims.get("sub_business_unit_name", ""),
            iso_country_code=dims.get("iso_country_code", ""),
            record_format_name=dims.get("record_format_name", "Electronic"),
            original_record_location_type_name=dims.get("original_record_location_type_name", "Shared Drive"),
            data_classification_name=dims.get("data_classification_name", "GE Internal"),
            divestiture_deal_name=dims.get("divestiture_deal_name", ""),
            # NLP enrichments
            dynamic_subtags=sorted(subtags)[:15],
            key_names=key_names,
            amount_found=amount_found,
            important_dates=important_dates,
            location_mentioned=locations,
            confidentiality=final_confidentiality,
            # Process & audit
            tagging_status=tagging_status,
            review_required=review_required,
            tagger_version=self.tagger_version,
            taxonomy_version=snap.version_id,
            tag_confidence_overall=overall,
            tag_confidence_by_field={
                key: {
                    "label": value.label,
                    "score": round(float(value.score), 4),
                    "reasons": value.reasons,
                    "used_fallback": bool(value.used_fallback),
                    "source": str(value.source or "model"),
                }
                for key, value in confidences.items()
            },
            metadata_mode=str(metadata_ctx.get("mode", "spacy_only_mode") or "spacy_only_mode"),
            metadata_source=str(metadata_ctx.get("source", "") or ""),
            extended_metadata=dict(metadata_ctx.get("extended", {}) or {}),
            # Provenance
            constraint_source=overall_constraint_source,
            forced_flag=forced_any,
            original_labels=orig_labels,
            original_scores=orig_scores,
            match_mode=match_modes,
            constraint_version=constraint_version,
        )

    def _check_cross_field_consistency(
        self, selected: Dict[str, str], confidences: Dict[str, FieldConfidence]
    ) -> List[str]:
        """Fix #8E: Detect incoherent field combinations that indicate hallucination."""
        issues: List[str] = []
        core_fields = ["category", "department", "purpose"]
        # If ALL 3 core fields fell back to defaults, flag as garbage
        all_fallback = all(
            confidences.get(f, FieldConfidence("", 0)).used_fallback for f in core_fields
        )
        if all_fallback:
            issues.append("all_fields_fallback")
        return issues
