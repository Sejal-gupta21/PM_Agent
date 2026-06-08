"""
LLM Utilities - Shared LLM-based planning, synthesis, and processing.

This module provides centralized LLM operations used across all agents:
- Planner: Tool selection and query planning (plan_mcp_call)
- Synthesizer: Result synthesis and multi-agent output synthesis
- Instructions: Unified instruction loading with fallbacks
- Utils: Shared utilities (JSON sanitization, context preservation, etc.)

Components:
- planner.py: Heavy LLM planner for tool selection
- synthesizer.py: Result and multi-agent synthesis
- instructions.py: Generic instruction loading
- utils.py: Shared utilities

Architecture (from diagram):
┌─────────────────────────────────────┐
│            LLM LAYER                │
│  ┌─────────────┐ ┌───────────────┐  │
│  │ Planner LLM │ │ Synthesis LLM │  │
│  └─────────────┘ └───────────────┘  │
└─────────────────────────────────────┘
                  │
                  ▼
         ┌─────────────────┐
         │  OBSERVABILITY  │
         │    Langfuse     │
         └─────────────────┘
"""

# ══════════════════════════════════════════════════════════════════════════════
# PLANNER (already exists)
# ══════════════════════════════════════════════════════════════════════════════
from .planner import (
    plan_mcp_call,
    clear_planner_cache,
)

# ══════════════════════════════════════════════════════════════════════════════
# SYNTHESIZER (new)
# ══════════════════════════════════════════════════════════════════════════════
from .synthesizer import (
    summarize_mcp_result,
    synthesize_agent_outputs,
)

# ══════════════════════════════════════════════════════════════════════════════
# INSTRUCTIONS (new)
# ══════════════════════════════════════════════════════════════════════════════
from .instructions import (
    load_copilot_instructions,
    load_synthesizer_instructions,
    load_planner_instructions,
    load_kb_instructions,
    clear_all_llm_caches,
)

# ══════════════════════════════════════════════════════════════════════════════
# UTILS (new)
# ══════════════════════════════════════════════════════════════════════════════
from .utils import (
    sanitize_json_text,
    extract_work_item_metadata,
    find_instruction_file,
    load_instruction_file,
    get_shared_thread_executor,
    run_in_executor_with_context,
)

# ══════════════════════════════════════════════════════════════════════════════
# LIGHT PLANNER (for routing hints)
# ══════════════════════════════════════════════════════════════════════════════
from .light_planner import (
    call_light_planner_for_routing,
    should_invoke_light_planner,
    get_light_planner_thresholds,
    clear_light_planner_cache,
)

# ══════════════════════════════════════════════════════════════════════════════
# NOTE: Deep LLM (plan_mcp_call) is in planner.py - Orchestrator calls it 
# when agent returns NEEDS_DEEP_ANALYSIS. No separate deep_replan module needed.
# ══════════════════════════════════════════════════════════════════════════════

# ══════════════════════════════════════════════════════════════════════════════
# EXPORTS
# ══════════════════════════════════════════════════════════════════════════════
__all__ = [
    # Planner
    "plan_mcp_call",
    "clear_planner_cache",
    
    # Synthesizer
    "summarize_mcp_result",
    "synthesize_agent_outputs",
    
    # Instructions
    "load_copilot_instructions",
    "load_synthesizer_instructions",
    "load_planner_instructions",
    "load_kb_instructions",
    "clear_all_llm_caches",
    
    # Utils
    "sanitize_json_text",
    "extract_work_item_metadata",
    "find_instruction_file",
    "load_instruction_file",
    "get_shared_thread_executor",
    "run_in_executor_with_context",
    
    # Light Planner
    "call_light_planner_for_routing",
    "should_invoke_light_planner",
    "get_light_planner_thresholds",
    "clear_light_planner_cache",
]
