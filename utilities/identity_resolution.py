# -*- coding: utf-8 -*-
"""
Identity Resolution Module - Robust person name resolution for ADO queries.

This module provides:
1. Person name resolution against ADO identities
2. Ambiguity handling (single match vs multiple matches)
3. Clarification flow for ambiguous names
4. Support for full names, first names, emails, and usernames

RESOLUTION RULES:
1. Full name (2+ words) or email/username format → proceed without clarification
2. First name only (single word) → may need clarification if multiple matches
3. No matches → return error message
4. Single match → use resolved identity
5. Multiple matches → ask for clarification (prefer full name, then email)

Usage:
    from utilities.identity_resolution import resolve_person_name, IdentityResolutionResult
    
    result = await resolve_person_name("John", mcp_connector)
    if result.needs_clarification:
        # Ask user for clarification
        print(result.clarification_message)
    elif result.resolved:
        # Use result.identity_email or result.identity_display_name
        print(f"Resolved to: {result.identity_email}")
"""

import re
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
from difflib import SequenceMatcher

logger = logging.getLogger(__name__)


@dataclass
class IdentityResolutionResult:
    """Result of identity resolution attempt."""
    resolved: bool = False
    identity_email: Optional[str] = None
    identity_display_name: Optional[str] = None
    identity_id: Optional[str] = None
    needs_clarification: bool = False
    clarification_message: Optional[str] = None
    error_message: Optional[str] = None
    match_count: int = 0
    all_matches: List[Dict[str, Any]] = field(default_factory=list)
    confidence: float = 0.0
    resolution_method: str = "none"  # "exact", "fuzzy", "single_match", "clarification_needed"


def normalize_name(name: str) -> str:
    """
    Normalize a name for comparison.
    
    Handles:
    - "John Smith" → "john smith"
    - "john.smith" → "john smith"
    - "john.smith@example.com" → "john smith"
    """
    if not name:
        return ""
    
    s = name.strip().lower()
    
    # Remove email domain if present
    if "@" in s:
        s = s.split("@")[0]
    
    # Replace dots, underscores, hyphens with spaces
    s = re.sub(r'[._-]+', ' ', s)
    
    # Collapse whitespace
    s = re.sub(r'\s+', ' ', s).strip()
    
    return s


def is_full_name_or_email(name: str) -> bool:
    """
    Check if name is a full name (2+ words), email, or username format.
    
    These formats don't need clarification:
    - "John Smith" (2 words)
    - "john.smith" (dot-separated)
    - "john.smith@example.com" (email)
    """
    if not name:
        return False
    
    name = name.strip()
    
    # Email format
    if "@" in name:
        return True
    
    # Dot-separated (username format like john.smith)
    if "." in name and not name.startswith(".") and not name.endswith("."):
        return True
    
    # Space-separated (full name)
    parts = name.split()
    if len(parts) >= 2:
        return True
    
    return False


def calculate_similarity(name1: str, name2: str) -> float:
    """Calculate similarity ratio between two names."""
    n1 = normalize_name(name1)
    n2 = normalize_name(name2)
    
    if n1 == n2:
        return 1.0
    
    # Check if one is a substring of the other
    if n1 in n2 or n2 in n1:
        shorter = min(len(n1), len(n2))
        longer = max(len(n1), len(n2))
        if longer > 0:
            ratio = shorter / longer
            return 0.75 + (ratio * 0.20)
    
    # Check if parts match (first name / last name)
    parts1 = n1.split()
    parts2 = n2.split()
    
    # Single word matching any word in the other name
    if len(parts1) == 1 and len(parts2) >= 1:
        if parts1[0] in parts2:
            return 0.85
    if len(parts2) == 1 and len(parts1) >= 1:
        if parts2[0] in parts1:
            return 0.85
    
    # Fuzzy match
    return SequenceMatcher(None, n1, n2).ratio()


def find_best_matches(
    query_name: str,
    identities: List[Dict[str, Any]],
    threshold: float = 0.6
) -> List[Tuple[Dict[str, Any], float]]:
    """
    Find best matching identities for a query name.
    
    Args:
        query_name: Name to search for
        identities: List of identity dicts from ADO
        threshold: Minimum similarity threshold
        
    Returns:
        List of (identity, score) tuples sorted by score descending
    """
    matches = []
    query_normalized = normalize_name(query_name)
    
    for identity in identities:
        display_name = identity.get('displayName', '')
        unique_name = identity.get('uniqueName', '')
        
        # Calculate scores for display name and unique name
        display_score = calculate_similarity(query_name, display_name) if display_name else 0
        unique_score = calculate_similarity(query_name, unique_name) if unique_name else 0
        
        best_score = max(display_score, unique_score)
        
        if best_score >= threshold:
            matches.append((identity, best_score))
    
    # Sort by score descending
    matches.sort(key=lambda x: x[1], reverse=True)
    
    return matches


async def resolve_person_name(
    person_name: str,
    mcp_connector: Any,
    project: str = None,
    allow_clarification: bool = True
) -> IdentityResolutionResult:
    """
    Resolve a person name to an ADO identity.
    
    Args:
        person_name: The name to resolve (first name, full name, email, or username)
        mcp_connector: MCP connector for ADO API calls
        project: Optional project name for context
        allow_clarification: If True, will ask for clarification on ambiguous names
        
    Returns:
        IdentityResolutionResult with resolution status and details
    """
    result = IdentityResolutionResult()
    
    if not person_name or not person_name.strip():
        result.error_message = "No person name provided."
        return result
    
    person_name = person_name.strip()
    is_likely_unique = is_full_name_or_email(person_name)
    
    logger.info(f"[IDENTITY_RESOLUTION] Resolving '{person_name}' (is_likely_unique={is_likely_unique})")
    
    try:
        # Use identity cache if available
        try:
            from agents.pm_agent.identity_cache import get_identity_cache, parse_identity_response
            cache = get_identity_cache()
            cached = cache.get(person_name)
            if cached:
                logger.debug(f"[IDENTITY_RESOLUTION] Cache hit for '{person_name}'")
                identities = cached
            else:
                # Call ADO API
                response = await mcp_connector.call_tool("core_get_identity_ids", {
                    "searchFilter": person_name
                })
                identities = parse_identity_response(response)
                cache.set(person_name, identities)
        except ImportError:
            # Fallback without cache
            response = await mcp_connector.call_tool("core_get_identity_ids", {
                "searchFilter": person_name
            })
            identities = _parse_identity_response_simple(response)
        
        result.match_count = len(identities)
        result.all_matches = identities
        
        if not identities:
            # No matches found
            result.resolved = False
            result.error_message = f"No user found matching '{person_name}' in Azure DevOps. Please check the spelling or provide the full name or email address."
            result.resolution_method = "no_match"
            logger.info(f"[IDENTITY_RESOLUTION] No matches for '{person_name}'")
            return result
        
        if len(identities) == 1:
            # Single match - use it
            identity = identities[0]
            result.resolved = True
            # Use 'or' instead of default to handle empty string values
            result.identity_email = identity.get('uniqueName') or identity.get('displayName') or ''
            result.identity_display_name = identity.get('displayName') or ''
            result.identity_id = identity.get('id', '')
            result.confidence = 0.95
            result.resolution_method = "single_match"
            logger.info(f"[IDENTITY_RESOLUTION] Single match: '{person_name}' → '{result.identity_email}'")
            return result
        
        # Multiple matches - check if we can auto-select
        # Find best matches based on similarity
        best_matches = find_best_matches(person_name, identities, threshold=0.7)
        
        if best_matches:
            top_match, top_score = best_matches[0]
            
            # If the top match is very high confidence and significantly better than second
            if top_score >= 0.95:
                result.resolved = True
                # Use 'or' instead of default to handle empty string values
                result.identity_email = top_match.get('uniqueName') or top_match.get('displayName') or ''
                result.identity_display_name = top_match.get('displayName') or ''
                result.identity_id = top_match.get('id', '')
                result.confidence = top_score
                result.resolution_method = "exact"
                logger.info(f"[IDENTITY_RESOLUTION] High-confidence match: '{person_name}' → '{result.identity_email}' (score={top_score:.2f})")
                return result
            
            # If query is a full name or email, use top match even with lower confidence
            if is_likely_unique and top_score >= 0.75:
                result.resolved = True
                # Use 'or' instead of default to handle empty string values
                result.identity_email = top_match.get('uniqueName') or top_match.get('displayName') or ''
                result.identity_display_name = top_match.get('displayName') or ''
                result.identity_id = top_match.get('id', '')
                result.confidence = top_score
                result.resolution_method = "fuzzy"
                logger.info(f"[IDENTITY_RESOLUTION] Full name match: '{person_name}' → '{result.identity_email}' (score={top_score:.2f})")
                return result
        
        # Need clarification
        if allow_clarification:
            result.needs_clarification = True
            result.resolution_method = "clarification_needed"
            
            # Build clarification message
            match_list = []
            for identity in identities[:5]:  # Show up to 5 matches
                display = identity.get('displayName', 'Unknown')
                email = identity.get('uniqueName', '')
                if email:
                    match_list.append(f"  • **{display}** ({email})")
                else:
                    match_list.append(f"  • **{display}**")
            
            more_text = f"\n  • ... and {len(identities) - 5} more" if len(identities) > 5 else ""
            
            result.clarification_message = (
                f"I found {len(identities)} users matching '{person_name}':\n"
                f"{chr(10).join(match_list)}{more_text}\n\n"
                f"Please specify the **full name** or **email address** of the user you mean."
            )
            logger.info(f"[IDENTITY_RESOLUTION] Clarification needed: {len(identities)} matches for '{person_name}'")
        else:
            # Use top match if available (fallback when clarification disabled)
            if best_matches:
                top_match, top_score = best_matches[0]
                result.resolved = True
                result.identity_email = top_match.get('uniqueName', top_match.get('displayName', ''))
                result.identity_display_name = top_match.get('displayName', '')
                result.identity_id = top_match.get('id', '')
                result.confidence = top_score * 0.8  # Lower confidence due to ambiguity
                result.resolution_method = "fuzzy_fallback"
            else:
                result.error_message = f"Multiple users found for '{person_name}' but no good match could be determined."
        
        return result
        
    except Exception as e:
        logger.exception(f"[IDENTITY_RESOLUTION] Error resolving '{person_name}': {e}")
        result.error_message = f"Error looking up user '{person_name}': {str(e)}"
        return result


def _parse_identity_response_simple(response: Any) -> List[Dict[str, Any]]:
    """Simple parser for identity response (fallback without cache module)."""
    import json
    
    if not response:
        return []
    
    if isinstance(response, str):
        if response.lower().startswith("no identit"):
            return []
        try:
            parsed = json.loads(response)
        except json.JSONDecodeError:
            return []
    else:
        parsed = response
    
    if isinstance(parsed, dict):
        if 'displayName' in parsed or 'uniqueName' in parsed:
            return [parsed]
        return parsed.get('value', parsed.get('identities', []))
    elif isinstance(parsed, list):
        return parsed
    
    return []


def format_identity_for_filter(identity: Dict[str, Any]) -> str:
    """
    Get the best identifier to use for assignedTo filter.
    
    Priority: uniqueName (email) > displayName
    """
    if identity.get('uniqueName'):
        return identity['uniqueName']
    return identity.get('displayName', '')


async def check_person_needs_clarification(
    person_name: str,
    mcp_connector: Any
) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Quick check if a person name needs clarification.
    
    Returns:
        Tuple of (needs_clarification, clarification_message, resolved_email)
    """
    result = await resolve_person_name(person_name, mcp_connector)
    
    if result.needs_clarification:
        return True, result.clarification_message, None
    elif result.error_message:
        return True, result.error_message, None
    elif result.resolved:
        return False, None, result.identity_email
    else:
        return True, f"Could not resolve user '{person_name}'", None
