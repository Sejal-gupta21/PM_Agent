"""
Shared utilities for LLM operations across all agents.

This module provides common functionality used by:
- planner.py (tool selection and planning)
- synthesizer.py (result synthesis)
- instructions.py (instruction loading)
- Any other LLM operation in the utilities layer

Functions:
- sanitize_json_text(): Clean malformed JSON from LLM outputs
- run_in_executor_with_context(): Run blocking calls with Langfuse context preservation
- extract_work_item_metadata(): Extract ID/title/state from work item objects
- get_shared_thread_executor(): Get shared ThreadPoolExecutor instance
- find_instruction_file(): Search for instruction files in multiple locations
- load_instruction_file(): Load instruction file with fallback

CRITICAL: This module has NO dependencies on agent-specific code.
It only depends on standard library and utilities/langfuse_client.
"""

import re
import logging
import asyncio
import contextvars
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Dict, Any, Tuple, List

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# THREAD POOL MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

# Shared thread pool for all async operations (single instance, reused)
_shared_executor: Optional[ThreadPoolExecutor] = None


def get_shared_thread_executor(max_workers: int = 2) -> ThreadPoolExecutor:
    """
    Get or create a shared ThreadPoolExecutor instance.
    
    Ensures only one executor is created across the entire utilities.llm module
    to avoid resource leaks and excessive threads.
    
    Args:
        max_workers: Maximum number of worker threads (default: 2)
    
    Returns:
        ThreadPoolExecutor: Shared executor instance
    """
    global _shared_executor
    if _shared_executor is None:
        _shared_executor = ThreadPoolExecutor(max_workers=max_workers)
        logger.debug("[LLM_UTILS] Initialized shared ThreadPoolExecutor with max_workers=%d", max_workers)
    return _shared_executor


async def run_in_executor_with_context(func, *args, executor=None) -> Any:
    """
    Run a blocking function in a thread pool while preserving contextvars.
    
    Critical for maintaining Langfuse trace context across threads when
    calling blocking APIs like OpenAI.
    
    Args:
        func: Blocking function to run
        *args: Arguments to pass to func
        executor: ThreadPoolExecutor to use (default: shared executor)
    
    Returns:
        Result of func(*args)
    """
    if executor is None:
        executor = get_shared_thread_executor()
    
    # Copy current context for the thread
    ctx = contextvars.copy_context()
    loop = asyncio.get_event_loop()
    
    try:
        # Run function in thread pool with copied context
        result = await loop.run_in_executor(
            executor,
            lambda: ctx.run(func, *args)
        )
        return result
    except Exception as e:
        logger.error("[LLM_UTILS] Executor failed: %s", e)
        raise


# ══════════════════════════════════════════════════════════════════════════════
# JSON SANITIZATION
# ══════════════════════════════════════════════════════════════════════════════

def sanitize_json_text(text: str) -> str:
    """
    Attempt to sanitize malformed JSON from LLM output.
    
    Handles common issues:
    - Markdown code blocks (```json ... ```)
    - Trailing commas before } or ]
    - Extra prose around JSON object
    
    Args:
        text: Raw text from LLM that should contain JSON
    
    Returns:
        str: Cleaned JSON string (or original if cleaning fails)
    """
    if not text:
        return text
    
    # Strip markdown code blocks if present
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    if text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()
    
    # Remove trailing commas before } or ]
    original_text = text
    text = re.sub(r',\s*([}\]])', r'\1', text)
    if text != original_text:
        logger.debug("[LLM_UTILS] Removed trailing commas from JSON")
    
    # Try to extract JSON object if there's prose around it
    match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text, re.DOTALL)
    if match:
        extracted = match.group(0)
        if extracted != text:
            text = extracted
            logger.debug("[LLM_UTILS] Extracted JSON object from prose")
    
    return text


# ══════════════════════════════════════════════════════════════════════════════
# WORK ITEM METADATA EXTRACTION
# ══════════════════════════════════════════════════════════════════════════════

def _find_key_recursive(d: Dict[str, Any], candidates: List[str]) -> Optional[Any]:
    """
    Recursively search for a key in a dict by checking multiple candidate names.
    
    Useful for defensive extraction when ADO response shapes vary.
    
    Args:
        d: Dictionary to search
        candidates: List of possible key names (case-insensitive)
    
    Returns:
        Value if found, None otherwise
    """
    if not isinstance(d, dict):
        return None
    
    # Direct match (case-insensitive)
    for key in d.keys():
        low = key.lower()
        for cand in candidates:
            if low == cand.lower():
                return d.get(key)
    
    # Recurse one level deep
    for v in d.values():
        if isinstance(v, dict):
            res = _find_key_recursive(v, candidates)
            if res is not None:
                return res
    
    return None


def extract_work_item_metadata(item_obj: Dict[str, Any]) -> Tuple[str, str, str]:
    """
    Extract ID, title, and state from a work item, PR, or other entity object.
    
    Handles multiple ADO response shapes defensively:
    - Work items: item['id'], item['fields']['System.Id']
    - Pull requests: item['pullRequestId'], item['codeReviewId']
    - Repositories: item['id'] (GUID), item['name']
    - Iterations: item['id'], item['name']
    - Generic: Falls back to common field names
    
    Args:
        item_obj: Entity dict from ADO (work item, PR, repo, etc.)
    
    Returns:
        Tuple[id_str, title_str, state_str] - All converted to strings, with "?" as fallback
    """
    item_id = None
    item_title = None
    item_state = None
    
    # Normalize one-level wrappers
    if isinstance(item_obj, dict) and len(item_obj) == 1:
        only = next(iter(item_obj.values()))
        if isinstance(only, dict):
            item_obj = only
    
    if isinstance(item_obj, dict):
        # Get fields dict if present
        fields = item_obj.get("fields")
        if not isinstance(fields, dict):
            fields = {}
        
        # ══════════════════════════════════════════════════════════════════
        # ENTITY TYPE DETECTION: Check for specific entity types first
        # ══════════════════════════════════════════════════════════════════
        
        # Pull Request detection (has pullRequestId or codeReviewId)
        if "pullRequestId" in item_obj or "codeReviewId" in item_obj:
            item_id = item_obj.get("pullRequestId") or item_obj.get("codeReviewId")
            item_title = item_obj.get("title", "")
            item_state = item_obj.get("status", "")
            logger.debug("[LLM_UTILS] Detected PR entity: ID=%s", item_id)
        
        # Repository detection (has webUrl and name, no fields)
        elif "webUrl" in item_obj and "name" in item_obj and not fields:
            item_id = item_obj.get("id")  # This is a GUID for repos
            item_title = item_obj.get("name", "")
            item_state = ""  # Repos don't have state
            logger.debug("[LLM_UTILS] Detected repository entity: ID=%s", item_id)
        
        # Iteration/Sprint detection (has path attribute)
        elif "path" in item_obj and ("attributes" in item_obj or "name" in item_obj):
            item_id = item_obj.get("id")
            item_title = item_obj.get("name", "")
            # Check for timeFrame in attributes
            attrs = item_obj.get("attributes", {})
            if isinstance(attrs, dict):
                time_frame = attrs.get("timeFrame", "")
                if time_frame == 0:
                    item_state = "past"
                elif time_frame == 1:
                    item_state = "current"
                elif time_frame == 2:
                    item_state = "future"
                else:
                    item_state = ""
            logger.debug("[LLM_UTILS] Detected iteration entity: ID=%s", item_id)
        
        # Build/Pipeline detection (has buildNumber or definitionId)
        elif "buildNumber" in item_obj or "definitionId" in item_obj:
            item_id = item_obj.get("id") or item_obj.get("buildNumber")
            item_title = item_obj.get("buildNumber", "") or item_obj.get("name", "")
            item_state = item_obj.get("status", "") or item_obj.get("result", "")
            logger.debug("[LLM_UTILS] Detected build entity: ID=%s", item_id)
        
        # Wiki detection (has projectId and type)
        elif "projectId" in item_obj and "type" in item_obj and "versions" in item_obj:
            item_id = item_obj.get("id")
            item_title = item_obj.get("name", "")
            item_state = ""
            logger.debug("[LLM_UTILS] Detected wiki entity: ID=%s", item_id)
        
        # Team detection (has projectId and identity)
        elif "projectId" in item_obj and ("identity" in item_obj or "identityUrl" in item_obj):
            item_id = item_obj.get("id")
            item_title = item_obj.get("name", "")
            item_state = ""
            logger.debug("[LLM_UTILS] Detected team entity: ID=%s", item_id)
        
        # Test Plan detection (has rootSuite or areaPath in specific structure)
        elif "rootSuite" in item_obj or ("areaPath" in item_obj and "iteration" in item_obj):
            item_id = item_obj.get("id")
            item_title = item_obj.get("name", "")
            item_state = item_obj.get("state", "")
            logger.debug("[LLM_UTILS] Detected test plan entity: ID=%s", item_id)
        
        # ══════════════════════════════════════════════════════════════════
        # WORK ITEM FALLBACK: Standard work item extraction
        # ══════════════════════════════════════════════════════════════════
        else:
            # FIX: Check fields FIRST for work item ID (handles search_workitem format)
            # search_workitem returns: {"project":{"id":"GUID"}, "fields":{"system.id":"79458"}}
            # We must check fields.system.id BEFORE recursive search, otherwise we get project GUID
            
            # Try fields first (case-insensitive) - this handles search_workitem format
            for id_key in ["System.Id", "system.id", "System.ID"]:
                if fields.get(id_key):
                    item_id = fields.get(id_key)
                    break
            
            # If not found in fields, try top-level id (standard work item format)
            if not item_id:
                item_id = item_obj.get("id")
            
            # Last resort: recursive search (but this may find project.id first!)
            if not item_id:
                item_id = _find_key_recursive(item_obj, ["System.Id", "id"])
            
            # Title extraction - check fields first
            for title_key in ["System.Title", "system.title"]:
                if fields.get(title_key):
                    item_title = fields.get(title_key)
                    break
            
            if not item_title:
                item_title = (item_obj.get("title") or 
                              item_obj.get("name") or 
                              _find_key_recursive(item_obj, ["System.Title", "title"]))
            
            # State extraction - check fields first
            for state_key in ["System.State", "system.state"]:
                if fields.get(state_key):
                    item_state = fields.get(state_key)
                    break
            
            if not item_state:
                item_state = (item_obj.get("state") or 
                              _find_key_recursive(item_obj, ["System.State", "state"]))
            
            # Fallback: try nested workItem structure
            if not item_id and "workItem" in item_obj:
                wi = item_obj.get("workItem")
                if isinstance(wi, dict):
                    item_id = wi.get("id") or _find_key_recursive(wi, ["System.Id", "id"])
                    if not item_title and isinstance(wi.get("fields"), dict):
                        item_title = wi["fields"].get("System.Title")
    
    # Final fallbacks to string representation
    if not item_id:
        item_id = "?"
    if not item_title:
        item_title = "?"
    if not item_state:
        item_state = ""
    
    return str(item_id), str(item_title), str(item_state)


# ══════════════════════════════════════════════════════════════════════════════
# INSTRUCTION FILE LOADING
# ══════════════════════════════════════════════════════════════════════════════

def find_instruction_file(
    filename: str,
    search_paths: Optional[List[Path]] = None
) -> Optional[Path]:
    """
    Search for an instruction file in multiple locations.
    
    Default search order:
    1. agents/pm_agent/prompts/
    2. agents/pm_agent/
    3. utilities/llm/prompts/ (where synthesizer/planner instructions live)
    4. utilities/llm/instructions/ (future)
    5. Repository root
    
    Args:
        filename: Name of the instruction file (e.g., "synthesizer-instructions.md")
        search_paths: Optional custom list of Path objects to search
    
    Returns:
        Path to file if found, None otherwise
    """
    if search_paths is None:
        # Build default search paths from multiple locations
        base_path = Path(__file__).resolve().parent
        search_paths = [
            # From utilities/llm folder
            base_path.parent.parent / "agents" / "pm_agent" / "prompts",
            base_path.parent.parent / "agents" / "pm_agent",
            base_path / "prompts",  # utilities/llm/prompts/
            base_path / "instructions",  # utilities/llm/instructions/ (future)
            base_path.parent.parent,  # Repo root
        ]
    
    for search_path in search_paths:
        if not search_path.exists():
            continue
        
        full_path = search_path / filename
        if full_path.exists():
            logger.debug("[LLM_UTILS] Found instruction file: %s", full_path)
            return full_path
    
    logger.debug("[LLM_UTILS] Instruction file not found: %s (checked %d paths)", filename, len(search_paths))
    return None


def load_instruction_file(filename: str, fallback: str = "") -> str:
    """
    Load an instruction file from disk with fallback to inline string.
    
    Args:
        filename: Name of the file (e.g., "synthesizer-instructions.md")
        fallback: Fallback content if file not found
    
    Returns:
        File contents or fallback string
    """
    file_path = find_instruction_file(filename)
    
    if file_path:
        try:
            content = file_path.read_text(encoding="utf-8")
            logger.info("[LLM_UTILS] Loaded instruction file: %s", file_path)
            return content
        except Exception as e:
            logger.warning("[LLM_UTILS] Failed to read instruction file %s: %s", file_path, e)
    
    if fallback:
        logger.warning("[LLM_UTILS] Using fallback content for %s", filename)
    
    return fallback


# ══════════════════════════════════════════════════════════════════════════════
# EXPORT ALL PUBLIC FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

__all__ = [
    # Thread pool
    "get_shared_thread_executor",
    "run_in_executor_with_context",
    
    # JSON utilities
    "sanitize_json_text",
    
    # Work item extraction
    "extract_work_item_metadata",
    
    # Instruction file loading
    "find_instruction_file",
    "load_instruction_file",
]
