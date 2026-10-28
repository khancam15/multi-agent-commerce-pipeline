"""
logger.py — Centralized structured logging
────────────────────────────────────────────
Replaces scattered print() statements with proper logging infrastructure.

Usage:
    from logger import get_logger

    logger = get_logger(__name__)
    logger.info("Processing product", extra={"product_name": "Example"})
    logger.error("API failed", extra={"status_code": 503, "attempt": 3})

Features:
  • Structured JSON logs for production (machine-readable)
  • Human-readable console output for development
  • Automatic context tracking (timestamps, module names)
  • Log rotation (keeps last 7 days)
  • Different log levels per module

Configuration via environment:
    LOG_LEVEL=DEBUG         # DEBUG, INFO, WARNING, ERROR, CRITICAL
    LOG_FORMAT=json         # json or console (default: console)
    LOG_FILE=logs/app.log   # Log file path
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# ── Configuration ─────────────────────────────────────────────────────────────

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_FORMAT = os.getenv("LOG_FORMAT", "console")  # 'json' or 'console'
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
LOG_FILE = LOG_DIR / "pipeline.log"

# Ensure log directory exists
LOG_DIR.mkdir(exist_ok=True)


# ── Custom Formatter ──────────────────────────────────────────────────────────

class StructuredFormatter(logging.Formatter):
    """
    Outputs logs as JSON for machine parsing, or human-readable for console.
    """

    def __init__(self, fmt_type: str = "console"):
        super().__init__()
        self.fmt_type = fmt_type

    def format(self, record: logging.LogRecord) -> str:
        if self.fmt_type == "json":
            import json
            log_data = {
                "timestamp": self.formatTime(record, "%Y-%m-%d %H:%M:%S"),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
                "module": record.module,
                "function": record.funcName,
                "line": record.lineno,
            }

            # Add extra fields if present
            if hasattr(record, "product_name"):
                log_data["product_name"] = record.product_name
            if hasattr(record, "phase"):
                log_data["phase"] = record.phase
            if hasattr(record, "status_code"):
                log_data["status_code"] = record.status_code

            # Include exception info if present
            if record.exc_info:
                log_data["exception"] = self.formatException(record.exc_info)

            return json.dumps(log_data)

        else:  # console format
            timestamp = self.formatTime(record, "%H:%M:%S")
            level_colors = {
                "DEBUG": "\033[36m",      # Cyan
                "INFO": "\033[32m",       # Green
                "WARNING": "\033[33m",    # Yellow
                "ERROR": "\033[31m",      # Red
                "CRITICAL": "\033[1;31m", # Bold Red
            }
            reset = "\033[0m"

            color = level_colors.get(record.levelname, "")
            level = f"{color}{record.levelname:8s}{reset}"

            # Build extra context
            extras = []
            if hasattr(record, "product_name"):
                extras.append(f"product={record.product_name}")
            if hasattr(record, "phase"):
                extras.append(f"phase={record.phase}")
            if hasattr(record, "status_code"):
                extras.append(f"status={record.status_code}")

            extra_str = f" [{', '.join(extras)}]" if extras else ""

            msg = f"[{timestamp}] {level} {record.name:20s} {record.getMessage()}{extra_str}"

            if record.exc_info:
                msg += "\n" + self.formatException(record.exc_info)

            return msg


# ── Logger Factory ────────────────────────────────────────────────────────────

_loggers: dict[str, logging.Logger] = {}


def get_logger(name: str, level: str | None = None) -> logging.Logger:
    """
    Get or create a logger with structured output.

    Args:
        name: Logger name (typically __name__)
        level: Optional override for log level (DEBUG, INFO, etc.)

    Returns:
        Configured logger instance
    """
    if name in _loggers:
        return _loggers[name]

    logger = logging.getLogger(name)
    logger.setLevel(level or LOG_LEVEL)
    logger.propagate = False

    # Console handler (always human-readable)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG)
    console_handler.setFormatter(StructuredFormatter("console"))
    logger.addHandler(console_handler)

    # File handler (JSON or console based on config)
    file_handler = RotatingFileHandler(
        LOG_FILE,
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=7,  # Keep last 7 rotations
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(StructuredFormatter(LOG_FORMAT))
    logger.addHandler(file_handler)

    _loggers[name] = logger
    return logger


# ── Helper for legacy print() migration ───────────────────────────────────────

def migrate_prints(module_name: str) -> logging.Logger:
    """
    Helper for gradual migration from print() to logging.

    Usage in existing scripts:
        from logger import migrate_prints
        logger = migrate_prints(__name__)

        # Replace: print(f"Starting phase 5 for {product}")
        # With:    logger.info("Starting phase 5", extra={"product_name": product})
    """
    return get_logger(module_name)


# ── Quick test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logger = get_logger("test")

    logger.debug("Debug message - you shouldn't see this unless LOG_LEVEL=DEBUG")
    logger.info("Pipeline starting", extra={"phase": "phase1"})
    logger.warning("Rate limit approaching", extra={"status_code": 429})
    logger.error("API call failed", extra={"product_name": "Test Product", "status_code": 503})

    try:
        raise ValueError("Test exception")
    except Exception:
        logger.exception("Caught an error")

    print("\n✅ Logger test complete. Check logs/pipeline.log")
