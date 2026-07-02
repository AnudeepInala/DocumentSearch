"""
Tagging data models.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class TaxonomyRow:
    field: str
    label: str
    aliases: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    active: bool = True
    priority: int = 0


@dataclass
class FieldConfidence:
    label: str
    score: float
    reasons: List[str] = field(default_factory=list)
    used_fallback: bool = False
    source: str = "model"


@dataclass
class TaggingRequest:
    file_id: Optional[int] = None
    file_path: str = ""
    file_name: str = ""
    file_hash: str = ""
    doc_id: str = ""
    file_type: str = ""
    mime_type: str = ""
    main_content: str = ""
    ocr_content: str = ""
    embedded_content: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TaggingResult:
    # Legacy fields (backward-compatible aliases for the 12-dimension system)
    category: str = ""
    department: str = ""
    purpose: str = ""
    file_type: str = ""

    # ===== 12 Core Taxonomy Dimensions (Content-Driven, Sheet 3 Enforced) =====
    metadata_level_code: str = "File"
    record_class_name: str = "Undefined"
    record_category_name_functional: str = ""
    record_category_name_transactional: str = ""
    record_type_code: str = ""
    business_unit_name: str = ""
    sub_business_unit_name: str = ""
    iso_country_code: str = ""
    record_format_name: str = "Electronic"
    original_record_location_type_name: str = "Shared Drive"
    data_classification_name: str = "GE Internal"
    divestiture_deal_name: str = ""

    # NLP Enrichments & Outputs
    dynamic_subtags: List[str] = field(default_factory=list)
    key_names: List[str] = field(default_factory=list)
    amount_found: str = ""
    important_dates: List[str] = field(default_factory=list)
    location_mentioned: List[str] = field(default_factory=list)
    confidentiality: str = ""

    # Process & Audit fields
    tagging_status: str = "final"
    review_required: bool = False
    tagger_version: str = ""
    taxonomy_version: str = ""
    tag_confidence_overall: float = 0.0
    tag_confidence_by_field: Dict[str, Any] = field(default_factory=dict)
    metadata_mode: str = ""
    metadata_source: str = ""
    extended_metadata: Dict[str, str] = field(default_factory=dict)  # Dynamic metadata fields

    # Provenance fields
    constraint_source: str = ""
    forced_flag: bool = False
    original_labels: Dict[str, str] = field(default_factory=dict)
    original_scores: Dict[str, float] = field(default_factory=dict)
    match_mode: Dict[str, str] = field(default_factory=dict)
    constraint_version: str = ""

    def to_document_update(self) -> Dict[str, Any]:
        return {
            # Legacy backward-compatible fields
            "category": self.category,
            "department": self.department,
            "purpose": self.purpose,
            "file_type": self.file_type,
            "confidentiality": self.confidentiality,
            # 12 Core Taxonomy Dimensions
            "metadata_level_code": self.metadata_level_code,
            "record_class_name": self.record_class_name,
            "record_category_name_functional": self.record_category_name_functional,
            "record_category_name_transactional": self.record_category_name_transactional,
            "record_type_code": self.record_type_code,
            "business_unit_name": self.business_unit_name,
            "sub_business_unit_name": self.sub_business_unit_name,
            "iso_country_code": self.iso_country_code,
            "record_format_name": self.record_format_name,
            "original_record_location_type_name": self.original_record_location_type_name,
            "data_classification_name": self.data_classification_name,
            "divestiture_deal_name": self.divestiture_deal_name,
            # NLP enrichments
            "dynamic_subtags": self.dynamic_subtags,
            "key_names": self.key_names,
            "amount_found": self.amount_found,
            "important_dates": self.important_dates,
            "location_mentioned": self.location_mentioned,
            # Process & audit
            "tagging_status": self.tagging_status,
            "review_required": bool(self.review_required),
            "tagger_version": self.tagger_version,
            "taxonomy_version": self.taxonomy_version,
            "tag_confidence_overall": float(self.tag_confidence_overall or 0.0),
            "tag_confidence_by_field": self.tag_confidence_by_field,
            "tag_confidence": float(self.tag_confidence_overall or 0.0),
            "metadata_mode": self.metadata_mode,
            "metadata_source": self.metadata_source,
            "extended_metadata": self.extended_metadata,
            # Provenance
            "constraint_source": self.constraint_source,
            "forced_flag": bool(self.forced_flag),
            "original_labels": self.original_labels,
            "original_scores": self.original_scores,
            "match_mode": self.match_mode,
            "constraint_version": self.constraint_version,
        }


@dataclass
class ReviewDecision:
    file_key: str
    smart_id: str
    field_name: str
    old_value: str
    new_value: str
    actor: str = "user"
    reason: str = ""
    event_time: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)

