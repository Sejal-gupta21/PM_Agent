"""
Rollback Manager for Multi-Tool Execution.

This module provides rollback capabilities for multi-tool execution plans
when a step fails after previous steps have made changes.

Architecture Position:
    MultiToolOrchestrator → RollbackManager → MCP Connector

Features:
1. Track steps with side effects during execution
2. Generate rollback operations from step results
3. Execute rollback in reverse order
4. Provide rollback status report

Limitations:
- Cannot rollback all operations (some ADO changes are permanent)
- Best-effort rollback - may partially fail
- Only handles known tool patterns

Usage:
    from agents.pm_agent.rollback_manager import RollbackManager
    
    manager = RollbackManager(mcp_connector)
    
    # Track a completed step with side effects
    manager.track_step(step, result)
    
    # On failure, attempt rollback
    rollback_result = await manager.rollback_all()
"""

import logging
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger("agents.pm_agent.rollback_manager")


class RollbackStatus(Enum):
    """Status of a rollback operation."""
    NOT_NEEDED = "not_needed"       # No side effects to rollback
    SUCCESS = "success"             # All rollbacks succeeded
    PARTIAL = "partial"             # Some rollbacks succeeded
    FAILED = "failed"               # All rollbacks failed
    NOT_POSSIBLE = "not_possible"   # Operation cannot be rolled back


@dataclass
class RollbackableStep:
    """A step that can potentially be rolled back."""
    step_id: str
    tool_name: str
    original_args: Dict[str, Any]
    result: Dict[str, Any]
    
    # Rollback configuration
    rollback_tool: Optional[str] = None
    rollback_args: Optional[Dict[str, Any]] = None
    can_rollback: bool = False


@dataclass
class RollbackResult:
    """Result of a rollback operation."""
    status: RollbackStatus
    rolled_back: List[str] = field(default_factory=list)     # step_ids successfully rolled back
    failed: List[str] = field(default_factory=list)          # step_ids that failed to rollback
    not_possible: List[str] = field(default_factory=list)    # step_ids that can't be rolled back
    errors: Dict[str, str] = field(default_factory=dict)     # step_id → error message
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for logging/response."""
        return {
            "status": self.status.value,
            "rolled_back": self.rolled_back,
            "failed": self.failed,
            "not_possible": self.not_possible,
            "errors": self.errors
        }


class RollbackManager:
    """
    Manages rollback operations for multi-tool execution.
    
    Maintains a stack of completed steps with side effects
    and can attempt to undo them in reverse order.
    """
    
    # Tool patterns that can be rolled back
    # Maps: tool_name → (rollback_tool, args_builder_fn)
    ROLLBACK_PATTERNS: Dict[str, Dict[str, Any]] = {
        # Create operations can be rolled back with delete
        "wit_create_work_item": {
            "rollback_tool": "wit_delete_work_item",
            "args_builder": lambda args, result: {
                "id": result.get("id") or result.get("fields", {}).get("System.Id"),
                "project": args.get("project")
            }
        },
        # Update operations - complex, need to store previous state
        "wit_update_work_item": {
            "rollback_tool": "wit_update_work_item",
            "can_rollback": False,  # Would need previous state
            "reason": "Cannot restore previous state without snapshot"
        },
        # Link operations can be unlinked
        "wit_add_link": {
            "rollback_tool": "wit_remove_link",
            "args_builder": lambda args, result: args  # Same args to remove
        },
        # Comment operations - cannot delete comments in ADO
        "wit_add_comment": {
            "can_rollback": False,
            "reason": "ADO does not support comment deletion"
        },
        # Assignment changes - would need previous assignee
        "wit_assign_work_item": {
            "can_rollback": False,
            "reason": "Would need previous assignee to restore"
        }
    }
    
    # Tools that are purely read operations (no rollback needed)
    READ_ONLY_TOOLS = {
        "wit_get_work_item",
        "wit_get_work_items",
        "execute_wiql",
        "search_workitem",
        "core_list_projects",
        "core_list_project_teams",
        "work_list_team_iterations",
        "work_list_iterations",
        "wit_get_work_items_for_iteration",
        "wit_list_area_paths",
        "wit_list_iteration_paths",
        "work_get_team_settings",
    }
    
    def __init__(self, mcp_connector):
        """
        Initialize the rollback manager.
        
        Args:
            mcp_connector: MCP connector for executing rollback tools
        """
        self.mcp_connector = mcp_connector
        self.rollback_stack: List[RollbackableStep] = []
    
    def clear(self):
        """Clear the rollback stack for a new execution."""
        self.rollback_stack.clear()
    
    def track_step(
        self,
        step_id: str,
        tool_name: str,
        args: Dict[str, Any],
        result: Dict[str, Any]
    ) -> bool:
        """
        Track a completed step for potential rollback.
        
        Args:
            step_id: Unique step identifier
            tool_name: Name of the executed tool
            args: Original arguments used
            result: Result from tool execution
            
        Returns:
            True if step was tracked (has side effects)
        """
        # Skip read-only tools
        if tool_name in self.READ_ONLY_TOOLS:
            logger.debug(f"[ROLLBACK] Skipping read-only tool: {tool_name}")
            return False
        
        # Check if we have a rollback pattern
        pattern = self.ROLLBACK_PATTERNS.get(tool_name)
        
        rollbackable = RollbackableStep(
            step_id=step_id,
            tool_name=tool_name,
            original_args=args,
            result=result
        )
        
        if pattern:
            rollbackable.rollback_tool = pattern.get("rollback_tool")
            rollbackable.can_rollback = pattern.get("can_rollback", True)
            
            # Build rollback args if possible
            if rollbackable.can_rollback and pattern.get("args_builder"):
                try:
                    rollbackable.rollback_args = pattern["args_builder"](args, result)
                except Exception as e:
                    logger.warning(f"[ROLLBACK] Failed to build rollback args for {tool_name}: {e}")
                    rollbackable.can_rollback = False
        else:
            # Unknown tool with potential side effects
            logger.warning(f"[ROLLBACK] Unknown tool with potential side effects: {tool_name}")
            rollbackable.can_rollback = False
        
        self.rollback_stack.append(rollbackable)
        logger.info(f"[ROLLBACK] Tracked step {step_id} ({tool_name}), can_rollback={rollbackable.can_rollback}")
        return True
    
    async def rollback_all(self) -> RollbackResult:
        """
        Attempt to rollback all tracked steps in reverse order.
        
        Returns:
            RollbackResult with status and details
        """
        if not self.rollback_stack:
            logger.info("[ROLLBACK] No steps to rollback")
            return RollbackResult(status=RollbackStatus.NOT_NEEDED)
        
        result = RollbackResult(status=RollbackStatus.SUCCESS)
        
        # Process in reverse order (LIFO)
        for step in reversed(self.rollback_stack):
            if not step.can_rollback:
                result.not_possible.append(step.step_id)
                if step.tool_name in self.ROLLBACK_PATTERNS:
                    reason = self.ROLLBACK_PATTERNS[step.tool_name].get("reason", "No rollback available")
                else:
                    reason = "Unknown tool - rollback pattern not defined"
                result.errors[step.step_id] = reason
                logger.warning(f"[ROLLBACK] Cannot rollback {step.step_id}: {reason}")
                continue
            
            if not step.rollback_tool or not step.rollback_args:
                result.not_possible.append(step.step_id)
                result.errors[step.step_id] = "Missing rollback configuration"
                logger.warning(f"[ROLLBACK] Missing rollback config for {step.step_id}")
                continue
            
            # Attempt rollback
            try:
                logger.info(f"[ROLLBACK] Rolling back {step.step_id} using {step.rollback_tool}")
                
                rollback_response = await self.mcp_connector.call_tool(
                    step.rollback_tool,
                    step.rollback_args
                )
                
                # Check if rollback succeeded
                if rollback_response and not isinstance(rollback_response, str):
                    rollback_data = rollback_response
                elif isinstance(rollback_response, str):
                    try:
                        import json
                        rollback_data = json.loads(rollback_response)
                    except:
                        rollback_data = {"response": rollback_response}
                else:
                    rollback_data = {}
                
                # Consider success if no error
                if rollback_data.get("error"):
                    raise Exception(rollback_data["error"])
                
                result.rolled_back.append(step.step_id)
                logger.info(f"[ROLLBACK] Successfully rolled back {step.step_id}")
                
            except Exception as e:
                result.failed.append(step.step_id)
                result.errors[step.step_id] = str(e)
                logger.error(f"[ROLLBACK] Failed to rollback {step.step_id}: {e}")
        
        # Determine overall status
        if result.failed:
            if result.rolled_back:
                result.status = RollbackStatus.PARTIAL
            else:
                result.status = RollbackStatus.FAILED
        elif result.not_possible and not result.rolled_back:
            result.status = RollbackStatus.NOT_POSSIBLE
        
        logger.info(f"[ROLLBACK] Rollback complete: {result.status.value}")
        return result
    
    def get_pending_rollbacks(self) -> List[Dict[str, Any]]:
        """Get list of steps that would be rolled back."""
        return [
            {
                "step_id": step.step_id,
                "tool_name": step.tool_name,
                "can_rollback": step.can_rollback,
                "rollback_tool": step.rollback_tool
            }
            for step in reversed(self.rollback_stack)
        ]


# Singleton instance
_manager: Optional[RollbackManager] = None


def get_rollback_manager(mcp_connector=None) -> RollbackManager:
    """Get singleton rollback manager instance."""
    global _manager
    if _manager is None:
        if mcp_connector is None:
            raise ValueError("mcp_connector required for first initialization")
        _manager = RollbackManager(mcp_connector)
    return _manager
