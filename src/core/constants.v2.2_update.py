"""
Enterprise Document Search System - Constants
Production-grade constant definitions
"""

import os
from enum import Enum
from typing import Final


def get_user_namespace() -> str:
    """Return the OS username for per-user data isolation."""
    return os.getenv("USERNAME") or os.getenv("USER") or "default"

# ============================================================================
# VERSION INFORMATION
# ============================================================================
VERSION: Final[str] = "1.0.0"
BUILD_DATE: Final[str] = "2026-01-21"
SYSTEM_NAME: Final[str] = "Enterprise Document Search System"

# ============================================================================
# QUEUE STATUS CONSTANTS
# ============================================================================
class QueueStatus(str, Enum):
    """File processing status in queue"""
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    DUPLICATE = "duplicate"

# ============================================================================
# FILE SIZE CATEGORIES
# ============================================================================
class SizeCategory(str, Enum):
    """File size categories for routing"""
    TINY = "tiny"       # < 1 MB
    SMALL = "small"     # 1-10 MB
    MEDIUM = "medium"   # 10-50 MB
    LARGE = "large"     # > 50 MB

# Size thresholds in bytes
SIZE_TINY_MAX: Final[int] = 1 * 1024 * 1024          # 1 MB
SIZE_SMALL_MAX: Final[int] = 10 * 1024 * 1024        # 10 MB
SIZE_MEDIUM_MAX: Final[int] = 50 * 1024 * 1024       # 50 MB

# ============================================================================
# WORKER POOL TYPES
# ============================================================================
class WorkerPoolType(str, Enum):
    """Extraction worker pool types"""
    FAST_TRACK = "fast_track"
    STANDARD_TRACK = "standard_track"
    HEAVY_TRACK = "heavy_track"
    EXTREME_TRACK = "extreme_track"

# ============================================================================
# PROCESSING STAGES
# ============================================================================
class ProcessingStage(str, Enum):
    """System processing stages"""
    DISCOVERY = "discovery"
    EXTRACTION = "extraction"
    INDEXING = "indexing"
    OCR = "ocr"
    TAGGING = "tagging"
    COMPLETED = "completed"

# ============================================================================
# ERROR TYPES
# ============================================================================
class ErrorType(str, Enum):
    """Error categorization"""
    TIMEOUT = "timeout"
    CORRUPTED_FILE = "corrupted_file"
    PERMISSION_DENIED = "permission_denied"
    FILE_NOT_FOUND = "file_not_found"
    SERVICE_UNAVAILABLE = "service_unavailable"
    PARSE_ERROR = "parse_error"
    MEMORY_ERROR = "memory_error"
    NETWORK_ERROR = "network_error"
    INDEXING_ERROR = "indexing_error"
    EXTRACTION_FAILED = "extraction_failed"
    INVALID_FORMAT = "invalid_format"
    PASSWORD_PROTECTED = "password_protected"
    OCR_ERROR = "ocr_error"
    TAGGING_ERROR = "tagging_error"
    UNKNOWN = "unknown"

# ============================================================================
# HEALTH STATUS
# ============================================================================
class HealthStatus(str, Enum):
    """Component health status"""
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"

# ============================================================================
# WORKER STATUS
# ============================================================================
class WorkerStatus(str, Enum):
    """Worker operational status"""
    IDLE = "idle"
    BUSY = "busy"
    STARTING = "starting"
    STOPPING = "stopping"
    CRASHED = "crashed"
    RESTARTING = "restarting"

# ============================================================================
# DUPLICATE TYPES
# ============================================================================
class DuplicateType(str, Enum):
    """Type of duplicate detected"""
    EXACT_FILE = "exact_file"          # Same file hash
    EXACT_CONTENT = "exact_content"    # Same content hash
    SIMILAR_CONTENT = "similar_content"  # Near-duplicate (future)

# ============================================================================
# OCR STATUS
# ============================================================================
class OCRStatus(str, Enum):
    """OCR processing status"""
    NOT_NEEDED = "not_needed"
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"

# ============================================================================
# PRIORITY LEVELS
# ============================================================================
class Priority(int, Enum):
    """Processing priority levels (1=highest, 10=lowest)"""
    CRITICAL = 1
    HIGH = 2
    ELEVATED = 3
    NORMAL = 5
    LOW = 7
    ARCHIVE = 10

# ============================================================================
# HASH ALGORITHMS
# ============================================================================
HASH_ALGORITHM: Final[str] = "sha256"
HASH_CHUNK_SIZE: Final[int] = 64 * 1024  # 64 KB chunks

# ============================================================================
# BATCH SIZES
# ============================================================================
DISCOVERY_BATCH_SIZE: Final[int] = 5000
INDEXING_BATCH_SIZE_INITIAL: Final[int] = 1000
OCR_UPDATE_BATCH_SIZE: Final[int] = 100
QUEUE_SYNC_BATCH_SIZE: Final[int] = 5000

# ============================================================================
# TIMEOUTS
# ============================================================================
TIKA_TIMEOUT_SECONDS: Final[int] = 60
OPENSEARCH_TIMEOUT_SECONDS: Final[int] = 30
OCR_TIMEOUT_SECONDS: Final[int] = 600
WORKER_HEARTBEAT_TIMEOUT_SECONDS: Final[int] = 90
SERVICE_CHECK_TIMEOUT_SECONDS: Final[int] = 10

# ============================================================================
# RETRY CONFIGURATION
# ============================================================================
MAX_RETRIES: Final[int] = 3
RETRY_BACKOFF_BASE: Final[float] = 1.0  # Exponential backoff base
MAX_RETRY_DELAY_SECONDS: Final[int] = 30

# ============================================================================
# CHECKPOINT CONFIGURATION
# ============================================================================
CHECKPOINT_INTERVAL_SECONDS: Final[int] = 300  # 5 minutes
CHECKPOINT_RETENTION_COUNT: Final[int] = 10

# ============================================================================
# LOG ROTATION
# ============================================================================
LOG_MAX_BYTES: Final[int] = 100 * 1024 * 1024  # 100 MB
LOG_BACKUP_COUNT: Final[int] = 30  # 30 days

# ============================================================================
# OPENSEARCH CONSTANTS
# ============================================================================
OPENSEARCH_INDEX_NAME: str = f"enterprise_documents_{get_user_namespace()}"
OPENSEARCH_SHARDS: Final[int] = 5
OPENSEARCH_REPLICAS: Final[int] = 1
OPENSEARCH_BULK_REFRESH_INTERVAL: Final[str] = "30s"
OPENSEARCH_NORMAL_REFRESH_INTERVAL: Final[str] = "1s"

# Field names
FIELD_FILE_PATH: Final[str] = "file_path"
FIELD_FILE_NAME: Final[str] = "file_name"
FIELD_FILE_TYPE: Final[str] = "file_type"
FIELD_FILE_SIZE: Final[str] = "file_size"
FIELD_MAIN_CONTENT: Final[str] = "main_content"
FIELD_EMBEDDED_CONTENT: Final[str] = "embedded_content"
FIELD_OCR_CONTENT: Final[str] = "ocr_content"
FIELD_ALL_TEXT: Final[str] = "all_text"
FIELD_FILE_HASH: Final[str] = "file_hash"
FIELD_CONTENT_HASH: Final[str] = "content_hash"
FIELD_IS_DUPLICATE: Final[str] = "is_duplicate"
FIELD_DUPLICATE_OF: Final[str] = "duplicate_of"
FIELD_DUPLICATE_PATHS: Final[str] = "duplicate_paths"
FIELD_OCR_PENDING: Final[str] = "ocr_pending"
FIELD_OCR_CONFIDENCE: Final[str] = "ocr_confidence"
FIELD_INDEXED_DATE: Final[str] = "indexed_date"
FIELD_LAST_MODIFIED: Final[str] = "last_modified"

# ============================================================================
# IMAGE PROCESSING CONSTANTS
# ============================================================================
OCR_TARGET_DPI: Final[int] = 300
OCR_MIN_CONFIDENCE: Final[int] = 25
OCR_GOOD_CONFIDENCE: Final[int] = 70
OCR_EXCELLENT_CONFIDENCE: Final[int] = 90

# ============================================================================
# PERFORMANCE TARGETS
# ============================================================================
TARGET_DISCOVERY_RATE: Final[int] = 30000  # files/second
TARGET_EXTRACTION_RATE: Final[int] = 180   # files/second
TARGET_INDEXING_RATE: Final[int] = 7000    # docs/second
TARGET_OCR_RATE: Final[int] = 600          # pages/hour

# ============================================================================
# RESOURCE THRESHOLDS
# ============================================================================
CPU_HIGH_THRESHOLD: Final[float] = 0.95
CPU_LOW_THRESHOLD: Final[float] = 0.60
MEMORY_WARNING_THRESHOLD_GB: Final[int] = 115
MEMORY_CRITICAL_THRESHOLD_GB: Final[int] = 120
MEMORY_EMERGENCY_THRESHOLD_GB: Final[int] = 124
DISK_WARNING_THRESHOLD_GB: Final[int] = 200
DISK_CRITICAL_THRESHOLD_GB: Final[int] = 100
DISK_EMERGENCY_THRESHOLD_GB: Final[int] = 50

# ============================================================================
# WORKER LIFECYCLE
# ============================================================================
WORKER_RESTART_AFTER_FILES: Final[int] = 500
WORKER_HEARTBEAT_INTERVAL: Final[int] = 30

# ============================================================================
# TEXT NORMALIZATION
# ============================================================================
MIN_TEXT_LENGTH_FOR_OCR: Final[int] = 100

# ============================================================================
# MIME TYPE PREFIXES (for dynamic detection)
# ============================================================================
# These are used for categorization, not filtering
# All MIME types detected by Tika will be processed

IMAGE_MIME_PREFIX: Final[str] = "image/"
DOCUMENT_MIME_PREFIXES: Final[tuple] = (
    "application/pdf",
    "application/msword",
    "application/vnd.openxmlformats",
    "application/vnd.ms-",
    "application/vnd.oasis.opendocument",
    "text/"
)
ARCHIVE_MIME_PREFIXES: Final[tuple] = (
    "application/zip",
    "application/x-rar",
    "application/x-7z",
    "application/x-tar",
    "application/gzip"
)
EMAIL_MIME_PREFIXES: Final[tuple] = (
    "application/vnd.ms-outlook",
    "message/rfc822",
    "message/"
)

# ============================================================================
# API CONSTANTS
# ============================================================================
API_DEFAULT_PAGE_SIZE: Final[int] = 20
API_MAX_PAGE_SIZE: Final[int] = 100
API_MAX_HIGHLIGHTS: Final[int] = 3
API_HIGHLIGHT_SNIPPET_SIZE: Final[int] = 150

# ============================================================================
# CIRCUIT BREAKER
# ============================================================================
CIRCUIT_BREAKER_FAILURE_THRESHOLD: Final[int] = 5
CIRCUIT_BREAKER_TIMEOUT_SECONDS: Final[int] = 60
CIRCUIT_BREAKER_HALF_OPEN_ATTEMPTS: Final[int] = 3

# ============================================================================
# BLOOM FILTER
# ============================================================================
BLOOM_FILTER_EXPECTED_ELEMENTS: Final[int] = 5_000_000
BLOOM_FILTER_FPR: Final[float] = 0.01  # 1% false positive rate

# ============================================================================
# OPERATIONAL MODES
# ============================================================================
class OperationalMode(str, Enum):
    """System operational modes"""
    FULL = "full"              # Index everything from scratch
    RESUME = "resume"          # Continue from checkpoint
    INCREMENTAL = "incremental"  # Only new/changed files

# ============================================================================
# ALERT TYPES
# ============================================================================
class AlertType(str, Enum):
    """Alert severity levels"""
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"

# ============================================================================
# DATABASE TABLES
# ============================================================================
TABLE_DISCOVERED_FILES: Final[str] = "discovered_files"
TABLE_EXTRACTION_QUEUE: Final[str] = "extraction_queue"
TABLE_INDEXING_QUEUE: Final[str] = "indexing_queue"
TABLE_OCR_QUEUE: Final[str] = "ocr_queue"
TABLE_TAGGING_QUEUE: Final[str] = "tagging_queue"
TABLE_FAILED_FILES: Final[str] = "failed_files"
TABLE_COMPLETED_FILES: Final[str] = "completed_files"
TABLE_FILE_HASHES: Final[str] = "file_hashes"
TABLE_CONTENT_HASHES: Final[str] = "content_hashes"

# ============================================================================
# TEMPORARY FILE PATTERNS
# ============================================================================
TEMP_FILE_PATTERNS: Final[list] = [
    "*.tmp",
    "*.temp",
    "~$*",
    ".~*"
]

# ============================================================================
# FILE EXCLUSIONS (only exclude what can't be processed)
# ============================================================================
# These are common binary/executable extensions that should be excluded
# All other files will be processed and Tika will determine if they're supported
EXCLUDED_EXTENSIONS: Final[set] = {
    ".exe", ".dll", ".sys", ".drv", ".bin", ".dat", ".db", ".lock"
}

# ============================================================================
# SYSTEM FOLDER EXCLUSIONS
# ============================================================================
SYSTEM_FOLDER_EXCLUSIONS: Final[set] = {
    "$RECYCLE.BIN",
    "System Volume Information",
    "Windows",
    "Program Files",
    "Program Files (x86)",
    "ProgramData",
    "Recovery",
    "$Windows.~BT",
    "$Windows.~WS"
}

# ============================================================================
# HTTP STATUS CODES
# ============================================================================
HTTP_OK: Final[int] = 200
HTTP_BAD_REQUEST: Final[int] = 400
HTTP_UNAUTHORIZED: Final[int] = 401
HTTP_NOT_FOUND: Final[int] = 404
HTTP_TOO_MANY_REQUESTS: Final[int] = 429
HTTP_INTERNAL_ERROR: Final[int] = 500
HTTP_SERVICE_UNAVAILABLE: Final[int] = 503

# ============================================================================
# METRICS COLLECTION
# ============================================================================
METRICS_COLLECTION_INTERVAL: Final[int] = 60  # seconds
METRICS_HISTORY_HOURS: Final[int] = 24

# ============================================================================
# GRACEFUL SHUTDOWN
# ============================================================================
SHUTDOWN_GRACE_PERIOD_SECONDS: Final[int] = 60

# ============================================================================
# CONTENT LIMITS
# ============================================================================
MAX_CONTENT_LENGTH_BYTES: Final[int] = 100 * 1024 * 1024  # 100 MB
MAX_DUPLICATE_PATHS: Final[int] = 100

# ============================================================================
# DATE FORMATS
# ============================================================================
DATETIME_FORMAT: Final[str] = "%Y-%m-%d %H:%M:%S"
DATE_FORMAT: Final[str] = "%Y-%m-%d"
CHECKPOINT_DATETIME_FORMAT: Final[str] = "%Y%m%d_%H%M%S"
