"""
Enterprise Document Search System - Bloom Filter
Production-grade Bloom filter for fast duplicate detection
Thread-safe implementation for concurrent access
"""

import os
import mmh3  # MurmurHash3
from bitarray import bitarray
import math
import threading

from core.constants import BLOOM_FILTER_EXPECTED_ELEMENTS, BLOOM_FILTER_FPR
from core.logging_manager import get_logger


logger = get_logger("bloom_filter")


class BloomFilter:
    """
    Bloom filter for probabilistic membership testing
    Used for fast file hash duplicate detection
    
    Properties:
    - False positives possible (probability = FPR)
    - False negatives impossible
    - Space-efficient (bits, not full data)
    - Very fast lookups (microseconds)
    """
    
    def __init__(
        self,
        expected_elements: int = BLOOM_FILTER_EXPECTED_ELEMENTS,
        false_positive_rate: float = BLOOM_FILTER_FPR
    ):
        """
        Initialize Bloom filter
        
        Args:
            expected_elements: Number of expected elements (default: 5 million)
            false_positive_rate: Desired false positive rate (default: 0.01 = 1%)
        """
        self.expected_elements = expected_elements
        self.false_positive_rate = false_positive_rate
        
        # Thread safety lock for concurrent access
        self._lock = threading.RLock()
        
        # Calculate optimal size and number of hash functions
        self.size = self._calculate_size(expected_elements, false_positive_rate)
        self.hash_count = self._calculate_hash_count(self.size, expected_elements)
        
        # Initialize bit array
        self.bit_array = bitarray(self.size)
        self.bit_array.setall(0)
        
        # Statistics
        self.elements_added = 0
        
        logger.info(
            f"Bloom filter initialized: {self.size:,} bits "
            f"({self.size / 8 / 1024 / 1024:.2f} MB), "
            f"{self.hash_count} hash functions, "
            f"expected {expected_elements:,} elements, "
            f"FPR: {false_positive_rate}"
        )
    
    @staticmethod
    def _calculate_size(n: int, p: float) -> int:
        """
        Calculate optimal bit array size
        
        Formula: m = -(n * ln(p)) / (ln(2)^2)
        Where:
            m = bit array size
            n = expected number of elements
            p = false positive probability
        
        Args:
            n: Expected number of elements
            p: Desired false positive probability
        
        Returns:
            Optimal size in bits
        """
        m = -(n * math.log(p)) / (math.log(2) ** 2)
        return int(m)
    
    @staticmethod
    def _calculate_hash_count(m: int, n: int) -> int:
        """
        Calculate optimal number of hash functions
        
        Formula: k = (m/n) * ln(2)
        Where:
            k = number of hash functions
            m = bit array size
            n = expected number of elements
        
        Args:
            m: Bit array size
            n: Expected number of elements
        
        Returns:
            Optimal number of hash functions
        """
        k = (m / n) * math.log(2)
        return int(k)
    
    def _get_hash_positions(self, item: str) -> list[int]:
        """
        Get bit positions to set/check for item
        
        Uses double hashing technique:
            hash_i = (hash1 + i * hash2) mod m
        
        Args:
            item: Item to hash (file hash string)
        
        Returns:
            List of bit positions
        """
        # Use MurmurHash3 (very fast, good distribution)
        # FIX: Use unsigned hash for better distribution and standard compliance
        hash1 = mmh3.hash(item, seed=0, signed=False) % self.size
        hash2 = mmh3.hash(item, seed=1, signed=False) % self.size
        
        positions = []
        for i in range(self.hash_count):
            position = (hash1 + i * hash2) % self.size
            positions.append(position)
        
        return positions
    
    def add(self, item: str) -> None:
        """
        Add item to Bloom filter (thread-safe)
        
        Args:
            item: Item to add (file hash string)
        """
        positions = self._get_hash_positions(item)
        
        with self._lock:
            for position in positions:
                self.bit_array[position] = 1
            
            self.elements_added += 1
    
    def add_batch(self, items: list[str]) -> None:
        """
        Add multiple items efficiently (thread-safe)
        
        Args:
            items: List of items to add
        """
        # Pre-compute all positions outside the lock
        all_positions = [(item, self._get_hash_positions(item)) for item in items]
        
        with self._lock:
            for item, positions in all_positions:
                for position in positions:
                    self.bit_array[position] = 1
                self.elements_added += 1
    
    def contains(self, item: str) -> bool:
        """
        Check if item might be in set (probabilistic, thread-safe)
        
        Args:
            item: Item to check
        
        Returns:
            False: Definitely not in set
            True: Probably in set (with FPR probability of false positive)
        """
        positions = self._get_hash_positions(item)
        
        # All positions must be set for membership
        with self._lock:
            return all(self.bit_array[position] for position in positions)
    
    def __contains__(self, item: str) -> bool:
        """Support 'in' operator (thread-safe)"""
        return self.contains(item)

    def __len__(self) -> int:
        """Return number of elements added (for len(), thread-safe)."""
        with self._lock:
            return self.elements_added
    
    def current_fpr(self) -> float:
        """
        Calculate current false positive rate based on elements added (thread-safe)
        
        Formula: p = (1 - e^(-kn/m))^k
        Where:
            p = false positive probability
            k = number of hash functions
            n = number of elements added
            m = bit array size
        
        Returns:
            Estimated false positive rate
        """
        with self._lock:
            if self.elements_added == 0:
                return 0.0
            
            # Calculate current FPR
            k = self.hash_count
            n = self.elements_added
            m = self.size
            
            exponent = -(k * n) / m
            p = (1 - math.exp(exponent)) ** k
            
            return p
    
    def capacity_remaining(self) -> float:
        """
        Calculate remaining capacity before FPR increases significantly (thread-safe)
        
        Returns:
            Fraction of capacity remaining (0.0 to 1.0)
        """
        with self._lock:
            if self.elements_added >= self.expected_elements:
                return 0.0
            
            remaining = self.expected_elements - self.elements_added
            return remaining / self.expected_elements
    
    def get_statistics(self) -> dict:
        """
        Get Bloom filter statistics (thread-safe)
        
        Returns:
            Dictionary with statistics
        """
        with self._lock:
            return {
                'size_bits': self.size,
                'size_mb': self.size / 8 / 1024 / 1024,
                'hash_count': self.hash_count,
                'expected_elements': self.expected_elements,
                'elements_added': self.elements_added,
                'capacity_used': self.elements_added / self.expected_elements,
                'capacity_remaining': self.capacity_remaining(),
                'designed_fpr': self.false_positive_rate,
                'current_fpr': self.current_fpr(),
                'bits_set': self.bit_array.count(),
                'bits_set_percent': (self.bit_array.count() / self.size) * 100
            }
    
    def save_to_file(self, filepath: str) -> None:
        """
        Save Bloom filter to file for persistence (thread-safe)
        Uses safe bitarray.tofile() + JSON sidecar instead of pickle.
        
        Args:
            filepath: Path to save file
        """
        import json as _json
        
        with self._lock:
            # Write raw bits
            with open(filepath, 'wb') as f:
                self.bit_array.tofile(f)
            
            # Write metadata as JSON sidecar
            meta = {
                'expected_elements': self.expected_elements,
                'false_positive_rate': self.false_positive_rate,
                'size': self.size,
                'hash_count': self.hash_count,
                'elements_added': self.elements_added
            }
            meta_path = filepath + '.meta.json'
            with open(meta_path, 'w', encoding='utf-8') as f:
                _json.dump(meta, f, indent=2)
        
        logger.info(f"Bloom filter saved to {filepath} (safe format)")
    
    @classmethod
    def load_from_file(cls, filepath: str) -> 'BloomFilter':
        """
        Load Bloom filter from file (creates new instance, inherently thread-safe)
        Supports safe format (bitarray + JSON sidecar) with legacy pickle fallback.
        
        Args:
            filepath: Path to load file
        
        Returns:
            Loaded BloomFilter instance
        """
        import json as _json
        
        meta_path = filepath + '.meta.json'
        
        if os.path.exists(meta_path):
            # Safe format: bitarray + JSON sidecar
            with open(meta_path, 'r', encoding='utf-8') as f:
                meta = _json.load(f)
            
            bloom = cls(expected_elements=1, false_positive_rate=0.5)
            with bloom._lock:
                bloom.expected_elements = meta['expected_elements']
                bloom.false_positive_rate = meta['false_positive_rate']
                bloom.size = meta['size']
                bloom.hash_count = meta['hash_count']
                bloom.elements_added = meta['elements_added']
                bloom.bit_array = bitarray()
                with open(filepath, 'rb') as f:
                    bloom.bit_array.fromfile(f)
                # Trim padding bits added by tofile()
                if len(bloom.bit_array) > bloom.size:
                    del bloom.bit_array[bloom.size:]
            
            logger.info(f"Bloom filter loaded from {filepath} (safe format)")
        else:
            # Legacy pickle fallback (with security warning)
            import pickle
            logger.warning(
                f"Loading bloom filter from legacy pickle format: {filepath}. "
                f"Re-save to convert to safe format."
            )
            with open(filepath, 'rb') as f:
                state = pickle.load(f)
            
            bloom = cls(expected_elements=1, false_positive_rate=0.5)
            with bloom._lock:
                bloom.expected_elements = state.get('expected_elements', bloom.expected_elements)
                bloom.false_positive_rate = state.get('false_positive_rate', bloom.false_positive_rate)
                bloom.size = state['size']
                bloom.hash_count = state['hash_count']
                bloom.bit_array = state['bit_array']
                bloom.elements_added = state['elements_added']
        
        return bloom
    
    def populate_from_database(self, queue_manager) -> int:
        """
        Populate Bloom filter with existing file hashes from database
        
        Args:
            queue_manager: QueueManager instance (SQLite or Redis)
        
        Returns:
            Number of hashes loaded
        """
        count = 0
        
        # Check if this is a Redis queue manager
        if hasattr(queue_manager, 'client'):
            # Redis queue manager - get all file hashes from the set
            from core.redis_queue_manager import RedisQueueManager
            try:
                # Use SSCAN for memory-efficient iteration over large sets
                cursor = 0
                batch = []
                while True:
                    cursor, hashes = queue_manager.client.sscan(
                        RedisQueueManager.SET_FILE_HASHES,
                        cursor=cursor,
                        count=10000
                    )
                    for file_hash in hashes:
                        if file_hash:
                            batch.append(file_hash)
                            count += 1
                        
                        if len(batch) >= 10000:
                            self.add_batch(batch)
                            batch = []
                    
                    if cursor == 0:
                        break
                
                if batch:
                    self.add_batch(batch)
                    
            except Exception as e:
                logger.warning(f"Error populating bloom filter from Redis: {e}")
        else:
            # SQLite queue manager - use connection
            from core.queue_manager import TABLE_DISCOVERED_FILES
            
            with queue_manager._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute(f"SELECT file_hash FROM {TABLE_DISCOVERED_FILES}")
                
                batch = []
                for row in cursor:
                    if row['file_hash']:
                        batch.append(row['file_hash'])
                        count += 1
                    
                    if len(batch) >= 10000:
                        self.add_batch(batch)
                        batch = []
                
                if batch:
                    self.add_batch(batch)
        
        return count


def create_bloom_filter_from_database(queue_manager) -> BloomFilter:
    """
    Create and populate Bloom filter from existing completed files in database
    
    Args:
        queue_manager: QueueManager instance (SQLite or Redis)
    
    Returns:
        Populated BloomFilter
    """
    logger.info("Creating Bloom filter from database...")
    
    # Check if this is a Redis queue manager
    if hasattr(queue_manager, 'client'):
        # Redis queue manager
        from core.redis_queue_manager import RedisQueueManager
        
        # Count existing file hashes
        count = queue_manager.client.scard(RedisQueueManager.SET_FILE_HASHES)
        
        # Create Bloom filter sized for existing + expected new files
        total_expected = max(count + 1_000_000, BLOOM_FILTER_EXPECTED_ELEMENTS)
        bloom = BloomFilter(expected_elements=total_expected)
        
        # Load all file hashes using SSCAN for memory-efficient iteration
        cursor = 0
        batch = []
        while True:
            cursor, hashes = queue_manager.client.sscan(
                RedisQueueManager.SET_FILE_HASHES,
                cursor=cursor,
                count=10000
            )
            for file_hash in hashes:
                batch.append(file_hash)
                
                if len(batch) >= 10000:
                    bloom.add_batch(batch)
                    batch = []
            
            if cursor == 0:
                break
        
        if batch:
            bloom.add_batch(batch)
    else:
        # SQLite queue manager
        from core.queue_manager import TABLE_COMPLETED_FILES
        
        # Count existing file hashes
        with queue_manager._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"SELECT COUNT(*) as count FROM {TABLE_COMPLETED_FILES}")
            row = cursor.fetchone()
            count = row['count'] if row else 0
        
        # Create Bloom filter sized for existing + expected new files
        total_expected = max(count + 1_000_000, BLOOM_FILTER_EXPECTED_ELEMENTS)
        bloom = BloomFilter(expected_elements=total_expected)
        
        # Load all file hashes
        with queue_manager._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"SELECT file_hash FROM {TABLE_COMPLETED_FILES}")
            
            batch = []
            for row in cursor:
                batch.append(row['file_hash'])
                
                if len(batch) >= 10000:
                    bloom.add_batch(batch)
                    batch = []
            
            if batch:
                bloom.add_batch(batch)
    
    stats = bloom.get_statistics()
    logger.info(
        f"Bloom filter populated with {stats['elements_added']:,} hashes "
        f"({stats['size_mb']:.2f} MB, current FPR: {stats['current_fpr']:.6f})"
    )
    
    return bloom
