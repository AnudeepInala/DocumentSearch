"""
Image Preprocessor - OpenCV-based image preprocessing for better OCR
"""

import cv2
import numpy as np
from PIL import Image
from typing import Optional
import io

from core.logging_manager import get_logger
from core.config_manager import get_config

logger = get_logger("ocr.preprocessor")


class ImagePreprocessor:
    """Preprocesses images to improve OCR accuracy"""
    
    def __init__(self):
        self.config = get_config()
        self.preprocessing_config = self.config.ocr.preprocessing
        
        self.target_dpi = self.preprocessing_config.get('target_dpi', 300)
        self.use_opencv = self.preprocessing_config.get('use_opencv', True)
        self.use_pillow = self.preprocessing_config.get('use_pillow', True)
    
    def preprocess(self, image_data: bytes) -> Optional[bytes]:
        """
        Preprocess image for OCR
        
        Args:
            image_data: Raw image bytes
            
        Returns:
            Preprocessed image bytes or None on error
        """
        try:
            # Check image size to prevent memory issues
            # Skip preprocessing for very large images (> 50MB uncompressed estimate)
            max_raw_size = 50 * 1024 * 1024  # 50MB
            if len(image_data) > max_raw_size:
                logger.warning(f"Image too large ({len(image_data) / 1024 / 1024:.1f}MB), skipping preprocessing")
                return image_data
            
            if self.use_opencv:
                return self._preprocess_opencv(image_data)
            elif self.use_pillow:
                return self._preprocess_pillow(image_data)
            else:
                return image_data
                
        except Exception as e:
            logger.error(f"Error preprocessing image: {e}")
            return None
    
    def _preprocess_opencv(self, image_data: bytes) -> Optional[bytes]:
        """Preprocess using OpenCV"""
        try:
            # Convert bytes to numpy array
            nparr = np.frombuffer(image_data, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            
            if img is None:
                logger.warning("Failed to decode image with OpenCV")
                return image_data
            
            # Check image dimensions - downscale if too large to prevent memory issues
            # Images larger than 6000x6000 pixels (36MP) can cause Tesseract memory issues
            max_dimension = 6000
            height, width = img.shape[:2]
            if height > max_dimension or width > max_dimension:
                scale = min(max_dimension / height, max_dimension / width)
                new_width = int(width * scale)
                new_height = int(height * scale)
                logger.info(f"Downscaling large image from {width}x{height} to {new_width}x{new_height}")
                img = cv2.resize(img, (new_width, new_height), interpolation=cv2.INTER_AREA)
            
            # Track if we're in color or grayscale mode
            is_color = len(img.shape) == 3
            
            # Noise reduction (BEFORE grayscale conversion for better quality)
            if self.preprocessing_config.get('apply_noise_reduction', True):
                if is_color:
                    img = cv2.fastNlMeansDenoisingColored(img, None, 10, 10, 7, 21)
                else:
                    img = cv2.fastNlMeansDenoising(img, None, 10, 7, 21)
            
            # Enhance contrast (BEFORE grayscale for better quality)
            if self.preprocessing_config.get('enhance_contrast', True):
                if is_color:
                    lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
                    l, a, b = cv2.split(lab)
                    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
                    cl = clahe.apply(l)
                    limg = cv2.merge((cl, a, b))
                    img = cv2.cvtColor(limg, cv2.COLOR_LAB2BGR)
                else:
                    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
                    img = clahe.apply(img)
            
            # Convert to grayscale AFTER noise reduction and contrast enhancement
            if self.preprocessing_config.get('convert_to_grayscale', True):
                if len(img.shape) == 3:
                    img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            
            # Correct skew (works on grayscale)
            if self.preprocessing_config.get('correct_skew', True):
                img = self._correct_skew(img)
            
            # Binarization (Otsu's thresholding) - must be grayscale
            if self.preprocessing_config.get('binarize', True):
                # Ensure grayscale for binarization
                if len(img.shape) == 3:
                    img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                
                _, img = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            
            # Encode back to bytes
            _, buffer = cv2.imencode('.png', img)
            return buffer.tobytes()
            
        except Exception as e:
            logger.error(f"Error in OpenCV preprocessing: {e}", exc_info=True)
            return image_data
    
    def _correct_skew(self, image: np.ndarray) -> np.ndarray:
        """Detect and correct skew in image"""
        try:
            # Convert to grayscale if needed
            if len(image.shape) == 3:
                gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            else:
                gray = image

            # Detect edges
            edges = cv2.Canny(gray, 50, 150, apertureSize=3)
            
            # Detect lines using Hough transform
            lines = cv2.HoughLines(edges, 1, np.pi / 180, 200)
            
            if lines is None:
                return image
            
            # Calculate angles
            angles = []
            for rho, theta in lines[:, 0]:
                angle = np.degrees(theta) - 90
                angles.append(angle)
            
            if not angles:
                return image
            
            # Get median angle
            median_angle = np.median(angles)
            
            # Rotate image if skew detected (more than 0.5 degrees)
            if abs(median_angle) > 0.5:
                (h, w) = image.shape[:2]
                center = (w // 2, h // 2)
                M = cv2.getRotationMatrix2D(center, median_angle, 1.0)
                rotated = cv2.warpAffine(image, M, (w, h),
                                        flags=cv2.INTER_CUBIC,
                                        borderMode=cv2.BORDER_REPLICATE)
                return rotated
            
            return image
            
        except Exception as e:
            logger.warning(f"Skew correction failed: {e}")
            return image
            
        except Exception:
            return image
    
    def _preprocess_pillow(self, image_data: bytes) -> Optional[bytes]:
        """Preprocess using PIL/Pillow (simpler fallback)"""
        try:
            img = Image.open(io.BytesIO(image_data))
            
            # Convert to grayscale
            if self.preprocessing_config.get('convert_to_grayscale', True):
                img = img.convert('L')
            
            # Enhance contrast
            if self.preprocessing_config.get('enhance_contrast', True):
                from PIL import ImageEnhance
                enhancer = ImageEnhance.Contrast(img)
                img = enhancer.enhance(2.0)
            
            # Save to bytes
            output = io.BytesIO()
            img.save(output, format='PNG')
            return output.getvalue()
            
        except Exception as e:
            logger.error(f"Error in Pillow preprocessing: {e}")
            return image_data
