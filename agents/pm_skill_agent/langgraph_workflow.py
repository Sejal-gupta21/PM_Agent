"""
LangGraph workflow for PM Skills Agent.

This module implements a LangGraph state machine for routing and executing
PM fixed skills in a structured, traceable manner.

Workflow:
    User Query → Skill Matching → Skill Execution → Response Formatting
"""

import logging
from typing import Dict, Any, List, Optional, TypedDict, Literal, Annotated
from dataclasses import dataclass
import operator

from langgraph.graph import StateGraph, END
from langgraph.checkpoint.memory import MemorySaver

from .skills import SkillRegistry, SkillResult, execute_skill
from utilities.langfuse_client import get_langfuse_client, create_span, create_trace, finalize_span

logger = logging.getLogger("pm_skill_agent.langgraph")
_langfuse_client = get_langfuse_client()


# ============================================================================
# STATE DEFINITIONS
# ============================================================================


class SkillWorkflowState(TypedDict):
    """State for the skill execution workflow."""
    # Input
    query: str
    params: Dict[str, Any]
    session_id: str
    
    # Routing
    matched_skill: Optional[str]
    skill_confidence: float
    routing_metadata: Dict[str, Any]
    
    # Execution
    execution_result: Optional[SkillResult]
    execution_metadata: Dict[str, Any]
    
    # Output
    response: Dict[str, Any]
    errors: Annotated[List[str], operator.add]  # Accumulate errors
    
    # Workflow control
    next_action: Optional[Literal["execute_skill", "route_to_llm", "format_response", "end"]]


# ============================================================================
# RESULT FORMATTING
# ============================================================================


def _wi_field(wi: dict, field_name: str) -> Any:
    """Extract a field value from a work item dict, trying multiple key formats."""
    if not isinstance(wi, dict):
        return None
    # Direct key
    if field_name in wi:
        return wi[field_name]
    # Nested in 'fields' dict
    fields = wi.get("fields", {})
    if isinstance(fields, dict):
        # Try System.X prefix
        for prefix in ("System.", "Microsoft.VSTS.Common.", ""):
            key = f"{prefix}{field_name}"
            if key in fields:
                return fields[key]
    return None


def _format_skill_result(skill_name: str, result: Any) -> str:
    """
    Format a skill result into human-readable text for UI display.
    
    Converts structured dict results into nice formatted strings.
    
    Args:
        skill_name: Name of the skill that produced the result
        result: Raw result (dict, list, or string)
        
    Returns:
        Formatted string for display
    """
    if isinstance(result, str):
        return result
    
    if not isinstance(result, dict):
        return str(result)
    
    # Skill-specific formatters
    if skill_name == "list_area_paths":
        project = result.get("project", "Unknown")
        paths = result.get("area_paths", [])
        count = result.get("count", len(paths))
        
        if not paths:
            return f"No area paths found for project '{project}'."
        
        lines = [f"**Area Paths for '{project}' ({count} total):**", ""]
        for path in paths:
            lines.append(f"- {path}")
        return "\n".join(lines)
    
    elif skill_name == "list_iterations":
        project = result.get("project", "Unknown")
        iterations = result.get("iterations", [])
        
        if not iterations:
            return f"No iterations found for project '{project}'."
        
        lines = [f"**Iterations for '{project}':**", ""]
        for it in iterations:
            if isinstance(it, dict):
                name = it.get("name", it.get("path", "Unknown"))
                lines.append(f"- {name}")
            else:
                lines.append(f"- {it}")
        return "\n".join(lines)
    
    elif skill_name == "list_projects":
        projects = result.get("projects", [])
        
        if not projects:
            return "No projects found."
        
        lines = ["**Available Projects:**", ""]
        for p in projects:
            if isinstance(p, dict):
                name = p.get("name", "Unknown")
                lines.append(f"- {name}")
            else:
                lines.append(f"- {p}")
        return "\n".join(lines)
    
    elif skill_name == "execute_wiql":
        # Format WIQL query results into a readable work item table
        work_items = result.get("work_items", result.get("items", []))
        count = result.get("count", len(work_items))
        wiql_query = result.get("wiql_query", "")
        
        if not work_items:
            return f"No work items found matching the query.\n\n_WIQL:_ `{wiql_query[:200]}`" if wiql_query else "No work items found matching the query."
        
        lines = [f"**Found {count} work item(s):**", ""]
        
        # Build table header based on available fields
        has_priority = any(_wi_field(wi, "Priority") for wi in work_items[:5])
        has_type = any(_wi_field(wi, "WorkItemType") for wi in work_items[:5])
        
        header_parts = ["| ID | Title"]
        if has_type:
            header_parts.append("| Type")
        header_parts.append("| State")
        if has_priority:
            header_parts.append("| Priority")
        header_parts.append("| Assigned To |")
        lines.append("".join(header_parts))
        
        sep_parts = ["|---|---"]
        if has_type:
            sep_parts.append("|---")
        sep_parts.append("|---")
        if has_priority:
            sep_parts.append("|---")
        sep_parts.append("|---|")
        lines.append("".join(sep_parts))
        
        # Show up to 50 items in table
        for wi in work_items[:50]:
            wi_id = _wi_field(wi, "Id") or _wi_field(wi, "id") or ""
            title = _wi_field(wi, "Title") or ""
            state = _wi_field(wi, "State") or ""
            assigned = _wi_field(wi, "AssignedTo") or ""
            # Handle identity dicts
            if isinstance(assigned, dict):
                assigned = assigned.get("displayName", str(assigned))
            
            row_parts = [f"| {wi_id} | {title[:80]}"]
            if has_type:
                wi_type = _wi_field(wi, "WorkItemType") or ""
                row_parts.append(f"| {wi_type}")
            row_parts.append(f"| {state}")
            if has_priority:
                prio = _wi_field(wi, "Priority") or ""
                row_parts.append(f"| {prio}")
            row_parts.append(f"| {assigned} |")
            lines.append("".join(row_parts))
        
        if count > 50:
            lines.append(f"\n_...and {count - 50} more work items._")
        
        return "\n".join(lines)
    
    elif skill_name in ("bug_areas_highlight", "feedback_to_dev", "overlooked_stories"):
        # These usually have message or summary fields
        if "message" in result:
            return result["message"]
        if "summary" in result:
            return result["summary"]
    
    elif skill_name == "iteration_report":
        if "message" in result:
            return result["message"]
        if "report_path" in result:
            return f"Iteration report generated: {result['report_path']}"
    
    # Default: Return as formatted key-value pairs
    lines = []
    for key, value in result.items():
        if key in ("adapter", "metadata", "raw_data"):
            continue  # Skip internal fields
        if isinstance(value, list) and len(value) > 5:
            lines.append(f"**{key}**: {len(value)} items")
        elif isinstance(value, list):
            lines.append(f"**{key}**: {', '.join(str(v) for v in value)}")
        else:
            lines.append(f"**{key}**: {value}")
    
    return "\n".join(lines) if lines else str(result)


# ============================================================================
# NODE FUNCTIONS
# ============================================================================


def match_skill_node(state: SkillWorkflowState) -> Dict[str, Any]:
    """
    Match the user query to a fixed skill.
    
    Uses the skill router to determine if the query can be handled
    by a fixed skill or should be routed to the LLM planner.
    
    If skill is already pre-set (explicit routing), skips matching.
    """
    from agents.pm_agent.skills import should_use_fixed_skill
    
    query = state["query"]
    session_id = state['session_id']
    
    # Check if skill is already pre-set (explicit routing)
    if state.get("matched_skill") and state.get("routing_metadata", {}).get("explicit_skill"):
        logger.info(f"[{session_id}] Skill pre-set: {state['matched_skill']} (explicit routing)")
        return {
            "matched_skill": state["matched_skill"],
            "skill_confidence": state["skill_confidence"],
            "routing_metadata": {
                **state.get("routing_metadata", {}),
                "router": "explicit"
            },
            "next_action": "execute_skill"
        }
    
    logger.info(f"[{session_id}] Matching skill for query: {query[:100]}")
    
    # Create Langfuse span
    span = None
    if _langfuse_client:
        try:
            span = create_span(
                name="langgraph_match_skill",
                input_data={"query": query[:200]},
                metadata={"session_id": session_id, "workflow_node": "match_skill"},
                session_id=session_id  # CRITICAL: Same session as parent
            )
        except Exception as e:
            logger.debug(f"[{session_id}] Langfuse span failed: {e}")
    
    skill_match = should_use_fixed_skill(query)
    
    if skill_match:
        logger.info(
            f"[{session_id}] Matched skill '{skill_match.skill_name}' "
            f"with confidence {skill_match.confidence:.2f}"
        )
        
        # Finalize span
        if span:
            try:
                finalize_span(span, output={"skill": skill_match.skill_name, "confidence": skill_match.confidence})
            except Exception as e:
                logger.debug(f"[{session_id}] Langfuse span end failed: {e}")
        
        return {
            "matched_skill": skill_match.skill_name,
            "skill_confidence": skill_match.confidence,
            "routing_metadata": {
                "extracted_params": skill_match.extracted_params,
                "router": "fixed_skill",
                "match_time": __import__('datetime').datetime.utcnow().isoformat()
            },
            "next_action": "execute_skill"
        }
    else:
        logger.info(f"[{session_id}] No skill match, routing to LLM")
        
        # Finalize span
        if span:
            try:
                finalize_span(span, output={"matched": False, "reason": "no_skill_match"})
            except Exception as e:
                logger.debug(f"[{session_id}] Langfuse span end failed: {e}")
        
        return {
            "matched_skill": None,
            "skill_confidence": 0.0,
            "routing_metadata": {
                "router": "llm_fallback",
                "reason": "no_skill_match"
            },
            "next_action": "route_to_llm"
        }


async def execute_skill_node(state: SkillWorkflowState) -> Dict[str, Any]:
    """
    Execute the matched skill.
    
    Calls the skill handler with the provided parameters and
    captures the result for formatting.
    """
    skill_name = state["matched_skill"]
    params = state["params"]
    session_id = state["session_id"]
    
    logger.info(f"[{session_id}] Executing skill: {skill_name}")
    
    # Create Langfuse span
    span = None
    if _langfuse_client:
        try:
            span = create_span(
                name="langgraph_execute_skill",
                input_data={"skill": skill_name, "params": params},
                metadata={"session_id": session_id, "workflow_node": "execute_skill"},
                session_id=session_id  # CRITICAL: Same session as parent
            )
        except Exception as e:
            logger.debug(f"[{session_id}] Langfuse span failed: {e}")
    
    try:
        # Merge extracted params from routing with provided params
        # CRITICAL: Include the original query so skill handlers can access it (e.g., for team extraction)
        merged_params = {
            "query": state["query"],  # Add query from state so skills can extract context from it
            **state["routing_metadata"].get("extracted_params", {}),
            **params
        }
        
        # CRITICAL: Include the original query so skills can extract parameters from it
        if "query" not in merged_params and state.get("query"):
            merged_params["query"] = state["query"]
        
        # Execute the skill
        result = await execute_skill(skill_name, merged_params)
        
        if result.success:
            logger.info(f"[{session_id}] Skill {skill_name} completed successfully")
            
            # Finalize span
            if span:
                try:
                    finalize_span(span, output={"success": True, "skill": skill_name})
                except Exception as e:
                    logger.debug(f"[{session_id}] Langfuse span end failed: {e}")
            
            return {
                "execution_result": result,
                "execution_metadata": {
                    "skill_name": skill_name,
                    "execution_time": __import__('datetime').datetime.utcnow().isoformat(),
                    "success": True,
                    **result.metadata
                },
                "next_action": "format_response"
            }
        else:
            logger.warning(f"[{session_id}] Skill {skill_name} failed: {result.error}")
            
            # Finalize span with warning
            if span:
                try:
                    finalize_span(span, output={"success": False, "error": result.error}, level="WARNING")
                except Exception as e:
                    logger.debug(f"[{session_id}] Langfuse span end failed: {e}")
            
            return {
                "execution_result": result,
                "execution_metadata": {
                    "skill_name": skill_name,
                    "execution_time": __import__('datetime').datetime.utcnow().isoformat(),
                    "success": False
                },
                "errors": [f"Skill execution failed: {result.error}"],
                "next_action": "format_response"
            }
    
    except Exception as e:
        logger.exception(f"[{session_id}] Error executing skill {skill_name}: {e}")
        
        # Finalize span with error
        if span:
            try:
                finalize_span(span, output={"success": False, "exception": str(e)}, level="ERROR")
            except Exception as span_error:
                logger.debug(f"[{session_id}] Langfuse span end failed: {span_error}")
        
        return {
            "execution_result": None,
            "execution_metadata": {
                "skill_name": skill_name,
                "execution_time": __import__('datetime').datetime.utcnow().isoformat(),
                "success": False
            },
            "errors": [f"Skill execution error: {str(e)}"],
            "next_action": "format_response"
        }


def format_response_node(state: SkillWorkflowState) -> Dict[str, Any]:
    """
    Format the skill execution result into a standardized response.
    
    Converts the SkillResult into a format suitable for the chat UI.
    """
    session_id = state["session_id"]
    result = state["execution_result"]
    
    logger.info(f"[{session_id}] Formatting response")
    
    # Create Langfuse span
    span = None
    if _langfuse_client:
        try:
            span = create_span(
                name="langgraph_format_response",
                input_data={"has_result": result is not None},
                metadata={"session_id": session_id, "workflow_node": "format_response"},
                session_id=session_id  # CRITICAL: Same session as parent
            )
        except Exception as e:
            logger.debug(f"[{session_id}] Langfuse span failed: {e}")
    
    if result and result.success:
        # Format the result into human-readable text for UI display
        # For execute_wiql skill, always use the structured formatter for rich table display
        skill_name = state.get("matched_skill", "")
        if skill_name == "execute_wiql" and result.result:
            formatted_result = _format_skill_result(skill_name, result.result)
        elif hasattr(result, 'message') and result.message and isinstance(result.message, str) and len(result.message) > 50:
            # Use the message directly as it's already formatted
            formatted_result = result.message
        else:
            # Fall back to formatting the result dict
            formatted_result = _format_skill_result(state["matched_skill"], result.result)
        
        response = {
            "success": True,
            "skill": state["matched_skill"],
            "result": formatted_result,
            "raw_result": result.result,  # Keep raw data for programmatic access
            "metadata": {
                **state["execution_metadata"],
                **state["routing_metadata"],
                "workflow": "langgraph"
            }
        }
        
        # Finalize span with success
        if span:
            try:
                finalize_span(span, output={"success": True, "skill": state["matched_skill"]})
            except Exception as e:
                logger.debug(f"[{session_id}] Langfuse span end failed: {e}")
    else:
        error_msg = result.error if result else "Unknown error"
        # Get the user-friendly message if available (for billing_deviation etc.)
        user_message = result.message if result and hasattr(result, 'message') else None
        # Get result metadata to preserve pending operations
        result_metadata = result.metadata if result and hasattr(result, 'metadata') else {}
        
        response = {
            "success": False,
            "skill": state["matched_skill"],
            "error": error_msg,
            "message": user_message,  # Include user-friendly message
            "metadata": {
                **result_metadata,  # Include result metadata (pending_billing_deviation, etc.)
                **state.get("execution_metadata", {}),
                **state["routing_metadata"],
                "workflow": "langgraph"
            }
        }
        
        # Finalize span with failure
        if span:
            try:
                finalize_span(span, output={"success": False, "error": error_msg}, level="WARNING")
            except Exception as e:
                logger.debug(f"[{session_id}] Langfuse span end failed: {e}")
    
    return {
        "response": response,
        "next_action": "end"
    }


def route_to_llm_node(state: SkillWorkflowState) -> Dict[str, Any]:
    """
    Placeholder for routing to LLM planner.
    
    This node indicates that the query should be handled by the
    LLM-based planning system instead of fixed skills.
    """
    logger.info(f"[{state['session_id']}] Routing to LLM planner")
    
    return {
        "response": {
            "success": False,
            "requires_llm": True,
            "metadata": {
                **state["routing_metadata"],
                "workflow": "langgraph"
            }
        },
        "next_action": "end"
    }


# ============================================================================
# ROUTING FUNCTIONS
# ============================================================================


def should_execute_skill(state: SkillWorkflowState) -> Literal["execute_skill", "route_to_llm"]:
    """Determine if we should execute a skill or route to LLM."""
    next_action = state.get("next_action")
    
    if next_action == "execute_skill":
        return "execute_skill"
    elif next_action == "route_to_llm":
        return "route_to_llm"
    else:
        # Default to LLM if unclear
        return "route_to_llm"


def should_continue(state: SkillWorkflowState) -> Literal["format_response", "end"]:
    """Determine if we should format response or end."""
    next_action = state.get("next_action")
    
    if next_action == "format_response":
        return "format_response"
    else:
        return "end"


# ============================================================================
# GRAPH CONSTRUCTION
# ============================================================================


def create_skill_workflow() -> StateGraph:
    """
    Create the LangGraph workflow for skill execution.
    
    Workflow nodes:
        - match_skill: Match query to fixed skill
        - execute_skill: Execute the matched skill
        - route_to_llm: Route to LLM planner (fallback)
        - format_response: Format result for UI
        
    Returns:
        Compiled LangGraph StateGraph
    """
    # Create the graph
    workflow = StateGraph(SkillWorkflowState)
    
    # Add nodes
    workflow.add_node("match_skill", match_skill_node)
    workflow.add_node("execute_skill", execute_skill_node)
    workflow.add_node("route_to_llm", route_to_llm_node)
    workflow.add_node("format_response", format_response_node)
    
    # Set entry point
    workflow.set_entry_point("match_skill")
    
    # Add conditional edges from match_skill
    workflow.add_conditional_edges(
        "match_skill",
        should_execute_skill,
        {
            "execute_skill": "execute_skill",
            "route_to_llm": "route_to_llm"
        }
    )
    
    # Add edge from execute_skill to format_response
    workflow.add_edge("execute_skill", "format_response")
    
    # Add edges to END
    workflow.add_edge("route_to_llm", END)
    workflow.add_edge("format_response", END)
    
    # Compile with checkpointer for state persistence
    checkpointer = MemorySaver()
    compiled_workflow = workflow.compile(checkpointer=checkpointer)
    
    logger.info("LangGraph skill workflow created and compiled")
    
    return compiled_workflow


# ============================================================================
# WORKFLOW INTERFACE
# ============================================================================


# Global workflow instance
_workflow = None


def get_workflow() -> StateGraph:
    """Get or create the global workflow instance."""
    global _workflow
    if _workflow is None:
        _workflow = create_skill_workflow()
    return _workflow


async def execute_skill_via_langgraph(
    query: str,
    params: Optional[Dict[str, Any]] = None,
    session_id: str = "default",
    skill_name: Optional[str] = None,
    parent_trace: Optional[Any] = None
) -> Dict[str, Any]:
    """
    Execute a skill query through the LangGraph workflow.
    
    Args:
        query: User query to process
        params: Optional parameters for skill execution
        session_id: Session identifier for tracking
        skill_name: Optional explicit skill name (skips matching if provided)
        parent_trace: Parent trace from orchestrator for proper nesting
        
    Returns:
        Response dict with success, result, metadata
    """
    workflow = get_workflow()
    
    # Create span under parent trace (from orchestrator)
    span = None
    if _langfuse_client and parent_trace:
        try:
            from utilities.langfuse_client import create_span
            span = create_span(
                name="langgraph_workflow",
                input_data={"query": query[:200], "params": params, "explicit_skill": skill_name},
                metadata={"session_id": session_id, "workflow": "pm_skills_langgraph"},
                parent_trace=parent_trace,  # Use explicit parent from orchestrator
                session_id=session_id  # CRITICAL: Same session as parent
            )
            # Set as current trace so child workflow spans nest under it
            if span:
                from utilities.langfuse_client import set_current_trace
                set_current_trace(span)
            logger.debug(f"[{session_id}] Langfuse span created for LangGraph workflow with parent")
        except Exception as e:
            logger.debug(f"[{session_id}] Langfuse span creation failed: {e}")
    
    # Initialize state
    initial_state: SkillWorkflowState = {
        "query": query,
        "params": params or {},
        "session_id": session_id,
        "matched_skill": skill_name,  # Pre-set if explicit skill provided
        "skill_confidence": 1.0 if skill_name else 0.0,  # High confidence if explicit
        "routing_metadata": {"explicit_skill": True} if skill_name else {},
        "execution_result": None,
        "execution_metadata": {},
        "response": {},
        "errors": [],
        "next_action": "execute_skill" if skill_name else None  # Skip matching if skill provided
    }
    
    # Run the workflow - use unique thread_id per invocation to prevent
    # MemorySaver checkpoint reuse (which can cause hangs on END state)
    import uuid
    unique_thread_id = f"{session_id}_{uuid.uuid4().hex[:8]}"
    config = {"configurable": {"thread_id": unique_thread_id}}
    
    try:
        final_state = await workflow.ainvoke(initial_state, config)
        response = final_state["response"]
        
        # Finalize span with success
        if span:
            try:
                from utilities.langfuse_client import finalize_span
                finalize_span(span, output=response, status="success")
                logger.debug(f"[{session_id}] Langfuse span finalized successfully")
            except Exception as e:
                logger.debug(f"[{session_id}] Langfuse span finalization failed: {e}")
        
        return response
    except Exception as e:
        logger.exception(f"[{session_id}] Workflow execution failed: {e}")
        
        error_response = {
            "success": False,
            "error": str(e),
            "metadata": {
                "workflow": "langgraph",
                "error_type": "workflow_execution_error"
            }
        }
        
        # Finalize span with error
        if span:
            try:
                from utilities.langfuse_client import finalize_span
                finalize_span(span, output=error_response, status="error", level="ERROR")
                logger.debug(f"[{session_id}] Langfuse span finalized with error")
            except Exception as span_error:
                logger.debug(f"[{session_id}] Langfuse span finalization failed: {span_error}")
        
        return error_response
