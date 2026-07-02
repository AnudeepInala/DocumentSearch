"""
Taxonomy loader/manager for Excel-driven controlled tags.
"""

from __future__ import annotations

import hashlib
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from core.config_manager import get_config
from core.logging_manager import get_logger
from core.reporting_manager import record_taxonomy_version

from .tagging_models import TaxonomyRow

logger = get_logger("tagging.taxonomy")


def _norm(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def _parse_bool(value: str) -> bool:
    # T10: Empty string handled as explicit default (True = active by default)
    # rather than being in the truthy set, for clarity
    raw = str(value or "").strip().lower()
    if not raw:
        return True  # default: active when cell is blank
    if raw in {"1", "true", "yes", "y", "active"}:
        return True
    if raw in {"0", "false", "no", "n", "inactive"}:
        return False
    return True


def _split_csv(value: str) -> List[str]:
    txt = str(value or "")
    parts = txt.replace("|", ",").replace(";", ",").split(",")
    return [p.strip() for p in parts if p and p.strip()]


@dataclass
class TaxonomySnapshot:
    version_id: str
    checksum: str
    source_file: str
    loaded_at: float
    rows_by_field: Dict[str, List[TaxonomyRow]]
    alias_map: Dict[str, Dict[str, str]]
    defaults: Dict[str, str]
    feedback_boost: Dict[str, Dict[str, float]]


class TaxonomyManager:
    REQUIRED_FIELD_SHEETS = ("category", "department", "purpose")
    REQUIRED_FIELD_COLUMNS = ("label", "aliases", "keywords", "active", "priority")

    def __init__(self, source_path: Optional[str] = None) -> None:
        cfg = get_config()
        tagging_cfg = getattr(cfg, "tagging", None)
        default_path = getattr(
            tagging_cfg,
            "taxonomy_path",
            str(Path(cfg.paths.working_root) / "taxonomy" / "master_taxonomy.xlsx"),
        )
        self.source_path = Path(source_path or default_path)
        self.hot_reload_seconds = max(3, int(getattr(tagging_cfg, "hot_reload_seconds", 30) or 30))
        self._snapshot: Optional[TaxonomySnapshot] = None
        self._last_check = 0.0
        self._last_mtime = 0.0
        # T11: Thread lock for hot-reload safety
        self._reload_lock = threading.Lock()

    def ensure_loaded(self) -> TaxonomySnapshot:
        now = time.time()
        # Fast path: no lock needed if snapshot is fresh
        if self._snapshot and (now - self._last_check) < self.hot_reload_seconds:
            return self._snapshot

        # T11: Lock the reload path so concurrent workers don't corrupt state
        with self._reload_lock:
            # Double-check after acquiring lock
            if self._snapshot and (time.time() - self._last_check) < self.hot_reload_seconds:
                return self._snapshot

            self._last_check = time.time()
            try:
                current_mtime = self.source_path.stat().st_mtime if self.source_path.exists() else 0.0
            except Exception:
                current_mtime = 0.0

            if self._snapshot and current_mtime == self._last_mtime:
                return self._snapshot

            return self._load_taxonomy()

    def _load_taxonomy(self) -> TaxonomySnapshot:
        if not self.source_path.exists():
            logger.warning("Taxonomy file not found at %s; using builtin fallback taxonomy", self.source_path)
            snapshot = self._build_builtin()
            self._snapshot = snapshot
            self._last_mtime = 0.0
            return snapshot

        import pandas as pd

        workbook = pd.read_excel(self.source_path, sheet_name=None, dtype=str)
        missing_sheets = [s for s in self.REQUIRED_FIELD_SHEETS if s not in workbook]
        if missing_sheets:
            raise ValueError(f"Taxonomy workbook missing sheets: {', '.join(missing_sheets)}")
        if "defaults" not in workbook:
            raise ValueError("Taxonomy workbook missing required sheet: defaults")

        rows_by_field: Dict[str, List[TaxonomyRow]] = {}
        alias_map: Dict[str, Dict[str, str]] = {}
        defaults: Dict[str, str] = {}

        for field in self.REQUIRED_FIELD_SHEETS:
            sheet = workbook[field]
            columns = {str(c).strip().lower() for c in sheet.columns}
            missing_cols = [c for c in self.REQUIRED_FIELD_COLUMNS if c not in columns]
            if missing_cols:
                raise ValueError(f"Sheet '{field}' missing columns: {', '.join(missing_cols)}")

            # normalize column names
            normalized = sheet.rename(columns={c: str(c).strip().lower() for c in sheet.columns})
            field_rows: List[TaxonomyRow] = []
            field_alias_map: Dict[str, str] = {}

            for _, row in normalized.fillna("").iterrows():
                label = str(row.get("label", "")).strip()
                if not label:
                    continue

                aliases = _split_csv(str(row.get("aliases", "")))
                keywords = _split_csv(str(row.get("keywords", "")))
                active = _parse_bool(str(row.get("active", "true")))
                try:
                    priority = int(str(row.get("priority", "0") or "0"))
                except ValueError:
                    priority = 0

                tax_row = TaxonomyRow(
                    field=field,
                    label=label,
                    aliases=aliases,
                    keywords=keywords,
                    active=active,
                    priority=priority,
                )
                field_rows.append(tax_row)

                if active:
                    for alias in [label, *aliases]:
                        normalized_alias = _norm(alias)
                        if normalized_alias:
                            field_alias_map[normalized_alias] = label

            if not field_rows:
                raise ValueError(f"Sheet '{field}' has no usable taxonomy rows")

            # Validate: warn about duplicate labels and empty keywords
            seen_labels: set = set()
            for row in field_rows:
                norm_label = _norm(row.label)
                if norm_label in seen_labels:
                    logger.warning("Taxonomy sheet '%s' has duplicate label: '%s'", field, row.label)
                seen_labels.add(norm_label)
                if row.active and not row.keywords:
                    logger.warning("Taxonomy sheet '%s' label '%s' has no keywords — scoring will rely on alias/semantic only", field, row.label)

            rows_by_field[field] = field_rows
            alias_map[field] = field_alias_map

        # Defaults parser (supports either {field, fallback_label} rows or one-row wide format)
        defaults_sheet = workbook["defaults"].rename(columns={c: str(c).strip().lower() for c in workbook["defaults"].columns})
        defaults_rows = defaults_sheet.fillna("")

        if {"field", "fallback_label"}.issubset(set(defaults_rows.columns)):
            for _, row in defaults_rows.iterrows():
                field = str(row.get("field", "")).strip().lower()
                fallback = str(row.get("fallback_label", "")).strip()
                if field in self.REQUIRED_FIELD_SHEETS and fallback:
                    defaults[field] = fallback
        else:
            first_row = defaults_rows.iloc[0] if not defaults_rows.empty else None
            if first_row is not None:
                for field in self.REQUIRED_FIELD_SHEETS:
                    value = str(first_row.get(field, "")).strip()
                    if value:
                        defaults[field] = value

        # aliases sheet (optional)
        if "aliases" in workbook:
            alias_sheet = workbook["aliases"].rename(columns={c: str(c).strip().lower() for c in workbook["aliases"].columns})
            if {"field", "label", "aliases"}.issubset(set(alias_sheet.columns)):
                for _, row in alias_sheet.fillna("").iterrows():
                    field = str(row.get("field", "")).strip().lower()
                    label = str(row.get("label", "")).strip()
                    if field not in alias_map or not label:
                        continue
                    for alias in _split_csv(str(row.get("aliases", ""))):
                        normalized_alias = _norm(alias)
                        if normalized_alias:
                            alias_map[field][normalized_alias] = label

        # enforce defaults for every mandatory field
        for field in self.REQUIRED_FIELD_SHEETS:
            if not defaults.get(field):
                first_active = next((r.label for r in rows_by_field[field] if r.active), "")
                defaults[field] = first_active or "General"

        checksum = hashlib.sha1(self.source_path.read_bytes()).hexdigest()
        version_id = f"tax-{time.strftime('%Y%m%d')}-{checksum[:8]}"
        feedback_boost = self._load_feedback_weights()
        snapshot = TaxonomySnapshot(
            version_id=version_id,
            checksum=checksum,
            source_file=str(self.source_path),
            loaded_at=time.time(),
            rows_by_field=rows_by_field,
            alias_map=alias_map,
            defaults=defaults,
            feedback_boost=feedback_boost,
        )

        record_taxonomy_version(
            version_id=version_id,
            source_file=str(self.source_path),
            checksum=checksum,
            status="loaded",
            notes="taxonomy loaded",
        )
        self._snapshot = snapshot
        self._last_mtime = self.source_path.stat().st_mtime
        logger.info("Loaded taxonomy version %s from %s", version_id, self.source_path)
        return snapshot

    def _build_builtin(self) -> TaxonomySnapshot:
        rows_by_field: Dict[str, List[TaxonomyRow]] = {
            "category": [
                TaxonomyRow("category", "General", ["general"], ["document", "file"], True, 1),
                TaxonomyRow("category", "Invoice", ["invoice", "bill"], ["invoice", "amount", "payment"], True, 3),
                TaxonomyRow("category", "Contract", ["contract", "agreement"], ["contract", "agreement", "term"], True, 2),
            ],
            "department": [
                TaxonomyRow("department", "Operations", ["ops"], ["process", "system", "operation"], True, 1),
                TaxonomyRow("department", "Finance", ["fin"], ["finance", "payment", "amount", "invoice"], True, 3),
                TaxonomyRow("department", "HR", ["human resources"], ["employee", "candidate", "leave"], True, 2),
            ],
            "purpose": [
                TaxonomyRow("purpose", "Reference", ["reference"], ["record", "reference", "information"], True, 1),
                TaxonomyRow("purpose", "Payment", ["payment"], ["payment", "due", "invoice"], True, 3),
                TaxonomyRow("purpose", "Policy", ["policy"], ["policy", "guideline", "procedure"], True, 2),
            ],
        }
        alias_map: Dict[str, Dict[str, str]] = {}
        for field, rows in rows_by_field.items():
            alias_map[field] = {}
            for row in rows:
                for alias in [row.label, *row.aliases]:
                    normalized = _norm(alias)
                    if normalized:
                        alias_map[field][normalized] = row.label

        snapshot = TaxonomySnapshot(
            version_id="tax-builtin-v1",
            checksum="builtin",
            source_file="builtin",
            loaded_at=time.time(),
            rows_by_field=rows_by_field,
            alias_map=alias_map,
            defaults={"category": "General", "department": "Operations", "purpose": "Reference"},
            feedback_boost=self._load_feedback_weights(),
        )
        record_taxonomy_version(
            version_id=snapshot.version_id,
            source_file="builtin",
            checksum="builtin",
            status="fallback",
            notes="taxonomy fallback used",
        )
        return snapshot

    @staticmethod
    def _load_feedback_weights() -> Dict[str, Dict[str, float]]:
        from core.reporting_manager import get_tag_feedback_weights

        try:
            return get_tag_feedback_weights()
        except Exception:
            return {}

    def get_snapshot(self) -> TaxonomySnapshot:
        return self.ensure_loaded()

    def get_field_rows(self, field: str) -> List[TaxonomyRow]:
        snap = self.ensure_loaded()
        return list(snap.rows_by_field.get(field, []))

    def get_default(self, field: str) -> str:
        snap = self.ensure_loaded()
        return str(snap.defaults.get(field, "General") or "General")

    def get_alias_label(self, field: str, alias: str) -> str:
        snap = self.ensure_loaded()
        return str(snap.alias_map.get(field, {}).get(_norm(alias), ""))

    def canonicalize_label(self, field: str, value: str) -> str:
        """Return canonical active taxonomy label for a field, or empty string."""
        txt = str(value or "").strip()
        if not txt:
            return ""

        snap = self.ensure_loaded()
        alias_hit = snap.alias_map.get(field, {}).get(_norm(txt), "")
        if alias_hit:
            return str(alias_hit)

        for row in snap.rows_by_field.get(field, []):
            if row.active and _norm(row.label) == _norm(txt):
                return row.label
        return ""

    def is_valid_label(self, field: str, value: str) -> bool:
        return bool(self.canonicalize_label(field, value))

    def get_feedback_boost(self, field: str, label: str) -> float:
        snap = self.ensure_loaded()
        return float(snap.feedback_boost.get(field, {}).get(label, 0.0) or 0.0)

    def get_version(self) -> Tuple[str, str]:
        snap = self.ensure_loaded()
        return snap.version_id, snap.checksum


_taxonomy_lock = threading.Lock()
_taxonomy_manager: Optional[TaxonomyManager] = None


def get_taxonomy_manager() -> TaxonomyManager:
    global _taxonomy_manager
    if _taxonomy_manager is None:
        with _taxonomy_lock:
            if _taxonomy_manager is None:
                _taxonomy_manager = TaxonomyManager()
    return _taxonomy_manager

