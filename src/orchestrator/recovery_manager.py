"""
Recovery Manager - Handles rescuing of 'zombie' tasks that are lost from queues
"""

import time
import json
from typing import Dict, Any
import threading

from core.logging_manager import get_logger
from core.config_manager import get_config
from core.queue_manager import get_queue_manager
from core.constants import QueueStatus

logger = get_logger("recovery_manager")

class RecoveryManager:
    """
    Scans for and recovers tasks that are in an inconsistent state:
    - Marked 'pending' but not in any queue
    - Marked 'processing' but not in any processing set (orphaned)
    """
    
    def __init__(self):
        self.config = get_config()
        self.queue_manager = get_queue_manager()
        self.running = False
        self._lock = threading.Lock()
        
    def recover_all(self) -> Dict[str, int]:
        """
        Run a full recovery scan.
        
        OPTIMIZED: Pre-collects all queued file IDs into a set (O(M)), then checks
        each file against the set (O(1) per file). Total complexity: O(N+M) instead
        of O(N*M).
        
        Returns stats on recovered items.
        """
        logger.info("Starting system recovery scan for zombie tasks...")
        start_time = time.time()
        
        recovered_counts = {
            'extraction': 0,
            'indexing': 0,
            'ocr': 0,
            'discovery': 0
        }
        
        try:
            # Phase 1: Build a set of all file IDs currently in queues (O(M))
            active_file_ids = self._collect_all_queued_file_ids()
            logger.info(f"Found {len(active_file_ids)} file IDs across all queues/processing sets")
            
            # Phase 2: Also build a set of all file IDs in processing sets
            processing_file_ids = self._collect_all_processing_file_ids()
            logger.info(f"Found {len(processing_file_ids)} file IDs in processing sets")
            
            # Combine both sets for master lookup
            all_active_ids = active_file_ids | processing_file_ids
            
            # Phase 3: Iterate files and check O(1) membership
            cursor = 0
            processed_count = 0
            
            while True:
                cursor, keys = self.queue_manager.client.scan(
                    cursor=cursor, 
                    match=f"{self.queue_manager.PREFIX}files:*", 
                    count=100
                )
                
                for key in keys:
                    try:
                        # key is like b"docsearch:files:12345" or "docsearch:files:12345"
                        file_id = key.split(":")[-1] if isinstance(key, str) else key.decode().split(":")[-1]
                        self._check_and_recover_file_fast(file_id, recovered_counts, all_active_ids)
                        processed_count += 1
                    except Exception as e:
                        logger.error(f"Error checking file key {key}: {e}")
                
                if cursor == 0:
                    break
                    
            logger.info(f"Recovery scan complete. Checked {processed_count} files in {time.time() - start_time:.2f}s")
            logger.info(f"Recovered: {recovered_counts}")
            
            return recovered_counts
            
        except Exception as e:
            logger.error(f"Recovery scan failed: {e}")
            return recovered_counts

    def _collect_all_queued_file_ids(self) -> set:
        """Collect all file IDs from all queues into a set (O(M) total)."""
        ids = set()
        qm = self.queue_manager
        
        # Discovery queue (sorted set with raw file_ids)
        cursor = 0
        while True:
            cursor, members = qm.client.zscan(qm.QUEUE_DISCOVERY, cursor, count=500)
            for member, _score in members:
                m = member.decode() if isinstance(member, bytes) else str(member)
                ids.add(m)
            if cursor == 0:
                break
        
        # Extraction queues (sorted sets with JSON items)
        for cat in ['tiny', 'small', 'medium', 'large']:
            queue_key = f"{qm.QUEUE_EXTRACTION}:{cat}"
            cursor = 0
            while True:
                cursor, members = qm.client.zscan(queue_key, cursor, count=500)
                for member, _score in members:
                    try:
                        item = json.loads(member)
                        fid = str(item.get('file_id', item.get('id', '')))
                        if fid:
                            ids.add(fid)
                    except (json.JSONDecodeError, TypeError):
                        m = member.decode() if isinstance(member, bytes) else str(member)
                        ids.add(m)
                if cursor == 0:
                    break
        
        # OCR queue (sorted set with JSON items)
        cursor = 0
        while True:
            cursor, members = qm.client.zscan(qm.QUEUE_OCR, cursor, count=500)
            for member, _score in members:
                try:
                    item = json.loads(member)
                    fid = str(item.get('file_id', ''))
                    if fid:
                        ids.add(fid)
                except (json.JSONDecodeError, TypeError):
                    m = member.decode() if isinstance(member, bytes) else str(member)
                    ids.add(m)
            if cursor == 0:
                break
        
        # L9: Indexing queue (LIST — was previously missing!)
        try:
            idx_items = qm.client.lrange(qm.QUEUE_INDEXING, 0, -1)
            for item_json in idx_items:
                try:
                    item = json.loads(item_json)
                    fid = str(item.get('file_id', ''))
                    if fid:
                        ids.add(fid)
                except (json.JSONDecodeError, TypeError):
                    pass
        except Exception:
            pass
        
        # Tagging queue (sorted set with JSON items)
        try:
            cursor = 0
            while True:
                cursor, members = qm.client.zscan(qm.QUEUE_TAGGING, cursor, count=500)
                for member, _score in members:
                    try:
                        item = json.loads(member)
                        fid = str(item.get('file_id', ''))
                        if fid:
                            ids.add(fid)
                    except (json.JSONDecodeError, TypeError):
                        pass
                if cursor == 0:
                    break
        except Exception:
            pass
        
        return ids
    
    def _collect_all_processing_file_ids(self) -> set:
        """Collect all file IDs from all processing sets (O(P) total)."""
        ids = set()
        qm = self.queue_manager
        
        # Extraction processing sets
        for key in qm._get_extraction_processing_keys():
            try:
                fields = qm.client.hkeys(key)
                for f in fields:
                    ids.add(f.decode() if isinstance(f, bytes) else str(f))
            except Exception:
                pass
        
        # Indexing processing sets
        for key in qm._get_indexing_processing_keys():
            try:
                fields = qm.client.hkeys(key)
                for f in fields:
                    ids.add(f.decode() if isinstance(f, bytes) else str(f))
            except Exception:
                pass
        
        # OCR processing sets
        for key in qm._get_ocr_processing_keys():
            try:
                fields = qm.client.hkeys(key)
                for f in fields:
                    ids.add(f.decode() if isinstance(f, bytes) else str(f))
            except Exception:
                pass
        
        # Tagging processing sets
        for key in qm._get_tagging_processing_keys():
            try:
                fields = qm.client.hkeys(key)
                for f in fields:
                    ids.add(f.decode() if isinstance(f, bytes) else str(f))
            except Exception:
                pass
        
        return ids

    def _check_and_recover_file_fast(self, file_id: str, counts: Dict[str, int], all_active_ids: set) -> None:
        """Check a single file for zombie status using pre-collected set (O(1) lookup)."""
        try:
            file_data = self.queue_manager.client.hgetall(f"{self.queue_manager.HASH_FILES}:{file_id}")
            if not file_data:
                return
                
            status = file_data.get('status')
            if isinstance(status, bytes):
                status = status.decode()
            
            # 1. Check PENDING files that are missing from queues
            if status == QueueStatus.PENDING.value:
                if str(file_id) not in all_active_ids:
                    file_path = file_data.get('file_path', b'')
                    if isinstance(file_path, bytes):
                        file_path = file_path.decode()
                    logger.warning(f"Found zombie PENDING file: {file_id} ({file_path})")
                    if self._requeue_file(file_id, file_data):
                        counts['extraction'] += 1
                        
            # 2. Check PROCESSING files that are missing from processing sets (orphans)
            elif status == QueueStatus.PROCESSING.value:
                if str(file_id) not in all_active_ids:
                    file_path = file_data.get('file_path', b'')
                    if isinstance(file_path, bytes):
                        file_path = file_path.decode()
                    logger.warning(f"Found zombie PROCESSING file: {file_id} ({file_path})")
                    if self._requeue_file(file_id, file_data):
                        counts['extraction'] += 1
                        
        except Exception as e:
            logger.debug(f"Error checking file {file_id}: {e}")

    # Keep legacy method for backward compat but delegate to fast version
    def _check_and_recover_file(self, file_id: str, counts: Dict[str, int]) -> None:
        """Legacy: Check a single file for zombie status (builds set on demand)."""
        all_active = self._collect_all_queued_file_ids() | self._collect_all_processing_file_ids()
        self._check_and_recover_file_fast(file_id, counts, all_active)

    def _is_in_processing_set(self, file_id: str) -> bool:
        """Check if file ID exists in any processing set"""
        qm = self.queue_manager
        
        # Check extraction processing
        keys = qm._get_extraction_processing_keys()
        for key in keys:
            if qm.client.hexists(key, file_id):
                return True
                
        # Check indexing processing
        keys = qm._get_indexing_processing_keys()
        for key in keys:
             if qm.client.hexists(key, file_id):
                return True
                
        # Check OCR processing
        keys = qm._get_ocr_processing_keys()
        for key in keys:
             if qm.client.hexists(key, file_id):
                return True
                
        return False

    def _requeue_file(self, file_id: str, file_data: Dict[str, Any]) -> bool:
        """Re-add file to extraction queue"""
        try:
            # Default to extraction queue as safety net
            # If it was in indexing, it will just get re-extracted (safe)
            # If it was in OCR, it will get re-extracted (safe)
            
            qm = self.queue_manager
            
            file_path = file_data.get('file_path', '')
            try:
                file_size = int(file_data.get('file_size', 0))
                priority_val = int(file_data.get('priority', 5))
                size_cat_val = file_data.get('size_category', 'small')
            except:
                file_size = 0
                priority_val = 5
                size_cat_val = 'small'
                
            # Convert string metrics to objects if needed, or pass raw values if QM supports it
            # QM expects objects
            from core.constants import SizeCategory, Priority
            
            try:
                size_cat = SizeCategory(size_cat_val)
            except:
                size_cat = SizeCategory.SMALL
                
            try:
                priority = Priority(priority_val)
            except:
                priority = Priority.NORMAL
            
            qm.add_to_extraction_queue(
                file_id=int(file_id),
                file_path=file_path,
                file_size=file_size,
                size_category=size_cat,
                priority=priority
            )
            
            logger.info(f"Recovered file {file_id} -> Extraction Queue")
            return True
            
        except Exception as e:
            logger.error(f"Failed to requeue file {file_id}: {e}")
            return False
