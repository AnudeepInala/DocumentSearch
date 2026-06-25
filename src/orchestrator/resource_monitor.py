"""
Resource Monitor - System resource monitoring
"""

import psutil
from typing import Dict, Any

from core.logging_manager import get_logger
from core.config_manager import get_config

logger = get_logger("orchestrator.resources")


class ResourceMonitor:
    """Monitors system resources (CPU, RAM, Disk)"""
    
    def __init__(self):
        self.config = get_config()
        self.thresholds = self.config.orchestrator

    def _effective_memory_thresholds(self, total_gb: float) -> Dict[str, float]:
        """Compute memory thresholds that work across both small and large RAM hosts.

        Configured GB values are treated as minimums. We also enforce percent-based
        floors to avoid false positives on machines with very large total RAM.
        """
        configured_warning = float(self.thresholds.memory['warning_threshold_gb'])
        configured_critical = float(self.thresholds.memory['critical_threshold_gb'])

        # Percent floors keep behavior sane on high-memory systems.
        percent_warning = total_gb * 0.85
        percent_critical = total_gb * 0.90

        warning_gb = max(configured_warning, percent_warning)
        critical_gb = max(configured_critical, percent_critical)

        # Ensure ordering even if config is inverted.
        if critical_gb <= warning_gb:
            critical_gb = warning_gb + 1.0

        return {
            'warning_gb': warning_gb,
            'critical_gb': critical_gb,
        }
    
    def check_resources(self) -> Dict[str, Any]:
        """Check current resource usage"""
        cpu_percent = psutil.cpu_percent(interval=1)
        memory = psutil.virtual_memory()
        disk = psutil.disk_usage(self.config.paths.working_root)
        
        # Check thresholds
        cpu_high = cpu_percent > self.thresholds.cpu['high_threshold_percent']
        
        memory_gb = memory.used / (1024**3)
        total_gb = memory.total / (1024**3)
        mem_thresholds = self._effective_memory_thresholds(total_gb)
        memory_warning = memory_gb > mem_thresholds['warning_gb']
        memory_critical = memory_gb > mem_thresholds['critical_gb']
        
        disk_gb_free = disk.free / (1024**3)
        disk_warning = disk_gb_free < self.thresholds.disk['warning_threshold_gb']
        disk_critical = disk_gb_free < self.thresholds.disk['critical_threshold_gb']
        
        result = {
            'cpu_percent': cpu_percent,
            'cpu_high': cpu_high,
            'memory_used_gb': memory_gb,
            'memory_total_gb': total_gb,
            'memory_percent': memory.percent,
            'memory_warning': memory_warning,
            'memory_critical': memory_critical,
            'disk_used_gb': disk.used / (1024**3),
            'disk_free_gb': disk_gb_free,
            'disk_total_gb': disk.total / (1024**3),
            'disk_percent': disk.percent,
            'disk_warning': disk_warning,
            'disk_critical': disk_critical,
            'critical': memory_critical or disk_critical
        }
        
        # Log warnings
        if cpu_high:
            logger.warning(f"High CPU usage: {cpu_percent:.1f}%")
        
        if memory_warning:
            logger.warning(f"High memory usage: {memory_gb:.1f}GB / {memory.total/(1024**3):.1f}GB ({memory.percent:.1f}%)")
        
        if disk_warning:
            logger.warning(f"Low disk space: {disk_gb_free:.1f}GB free")
        
        return result
