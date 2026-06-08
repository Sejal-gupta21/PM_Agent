"""
Tool Dispatcher - Routes tool execution to MCP or PM Skill handlers.

This module provides a unified interface for executing both:
- MCP tools (Azure DevOps API via MCP connector)
- PM Skill tools (Business logic in PM Skill Agent)

It handles:
- Tool type detection and routing
- Argument resolution (variable references like ${step_id.field})
- Execution context management
- Error handling and retries
- Result standardization
"""

import re
import json
import logging
from typing import Dict, Any, Optional, List
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger("pm_agent.tool_dispatcher")


class ToolType(Enum):
    """Types of tools that can be dispatched."""
    MCP = "mcp"  # Azure DevOps MCP tools
    PM_SKILL = "pm_skill"  # PM Skill Agent business logic
    UNKNOWN = "unknown"


@dataclass
class DispatchResult:
    """Result from dispatching a tool."""
    success: bool
    tool_name: str
    tool_type: ToolType
    result: Any = None
    error: Optional[str] = None
    execution_time_ms: int = 0
    metadata: Dict[str, Any] = None
    
    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


class ToolDispatcher:
    """
    Dispatcher for routing tool execution to appropriate handlers.
    
    Handles both MCP tools (via ToolExecutor) and PM Skill tools (via PM Skill Agent).
    """
    
    def __init__(
        self,
        tool_executor,  # ToolExecutor instance for MCP tools
        pm_skill_agent,  # PM Skill Agent instance for skill tools
        mcp_tool_registry: Dict[str, Dict[str, Any]],
        pm_skill_registry: Dict[str, Dict[str, Any]]
    ):
        """
        Initialize tool dispatcher.
        
        Args:
            tool_executor: ToolExecutor instance for MCP tool calls
            pm_skill_agent: PM Skill Agent instance for skill execution
            mcp_tool_registry: MCP_TOOL_REGISTRY from utilities.mcp.tool_registry
            pm_skill_registry: SKILL_DEFINITIONS from agents.pm_skill_agent.skills
        """
        self.tool_executor = tool_executor
        self.pm_skill_agent = pm_skill_agent
        self.mcp_registry = mcp_tool_registry
        self.pm_skill_registry = pm_skill_registry
        
        logger.info(
            f"Tool dispatcher initialized: {len(mcp_tool_registry)} MCP tools, "
            f"{len(pm_skill_registry)} PM skills"
        )
    
    async def dispatch(
        self,
        tool_name: str,
        args: Dict[str, Any],
        execution_context: Optional[Dict[str, Any]] = None,
        tool_type: Optional[ToolType] = None
    ) -> DispatchResult:
        """
        Dispatch tool execution to appropriate handler.
        
        Args:
            tool_name: Name of the tool to execute
            args: Arguments for the tool (may contain variable references)
            execution_context: Context with previous step results for variable resolution
            tool_type: Optional explicit tool type (auto-detected if None)
        
        Returns:
            DispatchResult with execution outcome
        """
        import time
        start_time = time.time()
        
        try:
            # Detect tool type if not specified
            if tool_type is None:
                tool_type = self._detect_tool_type(tool_name)
            
            logger.info(f"Dispatching {tool_type.value} tool: {tool_name}")
            
            # Resolve variable references in args
            resolved_args = self._resolve_args(args, execution_context or {})
            
            # Dispatch to appropriate handler
            if tool_type == ToolType.MCP:
                result = await self._execute_mcp_tool(tool_name, resolved_args)
            elif tool_type == ToolType.PM_SKILL:
                result = await self._execute_pm_skill(tool_name, resolved_args)
            else:
                raise ValueError(f"Unknown tool type for: {tool_name}")
            
            execution_time_ms = int((time.time() - start_time) * 1000)
            
            logger.info(
                f"Tool {tool_name} completed successfully in {execution_time_ms}ms"
            )
            
            return DispatchResult(
                success=True,
                tool_name=tool_name,
                tool_type=tool_type,
                result=result,
                execution_time_ms=execution_time_ms,
                metadata={"resolved_args": resolved_args}
            )
            
        except Exception as e:
            execution_time_ms = int((time.time() - start_time) * 1000)
            error_msg = str(e)
            
            logger.error(
                f"Tool {tool_name} failed after {execution_time_ms}ms: {error_msg}",
                exc_info=True
            )
            
            return DispatchResult(
                success=False,
                tool_name=tool_name,
                tool_type=tool_type or ToolType.UNKNOWN,
                error=error_msg,
                execution_time_ms=execution_time_ms,
                metadata={"exception_type": type(e).__name__}
            )
    
    def _detect_tool_type(self, tool_name: str) -> ToolType:
        """
        Detect tool type based on name and registries.
        
        Args:
            tool_name: Name of the tool
        
        Returns:
            ToolType enum value
        """
        # Check PM Skill registry first (more specific)
        if tool_name in self.pm_skill_registry:
            return ToolType.PM_SKILL
        
        # Check MCP registry
        if tool_name in self.mcp_registry:
            return ToolType.MCP
        
        # Heuristic: MCP tools often have prefixes
        mcp_prefixes = ["wit_", "work_", "core_", "repo_", "search_"]
        if any(tool_name.startswith(prefix) for prefix in mcp_prefixes):
            return ToolType.MCP
        
        logger.warning(f"Could not detect tool type for: {tool_name}, defaulting to MCP")
        return ToolType.MCP
    
    def _resolve_args(
        self,
        args: Dict[str, Any],
        execution_context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Resolve variable references in arguments.
        
        Variable references have format: ${step_id.field_name}
        Example: ${velocity_data.velocity} → 200.5 from execution_context
        
        Args:
            args: Arguments potentially containing variable references
            execution_context: Context with previous step results
        
        Returns:
            Resolved arguments with variables replaced by actual values
        """
        resolved = {}
        
        for key, value in args.items():
            resolved[key] = self._resolve_value(value, execution_context)
        
        return resolved
    
    def _resolve_value(self, value: Any, context: Dict[str, Any]) -> Any:
        """
        Recursively resolve a single value (handles nested dicts/lists).
        
        Args:
            value: Value to resolve (may be string with ${ref}, dict, list, or literal)
            context: Execution context for variable lookup
        
        Returns:
            Resolved value
        """
        # Handle string variable references
        if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
            return self._lookup_variable(value, context)
        
        # Handle dicts recursively
        if isinstance(value, dict):
            return {k: self._resolve_value(v, context) for k, v in value.items()}
        
        # Handle lists recursively
        if isinstance(value, list):
            return [self._resolve_value(item, context) for item in value]
        
        # Return literals as-is
        return value
    
    def _lookup_variable(self, var_ref: str, context: Dict[str, Any]) -> Any:
        """
        Look up a variable reference in the execution context.
        
        Args:
            var_ref: Variable reference like "${step_id.field_name}"
            context: Execution context
        
        Returns:
            Resolved value from context
        
        Raises:
            KeyError: If variable reference cannot be resolved
        """
        # Extract path from ${...}
        path = var_ref[2:-1]  # Remove ${ and }
        
        # Split into parts: step_id.field_name.subfield
        parts = path.split(".")
        
        # Navigate through context
        current = context
        for part in parts:
            if isinstance(current, dict):
                if part not in current:
                    raise KeyError(f"Variable reference '{var_ref}' not found in context (missing key: {part})")
                current = current[part]
            else:
                raise KeyError(f"Variable reference '{var_ref}' navigation failed at '{part}' (not a dict)")
        
        logger.debug(f"Resolved {var_ref} → {current}")
        return current
    
    async def _execute_mcp_tool(
        self,
        tool_name: str,
        args: Dict[str, Any]
    ) -> Any:
        """
        Execute an MCP tool via ToolExecutor.
        
        Args:
            tool_name: MCP tool name
            args: Resolved arguments
        
        Returns:
            Tool execution result
        """
        logger.debug(f"Executing MCP tool {tool_name} with args: {args}")
        
        # Call ToolExecutor's execute method
        result = await self.tool_executor.execute(tool_name, args)
        
        # Parse JSON if string response
        if isinstance(result, str) and result.strip().startswith(("{", "[")):
            try:
                result = json.loads(result)
            except json.JSONDecodeError:
                logger.warning(f"Could not parse MCP result as JSON: {result[:100]}")
        
        return result
    
    async def _execute_pm_skill(
        self,
        skill_name: str,
        args: Dict[str, Any]
    ) -> Any:
        """
        Execute a PM Skill via PM Skill Agent.
        
        Args:
            skill_name: PM Skill name
            args: Resolved arguments
        
        Returns:
            Skill execution result
        """
        logger.debug(f"Executing PM Skill {skill_name} with args: {args}")
        
        # Import execute_skill from skills module
        from agents.pm_skill_agent.skills import execute_skill
        
        # Execute the skill
        skill_result = await execute_skill(skill_name, args)
        
        # Check success
        if not skill_result.success:
            raise RuntimeError(f"Skill {skill_name} failed: {skill_result.error}")
        
        # Return the data from SkillResult
        # SkillResult has: success, result, data, message, error, confidence, metadata
        return {
            "success": skill_result.success,
            "data": skill_result.data,
            "result": skill_result.result,
            "message": skill_result.message,
            "confidence": skill_result.confidence,
            "metadata": skill_result.metadata
        }
    
    def get_tool_info(self, tool_name: str) -> Optional[Dict[str, Any]]:
        """
        Get metadata about a tool.
        
        Args:
            tool_name: Name of the tool
        
        Returns:
            Tool metadata dict or None if not found
        """
        # Check PM Skill registry first
        if tool_name in self.pm_skill_registry:
            skill_def = self.pm_skill_registry[tool_name]
            return {
                "name": tool_name,
                "type": "pm_skill",
                "description": skill_def.description,
                "required_params": skill_def.required_params,
                "optional_params": skill_def.optional_params,
                "use_cases": skill_def.use_cases
            }
        
        # Check MCP registry
        if tool_name in self.mcp_registry:
            mcp_def = self.mcp_registry[tool_name]
            return {
                "name": tool_name,
                "type": "mcp",
                "description": mcp_def.get("description", ""),
                "required_args": mcp_def.get("required_args", []),
                "optional_args": mcp_def.get("optional_args", {}),
                "use_cases": mcp_def.get("use_cases", [])
            }
        
        return None
    
    def list_available_tools(
        self,
        tool_type: Optional[ToolType] = None
    ) -> List[str]:
        """
        List all available tools.
        
        Args:
            tool_type: Filter by tool type (None = all tools)
        
        Returns:
            List of tool names
        """
        tools = []
        
        if tool_type is None or tool_type == ToolType.PM_SKILL:
            tools.extend(self.pm_skill_registry.keys())
        
        if tool_type is None or tool_type == ToolType.MCP:
            tools.extend(self.mcp_registry.keys())
        
        return sorted(tools)
    
    def validate_args(
        self,
        tool_name: str,
        args: Dict[str, Any]
    ) -> tuple[bool, Optional[str]]:
        """
        Validate arguments for a tool (before resolution).
        
        Args:
            tool_name: Name of the tool
            args: Arguments to validate
        
        Returns:
            (is_valid, error_message) - error_message is None if valid
        """
        tool_info = self.get_tool_info(tool_name)
        
        if not tool_info:
            return False, f"Unknown tool: {tool_name}"
        
        # Get required params based on tool type
        if tool_info["type"] == "pm_skill":
            required_params = tool_info.get("required_params", [])
        else:  # mcp
            required_params = tool_info.get("required_args", [])
        
        # Check all required params are present (allow variable references)
        missing_params = []
        for param in required_params:
            if param not in args:
                missing_params.append(param)
        
        if missing_params:
            return False, f"Missing required parameters: {missing_params}"
        
        return True, None


# Helper function for creating dispatcher
def create_tool_dispatcher(
    tool_executor,
    pm_skill_agent,
    mcp_tool_registry: Optional[Dict[str, Dict[str, Any]]] = None,
    pm_skill_registry: Optional[Dict[str, Dict[str, Any]]] = None
) -> ToolDispatcher:
    """
    Factory function to create a ToolDispatcher with auto-loaded registries.
    
    Args:
        tool_executor: ToolExecutor instance
        pm_skill_agent: PM Skill Agent instance
        mcp_tool_registry: Optional MCP registry (auto-loaded if None)
        pm_skill_registry: Optional PM Skill registry (auto-loaded if None)
    
    Returns:
        Configured ToolDispatcher instance
    """
    # Auto-load MCP registry if not provided
    if mcp_tool_registry is None:
        from utilities.mcp.tool_registry import MCP_TOOL_REGISTRY
        mcp_tool_registry = MCP_TOOL_REGISTRY
    
    # Auto-load PM Skill registry if not provided
    if pm_skill_registry is None:
        from agents.pm_skill_agent.skills import SKILL_DEFINITIONS
        pm_skill_registry = SKILL_DEFINITIONS
    
    return ToolDispatcher(
        tool_executor=tool_executor,
        pm_skill_agent=pm_skill_agent,
        mcp_tool_registry=mcp_tool_registry,
        pm_skill_registry=pm_skill_registry
    )


# Export
__all__ = ["ToolDispatcher", "DispatchResult", "ToolType", "create_tool_dispatcher"]
