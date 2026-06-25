"""
Discovery Worker - Orchestrates file scanning, hashing, and queue insertion
"""

import time
import signal
import threading
from pathlib import Path
from typing import Dict, Any
from datetime import datetime

from core.logging_manager import get_logger
from core.config_manager import get_config
from core.queue_manager import get_queue_manager
from core.constants import SizeCategory, Priority
from core.reporting_manager import (
    AuditEvent,
    FileStateRow,
    build_smart_id,
    derive_file_key,
    normalize_file_type,
    record_event,
    upsert_file_state,
)
from utils.bloom_filter import BloomFilter

from .file_scanner import FileScanner
from .hash_calculator import HashCalculator

logger = get_logger("discovery.worker")


class DiscoveryWorker:
    """Discovery worker process - scans filesystem and populates queues"""
    
    def __init__(self, worker_id):
        """Initialize discovery worker.
        
        Args:
            worker_id: Worker identifier (int or str). Will be converted to string.
        """
        # Convert to string for consistent usage (orchestrator may pass int or str)
        self.worker_id = str(worker_id)
        self.config = get_config()
        self.queue_manager = get_queue_manager()
        
        # Initialize components
        self.scanner = FileScanner()
        self.hash_calculator = HashCalculator()
        
        # Bloom filter persistence path
        self.bloom_filter_path = Path(self.config.paths.working_root) / "discovery" / f"bloom_filter_worker_{worker_id}.pkl"
        self.bloom_filter_path.parent.mkdir(parents=True, exist_ok=True)
        
        # Load or initialize Bloom filter
        self.bloom_filter = self._initialize_bloom_filter()
        
        # Statistics
        self.files_discovered = 0
        self.files_duplicate = 0        # Bloom+DB confirmed hash duplicates only
        self.files_already_indexed = 0  # H2: path+size+mtime match (indexed differently)
        self.files_queued = 0
        self.files_failed = 0
        self.start_time = None
        self.running = False
        
        # Batch configuration
        self.batch_size = self.config.discovery.batch_size
        
        # Setup signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
    
    def _initialize_bloom_filter(self) -> BloomFilter:
        """Initialize Bloom filter from saved file, database, or create new one"""
        bloom_config = self.config.deduplication['bloom_filter']
        
        # Try to load from disk first (fastest)
        if self.bloom_filter_path.exists():
            try:
                logger.info(f"Worker {self.worker_id}: Loading Bloom filter from {self.bloom_filter_path}")
                bloom = BloomFilter.load_from_file(str(self.bloom_filter_path))
                logger.info(f"Worker {self.worker_id}: Loaded Bloom filter with {bloom.elements_added:,} elements")
                return bloom
            except Exception as e:
                logger.warning(f"Worker {self.worker_id}: Failed to load Bloom filter: {e}. Creating new one.")
        
        # Create new Bloom filter
        bloom = BloomFilter(
            expected_elements=bloom_config['expected_elements'],
            false_positive_rate=bloom_config['false_positive_rate']
        )
        
        # Populate from database
        logger.info(f"Worker {self.worker_id}: Populating Bloom filter from database...")
        count = bloom.populate_from_database(self.queue_manager)
        logger.info(f"Worker {self.worker_id}: Loaded {count:,} hashes into Bloom filter")
        
        return bloom
    
    def run(self) -> None:
        """Main worker loop"""
        self.running = True
        self.start_time = time.time()
        last_log_time = time.time()

        # Start heartbeat thread
        heartbeat_thread = threading.Thread(target=self._heartbeat_loop, daemon=True)
        heartbeat_thread.start()
        
        # Check if discovery is already complete - if so, exit immediately
        if self.queue_manager.is_discovery_complete():
            logger.info(f"Worker {self.worker_id}: Discovery already marked COMPLETE, exiting")
            self._log_final_stats()
            return
        
        source_drive = self.config.paths.source_drive
        logger.info(f"Worker {self.worker_id}: Starting discovery on {source_drive}")
        
        # Bootstrap: If queue is empty, push root
        # RATE CONDITION FIX: Use SETNX to ensure only ONE worker pushes the root path
        try:
            root_init_key = getattr(
                self.queue_manager,
                "DISCOVERY_ROOT_INITIALIZED_KEY",
                "docsearch:discovery:root_initialized",
            )
            # Use a specialized lock key for bootstrapping to prevent duplicate root scans
            if self.queue_manager.client.setnx(root_init_key, "1"):
                logger.info(f"Worker {self.worker_id}: Bootstrapping with root: {source_drive}")
                self.queue_manager.push_folder(str(source_drive))
            elif self.queue_manager.client.llen(self.queue_manager.QUEUE_FOLDERS) == 0:
                # Self-heal stale bootstrap state (e.g. previous crash) by re-seeding root.
                # Guard with a short lock so only one worker performs this fallback.
                reseed_lock_key = f"{root_init_key}:reseed_lock"
                # M10: Extend reseed lock TTL to 120s — fixes race when multiple
                # workers start simultaneously and one is slow to push root.
                if self.queue_manager.client.set(reseed_lock_key, "1", nx=True, ex=120):
                    logger.warning(
                        f"Worker {self.worker_id}: Root init key exists but folder queue is empty; re-seeding root"
                    )
                    self.queue_manager.push_folder(str(source_drive))
        except Exception as e:
            logger.error(f"Worker {self.worker_id}: Error inspecting queue: {e}")
        
        batch = []
        
        try:
            while self.running:
                # 1. Pop folder
                folder_path = self.queue_manager.pop_folder()
                
                if not folder_path:
                    # Flush partial discoveries so low-volume incremental updates
                    # are not blocked behind the large batch threshold.
                    if batch:
                        self._process_batch(batch)
                        batch = []
                    # Queue empty, wait a bit
                    # Check for termination condition? For continuous service, just wait.
                    time.sleep(1)
                    continue
 
                # 2. Get cached mtime
                cached_mtime_str = self.queue_manager.get_folder_mtime(folder_path)
                cached_mtime = float(cached_mtime_str) if cached_mtime_str else None

                # 3. Scan folder (Differential)
                files, subfolders, new_mtime, skipped = self.scanner.scan_folder(folder_path, cached_mtime)

                # 4. Push Subfolders (Always traverse deep)
                for sub in subfolders:
                    self.queue_manager.push_folder(sub)

                if skipped:
                    pass
                else:
                    # 5. Process Files
                    for file_metadata in files:
                        # Optimization: Check if file exists in DB with same metadata (path+size+mtime)
                        # This avoids expensive hashing for unchanged files
                        if self.queue_manager.check_file_exists(
                            file_path=file_metadata['file_path'],
                            file_size=file_metadata['file_size'],
                            last_modified=file_metadata['modified_time']
                        ):
                            # H2: This is an already-indexed file (path+size+mtime match)
                            # counted separately from Bloom filter hash duplicates
                            self.files_already_indexed += 1
                            continue

                        # Calculate file hash
                        file_hash = self.hash_calculator.calculate_hash(file_metadata['file_path'])
                        if not file_hash:
                            continue
                        
                        file_metadata['file_hash'] = file_hash
                        self.files_discovered += 1
                        
                        # Log progress periodically (every 50 files)
                        if self.files_discovered % 50 == 0:
                            self._log_progress()
                        
                        # C3: Bloom filter positive — verify with DB to catch false positives
                        if self.bloom_filter.contains(file_hash):
                            # DB confirmation: only skip if the hash is actually in the DB
                            if hasattr(self.queue_manager, 'check_hash_exists') and \
                               self.queue_manager.check_hash_exists(file_hash):
                                self.files_duplicate += 1
                                continue
                            else:
                                # Bloom false positive — treat as new file
                                logger.debug(
                                    "Worker %s: Bloom false-positive for hash %s — allowing through",
                                    self.worker_id, file_hash[:12]
                                )
                        
                        # Not a duplicate
                        self.bloom_filter.add(file_hash)
                        batch.append(file_metadata)
                        
                        if len(batch) >= self.batch_size:
                            self._process_batch(batch)
                            batch = []

                    # 6. Update mtime in Redis
                    self.queue_manager.set_folder_mtime(folder_path, new_mtime)

            # Process remaining files
            if batch:
                self._process_batch(batch)
            
            self._log_final_stats()
            
        except (KeyboardInterrupt, SystemExit):
            logger.info(f"Worker {self.worker_id}: Interrupted by user")
        except Exception as e:
            logger.error(f"Worker {self.worker_id}: Fatal error: {e}", exc_info=True)
        finally:
            self._save_bloom_filter()
            self.running = False
            logger.info(f"Worker {self.worker_id}: Shutdown complete")
    
    def _process_batch(self, batch: list) -> None:
        """Process a batch of discovered files"""
        for file_metadata in batch:
            try:
                # Add to discovered_files table
                file_id = self.queue_manager.add_discovered_file(
                    file_path=file_metadata['file_path'],
                    file_name=file_metadata['file_name'],
                    file_size=file_metadata['file_size'],
                    file_extension=file_metadata['extension'],
                    file_hash=file_metadata['file_hash'],
                    last_modified=file_metadata['modified_time'],
                    created=file_metadata['created_time'],
                    # M2: compute size_cat once and reuse below — removes duplicate call
                    size_category=self._categorize_file_size(file_metadata['file_size']),
                    priority=Priority(file_metadata['priority'])
                )
                
                # Skip if duplicate (file_id is None — hash already registered)
                if file_id is None:
                    self.files_duplicate += 1
                    continue
                
                # Determine size category for routing (M2: reuse from batch entry — used below)
                size_category = self._categorize_file_size(file_metadata['file_size'])
                
                # Add to extraction queue
                self.queue_manager.add_to_extraction_queue(
                    file_id=file_id,
                    file_path=file_metadata['file_path'],
                    file_size=file_metadata['file_size'],
                    size_category=size_category,
                    priority=Priority(file_metadata['priority'])
                )
                
                self.files_queued += 1
                self._record_discovery_event(file_metadata, file_id, size_category)
                
            except Exception as e:
                logger.error(f"Worker {self.worker_id}: Error processing file "
                           f"{file_metadata['file_path']}: {e}")
                try:
                    file_path = str(file_metadata.get("file_path", ""))
                    file_name = str(file_metadata.get("file_name", Path(file_path).name))
                    file_hash = str(file_metadata.get("file_hash", ""))
                    file_key = derive_file_key(file_hash=file_hash, file_path=file_path)
                    file_type = normalize_file_type(str(file_metadata.get("extension", "")), file_name=file_name, file_path=file_path)
                    processed_on = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
                    smart_id = build_smart_id(file_key=file_key, when_iso=processed_on)
                    record_event(
                        AuditEvent(
                            event_time=processed_on,
                            file_key=file_key,
                            smart_id=smart_id,
                            file_name=file_name,
                            file_path=file_path,
                            stage="discovery",
                            status="failed",
                            worker_id=self.worker_id,
                            file_type=file_type,
                            error_type="DISCOVERY_ERROR",
                            error_message=str(e),
                        )
                    )
                    upsert_file_state(
                        FileStateRow(
                            file_key=file_key,
                            smart_id=smart_id,
                            file_name=file_name,
                            current_status="failed",
                            processed_on=processed_on,
                            file_type=file_type,
                            file_size=int(file_metadata.get("file_size", 0) or 0),
                            file_path=file_path,
                            updated_at=processed_on,
                            source_stage="discovery",
                            worker_id=self.worker_id,
                        )
                    )
                except Exception:
                    pass

    def _record_discovery_event(self, file_metadata: Dict[str, Any], file_id: int, size_category: SizeCategory) -> None:
        """Write discovery-stage audit event and initial state row."""
        try:
            file_path = str(file_metadata.get("file_path", ""))
            file_name = str(file_metadata.get("file_name", Path(file_path).name))
            file_hash = str(file_metadata.get("file_hash", ""))
            file_key = derive_file_key(file_hash=file_hash, file_id=file_id, file_path=file_path)
            file_type = normalize_file_type(str(file_metadata.get("extension", "")), file_name=file_name, file_path=file_path)
            processed_on = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
            smart_id = build_smart_id(file_key=file_key, department="", when_iso=processed_on)

            record_event(
                AuditEvent(
                    event_time=processed_on,
                    file_key=file_key,
                    smart_id=smart_id,
                    file_name=file_name,
                    file_path=file_path,
                    stage="discovery",
                    status="completed",
                    worker_id=self.worker_id,
                    file_type=file_type,
                    payload_json={
                        "size_category": size_category.value,
                        "priority": file_metadata.get("priority"),
                    },
                )
            )

            upsert_file_state(
                FileStateRow(
                    file_key=file_key,
                    smart_id=smart_id,
                    file_name=file_name,
                    current_status="pending",
                    processed_on=processed_on,
                    file_type=file_type,
                    file_size=int(file_metadata.get("file_size", 0) or 0),
                    file_path=file_path,
                    updated_at=processed_on,
                    source_stage="discovery",
                    worker_id=self.worker_id,
                )
            )
        except Exception as exc:
            logger.debug("Worker %s: discovery audit write failed: %s", self.worker_id, exc)
    
    def _categorize_file_size(self, size: int) -> SizeCategory:
        """Categorize file by size for worker routing"""
        thresholds = self.config.discovery.size_categories
        
        if size < thresholds['tiny']:
            return SizeCategory.TINY
        elif size < thresholds['small']:
            return SizeCategory.SMALL
        elif size < thresholds['medium']:
            return SizeCategory.MEDIUM
        else:
            return SizeCategory.LARGE
    
    def _log_progress(self) -> None:
        """Log progress statistics with clear formatting"""
        elapsed = time.time() - self.start_time
        rate = self.files_discovered / elapsed if elapsed > 0 else 0
        new_files = self.files_queued
        
        # Calculate percentage of files that are new vs already indexed
        total_found = self.files_discovered
        pct_new = (self.files_queued / total_found * 100) if total_found > 0 else 0
        pct_dup = (self.files_duplicate / total_found * 100) if total_found > 0 else 0
        
        logger.info(
            f"[Discovery-{self.worker_id}] "
            f"Scanned: {self.files_discovered:,} ({rate:.0f}/s) | "
            f"New: {new_files:,} ({pct_new:.0f}%) | "
            f"Skip: {self.files_duplicate:,} ({pct_dup:.0f}%)"
        )
    
    def _log_final_stats(self) -> None:
        """Log final statistics and mark discovery complete"""
        elapsed = time.time() - self.start_time
        avg_rate = self.files_discovered / elapsed if elapsed > 0 else 0
        
        scanner_stats = self.scanner.get_stats()
        hash_stats = self.hash_calculator.get_stats()

        # Guard against BloomFilter implementations without __len__ to avoid fatal TypeError
        try:
            bloom_elements = len(self.bloom_filter)
        except Exception:
            bloom_elements = getattr(self.bloom_filter, "elements_added", 0)
        
        logger.info(
            f"\n{'='*60}\n"
            f"Worker {self.worker_id} - Discovery Complete\n"
            f"{'='*60}\n"
            f"  Files Scanned:           {scanner_stats['files_scanned']:,}\n"
            f"  Files Skipped:           {scanner_stats['files_skipped']:,}\n"
            f"  Files Discovered:        {self.files_discovered:,}\n"
            f"  Already Indexed:         {self.files_duplicate:,}\n"
            f"  New Files Queued:        {self.files_queued:,}\n"
            f"  Hash Errors:             {hash_stats['errors']}\n"
            f"  Total Time:              {elapsed:.1f}s\n"
            f"  Average Rate:            {avg_rate:.0f} files/sec\n"
            f"  Bloom Filter Elements:   {bloom_elements:,}\n"
            f"{'='*60}"
        )
        
        # Mark discovery complete only when the folder queue is empty
        # This prevents premature completion when other workers are still running
        try:
            folder_queue_len = self.queue_manager.client.llen(self.queue_manager.QUEUE_FOLDERS)
            if folder_queue_len == 0:
                logger.info(f"Worker {self.worker_id}: Folder queue empty, marking discovery as complete")
                self.queue_manager.mark_discovery_complete()
            else:
                logger.info(f"Worker {self.worker_id}: {folder_queue_len} folders still in queue, not marking complete")
        except Exception as e:
            logger.warning(f"Worker {self.worker_id}: Could not check folder queue: {e}")
    
    def _signal_handler(self, signum, frame) -> None:
        """Handle shutdown signals gracefully"""
        logger.info(f"Worker {self.worker_id}: Received signal {signum}, shutting down gracefully...")
        self.stop()
    
    def _save_bloom_filter(self) -> None:
        """Save Bloom filter to disk for resume"""
        try:
            logger.info(f"Worker {self.worker_id}: Saving Bloom filter to {self.bloom_filter_path}")
            self.bloom_filter.save_to_file(str(self.bloom_filter_path))
            logger.info(f"Worker {self.worker_id}: Bloom filter saved ({self.bloom_filter.elements_added:,} elements)")
        except Exception as e:
            logger.error(f"Worker {self.worker_id}: Failed to save Bloom filter: {e}")
    
    def stop(self) -> None:
        """Stop the worker gracefully"""
        logger.info(f"Worker {self.worker_id}: Stop requested")
        self.running = False
        self._save_bloom_filter()
    
    def get_stats(self) -> Dict[str, Any]:
        """Get current statistics"""
        elapsed = time.time() - self.start_time if self.start_time else 0
        
        return {
            'worker_id': self.worker_id,
            'running': self.running,
            'files_discovered': self.files_discovered,
            'files_duplicate': self.files_duplicate,
            'files_queued': self.files_queued,
            'elapsed_seconds': elapsed,
            'rate_per_second': self.files_discovered / elapsed if elapsed > 0 else 0
        }

    def _heartbeat_loop(self) -> None:
        """Send heartbeat periodically"""
        while self.running:
            try:
                self.queue_manager.update_worker_heartbeat(self.worker_id)
            except Exception as exc:
                # L7: Log at debug rather than silently swallowing heartbeat errors
                logger.debug(
                    "Worker %s: Heartbeat update failed (non-fatal): %s",
                    self.worker_id, exc
                )
            time.sleep(10)
