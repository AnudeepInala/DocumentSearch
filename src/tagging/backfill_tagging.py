"""
Backfill script: enqueue existing indexed documents into tagging queue.
"""

from __future__ import annotations

from typing import Any, Dict

from core.logging_manager import get_logger, setup_logging
from core.queue_manager import get_queue_manager
from indexing.opensearch_client import OpenSearchClient

logger = get_logger("tagging.backfill")


def _parse_file_id(hit_id: str) -> int:
    if not hit_id:
        return 0
    if hit_id.startswith("file-"):
        try:
            return int(hit_id.split("-", 1)[1])
        except Exception:
            return 0
    return 0


def run_backfill(batch_size: int = 500, max_docs: int = 0) -> Dict[str, Any]:
    os_client = OpenSearchClient()
    queue_manager = get_queue_manager()
    processed = 0
    queued = 0

    search_after = None
    while True:
        body: Dict[str, Any] = {
            "size": int(batch_size),
            "sort": [{"_doc": "asc"}],
            "_source": ["file_path", "file_hash"],
            "query": {"match_all": {}},
        }
        if search_after is not None:
            body["search_after"] = search_after

        response = os_client.client.search(index=os_client.index_name, body=body)
        hits = response.get("hits", {}).get("hits", [])
        if not hits:
            break

        for hit in hits:
            processed += 1
            src = hit.get("_source", {}) or {}
            file_path = str(src.get("file_path", "") or "")
            file_hash = str(src.get("file_hash", "") or "")
            doc_id = str(hit.get("_id", "") or "")
            file_id = _parse_file_id(doc_id)

            try:
                queue_manager.add_to_tagging_queue(
                    file_id=file_id,
                    file_path=file_path,
                    file_hash=file_hash,
                    doc_id=doc_id,
                    priority=5,
                )
                queued += 1
            except Exception as exc:
                logger.warning("Failed to queue doc %s: %s", doc_id, exc)

            if max_docs > 0 and processed >= max_docs:
                return {"processed": processed, "queued": queued}

        # T12: Guard against infinite loop if sort values are missing
        new_search_after = hits[-1].get("sort")
        if new_search_after is None:
            logger.warning("Backfill: last hit has no sort value; stopping to avoid infinite loop")
            break
        if new_search_after == search_after:
            logger.warning("Backfill: search_after unchanged; stopping to avoid infinite loop")
            break
        search_after = new_search_after

    return {"processed": processed, "queued": queued}


if __name__ == "__main__":
    setup_logging()
    result = run_backfill()
    print(f"Backfill complete: processed={result['processed']} queued={result['queued']}")

