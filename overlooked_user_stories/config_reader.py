"""
Config Reader Module - Load Configuration from config.yaml

This module provides functions to read email recipients from config.yaml.

Architectural Rule: This module ONLY contains config-reading logic.
"""
from __future__ import annotations
import sys
import yaml
from pathlib import Path
from typing import List, Optional


def load_email_recipients() -> List[str]:
    """
    Load allowed email recipients from config.yaml.
    
    Returns:
        List of email addresses from reportEmailRecipients
    
    Raises:
        RuntimeError: If config.yaml cannot be read or is malformed
    """
    config_path = Path(__file__).resolve().parent.parent / "config.yaml"
    
    if not config_path.exists():
        raise RuntimeError(f"Configuration file not found: {config_path}")
    
    try:
        with config_path.open("r", encoding="utf-8") as fh:
            config = yaml.safe_load(fh)
        
        recipients = config.get("reportEmailRecipients", [])
        
        if not isinstance(recipients, list):
            raise RuntimeError("reportEmailRecipients must be a list in config.yaml")
        
        # Filter out empty strings and validate email format
        valid_recipients = [r.strip() for r in recipients if r and r.strip()]
        
        if not valid_recipients:
            raise RuntimeError("No valid email recipients found in config.yaml reportEmailRecipients")
        
        return valid_recipients
    
    except yaml.YAMLError as e:
        raise RuntimeError(f"Failed to parse config.yaml: {e}")
    except Exception as e:
        raise RuntimeError(f"Error reading config.yaml: {e}")


def validate_recipient(email: str, allowed_recipients: List[str]) -> bool:
    """
    Validate that an email is in the allowed recipients list.
    
    Args:
        email: Email address to validate
        allowed_recipients: List of allowed email addresses
    
    Returns:
        True if email is allowed, False otherwise
    """
    if not email or not email.strip():
        return False
    
    email_lower = email.strip().lower()
    return any(r.strip().lower() == email_lower for r in allowed_recipients)
