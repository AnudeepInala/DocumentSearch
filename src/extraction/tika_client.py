"""
Tika Client - HTTP client for Apache Tika with connection pooling and retry logic
"""

import os
import mimetypes
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from typing import Optional, Dict, Any
import time

# Explicit MIME map for formats that mimetypes may misidentify or miss entirely.
# Sending the correct Content-Type lets Tika skip detection and use the right
# parser immediately — critical for multi-font documents where auto-detection fails.
_MIME_MAP = {
    '.pdf':  'application/pdf',
    '.docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    '.xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    '.pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
    '.doc':  'application/msword',
    '.xls':  'application/vnd.ms-excel',
    '.ppt':  'application/vnd.ms-powerpoint',
    '.rtf':  'application/rtf',
    '.odt':  'application/vnd.oasis.opendocument.text',
    '.ods':  'application/vnd.oasis.opendocument.spreadsheet',
    '.odp':  'application/vnd.oasis.opendocument.presentation',
    '.html': 'text/html',
    '.htm':  'text/html',
    '.txt':  'text/plain; charset=UTF-8',
    '.msg':  'application/vnd.ms-outlook',
    '.eml':  'message/rfc822',
    '.zip':  'application/zip',
    '.csv':  'text/csv',
    '.xml':  'application/xml',
    '.json': 'application/json',
}

from core.logging_manager import get_logger
from core.config_manager import get_config

logger = get_logger("extraction.tika")


class TikaClient:
    """HTTP client for Apache Tika with robust error handling"""
    
    def __init__(self, tika_host: str, tika_port: int):
        self.base_url = f"http://{tika_host}:{tika_port}"
        self.config = get_config()
        
        # Get Tika configuration
        tika_config = self.config.extraction.tika
        self.timeout = tika_config.timeout_seconds
        self.max_retries = tika_config.max_retries
        self.retry_backoff = tika_config.retry_backoff_seconds
        
        # Create session with connection pooling
        self.session = self._create_session(tika_config.connection_pool_size)
        
        # Statistics
        self.requests_sent = 0
        self.requests_failed = 0
        self.total_bytes_sent = 0
        self.total_response_time = 0
    
    def _content_type_for(self, file_path: str) -> str:
        """Return the MIME type for file_path, preferring our explicit map."""
        ext = os.path.splitext(file_path)[1].lower()
        if ext in _MIME_MAP:
            return _MIME_MAP[ext]
        guessed, _ = mimetypes.guess_type(file_path)
        return guessed or 'application/octet-stream'

    def _create_session(self, pool_size: int) -> requests.Session:
        """Create requests session with connection pooling and retry strategy"""
        session = requests.Session()
        
        # Configure retry strategy
        retry_strategy = Retry(
            total=0,  # We handle retries manually
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            raise_on_status=False
        )
        
        # Configure connection pooling
        adapter = HTTPAdapter(
            pool_connections=pool_size,
            pool_maxsize=pool_size,
            max_retries=retry_strategy
        )
        
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        
        return session
    
    def unpack(self, file_path: str) -> Optional[bytes]:
        """
        Unpack embedded files from file using Tika's /unpack/all endpoint
        
        Args:
            file_path: Path to file to unpack
            
        Returns:
            ZIP archive bytes containing all embedded files or None on failure
        """
        url = f"{self.base_url}/unpack/all"
        attempt = 0
        last_error = None
        
        content_type = self._content_type_for(file_path)
        while attempt <= self.max_retries:
            try:
                with open(file_path, 'rb') as f:
                    response = self.session.put(
                        url,
                        data=f,
                        headers={
                            'Accept': 'application/zip',
                            'Content-Type': content_type,
                        },
                        timeout=self.timeout
                    )
                if response.status_code == 200:
                    return response.content
                elif response.status_code == 204:
                    return None  # No embedded files
                else:
                    last_error = f"HTTP {response.status_code}"
            except Exception as e:
                last_error = str(e)
            
            attempt += 1
            if attempt <= self.max_retries:
                wait_time = self.retry_backoff[attempt - 1] if (attempt - 1) < len(self.retry_backoff) else self.retry_backoff[-1]
                time.sleep(wait_time)
        
        logger.warning(f"Failed to unpack {file_path} via Tika: {last_error}")
        return None

    def extract(self, file_path: str) -> Optional[Dict[str, Any]]:
        """
        Extract content from file using Tika
        
        Args:
            file_path: Path to file to extract
            
        Returns:
            Dictionary with extraction results or None on failure
        """
        url = f"{self.base_url}/rmeta/text"
        
        attempt = 0
        last_error = None
        
        # Get file size before starting retries
        try:
            file_size = os.path.getsize(file_path)
        except OSError:
            file_size = 0
        
        content_type = self._content_type_for(file_path)
        while attempt <= self.max_retries:
            try:
                start_time = time.time()

                # Stream file to Tika to avoid loading into memory
                with open(file_path, 'rb') as f:
                    response = self.session.put(
                        url,
                        data=f,
                        headers={
                            'Accept': 'application/json',
                            'Content-Type': content_type,
                            'X-Tika-OCRLanguage': 'eng',
                        },
                        timeout=self.timeout
                    )
                
                elapsed = time.time() - start_time
                self.total_response_time += elapsed
                self.requests_sent += 1
                self.total_bytes_sent += file_size
                
                # Check response
                if response.status_code == 200:
                    result = response.json()
                    
                    # Tika returns a list of documents (main + embedded)
                    if isinstance(result, list) and len(result) > 0:
                        return {
                            'success': True,
                            'documents': result,
                            'response_time_ms': int(elapsed * 1000),
                            'file_size': file_size
                        }
                    else:
                        logger.warning(f"Empty response from Tika for {file_path}")
                        return None
                
                elif response.status_code == 422:
                    # Unsupported file type
                    logger.debug(f"Unsupported file type: {file_path}")
                    return None
                
                elif response.status_code >= 500:
                    # Server error - retry
                    last_error = f"HTTP {response.status_code}"
                    if attempt < self.max_retries:
                        wait_time = self.retry_backoff[attempt] if attempt < len(self.retry_backoff) else self.retry_backoff[-1]
                        logger.warning(f"Tika server error {response.status_code}, retrying in {wait_time}s...")
                        time.sleep(wait_time)
                        attempt += 1
                        continue
                
                else:
                    logger.error(f"Tika extraction failed with status {response.status_code} for {file_path}")
                    return None
                
            except requests.exceptions.Timeout:
                last_error = "Timeout"
                logger.warning(f"Timeout extracting {file_path} (attempt {attempt + 1}/{self.max_retries + 1})")
                if attempt < self.max_retries:
                    wait_time = self.retry_backoff[attempt] if attempt < len(self.retry_backoff) else self.retry_backoff[-1]
                    time.sleep(wait_time)
                    attempt += 1
                    continue
                break
                
            except Exception as e:
                last_error = str(e)
                logger.error(f"Error extracting {file_path}: {e}")
                if attempt < self.max_retries:
                    wait_time = self.retry_backoff[attempt] if attempt < len(self.retry_backoff) else self.retry_backoff[-1]
                    time.sleep(wait_time)
                    attempt += 1
                    continue
                break
        
        # All retries failed
        self.requests_failed += 1
        logger.error(f"Failed to extract {file_path} after {attempt} attempts: {last_error}")
        return None
    
    def health_check(self) -> bool:
        """Check if Tika server is healthy"""
        try:
            response = self.session.get(f"{self.base_url}/tika", timeout=5)
            return response.status_code == 200
        except Exception:
            return False
    
    def get_version(self) -> Optional[str]:
        """Get Tika server version"""
        try:
            response = self.session.get(f"{self.base_url}/version", timeout=5)
            if response.status_code == 200:
                return response.text.strip()
        except Exception:
            pass
        return None
    
    def get_stats(self) -> Dict[str, Any]:
        """Get client statistics"""
        avg_response_time = (self.total_response_time / self.requests_sent 
                            if self.requests_sent > 0 else 0)
        
        return {
            'requests_sent': self.requests_sent,
            'requests_failed': self.requests_failed,
            'total_bytes_sent': self.total_bytes_sent,
            'average_response_time_ms': int(avg_response_time * 1000),
            'success_rate': (self.requests_sent - self.requests_failed) / self.requests_sent 
                           if self.requests_sent > 0 else 0
        }
    
    def close(self) -> None:
        """Close session and cleanup"""
        if self.session:
            self.session.close()
    
    def __enter__(self):
        """Context manager entry"""
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - ensure session is closed"""
        self.close()
        return False
    
    def __del__(self):
        """Destructor - cleanup session on garbage collection"""
        try:
            self.close()
        except Exception:
            pass
