"""
Enterprise Document Search System - Redis Queue Manager
High-performance queue with Redis for multi-worker concurrency
No more 'database is locked' errors!
"""

import redis
import json
import threading
import time
import random
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime
import hashlib

from core.config_manager import get_config
from core.constants import (
    QueueStatus, SizeCategory, Priority, ErrorType
)
from core.logging_manager import get_logger


logger = get_logger("redis_queue_manager")


class RedisQueueManager:
    """
    High-performance queue manager using Redis
    Handles concurrent worker access without locking issues
    
    Uses Redis data structures:
    - Lists for FIFO queues (LPUSH/BRPOP)
    - Sorted Sets for priority queues
    - Hashes for metadata storage
    - Sets for deduplication
    """
    
    # Redis key prefixes
    PREFIX = "docsearch:"
    QUEUE_DISCOVERY = f"{PREFIX}queue:discovery"
    QUEUE_EXTRACTION = f"{PREFIX}queue:extraction"  # Base, extended with size category
    QUEUE_INDEXING = f"{PREFIX}queue:indexing"
    QUEUE_OCR = f"{PREFIX}queue:ocr"
    QUEUE_TAGGING = f"{PREFIX}queue:tagging"
    QUEUE_FOLDERS = f"{PREFIX}queue:folders"  # Queue for directory scanning
    
    PROCESSING_EXTRACTION = f"{PREFIX}processing:extraction"
    PROCESSING_INDEXING = f"{PREFIX}processing:indexing"
    PROCESSING_OCR = f"{PREFIX}processing:ocr"
    PROCESSING_TAGGING = f"{PREFIX}processing:tagging"
    
    # Counter for extraction completed files (not yet indexed)
    COUNTER_EXTRACTION_COMPLETED = f"{PREFIX}counter:extraction_completed"
    # Counter for total discovered files
    COUNTER_DISCOVERED = f"{PREFIX}counter:discovered"
    # Counter for discovered bytes
    COUNTER_DISCOVERED_BYTES = f"{PREFIX}counter:discovered_bytes"
    # Counter for fully completed (indexed) files
    COUNTER_COMPLETED = f"{PREFIX}counter:completed"
    # Counter for completed bytes (indexed size)
    COUNTER_COMPLETED_BYTES = f"{PREFIX}counter:completed_bytes"
    COUNTER_TAGGING_COMPLETED = f"{PREFIX}counter:tagging_completed"
    
    # New counters for Root File tracking (deduplicated by file_id)
    SET_COMPLETED_FILE_IDS = f"{PREFIX}completed_file_ids"
    COUNTER_ROOT_COMPLETED = f"{PREFIX}counter:root_completed"

    # Counters for completed stats aggregation
    COUNTER_COMPLETED_EXTRACT_MS = f"{PREFIX}counter:completed_extract_ms"
    COUNTER_COMPLETED_INDEX_MS = f"{PREFIX}counter:completed_index_ms"
    COUNTER_DUPLICATES = f"{PREFIX}counter:duplicates"
    COUNTER_OCR_COMPLETED = f"{PREFIX}counter:ocr_completed"
    
    HASH_FILES = f"{PREFIX}files"  # Hash: file_id -> file metadata
    HASH_COMPLETED = f"{PREFIX}completed"  # Hash: file_hash -> completion data
    HASH_FAILED = f"{PREFIX}failed"  # Hash: file_id -> failure data
    HASH_FAILURE_COUNTS = f"{PREFIX}failure_counts"  # Hash: error_type -> count (O(1) breakdown)
    ZSET_COMPLETED_BY_SIZE = f"{PREFIX}completed_by_size"  # ZSet: file_hash -> file_size (for top-N)
    HASH_FOLDER_META = f"{PREFIX}folder_meta"  # Hash: folder_path -> mtime_stats
    HASH_FILE_PATHS = f"{PREFIX}file_paths"    # Hash: file_path -> file_id
    
    SET_FILE_HASHES = f"{PREFIX}file_hashes"  # Set of known file hashes
    SET_CONTENT_HASHES = f"{PREFIX}content_hashes"  # Set of known content hashes
    
    COUNTER_FILE_ID = f"{PREFIX}counter:file_id"
    HASH_WORKER_HEARTBEATS = f"{PREFIX}worker_heartbeats"
    HASH_PARENT_MAP = f"{PREFIX}parent_map"
    DISCOVERY_COMPLETED_FLAG = f"{PREFIX}discovery:completed"
    DISCOVERY_FORCE_RUN_FLAG = f"{PREFIX}discovery:force_run"
    DISCOVERY_ROOT_INITIALIZED_KEY = f"{PREFIX}discovery:root_initialized"
    
    def __init__(self, redis_url: str = None):
        """
        Initialize Redis queue manager
        
        Args:
            redis_url: Redis connection URL (default: redis://localhost:6379/0)
        """
        config = get_config()
        
        # Get Redis URL from config or use default
        if redis_url is None:
            redis_config = getattr(config, 'redis', None)
            if redis_config:
                redis_url = getattr(redis_config, 'url', 'redis://localhost:6379/0')
            else:
                redis_url = 'redis://localhost:6379/0'
        
        self.redis_url = redis_url

        redis_config = getattr(config, "redis", None)
        max_connections = int(getattr(redis_config, "max_connections", 50) or 50)
        socket_timeout = int(getattr(redis_config, "timeout", 5) or 5)
        
        # Create connection pool for thread safety
        self.pool = redis.ConnectionPool.from_url(
            redis_url,
            max_connections=max(10, max_connections),
            decode_responses=True,
            protocol=2,
            socket_connect_timeout=3,
            socket_timeout=max(2, socket_timeout),
            health_check_interval=30,
            retry_on_timeout=True
        )
        
        self._local = threading.local()
        self._lock = threading.RLock()
        
        # Cache for OCR count to avoid slow OpenSearch queries
        self._ocr_count_cache = {'value': 0, 'timestamp': 0, 'ttl': 60}
        
        # Cache for worker processing keys (avoids 600K+ key SCAN)
        self._worker_keys_cache: Dict[str, list] = {}
        self._worker_keys_ts: Dict[str, float] = {}
        
        # Register Lua scripts for atomic operations
        self._register_scripts()
        
        logger.info(f"Redis queue manager initialized: {redis_url}")

    def _register_scripts(self):
        """Register Lua scripts for atomic operations"""
        # Script to atomically add file ID to set and increment counters if new
        # KEYS[1] = set_key (SET_COMPLETED_FILE_IDS)
        # KEYS[2] = count_key (COUNTER_ROOT_COMPLETED)
        # KEYS[3] = size_key (COUNTER_COMPLETED_BYTES)
        # ARGV[1] = file_id
        # ARGV[2] = file_size
        # Use valid client to register script
        self.lua_add_root_completion = self.client.register_script("""
            local set_key = KEYS[1]
            local count_key = KEYS[2]
            local size_key = KEYS[3]
            local file_id = ARGV[1]
            local file_size = tonumber(ARGV[2])

            if redis.call('SADD', set_key, file_id) == 1 then
                redis.call('INCR', count_key)
                if file_size > 0 then
                    redis.call('INCRBY', size_key, file_size)
                end
                return 1 -- New root file
            else
                return 0 -- Already counted
            end
        """)

        self.lua_fallback_ocr_completed = self.client.register_script("""
            local key_last_proc = KEYS[1]
            local key_last_fail = KEYS[2]
            local key_ocr_completed = KEYS[3]
            local key_failure_counts = KEYS[4]
            local current_proc = tonumber(ARGV[1])

            local increment = 0

            local val_fail = redis.call('HGET', key_failure_counts, 'ocr_error')
            local current_fail = 0
            if val_fail then
                current_fail = tonumber(val_fail) or 0
            end

            local last_proc_val = redis.call('GET', key_last_proc)
            local last_fail_val = redis.call('GET', key_last_fail)

            if last_proc_val then
                local last_proc = tonumber(last_proc_val)
                local last_fail = tonumber(last_fail_val or "0")

                local delta_proc = last_proc - current_proc
                local delta_fail = current_fail - last_fail

                if delta_proc > 0 then
                    local inc = delta_proc - math.max(0, delta_fail)
                    if inc > 0 then
                        redis.call('INCRBY', key_ocr_completed, inc)
                        increment = inc
                    end
                end
            end

            redis.call('SET', key_last_proc, current_proc)
            redis.call('SET', key_last_fail, current_fail)

            return increment
        """)
    
    @property
    def client(self) -> redis.Redis:
        """Get thread-local Redis client with connection health check."""
        if not hasattr(self._local, 'client') or self._local.client is None:
            self._local.client = redis.Redis(connection_pool=self.pool)
        else:
            # Health check: detect broken connections and recreate
            try:
                self._local.client.ping()
            except Exception:
                self._local.client = redis.Redis(connection_pool=self.pool)
        return self._local.client
    
    def _get_extraction_processing_keys(self) -> List[str]:
        """Get all possible extraction processing keys (O(1) - no scan).
        Derives max worker count from config to avoid hardcoded limits."""
        config = get_config()
        max_per_type = max(int(getattr(config.extraction, 'total_workers', 50) or 50), 50)
        keys = []
        for worker_type in ['fast', 'std', 'heavy', 'extreme']:
            for i in range(1, max_per_type + 1):
                keys.append(f"{self.PROCESSING_EXTRACTION}:extraction-{worker_type}-{i}")
        return keys
    
    def _get_indexing_processing_keys(self) -> List[str]:
        """Get all possible indexing processing keys (O(1) - no scan).
        Derives max worker count from config to avoid hardcoded limits."""
        config = get_config()
        max_workers = max(int(getattr(config.indexing, 'num_workers', 20) or 20), 20)
        keys = []
        for i in range(1, max_workers + 1):
            keys.append(f"{self.PROCESSING_INDEXING}:indexing-{i}")
        return keys
    
    def _get_ocr_processing_keys(self) -> List[str]:
        """Get all possible OCR processing keys (O(1) - no scan).
        Derives max worker count from config to avoid hardcoded limits."""
        config = get_config()
        max_workers = max(
            int(getattr(config.ocr, 'initial_workers', 16) or 16) +
            int(getattr(config.ocr, 'post_indexing_workers', 16) or 16),
            35
        )
        keys = []
        for i in range(1, max_workers + 1):
            keys.append(f"{self.PROCESSING_OCR}:ocr-{i}")
        return keys

    def _get_tagging_processing_keys(self) -> List[str]:
        """Get all possible tagging processing keys (O(1) - no scan).
        Derives max worker count from config to avoid hardcoded limits."""
        config = get_config()
        max_workers = max(int(getattr(config.tagging, 'workers', 35) or 35), 35)
        keys = []
        for i in range(1, max_workers + 1):
            keys.append(f"{self.PROCESSING_TAGGING}:tagging-{i}")
        return keys
    
    def reset_database(self) -> None:
        """
        Reset all queues by flushing Redis keys
        WARNING: This deletes all data!
        """
        try:
            # Use SCAN instead of KEYS for better performance (O(1) vs O(N))
            cursor = 0
            keys_to_delete = []
            while True:
                cursor, found_keys = self.client.scan(
                    cursor=cursor,
                    match=f"{self.PREFIX}*",
                    count=1000
                )
                keys_to_delete.extend(found_keys)
                if cursor == 0:
                    break
            
            if keys_to_delete:
                self.client.delete(*keys_to_delete)
            logger.info("Redis queues reset complete")
        except Exception as e:
            logger.error(f"Error resetting Redis: {e}")
            raise
    
    def _generate_file_id(self) -> int:
        """Generate unique file ID using Redis INCR"""
        return self.client.incr(self.COUNTER_FILE_ID)
    
    def _zpopmin_compat(self, key: str, count: int = 1) -> List[tuple]:
        """
        Pop lowest-score items from sorted set.

        Fast path: use native ZPOPMIN (atomic, low contention).
        Fallback: Redis 3.x-compatible WATCH + ZRANGE + ZREM loop.
        Returns list of (value, score) tuples.
        """
        if count <= 0:
            return []

        # Native Redis command (Redis >= 5) dramatically reduces contention.
        try:
            raw = self.client.execute_command("ZPOPMIN", key, count)
            if not raw:
                return []

            items: List[tuple] = []
            # redis-py may return either [[member, score], ...] or flat [member, score, ...]
            if isinstance(raw, list) and raw and isinstance(raw[0], (list, tuple)):
                for member, score in raw:
                    items.append((member, float(score)))
            elif isinstance(raw, list):
                for i in range(0, len(raw), 2):
                    member = raw[i]
                    score = raw[i + 1] if i + 1 < len(raw) else 0
                    items.append((member, float(score)))

            return items
        except redis.ResponseError as e:
            # Older Redis without ZPOPMIN - use fallback below.
            if "unknown command" not in str(e).lower():
                logger.error(f"ZPOPMIN failed for key {key}: {e}")
                return []
        except (redis.TimeoutError, redis.ConnectionError) as e:
            logger.warning(f"ZPOPMIN timeout/connection issue for key {key}: {e}")
            return []
        except Exception as e:
            logger.error(f"Error in ZPOPMIN for key {key}: {e}")
            return []

        max_attempts = 3
        for _ in range(max_attempts):
            pipe = self.client.pipeline(True)
            try:
                # Use one watched pipeline connection for both read+delete.
                pipe.watch(key)
                items = pipe.zrange(key, 0, count - 1, withscores=True)

                if not items:
                    pipe.unwatch()
                    return []

                pipe.multi()
                for item, _score in items:
                    pipe.zrem(key, item)
                pipe.execute()
                return items
            except redis.WatchError:
                time.sleep(random.uniform(0.01, 0.05))
                continue
            except (redis.TimeoutError, redis.ConnectionError) as e:
                logger.warning(f"Fallback zpopmin timeout/connection issue for key {key}: {e}")
                return []
            except Exception as e:
                logger.error(f"Error in zpopmin_compat: {e}")
                return []
            finally:
                try:
                    pipe.reset()
                except Exception:
                    pass

        logger.debug(f"zpopmin_compat: contention exceeded retry limit for key {key}")
        return []
    
    # ========================================================================
    # DISCOVERY OPERATIONS
    # ========================================================================
    
    def check_file_exists(self, file_path: str, file_size: int, last_modified: float) -> bool:
        """
        Check if file already exists with same metadata (path, size, mtime)
        Used to skip re-hashing of unchanged files.
        """
        try:
            # 1. Get file_id from path
            file_id = self.client.hget(self.HASH_FILE_PATHS, file_path)
            if not file_id:
                return False
            
            # 2. Get metadata from per-file hash key
            # Data is stored as HMSET at f"{HASH_FILES}:{file_id}", NOT as
            # a single top-level hash. Use hgetall to fetch the full dict.
            if isinstance(file_id, bytes):
                file_id = file_id.decode('utf-8')
                
            metadata = self.client.hgetall(f"{self.HASH_FILES}:{file_id}")
            if not metadata:
                return False
            
            # 3. Compare size and mtime (allow small float diff for mtime)
            # stored mtime might be slightly different due to float precision serialization
            stored_mtime = float(metadata.get('last_modified', 0))
            stored_size = int(metadata.get('file_size', 0))
            
            if stored_size != file_size:
                return False
                
            if abs(stored_mtime - last_modified) > 0.001:
                return False
                
            return True
            
        except Exception as e:
            logger.error(f"Error checking file existence in Redis: {e}")
            return False

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
        try:
            # Use SADD as atomic duplicate guard — returns 1 if newly added, 0 if exists
            # This prevents the race condition where two workers both pass sismember
            # before either's pipeline executes sadd
            if not self.client.sadd(self.SET_FILE_HASHES, file_hash):
                return None  # Duplicate — another worker already added this hash
            
            # Generate file ID
            file_id = self._generate_file_id()
            
            # Store file metadata
            file_data = {
                'id': file_id,
                'file_path': file_path,
                'file_name': file_name,
                'file_size': file_size,
                'file_extension': file_extension,
                'file_hash': file_hash,
                'last_modified': last_modified,
                'created': created,
                'size_category': size_category.value,
                'priority': priority.value,
                'status': QueueStatus.PENDING.value,
                'discovered_at': datetime.now().timestamp()
            }
            
            # Use pipeline for atomic operations (hash already in SET_FILE_HASHES)
            pipe = self.client.pipeline()
            pipe.hmset(f"{self.HASH_FILES}:{file_id}", file_data)
            pipe.zadd(self.QUEUE_DISCOVERY, {str(file_id): priority.value})
            # Store path -> file_id mapping for check_file_exists
            pipe.hset(self.HASH_FILE_PATHS, file_path, file_id)
            # Increment discovered counters
            pipe.incr(self.COUNTER_DISCOVERED)
            pipe.incrby(self.COUNTER_DISCOVERED_BYTES, file_size)
            pipe.execute()
            
            return file_id
            
        except Exception as e:
            logger.error(f"Error adding discovered file: {e}")
            return None
    
    def add_discovered_files_batch(self, files: List[Dict[str, Any]]) -> int:
        """
        Add multiple discovered files in batch
        
        Args:
            files: List of file dictionaries
        
        Returns:
            Number of files added
        """
        inserted = 0
        total_bytes_added = 0
        current_time = datetime.now().timestamp()
        
        try:
            pipe = self.client.pipeline()
            
            for file_data in files:
                file_hash = file_data['file_hash']
                
                # Use SADD as atomic duplicate guard — returns 1 if newly added, 0 if exists
                # Prevents race condition where sismember passes for two workers
                if not self.client.sadd(self.SET_FILE_HASHES, file_hash):
                    continue
                
                file_id = self._generate_file_id()
                priority = file_data.get('priority', Priority.NORMAL.value)
                
                metadata = {
                    'id': file_id,
                    'file_path': file_data['file_path'],
                    'file_name': file_data['file_name'],
                    'file_size': file_data['file_size'],
                    'file_extension': file_data.get('file_extension', ''),
                    'file_hash': file_hash,
                    'last_modified': file_data['last_modified'],
                    'created': file_data.get('created', current_time),
                    'size_category': file_data['size_category'],
                    'priority': priority,
                    'status': QueueStatus.PENDING.value,
                    'discovered_at': current_time
                }
                
                pipe.hmset(f"{self.HASH_FILES}:{file_id}", metadata)
                pipe.zadd(self.QUEUE_DISCOVERY, {str(file_id): priority})
                # Store path -> file_id mapping for check_file_exists
                pipe.hset(self.HASH_FILE_PATHS, file_data['file_path'], file_id)
                inserted += 1
                total_bytes_added += file_data.get('file_size', 0)
            
            # Increment discovered counters for the batch
            if inserted > 0:
                pipe.incrby(self.COUNTER_DISCOVERED, inserted)
                if total_bytes_added > 0:
                    pipe.incrby(self.COUNTER_DISCOVERED_BYTES, total_bytes_added)
            
            pipe.execute()
            return inserted
            
        except Exception as e:
            logger.error(f"Error in batch add: {e}")
            return inserted
    
    def check_file_hash_exists(self, file_hash: str) -> Optional[Tuple[int, str]]:
        """
        Check if file hash exists in completed files
        
        Returns:
            (file_id, file_path) if exists, None otherwise
        """
        try:
            data = self.client.hget(self.HASH_COMPLETED, file_hash)
            if data:
                info = json.loads(data)
                return (info['file_id'], info['file_path'])
            return None
        except Exception as e:
            logger.error(f"Error checking file hash: {e}")
            return None
    
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
        try:
            queue_key = f"{self.QUEUE_EXTRACTION}:{size_category.value}"
            
            item = {
                'id': file_id,  # Use file_id as queue item id
                'file_id': file_id,
                'file_path': file_path,
                'file_size': file_size,
                'size_category': size_category.value,
                'priority': priority.value,
                'status': QueueStatus.PENDING.value,
                'added_at': datetime.now().timestamp()
            }
            
            # Add to priority queue (sorted set)
            self.client.zadd(queue_key, {json.dumps(item): priority.value})
            
            # Remove from discovery queue (it's now in extraction)
            self.client.zrem(self.QUEUE_DISCOVERY, str(file_id))
            
            return file_id
            
        except Exception as e:
            logger.error(f"Error adding to extraction queue: {e}")
            return -1
    
    def claim_extraction_work(
        self,
        size_category: SizeCategory,
        worker_id: str,
        batch_size: int = 1
    ) -> List[Dict[str, Any]]:
        """
        Atomically claim work from extraction queue using ZPOPMIN
        
        Priority ordering: Priority 1 (highest) = lowest score, so ZPOPMIN is correct.
        ZPOPMIN returns items with lowest scores first, which are highest priority.
        
        Args:
            size_category: Which queue to pull from
            worker_id: Worker claiming the work
            batch_size: Number of files to claim
        
        Returns:
            List of claimed work items (highest priority first)
        """
        try:
            queue_key = f"{self.QUEUE_EXTRACTION}:{size_category.value}"
            processing_key = f"{self.PROCESSING_EXTRACTION}:{worker_id}"
            
            # Use compatible zpopmin for Redis 3.x
            items = self._zpopmin_compat(queue_key, batch_size)
            
            if not items:
                return []
            
            claimed = []
            current_time = datetime.now().timestamp()
            
            for item_json, score in items:
                item = json.loads(item_json)
                item['claimed_at'] = current_time
                item['worker_id'] = worker_id
                item['status'] = QueueStatus.PROCESSING.value
                
                # Track in processing set with generous timeout (large files may take 30+ min)
                self.client.hset(processing_key, str(item['file_id']), json.dumps(item))
                self.client.expire(processing_key, 1800)  # 30 minute timeout
                
                claimed.append(item)
            
            return claimed
            
        except Exception as e:
            logger.error(f"Error claiming extraction work: {e}")
            return []
    
    def complete_extraction(self, queue_id: int, processing_time_ms: int, worker_id: str = None) -> None:
        """Mark extraction as complete"""
        try:
            # Remove from processing set - if worker_id provided, delete directly (O(1))
            # Otherwise fall back to deleting from all known processing keys
            if worker_id:
                processing_key = f"{self.PROCESSING_EXTRACTION}:{worker_id}"
                self.client.hdel(processing_key, str(queue_id))
            else:
                # Fallback: scan all worker keys (expensive). Callers should always pass worker_id.
                logger.warning(f"complete_extraction called without worker_id for queue_id={queue_id}. "
                               "This triggers brute-force cleanup. Ensure callers pass worker_id.")
                for key in self._get_extraction_processing_keys():
                    self.client.hdel(key, str(queue_id))
            
            # Update file metadata
            file_key = f"{self.HASH_FILES}:{queue_id}"
            size_category = self.client.hget(file_key, 'size_category')
            
            pipe = self.client.pipeline()
            pipe.hset(file_key, 'extraction_time_ms', processing_time_ms)
            pipe.hset(file_key, 'extraction_completed_at', datetime.now().timestamp())
            pipe.hset(file_key, 'status', 'extracted')  # Update status to extracted
            
            # Increment extraction completed counter
            pipe.incr(self.COUNTER_EXTRACTION_COMPLETED)
            
            # Increment per-category completed counter
            if size_category:
                pipe.incr(f"{self.COUNTER_EXTRACTION_COMPLETED}:{size_category}")
                
            pipe.execute()
            
            logger.debug(f"Completed extraction for file {queue_id} in {processing_time_ms}ms")
            
        except Exception as e:
            logger.error(f"Error completing extraction: {e}")
    
    def get_extraction_queue_size(self, size_category: SizeCategory = None) -> int:
        """Get pending extraction queue size
        
        Args:
            size_category: Specific category to check, or None for all categories
        
        Returns:
            Number of pending files in extraction queue(s)
        """
        try:
            if size_category:
                queue_key = f"{self.QUEUE_EXTRACTION}:{size_category.value}"
                return self.client.zcard(queue_key)
            else:
                # Sum all extraction queues
                total = 0
                for cat in ['tiny', 'small', 'medium', 'large']:
                    queue_key = f"{self.QUEUE_EXTRACTION}:{cat}"
                    total += self.client.zcard(queue_key)
                return total
        except Exception as e:
            logger.error(f"Error getting extraction queue size: {e}")
            return 0
    
    # ========================================================================
    # INDEXING QUEUE OPERATIONS
    # ========================================================================
    
    def add_to_indexing_queue(
        self,
        file_id: int,
        document_json: str
    ) -> int:
        """Add document to indexing queue"""
        try:
            item = {
                'id': file_id,
                'file_id': file_id,
                'document_json': document_json,
                'status': QueueStatus.PENDING.value,
                'added_at': datetime.now().timestamp()
            }
            
            # Use list for FIFO queue (fast LPUSH/BRPOP)
            self.client.lpush(self.QUEUE_INDEXING, json.dumps(item))
            
            return file_id
            
        except Exception as e:
            logger.error(f"Error adding to indexing queue: {e}")
            return -1
    
    def claim_indexing_work(
        self,
        worker_id: str,
        batch_size: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Atomically claim work from indexing queue
        
        Uses LRANGE + LTRIM for batch claiming (atomic with MULTI/EXEC)
        """
        try:
            processing_key = f"{self.PROCESSING_INDEXING}:{worker_id}"
            
            # Use MULTI/EXEC for atomic batch pop (prevents duplicate claiming)
            pipe = self.client.pipeline(transaction=True)
            pipe.lrange(self.QUEUE_INDEXING, -batch_size, -1)
            pipe.ltrim(self.QUEUE_INDEXING, 0, -(batch_size + 1))
            results = pipe.execute()
            
            items_json = results[0]
            
            if not items_json:
                return []
            
            claimed = []
            current_time = datetime.now().timestamp()
            
            for item_json in items_json:
                item = json.loads(item_json)
                item['claimed_at'] = current_time
                item['worker_id'] = worker_id
                item['status'] = QueueStatus.PROCESSING.value
                updated_item_json = json.dumps(item)
                
                # Track in processing hash with generous timeout
                self.client.hset(processing_key, str(item['file_id']), updated_item_json)
                self.client.expire(processing_key, 1800)  # 30 minute timeout
                
                claimed.append(item)
            
            return claimed
            
        except Exception as e:
            logger.error(f"Error claiming indexing work: {e}")
            return []
    
    def complete_indexing(self, file_ids: List[int], worker_id: str = None) -> None:
        """Mark indexing as complete for batch of files"""
        try:
            # Remove from processing set - use worker_id if provided for O(1) delete
            if worker_id:
                processing_key = f"{self.PROCESSING_INDEXING}:{worker_id}"
                for file_id in file_ids:
                    self.client.hdel(processing_key, str(file_id))
            else:
                # Fallback: scan all worker keys (expensive). Callers should always pass worker_id.
                logger.warning("complete_indexing called without worker_id. "
                               "This triggers brute-force cleanup. Ensure callers pass worker_id.")
                for key in self._get_indexing_processing_keys():
                    for file_id in file_ids:
                        self.client.hdel(key, str(file_id))
            
            current_time = datetime.now().timestamp()
            
            # Update file metadata
            pipe = self.client.pipeline()
            for file_id in file_ids:
                file_key = f"{self.HASH_FILES}:{file_id}"
                pipe.hset(file_key, 'indexed_at', current_time)
                pipe.hset(file_key, 'status', QueueStatus.COMPLETED.value)
            pipe.execute()
            
        except Exception as e:
            logger.error(f"Error completing indexing: {e}")
    
    # ========================================================================
    # OCR QUEUE OPERATIONS
    # ========================================================================
    
    def add_to_ocr_queue(
        self,
        file_id: int,
        file_path: str,
        priority: Priority,
        document_id: str = None
    ) -> int:
        """Add file to OCR queue"""
        try:
            item = {
                'id': file_id,
                'file_id': file_id,
                'file_path': file_path,
                'priority': priority.value,
                'document_id': document_id,
                'status': QueueStatus.PENDING.value,
                'added_at': datetime.now().timestamp()
            }
            
            # Priority queue using sorted set
            self.client.zadd(self.QUEUE_OCR, {json.dumps(item): priority.value})
            
            return file_id
            
        except Exception as e:
            logger.error(f"Error adding to OCR queue: {e}")
            return -1
    
    def claim_ocr_work(
        self,
        worker_id: str,
        batch_size: int = 1
    ) -> List[Dict[str, Any]]:
        """Atomically claim work from OCR queue"""
        try:
            processing_key = f"{self.PROCESSING_OCR}:{worker_id}"
            
            # Atomically pop items from sorted set (Redis 3.x compatible)
            items = self._zpopmin_compat(self.QUEUE_OCR, batch_size)
            
            if not items:
                return []
            
            claimed = []
            current_time = datetime.now().timestamp()
            
            for item_json, score in items:
                item = json.loads(item_json)
                item['claimed_at'] = current_time
                item['worker_id'] = worker_id
                item['status'] = QueueStatus.PROCESSING.value
                
                self.client.hset(processing_key, str(item['file_id']), json.dumps(item))
                self.client.expire(processing_key, 1800)  # 30 minute timeout for OCR
                
                claimed.append(item)
            
            return claimed
            
        except Exception as e:
            logger.error(f"Error claiming OCR work: {e}")
            return []
    
    def complete_ocr(
        self,
        queue_id: int,
        ocr_confidence: float,
        processing_time_ms: int,
        worker_id: str = None
    ) -> None:
        """Mark OCR as complete"""
        try:
            # Remove from processing set - use worker_id if provided for O(1) delete
            if worker_id:
                processing_key = f"{self.PROCESSING_OCR}:{worker_id}"
                self.client.hdel(processing_key, str(queue_id))
            else:
                # Fall back to scanning ocr worker processing keys
                config = get_config()
                max_ocr_workers = getattr(config.ocr, 'post_indexing_workers', 35) + 5
                for i in range(1, max_ocr_workers + 1):
                    key = f"{self.PROCESSING_OCR}:ocr-{i}"
                    self.client.hdel(key, str(queue_id))

            # Only increment OCR completed counter on first transition to completed.
            file_key = f"{self.HASH_FILES}:{queue_id}"
            previously_completed = str(self.client.hget(file_key, 'ocr_completed') or '0') == '1'
            
            # Update file metadata
            self.client.hmset(file_key, {
                'ocr_confidence': ocr_confidence,
                'ocr_time_ms': processing_time_ms,
                'ocr_completed_at': datetime.now().timestamp(),
                'ocr_completed': 1
            })
            
            # Increment Completed OCR Counter only once per file.
            if not previously_completed:
                self.client.incr(self.COUNTER_OCR_COMPLETED)
            
        except Exception as e:
            logger.error(f"Error completing OCR: {e}")

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
        """Add file to async tagging queue."""
        try:
            base_id = int(file_id or 0)
            if base_id <= 0:
                seed = str(file_hash or doc_id or file_path or time.time())
                base_id = int(hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12], 16)
            item = {
                'id': base_id,
                'file_id': int(file_id or 0),
                'file_path': file_path,
                'file_hash': file_hash,
                'doc_id': doc_id,
                'priority': int(priority or 5),
                'retry_count': 0,
                'status': QueueStatus.PENDING.value,
                'added_at': datetime.now().timestamp()
            }
            self.client.lpush(self.QUEUE_TAGGING, json.dumps(item))
            return base_id
        except Exception as e:
            logger.error(f"Error adding to tagging queue: {e}")
            return -1

    def claim_tagging_work(
        self,
        worker_id: str,
        batch_size: int = 8
    ) -> List[Dict[str, Any]]:
        """Atomically claim work from tagging queue."""
        try:
            processing_key = f"{self.PROCESSING_TAGGING}:{worker_id}"
            # T7: Use MULTI/EXEC for atomic batch pop (same fix as C3 for indexing)
            pipe = self.client.pipeline(transaction=True)
            pipe.lrange(self.QUEUE_TAGGING, -batch_size, -1)
            pipe.ltrim(self.QUEUE_TAGGING, 0, -(batch_size + 1))
            items_json, _ = pipe.execute()
            if not items_json:
                return []

            claimed: List[Dict[str, Any]] = []
            current_time = datetime.now().timestamp()
            for item_json in items_json:
                item = json.loads(item_json)
                item['claimed_at'] = current_time
                item['worker_id'] = worker_id
                item['status'] = QueueStatus.PROCESSING.value
                self.client.hset(processing_key, str(item['id']), json.dumps(item))
                self.client.expire(processing_key, 1200)
                claimed.append(item)
            return claimed
        except Exception as e:
            logger.error(f"Error claiming tagging work: {e}")
            return []

    def complete_tagging(
        self,
        queue_id: int,
        processing_time_ms: int,
        worker_id: str,
        status: str = QueueStatus.COMPLETED.value,
    ) -> None:
        """Mark tagging as complete/failed for a queue item."""
        try:
            processing_key = f"{self.PROCESSING_TAGGING}:{worker_id}"
            self.client.hdel(processing_key, str(queue_id))
            if status == QueueStatus.COMPLETED.value:
                self.client.incr(self.COUNTER_TAGGING_COMPLETED)
                file_key = f"{self.HASH_FILES}:{queue_id}"
                self.client.hset(file_key, 'tagged_at', datetime.now().timestamp())
        except Exception as e:
            logger.error(f"Error completing tagging for {queue_id}: {e}")

    def requeue_tagging(self, queue_id: int, reason: str = "") -> None:
        """Return tagging work item to pending queue for retry."""
        try:
            processing_keys = self._get_tagging_processing_keys()
            for key in processing_keys:
                item_json = self.client.hget(key, str(queue_id))
                if not item_json:
                    continue
                self.client.hdel(key, str(queue_id))
                item = json.loads(item_json)
                item['status'] = QueueStatus.PENDING.value
                item['claimed_at'] = None
                item['worker_id'] = None
                item['retry_count'] = int(item.get('retry_count', 0) or 0) + 1
                if reason:
                    item['last_error'] = reason
                self.client.lpush(self.QUEUE_TAGGING, json.dumps(item))
                break
        except Exception as e:
            logger.error(f"Error requeueing tagging item {queue_id}: {e}")
    
    def complete_indexing_batch(self, queue_ids: List[int], worker_id: str = None) -> None:
        """Mark batch of documents as indexed - remove from indexing processing"""
        try:
            # Remove from processing set - use worker_id if provided for O(1) delete
            if worker_id:
                processing_key = f"{self.PROCESSING_INDEXING}:{worker_id}"
                for queue_id in queue_ids:
                    self.client.hdel(processing_key, str(queue_id))
            else:
                # Fall back to scanning indexing worker processing keys
                config = get_config()
                max_idx_workers = getattr(config.indexing, 'num_workers', 12) + 5
                for i in range(1, max_idx_workers + 1):
                    key = f"{self.PROCESSING_INDEXING}:idx-{i}"
                    for queue_id in queue_ids:
                        self.client.hdel(key, str(queue_id))
        except Exception as e:
            logger.error(f"Error completing indexing batch: {e}")
    
    def requeue_indexing_items(
        self,
        queue_ids: List[int],
        *,
        increment_retry: bool = True
    ) -> None:
        """Return indexing items to queue for retry"""
        if not queue_ids:
            return
        
        try:
            # Get processing keys using fixed pattern (O(1) - no scan_iter)
            processing_keys = self._get_indexing_processing_keys()
            for queue_id in queue_ids:
                # Find and remove from processing
                for key in processing_keys:
                    item_json = self.client.hget(key, str(queue_id))
                    if item_json:
                        self.client.hdel(key, str(queue_id))
                        # Parse and increment retry if needed
                        item = json.loads(item_json)
                        if increment_retry:
                            item['retry_count'] = item.get('retry_count', 0) + 1
                        # Re-add to queue
                        self.client.lpush(self.QUEUE_INDEXING, json.dumps(item))
                        break
        except Exception as e:
            logger.error(f"Error requeuing indexing items: {e}")
    
    def fail_indexing_items(self, queue_ids: List[int]) -> None:
        """Mark indexing items as permanently failed - remove from processing"""
        if not queue_ids:
            return
        
        try:
            failed_count = 0
            # Get processing keys using fixed pattern (O(1) - no scan_iter)
            processing_keys = self._get_indexing_processing_keys()
            for queue_id in queue_ids:
                # Remove from processing sets
                for key in processing_keys:
                    item_json = self.client.hget(key, str(queue_id))
                    if item_json:
                        self.client.hdel(key, str(queue_id))
                        # Parse item and mark it failed
                        item = json.loads(item_json)
                        file_id = item.get('file_id')
                        file_path = item.get('file_path', 'unknown')
                        if file_id:
                            # Mark file as failed with all required arguments
                            self.mark_file_failed(
                                file_id=file_id,
                                file_path=file_path,
                                stage='indexing',
                                error_type=ErrorType.INDEXING_ERROR,
                                error_message='Indexing permanently failed'
                            )
                            failed_count += 1
                        break
            if failed_count > 0:
                logger.info(f"Marked {failed_count} indexing items as failed")
        except Exception as e:
            logger.error(f"Error failing indexing items: {e}")

    def reset_stale_processing(self, timeout_minutes: int = None) -> Dict[str, int]:
        """
        Reset items from all 'processing' maps back to their respective queues.
        This is used on system startup to recover items that were in-flight during a crash.
        """
        stats = {'extraction': 0, 'indexing': 0, 'ocr': 0, 'tagging': 0}
        logger.info("Resetting stale processing items...")
        
        try:
            # 1. Extraction
            logger.info(f"Scanning for extraction keys with pattern: {self.PROCESSING_EXTRACTION}:*")
            found_keys = 0
            # Use cached worker keys instead of slow scan_iter
            extraction_keys = self._get_worker_keys(self.PROCESSING_EXTRACTION)
            for key in extraction_keys:
                found_keys += 1
                logger.info(f"Processing restore key: {key}")
                items = self.client.hgetall(key)
                if items:
                    stale_ids = []
                    for queue_id, item_json in items.items():
                        try:
                            # Decode if bytes
                            if isinstance(item_json, bytes):
                                item_json = item_json.decode('utf-8')
                            
                            # Parse to check timestamp and get size category
                            item = json.loads(item_json)
                            claimed_at = item.get('claimed_at', 0)
                            
                            # Only re-queue if stale (default 5min timeout)
                            timeout_seconds = (timeout_minutes or 5) * 60
                            if time.time() - claimed_at > timeout_seconds:
                                size_cat = item.get('size_category', 'small')
                                queue_key = f"{self.QUEUE_EXTRACTION}:{size_cat}"
                                
                                # Re-queue with high priority (0)
                                self.client.zadd(queue_key, {item_json: 0})
                                stale_ids.append(queue_id)
                                stats['extraction'] += 1
                        except Exception as e:
                            logger.error(f"Error restoring item in {key}: {e}")

                    # Remove only stale entries; keep active in-flight work.
                    if stale_ids:
                        self.client.hdel(key, *stale_ids)

                    # Clean up empty processing keys.
                    if self.client.hlen(key) == 0:
                        self.client.delete(key)
            logger.info(f"Found {found_keys} extraction keys")
            
            # 2. Indexing
            indexing_keys = self._get_worker_keys(self.PROCESSING_INDEXING)
            for key in indexing_keys:
                items = self.client.hgetall(key)
                if items:
                    stale_items = []
                    for queue_id, item_json in items.items():
                        try:
                            # Decode if bytes
                            if isinstance(item_json, bytes):
                                item_json = item_json.decode('utf-8')
                            
                            item = json.loads(item_json)
                            claimed_at = item.get('claimed_at', 0)
                            
                            # Only re-queue if stale
                            timeout_seconds = (timeout_minutes or 5) * 60
                            if time.time() - claimed_at > timeout_seconds:
                                stale_items.append((queue_id, item_json))
                        except Exception as e:
                            logger.error(f"Error checking indexing item: {e}")
                    
                    if stale_items:
                        # Lpush to front of queue
                        self.client.lpush(self.QUEUE_INDEXING, *[item[1] for item in stale_items])
                        # Remove from processing map
                        self.client.hdel(key, *[item[0] for item in stale_items])
                        stats['indexing'] += len(stale_items)
            
            # 3. OCR
            ocr_keys = self._get_worker_keys(self.PROCESSING_OCR)
            for key in ocr_keys:
                items = self.client.hgetall(key)
                if items:
                    for queue_id, item_json in items.items():
                        try:
                            # Decode if bytes
                            if isinstance(item_json, bytes):
                                item_json = item_json.decode('utf-8')
                            
                            item = json.loads(item_json)
                            claimed_at = item.get('claimed_at', 0)
                            
                            # Only re-queue if stale
                            timeout_seconds = (timeout_minutes or 5) * 60
                            if time.time() - claimed_at > timeout_seconds:
                                self.client.zadd(self.QUEUE_OCR, {item_json: 0})
                                self.client.hdel(key, queue_id)
                                stats['ocr'] += 1
                        except Exception as e:
                            logger.error(f"Error restoring OCR item: {e}")

            # 4. TAGGING
            tagging_keys = self._get_worker_keys(self.PROCESSING_TAGGING)
            for key in tagging_keys:
                items = self.client.hgetall(key)
                if items:
                    stale_items = []
                    for queue_id, item_json in items.items():
                        try:
                            if isinstance(item_json, bytes):
                                item_json = item_json.decode('utf-8')

                            item = json.loads(item_json)
                            claimed_at = item.get('claimed_at', 0)
                            timeout_seconds = (timeout_minutes or 5) * 60
                            if time.time() - claimed_at > timeout_seconds:
                                stale_items.append((queue_id, item_json))
                        except Exception as e:
                            logger.error(f"Error checking tagging item: {e}")

                    if stale_items:
                        self.client.lpush(self.QUEUE_TAGGING, *[item[1] for item in stale_items])
                        self.client.hdel(key, *[item[0] for item in stale_items])
                        stats['tagging'] += len(stale_items)
            
            if sum(stats.values()) > 0:
                logger.info(f"Restored stale items: {stats}")
            
        except Exception as e:
            logger.error(f"Error resetting stale processing: {e}")
            
        return stats
    
    def cleanup_zombie_processing(self) -> int:
        """
        Clean up 'zombie' processing items that were successfully indexed
        but never marked complete (e.g., worker crashed after indexing).
        
        Returns: Number of zombie items cleaned up
        """
        cleaned = 0
        try:
            # Scan all processing keys
            all_proc_keys = []
            for prefix in [self.PROCESSING_EXTRACTION, self.PROCESSING_INDEXING,
                          self.PROCESSING_OCR, self.PROCESSING_TAGGING]:
                all_proc_keys.extend(self._get_worker_keys(prefix))
            
            for key in all_proc_keys:
                items = self.client.hgetall(key)
                zombie_ids = []
                
                for file_id_str, item_json in items.items():
                    try:
                        # Decode if bytes
                        if isinstance(item_json, bytes):
                            file_id_str = file_id_str.decode('utf-8')
                            item_json = item_json.decode('utf-8')
                        
                        file_id = int(file_id_str)
                        
                        # Check if already in completed set (shouldn't have processing key)
                        if self.client.sismember(self.SET_COMPLETED_FILE_IDS, file_id):
                            zombie_ids.append(file_id_str)
                            continue
                        
                        # Check file metadata for completion indicators
                        file_key = f"{self.HASH_FILES}:{file_id}"
                        file_meta = self.client.hgetall(file_key)
                        if not file_meta:
                            # File metadata deleted but processing key remains
                            zombie_ids.append(file_id_str)
                            continue
                        
                        # Decode metadata if needed
                        if file_meta and isinstance(list(file_meta.keys())[0], bytes):
                            file_meta = {k.decode(): v.decode() for k, v in file_meta.items()}
                        
                        # If file has 'indexed_at' timestamp but not in completed set,
                        # and claimed_at is old (>30min), it's a zombie
                        indexed_at = file_meta.get('indexed_at')
                        if indexed_at:
                            item = json.loads(item_json)
                            claimed_at = item.get('claimed_at', 0)
                            if time.time() - claimed_at > 1800:  # 30 minutes
                                zombie_ids.append(file_id_str)
                                logger.warning(
                                    f"Zombie processing item detected: file_id={file_id}, "
                                    f"indexed_at={indexed_at}, status={file_meta.get('status')}"
                                )
                    
                    except Exception as e:
                        logger.debug(f"Error checking zombie item {file_id_str}: {e}")
                
                # Remove zombies
                if zombie_ids:
                    self.client.hdel(key, *zombie_ids)
                    cleaned += len(zombie_ids)
                    logger.info(f"Cleaned {len(zombie_ids)} zombie items from {key}")
            
            if cleaned > 0:
                logger.info(f"Total zombie items cleaned: {cleaned}")
        
        except Exception as e:
            logger.error(f"Error cleaning zombie processing items: {e}")
        
        return cleaned
    
    # ========================================================================
    # FILE COMPLETION AND STATISTICS
    # ========================================================================
    
    def mark_file_completed(
        self,
        file_id: int,
        extraction_time_ms: int = 0,
        indexing_time_ms: int = 0,
        # Extended optional args for backward compatibility
        file_path: str = None,
        file_hash: str = None,
        content_hash: str = None,
        document_id: str = None,
        is_duplicate: bool = False,
        duplicate_of: str = None
    ) -> None:
        """Mark a file as fully completed"""
        try:
            file_size = 0
            # Get file info from stored metadata if not provided
            if file_path is None or file_hash is None:
                file_data = self.client.hgetall(f"{self.HASH_FILES}:{file_id}")
                if file_data:
                    file_path = file_path or file_data.get('file_path', '')
                    file_hash = file_hash or file_data.get('file_hash', '')
                    # Get file size from metadata
                    try:
                        file_size = int(file_data.get('file_size', 0))
                    except (ValueError, TypeError):
                        file_size = 0
                else:
                    # Try to find from any file reference
                    file_path = file_path or ''
                    file_hash = file_hash or str(file_id)
            else:
                # Fetch file_size from metadata even when path/hash are provided
                file_data = self.client.hgetall(f"{self.HASH_FILES}:{file_id}")
                if file_data:
                    try:
                        file_size = int(file_data.get('file_size', 0))
                    except (ValueError, TypeError):
                        file_size = 0
            
            completion_data = {
                'file_id': file_id,
                'file_path': file_path,
                'file_hash': file_hash,
                'file_size': file_size,  # Include file_size for size statistics
                'content_hash': content_hash,
                'document_id': document_id or '',
                'is_duplicate': is_duplicate,
                'duplicate_of': duplicate_of,
                'extraction_time_ms': extraction_time_ms,
                'indexing_time_ms': indexing_time_ms,
                'indexed_at': datetime.now().timestamp()
            }
            
            # Store in completed hash
            if file_hash:
                # Check if this is a new completion (not already in hash)
                is_new_hash = not self.client.hexists(self.HASH_COMPLETED, file_hash)
                
                # Use pipeline for hash updates
                pipe = self.client.pipeline()
                pipe.hset(self.HASH_COMPLETED, file_hash, json.dumps(completion_data))
                pipe.sadd(self.SET_FILE_HASHES, file_hash)
                 
                # Increment valid aggregated stats (hash-based) if new hash
                if is_new_hash:
                    # Deprecated legacy counter - keeping for backward compat
                    pipe.incr(self.COUNTER_COMPLETED)
                    
                    if extraction_time_ms > 0:
                        pipe.incrby(self.COUNTER_COMPLETED_EXTRACT_MS, extraction_time_ms)
                    if indexing_time_ms > 0:
                        pipe.incrby(self.COUNTER_COMPLETED_INDEX_MS, indexing_time_ms)
                    if is_duplicate:
                        pipe.incr(self.COUNTER_DUPLICATES)
                
                # Add content hash to set if present
                if content_hash:
                    pipe.sadd(self.SET_CONTENT_HASHES, content_hash)
                
                # Track in sorted set by size for efficient top-N queries
                if file_size > 0:
                    pipe.zadd(self.ZSET_COMPLETED_BY_SIZE, {file_hash: file_size})
                
                pipe.execute()

                # Atomic Update for Root Files (Critical for Accurate Metrics)
                # Skip embedded children — only parent files count toward root_completed.
                is_embedded_file = False
                if file_hash:
                    is_embedded_file = self.client.hexists(self.HASH_PARENT_MAP, file_hash)

                if not is_embedded_file:
                    try:
                        self.lua_add_root_completion(
                            keys=[self.SET_COMPLETED_FILE_IDS, self.COUNTER_ROOT_COMPLETED, self.COUNTER_COMPLETED_BYTES],
                            args=[str(file_id), file_size],
                            client=self.client
                        )
                    except Exception as e:
                        logger.error(f"Error executing atomic root completion script: {e}")
                        if self.client.sadd(self.SET_COMPLETED_FILE_IDS, str(file_id)) == 1:
                            self.client.incr(self.COUNTER_ROOT_COMPLETED)
                            if file_size > 0:
                                self.client.incrby(self.COUNTER_COMPLETED_BYTES, file_size)
            
        except Exception as e:
            logger.error(f"Error marking file completed: {e}")
    
    def mark_file_failed(
        self,
        file_id: int,
        file_path: str,
        stage: str,
        error_type: ErrorType,
        error_message: str,
        retry_count: int = 0,
        stack_trace: str = None
    ) -> None:
        """Mark a file as failed"""
        try:
            file_size = 0
            try:
                metadata = self.client.hgetall(f"{self.HASH_FILES}:{file_id}")
                file_size = int(metadata.get('file_size', 0) or 0)
            except (TypeError, ValueError):
                file_size = 0

            failure_data = {
                'file_id': file_id,
                'file_path': file_path,
                'file_size': file_size,
                'stage': stage,
                'error_type': error_type.value if hasattr(error_type, 'value') else str(error_type),
                'error_message': error_message,
                'retry_count': retry_count,
                'stack_trace': stack_trace,
                'failed_at': datetime.now().timestamp()
            }
            
            pipe = self.client.pipeline()
            pipe.hset(self.HASH_FAILED, str(file_id), json.dumps(failure_data))
            error_key = error_type.value if hasattr(error_type, 'value') else str(error_type)
            pipe.hincrby(self.HASH_FAILURE_COUNTS, error_key, 1)
            pipe.execute()
            
        except Exception as e:
            logger.error(f"Error marking file failed: {e}")
    
    # ---- Cached worker key discovery (deterministic, no keyspace SCAN) ----
    _WORKER_KEYS_TTL = 30.0                    # refresh every 30s

    def _get_worker_keys(self, prefix: str) -> list:
        """Get processing worker keys directly based on deterministic patterns.

        Avoids Redis keyspace scans and caching lag entirely.
        """
        try:
            if prefix == self.PROCESSING_EXTRACTION:
                return self._get_extraction_processing_keys()
            elif prefix == self.PROCESSING_INDEXING:
                return self._get_indexing_processing_keys()
            elif prefix == self.PROCESSING_OCR:
                return self._get_ocr_processing_keys()
            elif prefix == self.PROCESSING_TAGGING:
                return self._get_tagging_processing_keys()
            else:
                # Unknown prefix: fallback keyspace scan
                return list(self.client.scan_iter(match=f"{prefix}:*", count=1000))
        except Exception as e:
            logger.error(f"Error getting worker keys: {e}")
            return []

    def _count_processing_items(self, prefix: str) -> int:
        """Count total items in processing hashes for a given prefix.
        
        Uses cached key discovery + pipelined HLEN for O(workers) instead
        of O(keyspace) performance.
        """
        try:
            keys = self._get_worker_keys(prefix)
            if not keys:
                return 0
            pipe = self.client.pipeline(transaction=False)
            try:
                for key in keys:
                    pipe.hlen(key)
                results = pipe.execute()
            finally:
                try:
                    pipe.reset()
                except Exception:
                    pass
            return sum(r for r in results if isinstance(r, int))
        except Exception:
            return 0

    def _get_extraction_processing_stats(self) -> Dict[str, int]:
        """Get extraction processing counts by category.
        
        Uses cached key discovery + pipelined HVALS for speed.
        """
        stats = {'tiny': 0, 'small': 0, 'medium': 0, 'large': 0}
        try:
            keys = self._get_worker_keys(self.PROCESSING_EXTRACTION)
            if not keys:
                return stats
            pipe = self.client.pipeline(transaction=False)
            try:
                for key in keys:
                    pipe.hvals(key)
                results = pipe.execute()
            finally:
                try:
                    pipe.reset()
                except Exception:
                    pass
            for items in results:
                if not isinstance(items, list):
                    continue
                for item_json in items:
                    try:
                        item = json.loads(item_json)
                        cat = item.get('size_category')
                        if cat in stats:
                            stats[cat] += 1
                    except (json.JSONDecodeError, AttributeError):
                        pass
            return stats
        except Exception as e:
            logger.error(f"Error getting extraction processing stats: {e}")
            return stats

    def get_queue_stats(self) -> Dict[str, Dict[str, int]]:
        """Get statistics for all queues - FAST O(1) operations only.
        
        Uses a single Redis pipeline to batch all O(1) calls into ONE
        round-trip, eliminating per-call latency that caused dashboard zeros.
        """
        try:
            stats = {
                'discovery': {'pending': 0, 'processing': 0, 'completed': 0, 'failed': 0},
                'extraction': {},
                'extraction_total': {'pending': 0, 'processing': 0, 'completed': 0},
                'indexing': {'pending': 0, 'processing': 0, 'completed': 0},
                'ocr': {'pending': 0, 'processing': 0, 'completed': 0},
                'tagging': {'pending': 0, 'processing': 0, 'completed': 0}
            }
            
            # ---- Single pipeline for ALL O(1) reads ----
            pipe = self.client.pipeline(transaction=False)
            
            # 0: Discovery pending
            pipe.zcard(self.QUEUE_DISCOVERY)
            # 1: Total discovered counter
            pipe.get(self.COUNTER_DISCOVERED)
            # 2: Authoritative root completed counter
            pipe.get(self.COUNTER_ROOT_COMPLETED)
            # 3: Total completed (legacy items) counter
            pipe.get(self.COUNTER_COMPLETED)
            # 4: Fallback completed HLEN
            pipe.hlen(self.HASH_COMPLETED)
            # 5: Extraction completed counter
            pipe.get(self.COUNTER_EXTRACTION_COMPLETED)
            # 6-9:  Extraction queue sizes per category
            size_cats = ['tiny', 'small', 'medium', 'large']
            for cat in size_cats:
                pipe.zcard(f"{self.QUEUE_EXTRACTION}:{cat}")
            # 10-13: Extraction completed per category
            for cat in size_cats:
                pipe.get(f"{self.COUNTER_EXTRACTION_COMPLETED}:{cat}")
            # 14: Indexing pending (LLEN)
            pipe.llen(self.QUEUE_INDEXING)
            # 15: OCR pending (ZCARD)
            pipe.zcard(self.QUEUE_OCR)
            # 16: Failed count (HLEN)
            pipe.hlen(self.HASH_FAILED)
            # 17: Tagging pending (LLEN)
            pipe.llen(self.QUEUE_TAGGING)
            # 18: Tagging completed (counter)
            pipe.get(self.COUNTER_TAGGING_COMPLETED)
            # 19: OCR completed counter (direct read — no cache/estimation)
            pipe.get(self.COUNTER_OCR_COMPLETED)
            try:
                results = pipe.execute()
            finally:
                try:
                    pipe.reset()
                except Exception:
                    pass
            
            # ---- Unpack pipeline results ----
            def safe_int(val, default=0):
                if val is None: return default
                try: return int(val)
                except (ValueError, TypeError): return default

            discovery_pending = safe_int(results[0])
            discovered_total = safe_int(results[1])
            root_completed_counter = safe_int(results[2])
            total_completed_counter = safe_int(results[3])
            total_completed_hlen = safe_int(results[4])
            extraction_completed_counter = safe_int(results[5])
            
            total_completed = total_completed_counter if total_completed_counter > 0 else total_completed_hlen
            total_root_completed = root_completed_counter if root_completed_counter > 0 else total_completed
            # Use counter directly without fallback
            extraction_completed = extraction_completed_counter
            
            # Extraction per-category (results 6-9 = pending, 10-13 = completed)
            extraction_processing_by_cat = self._get_extraction_processing_stats()
            total_extraction_pending = 0
            total_extraction_processing = 0
            
            for i, cat in enumerate(size_cats):
                pending = safe_int(results[6 + i])
                cat_completed = safe_int(results[10 + i])
                processing = extraction_processing_by_cat.get(cat, 0)
                total_extraction_pending += pending
                total_extraction_processing += processing
                stats['extraction'][cat] = {
                    'pending': pending,
                    'processing': processing,
                    'completed': cat_completed,
                    'total': pending + processing + cat_completed  # Include all states
                }
            
            indexing_pending = safe_int(results[14])
            ocr_pending = safe_int(results[15])
            failed_count = safe_int(results[16])
            tagging_pending = safe_int(results[17])
            tagging_completed = safe_int(results[18])
            ocr_completed = safe_int(results[19])
            
            # Processing counts (still need separate calls for these hash scans)
            indexing_processing = self._count_processing_items(self.PROCESSING_INDEXING)
            ocr_processing = self._count_processing_items(self.PROCESSING_OCR)
            tagging_processing = self._count_processing_items(self.PROCESSING_TAGGING)


            
            # Discovery "completed" should reflect discovery-stage progress, not indexing completion.
            discovery_completed = max(0, discovered_total - discovery_pending)
            stats['discovery']['pending'] = discovery_pending
            stats['discovery']['completed'] = discovery_completed
            stats['discovery']['failed'] = failed_count
            
            stats['extraction_total']['pending'] = total_extraction_pending
            stats['extraction_total']['processing'] = total_extraction_processing
            stats['extraction_total']['completed'] = extraction_completed
            
            stats['indexing']['pending'] = indexing_pending
            stats['indexing']['processing'] = indexing_processing
            stats['indexing']['completed'] = total_root_completed
            
            stats['ocr']['pending'] = ocr_pending
            stats['ocr']['processing'] = ocr_processing
            stats['ocr']['completed'] = ocr_completed

            stats['tagging']['pending'] = tagging_pending
            stats['tagging']['processing'] = tagging_processing
            stats['tagging']['completed'] = tagging_completed
            
            return stats
            
        except Exception as e:
            logger.error(f"Error getting queue stats: {e}")
            return {}
    
    def get_queue_statistics(self) -> Dict[str, Any]:
        """Get comprehensive queue statistics (compatible with SQLite version)"""
        stats = self.get_queue_stats()
        
        # Transform to expected format with proper defaults (no None values)
        discovery_stats = stats.get('discovery', {})
        extraction_stats = stats.get('extraction', {})
        extraction_total_stats = stats.get('extraction_total', {})
        indexing_stats = stats.get('indexing', {})
        ocr_stats = stats.get('ocr', {})
        tagging_stats = stats.get('tagging', {})
        
        # Helper to safely get int value
        def safe_int(val, default=0):
            if val is None:
                return default
            try:
                return int(val)
            except (ValueError, TypeError):
                return default
        
        # Get accurate discovered count from counter
        try:
            discovered_total = int(self.client.get(self.COUNTER_DISCOVERED) or 0)
        except (ValueError, TypeError):
            discovered_total = 0
        
        # Fall back to pending + completed if counter not set
        if discovered_total == 0:
            discovered_total = safe_int(discovery_stats.get('pending')) + safe_int(discovery_stats.get('completed'))
        
        # Compute extraction total
        ext_pending = safe_int(extraction_total_stats.get('pending'))
        ext_processing = safe_int(extraction_total_stats.get('processing'))
        ext_completed = safe_int(extraction_total_stats.get('completed'))
        ext_total = ext_pending + ext_processing + ext_completed
        
        # Compute indexing total
        idx_pending = safe_int(indexing_stats.get('pending'))
        idx_processing = safe_int(indexing_stats.get('processing'))
        idx_completed = safe_int(indexing_stats.get('completed'))
        idx_total = idx_pending + idx_processing + idx_completed
        
        # Compute OCR total
        ocr_pending = safe_int(ocr_stats.get('pending'))
        ocr_processing = safe_int(ocr_stats.get('processing'))
        ocr_completed = safe_int(ocr_stats.get('completed'))
        ocr_total = ocr_pending + ocr_processing + ocr_completed

        # Compute tagging total
        tagging_pending = safe_int(tagging_stats.get('pending'))
        tagging_processing = safe_int(tagging_stats.get('processing'))
        tagging_completed = safe_int(tagging_stats.get('completed'))
        tagging_total = tagging_pending + tagging_processing + tagging_completed
        
        return {
            'discovery': {
                'total': discovered_total,
                'pending': safe_int(discovery_stats.get('pending')),
                'processing': safe_int(discovery_stats.get('processing')),
                'completed': safe_int(discovery_stats.get('completed')),
                'failed': safe_int(discovery_stats.get('failed'))
            },
            'extraction': extraction_stats,
            'extraction_total': {
                'total': ext_total,
                'pending': ext_pending,
                'processing': ext_processing,
                'completed': ext_completed
            },
            'indexing': {
                'total': idx_total,
                'pending': idx_pending,
                'processing': idx_processing,
                'completed': idx_completed
            },
            'ocr': {
                'total': ocr_total,
                'pending': ocr_pending,
                'processing': ocr_processing,
                'completed': ocr_completed
            },
            'tagging': {
                'total': tagging_total,
                'pending': tagging_pending,
                'processing': tagging_processing,
                'completed': tagging_completed
            },
            'completed': self.get_completed_files_stats(),
            'failures': self._get_failure_breakdown(),
            'total_failures': safe_int(discovery_stats.get('failed'))
        }
    
    def _get_failure_breakdown(self) -> Dict[str, int]:
        """Get failure counts by error type - O(1) using pre-aggregated hash"""
        try:
            # Fast path: use pre-aggregated failure counts hash (O(1))
            raw = self.client.hgetall(self.HASH_FAILURE_COUNTS)
            if raw:
                return {k: int(v) for k, v in raw.items()}
            
            # Fallback: scan HASH_FAILED if counts hash doesn't exist yet (migration)
            breakdown = {}
            cursor = 0
            while True:
                cursor, data = self.client.hscan(self.HASH_FAILED, cursor, count=100)
                for file_id, info_json in data.items():
                    try:
                        info = json.loads(info_json)
                        error_type = info.get('error_type', 'unknown')
                        breakdown[error_type] = breakdown.get(error_type, 0) + 1
                    except (json.JSONDecodeError, TypeError):
                        continue
                if cursor == 0:
                    break
            # Bootstrap the counts hash for future O(1) lookups
            if breakdown:
                pipe = self.client.pipeline()
                for error_type, count in breakdown.items():
                    pipe.hset(self.HASH_FAILURE_COUNTS, error_type, count)
                pipe.execute()
            return breakdown
        except Exception as e:
            logger.error(f"Error getting failure breakdown: {e}")
            return {}
    
    def get_size_statistics(self) -> Dict[str, Any]:
        """Get file size statistics for dashboard display.
        
        Uses a SINGLE pipeline for ALL Redis calls (1 round-trip instead of 15+).
        This prevents Redis connection exhaustion that caused dashboard zeros.
        """
        try:
            # ---- Single pipeline for ALL O(1) reads ----
            pipe = self.client.pipeline(transaction=False)
            
            pipe.get(self.COUNTER_DISCOVERED)           # 0
            pipe.get(self.COUNTER_DISCOVERED_BYTES)      # 1
            pipe.hlen(self.HASH_COMPLETED)               # 2 (fallback)
            pipe.get(self.COUNTER_ROOT_COMPLETED)        # 3
            pipe.get(self.COUNTER_COMPLETED)             # 4
            pipe.get(self.COUNTER_COMPLETED_BYTES)       # 5
            pipe.scard(self.SET_COMPLETED_FILE_IDS)      # 6
            pipe.hlen(self.HASH_FAILED)                  # 7
            for size_cat in ['tiny', 'small', 'medium', 'large']:
                pipe.zcard(f"{self.QUEUE_EXTRACTION}:{size_cat}")  # 8-11
            pipe.llen(self.QUEUE_INDEXING)               # 12
            pipe.zcard(self.QUEUE_OCR)                   # 13
            pipe.llen(self.QUEUE_TAGGING)                # 14
            try:
                results = pipe.execute()
            finally:
                try:
                    pipe.reset()
                except Exception:
                    pass
            
            def safe_int(val, default=0):
                if val is None: return default
                try: return int(val)
                except (ValueError, TypeError): return default
            
            total_discovered_files = safe_int(results[0])
            total_discovered_size = safe_int(results[1])
            completed_hlen = safe_int(results[2])
            
            if total_discovered_files == 0:
                total_discovered_files = completed_hlen
            
            completed_files = safe_int(results[3])  # ROOT_COMPLETED
            if completed_files == 0:
                completed_files = safe_int(results[4])  # Legacy COMPLETED
            completed_size = safe_int(results[5])
            
            if completed_files == 0:
                completed_files = safe_int(results[6])  # SCARD
                if completed_files == 0:
                    completed_files = completed_hlen
            
            failed_files = safe_int(results[7])
            
            # Calculate failed file sizes by scanning the failed hash
            failed_size = 0
            if failed_files > 0:
                try:
                    missing_size_ids: List[str] = []
                    cursor = 0
                    while True:
                        cursor, data = self.client.hscan(self.HASH_FAILED, cursor, count=100)
                        for file_id, val in data.items():
                            try:
                                info = json.loads(val)
                                file_size = info.get('file_size', None)
                                if file_size is None:
                                    missing_size_ids.append(str(info.get('file_id', file_id)))
                                else:
                                    failed_size += int(file_size or 0)
                            except (json.JSONDecodeError, TypeError, ValueError):
                                pass
                        if cursor == 0:
                            break

                    # Backfill legacy failed rows that don't carry file_size.
                    if missing_size_ids:
                        pipe = self.client.pipeline(transaction=False)
                        for mid in missing_size_ids:
                            pipe.hget(f"{self.HASH_FILES}:{mid}", 'file_size')
                        try:
                            missing_sizes = pipe.execute()
                        finally:
                            try:
                                pipe.reset()
                            except Exception:
                                pass
                        for sz in missing_sizes:
                            failed_size += safe_int(sz)
                except Exception:
                    pass
            
            # In-pipeline = pending queue lengths + active processing items.
            pending_pipeline = sum(safe_int(results[i]) for i in range(8, 15))
            processing_pipeline = (
                self._count_processing_items(self.PROCESSING_EXTRACTION)
                + self._count_processing_items(self.PROCESSING_INDEXING)
                + self._count_processing_items(self.PROCESSING_OCR)
                + self._count_processing_items(self.PROCESSING_TAGGING)
            )
            in_pipeline = pending_pipeline + processing_pipeline
            
            # Keep bytes logically consistent with file counts:
            # if there are no queued/processing files, in-pipeline size must be zero.
            if in_pipeline == 0:
                in_pipeline_size = 0
            else:
                in_pipeline_size = max(0, total_discovered_size - completed_size - failed_size)
            
            total_items_completed = safe_int(results[4])  # Legacy counter
            
            return {
                'discovered': {'files': total_discovered_files, 'size_bytes': total_discovered_size},
                'in_pipeline': {'files': in_pipeline, 'size_bytes': in_pipeline_size},
                'searchable': {
                    'files': completed_files,
                    'items': total_items_completed,
                    'size_bytes': completed_size
                },
                'failed': {'files': failed_files, 'size_bytes': failed_size}
            }
        except Exception as e:
            logger.error(f"Error getting size stats: {e}")
            return {
                'discovered': {'files': 0, 'size_bytes': 0},
                'in_pipeline': {'files': 0, 'size_bytes': 0},
                'searchable': {'files': 0, 'items': 0, 'size_bytes': 0},
                'failed': {'files': 0, 'size_bytes': 0}
            }
    def initialize_completed_counters(self) -> Dict[str, int]:
        """Bootstrap completed counters from existing HASH_COMPLETED data.
        Call this once to initialize counters for existing data.
        Returns dict with 'files' and 'bytes' counts."""
        completed_files = 0
        completed_size = 0
        total_extract = 0
        total_index = 0
        duplicates = 0
        ocr_completed = 0
        file_ids = []
        try:
            cursor = 0  # Use integer for cursor
            while True:
                cursor, data = self.client.hscan(self.HASH_COMPLETED, cursor, count=500)
                for file_hash, info_json in data.items():
                    try:
                        info = json.loads(info_json)
                        completed_files += 1
                        completed_size += info.get('file_size', 0)
                        total_extract += info.get('extraction_time_ms', 0)
                        total_index += info.get('indexing_time_ms', 0)
                        if info.get('is_duplicate'):
                            duplicates += 1
                        
                        f_id = info.get('file_id')
                        if f_id is not None:
                            file_ids.append(f_id)
                    except (json.JSONDecodeError, TypeError):
                        continue  # Skip invalid JSON entries
                if cursor == 0:
                    break
            
            # Check which of the completed files have completed OCR
            if file_ids:
                pipe = self.client.pipeline(transaction=False)
                for f_id in file_ids:
                    pipe.hget(f"{self.HASH_FILES}:{f_id}", 'ocr_completed')
                ocr_flags = pipe.execute()
                for flag in ocr_flags:
                    if flag and int(flag) == 1:
                        ocr_completed += 1

            # Set counters
            self.client.set(self.COUNTER_COMPLETED, completed_files)
            self.client.set(self.COUNTER_COMPLETED_BYTES, completed_size)
            self.client.set(self.COUNTER_COMPLETED_EXTRACT_MS, total_extract)
            self.client.set(self.COUNTER_COMPLETED_INDEX_MS, total_index)
            self.client.set(self.COUNTER_DUPLICATES, duplicates)
            self.client.set(self.COUNTER_OCR_COMPLETED, ocr_completed)
            logger.info(
                "Initialized completed counters: %s files, %s bytes, %s duplicates, %s OCR completed",
                completed_files,
                completed_size,
                duplicates,
                ocr_completed
            )
            return {
                'files': completed_files,
                'bytes': completed_size,
                'duplicates': duplicates,
                'ocr_completed': ocr_completed,
                'total_extract_ms': total_extract,
                'total_index_ms': total_index
            }
        except Exception as e:
            logger.error(f"Error initializing completed counters: {e}")
            return {'files': 0, 'bytes': 0}
    
    def get_failed_files(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get list of failed files with details"""
        failed = []
        try:
            cursor = 0  # Use integer for cursor
            count = 0
            while True:
                cursor, data = self.client.hscan(self.HASH_FAILED, cursor, count=min(100, limit - count))
                for file_id, info_json in data.items():
                    try:
                        info = json.loads(info_json)
                        info['file_id'] = file_id
                        failed.append(info)
                        count += 1
                        if count >= limit:
                            break
                    except (json.JSONDecodeError, TypeError):
                        continue  # Skip invalid JSON entries
                if cursor == 0 or count >= limit:
                    break
        except Exception as e:
            logger.error(f"Error getting failed files: {e}")
        return failed
    
    def get_file_info(self, file_id: int) -> Dict[str, Any]:
        """Get file information by ID"""
        try:
            data = self.client.hgetall(f"{self.HASH_FILES}:{file_id}")
            if data:
                return {k: v for k, v in data.items()}
            return {}
        except Exception as e:
            logger.error(f"Error getting file info: {e}")
            return {}
    
    def get_completed_items(self, count: int = 100) -> List[Dict]:
        """Get recently completed items"""
        return []

    # ========================================================================
    # FOLDER DISCOVERY OPERATIONS
    # ========================================================================

    def push_folder(self, folder_path: str, priority: int = 0) -> None:
        """Add folder to scanning queue"""
        self.client.rpush(self.QUEUE_FOLDERS, folder_path)

    def pop_folder(self) -> Optional[str]:
        """Get next folder to scan"""
        return self.client.lpop(self.QUEUE_FOLDERS)

    def get_folder_mtime(self, folder_path: str) -> Optional[float]:
        """Get cached modification time for folder"""
        return self.client.hget(self.HASH_FOLDER_META, folder_path)

    def set_folder_mtime(self, folder_path: str, mtime: float) -> None:
        """Update cached modification time for folder"""
        self.client.hset(self.HASH_FOLDER_META, folder_path, mtime)
        
    def get_largest_completed_files(self, limit: int = 10) -> List[Dict[str, Any]]:
        """Return the largest completed files - uses sorted set for O(log N + limit) retrieval"""
        try:
            # Fast path: use pre-built sorted set (O(log N + limit))
            top_entries = self.client.zrevrange(self.ZSET_COMPLETED_BY_SIZE, 0, limit - 1, withscores=True)
            if top_entries:
                results = []
                for file_hash, file_size in top_entries:
                    info_json = self.client.hget(self.HASH_COMPLETED, file_hash)
                    if info_json:
                        try:
                            info = json.loads(info_json)
                            results.append(info)
                        except (json.JSONDecodeError, TypeError):
                            continue
                return results
            
            # Fallback: scan if sorted set not populated yet (legacy data)
            completed = []
            cursor = 0
            while True:
                cursor, data = self.client.hscan(self.HASH_COMPLETED, cursor, count=100)
                for file_hash, info_json in data.items():
                    try:
                        info = json.loads(info_json)
                        completed.append(info)
                        # Bootstrap the sorted set
                        self.client.zadd(self.ZSET_COMPLETED_BY_SIZE, {file_hash: info.get('file_size', 0)})
                    except (json.JSONDecodeError, TypeError):
                        continue
                if cursor == 0:
                    break
            
            completed.sort(key=lambda x: x.get('file_size', 0), reverse=True)
            return completed[:limit]
        except Exception as e:
            logger.error(f"Error getting largest completed files: {e}")
            return []
    
    def get_ocr_pending_files(self, limit: int = 100) -> List[Dict[str, Any]]:
        """Get list of files pending OCR processing"""
        pending = []
        try:
            items = self.client.zrange(self.QUEUE_OCR, 0, limit - 1, withscores=True)
            for item_json, priority in items:
                try:
                    item = json.loads(item_json)
                    item['priority'] = priority
                    item['status'] = 'pending'
                    pending.append(item)
                except (json.JSONDecodeError, TypeError):
                    continue  # Skip invalid JSON entries
        except Exception as e:
            logger.error(f"Error getting OCR pending files: {e}")
        return pending
    
    def get_completed_files_stats(self) -> Dict[str, Any]:
        """Get completion statistics (pipelined - 1 round-trip instead of 7)"""
        try:
            pipe = self.client.pipeline(transaction=False)
            pipe.get(self.COUNTER_ROOT_COMPLETED)        # 0 (authoritative root files)
            pipe.get(self.COUNTER_COMPLETED)             # 1 (legacy counter)
            pipe.hlen(self.HASH_COMPLETED)               # 2 (fallback)
            pipe.get(self.COUNTER_COMPLETED_EXTRACT_MS)  # 3
            pipe.get(self.COUNTER_COMPLETED_INDEX_MS)    # 4
            pipe.get(self.COUNTER_DUPLICATES)            # 5
            results = pipe.execute()
            
            def safe_int(val, default=0):
                if val is None: return default
                try: return int(val)
                except (ValueError, TypeError): return default
            
            total = safe_int(results[0])
            if total == 0:
                total = safe_int(results[1])
            if total == 0:
                total = safe_int(results[2])
            
            total_extract = safe_int(results[3])
            total_index = safe_int(results[4])
            duplicates = safe_int(results[5])
            
            avg_extract = total_extract // total if total > 0 else 0
            avg_index = total_index // total if total > 0 else 0
            
            return {
                'total_completed': total,
                'total': total,
                'duplicates': duplicates,
                'avg_extraction_ms': avg_extract,
                'avg_indexing_ms': avg_index,
            }
        except Exception as e:
            logger.error(f"Error getting completed stats: {e}")
            return {
                'total_completed': 0,
                'total': 0,
                'duplicates': 0,
                'avg_extraction_ms': 0,
                'avg_indexing_ms': 0
            }
    def _get_cached_ocr_count(self, total_completed: int) -> int:
        """Get OCR completion count"""
        try:
            return int(self.client.get(self.COUNTER_OCR_COMPLETED) or 0)
        except (ValueError, TypeError):
            return 0

    
    def get_file_hash_by_id(self, file_id: int) -> Optional[str]:
        """Get file hash for a file ID"""
        try:
            return self.client.hget(f"{self.HASH_FILES}:{file_id}", 'file_hash')
        except Exception as e:
            logger.error(f"Error getting file hash: {e}")
            return None
    
    # ========================================================================
    # DISCOVERY COMPLETION TRACKING (for resume capability)
    # ========================================================================
    
    def mark_discovery_complete(self) -> None:
        """Mark discovery phase as complete (called when all discovery workers finish)"""
        try:
            pipe = self.client.pipeline(transaction=False)
            pipe.set(self.DISCOVERY_COMPLETED_FLAG, "1", ex=None)
            pipe.delete(self.DISCOVERY_FORCE_RUN_FLAG)
            pipe.execute()
            logger.info("Discovery phase marked as COMPLETE")
        except Exception as e:
            logger.error(f"Error marking discovery complete: {e}")
    
    def is_discovery_complete(self) -> bool:
        """Check if discovery phase has been completed"""
        try:
            # Explicit force-run mode (used by "full" startup mode) always wins.
            if self.client.exists(self.DISCOVERY_FORCE_RUN_FLAG) == 1:
                return False

            # Fast path: explicit completion flag.
            if self.client.exists(self.DISCOVERY_COMPLETED_FLAG) == 1:
                return True

            # Fallback for restored/reset states where flag may be missing.
            discovered = int(self.client.get(self.COUNTER_DISCOVERED) or 0)
            if discovered <= 0:
                return False

            discovery_pending = self.client.zcard(self.QUEUE_DISCOVERY)
            folder_pending = self.client.llen(self.QUEUE_FOLDERS)
            root_completed = int(self.client.get(self.COUNTER_ROOT_COMPLETED) or 0)
            failed = self.client.hlen(self.HASH_FAILED)

            # Consider discovery complete when there is discovered data,
            # no pending discovery/folder work, and at least some finalized items.
            return discovery_pending == 0 and folder_pending == 0 and (root_completed + failed) > 0
        except Exception as e:
            logger.error(f"Error checking discovery completion: {e}")
            return False
    
    def reset_discovery_completion_flag(self) -> None:
        """Reset discovery completion flag (for starting fresh discovery)"""
        try:
            pipe = self.client.pipeline(transaction=False)
            pipe.delete(self.DISCOVERY_COMPLETED_FLAG)
            # Ensure workers don't short-circuit on fallback heuristics.
            pipe.set(self.DISCOVERY_FORCE_RUN_FLAG, "1", ex=86400)
            # Remove stale bootstrap sentinel so a root folder is always queued again.
            pipe.delete(self.DISCOVERY_ROOT_INITIALIZED_KEY)
            # Clear stale folder queue state from previous runs.
            pipe.delete(self.QUEUE_FOLDERS)
            # CRITICAL FIX: Clear folder mtime cache so mtime-differential does not
            # skip previously-scanned folders. Without this, full-mode restarts skip
            # every folder whose mtime hasn't changed, causing files in those folders
            # to go undiscovered even though their bloom-filter entry was cleared.
            pipe.delete(self.HASH_FOLDER_META)
            pipe.execute()
            logger.info("Discovery completion flag reset (folder mtime cache cleared)")
        except Exception as e:
            logger.error(f"Error resetting discovery flag: {e}")
    
    def is_file_processed(self, file_hash: str) -> bool:
        """Check if a file hash has already been processed"""
        return self.client.sismember(self.SET_FILE_HASHES, file_hash)
    
    def validate_metrics(self) -> Dict[str, Any]:
        """
        Validate and fix metrics (self-healing)
        Checks for consistency between counters and actual sets.
        """
        corrections = {}
        try:
            # 1. Validate Root Completed Count
            set_count = self.client.scard(self.SET_COMPLETED_FILE_IDS)
            counter_count = int(self.client.get(self.COUNTER_ROOT_COMPLETED) or 0)
            
            if abs(set_count - counter_count) > 0:
                logger.warning(f"Metric mismatch: Root Counter ({counter_count}) != Set Size ({set_count}). Fixing...")
                self.client.set(self.COUNTER_ROOT_COMPLETED, set_count)
                corrections['root_completed_fixed'] = True
                
            # 2. Validate Processing Counts (sanity check negative processing)
            stats = self.get_queue_stats()
            # If get_size_statistics would return negative processing, it stays 0, but we can detect it here
            discovered = int(self.client.get(self.COUNTER_DISCOVERED) or 0)
            completed_root = int(self.client.get(self.COUNTER_ROOT_COMPLETED) or 0)
            
            # Simple heuristic: Completed cannot be > Discovered (unless files were deleted from discovered)
            if completed_root > discovered:
                # This logic assumes we don't delete from discovered often. 
                # If we do, we should update discovered count. Only warning for now.
                logger.warning(f"Metric anomaly: Completed ({completed_root}) > Discovered ({discovered})")
                corrections['anomaly_detected'] = True
                
        except Exception as e:
            logger.error(f"Error validating metrics: {e}")
            
        return corrections

    def remove_worker_heartbeat(self, worker_id: str) -> None:
        """Remove heartbeat record for a worker"""
        try:
            self.client.hdel(self.HASH_WORKER_HEARTBEATS, worker_id)
        except Exception as e:
            logger.error(f"Error removing heartbeat for {worker_id}: {e}")

    def update_worker_heartbeat(self, worker_id: str) -> None:
        """Update heartbeat timestamp for a worker"""
        try:
            self.client.hset(self.HASH_WORKER_HEARTBEATS, worker_id, datetime.now().timestamp())
        except Exception as e:
            logger.error(f"Error updating heartbeat for {worker_id}: {e}")

    def get_worker_heartbeats(self) -> Dict[str, float]:
        """Get all worker heartbeats"""
        try:
            heartbeats = self.client.hgetall(self.HASH_WORKER_HEARTBEATS)
            result = {}
            for k, v in heartbeats.items():
                key = k.decode('utf-8') if isinstance(k, bytes) else k
                val = float(v)
                result[key] = val
            return result
        except Exception as e:
            logger.error(f"Error getting worker heartbeats: {e}")
            return {}

    def close(self) -> None:
        """Close Redis connections"""
        try:
            self.pool.disconnect()
        except Exception as e:
            logger.debug(f"Error disconnecting Redis pool: {e}")

    def reconcile_missing_files(self, dry_run: bool = False) -> Dict[str, int]:
        """
        Scan all discovered files and check if they are missing from all workflow sets.
        If missing, re-queue them.
        """
        stats = {'scanned': 0, 'missing': 0, 'requeued': 0}
        
        try:
            logger.info("Starting missing file reconciliation...")
            
            # 1. Get all known file IDs from HASH_FILE_PATHS (actually populated)
            # For 2k files, this is instant. For 10M, this needs batching. 
            # We'll use HSCAN on HASH_FILE_PATHS.
            
            cursor = 0
            while True:
                cursor, data = self.client.hscan(self.HASH_FILE_PATHS, cursor=cursor, count=1000)
                
                for file_id, meta_json in data.items():
                    stats['scanned'] += 1
                    file_id = file_id.decode() if isinstance(file_id, bytes) else file_id
                    
                    # 2. Check if Completed
                    if self.client.sismember(self.SET_COMPLETED_FILE_IDS, file_id):
                        continue
                        
                    # 3. Check if Failed
                    if self.client.hexists(self.HASH_FAILED, file_id):
                        continue
                    
                    # 4. Check if Queued or Processing
                    # This is harder because queues store JSONs, not IDs directly (except processing maps keys).
                    # But we can check file_hash if we had it.
                    # Actually, we can check if it's in the specialized processing maps? 
                    # No, that's iterating.
                    
                    # OPTIMIZATION:
                    # If the stats say "0 Queued" and "0 Processing", then ANY file not completed/failed is missing.
                    # But let's be safe.
                    
                    # We will assume if not Completed and not Failed, it *should* be in queue/processing.
                    # If we can't easily check queue (O(N)), we might just re-queue and let dedupe handle it?
                    # RedisQueueManager doesn't have strict unique-in-queue check, but Orchestrator might.
                    
                    # Let's try to verify if it's really missing.
                    # Check Processing Maps (fast-ish)
                    is_processing = False
                    # This check is expensive if we do it for every file.
                    # But for "Self Healing" usually triggered when queues are empty.
                    
                    # Let's just re-queue. If it's already processed, the worker might skip it (idempotency).
                    # Re-queueing as "Found missing file"
                    
                    stats['missing'] += 1
                    
                    if not dry_run:
                        try:
                            # Re-queue for extraction
                            file_data = json.loads(meta_json)
                            priority = 10 # High priority for recovery
                            
                            # Determine queue based on size
                            file_size = file_data.get('file_size', 0)
                            if file_size < 1024 * 1024:
                                queue = 'tiny'
                            elif file_size < 10 * 1024 * 1024:
                                queue = 'small'
                            elif file_size < 50 * 1024 * 1024:
                                queue = 'medium'
                            else:
                                queue = 'large'
                            
                            target_queue = f"{self.QUEUE_EXTRACTION}:{queue}"
                            
                            # Add back to queue
                            self.client.zadd(target_queue, {meta_json: priority})
                            stats['requeued'] += 1
                            logger.info(f"Re-queued missing file: {file_data.get('file_path')}")
                            
                        except Exception as e:
                            logger.error(f"Failed to re-queue file {file_id}: {e}")

                if cursor == 0:
                    break

            logger.info(f"Reconciliation complete. Stats: {stats}")
            
            # 5. Fix Counter Drift (Ghost Files)
            # If HASH_FILES was lost but counters persist, or if files were deleted without updating counters.
            try:
                real_discovered_count = self.client.hlen(self.HASH_FILES)
                counter_discovered = int(self.client.get(self.COUNTER_DISCOVERED) or 0)
                
                # If HASH_FILES is empty but we have completed files, implying data loss of metadata
                # We should set Discovered to max(real_discovered, completed + failed) to allow pipeline to settle
                
                completed_count = self.client.scard(self.SET_COMPLETED_FILE_IDS)
                failed_count = self.client.hlen(self.HASH_FAILED)
                in_pipeline_count = (
                    self.client.zcard(self.QUEUE_OCR) + 
                    self.client.llen(self.QUEUE_INDEXING) +
                    self.client.llen(self.QUEUE_TAGGING) +
                    sum(self.client.zcard(f"{self.QUEUE_EXTRACTION}:{cat}") for cat in ['tiny', 'small', 'medium', 'large'])
                )
                
                # The minimum valid discovered count is what we know about
                min_valid_discovered = completed_count + failed_count + in_pipeline_count
                
                # If the counter is higher than what we can account for (Ghost files)
                # AND we couldn't find them in HASH_FILES (missing metadata)
                if counter_discovered > min_valid_discovered:
                    diff = counter_discovered - min_valid_discovered
                    if diff > 0 and stats['requeued'] == 0:
                        # Only fix if we didn't find/requeue anything (which would have fixed it naturally)
                        logger.warning(f"Detected {diff} ghost files (Counter {counter_discovered} > Valid {min_valid_discovered}). Adjusting counter.")
                        self.client.set(self.COUNTER_DISCOVERED, min_valid_discovered)
                        stats['counter_fixed'] = diff
            except Exception as e:
                logger.error(f"Error checking counter drift: {e}")

            # 6. Fix Size Drift (Ghost Bytes)
            try:
                # Re-calculate in pipeline count accurately
                in_pipeline_count = (
                    self.client.zcard(self.QUEUE_OCR) + 
                    self.client.llen(self.QUEUE_INDEXING) +
                    self.client.llen(self.QUEUE_TAGGING) +
                    sum(self.client.zcard(f"{self.QUEUE_EXTRACTION}:{cat}") for cat in ['tiny', 'small', 'medium', 'large'])
                )
                
                # If queues are empty, and we assume processing is empty (because the user said so, and previous reconcile found nothing)
                if in_pipeline_count == 0:
                    disc_size = int(self.client.get(self.COUNTER_DISCOVERED_BYTES) or 0)
                    comp_size = int(self.client.get(self.COUNTER_COMPLETED_BYTES) or 0)
                    
                    if disc_size > comp_size:
                        diff_size = disc_size - comp_size
                        logger.warning(f"Pipeline count is 0, but size difference is {diff_size} bytes. Syncing Discovered Size to Completed Size.")
                        self.client.set(self.COUNTER_DISCOVERED_BYTES, comp_size)
                        stats['size_fixed'] = diff_size
                        
            except Exception as e:
                logger.error(f"Error checking size drift: {e}")
            
        except Exception as e:
            logger.error(f"Error during reconciliation: {e}")
            
        return stats


# Singleton instance with thread safety
_redis_queue_manager: Optional[RedisQueueManager] = None
_redis_queue_manager_lock = threading.Lock()


def get_redis_queue_manager() -> RedisQueueManager:
    """Get singleton Redis queue manager instance (thread-safe)"""
    global _redis_queue_manager
    
    if _redis_queue_manager is None:
        with _redis_queue_manager_lock:
            if _redis_queue_manager is None:
                _redis_queue_manager = RedisQueueManager()
    
    return _redis_queue_manager


def reset_redis_queue_manager() -> None:
    """Reset the singleton Redis queue manager instance"""
    global _redis_queue_manager
    
    with _redis_queue_manager_lock:
        if _redis_queue_manager is not None:
            try:
                _redis_queue_manager.close()
            except Exception as e:
                logger.warning(f"Error checking connection during reset: {e}")
            _redis_queue_manager = None
