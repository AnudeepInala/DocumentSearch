import redis
import sys
import os
import glob
from pathlib import Path

# Adjust path to import core
current_dir = os.path.dirname(os.path.abspath(__file__))
src_dir = os.path.dirname(current_dir)
sys.path.insert(0, src_dir)

from core.config_manager import get_config

def clear_redis():
    print("Attempting to clear Redis and Bloom filters...")
    config = get_config()
    
    # 1. Clear Bloom filter files on disk
    bloom_dir = Path(config.paths.working_root) / "discovery"
    if bloom_dir.exists():
        pattern = str(bloom_dir / "bloom_filter_worker_*.pkl")
        files = glob.glob(pattern)
        for f in files:
            try:
                Path(f).unlink()
                print(f"Deleted Bloom filter: {f}")
            except Exception as e:
                print(f"Failed to delete {f}: {e}")
                
    # 2. Flush Redis
    try:
        # Try localhost
        r = redis.Redis(host='localhost', port=6379, db=0, protocol=2)
        r.ping()
        print("Connected to localhost:6379")
        r.flushdb()
        print("Flushed localhost:6379")
        return
    except Exception as e:
        print(f"Failed localhost: {e}")

    try:
        # Try 127.0.0.1
        r = redis.Redis(host='127.0.0.1', port=6379, db=0, protocol=2)
        r.ping()
        print("Connected to 127.0.0.1:6379")
        r.flushdb()
        print("Flushed 127.0.0.1:6379")
        return
    except Exception as e:
        print(f"Failed 127.0.0.1: {e}")
        
    print("Could not clear Redis.")
    sys.exit(1)

if __name__ == "__main__":
    clear_redis()
