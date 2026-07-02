"""
Backfill accuracy metrics for all documents that already exist in file_state.

This script:
1. Reads all file_key + file_path from file_state where extraction_accuracy IS NULL
2. For each file, runs AccuracyAnalyzer.analyze()
3. Updates the 10 accuracy columns in file_state

Usage:
    cd src
    python -m extraction.backfill_accuracy
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

# Ensure src is on path
src_dir = Path(__file__).resolve().parent.parent
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

from core.logging_manager import get_logger
from core.config_manager import get_config
from core.reporting_manager import update_accuracy_metrics

logger = get_logger("extraction.backfill_accuracy")


def main():
    """Backfill accuracy metrics for existing documents."""
    # Import here to avoid circular imports during module init
    from extraction.accuracy_analyzer import AccuracyAnalyzer

    cfg = get_config()
    db_path = Path(cfg.paths.working_root) / "audit" / "audit.db"

    if not db_path.exists():
        print(f"Database not found: {db_path}")
        return

    # Ensure database schema is up-to-date with new accuracy columns
    from core.reporting_manager import _get_manager
    _get_manager()._ensure_schema()

    # Initialize analyzer
    print("Initializing AccuracyAnalyzer...")
    analyzer = AccuracyAnalyzer(enable_yolo=True, enable_doctr=True)
    print(f"Active tier: {analyzer.tier}")

    # Get all documents needing accuracy analysis
    conn = sqlite3.connect(str(db_path), timeout=20)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT file_key, file_path, file_type, file_name
        FROM file_state
        WHERE extraction_accuracy IS NULL OR extraction_accuracy = 0.0
        ORDER BY updated_at DESC
    """).fetchall()
    conn.close()

    total = len(rows)
    if total == 0:
        print("All documents already have accuracy metrics!")
        return

    print(f"Found {total} documents needing accuracy backfill")
    start_time = time.time()
    success = 0
    skipped = 0
    errors = 0

    for i, row in enumerate(rows, 1):
        file_key = row["file_key"]
        file_path = row["file_path"]
        file_type = row["file_type"] or ""
        file_name = row["file_name"] or ""

        if not file_path or not os.path.isfile(file_path):
            skipped += 1
            if i % 50 == 0 or i == total:
                elapsed = time.time() - start_time
                rate = i / max(elapsed, 0.1)
                print(f"  [{i}/{total}] skip (file missing): {file_name} | Rate: {rate:.1f}/s")
            continue

        try:
            # Read file content via simple text extraction for analysis
            ext = Path(file_path).suffix.lower()

            # For text-based formats, we need the extracted text
            # Since we don't have the Tika response, we'll read directly
            extracted_text = ""
            try:
                if ext in {".txt", ".csv", ".md", ".json", ".xml", ".html"}:
                    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                        extracted_text = f.read()
                elif ext in {".docx", ".xlsx"}:
                    # analyzer opens these files directly - placeholder is fine
                    extracted_text = "x" * max(1, os.path.getsize(file_path) // 2)
                elif ext == ".pdf":
                    # For PDFs, try to get real page count estimate
                    try:
                        import fitz  # PyMuPDF
                        doc = fitz.open(file_path)
                        extracted_text = ""
                        for page in doc:
                            extracted_text += page.get_text()
                        doc.close()
                    except ImportError:
                        # Fallback: estimate from file size
                        extracted_text = "x" * max(1, os.path.getsize(file_path) // 3)
                    except Exception:
                        extracted_text = "x" * max(1, os.path.getsize(file_path) // 3)
                elif ext in {".doc", ".xls", ".ppt", ".pptx", ".rtf"}:
                    # Legacy Office formats - generic byte ratio
                    extracted_text = "x" * max(1, os.path.getsize(file_path) // 3)
                else:
                    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                        extracted_text = f.read()
            except Exception:
                extracted_text = ""

            metrics = analyzer.analyze(file_path, extracted_text)
            update_accuracy_metrics(file_key, metrics)
            success += 1

            if i % 25 == 0 or i == total:
                elapsed = time.time() - start_time
                rate = i / max(elapsed, 0.1)
                acc = metrics.get("extraction_accuracy", 0)
                print(
                    f"  [{i}/{total}] {file_name}: "
                    f"{acc:.1f}% ({metrics.get('pipeline_type', '?')}) | "
                    f"Rate: {rate:.1f}/s"
                )

        except Exception as exc:
            errors += 1
            if errors <= 5:
                print(f"  ERROR: {file_name}: {exc}")

    elapsed = time.time() - start_time
    print(f"\n{'='*60}")
    print(f"Backfill complete in {elapsed:.1f}s")
    print(f"  Success: {success}/{total}")
    print(f"  Skipped: {skipped} (file missing)")
    print(f"  Errors:  {errors}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
