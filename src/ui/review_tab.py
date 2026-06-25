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
    get_page_segmentation_breakdown,
    get_snippet_suppressions,
)
from indexing.opensearch_client import OpenSearchClient
from ocr.visual_memory import VisualMemoryEngine

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
    "faded_text": [
        "Faded text readable — verified content matches",
        "Enhanced image text — verified legibility",
        "Low-contrast printed text — routing to OCR flow",
        "Partially faded text — spelling verified",
        "Expected printed text — no signature present",
    ],
    "full_page": [
        "Verified all elements present and extracted",
        "No signatures or stamps required on this page",
        "Valid layout structure",
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
    "faded_text": {
        "icon": "📝",
        "label": "Faded Text",
        "color": "#3B82F6",
        "bg": "#EFF6FF",
        "border": "#93C5FD",
    },
    "handwritten": {
        "icon": "✍️",
        "label": "Handwritten",
        "color": "#EC4899",
        "bg": "#FDF2F8",
        "border": "#F9A8D4",
    },
    "noise": {
        "icon": "🔲",
        "label": "Noise/Artifact",
        "color": "#64748B",
        "bg": "#F8FAFC",
        "border": "#CBD5E1",
    },
    "full_page": {
        "icon": "📄",
        "label": "Full Page Validation",
        "color": "#475569",
        "bg": "#F1F5F9",
        "border": "#CBD5E1",
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


def _resolve_document_path(file_path: Any) -> Optional[str]:
    """Resolve legacy document file paths using current environment settings."""
    if not file_path:
        return None
    p = Path(str(file_path))
    if p.exists():
        return str(p)
    try:
        config = get_config()
        source_drive = getattr(getattr(config, "paths", None), "source_drive", None)
        if source_drive:
            mapped = Path(str(source_drive)) / p.name
            if mapped.exists():
                return str(mapped)
    except Exception:
        pass
    return None


def _open_source_document(file_path: Any, page_num: Any) -> None:
    """Open source document with system default app."""
    try:
        page_num_int = int(page_num or 1)
    except Exception:
        page_num_int = 1

    resolved_path = _resolve_document_path(file_path)
    if not resolved_path:
        st.error("Cannot open document: file path not found.")
        return

    try:
        if os.name == 'nt':
            os.startfile(str(resolved_path))
        else:
            uri = Path(resolved_path).resolve().as_uri()
            if page_num_int > 1:
                uri += f"#page={page_num_int}"
            subprocess.run(['xdg-open', uri], check=False)
    except Exception as e:
        st.error(f"Failed to open file: {e}")


def _render_page_composition_bar(selected_doc: Dict[str, Any]) -> None:
    smart_id = selected_doc.get("smart_id")
    if not smart_id:
        st.warning("No document selected.")
        return

    breakdown = get_page_segmentation_breakdown(smart_id)
    if not breakdown:
        st.info("No page segmentation breakdown available for this document.")
        return

    segment_cfg = {
        "clean": {"label": "Clean Text", "color": "#10B981"},
        "faded": {"label": "Faded Text", "color": "#3B82F6"},
        "logo": {"label": "Logo/Image", "color": "#8B5CF6"},
        "stamp": {"label": "Stamp", "color": "#F59E0B"},
        "handwritten": {"label": "Handwritten", "color": "#EC4899"},
        "whitespace": {"label": "Whitespace", "color": "#F1F5F9", "border": "#CBD5E1"},
        "noise": {"label": "Noise", "color": "#64748B"},
    }

    legend_html_parts = []
    for key, cfg in segment_cfg.items():
        border_style = f" border: 1px solid {cfg['border']};" if "border" in cfg else ""
        legend_html_parts.append(
            f'<span style="display: inline-flex; align-items: center; gap: 6px;">'
            f'<span style="display: inline-block; width: 12px; height: 12px; background: {cfg["color"]}; border-radius: 3px;{border_style}"></span>'
            f'{cfg["label"]}</span>'
        )
    legend_html = "".join(legend_html_parts)

    st.markdown(f"""<div style="border: 1px solid #E5E7EB; border-radius: 10px; padding: 16px; background: #FFFFFF; font-family: sans-serif; box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.05); margin-bottom: 15px;">
  <div style="font-weight: 700; color: #1E293B; margin-bottom: 12px; font-size: 15px;">Page-by-Page Composition Breakdown</div>
  <!-- Legend -->
  <div style="display: flex; flex-wrap: wrap; gap: 12px; font-size: 12px; color: #475569;">
    {legend_html}
  </div>
</div>""", unsafe_allow_html=True)

    file_path = _resolve_document_path(selected_doc.get("file_path"))

    for row in breakdown:
        page_num = row.get("page_num", 1)
        clean = float(row.get("clean_text_pct") or 0.0)
        faded = float(row.get("faded_text_pct") or 0.0)
        logo = float(row.get("logo_pct") or 0.0)
        stamp = float(row.get("stamp_pct") or 0.0)
        handwritten = float(row.get("handwritten_pct") or 0.0)
        whitespace = float(row.get("whitespace_pct") or 0.0)
        noise = float(row.get("noise_pct") or 0.0)

        # Normalize to 100%
        total = clean + faded + logo + stamp + handwritten + whitespace + noise
        if total > 0:
            scale = 100.0 / total
            clean *= scale
            faded *= scale
            logo *= scale
            stamp *= scale
            handwritten *= scale
            whitespace *= scale
            noise *= scale
        else:
            whitespace = 100.0

        col_btn, col_bar = st.columns([1.5, 8.5])
        with col_btn:
            if file_path:
                st.button(
                    f"🔗 P. {page_num}",
                    key=f"open_comp_page_{smart_id}_{page_num}",
                    on_click=_open_source_document,
                    args=(file_path, page_num),
                    help=f"Open document to page {page_num}"
                )
            else:
                st.markdown(f"**Page {page_num}**")

        with col_bar:
            page_html_parts = []
            page_html_parts.append(f"""<div style="font-family: sans-serif; margin-bottom: 14px; padding: 8px; border: 1px solid #E2E8F0; border-radius: 8px; background: #F8FAFC;">
  <div style="display: flex; justify-content: space-between; font-size: 11px; font-weight: 600; color: #64748B; margin-bottom: 4px;">
    <span>Page {page_num} Breakdown</span>
    <span>Width: {row.get('page_width_px', 0)}px | Height: {row.get('page_height_px', 0)}px</span>
  </div>
  <div style="display: flex; width: 100%; height: 20px; border-radius: 4px; overflow: hidden; background: #E2E8F0;">""")

            if clean > 0:
                page_html_parts.append(f'<div style="width: {clean:.2f}%; background: {segment_cfg["clean"]["color"]}; height: 100%;" title="Clean Text: {clean:.1f}%"></div>')
            if faded > 0:
                page_html_parts.append(f'<div style="width: {faded:.2f}%; background: {segment_cfg["faded"]["color"]}; height: 100%;" title="Faded Text: {faded:.1f}%"></div>')
            if logo > 0:
                page_html_parts.append(f'<div style="width: {logo:.2f}%; background: {segment_cfg["logo"]["color"]}; height: 100%;" title="Logo: {logo:.1f}%"></div>')
            if stamp > 0:
                page_html_parts.append(f'<div style="width: {stamp:.2f}%; background: {segment_cfg["stamp"]["color"]}; height: 100%;" title="Stamp: {stamp:.1f}%"></div>')
            if handwritten > 0:
                page_html_parts.append(f'<div style="width: {handwritten:.2f}%; background: {segment_cfg["handwritten"]["color"]}; height: 100%;" title="Handwritten: {handwritten:.1f}%"></div>')
            if whitespace > 0:
                ws_border = f" border-left: 1px solid {segment_cfg['whitespace']['border']}; border-right: 1px solid {segment_cfg['whitespace']['border']};"
                page_html_parts.append(f'<div style="width: {whitespace:.2f}%; background: {segment_cfg["whitespace"]["color"]};{ws_border} height: 100%;" title="Whitespace: {whitespace:.1f}%"></div>')
            if noise > 0:
                page_html_parts.append(f'<div style="width: {noise:.2f}%; background: {segment_cfg["noise"]["color"]}; height: 100%;" title="Noise: {noise:.1f}%"></div>')

            page_html_parts.append("""</div>
  <!-- Numeric breakdown tooltip inline -->
  <div style="display: flex; gap: 10px; font-size: 10px; color: #64748B; margin-top: 5px; flex-wrap: wrap;">""")

            metrics = [
                ("clean", clean, "Text"),
                ("faded", faded, "Faded"),
                ("logo", logo, "Logo"),
                ("stamp", stamp, "Stamp"),
                ("handwritten", handwritten, "Hand"),
                ("whitespace", whitespace, "WS"),
                ("noise", noise, "Noise"),
            ]
            for key, val, short_lbl in metrics:
                if val > 0:
                    cfg = segment_cfg[key]
                    border_style = f" border: 1px solid {cfg['border']};" if "border" in cfg else ""
                    pill_html = (
                        f'<span style="display: inline-flex; align-items: center; gap: 4px; white-space: nowrap;">'
                        f'<span style="display: inline-block; width: 8px; height: 8px; background: {cfg["color"]}; border-radius: 2px;{border_style}"></span>'
                        f'{short_lbl}: {val:.1f}%</span>'
                    )
                    page_html_parts.append(pill_html)

            page_html_parts.append("</div></div>")

            st.markdown("".join(page_html_parts), unsafe_allow_html=True)


def _render_accuracy_waterfall_chart(snippets: List[Dict[str, Any]], selected_doc: Dict[str, Any]) -> None:
    """Render accuracy waterfall chart showing true page composition corrected by reviews."""
    smart_id = selected_doc.get("smart_id")
    breakdown = get_page_segmentation_breakdown(smart_id)
    
    # Calculate average page composition percentages from database
    avg_clean = 0.0
    avg_whitespace = 0.0
    avg_faded = 0.0
    avg_logo = 0.0
    avg_stamp = 0.0
    avg_handwritten = 0.0
    avg_noise = 0.0
    
    if breakdown:
        num_pages = len(breakdown)
        avg_clean = sum(float(p.get("clean_text_pct") or 0.0) for p in breakdown) / num_pages
        avg_whitespace = sum(float(p.get("whitespace_pct") or 0.0) for p in breakdown) / num_pages
        avg_faded = sum(float(p.get("faded_text_pct") or 0.0) for p in breakdown) / num_pages
        avg_logo = sum(float(p.get("logo_pct") or 0.0) for p in breakdown) / num_pages
        avg_stamp = sum(float(p.get("stamp_pct") or 0.0) for p in breakdown) / num_pages
        avg_handwritten = sum(float(p.get("handwritten_pct") or 0.0) for p in breakdown) / num_pages
        avg_noise = sum(float(p.get("noise_pct") or 0.0) for p in breakdown) / num_pages
    else:
        # Fallback to general baseline if database records are empty
        baseline_acc = float(selected_doc.get("extraction_accuracy") or 0.0)
        avg_clean = max(1.0, baseline_acc)
        avg_whitespace = max(1.0, 100.0 - avg_clean)
        
    # Calculate counts and impact sums dynamically from snippets (all reviews for the doc)
    pending_counts = {}
    pending_impacts = {}
    accepted_impact_total = 0.0
    doc_snippet_types = set()
    
    for s in snippets:
        t = str(s.get("snippet_type") or "").strip().lower()
        doc_snippet_types.add(t)
        status = s.get("status", "pending")
        impact = max(0.0, float(s.get("accuracy_impact") or 0.0))
        
        if status == "pending":
            pending_counts[t] = pending_counts.get(t, 0) + 1
            pending_impacts[t] = pending_impacts.get(t, 0.0) + impact
        elif status == "accepted":
            accepted_impact_total += impact

    # Mapping display configuration for segments
    segment_cfg = {
        "clean": {"label": "Extractable Text", "value": avg_clean, "color": "#10B981"},
        "whitespace": {"label": "Whitespace", "value": avg_whitespace, "color": "#F1F5F9", "border": "#CBD5E1"},
        "faded": {"label": "Faded Text", "value": avg_faded, "color": "#3B82F6", "snippet_key": "faded_text"},
        "logo": {"label": "Logo/Image", "value": avg_logo, "color": "#8B5CF6", "snippet_key": "logo"},
        "stamp": {"label": "Stamp", "value": avg_stamp, "color": "#F59E0B", "snippet_key": "stamp"},
        "handwritten": {
            "label": "Handwritten",
            "value": avg_handwritten,
            "color": "#EC4899",
            "snippet_key": "handwritten",
            "extra_snippet_key": "signature"
        },
        "noise": {"label": "Noise", "value": avg_noise, "color": "#64748B", "snippet_key": "noise", "extra_snippet_key": "text_anomaly"},
    }

    # Add verified (accepted) snippet impacts to the baseline Extractable Text bar
    segment_cfg["clean"]["value"] += accepted_impact_total

    # For categories present as snippets in the document, use their actual snippet impact.
    # Ensure bars appear for any snippet type that exists, even if db composition is 0.
    for key, cfg in segment_cfg.items():
        if key == "clean" or key == "whitespace":
            continue

        has_snippets = False
        snippet_total_impact = 0.0
        if "snippet_key" in cfg:
            sk = cfg["snippet_key"]
            if sk in doc_snippet_types:
                has_snippets = True
            snippet_total_impact += pending_impacts.get(sk, 0.0)
        if "extra_snippet_key" in cfg:
            esk = cfg["extra_snippet_key"]
            if esk in doc_snippet_types:
                has_snippets = True
            snippet_total_impact += pending_impacts.get(esk, 0.0)

        if has_snippets:
            # Use actual snippet impact; guarantee minimum visibility if snippets exist
            cfg["value"] = max(snippet_total_impact, 1.0) if snippet_total_impact < 1.0 else snippet_total_impact

    # Normalize all segments to sum to exactly 100.0% dynamically
    total_val_sum = sum(cfg["value"] for cfg in segment_cfg.values())
    if total_val_sum > 0:
        scale = 100.0 / total_val_sum
        for cfg in segment_cfg.values():
            cfg["value"] *= scale
    else:
        segment_cfg["whitespace"]["value"] = 100.0

    # Order of presentation in waterfall
    order = ["clean", "whitespace", "faded", "logo", "stamp", "handwritten", "noise"]
    
    bars = []
    running_bottom = 0.0
    
    for key in order:
        cfg = segment_cfg[key]
        val = cfg["value"]
        if val <= 0.001:
            continue
            
        # Determine displaying label with snippet counts
        lbl = cfg["label"]
        count = 0
        if "snippet_key" in cfg:
            count += pending_counts.get(cfg["snippet_key"], 0)
        if "extra_snippet_key" in cfg:
            count += pending_counts.get(cfg["extra_snippet_key"], 0)
            
        if count > 0:
            lbl = f"{lbl} ({count})"
            
        border_color = cfg["color"]
        fill_color = cfg["color"]
        
        bars.append({
            "label": lbl,
            "value": val,
            "bottom": running_bottom,
            "color": border_color,
            "fill_color": fill_color,
            "text": f"{val:.2f}%",
            "style": "solid" if key != "whitespace" else "whitespace"
        })
        running_bottom = min(100.0, running_bottom + val)
        
    # Final Total bar representing 100% composition
    bars.append({
        "label": "Total",
        "value": 100.0,
        "bottom": 0.0,
        "color": "#2563EB",
        "fill_color": "#DBEAFE",
        "text": "100%",
        "style": "dotted"
    })

    left_pad = 40
    right_pad = 40
    chart_h = 240
    plot_top = 20
    plot_h = chart_h - 80

    # Ensure dynamic slots fit screen space and don't overflow
    bar_slot = max(120, int(800 / max(1, len(bars))))
    bar_width = int(bar_slot * 0.35)
    svg_width = left_pad + bar_slot * len(bars) + right_pad

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
        elif bar["style"] == "whitespace":
            bar_rects.append(
                f"<rect x='{x}' y='{y:.1f}' width='{bar_width}' height='{px_h:.1f}' rx='6' "
                f"fill='{bar['fill_color']}' stroke='#CBD5E1' stroke-width='1'></rect>"
            )
        else:
            bar_rects.append(
                f"<rect x='{x}' y='{y:.1f}' width='{bar_width}' height='{px_h:.1f}' rx='6' "
                f"fill='{bar['fill_color']}'></rect>"
            )

        pct_labels.append(
            f"<text x='{x + bar_width/2:.1f}' y='{max(12, y - 8):.1f}' text-anchor='middle' font-size='11' font-weight='700' fill='#0F172A'>{bar['text']}</text>"
        )
        x_labels.append(
            f"<text x='{x + bar_width/2:.1f}' y='{chart_h - 28}' text-anchor='middle' font-size='11' font-weight='700' fill='#111827'>{bar['label']}</text>"
        )
        
        border_dashed = " border:1px dashed #2563EB;" if bar["style"] == "dotted" else ""
        border_solid = " border:1px solid #CBD5E1;" if bar["style"] == "whitespace" else ""
        legend_items.append(
            f"<span style='display:inline-flex; align-items:center; gap:6px; margin-right:14px; font-size:12px; color:#334155;'>"
            f"<span style='display:inline-block; width:10px; height:10px; border-radius:2px; background:{bar['color']};{border_dashed}{border_solid}'></span>"
            f"{bar['label']}</span>"
        )

    svg = f"""
    <div style='border:1px solid #E5E7EB; border-radius:10px; background:#FFFFFF; padding:8px 10px 10px 10px;'>
      <svg width='{svg_width}' height='{chart_h}' viewBox='0 0 {svg_width} {chart_h}'>
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
    components.html(svg, height=chart_h + 75, width=svg_width + 10, scrolling=False)


def _render_snippet_card(
    snippet: Dict[str, Any],
    working_root: Path,
    idx: int,
    compact: bool = False,
) -> None:
    """Render a single snippet review card with image, details, and action buttons."""
    review_id = snippet.get("review_id") or ""
    snippet_type = snippet.get("snippet_type") or "signature"
    snippet_path = Path(str(snippet.get("snippet_path") or ""))
    snippet_path = _resolve_snippet_path(snippet_path, working_root)
    page_num = snippet.get("page_num") or 1
    accuracy_impact = float(snippet.get("accuracy_impact") or 0.0)
    reviewer_role = snippet.get("reviewer_role") or "Document Reviewer"
    status = snippet.get("status", "pending")
    extracted_text = snippet.get("extracted_text") or ""
    label_font = "0.72rem"
    final_reason = ""

    type_cfg = SNIPPET_TYPE_CONFIG.get(snippet_type, SNIPPET_TYPE_CONFIG["signature"])
    status_cfg = STATUS_BADGES.get(status, STATUS_BADGES["pending"])
    file_path = str(snippet.get("file_path") or "")

    # Wrap the entire card in a container with a border
    with st.container(border=True):
        st.markdown(f'<div class="snippet-card-anchor {status}"></div>', unsafe_allow_html=True)

        # ── Row 1: Header (Type tag, Page Number / Link) ──
        badge_html = f"""
            <div style="margin-top: 4px;">
                <span style="
                    background: {type_cfg['bg']};
                    color: {type_cfg['color']};
                    font-size: 0.72rem;
                    font-weight: 600;
                    padding: 0.2rem 0.55rem;
                    border-radius: 4px;
                    display: inline-flex;
                    align-items: center;
                    gap: 0.25rem;
                    white-space: nowrap;
                ">{type_cfg['icon']} {type_cfg['label']}</span>
            </div>
        """

        col_badge, col_btn = st.columns([1.3, 1.7])
        with col_badge:
            st.markdown(badge_html, unsafe_allow_html=True)
        with col_btn:
            if file_path:
                st.button(
                    f"📄 Page {page_num}",
                    key=f"open_card_page_{review_id}_{idx}",
                    on_click=_open_source_document,
                    args=(file_path, page_num),
                    use_container_width=True,
                    help=f"Open document to page {page_num}"
                )
            else:
                st.markdown(
                    f'<div style="text-align: right; margin-top: 4px;"><span style="background:#F1F5F9; color:#94A3B8; font-size:0.71rem; padding:0.2rem 0.5rem; border-radius:4px; border:1px solid #E2E8F0; display:inline-block;">📄 P. {page_num}</span></div>',
                    unsafe_allow_html=True
                )

        # ── Row 2: Render Snippet Image ──
        if snippet_path.exists():
            try:
                if compact:
                    thumb = _build_uniform_thumbnail(Image.open(str(snippet_path)), width=480, height=160)
                    st.image(thumb, use_column_width=True)
                else:
                    st.image(str(snippet_path), use_column_width=True)
            except Exception as img_err:
                st.error(f"Could not load snippet image: {img_err}")
        else:
            st.warning(f"Snippet file not found: `{snippet_path.name}`")

        # ── Row 3: Metadata Details (table format) ──
        st.markdown(f"""
            <table style="width: 100%; border-collapse: collapse; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; font-size: 0.72rem; margin-top: 0.5rem; margin-bottom: 0.5rem;">
                <tbody>
                    <tr style="border-bottom: 1px solid #F1F5F9; height: 1.8rem;">
                        <td style="color: #64748B; font-weight: 500; padding: 0.2rem 0;">Status</td>
                        <td style="padding: 0.2rem 0; text-align: right;">
                            <span style="background: {status_cfg['bg']}; color: {status_cfg['color']}; padding: 0.15rem 0.4rem; border-radius: 4px; font-weight: 600; font-size: 0.68rem;">{status_cfg['label']}</span>
                        </td>
                    </tr>
                    <tr style="height: 1.8rem;">
                        <td style="color: #64748B; font-weight: 500; padding: 0.2rem 0;">Accuracy Impact</td>
                        <td style="padding: 0.2rem 0; color: #DC2626; font-weight: 700; text-align: right; font-size: 0.78rem;">-{accuracy_impact:.2f}%</td>
                    </tr>
                </tbody>
            </table>
        """, unsafe_allow_html=True)

        with st.expander("RADAR Engine Extracted", expanded=False):
            st.code(extracted_text.strip() if extracted_text.strip() else "(empty)", language=None)

        # ── Row 4: Review section ──
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
            if status == "accepted":
                if st.button(
                    "❌ Reject",
                    key=f"btn_reject_accepted_{review_id}_{idx}",
                    use_container_width=True,
                ):
                    update_snippet_review_status(
                        review_id,
                        status="rejected",
                        review_reason="Manually rejected after acceptance",
                        reviewed_by="Human Reviewer",
                    )
                    st.rerun()
            return

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
        rejection_cat = st.selectbox(
            "Rejection Reason",
            options=["Noise / Artifact", "Not a Signature/Stamp", "Low Quality / Illegible", "Other"],
            key=f"rej_cat_{review_id}",
            label_visibility="collapsed"
        )
        if st.button(
            "❌ Reject",
            key=f"btn_rej_{review_id}",
            use_container_width=True,
        ):
            try:
                update_snippet_review_status(
                    review_id=review_id,
                    status="rejected",
                    review_reason=f"Rejected: {rejection_cat}",
                    reviewed_by="Dashboard User",
                    rejection_category=rejection_cat
                )
                st.toast(f"❌ Rejected — {rejection_cat}", icon="🚫")
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

                            # Also add verified text as a dynamic_subtag for slash-command search
                            try:
                                existing_tags = doc_response['_source'].get('dynamic_subtags', []) or []
                            except Exception:
                                existing_tags = []
                            new_tag = typed.strip()
                            if new_tag and new_tag not in existing_tags:
                                existing_tags = existing_tags + [new_tag]

                            update_payload = {
                                "reviewed_snippets": reviewed_snippets,
                                "reviewed_content": combined_content,
                                "dynamic_subtags": existing_tags,
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
                        try:
                            siblings = get_all_reviews_for_doc(snippet["smart_id"]) or []
                        except Exception:
                            siblings = []
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


def _render_page_composition_bar_legacy(smart_id: str, page_num: int) -> None:
    """Render the visual zone breakdown for a specific page."""
    breakdown = get_page_segmentation_breakdown(smart_id)
    if not breakdown:
        return
    page_data = next((x for x in breakdown if x["page_num"] == page_num), None)
    if not page_data:
        return

    st.markdown(
        f"""
        <div style="margin-bottom: 0.5rem;">
            <div style="font-size: 0.75rem; color: #64748B; margin-bottom: 0.2rem; font-weight: 600;">
                PAGE {page_num} COMPOSITION
            </div>
            <div style="display: flex; width: 100%; height: 8px; border-radius: 4px; overflow: hidden; background: #E2E8F0;">
                <div style="width: {page_data.get('clean_text_pct', 0) * 100}%; background: #3B82F6;" title="Clean Text"></div>
                <div style="width: {page_data.get('faded_text_pct', 0) * 100}%; background: #93C5FD;" title="Faded Text"></div>
                <div style="width: {page_data.get('handwritten_pct', 0) * 100}%; background: #F59E0B;" title="Handwritten"></div>
                <div style="width: {page_data.get('stamp_pct', 0) * 100}%; background: #EF4444;" title="Stamp/Logo"></div>
                <div style="width: {page_data.get('logo_pct', 0) * 100}%; background: #8B5CF6;" title="Logo"></div>
                <div style="width: {page_data.get('noise_pct', 0) * 100}%; background: #94A3B8;" title="Noise"></div>
                <div style="width: {page_data.get('whitespace_pct', 0) * 100}%; background: #FFFFFF;" title="Whitespace"></div>
            </div>
            <div style="display: flex; gap: 0.5rem; font-size: 0.65rem; color: #64748B; margin-top: 0.2rem; flex-wrap: wrap;">
                <div><span style="color: #3B82F6;">■</span> Text</div>
                <div><span style="color: #93C5FD;">■</span> Faded</div>
                <div><span style="color: #F59E0B;">■</span> Hand</div>
                <div><span style="color: #EF4444;">■</span> Stamp</div>
                <div><span style="color: #8B5CF6;">■</span> Logo</div>
                <div><span style="color: #94A3B8;">■</span> Noise</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True
    )


def _render_suppression_audit_log(smart_id: str) -> None:
    """Render the log of auto-suppressed snippets."""
    suppressions = get_snippet_suppressions(smart_id)
    if not suppressions:
        return
        
    st.markdown("#### 🛡️ Auto-Suppressed Elements")
    st.caption("Elements hidden from manual review by machine heuristics.")
    
    with st.expander(f"View {len(suppressions)} suppressed elements"):
        for sup in suppressions:
            st.markdown(
                f"""
                <div style="font-size: 0.8rem; padding: 0.4rem; border-bottom: 1px solid #E2E8F0;">
                    <span style="font-weight: 600; color: #1E293B;">Page {sup.get('page_num', '?')}</span> | 
                    <span style="color: #64748B;">Type: {sup.get('snippet_type', 'unknown')}</span> | 
                    <span style="color: #EF4444;">Suppressed By: {sup.get('suppressed_by', 'heuristic')}</span>
                </div>
                """,
                unsafe_allow_html=True
            )

def render_snippet_review_tab(config: Any) -> None:
    """Render the production-grade HITL Visual Verification Portal."""

    try:
        _render_snippet_review_tab_inner(config)
    except Exception as e:
        st.error(f"Snippet Review tab encountered an error: {e}")
        st.caption("Try refreshing the page. If the issue persists, check that the audit database is accessible.")
        import traceback
        with st.expander("Error details"):
            st.code(traceback.format_exc())


def _render_snippet_review_tab_inner(config: Any) -> None:
    """Inner implementation of the snippet review tab."""

    # ── Page Header (render immediately so page is never blank) ──
    st.markdown(
        """
        <style>
        div[data-testid="stVerticalBlockBorderWrapper"]:not(:has(div[data-testid="stVerticalBlockBorderWrapper"])):has(div.snippet-card-anchor) {
            border: 1px solid #E2E8F0 !important;
            border-radius: 14px !important;
            background: #FFFFFF !important;
            box-shadow: 0 4px 10px rgba(0, 0, 0, 0.03) !important;
            transition: all 0.25s ease !important;
            padding: 1rem !important;
            margin-bottom: 0.5rem !important;
        }
        div[data-testid="stVerticalBlockBorderWrapper"]:not(:has(div[data-testid="stVerticalBlockBorderWrapper"])):has(div.snippet-card-anchor):hover {
            transform: translateY(-2px) !important;
            box-shadow: 0 10px 22px rgba(0, 0, 0, 0.06) !important;
        }
        div[data-testid="stVerticalBlockBorderWrapper"]:not(:has(div[data-testid="stVerticalBlockBorderWrapper"])):has(div.snippet-card-anchor.accepted) {
            border: 1px solid #A7F3D0 !important;
            opacity: 0.7 !important;
        }
        div[data-testid="stVerticalBlockBorderWrapper"]:not(:has(div[data-testid="stVerticalBlockBorderWrapper"])):has(div.snippet-card-anchor.rejected) {
            border: 1px solid #FECACA !important;
            opacity: 0.7 !important;
        }
        </style>
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

    # Reset pagination when entering from a different view to avoid stale offsets
    if st.session_state.get("_review_tab_loaded") is not True:
        st.session_state["_review_tab_loaded"] = True
        st.session_state.pop("snippet_page_offset", None)

    if "visual_memory" not in st.session_state:
        try:
            st.session_state.visual_memory = VisualMemoryEngine()
        except Exception:
            st.session_state.visual_memory = None

    # ── Fetch documents with reviews ──
    try:
        docs = get_docs_with_reviews()
    except Exception as _db_err:
        st.error(f"Failed to load review documents: {_db_err}")
        return
    if not docs:
        st.info("📭 No documents with visual review elements found. Process documents first to generate snippet reviews.")
        return

    # ── Document Selector ──
    st.markdown("---")
    doc_options = {}
    smart_id_to_label = {}

    # Separate parent files from embedded children for hierarchical display.
    parent_docs = []
    embedded_docs = []  # (doc, parent_hash)
    for doc in docs:
        fpath = (doc.get("file_path") or "").replace("\\", "/")
        if "/data/embedded/" in fpath:
            parts = fpath.split("/data/embedded/")
            parent_hash = parts[1].split("/")[0] if len(parts) > 1 else ""
            embedded_docs.append((doc, parent_hash))
        else:
            parent_docs.append(doc)

    # Resolve parent_hash → parent file_name via Redis parent_info for matching
    _parent_name_by_hash: Dict[str, str] = {}
    if embedded_docs:
        try:
            from core.queue_manager import get_queue_manager
            _qm = get_queue_manager()
            _r = getattr(_qm, "client", None)
            if _r:
                unique_hashes = {ph for _, ph in embedded_docs if ph}
                for ph in unique_hashes:
                    raw = _r.hget("docsearch:parent_info", ph)
                    if raw:
                        info = json.loads(raw)
                        _parent_name_by_hash[ph] = info.get("file_name", "")
        except Exception:
            pass

    # Map parent file_name → parent doc for child grouping
    parent_by_name: Dict[str, Dict[str, Any]] = {}
    for pdoc in parent_docs:
        parent_by_name[pdoc.get("file_name", "")] = pdoc

    # Build ordered option list: parent first, then its children indented
    def _doc_label(doc: Dict[str, Any], indent: bool = False) -> str:
        fname = doc.get("file_name", "Unknown")
        pending = doc.get("pending_count", 0)
        prefix = "🔴 " if pending > 0 else "🟢 "
        stats = f"{pending} pending | {doc.get('accepted_count', 0)} accepted | {doc.get('rejected_count', 0)} rejected"
        if indent:
            return f"    ↳ {prefix}{fname}  —  {stats}"
        return f"{prefix}{fname}  —  {stats}"

    # Group embedded docs by their parent's file_name
    from collections import defaultdict
    embedded_by_parent_name: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    orphan_embedded: List[Dict[str, Any]] = []
    for edoc, phash in embedded_docs:
        parent_name = _parent_name_by_hash.get(phash, "")
        if parent_name and parent_name in parent_by_name:
            embedded_by_parent_name[parent_name].append(edoc)
        else:
            orphan_embedded.append(edoc)

    for pdoc in parent_docs:
        label = _doc_label(pdoc)
        doc_options[label] = pdoc
        smart_id_to_label[pdoc.get("smart_id", "")] = label
        # Append embedded children right after their parent
        pname = pdoc.get("file_name", "")
        for edoc in embedded_by_parent_name.get(pname, []):
            elabel = _doc_label(edoc, indent=True)
            doc_options[elabel] = edoc
            smart_id_to_label[edoc.get("smart_id", "")] = elabel

    # Orphan embedded docs whose parent isn't in the review list
    for edoc in orphan_embedded:
        elabel = _doc_label(edoc, indent=True)
        doc_options[elabel] = edoc
        smart_id_to_label[edoc.get("smart_id", "")] = elabel

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
        try:
            all_snippets = get_all_reviews_for_doc(selected_smart_id)
        except Exception as _db_err:
            st.error(f"Failed to load snippets for this document: {_db_err}")
            return
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
                [
                    "All Roles",
                    "Contract Auditor",
                    "Compliance Officer",
                    "Brand Integrity Reviewer",
                    "Marketing Reviewer",
                    "Transcription Auditor",
                    "Faded Text Specialist",
                    "Text Specialist",
                    "Quality Control",
                    "Operations Manager",
                    "Document Reviewer",
                    "Finance Auditor",
                    "Legal Counsel",
                    "Records Manager",
                ],
                index=0,
                key="snippet_role_filter",
                help="Contract Auditor: Signatures • Compliance Officer: Stamps & Seals • Brand Integrity Reviewer: Logos • Transcription Auditor: Handwritten • Text Specialist: OCR anomalies • Quality Control: Noise • Document Reviewer: General/Full Page",
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

        # All snippets shown for human review — no auto-suppression

        # Sort: pending first (urgent), then by page number for readability
        def sort_priority(s):
            status_order = {"pending": 0, "accepted": 1, "rejected": 2}
            return (status_order.get(s.get("status"), 3), s.get("page_num", 0))
        filtered = sorted(filtered, key=sort_priority)


        view_tabs = st.tabs(["Waterfall Analysis", "Page Composition"])
        with view_tabs[0]:
            st.markdown("<div style='margin:0.35rem 0 0.5rem 0;'><b>Accuracy Impact Waterfall</b></div>", unsafe_allow_html=True)
            _render_accuracy_waterfall_chart(all_snippets, selected_doc)
        with view_tabs[1]:
            _render_page_composition_bar(selected_doc)

        if not filtered:
            st.info(f"No snippets matching filters: Status={status_filter}, Role={role_filter} ({len(all_snippets)} total available)")
        else:
            st.markdown(
                f"<p style='font-size:0.82rem; color:#64748B; margin:0.5rem 0;'>"
                f"Showing <b>{len(filtered)}</b> of <b>{len(all_snippets)}</b> elements</p>",
                unsafe_allow_html=True,
            )

            # ── Render snippet cards in compact 4-column tile layout with lazy pagination ──
            tiles_per_row = 4
            snippets_per_page = 20  # Load 20 snippets per page (5 rows of 4)
            
            # Initialize pagination state
            if "snippet_page_offset" not in st.session_state:
                st.session_state["snippet_page_offset"] = 0
            
            total_pages = (len(filtered) + snippets_per_page - 1) // snippets_per_page
            current_page = st.session_state["snippet_page_offset"]
            
            # Show current page info and navigation
            nav_col1, nav_col2, nav_col3 = st.columns([1, 3, 1])
            with nav_col1:
                if current_page > 0 and st.button("◀ Prev", key="prev_page"):
                    st.session_state["snippet_page_offset"] = max(0, current_page - 1)
                    st.rerun()
            with nav_col2:
                st.caption(f"Page {current_page + 1} of {total_pages} | Total: {len(filtered)} snippets")
            with nav_col3:
                if current_page < total_pages - 1 and st.button("Next ▶", key="next_page"):
                    st.session_state["snippet_page_offset"] = min(total_pages - 1, current_page + 1)
                    st.rerun()
            
            # Calculate start/end indices for this page
            start_idx = current_page * snippets_per_page
            end_idx = min(start_idx + snippets_per_page, len(filtered))
            page_snippets = filtered[start_idx:end_idx]
            
            # Render only the current page of snippets
            for row_start in range(0, len(page_snippets), tiles_per_row):
                cols = st.columns(tiles_per_row)
                for col_idx in range(tiles_per_row):
                    snippet_idx = row_start + col_idx
                    if snippet_idx >= len(page_snippets):
                        continue
                    with cols[col_idx]:
                        _render_snippet_card(page_snippets[snippet_idx], working_root, snippet_idx, compact=True)
            
            # Show pagination controls at bottom as well
            st.markdown("---")
            bot_col1, bot_col2, bot_col3 = st.columns([1, 3, 1])
            with bot_col1:
                if current_page > 0 and st.button("◀ Previous", key="prev_page_bottom"):
                    st.session_state["snippet_page_offset"] = max(0, current_page - 1)
                    st.rerun()
            with bot_col2:
                st.caption(f"Showing snippets {start_idx + 1}–{end_idx} of {len(filtered)}")
            with bot_col3:
                if current_page < total_pages - 1 and st.button("Next ▶", key="next_page_bottom"):
                    st.session_state["snippet_page_offset"] = min(total_pages - 1, current_page + 1)
                    st.rerun()

            _render_suppression_audit_log(selected_smart_id)

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
