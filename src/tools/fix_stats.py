
import sys
import os
import json

# Ensure we can import from src
# Adjust path to point to project root
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(os.path.dirname(current_dir))
sys.path.insert(0, project_root)

from src.core.redis_queue_manager import get_redis_queue_manager
from src.core.logging_manager import setup_logging

def fix_statistics():
    """
    Re-calculate Redis statistics scanning the actual data.
    Fixes the 'Completed' count by distinguishing Root files from Embedded files.
    """
    logger = setup_logging("fix_stats")
    logger.info("Starting statistics repair...")
    
    try:
        # Get manager and client
        qm = get_redis_queue_manager()
        r = qm.client
        
        # Reset counters
        logger.info("Resetting counters...")
        
        # We will recalculate these
        unique_file_ids = set()
        total_items = 0
        total_bytes = 0
        total_extract_ms = 0
        total_index_ms = 0
        duplicates = 0
        
        # Scan all completed files
        logger.info(f"Scanning {qm.HASH_COMPLETED}...")
        cursor = 0
        while True:
            cursor, data = r.hscan(qm.HASH_COMPLETED, cursor, count=1000)
            for file_hash, json_data in data.items():
                try:
                    data_dict = json.loads(json_data)
                    total_items += 1
                    
                    # Track unique root files
                    file_id = data_dict.get('file_id')
                    if file_id:
                        unique_file_ids.add(str(file_id))
                    
                    # Sum sizes (only for root files ideally, but we need to check if we can distinguish)
                    # Use unique_file_ids to sum size ONLY if it's the first time we see this file_id
                    # BUT, we are iterating hashes. A root file and its embedded parts might share file_id?
                    # No, embedded files usually have the SAME file_id as parent in some schemas, 
                    # but in this system, `mark_file_completed` is called with `file_id`.
                    # If embedded files share the SAME file_id, then `unique_file_ids` correctly de-dupes them.
                    # If they have different file_ids, then we are counting them as root files.
                    # Based on `RedisQueueManager` logic: `is_new_root = self.client.sadd(self.SET_COMPLETED_FILE_IDS, str(file_id))`
                    # This implies file_id IS the unique identifier for the root file.
                    
                    # So we should sum size only once per file_id.
                    # We can't easily do that in this loop without keeping a "seen" set for size too.
                    # Simplified: We will trust the stored file_size in the completion record
                    # AND assuming that we only add size for the Root file.
                    pass 

                except Exception as e:
                    logger.warning(f"Bad data in hash {file_hash}: {e}")
                    
            if cursor == 0:
                break
                
        logger.info(f"Scanned {total_items} total completion records.")
        logger.info(f"Found {len(unique_file_ids)} unique Root Files.")
        
        # Now we need to calculate size. 
        # Since we can't easily know WHICH record holds the "Root" size if there are duplicates/embedded with same ID,
        # we will iterate the SET of unique IDs and fetch metadata from HASH_FILES.
        logger.info("Recalculating total size from File Metadata...")
        calculated_size = 0
        
        # Chunked fetch of file info
        file_ids = list(unique_file_ids)
        batch_size = 100
        for i in range(0, len(file_ids), batch_size):
            batch = file_ids[i:i+batch_size]
            pipe = r.pipeline()
            for fid in batch:
                pipe.hget(f"{qm.HASH_FILES}:{fid}", "file_size")
            sizes = pipe.execute()
            
            for s in sizes:
                if s:
                    try:
                        calculated_size += int(s)
                    except:
                        pass
        
        logger.info(f"Calculated Total Root Size: {calculated_size} bytes")

        # Update Redis keys using Class Constants
        logger.info("Updating Redis Counters...")
        
        pipe = r.pipeline()
        
        # Root Counters
        pipe.set(qm.COUNTER_ROOT_COMPLETED, len(unique_file_ids))
        pipe.set(qm.COUNTER_COMPLETED_BYTES, calculated_size)
        
        # Re-populate the Set of Completed IDs
        # (Delete first to ensure it matches exactly)
        pipe.delete(qm.SET_COMPLETED_FILE_IDS)
        if unique_file_ids:
            # SADD supports multiple args
            # Split into chunks to avoid command too large
            chunk_size = 1000
            ids_list = list(unique_file_ids)
            for i in range(0, len(ids_list), chunk_size):
                chunk = ids_list[i:i+chunk_size]
                pipe.sadd(qm.SET_COMPLETED_FILE_IDS, *chunk)
        
        # Legacy/Total Counters
        pipe.set(qm.COUNTER_COMPLETED, total_items)
        # We don't verify extract/index ms here, assume previous values or reset if needed.
        # Let's verify discovered count too
        
        # Discovered Count
        discovered_count = r.hlen(qm.HASH_FILES)
        # Or scard SET_FILE_HASHES
        # pipe.set(qm.COUNTER_DISCOVERED, discovered_count) 
        # Don't overwrite discovered blindly, but good to know
        
        pipe.execute()
        
        logger.info("Successfully repaired statistics.")
        logger.info(f"  Root Completed: {len(unique_file_ids)}")
        logger.info(f"  Total Items:    {total_items}")
        logger.info(f"  Total Size:     {calculated_size}")

    except Exception as e:
        logger.error(f"Repair failed: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    fix_statistics()
