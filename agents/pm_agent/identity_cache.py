"""
LRU Identity Cache for PM Agent.

Caches identity lookups (core_get_identity_ids) to:
1. Reduce repeated MCP calls for the same person token
2. Improve determinism (same input → same cached output)
3. Support TTL-based expiration

IDENTITY NORMALIZATION RULES:
All these formats are treated as equivalent (examples use a generic placeholder):
- "First Last" (display name with space)
- "first.last" (ADO username with dot)
- "first.last@example.com" (full email)
- "first last" (lowercase with space)

Usage:
    from .identity_cache import IdentityCache
    cache = IdentityCache()
    result = cache.get("first.last")  # Returns cached result or None
    cache.set("first", [{"displayName": "First Last", "uniqueName": "first.last@example.com"}])
"""

import re
import time
import logging
from typing import Any, Dict, List, Optional, Tuple
from collections import OrderedDict
from threading import Lock

logger = logging.getLogger(__name__)


def normalize_identity_token(token: str) -> str:
    """
    Normalize an identity token to a canonical form for consistent matching.
    
    This function ensures that all these formats normalize to the same key:
    - "John Smith" → "john smith"
    - "john.smith" → "john smith"
    - "john.smith@example.com" → "john smith"
    - "JOHN SMITH" → "john smith"
    
    Args:
        token: Raw identity token (name, username, or email)
        
    Returns:
        Normalized canonical string
    """
    if not token:
        return ""
    
    s = token.strip().lower()
    
    # If it's an email, extract the local part (before @)
    if '@' in s:
        s = s.split('@')[0]
    
    # Replace dots, underscores, and hyphens with spaces
    s = re.sub(r'[._-]+', ' ', s)
    
    # Collapse multiple spaces
    s = re.sub(r'\s+', ' ', s).strip()
    
    return s


def identity_matches(token1: str, token2: str, threshold: float = 0.85) -> Tuple[bool, float]:
    """
    Check if two identity tokens refer to the same person.
    
    Uses normalized comparison first, then fuzzy matching as fallback.
    
    Args:
        token1: First identity token
        token2: Second identity token
        threshold: Minimum similarity score for fuzzy match (0.0-1.0)
        
    Returns:
        Tuple of (is_match, similarity_score)
    """
    from difflib import SequenceMatcher
    
    norm1 = normalize_identity_token(token1)
    norm2 = normalize_identity_token(token2)
    
    # Exact match after normalization
    if norm1 == norm2:
        return True, 1.0
    
    # Check if one token matches a word in the other (first/last name match)
    parts1 = norm1.split()
    parts2 = norm2.split()
    
    # Single word matching a part of multi-word name
    # Only match if the single word is EXACTLY in the other name's parts
    if len(parts1) == 1 and len(parts2) >= 2:
        if parts1[0] in parts2:  # Exact word match (must be exact word, not substring)
            return True, 0.90
    if len(parts2) == 1 and len(parts1) >= 2:
        if parts2[0] in parts1:  # Exact word match
            return True, 0.90
    
    # Check if multi-part names share BOTH first and last name (for full name matching)
    if len(parts1) >= 2 and len(parts2) >= 2:
        # Check if first and last name match (order matters for full names)
        if parts1 == parts2:
            return True, 1.0
        # Check for first.last format matching first last format
        if parts1[0] == parts2[0] and parts1[-1] == parts2[-1]:
            return True, 0.95
    
    # Fuzzy match using SequenceMatcher - require higher threshold for safety
    similarity = SequenceMatcher(None, norm1, norm2).ratio()
    
    # For short tokens, require exact match (no fuzzy)
    if len(norm1) <= 5 or len(norm2) <= 5:
        # Short tokens - only match if very high similarity
        return similarity >= 0.95, similarity
    
    return similarity >= threshold, similarity


def get_search_variants(token: str) -> List[str]:
    """
    Get search variants for a person token string (for ADO identity lookup).
    
    Generates multiple search strings to try when looking up an identity:
    - Original token
    - Normalized (spaces)
    - With dots
    - First name only
    
    Args:
        token: Raw person token (name, email, username)
        
    Returns:
        List of search variant strings to try
    """
    variants = []
    seen = set()
    
    def add_variant(v: str) -> None:
        if v and v.lower() not in seen:
            variants.append(v)
            seen.add(v.lower())
    
    # Original token
    add_variant(token)
    
    # Normalized (with spaces)
    normalized = normalize_identity_token(token)
    add_variant(normalized)
    
    # With dots instead of spaces
    parts = normalized.split()
    if len(parts) >= 2:
        add_variant('.'.join(parts))
    
    # First name only (for broader search)
    if parts:
        add_variant(parts[0])
    
    return variants


def get_all_identity_variants(identity: Dict[str, Any]) -> List[str]:
    """
    Get all possible search variants for an identity dict.
    
    This generates multiple search strings that could match the identity,
    useful for comprehensive caching and lookup.
    
    Args:
        identity: Identity dict with displayName, uniqueName, etc.
        
    Returns:
        List of normalized variant strings
    """
    variants = set()
    
    display_name = identity.get('displayName', '')
    unique_name = identity.get('uniqueName', '')
    
    if display_name:
        variants.add(normalize_identity_token(display_name))
        # Also add first name only
        parts = normalize_identity_token(display_name).split()
        if parts:
            variants.add(parts[0])
    
    if unique_name:
        variants.add(normalize_identity_token(unique_name))
    
    return list(variants)


class IdentityCache:
    """Thread-safe LRU cache with TTL for identity lookups."""
    
    def __init__(self, max_size: int = 256, ttl_seconds: int = 600):
        """
        Initialize the identity cache.
        
        Args:
            max_size: Maximum number of entries to cache
            ttl_seconds: Time-to-live for each cache entry in seconds
        """
        self._cache: OrderedDict[str, Dict[str, Any]] = OrderedDict()
        self._max_size = max_size
        self._ttl_seconds = ttl_seconds
        self._lock = Lock()
        logger.debug(f"[IDENTITY_CACHE] Initialized with max_size={max_size}, ttl={ttl_seconds}s")
    
    def _normalize_key(self, person_token: str) -> str:
        """Normalize the cache key using consistent identity normalization."""
        return normalize_identity_token(person_token)
    
    def get(self, person_token: str) -> Optional[List[Dict[str, Any]]]:
        """
        Get cached identity lookup result.
        
        Args:
            person_token: The person name/token to look up
            
        Returns:
            List of identity dicts if cached and not expired, None otherwise
        """
        key = self._normalize_key(person_token)
        
        with self._lock:
            if key not in self._cache:
                logger.debug(f"[IDENTITY_CACHE] MISS for '{person_token}'")
                return None
            
            entry = self._cache[key]
            
            # Check TTL
            if time.time() - entry['timestamp'] > self._ttl_seconds:
                # Entry expired, remove it
                del self._cache[key]
                logger.debug(f"[IDENTITY_CACHE] EXPIRED for '{person_token}'")
                return None
            
            # Move to end (most recently used)
            self._cache.move_to_end(key)
            logger.debug(f"[IDENTITY_CACHE] HIT for '{person_token}' ({len(entry['identities'])} identities)")
            return entry['identities']
    
    def set(self, person_token: str, identities: List[Dict[str, Any]]) -> None:
        """
        Cache identity lookup result.
        
        Args:
            person_token: The person name/token that was looked up
            identities: List of identity dicts from core_get_identity_ids
        """
        key = self._normalize_key(person_token)
        
        with self._lock:
            # Remove oldest if at capacity
            while len(self._cache) >= self._max_size:
                oldest_key = next(iter(self._cache))
                del self._cache[oldest_key]
                logger.debug(f"[IDENTITY_CACHE] Evicted oldest entry: '{oldest_key}'")
            
            self._cache[key] = {
                'identities': identities,
                'timestamp': time.time()
            }
            logger.debug(f"[IDENTITY_CACHE] SET for '{person_token}' ({len(identities)} identities)")
    
    def clear(self) -> None:
        """Clear all cached entries."""
        with self._lock:
            self._cache.clear()
            logger.debug("[IDENTITY_CACHE] Cleared all entries")
    
    def stats(self) -> Dict[str, Any]:
        """Get cache statistics."""
        with self._lock:
            return {
                'size': len(self._cache),
                'max_size': self._max_size,
                'ttl_seconds': self._ttl_seconds
            }


# Global singleton instance
_identity_cache: Optional[IdentityCache] = None


def get_identity_cache() -> IdentityCache:
    """Get or create the global identity cache instance."""
    global _identity_cache
    if _identity_cache is None:
        from config import config
        _identity_cache = IdentityCache(
            max_size=config.pm_identity_cache_size,
            ttl_seconds=config.pm_identity_cache_ttl
        )
    return _identity_cache


def parse_identity_response(response: Any) -> List[Dict[str, Any]]:
    """
    Parse the response from core_get_identity_ids into a list of identity dicts.
    
    Args:
        response: Raw response from MCP tool (string JSON or dict)
        
    Returns:
        List of identity dicts with keys: displayName, uniqueName, descriptor, id
    """
    import json
    
    if not response:
        return []
    
    # Handle string response
    if isinstance(response, str):
        response_lower = response.strip().lower()
        # Handle "no identities found" type responses
        if response_lower.startswith("no identity") or response_lower.startswith("no identities"):
            return []
        try:
            parsed = json.loads(response)
        except json.JSONDecodeError:
            logger.debug(f"[IDENTITY_CACHE] Failed to parse identity response as JSON: {response[:100]}")
            return []
    else:
        parsed = response
    
    # Extract identity list
    identities = []
    
    if isinstance(parsed, dict):
        # First check if this is a single identity dict (has displayName or uniqueName directly)
        if 'displayName' in parsed or 'uniqueName' in parsed or 'display' in parsed or 'name' in parsed:
            # Single identity as dict
            identities = [parsed]
        else:
            # Handle {"value": [...]} format
            items = parsed.get('value', parsed.get('identities', []))
            if isinstance(items, list):
                identities = items
    elif isinstance(parsed, list):
        identities = parsed
    
    # Normalize identity dicts
    result = []
    for ident in identities:
        if not isinstance(ident, dict):
            continue
        
        normalized = {
            'displayName': ident.get('displayName') or ident.get('display') or ident.get('name') or '',
            'uniqueName': ident.get('uniqueName') or ident.get('email') or ident.get('mailAddress') or '',
            'descriptor': ident.get('descriptor') or ident.get('subjectDescriptor') or '',
            'id': ident.get('id') or ident.get('originId') or ''
        }
        
        # Only include if we have at least displayName or uniqueName
        if normalized['displayName'] or normalized['uniqueName']:
            result.append(normalized)
    
    return result


def get_canonical_identity_token(identity: Dict[str, Any]) -> str:
    """
    Get the best canonical token to use for assignedTo filters.
    
    Priority: uniqueName (email) > displayName (if email) > descriptor > displayName
    
    Args:
        identity: Identity dict with displayName, uniqueName, descriptor, id
        
    Returns:
        Best canonical token string
    """
    # Prefer uniqueName (usually email format)
    if identity.get('uniqueName'):
        return identity['uniqueName']
    
    # Check if displayName is an email (common in Azure DevOps responses)
    display_name = identity.get('displayName', '')
    if display_name and '@' in display_name:
        return display_name
    
    # Fallback to descriptor
    if identity.get('descriptor'):
        return identity['descriptor']
    
    # Last resort: displayName
    return display_name


def format_identity_for_clarification(identity: Dict[str, Any]) -> str:
    """
    Format an identity for display in clarification messages.
    
    Args:
        identity: Identity dict
        
    Returns:
        Human-readable string like "John Smith (john.smith@example.com)"
    """
    display = identity.get('displayName', '')
    unique = identity.get('uniqueName', '')
    
    if display and unique:
        return f"{display} ({unique})"
    elif display:
        return display
    elif unique:
        return unique
    else:
        return identity.get('descriptor', 'Unknown')


# =============================================================================
# CLARIFICATION CONTEXT
# Stores pending identity clarification state for multi-turn conversations
# =============================================================================

class ClarificationContext:
    """
    Stores pending identity clarification context for multi-turn resolution.
    
    When the agent asks "Which person did you mean?" and provides a numbered list,
    this stores the context so the next user message (e.g., "1" or "deepak.yadav@...")
    can be resolved and the original query re-executed.
    
    This is instance-scoped (per conversation/session), not global.
    """
    
    def __init__(self):
        self._pending: Optional[Dict[str, Any]] = None
        self._lock = Lock()
        self._created_at: Optional[float] = None
        self._ttl_seconds = 300  # 5 minute TTL for clarification context
    
    def set_pending(
        self,
        original_query: str,
        original_tool: str,
        original_args: Dict[str, Any],
        person_token: str,
        candidates: List[Dict[str, Any]],
        context: Optional[Dict[str, Any]] = None
    ) -> None:
        """
        Store pending clarification context.
        
        Args:
            original_query: The original user query that triggered clarification
            original_tool: The tool that was about to be called
            original_args: The args for the tool call
            person_token: The ambiguous person token
            candidates: List of identity candidates shown to user
            context: Additional context (project, etc.)
        """
        with self._lock:
            self._pending = {
                'original_query': original_query,
                'original_tool': original_tool,
                'original_args': original_args,
                'person_token': person_token,
                'candidates': candidates,
                'context': context or {}
            }
            self._created_at = time.time()
            logger.info(f"[CLARIFICATION] Stored pending context for '{person_token}' with {len(candidates)} candidates")
    
    def get_pending(self) -> Optional[Dict[str, Any]]:
        """
        Get the pending clarification context if valid.
        
        Returns:
            Pending context dict or None if expired/not set
        """
        with self._lock:
            if self._pending is None:
                return None
            
            # Check TTL
            if self._created_at and (time.time() - self._created_at > self._ttl_seconds):
                self._pending = None
                self._created_at = None
                logger.debug("[CLARIFICATION] Pending context expired")
                return None
            
            return self._pending
    
    def clear(self) -> None:
        """Clear the pending clarification context."""
        with self._lock:
            self._pending = None
            self._created_at = None
            logger.debug("[CLARIFICATION] Cleared pending context")
    
    def resolve_clarification(self, user_response: str) -> Optional[Dict[str, Any]]:
        """
        Attempt to resolve a clarification response from the user.
        
        Handles:
        - Numeric selection: "1", "2", etc.
        - Full email: "deepak.yadav@company.com"
        - Full name: "Deepak Yadav"
        
        Args:
            user_response: The user's clarification response
            
        Returns:
            Resolved identity dict, or None if not resolvable
        """
        pending = self.get_pending()
        if not pending:
            return None
        
        candidates = pending.get('candidates', [])
        if not candidates:
            return None
        
        response = user_response.strip()
        
        # Try numeric selection first
        if response.isdigit():
            idx = int(response) - 1  # 1-indexed to 0-indexed
            if 0 <= idx < len(candidates):
                resolved = candidates[idx]
                logger.info(f"[CLARIFICATION] Resolved by number: {idx + 1} -> {resolved.get('displayName', '')}")
                return resolved
        
        # Try matching by email or display name
        response_lower = response.lower()
        
        for candidate in candidates:
            display = candidate.get('displayName', '').lower()
            unique = candidate.get('uniqueName', '').lower()
            
            # Exact email match
            if unique and response_lower == unique:
                logger.info(f"[CLARIFICATION] Resolved by exact email: {unique}")
                return candidate
            
            # Exact display name match
            if display and response_lower == display:
                logger.info(f"[CLARIFICATION] Resolved by exact display name: {display}")
                return candidate
            
            # Email contains (user might provide partial email)
            if '@' in response_lower and unique and response_lower in unique:
                logger.info(f"[CLARIFICATION] Resolved by partial email match: {response_lower} in {unique}")
                return candidate
        
        # Fuzzy match using identity_matches
        for candidate in candidates:
            display = candidate.get('displayName', '')
            unique = candidate.get('uniqueName', '')
            
            is_display_match, display_score = identity_matches(response, display, threshold=0.9)
            is_unique_match, unique_score = identity_matches(response, unique, threshold=0.9)
            
            if is_display_match or is_unique_match:
                logger.info(f"[CLARIFICATION] Resolved by fuzzy match: {display} (score: {max(display_score, unique_score):.2f})")
                return candidate
        
        logger.debug(f"[CLARIFICATION] Could not resolve '{user_response}' from {len(candidates)} candidates")
        return None


# Global clarification context store (per-session contexts)
_clarification_contexts: Dict[str, ClarificationContext] = {}
_contexts_lock = Lock()


def get_clarification_context(session_id: str = "default") -> ClarificationContext:
    """
    Get or create a clarification context for a session.
    
    Args:
        session_id: Unique session/conversation ID
        
    Returns:
        ClarificationContext for the session
    """
    with _contexts_lock:
        if session_id not in _clarification_contexts:
            _clarification_contexts[session_id] = ClarificationContext()
        return _clarification_contexts[session_id]
