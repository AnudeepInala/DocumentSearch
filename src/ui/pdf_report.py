"""
PDF Report Generator — per-document and system-wide reports using reportlab.
"""
from __future__ import annotations

import io
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from xml.sax.saxutils import escape

# ---------------------------------------------------------------------------
# reportlab imports (installed via: pip install reportlab)
# ---------------------------------------------------------------------------
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm
from reportlab.platypus import (
    HRFlowable,
    Image as RLImage,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)


# ---------------------------------------------------------------------------
# Colour palette
# ---------------------------------------------------------------------------
_PRIMARY = colors.HexColor("#1E3A5F")
_ACCENT  = colors.HexColor("#2E86AB")
_LIGHT   = colors.HexColor("#F0F4F8")
_SUCCESS = colors.HexColor("#2E7D32")
_WARN    = colors.HexColor("#F57F17")
_ERROR   = colors.HexColor("#C62828")


def _base_styles():
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(
        name="ReportTitle",
        fontSize=18, leading=22,
        textColor=_PRIMARY, alignment=TA_CENTER,
        spaceAfter=6,
    ))
    styles.add(ParagraphStyle(
        name="SectionHead",
        fontSize=12, leading=16,
        textColor=_PRIMARY, spaceAfter=4, spaceBefore=10,
        fontName="Helvetica-Bold",
    ))
    styles.add(ParagraphStyle(
        name="SubHead",
        fontSize=10, leading=14,
        textColor=_ACCENT, spaceAfter=2, spaceBefore=6,
        fontName="Helvetica-Bold",
    ))
    styles.add(ParagraphStyle(
        name="Body",
        fontSize=9, leading=13,
        textColor=colors.black, spaceAfter=2,
    ))
    styles.add(ParagraphStyle(
        name="SmallGray",
        fontSize=8, leading=11,
        textColor=colors.gray,
    ))
    return styles


def _header(styles, title: str, subtitle: str = "") -> list:
    elems = []
    elems.append(Paragraph(title, styles["ReportTitle"]))
    if subtitle:
        elems.append(Paragraph(subtitle, styles["SmallGray"]))
    elems.append(HRFlowable(width="100%", thickness=1.5, color=_ACCENT, spaceAfter=8))
    return elems


def _kv_table(rows: list[tuple[str, Any]], col_widths=(6 * cm, 11 * cm)) -> Table:
    """Two-column key-value table with word-wrap support for long values."""
    wrap_style = ParagraphStyle(
        "TableCell", fontSize=8, leading=10, textColor=colors.black,
    )
    header_style = ParagraphStyle(
        "TableHeader", fontSize=9, leading=11, textColor=colors.white,
        fontName="Helvetica-Bold",
    )
    data = [
        [Paragraph("Field", header_style), Paragraph("Value", header_style)]
    ] + [
        [Paragraph(escape(str(k)), wrap_style), Paragraph(escape(str(v)), wrap_style)]
        for k, v in rows
    ]
    t = Table(data, colWidths=col_widths)
    t.setStyle(TableStyle([
        ("BACKGROUND",   (0, 0), (-1, 0),  _PRIMARY),
        ("TEXTCOLOR",    (0, 0), (-1, 0),  colors.white),
        ("FONTNAME",     (0, 0), (-1, 0),  "Helvetica-Bold"),
        ("FONTSIZE",     (0, 0), (-1, 0),  9),
        ("FONTSIZE",     (0, 1), (-1, -1), 8),
        ("BACKGROUND",   (0, 1), (-1, -1), _LIGHT),
        ("ROWBACKGROUNDS",(0, 1), (-1, -1), [_LIGHT, colors.white]),
        ("GRID",         (0, 0), (-1, -1), 0.25, colors.lightgrey),
        ("VALIGN",       (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING",  (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING",   (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING",(0, 0), (-1, -1), 4),
    ]))
    return t


def _conf_badge(conf: float) -> str:
    """Return a coloured text label for OCR confidence."""
    if conf >= 90:
        return f"<font color='#2E7D32'>{conf:.1f}%  ✓ Excellent</font>"
    if conf >= 70:
        return f"<font color='#1565C0'>{conf:.1f}%  Good</font>"
    if conf >= 50:
        return f"<font color='#F57F17'>{conf:.1f}%  Fair</font>"
    return f"<font color='#C62828'>{conf:.1f}%  ⚠ Poor</font>"


def _snippet_type_label(t: str) -> str:
    return {
        "signature": "Signature",
        "stamp": "Stamp",
        "logo": "Logo / Image",
        "text_anomaly": "Text Anomaly",
        "full_page": "Full Page",
    }.get(t, t.replace("_", " ").title())


def _status_color(status: str) -> str:
    return {"accepted": "#2E7D32", "rejected": "#C62828", "pending": "#F57F17"}.get(
        status.lower(), "#555555"
    )


def _resolve_snippet_image(path_str: str) -> Optional[Path]:
    """Return a resolved Path for the snippet image, or None if not found."""
    if not path_str:
        return None
    p = Path(path_str)
    if p.exists():
        return p
    # Try mapping legacy/different-drive paths to known locations
    normalized = str(p).replace("\\", "/")
    for marker in ("/data/review_snippets/", "/runtime/review_snippets/"):
        if marker in normalized:
            rel = normalized.split(marker, 1)[1]
            for root in (
                Path("data/review_snippets"),
                Path("runtime/review_snippets"),
                Path("E:/DocSearch_v1/data/review_snippets"),
                Path("E:/DocSearch_v1/runtime/review_snippets"),
                Path("E:/DocumentSearch/data/review_snippets"),
                Path("E:/DocumentSearch/runtime/review_snippets"),
            ):
                candidate = root / rel
                if candidate.exists():
                    return candidate
    return None


def _append_text_block(elems: List[Any], styles, title: str, text: str) -> None:
    """Append large text content to the PDF, preserving page boundaries.

    OCR content typically contains page markers like "--- Page N ---" or
    form-feed characters.  Each page's text starts on a new line in the PDF.
    """
    if not text or not text.strip():
        return
    import re as _re
    elems.append(Paragraph(title, styles["SubHead"]))

    # Split on page boundary markers (e.g. "--- Page 2 ---", "\f", or "\n\n\n+")
    page_pattern = _re.compile(
        r'(?:\r?\n)?-{2,}\s*[Pp]age\s*\d+\s*-{2,}(?:\r?\n)?'
        r'|\f'
        r'|\r?\n{3,}'
    )
    pages = page_pattern.split(str(text))

    chunk_size = 1600
    page_num = 0
    for page_text in pages:
        page_text = page_text.strip()
        if not page_text:
            continue
        page_num += 1
        if page_num > 1:
            elems.append(Spacer(1, 0.15 * cm))
        elems.append(Paragraph(
            f"<b>— Page {page_num} —</b>", styles["SmallGray"]
        ))
        cleaned = " ".join(page_text.split())
        for i in range(0, len(cleaned), chunk_size):
            chunk = cleaned[i:i + chunk_size]
            elems.append(Paragraph(escape(chunk), styles["Body"]))
    elems.append(Spacer(1, 0.2 * cm))


def _make_accuracy_chart(snippets: List[Dict], smart_id: str = "") -> Optional[bytes]:
    """Render the same page-composition waterfall chart as the snippet review tab.

    This mirrors the SVG chart rendered by ``_render_accuracy_waterfall_chart`` in
    ``review_tab.py`` — using matplotlib so it can be embedded in the PDF.
    """
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        display_snippets = [s for s in snippets if s.get("snippet_type") != "full_page"]
        if not display_snippets:
            return None

        # --- Fetch page composition breakdown from audit DB (same as review tab) ---
        avg_clean = 0.0
        avg_whitespace = 0.0
        avg_faded = 0.0
        avg_logo = 0.0
        avg_stamp = 0.0
        avg_handwritten = 0.0
        avg_noise = 0.0

        try:
            from core.reporting_manager import get_page_segmentation_breakdown
            breakdown = get_page_segmentation_breakdown(smart_id) if smart_id else []
            if breakdown:
                num_pages = len(breakdown)
                avg_clean = sum(float(p.get("clean_text_pct") or 0.0) for p in breakdown) / num_pages
                avg_whitespace = sum(float(p.get("whitespace_pct") or 0.0) for p in breakdown) / num_pages
                avg_faded = sum(float(p.get("faded_text_pct") or 0.0) for p in breakdown) / num_pages
                avg_logo = sum(float(p.get("logo_pct") or 0.0) for p in breakdown) / num_pages
                avg_stamp = sum(float(p.get("stamp_pct") or 0.0) for p in breakdown) / num_pages
                avg_handwritten = sum(float(p.get("handwritten_pct") or 0.0) for p in breakdown) / num_pages
                avg_noise = sum(float(p.get("noise_pct") or 0.0) for p in breakdown) / num_pages
        except Exception:
            pass

        if avg_clean == 0.0 and avg_whitespace == 0.0:
            avg_clean = 70.0
            avg_whitespace = 30.0

        # --- Calculate snippet impacts (same logic as review tab) ---
        pending_counts: Dict[str, int] = {}
        pending_impacts: Dict[str, float] = {}
        accepted_impacts_by_type: Dict[str, float] = {}
        accepted_impact_total = 0.0
        doc_snippet_types: set = set()

        for s in snippets:
            t = str(s.get("snippet_type") or "").strip().lower()
            doc_snippet_types.add(t)
            status = s.get("status", "pending")
            impact = max(0.0, float(s.get("accuracy_impact") or 0.0))
            if status == "pending":
                pending_counts[t] = pending_counts.get(t, 0) + 1
                pending_impacts[t] = pending_impacts.get(t, 0.0) + impact
            elif status == "accepted":
                accepted_impacts_by_type[t] = accepted_impacts_by_type.get(t, 0.0) + impact
                accepted_impact_total += impact

        segment_cfg = {
            "clean": {"label": "Extractable Text", "value": avg_clean, "color": "#10B981"},
            "whitespace": {"label": "Whitespace", "value": avg_whitespace, "color": "#E2E8F0"},
            "faded": {"label": "Faded Text", "value": avg_faded, "color": "#3B82F6", "snippet_key": "faded_text"},
            "logo": {"label": "Logo/Image", "value": avg_logo, "color": "#8B5CF6", "snippet_key": "logo"},
            "stamp": {"label": "Stamp", "value": avg_stamp, "color": "#F59E0B", "snippet_key": "stamp"},
            "handwritten": {"label": "Handwritten", "value": avg_handwritten, "color": "#EC4899",
                            "snippet_key": "handwritten", "extra_snippet_key": "signature"},
            "noise": {"label": "Noise", "value": avg_noise, "color": "#64748B", "snippet_key": "noise",
                      "extra_snippet_key": "text_anomaly"},
        }

        segment_cfg["clean"]["value"] += accepted_impact_total

        for key, cfg in segment_cfg.items():
            if key in ("clean", "whitespace"):
                continue

            accepted_type_impact = 0.0
            pending_type_impact = 0.0
            if "snippet_key" in cfg:
                sk = cfg["snippet_key"]
                accepted_type_impact += accepted_impacts_by_type.get(sk, 0.0)
                pending_type_impact += pending_impacts.get(sk, 0.0)
            if "extra_snippet_key" in cfg:
                esk = cfg["extra_snippet_key"]
                accepted_type_impact += accepted_impacts_by_type.get(esk, 0.0)
                pending_type_impact += pending_impacts.get(esk, 0.0)

            # Subtract resolved (accepted) impacts from baseline, but ensure it is at least the remaining pending (unresolved) impact
            cfg["value"] = max(cfg["value"] - accepted_type_impact, pending_type_impact)
            cfg["value"] = max(0.0, cfg["value"])

        total_val_sum = sum(cfg["value"] for cfg in segment_cfg.values())
        if total_val_sum > 0:
            scale = 100.0 / total_val_sum
            for cfg in segment_cfg.values():
                cfg["value"] *= scale
        else:
            segment_cfg["whitespace"]["value"] = 100.0

        # --- Build bars (stacked waterfall, same order as review tab) ---
        order = ["clean", "whitespace", "faded", "logo", "stamp", "handwritten", "noise"]
        bars = []
        running_bottom = 0.0

        for key in order:
            cfg = segment_cfg[key]
            val = cfg["value"]
            if val <= 0.001:
                continue
            lbl = cfg["label"]
            count = 0
            if "snippet_key" in cfg:
                count += pending_counts.get(cfg["snippet_key"], 0)
            if "extra_snippet_key" in cfg:
                count += pending_counts.get(cfg["extra_snippet_key"], 0)
            if count > 0:
                lbl = f"{lbl} ({count})"
            bars.append({
                "label": lbl,
                "value": val,
                "bottom": running_bottom,
                "color": cfg["color"],
                "text": f"{val:.1f}%",
                "is_total": False,
            })
            running_bottom = min(100.0, running_bottom + val)

        bars.append({
            "label": "Total",
            "value": 100.0,
            "bottom": 0.0,
            "color": "#2563EB",
            "text": "100%",
            "is_total": True,
        })

        # --- Render with matplotlib ---
        fig, ax = plt.subplots(figsize=(7, 3.5), dpi=140)
        x_positions = list(range(len(bars)))

        for i, b in enumerate(bars):
            edge = b["color"]
            lw = 1.5 if b["is_total"] else 0.5
            ls = "--" if b["is_total"] else "-"
            fill = "#DBEAFE" if b["is_total"] else b["color"]
            ax.bar(
                i, b["value"], bottom=b["bottom"],
                color=fill, edgecolor=edge, linewidth=lw, linestyle=ls, width=0.55,
            )
            ax.text(
                i, b["bottom"] + b["value"] + 1.2,
                b["text"], ha="center", va="bottom", fontsize=8, fontweight="bold",
            )

        ax.set_xticks(x_positions)
        ax.set_xticklabels([b["label"] for b in bars], fontsize=7.5)
        ax.set_ylim(0, 110)
        ax.set_ylabel("Composition (%)", fontsize=9)
        ax.set_title("Accuracy Impact Waterfall — Page Composition",
                     fontsize=10, fontweight="bold", color="#1E3A5F", pad=8)
        ax.tick_params(labelsize=7.5)
        ax.spines["top"].set_visible(False)
        ax.spines["right"].set_visible(False)
        ax.axhline(y=100, color="#2563EB", linewidth=0.8, linestyle="--", alpha=0.5)
        fig.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format="png", bbox_inches="tight", dpi=140)
        plt.close(fig)
        buf.seek(0)
        return buf.read()
    except Exception:
        return None


# ===========================================================================
# Public API
# ===========================================================================

def generate_document_report(result: Dict[str, Any]) -> bytes:
    """Generate a per-document PDF report.

    Args:
        result: A document dict as returned by the OpenSearch search results.

    Returns:
        Raw PDF bytes suitable for ``st.download_button(data=...)``.
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=2 * cm, rightMargin=2 * cm,
        topMargin=2 * cm, bottomMargin=2 * cm,
    )
    styles = _base_styles()
    elems = []

    # ------------------------------------------------------------------
    # Derive / normalise fields from whatever the result dict provides
    # ------------------------------------------------------------------
    _DASH = "–"   # em-dash placeholder when a value is genuinely unavailable

    file_path_full = result.get("file_path") or result.get("filepath") or ""
    filename = result.get("filename") or result.get("file_name") or (Path(file_path_full).name if file_path_full else "Unknown")
    if not file_path_full:
        file_path_full = filename
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    metadata  = result.get("metadata") or {}

    # File type: try every known source before giving up
    _ft = (
        result.get("file_type")
        or metadata.get("file_type")
        or metadata.get("mime_type", "").split("/")[-1]
        or Path(filename).suffix.lstrip(".")
    )
    file_type = _ft.upper() if _ft else _DASH

    # File size: prefer explicit field, fall back to reading the file from disk
    size_bytes = int(result.get("file_size") or 0)
    if not size_bytes and filename and filename != "Unknown":
        try:
            size_bytes = Path(filename).stat().st_size
        except Exception:
            pass
    if size_bytes >= 1_048_576:
        size_str = f"{size_bytes / 1_048_576:.2f} MB"
    elif size_bytes >= 1024:
        size_str = f"{size_bytes / 1024:.1f} KB"
    elif size_bytes > 0:
        size_str = f"{size_bytes} B"
    else:
        size_str = _DASH

    doc_id     = result.get("id") or result.get("file_hash") or result.get("doc_id") or _DASH
    indexed_at = result.get("indexed_at") or _DASH

    # Text content — use OCR if available, fall back to main_content
    raw_text = (
        str(result.get("ocr_content") or "").strip()
        or str(result.get("main_content") or "").strip()
        or str(result.get("content") or result.get("extracted_text") or "").strip()
    )

    # Derived metrics
    word_count = len(raw_text.split()) if raw_text else 0
    char_count = len(raw_text) if raw_text else 0

    # Page count: try multiple keys
    page_count_raw = (
        metadata.get("page_count")
        or result.get("page_count")
        or metadata.get("pages")
        or result.get("pages")
    )
    page_count = str(int(page_count_raw)) if page_count_raw is not None else _DASH

    conf       = float(result.get("ocr_confidence") or 0.0)
    _proc_ms   = result.get("extraction_time_ms") or result.get("processing_time_ms")
    proc_ms    = str(int(_proc_ms)) if _proc_ms is not None else _DASH

    # Language: try metadata, then result fields, then auto-detect from text
    lang = (
        metadata.get("language")
        or result.get("language")
        or result.get("lang")
    )
    if not lang and raw_text:
        # Cheap heuristic: check for ASCII dominance → "en", else "Unknown"
        ascii_chars = sum(1 for c in raw_text[:200] if ord(c) < 128)
        lang = "en" if ascii_chars / max(len(raw_text[:200]), 1) > 0.85 else "Multilingual"
    lang = lang or _DASH

    ext_method = (
        "PaddleOCR"
        if (result.get("ocr_completed")
            or bool(result.get("ocr_content"))
            or bool(result.get("has_ocr_content"))
            or (result.get("ocr_confidence") or 0) > 0)
        else result.get("extraction_method")
            or metadata.get("extraction_method")
            or "Apache Tika"
    )

    # Visual flags from metadata
    flags = []
    if metadata.get("has_signature"):   flags.append("Signature")
    if metadata.get("has_stamp"):       flags.append("Stamp")
    if metadata.get("has_logo"):        flags.append("Logo")
    if metadata.get("has_handwritten"): flags.append("Handwritten")

    # ------------------------------------------------------------------
    elems += _header(styles, "RADAR Document Analysis Report", f"Generated: {generated_at}")

    # ---- Document Metadata ----
    elems.append(Paragraph("Document Metadata", styles["SectionHead"]))
    meta_rows = [
        ("Filename",   Path(filename).name),
        ("Full Path",  file_path_full),
        ("File Type",  file_type),
        ("File Size",  size_str),
        ("Document ID", doc_id),
        ("Indexed At", indexed_at),
    ]
    elems.append(_kv_table(meta_rows))
    elems.append(Spacer(1, 0.4 * cm))

    # ---- OCR Metrics ----
    is_ocr = bool(result.get("ocr_completed"))
    elems.append(Paragraph("RADAR Engine Metrics", styles["SectionHead"]))
    metrics_rows = [
        ("Extraction Method",        "RADAR Engine"),
        ("Word Count",           f"{word_count:,}" if word_count else _DASH),
        ("Character Count",      f"{char_count:,}" if char_count else _DASH),
        ("Page Count",           page_count),
        ("Processing Time (ms)", proc_ms),
        ("Language Detected",    lang),
        ("Visual Flags",         ", ".join(flags) if flags else "None"),
    ]
    if is_ocr:
        metrics_rows.insert(2, ("OCR Confidence", f"{conf:.2f}%"))
        elems.append(Paragraph(
            f"OCR Confidence: {_conf_badge(conf)}",
            ParagraphStyle("ConfBadge", parent=styles["Body"], fontSize=10, spaceAfter=6),
        ))
    elems.append(_kv_table(metrics_rows))
    elems.append(Spacer(1, 0.4 * cm))

    # ---- Visual Snippet Review ----
    smart_id = result.get("smart_id") or result.get("id") or ""
    if smart_id:
        try:
            from core.reporting_manager import get_all_reviews_for_doc
            snippets = get_all_reviews_for_doc(smart_id)
            display_snippets = [s for s in snippets
                                if s.get("snippet_type") != "full_page"]
            if display_snippets:
                pending   = [s for s in display_snippets if s.get("status") == "pending"]
                accepted  = [s for s in display_snippets if s.get("status") == "accepted"]
                rejected  = [s for s in display_snippets if s.get("status") == "rejected"]
                total_impact   = sum(float(s.get("accuracy_impact") or 0) for s in pending)
                current_acc    = max(0.0, 100.0 - total_impact)

                elems.append(Paragraph("Visual Snippet Review", styles["SectionHead"]))
                summary_rows = [
                    ("Total Snippets",   str(len(display_snippets))),
                    ("Pending Review",   str(len(pending))),
                    ("Accepted",         str(len(accepted))),
                    ("Rejected",         str(len(rejected))),
                    ("Current Accuracy", f"{current_acc:.2f}%"),
                    ("Pending Impact",   f"{total_impact:.2f}%"),
                ]
                elems.append(_kv_table(summary_rows))
                elems.append(Spacer(1, 0.3 * cm))

                # Accuracy waterfall chart (mirrors snippet review tab)
                # Pass all snippets so accepted impacts are counted correctly
                chart_bytes = _make_accuracy_chart(snippets, smart_id=smart_id)
                if chart_bytes:
                    chart_img = RLImage(io.BytesIO(chart_bytes), width=14 * cm, height=7 * cm)
                    elems.append(chart_img)
                    elems.append(Spacer(1, 0.4 * cm))
                else:
                    elems.append(Paragraph(
                        "Bar chart is unavailable because there are no pending snippet impacts to plot.",
                        styles["Body"],
                    ))
                    elems.append(Spacer(1, 0.2 * cm))

                # Per-snippet cards (cap at 30)
                elems.append(Paragraph("Snippets", styles["SubHead"]))
                for s in display_snippets[:30]:
                    stype   = _snippet_type_label(s.get("snippet_type", ""))
                    status  = (s.get("status") or "pending").title()
                    impact  = float(s.get("accuracy_impact") or 0)
                    page    = s.get("page_num", "?")
                    excerpt = (
                        str(s.get("extracted_text") or s.get("transcription_text") or "")[:120]
                    )
                    stat_color = _status_color(s.get("status", "pending"))

                    detail_rows = [
                        ("Type",   stype),
                        ("Status", f'<font color="{stat_color}">{status}</font>'),
                        ("Page",   str(page)),
                        ("Impact", f"{impact:.2f}%"),
                    ]
                    if excerpt:
                        detail_rows.append(("Text", excerpt))

                    detail_table = _kv_table(detail_rows, col_widths=(3 * cm, 5.5 * cm))

                    img_path = _resolve_snippet_image(s.get("snippet_path", ""))
                    if img_path:
                        try:
                            from PIL import Image as _PILImage
                            with _PILImage.open(str(img_path)) as _pimg:
                                iw, ih = _pimg.size
                            max_w = 8.0 * cm
                            scale = max_w / max(iw, 1)
                            disp_w = max_w
                            disp_h = min(float(ih) * scale, 5.0 * cm)
                            snippet_img = RLImage(str(img_path), width=disp_w, height=disp_h)
                            card = Table(
                                [[snippet_img, detail_table]],
                                colWidths=[8.5 * cm, 8.5 * cm],
                            )
                            card.setStyle(TableStyle([
                                ("VALIGN",        (0, 0), (-1, -1), "TOP"),
                                ("LEFTPADDING",   (0, 0), (-1, -1), 4),
                                ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
                                ("TOPPADDING",    (0, 0), (-1, -1), 4),
                                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                                ("BOX",           (0, 0), (-1, -1), 0.5, colors.lightgrey),
                            ]))
                            elems.append(card)
                        except Exception:
                            elems.append(detail_table)
                    else:
                        elems.append(detail_table)
                    elems.append(Spacer(1, 0.2 * cm))
            else:
                elems.append(Paragraph("Visual Snippet Review", styles["SectionHead"]))
                elems.append(Paragraph("No snippet review data is available for this document.", styles["Body"]))
                elems.append(Spacer(1, 0.2 * cm))
        except Exception:
            pass  # Never let snippet errors break the report

    # ---- Extracted Text (End Section) — Only OCR content ----
    elems.append(Paragraph("OCR Extracted Text", styles["SectionHead"]))
    added_text = False

    ocr_content = str(result.get("ocr_content") or "").strip()
    if ocr_content:
        _append_text_block(elems, styles, "OCR Scanned Text Extraction", ocr_content)
        added_text = True

    reviewed_content = str(result.get("reviewed_content") or "").strip()
    if reviewed_content:
        _append_text_block(elems, styles, "Verified Review Label", reviewed_content)
        added_text = True

    if not added_text:
        elems.append(Paragraph("No OCR text content was extracted from this document.", styles["Body"]))
        elems.append(Spacer(1, 0.2 * cm))

    # ---- Footer ----
    elems.append(HRFlowable(width="100%", thickness=0.5, color=colors.lightgrey, spaceAfter=4))
    elems.append(Paragraph(
        "This report was generated automatically by RADAR Engine.",
        styles["SmallGray"],
    ))

    doc.build(elems)
    buf.seek(0)
    return buf.read()


def generate_system_report(queue_stats: Dict[str, Any],
                            size_stats: Dict[str, Any] | None = None) -> bytes:
    """Generate an overall system health / pipeline metrics PDF report.

    Args:
        queue_stats: Dict as returned by ``get_cached_queue_stats()``.
        size_stats:  Optional dict as returned by ``get_cached_size_stats()``.

    Returns:
        Raw PDF bytes.
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=2 * cm, rightMargin=2 * cm,
        topMargin=2 * cm, bottomMargin=2 * cm,
    )
    styles = _base_styles()
    elems = []

    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    elems += _header(styles, "System Monitor Report", f"Generated: {generated_at}")

    # Helper
    def safe_int(d: dict, *keys, default: int = 0) -> int:
        for k in keys:
            v = d.get(k)
            if v is not None:
                try:
                    return int(v)
                except (TypeError, ValueError):
                    pass
        return default

    # ---- Pipeline Overview ----
    elems.append(Paragraph("Pipeline Overview", styles["SectionHead"]))
    disc  = queue_stats.get("discovery",         {}) or {}
    ext_t = queue_stats.get("extraction_total",  {}) or {}
    idx   = queue_stats.get("indexing",          {}) or {}
    ocr   = queue_stats.get("ocr",              {}) or {}
    tag   = queue_stats.get("tagging",          {}) or {}

    overview_rows = [
        ("Discovery — Pending",          safe_int(disc, "pending")),
        ("Discovery — Completed",        safe_int(disc, "completed")),
        ("RADAR Engine — Pending",       safe_int(ext_t, "pending")),
        ("RADAR Engine — Completed",     safe_int(ext_t, "completed")),
        ("Indexing — Pending",           safe_int(idx, "pending")),
        ("Indexing — Completed",         safe_int(idx, "completed")),
        ("OCR — Pending",                safe_int(ocr, "pending")),
        ("OCR — Processing",             safe_int(ocr, "processing")),
        ("OCR — Completed",              safe_int(ocr, "completed")),
        ("Tagging — Pending",            safe_int(tag, "pending")),
        ("Tagging — Completed",          safe_int(tag, "completed")),
        ("Failed Documents",             safe_int(disc, "failed")),
    ]
    elems.append(_kv_table(overview_rows))
    elems.append(Spacer(1, 0.4 * cm))

    # ---- OCR Accuracy ----
    elems.append(Paragraph("OCR Accuracy Metrics", styles["SectionHead"]))
    acc_rows = [
        ("Total OCR Jobs Completed",   safe_int(ocr, "completed")),
        ("Currently In Progress",      safe_int(ocr, "processing")),
        ("Queued / Pending",           safe_int(ocr, "pending")),
    ]
    elems.append(_kv_table(acc_rows))
    elems.append(Spacer(1, 0.4 * cm))

    # ---- RADAR Engine by Size Category ----
    ext_by_cat = queue_stats.get("extraction", {}) or {}
    if ext_by_cat:
        elems.append(Paragraph("RADAR Engine by Size Category", styles["SectionHead"]))
        cat_data = [["Category", "Pending", "Processing", "Completed", "Total"]]
        for cat, cat_stats in sorted(ext_by_cat.items()):
            if not isinstance(cat_stats, dict):
                continue
            cat_data.append([
                cat.title(),
                safe_int(cat_stats, "pending"),
                safe_int(cat_stats, "processing"),
                safe_int(cat_stats, "completed"),
                safe_int(cat_stats, "total"),
            ])
        t = Table(cat_data, colWidths=[4 * cm, 3 * cm, 3.5 * cm, 3.5 * cm, 3 * cm])
        t.setStyle(TableStyle([
            ("BACKGROUND",    (0, 0), (-1, 0),  _PRIMARY),
            ("TEXTCOLOR",     (0, 0), (-1, 0),  colors.white),
            ("FONTNAME",      (0, 0), (-1, 0),  "Helvetica-Bold"),
            ("FONTSIZE",      (0, 0), (-1, -1), 8),
            ("GRID",          (0, 0), (-1, -1), 0.25, colors.lightgrey),
            ("ROWBACKGROUNDS",(0, 1), (-1, -1), [_LIGHT, colors.white]),
            ("ALIGN",         (1, 0), (-1, -1), "RIGHT"),
            ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING",   (0, 0), (-1, -1), 5),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 5),
            ("TOPPADDING",    (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        elems.append(t)
        elems.append(Spacer(1, 0.4 * cm))

    # ---- Storage / Size Stats ----
    if size_stats:
        elems.append(Paragraph("Storage & Size Statistics", styles["SectionHead"]))
        def fmt_bytes(b):
            b = int(b or 0)
            if b >= 1_073_741_824:
                return f"{b / 1_073_741_824:.2f} GB"
            if b >= 1_048_576:
                return f"{b / 1_048_576:.2f} MB"
            if b >= 1024:
                return f"{b / 1024:.1f} KB"
            return f"{b} B"

        size_rows = [
            ("Total Indexed Size",   fmt_bytes(size_stats.get("total_size_bytes"))),
            ("Average File Size",    fmt_bytes(size_stats.get("avg_size_bytes"))),
            ("Largest File",         size_stats.get("largest_file_name") or "N/A"),
            ("Largest File Size",    fmt_bytes(size_stats.get("largest_file_size_bytes"))),
            ("Total Files Tracked",  size_stats.get("total_files") or "N/A"),
        ]
        elems.append(_kv_table(size_rows))
        elems.append(Spacer(1, 0.4 * cm))

    # ---- Active Taxonomy Tags ----
    try:
        from tagging.taxonomy_manager import TaxonomyManager
        tax_mgr = TaxonomyManager()
        snapshot = tax_mgr.ensure_loaded()

        elems.append(Paragraph("Active Taxonomy Tags", styles["SectionHead"]))
        _wrap_gray = ParagraphStyle("WrapGray", parent=styles["SmallGray"], wordWrap='CJK')
        elems.append(Paragraph(
            f"Source: {escape(snapshot.source_file)} &nbsp;|&nbsp; "
            f"Version: {escape(snapshot.version_id)}",
            _wrap_gray,
        ))
        elems.append(Spacer(1, 0.2 * cm))

        for field in ("category", "department", "purpose"):
            rows = snapshot.rows_by_field.get(field, [])
            active_rows = [r for r in rows if r.active]
            if not active_rows:
                continue

            elems.append(Paragraph(f"{field.title()} ({len(active_rows)} tags)", styles["SubHead"]))

            _cell = ParagraphStyle("TaxCell", fontSize=7, leading=9, textColor=colors.black)
            _hdr_cell = ParagraphStyle("TaxHdrCell", fontSize=7, leading=9, textColor=colors.white, fontName="Helvetica-Bold")
            tag_data = [[
                Paragraph("Label", _hdr_cell),
                Paragraph("Aliases", _hdr_cell),
                Paragraph("Keywords", _hdr_cell),
                Paragraph("Priority", _hdr_cell),
            ]]
            for row in sorted(active_rows, key=lambda r: r.priority):
                tag_data.append([
                    Paragraph(escape(row.label), _cell),
                    Paragraph(escape(", ".join(row.aliases[:5]) + ("..." if len(row.aliases) > 5 else "")), _cell),
                    Paragraph(escape(", ".join(row.keywords[:5]) + ("..." if len(row.keywords) > 5 else "")), _cell),
                    Paragraph(str(row.priority), _cell),
                ])

            t = Table(tag_data, colWidths=[3.5 * cm, 4.5 * cm, 6 * cm, 2.5 * cm])
            t.setStyle(TableStyle([
                ("BACKGROUND",    (0, 0), (-1, 0),  _PRIMARY),
                ("TEXTCOLOR",     (0, 0), (-1, 0),  colors.white),
                ("FONTNAME",      (0, 0), (-1, 0),  "Helvetica-Bold"),
                ("FONTSIZE",      (0, 0), (-1, -1), 7),
                ("GRID",          (0, 0), (-1, -1), 0.25, colors.lightgrey),
                ("ROWBACKGROUNDS",(0, 1), (-1, -1), [_LIGHT, colors.white]),
                ("VALIGN",        (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING",   (0, 0), (-1, -1), 4),
                ("RIGHTPADDING",  (0, 0), (-1, -1), 4),
                ("TOPPADDING",    (0, 0), (-1, -1), 2),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
            ]))
            elems.append(t)
            elems.append(Spacer(1, 0.3 * cm))

        # Defaults
        if snapshot.defaults:
            elems.append(Paragraph("Default Fallback Labels", styles["SubHead"]))
            default_rows = [(f.title(), v) for f, v in sorted(snapshot.defaults.items())]
            elems.append(_kv_table(default_rows, col_widths=(4 * cm, 13 * cm)))
            elems.append(Spacer(1, 0.4 * cm))

    except Exception:
        pass

    # ---- Footer ----
    elems.append(HRFlowable(width="100%", thickness=0.5, color=colors.lightgrey, spaceAfter=4))
    elems.append(Paragraph(
        "This report was generated automatically by RADAR Engine System Monitor.",
        styles["SmallGray"],
    ))

    doc.build(elems)
    buf.seek(0)
    return buf.read()
