"""
Generic instruction loading for all LLM operations.

Provides unified interface for:
- Loading copilot instructions (PM Agent system prompt)
- Loading synthesizer instructions (synthesis system prompt)
- Loading planner instructions (tool selection system prompt)
- Clearing all caches at once

Uses file-based loading with fallback to inline defaults.
Agent-agnostic: works with any agent without hardcoding agent-specific paths.

CRITICAL: All instruction caches are managed centrally here.
Call clear_all_llm_caches() to reset all caches.
"""

import logging
from typing import Optional

from .utils import load_instruction_file

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# GLOBAL CACHES FOR INSTRUCTIONS
# ══════════════════════════════════════════════════════════════════════════════

_COPILOT_INSTRUCTIONS_CACHE: Optional[str] = None
_SYNTHESIZER_INSTRUCTIONS_CACHE: Optional[str] = None
_PLANNER_INSTRUCTIONS_CACHE: Optional[str] = None
_KB_INSTRUCTIONS_CACHE: Optional[str] = None


# ══════════════════════════════════════════════════════════════════════════════
# COPILOT INSTRUCTIONS (PM Agent system prompt)
# ══════════════════════════════════════════════════════════════════════════════

def load_copilot_instructions() -> str:
    """
    Load copilot (PM Agent) system instructions from file.
    
    Search paths (in order):
    1. agents/pm_agent/pm-agent-instructions.md
    2. agents/pm_agent/prompts/pm-agent-instructions.md
    3. Repository root: pm-agent-instructions.md
    4. Fallback: minimal inline instructions
    
    Cached globally after first load.
    
    Returns:
        System prompt string for copilot LLM
    """
    global _COPILOT_INSTRUCTIONS_CACHE
    
    if _COPILOT_INSTRUCTIONS_CACHE is not None:
        logger.debug("[LLM_INSTRUCTIONS] Copilot instructions cache hit")
        return _COPILOT_INSTRUCTIONS_CACHE
    
    fallback = """You are a PM assistant. Return ONLY valid JSON. Never invent tools or arguments."""
    
    _COPILOT_INSTRUCTIONS_CACHE = load_instruction_file(
        "pm-agent-instructions.md",
        fallback=fallback
    )
    
    logger.info("[LLM_INSTRUCTIONS] Loaded copilot instructions (%d chars)", len(_COPILOT_INSTRUCTIONS_CACHE))
    return _COPILOT_INSTRUCTIONS_CACHE


# ══════════════════════════════════════════════════════════════════════════════
# SYNTHESIZER INSTRUCTIONS
# ══════════════════════════════════════════════════════════════════════════════

def load_synthesizer_instructions() -> str:
    """
    Load synthesizer system instructions from file.
    
    Search paths (in order):
    1. agents/pm_agent/prompts/synthesizer-instructions.md
    2. utilities/llm/instructions/synthesizer-instructions.md
    3. Fallback: empty string (uses inline defaults in synthesizer)
    
    Cached globally after first load.
    
    Returns:
        System prompt string for synthesizer LLM
    """
    global _SYNTHESIZER_INSTRUCTIONS_CACHE
    
    if _SYNTHESIZER_INSTRUCTIONS_CACHE is not None:
        logger.debug("[LLM_INSTRUCTIONS] Synthesizer instructions cache hit")
        return _SYNTHESIZER_INSTRUCTIONS_CACHE
    
    _SYNTHESIZER_INSTRUCTIONS_CACHE = load_instruction_file(
        "synthesizer-instructions.md",
        fallback=""
    )
    
    logger.info("[LLM_INSTRUCTIONS] Loaded synthesizer instructions (%d chars)", len(_SYNTHESIZER_INSTRUCTIONS_CACHE))
    return _SYNTHESIZER_INSTRUCTIONS_CACHE


# ══════════════════════════════════════════════════════════════════════════════
# PLANNER INSTRUCTIONS
# ══════════════════════════════════════════════════════════════════════════════

def load_planner_instructions() -> str:
    """
    Load planner system instructions from file.
    
    Search paths (in order):
    1. agents/pm_agent/prompts/planner-instructions.md
    2. utilities/llm/instructions/planner-instructions.md
    3. Fallback: empty string (planner.py has its own inline defaults)
    
    Cached globally after first load.
    
    Returns:
        System prompt string for planner LLM
    """
    global _PLANNER_INSTRUCTIONS_CACHE
    
    if _PLANNER_INSTRUCTIONS_CACHE is not None:
        logger.debug("[LLM_INSTRUCTIONS] Planner instructions cache hit")
        return _PLANNER_INSTRUCTIONS_CACHE
    
    _PLANNER_INSTRUCTIONS_CACHE = load_instruction_file(
        "planner-instructions.md",
        fallback=""
    )
    
    logger.info("[LLM_INSTRUCTIONS] Loaded planner instructions (%d chars)", len(_PLANNER_INSTRUCTIONS_CACHE))
    return _PLANNER_INSTRUCTIONS_CACHE


# ══════════════════════════════════════════════════════════════════════════════
# PM KNOWLEDGE BASE
# ══════════════════════════════════════════════════════════════════════════════

def _load_kb_from_directory() -> str:
    """
    Load and concatenate all *.md files from the kb/ subdirectory in sorted
    filename order.  Files named with numeric prefixes (01-*, 02-*, …) load
    in the intended sequence.  Adding a new section requires only dropping a
    new file into the directory — no code changes needed.

    Returns:
        Assembled KB string, or empty string if the directory is missing/empty.
    """
    import glob as _glob
    from pathlib import Path as _Path

    base = _Path(__file__).resolve().parent
    kb_candidates = [
        base.parent.parent / "agents" / "pm_agent" / "prompts" / "kb",
        base / "prompts" / "kb",
        base.parent.parent / "kb",
    ]

    for kb_dir in kb_candidates:
        if not kb_dir.is_dir():
            continue
        files = sorted(_glob.glob(str(kb_dir / "*.md")))
        if not files:
            continue
        sections: list = []
        for fpath in files:
            try:
                sections.append(_Path(fpath).read_text(encoding="utf-8").strip())
                logger.debug("[LLM_INSTRUCTIONS] Loaded KB section: %s", fpath)
            except Exception as exc:
                logger.warning("[LLM_INSTRUCTIONS] Could not read KB file %s: %s", fpath, exc)
        if sections:
            logger.info(
                "[LLM_INSTRUCTIONS] Assembled KB from %d section file(s) in %s",
                len(sections), kb_dir
            )
            return "\n\n---\n\n".join(sections)

    return ""


def load_kb_instructions() -> str:
    """
    Load PM Knowledge Base.

    Resolution order:
    1. kb/ subdirectory (agents/pm_agent/prompts/kb/*.md sorted by filename)
       — individual section files are assembled in order at runtime.
       — add new sections by creating NN-name.md files; no code change needed.
    2. Fallback: single legacy agents/pm_agent/prompts/pm-kb.md

    Cached globally after first load.

    Returns:
        Assembled PM Knowledge Base string, or empty string if not found.
    """
    global _KB_INSTRUCTIONS_CACHE

    if _KB_INSTRUCTIONS_CACHE is not None:
        logger.debug("[LLM_INSTRUCTIONS] KB instructions cache hit")
        return _KB_INSTRUCTIONS_CACHE

    # 1. Try structured directory (new format)
    _KB_INSTRUCTIONS_CACHE = _load_kb_from_directory()

    # 2. Fallback to legacy single-file format
    if not _KB_INSTRUCTIONS_CACHE:
        _KB_INSTRUCTIONS_CACHE = load_instruction_file("pm-kb.md", fallback="")

    logger.info("[LLM_INSTRUCTIONS] Loaded KB instructions (%d chars)", len(_KB_INSTRUCTIONS_CACHE))
    return _KB_INSTRUCTIONS_CACHE


# ══════════════════════════════════════════════════════════════════════════════
# CACHE MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

def clear_all_llm_caches() -> None:
    """
    Clear all LLM instruction caches.
    
    Use this when instruction files are updated and need to be reloaded.
    Also clears the planner cache in the shared planner module.
    
    Call this from:
    - agents/pm_agent/llm.py clear_instructions_cache()
    - Admin/debug endpoints when instructions change
    """
    global _COPILOT_INSTRUCTIONS_CACHE, _SYNTHESIZER_INSTRUCTIONS_CACHE, _PLANNER_INSTRUCTIONS_CACHE, _KB_INSTRUCTIONS_CACHE

    _COPILOT_INSTRUCTIONS_CACHE = None
    _SYNTHESIZER_INSTRUCTIONS_CACHE = None
    _PLANNER_INSTRUCTIONS_CACHE = None
    _KB_INSTRUCTIONS_CACHE = None
    
    # Also clear planner cache in shared layer (planner has its own internal caches)
    try:
        from .planner import clear_planner_cache
        clear_planner_cache()
        logger.debug("[LLM_INSTRUCTIONS] Cleared planner module cache")
    except ImportError:
        logger.debug("[LLM_INSTRUCTIONS] Planner module not available for cache clear")
    except Exception as e:
        logger.warning("[LLM_INSTRUCTIONS] Could not clear planner cache: %s", e)
    
    logger.info("[LLM_INSTRUCTIONS] All LLM instruction caches cleared")


# ══════════════════════════════════════════════════════════════════════════════
# EXPORT ALL PUBLIC FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

__all__ = [
    "load_copilot_instructions",
    "load_synthesizer_instructions",
    "load_planner_instructions",
    "load_kb_instructions",
    "clear_all_llm_caches",
]
