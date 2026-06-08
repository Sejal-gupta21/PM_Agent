"""Helpers for resolving Azure DevOps personal access tokens (PAT).

These utilities centralize how the app locates credentials so Streamlit,
CLI scripts, and background workers share the same lookup order.
"""

import os
from typing import Mapping, Optional, Tuple

# Preference order for environment variables that may contain the PAT.
_PAT_ENV_PRIORITY: Tuple[str, ...] = ("ADO_PAT", "ADO_MCP_AUTH_TOKEN")


def get_pat(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Return the first non-empty PAT from config or environment variables."""
    # First try config
    try:
        from config import config as app_config
        pat = app_config.ado_pat
        if pat and pat.strip():
            return pat.strip()
    except Exception:
        pass
    
    # Fallback to environment if provided
    source_env = env if env is not None else {}
    for name in _PAT_ENV_PRIORITY:
        value = source_env.get(name, "").strip()
        if value:
            return value
    return None


def get_pat_with_source(env: Optional[Mapping[str, str]] = None) -> Tuple[Optional[str], Optional[str]]:
    """Return `(pat, source_var)` so callers can document which var supplied it."""
    # First try config
    try:
        from config import config as app_config
        pat = app_config.ado_pat
        if pat and pat.strip():
            return pat.strip(), "config.yaml"
    except Exception:
        pass
    
    # Fallback to environment if provided
    source_env = env if env is not None else {}
    for name in _PAT_ENV_PRIORITY:
        value = source_env.get(name, "").strip()
        if value:
            return value, name
    return None, None
