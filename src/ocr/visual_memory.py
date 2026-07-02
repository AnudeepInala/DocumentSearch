import os
import json
from typing import Optional, Tuple
import numpy as np
from pathlib import Path
from PIL import Image
import cv2
from core.logging_manager import get_logger

logger = get_logger("ocr.visual_memory")

try:
    import torch
    import torchvision.models as models
    import torchvision.transforms as transforms
    _HAS_TORCH = True
except ImportError:
    _HAS_TORCH = False
    logger.warning("PyTorch/Torchvision is not available. VisualMemoryEngine will fall back to perceptual hashing.")


def _models_dir() -> Path:
    """Return the absolute path to the models/ directory.

    Resolution order:
      1. ``config.paths.app_root / models`` (when config is available)
      2. Four levels up from this file — i.e. <repo-root>/models (fallback)
    """
    try:
        from core.config_manager import get_config
        cfg = get_config()
        candidate = Path(cfg.paths.app_root) / "models"
        if candidate.exists():
            return candidate
    except Exception:
        pass
    # Fallback: src/ocr/visual_memory.py → src/ocr → src → <repo-root>
    return Path(__file__).resolve().parents[2] / "models"


class VisualMemoryEngine:
    """Deep Learning-based Visual Memory Engine.
    Extracts CNN feature vectors from cropped document snippets (signatures, seals, logos)
    and computes cosine similarity against previously approved visual templates.
    """

    def __init__(self):
        self.device = "cuda" if (_HAS_TORCH and torch.cuda.is_available()) else "cpu"
        self.model = None
        self.transform = None
        self.net = None
        self.layout_net = None  # YOLOv8-based layout segmentation model

        models_dir = _models_dir()

        # 1. Try OpenCV DNN ONNX first (MobileNetV3 feature extractor)
        onnx_path = str(models_dir / "mobilenetv3.onnx")
        if os.path.exists(onnx_path):
            try:
                self.net = cv2.dnn.readNetFromONNX(onnx_path)
                self.net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
                self.net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
                logger.info(f"VisualMemoryEngine: Loaded {onnx_path} natively via OpenCV DNN")
            except Exception as exc:
                logger.warning(f"VisualMemoryEngine: Failed to load ONNX via OpenCV DNN: {exc}")

        # 2. Load YOLOv8 layout segmentation model for snippet region detection
        layout_onnx = str(models_dir / "yolov8_layout.onnx")
        if os.path.exists(layout_onnx):
            try:
                self.layout_net = cv2.dnn.readNetFromONNX(layout_onnx)
                self.layout_net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
                self.layout_net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
                logger.info(f"VisualMemoryEngine: Loaded layout model {layout_onnx} via OpenCV DNN")
            except Exception as exc:
                logger.warning(f"VisualMemoryEngine: Failed to load layout ONNX: {exc}")

        # 3. Fallback to Torch model if ONNX not loaded
        if self.net is None and _HAS_TORCH:
            try:
                # Use a lightweight MobileNetV3 for lightning fast CPU inference
                self.model = models.mobilenet_v3_small(pretrained=True)
                self.model.eval()  # Freeze weights and activate evaluation mode
                self.model.to(self.device)

                self.transform = transforms.Compose([
                    transforms.Resize((224, 224)),
                    transforms.ToTensor(),
                    transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])
                ])
                logger.info(f"VisualMemoryEngine: CNN model (MobileNetV3) loaded successfully on {self.device}")
            except Exception as exc:
                logger.error(f"VisualMemoryEngine: Failed to load pre-trained CNN: {exc}")
                self.model = None

    def detect_layout_regions(self, image_path: str, conf_threshold: float = 0.40) -> list:
        """Detect document layout regions (text, figure, table, signature …) using
        the bundled YOLOv8 ONNX layout model.

        Returns a list of dicts:
            [{"class_id": int, "confidence": float, "bbox": [x1, y1, x2, y2]}, …]

        Falls back to an empty list when the layout model is not loaded or inference fails.
        """
        if self.layout_net is None:
            return []
        try:
            img = cv2.imread(image_path)
            if img is None:
                return []
            h, w = img.shape[:2]
            # YOLOv8 expects 640×640 input
            blob = cv2.dnn.blobFromImage(img, scalefactor=1.0 / 255.0, size=(640, 640),
                                          swapRB=True, crop=False)
            self.layout_net.setInput(blob)
            outputs = self.layout_net.forward()  # shape: (1, num_classes+4, num_anchors)

            regions = []
            # YOLOv8 output layout: [batch, 4+num_classes, num_proposals]
            preds = outputs[0]  # (4+C, N) after squeezing batch dim
            if preds.ndim == 3:
                preds = preds[0]  # (4+C, N)
            preds = preds.T      # (N, 4+C)
            for det in preds:
                box_scores = det[4:]
                class_id = int(np.argmax(box_scores))
                confidence = float(box_scores[class_id])
                if confidence < conf_threshold:
                    continue
                cx, cy, bw, bh = det[0], det[1], det[2], det[3]
                # Convert from 640-normalised centre-format to absolute pixel coords
                x1 = int((cx - bw / 2) / 640 * w)
                y1 = int((cy - bh / 2) / 640 * h)
                x2 = int((cx + bw / 2) / 640 * w)
                y2 = int((cy + bh / 2) / 640 * h)
                regions.append({
                    "class_id": class_id,
                    "confidence": round(confidence, 4),
                    "bbox": [max(0, x1), max(0, y1), min(w, x2), min(h, y2)],
                })
            return regions
        except Exception as exc:
            logger.error(f"VisualMemoryEngine: Layout detection failed for {image_path}: {exc}")
            return []

    def extract_vector(self, image_path: str) -> Optional[np.ndarray]:
        """Extract a deep learning feature vector from a cropped snippet image."""
        # Try OpenCV DNN first
        if self.net is not None:
            try:
                img = Image.open(image_path).convert('RGB')
                img_np = np.array(img)
                # Input expects 224x224 RGB image normalized via torchvision mean/std
                blob = cv2.dnn.blobFromImage(
                    img_np,
                    scalefactor=1.0/255.0,
                    size=(224, 224),
                    swapRB=False,  # PIL image is RGB, so keep RGB
                    crop=False
                )
                # Normalize manually: (x - mean) / std
                mean = np.array([0.485, 0.456, 0.406]).reshape(1, 3, 1, 1).astype(np.float32)
                std = np.array([0.229, 0.224, 0.225]).reshape(1, 3, 1, 1).astype(np.float32)
                blob = (blob - mean) / std

                self.net.setInput(blob)
                features = self.net.forward()  # Shape: (1, 576)
                vector_np = features[0]

                norm = np.linalg.norm(vector_np)
                if norm > 0:
                    vector_np = vector_np / norm
                return vector_np
            except Exception as exc:
                logger.error(f"VisualMemoryEngine: OpenCV DNN Feature extraction failed for {image_path}: {exc}")

        # Fallback to Torch torchvision
        if not _HAS_TORCH or self.model is None:
            return self._extract_fallback_hash(image_path)

        try:
            img = Image.open(image_path).convert('RGB')

            tensor = self.transform(img).unsqueeze(0).to(self.device)
            
            with torch.no_grad():
                # Extract features from the final global average pooling layer
                features = self.model.features(tensor)
                vector = torch.nn.functional.adaptive_avg_pool2d(features, 1).flatten()
                
            # Convert to normalized numpy array
            vector_np = vector.cpu().numpy()
            norm = np.linalg.norm(vector_np)
            if norm > 0:
                vector_np = vector_np / norm
            return vector_np
        except Exception as exc:
            logger.error(f"VisualMemoryEngine: Feature extraction failed for {image_path}: {exc}")
            return None

    def compute_similarity(self, vector_a: np.ndarray, vector_b: np.ndarray) -> float:
        """Compute cosine similarity between two normalized feature vectors."""
        # Vectors are already L2-normalized, so similarity is just their dot product
        return float(np.dot(vector_a, vector_b))

    def _get_review_text(self, review_id: str, db_path: Optional[str] = None) -> Optional[str]:
        if not db_path:
            try:
                from core.config_manager import get_config
                cfg = get_config()
                db_path = str(Path(cfg.paths.working_root) / "audit" / "audit.db")
            except Exception:
                return None
                
        if not db_path or not os.path.exists(db_path):
            return None
            
        import sqlite3
        try:
            conn = sqlite3.connect(db_path, timeout=5.0)
            cursor = conn.cursor()
            cursor.execute(
                "SELECT transcription_text, extracted_text FROM snippet_reviews WHERE review_id = ?",
                (review_id,)
            )
            row = cursor.fetchone()
            conn.close()
            if row:
                # Use transcription_text if set (verified by human), otherwise raw extracted_text
                return row[0] if row[0] else row[1]
        except Exception as e:
            logger.error(f"VisualMemoryEngine: Error querying review text for {review_id}: {e}")
            
        return None

    def _match_vector_with_text(
        self,
        candidate_vector: np.ndarray,
        vectors_dir: str,
        threshold: float,
        candidate_text: Optional[str] = None,
        db_path: Optional[str] = None,
    ) -> Tuple[bool, Optional[str]]:
        vectors_path = Path(vectors_dir)
        if not vectors_path.exists():
            return False, None

        best_score = -1.0
        best_match_path = None

        # Clean candidate text if provided
        import re
        import difflib
        
        clean_candidate = re.sub(r"[^a-zA-Z0-9]", "", candidate_text).lower() if candidate_text else ""

        for f in vectors_path.glob("*.npy"):
            try:
                approved_vector = np.load(str(f))
                score = self.compute_similarity(candidate_vector, approved_vector)
                if score > best_score:
                    # Perform hybrid text-similarity validation if score passes threshold
                    if score >= threshold:
                        # Load template text using review_id (filename stem)
                        template_review_id = f.stem
                        template_text = self._get_review_text(template_review_id, db_path)
                        clean_template = re.sub(r"[^a-zA-Z0-9]", "", template_text).lower() if template_text else ""
                        
                        # If both texts are non-empty and of significant length (>= 2 chars), they must be similar
                        if len(clean_candidate) >= 2 and len(clean_template) >= 2:
                            text_sim = difflib.SequenceMatcher(None, clean_candidate, clean_template).ratio()
                            if text_sim < 0.50:
                                logger.info(
                                    f"VisualMemoryEngine: High visual similarity ({score:.4f}) but conflicting texts "
                                    f"('{clean_candidate}' vs '{clean_template}', similarity={text_sim:.2f}). Rejecting match."
                                )
                                continue # Conflicting texts, reject this match candidate
                                
                    best_score = score
                    best_match_path = str(f)
            except Exception as e:
                logger.error(f"VisualMemoryEngine: Error matching against {f.name}: {e}")

        logger.info(f"VisualMemoryEngine: Best match score: {best_score:.4f} (Threshold: {threshold})")
        if best_score >= threshold:
            return True, best_match_path
        return False, None

    def match_snippet(
        self,
        candidate_image_path: str,
        approved_vectors_dir: str,
        threshold: float = 0.88,
        candidate_text: Optional[str] = None,
        db_path: Optional[str] = None,
    ) -> Tuple[bool, Optional[str]]:
        """Compare a candidate snippet against a database of accepted vectors.

        Args:
            candidate_image_path: Path to the newly cropped visual snippet.
            approved_vectors_dir: Directory containing accepted numpy vector files (.npy).
            threshold: Cosine similarity threshold for matching (default 0.88).
            candidate_text: Optional text string associated with the candidate.
            db_path: Optional path to SQLite database.

        Returns:
            (is_match, matched_vector_path)
        """
        candidate_vector = self.extract_vector(candidate_image_path)
        if candidate_vector is None:
            return False, None
        return self._match_vector_with_text(
            candidate_vector=candidate_vector,
            vectors_dir=approved_vectors_dir,
            threshold=threshold,
            candidate_text=candidate_text,
            db_path=db_path,
        )

    def match_snippet_global(
        self,
        candidate_image_path_or_vector,
        global_vectors_dir: str,
        threshold: float = 0.90,
        candidate_text: Optional[str] = None,
        db_path: Optional[str] = None,
    ) -> Tuple[bool, Optional[str]]:
        """Compare a candidate snippet against the global database of accepted vectors.

        Args:
            candidate_image_path_or_vector: Either a path string to an image file,
                or a pre-computed numpy ndarray feature vector.
            global_vectors_dir: Directory containing approved .npy vector files.
            threshold: Cosine similarity threshold (default 0.90).
            candidate_text: Optional text string associated with the candidate.
            db_path: Optional path to SQLite database.

        Returns:
            (is_match, matched_vector_path)
        """
        if isinstance(candidate_image_path_or_vector, np.ndarray):
            return self._match_vector_with_text(
                candidate_vector=candidate_image_path_or_vector,
                vectors_dir=global_vectors_dir,
                threshold=threshold,
                candidate_text=candidate_text,
                db_path=db_path,
            )
        else:
            return self.match_snippet(
                candidate_image_path=candidate_image_path_or_vector,
                approved_vectors_dir=global_vectors_dir,
                threshold=threshold,
                candidate_text=candidate_text,
                db_path=db_path,
            )

    def _extract_fallback_hash(self, image_path: str) -> Optional[np.ndarray]:
        """A simple structural/perceptual fallback if PyTorch is missing."""
        try:
            img = Image.open(image_path).convert('L').resize((16, 16), Image.Resampling.LANCZOS)
            pixels = np.array(img, dtype=float)
            # Normalize vector
            flat = pixels.flatten()
            norm = np.linalg.norm(flat)
            if norm > 0:
                flat = flat / norm
            return flat
        except Exception as exc:
            logger.error(f"VisualMemoryEngine: Fallback hashing failed for {image_path}: {exc}")
            return None
