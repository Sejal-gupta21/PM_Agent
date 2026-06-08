"""
Synthesis Layer - Unified response formatting for orchestrator.

This module provides centralized response synthesis for ALL orchestrator outcomes:
- Guardrails BLOCK → synthesize("blocked")
- Agent SUCCESS → synthesize("success")
- Agent FAILED → synthesize("failed")
- Agent NEEDS_DEEP_ANALYSIS → Deep LLM Replan → synthesize("replanned")

Architecture Position:
    Controller → Orchestrator → [Guardrails] → [Intent Resolution] → [Agent Dispatch]
                                    ↓                                     ↓
                               BLOCK path                           Agent returns
                                    ↓                                     ↓
                              ┌─────┴─────────────────────────────────────┘
                              ↓
                         [SYNTHESIS] ← ALL PATHS CONVERGE HERE
                              ↓
                       Final Response

KEY RESPONSIBILITIES:
- Format responses consistently for UI/clients
- Preserve agent metadata (confidence, warnings, etc.)
- Handle all status types uniformly
- Provide Langfuse span for synthesis step

NON-RESPONSIBILITIES:
- NO LLM calls (pure formatting)
- NO agent logic
- NO routing decisions
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List
from enum import Enum

from utilities.langfuse_client import create_span, finalize_span

logger = logging.getLogger("orchestrator.synthesis")


# ══════════════════════════════════════════════════════════════════════════════
# WORK ITEM FORMATTING HELPERS
# These functions format work items from ADO into user-friendly output
# ══════════════════════════════════════════════════════════════════════════════

def format_work_items(work_items: List[Dict[str, Any]], filters: Optional[Dict] = None) -> str:
    """
    Format work items array into user-friendly response.
    
    This is the CORRECT output format for sprint/work item queries.
    It produces: "Found N items:\n1. [ID] Title\n   Type: X | State: Y | Assigned: Z"
    
    Args:
        work_items: List of work item dicts with {id, fields: {System.Title, ...}}
        filters: Optional filters to re-apply during formatting (workItemType, state, priority)
        
    Returns:
        Formatted string response with work item details
    """
    if not work_items:
        return "No work items found matching the criteria."
    
    # Apply filters if provided
    filtered_items = work_items
    if filters:
        filtered_items = _apply_work_item_filters(work_items, filters)
    
    if not filtered_items:
        filter_desc = ", ".join(f"{k}={v}" for k, v in (filters or {}).items())
        return f"No work items found matching the specified filters ({filter_desc})."
    
    # Build formatted response
    lines = [f"Found {len(filtered_items)} items:\n"]
    
    for idx, item in enumerate(filtered_items, 1):
        # Handle different item structures (direct fields vs nested)
        item_id = item.get("id") or item.get("System.Id")
        
        # Try multiple field access patterns
        fields = item.get("fields", {})
        if not fields and "System.Title" in item:
            # Fields are at top level
            fields = item
        
        title = (
            fields.get("System.Title") or 
            item.get("title") or 
            item.get("System.Title") or 
            "Unknown"
        )
        work_type = (
            fields.get("System.WorkItemType") or 
            item.get("workItemType") or 
            item.get("System.WorkItemType") or 
            "Unknown"
        )
        state = (
            fields.get("System.State") or 
            item.get("state") or 
            item.get("System.State") or 
            "Unknown"
        )
        
        # Handle assigned to (can be dict or string)
        assigned_to = fields.get("System.AssignedTo") or item.get("assignedTo", {})
        if isinstance(assigned_to, dict):
            assigned_to_name = assigned_to.get("displayName") or assigned_to.get("uniqueName", "Unassigned")
        elif isinstance(assigned_to, str):
            assigned_to_name = assigned_to
        else:
            assigned_to_name = "Unassigned"
        
        priority = fields.get("Microsoft.VSTS.Common.Priority") or item.get("priority", "")
        priority_str = f" | Priority: {priority}" if priority else ""
        
        # Format line
        lines.append(f"{idx}. [{item_id}] {title}")
        lines.append(f"   Type: {work_type} | State: {state}{priority_str}")
        lines.append(f"   Assigned to: {assigned_to_name}")
        lines.append("")
    
    return "\n".join(lines)


def _apply_work_item_filters(
    work_items: List[Dict[str, Any]],
    filters: Dict[str, Any]
) -> List[Dict[str, Any]]:
    """
    Filter work items based on provided criteria.
    
    Args:
        work_items: List of work items
        filters: {"workItemType": "Bug", "state": "Active", "priority": 1}
        
    Returns:
        Filtered list
    """
    result = []
    
    for item in work_items:
        fields = item.get("fields", {})
        if not fields and "System.Title" in item:
            fields = item
        
        matches = True
        
        # Check workItemType filter
        if "workItemType" in filters:
            item_type = fields.get("System.WorkItemType") or item.get("workItemType")
            if item_type and item_type.lower() != filters["workItemType"].lower():
                matches = False
        
        # Check state filter
        if "state" in filters and matches:
            item_state = fields.get("System.State") or item.get("state")
            if item_state and item_state.lower() != filters["state"].lower():
                matches = False
        
        # Check priority filter
        if "priority" in filters and matches:
            item_priority = fields.get("Microsoft.VSTS.Common.Priority") or item.get("priority")
            if item_priority and item_priority != filters["priority"]:
                matches = False
        
        if matches:
            result.append(item)
    
    return result


def is_work_items_array(content: Any) -> bool:
    """
    Check if content appears to be a work items array.
    
    Returns True if content is a list of dicts with work item-like structure.
    """
    if not isinstance(content, list):
        return False
    if not content:
        return False
    if not isinstance(content[0], dict):
        return False
    
    # Check for work item indicators
    first_item = content[0]
    work_item_indicators = [
        "id" in first_item,
        "fields" in first_item,
        "System.Title" in first_item,
        "System.WorkItemType" in first_item,
        "workItemType" in first_item,
    ]
    
    return any(work_item_indicators)


def is_iteration_metadata(content: Any) -> bool:
    """
    Check if content appears to be iteration/sprint metadata (NOT work items).
    
    Returns True if content looks like sprint list rather than work items.
    This helps detect when wrong data type is being synthesized.
    """
    if not isinstance(content, list):
        return False
    if not content:
        return False
    
    # Check for iteration indicators
    first_item = content[0] if isinstance(content[0], dict) else {}
    iteration_indicators = [
        "path" in first_item and "attributes" in first_item,
        "startDate" in first_item and "finishDate" in first_item,
        isinstance(content[0], str) and any(x in str(content[0]) for x in ["past", "current", "future"]),
    ]
    
    return any(iteration_indicators)


class AgentStatus(Enum):
    """Status codes returned by agents."""
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    NEEDS_DEEP_ANALYSIS = "NEEDS_DEEP_ANALYSIS"
    # B4: Self-correction status codes
    REQUEST_REPLAN = "REQUEST_REPLAN"              # Agent requests re-planning with different tool
    REQUEST_ESCALATION = "REQUEST_ESCALATION"      # Agent requests escalation to different agent


class SynthesisStatus(Enum):
    """Synthesis status for final response."""
    BLOCKED = "blocked"           # Guardrails blocked the request
    SUCCESS = "success"           # Agent completed successfully
    FAILED = "failed"             # Agent failed, no replan
    REPLANNED = "replanned"       # Completed after deep LLM replan
    # B4: Self-correction synthesis statuses
    SELF_CORRECTED = "self_corrected"  # Completed after agent-initiated replan
    ESCALATED = "escalated"            # Completed after escalation to different agent


@dataclass
class AgentResult:
    """
    Standardized agent result structure.
    
    All agents MUST return this structure (or dict with same keys).
    This enables the orchestrator to handle all agents uniformly.
    """
    is_task_complete: bool = True
    status: AgentStatus = AgentStatus.SUCCESS
    content: Any = None
    error: Optional[str] = None
    confidence: float = 1.0
    
    # Deep analysis context (only if status == NEEDS_DEEP_ANALYSIS)
    deep_analysis_context: Optional[Dict[str, Any]] = None
    
    # Metadata for tracing/UI
    metadata: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AgentResult":
        """Create AgentResult from dict (for backward compatibility)."""
        # Handle old format without status field
        status_str = data.get("status")
        
        if status_str is None:
            # No explicit status - infer from other fields
            if data.get("error"):
                status = AgentStatus.FAILED
            elif data.get("deep_analysis_context"):
                status = AgentStatus.NEEDS_DEEP_ANALYSIS
            else:
                status = AgentStatus.SUCCESS
        elif isinstance(status_str, str):
            try:
                status = AgentStatus(status_str.upper())
            except ValueError:
                # Invalid status string - fallback
                if data.get("error"):
                    status = AgentStatus.FAILED
                elif data.get("deep_analysis_context"):
                    status = AgentStatus.NEEDS_DEEP_ANALYSIS
                else:
                    status = AgentStatus.SUCCESS
        else:
            status = status_str
        
        return cls(
            is_task_complete=data.get("is_task_complete", True),
            status=status,
            content=data.get("content"),
            error=data.get("error"),
            confidence=data.get("confidence", 1.0),
            deep_analysis_context=data.get("deep_analysis_context"),
            metadata=data.get("metadata", {}),
            warnings=data.get("warnings", [])
        )
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization."""
        result = {
            "is_task_complete": self.is_task_complete,
            "status": self.status.value if isinstance(self.status, AgentStatus) else self.status,
            "content": self.content,
            "confidence": self.confidence,
        }
        if self.error:
            result["error"] = self.error
        if self.deep_analysis_context:
            result["deep_analysis_context"] = self.deep_analysis_context
        if self.metadata:
            result["metadata"] = self.metadata
        if self.warnings:
            result["warnings"] = self.warnings
        return result


@dataclass
class SynthesisResult:
    """Final synthesized response."""
    is_task_complete: bool
    content: Any
    status: SynthesisStatus
    confidence: float
    metadata: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    warnings: List[str] = field(default_factory=list)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON serialization."""
        result = {
            "is_task_complete": self.is_task_complete,
            "content": self.content,
            "status": self.status.value,
            "confidence": self.confidence,
        }
        if self.metadata:
            result["metadata"] = self.metadata
        if self.error:
            result["error"] = self.error
        if self.warnings:
            result["warnings"] = self.warnings
        return result


class Synthesizer:
    """
    Unified response synthesis layer.
    
    All orchestrator paths converge here for consistent response formatting.
    This is a pure formatting layer - NO LLM calls, NO agent logic.
    """
    
    def __init__(self):
        logger.debug("Synthesizer initialized")
    
    def synthesize(
        self,
        status: SynthesisStatus,
        content: Any = None,
        error: Optional[str] = None,
        agent_result: Optional[AgentResult] = None,
        metadata: Optional[Dict[str, Any]] = None,
        parent_trace: Optional[Any] = None,
        session_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Synthesize final response based on execution status.
        
        This method handles ALL orchestrator outcomes:
        - BLOCKED: Guardrails rejected the request
        - SUCCESS: Agent completed successfully
        - FAILED: Agent failed, no replan was possible
        - REPLANNED: Completed after deep LLM replanning
        
        Args:
            status: Final execution status
            content: Response content (overrides agent_result.content if provided)
            error: Error message (overrides agent_result.error if provided)
            agent_result: Agent result object (if available)
            metadata: Additional metadata to include
            parent_trace: Parent trace for Langfuse span
            session_id: Session ID for tracing
            
        Returns:
            Dict with synthesized response (ready for UI/client)
        """
        # Create synthesis span
        synth_span = None
        if parent_trace:
            synth_span = create_span(
                name="orchestrator.synthesis",
                input_data={"status": status.value},
                metadata={"step": "synthesis"},
                parent_trace=parent_trace,
                session_id=session_id
            )
        
        try:
            # Build response based on status
            if status == SynthesisStatus.BLOCKED:
                result = self._synthesize_blocked(error, metadata)
            
            elif status == SynthesisStatus.SUCCESS:
                result = self._synthesize_success(content, agent_result, metadata)
            
            elif status == SynthesisStatus.FAILED:
                result = self._synthesize_failed(content, error, agent_result, metadata)
            
            elif status == SynthesisStatus.REPLANNED:
                result = self._synthesize_replanned(content, agent_result, metadata)
            
            else:
                # Unknown status - treat as failed
                logger.warning(f"Unknown synthesis status: {status}")
                result = self._synthesize_failed(
                    content=None,
                    error=f"Unknown synthesis status: {status}",
                    agent_result=agent_result,
                    metadata=metadata
                )
            
            # Finalize span
            if synth_span:
                finalize_span(
                    synth_span,
                    output={"status": status.value, "has_content": bool(result.get("content"))},
                    status="success"
                )
            
            logger.info(f"[SYNTHESIS] Status={status.value}, has_content={bool(result.get('content'))}")
            return result
            
        except Exception as e:
            logger.exception(f"[SYNTHESIS] Error: {e}")
            if synth_span:
                finalize_span(synth_span, output={"error": str(e)}, status="error", level="ERROR")
            
            # Return safe error response
            return {
                "is_task_complete": True,
                "content": f"Error during synthesis: {str(e)}",
                "status": "failed",
                "confidence": 0.0,
                "error": str(e)
            }
    
    def _synthesize_blocked(
        self,
        error: Optional[str],
        metadata: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Synthesize response for blocked requests."""
        return {
            "is_task_complete": True,
            "content": f"I cannot process this request: {error or 'Request blocked by safety filters'}",
            "status": "blocked",
            "confidence": 0.0,
            "error": error,
            "metadata": {
                **(metadata or {}),
                "blocked": True,
                "blocked_reason": error
            }
        }
    
    def _synthesize_success(
        self,
        content: Any,
        agent_result: Optional[AgentResult],
        metadata: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Synthesize response for successful execution.
        
        CRITICAL: This method now detects work items arrays and formats them properly.
        This prevents returning raw JSON or iteration metadata to the user.
        """
        # Use provided content or agent result content
        final_content = content
        if final_content is None and agent_result:
            final_content = agent_result.content
        
        # ═══════════════════════════════════════════════════════════════════════
        # WORK ITEM DETECTION AND FORMATTING
        # If content is a work items array, format it for user display
        # ═══════════════════════════════════════════════════════════════════════
        if is_work_items_array(final_content):
            logger.info(f"[SYNTHESIS] Detected work items array ({len(final_content)} items), formatting...")
            
            # Extract filters from metadata if available
            filters = None
            if metadata and "filters" in metadata:
                filters = metadata["filters"]
            elif agent_result and agent_result.metadata and "filters" in agent_result.metadata:
                filters = agent_result.metadata["filters"]
            
            # Format work items for display
            final_content = format_work_items(final_content, filters)
            logger.info(f"[SYNTHESIS] Formatted work items output: {len(final_content)} chars")
        
        # ═══════════════════════════════════════════════════════════════════════
        # ITERATION METADATA DETECTION (WRONG DATA TYPE)
        # If content looks like iteration metadata, log warning
        # ═══════════════════════════════════════════════════════════════════════
        elif is_iteration_metadata(final_content):
            logger.warning(f"[SYNTHESIS] Detected iteration metadata instead of work items!")
            logger.warning(f"[SYNTHESIS] This suggests a planning/execution bug - wrong tool used")
            # Convert to informative message
            if isinstance(final_content, list):
                final_content = f"Query returned sprint/iteration metadata rather than work items. Found {len(final_content)} iterations. Please try rephrasing your query or contact support if this persists."
        
        # Build metadata
        final_metadata = metadata or {}
        if agent_result and agent_result.metadata:
            final_metadata = {**agent_result.metadata, **final_metadata}
        
        # Get confidence
        confidence = 1.0
        if agent_result:
            confidence = agent_result.confidence
        
        result = {
            "is_task_complete": True,
            "content": final_content,
            "status": "success",
            "confidence": confidence,
        }
        
        if final_metadata:
            result["metadata"] = final_metadata
        
        if agent_result and agent_result.warnings:
            result["warnings"] = agent_result.warnings
        
        return result
    
    def _synthesize_failed(
        self,
        content: Any,
        error: Optional[str],
        agent_result: Optional[AgentResult],
        metadata: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Synthesize response for failed execution."""
        # Use provided error or agent result error
        final_error = error
        if final_error is None and agent_result:
            final_error = agent_result.error
        
        # Use provided content or construct from error
        final_content = content
        if final_content is None:
            if final_error:
                final_content = f"I couldn't complete this request: {final_error}"
            else:
                final_content = "I couldn't complete this request due to an unexpected error."
        
        return {
            "is_task_complete": True,
            "content": final_content,
            "status": "failed",
            "confidence": 0.1,
            "error": final_error,
            "metadata": metadata or {}
        }
    
    def _synthesize_replanned(
        self,
        content: Any,
        agent_result: Optional[AgentResult],
        metadata: Optional[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """Synthesize response for replanned execution."""
        # Use provided content or agent result content
        final_content = content
        if final_content is None and agent_result:
            final_content = agent_result.content
        
        # Build metadata with replan info
        final_metadata = metadata or {}
        if agent_result and agent_result.metadata:
            final_metadata = {**agent_result.metadata, **final_metadata}
        final_metadata["replanned"] = True
        
        # Reduce confidence for replanned results
        confidence = 0.7
        if agent_result:
            confidence = min(agent_result.confidence, 0.7)
        
        result = {
            "is_task_complete": True,
            "content": final_content,
            "status": "replanned",
            "confidence": confidence,
            "metadata": final_metadata
        }
        
        if agent_result and agent_result.warnings:
            result["warnings"] = agent_result.warnings
        
        return result


# Singleton instance
_synthesizer: Optional[Synthesizer] = None


def get_synthesizer() -> Synthesizer:
    """Get the global synthesizer instance."""
    global _synthesizer
    if _synthesizer is None:
        _synthesizer = Synthesizer()
    return _synthesizer


# Helper function for quick synthesis
def synthesize_response(
    status: SynthesisStatus,
    content: Any = None,
    error: Optional[str] = None,
    agent_result: Optional[AgentResult] = None,
    metadata: Optional[Dict[str, Any]] = None,
    parent_trace: Optional[Any] = None,
    session_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Quick synthesis helper function.
    
    Convenience wrapper around Synthesizer.synthesize().
    """
    return get_synthesizer().synthesize(
        status=status,
        content=content,
        error=error,
        agent_result=agent_result,
        metadata=metadata,
        parent_trace=parent_trace,
        session_id=session_id
    )
