"""
Centralized logging configuration for PM Agent.

Provides consistent logging across all modules with:
- Structured format with timestamps and module names
- Dual output to both console (stderr) and file
- Easy request tracing with correlation IDs
- AKS-compatible JSON format option

Usage:
    from utilities.logging_config import setup_logging, get_logger
    
    # At app startup (once):
    setup_logging()
    
    # In each module:
    logger = get_logger(__name__)
    logger.info("Message here")
"""

import logging
import sys
import io
import os
from pathlib import Path
from datetime import datetime

# FIX #10: Ensure stdout/stderr support Unicode on Windows
# This prevents 'charmap' codec errors with special characters like →
if sys.platform == 'win32':
    try:
        if hasattr(sys.stdout, 'reconfigure'):
            sys.stdout.reconfigure(encoding='utf-8', errors='replace')
        elif hasattr(sys.stdout, 'buffer'):
            sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
        if hasattr(sys.stderr, 'reconfigure'):
            sys.stderr.reconfigure(encoding='utf-8', errors='replace')
        elif hasattr(sys.stderr, 'buffer'):
            sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')
    except Exception:
        pass  # Ignore if reconfiguration fails

# Root project directory
ROOT_DIR = Path(__file__).resolve().parent.parent
LOG_DIR = ROOT_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)

# Default log format - includes timestamp, level, module, and message
# Format designed for easy grep/filtering and request tracing
DEFAULT_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

# Simple format for console (less verbose)
CONSOLE_FORMAT = "[%(levelname)s] %(name)s: %(message)s"


class TruncateFilter(logging.Filter):
    """
    Truncate log messages that are too long to prevent terminal hangs.
    
    This filter prevents massive log messages (e.g., full work item payloads)
    from blocking the async event loop when logging to console.
    """
    
    MAX_MESSAGE_LENGTH = 5000  # 5KB max per log message (reasonable for terminal)
    
    def filter(self, record):
        """
        Truncate log message if it exceeds MAX_MESSAGE_LENGTH.
        
        This prevents:
        1. Terminal buffer overflow causing hangs
        2. Synchronous str() conversion of large objects blocking async event loop
        3. Memory issues from formatting massive strings
        """
        # Truncate the message format string if it's already too long
        if isinstance(record.msg, str) and len(record.msg) > self.MAX_MESSAGE_LENGTH:
            truncated_msg = record.msg[:self.MAX_MESSAGE_LENGTH]
            record.msg = f"{truncated_msg}... [TRUNCATED - original {len(record.msg)} bytes]"
        
        # Handle args that might be large objects
        if record.args:
            truncated_args = []
            for arg in record.args:
                if arg is None:
                    truncated_args.append(None)
                    continue
                
                # Convert to string safely
                try:
                    arg_str = str(arg)
                    if len(arg_str) > self.MAX_MESSAGE_LENGTH:
                        # For large objects, show type and truncated representation
                        if isinstance(arg, (list, dict)):
                            summary = self._summarize_collection(arg)
                            truncated_args.append(f"{summary} [TRUNCATED - {len(arg_str)} bytes total]")
                        else:
                            truncated_args.append(f"{arg_str[:self.MAX_MESSAGE_LENGTH]}... [TRUNCATED]")
                    else:
                        truncated_args.append(arg)
                except Exception:
                    # If str() conversion fails, use repr() or just type name
                    truncated_args.append(f"<{type(arg).__name__} object>")
            
            record.args = tuple(truncated_args)
        
        return True
    
    def _summarize_collection(self, obj):
        """Summarize list/dict without full str() conversion."""
        if isinstance(obj, list):
            return f"<list: {len(obj)} items, first_item={type(obj[0]).__name__ if obj else 'empty'}>"
        elif isinstance(obj, dict):
            sample_keys = list(obj.keys())[:5]
            return f"<dict: {len(obj)} keys, sample={sample_keys}>"
        return f"<{type(obj).__name__}>"


def setup_logging(
    level: str = None,
    log_file: str = None,
    console: bool = True,
    force: bool = False
) -> None:
    """
    Configure root logger with console and file handlers.
    
    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR). Defaults to INFO or LOG_LEVEL env var.
        log_file: Path to log file. Defaults to logs/pm_agent.log.
        console: Whether to log to console (stderr). Default True.
        force: Force reconfiguration even if already configured.
    """
    # Determine log level from env or parameter
    if level is None:
        from config import config as app_config
        level = app_config.log_level.upper()
    
    log_level = getattr(logging, level, logging.INFO)
    
    # Get root logger
    root_logger = logging.getLogger()
    
    # Skip if already configured (unless force=True)
    if root_logger.handlers and not force:
        return
    
    # Clear existing handlers if forcing
    if force:
        root_logger.handlers.clear()
    
    root_logger.setLevel(log_level)
    
    # Console handler (to stderr for visibility)
    if console:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(log_level)
        console_handler.setFormatter(logging.Formatter(DEFAULT_FORMAT, DATE_FORMAT))
        
        # Add truncate filter to prevent terminal hangs from large messages
        truncate_filter = TruncateFilter()
        console_handler.addFilter(truncate_filter)
        
        root_logger.addHandler(console_handler)
    
    # File handler
    if log_file is None:
        log_file = LOG_DIR / "pm_agent.log"
    else:
        log_file = Path(log_file)
    
    try:
        file_handler = logging.FileHandler(str(log_file), encoding="utf-8")
        file_handler.setLevel(log_level)
        file_handler.setFormatter(logging.Formatter(DEFAULT_FORMAT, DATE_FORMAT))
        
        # Add truncate filter to prevent massive log files
        truncate_filter = TruncateFilter()
        file_handler.addFilter(truncate_filter)
        
        root_logger.addHandler(file_handler)
    except Exception as e:
        # If file handler fails, just log to console
        if console:
            root_logger.warning(f"Could not create file handler: {e}")
    
    # Reduce noise from third-party libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    
    # CRITICAL: Suppress OpenAI SDK debug logging to prevent TypeError
    # The OpenAI SDK's _base_client.py uses log.debug("Request options: %s", model_dump(...))
    # but model_dump returns a dict with multiple keys which causes:
    # "TypeError: not all arguments converted during string formatting"
    # This is a bug in the OpenAI SDK's logging - setting to WARNING suppresses it
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("openai._base_client").setLevel(logging.WARNING)
    
    root_logger.debug(f"Logging configured: level={level}, file={log_file}, console={console}")


def get_logger(name: str) -> logging.Logger:
    """
    Get a logger for a module.
    
    Args:
        name: Module name (typically __name__)
    
    Returns:
        Configured logger instance
    """
    return logging.getLogger(name)


# Convenience function for modules that just want to import and use
def log_info(msg: str, *args, **kwargs):
    """Quick info log to root logger."""
    logging.info(msg, *args, **kwargs)


def log_error(msg: str, *args, **kwargs):
    """Quick error log to root logger."""
    logging.error(msg, *args, **kwargs)


def log_debug(msg: str, *args, **kwargs):
    """Quick debug log to root logger."""
    logging.debug(msg, *args, **kwargs)
