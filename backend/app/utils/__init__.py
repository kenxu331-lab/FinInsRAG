# Set the process timezone where the platform supports it (Unix only).
import time

if hasattr(time, "tzset"):
    time.tzset()

try:
    from .get_logger import get_logger

    logger = get_logger()
except ImportError:
    import logging

    logger = logging.getLogger("fininsrag")
