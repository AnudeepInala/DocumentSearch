"""
Search API - FastAPI REST endpoints
"""

from fastapi import FastAPI, HTTPException, Query, Depends, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from typing import Optional, List, Dict
import os
import secrets
import signal
import threading
import time
import sys
from pathlib import Path
from collections import defaultdict
from opensearchpy.exceptions import NotFoundError, ConnectionError as OpenSearchConnectionError, TransportError

# Ensure src root is importable when running `python src/api/search_api.py`.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from core.logging_manager import get_logger
from core.config_manager import get_config
from core.queue_manager import get_queue_manager
from indexing.opensearch_client import OpenSearchClient
try:
    from .query_builder import QueryBuilder
except ImportError:
    from api.query_builder import QueryBuilder

logger = get_logger("api")

# L2: Simple in-memory rate limiter
MAX_RESULT_WINDOW = 10_000  # H4: OpenSearch default limit
RATE_LIMIT_REQUESTS = 100   # Max requests per window
RATE_LIMIT_WINDOW = 60      # Window in seconds

_rate_limit_store: Dict[str, list] = defaultdict(list)
_rate_limit_lock = threading.Lock()

# Initialize app
app = FastAPI(
    title="Enterprise Document Search API",
    description="Search API for enterprise document indexing system",
    version="1.0.0"
)

# Initialize components
config = get_config()
api_config = config.api
os_client = OpenSearchClient()
query_builder = QueryBuilder()
queue_manager = get_queue_manager()

# L2: Rate limiting middleware
@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    """Simple per-IP rate limiter with X-Forwarded-For support."""
    # Check X-Forwarded-For header for proper IP detection behind proxy
    forwarded_for = request.headers.get("X-Forwarded-For")
    if forwarded_for:
        # Take the first IP (original client)
        client_ip = forwarded_for.split(",")[0].strip()
    else:
        client_ip = request.client.host if request.client else "unknown"
    
    now = time.time()
    
    with _rate_limit_lock:
        # Purge old entries
        _rate_limit_store[client_ip] = [
            t for t in _rate_limit_store[client_ip]
            if now - t < RATE_LIMIT_WINDOW
        ]
        
        # Cleanup stale IPs occasionally (simple heuristic: every 100 requests overall)
        # This prevents the dict from growing with infinite distinct IPs
        if len(_rate_limit_store) > 10000:
             # Remove empty or old IPs
             to_remove = []
             for ip, times in _rate_limit_store.items():
                 if not times or (now - times[-1] > RATE_LIMIT_WINDOW):
                     to_remove.append(ip)
             for ip in to_remove:
                 del _rate_limit_store[ip]

        if len(_rate_limit_store[client_ip]) >= RATE_LIMIT_REQUESTS:
            return JSONResponse(
                status_code=429,
                content={"detail": f"Rate limit exceeded. Max {RATE_LIMIT_REQUESTS} requests per {RATE_LIMIT_WINDOW}s."}
            )
        _rate_limit_store[client_ip].append(now)
    
    return await call_next(request)

# CORS configuration
if api_config.cors_enabled:
    app.add_middleware(
        CORSMiddleware,
        allow_origins=api_config.allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

# Authentication
def verify_token(authorization: Optional[str] = Header(None)):
    """Verify API token"""
    if not api_config.require_auth:
        return True
    
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing authorization header")
    
    token = authorization.replace("Bearer ", "")
    expected_token = api_config.api_token or os.getenv("API_TOKEN")
    
    # Use constant-time comparison to prevent timing attacks
    if not expected_token or not secrets.compare_digest(token, expected_token):
        raise HTTPException(status_code=403, detail="Invalid token")
    
    return True


@app.get("/")
def root():
    """Root endpoint"""
    return {
        "name": "Enterprise Document Search API",
        "version": "1.0.0",
        "status": "running"
    }


@app.get("/search")
def search(
    q: str = Query(..., description="Search query"),
    page: int = Query(1, ge=1, description="Page number"),
    size: int = Query(20, ge=1, le=100, description="Results per page"),
    fields: Optional[List[str]] = Query(None, description="Fields to search"),
    authenticated: bool = Depends(verify_token)
):
    """
    Search documents
    
    Args:
        q: Search query
        page: Page number (1-indexed)
        size: Results per page
        fields: Fields to search (default: all)
    """
    try:
        # Build query
        if not fields:
            fields = api_config.search['default_fields']
        
        query = query_builder.build_search_query(
            query_text=q,
            fields=fields,
            page=page,
            size=size
        )

        # Only return documents that have finished OCR (or never needed it).
        # Inject into the outermost bool.filter so it applies to every query shape.
        ocr_ready_filter = {
            "bool": {
                "should": [
                    {"term": {"needs_ocr": False}},
                    {"term": {"ocr_completed": True}},
                ],
                "minimum_should_match": 1,
            }
        }
        outer_bool = query.get("query", {}).get("bool")
        if outer_bool is not None:
            existing = outer_bool.get("filter", [])
            if isinstance(existing, dict):
                existing = [existing]
            outer_bool["filter"] = existing + [ocr_ready_filter]
        else:
            query["query"] = {"bool": {"must": query["query"], "filter": [ocr_ready_filter]}}

        # H4: Guard against deep pagination exceeding OpenSearch max_result_window
        from_offset = (page - 1) * size
        if from_offset + size > MAX_RESULT_WINDOW:
            raise HTTPException(
                status_code=400,
                detail=f"Pagination too deep. from+size ({from_offset + size}) exceeds "
                       f"max_result_window ({MAX_RESULT_WINDOW}). Use a smaller page number "
                       f"or fewer results per page."
            )
        
        response = os_client.client.search(
            index=os_client.index_name,
            body=query,
            from_=from_offset,
            size=size
        )
        
        # Format response
        hits = response['hits']
        results = {
            'total': hits['total']['value'],
            'page': page,
            'size': size,
            'results': [
                {
                    'id': hit['_id'],
                    'score': hit['_score'],
                    'document': hit['_source'],
                    'highlights': hit.get('highlight', {})
                }
                for hit in hits['hits']
            ]
        }
        
        return results
        
    except Exception as e:
        logger.error(f"Search error: {e}")
        raise HTTPException(status_code=500, detail="Search service temporarily unavailable")


@app.get("/document/{doc_id}")
def get_document(
    doc_id: str,
    authenticated: bool = Depends(verify_token)
):
    """Get a specific document by ID"""
    try:
        response = os_client.client.get(
            index=os_client.index_name,
            id=doc_id
        )
        
        return {
            'id': response['_id'],
            'document': response['_source']
        }
        
    except NotFoundError:
        raise HTTPException(status_code=404, detail="Document not found")
    except (OpenSearchConnectionError, TransportError) as e:
        logger.error(f"OpenSearch error fetching document {doc_id}: {e}")
        raise HTTPException(status_code=503, detail="Search backend unavailable")
    except Exception as e:
        logger.error(f"Error fetching document {doc_id}: {e}")
        raise HTTPException(status_code=500, detail="Unexpected server error")


@app.get("/status")
@app.get("/api/status")
def get_status():
    """Get system status"""
    try:
        queue_stats = queue_manager.get_queue_stats()
        
        # H6: Calculate progress using extraction + indexing completion,
        # not discovery counts. This prevents progress jumping backward
        # during active discovery when total_discovered keeps increasing.
        discovery = queue_stats.get('discovery', {})
        indexing = queue_stats.get('indexing', {})
        extraction_total = queue_stats.get('extraction_total', {})
        
        # Use discovered counter as denominator (stable after discovery completes)
        total_discovered = sum(discovery.values()) if discovery else 0
        
        # Completed = files that passed through the full pipeline
        completed = indexing.get('completed', 0)
        failed = indexing.get('failed', 0) + extraction_total.get('failed', 0)
        
        progress = 0
        if total_discovered > 0:
            progress = min(100.0, ((completed + failed) / total_discovered) * 100)
        
        return {
            'status': 'running',
            'progress_percent': round(progress, 2),
            'total_discovered': total_discovered,
            'completed': completed,
            'failed': failed,
            'queues': {
                'extraction_pending': extraction_total.get('pending', 0),
                'indexing_pending': indexing.get('pending', 0),
                'ocr_pending': queue_stats.get('ocr', {}).get('pending', 0)
            }
        }
        
    except Exception as e:
        logger.error(f"Status error: {e}")
        raise HTTPException(status_code=500, detail="Status service temporarily unavailable")


@app.get("/metrics")
def get_metrics(authenticated: bool = Depends(verify_token)):
    """Get detailed system metrics"""
    try:
        queue_stats = queue_manager.get_queue_stats()
        
        return {
            'queues': queue_stats,
            'opensearch': os_client.get_stats()
        }
        
    except Exception as e:
        logger.error(f"Metrics error: {e}")
        raise HTTPException(status_code=500, detail="Metrics service temporarily unavailable")


@app.post("/api/shutdown")
def shutdown(authenticated: bool = Depends(verify_token)):
    """Shutdown API process gracefully."""
    def _delayed_shutdown() -> None:
        time.sleep(0.25)
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=_delayed_shutdown, daemon=True).start()
    return {"status": "shutting_down"}


if __name__ == "__main__":
    import uvicorn
    worker_count = max(1, int(getattr(api_config, "workers", 1) or 1))
    uvicorn.run(
        "api.search_api:app",
        host=api_config.host,
        port=api_config.port,
        workers=worker_count
    )
