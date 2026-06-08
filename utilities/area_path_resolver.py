"""
Area Path Resolver for Azure DevOps

Resolves free-text area path queries to canonical ADO AreaPath values
by fetching the classification tree and scoring candidates.

Enterprise Contract (Dynamic Resolution):
- Area paths are NEVER guessed or hardcoded
- Resolution uses live ADO classification nodes API
- All resolutions are logged to Langfuse for auditability
- See: docs/AREA_PATH_DYNAMIC_RESOLUTION_PLAN.md
"""

import re
import logging
from functools import lru_cache
from typing import Dict, List, Optional, Tuple
import difflib

logger = logging.getLogger(__name__)

API_VERSION = "7.1"

# ════════════════════════════════════════════════════════════════════════════════
# LANGFUSE TELEMETRY (Enterprise Contract - Auditability)
# ════════════════════════════════════════════════════════════════════════════════

def _log_area_path_resolution(
    operation: str,
    user_text: str,
    project: str,
    result: Dict,
    duration_ms: float = 0
) -> None:
    """
    Log area path resolution to Langfuse for observability.
    
    Args:
        operation: Operation type (resolve, fetch, build_wiql)
        user_text: User's input text
        project: ADO project name
        result: Resolution result dict
        duration_ms: Operation duration in milliseconds
    """
    try:
        from utilities.langfuse_client import create_span, finalize_span
        
        span = create_span(
            name=f"area_path_{operation}",
            span_type="resolution",
            input_data={
                "user_text": user_text,
                "project": project
            },
            metadata={
                "status": result.get("status", "unknown"),
                "confidence": result.get("confidence", 0),
                "choice": result.get("choice", ""),
                "candidates_count": len(result.get("top_matches", [])),
                "operation": operation,
                "dynamic_resolution": True  # Enterprise Contract flag
            }
        )
        
        if span:
            finalize_span(
                span,
                output_data=result,
                status="success" if result.get("status") in ("ok", "likely") else "ambiguous",
                metadata={"duration_ms": duration_ms}
            )
            
    except Exception as e:
        logger.debug(f"[AREA_PATH] Langfuse logging failed (non-critical): {e}")


def normalize_text(text: str) -> str:
    """Normalize text for comparison: lowercase, collapse separators, remove punctuation."""
    if not text:
        return ""
    text = text.lower().strip()
    # Replace path separators with space
    text = re.sub(r"[\\/>\-]", " ", text)
    # Remove punctuation except spaces
    text = re.sub(r"[^\w\s]", "", text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text)
    return text


def tokenize(text: str) -> set:
    """Split normalized text into tokens."""
    return set(normalize_text(text).split())


def flatten_classification_nodes(node: Dict, prefix: str = "", project_prefix: bool = False) -> List[str]:
    """
    Recursively flatten classification node tree into list of full paths.
    
    Args:
        node: Classification node from ADO API
        prefix: Current path prefix
        project_prefix: Whether to include project name in path
        
    Returns:
        List of canonical full paths
    """
    name = node.get("name", "")
    if not name:
        return []
    
    # Build full path
    if prefix:
        path = f"{prefix}\\{name}"
    else:
        path = name
    
    paths = [path]
    
    # Recurse into children
    for child in node.get("children", []):
        paths.extend(flatten_classification_nodes(child, path, project_prefix))
    
    return paths


@lru_cache(maxsize=32)
def fetch_area_paths(org: str, project: str, pat: str) -> List[str]:
    """
    Fetch all area paths for a project from ADO classification nodes API.
    
    Args:
        org: ADO organization name
        project: Project name
        pat: Personal access token
        
    Returns:
        List of canonical area paths
    """
    import requests
    
    url = f"https://dev.azure.com/{org}/{project}/_apis/wit/classificationnodes?$depth=10&api-version={API_VERSION}"
    auth = ("", pat)
    
    try:
        response = requests.get(url, auth=auth, timeout=30)
        response.raise_for_status()
        # The response has 'count' and 'value' keys, 'value' is a list with area and iteration nodes
        data = response.json()
        # Find the 'areas' node in the value list
        root = None
        for node in data.get('value', []):
            if node.get('structureType') == 'area':
                root = node
                break
        if not root:
            return []
        
        # Flatten tree starting from children (skip root project node)
        paths = []
        for child in root.get("children", []):
            paths.extend(flatten_classification_nodes(child, project))
        
        logger.info(f"Fetched {len(paths)} area paths for project {project}")
        return paths
        
    except Exception as e:
        logger.error(f"Failed to fetch area paths: {e}")
        return []


def score_candidate(user_text: str, candidate_path: str) -> float:
    """
    Score how well a candidate path matches the user's text.
    
    Scoring components:
    - Exact match: 100 points
    - Token overlap: up to 80 points
    - Starts-with bonus: 5 points
    - Fuzzy similarity: up to 15 points
    
    Args:
        user_text: User's input text
        candidate_path: Canonical area path from ADO
        
    Returns:
        Score from 0-100
    """
    user_norm = normalize_text(user_text)
    cand_norm = normalize_text(candidate_path)
    
    # Exact match
    if user_norm == cand_norm:
        return 100.0
    
    # Token overlap score
    user_tokens = tokenize(user_norm)
    cand_tokens = tokenize(cand_norm)
    
    if not user_tokens:
        return 0.0
    
    common_tokens = user_tokens & cand_tokens
    overlap_ratio = len(common_tokens) / len(user_tokens)
    overlap_score = overlap_ratio * 80
    
    # Starts-with bonus
    starts_bonus = 5 if cand_norm.startswith(user_norm) else 0
    
    # Fuzzy similarity using difflib
    seq_matcher = difflib.SequenceMatcher(None, user_norm, cand_norm)
    fuzzy_ratio = seq_matcher.ratio()
    fuzzy_score = fuzzy_ratio * 15
    
    total_score = overlap_score + starts_bonus + fuzzy_score
    return min(100.0, total_score)


def resolve_area_path(
    org: str,
    project: str,
    pat: str,
    user_text: str,
    top_k: int = 3
) -> Dict:
    """
    Resolve user's free-text area path to canonical ADO path(s).
    
    Decision thresholds:
    - score >= 95: Single exact match (high confidence)
    - 70 <= score < 95: Single likely match (medium confidence)
    - 50 <= score < 70: Multiple candidates (disambiguation needed)
    - score < 50: No good match (fallback to search)
    
    Enterprise Contract:
    - Uses LIVE ADO classification nodes API
    - NO static mappings or hardcoded values
    - Logs all resolutions to Langfuse
    
    Args:
        org: ADO organization
        project: Project name
        pat: Personal access token
        user_text: User's area path query
        top_k: Number of top candidates to return
        
    Returns:
        Dict with status, choice/top_matches, and confidence
    """
    import time
    start_time = time.time()
    
    if not user_text or not user_text.strip():
        result = {"status": "empty", "candidates": []}
        _log_area_path_resolution("resolve", user_text or "", project, result)
        return result
    
    # Fetch canonical paths (uses live ADO API)
    candidates = fetch_area_paths(org, project, pat)
    
    if not candidates:
        result = {
            "status": "no_candidates",
            "candidates": [],
            "query": user_text,
            "message": "Could not fetch area paths from ADO"
        }
        _log_area_path_resolution("resolve", user_text, project, result)
        return result
    
    # Score all candidates
    scored = [(path, score_candidate(user_text, path)) for path in candidates]
    scored.sort(key=lambda x: x[1], reverse=True)
    
    if not scored or scored[0][1] == 0:
        return {
            "status": "no_match",
            "query": user_text,
            "top_matches": scored[:top_k],
            "message": f"No area path matches '{user_text}'"
        }
    
    top_path, top_score = scored[0]
    
    # Decision based on score
    # Also consider the gap between top and second-best scores
    second_score = scored[1][1] if len(scored) > 1 else 0
    score_gap = top_score - second_score
    
    if top_score >= 95:
        logger.info(f"[DYNAMIC] Resolved '{user_text}' to '{top_path}' (exact, score={top_score:.1f})")
        result = {
            "status": "ok",
            "choice": top_path,
            "confidence": top_score / 100.0,
            "score": top_score
        }
        duration_ms = (time.time() - start_time) * 1000
        _log_area_path_resolution("resolve", user_text, project, result, duration_ms)
        return result
    elif top_score >= 70 or (top_score >= 50 and score_gap >= 20):
        # Accept if score >= 70, OR if score >= 50 AND significantly better than second
        logger.info(f"[DYNAMIC] Resolved '{user_text}' to '{top_path}' (likely, score={top_score:.1f}, gap={score_gap:.1f})")
        result = {
            "status": "likely",
            "choice": top_path,
            "confidence": top_score / 100.0,
            "score": top_score,
            "top_matches": scored[:top_k]
        }
        duration_ms = (time.time() - start_time) * 1000
        _log_area_path_resolution("resolve", user_text, project, result, duration_ms)
        return result
    elif top_score >= 50:
        logger.warning(f"[DYNAMIC] Ambiguous area path '{user_text}' - {len(scored[:top_k])} candidates - clarification needed")
        result = {
            "status": "ambiguous",
            "top_matches": scored[:top_k],
            "query": user_text,
            "message": f"Multiple area paths match '{user_text}'. Please be more specific."
        }
        duration_ms = (time.time() - start_time) * 1000
        _log_area_path_resolution("resolve", user_text, project, result, duration_ms)
        return result
    else:
        logger.warning(f"[DYNAMIC] No good match for '{user_text}' (best score={top_score:.1f})")
        result = {
            "status": "no_good_match",
            "top_matches": scored[:top_k],
            "query": user_text,
            "message": f"No clear area path match for '{user_text}'"
        }
        duration_ms = (time.time() - start_time) * 1000
        _log_area_path_resolution("resolve", user_text, project, result, duration_ms)
        return result


def build_area_path_wiql_clause(resolved: Dict) -> str:
    """
    Build WIQL WHERE clause for area path filtering based on resolution result.
    
    Args:
        resolved: Result from resolve_area_path()
        
    Returns:
        WIQL clause string (empty if no filter needed)
    """
    status = resolved.get("status")
    
    if status in ("ok", "likely"):
        # Single canonical path - use exact match or UNDER
        path = resolved.get("choice", "")
        if path:
            # Use UNDER to include child paths
            return f"[System.AreaPath] UNDER '{path}'"
    
    elif status == "ambiguous":
        # Multiple candidates - build OR clause
        top_matches = resolved.get("top_matches", [])
        if top_matches:
            paths = [f"[System.AreaPath] = '{path}'" for path, score in top_matches if score >= 60]
            if paths:
                return f"({' OR '.join(paths)})"
    
    return ""
