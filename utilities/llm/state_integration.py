"""State Resolution Integration - Bridges state_resolver with light_planner.

This module provides integration between the state resolver and the light planner.
When the LLM generates a plan with state filters, this module resolves those states
to valid ADO states using intelligent semantic mapping.

Key Functions:
- resolve_plan_states: Takes a plan and resolves all state filters
- resolve_user_state: Wrapper around state_resolver for plan processing
"""

import logging
import sys
from pathlib import Path
from typing import List, Dict, Any, Optional

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from state_resolver import (
    resolve_state, 
    resolve_states, 
    state_exists_in_ado,
    StateResolutionResult
)

logger = logging.getLogger(__name__)


def resolve_plan_states(plan: Dict[str, Any]) -> Dict[str, Any]:
    """
    Process a plan and resolve all state filters using the state resolver.
    
    This function:
    1. Iterates through all steps in the plan
    2. Finds steps with state arguments
    3. Resolves each state using the state resolver
    4. Updates the plan with resolved states
    5. Logs mapping decisions for auditing
    
    Args:
        plan: Plan dict from light planner with structure:
            {
                "type": "single" or "multi",
                "steps": [
                    {
                        "step_id": "step_1",
                        "action": "call_tool",
                        "tool": "wit_get_work_items_for_iteration",
                        "args": {
                            "state": ["Done"],  # ← May not exist in ADO
                            ...
                        }
                    }
                ]
            }
    
    Returns:
        Plan with all states resolved to valid ADO states
        
    Example:
        >>> plan = {
        ...     "type": "single",
        ...     "steps": [{
        ...         "step_id": "step_1",
        ...         "action": "call_tool",
        ...         "tool": "wit_get_work_items_for_iteration",
        ...         "args": {"state": ["Done"]}
        ...     }]
        ... }
        >>> resolved = resolve_plan_states(plan)
        >>> resolved["steps"][0]["args"]["state"]
        ['Resolved']
    """
    
    if not plan or not isinstance(plan, dict):
        logger.warning("[StateResolution] Invalid plan object")
        return plan
    
    plan_copy = plan.copy()
    steps = plan_copy.get("steps", [])
    
    if not steps:
        logger.debug("[StateResolution] No steps in plan")
        return plan_copy
    
    logger.info(f"[StateResolution] Processing plan with {len(steps)} steps")
    
    for step_idx, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        
        # Get step args
        args = step.get("args", {})
        if not isinstance(args, dict):
            continue
        
        # Check for state parameter
        if "state" not in args:
            continue
        
        state_value = args["state"]
        step_id = step.get("step_id", f"step_{step_idx + 1}")
        
        logger.info(f"[StateResolution] Step '{step_id}': Processing state filter: {state_value}")
        
        # Ensure state_value is a list
        if isinstance(state_value, str):
            state_list = [state_value]
        elif isinstance(state_value, list):
            state_list = state_value
        else:
            logger.warning(f"[StateResolution] Step '{step_id}': Unexpected state type {type(state_value)}")
            continue
        
        # Resolve each state
        resolved_states = []
        resolution_logs = []
        
        for user_state in state_list:
            result = resolve_state(user_state)
            
            if result.is_valid and result.resolved_state:
                resolved_states.append(result.resolved_state)
                
                # Log what happened
                if result.was_mapped:
                    log_msg = (
                        f"Mapped '{user_state}' → '{result.resolved_state}' "
                        f"({result.mapping_reason})"
                    )
                    resolution_logs.append(log_msg)
                    logger.info(f"[StateResolution] Step '{step_id}': {log_msg}")
                else:
                    log_msg = f"Using '{user_state}' as-is (exists in ADO)"
                    resolution_logs.append(log_msg)
                    logger.debug(f"[StateResolution] Step '{step_id}': {log_msg}")
            else:
                # State could not be resolved
                logger.warning(
                    f"[StateResolution] Step '{step_id}': "
                    f"Could not resolve state '{user_state}' - {result.mapping_reason}"
                )
                # Don't include unresolved states - this will result in empty filter
                # and the user will get "no items in requested state" message
        
        # Update the plan with resolved states
        if resolved_states:
            plan_copy["steps"][step_idx]["args"]["state"] = resolved_states
            logger.info(
                f"[StateResolution] Step '{step_id}': "
                f"Resolved {len(state_list)} states → {resolved_states}"
            )
        else:
            logger.warning(
                f"[StateResolution] Step '{step_id}': "
                f"No valid states after resolution. Original: {state_list}"
            )
            # Keep empty list - this will cause no items to be returned (correct behavior)
            plan_copy["steps"][step_idx]["args"]["state"] = []
        
        # Add resolution info to step for auditing
        if resolution_logs:
            if "resolution_info" not in step:
                plan_copy["steps"][step_idx]["resolution_info"] = {
                    "original_states": state_list,
                    "resolved_states": resolved_states,
                    "logs": resolution_logs
                }
    
    return plan_copy


def resolve_user_state(user_state: str) -> Optional[str]:
    """
    Resolve a single user-specified state to an ADO state.
    
    This is a convenience wrapper around state_resolver.resolve_state() that:
    - Returns the resolved state name (or None if cannot be resolved)
    - Logs the resolution
    - Suitable for use in tool execution/filtering
    
    Args:
        user_state: State name from user query
        
    Returns:
        Resolved ADO state name, or None if not valid/not found
        
    Example:
        >>> resolve_user_state("Done")
        'Resolved'
        
        >>> resolve_user_state("Active")  # Exists in ADO
        'Active'
        
        >>> resolve_user_state("Unknown")  # Not found
        None
    """
    
    if not user_state:
        return None
    
    result = resolve_state(user_state)
    
    if result.is_valid and result.resolved_state:
        if result.was_mapped:
            logger.info(
                f"[StateResolution] Resolved user state '{user_state}' → "
                f"'{result.resolved_state}' ({result.mapping_reason})"
            )
        else:
            logger.debug(
                f"[StateResolution] User state '{user_state}' exists in ADO as '{result.resolved_state}'"
            )
        return result.resolved_state
    else:
        logger.warning(
            f"[StateResolution] Could not resolve user state '{user_state}': {result.mapping_reason}"
        )
        return None


def describe_state_resolution(user_state: str) -> str:
    """Get human-readable explanation of how a state was resolved.
    
    Args:
        user_state: Original state from user
        
    Returns:
        Human-readable explanation string
    """
    from state_resolver import describe_resolution
    return describe_resolution(user_state)


# ============================================================================
# TESTING
# ============================================================================

if __name__ == "__main__":
    # Test state resolution in a plan
    test_plan = {
        "type": "multi",
        "steps": [
            {
                "step_id": "step_1",
                "action": "call_tool",
                "tool": "wit_get_work_items_for_iteration",
                "args": {
                    "project": "FracPro-OPS",
                    "team": "XOPS 25",
                    "iterationId": "@CurrentIteration",
                    "state": ["Done"]  # ← Will be mapped to Resolved
                }
            },
            {
                "step_id": "step_2",
                "action": "call_tool",
                "tool": "search_workitem",
                "args": {
                    "searchText": "Bug",
                    "project": ["FracPro-OPS"],
                    "state": ["In Progress", "Active"]  # ← First will map, second is exact
                }
            }
        ]
    }
    
    print("\n" + "="*80)
    print("STATE RESOLUTION IN PLAN TEST")
    print("="*80 + "\n")
    
    print("ORIGINAL PLAN:")
    for step in test_plan["steps"]:
        if "state" in step.get("args", {}):
            print(f"  Step {step['step_id']}: state = {step['args']['state']}")
    
    resolved_plan = resolve_plan_states(test_plan)
    
    print("\nRESO LVED PLAN:")
    for step in resolved_plan["steps"]:
        if "state" in step.get("args", {}):
            print(f"  Step {step['step_id']}: state = {step['args']['state']}")
        if "resolution_info" in step:
            info = step["resolution_info"]
            print(f"    Resolution: {info['original_states']} → {info['resolved_states']}")
            for log in info["logs"]:
                print(f"      - {log}")
    
    print("\n" + "="*80 + "\n")
