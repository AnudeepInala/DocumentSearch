"""
Enterprise Document Search System - Queue Manager
Production-grade persistent queue with atomic operations and crash recovery
"""

import sqlite3
import threading
import time
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime
from contextlib import contextmanager

from core.config_manager import get_config
from core.constants import (
    QueueStatus, SizeCategory, Priority, ErrorType,
    TABLE_DISCOVERED_FILES, TABLE_EXTRACTION_QUEUE,
    TABLE_INDEXING_QUEUE, TABLE_OCR_QUEUE, TABLE_TAGGING_QUEUE,
    TABLE_FAILED_FILES, TABLE_COMPLETED_FILES,
    TABLE_FILE_HASHES, TABLE_CONTENT_HASHES
)
TABLE_WORKER_HEARTBEATS = "worker_heartbeats"
TABLE_SYSTEM_FLAGS = "system_flags"

from core.logging_manager import get_logger
from core.redis_queue_manager import RedisQueueManager


logger = get_logger("queue_manager")


class QueueManager:
    """
    Thread-safe persistent queue manager using SQLite
    Handles all queue operations with ACID guarantees
    """
    
    def __init__(self, db_path: Optional[str] = None):
        """
        Initialize queue manager
        
        Args:
            db_path: Path to SQLite database file
        """
        if db_path is None:
            config = get_config()
            db_path = Path(config.paths.queue_db) / "queues.db"
        
        self.db_path = str(db_path)
        self._local = threading.local()
        self._lock = threading.RLock()
        
        # Ensure database directory exists
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        
        # Initialize database schema
        self._initialize_database()
        
        logger.info(f"Queue manager initialized: {self.db_path}")

    
    def get_completed_items(self, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        """
        Get list of completed items from TABLE_COMPLETED_FILES.
        
        Args:
            limit: Max items to return
            offset: Pagination offset
            
        Returns:
            List of completed item dictionaries
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(f"""
                    SELECT 
                        c.id, c.file_id, c.indexed_at, c.extraction_time_ms, 
                        c.indexing_time_ms, c.is_duplicate,
                        d.file_path, d.file_name, d.file_size, d.file_hash,
                        d.file_extension, d.last_modified, d.created
                    FROM {TABLE_COMPLETED_FILES} c
                    JOIN {TABLE_DISCOVERED_FILES} d ON c.file_id = d.id
                    ORDER BY c.indexed_at DESC
                    LIMIT ? OFFSET ?
                """, (limit, offset))
                
                rows = cursor.fetchall()
                return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"Error getting completed items: {e}")
            return []
    def reset_database(self) -> None:
        """
        Reset the database by dropping all tables and recreating them.
        WARNING: This deletes all data!
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            # Disable foreign keys temporarily
            cursor.execute("PRAGMA foreign_keys = OFF")
            
            # Drop all known tables
            tables = [
                TABLE_DISCOVERED_FILES,
                TABLE_EXTRACTION_QUEUE,
                TABLE_INDEXING_QUEUE,
                TABLE_OCR_QUEUE,
                TABLE_TAGGING_QUEUE,
                TABLE_FAILED_FILES,
                TABLE_COMPLETED_FILES,
                TABLE_FILE_HASHES,

                TABLE_CONTENT_HASHES,
                TABLE_WORKER_HEARTBEATS
            ]
            
            for table in tables:
                try:
                    cursor.execute(f"DROP TABLE IF EXISTS {table}")
                except Exception as e:
                    logger.error(f"Error dropping table {table}: {e}")
            
            # Re-enable foreign keys
            cursor.execute("PRAGMA foreign_keys = ON")
            
        # Re-initialize schema
        self._initialize_database()
        logger.info("Database reset complete")
    
    @contextmanager
    def _get_connection(self):
        """Get thread-local database connection with WAL mode and proper transaction handling"""
        if not hasattr(self._local, 'connection') or self._local.connection is None:
            self._local.connection = sqlite3.connect(
                self.db_path,
                isolation_level='DEFERRED',  # Use proper transaction mode (not autocommit)
                check_same_thread=False,
                timeout=60.0  # Increased timeout for concurrent access
            )
            self._local.connection.row_factory = sqlite3.Row
            
            # Enable WAL mode for better concurrency
            self._local.connection.execute("PRAGMA journal_mode=WAL")
            self._local.connection.execute("PRAGMA synchronous=NORMAL")
            self._local.connection.execute("PRAGMA cache_size=-64000")  # 64MB cache
            self._local.connection.execute("PRAGMA temp_store=MEMORY")
            self._local.connection.execute("PRAGMA busy_timeout=60000")  # 60 second busy timeout
        
        try:
            yield self._local.connection
        except sqlite3.OperationalError as e:
            if "database is locked" in str(e):
                logger.warning(f"Database lock detected, retrying: {e}")
                # Wait and retry
                time.sleep(0.5)
                # Don't reset connection on lock - let caller retry
            raise
        except sqlite3.Error as e:
            logger.error(f"SQLite error: {e}")
            # Try to reset connection on SQLite errors
            try:
                if hasattr(self._local, 'connection') and self._local.connection:
                    self._local.connection.close()
            except Exception:
                pass
            self._local.connection = None
            raise
        except Exception as e:
            logger.error(f"Database error: {e}")
            raise
    
    def _initialize_database(self) -> None:
        """Initialize database schema with all required tables and indexes"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            # System flags table (M9 Fix)
            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS {TABLE_SYSTEM_FLAGS} (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at REAL
                )
            """)

            # ================================================================
            # Table: discovered_files
            # Tracks all files discovered during scanning
            # ================================================================
            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS {TABLE_DISCOVERED_FILES} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_path TEXT NOT NULL UNIQUE,
                    file_name TEXT NOT NULL,
                    file_size INTEGER NOT NULL,
                    file_extension TEXT,
                    file_hash TEXT NOT NULL,
                    last_modified REAL NOT NULL,
                    created REAL NOT NULL,
                    size_category TEXT NOT NULL,
                    priority INTEGER NOT NULL DEFAULT 5,
                    status TEXT NOT NULL DEFAULT '{QueueStatus.PENDING.value}',
                    discovered_at REAL NOT NULL,
                    worker_id TEXT,
                    processing_started_at REAL,
                    processing_completed_at REAL,
                    error_type TEXT,
                    error_message TEXT,
                    retry_count INTEGER DEFAULT 0
                )
            """)
            
            # Create indexes for discovered_files
            cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_discovered_status ON {TABLE_DISCOVERED_FILES} (status)")
            cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_discovered_file_hash ON {TABLE_DISCOVERED_FILES} (file_hash)")
            cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_discovered_priority_status ON {TABLE_DISCOVERED_FILES} (priority, status)")
            cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_discovered_size_category ON {TABLE_DISCOVERED_FILES} (size_category, status)")
            
            # ================================================================
            # Table: extraction_queue  
            # Separate queues by size category for routing
            # ================================================================
            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS {TABLE_EXTRACTION_QUEUE} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_id INTEGER NOT NULL,
                    file_path TEXT NOT NULL,
                    file_size INTEGER NOT NULL,
                    size_category TEXT NOT NULL,
                    priority INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT '{QueueStatus.PENDING.value}',
                    worker_id TEXT,
                    claimed_at REAL,
                    completed_at REAL,
                    processing_time_ms INTEGER,
                    FOREIGN KEY (file_id) REFERENCES {TABLE_DISCOVERED_FILES}(id)
                )
            """)
            
            # Create indexes for extraction_queue
            cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_extraction_size_status ON {TABLE_EXTRACTION_QUEUE} (size_category, status, priority)")
            cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_extraction_worker ON {TABLE_EXTRACTION_QUEUE} (worker_id)")
            cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_extraction_claimed_timeout ON {TABLE_EXTRACTION_QUEUE} (status, claimed_at)")
            
            # ================================================================
            # Table: indexing_queue
            # Documents ready for OpenSearch indexing
            # ================================================================
            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS {TABLE_INDEXING_QUEUE} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_id INTEGER NOT NULL,
                    document_json TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT '{QueueStatus.PENDING.value}',
                    worker_id TEXT,
                    claimed_at REAL,
                    indexed_at REAL,
                    batch_id TEXT,
                    retry_count INTEGER DEFAULT 0,
                    FOREIGN KEY (file_id) REFERENCES {TABLE_DISCOVERED_FILES}(id)
                )
            """)
            
            # Create indexes for indexing_queue
            cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_indexing_status ON {TABLE_INDEXING_QUEUE} (status)")
            cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_indexing_batch ON {TABLE_INDEXING_QUEUE} (batch_id)")
            cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_indexing_claimed_timeout ON {TABLE_INDEXING_QUEUE} (status, claimed_at)")
            
            # ================================================================
            # Table: ocr_queue
            # Files/pages requiring OCR processing
            # ================================================================
            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS {TABLE_OCR_QUEUE} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_id INTEGER NOT NULL,
                    file_path TEXT NOT NULL,
                    page_number INTEGER DEFAULT NULL,
                    priority INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT '{QueueStatus.PENDING.value}',
                    worker_id TEXT,
                    claimed_at REAL,
                    completed_at REAL,
                    ocr_confidence REAL,
                    processing_time_ms INTEGER,
                    document_id TEXT,
                    FOREIGN KEY (file_id) REFERENCES {TABLE_DISCOVERED_FILES}(id)
                )
            """)
            
            # Create indexes for ocr_queue
            cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_ocr_priority_status ON {TABLE_OCR_QUEUE} (priority, status)")
            cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_ocr_worker ON {TABLE_OCR_QUEUE} (worker_id)")
            cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_ocr_claimed_timeout ON {TABLE_OCR_QUEUE} (status, claimed_at)")

            # ================================================================
            # Table: tagging_queue
            # Async semantic tagging work
            # ================================================================
            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS {TABLE_TAGGING_QUEUE} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_id INTEGER NOT NULL,
                    file_path TEXT,
                    file_hash TEXT,
                    doc_id TEXT,
                    priority INTEGER NOT NULL DEFAULT 5,
                    status TEXT NOT NULL DEFAULT '{QueueStatus.PENDING.value}',
                    worker_id TEXT,
                    claimed_at REAL,
                    completed_at REAL,
                    processing_time_ms INTEGER,
                    retry_count INTEGER DEFAULT 0,
                    error_message TEXT,
                    FOREIGN KEY (file_id) REFERENCES {TABLE_DISCOVERED_FILES}(id)
                )
            """)
            cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_tagging_status ON {TABLE_TAGGING_QUEUE} (status)")
            cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_tagging_claimed_timeout ON {TABLE_TAGGING_QUEUE} (status, claimed_at)")
            cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_tagging_file_id ON {TABLE_TAGGING_QUEUE} (file_id)")
            
            # ================================================================
            # Table: failed_files
            # Permanent failures for later review
            # ================================================================
            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS {TABLE_FAILED_FILES} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_id INTEGER NOT NULL,
                    file_path TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    error_type TEXT NOT NULL,
                    error_message TEXT,
                    failed_at REAL NOT NULL,
                    retry_count INTEGER,
                    stack_trace TEXT,
                    FOREIGN KEY (file_id) REFERENCES {TABLE_DISCOVERED_FILES}(id)
                )
            """)
            
            # Create index for failed_files
            cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_failed_stage_error ON {TABLE_FAILED_FILES} (stage, error_type)")
            
            # ================================================================
            # Table: completed_files
            # Successfully processed files
            # ================================================================
            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS {TABLE_COMPLETED_FILES} (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_id INTEGER NOT NULL,
                    file_path TEXT NOT NULL,
                    file_hash TEXT NOT NULL,
                    content_hash TEXT,
                    document_id TEXT,
                    is_duplicate BOOLEAN DEFAULT 0,
                    duplicate_of TEXT,
                    indexed_at REAL NOT NULL,
                    extraction_time_ms INTEGER,
                    indexing_time_ms INTEGER,
                    ocr_completed BOOLEAN DEFAULT 0,
                    ocr_confidence REAL,
                    FOREIGN KEY (file_id) REFERENCES {TABLE_DISCOVERED_FILES}(id)
                )
            """)
            
            # Create indexes for completed_files
            cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_completed_file_hash ON {TABLE_COMPLETED_FILES} (file_hash)")
            cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_completed_content_hash ON {TABLE_COMPLETED_FILES} (content_hash)")
            cursor.execute(f"CREATE INDEX IF NOT EXISTS idx_completed_document_id ON {TABLE_COMPLETED_FILES} (document_id)")
            
            # ================================================================
            # Table: file_hashes
            # Fast lookup for file-level deduplication
            # ================================================================
            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS {TABLE_FILE_HASHES} (
                    file_hash TEXT PRIMARY KEY,
                    first_file_path TEXT NOT NULL,
                    first_seen_at REAL NOT NULL,
                    duplicate_count INTEGER DEFAULT 0,
                    duplicate_paths TEXT
                )
            """)
            
            # ================================================================
            # Table: content_hashes
            # Fast lookup for content-level deduplication
            # ================================================================
            cursor.execute(f"""
                CREATE TABLE IF NOT EXISTS {TABLE_CONTENT_HASHES} (
                    content_hash TEXT PRIMARY KEY,
                    first_file_path TEXT NOT NULL,
                    first_document_id TEXT NOT NULL,
                    first_seen_at REAL NOT NULL,
                    duplicate_count INTEGER DEFAULT 0
                )
            """)
            
            conn.commit()
            # Apply lightweight schema migrations for older DBs
            try:
                self._apply_schema_migrations(conn)
            except Exception as e:
                logger.error(f"Error applying schema migrations: {e}")

            logger.info("Database schema initialized successfully")
    
    # ========================================================================
    # DISCOVERY OPERATIONS
    # ========================================================================
    
    def add_discovered_file(
        self,
        file_path: str,
        file_name: str,
        file_size: int,
        file_extension: str,
        file_hash: str,
        last_modified: float,
        created: float,
        size_category: SizeCategory,
        priority: Priority
    ) -> Optional[int]:
        """
        Add discovered file to queue
        
        Returns:
            File ID if added, None if duplicate
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            try:
                cursor.execute(f"""
                    INSERT INTO {TABLE_DISCOVERED_FILES}
                    (file_path, file_name, file_size, file_extension, file_hash,
                     last_modified, created, size_category, priority, status, discovered_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    file_path, file_name, file_size, file_extension, file_hash,
                    last_modified, created, size_category.value, priority.value,
                    QueueStatus.PENDING.value, datetime.now().timestamp()
                ))
                
                conn.commit()
                return cursor.lastrowid
                
            except sqlite3.IntegrityError:
                # File already exists (duplicate path)
                return None
    
    def add_discovered_files_batch(self, files: List[Dict[str, Any]]) -> int:
        """
        Add multiple discovered files in batch
        
        Args:
            files: List of file dictionaries
        
        Returns:
            Number of files added
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            inserted = 0
            current_time = datetime.now().timestamp()
            
            for file_data in files:
                try:
                    cursor.execute(f"""
                        INSERT INTO {TABLE_DISCOVERED_FILES}
                        (file_path, file_name, file_size, file_extension, file_hash,
                         last_modified, created, size_category, priority, status, discovered_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        file_data['file_path'],
                        file_data['file_name'],
                        file_data['file_size'],
                        file_data.get('file_extension', ''),
                        file_data['file_hash'],
                        file_data['last_modified'],
                        file_data.get('created', current_time),
                        file_data['size_category'],
                        file_data.get('priority', Priority.NORMAL.value),
                        QueueStatus.PENDING.value,
                        current_time
                    ))
                    inserted += 1
                except sqlite3.IntegrityError:
                    continue
            
            conn.commit()
            return inserted
    
    def check_file_hash_exists(self, file_hash: str) -> Optional[Tuple[int, str]]:
        """
        Check if file hash exists in completed files
        
        Returns:
            (file_id, file_path) if exists, None otherwise
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute(f"""
                SELECT file_id, file_path
                FROM {TABLE_COMPLETED_FILES}
                WHERE file_hash = ?
                LIMIT 1
            """, (file_hash,))
            
            row = cursor.fetchone()
            return (row['file_id'], row['file_path']) if row else None
    
    # ========================================================================
    # EXTRACTION QUEUE OPERATIONS
    # ========================================================================
    
    def add_to_extraction_queue(
        self,
        file_id: int,
        file_path: str,
        file_size: int,
        size_category: SizeCategory,
        priority: Priority
    ) -> int:
        """Add file to extraction queue"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute(f"""
                INSERT INTO {TABLE_EXTRACTION_QUEUE}
                (file_id, file_path, file_size, size_category, priority, status)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                file_id, file_path, file_size,
                size_category.value, priority.value, QueueStatus.PENDING.value
            ))
            
            conn.commit()
            return cursor.lastrowid
    
    def claim_extraction_work(
        self,
        size_category: SizeCategory,
        worker_id: str,
        batch_size: int = 1
    ) -> List[Dict[str, Any]]:
        """
        Atomically claim work from extraction queue
        
        Args:
            size_category: Which queue to pull from
            worker_id: Worker claiming the work
            batch_size: Number of files to claim
        
        Returns:
            List of claimed work items
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            # Begin transaction
            cursor.execute("BEGIN IMMEDIATE")
            
            try:
                # Select available work (not claimed or timed out)
                timeout_threshold = datetime.now().timestamp() - 300  # 5 minutes
                
                cursor.execute(f"""
                    SELECT id, file_id, file_path, file_size, priority
                    FROM {TABLE_EXTRACTION_QUEUE}
                    WHERE size_category = ?
                      AND (status = ? OR (status = ? AND claimed_at < ?))
                    ORDER BY priority ASC, id ASC
                    LIMIT ?
                """, (
                    size_category.value,
                    QueueStatus.PENDING.value,
                    QueueStatus.PROCESSING.value,
                    timeout_threshold,
                    batch_size
                ))
                
                rows = cursor.fetchall()
                
                if not rows:
                    conn.rollback()
                    return []
                
                # Claim the work
                work_ids = [row['id'] for row in rows]
                placeholders = ','.join('?' * len(work_ids))
                current_time = datetime.now().timestamp()
                
                cursor.execute(f"""
                    UPDATE {TABLE_EXTRACTION_QUEUE}
                    SET status = ?,
                        worker_id = ?,
                        claimed_at = ?
                    WHERE id IN ({placeholders})
                """, (QueueStatus.PROCESSING.value, worker_id, current_time, *work_ids))
                
                conn.commit()
                
                return [dict(row) for row in rows]
                
            except Exception as e:
                conn.rollback()
                logger.error(f"Error claiming extraction work: {e}")
                return []
    
    def complete_extraction(
        self,
        queue_id: int,
        processing_time_ms: int,
        worker_id: str = None
    ) -> None:
        """Mark extraction work as completed (worker_id for Redis compatibility)"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute(f"""
                UPDATE {TABLE_EXTRACTION_QUEUE}
                SET status = ?,
                    completed_at = ?,
                    processing_time_ms = ?
                WHERE id = ?
            """, (
                QueueStatus.COMPLETED.value,
                datetime.now().timestamp(),
                processing_time_ms,
                queue_id
            ))
            
            conn.commit()
    
    # ========================================================================
    # INDEXING QUEUE OPERATIONS
    # ========================================================================
    
    def add_to_indexing_queue(
        self,
        file_id: int,
        document_json: str
    ) -> int:
        """Add document to indexing queue"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute(f"""
                INSERT INTO {TABLE_INDEXING_QUEUE}
                (file_id, document_json, status)
                VALUES (?, ?, ?)
            """, (file_id, document_json, QueueStatus.PENDING.value))
            
            conn.commit()
            return cursor.lastrowid
    
    def claim_indexing_work(
        self,
        worker_id: str,
        batch_size: int
    ) -> List[Dict[str, Any]]:
        """Atomically claim documents for indexing"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute("BEGIN IMMEDIATE")
            
            try:
                timeout_threshold = datetime.now().timestamp() - 300
                
                cursor.execute(f"""
                    SELECT id, file_id, document_json, retry_count
                    FROM {TABLE_INDEXING_QUEUE}
                    WHERE status = ? OR (status = ? AND claimed_at < ?)
                    ORDER BY id ASC
                    LIMIT ?
                """, (
                    QueueStatus.PENDING.value,
                    QueueStatus.PROCESSING.value,
                    timeout_threshold,
                    batch_size
                ))
                
                rows = cursor.fetchall()
                
                if not rows:
                    conn.rollback()
                    return []
                
                work_ids = [row['id'] for row in rows]
                placeholders = ','.join('?' * len(work_ids))
                current_time = datetime.now().timestamp()
                
                cursor.execute(f"""
                    UPDATE {TABLE_INDEXING_QUEUE}
                    SET status = ?, worker_id = ?, claimed_at = ?
                    WHERE id IN ({placeholders})
                """, (QueueStatus.PROCESSING.value, worker_id, current_time, *work_ids))
                
                conn.commit()
                
                return [dict(row) for row in rows]
                
            except Exception as e:
                conn.rollback()
                logger.error(f"Error claiming indexing work: {e}")
                return []
    
    def complete_indexing_batch(self, queue_ids: List[int], worker_id: str = None) -> None:
        """Mark batch of documents as indexed (worker_id for Redis compatibility)"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            placeholders = ','.join('?' * len(queue_ids))
            current_time = datetime.now().timestamp()
            
            cursor.execute(f"""
                UPDATE {TABLE_INDEXING_QUEUE}
                SET status = ?, indexed_at = ?
                WHERE id IN ({placeholders})
            """, (QueueStatus.COMPLETED.value, current_time, *queue_ids))
            
            conn.commit()

    def requeue_indexing_items(
        self,
        queue_ids: List[int],
        *,
        increment_retry: bool = True
    ) -> None:
        """Return indexing items to pending status for retry"""
        if not queue_ids:
            return

        with self._get_connection() as conn:
            cursor = conn.cursor()

            placeholders = ','.join('?' * len(queue_ids))
            set_parts = [
                "status = ?",
                "worker_id = NULL",
                "claimed_at = NULL",
                "indexed_at = NULL"
            ]

            if increment_retry:
                set_parts.insert(1, "retry_count = retry_count + 1")

            set_clause = ",\n                    ".join(set_parts)

            sql = f"""
                UPDATE {TABLE_INDEXING_QUEUE}
                SET {set_clause}
                WHERE id IN ({placeholders})
            """

            cursor.execute(sql, (QueueStatus.PENDING.value, *queue_ids))

            conn.commit()

    def fail_indexing_items(self, queue_ids: List[int]) -> None:
        """Mark indexing queue items as failed"""
        if not queue_ids:
            return

        with self._get_connection() as conn:
            cursor = conn.cursor()

            placeholders = ','.join('?' * len(queue_ids))

            cursor.execute(
                f"""
                UPDATE {TABLE_INDEXING_QUEUE}
                SET status = ?,
                    worker_id = NULL,
                    claimed_at = NULL
                WHERE id IN ({placeholders})
                """,
                (QueueStatus.FAILED.value, *queue_ids)
            )

            conn.commit()
    
    # ========================================================================
    # OCR QUEUE OPERATIONS
    # ========================================================================
    
    def add_to_ocr_queue(
        self,
        file_id: int,
        file_path: str,
        priority: Priority,
        page_number: Optional[int] = None,
        document_id: Optional[str] = None
    ) -> int:
        """Add file/page to OCR queue"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute(f"""
                INSERT INTO {TABLE_OCR_QUEUE}
                (file_id, file_path, page_number, priority, status, document_id)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                file_id, file_path, page_number,
                priority.value, QueueStatus.PENDING.value, document_id
            ))
            
            conn.commit()
            return cursor.lastrowid
    
    def claim_ocr_work(
        self,
        worker_id: str,
        batch_size: int = 1
    ) -> List[Dict[str, Any]]:
        """Atomically claim OCR work"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute("BEGIN IMMEDIATE")
            
            try:
                timeout_threshold = datetime.now().timestamp() - 600  # 10 minutes
                
                cursor.execute(f"""
                    SELECT id, file_id, file_path, page_number, document_id
                    FROM {TABLE_OCR_QUEUE}
                    WHERE status = ? OR (status = ? AND claimed_at < ?)
                    ORDER BY priority ASC, id ASC
                    LIMIT ?
                """, (
                    QueueStatus.PENDING.value,
                    QueueStatus.PROCESSING.value,
                    timeout_threshold,
                    batch_size
                ))
                
                rows = cursor.fetchall()
                
                if not rows:
                    conn.rollback()
                    return []
                
                work_ids = [row['id'] for row in rows]
                placeholders = ','.join('?' * len(work_ids))
                current_time = datetime.now().timestamp()
                
                cursor.execute(f"""
                    UPDATE {TABLE_OCR_QUEUE}
                    SET status = ?, worker_id = ?, claimed_at = ?
                    WHERE id IN ({placeholders})
                """, (QueueStatus.PROCESSING.value, worker_id, current_time, *work_ids))
                
                conn.commit()
                
                return [dict(row) for row in rows]
                
            except Exception as e:
                conn.rollback()
                logger.error(f"Error claiming OCR work: {e}")
                return []
    
    def complete_ocr(
        self,
        queue_id: int,
        ocr_confidence: float,
        processing_time_ms: int,
        worker_id: str = None
    ) -> None:
        """Mark OCR work as completed (worker_id for Redis compatibility)"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute(f"""
                UPDATE {TABLE_OCR_QUEUE}
                SET status = ?,
                    completed_at = ?,
                    ocr_confidence = ?,
                    processing_time_ms = ?
                WHERE id = ?
            """, (
                QueueStatus.COMPLETED.value,
                datetime.now().timestamp(),
                ocr_confidence,
                processing_time_ms,
                queue_id
            ))
            
            conn.commit()

    # ========================================================================
    # TAGGING QUEUE OPERATIONS
    # ========================================================================

    def add_to_tagging_queue(
        self,
        file_id: int,
        file_path: str,
        file_hash: str = "",
        doc_id: str = "",
        priority: int = 5,
    ) -> int:
        """Add file to tagging queue."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"""
                INSERT INTO {TABLE_TAGGING_QUEUE}
                (file_id, file_path, file_hash, doc_id, priority, status)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    file_id,
                    file_path,
                    file_hash,
                    doc_id,
                    int(priority or 5),
                    QueueStatus.PENDING.value,
                ),
            )
            conn.commit()
            return cursor.lastrowid

    def claim_tagging_work(
        self,
        worker_id: str,
        batch_size: int = 8,
    ) -> List[Dict[str, Any]]:
        """Atomically claim tagging work items."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            try:
                timeout_threshold = datetime.now().timestamp() - 600
                cursor.execute(
                    f"""
                    SELECT id, file_id, file_path, file_hash, doc_id, priority, retry_count
                    FROM {TABLE_TAGGING_QUEUE}
                    WHERE status = ? OR (status = ? AND claimed_at < ?)
                    ORDER BY priority ASC, id ASC
                    LIMIT ?
                    """,
                    (
                        QueueStatus.PENDING.value,
                        QueueStatus.PROCESSING.value,
                        timeout_threshold,
                        int(batch_size or 8),
                    ),
                )
                rows = cursor.fetchall()
                if not rows:
                    conn.rollback()
                    return []

                queue_ids = [row['id'] for row in rows]
                placeholders = ",".join("?" * len(queue_ids))
                now_ts = datetime.now().timestamp()
                cursor.execute(
                    f"""
                    UPDATE {TABLE_TAGGING_QUEUE}
                    SET status = ?, worker_id = ?, claimed_at = ?
                    WHERE id IN ({placeholders})
                    """,
                    (QueueStatus.PROCESSING.value, worker_id, now_ts, *queue_ids),
                )
                conn.commit()
                return [dict(row) for row in rows]
            except Exception:
                conn.rollback()
                raise

    def complete_tagging(
        self,
        queue_id: int,
        processing_time_ms: int,
        worker_id: str,
        status: str = QueueStatus.COMPLETED.value,
    ) -> None:
        """Mark tagging queue item as completed/failed."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"""
                UPDATE {TABLE_TAGGING_QUEUE}
                SET status = ?, completed_at = ?, processing_time_ms = ?, worker_id = ?
                WHERE id = ?
                """,
                (
                    status,
                    datetime.now().timestamp(),
                    int(processing_time_ms or 0),
                    worker_id,
                    queue_id,
                ),
            )
            conn.commit()

    def requeue_tagging(self, queue_id: int, reason: str = "") -> None:
        """Return tagging item to pending state and increment retry count."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"""
                UPDATE {TABLE_TAGGING_QUEUE}
                SET status = ?,
                    worker_id = NULL,
                    claimed_at = NULL,
                    completed_at = NULL,
                    retry_count = retry_count + 1,
                    error_message = ?
                WHERE id = ?
                """,
                (QueueStatus.PENDING.value, str(reason or ""), queue_id),
            )
            conn.commit()
    
    # ========================================================================
    # DEDUPLICATION OPERATIONS
    # ========================================================================
    
    def register_file_hash(
        self,
        file_hash: str,
        file_path: str
    ) -> bool:
        """
        Register file hash
        
        Returns:
            True if new, False if duplicate
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            try:
                cursor.execute(f"""
                    INSERT INTO {TABLE_FILE_HASHES}
                    (file_hash, first_file_path, first_seen_at, duplicate_count)
                    VALUES (?, ?, ?, 0)
                """, (file_hash, file_path, datetime.now().timestamp()))
                
                conn.commit()
                return True
                
            except sqlite3.IntegrityError:
                # Duplicate detected, update count
                cursor.execute(f"""
                    UPDATE {TABLE_FILE_HASHES}
                    SET duplicate_count = duplicate_count + 1,
                        duplicate_paths = COALESCE(duplicate_paths || ',', '') || ?
                    WHERE file_hash = ?
                """, (file_path, file_hash))
                
                conn.commit()
                return False
    
    def register_content_hash(
        self,
        content_hash: str,
        file_path: str,
        document_id: str
    ) -> Optional[str]:
        """
        Register content hash
        
        Returns:
            None if new, document_id of primary if duplicate
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            try:
                cursor.execute(f"""
                    INSERT INTO {TABLE_CONTENT_HASHES}
                    (content_hash, first_file_path, first_document_id, first_seen_at, duplicate_count)
                    VALUES (?, ?, ?, ?, 0)
                """, (content_hash, file_path, document_id, datetime.now().timestamp()))
                
                conn.commit()
                return None
                
            except sqlite3.IntegrityError:
                # Content duplicate detected
                cursor.execute(f"""
                    SELECT first_document_id
                    FROM {TABLE_CONTENT_HASHES}
                    WHERE content_hash = ?
                """, (content_hash,))
                
                row = cursor.fetchone()
                primary_doc_id = row['first_document_id'] if row else None
                
                cursor.execute(f"""
                    UPDATE {TABLE_CONTENT_HASHES}
                    SET duplicate_count = duplicate_count + 1
                    WHERE content_hash = ?
                """, (content_hash,))
                
                conn.commit()
                return primary_doc_id
    
    # ========================================================================
    # COMPLETION AND FAILURE TRACKING
    # ========================================================================
    
    def mark_completed(
        self,
        file_id: int,
        file_path: str,
        file_hash: str,
        content_hash: Optional[str],
        document_id: str,
        is_duplicate: bool,
        duplicate_of: Optional[str],
        extraction_time_ms: int,
        indexing_time_ms: int
    ) -> None:
        """Mark file as successfully completed"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute(f"""
                INSERT INTO {TABLE_COMPLETED_FILES}
                (file_id, file_path, file_hash, content_hash, document_id,
                 is_duplicate, duplicate_of, indexed_at, extraction_time_ms, indexing_time_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                file_id, file_path, file_hash, content_hash, document_id,
                is_duplicate, duplicate_of, datetime.now().timestamp(),
                extraction_time_ms, indexing_time_ms
            ))
            
            conn.commit()
    
    def mark_file_completed(self, file_id: int, extraction_time_ms: int = 0, indexing_time_ms: int = 0) -> None:
        """Mark file as completed with optional timing info"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            # Get file info from discovered_files
            cursor.execute(f"""
                SELECT file_path, file_hash
                FROM {TABLE_DISCOVERED_FILES}
                WHERE id = ?
            """, (file_id,))
            
            result = cursor.fetchone()
            if result:
                file_path, file_hash = result
                
                # Insert into completed_files with timing info
                cursor.execute(f"""
                    INSERT OR IGNORE INTO {TABLE_COMPLETED_FILES}
                    (file_id, file_path, file_hash, content_hash, document_id,
                     is_duplicate, duplicate_of, indexed_at, extraction_time_ms, indexing_time_ms)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    file_id, file_path, file_hash, None, '',
                    False, None, datetime.now().timestamp(), extraction_time_ms, indexing_time_ms
                ))
                
                conn.commit()
    
    def reset_stale_processing(
        self,
        table_name: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
        timeout_minutes: Optional[int] = None
    ):
        """Reset stale processing items back to pending
        
        Args:
            table_name: Name of queue table (extraction_queue, indexing_queue, ocr_queue).
                If omitted, resets all queue tables and returns per-table counts.
            timeout_seconds: Items processing longer than this are considered stale.
            timeout_minutes: Optional minute-form timeout for compatibility with Redis API.
            
        Returns:
            Number of items reset (single table mode) OR dict of counts (all-table mode)
        """
        if timeout_seconds is None:
            timeout_seconds = int((timeout_minutes or 30) * 60)

        if table_name is None:
            return {
                'extraction': self.reset_stale_processing(TABLE_EXTRACTION_QUEUE, timeout_seconds),
                'indexing': self.reset_stale_processing(TABLE_INDEXING_QUEUE, timeout_seconds),
                'ocr': self.reset_stale_processing(TABLE_OCR_QUEUE, timeout_seconds),
                'tagging': self.reset_stale_processing(TABLE_TAGGING_QUEUE, timeout_seconds),
            }

        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            cutoff_time = datetime.now().timestamp() - timeout_seconds
            
            cursor.execute(f"""
                UPDATE {table_name}
                SET status = '{QueueStatus.PENDING.value}',
                    worker_id = NULL,
                    claimed_at = NULL
                WHERE status = '{QueueStatus.PROCESSING.value}'
                AND claimed_at < ?
            """, (cutoff_time,))
            
            count = cursor.rowcount
            conn.commit()
            
            if count > 0:
                logger.info(f"Reset {count} stale items in {table_name}")
            
            return count
    
    def mark_failed(
        self,
        file_id: int,
        file_path: str,
        stage: str,
        error_type: ErrorType,
        error_message: str,
        retry_count: int,
        stack_trace: Optional[str] = None
    ) -> None:
        """Mark file as permanently failed"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            cursor.execute(f"""
                INSERT INTO {TABLE_FAILED_FILES}
                (file_id, file_path, stage, error_type, error_message,
                 failed_at, retry_count, stack_trace)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                file_id, file_path, stage, error_type.value, error_message,
                datetime.now().timestamp(), retry_count, stack_trace
            ))
            
            conn.commit()

    def mark_file_failed(
        self,
        file_id: int,
        stage: str,
        error_type: ErrorType,
        error_message: str,
        file_path: str,
        retry_count: int = 0
    ) -> None:
        """Wrapper for compatibility with worker calls"""
        self.mark_failed(
            file_id=file_id,
            file_path=file_path,
            stage=stage,
            error_type=error_type,
            error_message=error_message,
            retry_count=retry_count
        )
    
    # ========================================================================
    # OCR ENGINE PREFERENCE (SQLite fallback — no-ops; Redis is authoritative)
    # ========================================================================

    def set_ocr_engine(self, engine: str) -> None:
        """No-op for SQLite backend; Redis is the authoritative store."""
        pass

    def get_ocr_engine(self) -> str:
        """No-op for SQLite backend; returns '' so factory falls back to config.yaml."""
        return ""

    # ========================================================================
    # STATISTICS AND MONITORING
    # ========================================================================

    def get_queue_statistics(self) -> Dict[str, Any]:
        """Get comprehensive queue statistics"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            stats = {}
            
            # Discovery stats
            cursor.execute(f"""
                SELECT 
                    COUNT(*) as total,
                    SUM(CASE WHEN status = '{QueueStatus.PENDING.value}' THEN 1 ELSE 0 END) as pending,
                    SUM(CASE WHEN status = '{QueueStatus.PROCESSING.value}' THEN 1 ELSE 0 END) as processing,
                    SUM(CASE WHEN status = '{QueueStatus.COMPLETED.value}' THEN 1 ELSE 0 END) as completed,
                    SUM(CASE WHEN status = '{QueueStatus.FAILED.value}' THEN 1 ELSE 0 END) as failed
                FROM {TABLE_DISCOVERED_FILES}
            """)
            stats['discovery'] = dict(cursor.fetchone())
            
            # Extraction queue stats by category
            cursor.execute(f"""
                SELECT 
                    size_category,
                    COUNT(*) as total,
                    SUM(CASE WHEN status = '{QueueStatus.PENDING.value}' THEN 1 ELSE 0 END) as pending,
                    SUM(CASE WHEN status = '{QueueStatus.PROCESSING.value}' THEN 1 ELSE 0 END) as processing,
                    SUM(CASE WHEN status = '{QueueStatus.COMPLETED.value}' THEN 1 ELSE 0 END) as completed
                FROM {TABLE_EXTRACTION_QUEUE}
                GROUP BY size_category
            """)
            stats['extraction'] = {row['size_category']: dict(row) for row in cursor.fetchall()}
            
            # Add extraction totals (aggregate across all categories)
            cursor.execute(f"""
                SELECT 
                    COUNT(*) as total,
                    SUM(CASE WHEN status = '{QueueStatus.PENDING.value}' THEN 1 ELSE 0 END) as pending,
                    SUM(CASE WHEN status = '{QueueStatus.PROCESSING.value}' THEN 1 ELSE 0 END) as processing,
                    SUM(CASE WHEN status = '{QueueStatus.COMPLETED.value}' THEN 1 ELSE 0 END) as completed
                FROM {TABLE_EXTRACTION_QUEUE}
            """)
            stats['extraction_total'] = dict(cursor.fetchone())
            
            # Indexing queue stats
            cursor.execute(f"""
                SELECT 
                    COUNT(*) as total,
                    SUM(CASE WHEN status = '{QueueStatus.PENDING.value}' THEN 1 ELSE 0 END) as pending,
                    SUM(CASE WHEN status = '{QueueStatus.PROCESSING.value}' THEN 1 ELSE 0 END) as processing,
                    SUM(CASE WHEN status = '{QueueStatus.COMPLETED.value}' THEN 1 ELSE 0 END) as completed
                FROM {TABLE_INDEXING_QUEUE}
            """)
            stats['indexing'] = dict(cursor.fetchone())
            
            # OCR queue stats
            cursor.execute(f"""
                SELECT 
                    COUNT(*) as total,
                    SUM(CASE WHEN status = '{QueueStatus.PENDING.value}' THEN 1 ELSE 0 END) as pending,
                    SUM(CASE WHEN status = '{QueueStatus.PROCESSING.value}' THEN 1 ELSE 0 END) as processing,
                    SUM(CASE WHEN status = '{QueueStatus.COMPLETED.value}' THEN 1 ELSE 0 END) as completed
                FROM {TABLE_OCR_QUEUE}
            """)
            stats['ocr'] = dict(cursor.fetchone())

            # Tagging queue stats
            cursor.execute(f"""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN status = '{QueueStatus.PENDING.value}' THEN 1 ELSE 0 END) as pending,
                    SUM(CASE WHEN status = '{QueueStatus.PROCESSING.value}' THEN 1 ELSE 0 END) as processing,
                    SUM(CASE WHEN status = '{QueueStatus.COMPLETED.value}' THEN 1 ELSE 0 END) as completed
                FROM {TABLE_TAGGING_QUEUE}
            """)
            stats['tagging'] = dict(cursor.fetchone())
            
            # Completion stats
            cursor.execute(f"""
                SELECT 
                    COUNT(*) as total_completed,
                    SUM(CASE WHEN is_duplicate = 1 THEN 1 ELSE 0 END) as duplicates,
                    AVG(extraction_time_ms) as avg_extraction_ms,
                    AVG(indexing_time_ms) as avg_indexing_ms
                FROM {TABLE_COMPLETED_FILES}
            """)
            stats['completed'] = dict(cursor.fetchone())
            
            # Failure stats
            cursor.execute(f"""
                SELECT 
                    COUNT(*) as total_failures,
                    error_type,
                    COUNT(*) as count
                FROM {TABLE_FAILED_FILES}
                GROUP BY error_type
            """)
            stats['failures'] = {row['error_type']: row['count'] for row in cursor.fetchall()}
            stats['total_failures'] = sum(stats['failures'].values())
            
            return stats
    
    def get_size_statistics(self) -> Dict[str, Any]:
        """Get file size statistics for dashboard display"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            stats = {}
            
            # Total discovered data size
            cursor.execute(f"""
                SELECT 
                    COUNT(*) as total_files,
                    COALESCE(SUM(file_size), 0) as total_size
                FROM {TABLE_DISCOVERED_FILES}
            """)
            row = cursor.fetchone()
            stats['discovered'] = {
                'files': row['total_files'] or 0,
                'size_bytes': row['total_size'] or 0
            }
            
            # Currently in pipeline (extraction + indexing pending/processing)
            cursor.execute(f"""
                SELECT 
                    COUNT(*) as total_files,
                    COALESCE(SUM(file_size), 0) as total_size
                FROM {TABLE_EXTRACTION_QUEUE}
                WHERE status IN ('{QueueStatus.PENDING.value}', '{QueueStatus.PROCESSING.value}')
            """)
            row = cursor.fetchone()
            extraction_in_progress = {
                'files': row['total_files'] or 0,
                'size_bytes': row['total_size'] or 0
            }
            
            cursor.execute(f"""
                SELECT COUNT(*) as total_files
                FROM {TABLE_INDEXING_QUEUE}
                WHERE status IN ('{QueueStatus.PENDING.value}', '{QueueStatus.PROCESSING.value}')
            """)
            row = cursor.fetchone()
            indexing_in_progress_files = row['total_files'] or 0
            
            # Also compute size for indexing-in-progress by joining to discovered_files
            cursor.execute(f"""
                SELECT COALESCE(SUM(d.file_size), 0) as total_size
                FROM {TABLE_INDEXING_QUEUE} i
                JOIN {TABLE_DISCOVERED_FILES} d ON i.file_id = d.id
                WHERE i.status IN ('{QueueStatus.PENDING.value}', '{QueueStatus.PROCESSING.value}')
            """)
            row = cursor.fetchone()
            indexing_in_progress_size = row['total_size'] or 0

            # OCR in-progress (join to discovered_files for sizes)
            cursor.execute(f"""
                SELECT COUNT(*) as total_files, COALESCE(SUM(d.file_size), 0) as total_size
                FROM {TABLE_OCR_QUEUE} o
                JOIN {TABLE_DISCOVERED_FILES} d ON o.file_id = d.id
                WHERE o.status IN ('{QueueStatus.PENDING.value}', '{QueueStatus.PROCESSING.value}')
            """)
            row = cursor.fetchone()
            ocr_in_progress_files = row['total_files'] or 0
            ocr_in_progress_size = row['total_size'] or 0

            # Tagging in-progress (join to discovered_files for sizes)
            cursor.execute(f"""
                SELECT COUNT(*) as total_files, COALESCE(SUM(d.file_size), 0) as total_size
                FROM {TABLE_TAGGING_QUEUE} t
                JOIN {TABLE_DISCOVERED_FILES} d ON t.file_id = d.id
                WHERE t.status IN ('{QueueStatus.PENDING.value}', '{QueueStatus.PROCESSING.value}')
            """)
            row = cursor.fetchone()
            tagging_in_progress_files = row['total_files'] or 0
            tagging_in_progress_size = row['total_size'] or 0

            stats['in_pipeline'] = {
                'files': extraction_in_progress['files'] + indexing_in_progress_files + ocr_in_progress_files + tagging_in_progress_files,
                'size_bytes': extraction_in_progress['size_bytes'] + indexing_in_progress_size + ocr_in_progress_size + tagging_in_progress_size
            }
            
            # Searchable (completed files in OpenSearch) - from completed_files table
            # Join with discovered_files to get the file sizes
            cursor.execute(f"""
                SELECT 
                    COUNT(*) as total_files,
                    COALESCE(SUM(d.file_size), 0) as total_size
                FROM {TABLE_COMPLETED_FILES} c
                JOIN {TABLE_DISCOVERED_FILES} d ON c.file_id = d.id
                WHERE c.is_duplicate = 0
            """)
            row = cursor.fetchone()
            completed_files = row['total_files'] or 0
            completed_size = row['total_size'] or 0
            
            stats['searchable'] = {
                'files': completed_files,
                'size_bytes': completed_size
            }
            
            # Failed files - join with discovered_files to get sizes
            cursor.execute(f"""
                SELECT 
                    COUNT(*) as total_files,
                    COALESCE(SUM(d.file_size), 0) as total_size
                FROM {TABLE_FAILED_FILES} f
                JOIN {TABLE_DISCOVERED_FILES} d ON f.file_id = d.id
            """)
            row = cursor.fetchone()
            stats['failed'] = {
                'files': row['total_files'] or 0,
                'size_bytes': row['total_size'] or 0
            }
            
            return stats
    
    def get_file_info(self, file_id: int) -> Dict[str, Any]:
        """Get file information by ID"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"""
                SELECT * FROM {TABLE_DISCOVERED_FILES}
                WHERE id = ?
            """, (file_id,))
            
            row = cursor.fetchone()
            return dict(row) if row else {}

    def _apply_schema_migrations(self, conn) -> None:
        """Apply minimal schema migrations to support older database files.

        This function is intentionally conservative: it only adds columns
        when they are missing and uses safe ALTER TABLE ADD COLUMN
        statements with default values so existing rows are valid.
        """
        cursor = conn.cursor()

        def table_has_column(table: str, column: str) -> bool:
            cursor.execute(f"PRAGMA table_info({table})")
            cols = [r['name'] for r in cursor.fetchall()]
            return column in cols

        # Ensure discovered_files has file_size (older DBs may lack it)
        try:
            if not table_has_column(TABLE_DISCOVERED_FILES, 'file_size'):
                logger.info('Migrating: adding file_size to discovered_files')
                cursor.execute(f"ALTER TABLE {TABLE_DISCOVERED_FILES} ADD COLUMN file_size INTEGER NOT NULL DEFAULT 0")

            # Extraction queue should have file_size
            if not table_has_column(TABLE_EXTRACTION_QUEUE, 'file_size'):
                logger.info('Migrating: adding file_size to extraction_queue')
                cursor.execute(f"ALTER TABLE {TABLE_EXTRACTION_QUEUE} ADD COLUMN file_size INTEGER NOT NULL DEFAULT 0")

            # OCR queue: some older code expected created_at; add if missing
            if not table_has_column(TABLE_OCR_QUEUE, 'created_at'):
                logger.info('Migrating: adding created_at to ocr_queue')
                cursor.execute(f"ALTER TABLE {TABLE_OCR_QUEUE} ADD COLUMN created_at REAL DEFAULT 0")

            # Indexing queue: add created_at if missing (defensive)
            if not table_has_column(TABLE_INDEXING_QUEUE, 'created_at'):
                logger.info('Migrating: adding created_at to indexing_queue')
                cursor.execute(f"ALTER TABLE {TABLE_INDEXING_QUEUE} ADD COLUMN created_at REAL DEFAULT 0")

            conn.commit()
        except sqlite3.OperationalError as e:
            logger.error(f"Schema migration operational error: {e}")
        except Exception as e:
            logger.error(f"Unexpected error during schema migration: {e}")
    
    def get_queue_stats(self) -> Dict[str, Dict[str, int]]:
        """Get statistics for all queues"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            
            stats = {}
            
            # Discovery queue stats
            cursor.execute(f"""
                SELECT status, COUNT(*) as count
                FROM {TABLE_DISCOVERED_FILES}
                GROUP BY status
            """)
            discovery_stats = {row['status']: row['count'] for row in cursor.fetchall()}
            stats['discovery'] = discovery_stats
            
            # Extraction queue stats
            cursor.execute(f"""
                SELECT status, COUNT(*) as count
                FROM {TABLE_EXTRACTION_QUEUE}
                GROUP BY status
            """)
            extraction_stats = {row['status']: row['count'] for row in cursor.fetchall()}
            stats['extraction'] = extraction_stats
            
            # Indexing queue stats
            cursor.execute(f"""
                SELECT status, COUNT(*) as count
                FROM {TABLE_INDEXING_QUEUE}
                GROUP BY status
            """)
            indexing_stats = {row['status']: row['count'] for row in cursor.fetchall()}
            stats['indexing'] = indexing_stats
            
            # OCR queue stats
            cursor.execute(f"""
                SELECT status, COUNT(*) as count
                FROM {TABLE_OCR_QUEUE}
                GROUP BY status
            """)
            ocr_stats = {row['status']: row['count'] for row in cursor.fetchall()}
            stats['ocr'] = ocr_stats

            # Tagging queue stats
            cursor.execute(f"""
                SELECT status, COUNT(*) as count
                FROM {TABLE_TAGGING_QUEUE}
                GROUP BY status
            """)
            tagging_stats = {row['status']: row['count'] for row in cursor.fetchall()}
            stats['tagging'] = tagging_stats
            
            return stats
    
    def get_failed_files(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get list of failed files with details"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"""
                SELECT 
                    id, file_id, file_path, stage, error_type, 
                    error_message, failed_at, retry_count
                FROM {TABLE_FAILED_FILES}
                ORDER BY failed_at DESC
                LIMIT ?
            """, (limit,))
            
            return [dict(row) for row in cursor.fetchall()]

    def get_largest_completed_files(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Return the largest completed (non-duplicate) files joined with discovered_files.

        Returns a list of dicts with keys: file_path, file_size, indexed_at
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"""
                SELECT d.file_path as file_path, d.file_size as file_size, c.indexed_at as indexed_at
                FROM {TABLE_COMPLETED_FILES} c
                JOIN {TABLE_DISCOVERED_FILES} d ON c.file_id = d.id
                WHERE c.is_duplicate = 0
                ORDER BY d.file_size DESC
                LIMIT ?
            """, (limit,))

            return [dict(row) for row in cursor.fetchall()]
    
    def get_ocr_pending_files(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get list of files pending OCR processing"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"""
                SELECT 
                    o.id, o.file_id, o.file_path, o.status, o.priority,
                    o.claimed_at
                FROM {TABLE_OCR_QUEUE} o
                WHERE o.status IN ('pending', 'processing')
                ORDER BY o.priority ASC, o.id ASC
                LIMIT ?
            """, (limit,))
            
            return [dict(row) for row in cursor.fetchall()]
    
    def is_file_processed(self, file_hash: str) -> bool:
        """Check if a file hash has already been processed"""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"""
                SELECT COUNT(*) as count FROM {TABLE_DISCOVERED_FILES}
                WHERE file_hash = ?
            """, (file_hash,))
            
            row = cursor.fetchone()
            return row['count'] > 0 if row else False
    
    def reset_discovery_completion_flag(self) -> None:
        """Reset discovery completion flag (for starting fresh discovery)"""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                now = time.time()
                # Set force_run = 1, completion = 0
                cursor.execute(f"""
                    INSERT OR REPLACE INTO {TABLE_SYSTEM_FLAGS} (key, value, updated_at)
                    VALUES (?, ?, ?)
                """, ('discovery_force_run', '1', now))
                
                cursor.execute(f"""
                    INSERT OR REPLACE INTO {TABLE_SYSTEM_FLAGS} (key, value, updated_at)
                    VALUES (?, ?, ?)
                """, ('discovery_complete', '0', now))
                conn.commit()
            logger.info("Discovery completion flag reset (SQLite initialized)")
        except Exception as e:
            logger.error(f"Error resetting discovery flag: {e}")
    
    def is_discovery_complete(self) -> bool:
        """Check if discovery phase is complete"""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                
                # 1. Check Force Run Flag
                cursor.execute(f"SELECT value FROM {TABLE_SYSTEM_FLAGS} WHERE key = 'discovery_force_run'")
                row = cursor.fetchone()
                if row and row['value'] == '1':
                    return False
                
                # 2. Check Explicit Completion Flag
                cursor.execute(f"SELECT value FROM {TABLE_SYSTEM_FLAGS} WHERE key = 'discovery_complete'")
                row = cursor.fetchone()
                if row and row['value'] == '1':
                    return True
            
                # 3. Fallback: Check file counts (Legacy logic)
                cursor.execute(f"SELECT COUNT(*) as count FROM {TABLE_DISCOVERED_FILES}")
                row = cursor.fetchone()
                total_files = row['count'] if row else 0
                
                if total_files == 0:
                    return False
                
                cursor.execute(f"""
                    SELECT COUNT(*) as count FROM {TABLE_DISCOVERED_FILES}
                    WHERE status = '{QueueStatus.PENDING.value}'
                """)
                row = cursor.fetchone()
                pending_count = row['count'] if row else 0
                
                return pending_count == 0
        except Exception as e:
            logger.error(f"Error checking discovery completion: {e}")
            return False

    def check_file_exists(self, file_path: str, file_size: int, last_modified: float) -> bool:
        """
        Check if file already exists with same metadata (path, size, mtime)
        """
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(f"""
                    SELECT file_size, last_modified 
                    FROM {TABLE_DISCOVERED_FILES} 
                    WHERE file_path = ?
                """, (file_path,))
                
                row = cursor.fetchone()
                if not row:
                    return False
                
                stored_size = row['file_size']
                stored_mtime = row['last_modified']
                
                if stored_size != file_size:
                    return False
                    
                if abs(stored_mtime - last_modified) > 0.001:
                    return False
                    
                return True
                
        except Exception as e:
            logger.error(f"Error checking file existence in SQLite: {e}")
            return False
    
    def mark_discovery_complete(self) -> None:
        """Mark discovery phase as complete"""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                now = time.time()
                # Set completion = 1, clear force_run
                cursor.execute(f"""
                    INSERT OR REPLACE INTO {TABLE_SYSTEM_FLAGS} (key, value, updated_at)
                    VALUES (?, ?, ?)
                """, ('discovery_complete', '1', now))
                
                cursor.execute(f"DELETE FROM {TABLE_SYSTEM_FLAGS} WHERE key = 'discovery_force_run'")
                conn.commit()
            logger.info("Discovery marked as complete")
        except Exception as e:
            logger.error(f"Error marking discovery complete: {e}")
    
    def update_worker_heartbeat(self, worker_id: str) -> None:
        """Update heartbeat timestamp for a worker"""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(f"""
                    INSERT OR REPLACE INTO {TABLE_WORKER_HEARTBEATS} (worker_id, last_heartbeat)
                    VALUES (?, ?)
                """, (worker_id, datetime.now().timestamp()))
                conn.commit()
        except Exception as e:
            logger.error(f"Error updating heartbeat for {worker_id}: {e}")

    def get_worker_heartbeats(self) -> Dict[str, float]:
        """Get all worker heartbeats"""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(f"SELECT worker_id, last_heartbeat FROM {TABLE_WORKER_HEARTBEATS}")
                return {row['worker_id']: row['last_heartbeat'] for row in cursor.fetchall()}
        except Exception as e:
            logger.error(f"Error getting worker heartbeats: {e}")
            return {}

    def remove_worker_heartbeat(self, worker_id: str) -> None:
        """Remove heartbeat record for a worker"""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(f"DELETE FROM {TABLE_WORKER_HEARTBEATS} WHERE worker_id = ?", (worker_id,))
                conn.commit()
        except Exception as e:
            logger.error(f"Error removing heartbeat for {worker_id}: {e}")

    def close(self) -> None:
        """Close database connections"""
        if hasattr(self._local, 'connection') and self._local.connection:
            self._local.connection.close()
            self._local.connection = None


# Singleton instance with thread safety
_queue_manager: Optional[QueueManager] = None
_queue_manager_lock = threading.Lock()
_using_redis: bool = False


def get_queue_manager():
    """Get singleton queue manager instance (thread-safe)
    
    Uses Redis if available, falls back to SQLite.
    If SQLite was used initially and Redis becomes available later,
    can sync queues using sync_sqlite_to_redis().
    """
    global _queue_manager
    global _using_redis
    
    if _queue_manager is None:
        with _queue_manager_lock:
            # Double-check locking pattern
            if _queue_manager is None:
                # Try Redis first with explicit connection test
                try:
                    redis_qm = RedisQueueManager()
                    # Test connection explicitly
                    redis_qm.client.ping()
                    _queue_manager = redis_qm
                    _using_redis = True
                    logger.info("Using Redis queue manager (connection verified)")
                except Exception as e:
                    logger.error(f"Redis unavailable: {e}")
                    # STRICT MODE: Fail if Redis is not available (User Request)
                    # logger.warning(f"Redis unavailable, using SQLite: {e}")
                    # _queue_manager = QueueManager()
                    # _using_redis = False
                    raise ConnectionError(f"Redis backend is mandatory but unavailable: {e}")
    
    return _queue_manager


def is_using_redis() -> bool:
    """Check if currently using Redis backend"""
    global _using_redis
    return _using_redis


def try_switch_to_redis() -> bool:
    """Try to switch from SQLite to Redis if Redis becomes available.
    
    Returns:
        True if successfully switched to Redis, False otherwise
    """
    global _queue_manager
    global _using_redis
    
    if _using_redis:
        return True  # Already using Redis
    
    with _queue_manager_lock:
        try:
            redis_qm = RedisQueueManager()
            redis_qm.client.ping()
            
            # Redis is now available - sync data from SQLite if needed
            if _queue_manager is not None and isinstance(_queue_manager, QueueManager):
                sqlite_qm = _queue_manager
                synced = sync_sqlite_to_redis(sqlite_qm, redis_qm)
                if synced > 0:
                    logger.info(f"Synced {synced} items from SQLite to Redis")
            
            _queue_manager = redis_qm
            _using_redis = True
            logger.info("Switched to Redis queue manager")
            return True
            
        except Exception as e:
            logger.debug(f"Redis still unavailable: {e}")
            return False


def sync_sqlite_to_redis(sqlite_qm: 'QueueManager', redis_qm: 'RedisQueueManager') -> int:
    """Sync pending items from SQLite to Redis.
    
    Migrates pending extraction queue items from SQLite to Redis.
    Used when Redis becomes available after initial SQLite fallback.
    
    Returns:
        Number of items synced
    """
    synced = 0
    try:
        # Get pending items from SQLite extraction queue
        with sqlite_qm._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"""
                SELECT id, file_id, file_path, file_size, size_category, priority
                FROM {TABLE_EXTRACTION_QUEUE}
                WHERE status = '{QueueStatus.PENDING.value}'
                ORDER BY priority ASC, id ASC
            """)
            
            rows = cursor.fetchall()
            
            if not rows:
                logger.info("No pending items in SQLite to sync to Redis")
                return 0
            
            logger.info(f"Syncing {len(rows)} pending extraction items from SQLite to Redis...")
            
            # Add each item to Redis
            synced_ids: List[int] = []
            for row in rows:
                try:
                    size_cat = SizeCategory(row['size_category'])
                    priority = Priority(row['priority'])
                    
                    redis_qm.add_to_extraction_queue(
                        file_id=row['file_id'],
                        file_path=row['file_path'],
                        file_size=row['file_size'],
                        size_category=size_cat,
                        priority=priority
                    )
                    synced += 1
                    synced_ids.append(row['id'])
                    
                except Exception as e:
                    logger.warning(f"Failed to sync item {row['id']}: {e}")
            
            # Mark synced items in SQLite as processing (to avoid re-sync)
            if synced_ids:
                placeholders = ','.join('?' * len(synced_ids))
                cursor.execute(f"""
                    UPDATE {TABLE_EXTRACTION_QUEUE}
                    SET status = 'synced_to_redis'
                    WHERE id IN ({placeholders})
                """, synced_ids)
                conn.commit()
            
            logger.info(f"Successfully synced {synced} items from SQLite to Redis")
            
    except Exception as e:
        logger.error(f"Error syncing SQLite to Redis: {e}")
    
    return synced


def reset_queue_manager() -> None:
    """Reset the singleton queue manager instance (call after database reset)"""
    global _queue_manager
    global _using_redis
    
    with _queue_manager_lock:
        if _queue_manager is not None:
            try:
                _queue_manager.close()
            except Exception:
                pass
            _queue_manager = None
            _using_redis = False
