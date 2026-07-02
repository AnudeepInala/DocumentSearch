
import os
import redis
import time

def verify():
    try:
        r = redis.Redis(host='localhost', port=6379, db=0, decode_responses=True)
        prefix = f"{os.getenv('USERNAME') or os.getenv('USER') or 'default'}:docsearch:"

        print(f"{'Time':<10} | {'Root':<10} | {'Total':<10} | {'Discovered':<10}")
        print("-" * 50)

        for _ in range(5):
            root = r.get(f"{prefix}counter:root_completed") or 0
            total = r.get(f"{prefix}counter:completed") or 0
            discovered = r.get(f"{prefix}counter:discovered") or 0
            
            print(f"{time.strftime('%H:%M:%S'):<10} | {root:<10} | {total:<10} | {discovered:<10}")
            time.sleep(2)
            
    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    verify()
