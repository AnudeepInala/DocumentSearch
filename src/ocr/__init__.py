"""OCR stage - PaddleOCR integration for scanned documents"""

from .image_preprocessor_advanced import ImagePreprocessor
from .paddle_wrapper import PaddleWrapper
from .ocr_worker import OCRWorker

__all__ = ['ImagePreprocessor', 'PaddleWrapper', 'OCRWorker']
