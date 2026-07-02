import re
from pathlib import Path
from typing import Dict, Any, List
import numpy as np
from PIL import Image

from ocr.paddle_wrapper import PaddleWrapper
from core.reporting_manager import log_snippet_suppression

class SnippetFilter:
    """Service to heuristically filter out noisy or erroneous visual snippets."""

    def __init__(self, ocr_engine=None):
        self._ocr_engine = ocr_engine or PaddleWrapper()

    def is_obvious_noise(self, snippet_path: str, snippet_type: str = "", accuracy_impact: float = 0.0) -> bool:
        """Identify sparse dot/blob snippets that should not appear in visual review queue."""
        
        # Hard suppression only for extremely tiny impact
        if snippet_type == "signature" and accuracy_impact <= 0.03:
            return True

        path = Path(snippet_path)
        if not path.exists():
            return False

        try:
            arr = np.array(Image.open(str(path)).convert("L"))
            if arr.ndim != 2:
                return False

            h, w = arr.shape
            area = max(1, h * w)
            ink_mask = arr < 200
            ink_px = int(np.count_nonzero(ink_mask))
            if ink_px == 0:
                return True

            ink_ratio = ink_px / area
            row_coverage = float(np.count_nonzero(np.any(ink_mask, axis=1)) / max(1, h))
            col_coverage = float(np.count_nonzero(np.any(ink_mask, axis=0)) / max(1, w))

            return (
                ink_ratio < 0.004
                or (ink_ratio < 0.010 and row_coverage < 0.35 and col_coverage < 0.35)
                or (area > 1500 and ink_px < 60)
            )
        except Exception:
            return False

    def is_printed_font_artifact(self, snippet_path: str, snippet_type: str = "") -> bool:
        """Return True when the saved crop looks like printed/digital text, not handwriting.
        Uses numpy-only stroke-width uniformity check.
        """
        if snippet_type != "signature":
            return False

        path = Path(snippet_path)
        if not path.exists():
            return False

        try:
            arr = np.array(Image.open(str(path)).convert("L"))
            if arr.ndim != 2:
                return False

            h, w = arr.shape
            if h < 8 or w < 8:
                return False

            # Binarize: ink = True
            ink = arr < 128
            ink_px = int(np.count_nonzero(ink))
            if ink_px < 20:
                return False

            run_lengths = []
            for row in ink:
                in_run = False
                run_len = 0
                for px in row:
                    if px:
                        in_run = True
                        run_len += 1
                    else:
                        if in_run and run_len > 0:
                            run_lengths.append(run_len)
                        in_run = False
                        run_len = 0
                if in_run and run_len > 0:
                    run_lengths.append(run_len)

            if len(run_lengths) < 10:
                return False

            rl = np.array(run_lengths, dtype=np.float32)
            cv_val = float(np.std(rl) / max(np.mean(rl), 0.001))

            is_uniform_stroke = cv_val < 0.55

            row_densities = np.sum(ink, axis=1) / max(w, 1)
            nonzero_rows = row_densities[row_densities > 0]
            if len(nonzero_rows) < 3:
                return False
            density_cv = float(np.std(nonzero_rows) / max(np.mean(nonzero_rows), 0.001))

            is_uniform_density = density_cv < 0.65

            return is_uniform_stroke and is_uniform_density

        except Exception:
            return False

    def is_text_like(self, snippet_path: str, snippet_type: str = "") -> bool:
        """Hide snippets that are actually machine-readable text tokens."""
        path = Path(snippet_path)
        if not path.exists():
            return False

        if not self._ocr_engine:
            return False

        try:
            ocr_result = self._ocr_engine.extract_text(str(path))
            if not ocr_result:
                return False

            text, confidence = ocr_result
            cleaned = re.sub(r"\s+", "", text or "")
            alnum = re.sub(r"[^A-Za-z0-9]", "", cleaned)
            alpha_only = re.sub(r"[^A-Za-z]", "", cleaned)
            conf = float(confidence or 0.0)

            base_text_like = (len(alnum) >= 4 and conf >= 18.0) or (len(alnum) >= 7 and conf >= 12.0)

            alpha_ratio = (len(alpha_only) / max(1, len(alnum))) if alnum else 0.0
            printed_signature_word = (
                snippet_type == "signature"
                and len(alpha_only) >= 6
                and alpha_ratio >= 0.85
                and conf >= 8.0
            )

            return base_text_like or printed_signature_word
        except Exception:
            return False

    def apply_config_policy(self, config: Any, snippet: Dict[str, Any]) -> bool:
        """Check if snippet violates any explicit policies from config.yaml."""
        try:
            sup_conf = getattr(getattr(config, "extraction", None), "suppression", None)
            if not sup_conf:
                return False
            
            stype = snippet.get("snippet_type", "")
            
            # Example policies we could enforce if present in config:
            # if getattr(sup_conf, "ignore_small_signatures", False) and stype == "signature":
            #    area = snippet.get("width", 0) * snippet.get("height", 0)
            #    if area > 0 and area < 2000:
            #        return True
                    
        except Exception:
            pass
            
        return False

    def filter_page_snippets(
        self, 
        smart_id: str, 
        page_num: int, 
        snippets: List[Dict[str, Any]], 
        config: Any = None,
        worker_id: str = None
    ) -> List[Dict[str, Any]]:
        """Run all filters on a list of snippets, logging suppressions."""
        retained = []
        for snip in snippets:
            stype = snip.get("snippet_type", "")
            spath = snip.get("snippet_path", "")
            impact = float(snip.get("accuracy_impact") or 0.0)
            
            suppress_reason = None
            
            if config and self.apply_config_policy(config, snip):
                suppress_reason = "config_policy"
            elif self.is_obvious_noise(spath, stype, impact):
                suppress_reason = "obvious_noise"
            elif self.is_printed_font_artifact(spath, stype):
                suppress_reason = "printed_font"
            elif self.is_text_like(spath, stype):
                suppress_reason = "text_like"
                
            if suppress_reason:
                bbox = snip.get("bounding_box", [])
                import json
                log_snippet_suppression(
                    smart_id=smart_id,
                    page_num=page_num,
                    bbox_json=json.dumps(bbox),
                    suppressed_by=suppress_reason,
                    snippet_type=stype,
                    accuracy_impact=impact,
                    worker_id=worker_id
                )
            else:
                retained.append(snip)
                
        return retained
