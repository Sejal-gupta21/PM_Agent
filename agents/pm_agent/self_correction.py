"""
Agent Self-Correction and Escalation Module.

This module provides intelligent failure recovery for the PM Agent:
1. Failure Analysis - Determine if error is recoverable
2. Replan Suggestions - Suggest alternative tools when current fails
3. Escalation Detection - Identify when different agent is needed
4. Recovery Strategies - Implement recovery without infinite loops

Architecture Position:
    Tool Executor → SelfCorrectionHandler → Agent Response

Integration:
- Called after tool execution fails in agent.py
- Returns self-correction context for orchestrator handling
- Integrates with metrics for observability

Usage:
    from agents.pm_agent.self_correction import SelfCorrectionHandler, FailureType
    
    handler = SelfCorrectionHandler()
    analysis = handler.analyze_failure(
        error=error_message,
        tool_plan=executed_plan,
        query=original_query
    )
    
    if analysis.failure_type == FailureType.WRONG_TOOL:
        # Return REQUEST_REPLAN with context
        return _make_agent_response(
            content=None,
            status="REQUEST_REPLAN",
            replan_context=analysis.to_replan_context()
        )
"""

import re
import logging
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger("agents.pm_agent.self_correction")


class FailureType(Enum):
    """Types of failures that can occur during tool execution."""
    WRONG_TOOL = "wrong_tool"                  # Tool was incorrect for query
    MISSING_ARGS = "missing_args"              # Required arguments not provided
    INVALID_ARGS = "invalid_args"              # Argument values invalid
    PERMISSION_DENIED = "permission_denied"    # User lacks permission
    RESOURCE_NOT_FOUND = "resource_not_found"  # Entity doesn't exist
    API_ERROR = "api_error"                    # ADO API error
    NEEDS_SKILL_AGENT = "needs_skill_agent"    # Query needs PM Skills Agent
    TRANSIENT_ERROR = "transient_error"        # Temporary failure, can retry
    GENUINE_FAILURE = "genuine_failure"        # Non-recoverable failure


class RecoveryAction(Enum):
    """Recommended recovery actions."""
    REPLAN_DIFFERENT_TOOL = "replan_different_tool"
    REPLAN_FIXED_ARGS = "replan_fixed_args"
    RETRY_SAME_TOOL = "retry_same_tool"
    ESCALATE_TO_SKILL_AGENT = "escalate_to_skill_agent"
    ESCALATE_TO_PM_AGENT = "escalate_to_pm_agent"
    RETURN_ERROR = "return_error"
    ASK_CLARIFICATION = "ask_clarification"


@dataclass
class FailureAnalysis:
    """Result of failure analysis."""
    failure_type: FailureType
    recovery_action: RecoveryAction
    error_message: str
    confidence: float = 0.0
    
    # Recovery context
    alternative_tools: List[str] = field(default_factory=list)
    fixed_args: Optional[Dict[str, Any]] = None
    exclude_tools: List[str] = field(default_factory=list)
    target_agent: Optional[str] = None
    clarification_question: Optional[str] = None
    
    # Metadata
    original_tool: Optional[str] = None
    original_error: Optional[str] = None
    
    def to_replan_context(self) -> Dict[str, Any]:
        """Convert to replan context for orchestrator."""
        return {
            "reason": self.failure_type.value,
            "failed_tool": self.original_tool,
            "error": self.original_error,
            "exclude_tools": self.exclude_tools,
            "suggest_tools": self.alternative_tools,
            "fixed_args": self.fixed_args,
            "confidence": self.confidence
        }
    
    def to_escalation_context(self) -> Dict[str, Any]:
        """Convert to escalation context for orchestrator."""
        return {
            "target_agent": self.target_agent,
            "reason": self.failure_type.value,
            "attempted_tool": self.original_tool,
            "original_error": self.original_error
        }


class SelfCorrectionHandler:
    """
    Analyzes tool execution failures and determines recovery strategy.
    
    Implements:
    1. Error pattern matching to classify failures
    2. Tool alternative suggestions based on query intent
    3. Skill agent escalation detection
    4. Confidence-based recovery decisions
    """
    
    # Error patterns indicating wrong tool was selected
    WRONG_TOOL_PATTERNS = [
        (r"not found", FailureType.RESOURCE_NOT_FOUND),
        (r"does not exist", FailureType.RESOURCE_NOT_FOUND),
        (r"invalid.*id", FailureType.INVALID_ARGS),
        (r"invalid.*path", FailureType.INVALID_ARGS),
        (r"required.*missing", FailureType.MISSING_ARGS),
        (r"parameter.*required", FailureType.MISSING_ARGS),
        (r"unauthorized|forbidden|permission denied", FailureType.PERMISSION_DENIED),
        (r"access denied", FailureType.PERMISSION_DENIED),
        (r"timeout|timed out", FailureType.TRANSIENT_ERROR),
        (r"rate limit", FailureType.TRANSIENT_ERROR),
        (r"server error|internal error|500|503", FailureType.TRANSIENT_ERROR),
        (r"bad request|400", FailureType.INVALID_ARGS),
        (r"no .* found|empty result", FailureType.RESOURCE_NOT_FOUND),
    ]
    
    # Tool fallback mappings (tool -> alternatives)
    TOOL_ALTERNATIVES = {
        "wit_get_work_items_for_iteration": ["execute_wiql", "search_workitem"],
        "work_get_iteration_work_items": ["execute_wiql", "search_workitem"],
        "search_workitem": ["execute_wiql"],
        "execute_wiql": ["search_workitem"],
        "wit_get_work_item": ["wit_get_work_items", "search_workitem"],
        "wit_get_work_items": ["execute_wiql"],
        "core_list_project_teams": ["core_list_projects"],
        "work_list_team_iterations": ["work_list_iterations"],
    }
    
    # Patterns indicating need for PM Skills Agent
    SKILL_AGENT_PATTERNS = [
        r"iteration\s+report",
        r"recurring\s+bugs?",
        r"bug\s+patterns?",
        r"overlooked\s+stories",
        r"stale\s+items",
        r"skill\s+matrix",
        r"developer\s+(skills?|expertise)",
        r"send\s+email",
        r"generate\s+report",
        r"billing\s+deviation",
    ]
    
    def __init__(self):
        """Initialize the handler."""
        # Compile patterns
        self._wrong_tool_patterns = [
            (re.compile(pattern, re.IGNORECASE), failure_type)
            for pattern, failure_type in self.WRONG_TOOL_PATTERNS
        ]
        self._skill_agent_patterns = [
            re.compile(pattern, re.IGNORECASE)
            for pattern in self.SKILL_AGENT_PATTERNS
        ]
    
    def analyze_failure(
        self,
        error: str,
        tool_plan: Dict[str, Any],
        query: str,
        retry_count: int = 0,
        max_retries: int = 1
    ) -> FailureAnalysis:
        """
        Analyze a tool execution failure and determine recovery strategy.
        
        Args:
            error: Error message from tool execution
            tool_plan: The plan that failed (contains tool, args)
            query: Original user query
            retry_count: Number of retries already attempted
            max_retries: Maximum allowed retries
            
        Returns:
            FailureAnalysis with recovery recommendation
        """
        tool = tool_plan.get("tool", "unknown")
        args = tool_plan.get("args", {})
        error_lower = error.lower() if error else ""
        
        logger.info(f"[SELF_CORRECTION] Analyzing failure for {tool}: {error[:100] if error else 'No error'}")
        
        # Step 1: Classify the failure type
        failure_type = self._classify_failure(error_lower)
        
        # Step 2: Check if query needs skill agent
        if self._needs_skill_agent(query):
            logger.info(f"[SELF_CORRECTION] Query matches skill agent patterns")
            return FailureAnalysis(
                failure_type=FailureType.NEEDS_SKILL_AGENT,
                recovery_action=RecoveryAction.ESCALATE_TO_SKILL_AGENT,
                error_message=error,
                confidence=0.8,
                target_agent="pm_skill_agent",
                original_tool=tool,
                original_error=error
            )
        
        # Step 3: Determine recovery action based on failure type
        if failure_type == FailureType.TRANSIENT_ERROR:
            if retry_count < max_retries:
                return FailureAnalysis(
                    failure_type=failure_type,
                    recovery_action=RecoveryAction.RETRY_SAME_TOOL,
                    error_message=error,
                    confidence=0.7,
                    original_tool=tool,
                    original_error=error
                )
        
        if failure_type in (FailureType.WRONG_TOOL, FailureType.RESOURCE_NOT_FOUND, FailureType.INVALID_ARGS):
            alternatives = self._get_alternative_tools(tool, query)
            if alternatives:
                logger.info(f"[SELF_CORRECTION] Suggesting alternatives: {alternatives}")
                return FailureAnalysis(
                    failure_type=failure_type,
                    recovery_action=RecoveryAction.REPLAN_DIFFERENT_TOOL,
                    error_message=error,
                    confidence=0.75,
                    alternative_tools=alternatives,
                    exclude_tools=[tool],
                    original_tool=tool,
                    original_error=error
                )
        
        if failure_type == FailureType.MISSING_ARGS:
            fixed = self._try_fix_args(args, error, query)
            if fixed:
                return FailureAnalysis(
                    failure_type=failure_type,
                    recovery_action=RecoveryAction.REPLAN_FIXED_ARGS,
                    error_message=error,
                    confidence=0.7,
                    fixed_args=fixed,
                    original_tool=tool,
                    original_error=error
                )
        
        if failure_type == FailureType.PERMISSION_DENIED:
            return FailureAnalysis(
                failure_type=failure_type,
                recovery_action=RecoveryAction.RETURN_ERROR,
                error_message=f"Permission denied: {error}",
                confidence=0.9,
                original_tool=tool,
                original_error=error
            )
        
        # Default: genuine failure
        return FailureAnalysis(
            failure_type=FailureType.GENUINE_FAILURE,
            recovery_action=RecoveryAction.RETURN_ERROR,
            error_message=error,
            confidence=0.9,
            original_tool=tool,
            original_error=error
        )
    
    def _classify_failure(self, error_lower: str) -> FailureType:
        """Classify the failure type based on error message."""
        for pattern, failure_type in self._wrong_tool_patterns:
            if pattern.search(error_lower):
                return failure_type
        return FailureType.GENUINE_FAILURE
    
    def _needs_skill_agent(self, query: str) -> bool:
        """Check if query matches PM Skills Agent patterns."""
        for pattern in self._skill_agent_patterns:
            if pattern.search(query):
                return True
        return False
    
    def _get_alternative_tools(self, failed_tool: str, query: str) -> List[str]:
        """Get alternative tools for a failed tool."""
        alternatives = list(self.TOOL_ALTERNATIVES.get(failed_tool, []))
        
        # Add query-based suggestions
        query_lower = query.lower()
        
        if "wiql" in query_lower or "query" in query_lower:
            if "execute_wiql" not in alternatives:
                alternatives.insert(0, "execute_wiql")
        
        if "search" in query_lower or "find" in query_lower:
            if "search_workitem" not in alternatives:
                alternatives.insert(0, "search_workitem")
        
        if "iteration" in query_lower or "sprint" in query_lower:
            if "wit_get_work_items_for_iteration" not in alternatives:
                alternatives.append("wit_get_work_items_for_iteration")
        
        return alternatives[:3]  # Limit to top 3
    
    def _try_fix_args(
        self,
        args: Dict[str, Any],
        error: str,
        query: str
    ) -> Optional[Dict[str, Any]]:
        """Try to fix missing/invalid arguments."""
        fixed = dict(args)
        error_lower = error.lower()
        
        # Try to extract missing project from query
        if "project" in error_lower and "project" not in fixed:
            # Look for project name patterns
            project_match = re.search(r'(FracPro[-\s]?\w+|XOPS)', query, re.IGNORECASE)
            if project_match:
                fixed["project"] = project_match.group(1)
                return fixed
        
        # Try to fix iteration path
        if "iteration" in error_lower:
            if "iterationId" in fixed and fixed["iterationId"].startswith("@"):
                # Macro wasn't resolved - return None to trigger replan
                return None
        
        return None


# Singleton instance
_handler: Optional[SelfCorrectionHandler] = None


def get_self_correction_handler() -> SelfCorrectionHandler:
    """Get singleton handler instance."""
    global _handler
    if _handler is None:
        _handler = SelfCorrectionHandler()
    return _handler


def analyze_failure(
    error: str,
    tool_plan: Dict[str, Any],
    query: str,
    retry_count: int = 0
) -> FailureAnalysis:
    """Convenience function to analyze a failure."""
    return get_self_correction_handler().analyze_failure(
        error=error,
        tool_plan=tool_plan,
        query=query,
        retry_count=retry_count
    )
