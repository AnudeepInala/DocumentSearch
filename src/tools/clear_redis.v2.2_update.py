
import redis
import sys

def clear_redis():
    print("Attempting to clear Redis...")
    try:
        # Try localhost
        r = redis.Redis(host='localhost', port=6379, db=0)
        r.ping()
        print("Connected to localhost:6379")
        r.flushdb()
        print("Flushed localhost:6379")
        return
    except Exception as e:
        print(f"Failed localhost: {e}")

    try:
        # Try 127.0.0.1
        r = redis.Redis(host='127.0.0.1', port=6379, db=0)
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
