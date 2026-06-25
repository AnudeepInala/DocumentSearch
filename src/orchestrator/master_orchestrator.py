"""
Master Orchestrator - Main coordinator for all workers
"""

import time
import multiprocessing as mp
import gc
from typing import Dict, Any
from pathlib import Path
import signal
import glob

from core.logging_manager import get_logger
from core.config_manager import get_config
from core.queue_manager import get_queue_manager, is_using_redis, try_switch_to_redis
from core.constants import SizeCategory

from discovery.discovery_worker import DiscoveryWorker
from extraction.extraction_worker import ExtractionWorker
from indexing.indexing_worker import IndexingWorker
from ocr.ocr_worker import OCRWorker
from tagging.tagging_worker import TaggingWorker

from .health_monitor import HealthMonitor
from .resource_monitor import ResourceMonitor
from .checkpoint_manager import CheckpointManager

logger = get_logger("orchestrator")


class MasterOrchestrator:
    """Master orchestrator - spawns and manages all workers"""
    
    def __init__(self):
        self.config = get_config()
        self.queue_manager = get_queue_manager()
        
        # Components
        self.health_monitor = HealthMonitor()
        self.resource_monitor = ResourceMonitor()
        self.checkpoint_manager = CheckpointManager()
        self.recovery_manager = None  # Lazy load to avoid circular imports if any
        
        # Worker processes
        self.workers: Dict[str, mp.Process] = {}
        self.running = False
        self._stopping = False
        self._tika_port_index: Dict[str, int] = {}
        self._resource_paused = False  # H5: track if we've paused workers due to resource exhaustion
        
        # H2: Crash backoff tracking — (crash_count, first_crash_time)
        self._crash_history: Dict[str, list] = {}  # worker_id -> [timestamp, timestamp, ...]
        self._max_crashes = 5  # Max crashes before giving up
        self._crash_window = 300  # 5 minute window
        
        # Setup signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
    
    def start(self, mode: str = 'full') -> None:
        """
        Start the system
        
        Args:
            mode: Operation mode (full, resume, incremental)
        """
        self.running = True
        
        logger.info(f"Starting Enterprise Document Search System in {mode} mode")
        logger.info(f"{'=' * 80}")
        
        # Load checkpoint if resuming
        checkpoint_data = None
        if mode == 'resume':
            checkpoint_data = self.checkpoint_manager.load_checkpoint()
            if checkpoint_data:
                logger.info(
                    "Resume mode: loaded checkpoint from %s",
                    checkpoint_data.get('created_at') or checkpoint_data.get('timestamp', 'unknown')
                )
            else:
                logger.warning("Resume mode: no checkpoint found, continuing with live queue state")
        
        # In 'full' mode, reset discovery completion flag AND Bloom filters to force re-discovery
        if mode == 'full':
            self.queue_manager.reset_discovery_completion_flag()
            self._clear_bloom_filter_files()
            logger.info("Full mode: discovery will run again with fresh Bloom filters")

        # Self-heal completed counters after Redis restarts/partial key loss
        # and repair OCR counter drift from earlier non-idempotent increments.
        try:
            if hasattr(self.queue_manager, 'client') and hasattr(self.queue_manager, 'COUNTER_DUPLICATES'):
                completed_count = int(self.queue_manager.client.hlen(self.queue_manager.HASH_COMPLETED) or 0)
                has_completed_data = completed_count > 0
                dup_counter_exists = self.queue_manager.client.exists(self.queue_manager.COUNTER_DUPLICATES) == 1
                ocr_counter_exists = self.queue_manager.client.exists(self.queue_manager.COUNTER_OCR_COMPLETED) == 1
                ocr_counter = int(self.queue_manager.client.get(self.queue_manager.COUNTER_OCR_COMPLETED) or 0)

                should_rebuild = (
                    has_completed_data and (
                        (not dup_counter_exists)
                        or (not ocr_counter_exists)
                        or (ocr_counter > completed_count)
                    )
                )

                if should_rebuild:
                    logger.warning(
                        "Counter drift/missing key detected (completed=%s, ocr=%s). Rebuilding completed counters...",
                        completed_count,
                        ocr_counter,
                    )
                    self.queue_manager.initialize_completed_counters()
        except Exception as e:
            logger.warning(f"Completed counter self-heal skipped: {e}")

        # Continuous discovery settings from config
        continuous_discovery = bool(getattr(self.config.discovery, 'continuous_discovery', False))
        
        # Spawn discovery workers: check if there's still work to do
        folder_queue_len = 0
        try:
            if hasattr(self.queue_manager, 'client'):
                folder_queue_len = self.queue_manager.client.llen(
                    getattr(self.queue_manager, 'QUEUE_FOLDERS', 'docsearch:queue:folders')
                )
        except Exception:
            pass

        discovery_complete = self.queue_manager.is_discovery_complete()

        # Continuous mode should keep discovery workers alive across restarts.
        # When queue is empty and discovery is marked complete, clear the completion
        # state so workers can bootstrap from the source root again.
        if continuous_discovery and discovery_complete and folder_queue_len == 0:
            logger.info(
                "Continuous discovery enabled: resetting discovery completion flag "
                "and starting discovery workers"
            )
            self.queue_manager.reset_discovery_completion_flag()
            discovery_complete = False

        if discovery_complete and folder_queue_len == 0 and not continuous_discovery:
            logger.info("Discovery already complete and folder queue empty, skipping discovery workers")
        else:
            # If discovery was marked complete but folders remain, reset the flag
            if discovery_complete and folder_queue_len > 0:
                logger.warning(
                    f"Discovery was marked complete but {folder_queue_len} folders still in queue! "
                    f"Resetting discovery flag to process remaining folders."
                )
                self.queue_manager.reset_discovery_completion_flag()
                discovery_complete = False
            self._spawn_discovery_workers(force=continuous_discovery)
        
        # Always spawn extraction, indexing, and OCR workers
        self._spawn_extraction_workers()
        self._spawn_indexing_workers()
        self._spawn_ocr_workers()
        self._spawn_tagging_workers()
        
        # Run initial recovery scan to catch any zombie tasks from previous run
        try:
            from .recovery_manager import RecoveryManager
            self.recovery_manager = RecoveryManager()
            
            # M8 FIX: Run recovery in background thread to avoid blocking startup
            # This scan is O(N) and can take a long time on large datasets
            logger.info("Starting initial recovery scan in background...")
            import threading
            recovery_thread = threading.Thread(
                target=self.recovery_manager.recover_all,
                name="StartupRecoveryScan",
                daemon=True
            )
            recovery_thread.start()
            
            # Also reset any stale processing items immediately
            logger.info("Resetting stale processing items from previous run...")
            self.queue_manager.reset_stale_processing(timeout_minutes=5)
            
        except Exception as e:
            logger.error(f"Recovery scan failed on startup: {e}")
        
        logger.info(f"All workers spawned. Total: {len(self.workers)}")
        logger.info(f"{'=' * 80}")
        
        # Start monitoring loops
        try:
            self._main_loop()
        except KeyboardInterrupt:
            logger.info("Keyboard interrupt received")
        finally:
            self.stop()
    
    def _clear_bloom_filter_files(self) -> None:
        """Clear all Bloom filter files to force fresh discovery"""
        bloom_dir = Path(self.config.paths.working_root) / "discovery"
        if bloom_dir.exists():
            pattern = str(bloom_dir / "bloom_filter_worker_*.pkl")
            files = glob.glob(pattern)
            for f in files:
                try:
                    Path(f).unlink()
                    logger.info(f"Deleted Bloom filter: {f}")
                except Exception as e:
                    logger.warning(f"Failed to delete {f}: {e}")
            if files:
                logger.info(f"Cleared {len(files)} Bloom filter files for fresh discovery")
    
    def _spawn_discovery_workers(self, force: bool = False) -> None:
        """Spawn discovery workers"""
        # Check if discovery is already complete
        if self.queue_manager.is_discovery_complete() and not force:
            logger.info("Discovery already complete (status=COMPLETE), skipping discovery worker spawn")
            return
        
        num_workers = self.config.discovery.num_workers
        
        logger.info(f"Spawning {num_workers} discovery workers...")
        
        for i in range(num_workers):
            worker_id = f"discovery-{i+1}"
            
            process = mp.Process(
                target=self._run_discovery_worker,
                args=(i+1,),
                name=worker_id
            )
            process.daemon = True
            process.start()
            self.workers[worker_id] = process
            
            logger.info(f"  Started {worker_id} (PID: {process.pid})")

    def _next_tika_port(self, pool_name: str) -> int:
        """Return next configured Tika port for a pool using round-robin."""
        pool = self.config.extraction.pools[pool_name]
        ports = list(pool.tika_ports or [])
        if not ports:
            raise ValueError(f"No tika_ports configured for pool '{pool_name}'")
        idx = self._tika_port_index.get(pool_name, 0)
        port = ports[idx % len(ports)]
        self._tika_port_index[pool_name] = (idx + 1) % len(ports)
        return port
    
    def _spawn_extraction_workers(self) -> None:
        """Spawn extraction workers"""
        pools = self.config.extraction.pools
        
        logger.info("Spawning extraction workers...")
        
        worker_id_counter = 1
        
        # Fast track workers
        for i in range(pools['fast_track'].num_workers):
            worker_id = f"extraction-fast-{worker_id_counter}"
            process = mp.Process(
                target=self._run_extraction_worker,
                args=(worker_id, 'fast_track', SizeCategory.TINY, self._next_tika_port('fast_track')),
                name=worker_id
            )
            process.daemon = True
            process.start()
            self.workers[worker_id] = process
            logger.info(f"  Started {worker_id} (PID: {process.pid})")
            worker_id_counter += 1
        
        # Standard track workers
        for i in range(pools['standard_track'].num_workers):
            worker_id = f"extraction-std-{worker_id_counter}"
            process = mp.Process(
                target=self._run_extraction_worker,
                args=(worker_id, 'standard_track', SizeCategory.SMALL, self._next_tika_port('standard_track')),
                name=worker_id
            )
            process.daemon = True
            process.start()
            self.workers[worker_id] = process
            logger.info(f"  Started {worker_id} (PID: {process.pid})")
            worker_id_counter += 1
        
        # Heavy track workers
        for i in range(pools['heavy_track'].num_workers):
            worker_id = f"extraction-heavy-{worker_id_counter}"
            process = mp.Process(
                target=self._run_extraction_worker,
                args=(worker_id, 'heavy_track', SizeCategory.MEDIUM, self._next_tika_port('heavy_track')),
                name=worker_id
            )
            process.daemon = True
            process.start()
            self.workers[worker_id] = process
            logger.info(f"  Started {worker_id} (PID: {process.pid})")
            worker_id_counter += 1
        
        # Extreme track workers
        for i in range(pools['extreme_track'].num_workers):
            worker_id = f"extraction-extreme-{worker_id_counter}"
            process = mp.Process(
                target=self._run_extraction_worker,
                args=(worker_id, 'extreme_track', SizeCategory.LARGE, self._next_tika_port('extreme_track')),
                name=worker_id
            )
            process.daemon = True
            process.start()
            self.workers[worker_id] = process
            logger.info(f"  Started {worker_id} (PID: {process.pid})")
            worker_id_counter += 1
    
    def _spawn_indexing_workers(self) -> None:
        """Spawn indexing workers"""
        num_workers = self.config.indexing.num_workers
        
        logger.info(f"Spawning {num_workers} indexing workers...")
        
        for i in range(num_workers):
            worker_id = f"indexing-{i+1}"
            
            process = mp.Process(
                target=self._run_indexing_worker,
                args=(worker_id,),
                name=worker_id
            )
            process.daemon = True
            process.start()
            self.workers[worker_id] = process
            
            logger.info(f"  Started {worker_id} (PID: {process.pid})")
    
    def _spawn_ocr_workers(self) -> None:
        """Spawn OCR workers with staggered startup to avoid memory pressure"""
        num_workers = self.config.ocr.initial_workers

        logger.info(f"Spawning {num_workers} OCR workers...")

        for i in range(num_workers):
            worker_id = f"ocr-{i+1}"

            process = mp.Process(
                target=self._run_ocr_worker,
                args=(worker_id,),
                name=worker_id
            )
            process.daemon = True
            process.start()
            self.workers[worker_id] = process

            logger.info(f"  Started {worker_id} (PID: {process.pid})")

            # Stagger worker starts so PaddleOCR model loading doesn't
            # exhaust memory when all workers init simultaneously
            if i < num_workers - 1:
                time.sleep(8)

    def _spawn_tagging_workers(self) -> None:
        """Spawn tagging workers."""
        num_workers = int(getattr(self.config.tagging, "workers", 2) or 2)
        logger.info(f"Spawning {num_workers} tagging workers...")
        for i in range(num_workers):
            worker_id = f"tagging-{i+1}"
            process = mp.Process(
                target=self._run_tagging_worker,
                args=(worker_id,),
                name=worker_id
            )
            process.daemon = True
            process.start()
            self.workers[worker_id] = process
            logger.info(f"  Started {worker_id} (PID: {process.pid})")
    
    @staticmethod
    def _run_discovery_worker(worker_id: int):
        """Run discovery worker in separate process"""
        try:
            # Fix Python path for multiprocessing workers
            import sys
            from pathlib import Path
            src_dir = Path(__file__).parent.parent
            if str(src_dir) not in sys.path:
                sys.path.insert(0, str(src_dir))
            
            worker = DiscoveryWorker(worker_id)
            worker.run()
        except (KeyboardInterrupt, SystemExit):
            pass
    
    @staticmethod
    def _run_extraction_worker(worker_id: str, pool_type: str, size_category: SizeCategory, tika_port: int):
        """Run extraction worker in separate process"""
        try:
            # Fix Python path for multiprocessing workers
            import sys
            from pathlib import Path
            src_dir = Path(__file__).parent.parent
            if str(src_dir) not in sys.path:
                sys.path.insert(0, str(src_dir))
            
            worker = ExtractionWorker(worker_id, pool_type, size_category, tika_port)
            worker.run()
        except (KeyboardInterrupt, SystemExit):
            pass
    
    @staticmethod
    def _run_indexing_worker(worker_id: str):
        """Run indexing worker in separate process"""
        try:
            # Fix Python path for multiprocessing workers
            import sys
            from pathlib import Path
            src_dir = Path(__file__).parent.parent
            if str(src_dir) not in sys.path:
                sys.path.insert(0, str(src_dir))
            
            worker = IndexingWorker(worker_id)
            worker.run()
        except (KeyboardInterrupt, SystemExit):
            pass
    
    @staticmethod
    def _run_ocr_worker(worker_id: str):
        """Run OCR worker in separate process"""
        try:
            # Fix Python path for multiprocessing workers
            import sys
            from pathlib import Path
            src_dir = Path(__file__).parent.parent
            if str(src_dir) not in sys.path:
                sys.path.insert(0, str(src_dir))
            
            worker = OCRWorker(worker_id)
            worker.run()
        except (KeyboardInterrupt, SystemExit):
            pass

    @staticmethod
    def _run_tagging_worker(worker_id: str):
        """Run tagging worker in separate process."""
        try:
            import sys
            from pathlib import Path
            src_dir = Path(__file__).parent.parent
            if str(src_dir) not in sys.path:
                sys.path.insert(0, str(src_dir))

            worker = TaggingWorker(worker_id)
            worker.run()
        except (KeyboardInterrupt, SystemExit):
            pass
    
    def _main_loop(self) -> None:
        """Main monitoring loop"""
        checkpoint_interval = self.config.orchestrator.checkpoint['interval_seconds']
        last_checkpoint = time.time()
        last_stale_cleanup = time.time()
        last_redis_check = time.time()
        last_discovery_rescan = time.time()  # Track last discovery rescan
        last_realloc_check = time.time()  # Track dynamic reallocation
        stale_cleanup_interval = 120  # Check for stale items every 2 minutes
        stale_timeout_minutes = 30  # Match processing-key TTL (30m) to avoid requeuing active work
        redis_check_interval = 60  # Check for Redis availability every 60 seconds
        realloc_interval = 60  # Check for idle workers every 60 seconds
        
        # Continuous discovery settings from config
        continuous_discovery = getattr(self.config.discovery, 'continuous_discovery', False)
        rescan_interval_seconds = getattr(self.config.discovery, 'rescan_interval_seconds', 300)
        
        # Adjust main loop sleep based on continuous discovery setting
        main_loop_sleep = min(10, rescan_interval_seconds) if continuous_discovery else 30
        
        while self.running:
            try:
                # Check worker health and respawn dead workers
                self._check_workers()
                
                # Monitor system resources
                resources = self.resource_monitor.check_resources()
                
                if resources.get('critical', False):
                    logger.error("CRITICAL: System resources exhausted! Pausing ingestion workers.")
                    if not self._resource_paused:
                        self._resource_paused = True
                        # Stop discovery and extraction workers to reduce load
                        for worker_id, process in list(self.workers.items()):
                            if worker_id.startswith(('discovery-', 'extraction-')):
                                logger.warning(f"Pausing {worker_id} due to resource exhaustion")
                                process.terminate()
                elif self._resource_paused:
                    # Resources recovered — clear the flag so workers can be respawned
                    logger.info("Resources recovered. Workers will be respawned on next check.")
                    self._resource_paused = False
                
                # Try to switch to Redis if currently using SQLite fallback
                if not is_using_redis() and (time.time() - last_redis_check) >= redis_check_interval:
                    if try_switch_to_redis():
                        logger.info("Successfully migrated from SQLite to Redis backend")
                        # Update queue_manager reference
                        self.queue_manager = get_queue_manager()
                    last_redis_check = time.time()
                
                # Periodic cleanup of stale processing items
                if time.time() - last_stale_cleanup >= stale_cleanup_interval:
                    try:
                        # Handle different signatures for SQLite vs Redis
                        if hasattr(self.queue_manager, 'client'):  # Redis backend
                            reset_counts = self.queue_manager.reset_stale_processing(
                                timeout_minutes=stale_timeout_minutes
                            )
                            if any(reset_counts.values()):
                                logger.info(f"Reset stale items: {reset_counts}")
                            
                            # Clean up zombie items (indexed but not marked complete)
                            zombie_count = self.queue_manager.cleanup_zombie_processing()
                            if zombie_count > 0:
                                logger.info(f"Cleaned {zombie_count} zombie processing items")
                        else:  # SQLite backend
                            logger.debug("Stale cleanup skipped for SQLite backend (not supported)")
                            
                    except Exception as e:
                        logger.warning(f"Stale cleanup failed: {e}")
                    last_stale_cleanup = time.time()
                
                # Periodic discovery rescan (continuous monitoring for new files)
                # Only push root if discovery workers exist to consume it
                if continuous_discovery and (time.time() - last_discovery_rescan) >= rescan_interval_seconds:
                    try:
                        has_discovery_workers = any(
                            wid.startswith('discovery-') for wid in self.workers
                        )
                        if has_discovery_workers:
                            source_drive = self.config.paths.source_drive
                            self.queue_manager.push_folder(str(source_drive))
                            logger.info(f"Continuous discovery: scanning {source_drive} for new files")
                        else:
                            logger.debug("Continuous discovery: no discovery workers alive, skipping push")
                    except Exception as e:
                        logger.error(f"Error during continuous discovery rescan: {e}")
                    last_discovery_rescan = time.time()
                
                # Create checkpoint
                if time.time() - last_checkpoint >= checkpoint_interval:
                    self.checkpoint_manager.create_checkpoint()
                    last_checkpoint = time.time()
                
                # Check if work is complete and do dynamic reallocation
                stats = self.queue_manager.get_queue_stats()
                if self._is_work_complete(stats):
                    logger.info("All queues empty - work complete!")
                
                # Dynamic worker reallocation — move idle workers to bottleneck stages
                if time.time() - last_realloc_check >= realloc_interval:
                    try:
                        self._reallocate_idle_workers(stats)
                    except Exception as e:
                        logger.warning(f"Dynamic reallocation failed: {e}")
                    last_realloc_check = time.time()
                
                # Periodic memory cleanup
                gc.collect()
                
                time.sleep(main_loop_sleep)  # Check frequently when continuous discovery enabled
                
            except Exception as e:
                logger.error(f"Error in main loop: {e}", exc_info=True)
                time.sleep(5)
    
    def _check_workers(self) -> None:
        """Check health of worker processes and respawn dead ones"""
        for worker_id, process in list(self.workers.items()):
            if not process.is_alive():
                exit_code = process.exitcode
                logger.warning(f"Worker {worker_id} (PID: {process.pid}) has died (exit code: {exit_code})")
                
                # Remove from tracking
                del self.workers[worker_id]
                
                # H2: Crash backoff — don't respawn if crashing repeatedly
                now = time.time()
                if worker_id not in self._crash_history:
                    self._crash_history[worker_id] = []
                
                # Purge old crash timestamps outside the window
                self._crash_history[worker_id] = [
                    t for t in self._crash_history[worker_id]
                    if (now - t) < self._crash_window
                ]
                self._crash_history[worker_id].append(now)
                
                if len(self._crash_history[worker_id]) >= self._max_crashes:
                    logger.error(
                        f"Worker {worker_id} has crashed {len(self._crash_history[worker_id])} times "
                        f"in {self._crash_window}s. NOT respawning to prevent crash loop."
                    )
                    continue
                
                # H5: Don't respawn discovery/extraction workers while resources are paused
                if self._resource_paused and worker_id.startswith(('discovery-', 'extraction-')):
                    logger.warning(f"Skipping respawn of {worker_id} — resources are exhausted")
                    continue
                
                # Auto-respawn the worker
                self._respawn_worker(worker_id)
    
    def _respawn_worker(self, worker_id: str) -> None:
        """Respawn a dead worker based on its ID"""
        try:
            if worker_id.startswith('discovery-'):
                # Parse worker number from ID like "discovery-1"
                worker_num = int(worker_id.split('-')[1])
                process = mp.Process(
                    target=self._run_discovery_worker,
                    args=(worker_num,),
                    name=worker_id
                )
                process.daemon = True
                process.start()
                self.workers[worker_id] = process
                logger.info(f"Respawned {worker_id} (PID: {process.pid})")
                
            elif worker_id.startswith('extraction-fast-'):
                process = mp.Process(
                    target=self._run_extraction_worker,
                    args=(worker_id, 'fast_track', SizeCategory.TINY, self._next_tika_port('fast_track')),
                    name=worker_id
                )
                process.daemon = True
                process.start()
                self.workers[worker_id] = process
                logger.info(f"Respawned {worker_id} (PID: {process.pid})")
                
            elif worker_id.startswith('extraction-std-'):
                process = mp.Process(
                    target=self._run_extraction_worker,
                    args=(worker_id, 'standard_track', SizeCategory.SMALL, self._next_tika_port('standard_track')),
                    name=worker_id
                )
                process.daemon = True
                process.start()
                self.workers[worker_id] = process
                logger.info(f"Respawned {worker_id} (PID: {process.pid})")
                
            elif worker_id.startswith('extraction-heavy-'):
                process = mp.Process(
                    target=self._run_extraction_worker,
                    args=(worker_id, 'heavy_track', SizeCategory.MEDIUM, self._next_tika_port('heavy_track')),
                    name=worker_id
                )
                process.daemon = True
                process.start()
                self.workers[worker_id] = process
                logger.info(f"Respawned {worker_id} (PID: {process.pid})")
                
            elif worker_id.startswith('extraction-extreme-'):
                process = mp.Process(
                    target=self._run_extraction_worker,
                    args=(worker_id, 'extreme_track', SizeCategory.LARGE, self._next_tika_port('extreme_track')),
                    name=worker_id
                )
                process.daemon = True
                process.start()
                self.workers[worker_id] = process
                logger.info(f"Respawned {worker_id} (PID: {process.pid})")
                
            elif worker_id.startswith('indexing-'):
                process = mp.Process(
                    target=self._run_indexing_worker,
                    args=(worker_id,),
                    name=worker_id
                )
                process.daemon = True
                process.start()
                self.workers[worker_id] = process
                logger.info(f"Respawned {worker_id} (PID: {process.pid})")
                
            elif worker_id.startswith('ocr-'):
                process = mp.Process(
                    target=self._run_ocr_worker,
                    args=(worker_id,),
                    name=worker_id
                )
                process.daemon = True
                process.start()
                self.workers[worker_id] = process
                logger.info(f"Respawned {worker_id} (PID: {process.pid})")
            elif worker_id.startswith('tagging-'):
                process = mp.Process(
                    target=self._run_tagging_worker,
                    args=(worker_id,),
                    name=worker_id
                )
                process.daemon = True
                process.start()
                self.workers[worker_id] = process
                logger.info(f"Respawned {worker_id} (PID: {process.pid})")
                
        except Exception as e:
            logger.error(f"Failed to respawn worker {worker_id}: {e}")
    
    def _is_work_complete(self, stats: Dict[str, Any]) -> bool:
        """Check if all work is complete"""
        # Work is complete only when all active pipeline stages are drained.
        discovery_pending = stats.get('discovery', {}).get('pending', 0)
        discovery_processing = stats.get('discovery', {}).get('processing', 0)
        extraction_pending = stats.get('extraction_total', {}).get('pending', 0)
        extraction_processing = stats.get('extraction_total', {}).get('processing', 0)
        indexing_pending = stats.get('indexing', {}).get('pending', 0)
        indexing_processing = stats.get('indexing', {}).get('processing', 0)
        ocr_pending = stats.get('ocr', {}).get('pending', 0)
        ocr_processing = stats.get('ocr', {}).get('processing', 0)
        tagging_pending = stats.get('tagging', {}).get('pending', 0)
        tagging_processing = stats.get('tagging', {}).get('processing', 0)
        
        return (
            discovery_pending == 0 and discovery_processing == 0 and
            extraction_pending == 0 and extraction_processing == 0 and
            indexing_pending == 0 and indexing_processing == 0 and
            ocr_pending == 0 and ocr_processing == 0 and
            tagging_pending == 0 and tagging_processing == 0
        )
    
    def stop(self) -> None:
        """Stop all workers gracefully"""
        if getattr(self, "_stopping", False):
            return
        self._stopping = True
        self.running = False
        
        logger.info("Stopping all workers...")
        logger.info(f"{'=' * 80}")
        
        # Create final checkpoint
        try:
            self.checkpoint_manager.create_checkpoint()
        except Exception as e:
            logger.error(f"Failed to create final checkpoint: {e}")
        
        # Terminate all worker processes
        for worker_id, process in list(self.workers.items()):
            logger.info(f"Stopping {worker_id} (PID: {process.pid})...")
            try:
                if process.is_alive():
                    process.terminate()
            except Exception as e:
                logger.warning(f"Failed to call terminate on {worker_id}: {e}")
        
        # Wait for processes to finish
        grace_period = self.config.orchestrator.shutdown['grace_period_seconds']
        start_time = time.time()
        
        while time.time() - start_time < grace_period:
            alive = [w for w in self.workers.values() if w.is_alive()]
            if not alive:
                break
            time.sleep(1)
        
        # Force kill any remaining processes and reap all processes
        for worker_id, process in list(self.workers.items()):
            if process.is_alive():
                logger.warning(f"Force killing process {worker_id} (PID: {process.pid})")
                try:
                    process.kill()
                    process.join(timeout=2)
                except Exception as e:
                    logger.error(f"Failed to kill process {worker_id}: {e}")
            else:
                try:
                    process.join(timeout=1)
                except Exception:
                    pass
        
        logger.info("All workers stopped")
        logger.info(f"{'=' * 80}")
    
    def _signal_handler(self, signum, frame):
        """Handle termination signals"""
        logger.info(f"Received signal {signum}")
        self.running = False

    # =========================================================================
    # Dynamic Worker Reallocation
    # =========================================================================
    def _reallocate_idle_workers(self, stats: Dict[str, Any]) -> None:
        """
        Detect idle worker pools and reallocate them to bottleneck stages.
        
        Strategy:
        - If extraction/indexing/ocr queues are empty AND tagging has backlog,
          kill idle workers and spawn extra tagging workers instead.
        - If discovery has pending folders but no workers, spawn discovery workers.
        - Cap total workers to avoid memory exhaustion.
        """
        import psutil
        
        # Get queue depths
        extraction_pending = stats.get('extraction_total', {}).get('pending', 0)
        indexing_pending = stats.get('indexing', {}).get('pending', 0)
        ocr_pending = stats.get('ocr', {}).get('pending', 0)
        tagging_pending = stats.get('tagging', {}).get('pending', 0)
        discovery_pending = stats.get('discovery', {}).get('pending', 0)
        
        # Memory check - don't reallocate if memory is high
        mem = psutil.virtual_memory()
        if mem.percent > 85:
            logger.debug("Memory at %.1f%%, skipping worker reallocation", mem.percent)
            return
        
        # Count current workers by type
        current_counts = {}
        for wid in self.workers:
            prefix = wid.rsplit('-', 1)[0] if '-' in wid else wid
            # Normalize: extraction-fast, extraction-std, etc → extraction
            if prefix.startswith('extraction'):
                prefix = 'extraction'
            current_counts[prefix] = current_counts.get(prefix, 0) + 1
        
        current_tagging = current_counts.get('tagging', 0)
        current_extraction = current_counts.get('extraction', 0)
        current_indexing = current_counts.get('indexing', 0)
        current_ocr = current_counts.get('ocr', 0)
        current_discovery = current_counts.get('discovery', 0)
        
        # === CASE 1: Discovery has pending work but no workers ===
        if discovery_pending > 0 and current_discovery == 0:
            logger.info(
                f"Dynamic realloc: {discovery_pending} discovery folders pending, "
                f"spawning discovery workers"
            )
            self._spawn_discovery_workers()
            return
        
        # === CASE 2: Tagging backlog with idle extraction/indexing/ocr ===
        if tagging_pending > 100 and current_tagging < 8:
            idle_pools = []
            
            # Check which pools are idle (empty queues, no pending work)
            if extraction_pending == 0 and current_extraction > 0:
                idle_pools.append(('extraction', current_extraction))
            if indexing_pending == 0 and current_indexing > 0:
                idle_pools.append(('indexing', current_indexing))
            if ocr_pending == 0 and current_ocr > 0:
                idle_pools.append(('ocr', current_ocr))
            
            if not idle_pools:
                return
            
            # Calculate how many extra tagging workers we can spawn
            # Each idle pool donates floor(count/2) workers (keep at least 1 alive per pool)
            extra_tagging = 0
            workers_to_kill = []
            
            for pool_name, pool_count in idle_pools:
                donate_count = max(0, pool_count - 1)  # Keep at least 1 per pool
                if donate_count > 0:
                    extra_tagging += donate_count
                    # Find workers to terminate
                    killed = 0
                    for wid, proc in list(self.workers.items()):
                        if killed >= donate_count:
                            break
                        if wid.startswith(pool_name):
                            workers_to_kill.append(wid)
                            killed += 1
            
            # Cap total tagging workers
            max_tagging = 8
            extra_tagging = min(extra_tagging, max_tagging - current_tagging)
            
            if extra_tagging <= 0:
                return
            
            logger.info(
                f"Dynamic realloc: tagging backlog={tagging_pending}, "
                f"killing {len(workers_to_kill)} idle workers, "
                f"spawning {extra_tagging} extra tagging workers"
            )
            
            # Kill idle workers
            for wid in workers_to_kill[:extra_tagging]:
                proc = self.workers.get(wid)
                if proc and proc.is_alive():
                    logger.info(f"Terminating idle worker {wid}...")
                    try:
                        proc.terminate()
                        proc.join(timeout=5)
                        if proc.is_alive():
                            logger.warning(f"Idle worker {wid} did not terminate. Force killing...")
                            proc.kill()
                            proc.join(timeout=2)
                    except Exception as e:
                        logger.error(f"Failed to stop idle worker {wid}: {e}")
                if wid in self.workers:
                    del self.workers[wid]
                logger.info(f"  Terminated idle worker {wid}")
            
            # Spawn extra tagging workers
            existing_tag_ids = [
                int(wid.split('-')[-1]) for wid in self.workers
                if wid.startswith('tagging-')
            ]
            next_id = max(existing_tag_ids, default=0) + 1
            
            for i in range(extra_tagging):
                worker_id = f"tagging-{next_id + i}"
                process = mp.Process(
                    target=self._run_tagging_worker,
                    args=(worker_id,),
                    name=worker_id
                )
                process.daemon = True
                process.start()
                self.workers[worker_id] = process
                logger.info(f"  Spawned extra tagging worker {worker_id} (PID: {process.pid})")
            
            # Force GC after reallocation
            gc.collect()
        
        # === CASE 3: All queues empty — ensure tagging has enough workers ===
        elif tagging_pending > 0 and tagging_pending <= 100 and current_tagging < 4:
            # Small backlog — ensure at least 4 tagging workers
            needed = min(4, 4 - current_tagging)
            if needed > 0:
                existing_tag_ids = [
                    int(wid.split('-')[-1]) for wid in self.workers
                    if wid.startswith('tagging-')
                ]
                next_id = max(existing_tag_ids, default=0) + 1
                for i in range(needed):
                    worker_id = f"tagging-{next_id + i}"
                    process = mp.Process(
                        target=self._run_tagging_worker,
                        args=(worker_id,),
                        name=worker_id
                    )
                    process.daemon = True
                    process.start()
                    self.workers[worker_id] = process
                    logger.info(f"  Auto-scaled tagging worker {worker_id} (PID: {process.pid})")

