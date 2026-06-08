"""Centralized logging configuration.

Goals:
- JSON logs to stdout (AKS/container friendly)
- Context propagation via contextvars (request-scoped fields)
- Environment-driven log level via LOG_LEVEL

Usage:
    from utilities.common.logging_config import init_logging, bind_context
    init_logging(service_name="pm-agent")
    bind_context(request_id="...", project="...")
"""

from __future__ import annotations

import contextvars
import io
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Optional


# FIX #10: Ensure stdout supports Unicode on Windows
# This prevents 'charmap' codec errors with special characters like →
if sys.platform == 'win32':
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    elif hasattr(sys.stdout, 'buffer'):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')


_context: contextvars.ContextVar[Dict[str, Any]] = contextvars.ContextVar("log_context", default={})
_initialized: bool = False


_SENSITIVE_KEY_SUBSTRINGS = (
    "pat",
    "token",
    "secret",
    "password",
    "authorization",
    "api_key",
    "apikey",
    "key",
)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _parse_level(value: Optional[str]) -> int:
    if not value:
        return logging.INFO
    value_norm = value.strip().upper()
    return getattr(logging, value_norm, logging.INFO)


def _is_sensitive_key(key: str) -> bool:
    key_norm = key.lower()
    return any(substr in key_norm for substr in _SENSITIVE_KEY_SUBSTRINGS)


def _redact_value(value: Any) -> str:
    if value is None:
        return ""
    return "***REDACTED***"


def _sanitize_mapping(data: Mapping[str, Any]) -> Dict[str, Any]:
    sanitized: Dict[str, Any] = {}
    for k, v in data.items():
        if not isinstance(k, str):
            continue
        if _is_sensitive_key(k):
            sanitized[k] = _redact_value(v)
        else:
            sanitized[k] = v
    return sanitized


def bind_context(**fields: Any) -> None:
    """Merge fields into the current logging context."""
    current = dict(_context.get())
    current.update(_sanitize_mapping(fields))
    _context.set(current)


def clear_context() -> None:
    """Clear the current logging context."""
    _context.set({})


class ContextFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record._log_context = _context.get()  # type: ignore[attr-defined]
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        base: Dict[str, Any] = {
            "ts": _utc_now_iso(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }

        context_fields = getattr(record, "_log_context", None)
        if isinstance(context_fields, dict) and context_fields:
            base.update(context_fields)

        if record.exc_info:
            base["exc_info"] = self.formatException(record.exc_info)

        # Allow structured extras by passing `extra={"fields": {...}}`
        extra_fields = getattr(record, "fields", None)
        if isinstance(extra_fields, dict) and extra_fields:
            base.update(_sanitize_mapping(extra_fields))

        return json.dumps(base, ensure_ascii=False)


def init_logging(*, service_name: str = "pm-agent") -> None:
    """Initialize process-wide logging once.

    - Writes JSON to stdout
    - Sets root level from LOG_LEVEL (default INFO)
    - Ensures common framework loggers propagate to root
    """

    global _initialized
    if _initialized:
        return

    from config import config as app_config
    log_level = _parse_level(app_config.log_level)

    root = logging.getLogger()
    root.setLevel(log_level)

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setLevel(log_level)
    handler.setFormatter(JsonFormatter())
    handler.addFilter(ContextFilter())

    # Replace existing handlers to avoid duplicate logs on Streamlit reruns.
    root.handlers = [handler]

    # Reasonable defaults for chatty libs.
    for noisy in ("httpx", "urllib3"):
        logging.getLogger(noisy).setLevel(max(log_level, logging.WARNING))

    # Ensure uvicorn logs flow through root.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers = []
        lg.propagate = True

    from config import config as app_config
    environment = app_config.environment
    bind_context(service=service_name, environment=environment)
    _initialized = True
