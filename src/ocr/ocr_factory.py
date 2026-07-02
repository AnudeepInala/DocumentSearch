"""
OCR engine factory — returns the configured engine (VL-1.6 or standard PaddleOCR).
Engine selection order: runtime override (Redis) > config.yaml > default ("vl16").
"""

from core.logging_manager import get_logger
from core.config_manager import get_config

logger = get_logger("ocr.factory")


def get_ocr_engine():
    """Return the correct OCR engine wrapper based on current configuration.

    Returns an instance with the interface:
        extract_text(image_path) -> Optional[Tuple[str, float]]
        health_check()           -> bool
        get_version()            -> Optional[str]
        get_stats()              -> Dict[str, Any]
    """
    engine = _resolve_engine()
    logger.info(f"OCR engine selected: {engine}")

    if engine == "tiered":
        from .ocr_tier_router import TieredOCREngine
        return TieredOCREngine()

    if engine == "vl16":
        from .vl16_wrapper import VL16Wrapper
        return VL16Wrapper()

    # Default / fallback: standard PaddleOCR
    from .paddle_wrapper import PaddleWrapper
    return PaddleWrapper()


def _resolve_engine() -> str:
    """Determine which engine to use, checking Redis override first."""
    # 1. Check Redis for a runtime override set via the dashboard
    try:
        from core.queue_manager import get_queue_manager
        qm = get_queue_manager()
        if hasattr(qm, "get_ocr_engine"):
            runtime_engine = qm.get_ocr_engine()
            if runtime_engine:
                return runtime_engine.strip().lower()
    except Exception:
        pass

    # 2. Fall back to config.yaml value
    try:
        cfg = get_config()
        engine = getattr(cfg.ocr, "engine", "vl16")
        if engine:
            return str(engine).strip().lower()
    except Exception:
        pass

    return "vl16"
