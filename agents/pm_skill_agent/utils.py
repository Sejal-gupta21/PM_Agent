"""
Utility functions for PM Skills Agent.
"""

import os
import re
import logging
from typing import Dict, Any, Optional, List
from datetime import datetime

logger = logging.getLogger("pm_skill_agent.utils")


def parse_email_recipients(text: str) -> List[str]:
    """
    Extract email addresses from text.
    
    Args:
        text: Text that may contain email addresses
        
    Returns:
        List of extracted email addresses
    """
    pattern = r"[\w.+-]+@[\w-]+\.[\w.-]+"
    return re.findall(pattern, text)


def format_work_item_id(wi_id: Any) -> str:
    """Format work item ID as string."""
    if isinstance(wi_id, int):
        return str(wi_id)
    return str(wi_id) if wi_id else ""


def safe_get_nested(data: Dict, *keys, default=None):
    """
    Safely get nested dictionary value.
    
    Args:
        data: Dictionary to traverse
        *keys: Keys to traverse
        default: Default value if not found
        
    Returns:
        Value at path or default
    """
    current = data
    for key in keys:
        if isinstance(current, dict):
            current = current.get(key)
        else:
            return default
        if current is None:
            return default
    return current


def truncate_text(text: str, max_length: int = 100, suffix: str = "...") -> str:
    """
    Truncate text to max length with suffix.
    
    Args:
        text: Text to truncate
        max_length: Maximum length including suffix
        suffix: Suffix to add if truncated
        
    Returns:
        Truncated text
    """
    if not text or len(text) <= max_length:
        return text
    return text[:max_length - len(suffix)] + suffix


def format_date(date_str: str, output_format: str = "%Y-%m-%d") -> str:
    """
    Parse and reformat a date string.
    
    Args:
        date_str: Date string in ISO format
        output_format: Desired output format
        
    Returns:
        Formatted date string or original if parsing fails
    """
    if not date_str:
        return ""
    try:
        # Parse ISO format with optional timezone
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.strftime(output_format)
    except Exception:
        return date_str


def normalize_skill_name(name: str) -> str:
    """
    Normalize skill name to standard format.
    
    Args:
        name: Skill name with possible variations
        
    Returns:
        Normalized skill name
    """
    # Convert to lowercase and replace spaces/hyphens with underscores
    normalized = name.lower().strip()
    normalized = re.sub(r"[\s-]+", "_", normalized)
    return normalized


def build_wiql_date_filter(days: int) -> str:
    """
    Build WIQL date filter for last N days.
    
    Args:
        days: Number of days to look back
        
    Returns:
        WIQL date condition string
    """
    return f"[System.CreatedDate] >= @Today - {days}"


def validate_email_address(email: str) -> bool:
    """
    Validate email address format.
    
    Args:
        email: Email address to validate
        
    Returns:
        True if valid format
    """
    pattern = r"^[\w.+-]+@[\w-]+\.[\w.-]+$"
    return bool(re.match(pattern, email))


def get_project_from_context(params: Dict[str, Any]) -> str:
    """
    Get project name from params or config.
    
    Args:
        params: Parameters dict that may contain project
        
    Returns:
        Project name
    """
    from config import config
    return params.get("project") or config.ado_project


def merge_params_with_defaults(
    params: Dict[str, Any],
    defaults: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Merge parameters with defaults (params take precedence).
    
    Args:
        params: User-provided parameters
        defaults: Default values
        
    Returns:
        Merged parameters
    """
    result = dict(defaults)
    result.update({k: v for k, v in params.items() if v is not None})
    return result
