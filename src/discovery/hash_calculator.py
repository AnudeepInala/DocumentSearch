"""
Hash Calculator - Efficient file hashing with memory mapping
"""

import hashlib
import mmap
from pathlib import Path
from typing import Optional

from core.logging_manager import get_logger

logger = get_logger("discovery.hash")


class HashCalculator:
    """Calculates SHA-256 file hashes efficiently"""
    
    # Files larger than 100MB use memory mapping
    MMAP_THRESHOLD = 100 * 1024 * 1024
    
    # Read buffer size for standard hashing
    BUFFER_SIZE = 65536  # 64KB
    
    def __init__(self):
        self.files_hashed = 0
        self.bytes_hashed = 0
        self.errors = 0
    
    def calculate_hash(self, file_path: str) -> Optional[str]:
        """
        Calculate SHA-256 hash of file
        
        Args:
            file_path: Path to file
            
        Returns:
            Hex digest of SHA-256 hash, or None on error
        """
        try:
            path = Path(file_path)
            file_size = path.stat().st_size
            
            # Use memory mapping for large files
            if file_size > self.MMAP_THRESHOLD:
                hash_value = self._hash_with_mmap(path)
            else:
                hash_value = self._hash_standard(path)
            
            if hash_value:
                self.files_hashed += 1
                self.bytes_hashed += file_size
            
            return hash_value
            
        except Exception as e:
            self.errors += 1
            logger.warning(f"Error hashing file {file_path}: {e}")
            return None
    
    def _hash_standard(self, path: Path) -> Optional[str]:
        """Hash file using standard buffered reading"""
        try:
            sha256 = hashlib.sha256()
            
            with open(path, 'rb') as f:
                while True:
                    data = f.read(self.BUFFER_SIZE)
                    if not data:
                        break
                    sha256.update(data)
            
            return sha256.hexdigest()
            
        except Exception as e:
            logger.warning(f"Error in standard hash for {path}: {e}")
            return None
    
    def _hash_with_mmap(self, path: Path) -> Optional[str]:
        """Hash file using memory mapping for efficiency"""
        try:
            sha256 = hashlib.sha256()
            
            with open(path, 'rb') as f:
                # Memory-map the file
                with mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as mm:
                    # Hash in chunks
                    for i in range(0, len(mm), self.BUFFER_SIZE):
                        chunk = mm[i:i + self.BUFFER_SIZE]
                        sha256.update(chunk)
            
            return sha256.hexdigest()
            
        except Exception as e:
            logger.warning(f"Error in mmap hash for {path}: {e}")
            # Fallback to standard hashing
            return self._hash_standard(path)
    
    def get_stats(self) -> dict:
        """Get hashing statistics"""
        return {
            'files_hashed': self.files_hashed,
            'bytes_hashed': self.bytes_hashed,
            'errors': self.errors
        }
