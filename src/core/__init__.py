"""
Enterprise Document Search System - Core Module
Production-grade core components
"""

__version__ = "1.0.0"

from .constants import *
from .config_manager import get_config, get_config_manager
from .logging_manager import setup_logging, get_logger
from .queue_manager import get_queue_manager

__all__ = [
    'get_config',
    'get_config_manager',
    'setup_logging',
    'get_logger',
    'get_queue_manager',
]
