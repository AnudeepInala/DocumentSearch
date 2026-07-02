"""
Health Monitor - Service health checking
"""

import requests
import time
from typing import Dict, Any

from core.logging_manager import get_logger
from core.config_manager import get_config

logger = get_logger("orchestrator.health")


class HealthMonitor:
    """Monitors health of external services"""
    
    def __init__(self):
        self.config = get_config()
    
    def check_all_services(self) -> Dict[str, Any]:
        """Check health of all services"""
        return {
            'tika': self.check_tika(),
            'opensearch': self.check_opensearch(),
            'paddle': self.check_paddle()
        }
    
    def check_tika(self) -> Dict[str, bool]:
        """Check Tika instances with retry on transient failure."""
        results = {}
        
        for instance in self.config.extraction.tika.instances:
            url = f"http://{instance.host}:{instance.port}/tika"
            
            # L7: Retry once on transient failure before marking as dead
            for attempt in range(2):
                try:
                    response = requests.get(url, timeout=5)
                    results[instance.port] = response.status_code == 200
                    break
                except Exception:
                    if attempt == 0:
                        time.sleep(2)  # Wait before retry
                    else:
                        results[instance.port] = False
        
        return results
    
    def check_opensearch(self) -> bool:
        """Check OpenSearch cluster with retry on transient failure."""
        # L7: Retry once before reporting failure
        for attempt in range(2):
            try:
                host = self.config.indexing.opensearch.hosts[0]
                response = requests.get(f"{host}/_cluster/health", timeout=5)
                
                if response.status_code == 200:
                    health = response.json()
                    return health.get('status') in ['green', 'yellow']
            except Exception:
                if attempt == 0:
                    time.sleep(2)
        
        return False
    
    def check_paddle(self) -> bool:
        """Check PaddleOCR availability"""
        try:
            import paddleocr  # noqa: F401
            return True
        except ImportError:
            return False
