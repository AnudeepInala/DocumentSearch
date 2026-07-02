"""
Image Preprocessor - Advanced OpenCV-based preprocessing for better OCR
Based on image_processor.py reference with mode-based and adaptive processing
"""

import cv2
import numpy as np
from PIL import Image
from typing import Optional, Tuple
from enum import Enum
from dataclasses import dataclass
import io

from core.logging_manager import get_logger
from core.config_manager import get_config

logger = get_logger("ocr.preprocessor")


class EnhancementLevel(Enum):
    """Enhancement intensity levels"""
    LIGHT = "light"
    BALANCED = "balanced"
    AGGRESSIVE = "aggressive"
    EXTREME_RESTORE = "extreme_restore"


@dataclass
class PreprocessingConfig:
    """Configuration for preprocessing"""
    apply_clahe: bool = True
    clahe_clip_limit: float = 2.0
    clahe_grid_size: tuple = (8, 8)
    
    apply_denoise: bool = False
    denoise_strength: int = 10
    
    apply_deskew: bool = False
    
    apply_sharpen: bool = False
    sharpen_amount: float = 1.0
    
    brightness_adjust: float = 0.0


class ImagePreprocessor:
    """
    Advanced image preprocessor for OCR
    
    Features:
    - Mode-based preprocessing (LIGHT, BALANCED, AGGRESSIVE, EXTREME_RESTORE)
    - Dynamic adjustments based on image analysis
    - CLAHE contrast enhancement
    - Gamma correction
    - Denoising
    - Sharpening
    - Skew correction
    - Faded text enhancement
    - Colored background removal
    """
    
    def __init__(self):
        self.config = get_config()
        self.preprocessing_config = self.config.ocr.preprocessing
        
        self.target_dpi = self.preprocessing_config.get('target_dpi', 300)
        self.use_opencv = self.preprocessing_config.get('use_opencv', True)
        self.use_pillow = self.preprocessing_config.get('use_pillow', True)
        
        self._has_opencv_contrib = self._check_opencv_contrib()
        
        logger.info(f"ImagePreprocessor initialized (OpenCV contrib: {self._has_opencv_contrib})")
    
    def _check_opencv_contrib(self) -> bool:
        """Check if OpenCV contrib modules are available"""
        try:
            test_img = np.zeros((10, 10), dtype=np.uint8)
            _ = cv2.ximgproc.createFastBilateralSolverFilter(test_img, 1, 1, 1)
            return True
        except (AttributeError, cv2.error):
            return False
    
    def preprocess(self, image_data: bytes, quality_score: float = None) -> Optional[bytes]:
        """
        Preprocess image for OCR with adaptive enhancement
        
        Args:
            image_data: Raw image bytes
            quality_score: Optional quality score (0-1) for adaptive processing
            
        Returns:
            Preprocessed image bytes or None on error
        """
        try:
            # Check image size to prevent memory issues
            max_raw_size = 50 * 1024 * 1024  # 50MB
            if len(image_data) > max_raw_size:
                logger.warning(f"Image too large ({len(image_data) / 1024 / 1024:.1f}MB), skipping preprocessing")
                return image_data
            
            if self.use_opencv:
                return self._preprocess_opencv_advanced(image_data, quality_score)
            elif self.use_pillow:
                return self._preprocess_pillow(image_data)
            else:
                return image_data
                
        except Exception as e:
            logger.error(f"Error preprocessing image: {e}")
            return None

    def resize_image(self, image_data: bytes, scale: float = 2.0) -> bytes:
        """Resize image by a scale factor (upstream helper)"""
        try:
            nparr = np.frombuffer(image_data, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            if img is None: return image_data
            
            new_width = int(img.shape[1] * scale)
            new_height = int(img.shape[0] * scale)
            
            resized = cv2.resize(img, (new_width, new_height), interpolation=cv2.INTER_CUBIC)
            
            _, buffer = cv2.imencode('.png', resized)
            return buffer.tobytes()
        except Exception as e:
            logger.warning(f"Resize failed: {e}")
            return image_data

    def rotate_image(self, image_data: bytes, angle: int) -> bytes:
        """Rotate image by fixed angle (90, 180, 270)"""
        try:
            nparr = np.frombuffer(image_data, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            if img is None: return image_data
            
            if angle == 90:
                rotated = cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
            elif angle == 180:
                rotated = cv2.rotate(img, cv2.ROTATE_180)
            elif angle == 270:
                rotated = cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
            else:
                return image_data
                
            _, buffer = cv2.imencode('.png', rotated)
            return buffer.tobytes()
        except Exception as e:
            logger.warning(f"Rotation failed: {e}")
            return image_data

    def apply_binarization(self, image_data: bytes) -> bytes:
        """Apply adaptive binarization (upstream helper)"""
        try:
            nparr = np.frombuffer(image_data, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            if img is None: return image_data
            
            if len(img.shape) == 3:
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            else:
                gray = img
            
            # Median blur to remove noise
            blurred = cv2.medianBlur(gray, 3)
            
            # Adaptive thresholding
            binary = cv2.adaptiveThreshold(
                blurred, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, 
                cv2.THRESH_BINARY, 11, 2
            )
            
            _, buffer = cv2.imencode('.png', binary)
            return buffer.tobytes()
        except Exception as e:
            logger.warning(f"Binarization failed: {e}")
            return image_data

    def apply_clahe_only(self, image_data: bytes) -> bytes:
        """Apply only CLAHE contrast enhancement (upstream helper)"""
        try:
            nparr = np.frombuffer(image_data, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            if img is None: return image_data
            
            enhanced = self._apply_clahe(img, clip_limit=3.0, grid_size=(8,8))
            
            _, buffer = cv2.imencode('.png', enhanced)
            return buffer.tobytes()
        except Exception as e:
            logger.warning(f"CLAHE-only failed: {e}")
            return image_data
    
    def _preprocess_opencv_advanced(self, image_data: bytes, quality_score: float = None) -> Optional[bytes]:
        """
        Advanced preprocessing using OpenCV with adaptive enhancements
        """
        try:
            # Convert bytes to numpy array
            nparr = np.frombuffer(image_data, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            
            if img is None:
                logger.warning("Failed to decode image with OpenCV")
                return image_data
            
            # Check image dimensions - downscale if too large
            max_dimension = 6000
            height, width = img.shape[:2]
            if height > max_dimension or width > max_dimension:
                scale = min(max_dimension / height, max_dimension / width)
                new_width = int(width * scale)
                new_height = int(height * scale)
                logger.info(f"Downscaling large image from {width}x{height} to {new_width}x{new_height}")
                img = cv2.resize(img, (new_width, new_height), interpolation=cv2.INTER_AREA)
            
            # Step 1: Correct orientation (Must be first)
            # This requires pytesseract and may be slow, so we can make it configurable or only apply if confidence is low?
            # For "ideal" processing, we apply it.
            if self.preprocessing_config.get('correct_orientation', True):
                img = self._correct_orientation(img)
            
            # Step 2: Handle inverted text (White on Black)
            if self.preprocessing_config.get('handle_inverted', True):
                img = self._handle_inverted_text(img)

            # Step 3: Perspective Correction (Dewarping)
            if self.preprocessing_config.get('correct_perspective', True):
                img = self._correct_perspective(img)

            # Analyze image to determine best processing mode (Refreshed after geometry fixes)
            brightness, contrast, sharpness = self._analyze_image(img)
            
            # Select enhancement mode based on analysis
            mode = self._select_mode(brightness, contrast, sharpness, quality_score)
            logger.debug(f"Selected preprocessing mode: {mode} (brightness={brightness:.1f}, contrast={contrast:.1f}, sharpness={sharpness:.1f})")
            
            # Get config for mode
            config = self._get_config_for_mode(mode)
            
            # Apply dynamic adjustments
            config = self._adjust_config_dynamic(config, brightness, contrast, sharpness, quality_score)
            gamma = self._select_gamma(brightness)
            
            # Check for special conditions
            has_color = self._detect_colored_regions(img)
            has_faded = self._detect_faded_regions(img)
            
            # Step 4: Correct skew (Fine tuning)
            if self.preprocessing_config.get('correct_skew', True):
                img = self._correct_skew(img)

            # Step 5: Remove Shadows / Illumination Correction
            if self.preprocessing_config.get('remove_shadows', True) or mode in ["AGGRESSIVE", "EXTREME_RESTORE"]:
                img = self._remove_shadows(img)

            # Step 6: Remove Borders
            if self.preprocessing_config.get('remove_borders', True):
                img = self._remove_borders(img)

            # Step 7: Color background removal
            if has_color:
                img = self._remove_color_background(img)
            
            # Step 7.5: Normalize background
            if self.preprocessing_config.get('normalize_background', True):
                img = self._normalize_background(img)
            
            # Step 8: Denoise
            if config.apply_denoise:
                img = self._apply_denoise(img, config.denoise_strength)
            
            # Step 9: Gamma correction
            if gamma != 1.0:
                img = self._apply_gamma(img, gamma)
            
            # Step 10: Brightness adjustment
            if config.brightness_adjust != 0:
                img = self._apply_brightness(img, config.brightness_adjust)
            
            # Step 11: CLAHE contrast enhancement
            if config.apply_clahe:
                img = self._apply_clahe(img, config.clahe_clip_limit, config.clahe_grid_size)
            
            # Step 12: Sharpen
            if config.apply_sharpen:
                img = self._apply_sharpen(img, config.sharpen_amount)
            
            # Step 13: Faded text enhancement — skip for high-brightness images.
            # Bright images (background pixels in 150-220 range) trigger false
            # faded-detection; adaptive thresholding on them binarizes and corrupts text.
            binarize_max = self.preprocessing_config.get('binarize_max_brightness', 150)
            if has_faded and brightness < binarize_max:
                img = self._enhance_faded_text(img)

            # Step 14: Repair Broken Text (New)
            if (mode == "EXTREME_RESTORE" or has_faded) and brightness < binarize_max:
                img = self._repair_broken_text(img)
            
            # Step 15: Convert to grayscale for OCR
            if self.preprocessing_config.get('convert_to_grayscale', True):
                if len(img.shape) == 3:
                    img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            
            # Step 16: Binarization (optional) — skipped for high-brightness images.
            # Sauvola on bright images (mean >= binarize_max_brightness) fragments characters,
            # producing noise artifacts that VL16 reads as colons / dropped letters.
            if self.preprocessing_config.get('binarize', False):
                binarize_max = self.preprocessing_config.get('binarize_max_brightness', 150)
                if brightness < binarize_max:
                    if self.preprocessing_config.get('use_sauvola', True):
                        img = self._apply_sauvola_threshold(img)
                    else:
                        if len(img.shape) == 3:
                            img = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                        _, img = cv2.threshold(img, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
                else:
                    logger.debug(f"Skipping binarization: brightness={brightness:.1f} >= {binarize_max}")
            
            # Encode back to bytes
            _, buffer = cv2.imencode('.png', img)
            return buffer.tobytes()
            
        except Exception as e:
            logger.error(f"Error in OpenCV preprocessing: {e}", exc_info=True)
            return image_data
    
    def _analyze_image(self, img: np.ndarray) -> Tuple[float, float, float]:
        """Analyze image to get brightness, contrast, and sharpness metrics"""
        try:
            if len(img.shape) == 3:
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            else:
                gray = img
            
            brightness = float(np.mean(gray))
            contrast = float(np.std(gray))
            sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
            
            return brightness, contrast, sharpness
        except Exception as e:
            logger.warning(f"Image analysis failed: {e}")
            return 128.0, 50.0, 200.0  # Default values
    
    def _select_mode(self, brightness: float, contrast: float, sharpness: float, quality_score: float = None) -> str:
        """Select enhancement mode based on image analysis"""
        
        # Use quality score if available
        if quality_score is not None:
            if quality_score < 0.3:
                return "EXTREME_RESTORE"
            elif quality_score < 0.5:
                return "AGGRESSIVE"
            elif quality_score < 0.7:
                return "BALANCED"
            else:
                return "LIGHT"
        
        # Otherwise use image metrics
        # Low contrast images need more enhancement
        if contrast < 25:
            return "EXTREME_RESTORE"
        elif contrast < 40:
            return "AGGRESSIVE"
        
        # Low sharpness images need sharpening
        if sharpness < 80:
            return "AGGRESSIVE"
        elif sharpness < 150:
            return "BALANCED"
        
        # Very dark or very bright images
        if brightness < 60 or brightness > 200:
            return "AGGRESSIVE"
        
        # Normal images
        if brightness < 100 or brightness > 160:
            return "BALANCED"
        
        return "LIGHT"
    
    def _get_config_for_mode(self, mode: str) -> PreprocessingConfig:
        """Get preprocessing config for the given mode"""
        mode = mode.upper()
        
        if mode == "LIGHT":
            return PreprocessingConfig(
                apply_clahe=True,
                clahe_clip_limit=1.5,
                clahe_grid_size=(8, 8),
                apply_denoise=False,
                apply_sharpen=False,
                brightness_adjust=0
            )
        
        elif mode == "BALANCED":
            return PreprocessingConfig(
                apply_clahe=True,
                clahe_clip_limit=2.0,
                clahe_grid_size=(8, 8),
                apply_denoise=True,
                denoise_strength=5,
                apply_sharpen=False,
                brightness_adjust=0
            )
        
        elif mode == "AGGRESSIVE":
            return PreprocessingConfig(
                apply_clahe=True,
                clahe_clip_limit=3.0,
                clahe_grid_size=(4, 4),
                apply_denoise=True,
                denoise_strength=10,
                apply_sharpen=True,
                sharpen_amount=1.2,
                brightness_adjust=10
            )
        
        elif mode == "EXTREME_RESTORE":
            return PreprocessingConfig(
                apply_clahe=True,
                clahe_clip_limit=5.0,
                clahe_grid_size=(4, 4),
                apply_denoise=True,
                denoise_strength=15,
                apply_sharpen=True,
                sharpen_amount=1.5,
                brightness_adjust=20
            )
        
        else:
            logger.warning(f"Unknown mode '{mode}', using BALANCED")
            return self._get_config_for_mode("BALANCED")
    
    def _adjust_config_dynamic(
        self,
        config: PreprocessingConfig,
        brightness: float,
        contrast: float,
        sharpness: float,
        quality_score: Optional[float]
    ) -> PreprocessingConfig:
        """Adjust preprocessing config based on measured image quality"""
        
        # Contrast-driven CLAHE
        if contrast < 25:
            config.apply_clahe = True
            config.clahe_clip_limit = min(5.0, config.clahe_clip_limit + 1.5)
            config.clahe_grid_size = (6, 6)
        elif contrast < 40:
            config.apply_clahe = True
            config.clahe_clip_limit = min(4.0, config.clahe_clip_limit + 0.8)
        else:
            config.clahe_clip_limit = max(1.2, config.clahe_clip_limit - 0.3)
        
        # Sharpness-driven sharpening/denoise balance
        if sharpness < 80:
            config.apply_sharpen = True
            config.sharpen_amount = max(config.sharpen_amount, 1.6)
            config.denoise_strength = min(config.denoise_strength, 6)
        elif sharpness < 150:
            config.apply_sharpen = True
            config.sharpen_amount = max(config.sharpen_amount, 1.3)
        
        # Quality score nudges
        if quality_score is not None and quality_score < 0.50:
            config.apply_denoise = True
            config.denoise_strength = max(config.denoise_strength, 8)
            config.apply_sharpen = True
            config.sharpen_amount = max(config.sharpen_amount, 1.5)
        
        return config
    
    def _select_gamma(self, brightness: float) -> float:
        """Select gamma correction factor based on brightness"""
        if brightness < 80:
            return 0.85  # Brighten dark images
        if brightness > 180:
            return 1.15  # Darken bright images
        return 1.0
    
    def _apply_denoise(self, img: np.ndarray, strength: int) -> np.ndarray:
        """Apply denoising with bilateral filter"""
        try:
            return cv2.bilateralFilter(img, d=9, sigmaColor=strength*5, sigmaSpace=strength*5)
        except Exception as e:
            logger.warning(f"Denoising failed: {e}")
            return img
    
    def _apply_brightness(self, img: np.ndarray, adjust: float) -> np.ndarray:
        """Adjust brightness"""
        try:
            hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV).astype(np.float32)
            hsv[:, :, 2] = np.clip(hsv[:, :, 2] + adjust, 0, 255)
            hsv = hsv.astype(np.uint8)
            return cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)
        except Exception as e:
            logger.warning(f"Brightness adjustment failed: {e}")
            return img
    
    def _apply_clahe(self, img: np.ndarray, clip_limit: float, grid_size: tuple) -> np.ndarray:
        """Apply CLAHE contrast enhancement"""
        try:
            if len(img.shape) == 3:
                lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
                l, a, b = cv2.split(lab)
                clahe = cv2.createCLAHE(clipLimit=clip_limit, tileGridSize=grid_size)
                l = clahe.apply(l)
                lab = cv2.merge([l, a, b])
                return cv2.cvtColor(lab, cv2.COLOR_LAB2BGR)
            else:
                clahe = cv2.createCLAHE(clipLimit=clip_limit, tileGridSize=grid_size)
                return clahe.apply(img)
        except Exception as e:
            logger.warning(f"CLAHE failed: {e}")
            return img
    
    def _apply_sharpen(self, img: np.ndarray, amount: float) -> np.ndarray:
        """Apply unsharp masking"""
        try:
            gaussian = cv2.GaussianBlur(img, (0, 0), 2.0)
            sharpened = cv2.addWeighted(img, 1 + amount, gaussian, -amount, 0)
            return np.clip(sharpened, 0, 255).astype(np.uint8)
        except Exception as e:
            logger.warning(f"Sharpening failed: {e}")
            return img
    
    def _apply_gamma(self, img: np.ndarray, gamma: float) -> np.ndarray:
        """Apply gamma correction"""
        try:
            inv_gamma = 1.0 / max(gamma, 0.01)
            table = (np.arange(256) / 255.0) ** inv_gamma * 255.0
            table = table.astype("uint8")
            return cv2.LUT(img, table)
        except Exception as e:
            logger.warning(f"Gamma correction failed: {e}")
            return img
    
    def _detect_faded_regions(self, img: np.ndarray, threshold: float = 0.3) -> bool:
        """Detect if image has faded text regions"""
        try:
            if len(img.shape) == 3:
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            else:
                gray = img
            
            laplacian_var = cv2.Laplacian(gray, cv2.CV_64F).var()
            
            hist = cv2.calcHist([gray], [0], None, [256], [0, 256]).flatten()
            total_pixels = gray.size
            
            # Count pixels in faded gray range (150-220)
            faded_pixels = np.sum(hist[150:220])
            faded_ratio = faded_pixels / total_pixels
            
            is_faded = (faded_ratio > threshold) or (laplacian_var < 100)
            
            if is_faded:
                logger.debug(f"Faded text detected: faded_ratio={faded_ratio:.2f}, laplacian_var={laplacian_var:.1f}")
            
            return is_faded
            
        except Exception as e:
            logger.warning(f"Faded detection failed: {e}")
            return False
    
    def _detect_colored_regions(self, img: np.ndarray) -> bool:
        """Detect if image has colored regions (like blue table cells)"""
        try:
            if len(img.shape) != 3:
                return False
            
            hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
            saturation = hsv[:, :, 1]
            
            colored_pixels = np.sum(saturation > 30)
            total_pixels = saturation.size
            colored_ratio = colored_pixels / total_pixels
            
            has_color = colored_ratio > 0.05
            
            if has_color:
                logger.debug(f"Colored regions detected: {colored_ratio:.1%} of image")
            
            return has_color
            
        except Exception as e:
            logger.warning(f"Color detection failed: {e}")
            return False
    
    def _remove_color_background(self, img: np.ndarray) -> np.ndarray:
        """Remove colored backgrounds (any color) to improve OCR on graphical images.
        
        Handles: blue, yellow, green, red, orange, pink, purple, dark backgrounds.
        This is critical for extracting text from screenshots, presentations,
        diagrams, and colored table cells.
        """
        try:
            if len(img.shape) != 3:
                return img
            
            hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
            
            # HSV color ranges for common background colors
            color_ranges = [
                # Blue backgrounds (presentations, headers)
                (np.array([90, 30, 30]), np.array([130, 255, 255])),
                # Yellow backgrounds (highlights, warnings)
                (np.array([20, 30, 30]), np.array([35, 255, 255])),
                # Green backgrounds (cells, status indicators)
                (np.array([35, 30, 30]), np.array([85, 255, 255])),
                # Red backgrounds (low hue range, wraps around 0)
                (np.array([0, 40, 40]), np.array([10, 255, 255])),
                # Red backgrounds (high hue range)
                (np.array([160, 40, 40]), np.array([180, 255, 255])),
                # Orange backgrounds
                (np.array([10, 40, 40]), np.array([20, 255, 255])),
                # Purple/magenta backgrounds
                (np.array([130, 30, 30]), np.array([160, 255, 255])),
            ]
            
            combined_mask = np.zeros(hsv.shape[:2], dtype=np.uint8)
            for lower, upper in color_ranges:
                mask = cv2.inRange(hsv, lower, upper)
                combined_mask = cv2.bitwise_or(combined_mask, mask)
            
            # Also detect dark backgrounds (V < 60, any color/saturation)
            # and very dark areas where text might be light-on-dark
            dark_mask = cv2.inRange(hsv, np.array([0, 0, 0]), np.array([180, 255, 60]))
            # Only apply dark mask if it covers >20% of image (likely a dark background)
            dark_ratio = np.count_nonzero(dark_mask) / dark_mask.size
            if dark_ratio > 0.20:
                combined_mask = cv2.bitwise_or(combined_mask, dark_mask)
            
            # Dilate to get slightly larger regions  
            kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
            dilated_mask = cv2.dilate(combined_mask, kernel, iterations=2)
            
            # Check if colored regions are significant (>5% of image)
            color_ratio = np.count_nonzero(dilated_mask) / dilated_mask.size
            if color_ratio < 0.05:
                return img  # Not enough colored region to bother
            
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            
            # Apply aggressive CLAHE to colored regions
            clahe = cv2.createCLAHE(clipLimit=5.0, tileGridSize=(4, 4))
            enhanced = clahe.apply(gray)
            
            result = gray.copy()
            result[dilated_mask > 0] = enhanced[dilated_mask > 0]
            
            return cv2.cvtColor(result, cv2.COLOR_GRAY2BGR)
            
        except Exception as e:
            logger.warning(f"Color background removal failed: {e}")
            return img
    
    def remove_color_background_aggressive(self, image_data: bytes) -> bytes:
        """Aggressive color background removal for graphical images.
        
        Converts the image to remove ANY colored background, producing
        a clean black-on-white image for maximum OCR readability.
        Used as a Smart OCR strategy for screenshots and diagrams.
        """
        try:
            nparr = np.frombuffer(image_data, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            if img is None:
                return image_data
            
            # Convert to grayscale
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            
            # Apply aggressive CLAHE
            clahe = cv2.createCLAHE(clipLimit=4.0, tileGridSize=(8, 8))
            enhanced = clahe.apply(gray)
            
            # Use Otsu thresholding to get clean black-on-white
            _, binary = cv2.threshold(enhanced, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            
            # If the result is mostly black (inverted), flip it
            white_ratio = np.count_nonzero(binary) / binary.size
            if white_ratio < 0.3:
                binary = cv2.bitwise_not(binary)
            
            _, buffer = cv2.imencode('.png', binary)
            return buffer.tobytes()
        except Exception as e:
            logger.warning(f"Aggressive color removal failed: {e}")
            return image_data
    
    def invert_and_enhance(self, image_data: bytes) -> bytes:
        """Invert and enhance for light-text-on-dark-background images.
        
        Handles screenshots, dark-themed UIs, terminal outputs, etc.
        """
        try:
            nparr = np.frombuffer(image_data, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            if img is None:
                return image_data
            
            gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            
            # Check if the image is predominantly dark
            mean_brightness = np.mean(gray)
            if mean_brightness < 128:
                # Dark image - invert it
                gray = cv2.bitwise_not(gray)
            
            # Apply denoising
            denoised = cv2.fastNlMeansDenoising(gray, None, 10, 7, 21)
            
            # CLAHE for contrast
            clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
            enhanced = clahe.apply(denoised)
            
            _, buffer = cv2.imencode('.png', enhanced)
            return buffer.tobytes()
        except Exception as e:
            logger.warning(f"Invert-and-enhance failed: {e}")
            return image_data
    
    def _enhance_faded_text(self, img: np.ndarray) -> np.ndarray:
        """Enhanced processing for faded/broken text"""
        try:
            if len(img.shape) == 3:
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            else:
                gray = img.copy()
            
            # Aggressive CLAHE
            clahe = cv2.createCLAHE(clipLimit=6.0, tileGridSize=(4, 4))
            enhanced = clahe.apply(gray)
            
            # Bilateral filter
            denoised = cv2.bilateralFilter(enhanced, 9, 75, 75)
            
            # Adaptive thresholding for broken/faded text
            adaptive = cv2.adaptiveThreshold(
                denoised,
                255,
                cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                cv2.THRESH_BINARY,
                21,
                10
            )
            
            # Morphological close to connect broken text
            kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
            closed = cv2.morphologyEx(adaptive, cv2.MORPH_CLOSE, kernel)
            
            if len(img.shape) == 3:
                return cv2.cvtColor(closed, cv2.COLOR_GRAY2BGR)
            return closed
            
        except Exception as e:
            logger.warning(f"Faded text enhancement failed: {e}")
            return img
    
    def _correct_skew(self, image: np.ndarray) -> np.ndarray:
        """Detect and correct skew in image"""
        try:
            if len(image.shape) == 3:
                gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            else:
                gray = image

            edges = cv2.Canny(gray, 50, 150, apertureSize=3)
            lines = cv2.HoughLines(edges, 1, np.pi / 180, 200)
            
            if lines is None:
                return image
            
            angles = []
            for rho, theta in lines[:, 0]:
                angle = np.degrees(theta) - 90
                angles.append(angle)
            
            if not angles:
                return image
            
            median_angle = np.median(angles)
            
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
    
    def _preprocess_pillow(self, image_data: bytes) -> Optional[bytes]:
        """Preprocess using PIL/Pillow (simpler fallback)"""
        try:
            img = Image.open(io.BytesIO(image_data))
            
            if self.preprocessing_config.get('convert_to_grayscale', True):
                img = img.convert('L')
            
            if self.preprocessing_config.get('enhance_contrast', True):
                from PIL import ImageEnhance
                enhancer = ImageEnhance.Contrast(img)
                img = enhancer.enhance(2.0)
            
            output = io.BytesIO()
            img.save(output, format='PNG')
            return output.getvalue()
            
        except Exception as e:
            logger.error(f"Error in Pillow preprocessing: {e}")
            return image_data

    def _correct_orientation(self, img: np.ndarray) -> np.ndarray:
        """Correct whole-document orientation (90°, 180°, 270°) using Hough lines
        and horizontal projection profile analysis.

        Handles 90°: dominant vertical Hough lines → rotate CCW.
        Handles 180°: text projection profile asymmetry (upside-down text has
                       more ink in the bottom half of each text line).
        """
        try:
            if len(img.shape) == 3:
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            else:
                gray = img.copy()

            # --- 90° detection via Hough lines ---
            edges = cv2.Canny(gray, 50, 150, apertureSize=3)
            lines = cv2.HoughLines(edges, 1, np.pi / 180, threshold=200)

            if lines is not None:
                angles = [float(np.degrees(theta)) for _, theta in lines[:, 0]]
                near_vertical = sum(1 for a in angles if a < 20 or a > 160)
                near_horizontal = sum(1 for a in angles if 70 < a < 110)
                total = len(angles)

                if total > 0 and near_vertical / total > 0.7 and near_horizontal / total < 0.2:
                    logger.info("Detected 90° rotation via Hough lines — correcting.")
                    return cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)

            # --- 180° detection via horizontal projection profile ---
            # Binarize to isolate text pixels (dark on light)
            _, bw = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
            h, w = bw.shape

            # Compute row-wise ink density (horizontal projection)
            row_ink = bw.sum(axis=1).astype(np.float64)

            if row_ink.max() == 0:
                return img

            row_ink /= row_ink.max()

            # For upright text, ascenders (top of letters) dominate the upper half of
            # each text line zone. For 180°-rotated text the pattern reverses.
            # Compare top-quarter vs bottom-quarter ink weight across the full page.
            q = h // 4
            top_weight = row_ink[:q].sum()
            bottom_weight = row_ink[h - q:].sum()

            # Also compare the gradient direction — upright text typically has
            # more ink mass in the upper portion of the text zone.
            mid = h // 2
            upper_half = row_ink[:mid].sum()
            lower_half = row_ink[mid:].sum()

            # Confidence score: upright images have upper_half >= lower_half
            # because ascenders (d, h, l, t, b, f, k) are taller than descenders.
            # If lower_half is significantly heavier, the document is 180°-rotated.
            if lower_half > upper_half * 1.15 and bottom_weight > top_weight * 1.10:
                logger.info(
                    f"Detected 180° rotation via projection profile "
                    f"(upper={upper_half:.1f}, lower={lower_half:.1f}) — correcting."
                )
                return cv2.rotate(img, cv2.ROTATE_180)

            return img

        except Exception as e:
            logger.warning(f"Orientation correction failed: {e}")
            return img
            
    def _handle_inverted_text(self, img: np.ndarray) -> np.ndarray:
        """Handle inverted text (White text on black background)"""
        try:
            if len(img.shape) == 3:
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            else:
                gray = img
                
            # Calculate mean pixel value
            mean_val = np.mean(gray)
            
            # If image is mostly dark (< 100), assume inverted
            if mean_val < 100:
                logger.debug(f"Detected inverted text (mean={mean_val:.1f}). Inverting.")
                return cv2.bitwise_not(img)
                
            return img
        except Exception as e:
            logger.warning(f"Inverted text handling failed: {e}")
            return img

    def _correct_perspective(self, img: np.ndarray) -> np.ndarray:
        """Correct perspective distortion (Document Dewarping)"""
        try:
            if len(img.shape) == 3:
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            else:
                gray = img
            
            # Blur and detect edges
            blurred = cv2.GaussianBlur(gray, (5, 5), 0)
            edged = cv2.Canny(blurred, 75, 200)
            
            # Find contours
            cnts, _ = cv2.findContours(edged.copy(), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            cnts = sorted(cnts, key=cv2.contourArea, reverse=True)[:5]
            
            doc_cnt = None
            
            # Loop over contours to find a 4-point one
            for c in cnts:
                peri = cv2.arcLength(c, True)
                approx = cv2.approxPolyDP(c, 0.02 * peri, True)
                
                if len(approx) == 4:
                    # Check area constraints to avoid tiny false positives
                    area = cv2.contourArea(c)
                    if area > (gray.shape[0] * gray.shape[1] * 0.3): # At least 30% of image
                        doc_cnt = approx
                        break
            
            if doc_cnt is None:
                return img
                
            # Apply 4-point transform
            import numpy as np
            pts = doc_cnt.reshape(4, 2)
            rect = np.zeros((4, 2), dtype="float32")
            
            # Top-left, top-right, bottom-right, bottom-left
            s = pts.sum(axis=1)
            rect[0] = pts[np.argmin(s)]
            rect[2] = pts[np.argmax(s)]
            
            diff = np.diff(pts, axis=1)
            rect[1] = pts[np.argmin(diff)]
            rect[3] = pts[np.argmax(diff)]
            
            (tl, tr, br, bl) = rect
            
            widthA = np.sqrt(((br[0] - bl[0]) ** 2) + ((br[1] - bl[1]) ** 2))
            widthB = np.sqrt(((tr[0] - tl[0]) ** 2) + ((tr[1] - tl[1]) ** 2))
            maxWidth = max(int(widthA), int(widthB))
            
            heightA = np.sqrt(((tr[0] - br[0]) ** 2) + ((tr[1] - br[1]) ** 2))
            heightB = np.sqrt(((tl[0] - bl[0]) ** 2) + ((tl[1] - bl[1]) ** 2))
            maxHeight = max(int(heightA), int(heightB))
            
            dst = np.array([
                [0, 0],
                [maxWidth - 1, 0],
                [maxWidth - 1, maxHeight - 1],
                [0, maxHeight - 1]], dtype="float32")
                
            M = cv2.getPerspectiveTransform(rect, dst)
            warped = cv2.warpPerspective(img, M, (maxWidth, maxHeight))
            
            logger.info("Perspective correction applied")
            return warped
            
        except Exception as e:
            logger.warning(f"Perspective correction failed: {e}")
            return img

    def _remove_shadows(self, img: np.ndarray) -> np.ndarray:
        """Remove shadows and normalize illumination"""
        try:
            # Split channels
            if len(img.shape) == 3:
                planes = cv2.split(img)
                result_planes = []
                
                for plane in planes:
                    dilated = cv2.dilate(plane, np.ones((50,50), np.uint8))
                    bg_img = cv2.medianBlur(dilated, 21)
                    diff_img = 255 - cv2.absdiff(plane, bg_img)
                    norm_img = cv2.normalize(diff_img, None, alpha=0, beta=255, norm_type=cv2.NORM_MINMAX, dtype=cv2.CV_8U)
                    result_planes.append(norm_img)
                    
                return cv2.merge(result_planes)
            else:
                dilated = cv2.dilate(img, np.ones((50,50), np.uint8))
                bg_img = cv2.medianBlur(dilated, 21)
                diff_img = 255 - cv2.absdiff(img, bg_img)
                return cv2.normalize(diff_img, None, alpha=0, beta=255, norm_type=cv2.NORM_MINMAX, dtype=cv2.CV_8U)
                
        except Exception as e:
            logger.warning(f"Shadow removal failed: {e}")
            return img

    def _remove_borders(self, img: np.ndarray) -> np.ndarray:
        """Remove black scanning borders"""
        try:
            if len(img.shape) == 3:
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            else:
                gray = img.copy()
                
            # Threshold to find black content
            _, thresh = cv2.threshold(gray, 1, 255, cv2.THRESH_BINARY)
            
            # Find contours
            contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            
            if not contours:
                return img
            
            # Get bounding box of largest contour (which should be the page content)
            c = max(contours, key=cv2.contourArea)
            x,y,w,h = cv2.boundingRect(c)
            
            # Check if crop is valid and reasonable size (>50% of image)
            if w > img.shape[1] * 0.5 and h > img.shape[0] * 0.5:
                # Crop
                return img[y:y+h, x:x+w]
            
            return img
            
        except Exception as e:
            logger.warning(f"Border removal failed: {e}")
            return img

    def _repair_broken_text(self, img: np.ndarray) -> np.ndarray:
        """Repair broken text/characters using dilation"""
        try:
            # Slight dilation to connect broken components
            kernel = np.ones((2, 2), np.uint8)
            
            # If dark text on light background (standard), dilation connects dark pixels?
            # No, dilate expands white regions. Erode expands dark regions.
            # Assuming standard BGR/Gray where text is dark:
            # We want to Expand Dark -> Erode.
            
            # Check polarity
            if len(img.shape) == 3:
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            else:
                gray = img
            
            if np.mean(gray) > 127:
                # Light background, dark text -> Erode to make text thicker/connected
                repaired = cv2.erode(img, kernel, iterations=1)
            else:
                # Dark background, light text -> Dilate to make text thicker/connected
                repaired = cv2.dilate(img, kernel, iterations=1)
            
            logger.debug("Applied broken text repair (thickening)")
            return repaired
            
        except Exception as e:
            logger.warning(f"Text repair failed: {e}")
            return img

    def _normalize_background(self, img: np.ndarray) -> np.ndarray:
        """
        Normalize non-uniform, colorful, gradient, or patterned backgrounds.
        This uses morphological dilation followed by median blur to estimate
        the background illumination, and then divides the original image by
        the background to flatten it to clean white.
        """
        try:
            is_color = len(img.shape) == 3
            
            if is_color:
                # Process in L*a*b* space so we only normalize luminance (L channel)
                lab = cv2.cvtColor(img, cv2.COLOR_BGR2LAB)
                l, a, b = cv2.split(lab)
                
                # Estimate background for luminance
                dilated = cv2.dilate(l, np.ones((7, 7), np.uint8))
                bg = cv2.medianBlur(dilated, 21)
                
                # Normalize luminance: divide original by estimated background
                normalized_l = cv2.divide(l, bg, scale=255)
                
                # Merge back
                normalized_lab = cv2.merge((normalized_l, a, b))
                normalized_img = cv2.cvtColor(normalized_lab, cv2.COLOR_LAB2BGR)
                return normalized_img
            else:
                dilated = cv2.dilate(img, np.ones((7, 7), np.uint8))
                bg = cv2.medianBlur(dilated, 21)
                normalized_img = cv2.divide(img, bg, scale=255)
                return normalized_img
                
        except Exception as e:
            logger.warning(f"Background normalization failed: {e}")
            return img

    def _apply_sauvola_threshold(self, img: np.ndarray, window_size: int = 25, k: float = 0.2, R: float = 128.0) -> np.ndarray:
        """
        Apply Sauvola local adaptive thresholding for high quality binarization
        especially under non-uniform illumination or background noise.
        """
        try:
            # Ensure grayscale
            if len(img.shape) == 3:
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            else:
                gray = img
                
            # Convert to float for precise division / math
            gray_f = gray.astype(np.float32)
            
            # Calculate local mean using boxFilter (O(1) time complexity)
            mean = cv2.boxFilter(gray_f, cv2.CV_32F, (window_size, window_size))
            
            # Calculate local mean of square
            sq_mean = cv2.boxFilter(gray_f**2, cv2.CV_32F, (window_size, window_size))
            
            # Local standard deviation
            variance = sq_mean - (mean**2)
            # Ensure no negative variance due to floating-point rounding precision
            variance = np.clip(variance, 0, None)
            std = np.sqrt(variance)
            
            # Sauvola threshold formula
            thresh = mean * (1.0 + k * (std / R - 1.0))
            
            # Binarize
            binary = np.where(gray_f > thresh, 255, 0).astype(np.uint8)
            return binary
            
        except Exception as e:
            logger.warning(f"Sauvola thresholding failed: {e}")
            # Fallback to standard Otsu's thresholding
            if len(img.shape) == 3:
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
            else:
                gray = img
            _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
            return binary

    def apply_sauvola_binarization(self, image_data: bytes) -> bytes:
        """Helper to apply morphological background normalization followed by Sauvola thresholding."""
        try:
            nparr = np.frombuffer(image_data, np.uint8)
            img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            if img is None:
                return image_data
            
            # 1. Normalize background
            normalized = self._normalize_background(img)
            
            # 2. Apply Sauvola thresholding
            binary = self._apply_sauvola_threshold(normalized)
            
            _, buffer = cv2.imencode('.png', binary)
            return buffer.tobytes()
        except Exception as e:
            logger.warning(f"Sauvola binarization helper failed: {e}")
            return image_data

