import os
import json
import time
import logging
from typing import Any, Dict, Optional

from config import config

logger = logging.getLogger(__name__)


def _safe_preview(obj: Any, max_len: int = 500) -> Any:
    try:
        if isinstance(obj, (dict, list)):
            s = json.dumps(obj, default=str)
        else:
            s = str(obj)
        return s if len(s) <= max_len else s[:max_len] + '...'
    except Exception:
        return str(obj)[:max_len]


def log_planner_interaction(
    stage: str,
    query: str,
    context: Dict[str, Any],
    plan: Optional[Dict[str, Any]] = None,
    trace: Optional[Any] = None,
) -> None:
    """Append a small, sanitized planner interaction record to a file when enabled.

    Args:
        stage: 'pre' or 'post'
        query: Original user query
        context: Merged context (will only log keys)
        plan: Planner result (sanitized preview)
        trace: Optional trace/span object to extract trace id
    """
    try:
        if not config.pm_log_reasoning:
            return

        path = config.pm_reasoning_log_path
        os.makedirs(os.path.dirname(path), exist_ok=True)

        record = {
            "ts": int(time.time()),
            "stage": stage,
            "query_preview": _safe_preview(query, max_len=300),
            "context_keys": list(context.keys()) if isinstance(context, dict) else [],
            "plan_preview": None,
        }

        if plan:
            # Keep only top-level safe fields
            safe_plan = {
                "action": plan.get("action"),
                "tool": plan.get("tool"),
                "confidence": plan.get("confidence"),
                "message": _safe_preview(plan.get("message", ""), max_len=300),
                "args": _safe_preview(plan.get("args", {}), max_len=800),
            }
            record["plan_preview"] = safe_plan

        # Attach trace id when available
        try:
            if trace and hasattr(trace, "id"):
                record["trace_id"] = getattr(trace, "id")
            elif trace and hasattr(trace, "trace_id"):
                record["trace_id"] = getattr(trace, "trace_id")
        except Exception:
            pass

        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")

        logger.debug("[PLANNER_LOG] Wrote planner interaction (%s) to %s", stage, path)
    except Exception as e:
        logger.debug("[PLANNER_LOG] Failed to write planner interaction: %s", e)
