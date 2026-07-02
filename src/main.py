"""
Enterprise Document Search System - Main Entry Point
Production-grade CLI interface and application launcher
"""
# -*- coding: utf-8 -*-

import click
import sys
import os
from pathlib import Path
from datetime import datetime

# Prevent Intel OpenMP duplicate runtime conflicts / crashes
os.environ['KMP_DUPLICATE_LIB_OK'] = 'TRUE'

# Point PaddleX to the shared model cache before any paddlex import.
# paddlex.utils.cache reads this env var at import time, so it must be set here —
# before core/__init__.py pulls in paddleocr transitively.
os.environ.setdefault('PADDLE_PDX_CACHE_HOME', r'C:\softwares\paddle_models')

# Force UTF-8 output for Windows console
if sys.platform == 'win32':
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

# Add src directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from core.config_manager import get_config_manager, get_config
from core.logging_manager import setup_logging, get_logger
from core.constants import VERSION, SYSTEM_NAME


def _is_tika_healthy(host: str, port: int, timeout: int = 5) -> bool:
    """Return True when a Tika instance responds on either /tika or /."""
    import requests

    try:
        resp = requests.get(f"http://{host}:{port}/tika", timeout=timeout)
        if resp.status_code in (200, 405):
            return True
    except Exception:
        pass

    try:
        resp = requests.get(f"http://{host}:{port}/", timeout=timeout)
        return resp.status_code == 200
    except Exception:
        return False


def _find_running_start_processes():
    """Return running main.py start processes excluding current PID."""
    try:
        import psutil
    except Exception:
        return []

    current_pid = os.getpid()
    matches = []

    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            pid = proc.info.get('pid')
            if pid == current_pid:
                continue

            process_name = (proc.info.get('name') or '').lower()
            if 'python' not in process_name:
                continue

            cmdline = proc.info.get('cmdline') or []
            if not cmdline:
                continue

            cmd = " ".join(cmdline).lower()
            cmd_normalized = cmd.replace("\\", "/")
            if "src/main.py" in cmd_normalized and " start" in f" {cmd_normalized}":
                matches.append({
                    'pid': pid,
                    'cmdline': " ".join(cmdline)
                })
        except Exception:
            continue

    return matches


def _find_dashboard_processes():
    """Return running Streamlit/dashboard processes excluding current PID."""
    try:
        import psutil
    except Exception:
        return []

    current_pid = os.getpid()
    matches = []

    for proc in psutil.process_iter(['pid', 'name', 'cmdline']):
        try:
            pid = proc.info.get('pid')
            if pid == current_pid:
                continue

            process_name = (proc.info.get('name') or '').lower()
            cmdline = proc.info.get('cmdline') or []
            if not cmdline:
                continue

            cmd = " ".join(cmdline).lower().replace("\\", "/")

            # Restrict to python-based dashboard processes for safety.
            if 'python' not in process_name and not any('python' in str(part).lower() for part in cmdline[:1]):
                continue

            if (
                "streamlit run" in cmd
                or "src/ui/dashboard.py" in cmd
                or " ui/dashboard.py" in cmd
            ):
                matches.append({
                    'pid': pid,
                    'cmdline': " ".join(cmdline)
                })
        except Exception:
            continue

    return matches


def _terminate_process_tree(pid: int, timeout_sec: float = 10.0) -> bool:
    """Terminate a process and its descendants. Returns True on success."""
    try:
        import psutil
        root = psutil.Process(pid)
    except Exception:
        return False

    targets = []
    try:
        targets.extend(root.children(recursive=True))
    except Exception:
        pass
    targets.append(root)

    terminated = []
    for proc in targets:
        try:
            proc.terminate()
            terminated.append(proc)
        except Exception:
            continue

    if terminated:
        try:
            _, alive = psutil.wait_procs(terminated, timeout=timeout_sec)
        except Exception:
            alive = []
        for proc in alive:
            try:
                proc.kill()
            except Exception:
                pass

    # Best-effort verification.
    try:
        root.wait(timeout=1.0)
        return True
    except psutil.TimeoutExpired:
        try:
            return not root.is_running()
        except Exception:
            return False
    except psutil.NoSuchProcess:
        return True
    except Exception:
        return True


def _check_paddle_models() -> bool:
    """Check if PaddleOCR models are available. Returns True if ready."""
    cache_dir = Path(r'C:\softwares\paddle_models')
    if not cache_dir.exists():
        cache_dir.mkdir(parents=True, exist_ok=True)

    has_models = any(cache_dir.rglob('*.pdparams')) or any(cache_dir.rglob('*.pdiparams'))
    if has_models:
        return True

    official = cache_dir / 'official_models'
    models = cache_dir / 'models'
    if (official.exists() and any(official.iterdir())) or (models.exists() and any(models.iterdir())):
        return True

    return False


@click.group()
@click.version_option(version=VERSION, prog_name=SYSTEM_NAME)
def cli():
    """
    Enterprise Document Search System

    Production-grade document indexing and search platform
    """
    pass


@cli.command()
def check():
    """Check all required services are running before starting the system"""
    try:
        click.echo(f"\n{'='*80}")
        click.echo(f"{SYSTEM_NAME} v{VERSION}")
        click.echo("Service Health Check")
        click.echo(f"{'='*80}\n")
        
        config = get_config()
        all_ok = True
        
        # Check Tika instances
        import requests
        click.echo("Checking Tika instances...")
        tika_ok = 0
        for tika_inst in config.extraction.tika.instances:
            try:
                if _is_tika_healthy(tika_inst.host, tika_inst.port, timeout=3):
                    click.secho(f"  ✓ Tika on port {tika_inst.port}: OK", fg='green')
                    tika_ok += 1
                else:
                    click.secho(f"  ✗ Tika on port {tika_inst.port}: Not healthy", fg='red')
                    all_ok = False
            except Exception:
                click.secho(f"  ✗ Tika on port {tika_inst.port}: Not running", fg='red')
                all_ok = False
        
        if tika_ok == 0:
            click.echo("\n  📝 To start Tika: cd bin && .\\start_tika.bat")
        
        # Check OpenSearch
        click.echo("\nChecking OpenSearch...")
        try:
            os_config = config.indexing.opensearch
            auth = (os_config.username, os_config.password) if os_config.username else None
            response = requests.get(
                os_config.hosts[0],
                auth=auth,
                verify=False,
                timeout=3
            )
            if response.status_code == 200:
                cluster_info = response.json()
                version = cluster_info.get('version', {}).get('number', 'unknown')
                click.secho(f"  ✓ OpenSearch {version}: OK", fg='green')
            else:
                click.secho(f"  ✗ OpenSearch: HTTP {response.status_code}", fg='red')
                all_ok = False
        except Exception:
            click.secho("  ✗ OpenSearch: Not running", fg='red')
            click.echo("\n  📝 To start OpenSearch: cd bin && .\\start_opensearch.bat")
            all_ok = False
        
        # Check PaddleOCR (optional - OCR runs in background, does not block indexing)
        click.echo("\nChecking PaddleOCR...")
        try:
            import paddleocr  # noqa: F401
            click.secho("  ✓ PaddleOCR: Package installed", fg='green')
            if _check_paddle_models():
                click.secho("  ✓ PaddleOCR: Models available", fg='green')
            else:
                click.secho(r"  ⚠ PaddleOCR: Models not found in C:\softwares\paddle_models", fg='yellow')
        except ImportError:
            click.secho("  ⚠ PaddleOCR: Not installed (OCR disabled - run: pip install paddleocr paddlepaddle)", fg='yellow')

        # Summary
        click.echo(f"\n{'='*80}")
        if all_ok:
            click.secho("✓ All services are running!", fg='green', bold=True)
            click.echo("\nYou can now run:")
            click.echo("  python src/main.py start")
        else:
            click.secho("✗ Some services are not running", fg='red', bold=True)
            click.echo("\nPlease start the required services before running the system.")
            click.echo("See the messages above for instructions.")
        click.echo(f"{'='*80}\n")
        
        return 0 if all_ok else 1
        
    except Exception as e:
        click.secho(f"\n✗ Check failed: {e}", fg='red', bold=True)
        import traceback
        traceback.print_exc()
        return 1


@cli.command()
@click.option('--config', default=None, help='Path to configuration file')
def init(config):
    """Initialize system: create directories, validate configuration, setup database"""
    try:
        click.echo(f"\n{'='*80}")
        click.echo(f"{SYSTEM_NAME} v{VERSION}")
        click.echo("Initialization")
        click.echo(f"{'='*80}\n")
        
        # Load configuration
        click.echo("Loading configuration...")
        config_manager = get_config_manager(config)
        system_config = config_manager.get_config()
        
        # Initialize logging
        click.echo("Initializing logging...")
        setup_logging()
        logger = get_logger("main")
        logger.info("System initialization started")
        
        # Create directories
        click.echo("Creating directory structure...")
        config_manager.ensure_directories()
        click.secho("✓ Directories created", fg='green')
        
        # Initialize queue database
        click.echo("Initializing queue database...")
        from core.queue_manager import get_queue_manager
        queue_manager = get_queue_manager()
        click.secho("✓ Queue database initialized", fg='green')
        
        # Validate services
        click.echo("\nValidating services...")
        
        # Check Tika instances
        import requests
        for tika_inst in system_config.extraction.tika.instances:
            try:
                if _is_tika_healthy(tika_inst.host, tika_inst.port, timeout=5):
                    click.secho(f"✓ Tika instance on port {tika_inst.port} available", fg='green')
                else:
                    click.secho(f"✗ Tika instance on port {tika_inst.port} not healthy", fg='red')
            except Exception as e:
                click.secho(f"✗ Tika instance on port {tika_inst.port} not accessible: {e}", fg='red')
        
        # Check OpenSearch
        try:
            os_config = system_config.indexing.opensearch
            auth = (os_config.username, os_config.password) if os_config.username else None
            response = requests.get(
                os_config.hosts[0],
                auth=auth,
                verify=False,
                timeout=5
            )
            if response.status_code == 200:
                click.secho(f"✓ OpenSearch available at {os_config.hosts[0]}", fg='green')
            else:
                click.secho(f"✗ OpenSearch returned {response.status_code}", fg='red')
        except Exception as e:
            click.secho(f"✗ OpenSearch not accessible: {e}", fg='red')
        
        # Check PaddleOCR
        try:
            import paddleocr  # noqa: F401
            click.secho("✓ PaddleOCR: Available", fg='green')
        except ImportError:
            click.secho("✗ PaddleOCR: Not installed (run: pip install paddleocr paddlepaddle)", fg='red')
        
        # Print configuration summary
        click.echo(f"\n{'='*80}")
        click.echo("Configuration Summary")
        click.echo(f"{'='*80}\n")
        config_manager.print_config_summary()
        
        click.secho(f"\n{'='*80}", fg='green')
        click.secho("✓ Initialization complete!", fg='green', bold=True)
        click.secho(f"{'='*80}\n", fg='green')
        
        logger.info("System initialization completed successfully")
        
    except Exception as e:
        click.secho(f"\n✗ Initialization failed: {e}", fg='red', bold=True)
        import traceback
        traceback.print_exc()
        sys.exit(1)


@cli.command()
@click.option('--mode', type=click.Choice(['full', 'resume', 'incremental']), default='full',
              help='Operational mode')
@click.option('--metadata-file', default='',
              help='Optional path to metadata Excel (.xlsx). Highest priority input source for metadata mode.')
def start(mode, metadata_file):
    """Start the document search system"""
    # Initialize logging before try block so logger is always available
    setup_logging()
    logger = get_logger("main")
    
    try:
        click.echo(f"\n{'='*80}")
        click.echo(f"{SYSTEM_NAME} v{VERSION}")
        click.echo(f"Starting in {mode.upper()} mode")
        click.echo(f"{'='*80}\n")

        # Startup model check
        if not _check_paddle_models():
            click.secho(r"[WARN] PaddleOCR models not found in C:\softwares\paddle_models", fg='yellow')
            click.echo("       Please ensure paddle models are present in C:\\softwares\\paddle_models")
        else:
            click.secho("[OK] PaddleOCR models found", fg='green')

        logger.info(f"System starting in {mode} mode")

        # Optional metadata file override (highest priority source: CLI)
        metadata_file = str(metadata_file or "").strip()
        if metadata_file:
            from tagging.metadata_manager import set_active_metadata_source

            ok, message = set_active_metadata_source(metadata_file, source="cli", force=True)
            if ok:
                click.secho(f"[OK] Metadata source activated from CLI: {metadata_file}", fg='green')
            else:
                click.secho(f"[ERROR] Invalid metadata file: {message}", fg='red', bold=True)
                sys.exit(1)
        
        # Import and start orchestrator
        click.echo("Starting master orchestrator...")
        from orchestrator.master_orchestrator import MasterOrchestrator
        
        orchestrator = MasterOrchestrator()
        
        click.secho("\n[OK] System started successfully", fg='green', bold=True)
        click.echo("\nPress Ctrl+C to stop gracefully\n")
        
        # Run orchestrator (blocks until shutdown)
        orchestrator.start(mode=mode)
        
        click.secho("\n[OK] System stopped gracefully", fg='green')
        
    except KeyboardInterrupt:
        click.echo("\n\nReceived shutdown signal...")
        logger.info("Shutdown signal received")
        click.secho("[OK] System stopped", fg='green')
    except Exception as e:
        click.secho(f"\n[ERROR] System error: {e}", fg='red', bold=True)
        logger.error(f"System error: {e}", exc_info=True)
        import traceback
        traceback.print_exc()
        sys.exit(1)


@cli.command()
def stop():
    """Stop the running system gracefully"""
    click.echo("Stopping running system...")

    # Preferred path: API shutdown (if API service is running).
    try:
        import requests
        config = get_config()
        response = requests.post(
            f"http://localhost:{config.api.port}/api/shutdown",
            headers={'Authorization': f'Bearer {config.api.api_token}'},
            timeout=5
        )
        if response.status_code == 200:
            click.secho("[OK] Stop signal sent successfully via API", fg='green')
            click.echo("System will shutdown gracefully (60s grace period)")
            return
        click.secho(f"[WARN] API shutdown returned HTTP {response.status_code}", fg='yellow')
    except Exception:
        # API may be disabled in this run mode.
        pass

    # Fallback: stop orchestrator process directly.
    instances = _find_running_start_processes()
    if not instances:
        click.secho("[INFO] No running system instance found.", fg='yellow')
        return

    try:
        import psutil
    except Exception as e:
        click.secho(f"[ERROR] Cannot stop process without psutil: {e}", fg='red')
        return

    stopped = 0
    for proc in instances:
        pid = proc['pid']
        try:
            p = psutil.Process(pid)
            p.terminate()
            gone, alive = psutil.wait_procs([p], timeout=10)
            if alive:
                for still_alive in alive:
                    still_alive.kill()
            click.secho(f"[OK] Stopped process PID {pid}", fg='green')
            stopped += 1
        except Exception as e:
            click.secho(f"[WARN] Failed to stop PID {pid}: {e}", fg='yellow')

    if stopped == 0:
        click.secho("[ERROR] Could not stop running process(es).", fg='red')
    else:
        click.secho(f"[OK] Stopped {stopped} running instance(s).", fg='green')


@cli.command()
def status():
    """Get current system status"""
    try:
        import requests
        config = get_config()

        response = None
        endpoints = ["/api/status", "/status"]
        for endpoint in endpoints:
            try:
                response = requests.get(
                    f"http://localhost:{config.api.port}{endpoint}",
                    timeout=5
                )
                if response.status_code == 200:
                    break
            except requests.exceptions.ConnectionError:
                response = None
                continue
        
        if response is not None and response.status_code == 200:
            status = response.json()
            
            click.echo(f"\n{'='*80}")
            click.echo(f"System Status - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            click.echo(f"{'='*80}\n")
            
            click.echo(f"Status: {status.get('status', 'unknown').upper()}")
            click.echo(f"Mode: {status.get('mode', 'unknown')}")
            click.echo(f"Uptime: {status.get('uptime', 'unknown')}")
            
            if 'progress' in status:
                prog = status['progress']
                click.echo("\n Progress:")
                click.echo(f"  Discovered: {prog.get('discovered', 0):,}")
                click.echo(f"  Extracted:  {prog.get('extracted', 0):,}")
                click.echo(f"  Indexed:    {prog.get('indexed', 0):,}")
                click.echo(f"  OCR Pending: {prog.get('ocr_pending', 0):,}")
                click.echo(f"  OCR Complete: {prog.get('ocr_completed', 0):,}")
            
            if 'throughput' in status:
                tp = status['throughput']
                click.echo("\nThroughput:")
                click.echo(f"  Discovery: {tp.get('discovery_rate', 0):,.0f} files/sec")
                click.echo(f"  Extraction: {tp.get('extraction_rate', 0):,.0f} files/sec")
                click.echo(f"  Indexing: {tp.get('indexing_rate', 0):,.0f} docs/sec")
            
            if 'eta' in status:
                click.echo(f"\nEstimated Completion: {status['eta']}")
            
            click.echo(f"\n{'='*80}\n")
            return

        # API unavailable fallback: detect running orchestrator process directly.
        instances = _find_running_start_processes()
        if instances:
            click.secho("[OK] System process is running (API endpoint unavailable)", fg='yellow')
            for proc in instances[:5]:
                click.echo(f"  PID {proc['pid']}: {proc['cmdline']}")

            # Best-effort queue snapshot so users still get useful status.
            try:
                from core.queue_manager import get_queue_manager
                qm = get_queue_manager()
                qs = qm.get_queue_statistics() or {}
                d = qs.get('discovery', {})
                e = qs.get('extraction_total', {})
                i = qs.get('indexing', {})
                o = qs.get('ocr', {})
                t = qs.get('tagging', {})
                click.echo("\nQueue Snapshot:")
                click.echo(f"  Discovery: pending={d.get('pending', 0)} completed={d.get('completed', 0)}")
                click.echo(f"  Extraction: pending={e.get('pending', 0)} processing={e.get('processing', 0)} completed={e.get('completed', 0)}")
                click.echo(f"  Indexing: pending={i.get('pending', 0)} processing={i.get('processing', 0)} completed={i.get('completed', 0)}")
                click.echo(f"  OCR: pending={o.get('pending', 0)} processing={o.get('processing', 0)} completed={o.get('completed', 0)}")
                click.echo(f"  Tagging: pending={t.get('pending', 0)} processing={t.get('processing', 0)} completed={t.get('completed', 0)}")
            except Exception as e:
                click.secho(f"[WARN] Could not load queue snapshot: {e}", fg='yellow')
        else:
            if response is None:
                click.secho("[ERROR] System is not running", fg='red')
            else:
                click.secho(f"[ERROR] Failed to get status: HTTP {response.status_code}", fg='red')
            
    except requests.exceptions.ConnectionError:
        instances = _find_running_start_processes()
        if instances:
            click.secho("[OK] System process is running (API endpoint unavailable)", fg='yellow')
            for proc in instances[:5]:
                click.echo(f"  PID {proc['pid']}: {proc['cmdline']}")
        else:
            click.secho("[ERROR] System is not running", fg='red')
    except Exception as e:
        click.secho(f"[ERROR] {e}", fg='red')


@cli.command()
def stats():
    """Get detailed statistics"""
    try:
        from core.queue_manager import get_queue_manager
        
        setup_logging()
        queue_manager = get_queue_manager()
        stats = queue_manager.get_queue_statistics()
        
        click.echo(f"\n{'='*80}")
        click.echo(f"Detailed Statistics - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        click.echo(f"{'='*80}\n")
        
        # Discovery
        if 'discovery' in stats:
            d = stats['discovery']
            click.echo("Discovery:")
            click.echo(f"  Total:      {d.get('total', 0) or 0:,}")
            click.echo(f"  Pending:    {d.get('pending', 0) or 0:,}")
            click.echo(f"  Processing: {d.get('processing', 0) or 0:,}")
            click.echo(f"  Completed:  {d.get('completed', 0) or 0:,}")
            click.echo(f"  Failed:     {d.get('failed', 0) or 0:,}")
        
        # Extraction
        if 'extraction' in stats:
            click.echo("\nExtraction by Size Category:")
            for category, data in stats['extraction'].items():
                click.echo(f"  {category}:")
                click.echo(f"    Pending:    {data.get('pending', 0) or 0:,}")
                click.echo(f"    Processing: {data.get('processing', 0) or 0:,}")
                click.echo(f"    Completed:  {data.get('completed', 0) or 0:,}")
        
        # Indexing
        if 'indexing' in stats:
            i = stats['indexing']
            click.echo("\nIndexing:")
            click.echo(f"  Pending:    {i.get('pending', 0) or 0:,}")
            click.echo(f"  Processing: {i.get('processing', 0) or 0:,}")
            click.echo(f"  Completed:  {i.get('completed', 0) or 0:,}")
        
        # OCR
        if 'ocr' in stats:
            o = stats['ocr']
            click.echo("\nOCR:")
            click.echo(f"  Pending:    {o.get('pending', 0) or 0:,}")
            click.echo(f"  Processing: {o.get('processing', 0) or 0:,}")
            click.echo(f"  Completed:  {o.get('completed', 0) or 0:,}")
        
        # Completed
        if 'completed' in stats:
            c = stats['completed']
            click.echo("\nCompleted Files:")
            click.echo(f"  Total:         {c.get('total_completed', 0) or 0:,}")
            click.echo(f"  Duplicates:    {c.get('duplicates', 0) or 0:,}")
            avg_extract = c.get('avg_extraction_ms', 0) or 0
            avg_index = c.get('avg_indexing_ms', 0) or 0
            click.echo(f"  Avg Extract:   {avg_extract:.0f} ms")
            click.echo(f"  Avg Index:     {avg_index:.0f} ms")
        
        # Failures
        if 'failures' in stats and stats['failures']:
            click.echo(f"\nFailures ({stats.get('total_failures', 0) or 0:,} total):")
            for error_type, count in stats['failures'].items():
                click.echo(f"  {error_type}: {count or 0:,}")
        
        click.echo(f"\n{'='*80}\n")
        
    except Exception as e:
        click.secho(f"✗ Error: {e}", fg='red')
        import traceback
        traceback.print_exc()


@cli.command()
@click.option('--force', is_flag=True, help='Force reset without confirmation')
def reset(force):
    """Reset system: clear all queues and data to start fresh"""
    try:
        click.echo(f"\n{'='*80}")
        click.echo("System Reset")
        click.echo(f"{'='*80}\n")
        
        if not force:
            click.secho("⚠ WARNING: This will delete ALL progress and queue data!", fg='yellow', bold=True)
            click.echo("The system will forget all processed files and re-index everything from scratch.")
            click.echo("This cannot be undone.")
            
            if not click.confirm("\nAre you sure you want to continue?"):
                click.echo("Reset cancelled.")
                return

        config = get_config()
        working_root = Path(config.paths.working_root)
        
        # 0. Kill ALL processes that might be holding locks or stale in-memory stats
        click.echo("Stopping all DocumentSearch processes...")
        import os
        import time
        stop_errors = []
        stopped_pids = set()
        try:
            # Stop orchestrator trees first so worker children are terminated recursively.
            for proc in _find_running_start_processes():
                pid = proc.get('pid')
                if not isinstance(pid, int) or pid <= 0 or pid == os.getpid():
                    continue
                if _terminate_process_tree(pid, timeout_sec=8.0):
                    stopped_pids.add(pid)
                else:
                    stop_errors.append(f"could not fully stop orchestrator PID {pid}")

            # Stop dashboard processes so Streamlit does not show stale in-memory stats after reset.
            for proc in _find_dashboard_processes():
                pid = proc.get('pid')
                if not isinstance(pid, int) or pid <= 0 or pid == os.getpid() or pid in stopped_pids:
                    continue
                if _terminate_process_tree(pid, timeout_sec=5.0):
                    stopped_pids.add(pid)
                else:
                    stop_errors.append(f"could not fully stop dashboard PID {pid}")

            if stopped_pids:
                click.echo(f"  Stopped {len(stopped_pids)} related process(es) (orchestrator/dashboard)")
            else:
                click.echo("  No running orchestrator/dashboard processes found")
        except Exception as e:
            stop_errors.append(str(e))

        if stop_errors:
            click.secho("  ⚠ Some processes may still be running:", fg='yellow')
            for msg in stop_errors[:3]:
                click.secho(f"    - {msg}", fg='yellow')
            click.secho("  If counters repopulate immediately, close the dashboard/system and reset again.", fg='yellow')
        
        # Small delay to allow file handles to be released
        time.sleep(3)
        
        # 1. Delete queue database directly (avoid SQLite locking issues)
        click.echo("Clearing queue database...")
        queue_dir = working_root / "queue"
        db_cleared = False
        if queue_dir.exists():
            for db_file in ["queues.db", "queues.db-wal", "queues.db-shm", "queues.db-journal"]:
                db_path = queue_dir / db_file
                if db_path.exists():
                    # Try multiple times with increasing delays
                    import gc
                    import time
                    for attempt in range(3):
                        try:
                            gc.collect()
                            time.sleep(0.5 * attempt)  # Increasing delay
                            db_path.unlink()
                            click.echo(f"  Deleted: {db_file}")
                            deleted = True
                            db_cleared = True
                            break
                        except PermissionError:
                            if attempt < 2:
                                continue
                            click.secho(f"  Warning: {db_file} is locked. Stop dashboard first.", fg='yellow')
                        except Exception as e:
                            click.secho(f"  Warning: Could not delete {db_file}: {e}", fg='yellow')
                            break
        
        if db_cleared:
            click.secho("✓ Queue database cleared", fg='green')
        else:
            # Check if files just don't exist (already clean)
            queue_exists = any((queue_dir / f).exists() for f in ["queues.db", "queues.db-wal", "queues.db-shm", "queues.db-journal"]) if queue_dir.exists() else False
            if queue_exists:
                click.secho("⚠ Queue database files locked", fg='yellow')
                click.echo("  Close the dashboard and try again, or run: python src/main.py reset --force")
            else:
                click.secho("✓ Queue database already clean", fg='green')
        
        # 1.5. Clear Redis database if using Redis
        click.echo("Checking Redis configuration...")
        try:
            import redis
            from core.redis_queue_manager import RedisQueueManager
            from core.config_manager import get_config_manager

            # Safely read raw config section for Redis (ConfigurationManager does not expose 'redis' in SystemConfig)
            cfg_mgr = get_config_manager()
            raw = getattr(cfg_mgr, 'raw_config', {}) or {}
            redis_section = raw.get('redis')

            if isinstance(redis_section, dict) and redis_section.get('url'):
                redis_url = redis_section.get('url')
                click.echo("Redis detected - clearing Redis database...")
                try:
                    redis_manager = RedisQueueManager(redis_url=redis_url)
                    r = redis_manager.client
                    
                    # Use reset_database first (clears docsearch:* keys)
                    redis_manager.reset_database()
                    
                    # Then do a second pass to catch ANY remaining docsearch keys
                    remaining = 0
                    cursor = 0
                    while True:
                        cursor, keys = r.scan(cursor=cursor, match='docsearch:*', count=1000)
                        if keys:
                            r.delete(*keys)
                            remaining += len(keys)
                        if cursor == 0:
                            break
                    
                    if remaining > 0:
                        click.echo(f"  Cleaned {remaining} additional Redis keys")
                    
                    # Verify: confirm zero keys remain
                    verify_count = len(list(r.scan_iter('docsearch:*', count=100)))
                    if verify_count == 0:
                        click.secho("✓ Redis database cleared (verified: 0 keys remain)", fg='green')
                    else:
                        click.secho(f"⚠ Redis partially cleared ({verify_count} keys remain)", fg='yellow')
                    
                except Exception as redis_error:
                    if "connection" in str(redis_error).lower() or "refused" in str(redis_error).lower():
                        click.secho("  ⚠ Redis is not running (will be reset when started)", fg='yellow')
                    else:
                        click.secho(f"  Warning: Could not clear Redis: {redis_error}", fg='yellow')
            else:
                click.echo("  Redis not configured in config file. Attempting default Redis at redis://localhost:6379/0...")
                try:
                    # Try default Redis URL if present on system
                    redis_manager = RedisQueueManager()
                    redis_manager.reset_database()
                    click.secho("✓ Redis database cleared (default)", fg='green')
                except Exception:
                    # If default Redis not running, skip silently
                    click.echo("  Default Redis not available, skipping Redis reset")
        except ImportError:
            click.echo("  Redis module not available, using SQLite mode")
        except Exception as e:
            click.secho(f"  Warning: Could not check Redis: {e}", fg='yellow')
        
        # 2. Clear Bloom filter files (important for re-discovery!)
        click.echo("Clearing Bloom filters...")
        discovery_dir = working_root / "discovery"
        if discovery_dir.exists():
            bloom_files = list(discovery_dir.glob("bloom_filter_*.pkl"))
            for bf in bloom_files:
                try:
                    bf.unlink()
                    click.echo(f"  Deleted: {bf.name}")
                except Exception as e:
                    click.secho(f"  Warning: Could not delete {bf.name}: {e}", fg='yellow')
            if bloom_files:
                click.secho(f"✓ Cleared {len(bloom_files)} Bloom filter files", fg='green')
            else:
                click.echo("  No Bloom filter files to clear")
        
        # 3. Clear ALL .pkl files in working directory (hash caches, etc.)
        click.echo("Clearing cache files...")
        pkl_count = 0
        for pkl_file in working_root.rglob("*.pkl"):
            try:
                pkl_file.unlink()
                pkl_count += 1
            except Exception:
                pass
        if pkl_count > 0:
            click.secho(f"✓ Cleared {pkl_count} cache files", fg='green')
        
        # 4. Clear checkpoints
        click.echo("Clearing checkpoints...")
        checkpoint_dir = working_root / "checkpoints"
        if checkpoint_dir.exists():
            checkpoint_files = list(checkpoint_dir.glob("checkpoint_*.json"))
            for cp in checkpoint_files:
                try:
                    cp.unlink()
                except Exception:
                    pass
            if checkpoint_files:
                click.secho(f"✓ Cleared {len(checkpoint_files)} checkpoint files", fg='green')
            else:
                click.echo("  No checkpoint files to clear")
        
        # 5. Clear OpenSearch index
        click.echo("Clearing OpenSearch index...")
        try:
            from opensearchpy import OpenSearch
            os_config = config.indexing.opensearch
            
            # Parse hosts from config (list of URLs or host:port strings)
            hosts = []
            for host_str in os_config.hosts:
                if host_str.startswith('http'):
                    hosts.append(host_str)
                else:
                    parts = host_str.split(':')
                    host = parts[0]
                    port = int(parts[1]) if len(parts) > 1 else 9200
                    hosts.append({'host': host, 'port': port})
            
            client = OpenSearch(
                hosts=hosts,
                http_compress=True,
                timeout=5,
                max_retries=1
            )
            index_name = os_config.index_name
            if client.indices.exists(index=index_name):
                client.indices.delete(index=index_name)
                click.secho(f"✓ Deleted OpenSearch index: {index_name}", fg='green')
            else:
                click.echo(f"  OpenSearch index '{index_name}' does not exist")
        except Exception as e:
            error_msg = str(e)
            if "connection" in error_msg.lower() or "refused" in error_msg.lower():
                click.secho("  ⚠ OpenSearch is not running (index will be recreated automatically)", fg='yellow')
                click.echo("  To start OpenSearch: cd bin && .\\start_opensearch.bat")
            else:
                click.secho(f"  Warning: Could not clear OpenSearch index: {e}", fg='yellow')
        
        # 6. Clear error logs (optional - fresh start)
        click.echo("Clearing old logs...")
        logs_dir = working_root / "logs"
        if logs_dir.exists():
            log_count = 0
            for log_file in logs_dir.glob("*.log"):
                try:
                    log_file.unlink()
                    log_count += 1
                except Exception:
                    pass
            if log_count > 0:
                click.secho(f"✓ Cleared {log_count} log files", fg='green')
        
        # 7. Clear Audit Directory (ALL audit data including state matrices)
        click.echo("Clearing audit history...")
        audit_dir = working_root / "audit"
        if audit_dir.exists():
            audit_files_deleted = 0
            audit_locked = []
            # Delete ALL files in audit directory (audit.db, state_matrix_*.xlsx, etc.)
            for audit_file in audit_dir.iterdir():
                if audit_file.is_file():
                    for attempt in range(3):
                        try:
                            import gc
                            gc.collect()
                            time.sleep(0.3 * attempt)
                            audit_file.unlink()
                            audit_files_deleted += 1
                            break
                        except PermissionError:
                            if attempt == 2:
                                audit_locked.append(audit_file.name)
                        except Exception as e:
                            click.secho(f"  Warning: Could not delete {audit_file.name}: {e}", fg='yellow')
                            break
            
            if audit_files_deleted > 0:
                click.secho(f"✓ Cleared {audit_files_deleted} audit files (including state matrices)", fg='green')
            if audit_locked:
                click.secho(f"  ⚠ Locked files (close dashboard first): {', '.join(audit_locked)}", fg='yellow')
            if audit_files_deleted == 0 and not audit_locked:
                click.echo("  No audit files to clear")
        
        # 8. Clear Reports Directory (ALL generated reports)
        click.echo("Clearing reports...")
        reports_dir = working_root / "reports"
        if reports_dir.exists():
            reports_deleted = 0
            # Delete ALL files in reports directory
            for report_file in reports_dir.iterdir():
                if report_file.is_file():
                    try:
                        report_file.unlink()
                        reports_deleted += 1
                    except Exception as e:
                        click.secho(f"  Warning: Could not delete {report_file.name}: {e}", fg='yellow')
            
            if reports_deleted > 0:
                click.secho(f"✓ Cleared {reports_deleted} report files", fg='green')
            else:
                click.echo("  No report files to clear")
        
        # 9. Clear Metrics Directory
        click.echo("Clearing metrics...")
        metrics_dir = working_root / "metrics"
        if metrics_dir.exists():
            metrics_deleted = 0
            for metrics_file in metrics_dir.iterdir():
                if metrics_file.is_file():
                    try:
                        metrics_file.unlink()
                        metrics_deleted += 1
                    except Exception as e:
                        click.secho(f"  Warning: Could not delete {metrics_file.name}: {e}", fg='yellow')
            if metrics_deleted > 0:
                click.secho(f"✓ Cleared {metrics_deleted} metrics files", fg='green')
            else:
                click.echo("  No metrics files to clear")

        # 10. Write dashboard reset marker so surviving UI sessions hard-clear caches.
        try:
            marker_file = working_root / "cache" / "dashboard_reset.marker"
            marker_file.parent.mkdir(parents=True, exist_ok=True)
            marker_file.write_text(datetime.now().isoformat(), encoding='utf-8')
            click.echo("Set dashboard reset marker")
        except Exception as e:
            click.secho(f"  Warning: Could not write dashboard reset marker: {e}", fg='yellow')

        # ── Final Verification ──
        click.echo(f"\n{'='*80}")
        click.echo("Verification:")
        
        # Verify Redis is empty
        verify_failed = False
        try:
            import redis as redis_mod
            from core.redis_queue_manager import RedisQueueManager as _RQM
            from core.config_manager import get_config_manager as _gcm
            _cfg = _gcm()
            _raw = getattr(_cfg, 'raw_config', {}) or {}
            _rs = _raw.get('redis')
            if isinstance(_rs, dict) and _rs.get('url'):
                _r = redis_mod.from_url(_rs['url'], protocol=2)
                _remaining = len(list(_r.scan_iter('docsearch:*', count=100)))
                if _remaining == 0:
                    click.secho("  ✓ Redis: 0 keys (clean)", fg='green')
                else:
                    click.secho(f"  ✗ Redis: {_remaining} keys still remain!", fg='red')
                    verify_failed = True
        except Exception:
            pass
        
        # Verify audit directory
        if audit_dir.exists():
            remaining_files = list(audit_dir.iterdir())
            if remaining_files:
                click.secho(f"  ✗ Audit: {len(remaining_files)} files remain (locked by dashboard?)", fg='red')
                verify_failed = True
            else:
                click.secho("  ✓ Audit: empty (clean)", fg='green')
        else:
            click.secho("  ✓ Audit: directory not present (clean)", fg='green')
        
        # Verify queue database
        queue_db_path = working_root / "queue" / "queues.db"
        if queue_db_path.exists():
            click.secho(f"  ✗ Queue DB: still exists ({queue_db_path.stat().st_size} bytes)", fg='red')
            verify_failed = True
        else:
            click.secho("  ✓ Queue DB: deleted (clean)", fg='green')
        
        click.echo(f"{'='*80}")
        
        if verify_failed:
            click.secho("\n⚠ Reset partially completed!", fg='yellow', bold=True)
            click.echo("Some files could not be deleted (likely locked by the dashboard).")
            click.echo("To fix: close the Streamlit dashboard, then run reset again.")
        else:
            click.secho("\n✓ System reset complete!", fg='green', bold=True)
        
        click.echo("\nCleared:")
        click.echo("  • Redis counters & queues")
        click.echo("  • Queue database (file tracking)")
        click.echo("  • Bloom filters (duplicate detection)")
        click.echo("  • Cache files (.pkl)")
        click.echo("  • Checkpoints")
        click.echo("  • OpenSearch index")
        click.echo("  • Log files")
        click.echo("  • Audit files (audit.db + state matrices)")
        click.echo("  • Report files")
        click.echo("  • Metrics files")
        click.echo("\nYou can now run 'python src/main.py start' to re-process everything.")
        click.echo(f"{'='*80}\n")
        
    except Exception as e:
        click.secho(f"✗ Reset failed: {e}", fg='red', bold=True)
        import traceback
        traceback.print_exc()
        sys.exit(1)


@cli.command()
@click.option('--timeout-minutes', default=30, help='Timeout in minutes for stale items')
def reset_stale(timeout_minutes):
    """Reset stale processing items back to pending after crashes/shutdowns"""
    try:
        click.echo(f"\n{'='*80}")
        click.echo("Reset Stale Processing Items")
        click.echo(f"{'='*80}\n")
        
        from core.queue_manager import get_queue_manager
        queue_manager = get_queue_manager()
        
        # Reset stale items with backend-compatible calling convention
        extraction_reset = indexing_reset = ocr_reset = 0
        try:
            # RedisQueueManager-style API returns dict counters
            result = queue_manager.reset_stale_processing(timeout_minutes=timeout_minutes)
            if isinstance(result, dict):
                extraction_reset = int(result.get('extraction', 0))
                indexing_reset = int(result.get('indexing', 0))
                ocr_reset = int(result.get('ocr', 0))
            else:
                # Unexpected return shape, keep backwards-compat fallback below
                raise TypeError("Unexpected reset_stale_processing return type")
        except TypeError:
            # QueueManager-style API (SQLite) resets each table separately
            stale_timeout = timeout_minutes * 60  # Convert to seconds
            extraction_reset = queue_manager.reset_stale_processing('extraction_queue', stale_timeout)
            indexing_reset = queue_manager.reset_stale_processing('indexing_queue', stale_timeout)
            ocr_reset = queue_manager.reset_stale_processing('ocr_queue', stale_timeout)
        
        if extraction_reset or indexing_reset or ocr_reset:
            click.secho("\nReset Summary:", fg='green')
            if extraction_reset:
                click.echo(f"  Extraction: {extraction_reset} items reset")
            if indexing_reset:
                click.echo(f"  Indexing: {indexing_reset} items reset")
            if ocr_reset:
                click.echo(f"  OCR: {ocr_reset} items reset")
        else:
            click.echo("No stale items found.")
        
        click.echo(f"\n{'='*80}\n")
        
    except Exception as e:
        click.secho(f"\n✗ Error: {e}", fg='red')
        import traceback
        traceback.print_exc()


@cli.command()
def validate():
    """Validate system configuration and dependencies"""
    try:
        click.echo(f"\n{'='*80}")
        click.echo("Configuration Validation")
        click.echo(f"{'='*80}\n")
        
        # Load configuration
        config_manager = get_config_manager()
        config = config_manager.get_config()
        
        issues = []
        warnings = []
        
        # Check Python version
        if sys.version_info < (3, 10):
            issues.append("Python 3.10+ required")
        else:
            click.secho(f"✓ Python {sys.version_info.major}.{sys.version_info.minor}", fg='green')
        
        # Check source drive exists
        source_path = Path(config.paths.source_drive)
        if source_path.exists():
            click.secho(f"✓ Source drive exists: {config.paths.source_drive}", fg='green')
        else:
            issues.append(f"Source drive not found: {config.paths.source_drive}")
        
        # Check working root accessible
        working_root = Path(config.paths.working_root)
        if working_root.parent.exists():
            click.secho(f"✓ Working root parent accessible: {working_root.parent}", fg='green')
        else:
            issues.append(f"Working root parent not accessible: {working_root.parent}")
        
        # Check required Python packages
        required_packages = [
            'yaml', 'requests', 'opensearchpy', 'pytesseract',
            'PIL', 'cv2', 'mmh3', 'bitarray', 'fastapi', 'click',
            'pandas', 'openpyxl'
        ]
        
        for package in required_packages:
            try:
                __import__(package)
                click.secho(f"✓ Package installed: {package}", fg='green')
            except ImportError:
                issues.append(f"Required package not installed: {package}")
        
        # Print summary
        click.echo(f"\n{'='*80}")
        if not issues and not warnings:
            click.secho("✓ Validation passed - no issues found!", fg='green', bold=True)
        else:
            if warnings:
                click.secho(f"\n⚠ Warnings ({len(warnings)}):", fg='yellow', bold=True)
                for warning in warnings:
                    click.secho(f"  • {warning}", fg='yellow')
            
            if issues:
                click.secho(f"\n✗ Issues ({len(issues)}):", fg='red', bold=True)
                for issue in issues:
                    click.secho(f"  • {issue}", fg='red')
                click.secho("\nPlease resolve issues before starting the system", fg='red')
        
        click.echo(f"{'='*80}\n")
        
    except Exception as e:
        click.secho(f"✗ Validation error: {e}", fg='red')
        import traceback
        traceback.print_exc()





@cli.command()
def health_check():
    """Check health of all services"""
    try:
        click.echo(f"\n{'='*80}")
        click.echo("Service Health Check")
        click.echo(f"{'='*80}\n")
        
        config = get_config()
        all_healthy = True
        
        # Check Tika instances
        import requests
        click.echo("Tika Instances:")
        for tika_inst in config.extraction.tika.instances:
            try:
                if _is_tika_healthy(tika_inst.host, tika_inst.port, timeout=5):
                    click.secho(f"  ✓ Port {tika_inst.port}: Healthy", fg='green')
                else:
                    click.secho(f"  ✗ Port {tika_inst.port}: Unhealthy", fg='red')
                    all_healthy = False
            except Exception as e:
                click.secho(f"  ✗ Port {tika_inst.port}: Unreachable ({e})", fg='red')
                all_healthy = False
        
        # Check OpenSearch
        click.echo("\nOpenSearch:")
        try:
            os_config = config.indexing.opensearch
            auth = (os_config.username, os_config.password) if os_config.username else None
            response = requests.get(
                f"{os_config.hosts[0]}/_cluster/health",
                auth=auth,
                verify=False,
                timeout=5
            )
            if response.status_code == 200:
                health = response.json()
                status = health.get('status', 'unknown')
                if status == 'green':
                    click.secho(f"  ✓ Status: {status.upper()}", fg='green')
                elif status == 'yellow':
                    click.secho(f"  ⚠ Status: {status.upper()}", fg='yellow')
                    all_healthy = False
                else:
                    click.secho(f"  ✗ Status: {status.upper()}", fg='red')
                    all_healthy = False
            else:
                click.secho(f"  ✗ Unhealthy (HTTP {response.status_code})", fg='red')
                all_healthy = False
        except Exception as e:
            click.secho(f"  ✗ Unreachable ({e})", fg='red')
            all_healthy = False
        
        # Check PaddleOCR
        click.echo("\nPaddleOCR:")
        try:
            import paddleocr  # noqa: F401
            click.secho("  ✓ Available", fg='green')
        except ImportError:
            click.secho("  ✗ Not installed (run: pip install paddleocr paddlepaddle)", fg='red')
            all_healthy = False
        
        # Summary
        click.echo(f"\n{'='*80}")
        if all_healthy:
            click.secho("✓ All services healthy", fg='green', bold=True)
        else:
            click.secho("✗ Some services unhealthy", fg='red', bold=True)
        click.echo(f"{'='*80}\n")
        
    except Exception as e:
        click.secho(f"✗ Health check error: {e}", fg='red')
        import traceback
        traceback.print_exc()


if __name__ == '__main__':
    cli()
