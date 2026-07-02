"""
Checkpoint Manager - State persistence for resume capability
"""

import json
import time
from pathlib import Path
from typing import Dict, Any, Optional
from datetime import datetime

from core.logging_manager import get_logger
from core.config_manager import get_config
from core.queue_manager import get_queue_manager

logger = get_logger("orchestrator.checkpoint")


class CheckpointManager:
    """Manages system state checkpoints"""
    
    def __init__(self):
        self.config = get_config()
        self.queue_manager = get_queue_manager()
        
        self.checkpoint_dir = Path(self.config.paths.checkpoints_dir)
        self.checkpoint_dir.mkdir(parents=True, exist_ok=True)
        
        self.retention_count = self.config.orchestrator.checkpoint['retention_count']
    
    def create_checkpoint(self) -> bool:
        """Create a system state checkpoint using atomic write (temp + rename)."""
        try:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            checkpoint_file = self.checkpoint_dir / f"checkpoint_{timestamp}.json"
            temp_file = self.checkpoint_dir / f"checkpoint_{timestamp}.tmp"
            
            # Gather state data
            checkpoint_data = {
                'timestamp': timestamp,
                'created_at': datetime.now().isoformat(),
                'queue_stats': self.queue_manager.get_queue_stats(),
                'system_uptime': time.time()
            }
            
            # L6: Write to temp file first, then atomic rename.
            # If interrupted mid-write, the temp file is left behind
            # but the previous valid checkpoint survives.
            with open(temp_file, 'w') as f:
                json.dump(checkpoint_data, f, indent=2)
            
            # Atomic rename (on Windows, need to remove target first if exists)
            if checkpoint_file.exists():
                checkpoint_file.unlink()
            temp_file.rename(checkpoint_file)
            
            logger.info(f"Created checkpoint: {checkpoint_file.name}")
            
            # Cleanup old checkpoints
            self._cleanup_old_checkpoints()
            
            return True
            
        except Exception as e:
            logger.error(f"Error creating checkpoint: {e}")
            # Clean up temp file if it exists
            try:
                if temp_file.exists():
                    temp_file.unlink()
            except Exception:
                pass
            return False
    
    def load_checkpoint(self) -> Optional[Dict[str, Any]]:
        """Load the most recent checkpoint"""
        try:
            checkpoints = sorted(self.checkpoint_dir.glob("checkpoint_*.json"))
            
            if not checkpoints:
                logger.info("No checkpoints found")
                return None
            
            latest = checkpoints[-1]
            
            with open(latest, 'r') as f:
                data = json.load(f)
            
            logger.info(f"Loaded checkpoint: {latest.name}")
            return data
            
        except Exception as e:
            logger.error(f"Error loading checkpoint: {e}")
            return None
    
    def _cleanup_old_checkpoints(self) -> None:
        """Remove old checkpoints beyond retention count"""
        try:
            checkpoints = sorted(self.checkpoint_dir.glob("checkpoint_*.json"))
            
            if len(checkpoints) > self.retention_count:
                to_delete = checkpoints[:-self.retention_count]
                
                for checkpoint in to_delete:
                    checkpoint.unlink()
                    logger.debug(f"Deleted old checkpoint: {checkpoint.name}")
                    
        except Exception as e:
            logger.warning(f"Error cleaning up checkpoints: {e}")
