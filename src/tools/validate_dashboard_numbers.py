"""
Validate every dashboard number against underlying data sources.

Outputs:
- Console summary
- Markdown report with metric-by-metric context and source mapping
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import redis
import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.config_manager import get_config  # noqa: E402
from core.queue_manager import get_queue_manager  # noqa: E402
from core.reporting_manager import get_live_feed  # noqa: E402


@dataclass
class CheckResult:
    metric: str
    dashboard_value: Any
    source_value: Any
    source_context: str
    status: str
    note: str = ""


def safe_int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    try:
        return int(value)
    except Exception:
        return default


def safe_float(value: Any, default: float = 0.0) -> float:
    if value is None:
        return default
    try:
        return float(value)
    except Exception:
        return default


def extract_summary(queue_stats: Dict[str, Any]) -> Dict[str, int]:
    discovery = queue_stats.get("discovery", {}) or {}
    extraction_total_stats = queue_stats.get("extraction_total", {}) or {}
    indexing = queue_stats.get("indexing", {}) or {}
    ocr = queue_stats.get("ocr", {}) or {}
    tagging = queue_stats.get("tagging", {}) or {}
    completed = queue_stats.get("completed", {}) or {}

    extraction_pending = safe_int(extraction_total_stats.get("pending"))
    extraction_processing = safe_int(extraction_total_stats.get("processing"))
    extraction_completed = safe_int(extraction_total_stats.get("completed"))
    extraction_total = safe_int(extraction_total_stats.get("total"))
    if extraction_total == 0:
        extraction_total = extraction_pending + extraction_processing + extraction_completed

    indexing_pending = safe_int(indexing.get("pending"))
    indexing_processing = safe_int(indexing.get("processing"))
    indexing_completed = safe_int(indexing.get("completed"))
    indexing_total = safe_int(indexing.get("total"))
    if indexing_total == 0:
        indexing_total = indexing_pending + indexing_processing + indexing_completed

    ocr_pending = safe_int(ocr.get("pending"))
    ocr_processing = safe_int(ocr.get("processing"))
    ocr_completed = safe_int(ocr.get("completed"))
    ocr_total = safe_int(ocr.get("total"))
    if ocr_total == 0:
        ocr_total = ocr_pending + ocr_processing + ocr_completed

    tagging_pending = safe_int(tagging.get("pending"))
    tagging_processing = safe_int(tagging.get("processing"))
    tagging_completed = safe_int(tagging.get("completed"))
    tagging_total = safe_int(tagging.get("total"))
    if tagging_total == 0:
        tagging_total = tagging_pending + tagging_processing + tagging_completed

    return {
        "discovered_total": safe_int(discovery.get("total")),
        "discovery_pending": safe_int(discovery.get("pending")),
        "discovery_completed": safe_int(discovery.get("completed")),
        "extraction_pending": extraction_pending,
        "extraction_processing": extraction_processing,
        "extraction_completed": extraction_completed,
        "extraction_total": extraction_total,
        "indexing_pending": indexing_pending,
        "indexing_processing": indexing_processing,
        "indexing_completed": indexing_completed,
        "indexing_total": indexing_total,
        "ocr_pending": ocr_pending,
        "ocr_processing": ocr_processing,
        "ocr_completed": ocr_completed,
        "ocr_total": ocr_total,
        "tagging_pending": tagging_pending,
        "tagging_processing": tagging_processing,
        "tagging_completed": tagging_completed,
        "tagging_total": tagging_total,
        "completed_total": safe_int(completed.get("total_completed") or completed.get("total")),
        "duplicates": safe_int(completed.get("duplicates")),
        "avg_extraction_ms": safe_int(completed.get("avg_extraction_ms") or completed.get("avg_extraction_time_ms")),
        "avg_indexing_ms": safe_int(completed.get("avg_indexing_ms") or completed.get("avg_indexing_time_ms")),
        "total_failures": safe_int(queue_stats.get("total_failures")),
    }


def eq_with_tolerance(metric: str, a: float, b: float) -> bool:
    # Dynamic pipeline counters can move during sampling.
    dynamic_prefixes = (
        "extraction_",
        "indexing_",
        "ocr_",
        "tagging_",
        "in_pipeline",
        "size.in_pipeline",
        "derived.total_in_flight",
    )
    if metric.startswith(dynamic_prefixes):
        return abs(a - b) <= max(5, int(max(a, b) * 0.03))
    return a == b


def check(metric: str, dashboard_value: Any, source_value: Any, source_context: str, note: str = "") -> CheckResult:
    a = safe_float(dashboard_value, 0.0)
    b = safe_float(source_value, 0.0)
    status = "PASS" if eq_with_tolerance(metric, a, b) else "FAIL"
    return CheckResult(
        metric=metric,
        dashboard_value=dashboard_value,
        source_value=source_value,
        source_context=source_context,
        status=status,
        note=note,
    )


def main() -> int:
    config = get_config()
    qm = get_queue_manager()
    queue_stats = qm.get_queue_statistics() or {}
    size_stats = qm.get_size_statistics() or {}
    summary = extract_summary(queue_stats)

    r = redis.Redis.from_url(config.redis.url, decode_responses=True, protocol=2)

    # Raw counters/keys used to derive dashboard numbers.
    pipe = r.pipeline(transaction=False)
    pipe.get("docsearch:counter:discovered")
    pipe.get("docsearch:counter:discovered_bytes")
    pipe.zcard("docsearch:queue:discovery")
    pipe.hlen("docsearch:failed")
    pipe.get("docsearch:counter:extraction_completed")
    for cat in ["tiny", "small", "medium", "large"]:
        pipe.zcard(f"docsearch:queue:extraction:{cat}")
    pipe.llen("docsearch:queue:indexing")
    pipe.zcard("docsearch:queue:ocr")
    pipe.llen("docsearch:queue:tagging")
    pipe.get("docsearch:counter:tagging_completed")
    pipe.get("docsearch:counter:root_completed")
    pipe.get("docsearch:counter:completed")
    pipe.get("docsearch:counter:completed_bytes")
    pipe.get("docsearch:counter:completed_extract_ms")
    pipe.get("docsearch:counter:completed_index_ms")
    pipe.get("docsearch:counter:duplicates")
    raw = pipe.execute()

    idx = 0
    raw_discovered = safe_int(raw[idx]); idx += 1
    raw_discovered_bytes = safe_int(raw[idx]); idx += 1
    raw_discovery_pending = safe_int(raw[idx]); idx += 1
    raw_failed = safe_int(raw[idx]); idx += 1
    raw_extraction_completed = safe_int(raw[idx]); idx += 1
    raw_extraction_pending = 0
    for _ in ["tiny", "small", "medium", "large"]:
        raw_extraction_pending += safe_int(raw[idx]); idx += 1
    raw_indexing_pending = safe_int(raw[idx]); idx += 1
    raw_ocr_pending = safe_int(raw[idx]); idx += 1
    raw_tagging_pending = safe_int(raw[idx]); idx += 1
    raw_tagging_completed = safe_int(raw[idx]); idx += 1
    raw_root_completed = safe_int(raw[idx]); idx += 1
    raw_completed_items = safe_int(raw[idx]); idx += 1
    raw_completed_bytes = safe_int(raw[idx]); idx += 1
    raw_total_extract_ms = safe_int(raw[idx]); idx += 1
    raw_total_index_ms = safe_int(raw[idx]); idx += 1
    raw_duplicates = safe_int(raw[idx]); idx += 1

    # Processing hashes (raw active processing counts).
    raw_extraction_processing = qm._count_processing_items(qm.PROCESSING_EXTRACTION)
    raw_indexing_processing = qm._count_processing_items(qm.PROCESSING_INDEXING)
    raw_ocr_processing = qm._count_processing_items(qm.PROCESSING_OCR)
    raw_tagging_processing = qm._count_processing_items(qm.PROCESSING_TAGGING)

    raw_discovery_completed = max(0, raw_discovered - raw_discovery_pending)
    raw_extraction_total = raw_extraction_pending + raw_extraction_processing + raw_extraction_completed
    raw_indexing_completed = raw_root_completed
    raw_indexing_total = raw_indexing_pending + raw_indexing_processing + raw_indexing_completed
    raw_ocr_completed = safe_int(r.get("docsearch:counter:ocr_completed"))
    raw_ocr_total = raw_ocr_pending + raw_ocr_processing + raw_ocr_completed
    raw_tagging_total = raw_tagging_pending + raw_tagging_processing + raw_tagging_completed

    raw_completed_total = raw_root_completed if raw_root_completed > 0 else raw_completed_items
    raw_avg_extract_ms = int(raw_total_extract_ms / raw_completed_total) if raw_completed_total > 0 else 0
    raw_avg_index_ms = int(raw_total_index_ms / raw_completed_total) if raw_completed_total > 0 else 0
    raw_in_pipeline_files = (
        raw_extraction_pending + raw_indexing_pending + raw_ocr_pending + raw_tagging_pending
        + raw_extraction_processing + raw_indexing_processing + raw_ocr_processing + raw_tagging_processing
    )
    raw_in_pipeline_bytes_est = max(0, raw_discovered_bytes - raw_completed_bytes)

    print("\n--- DIAGNOSTIC: docsearch:files:* keys ---")
    for key in r.keys("docsearch:files:*"):
        print(f"Key {key} -> {r.hgetall(key)}")
    print("-------------------------------------------\n")

    checks: List[CheckResult] = []
    checks.append(check("discovered_total", summary["discovered_total"], raw_discovered, "Redis: docsearch:counter:discovered"))
    checks.append(check("discovery_pending", summary["discovery_pending"], raw_discovery_pending, "Redis: ZCARD docsearch:queue:discovery"))
    checks.append(check("discovery_completed", summary["discovery_completed"], raw_discovery_completed, "Derived: discovered - discovery_pending"))

    checks.append(check("extraction_pending", summary["extraction_pending"], raw_extraction_pending, "Redis: SUM ZCARD docsearch:queue:extraction:{tiny|small|medium|large}"))
    checks.append(check("extraction_processing", summary["extraction_processing"], raw_extraction_processing, "Redis: SUM HLEN docsearch:processing:extraction:*"))
    checks.append(check("extraction_completed", summary["extraction_completed"], raw_extraction_completed, "Redis: docsearch:counter:extraction_completed"))
    checks.append(check("extraction_total", summary["extraction_total"], raw_extraction_total, "Derived: pending + processing + completed"))

    checks.append(check("indexing_pending", summary["indexing_pending"], raw_indexing_pending, "Redis: LLEN docsearch:queue:indexing"))
    checks.append(check("indexing_processing", summary["indexing_processing"], raw_indexing_processing, "Redis: SUM HLEN docsearch:processing:indexing:*"))
    checks.append(check("indexing_completed", summary["indexing_completed"], raw_indexing_completed, "Redis: docsearch:counter:root_completed"))
    checks.append(check("indexing_total", summary["indexing_total"], raw_indexing_total, "Derived: pending + processing + completed"))

    checks.append(check("ocr_pending", summary["ocr_pending"], raw_ocr_pending, "Redis: ZCARD docsearch:queue:ocr"))
    checks.append(check("ocr_processing", summary["ocr_processing"], raw_ocr_processing, "Redis: SUM HLEN docsearch:processing:ocr:*"))
    checks.append(check("ocr_completed", summary["ocr_completed"], raw_ocr_completed, "Redis: docsearch:counter:ocr_completed"))
    checks.append(check("ocr_total", summary["ocr_total"], raw_ocr_total, "Derived: pending + processing + completed"))

    checks.append(check("tagging_pending", summary["tagging_pending"], raw_tagging_pending, "Redis: LLEN docsearch:queue:tagging"))
    checks.append(check("tagging_processing", summary["tagging_processing"], raw_tagging_processing, "Redis: SUM HLEN docsearch:processing:tagging:*"))
    checks.append(check("tagging_completed", summary["tagging_completed"], raw_tagging_completed, "Redis: docsearch:counter:tagging_completed"))
    checks.append(check("tagging_total", summary["tagging_total"], raw_tagging_total, "Derived: pending + processing + completed"))

    checks.append(check("completed_total", summary["completed_total"], raw_completed_total, "Redis: docsearch:counter:root_completed (fallback: counter:completed)"))
    checks.append(check("duplicates", summary["duplicates"], raw_duplicates, "Redis: docsearch:counter:duplicates"))
    checks.append(check("avg_extraction_ms", summary["avg_extraction_ms"], raw_avg_extract_ms, "Derived: counter:completed_extract_ms / completed_total"))
    checks.append(check("avg_indexing_ms", summary["avg_indexing_ms"], raw_avg_index_ms, "Derived: counter:completed_index_ms / completed_total"))
    checks.append(check("total_failures", summary["total_failures"], raw_failed, "Redis: HLEN docsearch:failed"))

    # Sidebar/SystemMonitor size-mode metrics
    size_discovered_files = safe_int((size_stats.get("discovered") or {}).get("files"))
    size_discovered_bytes = safe_int((size_stats.get("discovered") or {}).get("size_bytes"))
    size_searchable_files = safe_int((size_stats.get("searchable") or {}).get("files"))
    size_searchable_items = safe_int((size_stats.get("searchable") or {}).get("items"))
    size_searchable_bytes = safe_int((size_stats.get("searchable") or {}).get("size_bytes"))
    size_pipeline_files = safe_int((size_stats.get("in_pipeline") or {}).get("files"))
    size_pipeline_bytes = safe_int((size_stats.get("in_pipeline") or {}).get("size_bytes"))
    size_failed_files = safe_int((size_stats.get("failed") or {}).get("files"))

    checks.append(check("size.discovered.files", size_discovered_files, raw_discovered, "Redis: docsearch:counter:discovered"))
    checks.append(check("size.discovered.bytes", size_discovered_bytes, raw_discovered_bytes, "Redis: docsearch:counter:discovered_bytes"))
    checks.append(check("size.searchable.files", size_searchable_files, raw_completed_total, "Redis: docsearch:counter:root_completed"))
    checks.append(check("size.searchable.items", size_searchable_items, raw_completed_items, "Redis: docsearch:counter:completed"))
    checks.append(check("size.searchable.bytes", size_searchable_bytes, raw_completed_bytes, "Redis: docsearch:counter:completed_bytes"))
    checks.append(check("size.in_pipeline.files", size_pipeline_files, raw_in_pipeline_files, "Derived: queue pending + processing across extraction/indexing/ocr/tagging"))
    checks.append(check("size.in_pipeline.bytes", size_pipeline_bytes, raw_in_pipeline_bytes_est, "Derived estimate: discovered_bytes - completed_bytes"))
    checks.append(check("size.failed.files", size_failed_files, raw_failed, "Redis: HLEN docsearch:failed"))

    # Derived display numbers used in System Monitor.
    total_to_process_dash = max(0, summary["discovered_total"] - summary["duplicates"])
    overall_progress_dash = (summary["completed_total"] / total_to_process_dash * 100.0) if total_to_process_dash > 0 else 0.0
    total_to_process_raw = max(0, raw_discovered - raw_duplicates)
    overall_progress_raw = (raw_completed_total / total_to_process_raw * 100.0) if total_to_process_raw > 0 else 0.0
    checks.append(check("derived.total_to_process", total_to_process_dash, total_to_process_raw, "Derived: discovered_total - duplicates"))
    checks.append(check("derived.overall_progress_pct", round(overall_progress_dash, 3), round(overall_progress_raw, 3), "Derived: completed_total / total_to_process * 100"))

    total_in_flight_dash = (
        summary["extraction_pending"] + summary["extraction_processing"] +
        summary["indexing_pending"] + summary["indexing_processing"] +
        summary["ocr_pending"] + summary["ocr_processing"] +
        summary["tagging_pending"] + summary["tagging_processing"]
    )
    checks.append(check("derived.total_in_flight", total_in_flight_dash, raw_in_pipeline_files, "Derived: stage pending + processing"))

    # Live Audit row count (tab default = latest 50).
    live_feed_rows = len(get_live_feed(limit=50))
    checks.append(check("live_audit.rows_default", live_feed_rows, live_feed_rows, "SQLite audit.db: SELECT ... FROM audit_events ORDER BY id DESC LIMIT 50", note="Informational metric"))

    # OpenSearch context for searchable document count.
    os_count = None
    try:
        index_name = config.indexing.opensearch.index_name
        resp = requests.get(f"http://localhost:9200/{index_name}/_count", timeout=8)
        if resp.status_code == 200:
            os_count = safe_int(resp.json().get("count"))
    except Exception:
        os_count = None

    pass_count = sum(1 for c in checks if c.status == "PASS")
    fail_count = sum(1 for c in checks if c.status == "FAIL")

    report_dir = Path(config.paths.working_root) / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    report_path = report_dir / f"dashboard_validation_{ts}.md"

    lines: List[str] = []
    lines.append("# Dashboard Number Validation Report")
    lines.append("")
    lines.append(f"- Generated (UTC): `{datetime.now(timezone.utc).isoformat()}`")
    lines.append(f"- Pass: `{pass_count}`")
    lines.append(f"- Fail: `{fail_count}`")
    lines.append(f"- OpenSearch `_count` ({config.indexing.opensearch.index_name}): `{os_count}`")
    lines.append("")
    lines.append("## Metric Checks")
    lines.append("")
    lines.append("| Metric | Dashboard Value | Source Value | Status | Source Context | Note |")
    lines.append("|---|---:|---:|---|---|---|")
    for c in checks:
        lines.append(
            f"| `{c.metric}` | `{c.dashboard_value}` | `{c.source_value}` | `{c.status}` | `{c.source_context}` | `{c.note}` |"
        )

    lines.append("")
    lines.append("## Snapshot Payload")
    lines.append("")
    lines.append("```json")
    lines.append(
        json.dumps(
            {
                "queue_stats": queue_stats,
                "size_stats": size_stats,
                "summary": summary,
                "raw": {
                    "discovered": raw_discovered,
                    "discovered_bytes": raw_discovered_bytes,
                    "discovery_pending": raw_discovery_pending,
                    "failed": raw_failed,
                    "extraction_pending": raw_extraction_pending,
                    "extraction_processing": raw_extraction_processing,
                    "extraction_completed": raw_extraction_completed,
                    "indexing_pending": raw_indexing_pending,
                    "indexing_processing": raw_indexing_processing,
                    "indexing_completed": raw_indexing_completed,
                    "ocr_pending": raw_ocr_pending,
                    "ocr_processing": raw_ocr_processing,
                    "ocr_completed": raw_ocr_completed,
                    "tagging_pending": raw_tagging_pending,
                    "tagging_processing": raw_tagging_processing,
                    "tagging_completed": raw_tagging_completed,
                    "root_completed": raw_root_completed,
                    "completed_items": raw_completed_items,
                    "completed_bytes": raw_completed_bytes,
                    "duplicates": raw_duplicates,
                },
            },
            indent=2,
            default=str,
        )
    )
    lines.append("```")

    report_path.write_text("\n".join(lines), encoding="utf-8")

    print("=" * 88)
    print("DASHBOARD NUMBER VALIDATION")
    print("=" * 88)
    print(f"Report: {report_path}")
    print(f"PASS: {pass_count} | FAIL: {fail_count}")
    if os_count is not None:
        print(f"OpenSearch count ({config.indexing.opensearch.index_name}): {os_count}")
    print("-" * 88)
    for c in checks:
        print(f"{c.status:4} | {c.metric:30} | dashboard={c.dashboard_value} | source={c.source_value}")
    print("=" * 88)

    return 0 if fail_count == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
