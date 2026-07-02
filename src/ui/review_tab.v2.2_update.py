"""
Production-Grade Snippet Review Portal — Human-in-the-Loop Visual Verification.

This module renders the "Snippet Review" tab in the Streamlit dashboard.
It provides:
  1. Document selector with per-document dynamic KPI metrics
  2. Role-based snippet filtering (signatures, stamps, logos)
  3. Clean vertical card layout with snippet images, impact %, and reviewer roles
  4. Mandatory acceptance reason system with audit trail
  5. Activity log panel showing chronological review history
  6. Storage management for snippet crop files
"""
import json
import os
import re
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
from PIL import Image, ImageOps
import streamlit as st
import streamlit.components.v1 as components

from core.config_manager import get_config
from core.reporting_manager import (
    get_all_reviews_for_doc,
    get_docs_with_reviews,
    get_pending_reviews,
    get_review_activity_log,
    get_snippet_storage_stats,
    purge_old_snippets,
    update_snippet_review_status,
)
from indexing.opensearch_client import OpenSearchClient
from ocr.visual_memory import VisualMemoryEngine
from ocr.tesseract_wrapper import TesseractWrapper

# ── Standard acceptance reasons for each snippet type ────────────────────────
ACCEPTANCE_REASONS = {
    "signature": [
        "Authorized signatory — verified signer identity",
        "Known employee signature — HR verified",
        "Client signature — contractually approved",
        "Witness signature — standard legal requirement",
        "Digital signature — system-generated",
    ],
    "stamp": [
        "Official company stamp/seal — known template",
        "Notary public seal — standard legal element",
        "Government department stamp — regulatory requirement",
        "Quality assurance stamp — internal process",
        "Received/filed date stamp — administrative",
    ],
    "logo": [
        "Registered company logo — brand asset",
        "Partner/client logo — business relationship",
        "Regulatory body logo — compliance requirement",
        "Certification mark — industry standard",
        "Decorative header/footer — no content impact",
    ],
    "text_anomaly": [
        "OCR extracted correctly — no visual review required",
        "Machine-readable text region — routed to OCR flow",
        "Reference number/identifier — expected text token",
        "Printed text artifact — not a signature",
        "No manual visual validation needed",
    ],
}
GENERIC_REASONS = [
    "Verified element — no accuracy concern",
    "Standard document formatting — expected element",
    "Custom reason...",
]

# ── Snippet type display configuration ───────────────────────────────────────
SNIPPET_TYPE_CONFIG = {
    "signature": {
        "icon": "✒️",
        "label": "Signature",
        "color": "#3B82F6",
        "bg": "#EFF6FF",
        "border": "#93C5FD",
    },
    "stamp": {
        "icon": "🔏",
        "label": "Stamp & Seal",
        "color": "#EA580C",
        "bg": "#FFF7ED",
        "border": "#FDBA74",
    },
    "logo": {
        "icon": "🖼️",
        "label": "Logo & Image",
        "color": "#7C3AED",
        "bg": "#F5F3FF",
        "border": "#C4B5FD",
    },
    "text_anomaly": {
        "icon": "🔤",
        "label": "OCR Text Region",
        "color": "#0F766E",
        "bg": "#ECFEFF",
        "border": "#67E8F9",
    },
}

STATUS_BADGES = {
    "pending": {"color": "#F59E0B", "bg": "#FFFBEB", "label": "⏳ Pending Review"},
    "accepted": {"color": "#059669", "bg": "#ECFDF5", "label": "✅ Accepted"},
    "rejected": {"color": "#DC2626", "bg": "#FEF2F2", "label": "❌ Rejected"},
}


def _format_file_size(size_bytes: int) -> str:
    """Format bytes into human-readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / (1024 * 1024):.2f} MB"


def _render_metric_card(
    title: str,
    value: str,
    subtitle: str,
    border_color: str,
    bg_color: str,
    text_color: str,
    icon: str = "",
) -> None:
    """Render a styled metric card using HTML."""
    st.markdown(
        f"""
        <div style="
            background: {bg_color};
            padding: 1rem 1.2rem;
            border-radius: 10px;
            border-left: 4px solid {border_color};
            box-shadow: 0 1px 4px rgba(0,0,0,0.06);
            margin-bottom: 0.5rem;
        ">
            <p style="margin:0; font-size:0.78rem; font-weight:600; color:{text_color};
                       text-transform:uppercase; letter-spacing:0.5px;">
                {icon} {title}
            </p>
            <h3 style="margin:0.25rem 0 0 0; color:{border_color}; font-size:1.6rem;
                        font-weight:700; line-height:1.2;">
                {value}
            </h3>
            <p style="margin:0.15rem 0 0 0; font-size:0.72rem; color:{text_color}; opacity:0.85;">
                {subtitle}
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _resolve_snippet_path(snippet_path: Path, working_root: Path) -> Path:
    """Resolve legacy snippet paths into current workspace data path."""
    if snippet_path.exists():
        return snippet_path

    normalized = str(snippet_path).replace("\\", "/")
    marker = "/data/review_snippets/"
    if marker in normalized:
        relative_part = normalized.split(marker, 1)[1]
        mapped = working_root.parent / "data" / "review_snippets" / Path(relative_part)
        if mapped.exists():
            return mapped

    return snippet_path


def _build_uniform_thumbnail(img: Image.Image, width: int = 520, height: int = 280) -> Image.Image:
    """Build a fixed-size thumbnail canvas for consistent tile height."""
    if img.mode != "RGB":
        img = img.convert("RGB")

    # Keep aspect ratio while fitting into a fixed canvas for stable grid layout.
    resample = Image.Resampling.LANCZOS if hasattr(Image, "Resampling") else Image.LANCZOS
    fitted = ImageOps.contain(img, (width - 16, height - 16), method=resample)
    canvas = Image.new("RGB", (width, height), color=(248, 250, 252))
    x = (width - fitted.width) // 2
    y = (height - fitted.height) // 2
    canvas.paste(fitted, (x, y))
    return canvas


def _build_document_page_link(file_path: str, page_num: int) -> Optional[str]:
    """Build file:// URL with page hint for local PDF files."""
    if not file_path:
        return None
    try:
        p = Path(file_path)
        if not p.exists():
            return None
        uri = p.resolve().as_uri()
        if p.suffix.lower() == ".pdf":
            return f"{uri}#page={max(1, int(page_num or 1))}"
        return uri
    except Exception:
        return None


def _render_accuracy_waterfall_chart(snippets: List[Dict[str, Any]], selected_doc: Dict[str, Any]) -> None:
    """Render accuracy waterfall chart.

    Layout order:
      1. Current Accuracy (green, solid) — first bar
      2. Category error bars (signatures, stamps, etc.) — stacked starting from
         current accuracy upward toward 100%
      3. Total Accuracy (blue, dotted outline, light fill) — last bar
    """
    pending = [s for s in snippets if s.get("status") == "pending"]
    category_meta = {
        "stamp": ("Stamp", "#F59E0B"),
        "signature": ("Signature", "#EF4444"),
        "logo": ("Logo/Image", "#8B5CF6"),
        "text_anomaly": ("Text", "#14B8A6"),
    }

    by_type: Dict[str, Dict[str, float]] = {}
    for s in pending:
        t = str(s.get("snippet_type") or "other")
        impact = max(0.0, float(s.get("accuracy_impact") or 0.0))
        by_type.setdefault(t, {"impact": 0.0, "count": 0.0})
        by_type[t]["impact"] += impact
        by_type[t]["count"] += 1

    raw_fault_total = sum(v["impact"] for v in by_type.values())
    fault_total = min(100.0, raw_fault_total)
    current_accuracy = max(0.0, 100.0 - fault_total)

    # Build bars: Current first, then categories stacking upward, Total last
    bars = []

    # 1. Current Accuracy — solid green bar
    bars.append({
        "label": "Current",
        "value": current_accuracy,
        "bottom": 0.0,
        "color": "#16A34A",
        "fill_color": "#16A34A",
        "text": f"{current_accuracy:.2f}%",
        "style": "solid",
    })

    # 2. Category error bars — each starts where the previous ended above current accuracy
    sorted_types = sorted(by_type.items(), key=lambda kv: kv[1]["impact"], reverse=True)
    running_bottom = current_accuracy
    for t, data in sorted_types:
        raw = data["impact"]
        scaled = (raw / raw_fault_total * fault_total) if raw_fault_total > 0 else 0.0
        label, color = category_meta.get(t, (t.title(), "#64748B"))
        bars.append({
            "label": f"{label} ({int(data['count'])})",
            "value": scaled,
            "bottom": running_bottom,
            "color": color,
            "fill_color": color,
            "text": f"{raw:.2f}%",
            "style": "solid",
        })
        running_bottom = min(100.0, running_bottom + scaled)

    # 3. Total Accuracy — blue, dotted outline, light fill (last)
    bars.append({
        "label": "Total",
        "value": 100.0,
        "bottom": 0.0,
        "color": "#2563EB",
        "fill_color": "#DBEAFE",
        "text": "100%",
        "style": "dotted",
    })

    chart_width = max(800, 120 * len(bars))
    bar_slot = max(90, int(chart_width / max(1, len(bars))))
    bar_width = int(bar_slot * 0.35)
    left_pad = 30
    chart_h = 240
    plot_top = 20
    plot_h = chart_h - 80

    bar_rects: List[str] = []
    pct_labels: List[str] = []
    x_labels: List[str] = []
    legend_items: List[str] = []
    for i, bar in enumerate(bars):
        x = left_pad + i * bar_slot + (bar_slot - bar_width) // 2
        h = max(0.0, min(100.0, bar["value"]))
        b = max(0.0, min(100.0, bar["bottom"]))
        px_h = (h / 100.0) * plot_h
        px_b = (b / 100.0) * plot_h
        y = plot_top + (plot_h - px_b - px_h)

        if bar["style"] == "dotted":
            bar_rects.append(
                f"<rect x='{x}' y='{y:.1f}' width='{bar_width}' height='{px_h:.1f}' rx='6' "
                f"fill='{bar['fill_color']}' stroke='{bar['color']}' stroke-width='2' "
                f"stroke-dasharray='5,3'></rect>"
            )
        else:
            bar_rects.append(
                f"<rect x='{x}' y='{y:.1f}' width='{bar_width}' height='{px_h:.1f}' rx='6' "
                f"fill='{bar['fill_color']}'></rect>"
            )

        pct_labels.append(
            f"<text x='{x + bar_width/2:.1f}' y='{max(12, y - 8):.1f}' text-anchor='middle' font-size='12' font-weight='700' fill='#0F172A'>{bar['text']}</text>"
        )
        x_labels.append(
            f"<text x='{x + bar_width/2:.1f}' y='{chart_h - 28}' text-anchor='middle' font-size='12' font-weight='700' fill='#111827'>{bar['label']}</text>"
        )
        legend_items.append(
            f"<span style='display:inline-flex; align-items:center; gap:6px; margin-right:14px; font-size:12px; color:#334155;'>"
            f"<span style='display:inline-block; width:10px; height:10px; border-radius:2px; background:{bar['color']};{' border:1px dashed #2563EB;' if bar['style'] == 'dotted' else ''}'></span>"
            f"{bar['label']}</span>"
        )

    svg = f"""
    <div style='border:1px solid #E5E7EB; border-radius:10px; background:#FFFFFF; padding:8px 10px 10px 10px;'>
      <svg width='{left_pad + bar_slot * len(bars) + 20}' height='{chart_h}' viewBox='0 0 {left_pad + bar_slot * len(bars) + 20} {chart_h}'>
        <line x1='{left_pad-6}' y1='{plot_top + plot_h}' x2='{left_pad + bar_slot * len(bars)}' y2='{plot_top + plot_h}' stroke='#CBD5E1' stroke-width='1.2'></line>
        {''.join(bar_rects)}
        {''.join(pct_labels)}
        {''.join(x_labels)}
      </svg>
      <div style='margin-top:6px; padding-top:6px; border-top:1px dashed #E2E8F0; white-space:nowrap; overflow-x:auto;'>
        {''.join(legend_items)}
      </div>
    </div>
    """
    components.html(svg, height=chart_h + 70, width=chart_width, scrolling=False)


def _is_obvious_noise_review_snippet(snippet: Dict[str, Any], working_root: Path) -> bool:
    """Identify sparse dot/blob snippets that should not appear in visual review queue."""
    if snippet.get("status") != "pending":
        return False

    # Hard suppression only for extremely tiny impact — real cursive signatures
    # can have low area ratios on large pages so the threshold is kept very low.
    if snippet.get("snippet_type") == "signature":
        try:
            if float(snippet.get("accuracy_impact") or 0.0) <= 0.03:
                return True
        except Exception:
            pass

    snippet_path = _resolve_snippet_path(Path(snippet.get("snippet_path", "")), working_root)
    if not snippet_path.exists():
        return False

    try:
        arr = np.array(Image.open(str(snippet_path)).convert("L"))
        if arr.ndim != 2:
            return False

        h, w = arr.shape
        area = max(1, h * w)
        ink_mask = arr < 200
        ink_px = int(np.count_nonzero(ink_mask))
        if ink_px == 0:
            return True

        ink_ratio = ink_px / area
        row_coverage = float(np.count_nonzero(np.any(ink_mask, axis=1)) / max(1, h))
        col_coverage = float(np.count_nonzero(np.any(ink_mask, axis=0)) / max(1, w))

        return (
            ink_ratio < 0.004
            or (ink_ratio < 0.010 and row_coverage < 0.35 and col_coverage < 0.35)
            or (area > 1500 and ink_px < 60)
        )
    except Exception:
        return False


def _is_printed_font_review_snippet(snippet: Dict[str, Any], working_root: Path) -> bool:
    """Return True when the saved crop looks like printed/digital text, not handwriting.

    Uses numpy-only stroke-width uniformity check (no cv2 dependency in UI layer).
    Printed fonts: very uniform stroke widths → low coefficient of variation on the
    distance-field proxy (run-length distribution per row).
    Handwriting: highly variable stroke widths → high CV.
    """
    if snippet.get("snippet_type") != "signature":
        return False
    if snippet.get("status") != "pending":
        return False

    snippet_path = _resolve_snippet_path(Path(snippet.get("snippet_path", "")), working_root)
    if not snippet_path.exists():
        return False

    try:
        arr = np.array(Image.open(str(snippet_path)).convert("L"))
        if arr.ndim != 2:
            return False

        h, w = arr.shape
        if h < 8 or w < 8:
            return False

        # Binarize: ink = True
        ink = arr < 128

        ink_px = int(np.count_nonzero(ink))
        if ink_px < 20:
            return False

        # ── Stroke-width proxy: horizontal run lengths of ink pixels ──────
        # For each row collect the lengths of consecutive ink runs.
        # Printed fonts produce very consistent run lengths across all rows;
        # cursive handwriting has wildly variable run lengths.
        run_lengths = []
        for row in ink:
            in_run = False
            run_len = 0
            for px in row:
                if px:
                    in_run = True
                    run_len += 1
                else:
                    if in_run and run_len > 0:
                        run_lengths.append(run_len)
                    in_run = False
                    run_len = 0
            if in_run and run_len > 0:
                run_lengths.append(run_len)

        if len(run_lengths) < 10:
            return False

        rl = np.array(run_lengths, dtype=np.float32)
        cv_val = float(np.std(rl) / max(np.mean(rl), 0.001))

        # Printed text: uniform runs → CV < 0.55 (raised to tolerate scan noise)
        # Handwriting: irregular runs → CV > 0.65
        is_uniform_stroke = cv_val < 0.55

        # ── Edge regularity: row-wise ink fraction variance ───────────────
        # Printed glyphs have very consistent ink density per row within a letter;
        # cursive letters taper and swell → higher variance.
        row_densities = np.sum(ink, axis=1) / max(w, 1)
        nonzero_rows = row_densities[row_densities > 0]
        if len(nonzero_rows) < 3:
            return False
        density_cv = float(np.std(nonzero_rows) / max(np.mean(nonzero_rows), 0.001))

        # Suppress when both uniformity signals agree (2-of-2).
        is_uniform_density = density_cv < 0.65

        return is_uniform_stroke and is_uniform_density

    except Exception:
        return False


@st.cache_resource(show_spinner=False)
def _get_review_tesseract() -> Optional[TesseractWrapper]:
    """Initialize one reusable Tesseract wrapper for queue-side snippet suppression."""
    try:
        return TesseractWrapper()
    except Exception:
        return None


def _is_text_like_review_snippet(snippet: Dict[str, Any], working_root: Path) -> bool:
    """Hide queued snippets that are actually machine-readable text tokens."""
    if snippet.get("status") != "pending":
        return False

    snippet_path = _resolve_snippet_path(Path(snippet.get("snippet_path", "")), working_root)
    if not snippet_path.exists():
        return False

    tesseract = _get_review_tesseract()
    if not tesseract:
        return False

    try:
        ocr_result = tesseract.extract_text(str(snippet_path))
        if not ocr_result:
            return False

        text, confidence = ocr_result
        cleaned = re.sub(r"\s+", "", text or "")
        alnum = re.sub(r"[^A-Za-z0-9]", "", cleaned)
        alpha_only = re.sub(r"[^A-Za-z]", "", cleaned)
        conf = float(confidence or 0.0)

        # Cursive handwriting → Tesseract <10% confidence + garbled chars.
        # Printed/typewriter words → 18-60% confidence even on degraded scans.
        base_text_like = (len(alnum) >= 4 and conf >= 18.0) or (len(alnum) >= 7 and conf >= 12.0)

        # Signature false-positive guard for existing queue items:
        # hide alphabetic typed words that are often misdetected as signatures.
        snippet_type = str(snippet.get("snippet_type") or "")
        alpha_ratio = (len(alpha_only) / max(1, len(alnum))) if alnum else 0.0
        printed_signature_word = (
            snippet_type == "signature"
            and len(alpha_only) >= 6
            and alpha_ratio >= 0.85
            and conf >= 8.0
        )

        return base_text_like or printed_signature_word
    except Exception:
        return False


def _render_snippet_card(
    snippet: Dict[str, Any],
    working_root: Path,
    idx: int,
    compact: bool = False,
) -> None:
    """Render a single snippet review card with image, details, and action buttons."""
    review_id = snippet["review_id"]
    snippet_type = snippet["snippet_type"]
    snippet_path = Path(snippet["snippet_path"])
    snippet_path = _resolve_snippet_path(snippet_path, working_root)
    page_num = snippet["page_num"]
    accuracy_impact = snippet["accuracy_impact"]
    reviewer_role = snippet["reviewer_role"]
    status = snippet.get("status", "pending")

    type_cfg = SNIPPET_TYPE_CONFIG.get(snippet_type, SNIPPET_TYPE_CONFIG["signature"])
    status_cfg = STATUS_BADGES.get(status, STATUS_BADGES["pending"])
    file_path = str(snippet.get("file_path") or "")
    doc_link = _build_document_page_link(file_path=file_path, page_num=int(page_num or 1))

    card_padding = "0.85rem" if compact else "1.2rem"
    label_font = "0.72rem" if compact else "0.75rem"
    tag_padding = "0.25rem 0.55rem" if compact else "0.3rem 0.7rem"

    # ── Card container ──
    st.markdown(
        f"""
        <div style="
            border: 1px solid {type_cfg['border']};
            border-radius: 12px;
            padding: {card_padding};
            background: linear-gradient(135deg, {type_cfg['bg']} 0%, #FFFFFF 100%);
            margin-bottom: 0.55rem;
            box-shadow: 0 2px 8px rgba(0,0,0,0.04);
        ">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:0.5rem;">
                <div style="display:flex; align-items:center; gap:0.5rem;">
                    <span style="
                        background:{type_cfg['color']};
                        color:white;
                        font-size:{label_font};
                        font-weight:700;
                        padding:{tag_padding};
                        border-radius:6px;
                        letter-spacing:0.3px;
                    ">{type_cfg['icon']} {type_cfg['label']}</span>
                    <span style="
                        font-size:{label_font};
                        color:#6B7280;
                        font-weight:500;
                        background:#F3F4F6;
                        padding:0.2rem 0.45rem;
                        border-radius:4px;
                    ">📄 Page {page_num}</span>
                </div>
                <span style="
                    background:{status_cfg['bg']};
                    color:{status_cfg['color']};
                    font-size:{label_font};
                    font-weight:600;
                    padding:0.2rem 0.5rem;
                    border-radius:5px;
                    border: 1px solid {status_cfg['color']}22;
                ">{status_cfg['label']}</span>
            </div>
            <div style="display:flex; gap:1.5rem; align-items:flex-start; flex-wrap:wrap;">
                <div style="flex:0 0 auto;">
                    <div style="font-size:{label_font}; color:#374151; margin-bottom:0.24rem;">
                        <b>Accuracy Impact:</b>
                        <span style="color:#DC2626; font-weight:700; font-size:0.82rem;">
                            −{accuracy_impact:.2f}%
                        </span>
                    </div>
                    <div style="font-size:{label_font}; color:#374151;">
                        <b>Assigned To:</b>
                        <span style="font-weight:500; color:{type_cfg['color']};">{reviewer_role}</span>
                    </div>
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if doc_link and file_path:
        # Use native OS file opener via button callback (bypasses all browser restrictions)
        def open_source_file():
            """Open file with system default application to specific page."""
            try:
                page_num_int = int(page_num or 1)
                file_path_str = str(file_path)
                
                if os.name == 'nt':  # Windows
                    # Try Adobe Reader with page parameter first
                    try:
                        # Adobe Reader command: AcroRd32.exe /A "page=X=OpenActions" file.pdf
                        subprocess.run(
                            [r'AcroRd32.exe', f'/A', f'page={page_num_int}', file_path_str],
                            check=False,
                            timeout=5
                        )
                    except (FileNotFoundError, subprocess.TimeoutExpired):
                        # Fallback: Try Acrobat
                        try:
                            subprocess.run(
                                [r'Acrobat.exe', f'/A', f'page={page_num_int}', file_path_str],
                                check=False,
                                timeout=5
                            )
                        except (FileNotFoundError, subprocess.TimeoutExpired):
                            # Final fallback: Use default app (won't open to page, but will work)
                            os.startfile(file_path_str)
                elif os.uname().sysname == 'Darwin':  # macOS
                    # macOS: use open with -a for app and page parameter via URL scheme
                    file_uri = Path(file_path_str).resolve().as_uri()
                    if page_num_int > 1:
                        file_uri += f'#page={page_num_int}'
                    subprocess.run(['open', file_uri], check=True)
                else:  # Linux
                    # Linux: xdg-open respects file:// URLs with #page anchor
                    file_uri = Path(file_path_str).resolve().as_uri()
                    if page_num_int > 1:
                        file_uri += f'#page={page_num_int}'
                    subprocess.run(['xdg-open', file_uri], check=True)
            except Exception as e:
                st.error(f"Failed to open file: {e}")
        
        col_open, _ = st.columns([2, 3])
        with col_open:
            st.button(
                f"🔗",
                on_click=open_source_file,
                key=f"open_doc_{review_id}",
                help="Opens the source document to the specified page with your default PDF viewer"
            )

    # ── Render image ──
    if snippet_path.exists():
        try:
            img = Image.open(str(snippet_path))
            if compact:
                thumb = _build_uniform_thumbnail(img, width=520, height=280)
                st.image(
                    thumb,
                    use_column_width=True,
                    caption=f"{type_cfg['icon']} Page {page_num} | −{accuracy_impact:.2f}%",
                )
            else:
                st.image(
                    img,
                    use_column_width=True,
                    caption=f"{type_cfg['icon']} {type_cfg['label']} — Page {page_num} | Impact: −{accuracy_impact:.2f}%",
                )
        except Exception as img_err:
            st.error(f"Could not load snippet image: {img_err}")
    else:
        st.warning(f"Snippet file not found: `{snippet_path.name}`")

    # ── Review reason + review metadata for already-reviewed items ──
    if status in ("accepted", "rejected"):
        reason = snippet.get("review_reason", "")
        reviewed_by = snippet.get("reviewed_by", "")
        reviewed_at = snippet.get("reviewed_at", "")
        action_label = "Accepted" if status == "accepted" else "Rejected"
        st.markdown(
            f"""
            <div style="
                background:#F9FAFB; border:1px solid #E5E7EB; border-radius:8px;
                padding:0.6rem 0.8rem; margin-top:0.3rem; font-size:0.75rem; color:#374151;
            ">
                <b>{action_label}</b> by <i>{reviewed_by or 'Unknown'}</i>
                {f'on {reviewed_at[:19]}' if reviewed_at else ''}<br/>
                {f'<b>Reason:</b> {reason}' if reason else ''}
            </div>
            """,
            unsafe_allow_html=True,
        )
        return  # No action buttons for already-reviewed items

    # ── Action buttons for pending items ──
    # Acceptance reason selector
    type_reasons = ACCEPTANCE_REASONS.get(snippet_type, []) + GENERIC_REASONS
    reason_key = f"reason_{review_id}"
    custom_key = f"custom_reason_{review_id}"

    selected_reason = st.selectbox(
        "Acceptance Reason",
        options=type_reasons,
        key=reason_key,
        label_visibility="collapsed",
        help="Select or type a reason for accepting this visual element",
    )

    # Show custom text field if "Custom reason..." is selected
    custom_reason_text = ""
    if selected_reason == "Custom reason...":
        custom_reason_text = st.text_input(
            "Enter custom reason:",
            key=custom_key,
            placeholder="Describe why this element should be accepted...",
        )

    final_reason = custom_reason_text if selected_reason == "Custom reason..." else selected_reason

    # Action buttons
    btn_col1, btn_col2 = st.columns([1, 1])
    with btn_col1:
        if st.button(
            "✅ Accept & Learn",
            key=f"btn_acc_{review_id}",
            type="primary",
            use_container_width=True,
        ):
            st.session_state["active_review_editor"] = review_id
            st.rerun()

    with btn_col2:
        if st.button(
            "❌ Reject",
            key=f"btn_rej_{review_id}",
            use_container_width=True,
        ):
            try:
                update_snippet_review_status(
                    review_id=review_id,
                    status="rejected",
                    review_reason="Rejected — baseline accuracy maintained",
                    reviewed_by="Dashboard User",
                )
                st.toast("❌ Rejected — baseline accuracy maintained.", icon="🚫")
                st.rerun()
            except Exception as e:
                st.error(f"Failed to reject snippet: {e}")

    if st.session_state.get("active_review_editor") == review_id:
        st.markdown(
            f"""
            <div style="
                margin-top:0.6rem; border:1px solid #BFDBFE; border-radius:10px;
                background:#EFF6FF; padding:0.7rem;
            ">
                <div style="font-size:0.82rem; font-weight:700; color:#1E3A8A; margin-bottom:0.15rem;">
                    📝 {reviewer_role} Verification Window
                </div>
                <div style="font-size:0.74rem; color:#334155;">Type visible content, then submit to accept and auto-tag similar pending snippets.</div>
            </div>
            """,
            unsafe_allow_html=True,
        )

        if snippet_path.exists():
            try:
                preview_img = Image.open(str(snippet_path))
                st.image(_build_uniform_thumbnail(preview_img, width=640, height=220), use_column_width=True)
            except Exception:
                pass

        text_key = f"typed_content_{review_id}"
        typed_content = st.text_area(
            "Content in this snippet",
            key=text_key,
            placeholder=f"{reviewer_role} types here...",
            height=90,
        )

        submit_col, cancel_col = st.columns(2)
        with submit_col:
            if st.button("Submit & Accept", key=f"submit_accept_{review_id}", type="primary", use_container_width=True):
                if not final_reason or final_reason == "Custom reason...":
                    st.warning("⚠️ Please select or enter an acceptance reason before approving.")
                    return
                typed = (typed_content or "").strip()
                if not typed:
                    st.warning("⚠️ Please enter the visible content before accepting.")
                    return

                matched_vector_path = None
                vector_dir = working_root / "data" / "visual_memory" / snippet["smart_id"]
                if "visual_memory" in st.session_state and st.session_state.visual_memory and snippet_path.exists():
                    try:
                        candidate_vector = st.session_state.visual_memory.extract_vector(str(snippet_path))
                        if candidate_vector is not None:
                            vector_dir.mkdir(parents=True, exist_ok=True)
                            vector_path = vector_dir / f"{review_id}.npy"
                            np.save(str(vector_path), candidate_vector)
                            matched_vector_path = str(vector_path)
                    except Exception:
                        pass

                reason_with_text = f"{final_reason} | Role={reviewer_role} | Verified Content: {typed}"
                try:
                    update_snippet_review_status(
                        review_id=review_id,
                        status="accepted",
                        feature_vector_path=matched_vector_path,
                        review_reason=reason_with_text,
                        reviewed_by=reviewer_role,
                    )

                    # ── Index reviewed snippet to OpenSearch for searchability ──
                    try:
                        doc_id = snippet["smart_id"]
                        page_num_val = int(page_num or 1)
                        
                        # Create reviewed snippet entry
                        reviewed_entry = {
                            "page": page_num_val,
                            "snippet_type": snippet_type,
                            "verified_content": typed,
                            "reviewer_role": reviewer_role,
                            "acceptance_reason": final_reason,
                            "reviewed_at": datetime.now().isoformat()
                        }
                        
                        # Initialize OpenSearch client and append to reviewed_snippets
                        try:
                            os_client = OpenSearchClient()
                            
                            # Get current document to check for existing reviewed_snippets
                            try:
                                doc_response = os_client.client.get(index=os_client.index_name, id=doc_id)
                                existing_reviewed = doc_response['_source'].get('reviewed_snippets', [])
                            except Exception:
                                # Document doesn't exist yet or can't be retrieved, start fresh
                                existing_reviewed = []
                            
                            # Append new reviewed snippet
                            reviewed_snippets = existing_reviewed + [reviewed_entry]
                            
                            # Update document with reviewed snippets and combined searchable content
                            combined_content = " ".join([entry["verified_content"] for entry in reviewed_snippets])
                            
                            update_payload = {
                                "reviewed_snippets": reviewed_snippets,
                                "reviewed_content": combined_content
                            }
                            
                            os_client.update_document(doc_id=doc_id, updates=update_payload)
                        except Exception as index_error:
                            # Log but don't fail the review acceptance
                            pass
                    except Exception as review_index_error:
                        # Silent fail for indexing - doesn't block review acceptance
                        pass

                    auto_count = 0
                    if "visual_memory" in st.session_state and st.session_state.visual_memory and vector_dir.exists():
                        siblings = get_all_reviews_for_doc(snippet["smart_id"])
                        for sib in siblings:
                            if sib.get("status") != "pending":
                                continue
                            if sib.get("review_id") == review_id:
                                continue
                            if str(sib.get("snippet_type") or "") != str(snippet_type):
                                continue

                            sib_path = _resolve_snippet_path(Path(str(sib.get("snippet_path") or "")), working_root)
                            if not sib_path.exists():
                                continue

                            try:
                                is_match, matched_path = st.session_state.visual_memory.match_snippet(
                                    candidate_image_path=str(sib_path),
                                    approved_vectors_dir=str(vector_dir),
                                    threshold=0.90,
                                )
                                if not is_match:
                                    continue

                                update_snippet_review_status(
                                    review_id=str(sib.get("review_id")),
                                    status="accepted",
                                    feature_vector_path=matched_path,
                                    review_reason=f"Auto-tagged from {review_id} | Role={reviewer_role} | Verified Content: {typed}",
                                    reviewed_by=reviewer_role,
                                )
                                auto_count += 1
                            except Exception:
                                continue

                    st.session_state.pop("active_review_editor", None)
                    st.toast(f"✅ Accepted by {reviewer_role}. Auto-tagged {auto_count} similar snippet(s).", icon="✨")
                    st.rerun()
                except Exception as e:
                    st.error(f"Failed to accept snippet: {e}")
        with cancel_col:
            if st.button("Close", key=f"cancel_accept_{review_id}", use_container_width=True):
                st.session_state.pop("active_review_editor", None)
                st.rerun()

def render_snippet_review_tab(config: Any) -> None:
    """Render the production-grade HITL Visual Verification Portal."""

    # ── Page Header ──
    st.markdown(
        """
        <div style="margin-bottom:0.5rem;">
            <h3 style="margin:0; color:#1E293B;">🔍 Visual Verification Portal</h3>
            <p style="margin:0.2rem 0 0 0; font-size:0.85rem; color:#64748B;">
                Review extracted visual elements (signatures, stamps, logos) to improve document accuracy.
                Accepted elements are memorized by the CNN engine and auto-approved in future scans.
            </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # ── Paths & initialization ──
    working_root = Path(config.paths.working_root)

    # Clear any stale viewer state from older builds to avoid unintended document auto-open.
    st.session_state.pop("review_viewer_target", None)

    if "visual_memory" not in st.session_state:
        try:
            st.session_state.visual_memory = VisualMemoryEngine()
        except Exception:
            st.session_state.visual_memory = None

    # ── Fetch documents with reviews ──
    docs = get_docs_with_reviews()
    if not docs:
        st.info("📭 No documents with visual review elements found. Process documents first to generate snippet reviews.")
        return

    # ── Document Selector ──
    st.markdown("---")
    doc_options = {}
    smart_id_to_label = {}
    for doc in docs:
        fname = doc.get("file_name", "Unknown")
        sid = doc.get("smart_id", "")
        pending = doc.get("pending_count", 0)
        label = f"{'🔴 ' if pending > 0 else '🟢 '}{fname}  —  {pending} pending | {doc.get('accepted_count', 0)} accepted | {doc.get('rejected_count', 0)} rejected"
        doc_options[label] = doc
        smart_id_to_label[sid] = label

    option_keys = list(doc_options.keys())

    # Restore selection from session state so reruns don't jump to index 0
    saved_smart_id = st.session_state.get("review_selected_smart_id", "")
    saved_label = smart_id_to_label.get(saved_smart_id, "")
    default_index = option_keys.index(saved_label) if saved_label in option_keys else 0

    selected_label = st.selectbox(
        "📂 Select Document to Review",
        options=option_keys,
        index=default_index,
        help="Documents with pending reviews are marked with 🔴",
    )
    selected_doc = doc_options[selected_label]
    selected_smart_id = selected_doc["smart_id"]

    # Reset sticky filters when switching documents so old role/status filters
    # do not silently hide valid snippets (common cause of seeing only logos/stamps).
    prev_doc = st.session_state.get("review_prev_selected_smart_id")
    if prev_doc != selected_smart_id:
        st.session_state["snippet_status_filter"] = "All Statuses"
        st.session_state["snippet_role_filter"] = "All Roles"
        st.session_state["review_prev_selected_smart_id"] = selected_smart_id

    st.session_state["review_selected_smart_id"] = selected_smart_id

    # ── Per-Document Dynamic KPI Metrics ──
    baseline_acc = selected_doc.get("extraction_accuracy") or 0.0
    enhanced_acc = selected_doc.get("enhanced_accuracy") or baseline_acc
    pending_count = selected_doc.get("pending_count", 0)
    accepted_count = selected_doc.get("accepted_count", 0)
    rejected_count = selected_doc.get("rejected_count", 0)
    total_snippets = selected_doc.get("total_snippets", 0)
    pending_impact = selected_doc.get("pending_impact") or 0.0
    uplift = max(0, enhanced_acc - baseline_acc)

    m1, m2, m3, m4, m5 = st.columns(5)
    with m1:
        _render_metric_card(
            "Baseline Accuracy", f"{baseline_acc:.1f}%",
            "Raw OCR extraction", "#6366F1", "#EEF2FF", "#4338CA", "📊",
        )
    with m2:
        _render_metric_card(
            "Enhanced Accuracy", f"{enhanced_acc:.1f}%",
            f"+{uplift:.1f}% uplift" if uplift > 0 else "No uplift yet",
            "#059669", "#ECFDF5", "#047857", "📈",
        )
    with m3:
        _render_metric_card(
            "Pending Review", f"{pending_count}",
            f"−{pending_impact:.1f}% potential impact",
            "#F59E0B", "#FFFBEB", "#B45309", "⏳",
        )
    with m4:
        _render_metric_card(
            "Accepted", f"{accepted_count}",
            "Templates memorized",
            "#059669", "#ECFDF5", "#047857", "✅",
        )
    with m5:
        _render_metric_card(
            "Rejected", f"{rejected_count}",
            "Accuracy penalty kept",
            "#DC2626", "#FEF2F2", "#991B1B", "❌",
        )

    # ── Document info bar ──
    st.markdown(
        f"""
        <div style="
            background:#F8FAFC; border:1px solid #E2E8F0; border-radius:8px;
            padding:0.6rem 1rem; margin:0.75rem 0; font-size:0.78rem; color:#475569;
        ">
            <b>File:</b> {selected_doc.get('file_name', 'Unknown')} &nbsp;|&nbsp;
            <b>Smart ID:</b> <code>{selected_smart_id}</code> &nbsp;|&nbsp;
            <b>Pipeline:</b> {selected_doc.get('pipeline_type', 'N/A')} &nbsp;|&nbsp;
            <b>Status:</b> {selected_doc.get('approval_status', 'N/A')}
        </div>
        """,
        unsafe_allow_html=True,
    )

    # ── Sub-tabs: Review Queue | Activity Log | Storage ──
    review_subtab, log_subtab, storage_subtab = st.tabs([
        "📋 Review Queue", "📜 Activity Log", "💾 Storage Management"
    ])

    # ════════════════════════════════════════════════════════════════════════
    # TAB 1: Review Queue
    # ════════════════════════════════════════════════════════════════════════
    with review_subtab:
        all_snippets = get_all_reviews_for_doc(selected_smart_id)
        if not all_snippets:
            st.info("No visual elements found for this document.")
            return

        # ── Status & Role filters ──
        filter_col1, filter_col2 = st.columns(2)
        with filter_col1:
            status_filter = st.selectbox(
                "Filter by Status",
                ["All Statuses", "Pending", "Accepted", "Rejected"],
                index=0,
                key="snippet_status_filter",
            )
        with filter_col2:
            role_filter = st.selectbox(
                "Filter by Reviewer Role",
                ["All Roles", "Contract Auditor", "Operations Manager", "Marketing Reviewer", "Text Specialist"],
                index=0,
                key="snippet_role_filter",
                help="Contract Auditor: Signatures • Operations Manager: Stamps • Marketing Reviewer: Logos • Text Specialist: OCR Anomalies",
            )

        # Apply filters
        filtered = all_snippets
        if status_filter != "All Statuses":
            status_map = {"Pending": "pending", "Accepted": "accepted", "Rejected": "rejected"}
            filtered = [s for s in filtered if s.get("status") == status_map.get(status_filter, "")]
        if role_filter != "All Roles":
            filtered = [s for s in filtered if s.get("reviewer_role") == role_filter]

        # Config-driven strict visual policy (same semantics as OCR worker).
        preprocessing_cfg = dict(getattr(config.ocr, "preprocessing", {}) or {})

        # Optional static per-file overrides for known PDFs.
        selected_file_path = str(selected_doc.get("file_path") or "")
        normalized_path = selected_file_path.replace("\\", "/").lower()
        overrides = preprocessing_cfg.get("visual_pdf_overrides") or []
        if isinstance(overrides, list) and normalized_path:
            for item in overrides:
                if not isinstance(item, dict):
                    continue
                match_sub = str(item.get("match_substring", "") or "").strip().lower()
                if match_sub and match_sub in normalized_path:
                    for key in (
                        "visual_allowed_types",
                        "signature_min_impact",
                        "logo_min_impact",
                        "stamp_min_impact",
                        "text_anomaly_min_impact",
                        "max_per_page_per_type",
                        "review_keep_signatures",
                    ):
                        if key in item:
                            preprocessing_cfg[key] = item[key]
                    break

        # For matched static overrides, keep signature snippets visible in review
        # even if OCR/text heuristics think they are text-like.
        keep_signatures_visible = bool(preprocessing_cfg.get("review_keep_signatures", False))
        allowed_types = {
            str(t).strip().lower()
            for t in (preprocessing_cfg.get("visual_allowed_types") or [])
            if str(t).strip()
        }
        if keep_signatures_visible and allowed_types:
            # Legacy/manual signature boxes may have been saved as text_anomaly
            # before force-keep type pinning. Include them in review visibility.
            allowed_types.add("text_anomaly")
        if allowed_types:
            filtered = [
                s for s in filtered
                if str((s or {}).get("snippet_type", "")).lower() in allowed_types
            ]

        min_impact_by_type = {
            "signature": float(preprocessing_cfg.get("signature_min_impact", 0.0) or 0.0),
            "logo": float(preprocessing_cfg.get("logo_min_impact", 0.0) or 0.0),
            "stamp": float(preprocessing_cfg.get("stamp_min_impact", 0.0) or 0.0),
            "text_anomaly": float(preprocessing_cfg.get("text_anomaly_min_impact", 0.0) or 0.0),
        }
        filtered = [
            s for s in filtered
            if float((s or {}).get("accuracy_impact", 0.0) or 0.0)
            >= min_impact_by_type.get(str((s or {}).get("snippet_type", "")).lower(), 0.0)
        ]

        max_per_type_cfg = preprocessing_cfg.get("max_per_page_per_type") or {}
        if isinstance(max_per_type_cfg, dict) and filtered:
            grouped: Dict[str, List[Dict[str, Any]]] = {}
            for s in filtered:
                t = str((s or {}).get("snippet_type", "")).lower()
                page = int((s or {}).get("page_num", 0) or 0)
                key = f"{t}:{page}"
                grouped.setdefault(key, []).append(s)

            topk: List[Dict[str, Any]] = []
            for key, items in grouped.items():
                s_type = key.split(":", 1)[0]
                sorted_items = sorted(
                    items,
                    key=lambda it: float((it or {}).get("accuracy_impact", 0.0) or 0.0),
                    reverse=True,
                )
                try:
                    limit = int(max_per_type_cfg.get(s_type, 0) or 0)
                except Exception:
                    limit = 0
                if limit > 0:
                    sorted_items = sorted_items[:limit]
                topk.extend(sorted_items)
            filtered = topk

        # Auto-hide obvious dot/blob noise, printed-font text, and OCR text from queue.
        hidden_noise_count = 0
        hidden_text_count = 0
        hidden_printed_count = 0
        visible_snippets = []
        for snippet in filtered:
            snippet_type = str((snippet or {}).get("snippet_type") or "").lower()

            if keep_signatures_visible and snippet_type == "signature":
                visible_snippets.append(snippet)
                continue

            if _is_obvious_noise_review_snippet(snippet, working_root):
                hidden_noise_count += 1
                continue
            if _is_printed_font_review_snippet(snippet, working_root):
                hidden_printed_count += 1
                continue
            if _is_text_like_review_snippet(snippet, working_root):
                hidden_text_count += 1
                continue
            visible_snippets.append(snippet)
        filtered = visible_snippets

        # Sort: pending first (urgent), then by page number for readability
        def sort_priority(s):
            status_order = {"pending": 0, "accepted": 1, "rejected": 2}
            return (status_order.get(s.get("status"), 3), s.get("page_num", 0))
        filtered = sorted(filtered, key=sort_priority)

        if hidden_noise_count > 0:
            st.caption(f"Auto-hidden {hidden_noise_count} obvious noise snippets from review queue.")
        if hidden_printed_count > 0:
            st.caption(f"Auto-hidden {hidden_printed_count} printed-font snippets (not handwriting).")
        if hidden_text_count > 0:
            st.caption(f"Auto-hidden {hidden_text_count} text-like snippets (kept in OCR flow).")

        st.markdown("<div style='margin:0.35rem 0 0.5rem 0;'><b>Accuracy Impact Waterfall</b></div>", unsafe_allow_html=True)
        _render_accuracy_waterfall_chart(filtered, selected_doc)

        if not filtered:
            st.info(f"No snippets matching filters: Status={status_filter}, Role={role_filter} ({len(all_snippets)} total available)")
        else:
            st.markdown(
                f"<p style='font-size:0.82rem; color:#64748B; margin:0.5rem 0;'>"
                f"Showing <b>{len(filtered)}</b> of <b>{len(all_snippets)}</b> elements</p>",
                unsafe_allow_html=True,
            )

            # ── Render snippet cards in compact 4-column tile layout ──
            tiles_per_row = 4
            for row_start in range(0, len(filtered), tiles_per_row):
                cols = st.columns(tiles_per_row)
                for col_idx in range(tiles_per_row):
                    snippet_idx = row_start + col_idx
                    if snippet_idx >= len(filtered):
                        continue
                    with cols[col_idx]:
                        _render_snippet_card(filtered[snippet_idx], working_root, snippet_idx, compact=True)

    # ════════════════════════════════════════════════════════════════════════
    # TAB 2: Activity Log
    # ════════════════════════════════════════════════════════════════════════
    with log_subtab:
        st.markdown(
            """
            <div style="margin-bottom:0.5rem;">
                <h4 style="margin:0; color:#1E293B;">📜 Review Activity History</h4>
                <p style="margin:0.2rem 0; font-size:0.78rem; color:#64748B;">
                    Chronological audit trail of all review decisions for this document.
                </p>
            </div>
            """,
            unsafe_allow_html=True,
        )

        activity_log = get_review_activity_log(smart_id=selected_smart_id, limit=50)

        if not activity_log:
            st.info("No review activity recorded yet for this document. Accept or reject snippets to create entries.")
        else:
            for entry in activity_log:
                action = entry.get("action", "")
                action_icon = "✅" if action == "accepted" else ("❌" if action == "rejected" else "🔄")
                action_color = "#059669" if action == "accepted" else ("#DC2626" if action == "rejected" else "#6B7280")
                ts = entry.get("timestamp", "")[:19].replace("T", " ")
                acc_before = entry.get("accuracy_before", 0) or 0
                acc_after = entry.get("accuracy_after", 0) or 0
                delta = acc_after - acc_before

                st.markdown(
                    f"""
                    <div style="
                        border-left: 3px solid {action_color};
                        padding: 0.6rem 0.8rem;
                        margin-bottom: 0.5rem;
                        background: #FAFAFA;
                        border-radius: 0 6px 6px 0;
                        font-size: 0.78rem;
                    ">
                        <div style="display:flex; justify-content:space-between; align-items:center;">
                            <span>
                                <b style="color:{action_color};">{action_icon} {action.title()}</b>
                                — <i>{entry.get('snippet_type', '')}</i>
                                by <b>{entry.get('actor', 'Unknown')}</b>
                            </span>
                            <span style="color:#9CA3AF; font-size:0.72rem;">{ts}</span>
                        </div>
                        <div style="margin-top:0.25rem; color:#4B5563;">
                            {f'<b>Reason:</b> {entry.get("reason", "")}' if entry.get("reason") else ''}
                        </div>
                        <div style="margin-top:0.15rem; color:#6B7280; font-size:0.72rem;">
                            Accuracy: {acc_before:.1f}% → {acc_after:.1f}%
                            <span style="color:{'#059669' if delta >= 0 else '#DC2626'}; font-weight:600;">
                                ({'+' if delta >= 0 else ''}{delta:.1f}%)
                            </span>
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

    # ════════════════════════════════════════════════════════════════════════
    # TAB 3: Storage Management
    # ════════════════════════════════════════════════════════════════════════
    with storage_subtab:
        st.markdown(
            """
            <div style="margin-bottom:0.5rem;">
                <h4 style="margin:0; color:#1E293B;">💾 Snippet Storage Management</h4>
                <p style="margin:0.2rem 0; font-size:0.78rem; color:#64748B;">
                    Monitor and manage disk usage from cropped visual snippet files.
                </p>
            </div>
            """,
            unsafe_allow_html=True,
        )

        try:
            stats = get_snippet_storage_stats()
            total_mb = stats.get("total_size_mb", 0)
            per_doc = stats.get("per_doc", {})

            # Summary metrics
            s1, s2, s3 = st.columns(3)
            with s1:
                _render_metric_card(
                    "Total Disk Usage",
                    f"{total_mb:.2f} MB",
                    f"{sum(d.get('file_count', 0) for d in per_doc.values())} files on disk",
                    "#6366F1", "#EEF2FF", "#4338CA", "💿",
                )
            with s2:
                _render_metric_card(
                    "Documents Tracked",
                    str(len(per_doc)),
                    "With visual snippets",
                    "#0891B2", "#ECFEFF", "#155E75", "📁",
                )
            with s3:
                total_accepted = sum(d.get("accepted", 0) for d in per_doc.values())
                _render_metric_card(
                    "CNN Templates Saved",
                    str(total_accepted),
                    "Feature vectors (.npy) preserved",
                    "#059669", "#ECFDF5", "#047857", "🧠",
                )

            # Per-document breakdown table
            if per_doc:
                st.markdown("#### Per-Document Breakdown")
                rows = []
                for sid, info in per_doc.items():
                    rows.append({
                        "Document": info.get("file_name", sid),
                        "Files": info.get("file_count", 0),
                        "Size": _format_file_size(info.get("total_size", 0)),
                        "Pending": info.get("pending", 0),
                        "Accepted": info.get("accepted", 0),
                        "Rejected": info.get("rejected", 0),
                    })
                st.dataframe(rows, use_container_width=True, hide_index=True)

            # Purge controls
            st.markdown("---")
            st.markdown("#### 🗑️ Purge Old Snippet Files")
            st.caption(
                "Remove crop image files from disk for snippets that have been accepted or rejected. "
                "CNN feature vectors (.npy) are always preserved — only the large PNG crop files are deleted."
            )
            purge_days = st.number_input(
                "Delete snippets older than (days):",
                min_value=1,
                max_value=365,
                value=30,
                key="purge_days_input",
            )
            if st.button("🗑️ Purge Old Snippets", key="btn_purge_snippets", type="secondary"):
                result = purge_old_snippets(older_than_days=purge_days)
                freed = result.get("bytes_freed", 0)
                count = result.get("purged_count", 0)
                if count > 0:
                    st.success(f"✅ Purged {count} files, freed {_format_file_size(freed)}")
                else:
                    st.info("No snippet files older than the specified threshold found.")

        except Exception as e:
            st.warning(f"Could not load storage statistics: {e}")
