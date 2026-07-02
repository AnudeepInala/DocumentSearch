"""Tagging package — hybrid semantic + rule-based document classification."""

from .tagging_engine import TaggingEngine
from .tagging_models import FieldConfidence, TaggingRequest, TaggingResult, ReviewDecision, TaxonomyRow
from .tagging_worker import TaggingWorker
from .taxonomy_manager import TaxonomyManager, get_taxonomy_manager

__all__ = [
    "TaggingEngine",
    "TaggingWorker",
    "TaxonomyManager",
    "get_taxonomy_manager",
    "TaggingRequest",
    "TaggingResult",
    "FieldConfidence",
    "ReviewDecision",
    "TaxonomyRow",
]
