"""
Enterprise Document Search System - Master Orchestrator
Coordinates all workers, monitors health, handles failures, and tracks progress
Optimized for 128 vCPU / 64GB RAM AWS Instance
"""

import multiprocessing as mp
import time
import signal
import sys
import json
from pathlib import Path
from typing import Any, Dict, Optional
from datetime import datetime
import psutil

from core.logging_manager import get_logger
from core.config_manager import get_config
from core.queue_manager import get_queue_manager
from core.constants import (
    SizeCategory, WorkerPoolType,
    WORKER_HEARTBEAT_TIMEOUT_SECONDS
)

logger = get_logger("orchestrator")


class MasterOrchestrator:
    """Master orchestrator - coordinates entire document processing system"""
    
    def __init__(self):
        self.config = get_config()
        self.queue_manager = get_queue_manager()
        
        # Worker process tracking
        self.discovery_workers: Dict[str, mp.Process] = {}
        self.extraction_workers: Dict[str, mp.Process] = {}
        self.indexing_workers: Dict[str, mp.Process] = {}
        self.ocr_workers: Dict[str, mp.Process] = {}
        
        # Crash restart tracking (idle/clean exits are unlimited)
        self.crash_restart_counts: Dict[str, int] = {}
        self.max_crash_restarts = 3

        # Extraction worker metadata for accurate restarts/reassignment
        self.extraction_worker_config: Dict[str, Dict[str, Any]] = {}
        self.pool_port_index: Dict[str, int] = {
            name: 0 for name in getattr(self.config.extraction, 'pools', {})
        }
        self.pool_size_map = {
            WorkerPoolType.FAST_TRACK.value: SizeCategory.TINY,
            WorkerPoolType.STANDARD_TRACK.value: SizeCategory.SMALL,
            WorkerPoolType.HEAVY_TRACK.value: SizeCategory.MEDIUM,
            WorkerPoolType.EXTREME_TRACK.value: SizeCategory.LARGE
        }
        
        # System state
        self.running = False
        self.start_time = None
        self.last_checkpoint_time = None
        self.checkpoint_interval = 300  # 5 minutes
        
        # Statistics
        self.stats = {
            'discovered': 0,
            'extracted': 0,
            'indexed': 0,
            'ocr_completed': 0,
            'failed': 0
        }
        
        # Setup signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        
        logger.info("Master Orchestrator initialized")
    
    def start(self):
        """Start the entire system"""
        logger.info("=" * 80)
        logger.info("ENTERPRISE DOCUMENT SEARCH SYSTEM")
        logger.info("Optimized for 128 vCPU / 64GB RAM")
        logger.info("=" * 80)
        
        self.running = True
        self.start_time = time.time()
        
        try:
            # 1. Verify prerequisites
            logger.info("Step 1: Verifying system prerequisites...")
            if not self._verify_services():
                logger.error("Service verification failed. Please start required services.")
                return False
            
            # 2. Initialize databases
            logger.info("Step 2: Initializing queue databases...")
            self._initialize_databases()
            
            # 3. Check for checkpoint resume
            logger.info("Step 3: Checking for previous checkpoint...")
            checkpoint = self._load_checkpoint()
            if checkpoint:
                logger.info(f"Found checkpoint from {checkpoint.get('timestamp')}")
                # Auto-resume if queues have pending items (no manual prompt)
                # This prevents confusion when system shows "Discovered: 0" on fresh starts
                try:
                    queue_stats = self.queue_manager.get_queue_statistics() or {}
                    total_pending = self._count_total_pending(queue_stats)
                    if total_pending > 0:
                        logger.info(f"Found {total_pending:,} pending items in queues, auto-resuming from checkpoint...")
                        self._restore_from_checkpoint(checkpoint)
                    else:
                        logger.info("No pending items in queues, starting fresh")
                except Exception as e:
                    logger.warning(f"Could not auto-resume: {e}, starting fresh")
            else:
                logger.info("No checkpoint found, starting fresh")
            
            # 4. Start all workers
            logger.info("Step 4: Starting all worker processes...")
            self._start_all_workers()
            
            # 5. Enter monitoring loop
            logger.info("Step 5: Entering monitoring loop...")
            logger.info("=" * 80)
            logger.info("System is now running. Press Ctrl+C to stop gracefully.")
            logger.info("=" * 80)
            
            self._monitoring_loop()
            
        except Exception as e:
            logger.error(f"Fatal error in orchestrator: {e}", exc_info=True)
            return False
        
        finally:
            self._shutdown()
        
        return True
    
    def _verify_services(self) -> bool:
        """Verify all required services are running"""
        import requests
        
        services_ok = True
        
        # Check Tika instances
        logger.info("Checking Tika instances...")
        for instance in self.config.extraction.tika.instances:
            try:
                url = f"http://{instance.host}:{instance.port}/tika"
                response = requests.get(url, timeout=5)
                if response.status_code == 200:
                    logger.info(f"  ✓ Tika on port {instance.port}: OK")
                else:
                    logger.error(f"  ✗ Tika on port {instance.port}: HTTP {response.status_code}")
                    services_ok = False
            except Exception as e:
                logger.error(f"  ✗ Tika on port {instance.port}: {e}")
                services_ok = False
        
        # Check OpenSearch
        logger.info("Checking OpenSearch...")
        try:
            response = requests.get("http://localhost:9200", timeout=5)
            if response.status_code == 200:
                cluster_info = response.json()
                logger.info(f"  ✓ OpenSearch: {cluster_info.get('version', {}).get('number', 'unknown')}")
            else:
                logger.error(f"  ✗ OpenSearch: HTTP {response.status_code}")
                services_ok = False
        except Exception as e:
            logger.error(f"  ✗ OpenSearch: {e}")
            services_ok = False
        
        # Check PaddleOCR
        logger.info("Checking PaddleOCR...")
        try:
            import paddleocr  # noqa: F401
            logger.info("  ✓ PaddleOCR: Available")
        except ImportError as e:
            logger.error(f"  ✗ PaddleOCR: Not installed — {e}")
            services_ok = False
        
        return services_ok
    
    def _initialize_databases(self):
        """Initialize queue databases"""
        # Queue manager already initializes on first access
        # Just verify it's working
        try:
            stats = self.queue_manager.get_queue_statistics()
            logger.info(f"Queue database initialized. Current stats: {stats}")
        except Exception as e:
            logger.error(f"Failed to initialize queue database: {e}")
            raise

    def _next_tika_port(self, pool_type: str) -> int:
        """Round-robin selector for Tika ports per pool"""
        pool_config = self.config.extraction.pools.get(pool_type)
        if not pool_config or not getattr(pool_config, 'tika_ports', None):
            return 0
        index = self.pool_port_index.get(pool_type, 0) % len(pool_config.tika_ports)
        self.pool_port_index[pool_type] = index + 1
        return pool_config.tika_ports[index]

    def _make_extraction_assignment(self, pool_type: str, size_category: Any, tika_port: int) -> Dict[str, Any]:
        """Normalize extraction assignment payload"""
        try:
            if isinstance(size_category, str):
                size_category = SizeCategory(size_category)
        except Exception:
            logger.warning(f"Invalid size category '{size_category}', falling back to MEDIUM")
            size_category = SizeCategory.MEDIUM
        return {
            'pool_type': pool_type,
            'size_category': size_category,
            'tika_port': tika_port
        }

    def _start_extraction_worker_process(self, worker_id: str, assignment: Dict[str, Any]) -> mp.Process:
        """Start extraction worker with stored assignment metadata"""
        process = mp.Process(
            target=self._run_extraction_worker,
            args=(
                worker_id,
                assignment['pool_type'],
                assignment['size_category'],
                assignment['tika_port']
            ),
            name=worker_id
        )
        process.start()
        self.extraction_workers[worker_id] = process
        self.extraction_worker_config[worker_id] = assignment
        return process

    def _choose_extraction_assignment(self, current_assignment: Dict[str, Any]) -> Dict[str, Any]:
        """Redirect idle extraction workers toward queues with pending work"""
        try:
            stats = self.queue_manager.get_queue_statistics()
            extraction_stats = stats.get('extraction', {})
        except Exception as exc:
            logger.error(f"Unable to fetch queue stats for assignment: {exc}")
            return current_assignment

        best_pool = current_assignment['pool_type']
        best_size = current_assignment['size_category']
        best_pending = extraction_stats.get(
            best_size.value if isinstance(best_size, SizeCategory) else str(best_size),
            {}
        ).get('pending', 0) or 0

        for pool_type, size_cat in self.pool_size_map.items():
            size_key = size_cat.value if isinstance(size_cat, SizeCategory) else str(size_cat)
            pending = extraction_stats.get(size_key, {}).get('pending', 0) or 0
            if pending > best_pending and pending > 0:
                best_pool = pool_type
                best_size = size_cat
                best_pending = pending

        if best_pool == current_assignment['pool_type']:
            return current_assignment

        return self._make_extraction_assignment(
            pool_type=best_pool,
            size_category=best_size,
            tika_port=self._next_tika_port(best_pool)
        )
    
    def _start_all_workers(self):
        """Start all worker processes"""
        logger.info("Starting worker processes...")
        
        # Discovery workers
        logger.info(f"Starting {self.config.discovery.num_workers} discovery workers...")
        self._start_discovery_workers()
        
        # Extraction workers
        total_extraction = self.config.extraction.total_workers
        logger.info(f"Starting {total_extraction} extraction workers...")
        self._start_extraction_workers()
        
        # Indexing workers
        logger.info(f"Starting {self.config.indexing.num_workers} indexing workers...")
        self._start_indexing_workers()
        
        # OCR workers
        logger.info(f"Starting {self.config.ocr.initial_workers} OCR workers...")
        self._start_ocr_workers()
        
        logger.info(f"Total workers started: {len(self.discovery_workers) + len(self.extraction_workers) + len(self.indexing_workers) + len(self.ocr_workers)}")
    
    def _start_discovery_workers(self):
        """Start discovery worker processes"""
        
        for i in range(self.config.discovery.num_workers):
            worker_id = f"disc-{i+1}"
            process = mp.Process(
                target=self._run_discovery_worker,
                args=(worker_id,),
                name=worker_id
            )
            process.start()
            self.discovery_workers[worker_id] = process
            logger.info(f"  Started {worker_id} (PID: {process.pid})")
            time.sleep(0.1)  # Stagger starts slightly
    
    def _start_extraction_workers(self):
        """Start extraction worker processes across all pools"""
        pools = [
            (WorkerPoolType.FAST_TRACK.value, SizeCategory.TINY, self.config.extraction.pools['fast_track'], 'ext-fast', 'fast track'),
            (WorkerPoolType.STANDARD_TRACK.value, SizeCategory.SMALL, self.config.extraction.pools['standard_track'], 'ext-std', 'standard track'),
            (WorkerPoolType.HEAVY_TRACK.value, SizeCategory.MEDIUM, self.config.extraction.pools['heavy_track'], 'ext-heavy', 'heavy track'),
            (WorkerPoolType.EXTREME_TRACK.value, SizeCategory.LARGE, self.config.extraction.pools['extreme_track'], 'ext-extreme', 'extreme track')
        ]

        for pool_type, size_category, pool_config, prefix, label in pools:
            for i in range(pool_config.num_workers):
                worker_id = f"{prefix}-{i+1}"
                tika_port = pool_config.tika_ports[i % len(pool_config.tika_ports)]
                assignment = self._make_extraction_assignment(pool_type, size_category, tika_port)
                self._start_extraction_worker_process(worker_id, assignment)
                if i % 10 == 0:
                    logger.info(f"  Started {i+1}/{pool_config.num_workers} {label} workers...")
                time.sleep(0.05)

        logger.info(f"  Started {len(self.extraction_workers)} extraction workers total")
    
    def _start_indexing_workers(self):
        """Start indexing worker processes"""
        
        for i in range(self.config.indexing.num_workers):
            worker_id = f"idx-{i+1}"
            process = mp.Process(
                target=self._run_indexing_worker,
                args=(worker_id,),
                name=worker_id
            )
            process.start()
            self.indexing_workers[worker_id] = process
            logger.info(f"  Started {worker_id} (PID: {process.pid})")
            time.sleep(0.1)
    
    def _start_ocr_workers(self):
        """Start OCR worker processes"""
        
        num_workers = self.config.ocr.initial_workers
        for i in range(num_workers):
            worker_id = f"ocr-{i+1}"
            process = mp.Process(
                target=self._run_ocr_worker,
                args=(worker_id,),
                name=worker_id
            )
            process.start()
            self.ocr_workers[worker_id] = process
            if i % 10 == 0:  # Log progress every 10 workers
                logger.info(f"  Started {i+1}/{num_workers} OCR workers...")
            time.sleep(0.1)
    
    @staticmethod
    def _run_discovery_worker(worker_id: str):
        """Run discovery worker (in separate process)"""
        from discovery.discovery_worker import DiscoveryWorker
        worker = DiscoveryWorker(worker_id)
        worker.run()
    
    @staticmethod
    def _run_extraction_worker(worker_id: str, pool_type: str, size_category, tika_port: int):
        """Run extraction worker (in separate process)"""
        from extraction.extraction_worker import ExtractionWorker
        worker = ExtractionWorker(worker_id, pool_type, size_category, tika_port)
        worker.run()
    
    @staticmethod
    def _run_indexing_worker(worker_id: str):
        """Run indexing worker (in separate process)"""
        from indexing.indexing_worker import IndexingWorker
        worker = IndexingWorker(worker_id)
        worker.run()
    
    @staticmethod
    def _run_ocr_worker(worker_id: str):
        """Run OCR worker (in separate process)"""
        from ocr.ocr_worker import OCRWorker
        worker = OCRWorker(worker_id)
        worker.run()
    
    def _monitoring_loop(self):
        """Main monitoring loop"""
        last_stats_time = time.time()
        stats_interval = 60  # Log stats every 60 seconds
        
        while self.running:
            try:
                # Check worker health
                self._check_worker_health()
                
                # Check system resources
                self._check_system_resources()
                
                # Update statistics
                current_time = time.time()
                if current_time - last_stats_time >= stats_interval:
                    self._log_statistics()
                    last_stats_time = current_time
                
                # Save checkpoint periodically
                if self.last_checkpoint_time is None or \
                   (current_time - self.last_checkpoint_time) >= self.checkpoint_interval:
                    self._save_checkpoint()
                    self.last_checkpoint_time = current_time
                
                # Sleep to avoid busy loop
                time.sleep(30)
                
            except KeyboardInterrupt:
                logger.info("Shutdown signal received")
                break
            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}", exc_info=True)
                time.sleep(5)
    
    def _check_worker_health(self):
        """Check if all workers are alive, restart if needed"""
        all_workers = {
            **self.discovery_workers,
            **self.extraction_workers,
            **self.indexing_workers,
            **self.ocr_workers
        }
        
        dead_workers = []  # Crashed workers (exitcode != 0)
        idle_workers = []  # Clean exits (exitcode == 0) hit restart threshold or no work
        
        # Get heartbeats
        heartbeats = self.queue_manager.get_worker_heartbeats()
        current_time = time.time()
        
        for worker_id, process in all_workers.items():
            if not process.is_alive():
                exit_code = process.exitcode

                # Treat SIGINT/SIGTERM-triggered exits as clean during shutdown
                if exit_code in (-signal.SIGINT, -signal.SIGTERM):
                    exit_code = 0

                # Exit code 0 (or None) == clean exit (restart threshold hit or empty queue)
                if exit_code is None or exit_code == 0:
                    idle_workers.append(worker_id)
                else:
                    dead_workers.append((worker_id, process))
                    logger.warning(f"Worker {worker_id} (PID {process.pid}) crashed. Exit code: {exit_code}")
            
            else:
                # Process is alive - check heartbeat
                last_heartbeat = heartbeats.get(worker_id)
                if last_heartbeat:
                    time_since_heartbeat = current_time - last_heartbeat
                    if time_since_heartbeat > WORKER_HEARTBEAT_TIMEOUT_SECONDS:
                        logger.error(
                            f"Worker {worker_id} (PID {process.pid}) is STUCK! "
                            f"Last heartbeat {time_since_heartbeat:.1f}s ago (Timeout: {WORKER_HEARTBEAT_TIMEOUT_SECONDS}s). "
                            "Terminating..."
                        )
                        try:
                            # Kill process tree to ensure subprocesses (Tika/Tesseract) also die
                            parent = psutil.Process(process.pid)
                            for child in parent.children(recursive=True):
                                try: child.kill()
                                except: pass
                            parent.kill()
                            
                            # Clean up heartbeat so we don't kill it again immediately (though PID changes)
                            self.queue_manager.remove_worker_heartbeat(worker_id)
                            
                        except Exception as e:
                            logger.error(f"Failed to kill stuck worker {worker_id}: {e}")

        
        # Restart idle workers (they hit restart threshold or found no work initially)
        # MEMORY GUARD: Only restart if memory is below 70% (keep 30% headroom for restarts)
        if idle_workers:
            mem = psutil.virtual_memory()
            if mem.percent > 70:
                logger.debug(
                    f"Skipping restart of {len(idle_workers)} idle workers – "
                    f"memory at {mem.percent:.0f}% (waiting for <70%)"
                )
                idle_workers = []  # skip restarts this cycle
            else:
                logger.debug(f"Restarting idle workers: {len(idle_workers)}")

        for worker_id in idle_workers:
                try:
                    # Get the old process
                    old_process = all_workers.get(worker_id)
                    if not old_process:
                        continue
                    
                    # Restart based on worker type
                    if worker_id.startswith('disc-'):
                        worker_num = int(worker_id.split('-')[1])
                        new_process = mp.Process(
                            target=self._run_discovery_worker,
                            args=(worker_num,),
                            name=worker_id
                        )
                        new_process.start()
                        self.discovery_workers[worker_id] = new_process
                    
                    elif worker_id.startswith('extraction-') or worker_id.startswith('ext-'):
                        assignment = self.extraction_worker_config.get(worker_id)
                        if not assignment:
                            logger.error(f"No assignment stored for idle worker {worker_id}, skipping restart")
                            continue
                        target_assignment = self._choose_extraction_assignment(assignment)
                        self._start_extraction_worker_process(worker_id, target_assignment)
                        logger.info(
                            "Restarted idle extraction worker %s on pool %s (port %s)",
                            worker_id,
                            target_assignment['pool_type'],
                            target_assignment['tika_port']
                        )
                    
                    elif worker_id.startswith('indexing-'):
                        worker_num = int(worker_id.split('-')[1])
                        new_process = mp.Process(
                            target=self._run_indexing_worker,
                            args=(worker_num,),
                            name=worker_id
                        )
                        new_process.start()
                        self.indexing_workers[worker_id] = new_process
                    
                    elif worker_id.startswith('ocr-'):
                        worker_num = int(worker_id.split('-')[1])
                        new_process = mp.Process(
                            target=self._run_ocr_worker,
                            args=(worker_num,),
                            name=worker_id
                        )
                        new_process.start()
                        self.ocr_workers[worker_id] = new_process
                    
                except Exception as e:
                    logger.error(f"Failed to restart idle worker {worker_id}: {e}", exc_info=True)
        
        # Restart crashed workers with backoff limit
        if dead_workers:
            logger.warning(f"Crashed workers: {len(dead_workers)}")
            
            for worker_id, dead_process in dead_workers:
                # Check crash restart count
                restart_count = self.crash_restart_counts.get(worker_id, 0)
                
                if restart_count >= self.max_crash_restarts:
                    logger.error(f"Worker {worker_id} has crashed {restart_count} times. Not restarting.")
                    continue
                
                # Increment crash restart counter
                self.crash_restart_counts[worker_id] = restart_count + 1
                
                # Restart based on worker type
                logger.info(f"Restarting worker {worker_id} after crash (attempt {restart_count + 1}/{self.max_crash_restarts})")
                
                try:
                    if worker_id.startswith('disc-'):
                        # Restart discovery worker
                        worker_num = int(worker_id.split('-')[1])
                        new_process = mp.Process(
                            target=self._run_discovery_worker,
                            args=(worker_num,),
                            name=worker_id
                        )
                        new_process.start()
                        self.discovery_workers[worker_id] = new_process
                        logger.info(f"Restarted discovery worker {worker_id} with PID {new_process.pid}")
                    
                    elif worker_id.startswith('extraction-') or worker_id.startswith('ext-'):
                        # Restart extraction worker using stored assignment
                        assignment = self.extraction_worker_config.get(worker_id)
                        if not assignment:
                            logger.error(f"No assignment stored for crashed worker {worker_id}, skipping restart")
                            continue
                        new_process = self._start_extraction_worker_process(worker_id, assignment)
                        logger.info(
                            f"Restarted extraction worker {worker_id} with PID {new_process.pid} on pool {assignment['pool_type']}"
                        )
                    
                    elif worker_id.startswith('indexing-'):
                        # Restart indexing worker
                        worker_num = int(worker_id.split('-')[1])
                        new_process = mp.Process(
                            target=self._run_indexing_worker,
                            args=(worker_num,),
                            name=worker_id
                        )
                        new_process.start()
                        self.indexing_workers[worker_id] = new_process
                        logger.info(f"Restarted indexing worker {worker_id} with PID {new_process.pid}")
                    
                    elif worker_id.startswith('ocr-'):
                        # Restart OCR worker
                        worker_num = int(worker_id.split('-')[1])
                        new_process = mp.Process(
                            target=self._run_ocr_worker,
                            args=(worker_num,),
                            name=worker_id
                        )
                        new_process.start()
                        self.ocr_workers[worker_id] = new_process
                        logger.info(f"Restarted OCR worker {worker_id} with PID {new_process.pid}")
                    
                except Exception as e:
                    logger.error(f"Failed to restart worker {worker_id}: {e}", exc_info=True)
    
    def _check_system_resources(self):
        """Monitor system resource usage and enforce the 20% free memory rule.
        
        When memory usage exceeds 80% (i.e. <20% free), the orchestrator
        proactively kills low-priority workers (OCR first, then extraction)
        to reclaim memory.  When memory drops below 70%, killed workers are
        restarted automatically in _check_worker_health.
        """
        cpu_percent = psutil.cpu_percent(interval=1)
        memory = psutil.virtual_memory()
        
        # Monitor C: drive (System drive on AWS)
        try:
            disk = psutil.disk_usage('C:\\')
        except Exception:
            # Fallback if C: doesn't exist (rare on Windows)
            disk = psutil.disk_usage('/')

        # Log warnings if resources are constrained
        if cpu_percent > 95:
            logger.warning(f"CPU usage very high: {cpu_percent}%")
        
        if memory.percent > 90:
            logger.warning(f"Memory usage CRITICAL: {memory.percent}% ({memory.used / (1024**3):.1f}GB / {memory.total / (1024**3):.1f}GB)")
        elif memory.percent > 80:
            logger.warning(f"Memory usage high: {memory.percent}% ({memory.used / (1024**3):.1f}GB / {memory.total / (1024**3):.1f}GB)")
        
        if disk.percent > 85:
            logger.warning(f"Disk usage high on D: {disk.percent}% ({disk.free / (1024**3):.1f}GB free)")

        # ---- Memory guard: keep at least 20% free ----
        if memory.percent > 80:
            self._throttle_for_memory(memory)
    
    def _throttle_for_memory(self, memory):
        """Pause / kill workers to stay below 80% memory usage.
        
        Priority for termination (least critical first):
          1. OCR workers (images can be retried later)
          2. Extraction workers (excess pool workers)
        
        Workers are automatically restarted by _check_worker_health once
        memory drops below 70%.
        """
        pct = memory.percent
        free_gb = memory.available / (1024**3)
        logger.warning(
            f"MEMORY GUARD: {pct:.1f}% used ({free_gb:.1f} GB free). "
            f"Throttling workers to stay under 80%..."
        )

        killed = 0

        # Phase 1: Kill OCR workers (up to half)
        if pct > 80:
            alive_ocr = [wid for wid, p in self.ocr_workers.items() if p.is_alive()]
            to_kill = max(1, len(alive_ocr) // 2)
            for wid in alive_ocr[:to_kill]:
                try:
                    proc = self.ocr_workers[wid]
                    proc.terminate()
                    proc.join(timeout=5)
                    if proc.is_alive():
                        proc.kill()
                    killed += 1
                    logger.info(f"  MEMORY GUARD: Terminated OCR worker {wid}")
                except Exception:
                    pass
            # Re-check
            memory = psutil.virtual_memory()
            pct = memory.percent

        # Phase 2: Kill extraction workers if still above 85%
        if pct > 85:
            alive_ext = [wid for wid, p in self.extraction_workers.items() if p.is_alive()]
            to_kill = max(1, len(alive_ext) // 3)
            for wid in alive_ext[:to_kill]:
                try:
                    proc = self.extraction_workers[wid]
                    proc.terminate()
                    proc.join(timeout=5)
                    if proc.is_alive():
                        proc.kill()
                    killed += 1
                    logger.info(f"  MEMORY GUARD: Terminated extraction worker {wid}")
                except Exception:
                    pass

        if killed > 0:
            logger.warning(f"MEMORY GUARD: Terminated {killed} workers. Will auto-restart when memory < 70%.")

        # Force garbage collection in main process
        import gc
        gc.collect()
    
    def _log_statistics(self):
        """Log current processing statistics"""
        try:
            stats = self.queue_manager.get_queue_statistics()
            runtime = time.time() - self.start_time
            runtime_hours = runtime / 3600
            
            logger.info("=" * 80)
            logger.info("SYSTEM STATISTICS")
            logger.info(f"Runtime: {runtime_hours:.2f} hours")
            logger.info(f"Discovered: {stats.get('total_discovered', 0):,}")
            logger.info(f"Extracted: {stats.get('total_extracted', 0):,}")
            logger.info(f"Indexed: {stats.get('total_indexed', 0):,}")
            logger.info(f"OCR Completed: {stats.get('total_ocr_completed', 0):,}")
            logger.info(f"Failed: {stats.get('total_failed', 0):,}")
            
            # Calculate rates
            if runtime_hours > 0:
                logger.info(f"Avg Rate: {stats.get('total_indexed', 0) / runtime_hours:.0f} docs/hour")
            
            logger.info("=" * 80)
            
        except Exception as e:
            logger.error(f"Error logging statistics: {e}")
    
    def _save_checkpoint(self):
        """Save system state checkpoint"""
        try:
            checkpoint_dir = Path(self.config.paths.checkpoints_dir)
            checkpoint_dir.mkdir(parents=True, exist_ok=True)
            
            checkpoint = {
                'timestamp': datetime.now().isoformat(),
                'runtime_seconds': time.time() - self.start_time,
                'stats': self.queue_manager.get_queue_statistics(),
                'worker_counts': {
                    'discovery': len(self.discovery_workers),
                    'extraction': len(self.extraction_workers),
                    'indexing': len(self.indexing_workers),
                    'ocr': len(self.ocr_workers)
                }
            }
            
            checkpoint_file = checkpoint_dir / f"checkpoint_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
            with open(checkpoint_file, 'w') as f:
                json.dump(checkpoint, f, indent=2)
            
            logger.info(f"Checkpoint saved: {checkpoint_file}")
            
            # Keep only last 10 checkpoints
            checkpoints = sorted(checkpoint_dir.glob("checkpoint_*.json"))
            if len(checkpoints) > 10:
                for old_checkpoint in checkpoints[:-10]:
                    old_checkpoint.unlink()
            
        except Exception as e:
            logger.error(f"Error saving checkpoint: {e}")
    
    def _load_checkpoint(self) -> Optional[Dict]:
        """Load most recent checkpoint"""
        try:
            checkpoint_dir = Path(self.config.paths.checkpoints_dir)
            if not checkpoint_dir.exists():
                return None
            
            checkpoints = sorted(checkpoint_dir.glob("checkpoint_*.json"))
            if not checkpoints:
                return None
            
            latest = checkpoints[-1]
            with open(latest, 'r') as f:
                return json.load(f)
                
        except Exception as e:
            logger.error(f"Error loading checkpoint: {e}")
            return None
    
    def _prompt_resume(self, checkpoint: Dict) -> bool:
        """Ask user if they want to resume from checkpoint"""
        print("\n" + "=" * 80)
        print("CHECKPOINT FOUND")
        print("=" * 80)
        print(f"Timestamp: {checkpoint.get('timestamp')}")
        print(f"Runtime: {checkpoint.get('runtime_seconds', 0) / 3600:.2f} hours")
        stats = checkpoint.get('stats', {})
        print(f"Discovered: {stats.get('total_discovered', 0):,}")
        print(f"Indexed: {stats.get('total_indexed', 0):,}")
        print("=" * 80)
        
        response = input("Resume from checkpoint? (y/n): ").strip().lower()
        return response == 'y'
    def _restore_from_checkpoint(self, checkpoint: Dict) -> None:
        """Restore system state from checkpoint."""
        logger.info("Restoring system state from checkpoint...")
        try:
            stats = checkpoint.get('stats', {})
            if stats:
                logger.info(
                    "Checkpoint stats: discovered=%s indexed=%s",
                    stats.get('total_discovered', 0),
                    stats.get('total_indexed', 0)
                )

            logger.info("Resetting stale 'processing' items in queues...")
            self.queue_manager.reset_stale_processing()
            logger.info("Stale items reset successfully.")
        except Exception as e:
            logger.error(f"Failed to restore from checkpoint: {e}")

    def _count_total_pending(self, queue_stats: Dict[str, Any]) -> int:
        """Count total pending items across all queues."""
        total = 0
        if 'discovery' in queue_stats:
            total += queue_stats['discovery'].get('pending', 0)
        if 'extraction' in queue_stats:
            for category_stats in queue_stats['extraction'].values():
                if isinstance(category_stats, dict):
                    total += category_stats.get('pending', 0)
        if 'indexing' in queue_stats:
            total += queue_stats['indexing'].get('pending', 0)
        if 'ocr' in queue_stats:
            total += queue_stats['ocr'].get('pending', 0)
        return total
    
    def _signal_handler(self, signum, frame):
        """Handle shutdown signals"""
        logger.info(f"\nReceived signal {signum}, initiating graceful shutdown...")
        self.running = False
    
    def _shutdown(self):
        """Gracefully shutdown all workers"""
        logger.info("=" * 80)
        logger.info("SHUTTING DOWN")
        logger.info("=" * 80)
        
        # Save final checkpoint
        logger.info("Saving final checkpoint...")
        self._save_checkpoint()
        
        # Give workers time to finish current task
        logger.info("Waiting for workers to finish (60 seconds grace period)...")
        time.sleep(60)
        
        # Terminate all workers
        logger.info("Terminating workers...")
        all_workers = {
            **self.discovery_workers,
            **self.extraction_workers,
            **self.indexing_workers,
            **self.ocr_workers
        }
        
        for worker_id, process in all_workers.items():
            if process.is_alive():
                logger.info(f"Terminating {worker_id}...")
                process.terminate()
                process.join(timeout=5)
                if process.is_alive():
                    process.kill()
        
        logger.info("Shutdown complete")
        logger.info("=" * 80)


def main():
    """Main entry point"""
    orchestrator = MasterOrchestrator()
    success = orchestrator.start()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
