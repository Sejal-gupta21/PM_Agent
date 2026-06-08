"""
PM Agent LLM Module - Thin wrapper re-exporting shared utilities.

This module now delegates all LLM operations to utilities/llm/ for:
- Modularity: All LLM code in one centralized location
- Reusability: Any agent can import from utilities.llm
- Consistency: Single source of truth for LLM behavior

Re-exports for backward compatibility:
- plan_mcp_call() - From utilities.llm.planner
- summarize_mcp_result() - From utilities.llm.synthesizer  
- synthesize_agent_outputs() - From utilities.llm.synthesizer
- load_copilot_instructions() - From utilities.llm.instructions
- load_synthesizer_instructions() - From utilities.llm.instructions
- clear_instructions_cache() - Calls utilities.llm.clear_all_llm_caches()

MIGRATION HISTORY:
- Phase 1: Planner moved to utilities/llm/planner.py
- Phase 2: Synthesizer moved to utilities/llm/synthesizer.py
- Phase 3: Instructions moved to utilities/llm/instructions.py
- Phase 4: Utils moved to utilities/llm/utils.py

Old imports from agents.pm_agent.llm still work via these re-exports.
"""

import logging

# ==============================================================================
# RE-EXPORTS FROM SHARED LLM LAYER
# ==============================================================================

# Planner functions from shared layer
from utilities.llm.planner import (
    plan_mcp_call,
    clear_planner_cache,
)

# Synthesizer functions from shared layer
from utilities.llm.synthesizer import (
    summarize_mcp_result,
    synthesize_agent_outputs,
)

# Instruction loading from shared layer
from utilities.llm.instructions import (
    load_copilot_instructions,
    load_synthesizer_instructions,
    load_planner_instructions,
    clear_all_llm_caches,
)

# Utils from shared layer
from utilities.llm.utils import (
    run_in_executor_with_context,
    sanitize_json_text,
)

logger = logging.getLogger(__name__)


# ==============================================================================
# BACKWARD COMPATIBILITY WRAPPERS
# ==============================================================================

def clear_instructions_cache() -> None:
    """
    Clear all LLM instruction caches (PM Agent compatibility wrapper).
    
    Delegates to shared layer's clear_all_llm_caches() for unified cache management.
    This maintains backward compatibility with existing code that calls this function.
    """
    clear_all_llm_caches()
    logger.info("[PM_AGENT_LLM] Instructions cache cleared (delegated to shared layer)")


# Legacy function aliases for backward compatibility
_load_copilot_instructions = load_copilot_instructions
_load_synthesizer_instructions = load_synthesizer_instructions
_sanitize_json_text = sanitize_json_text


# ==============================================================================
# EXPORTS
# ==============================================================================

__all__ = [
    # Planner (from utilities.llm.planner)
    "plan_mcp_call",
    "clear_planner_cache",
    
    # Synthesizer (from utilities.llm.synthesizer)
    "summarize_mcp_result",
    "synthesize_agent_outputs",
    
    # Instructions (from utilities.llm.instructions)
    "load_copilot_instructions",
    "load_synthesizer_instructions",
    "load_planner_instructions",
    "clear_all_llm_caches",
    
    # Utils (from utilities.llm.utils)
    "run_in_executor_with_context",
    "sanitize_json_text",
    
    # Backward compatibility
    "clear_instructions_cache",
    
    # Legacy aliases (private, but exported for any edge cases)
    "_load_copilot_instructions",
    "_load_synthesizer_instructions",
    "_sanitize_json_text",
]
