r"""
Streamlit-based monitoring dashboard for the Enterprise Document Search System.
Displays live queue metrics, completion stats, and failure breakdowns for end users.

USES STREAMLIT-AUTOREFRESH for smooth, non-freezing live updates.
The page content updates without full refresh - only the data changes, not the layout.
 & .\.venv\Scripts\python.exe -m streamlit run src/ui/dashboard.py
"""

from __future__ import annotations

import html as _html
import importlib
import os
import re
import subprocess
import sys
import time
import math
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional

from opensearchpy.exceptions import RequestError

import pandas as pd
import streamlit as st

# For smooth auto-refresh without page flashing
try:
    st_autorefresh = importlib.import_module("streamlit_autorefresh").st_autorefresh
    HAS_AUTOREFRESH = True
except ImportError:
    st_autorefresh = None
    HAS_AUTOREFRESH = False

# Ensure the project src directory is importable when running "streamlit run"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Point PaddleX to the local model cache before any paddlex/paddleocr import.
# paddlex.utils.cache reads PADDLE_PDX_CACHE_HOME at import time.
os.environ.setdefault('PADDLE_PDX_CACHE_HOME', r'C:\softwares\paddle_models')

from core.config_manager import get_config  # noqa: E402
from core.queue_manager import get_queue_manager, reset_queue_manager  # noqa: E402
from core.reporting_manager import (  # noqa: E402
    export_state_matrix_xlsx,
    get_live_feed,
    search_events,
)
from ui.dashboard_state import get_dashboard_stats_runtime  # noqa: E402
from indexing.opensearch_client import OpenSearchClient  # noqa: E402
from tagging.metadata_manager import (  # noqa: E402
    clear_active_metadata_source,
    get_metadata_status,
    set_active_metadata_source,
)


# =============================================================================
# CACHING LAYER
# Cache and background refresh state is managed by ui.dashboard_state.
# =============================================================================

def invalidate_all_caches() -> None:
    """Soft invalidate stats cache while preserving last-known-good payloads."""
    get_dashboard_stats_runtime().invalidate()
    try:
        get_cached_failed_files.clear()
        get_cached_ocr_pending.clear()
        get_cached_largest_files.clear()
    except Exception:
        pass


def clear_all_caches() -> None:
    """Hard-clear cached stats and last-known-good payloads."""
    get_dashboard_stats_runtime().clear()
    try:
        get_cached_failed_files.clear()
        get_cached_ocr_pending.clear()
        get_cached_largest_files.clear()
    except Exception:
        pass


def _consume_dashboard_reset_marker(config: Any) -> bool:
    """Consume reset marker and hard-clear all dashboard caches when present."""
    try:
        marker = Path(config.paths.working_root) / "cache" / "dashboard_reset.marker"
        if not marker.exists():
            return False

        clear_all_caches()
        try:
            marker.unlink()
        except Exception:
            pass
        return True
    except Exception:
        return False


def _force_refresh_from_redis() -> Dict[str, Any]:
    """Force queue + size refresh with bounded timeouts."""
    return get_dashboard_stats_runtime().force_refresh(timeout_sec=5.0)


def _is_real_data(data: Any, min_keys: int = 2) -> bool:
    """Check if data is a real stats dict with meaningful content."""
    # Issue 16: Function was unused but is kept as it may be useful for external callers
    return bool(data and isinstance(data, dict) and len(data) >= min_keys)


@st.cache_resource(show_spinner=False)
def _get_os_client() -> Optional[OpenSearchClient]:
    """Issue 13: Cached OpenSearch client — avoids re-creation on every Streamlit rerun."""
    try:
        return OpenSearchClient()
    except Exception:
        return None


def get_cached_queue_stats() -> Dict[str, Any]:
    """Read queue stats from process-stable runtime manager."""
    return get_dashboard_stats_runtime().get_queue_stats()


def get_cached_size_stats() -> Dict[str, Any]:
    """Read size stats from process-stable runtime manager."""
    return get_dashboard_stats_runtime().get_size_stats()


@st.cache_data(ttl=5, show_spinner=False)
def get_cached_failed_files(limit: int = 50) -> List[Dict[str, Any]]:
    """Get failed files with caching (5s TTL for near-real-time updates)"""
    try:
        qm = get_queue_manager()
        return qm.get_failed_files(limit=limit)
    except Exception:
        return []


@st.cache_data(ttl=3, show_spinner=False)
def get_cached_ocr_pending(limit: int = 30) -> List[Dict[str, Any]]:
    """Get OCR pending files with caching (3s TTL for real-time responsiveness)"""
    try:
        qm = get_queue_manager()
        return qm.get_ocr_pending_files(limit=limit)
    except Exception:
        return []


@st.cache_data(ttl=10, show_spinner=False)
def get_cached_largest_files(limit: int = 10) -> List[Dict[str, Any]]:
    """Get largest completed files with caching (10s TTL - static data)"""
    try:
        qm = get_queue_manager()
        return qm.get_largest_completed_files(limit=limit)
    except Exception:
        return []


def _esc(text: str) -> str:
    """HTML-escape a string to prevent XSS in unsafe_allow_html blocks."""
    return _html.escape(str(text or ""))


def _schedule_fallback_rerun(interval_seconds: int) -> None:
    """Fallback auto-refresh via frontend timer when streamlit-autorefresh is unavailable."""
    try:
        import streamlit.components.v1 as components

        interval_ms = max(1000, int(interval_seconds) * 1000)
        components.html(
            f"""<script>
            setTimeout(function() {{
                window.parent.postMessage({{type: 'streamlit:rerun'}}, '*');
            }}, {interval_ms});
            </script>""",
            height=0,
        )
    except Exception:
        pass


def open_file_with_default_app(filepath: str) -> None:
    """Open a file with the default system application.

    Validates that the path exists and is under the configured source_drive root
    to prevent arbitrary file/UNC path access from poisoned index data.
    """
    # --- Issue 2: Validate path before opening ---
    resolved = Path(filepath).resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"File does not exist: {filepath}")

    # Block UNC / network paths
    if str(resolved).startswith('\\\\'):
        raise PermissionError(f"Opening network paths is not allowed: {filepath}")

    # Restrict to configured source_drive OR working_root (for embedded files)
    try:
        cfg = get_config()
        allowed_roots = []
        source_drive = getattr(cfg.paths, 'source_drive', None)
        working_root = getattr(cfg.paths, 'working_root', None)
        app_root = getattr(cfg.paths, 'app_root', None)
        if source_drive:
            allowed_roots.append(str(Path(source_drive).resolve()))
        if working_root:
            allowed_roots.append(str(Path(working_root).resolve()))
        if app_root:
            allowed_roots.append(str(Path(app_root).resolve()))
        
        if allowed_roots:
            resolved_str = str(resolved)
            is_windows = os.name == 'nt'
            if is_windows:
                resolved_str_cmp = resolved_str.lower()
                allowed_roots_cmp = [r.lower() for r in allowed_roots]
            else:
                resolved_str_cmp = resolved_str
                allowed_roots_cmp = allowed_roots
            
            if not any(resolved_str_cmp.startswith(root) for root in allowed_roots_cmp):
                raise PermissionError(
                    f"Path '{filepath}' is outside allowed roots"
                )
    except (AttributeError, TypeError):
        pass  # No roots configured — allow

    try:
        if os.name == 'nt':  # Windows
            os.startfile(str(resolved))
        elif sys.platform == 'darwin':  # macOS
            subprocess.run(['open', str(resolved)], check=True)
        elif os.name == 'posix':  # Linux
            subprocess.run(['xdg-open', str(resolved)], check=True)
        else:
            raise RuntimeError(f"Unsupported platform: {os.name}")
    except Exception as e:
        raise RuntimeError(f"Failed to open file: {e}") from e


def format_number(value: Any) -> str:
    """Format numeric values with thousands separators and fallbacks."""
    if value is None:
        return "-"
    if isinstance(value, (int, float)):
        return f"{value:,}"
    return str(value)


def format_size(size_bytes: int) -> str:
    """Format bytes to human-readable size (MB/GB/TB)."""
    if size_bytes is None or size_bytes == 0:
        return "0 MB"
    
    # Define size thresholds
    TB = 1024 ** 4
    GB = 1024 ** 3
    MB = 1024 ** 2
    
    KB = 1024
    
    if size_bytes >= TB:
        return f"{size_bytes / TB:.2f} TB"
    elif size_bytes >= GB:
        return f"{size_bytes / GB:.2f} GB"
    elif size_bytes >= MB:
        return f"{size_bytes / MB:.2f} MB"
    elif size_bytes >= KB:
        return f"{size_bytes / KB:.2f} KB"
    else:
        return f"{size_bytes} B"


def calculate_progress_percentage(completed: int, total: int) -> float:
    """Calculate progress percentage safely."""
    if total == 0:
        return 0.0
    return min((completed / total) * 100, 100.0)


def seconds_to_human(s: float) -> str:
    """Convert seconds to human-friendly H M S string."""
    try:
        s = int(max(0, math.floor(s)))
    except Exception:
        try:
            s = int(max(0, int(s)))
        except Exception:
            return "0s"

    m, sec = divmod(s, 60)
    h, m = divmod(m, 60)
    parts = []
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    parts.append(f"{sec}s")
    return ' '.join(parts)


def extract_summary(queue_stats: Dict[str, Any]) -> Dict[str, Any]:
    """Compute top-level summary metrics for display with robust error handling."""
    try:
        discovery = queue_stats.get("discovery", {}) or {}
        extraction_by_size = queue_stats.get("extraction", {}) or {}
        extraction_total_stats = queue_stats.get("extraction_total", {}) or {}
        indexing = queue_stats.get("indexing", {}) or {}
        ocr = queue_stats.get("ocr", {}) or {}
        tagging = queue_stats.get("tagging", {}) or {}
        completed = queue_stats.get("completed", {}) or {}

        # Helper to safely get int value
        def safe_int(val):
            if val is None:
                return 0
            try:
                return int(val)
            except (ValueError, TypeError):
                return 0

        # Discovery metrics
        discovered_total = safe_int(discovery.get("total"))
        discovery_pending = safe_int(discovery.get("pending"))
        discovery_completed = safe_int(discovery.get("completed"))
        
        # Use extraction_total stats directly (more reliable than summing categories)
        extraction_pending = safe_int(extraction_total_stats.get("pending"))
        extraction_processing = safe_int(extraction_total_stats.get("processing"))
        extraction_completed = safe_int(extraction_total_stats.get("completed"))
        # Use total from stats, or calculate it
        extraction_total = safe_int(extraction_total_stats.get("total"))
        if extraction_total == 0:
            extraction_total = extraction_pending + extraction_processing + extraction_completed
        
        # Indexing metrics
        indexing_pending = safe_int(indexing.get("pending"))
        indexing_processing = safe_int(indexing.get("processing"))
        indexing_completed = safe_int(indexing.get("completed"))
        # Use total from stats, or calculate it
        indexing_total = safe_int(indexing.get("total"))
        if indexing_total == 0:
            indexing_total = indexing_pending + indexing_processing + indexing_completed
        
        # OCR metrics
        ocr_pending = safe_int(ocr.get("pending"))
        ocr_processing = safe_int(ocr.get("processing"))
        ocr_completed = safe_int(ocr.get("completed"))
        # Use total from stats, or calculate it
        ocr_total = safe_int(ocr.get("total"))
        if ocr_total == 0:
            ocr_total = ocr_pending + ocr_processing + ocr_completed

        # Tagging metrics
        tagging_pending = safe_int(tagging.get("pending"))
        tagging_processing = safe_int(tagging.get("processing"))
        tagging_completed = safe_int(tagging.get("completed"))
        tagging_total = safe_int(tagging.get("total"))
        if tagging_total == 0:
            tagging_total = tagging_pending + tagging_processing + tagging_completed

        summary = {
            "discovered_total": discovered_total,
            "discovery_pending": discovery_pending,
            "discovery_completed": discovery_completed,
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

        return summary
    except Exception as e:
        # Issue 9: Log the error instead of silently returning all zeros
        import logging
        logging.getLogger(__name__).warning("extract_summary failed: %s", e, exc_info=True)
        return {
            "discovered_total": 0, "discovery_pending": 0, "discovery_completed": 0,
            "extraction_pending": 0, "extraction_processing": 0, "extraction_completed": 0, "extraction_total": 0,
            "indexing_pending": 0, "indexing_processing": 0, "indexing_completed": 0, "indexing_total": 0,
            "ocr_pending": 0, "ocr_processing": 0, "ocr_completed": 0, "ocr_total": 0,
            "tagging_pending": 0, "tagging_processing": 0, "tagging_completed": 0, "tagging_total": 0,
            "completed_total": 0, "duplicates": 0, "avg_extraction_ms": 0, "avg_indexing_ms": 0, "total_failures": 0,
        }





def render_failure_chart(queue_stats: Dict[str, Any]) -> None:
    """Render failure breakdown as a bar chart."""
    failures = queue_stats.get("failures", {})
    if not failures:
        st.success("No failures recorded")
        return
    
    df = pd.DataFrame([
        {"Error Type": error_type, "Count": count}
        for error_type, count in sorted(failures.items(), key=lambda x: x[1], reverse=True)
    ])
    
    st.bar_chart(df.set_index("Error Type"))


def render_extraction_detail(queue_stats: Dict[str, Any]) -> None:
    """Render extraction queue breakdown by size category."""
    extraction_data = queue_stats.get("extraction", {})
    if not extraction_data:
        st.info("No extraction data available")
        return
    
    df_data = []
    for size_category, data in extraction_data.items():
        df_data.append({
            "Category": size_category.title(),
            "Total": data.get("total", 0) or 0,
            "Completed": data.get("completed", 0) or 0,
            "Processing": data.get("processing", 0) or 0,
            "Pending": data.get("pending", 0) or 0,
        })
    
    df = pd.DataFrame(df_data)
    st.dataframe(df, use_container_width=True, hide_index=True)


def render_dashboard() -> None:
    st.set_page_config(
        page_title="Document Search System",
        page_icon=":page_facing_up:",
        layout="wide",
        initial_sidebar_state="expanded"
    )

    # Custom CSS for better styling
    st.markdown("""
        <style>
        .main > div {
            padding-top: 2rem;
        }
        .stMetric {
            background-color: #f0f2f6;
            padding: 1rem;
            border-radius: 0.5rem;
        }
        h1 {
            color: #1f77b4;
            padding-bottom: 1rem;
        }
        h3 {
            color: #2c3e50;
            margin-top: 1rem;
        }
        .stProgress > div > div > div {
            background-color: #1f77b4;
        }
        .result-snippet { 
            font-size: 0.95rem; 
            color: #374151; 
            background: #f9fafb; 
            padding: 0.75rem; 
            border-radius: 6px; 
            line-height: 1.5; 
        }
        .highlight { 
            background-color: #fef08a; 
            padding: 0.1rem 0.2rem; 
            border-radius: 3px; 
            font-weight: 500; 
        }
        .doc-card {
            border: 1px solid #e5e7eb;
            border-radius: 8px;
            padding: 1rem;
            margin-bottom: 0.75rem;
            background: white;
        }
        .doc-filename {
            font-weight: 600;
            font-size: 1.1rem;
            color: #1f2937;
            margin-bottom: 0.5rem;
        }
        .doc-meta {
            font-size: 0.85rem;
            color: #6b7280;
            margin-bottom: 0.5rem;
        }
        </style>
    """, unsafe_allow_html=True)

    config = get_config()
    queue_manager = get_queue_manager()

    # Issue 13: Use cached OpenSearch client singleton
    os_client = _get_os_client()
    if os_client is None:
        st.error("Failed to initialize OpenSearch client.")

    # Header
    st.title("Document Retrieval System")
    
    
    # Sidebar - different content based on selected tab
    render_sidebar(config, os_client, queue_manager)
    
    # Main view selector (avoid rendering both views on every refresh)
    view = st.radio("View", ["Search", "Live Audit", "Snippet Review", "System Monitor"], horizontal=True, index=0)
    
    if view == "Search":
        render_search_tab(config, os_client)
    elif view == "Live Audit":
        render_live_audit_tab(config)
    elif view == "Snippet Review":
        from ui.review_tab import render_snippet_review_tab
        render_snippet_review_tab(config)
    else:
        render_monitoring_tab(config, queue_manager)


def render_sidebar(config: Any, os_client: Optional[OpenSearchClient], queue_manager: Any) -> None:
    """Render sidebar with system stats."""
    with st.sidebar:
        st.markdown("## \U0001F4CA Data Progress")
        
        # Toggle between file count and data size view
        show_as_files = st.toggle(
            "\U0001F4C1 Show as file count", 
            value=st.session_state.get("show_as_files", False), 
            help="Toggle between data size and file count",
            key="show_as_files"
        )
        
        # Get size statistics with CACHING to prevent lag
        try:
            size_stats = get_cached_size_stats()
            queue_stats = get_cached_queue_stats()
            if not size_stats and not queue_stats:
                # Issue 14: Cooldown on sidebar forced refresh (5s)
                _sidebar_last_refresh_key = "_sidebar_force_refresh_ts"
                last_refresh = st.session_state.get(_sidebar_last_refresh_key, 0)
                if time.time() - last_refresh >= 15:
                    st.session_state[_sidebar_last_refresh_key] = time.time()
                    refreshed = _force_refresh_from_redis()
                    size_stats = refreshed.get("size_stats") or size_stats
                    queue_stats = refreshed.get("queue_stats") or queue_stats
            if not size_stats and not queue_stats:
                st.info("Initializing system statistics... Monitoring metrics will appear shortly.")
                # We do NOT return here, so the rest of the dashboard (Search, Audit) can still render
                summary = extract_summary({})
                size_stats = {}
            else:
                summary = extract_summary(queue_stats)

            # Safely get nested values with defaults
            def safe_get(d, *keys, default=0):
                """Safely traverse nested dict structure"""
                try:
                    result = d
                    for key in keys:
                        if result is None:
                            return default
                        result = result.get(key, default) if isinstance(result, dict) else default
                    return result if result is not None else default
                except Exception:
                    return default
            
            # Calculate values with safe access
            discovered = size_stats.get('discovered', {}) or {}
            in_pipeline = size_stats.get('in_pipeline', {}) or {}
            searchable = size_stats.get('searchable', {}) or {}
            failed = size_stats.get('failed', {}) or {}
            
            # Total progress with safe int conversion
            total_discovered = safe_get(discovered, 'files', default=0)
            total_searchable_root = safe_get(searchable, 'files', default=0)
            total_searchable_items = safe_get(searchable, 'items', default=0)
            
            # If items count is 0 (legacy) or less than files, use files count
            if total_searchable_items < total_searchable_root:
                total_searchable_items = total_searchable_root
                
            total_embedded = max(0, total_searchable_items - total_searchable_root)
            
            # in_pipeline.files from Redis includes expanded embedded items from
            # the indexing queue.  Cap it so that sidebar numbers stay consistent:
            #   discovered = in_pipeline + searchable + failed
            raw_in_pipeline = safe_get(in_pipeline, 'files', default=0)
            total_failed = safe_get(failed, 'files', default=0)
            total_in_pipeline = min(raw_in_pipeline,
                                    max(0, total_discovered - total_searchable_root - total_failed))
            
            discovered_size = safe_get(discovered, 'size_bytes', default=0)
            searchable_size = safe_get(searchable, 'size_bytes', default=0)
            pipeline_size = safe_get(in_pipeline, 'size_bytes', default=0)
            if total_in_pipeline == 0:
                pipeline_size = 0
            
            if show_as_files:
                progress_pct = (total_searchable_root / max(total_discovered, 1)) * 100
            else:
                progress_pct = (searchable_size / max(discovered_size, 1)) * 100
            
            st.markdown("---")
            
            # Main progression display
            if show_as_files:
                # File count view
                st.markdown("### \U0001F4C2 Total Discovered")
                st.markdown(f"**{total_discovered:,}** files")
                
                st.markdown("### \U0001F504 In Pipeline")
                st.markdown(f"**{total_in_pipeline:,}** files")
                
                st.markdown("### \U0001F50D Searchable")
                st.markdown(f"**{total_searchable_root:,}** root files")
                if total_embedded > 0:
                    st.caption(f"+ {total_embedded:,} embedded items")
                
                if total_failed > 0:
                    st.markdown("### \u274C Failed")
                    st.markdown(f"**{total_failed:,}** files")
            else:
                # Data size view
                st.markdown("### \U0001F4C2 Total Discovered")
                st.markdown(f"**{format_size(discovered_size)}**")
                st.caption(f"{total_discovered:,} files")
                
                st.markdown("### \U0001F504 In Pipeline")
                st.markdown(f"**{format_size(pipeline_size)}**")
                st.caption(f"{total_in_pipeline:,} files processing")
                
                st.markdown("### \U0001F50D Searchable")
                st.markdown(f"**{format_size(searchable_size)}**")
                st.caption(f"{total_searchable_root:,} files ({total_searchable_items:,} items)")
                
                if total_failed > 0:
                    st.markdown("### \u274C Failed")
                    st.caption(f"{total_failed:,} files failed")
            
            st.markdown("---")
            
            # Overall progress bar
            st.markdown("### Overall Progress")
            st.progress(min(progress_pct / 100, 1.0), text=f"{progress_pct:.1f}% complete")
            
            st.markdown("---")
            
            # Pipeline status (compact)
            st.markdown("### \u2699\uFE0F Pipeline")
            
            # Discovery
            discovery_total = summary.get('discovered_total', 0) or 0
            discovery_completed = summary.get('discovery_completed', 0) or 0
            discovery_pending = summary.get('discovery_pending', 0) or 0
            if discovery_total > 0 or discovery_pending > 0:
                discovery_pct = (discovery_completed / discovery_total * 100) if discovery_total > 0 else 0
                st.progress(min(discovery_pct / 100, 1.0), text=f"Discovery: {discovery_pct:.0f}% ({discovery_completed:,}/{discovery_total:,})")
            
            # Extraction
            extraction_total = summary.get('extraction_total', 0) or 0
            extraction_completed = summary.get('extraction_completed', 0) or 0
            extraction_pending = summary.get('extraction_pending', 0) or 0
            extraction_processing = summary.get('extraction_processing', 0) or 0
            if extraction_total > 0 or extraction_pending > 0 or extraction_processing > 0:
                extraction_pct = (extraction_completed / extraction_total * 100) if extraction_total > 0 else 0
                status_text = f"Extraction: {extraction_pct:.0f}% ({extraction_completed:,}/{extraction_total:,})"
                if extraction_processing > 0:
                    status_text += f" | Processing: {extraction_processing:,}"
                st.progress(min(extraction_pct / 100, 1.0), text=status_text)
            
            # Indexing
            indexing_total = summary.get('indexing_total', 0) or 0
            indexing_completed = summary.get('indexing_completed', 0) or 0
            indexing_pending = summary.get('indexing_pending', 0) or 0
            indexing_processing = summary.get('indexing_processing', 0) or 0
            if indexing_total > 0 or indexing_pending > 0 or indexing_processing > 0:
                indexing_pct = (indexing_completed / indexing_total * 100) if indexing_total > 0 else 0
                status_text = f"Indexing: {indexing_pct:.0f}% ({indexing_completed:,}/{indexing_total:,})"
                if indexing_processing > 0:
                    status_text += f" | Processing: {indexing_processing:,}"
                st.progress(min(indexing_pct / 100, 1.0), text=status_text)
            
            # OCR
            ocr_total = summary.get('ocr_total', 0) or 0
            ocr_completed = summary.get('ocr_completed', 0) or 0
            ocr_pending = summary.get('ocr_pending', 0) or 0
            ocr_processing = summary.get('ocr_processing', 0) or 0
            if ocr_total > 0 or ocr_pending > 0 or ocr_processing > 0:
                ocr_pct = (ocr_completed / ocr_total * 100) if ocr_total > 0 else 0
                status_text = f"OCR: {ocr_pct:.0f}% ({ocr_completed:,}/{ocr_total:,})"
                if ocr_processing > 0:
                    status_text += f" | Processing: {ocr_processing:,}"
                st.progress(min(ocr_pct / 100, 1.0), text=status_text)
            
            if discovery_total == 0 and extraction_total == 0 and indexing_total == 0 and \
               discovery_pending == 0 and extraction_pending == 0 and indexing_pending == 0 and \
               (size_stats or queue_stats):
                st.info("System ready. No active processing.")
        
        except Exception as e:
            error_msg = str(e)
            if "no such table" in error_msg.lower() or "no such column" in error_msg.lower():
                # Database was reset - reset the singleton and try to re-create a fresh instance
                reset_queue_manager()
                queue_manager = get_queue_manager()
                try:
                    size_stats = queue_manager.get_size_statistics() or {}
                    queue_stats = queue_manager.get_queue_statistics() or {}
                    summary = extract_summary(queue_stats)
                except Exception:
                    # Still not available - show empty metrics and notify user
                    st.markdown("---")
                    st.markdown("### \U0001F4C2 Total Discovered")
                    st.markdown("**0** files")
                    st.markdown("### \U0001F504 In Pipeline")
                    st.markdown("**0** files")
                    st.markdown("### \U0001F50D Searchable")
                    st.markdown("**0** files")
                    st.progress(0.0, text="0% complete")
                    st.info("System reset. Start the orchestrator to begin processing.")
            else:
                # Show fallback UI with error message
                st.markdown("---")
                st.markdown("### \U0001F4C2 Total Discovered")
                st.markdown("**-** files")
                st.markdown("### \U0001F504 In Pipeline")
                st.markdown("**-** files")
                st.markdown("### \U0001F50D Searchable")
                st.markdown("**-** files")
                st.progress(0.0, text="Unable to load stats")
                st.warning(f"Stats unavailable: {str(e)[:100]}")

        # ── OCR Engine Selector ──────────────────────────────────────────
        st.markdown("---")
        st.markdown("### ⚙️ OCR Engine")

        _ENGINE_OPTIONS = {
            "PaddleOCR-VL-1.6 (High Quality)": "vl16",
            "PaddleOCR (Fast)": "paddle",
        }
        _ENGINE_LABELS = list(_ENGINE_OPTIONS.keys())

        # Determine current saved engine to pre-select
        _saved_engine = "vl16"
        try:
            _saved_engine = queue_manager.get_ocr_engine() or "vl16"
        except Exception:
            pass
        _default_idx = next(
            (i for i, k in enumerate(_ENGINE_LABELS) if _ENGINE_OPTIONS[k] == _saved_engine), 0
        )

        _selected_label = st.selectbox(
            "Select OCR engine",
            _ENGINE_LABELS,
            index=_default_idx,
            key="ocr_engine_selector",
            help=(
                "VL-1.6: Vision-language model, better quality on scanned/complex docs (~10-30s/page on CPU). "
                "PaddleOCR: Fast standard model (~1-2s/page). "
                "Restart OCR workers after switching."
            ),
        )
        _selected_engine = _ENGINE_OPTIONS[_selected_label]

        if _selected_engine != st.session_state.get("_ocr_engine_prev"):
            try:
                queue_manager.set_ocr_engine(_selected_engine)
                st.session_state["_ocr_engine_prev"] = _selected_engine
                st.success(f"Switched to **{_selected_label}**. Applied on next document.")
            except Exception as _e:
                st.warning(f"Could not save engine preference: {_e}")


@st.cache_data(ttl=120, show_spinner=False)
def _cached_filter_options(_os_client_id: int, field: str) -> List[str]:
    """
    Get unique values for a field from OpenSearch via terms aggregation.
    _os_client_id is the id(os_client) used as a cache key for Streamlit.
    """
    try:
        os_client = _get_os_client()
        if os_client is None:
            return []
        # Use .keyword subfield for text fields; skip for native keyword fields
        kw_field = field
        if field not in ("category", "department", "purpose", "file_type", "dynamic_subtags", "key_names"):
            kw_field = f"{field}.keyword"
        agg_query = {
            "size": 0,
            "aggs": {
                "unique_values": {
                    "terms": {"field": kw_field, "size": 100, "order": {"_key": "asc"}}
                }
            },
        }
        resp = os_client.client.search(index=os_client.index_name, body=agg_query)
        buckets = resp.get("aggregations", {}).get("unique_values", {}).get("buckets", [])
        return [b["key"] for b in buckets if b.get("key")]
    except Exception:
        return []


def _get_filter_options_multi(os_client: Optional[OpenSearchClient], fields: List[str]) -> List[str]:
    values: set[str] = set()
    if os_client is None:
        return []
    for field in fields:
        for item in _get_filter_options(os_client, field):
            if str(item).strip():
                values.add(str(item).strip())
    return sorted(values)


def _get_filter_options(os_client: Optional[OpenSearchClient], field: str) -> List[str]:
    """Public wrapper that passes a hashable key to the cached implementation."""
    if os_client is None:
        return []
    return _cached_filter_options(id(os_client), field)


def render_search_tab(config: Any, os_client: Optional[OpenSearchClient]) -> None:
    """Render the search interface tab."""
    # Auto-refresh every 5s so newly indexed / OCR-completed docs appear quickly
    if HAS_AUTOREFRESH:
        st_autorefresh(interval=5_000, key="search_tab_autorefresh")
    else:
        _schedule_fallback_rerun(5)

    st.markdown("### Search Your Documents")
    
    # Search input
    col1, col2 = st.columns([5, 1])
    with col1:
        query = st.text_input(
            "Search documents", 
            placeholder="Enter keywords, filenames, or exact phrases in quotes...", 
            label_visibility="collapsed",
            key="search_input"
        )
    with col2:
        search_button = st.button("\U0001F50D Search", use_container_width=True)
    
    # --- Multi-select filter panel ---
    filters: Dict[str, List[str]] = {}
    with st.expander("\U0001F50E Advanced Filters", expanded=False):
        fc1, fc2, fc3 = st.columns(3)
        with fc1:
            file_type_opts = _get_filter_options(os_client, "file_type")
            sel_types = st.multiselect(
                "File Type",
                options=file_type_opts,
                default=[],
                key="filter_file_type",
                help="Filter results by file extension (e.g. pdf, docx, xlsx)",
            )
            if sel_types:
                filters["file_type"] = sel_types
        with fc2:
            cat_opts = _get_filter_options(os_client, "category")
            sel_cats = st.multiselect(
                "Category",
                options=cat_opts,
                default=[],
                key="filter_category",
                help="Filter by document category assigned during tagging",
            )
            if sel_cats:
                filters["category"] = sel_cats
        with fc3:
            dept_opts = _get_filter_options(os_client, "department")
            sel_depts = st.multiselect(
                "Department",
                options=dept_opts,
                default=[],
                key="filter_department",
                help="Filter by department assigned during tagging",
            )
            if sel_depts:
                filters["department"] = sel_depts

        fc4, fc5 = st.columns(2)
        with fc4:
            business_unit_opts = _get_filter_options_multi(
                os_client,
                ["extended_metadata.business unit name", "extended_metadata.business unit"],
            )
            sel_business_units = st.multiselect(
                "Business Unit",
                options=business_unit_opts,
                default=[],
                key="filter_business_unit",
                help="Filter by metadata Business Unit",
            )
            if sel_business_units:
                filters["extended_metadata.business unit name"] = sel_business_units

        with fc5:
            sub_business_opts = _get_filter_options_multi(
                os_client,
                ["extended_metadata.sub business unit name", "extended_metadata.sub business"],
            )
            sel_sub_business = st.multiselect(
                "Sub Business Unit",
                options=sub_business_opts,
                default=[],
                key="filter_sub_business_unit",
                help="Filter by metadata Sub Business Unit",
            )
            if sel_sub_business:
                filters["extended_metadata.sub business unit name"] = sel_sub_business
    
    st.markdown("---")
    
    if not os_client:
        st.error("OpenSearch is not available. Please check the system status.")
        return
    
    # Perform search
    if (query and len(query) >= 2) or search_button:
        if query and len(query) >= 2:
            with st.spinner("Searching..."):
                try:
                    results = perform_search(os_client, query, filters=filters)
                    if results:
                        render_search_results(results, query)
                    else:
                        st.info("No results found matching your query. Try different keywords or check spelling.")
                        st.caption("Once documents are indexed, you'll be able to search through them here.")
                except Exception as e:
                    st.error(f"Search error: {e}")
        else:
            st.warning("Please enter at least 2 characters to search.")
    else:
        # Show recent documents
        render_recent_documents(os_client)


def extract_snippet_manually(text: str, query: str, max_length: int = 180) -> str:
    """Extract snippet around query match when highlights unavailable."""
    if not text:
        return ""
    
    # Normalize for searching
    query_lower = query.lower()
    query_terms = query_lower.split()
    
    # Find first occurrence of any query term
    best_pos = -1
    for term in query_terms:
        if not term:
            continue
        # Strip internal commas and periods for digits
        normalized_term = re.sub(r'(?<=\d)[,.]+(?=\d)', '', term)
        if normalized_term.isdigit():
            pattern = re.compile(r'(?<!\d)' + r'[.,]?'.join(list(normalized_term)), re.IGNORECASE)
        else:
            escaped = re.escape(term)
            start_boundary = r'\b' if term[0].isalnum() else ''
            end_boundary = r'\b' if term[-1].isalnum() else ''
            pattern = re.compile(start_boundary + escaped + end_boundary, re.IGNORECASE)
            
        match = pattern.search(text)
        if match:
            pos = match.start()
            if pos != -1 and (best_pos == -1 or pos < best_pos):
                best_pos = pos
    
    if best_pos == -1:
        # No match found, return beginning
        return text[:max_length] + "..." if len(text) > max_length else text
    
    # Extract context around match
    start = max(0, best_pos - max_length // 2)
    end = min(len(text), start + max_length)
    snippet = text[start:end]
    
    # Add ellipsis
    if start > 0:
        snippet = "..." + snippet
    if end < len(text):
        snippet = snippet + "..."
    
    # Highlight matching terms
    for term in query_terms:
        if len(term) >= 3:  # Only highlight meaningful terms
            # Strip internal commas and periods for digits
            normalized_term = re.sub(r'(?<=\d)[,.]+(?=\d)', '', term)
            if normalized_term.isdigit():
                pattern = re.compile(r'(?<!\d)' + r'[.,]?'.join(list(normalized_term)), re.IGNORECASE)
            else:
                escaped = re.escape(term)
                start_boundary = r'\b' if term[0].isalnum() else ''
                end_boundary = r'\b' if term[-1].isalnum() else ''
                pattern = re.compile(start_boundary + escaped + end_boundary, re.IGNORECASE)
                
            snippet = pattern.sub(lambda m: f"**{m.group()}**", snippet)
    
    return snippet


def adjust_numeric_highlights(snippet: str, query: str) -> str:
    """
    Adjust highlight markers (**) in the snippet so that for numeric queries,
    only the matched prefix of a number is highlighted, rather than the entire number.
    """
    if not snippet or not query:
        return snippet
        
    query_lower = query.lower()
    query_terms = query_lower.split()
    
    # We look for numeric query terms of length >= 3
    numeric_terms = []
    for term in query_terms:
        # Strip internal punctuation
        normalized = re.sub(r'(?<=\d)[,.]+(?=\d)', '', term)
        if normalized.isdigit() and len(normalized) >= 3:
            numeric_terms.append(normalized)
            
    if not numeric_terms:
        return snippet

    # Find all **...** highlights in the snippet
    def replace_highlight(match):
        inner_text = match.group(1)
        # Check if the highlighted text looks like a number (contains digits)
        digit_only = re.sub(r'[.,]', '', inner_text)
        if digit_only.isdigit():
            # Try to match any of our numeric query terms as a prefix
            for num_term in numeric_terms:
                # Create a regex to match the prefix in the actual inner_text
                # (allowing optional separators)
                pattern = re.compile(r'^' + r'[.,]?'.join(list(num_term)), re.IGNORECASE)
                m = pattern.search(inner_text)
                if m:
                    matched_part = m.group(0)
                    remaining_part = inner_text[m.end():]
                    return f"**{matched_part}**{remaining_part}"
        return match.group(0)

    # Use regex to find and replace **...** blocks
    adjusted_snippet = re.sub(r'\*\*(.*?)\*\*', replace_highlight, snippet)
    return adjusted_snippet


def _generate_ocr_variants(query: str) -> List[str]:
    """
    Generate query variants to handle common OCR character misrecognition.
    
    Common OCR errors:
    - 0 (zero) <-> O (letter O)
    - 1 (one) <-> l (lowercase L) <-> I (uppercase i)
    - 5 <-> S
    - 8 <-> B
    - rn <-> m
    - cl <-> d
    - vv <-> w
    """
    if not query:
        return [query]
    
    variants = set([query])
    query_lower = query.lower()
    
    # Character substitution mappings (source -> replacements)
    substitutions = [
        ('0', 'o'),
        ('o', '0'),
        ('1', 'l'),
        ('l', '1'),
        ('1', 'i'),
        ('i', '1'),
        ('5', 's'),
        ('s', '5'),
        ('8', 'b'),
        ('b', '8'),
        ('rn', 'm'),
        ('m', 'rn'),
        ('cl', 'd'),
        ('d', 'cl'),
        ('vv', 'w'),
        ('w', 'vv'),
    ]
    
    # Generate variants for each substitution
    for source, replacement in substitutions:
        if source in query_lower:
            # Replace first occurrence
            variant = query_lower.replace(source, replacement, 1)
            variants.add(variant)
            # Replace all occurrences
            variant_all = query_lower.replace(source, replacement)
            variants.add(variant_all)
    
    # Also generate variants without spaces (OCR sometimes drops spaces)
    no_space = query_lower.replace(' ', '')
    if len(no_space) > 3:
        variants.add(no_space)
    
    # Limit to prevent too many variants
    return list(variants)[:8]


def _parse_slash_command(query: str) -> Optional[Dict[str, str]]:
    """
    Parse slash commands in the main search bar (V3).
    Supported prefixes: both backslash (\\) and forward slash (/)
    
    Commands:
    - /uid:<id> or /<id> (if valid format) -> Smart ID
    - /ext:<ext> or /<ext> (if known ext)  -> File Extension
    - /p:<val> or /person:<val>            -> Key Names (Person)
    - /l:<val> or /loc:<val>               -> Location Mentioned
    - /d:<val> or /date:<val>              -> Important Dates
    - /c:<val> or /conf:<val>              -> Confidentiality
    - /cat:<val> or /category:<val>        -> Category
    - /dept:<val> or /department:<val>     -> Department
    - /purp:<val> or /purpose:<val>        -> Purpose
    - /<term> (generic)                    -> All Tag/Entity Fields (Strict)
    """
    if not (query.startswith("\\") or query.startswith("/")):
        return None

    # Remove the prefix
    body = query[1:].strip()
    if not body:
        return None

    # Helper for prefix matching
    def check_prefix(text: str, prefixes: List[str]) -> Optional[str]:
        text_lower = text.lower()
        for p in prefixes:
            if text_lower.startswith(p + ":") or text_lower.startswith(p + " "):
                # Return the value part
                return text[len(p)+1:].strip()
            elif text_lower.startswith(p + "="):
                return text[len(p)+1:].strip()
        return None

    # 1. Explicit Modes
    
    # UID
    val = check_prefix(body, ["uid"])
    if val: return {"mode": "uid", "value": val}

    # Extension
    val = check_prefix(body, ["ext", "type"])
    if val: return {"mode": "ext", "value": val.lstrip(".")}

    # Person / Key Names
    val = check_prefix(body, ["p", "person", "people", "name"])
    if val: return {"mode": "person", "value": val}

    # Location
    val = check_prefix(body, ["l", "loc", "location"])
    if val: return {"mode": "location", "value": val}

    # Date
    val = check_prefix(body, ["d", "date"])
    if val: return {"mode": "date", "value": val}

    # Confidentiality
    val = check_prefix(body, ["c", "conf", "confidentiality"])
    if val: return {"mode": "confidentiality", "value": val}

    # Category
    val = check_prefix(body, ["cat", "category"])
    if val: return {"mode": "category", "value": val}

    # Department
    val = check_prefix(body, ["dept", "department", "dep"])
    if val: return {"mode": "department", "value": val}

    # Purpose
    val = check_prefix(body, ["purp", "purpose"])
    if val: return {"mode": "purpose", "value": val}

    # 2. Implicit / Inference Modes

    # Infer Smart ID syntax: FIN-20260210-A7B2
    if re.match(r"^[A-Za-z]{2,8}-\d{8}-[A-Za-z0-9]{4,}$", body):
        return {"mode": "uid", "value": body}

    # Infer extension for common one-token extensions
    if " " not in body:
        token = body.lower().lstrip(".")
        known_ext = {
            "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
            "txt", "csv", "json", "xml", "html", "htm",
            "jpg", "jpeg", "png", "gif", "bmp", "tiff", "zip",
            "msg", "eml"
        }
        if token in known_ext:
            return {"mode": "ext", "value": token}

    # 3. Fallback: Generic Tag Search (only tagged metadata fields, NOT content)
    return {"mode": "tag", "value": body}


def _build_strict_slash_query(
    slash_cmd: Dict[str, str],
    source_fields: List[str],
    highlight_block: Dict[str, Any],
    limit: int,
) -> Dict[str, Any]:
    """
    Build STRICT term-only query for slash command mode.
    CRITICAL requirement: MUST NOT search content fields (main_content, ocr_content, etc).
    Only searches metadata/structural fields.
    """
    mode = slash_cmd.get("mode", "")
    value = (slash_cmd.get("value") or "").strip()
    
    # Handle wildcards if user didn't provide them but might expect prefix search
    # For now, we treat input as exact or wildcard depending on user input
    # normalized_val = value.lower()

    if not value:
        return {"query": {"match_none": {}}, "size": limit, "_source": source_fields}

    bool_query = {}

    if mode == "uid":
        bool_query = {
            "bool": {
                "should": [
                    {"term": {"smart_id": {"value": value, "boost": 20}}},
                    {"term": {"smart_id.text": {"value": value, "boost": 5}}},
                ],
                "minimum_should_match": 1,
            }
        }
    
    elif mode == "ext":
        ext = value.lower().lstrip(".")
        # Map common extensions to MIME types for broader matching
        mime_map = {
            "pdf": ["application/pdf"],
            "doc": ["application/msword"],
            "docx": ["application/vnd.openxmlformats-officedocument.wordprocessingml.document"],
            "xls": ["application/vnd.ms-excel"],
            "xlsx": ["application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"],
            "ppt": ["application/vnd.ms-powerpoint"],
            "pptx": ["application/vnd.openxmlformats-officedocument.presentationml.presentation"],
            "txt": ["text/plain"],
            "csv": ["text/csv"],
            "xml": ["application/xml", "text/xml"],
            "json": ["application/json"],
            "zip": ["application/zip", "application/x-zip-compressed"],
            "jpg": ["image/jpeg"],
            "jpeg": ["image/jpeg"],
            "png": ["image/png"],
        }
        
        should_clauses = [
            {"term": {"file_type": {"value": ext, "boost": 20}}},
            {"wildcard": {"file_name.keyword": {"value": f"*.{ext}", "boost": 5}}},
        ]
        
        for mime in mime_map.get(ext, []):
            should_clauses.append({"term": {"mime_type": {"value": mime, "boost": 10}}})
            
        bool_query = {
            "bool": {
                "should": should_clauses,
                "minimum_should_match": 1,
            }
        }

    elif mode == "person":
        # Search only key_names
        # Use simple_query_string to allow basic operators if needed, or stick to terms
        bool_query = {
            "bool": {
                "should": [
                    {"term": {"key_names": {"value": value, "case_insensitive": True, "boost": 10}}},
                    {"match_phrase": {"key_names": {"query": value, "boost": 5}}},
                ],
                "minimum_should_match": 1
            }
        }

    elif mode == "location":
        bool_query = {
            "bool": {
                "should": [
                    {"term": {"location_mentioned": {"value": value, "case_insensitive": True, "boost": 10}}},
                    {"match_phrase": {"location_mentioned": {"query": value, "boost": 5}}},
                ],
                "minimum_should_match": 1
            }
        }

    elif mode == "date":
        bool_query = {
            "bool": {
                "should": [
                    {"term": {"important_dates": {"value": value, "case_insensitive": True, "boost": 10}}},
                    {"match_phrase": {"important_dates": {"query": value, "boost": 5}}},
                ],
                "minimum_should_match": 1
            }
        }

    elif mode == "confidentiality":
        bool_query = {
            "bool": {
                "should": [
                    {"term": {"confidentiality": {"value": value, "case_insensitive": True, "boost": 10}}},
                ],
                "minimum_should_match": 1
            }
        }

    elif mode == "category":
        # Search ONLY the category field
        bool_query = {
            "bool": {
                "should": [
                    {"term": {"category": {"value": value, "case_insensitive": True, "boost": 20}}},
                    {"match_phrase": {"category": {"query": value, "boost": 10}}},
                    {"term": {"category.text": {"value": value.lower(), "boost": 5}}},
                ],
                "minimum_should_match": 1
            }
        }

    elif mode == "department":
        # Search ONLY the department field
        bool_query = {
            "bool": {
                "should": [
                    {"term": {"department": {"value": value, "case_insensitive": True, "boost": 20}}},
                    {"match_phrase": {"department": {"query": value, "boost": 10}}},
                    {"term": {"department.text": {"value": value.lower(), "boost": 5}}},
                ],
                "minimum_should_match": 1
            }
        }

    elif mode == "purpose":
        # Search ONLY the purpose field
        bool_query = {
            "bool": {
                "should": [
                    {"term": {"purpose": {"value": value, "case_insensitive": True, "boost": 20}}},
                    {"match_phrase": {"purpose": {"query": value, "boost": 10}}},
                    {"term": {"purpose.text": {"value": value.lower(), "boost": 5}}},
                ],
                "minimum_should_match": 1
            }
        }

    else:
        # Generic Tag Mode
        # STRICTLY search all metadata fields, NO content fields.
        # Only returns documents that have been tagged (category is not empty).
        variants = list({value, value.lower(), value.title(), value.upper()})
        
        # We target all "tag-like" lists and keyword fields
        fields_to_check = [
            "category", "department", "purpose", "dynamic_subtags",
            "key_names", "location_mentioned", "important_dates", "confidentiality"
        ]
        
        should_clauses = []
        for field in fields_to_check:
             should_clauses.append({"terms": {field: variants, "boost": 10}})
             should_clauses.append({"match_phrase": {f"{field}": {"query": value, "boost": 5}}})

        bool_query = {
            "bool": {
                "should": should_clauses,
                "minimum_should_match": 1,
                "filter": [
                    # Only return tagged documents
                    {"exists": {"field": "category"}}
                ]
            }
        }

    # Wrap with must_not filter to exclude embedded files from all slash queries
    wrapped_query = {
        "bool": {
            "must": bool_query
        }
    }

    return {
        "query": wrapped_query,
        "highlight": highlight_block,
        "size": limit,
        "_source": source_fields,
    }


def perform_search(os_client: OpenSearchClient, query: str, limit: int = 20, filters: Optional[Dict[str, List[str]]] = None) -> List[Dict[str, Any]]:
    """
    Perform accurate search on OpenSearch optimized for audit/document discovery.
    
    Search Modes:
    1. EXACT PHRASE MODE (quotes): Only returns documents with exact phrase match
    2. MULTI-TERM MODE (no quotes): Balanced precision/recall with proper ranking
    
    Ranking Priority (highest to lowest):
    1. Exact phrase match in filename
    2. Exact phrase match in content
    3. All terms present (AND match)
    4. Most terms present (75%+ match)
    5. Fuzzy matches (for typos)
    """
    # Normalize user query: trim and collapse whitespace
    normalized_query = re.sub(r"\s+", " ", query.strip())
    
    if not normalized_query:
        return []

    slash_command = _parse_slash_command(normalized_query)

    # Check if query is in quotes for STRICT exact phrase search
    is_phrase = normalized_query.startswith('"') and normalized_query.endswith('"')
    if is_phrase:
        normalized_query = normalized_query.strip('"')
        # Issue 15: Guard against empty quoted query
        if not normalized_query.strip():
            return []
    
    # Highlight configuration
    highlight_block = {
        "fields": {
            "file_name": {"number_of_fragments": 0},
            "main_content": {"fragment_size": 200, "number_of_fragments": 3},
            "embedded_content": {"fragment_size": 200, "number_of_fragments": 2},
            "ocr_content": {"fragment_size": 200, "number_of_fragments": 3},
            "tika_content": {"fragment_size": 200, "number_of_fragments": 2},
            "reviewed_content": {"fragment_size": 200, "number_of_fragments": 3},
        },
        "pre_tags": ["**"],
        "post_tags": ["**"],
        "require_field_match": False,
        "type": "unified"
    }
    
    # Source fields to return
    source_fields = [
        "file_name",
        "file_path",
        "file_hash",
        "file_size",
        "mime_type",
        "indexed_at",
        "metadata",
        "main_content",
        "tika_content",
        "embedded_content",
        "embedded_files",
        "ocr_content",
        "ocr_confidence",
        "ocr_completed",
        "smart_id",
        "is_embedded",
        "parent_file",
        "parent_path",
        "parent_name",
        "category",
        "department",
        "purpose",
        "dynamic_subtags",
        "file_type",
        "reviewed_content",
    ]

    if slash_command:
        search_query = _build_strict_slash_query(
            slash_cmd=slash_command,
            source_fields=source_fields,
            highlight_block=highlight_block,
            limit=limit,
        )
        normalized_query = slash_command.get("value", normalized_query)
        is_phrase = False
    elif is_phrase:
        # =======================================================================
        # EXACT PHRASE SEARCH MODE - STRICT MATCHING ONLY
        # =======================================================================
        # When user uses quotes, they want ONLY exact phrase matches.
        # NO fuzzy matching, NO partial matching, NO synonym expansion.
        # Uses a bool.must with a dis_max (best-field) across content fields
        # so the phrase MUST appear verbatim in at least one field.
        # Boosting controls ranking: filename > content > OCR > embedded.
        # Using the 'standard' sub-field avoids English-analyzer stemming
        # that could loosen the phrase match.
        
        search_query = {
            "query": {
                "bool": {
                    "must": {
                        "dis_max": {
                            "queries": [
                                # Exact phrase in filename (highest priority)
                                {"match_phrase": {"file_name": {"query": normalized_query, "boost": 100, "slop": 0}}},
                                # Exact phrase in file path
                                {"match_phrase": {"file_path": {"query": normalized_query, "boost": 60, "slop": 0}}},
                                # Exact phrase in main content - standard analyzer (no stemming)
                                {"match_phrase": {"main_content.standard": {"query": normalized_query, "boost": 40, "slop": 0}}},
                                {"match_phrase": {"main_content": {"query": normalized_query, "boost": 35, "slop": 0}}},
                                # Exact phrase in OCR content (slop=1 for minor OCR spacing issues)
                                {"match_phrase": {"ocr_content.standard": {"query": normalized_query, "boost": 40, "slop": 1}}},
                                {"match_phrase": {"ocr_content": {"query": normalized_query, "boost": 35, "slop": 1}}},
                                # Exact phrase in embedded content
                                {"match_phrase": {"embedded_content": {"query": normalized_query, "boost": 30, "slop": 0}}},
                                # Exact phrase in reviewer-typed snippet labels
                                {"match_phrase": {"reviewed_content": {"query": normalized_query, "boost": 50, "slop": 1}}},
                            ],
                            "tie_breaker": 0.1
                        }
                    }
                }
            },
            "highlight": highlight_block,
            "size": limit,
            "_source": source_fields
        }
    else:
        # =======================================================================
        # MULTI-TERM SEARCH MODE - Balanced precision and recall
        # =======================================================================
        # Issue 7: file_path (keyword mapping) removed from text tiers to avoid
        # fuzzy/cross_fields noise. It's still in phrase/exact tiers above.
        content_fields = [
            "file_name^15",            # Highest: filename match most relevant
            "file_name.english^10",    # English-analyzed filename
            "main_content^6",          # Medium-high: primary document content
            "main_content.standard^4", # Standard analyzer fallback
            "ocr_content^6",           # High priority for OCR (scanned docs important)
            "ocr_content.standard^4",  # Standard analyzer for OCR
            "tika_content^2",          # Lower: original Tika text (may be corrupted)
            "embedded_content^3",      # Lower: reduces Excel data cell noise
            "reviewed_content^8",      # High: reviewer-verified snippet labels (signatures, stamps)
            "reviewed_content.english^6",
        ]
        
        # Generate OCR-corrected variants for fuzzy tier
        ocr_variants = _generate_ocr_variants(normalized_query)
        
        bool_clauses = {
            "should": [
                # --- TIER 1: Exact Matches (Highest Priority - 100+ boost) ---
                {"term": {"file_name.keyword": {"value": normalized_query, "boost": 100}}},
                
                # --- TIER 2: Exact Phrase Matches (High Priority - 50-80 boost) ---
                {"match_phrase": {"file_name": {"query": normalized_query, "boost": 80, "slop": 0}}},
                {"match_phrase": {"file_path": {"query": normalized_query, "boost": 60, "slop": 0}}},
                {"match_phrase": {"main_content": {"query": normalized_query, "boost": 50, "slop": 0}}},
                {"match_phrase": {"ocr_content": {"query": normalized_query, "boost": 50, "slop": 1}}},
                {"match_phrase": {"embedded_content": {"query": normalized_query, "boost": 40, "slop": 0}}},
                
                # --- TIER 2.5: Prefix Matches (enables partial word highlighting) ---
                {"match_phrase_prefix": {"file_name": {"query": normalized_query, "boost": 70, "max_expansions": 20}}},
                {"match_phrase_prefix": {"main_content": {"query": normalized_query, "boost": 45, "max_expansions": 20}}},
                {"match_phrase_prefix": {"ocr_content": {"query": normalized_query, "boost": 45, "max_expansions": 20}}},
                {"match_phrase_prefix": {"embedded_content": {"query": normalized_query, "boost": 35, "max_expansions": 20}}},
                {"match_phrase_prefix": {"reviewed_content": {"query": normalized_query, "boost": 50, "max_expansions": 20}}},

                # --- TIER 3: All Terms Present (AND match - 30-40 boost) ---
                {
                    "multi_match": {
                        "query": normalized_query,
                        "type": "cross_fields",
                        "fields": content_fields,
                        "operator": "and",
                        "boost": 35
                    }
                },
                
                # --- TIER 4: Most Terms Match (75%+ - 20 boost) ---
                {
                    "multi_match": {
                        "query": normalized_query,
                        "type": "best_fields",
                        "fields": content_fields,
                        "operator": "or",
                        "minimum_should_match": "75%",
                        "boost": 20
                    }
                }
            ],
            "minimum_should_match": 1
        }
        
        # --- TIER 5: Fuzzy Matching for Typos (10 boost) - Skip for numeric terms ---
        fuzzy_terms = [t for t in normalized_query.split() if not re.sub(r'(?<=\d)[,.]+(?=\d)', '', t).isdigit()]
        if fuzzy_terms:
            bool_clauses["should"].append({
                "multi_match": {
                    "query": " ".join(fuzzy_terms),
                    "fields": content_fields,
                    "type": "best_fields",
                    "fuzziness": "AUTO",
                    "prefix_length": 3,
                    "max_expansions": 30,
                    "boost": 10
                }
            })
        
        # --- TIER 6: Wildcard Prefix Matching for Numeric Terms (5 boost) ---
        for term in normalized_query.split():
            clean_term = re.sub(r'(?<=\d)[,.]+(?=\d)', '', term)
            if clean_term.isdigit() and len(clean_term) >= 3:
                # Map standard content fields without boosts for query_string
                raw_fields = [f.split('^')[0] for f in content_fields]
                bool_clauses["should"].append({
                    "query_string": {
                        "query": f"{clean_term}*",
                        "fields": raw_fields,
                        "analyze_wildcard": True,
                        "boost": 5
                    }
                })
        
        # Add OCR variant searches for common OCR misreads
        for variant in ocr_variants:
            if variant != normalized_query and variant != normalized_query.lower():
                bool_clauses["should"].append({
                    "match_phrase": {
                        "ocr_content": {
                            "query": variant,
                            "boost": 15,
                            "slop": 1
                        }
                    }
                })
        
        # Wrap with function_score for recency and quality boosting
        # Also add must_not filter to exclude embedded files
        search_query = {
            "query": {
                "bool": {
                    "must": {
                        "function_score": {
                            "query": {"bool": bool_clauses},
                            "functions": [
                                # Recency boost: prefer newer documents
                                {
                                    "gauss": {
                                        "indexed_at": {
                                            "scale": "90d",
                                            "offset": "7d",
                                            "decay": 0.5
                                        }
                                    },
                                    "weight": 1.2
                                },
                                # Boost documents with high OCR confidence
                                {
                                    "filter": {"range": {"ocr_confidence": {"gte": 80}}},
                                    "weight": 1.3
                                },
                                # Slight penalty for low OCR confidence
                                {
                                    "filter": {"range": {"ocr_confidence": {"lt": 50, "gte": 0}}},
                                    "weight": 0.8
                                }
                            ],
                            "score_mode": "sum",
                            "boost_mode": "multiply"
                        }
                    }
                }
            },
            "highlight": highlight_block,
            "size": limit,
            "_source": source_fields
        }

        # Inject highlight_query to control exactly what gets highlighted.
        # Uses match_phrase_prefix for prefix highlighting + exact match.
        # This prevents fuzzy tier from highlighting irrelevant words.
        hl_fields = ["main_content", "ocr_content", "file_name", "embedded_content", "reviewed_content"]
        hl_should = [{"match": {f: {"query": normalized_query}}} for f in hl_fields]
        if len(normalized_query) >= 4:
            hl_should += [{"match_phrase_prefix": {f: {"query": normalized_query, "max_expansions": 10}}} for f in hl_fields]
        search_query["highlight"]["highlight_query"] = {"bool": {"should": hl_should}}

    # --- Always exclude OCR-pending documents from search results ---
    # A document with needs_ocr=true is only searchable once ocr_completed=true.
    # This filter applies to every search mode (slash, phrase, multi-term).
    ocr_ready_filter = {
        "bool": {
            "should": [
                {"term": {"needs_ocr": False}},
                {"term": {"ocr_completed": True}},
            ],
            "minimum_should_match": 1,
        }
    }
    outer_bool = search_query.get("query", {}).get("bool")
    if outer_bool is not None:
        existing_filter = outer_bool.get("filter", [])
        if isinstance(existing_filter, dict):
            existing_filter = [existing_filter]
        outer_bool["filter"] = existing_filter + [ocr_ready_filter]
    else:
        original_query = search_query["query"]
        search_query["query"] = {
            "bool": {
                "must": original_query,
                "filter": [ocr_ready_filter],
            }
        }

    # --- Inject multiselect filters as bool.filter clauses ---
    if filters:
        filter_clauses = []
        for field_name, values in filters.items():
            if values:
                # category/department/purpose/file_type are already keyword type in mapping
                kw_field = field_name
                if field_name not in ("category", "department", "purpose", "file_type", "dynamic_subtags", "key_names"):
                    kw_field = f"{field_name}.keyword"
                filter_clauses.append({"terms": {kw_field: values}})
        if filter_clauses:
            # Navigate into the outermost bool
            outer_bool = search_query.get("query", {}).get("bool")
            if outer_bool is not None:
                existing_filter = outer_bool.get("filter", [])
                if isinstance(existing_filter, dict):
                    existing_filter = [existing_filter]
                outer_bool["filter"] = existing_filter + filter_clauses
            else:
                # Wrap existing query in a bool with filter
                original_query = search_query["query"]
                search_query["query"] = {
                    "bool": {
                        "must": original_query,
                        "filter": filter_clauses,
                    }
                }

    # Execute search with robust error handling
    use_manual_snippets = False
    try:
        # Check if index exists first
        if not os_client.client.indices.exists(index=os_client.index_name):
            return []  # Return empty results if index doesn't exist
        
        response = os_client.client.search(
            index=os_client.index_name,
            body=search_query
        )
    except RequestError as err:
        error_msg = str(err)
        # Handle index not found
        if "index_not_found" in error_msg.lower() or "no such index" in error_msg.lower():
            return []  # Return empty results
        # Fallback for highlight-size errors: retry without highlights, extract manually
        elif any(keyword in error_msg.lower() for keyword in ['highlight', 'analyzed_offset', 'analyzer offset']):
            # Remove highlights entirely, will extract snippets manually
            search_query.pop("highlight", None)
            use_manual_snippets = True
            try:
                response = os_client.client.search(
                    index=os_client.index_name,
                    body=search_query
                )
            except Exception as retry_err:
                # If still failing, something else is wrong
                raise Exception(f"Search failed even without highlights: {retry_err}")
        else:
            # Different error, re-raise
            raise
    
    results = []
    for hit in response['hits']['hits']:
        source = hit['_source']
        
        # Keep embedded files (zips/attachments/nested content) as search results.
        # Lineage details are displayed below the filename in the UI.
        
        result = {
            'id': hit['_id'],
            'score': hit['_score'],
            'filename': source.get('file_name', 'Unknown'),
            'filepath': source.get('file_path', ''),
            'file_hash': source.get('file_hash', ''),
            'file_size': source.get('file_size', 0),
            'mime_type': source.get('mime_type', ''),
            'indexed_at': source.get('indexed_at', ''),
            'metadata': source.get('metadata', {}),
            'embedded_files': source.get('embedded_files', []),
            'highlights': hit.get('highlight', {}),
            'snippet': '',
            'matched_field': 'unknown',
            'ocr_confidence': source.get('ocr_confidence'),
            'ocr_completed': source.get('ocr_completed', False),
            'has_ocr_content': bool(source.get('ocr_content')),
            'main_content': source.get('main_content', ''),
            'embedded_content': source.get('embedded_content', ''),
            'ocr_content': source.get('ocr_content', ''),
            'tika_content': source.get('tika_content', ''),
            'reviewed_content': source.get('reviewed_content', ''),
            'is_embedded': source.get('is_embedded', False),
            'parent_file': source.get('parent_file', ''),
            'parent_path': source.get('parent_path', ''),
            'parent_name': source.get('parent_name', '')
        }
        
        # Extract snippet from highlights or manually
        highlights = hit.get('highlight', {})
        
        if highlights and not use_manual_snippets:
            # Use OpenSearch highlights when available
            # reviewed_content checked first when it matches — it is the most
            # precise signal (reviewer-verified label for this exact document).
            if 'reviewed_content' in highlights and source.get('reviewed_content'):
                result['snippet'] = ' ... '.join(highlights['reviewed_content'][:3])
                result['matched_field'] = 'reviewed_content'
            elif 'main_content' in highlights and source.get('main_content'):
                result['snippet'] = ' ... '.join(highlights['main_content'][:3])
                result['matched_field'] = 'main_content'
            elif 'embedded_content' in highlights and source.get('embedded_content'):
                # Only use embedded_content if it has substantial content
                # Filter out DOCX metadata paths like /docProps/thumbnail.jpeg
                embedded = source.get('embedded_content', '').strip()
                is_metadata_path = embedded.startswith('/docProps/') or embedded.startswith('docProps/')
                has_real_content = len(embedded) > 50 and not is_metadata_path

                if has_real_content:
                    result['snippet'] = ' ... '.join(highlights['embedded_content'][:2])
                    result['matched_field'] = 'embedded_content'
            elif 'ocr_content' in highlights and source.get('ocr_content'):
                result['snippet'] = ' ... '.join(highlights['ocr_content'][:2])
                result['matched_field'] = 'ocr_content'
            elif 'file_name' in highlights:
                result['snippet'] = highlights['file_name'][0]
                result['matched_field'] = 'file_name'

        # Manual snippet extraction when highlights unavailable or failed
        if not result['snippet']:
            # reviewed_content first — exact reviewer-typed label
            if source.get('reviewed_content'):
                result['snippet'] = extract_snippet_manually(source['reviewed_content'], normalized_query, 180)
                result['matched_field'] = 'reviewed_content' if result['snippet'] else 'none'
            if not result['snippet'] and source.get('main_content'):
                result['snippet'] = extract_snippet_manually(source['main_content'], normalized_query, 180)
                result['matched_field'] = 'main_content' if result['snippet'] else 'none'
            if not result['snippet'] and source.get('embedded_content'):
                # Only use embedded_content if it has substantial content
                # Filter out DOCX metadata paths
                embedded = source.get('embedded_content', '').strip()
                is_metadata_path = embedded.startswith('/docProps/') or embedded.startswith('docProps/')
                has_real_content = len(embedded) > 50 and not is_metadata_path

                if has_real_content:
                    result['snippet'] = extract_snippet_manually(source['embedded_content'], normalized_query, 180)
                    result['matched_field'] = 'embedded_content' if result['snippet'] else 'none'
            if not result['snippet'] and source.get('ocr_content'):
                result['snippet'] = extract_snippet_manually(source['ocr_content'], normalized_query, 180)
                result['matched_field'] = 'ocr_content' if result['snippet'] else 'none'
        
        # Fallback: show metadata if still no snippet
        if not result['snippet']:
            metadata = source.get('metadata', {})
            if metadata:
                preview_parts = []
                for key, value in list(metadata.items())[:3]:
                    if isinstance(value, str) and len(value) < 100:
                        preview_parts.append(f"{key}: {value}")
                if preview_parts:
                    result['snippet'] = ' | '.join(preview_parts)
                    result['matched_field'] = 'metadata'
        
        # Final fallback: filename only
        if not result['snippet']:
            result['snippet'] = result['filename']
            result['matched_field'] = 'filename'
        
        results.append(result)
    
    return results


def _find_embedded_pdf_path(result: Dict[str, Any], query: str) -> Optional[Path]:
    """For a zip/archive result matched on embedded_content, locate the extracted PDF on disk.

    Returns the Path of the first embedded PDF whose content contains the query,
    or the first PDF found in the extraction dir if content matching fails.
    Returns None if nothing is found.
    """
    filepath = result.get('filepath', '')
    if not filepath:
        return None
    if Path(filepath).suffix.lower() not in {'.zip', '.tar', '.gz'}:
        return None

    file_hash = result.get('file_hash', '')
    if not file_hash:
        return None

    try:
        cfg = get_config()
        embedded_root = Path(cfg.paths.working_root) / 'data' / 'embedded' / file_hash
    except Exception:
        return None

    if not embedded_root.exists():
        return None

    query_lower = query.lower()
    embedded_files: List[Dict[str, Any]] = result.get('embedded_files', [])

    # First pass: match by content
    for emb in embedded_files:
        name = emb.get('name', '')
        content = emb.get('content', '')
        if not name or Path(name).suffix.lower() != '.pdf':
            continue
        if query_lower in content.lower():
            safe_name = name.replace('/', '_').replace('\\', '_')
            candidate = embedded_root / safe_name
            if candidate.exists():
                return candidate

    # Second pass: return first PDF in extraction dir
    try:
        for f in sorted(embedded_root.iterdir()):
            if f.suffix.lower() == '.pdf' and f.is_file():
                return f
    except Exception:
        pass

    return None


def _tika_cells_to_rows(text: str) -> Optional[str]:
    """Detect Tika's one-cell-per-line table format and reassemble into proper rows.

    Tika /rmeta/text renders DOCX tables as:
        \\tCell1\\n\\tCell2\\n\\tCell3\\n\\tCell4\\n\\n   <- row (blank line = row boundary)

    Returns reassembled tab-separated rows if the pattern is detected, else None.
    """
    import re
    # A "tika cell line" is a line that is exactly \\t<value> (one leading tab, no other tabs)
    cell_line_re = re.compile(r'^\t[^\t]*$')
    lines = text.splitlines()

    # Quick check: do we have cell lines at all?
    cell_positions = [i for i, ln in enumerate(lines) if cell_line_re.match(ln)]
    if not cell_positions:
        return None

    # Pre-normalize: first cell of a row-group sometimes lacks leading \t
    normalized_lines = []
    for i, ln in enumerate(lines):
        if (ln.strip() and not ln.startswith('\t')
                and i + 1 < len(lines) and cell_line_re.match(lines[i + 1])
                and (i == 0 or not lines[i - 1].strip())):
            normalized_lines.append('\t' + ln)
        else:
            normalized_lines.append(ln)
    lines = normalized_lines

    # Group consecutive cell lines separated by blank lines into row-groups
    row_groups: list = []
    current: list = []
    for ln in lines:
        if cell_line_re.match(ln):
            current.append(ln.lstrip('\t').strip())
        else:
            if current:
                row_groups.append(current)
                current = []
    if current:
        row_groups.append(current)

    if not row_groups:
        return None

    # Infer column count from the first (header) row group
    n_cols = len(row_groups[0])
    if n_cols < 2:
        return None

    # Every group must have exactly n_cols cells; if not, fall back
    if not all(len(g) == n_cols for g in row_groups):
        return None

    # Reassemble as tab-separated rows
    return '\n'.join('\t'.join(g) for g in row_groups)


def _format_table_content(text: str) -> str:
    """Convert Tika table content into a padded ASCII table. Non-table text passes through."""
    if not text or '\t' not in text:
        return text

    # Handle Tika's one-cell-per-line format (each \\tCell on its own line)
    reassembled = _tika_cells_to_rows(text)
    work_text = reassembled if reassembled is not None else text

    lines = work_text.splitlines()
    table_indices = set(i for i, ln in enumerate(lines) if '\t' in ln)
    if not table_indices:
        return text

    row_map = {i: [c.strip() for c in lines[i].split('\t')] for i in table_indices}
    # Strip the empty leading cell produced by a leading tab
    for i in row_map:
        if row_map[i] and row_map[i][0] == '':
            row_map[i] = row_map[i][1:]

    max_cols = max(len(r) for r in row_map.values()) if row_map else 1
    col_widths = [0] * max_cols
    for row in row_map.values():
        for j, cell in enumerate(row):
            if j < max_cols:
                col_widths[j] = max(col_widths[j], len(cell))

    sep = '+' + '+'.join('-' * (w + 2) for w in col_widths) + '+'

    def fmt_row(cells):
        parts = []
        for j in range(max_cols):
            cell = cells[j] if j < len(cells) else ''
            parts.append(f' {cell:<{col_widths[j]}} ')
        return '|' + '|'.join(parts) + '|'

    result = []
    in_table = False
    first_row_of_table = True
    for i, line in enumerate(lines):
        if i in table_indices:
            if not in_table:
                result.append(sep)
                in_table = True
                first_row_of_table = True
            result.append(fmt_row(row_map[i]))
            if first_row_of_table:
                result.append(sep)
                first_row_of_table = False
        else:
            if in_table:
                result.append(sep)
                in_table = False
            result.append(line)
    if in_table:
        result.append(sep)

    return '\n'.join(result)


def get_downloadable_text(result: Dict[str, Any]) -> str:
    """Combine extracted text from document fields (metadata, main, embedded, OCR) into a printable format."""
    parts = []
    
    parts.append(f"================================================================================")
    parts.append(f"DOCUMENT METADATA")
    parts.append(f"================================================================================")
    parts.append(f"Filename: {result.get('filename', 'Unknown')}")
    parts.append(f"File Path: {result.get('filepath', '')}")
    parts.append(f"MIME Type: {result.get('mime_type', '')}")
    if result.get('ocr_confidence') is not None:
        raw_conf = float(result.get('ocr_confidence') or 0)
        # Stored as 0–1 (e.g. 0.85) or 0–100 (e.g. 85.0) depending on pipeline version
        pct = raw_conf * 100 if raw_conf <= 1.0 else raw_conf
        parts.append(f"OCR Confidence: {pct:.1f}%")
    parts.append("")
    parts.append(f"================================================================================")
    parts.append(f"EXTRACTED CONTENT")
    parts.append(f"================================================================================")
    parts.append("")
    
    has_content = False

    main_content = result.get('main_content', '')
    ocr_content = result.get('ocr_content', '')

    if ocr_content and str(ocr_content).strip():
        # Hybrid OCR extraction: OCR text (accurate) + table structure from standard extraction
        parts.append("--- [OCR EXTRACTION] ---")
        ocr_text = _html.unescape(str(ocr_content).strip())
        main_text = str(main_content).strip() if main_content else ''
        main_has_tables = main_text and '\t' in main_text
        if main_has_tables:
            # Extract formatted tables from standard extraction and append after OCR text
            table_lines = []
            for line in main_text.splitlines():
                if '\t' in line:
                    table_lines.append(line)
                elif table_lines:
                    # Non-table line after table block — keep boundary
                    table_lines.append('')
            if table_lines:
                formatted_tables = _format_table_content('\n'.join(table_lines))
                parts.append(ocr_text)
                parts.append("")
                parts.append("--- [TABLE STRUCTURE] ---")
                parts.append(formatted_tables)
            else:
                parts.append(ocr_text)
        else:
            parts.append(ocr_text)
        parts.append("")
        has_content = True
    elif main_content and str(main_content).strip():
        parts.append("--- [STANDARD DOCUMENT EXTRACTION] ---")
        parts.append(_format_table_content(_html.unescape(str(main_content).strip())))
        parts.append("")
        has_content = True

    embedded_content = result.get('embedded_content', '')
    _emb = str(embedded_content).strip()
    _is_office_path = _emb.startswith('/docProps/') or _emb.startswith('docProps/') or _emb.startswith('/word/') or _emb.startswith('/xl/')
    if _emb and not _is_office_path:
        parts.append("--- [EMBEDDED CONTENT] ---")
        parts.append(_emb)
        parts.append("")
        has_content = True

    tika_content = result.get('tika_content', '')
    if tika_content and str(tika_content).strip():
        parts.append("--- [TIKA ORIGINAL EXTRACTION (corrupted/garbled text preserved)] ---")
        parts.append(str(tika_content).strip())
        parts.append("")
        has_content = True
        
    reviewed_content = result.get('reviewed_content', '')
    if reviewed_content and str(reviewed_content).strip():
        parts.append("--- [VERIFIED REVIEW LABEL] ---")
        parts.append(str(reviewed_content).strip())
        parts.append("")
        has_content = True
        
    if not has_content:
        parts.append("(No text content was extracted from this document.)")
        
    return "\n".join(parts)


def _fetch_child_documents(os_client: OpenSearchClient, parent_hash: str) -> List[Dict[str, Any]]:
    """Return all child documents (is_embedded=true) whose parent_file matches parent_hash."""
    try:
        resp = os_client.client.search(
            index=os_client.index_name,
            body={
                "query": {
                    "bool": {
                        "must": [
                            {"term": {"parent_file": parent_hash}},
                        ],
                        "filter": [
                            {"bool": {
                                "should": [
                                    {"term": {"needs_ocr": False}},
                                    {"term": {"ocr_completed": True}},
                                ],
                                "minimum_should_match": 1,
                            }}
                        ]
                    }
                },
                "size": 50,
                "_source": ["file_name", "file_path", "file_hash", "file_size",
                            "mime_type", "main_content", "ocr_content",
                            "ocr_confidence", "needs_ocr", "ocr_completed",
                            "parent_name", "parent_path", "is_embedded"],
            }
        )
        children = []
        for hit in resp["hits"]["hits"]:
            s = hit["_source"]
            children.append({
                "id": hit["_id"],
                "filename": s.get("file_name", "Unknown"),
                "filepath": s.get("file_path", ""),
                "file_hash": s.get("file_hash", ""),
                "file_size": s.get("file_size", 0),
                "mime_type": s.get("mime_type", ""),
                "main_content": s.get("main_content", ""),
                "ocr_content": s.get("ocr_content", ""),
                "ocr_confidence": s.get("ocr_confidence"),
                "parent_name": s.get("parent_name", ""),
                "parent_path": s.get("parent_path", ""),
                "is_embedded": True,
            })
        return children
    except Exception:
        return []


def _render_child_file_card(child: Dict[str, Any], key_prefix: str) -> None:
    """Render a single embedded child file card inside a parent ZIP result."""
    st.markdown(
        f'<div style="background:#F0F4FF;border-left:3px solid #1E40AF;'
        f'border-radius:6px;padding:0.6rem 0.8rem;margin:0.3rem 0;">',
        unsafe_allow_html=True,
    )
    c1, c2 = st.columns([5, 1])
    with c1:
        st.markdown(
            f'<span style="font-size:0.92rem;font-weight:600;color:#1E3A8A;">📄 {_esc(child["filename"])}</span>',
            unsafe_allow_html=True,
        )
        child_size_mb = child.get("file_size", 0) / (1024 * 1024)
        child_meta = f'Path: {_esc(child["filepath"])}'
        if child_size_mb > 0:
            child_meta += f" | {child_size_mb:.2f} MB"
        if child.get("mime_type"):
            child_meta += f" | {child['mime_type']}"
        st.markdown(f'<div style="font-size:0.75rem;color:#6B7280;">{child_meta}</div>', unsafe_allow_html=True)
    with c2:
        file_opened = False
        file_open_error = None
        if st.button("Open", key=f"{key_prefix}_open", use_container_width=True):
            try:
                open_file_with_default_app(child["filepath"])
                file_opened = True
            except Exception as e:
                file_open_error = str(e)
        if file_opened:
            st.success("Opened.")
        elif file_open_error:
            st.error(f"Error: {file_open_error}")

        child_result_for_download = {**child, "embedded_content": "", "tika_content": "",
                                     "reviewed_content": "", "embedded_files": []}
        st.download_button(
            label="📥 Text",
            data=get_downloadable_text(child_result_for_download),
            file_name=f"{Path(child['filename']).stem}_extracted_text.txt",
            mime="text/plain",
            key=f"{key_prefix}_dl",
            use_container_width=True,
        )
    st.markdown("</div>", unsafe_allow_html=True)


def render_search_results(results: List[Dict[str, Any]], query: str) -> None:
    """Render search results with highlighting and OCR indicators."""
    if not results:
        st.info(f"No documents found matching '{query}'")
        return

    _last_updated = datetime.now().strftime("%H:%M:%S")
    st.markdown(f"### Found {len(results)} Result(s) &nbsp; <span style='font-size:0.75rem;color:#6B7280;font-weight:400;'>Updated {_last_updated}</span>", unsafe_allow_html=True)

    os_client = _get_os_client()

    for i, result in enumerate(results):
        with st.container():
            st.markdown('<div class="doc-card">', unsafe_allow_html=True)

            col1, col2 = st.columns([5, 1])

            with col1:
                # Show filename — badge for ZIP/archive
                fname = result["filename"]
                is_archive = Path(fname).suffix.lower() in {'.zip', '.tar', '.gz', '.rar', '.7z'}
                matched_field = result.get('matched_field', '')
                is_parent_match = matched_field == 'embedded_content' and is_archive

                if is_archive:
                    st.markdown(
                        f'<div class="doc-filename">📦 {_esc(fname)}'
                        f'<span style="font-size:0.72rem;font-weight:500;background:#DBEAFE;'
                        f'color:#1E40AF;border-radius:4px;padding:2px 7px;margin-left:8px;">Archive</span>'
                        f'</div>',
                        unsafe_allow_html=True,
                    )
                else:
                    st.markdown(f'<div class="doc-filename">{_esc(fname)}</div>', unsafe_allow_html=True)

                # Show parent-child lineage if this result IS an embedded child
                if result.get('is_embedded'):
                    st.markdown(
                        f'<div style="color:#4B5563;font-size:0.88rem;font-weight:500;'
                        f'margin-top:0.15rem;margin-bottom:0.4rem;">'
                        f'📁 Inside: <span style="color:#1E40AF;font-weight:600;">'
                        f'{_esc(result.get("parent_name"))}</span>'
                        f' <span style="color:#6B7280;font-size:0.78rem;">'
                        f'({_esc(result.get("parent_path"))})</span></div>',
                        unsafe_allow_html=True,
                    )

                # Metadata row
                file_size_mb = result.get('file_size', 0) / (1024 * 1024)
                meta_text = f"Path: {_esc(result['filepath'])}"
                if file_size_mb > 0:
                    meta_text += f" | Size: {file_size_mb:.2f} MB"
                if result.get('mime_type'):
                    meta_text += f" | MIME: {result['mime_type']}"

                field_labels = {
                    'main_content': 'Document Text',
                    'ocr_content': 'OCR Text',
                    'embedded_content': 'Embedded Files',
                    'reviewed_content': 'Verified Label',
                    'file_name': 'Filename',
                    'file_path': 'Path',
                    'metadata': 'Metadata',
                }
                confidence = result.get('score', 0)
                field_label = field_labels.get(matched_field)
                if field_label:
                    meta_text += f" | Matched: {field_label} | Score: {confidence:.1f}"
                else:
                    meta_text += f" | Score: {confidence:.1f}"
                st.markdown(f'<div class="doc-meta">{meta_text}</div>', unsafe_allow_html=True)

            with col2:
                if not is_parent_match:
                    # Normal file — open directly
                    file_opened = False
                    file_open_error = None
                    if st.button("Open", key=f"open_{i}", use_container_width=True):
                        try:
                            open_file_with_default_app(result["filepath"])
                            file_opened = True
                        except Exception as e:
                            file_open_error = str(e)
                    if file_opened:
                        st.success("File opened successfully.")
                    elif file_open_error:
                        st.error(f"Error opening file: {file_open_error}")

                download_text = get_downloadable_text(result)
                st.download_button(
                    label="📥 Download Text",
                    data=download_text,
                    file_name=f"{Path(result['filename']).stem}_extracted_text.txt",
                    mime="text/plain",
                    key=f"download_{i}",
                    use_container_width=True,
                )

            # Snippet
            snippet = result.get("snippet", "")
            if snippet:
                snippet = adjust_numeric_highlights(snippet, query)
                snippet_escaped = _esc(snippet)
                snippet_html = re.sub(r'\*\*(.+?)\*\*', r'<span class="highlight">\1</span>', snippet_escaped)
                st.markdown(f'<div class="result-snippet">{snippet_html}</div>', unsafe_allow_html=True)

            # ── Child files section ─────────────────────────────────────────
            # If this is an archive matched on embedded content, show child files
            if is_parent_match and os_client:
                children = _fetch_child_documents(os_client, result.get("file_hash", ""))
                if children:
                    st.markdown(
                        f'<div style="font-size:0.82rem;font-weight:600;color:#374151;'
                        f'margin:0.6rem 0 0.3rem 0;">📂 Files inside this archive ({len(children)}):</div>',
                        unsafe_allow_html=True,
                    )
                    for j, child in enumerate(children):
                        _render_child_file_card(child, key_prefix=f"child_{i}_{j}")
                else:
                    st.caption("⚠️ Child files not yet indexed or still processing.")

            st.markdown('</div>', unsafe_allow_html=True)

        st.markdown("")  # Spacing


def render_recent_documents(os_client: OpenSearchClient, limit: int = 50) -> None:
    """Render recently indexed documents."""
    try:
        # Check if index exists first
        if not os_client.client.indices.exists(index=os_client.index_name):
            st.info("No index found. System is ready to start indexing.")
            st.caption("Start the orchestrator to begin document discovery and indexing.")
            return
        
        query = {
            "query": {
                "bool": {
                    "should": [
                        {"term": {"needs_ocr": False}},
                        {"term": {"ocr_completed": True}},
                    ],
                    "minimum_should_match": 1,
                }
            },
            "sort": [{"indexed_at": {"order": "desc"}}],
            "size": limit,
            "_source": [
                "file_name", "file_path", "file_size", "mime_type", "indexed_at",
                "main_content", "embedded_content", "ocr_content", "tika_content", "reviewed_content", "ocr_confidence",
                "needs_ocr", "ocr_completed",
                "parent_name", "parent_path", "parent_file", "is_embedded"
            ]
        }
        
        response = os_client.client.search(
            index=os_client.index_name,
            body=query
        )
        
        docs = []
        for hit in response['hits']['hits']:
            source = hit['_source']
            docs.append({
                'id': hit['_id'],
                'filename': source.get('file_name', 'Unknown'),
                'filepath': source.get('file_path', ''),
                'file_size': source.get('file_size', 0),
                'mime_type': source.get('mime_type', ''),
                'indexed_at': source.get('indexed_at', ''),
                'main_content': source.get('main_content', ''),
                'embedded_content': source.get('embedded_content', ''),
                'ocr_content': source.get('ocr_content', ''),
                'tika_content': source.get('tika_content', ''),
                'reviewed_content': source.get('reviewed_content', ''),
                'ocr_confidence': source.get('ocr_confidence'),
                'is_embedded': source.get('is_embedded', False),
                'parent_file': source.get('parent_file', ''),
                'parent_path': source.get('parent_path', ''),
                'parent_name': source.get('parent_name', '')
            })
        
        if not docs:
            st.info("No documents indexed yet. Index is ready for documents.")
            return
        
        _last_updated = datetime.now().strftime("%H:%M:%S")
        st.markdown(f"### Recently Indexed Documents ({len(docs)}) &nbsp; <span style='font-size:0.75rem;color:#6B7280;font-weight:400;'>Updated {_last_updated}</span>", unsafe_allow_html=True)
        
        for i, doc in enumerate(docs):
            with st.container():
                st.markdown('<div class="doc-card">', unsafe_allow_html=True)
                
                col1, col2, col3 = st.columns([4, 1.5, 1.5])
                
                with col1:
                    st.markdown(f'<div class="doc-filename">{_esc(doc["filename"])}</div>', unsafe_allow_html=True)
                    # Show parent-child lineage if embedded
                    if doc.get('is_embedded'):
                        st.markdown(
                            f'<div style="color: #4B5563; font-size: 0.82rem; font-weight: 500; margin-top: 0.1rem; margin-bottom: 0.3rem;">'
                            f'📁 Parent: <span style="color: #1E40AF; font-weight: 600;">{_esc(doc.get("parent_name"))}</span>'
                            f' <span style="color: #6B7280; font-size: 0.75rem;">({_esc(doc.get("parent_path"))})</span></div>',
                            unsafe_allow_html=True
                        )
                    file_size_mb = doc.get('file_size', 0) / (1024 * 1024)
                    meta_text = f"Path: {_esc(doc['filepath'])}"
                    if file_size_mb > 0:
                        meta_text += f" | {file_size_mb:.2f} MB"
                    st.markdown(f'<div class="doc-meta">{meta_text}</div>', unsafe_allow_html=True)
                
                with col2:
                    if doc.get('mime_type'):
                        st.caption(f"MIME: {doc['mime_type']}")
                
                # Track if file was opened
                recent_file_opened = False
                recent_file_error = None
                
                with col3:
                    if st.button("Open", key=f"recent_{i}", use_container_width=True):
                        try:
                            open_file_with_default_app(doc["filepath"])
                            recent_file_opened = True
                        except Exception as e:
                            recent_file_error = str(e)
                    
                    download_text = get_downloadable_text(doc)
                    st.download_button(
                        label="📥 Download Text",
                        data=download_text,
                        file_name=f"{Path(doc['filename']).stem}_extracted_text.txt",
                        mime="text/plain",
                        key=f"download_recent_{i}",
                        use_container_width=True,
                    )
                
                # Show success/error messages outside columns
                if recent_file_opened:
                    st.success("File opened successfully.")
                elif recent_file_error:
                    st.error(f"Error opening file: {recent_file_error}")
                
                st.markdown('</div>', unsafe_allow_html=True)
            
            st.markdown("")  # Spacing
    
    except Exception as e:
        # Handle index not found gracefully
        if "index_not_found" in str(e).lower() or "no such index" in str(e).lower():
            st.info("No index found. System is ready to start indexing.")
            st.caption("Start the orchestrator to begin document discovery and indexing.")
        else:
            st.error(f"Unable to load recent documents: {e}")


def render_live_audit_tab(config: Any) -> None:
    """Render live audit feed, deep search, and State Matrix export."""
    st.markdown("### \U0001F4E1 Live Audit")
    st.caption("Real-time event feed from `audit.db` and filter-based State Matrix export.")

    # -- Metadata Input -------------------------------------------------------
    st.markdown("---")
    st.markdown("#### \U0001F4CE Metadata Input")
    st.caption("Upload or select metadata Excel. Active source drives metadata-first tagging.")

    metadata_status = get_metadata_status()
    status_mode = metadata_status.get("mode", "spacy_only_mode")
    if metadata_status.get("active"):
        st.success(
            f"Active metadata mode: {status_mode} | source={metadata_status.get('source', '')} | "
            f"file={metadata_status.get('path', '')}"
        )
    else:
        st.info("No active metadata file. System will run in spacy-only mode.")

    upload_col, path_col = st.columns([1, 1])
    with upload_col:
        uploaded_metadata = st.file_uploader(
            "Upload metadata Excel (.xlsx)",
            type=["xlsx"],
            key="metadata_excel_upload",
            help="Uploaded file is saved under runtime metadata upload directory.",
        )
    with path_col:
        metadata_path_input = st.text_input(
            "Or use existing metadata file path",
            value="",
            key="metadata_excel_path_input",
            placeholder="/absolute/path/to/metadata.xlsx",
        ).strip()

    action_col1, action_col2 = st.columns([1, 1])
    with action_col1:
        if st.button("Apply Metadata Source", key="apply_metadata_source", use_container_width=True):
            try:
                selected_path = ""
                if uploaded_metadata is not None:
                    upload_dir = Path(
                        getattr(config.tagging, "metadata_upload_dir", "")
                        or (Path(config.paths.working_root) / "metadata" / "uploads")
                    )
                    upload_dir.mkdir(parents=True, exist_ok=True)
                    safe_name = Path(uploaded_metadata.name).name
                    target = upload_dir / f"{int(time.time())}_{safe_name}"
                    target.write_bytes(uploaded_metadata.getvalue())
                    selected_path = str(target)
                elif metadata_path_input:
                    selected_path = metadata_path_input

                if not selected_path:
                    st.warning("Provide a metadata Excel by upload or file path.")
                else:
                    ok, message = set_active_metadata_source(selected_path, source="ui")
                    if ok:
                        st.success("Metadata source updated successfully.")
                        st.rerun()
                    else:
                        st.error(message)
            except Exception as exc:
                st.error(f"Could not apply metadata source: {exc}")

    with action_col2:
        if st.button("Clear Metadata Source", key="clear_metadata_source", use_container_width=True):
            clear_active_metadata_source()
            st.success("Metadata source cleared. spacy-only mode is active.")
            st.rerun()

    col1, col2, col3 = st.columns([2, 2, 1])
    with col1:
        auto_refresh = st.checkbox(
            "Auto-refresh",
            value=True,
            key="audit_auto_refresh",
            help="Refresh the live event feed automatically.",
        )
    with col2:
        refresh_seconds = st.slider(
            "Refresh interval (seconds)",
            min_value=3,
            max_value=60,
            value=8,
            disabled=not auto_refresh,
            key="audit_refresh_seconds",
        )
    with col3:
        limit = st.number_input(
            "Rows",
            min_value=10,
            max_value=200,
            value=50,
            step=10,
            key="audit_limit",
        )

    if auto_refresh and HAS_AUTOREFRESH:
        st_autorefresh(
            interval=int(refresh_seconds) * 1000,
            limit=None,
            key="audit_autorefresh",
        )
    elif auto_refresh:
        _schedule_fallback_rerun(int(refresh_seconds))

    filter_query = st.text_input(
        "Deep Search",
        value=st.session_state.get("audit_filter_query", ""),
        placeholder="status:failed AND type:pdf",
        help="Fields: status, stage, type, worker, name, path, smart_id. Operators: AND/OR.",
        key="audit_filter_query",
    ).strip()

    events: List[Dict[str, Any]] = []
    try:
        if filter_query:
            events = search_events(filter_query=filter_query, limit=int(limit))
        else:
            events = get_live_feed(limit=int(limit))
    except ValueError as exc:
        st.error(f"Invalid filter expression: {exc}")
        return  # Issue 10: Early return to avoid showing misleading 'No events' below
    except Exception as exc:
        st.error(f"Could not load audit events: {exc}")
        return

    if events:
        event_rows = []
        for item in events:
            event_rows.append(
                {
                    "Time": item.get("event_time", ""),
                    "Stage": item.get("stage", ""),
                    "Status": item.get("status", ""),
                    "Type": item.get("file_type", ""),
                    "Smart ID": item.get("smart_id", ""),
                    "Worker": item.get("worker_id", ""),
                    "File Name": item.get("file_name", ""),
                    "Path": item.get("file_path", ""),
                    "Error": item.get("error_message", ""),
                }
            )
        st.dataframe(pd.DataFrame(event_rows), use_container_width=True, hide_index=True)
    else:
        st.info("No audit events found for the current filter.")

    # -- State Matrix Export ---------------------------------------------------
    st.markdown("---")
    st.markdown("#### \U0001F4CA State Matrix Export")
    st.caption("Export the current file-state matrix as an Excel spreadsheet. Applies the Deep Search filter above.")

    # Use session state to persist the export file path across reruns
    if "audit_export_path" not in st.session_state:
        st.session_state["audit_export_path"] = None

    col_gen, col_dl = st.columns([1, 1])
    
    with col_gen:
        if st.button("\U0001F504 Generate Excel Report", key="audit_generate_btn", use_container_width=True):
            try:
                with st.spinner("Generating report..."):
                    output_dir = str(Path(config.paths.working_root) / "audit")
                    exported_path = export_state_matrix_xlsx(
                        filters={"filter_query": filter_query},
                        out_path=output_dir,
                    )
                    st.session_state["audit_export_path"] = exported_path
                    st.success("Report generated!")
            except Exception as exc:
                st.warning(f"Could not generate export: {exc}")

    # Show download button if a report exists in session state
    if st.session_state["audit_export_path"] and Path(st.session_state["audit_export_path"]).exists():
        export_path = Path(st.session_state["audit_export_path"])
        with col_dl:
            try:
                with open(export_path, "rb") as f:
                    xlsx_bytes = f.read()
                
                st.download_button(
                    label="\u2B07\uFE0F Download .xlsx",
                    data=xlsx_bytes,
                    file_name=export_path.name,
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    use_container_width=True,
                    key="audit_download_final"
                )
            except Exception as e:
                st.warning(f"File unavailable: {e}")


def render_monitoring_tab(config: Any, queue_manager: Any) -> None:
    """Render the system monitoring tab with smooth live updates using st_autorefresh."""
    
    # =========================================================================
    # SMOOTH AUTO-REFRESH using streamlit-autorefresh
    # =========================================================================
    # This triggers a soft rerun that only updates the data, not a full page reload.
    # The cache TTL controls how often Redis is actually queried.
    # The autorefresh interval controls how often the display checks for updates.
    
    # Monitoring controls
    col1, col2, col3 = st.columns([2, 2, 1])
    with col1:
        auto_refresh = st.checkbox(
            "\U0001F504 Auto-refresh", 
            value=True, 
            help="Smooth live updates - metrics update without page flashing"
        )
    with col2:
        refresh_interval = st.slider(
            "Refresh interval (seconds)", 
            min_value=3, 
            max_value=60, 
            value=10, 
            disabled=not auto_refresh
        )
    with col3:
        if st.button("\U0001F504 Refresh Now", use_container_width=True):
            # Hard-clear caches to avoid stale last-known-good values after resets.
            clear_all_caches()
            
            # Force fresh fetch from Redis with longer timeout (5s each))
            # This will either get fresh data or timeout and show waiting message
            _force_refresh_from_redis()  # Issue 19: result was unused
            
            st.rerun()

    # Enable smooth auto-refresh if available and enabled
    if auto_refresh and HAS_AUTOREFRESH:
        # st_autorefresh does a soft rerun without full page reload
        # It only triggers Streamlit's internal update mechanism
        st_autorefresh(  # Issue 19: return value was unused
            interval=refresh_interval * 1000,  # milliseconds
            limit=None,  # No limit on refreshes
            key="metrics_autorefresh"
        )
        # NOTE: We do NOT call invalidate_all_caches() here.
        # The background thread refreshes queue_stats every 5s and
        # size_stats every 15s automatically.  Periodic invalidation was
        # causing zeros-on-refresh by wiping the store faster than the
        # bg thread could re-populate it.
    elif auto_refresh:
        # Fallback: Show warning that smooth refresh isn't available
        st.caption("Install `streamlit-autorefresh` for smoother updates: `pip install streamlit-autorefresh`")
        _schedule_fallback_rerun(int(refresh_interval))
    
    # Show last update time
    st.caption(f"Last updated: {datetime.now().strftime('%H:%M:%S')}")

    # If reset command ran while dashboard stayed open, purge stale in-memory caches.
    if _consume_dashboard_reset_marker(config):
        st.info("Reset detected. Dashboard caches were cleared.")
    
    try:
        source_path = getattr(config.paths, 'source_drive', 'N/A')
        working_root = getattr(config.paths, 'working_root', 'N/A')
        st.markdown(f"**Source:** `{source_path}` | **Working Root:** `{working_root}` | **Backend:** `Redis`")
    except Exception:
        st.markdown("**Source:** `N/A` | **Working Root:** `N/A` | **Backend:** `Redis`")

    # Runtime cache/fetcher diagnostics
    runtime_health = get_dashboard_stats_runtime().health()
    thread_status = "alive" if runtime_health.get("thread_alive") else "stopped"
    queue_age = runtime_health.get("queue_age_sec")
    size_age = runtime_health.get("size_age_sec")
    queue_age_str = f"{queue_age:.1f}s" if isinstance(queue_age, (int, float)) else "n/a"
    size_age_str = f"{size_age:.1f}s" if isinstance(size_age, (int, float)) else "n/a"
    st.caption(
        f"Runtime: thread={thread_status}, queue_age={queue_age_str}, "
        f"size_age={size_age_str}, errors={runtime_health.get('consecutive_errors', 0)}"
    )
    if runtime_health.get("last_error"):
        st.caption(f"Last cache error: {runtime_health.get('last_error')}")
    st.markdown("---")

    # Enforce a minimum refresh interval
    refresh_interval = max(refresh_interval, 3)

    # Initialize with empty defaults
    queue_stats = {}
    size_stats = {}
    
    # Load data using CACHED functions to prevent lag
    try:
        queue_stats = get_cached_queue_stats()
        size_stats = get_cached_size_stats()
    except Exception as exc:
        error_msg = str(exc)
        if "no such table" in error_msg.lower() or "no such column" in error_msg.lower():
            # Database was reset - reset the singleton and try to re-create a fresh instance
            try:
                reset_queue_manager()
                invalidate_all_caches()
                queue_stats = get_cached_queue_stats()
                size_stats = get_cached_size_stats()
            except Exception:
                st.info("**System Reset Complete**")
                st.markdown("""
                The system has been reset and all data cleared. Ready for fresh indexing.
                
                **To start processing:**
                1. Open a terminal
                2. Run: `python src/main.py start`
                3. The system will begin discovering and indexing files
                4. Metrics will appear here once processing starts
                """)
                # Show empty metrics
                st.markdown("---")
                col1, col2, col3, col4 = st.columns(4)
                with col1:
                    st.metric("Files Discovered", "0")
                with col2:
                    st.metric("Fully Processed", "0")
                with col3:
                    st.metric("In Pipeline", "0")
                with col4:
                    st.metric("Failed", "0")
                st.progress(0.0, text="Ready to start - 0% complete")
                
                # Auto-refresh logic - non-blocking
                if auto_refresh:
                    import streamlit.components.v1 as components
                    components.html(
                        f"""<script>
                        setTimeout(function() {{
                            window.parent.postMessage({{type: 'streamlit:rerun'}}, '*');
                        }}, {int(refresh_interval * 1000)});
                        </script>""",
                        height=0
                    )
                return
            else:
                st.warning(f"Unable to load queue metrics: {str(exc)[:200]}")
                # DO NOT set fake zeros - keep whatever we got from the getters
                # queue_stats and size_stats already have their best-effort values

    # Guard: if no data at all yet, attempt one bounded forced refresh.
    if not queue_stats:
        refreshed = _force_refresh_from_redis()
        queue_stats = refreshed.get("queue_stats") or queue_stats
        size_stats = refreshed.get("size_stats") or size_stats

    if not queue_stats:
        # Issue 5: Bounded retry to prevent infinite sleep+rerun loop
        retry_key = "_monitor_empty_retries"
        retries = st.session_state.get(retry_key, 0)
        if retries >= 3:
            st.warning("**Backend metrics unavailable.** Redis may be unreachable. Refresh manually when ready.")
            st.session_state[retry_key] = 0  # Reset for next manual refresh
            return
        st.info("**Waiting for data...** The background thread is fetching metrics from Redis. This should take a few seconds.")
        st.session_state[retry_key] = retries + 1
        if auto_refresh and HAS_AUTOREFRESH:
            pass  # autorefresh will trigger a rerun
        else:
            time.sleep(2)
            st.rerun()
        return
    # Reset retry counter on successful data fetch
    st.session_state.pop("_monitor_empty_retries", None)

    summary = extract_summary(queue_stats)
    show_as_files = st.session_state.get("show_as_files", False) # Default to False (Data Size view)

    # Calculate additional useful metrics
    total_discovered = summary["discovered_total"]
    total_completed = summary["completed_total"]
    total_failures = summary["total_failures"]
    total_duplicates = summary["duplicates"]
    
    # Overall progress — do NOT subtract duplicates: they are content-hash
    # duplicates detected at completion time, already counted in both
    # discovered and root_completed.  Subtracting from the denominator only
    # causes progress to exceed 100%.
    total_to_process = max(1, total_discovered)
    overall_progress = (total_completed / total_to_process * 100) if total_to_process > 0 else 0
    
    # In-flight counts (raw queue numbers -- may include embedded items)
    in_extraction = summary["extraction_pending"] + summary["extraction_processing"]
    in_indexing = summary["indexing_pending"] + summary["indexing_processing"]
    in_ocr = summary["ocr_pending"] + summary["ocr_processing"]
    in_tagging = summary["tagging_pending"] + summary["tagging_processing"]
    # Show actual in-flight count including child files from archives (Fix #4)
    total_in_flight = in_extraction + in_indexing + in_ocr + in_tagging
    
    # Success rate
    total_processed = total_completed + total_failures
    success_rate = (total_completed / total_processed * 100) if total_processed > 0 else 100

    # Row 1: Key Progress Metrics
    st.markdown("### \U0001F4CA Overall Progress")
    
    col1, col2, col3, col4 = st.columns(4)
    
    if show_as_files:
        with col1:
            st.metric(
                label="\U0001F4C1 Files Discovered",
                value=format_number(total_discovered),
                help="Total files found on source drive"
            )
        
        with col2:
            st.metric(
                label="\u2705 Fully Processed",
                value=format_number(total_completed),
                delta=f"{overall_progress:.1f}% of total" if total_to_process > 0 else None,
                help="Files that completed all pipeline stages successfully"
            )
        
        with col3:
            st.metric(
                label="\U0001F504 In Pipeline",
                value=format_number(total_in_flight),
                help="Files currently being extracted, indexed, or OCR processed"
            )
        
        with col4:
            st.metric(
                label="\u274C Failed",
                value=format_number(total_failures),
                delta=f"{success_rate:.1f}% success rate",
                delta_color="normal",
                help="Files that encountered errors during processing"
            )
        
        # Overall progress bar
        if total_to_process > 0:
            st.progress(min(overall_progress / 100, 1.0), text=f"Overall Progress: {overall_progress:.1f}% ({total_completed:,} / {total_to_process:,} files)")
    else:
        # Data size view - use safe access
        disc_size = (size_stats.get('discovered') or {}).get('size_bytes', 0) or 0
        comp_size = (size_stats.get('searchable') or {}).get('size_bytes', 0) or 0
        pipe_size = (size_stats.get('in_pipeline') or {}).get('size_bytes', 0) or 0
        _ = (size_stats.get('failed') or {}).get('size_bytes', 0) or 0  # Issue 19: was unused fail_size
        
        size_progress = (comp_size / max(disc_size, 1)) * 100
        
        with col1:
            st.metric(
                label="\U0001F4C1 Data Discovered",
                value=format_size(disc_size),
                help="Total data volume found on source drive"
            )
        
        with col2:
            st.metric(
                label="\u2705 Data Indexed",
                value=format_size(comp_size),
                delta=f"{size_progress:.1f}% of total",
                help="Data volume fully indexed and searchable"
            )
            
        with col3:
            st.metric(
                label="\U0001F504 In Pipeline",
                value=format_size(pipe_size),
                help="Data volume currently in processing"
            )
            
        with col4:
            st.metric(
                label="\u274C Failed",
                value=format_number(total_failures),
                delta=f"{success_rate:.1f}% success rate",
                delta_color="normal",
                help="Number of files that encountered errors during processing"
            )

        # Overall progress bar
        if disc_size > 0:
            st.progress(min(size_progress / 100, 1.0), text=f"Overall Progress: {size_progress:.1f}% ({format_size(comp_size)} / {format_size(disc_size)} indexed)")

    st.markdown("---")
    
    # Row 2: Pipeline Breakdown
    st.markdown("### \u2699\uFE0F Pipeline Status")
    
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        st.markdown("**\U0001F4C2 Extraction**")
        st.metric("Pending", format_number(summary.get("extraction_pending", 0)))
        st.metric("Processing", format_number(summary.get("extraction_processing", 0)))
        st.metric("Done", format_number(summary.get("extraction_completed", 0)))
    
    with col2:
        st.markdown("**\U0001F5C2\uFE0F Indexing**")
        st.metric("Pending", format_number(summary.get("indexing_pending", 0)))
        st.metric("Processing", format_number(summary.get("indexing_processing", 0)))
        st.metric("Done", format_number(summary.get("indexing_completed", 0)))
    
    with col3:
        st.markdown("**\U0001F50D OCR Queue**")
        st.metric("Pending", format_number(summary.get("ocr_pending", 0)))
        st.metric("Processing", format_number(summary.get("ocr_processing", 0)))
        st.metric("Done", format_number(summary.get("ocr_completed", 0)))
    
    with col4:
        st.markdown("**\U0001F4C8 Performance**")
        avg_extract = summary.get("avg_extraction_ms", 0) or 0
        avg_index = summary.get("avg_indexing_ms", 0) or 0
        st.metric("Avg Extract", f"{avg_extract:.0f} ms")
        st.metric("Avg Index", f"{avg_index:.0f} ms")
        st.metric("Duplicates", format_number(total_duplicates))

    st.markdown("---")
    # ETA estimates per stage
    try:
        # Remaining counts per stage (use safe access)
        rem_extract = (summary.get('extraction_pending', 0) or 0) + (summary.get('extraction_processing', 0) or 0)
        rem_index = (summary.get('indexing_pending', 0) or 0) + (summary.get('indexing_processing', 0) or 0)
        rem_ocr = (summary.get('ocr_pending', 0) or 0) + (summary.get('ocr_processing', 0) or 0)

        # Average times (ms -> sec), fallbacks if unknown
        avg_extract_ms = summary.get('avg_extraction_ms') or 0
        avg_index_ms = summary.get('avg_indexing_ms') or 0
        avg_extract_sec = (avg_extract_ms / 1000.0) if avg_extract_ms > 0 else 10.0
        avg_index_sec = (avg_index_ms / 1000.0) if avg_index_ms > 0 else 1.0
        avg_ocr_sec = 5.0

        # Worker counts from config (with safe defaults)
        ext_workers = getattr(config.extraction, 'total_workers', 8) if hasattr(config, 'extraction') else 8
        idx_workers = getattr(config.indexing, 'num_workers', 4) if hasattr(config, 'indexing') else 4
        ocr_workers = getattr(config.ocr, 'initial_workers', 4) if hasattr(config, 'ocr') else 4

        eta_extract = (rem_extract * avg_extract_sec) / max(ext_workers, 1)
        eta_index = (rem_index * avg_index_sec) / max(idx_workers, 1)
        eta_ocr = (rem_ocr * avg_ocr_sec) / max(ocr_workers, 1)

        # Wall-clock ETA (parallel) and serial ETA
        wall_eta = max(eta_extract, eta_index, eta_ocr)
        # serial_eta removed — was unused (Issue 19)

        col_e1, col_e2, col_e3, col_e4 = st.columns(4)
        with col_e1:
            st.metric("Extraction Remaining", format_number(rem_extract), delta=seconds_to_human(eta_extract))
        with col_e2:
            st.metric("Indexing Remaining", format_number(rem_index), delta=seconds_to_human(eta_index))
        with col_e3:
            st.metric("OCR Remaining", format_number(rem_ocr), delta=seconds_to_human(eta_ocr))
        with col_e4:
            st.metric("Est. Time (wall)", seconds_to_human(wall_eta), help="Estimated wall-clock time to clear current pipeline")
    except Exception:
        pass
    # Extra: Indexed file metrics (average size + top largest files)
    try:
        comp_files = size_stats['searchable']['files']
        comp_bytes = size_stats['searchable']['size_bytes']
        avg_size = int(comp_bytes / comp_files) if comp_files > 0 else 0

        with st.expander("\U0001F4E6 Indexed File Metrics", expanded=False):
            col_a, col_b = st.columns([2, 3])
            with col_a:
                st.metric("Avg Indexed File", format_size(avg_size), help="Average size of indexed (searchable) files")
                st.caption(f"{comp_files:,} files indexed")

            with col_b:
                load_top = st.checkbox("Load largest indexed files", value=False, help="Load top files on demand")
                if load_top:
                    try:
                        top_files = get_cached_largest_files()
                        if top_files:
                            rows = []
                            for f in top_files:
                                rows.append({
                                    "File": Path(f['file_path']).name,
                                    "Size": format_size(f.get('file_size', 0)),
                                    "Path": f.get('file_path', '')
                                })
                            df = pd.DataFrame(rows)
                            st.dataframe(df, use_container_width=True)
                        else:
                            st.info("No indexed files available yet")
                    except Exception as e:
                        st.error(f"Unable to load top indexed files: {e}")
    except Exception:
        # Defensive: if size_stats missing keys
        pass

    # Detailed sections
    tab1, tab2, tab3 = st.tabs(["\U0001F4CA Extraction Details", "\u26A0\uFE0F Failure Analysis", "\U0001F4CB Queue Status"])
    
    with tab1:
        st.subheader("Extraction by Size Category")
        render_extraction_detail(queue_stats)
    
    with tab2:
        st.subheader("Failure Breakdown")
        render_failure_chart(queue_stats)
        
        # Show detailed failure table
        failures = queue_stats.get("failures", {})
        if failures:
            total_failures = queue_stats.get("total_failures", 0)
            failure_df = pd.DataFrame([
                {
                    "Error Type": error_type,
                    "Count": count,
                    "Percentage": f"{(count / total_failures * 100):.1f}%" if total_failures else "0%"
                }
                for error_type, count in sorted(failures.items(), key=lambda x: x[1], reverse=True)
            ])
            st.dataframe(failure_df, use_container_width=True, hide_index=True)
            
            # Show actual failed files
            st.markdown("---")
            st.subheader("\U0001F4CB Failed Files Details")
            try:
                failed_files = get_cached_failed_files()
                if failed_files:
                    failed_df = pd.DataFrame([
                        {
                            "File": Path(f['file_path']).name if f.get('file_path') else 'Unknown',
                            "Stage": f.get('stage', ''),
                            "Error": f.get('error_type', ''),
                            "Message": (f.get('error_message', '') or '')[:100] + '...' if len(f.get('error_message', '') or '') > 100 else f.get('error_message', ''),
                            "Path": f.get('file_path', '')
                        }
                        for f in failed_files
                    ])
                    st.dataframe(failed_df, use_container_width=True, hide_index=True)
                else:
                    st.info("No failed files to display")
            except Exception as e:
                st.warning(f"Could not load failed files: {e}")
        else:
            st.success("No failures recorded. System running smoothly.")
    
    with tab3:
        st.subheader("Queue Status Overview")
        
        status_data = []
        # Main queues to monitor
        for q_key in ["discovery", "extraction", "indexing", "ocr", "tagging"]:
            q_data = queue_stats.get(q_key, {})
            if q_key == "extraction":
                # Special handling for extraction size categories
                for size_cat, metrics in q_data.items():
                    if isinstance(metrics, dict):
                        for status, count in metrics.items():
                            if status in ["pending", "processing", "completed", "total"]:
                                status_data.append({
                                    "Queue": f"Extraction ({size_cat.title()})",
                                    "Status": status.title(),
                                    "Count": count or 0
                                })
            elif isinstance(q_data, dict):
                for status, count in q_data.items():
                    if status in ["pending", "processing", "completed", "failed", "total"]:
                        status_data.append({
                            "Queue": q_key.title(),
                            "Status": status.title(),
                            "Count": count or 0
                        })
        
        if status_data:
            status_df = pd.DataFrame(status_data)
            st.dataframe(status_df, use_container_width=True, hide_index=True)
        else:
            st.info("No queue status data available")
        
        # Show OCR pending files (flagged for OCR)
        st.markdown("---")
        st.subheader("\U0001F50D Files Flagged for OCR")
        try:
            ocr_pending = get_cached_ocr_pending()
            if ocr_pending:
                ocr_df = pd.DataFrame([
                    {
                        "File": Path(f['file_path']).name if f.get('file_path') else 'Unknown',
                        "Status": f.get('status', '').title(),
                        "Priority": f.get('priority', 5),
                        "Path": f.get('file_path', '')
                    }
                    for f in ocr_pending
                ])
                st.dataframe(ocr_df, use_container_width=True, hide_index=True)
                st.caption(f"Showing {len(ocr_pending)} of pending OCR files")
            else:
                st.success("No files pending OCR processing")
        except Exception as e:
            st.warning(f"Could not load OCR queue: {e}")

    # Note: Auto-refresh is handled by st_autorefresh at the top of this function
    # The st_autorefresh component triggers a soft rerun that updates the cached data


def main() -> None:
    render_dashboard()


if __name__ == "__main__":
    main()

