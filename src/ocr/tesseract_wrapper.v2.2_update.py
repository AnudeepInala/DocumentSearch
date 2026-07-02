"""
Tesseract Wrapper - Integration with Tesseract OCR engine
"""

import subprocess
import tempfile
import os
from pathlib import Path
from typing import Optional, Dict, Any, Tuple

from core.logging_manager import get_logger
from core.config_manager import get_config

# Configure pytesseract if imported
try:
    import pytesseract
    PYTESSERACT_AVAILABLE = True
except ImportError:
    PYTESSERACT_AVAILABLE = False

logger = get_logger("ocr.tesseract")


class TesseractWrapper:
    """Wrapper for Tesseract OCR with confidence scoring"""
    
    def __init__(self):
        self.config = get_config()
        self.tesseract_config = self.config.ocr.tesseract
        
        self.command = self.tesseract_config.command
        self.languages = '+'.join(self.tesseract_config.languages)
        self.engine_mode = self._get_engine_mode()
        self.psm = self._get_psm_mode()
        self.timeout = self.tesseract_config.timeout_seconds
        
        # Configure pytesseract if available
        if PYTESSERACT_AVAILABLE:
            pytesseract.pytesseract.tesseract_cmd = self.command
            logger.info(f"Configured pytesseract with: {self.command}")
        
        # Verify Tesseract is accessible
        self._verify_tesseract()
        
        # Statistics
        self.pages_processed = 0
        self.total_confidence = 0.0
        self.errors = 0
    
    def _verify_tesseract(self) -> None:
        """Verify Tesseract executable exists and is accessible"""
        tesseract_path = Path(self.command)
        
        if not tesseract_path.exists():
            error_msg = f"Tesseract not found at: {self.command}"
            logger.error(error_msg)
            raise FileNotFoundError(error_msg)
        
        # Try to run version command
        try:
            result = subprocess.run(
                [self.command, '--version'],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                version_line = result.stdout.split('\n')[0] if result.stdout else "Unknown"
                logger.info(f"Tesseract verified: {version_line}")
            else:
                logger.warning(f"Tesseract version check returned code {result.returncode}")
        except Exception as e:
            logger.error(f"Failed to verify Tesseract: {e}")
    
    def _get_engine_mode(self) -> str:
        """Convert engine mode to Tesseract OEM parameter"""
        mode = self.tesseract_config.engine_mode.upper()
        
        modes = {
            'LEGACY': '0',
            'LSTM': '1',
            'COMBINED': '2',
            'DEFAULT': '3'
        }
        
        return modes.get(mode, '1')  # Default to LSTM
    
    def _get_psm_mode(self) -> str:
        """Convert page segmentation mode to Tesseract PSM parameter"""
        mode = self.tesseract_config.page_segmentation_mode.upper()
        
        modes = {
            'OSD_ONLY': '0',
            'AUTO_OSD': '1',
            'AUTO': '3',
            'SINGLE_COLUMN': '4',
            'SINGLE_BLOCK_VERT_TEXT': '5',
            'SINGLE_BLOCK': '6',
            'SINGLE_LINE': '7',
            'SINGLE_WORD': '8',
            'CIRCLE_WORD': '9',
            'SINGLE_CHAR': '10',
            'SPARSE_TEXT': '11',
            'SPARSE_TEXT_OSD': '12'
        }
        
        return modes.get(mode, '3')  # Default to AUTO
    
    def extract_text(self, image_path: str) -> Optional[Tuple[str, float]]:
        """
        Extract text from image using Tesseract
        
        Args:
            image_path: Path to image file
            
        Returns:
            Tuple of (extracted_text, confidence_score) or None on error
        """
        try:
            # Create temporary output file
            with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as tmp_output:
                output_base = tmp_output.name.replace('.txt', '')
            # Use a temp file for stderr to avoid large in-memory buffers
            with tempfile.NamedTemporaryFile(mode='w', suffix='.log', delete=False) as tmp_err:
                err_path = tmp_err.name
            
            try:
                # Build Tesseract command
                cmd = [
                    self.command,
                    image_path,
                    output_base,
                    '-l', self.languages,
                    '--oem', self.engine_mode,
                    '--psm', self.psm,
                    'txt',  # Output text
                    'tsv'   # Also output TSV for confidence scores
                ]
                
                # Open stderr file for writing
                with open(err_path, 'w') as stderr_handle:
                    # Run Tesseract (avoid piping large stderr/stdout into memory)
                    result = subprocess.run(
                        cmd,
                        stdout=subprocess.DEVNULL,
                        stderr=stderr_handle,
                        text=True,
                        timeout=self.timeout
                    )
                
                # Check for memory allocation failures (std::bad_alloc)
                if result.returncode != 0:
                    stderr_text = self._read_error_snippet(err_path)
                    stderr_lower = stderr_text.lower()
                    if "bad_alloc" in stderr_lower or "out of memory" in stderr_lower:
                        logger.error(f"Tesseract ran out of memory on {image_path} - image may be too large")
                    elif "terminate" in stderr_lower:
                        logger.error(f"Tesseract crashed on {image_path}: {stderr_text[:200]}")
                    else:
                        logger.error(f"Tesseract failed: {stderr_text[:500] if stderr_text else 'Unknown error'}")
                    self.errors += 1
                    return None
                
                # Read extracted text
                text_file = output_base + '.txt'
                if os.path.exists(text_file):
                    with open(text_file, 'r', encoding='utf-8') as f:
                        text = f.read().strip()
                else:
                    text = ""
                
                # Read confidence scores from TSV
                confidence = self._calculate_confidence(output_base + '.tsv')
                
                self.pages_processed += 1
                self.total_confidence += confidence
                
                return (text, confidence)
                
            finally:
                # Cleanup temporary files
                for ext in ['.txt', '.tsv']:
                    file_path = output_base + ext
                    if os.path.exists(file_path):
                        try:
                            os.unlink(file_path)
                        except Exception:
                            pass
                if err_path and os.path.exists(err_path):
                    try:
                        os.unlink(err_path)
                    except Exception:
                        pass
            
        except subprocess.TimeoutExpired:
            logger.error(f"Tesseract timeout on {image_path}")
            self.errors += 1
            return None
            
        except Exception as e:
            logger.error(f"Error running Tesseract on {image_path}: {e}")
            self.errors += 1
            return None

    def _read_error_snippet(self, path: str, limit: int = 2000) -> str:
        """Read a limited amount of stderr output to avoid memory spikes."""
        try:
            if not path or not os.path.exists(path):
                return ""
            with open(path, 'r', encoding='utf-8', errors='ignore') as handle:
                return handle.read(limit)
        except Exception:
            return ""
    
    def _calculate_confidence(self, tsv_path: str) -> float:
        """Calculate average confidence from Tesseract TSV output"""
        try:
            if not os.path.exists(tsv_path):
                return 0.0
            
            confidences = []
            
            with open(tsv_path, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                
                # Skip header
                for line in lines[1:]:
                    parts = line.strip().split('\t')
                    if len(parts) >= 11:
                        try:
                            conf = float(parts[10])
                            if conf >= 0:  # -1 means no confidence
                                confidences.append(conf)
                        except ValueError:
                            pass
            
            if confidences:
                return sum(confidences) / len(confidences)
            else:
                return 0.0
                
        except Exception as e:
            logger.warning(f"Error calculating confidence: {e}")
            return 0.0
    
    def health_check(self) -> bool:
        """Check if Tesseract is available"""
        try:
            result = subprocess.run(
                [self.command, '--version'],
                capture_output=True,
                text=True,
                timeout=5
            )
            return result.returncode == 0
        except Exception:
            return False
    
    def get_version(self) -> Optional[str]:
        """Get Tesseract version"""
        try:
            result = subprocess.run(
                [self.command, '--version'],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                # Parse version from output
                first_line = result.stdout.split('\n')[0]
                return first_line.strip()
        except Exception:
            pass
        return None
    
    def get_stats(self) -> Dict[str, Any]:
        """Get processing statistics"""
        avg_confidence = (self.total_confidence / self.pages_processed 
                         if self.pages_processed > 0 else 0)
        
        return {
            'pages_processed': self.pages_processed,
            'average_confidence': avg_confidence,
            'errors': self.errors
        }
