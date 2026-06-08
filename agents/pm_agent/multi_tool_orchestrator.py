"""
Multi-Tool Orchestrator - Intelligent execution of complex queries requiring multiple tools.

This module handles:
1. Detection of multi-tool query requirements
2. Automatic tool chaining with dependency resolution
3. Result aggregation and synthesis
4. Error handling with fallback strategies
5. Context preservation across tool calls

Architecture:
    Query → Intent Analysis → Tool Sequence Planning → Execution → Result Synthesis
"""

import re
import json
import logging
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple, Callable
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)

# Import unified tool registry for plan validation
try:
    from agents.pm_agent.unified_tool_registry import UNIFIED_REGISTRY
    UNIFIED_REGISTRY_AVAILABLE = True
except ImportError:
    UNIFIED_REGISTRY_AVAILABLE = False
    logger.debug("[ORCHESTRATOR] Unified registry not available, plan validation disabled")

# Import tool argument normalization for enum coercion (string status to numbers, etc)
try:
    from utilities.mcp.tool_registry import normalize_tool_args, MCP_TOOL_REGISTRY
    NORMALIZE_ARGS_AVAILABLE = True
except ImportError:
    NORMALIZE_ARGS_AVAILABLE = False
    MCP_TOOL_REGISTRY = {}
    def normalize_tool_args(tool_name: str, args: Dict[str, Any], tool_schema: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, Any], Dict[str, str]]:
        """Stub: Returns args unchanged when tool_registry not available."""
        return args, {}
    logger.debug("[ORCHESTRATOR] Tool argument normalization not available")

# Import context resolver for variable resolution across steps
try:
    from agents.pm_agent.context_resolver import get_context_resolver, ContextResolver
    CONTEXT_RESOLVER_AVAILABLE = True
except ImportError:
    CONTEXT_RESOLVER_AVAILABLE = False
    logger.debug("[ORCHESTRATOR] Context resolver not available, variable resolution disabled")

# Import metrics for observability
try:
    from utilities.metrics import get_metrics_collector, MetricNames
    METRICS_AVAILABLE = True
except ImportError:
    METRICS_AVAILABLE = False
    logger.debug("[ORCHESTRATOR] Metrics not available")

# Import rollback manager for B5 rollback support
try:
    from agents.pm_agent.rollback_manager import RollbackManager, RollbackStatus
    ROLLBACK_AVAILABLE = True
except ImportError:
    ROLLBACK_AVAILABLE = False
    logger.debug("[ORCHESTRATOR] Rollback manager not available")

# Import identity resolution for person name handling
try:
    from utilities.identity_resolution import resolve_person_name
    IDENTITY_RESOLUTION_AVAILABLE = True
except ImportError:
    IDENTITY_RESOLUTION_AVAILABLE = False
    logger.debug("[ORCHESTRATOR] Identity resolution not available")

# Import synthesizer for intelligent result synthesis
try:
    from utilities.llm.synthesizer import synthesize_agent_outputs
    SYNTHESIZER_AVAILABLE = True
except ImportError:
    SYNTHESIZER_AVAILABLE = False
    logger.debug("[ORCHESTRATOR] Synthesizer not available")

# Import Langfuse client for tracing (consistent with rest of application)
try:
    from utilities.langfuse_client import (
        get_langfuse_client, 
        create_span, 
        finalize_span, 
        get_current_trace
    )
    LANGFUSE_AVAILABLE = True
except ImportError:
    LANGFUSE_AVAILABLE = False
    logger.debug("[ORCHESTRATOR] Langfuse client not available")
    # Provide stub functions when Langfuse is not available
    def get_langfuse_client(): 
        """Stub: Returns None when Langfuse is not installed."""
        return None
    def create_span(*args, **kwargs): 
        """Stub: No-op when Langfuse is not installed."""
        return None
    def finalize_span(*args, **kwargs): 
        """Stub: No-op when Langfuse is not installed."""
        pass  # Intentionally empty - Langfuse not available
    def get_current_trace(): 
        """Stub: Returns None when Langfuse is not installed."""
        return None


# ══════════════════════════════════════════════════════════════════════════════
# UNAVAILABLE TOOL REMAPPING
# Maps tools that are NOT available in MCP to their available alternatives.
# This allows the LLM planner to use natural tool names while the orchestrator
# automatically substitutes with working alternatives.
# ══════════════════════════════════════════════════════════════════════════════
UNAVAILABLE_TOOL_REMAPPING: Dict[str, Dict[str, Any]] = {
    # wit_run_wiql_query REMOVED — use execute_wiql skill (direct REST API)
    # wit_get_work_items is NOT available - use wit_get_work_items_batch_by_ids for batch
    "wit_get_work_items": {
        "replacement": "wit_get_work_items_batch_by_ids",
        "args_transform": lambda args: args,  # Same args structure
        "reason": "wit_get_work_items deprecated - using wit_get_work_items_batch_by_ids"
    },
    # pipelines_list is an alias for pipelines_get_build_definitions (same API, different name)
    "pipelines_list": {
        "replacement": "pipelines_get_build_definitions",
        "args_transform": lambda args: args,  # Same args structure
        "reason": "pipelines_list mapped to pipelines_get_build_definitions (build definitions ARE pipelines)"
    },
    # pipelines_list_pipelines is an alias for pipelines_get_build_definitions
    "pipelines_list_pipelines": {
        "replacement": "pipelines_get_build_definitions",
        "args_transform": lambda args: args,
        "reason": "pipelines_list_pipelines mapped to pipelines_get_build_definitions"
    },
    # pr_list_pull_requests is NOT available in MCP - use repo_list_pull_requests_by_repo_or_project instead
    # The repo tool accepts either repositoryId (for single repo) or project (for all repos in project)
    "pr_list_pull_requests": {
        "replacement": "repo_list_pull_requests_by_repo_or_project",
        "args_transform": lambda args: {
            "repositoryId": args.get("repositoryId"),  # Pass through if provided
            "project": args.get("project") or args.get("project_id"),  # Use project if repo not specified
            "status": None if args.get("status") == "all" else args.get("status"),  # Remove "all" filter, None means all statuses
            "top": args.get("top", 200),
            "skip": args.get("skip", 0),
        },
        "reason": "pr_list_pull_requests not available in MCP - using repo_list_pull_requests_by_repo_or_project"
    },
    # pr_get_pull_request is NOT available in MCP - use repo_get_pull_request_by_id instead
    "pr_get_pull_request": {
        "replacement": "repo_get_pull_request_by_id",
        "args_transform": lambda args: {
            "repositoryId": args.get("repositoryId"),  # Required
            "pullRequestId": args.get("pullRequestId"),  # Required
        },
        "reason": "pr_get_pull_request not available in MCP - using repo_get_pull_request_by_id"
    },
}


def remap_unavailable_tool(tool_name: str, args: Dict[str, Any]) -> Tuple[str, Dict[str, Any], Optional[str]]:
    """
    Remap an unavailable tool to its working alternative.
    
    Args:
        tool_name: The requested tool name
        args: Arguments for the tool
        
    Returns:
        Tuple of (new_tool_name, transformed_args, remap_reason or None)
    """
    if tool_name in UNAVAILABLE_TOOL_REMAPPING:
        remap_info = UNAVAILABLE_TOOL_REMAPPING[tool_name]
        new_tool = remap_info["replacement"]
        new_args = remap_info["args_transform"](args) if remap_info.get("args_transform") else args
        reason = remap_info.get("reason", f"Tool {tool_name} remapped to {new_tool}")
        
        # Clean up None values from args
        new_args = {k: v for k, v in new_args.items() if v is not None}
        
        logger.info(f"[ORCHESTRATOR] Tool remapped: {tool_name} → {new_tool} ({reason})")
        return new_tool, new_args, reason
    
    return tool_name, args, None


# ══════════════════════════════════════════════════════════════════════════════
# GENERIC PERSON ARG DETECTION PATTERNS
# These patterns identify arguments that may contain person names/identities
# and need resolution via core_get_identity_ids before MCP tool execution.
# This is NOT hardcoded to specific args - it uses heuristics to detect any
# person-related parameter dynamically.
# ══════════════════════════════════════════════════════════════════════════════
PERSON_ARG_PATTERNS = [
    # Explicit person-related arg names (case-insensitive matching)
    r"^assigned(?:To|_to)?$",
    r"^created(?:By|_by)?$",
    r"^author(?:Id)?$",
    r"^reviewer(?:s|Id)?$",
    r"^creator(?:Id)?$",
    r"^owner(?:Id)?$",
    r"^user(?:Id|Name)?$",
    r"^modified(?:By|_by)?$",
    r"^resolved(?:By|_by)?$",
    r"^closed(?:By|_by)?$",
    r"^approver(?:s|Id)?$",
    r"^requestor(?:Id)?$",
    r"^requester(?:Id)?$",
    r"^member(?:s|Id)?$",
    r"^searchFilter$",  # ADO identity search filter
]
# Compile patterns for efficiency
_PERSON_ARG_COMPILED = [re.compile(p, re.IGNORECASE) for p in PERSON_ARG_PATTERNS]


def _is_person_arg(arg_name: str) -> bool:
    """
    Dynamically detect if an argument name represents a person/identity field.
    
    Uses compiled regex patterns for efficiency. This is generic and not limited
    to any specific tool - any new tool with person-related args will be detected.
    
    Args:
        arg_name: The argument name to check
        
    Returns:
        True if the argument likely contains a person name/identifier
    """
    return any(pattern.match(arg_name) for pattern in _PERSON_ARG_COMPILED)


# ══════════════════════════════════════════════════════════════════════════════
# GENERIC IMPLICIT DEPENDENCY DETECTION PATTERNS
# These patterns identify argument VALUES that indicate a dependency on a 
# previous step's result. This prevents parallel execution of dependent steps.
# This is GENERIC - not limited to any specific tool or query pattern.
# ══════════════════════════════════════════════════════════════════════════════
IMPLICIT_DEPENDENCY_PATTERNS = [
    # Step reference patterns
    r"\$step_(\d+)",                              # $step_1, $step_2
    r"step[_\s]*(\d+)\s*(?:result|output)",       # step_1 result, step 2 output
    r"from\s+(?:previous|prior|earlier)\s+step", # from previous step
    r"result\s+(?:of|from)\s+step\s*(\d+)",       # result of step 1
    r"output\s+(?:of|from)\s+step\s*(\d+)",       # output of step 2
    r"use\s+(?:the\s+)?result",                   # use the result
    # Placeholder patterns that indicate incomplete data
    r"<[^>]+>",                                   # <query_id>, <ID of saved query>
    r"\[from\s+[^\]]+\]",                         # [from context], [from step 1], [from anything]
    r"TBD|TO_BE_FILLED|PLACEHOLDER",              # Explicit placeholders
    r"[A-Z_]+_FROM_STEP_(\d+)",                   # ALERT_ID_FROM_STEP_1, COMMIT_IDS_FROM_STEP_2
]
# Compile patterns for efficiency
_IMPLICIT_DEPENDENCY_COMPILED = [re.compile(p, re.IGNORECASE) for p in IMPLICIT_DEPENDENCY_PATTERNS]


def _detect_implicit_dependency(value: Any) -> Tuple[bool, Optional[str], str]:
    """
    Detect if a value indicates an implicit dependency on a previous step.
    
    This function uses generic pattern matching to detect when an argument
    value references or depends on a previous step's output. This is critical
    for preventing parallel execution of steps that have implicit dependencies.
    
    Args:
        value: The argument value to check (can be str, list, dict, etc.)
        
    Returns:
        Tuple of (has_dependency, step_id, reason):
        - has_dependency: True if the value indicates a dependency
        - step_id: The step ID this depends on (e.g., "step_1"), or None
        - reason: Description of why this is a dependency
    """
    if value is None:
        return False, None, ""
    
    # Convert to string for pattern matching
    if isinstance(value, str):
        value_str = value
    elif isinstance(value, (list, tuple)):
        # Recursively check list items
        for item in value:
            has_dep, step_id, reason = _detect_implicit_dependency(item)
            if has_dep:
                return has_dep, step_id, reason
        return False, None, ""
    elif isinstance(value, dict):
        # Recursively check dict values
        for v in value.values():
            has_dep, step_id, reason = _detect_implicit_dependency(v)
            if has_dep:
                return has_dep, step_id, reason
        return False, None, ""
    else:
        value_str = str(value)
    
    # Check against all patterns
    for i, pattern in enumerate(_IMPLICIT_DEPENDENCY_COMPILED):
        match = pattern.search(value_str)
        if match:
            # Extract step ID if present in the match
            step_id = None
            if match.groups():
                try:
                    step_num = match.group(1)
                    step_id = f"step_{step_num}"
                except (IndexError, TypeError):
                    pass
            
            pattern_desc = IMPLICIT_DEPENDENCY_PATTERNS[i]
            reason = f"pattern-match:{pattern_desc[:30]}"
            
            logger.info(
                f"[ORCHESTRATOR] Implicit dependency detected in value '{value_str[:50]}...': "
                f"pattern='{pattern_desc[:30]}', step_id={step_id}"
            )
            return True, step_id, reason
    
    return False, None, ""


def _detect_implicit_dependencies_in_step(step_args: Dict[str, Any]) -> Tuple[bool, Optional[str], str]:
    """
    Check all arguments in a step for implicit dependencies.
    
    Args:
        step_args: Dictionary of argument name -> value pairs
        
    Returns:
        Tuple of (has_dependency, step_id, reason) for the first dependency found
    """
    if not step_args:
        return False, None, ""
    
    for arg_name, arg_value in step_args.items():
        has_dep, step_id, reason = _detect_implicit_dependency(arg_value)
        if has_dep:
            return True, step_id, f"arg:{arg_name}:{reason}"
    
    return False, None, ""


class ToolDependency(Enum):
    """Types of dependencies between tool calls."""
    NONE = "none"  # Independent, can run in parallel
    SEQUENTIAL = "sequential"  # Must run after previous
    RESULT = "result"  # Needs result from previous step (used in step.produces)
    RESULT_DEPENDS = "result_depends"  # Needs result from previous tool
    CONDITIONAL = "conditional"  # Run only if condition met


class ExecutionStatus(Enum):
    """Status of tool execution."""
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    RETRYING = "retrying"


@dataclass
class ToolStep:
    """Represents a single step in a multi-tool execution plan."""
    tool_name: str
    args: Dict[str, Any]
    purpose: str
    reasoning: str = ""  # LLM rationale for this step
    dependency: ToolDependency = ToolDependency.NONE
    depends_on: Optional[str] = None  # Step ID this depends on
    condition: Optional[Callable] = None  # For conditional execution
    result_transformer: Optional[Callable] = None  # Transform result for next step
    max_retries: int = 2
    timeout_seconds: int = 30
    
    # Execution state
    step_id: str = ""
    status: ExecutionStatus = ExecutionStatus.PENDING
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    retry_count: int = 0
    execution_time_ms: int = 0
    
    # Parallel execution hints and safety metadata
    can_parallelize: bool = False  # Set true for read-only steps that can run concurrently
    read_only: bool = True  # True if step is non-mutating (default safe)
    expected_evidence: Optional[List[str]] = None  # Fields synthesizer expects for analysis
    
    # B5: Rollback support for write operations
    has_side_effects: bool = False  # True if step modifies data
    rollback_tool: Optional[str] = None  # Tool to call for rollback
    rollback_args_template: Optional[Dict[str, Any]] = None  # Args template for rollback (uses step result)


@dataclass
class ExecutionPlan:
    """Complete execution plan for a multi-tool query."""
    query: str
    steps: List[ToolStep] = field(default_factory=list)
    context: Dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=datetime.utcnow)
    
    # Execution tracking
    current_step_index: int = 0
    overall_status: ExecutionStatus = ExecutionStatus.PENDING
    intermediate_results: Dict[str, Any] = field(default_factory=dict)
    final_result: Optional[str] = None
    total_execution_time_ms: int = 0
    
    # B3: Context preservation for variable resolution
    execution_context: Dict[str, Any] = field(default_factory=dict)
    step_outputs: Dict[str, Any] = field(default_factory=dict)  # step_id → full result
    
    # B5: Rollback tracking
    completed_with_side_effects: List[str] = field(default_factory=list)  # step_ids that modified data
    rollback_attempted: bool = False


@dataclass 
class OrchestratorConfig:
    """
    Configuration for the orchestrator.
    
    Values are loaded from config.yaml when available, with sensible defaults.
    This allows runtime configuration without code changes.
    """
    max_parallel_tools: int = 3
    default_timeout_seconds: int = 60  # FIX #3: Increased from 30s to 60s for large tool payloads
    max_retries_per_tool: int = 2
    retry_delay_seconds: float = 1.0
    enable_fallback_strategies: bool = True
    preserve_partial_results: bool = True
    
    @classmethod
    def from_config(cls) -> "OrchestratorConfig":
        """
        Create OrchestratorConfig from config.yaml settings.
        
        This is the preferred way to create config - it loads values from
        config.yaml with fallback to defaults.
        """
        try:
            from config import config
            return cls(
                max_parallel_tools=config.orchestrator_max_parallel_tools,
                default_timeout_seconds=config.orchestrator_default_timeout,
                max_retries_per_tool=config.orchestrator_max_retries,
                # These don't have config properties yet, use defaults
                retry_delay_seconds=1.0,
                enable_fallback_strategies=True,
                preserve_partial_results=True
            )
        except ImportError:
            logger.debug("[ORCHESTRATOR] Config not available, using default OrchestratorConfig")
            return cls()


class MultiToolOrchestrator:
    """
    Orchestrates execution of complex queries requiring multiple ADO tools.
    
    Features:
    - Automatic query decomposition into tool steps
    - Parallel execution of independent tools
    - Sequential execution with result passing
    - Error recovery and fallback strategies
    - Result aggregation and synthesis
    """
    
    def __init__(self, tool_executor, mcp_connector, config: OrchestratorConfig = None):
        """
        Initialize the orchestrator.
        
        Args:
            tool_executor: ToolExecutor instance for running tools
            mcp_connector: MCPConnector for direct MCP calls
            config: Optional configuration (uses config.yaml values if not provided)
        """
        self.tool_executor = tool_executor
        self.mcp_connector = mcp_connector
        # Use config.yaml values via OrchestratorConfig.from_config() if no explicit config
        self.config = config or OrchestratorConfig.from_config()
        
        logger.info(
            f"[ORCHESTRATOR] Initialized with: max_parallel_tools={self.config.max_parallel_tools}, "
            f"timeout={self.config.default_timeout_seconds}s, max_retries={self.config.max_retries_per_tool}"
        )
        
        # B5: Initialize rollback manager if available
        self.rollback_manager = None
        if ROLLBACK_AVAILABLE:
            self.rollback_manager = RollbackManager(mcp_connector)
        
        # Tool metadata registry for parallel execution safety
        # This is the AUTHORITATIVE source for tool safety hints.
        # The pattern uses sensible defaults (read_only=True for GET operations)
        # and explicit overrides for known mutating operations.
        self.tool_metadata = self._build_tool_metadata()
        
        # FIX #32: Instance-level repo cache to avoid repeated MCP calls across steps
        self._fix30_repo_cache = {}  # project → list of repo dicts
        
        # Query pattern to tool mapping
        self._init_query_patterns()
    
    def _build_tool_metadata(self) -> Dict[str, Dict[str, Any]]:
        """
        Build tool metadata for parallel execution safety.
        
        Uses heuristics to determine if tools are read-only and safe for parallel execution:
        1. Tools starting with "get_", "list_", "search_" are read-only
        2. Tools starting with "create_", "update_", "delete_", "merge_" are mutating
        3. Explicit overrides for known tools
        
        This is dynamic and not limited to any specific tool list.
        """
        # Base defaults for common operations
        metadata = {}
        
        # Explicit overrides for known tools (these take precedence)
        explicit_metadata = {
            # Read-only tools safe for parallel execution
            "wit_get_work_items_for_iteration": {"read_only": True, "can_parallelize": True},
            "work_get_iteration_work_items": {"read_only": True, "can_parallelize": True},
            "work_list_team_iterations": {"read_only": True, "can_parallelize": True},
            "search_workitem": {"read_only": True, "can_parallelize": True},
            "execute_wiql": {"read_only": True, "can_parallelize": True},
            "wit_get_work_item": {"read_only": True, "can_parallelize": True},
            "wit_get_work_items": {"read_only": True, "can_parallelize": True},
            "core_list_projects": {"read_only": True, "can_parallelize": True},
            "core_list_project_teams": {"read_only": True, "can_parallelize": True},
            "core_get_identity_ids": {"read_only": True, "can_parallelize": True},
            "work_list_iterations": {"read_only": True, "can_parallelize": True},
            "pr_list_pull_requests": {"read_only": True, "can_parallelize": True},
            "pipelines_get_builds": {"read_only": True, "can_parallelize": True},
            "repo_list_repos_by_project": {"read_only": True, "can_parallelize": True},
            
            # Mutating tools - NEVER parallelize
            "wit_update_work_item": {"read_only": False, "can_parallelize": False},
            "wit_create_work_item": {"read_only": False, "can_parallelize": False},
            "pr_merge_pull_request": {"read_only": False, "can_parallelize": False},
            "pr_create_pull_request": {"read_only": False, "can_parallelize": False},
        }
        
        # Merge explicit metadata
        metadata.update(explicit_metadata)
        
        # Log for debugging
        logger.debug(f"[ORCHESTRATOR] Built tool metadata for {len(metadata)} tools")
        
        return metadata
    
    def get_tool_safety_hints(self, tool_name: str) -> Dict[str, Any]:
        """
        Get safety hints for a tool, with intelligent defaults.
        
        This uses pattern matching for unknown tools:
        - Tools starting with get_, list_, search_, query_ → read_only=True
        - Tools starting with create_, update_, delete_, merge_, post_ → read_only=False
        
        Args:
            tool_name: Name of the tool
            
        Returns:
            Dict with read_only and can_parallelize flags
        """
        # Check explicit metadata first
        if tool_name in self.tool_metadata:
            return self.tool_metadata[tool_name]
        
        # Pattern-based inference for unknown tools
        read_only_prefixes = ('get_', 'list_', 'search_', 'query_', 'find_', 'check_')
        mutating_prefixes = ('create_', 'update_', 'delete_', 'merge_', 'post_', 'put_', 'patch_')
        
        tool_lower = tool_name.lower()
        
        if any(tool_lower.startswith(p) or f"_{p.rstrip('_')}" in tool_lower for p in read_only_prefixes):
            logger.debug(f"[ORCHESTRATOR] Tool '{tool_name}' inferred as read-only based on name pattern")
            return {"read_only": True, "can_parallelize": True}
        elif any(tool_lower.startswith(p) or f"_{p.rstrip('_')}" in tool_lower for p in mutating_prefixes):
            logger.debug(f"[ORCHESTRATOR] Tool '{tool_name}' inferred as mutating based on name pattern")
            return {"read_only": False, "can_parallelize": False}
        else:
            # Default to safe (read-only, but not parallelizable to be cautious)
            logger.debug(f"[ORCHESTRATOR] Tool '{tool_name}' has unknown pattern, defaulting to read_only=True, can_parallelize=False")
            return {"read_only": True, "can_parallelize": False}
    
    def _init_query_patterns(self):
        """Initialize query patterns for multi-tool detection."""
        self.multi_tool_patterns = [
            # Pattern: query pattern regex, tool sequence builder function
            # CRITICAL: Pattern for "compare current sprint with previous sprint" - handle keyword-based iteration references
            # This pattern matches: "compare current sprint with previous sprint", "compare this iteration with last iteration", etc.
            # Note: (?:sprint|iteration) allows mix-and-match between sprint/iteration terms
            {
                "pattern": r"compare\s+(?:(?:the|my)\s+)?(?P<kw1>current|this|previous|last|prior)\s+(?:sprint|iteration)\s+(?:with|to|and|vs\.?)\s+(?:(?:the|my)\s+)?(?P<kw2>current|this|previous|last|prior)\s+(?:sprint|iteration)",
                "builder": self._build_iteration_comparison_from_keywords,
                "description": "Compare sprints using keywords (current/previous)"
            },
            # Alternate pattern: "compare my sprint with prior iteration" - captures natural phrases
            {
                "pattern": r"compare\s+(?P<kw1>my|current|this)\s+(?:sprint|iteration)\s+(?:with|to|and|vs\.?)\s+(?:(?:the)\s+)?(?P<kw2>previous|last|prior)\s+(?:sprint|iteration)",
                "builder": self._build_iteration_comparison_from_keywords,
                "description": "Compare my/current sprint with previous/last (alternate phrasing)"
            },
            # Pattern for "compare velocity between current and previous sprint" and similar
            {
                "pattern": r"compare\s+(?:velocity|progress|work|items?)\s+between\s+(?P<kw1>current|this|previous|last|prior)\s+(?:and|&)\s+(?P<kw2>current|this|previous|last|prior)\s+(?:sprint|iteration)",
                "builder": self._build_iteration_comparison_from_keywords,
                "description": "Compare velocity/progress between sprints using keywords"
            },
            # Pattern for specific iteration IDs: "compare sprint 25.22 with sprint 25.21"
            {
                "pattern": r"compare\s+(?:sprint|iteration)\s+(\S+)\s+(?:with|to|and|vs\.?)\s+(?:(?:sprint|iteration)\s+)?(\S+)",
                "builder": self._build_iteration_comparison_plan,
                "description": "Compare two sprints/iterations by ID"
            },
            {
                "pattern": r"(?:bugs?|items?)\s+(?:assigned\s+to|for)\s+(\w+(?:\s+\w+)?)\s+(?:in|during)\s+(?:sprint|iteration)\s+(\S+)",
                "builder": self._build_person_sprint_plan,
                "description": "Items for person in specific sprint"
            },
            {
                "pattern": r"(?:who|which\s+developer)\s+(?:has|have)\s+(?:the\s+)?(?:most|highest|maximum)\s+(?:bugs?|items?|work)",
                "builder": self._build_workload_analysis_plan,
                "description": "Find developer with most work"
            },
            {
                "pattern": r"(?:stale|stuck|inactive)\s+(?:bugs?|items?)\s+(?:by|per|for\s+each)\s+(?:area|assignee|developer)",
                "builder": self._build_stale_items_by_group_plan,
                "description": "Stale items grouped by area/assignee"
            },
            {
                "pattern": r"(?:all|complete)\s+(?:details?|info|information)\s+(?:about|for|of)\s+(?:sprint|iteration)\s+(\S+)",
                "builder": self._build_complete_sprint_report_plan,
                "description": "Complete sprint details"
            },
            {
                "pattern": r"(?:show|list|find|display)\s+(?:bugs|work items|items?)\s+(?:in|for|during)\s+(?:sprint|iteration)\s+(\S+)",
                "builder": self._build_items_in_sprint_plan,
                "description": "Items in specific sprint"
            },
            {
                "pattern": r"(?:prs?|pull\s+requests?)\s+(?:and|with)\s+(?:their|associated)\s+(?:bugs?|work\s+items?|linked\s+items?)",
                "builder": self._build_pr_with_workitems_plan,
                "description": "PRs with linked work items"
            },
            {
                "pattern": r"(?:build|pipeline)\s+(?:failures?|status)\s+(?:and|with)\s+(?:related|associated|linked)\s+(?:bugs?|issues?)",
                "builder": self._build_build_failures_with_bugs_plan,
                "description": "Build failures with related bugs"
            },
            {
                "pattern": r"team\s+(?:capacity|workload|status)\s+(?:for|in)\s+(?:sprint|iteration)\s+(\S+)",
                "builder": self._build_team_sprint_status_plan,
                "description": "Team status for sprint"
            },
            {
                "pattern": r"(?:history|changes|activity)\s+(?:of|for)\s+(?:work\s+item|bug|story|task)\s+#?(\d+)",
                "builder": self._build_workitem_history_plan,
                "description": "Work item history"
            },
            {
                "pattern": r"(?:recent|latest)\s+(?:commits?|changes?)\s+(?:by|from)\s+(\w+(?:\s+\w+)?)",
                "builder": self._build_person_commits_plan,
                "description": "Recent commits by person"
            },
            # Pattern for conjunction-based multi-queries: "get X, list Y, and show Z"
            {
                "pattern": r"(?P<req1>[^,]+?),\s*(?P<req2>[^,]+?)(?:,?\s+(?:and|&)\s+(?P<req3>.+))?$",
                "builder": self._build_conjunction_multi_query,
                "description": "Multiple comma/and-separated requests"
            },
        ]
    
    def requires_multi_tool(self, query: str) -> bool:
        """
        Determine if a query requires multiple tool calls.
        
        Args:
            query: User's natural language query
            
        Returns:
            True if multiple tools are needed
        """
        q = query.lower()
        
        # Check against known multi-tool patterns
        for pattern_info in self.multi_tool_patterns:
            if re.search(pattern_info["pattern"], q):
                return True
        
        # Heuristic checks for complex queries
        complexity_indicators = [
            # Multiple entities
            (r'\b(?:and|with|also|including)\b', 2),  # Conjunctions
            (r'\b(?:compare|versus|vs\.?)\b', 3),  # Comparisons
            (r'\b(?:between|from\s+\S+\s+to)\b', 2),  # Ranges
            (r'\b(?:each|every|per|by)\b.*\b(?:area|assignee|type|state)\b', 2),  # Grouping
            (r'\b(?:all|complete|full|detailed)\b.*\b(?:report|summary|overview)\b', 2),  # Full reports
        ]
        
        complexity_score = 0
        for pattern, score in complexity_indicators:
            if re.search(pattern, q):
                complexity_score += score
        
        return complexity_score >= 3
    
    async def plan_execution(self, query: str, context: Dict[str, Any]) -> ExecutionPlan:
        """
        Create an execution plan for a multi-tool query.
        
        Args:
            query: User's natural language query
            context: Current context (project, team, iteration, etc.)
            
        Returns:
            ExecutionPlan with tool steps
        """
        plan = ExecutionPlan(query=query, context=context)
        q = query.lower()
        
        # Match against known patterns
        for pattern_info in self.multi_tool_patterns:
            match = re.search(pattern_info["pattern"], q)
            if match:
                logger.info(f"[ORCHESTRATOR] Matched pattern: {pattern_info['description']}")
                steps = pattern_info["builder"](match, context)
                plan.steps = steps
                break
        
        # If no pattern matched, try to decompose heuristically
        if not plan.steps:
            plan.steps = self._decompose_query_heuristically(query, context)
        
        # Assign step IDs
        for i, step in enumerate(plan.steps):
            step.step_id = f"step_{i+1}"
        
        # Normalize safety hints from registry
        self._normalize_step_hints(plan.steps)
        
        # REMAP UNAVAILABLE TOOLS (NEW: FIX #20)
        # This ensures tools like pr_list_pull_requests are converted to their available alternatives
        for step in plan.steps:
            new_tool, new_args, remap_reason = remap_unavailable_tool(step.tool_name, step.args)
            if remap_reason:
                logger.info(f"[ORCHESTRATOR] FIX #20: Remapped unavailable tool: {step.tool_name} → {new_tool}")
                step.tool_name = new_tool
                step.args = new_args
                step.purpose = f"{step.purpose} (remapped from unavailable)"
        
        logger.info(f"[ORCHESTRATOR] Created plan with {len(plan.steps)} steps")
        return plan
    
    def convert_planner_plan_to_execution_plan(
        self, 
        planner_plan: Dict[str, Any], 
        context: Dict[str, Any]
    ) -> ExecutionPlan:
        """
        Convert an LLM Planner's execution_plan JSON to an ExecutionPlan object.
        
        This enables the orchestrator to execute multi-step plans generated dynamically
        by the LLM planner rather than only using pattern-matched plans.
        
        Args:
            planner_plan: Dict with 'execution_plan' key containing:
                - description: str
                - steps: List[{step_id, tool, args, purpose, depends_on}]
                - parallel_groups: List[List[int]] (optional)
                - analysis_hint: str (optional)
            context: Current context (project, team, iteration, etc.)
            
        Returns:
            ExecutionPlan ready for execute_plan()
        """
        execution_plan = planner_plan.get("execution_plan", {})
        description = execution_plan.get("description", "LLM-generated plan")
        # FIX #SYNTH-1: Use original user query for synthesis, not the LLM plan description
        # The agent.py passes "query": q inside execution_plan - use that as primary
        original_query = execution_plan.get("query") or planner_plan.get("query") or description
        steps_data = execution_plan.get("steps", [])
        parallel_groups = execution_plan.get("parallel_groups", [])
        analysis_hint = execution_plan.get("analysis_hint", "")
        analysis_criteria = planner_plan.get("analysis_criteria", {})
        
        plan = ExecutionPlan(
            query=original_query,
            context=context
        )
        
        # Build step ID mapping for dependency resolution
        step_id_map = {}
        
        for step_data in steps_data:
            raw_step_id = step_data.get("step_id", 1)
            # Normalize step_id: handle both "step_1" and 1 formats
            if isinstance(raw_step_id, str) and raw_step_id.startswith("step_"):
                step_id = raw_step_id  # Already prefixed
            else:
                step_id = f"step_{raw_step_id}"  # Add prefix
            
            step_type = step_data.get("type", "mcp")  # Default to mcp if not specified
            
            # Skip synthesis steps - they don't have tools and are handled by the synthesizer
            # Synthesis steps are metadata indicating result aggregation, not actual tool calls
            if step_type == "synthesis":
                logger.info(f"[ORCHESTRATOR] Skipping synthesis step {step_id} (handled by synthesizer)")
                continue
            
            # Skip call_skill steps - these require PM Skill Agent, not MCP tools
            # When mixed plans reach PM Agent, skill steps should be skipped
            # (They should have been routed to PM Skill Agent instead)
            if step_type == "call_skill":
                logger.warning(f"[ORCHESTRATOR] Skipping call_skill step {step_id} (tool={step_data.get('tool')}) - requires PM Skill Agent routing, not MCP")
                continue
            
            # Support both 'tool' and 'tool_name' field names from different planners
            tool_name = step_data.get("tool") or step_data.get("tool_name", "")
            
            # Validate tool_name is present for non-synthesis steps
            if not tool_name:
                logger.warning(f"[ORCHESTRATOR] Step {step_id} has no tool specified, skipping")
                continue
            
            # ═══════════════════════════════════════════════════════════════════════
            # FIX: Normalize tool names that don't exist in live MCP
            # ═══════════════════════════════════════════════════════════════════════
            TOOL_NAME_ALIASES = {
                "pr_list_pull_requests": "repo_list_pull_requests_by_repo_or_project",
                "pr_get_pull_request": "repo_get_pull_request_by_id", 
                "pr_create_pull_request": "repo_create_pull_request",
                "work_get_iteration_work_items": "wit_get_work_items_for_iteration",
            }
            if tool_name in TOOL_NAME_ALIASES:
                new_tool_name = TOOL_NAME_ALIASES[tool_name]
                logger.warning(f"[ORCHESTRATOR] Normalizing tool name: {tool_name} → {new_tool_name}")
                tool_name = new_tool_name
            
            args = step_data.get("args", {})
            
            # ═══════════════════════════════════════════════════════════════════════
            # FIX: Normalize iterationPath → iterationId for tools that require iterationId
            # Light Planner sometimes generates 'iterationPath' but MCP requires 'iterationId'
            # ═══════════════════════════════════════════════════════════════════════
            if tool_name == "wit_get_work_items_for_iteration":
                if "iterationPath" in args and "iterationId" not in args:
                    args["iterationId"] = args.pop("iterationPath")
                    logger.info(f"[ORCHESTRATOR] Normalized iterationPath → iterationId for step {step_id}")
            
            # Support both 'description' and 'purpose' for intent capture
            # agent.py usually sends 'description'
            purpose = step_data.get("purpose") or step_data.get("description", "")
            reasoning = step_data.get("reasoning", "")
            depends_on = step_data.get("depends_on", [])
            
            # Determine if this step can be parallelized
            tool_meta = self.tool_metadata.get(tool_name, {"read_only": True, "can_parallelize": True})
            is_read_only = tool_meta.get("read_only", True)
            can_parallelize = tool_meta.get("can_parallelize", True)
            
            # Check if in a parallel group (handle both string and numeric step_id formats)
            raw_id_for_group = raw_step_id if isinstance(raw_step_id, int) else raw_step_id.replace("step_", "")
            try:
                numeric_id = int(raw_id_for_group) if isinstance(raw_id_for_group, str) else raw_id_for_group
            except (ValueError, TypeError):
                numeric_id = None
            in_parallel_group = any(
                (numeric_id in group if numeric_id else step_id in [str(g) for g in group])
                for group in parallel_groups
            )
            
            # Determine dependency type - also normalize depends_on references
            dependency_type = ToolDependency.NONE
            depends_on_step = None
            if depends_on:
                if isinstance(depends_on, list) and len(depends_on) > 0:
                    dep = depends_on[0]
                    # Normalize dependency reference
                    if isinstance(dep, str) and dep.startswith("step_"):
                        depends_on_step = dep
                    else:
                        depends_on_step = f"step_{dep}"
                    dependency_type = ToolDependency.RESULT_DEPENDS  # Assume result dependency
            
            step = ToolStep(
                tool_name=tool_name,
                args=args,
                purpose=purpose,
                reasoning=reasoning,
                dependency=dependency_type,
                depends_on=depends_on_step,
                step_id=step_id,  # Already normalized above
                can_parallelize=can_parallelize and in_parallel_group and not depends_on,
                read_only=is_read_only,
                expected_evidence=analysis_criteria.get("evidence_fields", [])
            )
            
            plan.steps.append(step)
            step_id_map[step_id] = step
        
        # Store analysis metadata in context for synthesizer
        if analysis_hint:
            plan.context["analysis_hint"] = analysis_hint
        if analysis_criteria:
            plan.context["analysis_criteria"] = analysis_criteria
        
        # Normalize safety hints from registry
        self._normalize_step_hints(plan.steps)
        
        # PRE-VALIDATION: Remap any unavailable tools to their alternatives
        for step in plan.steps:
            new_tool, new_args, remap_reason = remap_unavailable_tool(step.tool_name, step.args)
            if remap_reason:
                logger.info(f"[ORCHESTRATOR] Pre-validation remap: {step.tool_name} → {new_tool}")
                step.tool_name = new_tool
                step.args = new_args
                step.purpose = f"{step.purpose} (remapped from unavailable tool)"
        
        # ═══════════════════════════════════════════════════════════════════════════
        # CRITICAL FIX: DEEP LLM SYNTHESIS GUARANTEE
        # ═══════════════════════════════════════════════════════════════════════════
        # Issue: Deep LLM plans may or may not include synthesis steps (depends on LLM following instructions)
        # Light LLM: Has mandatory _append_synthesis_step_if_needed() that ALWAYS adds synthesis
        # Deep LLM: Trusts LLM to include synthesis, but has no enforcement
        # 
        # Solution: Guarantee synthesis execution for multi-step plans (>=2 execution steps)
        # Note: Synthesis happens in _synthesize_results() method (line ~2900), not as a plan step.
        # Synthesis-type steps in plans are metadata/markers that get skipped during execution (line 823).
        # The orchestrator ALWAYS calls _synthesize_results() at line ~1145 after all steps complete.
        # This flag ensures we can track and log synthesis expectations for debugging.
        # ═══════════════════════════════════════════════════════════════════════════
        
        # Count actual execution steps (non-synthesis, non-skipped)
        execution_steps = [s for s in plan.steps if s.status != ExecutionStatus.SKIPPED]
        
        if len(execution_steps) >= 2:
            # Multi-step plan - synthesis is MANDATORY and will happen automatically
            logger.info(f"[ORCHESTRATOR] ✓ Multi-step plan detected ({len(execution_steps)} execution steps) - synthesis GUARANTEED via _synthesize_results()")
            logger.info(f"[ORCHESTRATOR]   Synthesis will use centralized synthesize_agent_outputs() from utilities/llm/synthesizer.py")
            
            # Set tracking flags in context for validation and debugging
            plan.context["_requires_synthesis"] = True
            plan.context["_synthesis_step_count"] = len(execution_steps)
            plan.context["_synthesis_guaranteed_by"] = "orchestrator_convert_plan"
        elif len(execution_steps) == 1:
            # Single-step plan - synthesis may still be beneficial for formatting
            logger.info(f"[ORCHESTRATOR] Single-step plan detected - synthesis will provide formatting/enhancement")
            plan.context["_requires_synthesis"] = False
            plan.context["_synthesis_step_count"] = 1
        else:
            # Edge case: Plan with no execution steps (should not happen in normal flow)
            logger.warning(f"[ORCHESTRATOR] ⚠️  Plan has 0 execution steps - synthesis may not produce meaningful output")
            plan.context["_requires_synthesis"] = False
            plan.context["_synthesis_step_count"] = 0
        
        logger.info(f"[ORCHESTRATOR] Converted planner plan to ExecutionPlan with {len(plan.steps)} steps")
        return plan
    
    def validate_plan(self, plan: ExecutionPlan) -> Tuple[bool, List[str]]:
        """
        Validate an execution plan against the unified tool registry.
        
        Checks:
        1. All tools exist in the registry (after remapping unavailable ones)
        2. Required arguments are present
        3. Dependencies reference valid step IDs
        
        Args:
            plan: ExecutionPlan to validate
            
        Returns:
            Tuple of (is_valid, list_of_errors)
        """
        errors = []
        step_ids = {step.step_id for step in plan.steps}
        
        # REMAP UNAVAILABLE TOOLS BEFORE VALIDATION (NEW: FIX #20b)
        # This ensures plans with unavailable tools (e.g., pr_list_pull_requests) 
        # are converted to available alternatives before validation
        logger.warning(f"[ORCHESTRATOR] FIX #20b DEBUG: Remapping {len(plan.steps)} steps BEFORE validation")
        for i, step in enumerate(plan.steps):
            logger.warning(f"[ORCHESTRATOR] FIX #20b DEBUG: Step {i} tool BEFORE: {step.tool_name}")
            new_tool, new_args, remap_reason = remap_unavailable_tool(step.tool_name, step.args)
            logger.warning(f"[ORCHESTRATOR] FIX #20b DEBUG: Remap result: new_tool={new_tool}, remap_reason={remap_reason}")
            if remap_reason:
                logger.warning(f"[ORCHESTRATOR] FIX #20b: Validation remapping unavailable tool: {step.tool_name} → {new_tool}")
                step.tool_name = new_tool
                step.args = new_args
                logger.warning(f"[ORCHESTRATOR] FIX #20b DEBUG: Step {i} tool AFTER: {step.tool_name}")
            else:
                logger.warning(f"[ORCHESTRATOR] FIX #20b DEBUG: Step {i} no remapping needed (remap_reason is None)")
        
        for step in plan.steps:
            tool_name = step.tool_name
            
            # Check tool exists
            if UNIFIED_REGISTRY_AVAILABLE:
                # Skip validation for virtual/local tools
                if tool_name in ('context_data_answer',):
                    continue
                
                tool_meta = UNIFIED_REGISTRY.get_tool(tool_name)
                if not tool_meta:
                    # FIX #26: Fallback check - tool may exist in live MCP connector even if not in static registry
                    # This allows dynamically registered tools and newly added tools to work without manual metadata updates
                    tool_in_live_mcp = False
                    if self.mcp_connector and hasattr(self.mcp_connector, 'tools_cache'):
                        tool_in_live_mcp = tool_name in self.mcp_connector.tools_cache
                    
                    if not tool_in_live_mcp:
                        errors.append(f"Step {step.step_id}: Tool '{tool_name}' not found in unified registry or live MCP")
                        continue
                    else:
                        logger.info(f"[ORCHESTRATOR] Tool '{tool_name}' found in live MCP connector (bypassing static registry check)")
                        # Tool exists in live MCP, skip required_args check since we don't have metadata
                        continue
                
                # Check required arguments — skip args that will be auto-enriched at execution time
                # These are injected by _execute_single_step BEFORE the MCP call:
                #   - wikiIdentifier: auto-fetched via wiki_list_wikis for wiki_ tools
                #   - repository/repositoryId/repositoryName: auto-fetched via repo_list_repos for repo_/advsec_ tools
                #   - project/team/iterationId: auto-injected by ToolExecutor._inject_context
                AUTO_ENRICHABLE_ARGS = {
                    "wikiIdentifier",       # auto-fetched for wiki_ tools
                    "repository",           # auto-fetched for repo_/advsec_ tools
                    "repositoryId",         # auto-fetched for repo_/advsec_ tools
                    "repositoryName",       # auto-fetched for advsec_ tools
                    "project",              # injected by ToolExecutor._inject_context
                    "team",                 # injected by ToolExecutor._inject_context
                    "iterationId",          # injected by ToolExecutor._inject_context
                }
                required_args = tool_meta.get("required_args", [])
                for arg in required_args:
                    if arg not in step.args and arg not in AUTO_ENRICHABLE_ARGS:
                        errors.append(f"Step {step.step_id}: Missing required argument '{arg}' for tool '{tool_name}'")
            
            # Check dependency references
            if step.depends_on and step.depends_on not in step_ids:
                errors.append(f"Step {step.step_id}: Depends on unknown step '{step.depends_on}'")
        
        is_valid = len(errors) == 0
        if not is_valid:
            logger.warning(f"[ORCHESTRATOR] Plan validation failed with {len(errors)} errors: {errors}")
        else:
            logger.info(f"[ORCHESTRATOR] Plan validated successfully ({len(plan.steps)} steps)")
        
        return is_valid, errors
    
    async def execute_plan(self, plan: ExecutionPlan) -> str:
        """
        Execute a multi-tool plan and synthesize results.
        
        Args:
            plan: Execution plan with tool steps
            
        Returns:
            Synthesized result string
        """
        start_time = datetime.utcnow()
        plan.overall_status = ExecutionStatus.RUNNING
        
        # B5: Clear rollback manager for new execution
        if self.rollback_manager:
            self.rollback_manager.clear()
        
        # Create Langfuse span for entire plan execution (consistent with rest of application)
        plan_span = None
        if LANGFUSE_AVAILABLE:
            plan_span = create_span(
                name="multi_tool_plan_execution",
                input_data={
                    "query": plan.query,
                    "step_count": len(plan.steps),
                    "steps": [
                        {"id": s.step_id, "tool": s.tool_name, "purpose": s.purpose}
                        for s in plan.steps
                    ]
                },
                metadata={
                    "component": "multi_tool_orchestrator",
                    "operation": "execute_plan"
                }
            )
        
        logger.info(f"[ORCHESTRATOR] ═══════════════════════════════════════════════════════════════")
        logger.info(f"[ORCHESTRATOR] Starting plan execution: {len(plan.steps)} steps for query: {plan.query[:100]}...")
        
        # ═══════════════════════════════════════════════════════════════════════════
        # SYNTHESIS EXECUTION TRACKING (FIX #2)
        # ═══════════════════════════════════════════════════════════════════════════
        # Log synthesis expectations based on plan context flags set during conversion
        requires_synthesis = (plan.context or {}).get("_requires_synthesis", len(plan.steps) >= 2)
        synthesis_step_count = (plan.context or {}).get("_synthesis_step_count", len(plan.steps))
        synthesis_source = (plan.context or {}).get("_synthesis_guaranteed_by", "unknown")
        
        if requires_synthesis:
            logger.info(f"[ORCHESTRATOR] 🔄 Synthesis REQUIRED: {synthesis_step_count} execution steps (guaranteed by: {synthesis_source})")
            logger.info(f"[ORCHESTRATOR]    → Will call _synthesize_results() after all steps complete")
            logger.info(f"[ORCHESTRATOR]    → Using centralized synthesize_agent_outputs() for quality output")
        else:
            logger.info(f"[ORCHESTRATOR] ℹ️  Synthesis OPTIONAL: Single-step plan (will still format output)")
        
        logger.info(f"[ORCHESTRATOR] ═══════════════════════════════════════════════════════════════")
        
        try:
            # ═══════════════════════════════════════════════════════════════
            # VALIDATE PLAN BEFORE EXECUTION (NEW: FIX #19)
            # ═══════════════════════════════════════════════════════════════
            is_valid, validation_errors = self.validate_plan(plan)
            if not is_valid:
                logger.error(f"[ORCHESTRATOR] Plan validation failed - will not execute:")
                for error in validation_errors:
                    logger.error(f"[ORCHESTRATOR]   ❌ {error}")
                
                # Synthesize error message for user
                error_summary = "Plan validation failed before execution:\n"
                for i, error in enumerate(validation_errors, 1):
                    error_summary += f"{i}. {error}\n"
                
                # Return error message to user
                return f"❌ **Plan Validation Error**\n\n{error_summary}\n**Suggestion:** This query requires tool parameters that cannot be automatically resolved. Please provide more specific details."
            
            # ═══════════════════════════════════════════════════════════════
            # PRE-RESOLVE ITERATION MACROS (to avoid race conditions in parallel)
            # ═══════════════════════════════════════════════════════════════
            await self._pre_resolve_iteration_macros(plan)
            
            # FIX #5: Validate iteration macro resolution (ensure current != previous)
            iteration_cache = (plan.context or {}).get("_iteration_cache", {})
            if "@CurrentIteration" in iteration_cache and "@PreviousIteration" in iteration_cache:
                current_id = iteration_cache["@CurrentIteration"]
                previous_id = iteration_cache["@PreviousIteration"]
                current_path = iteration_cache.get("_current_path", "")
                previous_path = iteration_cache.get("_previous_path", "")
                
                logger.info(f"[ORCHESTRATOR] FIX #5: Validating iteration resolution")
                logger.info(f"[ORCHESTRATOR]   @CurrentIteration:  ID={current_id}, Path={current_path}")
                logger.info(f"[ORCHESTRATOR]   @PreviousIteration: ID={previous_id}, Path={previous_path}")
                
                if current_id == previous_id:
                    logger.warning(f"[ORCHESTRATOR] FIX #5: VALIDATION FAILED - iterations are identical!")
                    logger.warning(f"[ORCHESTRATOR]   Both resolved to {current_id}")
                    logger.warning(f"[ORCHESTRATOR]   This may cause duplicate parallel calls and wasted execution")
            
            # Group steps into safe parallel batches and sequential steps
            parallel_batches, sequential_steps = self._group_safe_parallel_batches(plan.steps)
            
            # Execute parallel batches first (each batch respects max_parallel_tools limit)
            for batch_idx, batch in enumerate(parallel_batches):
                if batch:
                    logger.info(f"[ORCHESTRATOR] Executing parallel batch {batch_idx+1}/{len(parallel_batches)} with {len(batch)} steps")
                    await self._execute_parallel(batch, plan)
                    
                    # Check for failures in batch
                    failed_steps = [s for s in batch if s.status == ExecutionStatus.FAILED]
                    if failed_steps:
                        raise Exception(f"Batch {batch_idx+1} failed: {[s.step_id for s in failed_steps]}")
            
            # Execute sequential steps in original order (preserves dependencies)
            for step in sequential_steps:
                # Check condition if present
                if step.condition and not step.condition(plan.intermediate_results):
                    logger.info(f"[ORCHESTRATOR] Skipping step {step.step_id}: condition not met")
                    step.status = ExecutionStatus.SKIPPED
                    continue
                
                await self._execute_step(step, plan)
                
                # B5: Track steps with side effects for potential rollback
                if step.status == ExecutionStatus.SUCCESS and step.has_side_effects:
                    if self.rollback_manager:
                        self.rollback_manager.track_step(
                            step_id=step.step_id,
                            tool_name=step.tool_name,
                            args=step.args or {},
                            result=step.result or {}
                        )
                        plan.completed_with_side_effects.append(step.step_id)
                
                # Check for failure and trigger rollback if needed
                if step.status == ExecutionStatus.FAILED:
                    raise Exception(f"Step {step.step_id} failed: {step.error}")
            
            # ═══════════════════════════════════════════════════════════════════════════
            # SYNTHESIS VALIDATION & EXECUTION (FIX #2b)
            # ═══════════════════════════════════════════════════════════════════════════
            # Validate synthesis expectations before calling _synthesize_results
            expected_synthesis = (plan.context or {}).get("_requires_synthesis", True)
            successful_steps = [s for s in plan.steps if s.status == ExecutionStatus.SUCCESS]
            actual_step_count = len(successful_steps)
            
            # Log synthesis validation
            if expected_synthesis and actual_step_count == 0:
                logger.error(f"[ORCHESTRATOR] ❌ SYNTHESIS VALIDATION FAILED:")
                logger.error(f"[ORCHESTRATOR]    Plan required synthesis but NO steps succeeded!")
                logger.error(f"[ORCHESTRATOR]    This indicates execution failure - _synthesize_results will handle error case")
            elif expected_synthesis and actual_step_count >= 2:
                logger.info(f"[ORCHESTRATOR] ✅ SYNTHESIS VALIDATION PASSED:")
                logger.info(f"[ORCHESTRATOR]    {actual_step_count} steps succeeded (multi-step synthesis required)")
                logger.info(f"[ORCHESTRATOR]    Proceeding to _synthesize_results() for centralized synthesis")
            elif actual_step_count == 1:
                logger.info(f"[ORCHESTRATOR] ℹ️  SYNTHESIS INFO:")
                logger.info(f"[ORCHESTRATOR]    Single-step plan - synthesis will provide output formatting")
            
            # Synthesize results - ALWAYS called regardless of step count for consistent output formatting
            if SYNTHESIZER_AVAILABLE:
                plan.final_result = await self._synthesize_results(plan)
            else:
                # Fallback if synthesizer module not found (should not happen in prod)
                logger.warning(f"[ORCHESTRATOR] ⚠️  Synthesizer unavailable - using fallback")
                plan.final_result = self._synthesize_results_fallback(plan)
            
            plan.overall_status = ExecutionStatus.SUCCESS
            
            # Finalize Langfuse span with success
            if plan_span:
                step_results = {
                    s.step_id: {
                        "status": s.status.value,
                        "tool": s.tool_name,
                        "execution_time_ms": s.execution_time_ms
                    }
                    for s in plan.steps
                }
                finalize_span(plan_span, output={
                    "status": "success",
                    "step_results": step_results,
                    "total_execution_time_ms": int((datetime.utcnow() - start_time).total_seconds() * 1000)
                }, status="success")
            
        except Exception as e:
            logger.error(f"[ORCHESTRATOR] Plan execution failed: {e}")
            plan.overall_status = ExecutionStatus.FAILED
            
            # Finalize Langfuse span with error
            if plan_span:
                finalize_span(plan_span, output={
                    "status": "failed",
                    "error": str(e),
                    "total_execution_time_ms": int((datetime.utcnow() - start_time).total_seconds() * 1000)
                }, status="error", level="ERROR")
            
            # B5: Attempt rollback if we have steps with side effects
            if self.rollback_manager and plan.completed_with_side_effects:
                logger.info(f"[ORCHESTRATOR] Attempting rollback of {len(plan.completed_with_side_effects)} steps with side effects")
                try:
                    rollback_result = await self.rollback_manager.rollback_all()
                    plan.rollback_attempted = True
                    
                    if rollback_result.status == RollbackStatus.SUCCESS:
                        logger.info("[ORCHESTRATOR] Rollback completed successfully")
                        plan.final_result = self._handle_plan_failure(plan, str(e), rollback_info="All changes have been rolled back.")
                    elif rollback_result.status == RollbackStatus.PARTIAL:
                        logger.warning(f"[ORCHESTRATOR] Partial rollback: {rollback_result.rolled_back}")
                        plan.final_result = self._handle_plan_failure(
                            plan, str(e), 
                            rollback_info=f"Partial rollback: {len(rollback_result.rolled_back)} of {len(plan.completed_with_side_effects)} changes undone."
                        )
                    else:
                        logger.error(f"[ORCHESTRATOR] Rollback failed: {rollback_result.errors}")
                        plan.final_result = self._handle_plan_failure(
                            plan, str(e),
                            rollback_info=f"Rollback failed. Some changes may persist: {list(rollback_result.errors.keys())}"
                        )
                        
                    if METRICS_AVAILABLE:
                        get_metrics_collector().increment("rollback_attempted", {"status": rollback_result.status.value})
                        
                except Exception as rollback_error:
                    logger.error(f"[ORCHESTRATOR] Rollback error: {rollback_error}")
                    plan.final_result = self._handle_plan_failure(plan, str(e), rollback_info=f"Rollback error: {rollback_error}")
            else:
                plan.final_result = self._handle_plan_failure(plan, str(e))
        
        plan.total_execution_time_ms = int((datetime.utcnow() - start_time).total_seconds() * 1000)
        return plan.final_result
    
    async def _pre_resolve_iteration_macros(self, plan: ExecutionPlan):
        """
        Pre-resolve all iteration macros BEFORE parallel execution to avoid race conditions.
        
        FIX #5: Also store resolved paths for validation
        FIX #15: Expand to ALL tools, not just wit_get_work_items_for_iteration
        FIX #16: Semantic Placeholder Resolution - Auto-heal LLM placeholders
        
        This caches the resolved iteration at the plan level so parallel steps
        don't need to resolve independently.
        
        GENERIC: This now checks ANY argument in ANY tool for iteration macros.
        Not limited to specific tools or argument names.
        """
        # ═══════════════════════════════════════════════════════════════════════════
        # FIX #16: Auto-heal semantic placeholders from LLM
        # e.g., "<ID_FROM_STEP_1>" -> "@PreviousIteration" if context suggests it
        # ═══════════════════════════════════════════════════════════════════════════
        for step in plan.steps:
            if not isinstance(step.args, dict):
                continue
                
            for arg_name, arg_value in step.args.items():
                if isinstance(arg_value, str) and "<" in arg_value and ">" in arg_value:
                    # It's a placeholder. Check if it's iteration-related.
                    is_iteration_arg = arg_name.lower() in {
                        "iterationid", "iterationpath", "iteration", 
                        "sprintid", "sprintpath", "sprint"
                    }
                    
                    if is_iteration_arg:
                        # Check purpose/reasoning/value for intent
                        # Use getattr because fields might vary between ToolStep implementations
                        step_purpose = getattr(step, "purpose", "")
                        step_reasoning = getattr(step, "reasoning", "")
                        
                        intent_str = (
                            f"{step_purpose} {step_reasoning} {arg_value}"
                        ).lower()
                        
                        # Map based on semantic intent
                        new_value = None
                        if "previous" in intent_str or "last" in intent_str:
                            new_value = "@PreviousIteration"
                        elif "current" in intent_str or "this" in intent_str:
                            new_value = "@CurrentIteration"
                            
                        if new_value:
                            logger.info(f"[ORCHESTRATOR] Auto-healing placeholder '{arg_value}' -> '{new_value}' based on intent")
                            step.args[arg_name] = new_value

        # ═══════════════════════════════════════════════════════════════════════════
        # FIX #15: Check ALL tools and ALL iteration-related args for macros
        # Not limited to wit_get_work_items_for_iteration anymore
        # ═══════════════════════════════════════════════════════════════════════════
        # ═══════════════════════════════════════════════════════════════════════════
        # FIX #15: Check ALL tools and ALL iteration-related args for macros
        # Not limited to wit_get_work_items_for_iteration anymore
        # ═══════════════════════════════════════════════════════════════════════════
        ITERATION_ARG_NAMES = frozenset({
            "iterationId", "iterationPath", "iteration", "iterationName",
            "sprintId", "sprintPath", "sprint"
        })
        
        macros_to_resolve = set()
        sprint_names_to_resolve = set()  # bare sprint names like '26.01'
        _SPRINT_NAME_RE = re.compile(r'^(?:Sprint\s*)?(\d+\.\d+|\d+)$', re.IGNORECASE)
        
        for step in plan.steps:
            if not isinstance(step.args, dict):
                continue
            
            # Check ALL args that might contain iteration macros or sprint names
            for arg_name, arg_value in step.args.items():
                if not isinstance(arg_value, str):
                    continue
                is_iteration_arg = arg_name.lower() in {n.lower() for n in ITERATION_ARG_NAMES}
                
                # Check known iteration arg names
                if is_iteration_arg and arg_value.startswith("@"):
                    macros_to_resolve.add(arg_value)
                    logger.debug(f"[ORCHESTRATOR] Found iteration macro in {step.tool_name}.{arg_name}='{arg_value}'")
                # Also check any arg value that looks like an iteration macro
                elif arg_value.startswith("@") and any(
                    kw in arg_value.lower() for kw in ["iteration", "sprint", "current", "previous", "last", "next"]
                ):
                    macros_to_resolve.add(arg_value)
                    logger.debug(f"[ORCHESTRATOR] Found iteration macro pattern in {step.tool_name}.{arg_name}='{arg_value}'")
                # Bare sprint name (e.g. '26.01', '26.4', 'Sprint 26.1')
                elif is_iteration_arg and _SPRINT_NAME_RE.match(arg_value):
                    sprint_names_to_resolve.add(arg_value)
                    logger.debug(f"[ORCHESTRATOR] Found bare sprint name in {step.tool_name}.{arg_name}='{arg_value}'")
        
        # ═══════════════════════════════════════════════════════════════════════════
        # FIX: Also detect bare sprint numbers (e.g., "26.01", "25.19") in iteration args
        # These are NOT @-prefixed but still need resolution to GUIDs
        # ═══════════════════════════════════════════════════════════════════════════
        SPRINT_NUMBER_RE = re.compile(r'^\d+\.?\d*$')
        sprint_numbers_found = {}  # sprint_number -> list of (step, arg_name)
        
        for step in plan.steps:
            if not isinstance(step.args, dict):
                continue
            for arg_name, arg_value in step.args.items():
                if arg_name.lower() in {n.lower() for n in ITERATION_ARG_NAMES}:
                    if isinstance(arg_value, str) and SPRINT_NUMBER_RE.match(arg_value.strip()):
                        val = arg_value.strip()
                        if val not in sprint_numbers_found:
                            sprint_numbers_found[val] = []
                        sprint_numbers_found[val].append((step, arg_name))
        
        if sprint_numbers_found:
            logger.info(f"[ORCHESTRATOR] Pre-resolving {len(sprint_numbers_found)} bare sprint number(s): {list(sprint_numbers_found.keys())}")
            
            for sprint_number, step_arg_pairs in sprint_numbers_found.items():
                # Get project/team from first step's args or plan context
                sample_step, _ = step_arg_pairs[0]
                sample_args = sample_step.args or {}
                project = sample_args.get("project") or (plan.context or {}).get("project", "")
                team = sample_args.get("team") or (plan.context or {}).get("team", "")
                
                # Resolve using _resolve_iteration_macro which now handles bare sprint numbers
                resolved = await self._resolve_iteration_macro(sprint_number, sample_args, plan.context or {})
                
                if resolved:
                    # Cache it for step-level fallback
                    if "_iteration_cache" not in plan.context:
                        plan.context["_iteration_cache"] = {}
                    plan.context["_iteration_cache"][sprint_number] = resolved
                    
                    # Update all steps that reference this sprint number
                    for step, arg_name in step_arg_pairs:
                        if "id" in arg_name.lower():
                            step.args[arg_name] = resolved.get("iterationId")
                        else:
                            step.args[arg_name] = resolved.get("iterationPath") or resolved.get("iterationId")
                        
                        # Also set complementary fields
                        if "iterationId" not in step.args and resolved.get("iterationId"):
                            step.args["iterationId"] = resolved.get("iterationId")
                        if "iterationPath" not in step.args and resolved.get("iterationPath"):
                            step.args["iterationPath"] = resolved.get("iterationPath")
                        
                        logger.info(f"[ORCHESTRATOR] ✅ Sprint '{sprint_number}' resolved → {arg_name}={step.args[arg_name]} in {step.tool_name}")
                else:
                    logger.warning(f"[ORCHESTRATOR] Failed to resolve bare sprint number '{sprint_number}'")
        
        if not macros_to_resolve and not sprint_names_to_resolve:
            return
        
        # FIX #6: Sort macros to ensure @CurrentIteration is resolved FIRST
        # This is critical because @PreviousIteration needs to know what the current is to exclude it
        def macro_sort_key(m: str) -> int:
            if "current" in m.lower():
                return 0  # Current first
            elif "previous" in m.lower() or "last" in m.lower():
                return 1  # Previous second
            else:
                return 2  # Others last
        
        sorted_macros = sorted(macros_to_resolve, key=macro_sort_key)
        logger.info(f"[ORCHESTRATOR] Pre-resolving {len(sorted_macros)} iteration macro(s) in order: {sorted_macros}")
        
        # Resolve each unique macro once and cache in plan.context
        if "_iteration_cache" not in plan.context:
            plan.context["_iteration_cache"] = {}
        
        for macro in sorted_macros:
            # Use first step's args as template for context
            sample_args = {}
            for step in plan.steps:
                for arg_name, arg_value in (step.args or {}).items():
                    if arg_value == macro:
                        sample_args = step.args or {}
                        break
                if sample_args:
                    break
            
            resolved = await self._resolve_iteration_macro(macro, sample_args, plan.context)
            if resolved:
                plan.context["_iteration_cache"][macro] = resolved
                # FIX #5: Store paths for validation
                resolved_id = resolved.get("iterationId")
                resolved_path = resolved.get("iterationPath")
                logger.info(f"[ORCHESTRATOR] Pre-cached '{macro}' → ID={resolved_id}, Path={resolved_path}")
                
                # Store for FIX #5 validation
                if macro == "@CurrentIteration":
                    plan.context["_current_path"] = resolved_path
                elif macro == "@PreviousIteration":
                    plan.context["_previous_path"] = resolved_path
            else:
                logger.warning(f"[ORCHESTRATOR] Failed to pre-resolve '{macro}'")

        # ═══════════════════════════════════════════════════════════════════════════
        # Resolve bare sprint names (e.g. '26.01') to full paths / GUIDs
        # ═══════════════════════════════════════════════════════════════════════════
        if sprint_names_to_resolve:
            logger.info(f"[ORCHESTRATOR] Pre-resolving {len(sprint_names_to_resolve)} bare sprint name(s): {sprint_names_to_resolve}")
            for sprint_name in sprint_names_to_resolve:
                resolved = await self._resolve_iteration_macro(sprint_name, {}, plan.context)
                if resolved:
                    plan.context["_iteration_cache"][sprint_name] = resolved
                    logger.info(f"[ORCHESTRATOR] Pre-cached sprint '{sprint_name}' → ID={resolved.get('iterationId')}, Path={resolved.get('iterationPath')}")
                else:
                    logger.warning(f"[ORCHESTRATOR] Failed to pre-resolve sprint name '{sprint_name}'")

    async def _execute_parallel(self, steps: List[ToolStep], plan: ExecutionPlan):
        """
        Execute multiple steps in parallel with concurrency limits and safety checks.
        
        Safety checks:
        - All steps must be marked as read_only=True
        - All steps must be marked as can_parallelize=True
        - Concurrency limited by max_parallel_tools
        """
        # Safety check - ensure all steps declared safe for parallel execution
        unsafe = [s for s in steps if not (s.read_only and s.can_parallelize)]
        if unsafe:
            logger.warning(f"[ORCHESTRATOR] Unsafe steps detected in parallel batch: {[s.tool_name for s in unsafe]}")
            logger.warning("[ORCHESTRATOR] Falling back to sequential execution for safety")
            for s in steps:
                await self._execute_step(s, plan)
            return
        
        # Limit concurrency via semaphore
        sem = asyncio.Semaphore(self.config.max_parallel_tools)
        
        async def run_with_semaphore(step: ToolStep):
            async with sem:
                await self._execute_step(step, plan)
        
        # Execute all steps with concurrency limit
        tasks = [run_with_semaphore(step) for step in steps]
        await asyncio.gather(*tasks, return_exceptions=True)
    
    def _group_safe_parallel_batches(self, steps: List[ToolStep]) -> Tuple[List[List[ToolStep]], List[ToolStep]]:
        """
        Group steps into parallel-executable batches and sequential steps.
        
        FIX #4: Added deduplication for identical (tool, args) pairs
        FIX: Added IMPLICIT DEPENDENCY DETECTION in args
        
        ISSUE: When macros resolve to same iteration, we get duplicate tool calls
        SOLUTION: Detect identical calls and reuse results
        
        ISSUE: LLM generates plans where step 2 args contain "<result from step 1>"
        but doesn't set depends_on properly, causing parallel execution failure
        SOLUTION: Scan args for implicit dependency patterns and force sequential
        
        Safety rules:
        - Only steps with dependency==NONE, can_parallelize==True, and read_only==True
          are included in parallel batches
        - Steps with dependencies or unsafe flags are returned as sequential_steps
        - Steps with IMPLICIT DEPENDENCIES in args are forced to sequential
        - Identical (tool, args) pairs are deduplicated (second call skipped, first result reused)
        - Parallel batches are limited by max_parallel_tools
        
        Returns:
            Tuple of (parallel_batches, sequential_steps)
        """
        parallel_candidates = []
        sequential_steps = []
        seen_calls = {}  # FIX #4: (tool, args_json) -> first step_id
        
        # Classify steps (preserve original order for sequential)
        for step in steps:
            # ═══════════════════════════════════════════════════════════════════════
            # FIX: Check for IMPLICIT DEPENDENCIES in args
            # If args contain patterns like "<result from step 1>", "$step_1", etc.
            # the step MUST be sequential even if marked as can_parallelize
            # ═══════════════════════════════════════════════════════════════════════
            has_implicit_dep, implicit_dep_step, implicit_reason = _detect_implicit_dependencies_in_step(step.args or {})
            
            if has_implicit_dep:
                # Force sequential execution
                logger.warning(
                    f"[ORCHESTRATOR] IMPLICIT DEPENDENCY detected in step {step.step_id}: {implicit_reason}"
                )
                if implicit_dep_step and not step.depends_on:
                    # Set the depends_on if we detected a specific step
                    step.depends_on = implicit_dep_step
                    step.dependency = ToolDependency.RESULT_DEPENDS
                    logger.info(f"[ORCHESTRATOR]   → Set depends_on={implicit_dep_step}")
                elif not step.depends_on:
                    # Assume depends on previous step if no specific step detected
                    step_num = int(step.step_id.replace("step_", "")) if step.step_id.startswith("step_") else 1
                    if step_num > 1:
                        step.depends_on = f"step_{step_num - 1}"
                        step.dependency = ToolDependency.SEQUENTIAL
                        logger.info(f"[ORCHESTRATOR]   → Set depends_on=step_{step_num - 1} (assumed previous)")
                
                # Mark as not parallelizable
                step.can_parallelize = False
                sequential_steps.append(step)
                continue  # Skip parallel candidate check
            
            if (step.dependency == ToolDependency.NONE and 
                step.can_parallelize and 
                step.read_only):
                # FIX #4: Check for duplicate (tool, args) pair
                try:
                    args_key = json.dumps(step.args or {}, sort_keys=True, default=str)
                except Exception:
                    args_key = str(step.args)
                
                call_signature = (step.tool_name, args_key)
                
                if call_signature in seen_calls:
                    # FIX #4: Duplicate found - convert to sequential with dependency
                    first_step_id = seen_calls[call_signature]
                    logger.warning(f"[ORCHESTRATOR] FIX #4: Duplicate detected: {step.step_id} == {first_step_id}")
                    logger.warning(f"[ORCHESTRATOR]   Tool: {step.tool_name}, converting to sequential dependency")
                    step.depends_on = first_step_id
                    step.dependency = ToolDependency.RESULT
                    sequential_steps.append(step)
                else:
                    seen_calls[call_signature] = step.step_id
                    parallel_candidates.append(step)
            else:
                sequential_steps.append(step)
        
        # Batch parallel candidates according to max_parallel_tools
        batches = []
        max_parallel = max(1, self.config.max_parallel_tools)
        for i in range(0, len(parallel_candidates), max_parallel):
            batch = parallel_candidates[i:i+max_parallel]
            batches.append(batch)
        
        logger.info(f"[ORCHESTRATOR] Grouped {len(steps)} steps into {len(batches)} parallel batches and {len(sequential_steps)} sequential steps")
        if sequential_steps:
            logger.info(f"[ORCHESTRATOR]   Sequential steps: {[s.step_id for s in sequential_steps]}")
        return batches, sequential_steps
    
    def _normalize_step_hints(self, steps: List[ToolStep]):
        """
        Normalize parallel execution hints from tool metadata registry.
        
        Uses get_tool_safety_hints() for dynamic inference of tool safety,
        falling back to sensible defaults based on tool name patterns.
        Only updates hints if not explicitly set by planner.
        """
        for step in steps:
            # Use the dynamic safety hint method (handles unknown tools via pattern matching)
            meta = self.get_tool_safety_hints(step.tool_name)
            
            # Only update if not explicitly set by planner
            if not hasattr(step, 'read_only') or step.read_only is None:
                step.read_only = meta.get("read_only", True)
            if not hasattr(step, 'can_parallelize') or not step.can_parallelize:
                step.can_parallelize = meta.get("can_parallelize", False)
        
        logger.debug(f"[ORCHESTRATOR] Normalized {len(steps)} steps with safety hints")
    
    async def _resolve_identity_for_step(
        self, 
        person_value: str, 
        arg_name: str, 
        step: ToolStep
    ) -> str:
        """
        Resolve a person identifier to ADO identity email for a tool step.
        
        This method:
        1. Skips resolution if value is already an email (contains '@')
        2. Calls ADO identity API (core_get_identity_ids) to resolve names
        3. Uses best-match resolution for full names
        4. Logs all steps for debugging and Langfuse tracing
        
        Args:
            person_value: The person identifier to resolve (name, username, email)
            arg_name: The argument name (for logging, e.g., 'assignedTo')
            step: The ToolStep being executed (for context)
            
        Returns:
            Resolved email string, or original value if already resolved or resolution fails.
        """
        person_value = str(person_value).strip()
        
        # DOMAIN-AWARE EMAIL CHECK: Only skip if it's a REAL org email, not fabricated
        KNOWN_ORG_DOMAINS = ['walkingtree.tech', 'intelloger.com', 'fracpro.com', 'stratagen.com', 'linqx.io']
        if '@' in person_value:
            email_domain = person_value.split('@')[-1].lower()
            if email_domain in KNOWN_ORG_DOMAINS:
                # Known org domain — verify it's real via ADO API before trusting
                try:
                    verify_response = await self.mcp_connector.call_tool(
                        'core_get_identity_ids', {'searchFilter': person_value}
                    )
                    if verify_response and 'No identities found' not in str(verify_response):
                        logger.debug(f"[ORCHESTRATOR] Identity: Verified real email '{person_value}'")
                        return person_value
                    else:
                        # Fabricated email! Strip to raw name for resolution
                        raw_name = person_value.split('@')[0].replace('.', ' ').replace('_', ' ').title()
                        logger.warning(f"[ORCHESTRATOR] Identity: FABRICATED email detected: '{person_value}' → using raw name: '{raw_name}'")
                        person_value = raw_name
                except Exception as verify_err:
                    logger.warning(f"[ORCHESTRATOR] Identity: Email verification failed: {verify_err}")
                    raw_name = person_value.split('@')[0].replace('.', ' ').replace('_', ' ').title()
                    person_value = raw_name
            else:
                # External domain — trust it, skip resolution
                logger.debug(f"[ORCHESTRATOR] Identity: External email '{person_value}', passing through")
                return person_value
        
        # Skip empty values
        if not person_value:
            return person_value
        
        logger.info(
            f"[ORCHESTRATOR] Identity: Resolving {arg_name}='{person_value}' "
            f"for step={step.step_id} tool={step.tool_name}"
        )
        
        # Create Langfuse span for identity resolution (matches rest of application pattern)
        identity_span = None
        if LANGFUSE_AVAILABLE:
            identity_span = create_span(
                name="identity_resolution",
                input_data={
                    "person_value": person_value,
                    "arg_name": arg_name,
                    "step_id": step.step_id,
                    "tool_name": step.tool_name
                },
                metadata={
                    "component": "multi_tool_orchestrator",
                    "operation": "resolve_person_name"
                }
            )
        
        try:
            # Use the identity resolution module
            resolution = await resolve_person_name(
                person_value, 
                self.mcp_connector,
                allow_clarification=False  # In orchestrator, use best-match without blocking
            )
            
            if resolution.resolved and resolution.identity_email:
                resolved_email = resolution.identity_email
                logger.info(
                    f"[ORCHESTRATOR] Identity: Resolved '{person_value}' → '{resolved_email}' "
                    f"(confidence={resolution.confidence:.2f}, method={resolution.resolution_method})"
                )
                if METRICS_AVAILABLE:
                    get_metrics_collector().increment(
                        "identity_resolution_success", 
                        {"tool": step.tool_name, "arg": arg_name}
                    )
                # Finalize Langfuse span with success
                if identity_span:
                    finalize_span(identity_span, output={
                        "resolved": True,
                        "original": person_value,
                        "email": resolved_email,
                        "confidence": resolution.confidence,
                        "method": resolution.resolution_method
                    }, status="success")
                return resolved_email
            elif resolution.error_message:
                logger.warning(
                    f"[ORCHESTRATOR] Identity: No match for '{person_value}' - {resolution.error_message}. "
                    f"Using original value."
                )
                if identity_span:
                    finalize_span(identity_span, output={
                        "resolved": False,
                        "original": person_value,
                        "error": resolution.error_message
                    }, status="warning")
                return person_value
            else:
                # Best-effort: use display name from first match if available
                if resolution.all_matches:
                    best_match = resolution.all_matches[0]
                    email = best_match.get('uniqueName') or best_match.get('displayName', person_value)
                    logger.info(
                        f"[ORCHESTRATOR] Identity: Using best-match for '{person_value}' → '{email}' "
                        f"(out of {len(resolution.all_matches)} matches)"
                    )
                    if identity_span:
                        finalize_span(identity_span, output={
                            "resolved": True,
                            "original": person_value,
                            "email": email,
                            "method": "best_match",
                            "match_count": len(resolution.all_matches)
                        }, status="success")
                    return email
                if identity_span:
                    finalize_span(identity_span, output={
                        "resolved": False,
                        "original": person_value,
                        "error": "no_matches"
                    }, status="warning")
                return person_value
                
        except Exception as e:
            logger.warning(f"[ORCHESTRATOR] Identity: Resolution failed for '{person_value}': {e}")
            # Finalize Langfuse span with error
            if identity_span:
                finalize_span(identity_span, output={
                    "resolved": False,
                    "original": person_value,
                    "error": str(e)
                }, status="error", level="WARNING")
            return person_value
    
    def _resolve_placeholder_from_step_result(self, placeholder: str, prev_result: Any, arg_name: str, prev_step_id: str) -> Any:
        """
        FIX #17: Resolve angle-bracket placeholders using previous step results.
        
        Handles patterns like:
        - <IDS_FROM_STEP_1>, <IDS_OF_PULL_REQUESTS_FROM_STEP_1> → extract 'id' or 'ids' from items
        - <WORK_ITEMS_FROM_STEP_1>, <ITEMS_FROM_STEP_1> → extract items array
        - <COUNT_FROM_STEP_1> → extract count
        
        Args:
            placeholder: The placeholder string (e.g., "<IDS_FROM_STEP_1>")
            prev_result: The result dict from previous step
            arg_name: The argument name being resolved (e.g., "ids", "workItemIds")
            prev_step_id: The ID of the previous step
            
        Returns:
            Resolved value (array, string, or None if resolution fails)
        """
        if not isinstance(prev_result, dict):
            logger.warning(f"[ORCHESTRATOR] FIX #17: Previous result is not a dict, cannot resolve placeholder")
            return None
        
        placeholder_lower = placeholder.lower()
        
        # Determine what data to extract based on placeholder text and argument name
        extracted_data = None

        # Handle "top N" placeholders (e.g., <TOP_3_WORK_ITEM_IDS>) by ranking items
        # using comment count metadata when available. Falls back to raw order when
        # counts are missing so we still return a deterministic subset.
        top_match = re.search(r"top[_\s-]?(\d+)", placeholder_lower)
        top_n = int(top_match.group(1)) if top_match else None
        wants_top_work_items = top_n is not None and "work" in placeholder_lower
        
        # Check for ID-related placeholders
        if "id" in placeholder_lower:
            # Try to extract IDs from items array
            if "items" in prev_result and isinstance(prev_result["items"], list):
                items_source = prev_result["items"]

                # Optionally rank items by comment count when top_n is requested for work items
                if wants_top_work_items:
                    def _comment_count(item: Any) -> int:
                        if not isinstance(item, dict):
                            return 0
                        fields = item.get("fields", {}) or {}
                        # Try multiple likely field names for comment count
                        candidates = [
                            fields.get("System.CommentCount"),
                            fields.get("Microsoft.VSTS.Common.CommentCount"),
                            fields.get("commentCount"),
                            fields.get("CommentCount"),
                        ]
                        for val in candidates:
                            try:
                                return int(val)
                            except (TypeError, ValueError):
                                continue
                        return 0

                    items_source = sorted(items_source, key=_comment_count, reverse=True)

                # Limit to requested top N (for any top_N placeholder, not just work items)
                if top_n is not None and top_n > 0:
                    items_source = items_source[:top_n]
                    logger.info(f"[ORCHESTRATOR] FIX #17: Limited items to top {top_n} for placeholder '{placeholder}'")

                # Extract 'id' or 'identifier' from each item
                extracted_ids = []
                for item in items_source:
                    if isinstance(item, dict):
                        # Prefer 'id', then 'alertId', then 'identifier', then 'workItemId'
                        item_id = item.get("id") or item.get("alertId") or item.get("identifier") or item.get("workItemId")
                        if item_id:
                            try:
                                extracted_ids.append(int(item_id))
                            except (TypeError, ValueError):
                                extracted_ids.append(str(item_id))
                
                # FIX #17 ENHANCEMENT: Return SCALAR for single ID, ARRAY for multiple IDs
                # Most tools expect scalar (e.g., buildId: "24097", not ["24097"])
                # But some tools expect arrays (e.g., ids: ["1", "2", "3"])
                # Returning scalar for single ID fixes pipelines_get_build_changes error
                if len(extracted_ids) == 1:
                    # Single ID - return as scalar string
                    extracted_data = extracted_ids[0]
                    logger.info(f"[ORCHESTRATOR] FIX #17: Extracted single ID '{extracted_data}' from items (returning as scalar)")
                else:
                    # Multiple IDs or zero - return as array
                    extracted_data = extracted_ids
                    if extracted_ids:
                        logger.info(f"[ORCHESTRATOR] FIX #17: Extracted {len(extracted_ids)} IDs from items array (returning as array)")
                    else:
                        logger.info(f"[ORCHESTRATOR] FIX #17: Previous step returned 0 items, resolving placeholder to empty array []")
        
        # Check for items/results placeholders (only if ID extraction didn't already set data)
        if extracted_data is None and ("item" in placeholder_lower or "result" in placeholder_lower):
            if "items" in prev_result:
                extracted_data = prev_result["items"]
                logger.info(f"[ORCHESTRATOR] FIX #17: Extracted items array from previous result")
        
        # Check for count placeholders (only if previous extraction didn't already set data)
        if extracted_data is None and "count" in placeholder_lower:
            if "count" in prev_result:
                extracted_data = prev_result["count"]
                logger.info(f"[ORCHESTRATOR] FIX #17: Extracted count from previous result")
        
        # Fallback: try to use the entire previous result if it's a list or simple value (only if nothing extracted yet)
        if extracted_data is None:
            if isinstance(prev_result, list):
                extracted_data = prev_result
            elif "result" in prev_result:
                extracted_data = prev_result["result"]
            elif "data" in prev_result:
                extracted_data = prev_result["data"]
        
        if extracted_data is None:
            logger.warning(f"[ORCHESTRATOR] FIX #17: Could not extract data for placeholder '{placeholder}' from previous step result")
            return None
        
        return extracted_data
    
    async def _resolve_iteration_macro(self, macro: str, args: Dict[str, Any], context: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Resolve iteration macros like '@CurrentIteration' or '@PreviousIteration' 
        to concrete iteration identifiers.
        
        SUPPORTS OFFSET MACROS:
        - @CurrentIteration → Current iteration
        - @CurrentIteration - 1 → 1 iteration before current
        - @PreviousIteration → Iteration before current
        - @PreviousIteration - 1 → 2 iterations before current
        - @PreviousIteration - 2 → 3 iterations before current
        
        Resolution strategy (in order):
        1. Try team iterations via MCP (work_list_team_iterations)
        2. Try project-level iterations via MCP (work_list_iterations) 
        3. Fallback to direct REST API for iteration tree
        
        For finding current iteration:
        - Look for timeFrame=current or isCurrent=True flag
        - Find the most specific (leaf) iteration containing today's date
        
        Args:
            macro: The macro string (e.g., "@CurrentIteration", "@PreviousIteration - 2")
            args: Step arguments (may contain project/team)
            context: Execution context
            
        Returns:
            Dict with {"iterationId": <id>, "iterationPath": <path>} or None if resolution fails
        """
        project = args.get("project") or context.get("project", "")
        # FIX: Ensure project is a string, not a list (Planner sometimes returns lists)
        if isinstance(project, list) and project:
            project = project[0]
            
        team = args.get("team") or context.get("team", "")
        # FIX: Ensure team is a string
        if isinstance(team, list) and team:
            team = team[0]
        
        logger.info(f"[ORCHESTRATOR] Resolving macro '{macro}' with project={project}, team={team}")
        logger.debug(f"[ORCHESTRATOR] args keys: {list(args.keys()) if args else 'None'}")
        logger.debug(f"[ORCHESTRATOR] context keys: {list(context.keys()) if context else 'None'}")
        
        # ═══════════════════════════════════════════════════════════════════
        # PARSE OFFSET FROM MACRO (e.g., "@PreviousIteration - 2" → offset=2)
        # ═══════════════════════════════════════════════════════════════════
        offset = 0
        base_macro = macro.strip()
        
        # Match patterns like "@PreviousIteration - 2" or "@CurrentIteration-1"
        offset_match = re.search(r'(.+?)\s*-\s*(\d+)\s*$', macro)
        if offset_match:
            base_macro = offset_match.group(1).strip()
            offset = int(offset_match.group(2))
            logger.info(f"[ORCHESTRATOR] Parsed offset macro: '{macro}' → base='{base_macro}', offset={offset}")
        
        if not project:
            logger.warning("[ORCHESTRATOR] Cannot resolve iteration macro: missing project")
            return None
        
        iterations = []
        
        # ═══════════════════════════════════════════════════════════════════
        # STRATEGY 1: Try team iterations if team is available
        # ═══════════════════════════════════════════════════════════════════
        if team and team.strip():
            try:
                logger.info(f"[ORCHESTRATOR] Strategy 1: Trying team iterations for team={team}")
                raw_result = await self.mcp_connector.call_tool("work_list_team_iterations", {
                    "project": project,
                    "team": team
                })
                if raw_result and isinstance(raw_result, str) and raw_result.strip():
                    parsed = json.loads(raw_result)
                    iterations = parsed if isinstance(parsed, list) else parsed.get("value", parsed.get("iterations", []))
                    if iterations:
                        logger.info(f"[ORCHESTRATOR] Strategy 1 SUCCESS: Found {len(iterations)} team iterations")
            except Exception as e:
                logger.debug(f"[ORCHESTRATOR] Strategy 1 failed: {e}")
        
        # ═══════════════════════════════════════════════════════════════════
        # STRATEGY 2: Try project-level iterations via MCP
        # ═══════════════════════════════════════════════════════════════════
        if not iterations:
            try:
                logger.info(f"[ORCHESTRATOR] Strategy 2: Trying project-level iterations")
                raw_result = await self.mcp_connector.call_tool("work_list_iterations", {
                    "project": project,
                    "depth": 10  # Get full iteration tree including sprints
                })
                logger.debug(f"[ORCHESTRATOR] Strategy 2 MCP call returned, raw_result type: {type(raw_result)}, first 200 chars: {str(raw_result)[:200] if raw_result else 'None'}")
                if raw_result and isinstance(raw_result, str) and raw_result.strip():
                    parsed = json.loads(raw_result)
                    logger.debug(f"[ORCHESTRATOR] Strategy 2 parsed type: {type(parsed)}, keys: {list(parsed.keys()) if isinstance(parsed, dict) else 'list'}")
                    tree_data = parsed if isinstance(parsed, list) else parsed.get("value", parsed.get("iterations", []))
                    
                    # Flatten the nested tree structure
                    def flatten_iteration_tree(node, parent_path=""):
                        result = []
                        if isinstance(node, dict):
                            name = node.get('name', '')
                            path = node.get('path', '')
                            if not path and name:
                                path = f"{parent_path}\\{name}" if parent_path else name
                            
                            result.append({
                                'id': node.get('id'),
                                'identifier': node.get('identifier'),
                                'name': name,
                                'path': path,
                                'attributes': node.get('attributes', {}),
                                'hasChildren': node.get('hasChildren', False)
                            })
                            
                            for child in node.get('children', []):
                                result.extend(flatten_iteration_tree(child, path))
                        elif isinstance(node, list):
                            for item in node:
                                result.extend(flatten_iteration_tree(item, parent_path))
                        return result
                    
                    iterations = flatten_iteration_tree(tree_data)
                    if iterations:
                        logger.info(f"[ORCHESTRATOR] Strategy 2 SUCCESS: Found {len(iterations)} project iterations (flattened)")
                        # Log iteration names for debugging
                        iter_names = [it.get('name', 'unnamed') for it in iterations[:10]]
                        logger.debug(f"[ORCHESTRATOR] Strategy 2 iteration sample: {iter_names}")
                    else:
                        logger.debug(f"[ORCHESTRATOR] Strategy 2: Flattening returned empty list")
                else:
                    logger.debug(f"[ORCHESTRATOR] Strategy 2: raw_result empty or not string")
            except Exception as e:
                logger.info(f"[ORCHESTRATOR] Strategy 2 failed: {e}")
        
        # ═══════════════════════════════════════════════════════════════════
        # STRATEGY 3: Direct REST API fallback for iteration tree
        # ═══════════════════════════════════════════════════════════════════
        if not iterations:
            # Check if using mock connector - skip REST API in mock mode
            # Use class name check to avoid import error if mock module doesn't exist
            connector_class_name = type(self.mcp_connector).__name__
            is_mock_connector = "Mock" in connector_class_name
            if is_mock_connector:
                logger.info(f"[ORCHESTRATOR] Strategy 3: Skipping REST API (using {connector_class_name})")
            else:
                try:
                    logger.info(f"[ORCHESTRATOR] Strategy 3: Using direct REST API for iteration tree")
                    iterations = await self._get_iterations_from_rest_api(project)
                    if iterations:
                        logger.info(f"[ORCHESTRATOR] Strategy 3 SUCCESS: Found {len(iterations)} iterations from REST API")
                except Exception as e:
                    logger.warning(f"[ORCHESTRATOR] Strategy 3 failed: {e}")
        
        if not iterations:
            logger.warning(f"[ORCHESTRATOR] All strategies failed to get iterations for project={project}")
            return None
        
        # ═══════════════════════════════════════════════════════════════════
        # FIND THE TARGET ITERATION based on macro
        # ═══════════════════════════════════════════════════════════════════
        chosen_iteration = None
        base_macro_lower = base_macro.lower()
        now = datetime.utcnow()
        
        # ═══════════════════════════════════════════════════════════════════
        # BRANCH: Bare sprint name (e.g. '26.01', '26.4', 'Sprint 26.1')
        # Not a @macro — search iteration tree by name
        # ═══════════════════════════════════════════════════════════════════
        _SPRINT_NAME_RE = re.compile(r'^(?:Sprint\s*)?(\d+\.\d+|\d+)$', re.IGNORECASE)
        if not base_macro.startswith("@") and _SPRINT_NAME_RE.match(base_macro):
            chosen_iteration = self._find_iteration_by_sprint_name(iterations, base_macro)

            # ── FALLBACK: If sprint not found in team iterations, try project-level ──
            if not chosen_iteration and team and team.strip():
                logger.info(f"[ORCHESTRATOR] Sprint '{base_macro}' not in team '{team}' iterations, trying project-level fallback")
                project_iterations = []
                # Strategy 2: project-level iterations via MCP
                try:
                    raw_result = await self.mcp_connector.call_tool("work_list_iterations", {
                        "project": project,
                        "depth": 10
                    })
                    if raw_result and isinstance(raw_result, str) and raw_result.strip():
                        parsed = json.loads(raw_result)
                        tree_data = parsed if isinstance(parsed, list) else parsed.get("value", parsed.get("iterations", []))
                        def _flatten(node, parent_path=""):
                            result = []
                            if isinstance(node, dict):
                                name = node.get('name', '')
                                path = node.get('path', '')
                                if not path and name:
                                    path = f"{parent_path}\\{name}" if parent_path else name
                                result.append({
                                    'id': node.get('id'), 'identifier': node.get('identifier'),
                                    'name': name, 'path': path,
                                    'attributes': node.get('attributes', {}),
                                    'hasChildren': node.get('hasChildren', False)
                                })
                                for child in node.get('children', []):
                                    result.extend(_flatten(child, path))
                            elif isinstance(node, list):
                                for item in node:
                                    result.extend(_flatten(item, parent_path))
                            return result
                        project_iterations = _flatten(tree_data)
                        if project_iterations:
                            logger.info(f"[ORCHESTRATOR] Project-level fallback: {len(project_iterations)} iterations")
                            chosen_iteration = self._find_iteration_by_sprint_name(project_iterations, base_macro)
                except Exception as e:
                    logger.debug(f"[ORCHESTRATOR] Project-level fallback failed: {e}")

                # Strategy 3: REST API fallback
                if not chosen_iteration and not project_iterations:
                    connector_class_name = type(self.mcp_connector).__name__
                    if "Mock" not in connector_class_name:
                        try:
                            rest_iterations = await self._get_iterations_from_rest_api(project)
                            if rest_iterations:
                                logger.info(f"[ORCHESTRATOR] REST API fallback: {len(rest_iterations)} iterations")
                                chosen_iteration = self._find_iteration_by_sprint_name(rest_iterations, base_macro)
                        except Exception as e:
                            logger.debug(f"[ORCHESTRATOR] REST API fallback failed: {e}")

            if chosen_iteration:
                iteration_id = chosen_iteration.get("identifier") or chosen_iteration.get("iterationId") or chosen_iteration.get("id")
                iteration_path = chosen_iteration.get("path") or chosen_iteration.get("iterationPath") or chosen_iteration.get("name")
                if iteration_id is not None:
                    iteration_id = str(iteration_id)
                resolved = {
                    "iterationId": iteration_id or iteration_path,
                    "iterationPath": iteration_path,
                    "iterationName": chosen_iteration.get("name", ""),
                    "_resolved_from_macro": macro,
                    "_offset_applied": 0
                }
                logger.info(f"[ORCHESTRATOR] ✅ Resolved sprint name '{macro}' → path={resolved['iterationPath']}, id={resolved['iterationId']}")
                return resolved
            else:
                logger.warning(f"[ORCHESTRATOR] Sprint name '{macro}' not found in any iteration source")
                return None

        if "current" in base_macro_lower or base_macro == "@CurrentIteration":
            # First: Look for explicitly marked current iteration
            for it in iterations:
                attrs = it.get("attributes", {}) if isinstance(it, dict) else {}
                time_frame_raw = attrs.get("timeFrame", "")
                # ADO timeFrame values: 0=past, 1=current, 2=future
                # FIX: Was incorrectly checking == 2, should be == 1 for current
                if isinstance(time_frame_raw, int):
                    time_frame = "current" if time_frame_raw == 1 else ""
                else:
                    time_frame = str(time_frame_raw).lower() if time_frame_raw else ""
                is_current = attrs.get("isCurrent", False)
                
                if time_frame == "current" or is_current:
                    chosen_iteration = it
                    logger.info(f"[ORCHESTRATOR] Found current via flag: {it.get('name')}")
                    break
            
            # Second: Find most specific iteration containing today
            if not chosen_iteration:
                chosen_iteration = self._find_current_iteration_by_date(iterations, now)
                if chosen_iteration:
                    logger.info(f"[ORCHESTRATOR] Found current via date match: {chosen_iteration.get('name')}")
        
        elif "previous" in base_macro_lower or "last" in base_macro_lower or base_macro == "@PreviousIteration":
            # Find iteration that ended most recently before today
            # FIX #6: Pass pre-resolved current iteration from cache if available
            current_from_cache = context.get("_iteration_cache", {}).get("@CurrentIteration")
            chosen_iteration = self._find_previous_iteration_by_date(iterations, now, current_from_cache)
            if chosen_iteration:
                logger.info(f"[ORCHESTRATOR] Found previous iteration: {chosen_iteration.get('name')}")
        
        else:
            # ═══════════════════════════════════════════════════════════════
            # FIX: Handle bare sprint numbers (e.g., "26.01", "25.19")
            # Normalize and match by name/path against fetched iterations
            # ═══════════════════════════════════════════════════════════════
            sprint_normalized = base_macro.strip()
            parts = sprint_normalized.split(".")
            if len(parts) == 2:
                major = parts[0].lstrip('0') or '0'
                minor = parts[1].lstrip('0') or '0'
                sprint_normalized = f"{major}.{minor}"
            
            logger.info(f"[ORCHESTRATOR] Resolving bare sprint number '{macro}' (normalized: '{sprint_normalized}') against {len(iterations)} iterations")
            
            # Debug: log all iteration names for troubleshooting
            all_iter_names = [(it.get("name", ""), it.get("path", "")) for it in iterations]
            logger.info(f"[ORCHESTRATOR] Available iterations (first 20): {all_iter_names[:20]}")
            
            for it in iterations:
                name = (it.get("name") or "").strip()
                path = it.get("path") or ""
                
                # Dynamically extract the numeric portion from iteration name
                # Handles any prefix: "Sprint 26.3" → "26.3", "Iteration 26.3" → "26.3", "26.3" → "26.3"
                numeric_match = re.search(r'(\d+\.\d+)', name)
                if numeric_match:
                    raw_numeric = numeric_match.group(1)
                    nm_parts = raw_numeric.split(".")
                    nm_major = nm_parts[0].lstrip('0') or '0'
                    nm_minor = nm_parts[1].lstrip('0') or '0'
                    name_normalized = f"{nm_major}.{nm_minor}"
                else:
                    # Fallback: normalize the full name as before
                    name_parts = name.split(".")
                    if len(name_parts) == 2:
                        name_major = name_parts[0].lstrip('0') or '0'
                        name_minor = name_parts[1].lstrip('0') or '0'
                        name_normalized = f"{name_major}.{name_minor}"
                    else:
                        name_normalized = name
                
                if sprint_normalized == name_normalized:
                    chosen_iteration = it
                    logger.info(f"[ORCHESTRATOR] Matched sprint '{macro}' to iteration '{name}' (normalized: {name_normalized})")
                    break
                
                # Check if sprint ref is contained in path — handle both bare and prefixed forms
                # e.g., path might be "FracPro-OPS\FracPro Suite\Sprint 26.3" or "...\26.3"
                path_numeric_segments = re.findall(r'(\d+\.\d+)', path)
                path_matched = False
                for seg in path_numeric_segments:
                    seg_parts = seg.split(".")
                    seg_normalized = f"{seg_parts[0].lstrip('0') or '0'}.{seg_parts[1].lstrip('0') or '0'}"
                    if sprint_normalized == seg_normalized:
                        path_matched = True
                        break
                if path_matched:
                    chosen_iteration = it
                    logger.info(f"[ORCHESTRATOR] Matched sprint '{macro}' via path '{path}'")
                    break
            
            if not chosen_iteration:
                logger.warning(f"[ORCHESTRATOR] Sprint '{macro}' (normalized: '{sprint_normalized}') not found in {len(iterations)} team iterations")

            # ───────────────────────────────────────────────────────────────
            # PROJECT-LEVEL FALLBACK: If team iterations didn't contain the
            # sprint, fetch all project iterations and re-scan.
            # ───────────────────────────────────────────────────────────────
            if not chosen_iteration and team and team.strip():
                logger.warning(f"[ORCHESTRATOR] Project-level fallback: team '{team}' iterations didn't contain '{sprint_normalized}', trying project iterations")
                try:
                    proj_raw = await self.mcp_connector.call_tool("work_list_iterations", {
                        "project": project,
                        "depth": 10
                    })
                    proj_iterations: list = []
                    if proj_raw and isinstance(proj_raw, str) and proj_raw.strip():
                        proj_parsed = json.loads(proj_raw)
                        proj_tree = proj_parsed if isinstance(proj_parsed, list) else proj_parsed.get("value", proj_parsed.get("iterations", []))

                        def _flatten_proj(node, parent_path=""):
                            out: list = []
                            if isinstance(node, dict):
                                n = node.get("name", "")
                                p = node.get("path", "") or (f"{parent_path}\\{n}" if parent_path else n)
                                out.append({
                                    "id": node.get("id"),
                                    "identifier": node.get("identifier"),
                                    "name": n,
                                    "path": p,
                                    "attributes": node.get("attributes", {}),
                                    "hasChildren": node.get("hasChildren", False),
                                })
                                for ch in node.get("children", []):
                                    out.extend(_flatten_proj(ch, p))
                            elif isinstance(node, list):
                                for item in node:
                                    out.extend(_flatten_proj(item, parent_path))
                            return out

                        proj_iterations = _flatten_proj(proj_tree)
                        logger.warning(f"[ORCHESTRATOR] Project-level fallback: fetched {len(proj_iterations)} project iterations")

                    # Re-scan project iterations with same normalization logic
                    for it in proj_iterations:
                        pname = (it.get("name") or "").strip()
                        ppath = it.get("path") or ""

                        pnum = re.search(r'(\d+\.\d+)', pname)
                        if pnum:
                            raw = pnum.group(1)
                            pp = raw.split(".")
                            pnorm = f"{pp[0].lstrip('0') or '0'}.{pp[1].lstrip('0') or '0'}"
                        else:
                            pp2 = pname.split(".")
                            if len(pp2) == 2:
                                pnorm = f"{pp2[0].lstrip('0') or '0'}.{pp2[1].lstrip('0') or '0'}"
                            else:
                                pnorm = pname

                        if sprint_normalized == pnorm:
                            chosen_iteration = it
                            logger.warning(f"[ORCHESTRATOR] Project-level fallback MATCHED '{macro}' → '{pname}' (id={it.get('id')})")
                            break

                        # Path-based matching
                        psegs = re.findall(r'(\d+\.\d+)', ppath)
                        for seg in psegs:
                            sp = seg.split(".")
                            snorm = f"{sp[0].lstrip('0') or '0'}.{sp[1].lstrip('0') or '0'}"
                            if sprint_normalized == snorm:
                                chosen_iteration = it
                                logger.warning(f"[ORCHESTRATOR] Project-level fallback MATCHED '{macro}' via path '{ppath}'")
                                break
                        if chosen_iteration:
                            break

                    if not chosen_iteration:
                        logger.warning(f"[ORCHESTRATOR] Project-level fallback: sprint '{sprint_normalized}' not found in any project iterations either")
                except Exception as e:
                    logger.warning(f"[ORCHESTRATOR] Project-level fallback failed with error: {e}")

        # ═══════════════════════════════════════════════════════════════════
        # APPLY OFFSET (walk back N iterations from chosen)
        # ═══════════════════════════════════════════════════════════════════
        if chosen_iteration and offset > 0:
            logger.info(f"[ORCHESTRATOR] Applying offset={offset} to iteration '{chosen_iteration.get('name')}'")
            chosen_iteration = self._apply_iteration_offset(iterations, chosen_iteration, offset, now)
            if chosen_iteration:
                logger.info(f"[ORCHESTRATOR] After offset: {chosen_iteration.get('name')}")
            else:
                logger.warning(f"[ORCHESTRATOR] Failed to apply offset {offset} - not enough iterations")
                return None
        
        if not chosen_iteration:
            logger.warning(f"[ORCHESTRATOR] Could not resolve macro '{macro}' to any iteration")
            return None
        
        # Extract identifiers - IMPORTANT: Prefer 'identifier' (UUID) over 'id' (numeric)
        # Azure DevOps APIs require the UUID identifier, not the numeric ID
        iteration_id = chosen_iteration.get("identifier") or chosen_iteration.get("iterationId") or chosen_iteration.get("id")
        iteration_path = chosen_iteration.get("path") or chosen_iteration.get("iterationPath") or chosen_iteration.get("name")
        
        # Ensure iterationId is a string (MCP tools expect string, not int)
        if iteration_id is not None:
            iteration_id = str(iteration_id)
        
        resolved = {
            "iterationId": iteration_id or iteration_path,
            "iterationPath": iteration_path,
            "iterationName": chosen_iteration.get("name", ""),
            "_resolved_from_macro": macro,
            "_offset_applied": offset
        }
        
        logger.info(f"[ORCHESTRATOR] ✅ Resolved '{macro}' → path={resolved['iterationPath']}")
        return resolved
    
    async def _get_iterations_from_rest_api(self, project: str) -> List[Dict[str, Any]]:
        """
        Get all iterations using direct REST API and flatten the tree.
        Returns a flat list of iterations with their full paths.
        """
        import requests
        from config import config
        
        org_url = config.ado_org_url
        pat = config.ado_pat
        
        url = f"{org_url}/{project}/_apis/wit/classificationnodes/iterations?$depth=10&api-version=7.0"
        
        try:
            resp = requests.get(url, auth=("", pat), timeout=30)
            resp.raise_for_status()
            data = resp.json()
            
            # Flatten the tree into a list
            iterations = []
            
            def flatten_tree(node, parent_path=""):
                name = node.get('name', '')
                path = f"{parent_path}\\{name}" if parent_path else name
                attrs = node.get('attributes', {})
                
                iterations.append({
                    'id': node.get('id'),
                    'identifier': node.get('identifier'),
                    'name': name,
                    'path': path,
                    'attributes': attrs,
                    'hasChildren': node.get('hasChildren', False),
                    '_children_count': len(node.get('children', []))
                })
                
                for child in node.get('children', []):
                    flatten_tree(child, path)
            
            flatten_tree(data)
            return iterations
            
        except Exception as e:
            logger.warning(f"[ORCHESTRATOR] REST API iteration fetch failed: {e}")
            return []
    
    def _find_current_iteration_by_date(self, iterations: List[Dict], now: datetime) -> Optional[Dict]:
        """
        Find the most specific (leaf) iteration that contains today's date.
        Prefers iterations without children (sprints) over parent iterations (quarters/years).
        
        Note: Finish dates are treated as end-of-day (23:59:59) to include the entire last day.
        """
        candidates = []
        
        for it in iterations:
            attrs = it.get("attributes", {}) if isinstance(it, dict) else {}
            start_str = attrs.get("startDate") or it.get("startDate")
            finish_str = attrs.get("finishDate") or it.get("finishDate")
            
            if not start_str or not finish_str:
                continue
            
            try:
                start_dt = datetime.fromisoformat(start_str.replace('Z', '+00:00')).replace(tzinfo=None)
                finish_dt = datetime.fromisoformat(finish_str.replace('Z', '+00:00')).replace(tzinfo=None)
                
                # Treat finish date as end-of-day (include the entire last day)
                finish_dt_eod = finish_dt.replace(hour=23, minute=59, second=59)
                
                if start_dt <= now <= finish_dt_eod:
                    # Calculate specificity - smaller range = more specific
                    range_days = (finish_dt - start_dt).days
                    has_children = it.get('hasChildren', False) or it.get('_children_count', 0) > 0
                    
                    candidates.append({
                        'iteration': it,
                        'range_days': range_days,
                        'has_children': has_children
                    })
            except Exception:
                continue
        
        if not candidates:
            return None
        
        # Sort: prefer leaf nodes (no children), then by smallest range
        candidates.sort(key=lambda x: (x['has_children'], x['range_days']))
        
        return candidates[0]['iteration']
    
    @staticmethod
    def _normalize_sprint_name(raw: str) -> str:
        """Normalize a sprint name for comparison: '26.01' -> '26.1', 'Sprint 26.01' -> '26.1'."""
        s = re.sub(r'^(?:Sprint\s*)', '', str(raw), flags=re.IGNORECASE).strip()
        parts = s.split('.')
        if len(parts) == 2:
            try:
                major = str(int(parts[0]))
                minor = str(int(parts[1]))
                return f"{major}.{minor}"
            except ValueError:
                pass
        return s

    def _find_iteration_by_sprint_name(self, iterations: List[Dict], sprint_name: str) -> Optional[Dict]:
        """
        Find an iteration whose name or path matches a sprint name like '26.01', '26.1', 'Sprint 26.4'.
        Normalizes both sides ('26.01' == '26.1') and falls back to substring path match.
        """
        target = self._normalize_sprint_name(sprint_name)
        if not target:
            return None

        # Pass 1: exact name match (normalized)
        for it in iterations:
            name = it.get('name', '')
            if self._normalize_sprint_name(name) == target:
                logger.info(f"[ORCHESTRATOR] Sprint name match (exact): '{sprint_name}' -> '{it.get('path', name)}'")
                return it

        # Pass 2: path contains the target as a segment
        for it in iterations:
            path = it.get('path', '')
            # e.g. 'FracPro-OPS\2026\Q1\26.1' contains '26.1' as a segment
            path_segments = [self._normalize_sprint_name(seg) for seg in path.replace('/', '\\').split('\\')]
            if target in path_segments:
                logger.info(f"[ORCHESTRATOR] Sprint name match (path segment): '{sprint_name}' -> '{path}'")
                return it

        # Pass 3: loose substring match on path (last resort)
        for it in iterations:
            path = it.get('path', '')
            if target in path:
                logger.info(f"[ORCHESTRATOR] Sprint name match (substring): '{sprint_name}' -> '{path}'")
                return it

        logger.warning(f"[ORCHESTRATOR] Sprint name '{sprint_name}' (normalized: '{target}') not found in {len(iterations)} iterations")
        return None

    def _find_previous_iteration_by_date(self, iterations: List[Dict], now: datetime, 
                                          current_iteration_info: Optional[Dict] = None) -> Optional[Dict]:
        """
        Find the iteration that ended most recently before today.
        Prefers leaf iterations (sprints) over parent iterations.
        
        The 'previous' iteration is the one that ended before the current iteration started,
        or before today if no current iteration exists.
        
        Args:
            iterations: List of all iterations
            now: Current datetime
            current_iteration_info: Optional pre-resolved current iteration (from cache)
        """
        # FIX #6: Use pre-resolved current iteration if available, otherwise find it
        current = current_iteration_info
        
        if not current:
            # First: Check for isCurrent flag (ADO's explicit current marker)
            for it in iterations:
                attrs = it.get("attributes", {}) if isinstance(it, dict) else {}
                time_frame_raw = attrs.get("timeFrame", "")
                if isinstance(time_frame_raw, int):
                    time_frame = "current" if time_frame_raw == 1 else ""
                else:
                    time_frame = str(time_frame_raw).lower() if time_frame_raw else ""
                is_current = attrs.get("isCurrent", False)
                
                if time_frame == "current" or is_current:
                    current = it
                    logger.debug(f"[ORCHESTRATOR] Previous resolution: Found current via flag: {it.get('name')}")
                    break
            
            # Second: Fall back to date-based matching
            if not current:
                current = self._find_current_iteration_by_date(iterations, now)
                if current:
                    logger.debug(f"[ORCHESTRATOR] Previous resolution: Found current via date: {current.get('name')}")
        
        current_path = current.get('path', '') if current else ''
        current_name = current.get('name', current.get('iterationName', '')) if current else ''
        
        logger.info(f"[ORCHESTRATOR] Finding previous iteration. Current iteration: {current_name} (path={current_path})")
        
        candidates = []
        
        for it in iterations:
            it_path = it.get('path', '')
            it_name = it.get('name', '')
            
            # Skip current iteration - match by both path AND name to be safe
            if current and (it_path == current_path or it_name == current_name):
                logger.debug(f"[ORCHESTRATOR] Skipping current iteration: {it_name}")
                continue
                
            attrs = it.get("attributes", {}) if isinstance(it, dict) else {}
            finish_str = attrs.get("finishDate") or it.get("finishDate")
            start_str = attrs.get("startDate") or it.get("startDate")
            
            if not finish_str or not start_str:
                continue
            
            try:
                finish_dt = datetime.fromisoformat(finish_str.replace('Z', '+00:00')).replace(tzinfo=None)
                start_dt = datetime.fromisoformat(start_str.replace('Z', '+00:00')).replace(tzinfo=None)
                
                # Use end-of-day for finish comparison
                finish_dt_eod = finish_dt.replace(hour=23, minute=59, second=59)
                
                # Previous iteration must have ENDED before today (finish_dt < now)
                if finish_dt_eod < now:
                    range_days = (finish_dt - start_dt).days
                    has_children = it.get('hasChildren', False) or it.get('_children_count', 0) > 0
                    days_ago = (now - finish_dt).days
                    
                    logger.debug(f"[ORCHESTRATOR] Candidate: {it_name}, finished {days_ago} days ago, has_children={has_children}")
                    
                    candidates.append({
                        'iteration': it,
                        'finish_dt': finish_dt,
                        'range_days': range_days,
                        'has_children': has_children,
                        'days_ago': days_ago
                    })
            except Exception as e:
                logger.debug(f"[ORCHESTRATOR] Skipping iteration {it_name}: {e}")
                continue
        
        if not candidates:
            logger.warning(f"[ORCHESTRATOR] No previous iterations found (all {len(iterations)} iterations checked)")
            return None
        
        logger.info(f"[ORCHESTRATOR] Found {len(candidates)} previous iteration candidates")
        
        # Sort: prefer leaf nodes (no children), then most recent, then smallest range
        candidates.sort(key=lambda x: (x['has_children'], x['days_ago'], x['range_days']))
        
        selected = candidates[0]['iteration']
        logger.info(f"[ORCHESTRATOR] Selected previous iteration: {selected.get('name')} (finished {candidates[0]['days_ago']} days ago)")
        
        return selected
    
    def _apply_iteration_offset(self, iterations: List[Dict], base_iteration: Dict, offset: int, now: datetime) -> Optional[Dict]:
        """
        Apply an offset to walk back N iterations from a base iteration.
        
        Algorithm:
        1. Sort all leaf iterations (sprints) by finish date descending
        2. Find the base iteration in the sorted list
        3. Walk back N positions to apply offset
        
        Args:
            iterations: Full list of iterations
            base_iteration: The iteration to offset from (e.g., previous iteration)
            offset: Number of iterations to go back (e.g., 2 means go back 2 sprints)
            now: Current datetime for filtering
            
        Returns:
            The iteration N steps before base_iteration, or None if not enough history
        """
        # Get only leaf iterations (sprints without children) that have ended
        leaf_iterations = []
        for it in iterations:
            attrs = it.get("attributes", {}) if isinstance(it, dict) else {}
            finish_str = attrs.get("finishDate") or it.get("finishDate")
            start_str = attrs.get("startDate") or it.get("startDate")
            has_children = it.get('hasChildren', False) or it.get('_children_count', 0) > 0
            
            # Skip parent iterations (quarters/years)
            if has_children:
                continue
            
            # Skip iterations without dates
            if not finish_str or not start_str:
                continue
                
            try:
                finish_dt = datetime.fromisoformat(finish_str.replace('Z', '+00:00')).replace(tzinfo=None)
                start_dt = datetime.fromisoformat(start_str.replace('Z', '+00:00')).replace(tzinfo=None)
                
                leaf_iterations.append({
                    'iteration': it,
                    'finish_dt': finish_dt,
                    'start_dt': start_dt,
                    'path': it.get('path', it.get('name', ''))
                })
            except Exception:
                continue
        
        if not leaf_iterations:
            logger.warning("[ORCHESTRATOR] No leaf iterations found for offset calculation")
            return None
        
        # Sort by finish date descending (most recent first)
        leaf_iterations.sort(key=lambda x: x['finish_dt'], reverse=True)
        
        # Find base iteration in sorted list
        base_path = base_iteration.get('path', base_iteration.get('name', ''))
        base_index = None
        for i, item in enumerate(leaf_iterations):
            if item['path'] == base_path:
                base_index = i
                break
        
        if base_index is None:
            logger.warning(f"[ORCHESTRATOR] Base iteration '{base_path}' not found in sorted list")
            return None
        
        # Apply offset
        target_index = base_index + offset
        
        if target_index >= len(leaf_iterations):
            logger.warning(f"[ORCHESTRATOR] Offset {offset} exceeds available history (only {len(leaf_iterations) - base_index - 1} iterations before base)")
            return None
        
        target = leaf_iterations[target_index]
        logger.info(f"[ORCHESTRATOR] Offset applied: {base_path} - {offset} → {target['path']}")
        return target['iteration']
    
    async def _execute_step(self, step: ToolStep, plan: ExecutionPlan):
        """Execute a single step with retry logic and iteration macro resolution."""
        step.status = ExecutionStatus.RUNNING
        start_time = datetime.utcnow()
        
        # Create Langfuse span for step execution (matches rest of application pattern)
        step_span = None
        if LANGFUSE_AVAILABLE:
            step_span = create_span(
                name=f"tool_step_{step.step_id}",
                input_data={
                    "step_id": step.step_id,
                    "tool_name": step.tool_name,
                    "purpose": step.purpose,
                    "args": step.args
                },
                metadata={
                    "component": "multi_tool_orchestrator",
                    "operation": "execute_step",
                    "tool": step.tool_name
                }
            )
        
        # ═══════════════════════════════════════════════════════════════════════════
        # SAFETY CHECK: Validate tool_name is present
        # Synthesis steps should already be filtered, but this catches edge cases
        # ═══════════════════════════════════════════════════════════════════════════
        if not step.tool_name:
            step.status = ExecutionStatus.SKIPPED
            step.error = "No tool specified for step"
            logger.warning(f"[ORCHESTRATOR] Skipping step {step.step_id}: no tool_name specified")
            return
        
        # ═══════════════════════════════════════════════════════════════════════════
        # CONTEXT DATA ANSWER: Return data from plan context without MCP call
        # Handles queries like "list area paths" where data is already available
        # ═══════════════════════════════════════════════════════════════════════════
        if step.tool_name == 'context_data_answer':
            data_key = step.args.get('data_key', '') if isinstance(step.args, dict) else ''
            context_data = (plan.context or {}).get(data_key)
            
            if context_data:
                if isinstance(context_data, list):
                    # Format as items list for proper synthesis
                    items = []
                    for item in context_data:
                        if isinstance(item, str):
                            items.append({"name": item})
                        elif isinstance(item, dict):
                            items.append(item)
                        else:
                            items.append({"name": str(item)})
                    step.result = {"success": True, "count": len(items), "items": items}
                else:
                    step.result = {"success": True, "count": 1, "items": [{"name": str(context_data)}]}
                step.status = ExecutionStatus.SUCCESS
                logger.info(f"[ORCHESTRATOR] context_data_answer: returned {len(step.result.get('items', []))} items for key '{data_key}'")
                
                # Store in plan context for synthesis
                from agents.pm_agent.context_resolver import extract_step_data
                extract_step_data(plan.context, step.step_id, step.result)
                
                if step_span:
                    finalize_span(step_span, output={"status": "success", "count": step.result['count']}, status="success")
                return
            else:
                logger.warning(f"[ORCHESTRATOR] context_data_answer: key '{data_key}' not found in context")
                step.result = {"success": False, "count": 0, "items": [], "error": f"Data key '{data_key}' not found in context"}
                step.status = ExecutionStatus.FAILED
                step.error = f"Context key '{data_key}' not available"
                if step_span:
                    finalize_span(step_span, output={"status": "error"}, status="error")
                return
        
        # ═══════════════════════════════════════════════════════════════════════════
        # CRITICAL FIX #15: Resolve iteration macros BEFORE execution for ALL tools
        # Not limited to wit_get_work_items_for_iteration anymore
        # (Uses pre-resolved cache if available from _pre_resolve_iteration_macros)
        # ═══════════════════════════════════════════════════════════════════════════
        # Check ALL args for iteration macros - GENERIC approach
        ITERATION_ARG_NAMES = frozenset({
            "iterationId", "iterationPath", "iteration", "iterationName",
            "sprintId", "sprintPath", "sprint"
        })
        
        _SPRINT_NAME_RE = re.compile(r'^(?:Sprint\s*)?(\d+\.\d+|\d+)$', re.IGNORECASE)
        if isinstance(step.args, dict):
            for arg_name, arg_value in list(step.args.items()):
                # Check if this is an iteration-related arg with a macro or sprint name
                is_iteration_arg = arg_name.lower() in {n.lower() for n in ITERATION_ARG_NAMES}
                is_macro = isinstance(arg_value, str) and arg_value.startswith("@")
                # FIX: Also detect bare sprint numbers (e.g., "26.01", "25.19")
                is_bare_sprint = is_iteration_arg and isinstance(arg_value, str) and bool(re.match(r'^\d+\.?\d*$', arg_value.strip()))
                is_sprint_name = isinstance(arg_value, str) and is_iteration_arg and _SPRINT_NAME_RE.match(arg_value)
                
                if ((is_macro or is_bare_sprint) or is_sprint_name) and (is_iteration_arg or (is_macro and any(
                    kw in str(arg_value).lower() for kw in ["iteration", "sprint", "current", "previous"]
                ))):
                    logger.info(f"[ORCHESTRATOR] Step {step.step_id}: Detected iteration macro in {arg_name}='{arg_value}', resolving...")
                    
                    # FIRST: Check pre-resolved cache (avoids race conditions in parallel execution)
                    iteration_cache = (plan.context or {}).get("_iteration_cache", {})
                    resolved = iteration_cache.get(arg_value)
                    
                    if resolved:
                        logger.info(f"[ORCHESTRATOR] Step {step.step_id}: Using pre-cached resolution for '{arg_value}'")
                    else:
                        # Cache miss - resolve directly (happens for sequential steps or cache miss)
                        resolved = await self._resolve_iteration_macro(arg_value, step.args or {}, plan.context or {})
                    
                    if resolved:
                        # Update step args with resolved values
                        # Use iterationId if the arg was iterationId, otherwise use path
                        if "id" in arg_name.lower():
                            step.args[arg_name] = resolved.get("iterationId")
                        else:
                            step.args[arg_name] = resolved.get("iterationPath")
                        
                        # Also set the complementary field if not already set
                        if "iterationId" not in step.args and resolved.get("iterationId"):
                            step.args["iterationId"] = resolved.get("iterationId")
                        if "iterationPath" not in step.args and resolved.get("iterationPath"):
                            step.args["iterationPath"] = resolved.get("iterationPath")
                        
                        logger.info(f"[ORCHESTRATOR] ✅ Step {step.step_id}: Macro '{arg_value}' resolved → {arg_name}={step.args[arg_name]}")
                    else:
                        # Resolution failed - try WIQL fallback for iteration-specific tools only
                        if step.tool_name in ("wit_get_work_items_for_iteration", "work_get_iteration_work_items"):
                            logger.warning(f"[ORCHESTRATOR] Step {step.step_id}: Could not resolve macro '{arg_value}', attempting WIQL fallback")
                            
                            fallback_result = await self._fallback_wiql_for_iteration(step.args or {}, plan.context or {})
                            
                            if fallback_result:
                                step.result = fallback_result
                                step.status = ExecutionStatus.SUCCESS
                                plan.intermediate_results[step.step_id] = fallback_result
                                step.execution_time_ms = int((datetime.utcnow() - start_time).total_seconds() * 1000)
                                logger.info(f"[ORCHESTRATOR] ✅ Step {step.step_id}: WIQL fallback succeeded")
                                return
                        
                        # Both resolution and fallback failed
                        step.status = ExecutionStatus.FAILED
                        step.error = f"Could not resolve iteration macro '{arg_value}' for {arg_name}"
                        step.execution_time_ms = int((datetime.utcnow() - start_time).total_seconds() * 1000)
                        logger.error(f"[ORCHESTRATOR] ❌ Step {step.step_id}: {step.error}")
                        if METRICS_AVAILABLE:
                            get_metrics_collector().increment(MetricNames.MULTI_TOOL_STEP_FAILED, {"step": step.step_id, "reason": "macro_resolution"})
                        return
        
        # ═══════════════════════════════════════════════════════════════════════════
        # B3: CONTEXT PRESERVATION - Resolve variable references in step args
        # Supports: ${step_1.items}, ${step_1.count}, ${context.project}, etc.
        # ═══════════════════════════════════════════════════════════════════════════
        if CONTEXT_RESOLVER_AVAILABLE and step.args:
            try:
                resolver = get_context_resolver()
                resolved_args = resolver.resolve_step_args(
                    args=dict(step.args),
                    context=plan.execution_context,
                    step_outputs=plan.step_outputs
                )
                if resolved_args != step.args:
                    logger.info(f"[ORCHESTRATOR] Step {step.step_id}: Resolved variable references in args")
                    step.args = resolved_args
                    if METRICS_AVAILABLE:
                        get_metrics_collector().increment(MetricNames.MULTI_TOOL_CONTEXT_RESOLUTION, {"step": step.step_id})
            except Exception as e:
                logger.warning(f"[ORCHESTRATOR] Step {step.step_id}: Variable resolution failed: {e}")
        
        # Check dependency
        prev_step_id = None
        prev_result = None

        if step.depends_on and step.depends_on in plan.intermediate_results:
            prev_step_id = step.depends_on
            prev_result = plan.intermediate_results[step.depends_on]
        else:
            # Fallback: use the most recent completed step before this one
            for prior_step in plan.steps:
                if prior_step.step_id == step.step_id:
                    break
                if prior_step.step_id in plan.intermediate_results:
                    prev_step_id = prior_step.step_id
                    prev_result = plan.intermediate_results[prior_step.step_id]
                    # Keep scanning to get the most recent prior result

        if prev_result:
            # ═══════════════════════════════════════════════════════════════════════════
            # FIX #17: PLACEHOLDER RESOLUTION IN ARGS
            # Resolve placeholders (e.g., <IDs_FROM_STEP_1>, top_3_work_item_ids)
            # using previous step results even when the planner omitted depends_on
            # ═══════════════════════════════════════════════════════════════════════════
            if step.args and isinstance(step.args, dict):
                for arg_name, arg_value in list(step.args.items()):
                    if isinstance(arg_value, str):
                        is_angle_placeholder = "<" in arg_value and ">" in arg_value
                        is_top_placeholder = "top_" in arg_value.lower()
                        is_from_step_placeholder = bool(re.search(r'[A-Z_]+_FROM_STEP_\d+', arg_value))
                        if is_angle_placeholder or is_top_placeholder or is_from_step_placeholder:
                            resolved_value = self._resolve_placeholder_from_step_result(
                                placeholder=arg_value,
                                prev_result=prev_result,
                                arg_name=arg_name,
                                prev_step_id=prev_step_id or ""
                            )
                            
                            if resolved_value is not None and resolved_value != [] and resolved_value != '':
                                step.args[arg_name] = resolved_value
                                logger.info(f"[ORCHESTRATOR] FIX #17: Resolved placeholder '{arg_value}' in {arg_name} → {resolved_value}")
                            else:
                                # FIX #29: Placeholder couldn't be resolved OR resolved to empty
                                # (prior step returned empty results) - skip this step
                                logger.info(f"[ORCHESTRATOR] FIX #29: Skipping step {step.step_id} - placeholder '{arg_value}' in {arg_name} resolved to empty/None (prior step had no data)")
                                step.status = ExecutionStatus.SKIPPED
                                step.result = {"message": f"Skipped: dependent step returned no data to resolve '{arg_name}'", "items": [], "count": 0}
                                plan.intermediate_results[step.step_id] = step.result
                                plan.step_outputs[step.step_id] = step.result
                                if METRICS_AVAILABLE:
                                    get_metrics_collector().increment(MetricNames.MULTI_TOOL_STEP_SKIPPED, {"step": step.step_id, "reason": "empty_dependency"})
                                return  # Skip execution entirely
            
            # FIX #4 ENHANCEMENT: Check if this is a deduplicated call (same tool + args as depends_on)
            # If so, reuse the result instead of executing again
            if step.dependency == ToolDependency.RESULT:
                # Find the original step we depend on
                original_step = None
                for s in plan.steps:
                    if s.step_id == step.depends_on:
                        original_step = s
                        break
                
                # Check if tool and args match (indicating deduplication by FIX #4)
                if original_step and original_step.tool_name == step.tool_name:
                    try:
                        orig_args_key = json.dumps(original_step.args or {}, sort_keys=True, default=str)
                        step_args_key = json.dumps(step.args or {}, sort_keys=True, default=str)
                        if orig_args_key == step_args_key:
                            logger.info(f"[ORCHESTRATOR] FIX #4: Reusing result from {step.depends_on} for duplicate step {step.step_id}")
                            step.result = original_step.result
                            step.status = ExecutionStatus.SUCCESS
                            plan.intermediate_results[step.step_id] = step.result
                            plan.step_outputs[step.step_id] = step.result
                            step.execution_time_ms = 0  # No execution time, just reused
                            if METRICS_AVAILABLE:
                                get_metrics_collector().increment(MetricNames.MULTI_TOOL_STEP_SUCCESS, {"step": step.step_id, "reused": "true"})
                            return  # Skip actual execution
                    except Exception as e:
                        logger.debug(f"[ORCHESTRATOR] FIX #4: Could not compare args for dedup: {e}")
            
            # Transform previous result if transformer provided
            if step.result_transformer:
                try:
                    step.args = step.result_transformer(step.args, prev_result)
                except Exception as e:
                    logger.warning(f"[ORCHESTRATOR] Result transformer failed: {e}")
        
        # Check condition
        if step.condition:
            try:
                if not step.condition(plan.intermediate_results):
                    step.status = ExecutionStatus.SKIPPED
                    logger.info(f"[ORCHESTRATOR] Skipping step {step.step_id}: condition not met")
                    if METRICS_AVAILABLE:
                        get_metrics_collector().increment(MetricNames.MULTI_TOOL_STEP_SKIPPED, {"step": step.step_id})
                    return
            except Exception as e:
                logger.warning(f"[ORCHESTRATOR] Condition check failed: {e}")
        
        # ═══════════════════════════════════════════════════════════════════════════
        # IDENTITY RESOLUTION: Resolve person names to ADO identity emails
        # This ensures assignedTo, createdBy, etc. use proper email format
        # Uses generic _is_person_arg() function to detect person-related arguments
        # dynamically - not limited to any specific hardcoded list of arg names.
        # ═══════════════════════════════════════════════════════════════════════════
        if IDENTITY_RESOLUTION_AVAILABLE and step.args:
            # Dynamically detect which args are person-related using pattern matching
            person_args_to_resolve = [
                arg_name for arg_name in step.args.keys() 
                if _is_person_arg(arg_name) and step.args.get(arg_name)
            ]
            
            if person_args_to_resolve:
                logger.debug(
                    f"[ORCHESTRATOR] Identity: Detected {len(person_args_to_resolve)} person args "
                    f"for step {step.step_id}: {person_args_to_resolve}"
                )
            
            for person_arg in person_args_to_resolve:
                person_value = step.args[person_arg]
                
                # Handle array values
                if isinstance(person_value, list):
                    resolved_values = []
                    for pv in person_value:
                        resolved_pv = await self._resolve_identity_for_step(
                            pv, person_arg, step
                        )
                        resolved_values.append(resolved_pv)
                    step.args[person_arg] = resolved_values
                else:
                    # Single value
                    resolved_value = await self._resolve_identity_for_step(
                        person_value, person_arg, step
                    )
                    step.args[person_arg] = resolved_value
        
        # Execute with retries
        while step.retry_count <= step.max_retries:
            try:
                # Build plan dict for tool executor
                tool_plan = {
                    "action": "call_tool",
                    "tool": step.tool_name,
                    "args": dict(step.args or {}),
                    "confidence": 0.9
                }

                # Inject areaPath from plan.context when available and the tool is iteration-scoped
                area_path = plan.context.get('areaPath') if isinstance(plan.context, dict) else None
                if area_path and 'areaPath' not in tool_plan['args'] and step.tool_name in ("wit_get_work_items_for_iteration", "work_get_iteration_work_items"):
                    tool_plan['args']['areaPath'] = area_path

                # FIX: Inject team from plan.context when available and the tool requires it
                # wit_get_work_items_for_iteration API requires team parameter to function properly
                team = plan.context.get('team') if isinstance(plan.context, dict) else None
                if team and 'team' not in tool_plan['args'] and step.tool_name in ("wit_get_work_items_for_iteration", "work_get_iteration_work_items"):
                    tool_plan['args']['team'] = team
                    logger.info(f"[ORCHESTRATOR] Injected team='{team}' into {step.tool_name} args")

                # FIX #10: Inject wikiIdentifier for wiki tools when missing
                # Wiki tools need a wiki identifier - if not provided, auto-fetch the first wiki
                wiki_tools = ("wiki_list_pages", "wiki_get_page", "wiki_get_wiki", "wiki_get_page_content")
                if step.tool_name in wiki_tools and 'wikiIdentifier' not in tool_plan['args']:
                    project = tool_plan['args'].get('project')
                    if project:
                        try:
                            # Get first wiki from project - call wiki_list_wikis
                            wiki_list_raw = await self.mcp_connector.call_tool("wiki_list_wikis", {"project": project})
                            # call_tool returns a JSON string, parse it
                            wiki_list_result = wiki_list_raw
                            if isinstance(wiki_list_raw, str):
                                try:
                                    wiki_list_result = json.loads(wiki_list_raw)
                                except (json.JSONDecodeError, TypeError):
                                    wiki_list_result = None
                            # wiki_list_result can be a list of wikis or a dict with items
                            wikis = []
                            if isinstance(wiki_list_result, list):
                                wikis = wiki_list_result
                            elif isinstance(wiki_list_result, dict):
                                wikis = wiki_list_result.get('value', wiki_list_result.get('items', []))
                            
                            if wikis and len(wikis) > 0:
                                first_wiki = wikis[0]
                                wiki_id = first_wiki.get('id') or first_wiki.get('identifier') or first_wiki.get('name')
                                if wiki_id:
                                    tool_plan['args']['wikiIdentifier'] = wiki_id
                                    logger.info(f"[ORCHESTRATOR] Auto-injected wikiIdentifier='{wiki_id}' for {step.tool_name}")
                                else:
                                    logger.warning(f"[ORCHESTRATOR] Could not extract wiki ID from result: {first_wiki}")
                            else:
                                logger.warning(f"[ORCHESTRATOR] wiki_list_wikis returned no wikis for project {project}")
                        except Exception as e:
                            logger.warning(f"[ORCHESTRATOR] Could not auto-fetch wiki for {step.tool_name}: {e}")

                # FIX #28: Pre-inject project from plan context if missing from args
                # This MUST run BEFORE FIX #27 (repository injection) because FIX #27 needs project to fetch repos.
                # The ToolExecutor._inject_context also does this, but it runs AFTER this enrichment block.
                if 'project' not in tool_plan['args']:
                    ctx_project = plan.context.get('project') if isinstance(plan.context, dict) else None
                    if ctx_project:
                        tool_plan['args']['project'] = ctx_project
                        step.args['project'] = ctx_project
                        logger.info(f"[ORCHESTRATOR] FIX #28: Pre-injected project='{ctx_project}' into {step.tool_name} args")

                # FIX #30: For repo tools that need repositoryId (GUID), resolve name→GUID if needed.
                # Many tools like repo_list_branches_by_repo require repositoryId as a GUID, but the
                # planner often provides a repo NAME. Also, these tools have additionalProperties:false
                # so any extra params (like 'project') are stripped by MCP server.
                if step.tool_name.startswith(('repo_', 'advsec_')):
                    tool_schema_props = {}
                    if hasattr(self.mcp_connector, 'tools_cache'):
                        td = self.mcp_connector.tools_cache.get(step.tool_name, {})
                        tool_schema_props = td.get('inputSchema', {}).get('properties', {})
                    
                    # Check if tool needs repositoryId but NOT project (schema-level)
                    tool_needs_repo_id = 'repositoryId' in tool_schema_props
                    tool_needs_repo_name = 'repository' in tool_schema_props
                    tool_accepts_project = 'project' in tool_schema_props
                    
                    # Helper: fetch repo list (FIX #32: uses instance-level cache + catches BaseException)
                    _fix30_repos = None
                    async def _get_repos_for_fix30():
                        nonlocal _fix30_repos
                        if _fix30_repos is not None:
                            return _fix30_repos
                        project = tool_plan['args'].get('project')
                        if not project:
                            ctx_project = plan.context.get('project') if isinstance(plan.context, dict) else None
                            project = ctx_project or self.config.get('ado_project', 'FracPro-OPS')
                        # FIX #32: Check instance-level cache first to avoid repeated MCP calls
                        cache_key = str(project).lower()
                        if cache_key in self._fix30_repo_cache:
                            _fix30_repos = self._fix30_repo_cache[cache_key]
                            logger.info(f"[ORCHESTRATOR] FIX #32: Using cached repo list for project '{project}' ({len(_fix30_repos)} repos)")
                            return _fix30_repos
                        try:
                            repo_list_raw = await asyncio.wait_for(
                                self.mcp_connector.call_tool("repo_list_repos_by_project", {"project": project}),
                                timeout=60.0  # FIX #32: Explicit timeout shorter than MCP's 120s
                            )
                            repos = []
                            if isinstance(repo_list_raw, str):
                                try:
                                    parsed = json.loads(repo_list_raw)
                                    if isinstance(parsed, list):
                                        repos = parsed
                                    elif isinstance(parsed, dict):
                                        repos = parsed.get('value', parsed.get('items', []))
                                except (json.JSONDecodeError, TypeError):
                                    pass
                            elif isinstance(repo_list_raw, list):
                                repos = repo_list_raw
                            elif isinstance(repo_list_raw, dict):
                                repos = repo_list_raw.get('value', repo_list_raw.get('items', []))
                            _fix30_repos = repos
                            self._fix30_repo_cache[cache_key] = repos  # FIX #32: cache at instance level
                            return repos
                        except (asyncio.CancelledError, asyncio.TimeoutError) as e:
                            # FIX #32: CancelledError is BaseException in Python 3.9+, must catch explicitly
                            logger.warning(f"[ORCHESTRATOR] FIX #32: Repo fetch cancelled/timed out for {project}: {type(e).__name__}")
                            _fix30_repos = []
                            self._fix30_repo_cache[cache_key] = []
                            return []
                        except Exception as e:
                            logger.warning(f"[ORCHESTRATOR] FIX #30: Could not fetch repos: {e}")
                            _fix30_repos = []
                            return []
                    
                    def _choose_repo(repos, hint_name, project):
                        """Choose best matching repo from list."""
                        chosen = repos[0] if repos else None
                        if hint_name and repos:
                            for r in repos:
                                if r.get('name', '').lower() == str(hint_name).lower():
                                    return r
                        if project and repos:
                            for r in repos:
                                if r.get('name', '').lower() == project.lower():
                                    return r
                        return chosen
                    
                    import re as _re
                    
                    # 30a: Resolve repositoryId (name → GUID) for tools that need it
                    if tool_needs_repo_id:
                        current_repo_id = tool_plan['args'].get('repositoryId', '')
                        is_guid = bool(_re.match(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$', str(current_repo_id)))
                        
                        if not is_guid:
                            project = tool_plan['args'].get('project')
                            if not project:
                                ctx_project = plan.context.get('project') if isinstance(plan.context, dict) else None
                                project = ctx_project or self.config.get('ado_project', 'FracPro-OPS')
                            
                            logger.info(f"[ORCHESTRATOR] FIX #30a: repositoryId='{current_repo_id}' is not a GUID for {step.tool_name}, resolving via repo list")
                            repos = await _get_repos_for_fix30()
                            if repos:
                                chosen = _choose_repo(repos, current_repo_id, project)
                                if chosen:
                                    resolved_id = chosen.get('id', '')
                                    tool_plan['args']['repositoryId'] = resolved_id
                                    step.args['repositoryId'] = resolved_id
                                    logger.info(f"[ORCHESTRATOR] FIX #30a: Resolved repositoryId to '{resolved_id}' (repo='{chosen.get('name', '')}') for {step.tool_name}")
                    
                    # 30b: Validate/resolve 'repository' param (must be valid repo name or ID)
                    if tool_needs_repo_name:
                        current_repo = tool_plan['args'].get('repository', '')
                        # Check if repo value looks suspicious (empty, equals project name, or is a placeholder)
                        project = tool_plan['args'].get('project')
                        if not project:
                            ctx_project = plan.context.get('project') if isinstance(plan.context, dict) else None
                            project = ctx_project or self.config.get('ado_project', 'FracPro-OPS')
                        
                        logger.info(f"[FIX #30b] tool={step.tool_name}, repository='{current_repo}', project='{project}'")
                        
                        # Validate: fetch actual repo list and check if current_repo matches any real repo
                        repos = await _get_repos_for_fix30()
                        repo_names = [r.get('name', '').lower() for r in repos] if repos else []
                        repo_exists = str(current_repo).lower() in repo_names if current_repo else False
                        
                        needs_resolution = (
                            not current_repo  # empty
                            or not repo_exists  # doesn't match any actual repo
                            or '<' in current_repo  # placeholder like <repository>
                        )
                        logger.info(f"[FIX #30b] repo_exists={repo_exists}, needs_resolution={needs_resolution}, available_repos={repo_names[:5]}")
                        if needs_resolution:
                            logger.info(f"[ORCHESTRATOR] FIX #30b: repository='{current_repo}' needs resolution for {step.tool_name}")
                            repos = await _get_repos_for_fix30()
                            if repos:
                                chosen = _choose_repo(repos, current_repo, project)
                                if chosen:
                                    resolved_name = chosen.get('name', '')
                                    tool_plan['args']['repository'] = resolved_name
                                    step.args['repository'] = resolved_name
                                    logger.info(f"[ORCHESTRATOR] FIX #30b: Resolved repository to '{resolved_name}' for {step.tool_name}")
                    
                    # 30c: For repo_search_commits, ensure top is reasonable (default is only 10)
                    if step.tool_name == 'repo_search_commits':
                        if 'top' not in tool_plan['args'] or tool_plan['args'].get('top', 0) < 20:
                            tool_plan['args']['top'] = 50
                            step.args['top'] = 50
                            logger.info(f"[ORCHESTRATOR] FIX #30c: Set top=50 for repo_search_commits")
                    
                    # 30d: Clean up params not accepted by this tool (additionalProperties:false)
                    if not tool_accepts_project and 'project' in tool_plan['args']:
                        removed_project = tool_plan['args'].pop('project', None)
                        step.args.pop('project', None)
                        logger.info(f"[ORCHESTRATOR] FIX #30d: Removed 'project' (not in schema) from {step.tool_name} args")

                # FIX #27: Inject repository/repositoryId/repositoryName for repo and advsec tools when missing
                # Many repo_ and advsec_ tools require a repository identifier but planners often omit it
                repo_param_names = ('repository', 'repositoryId', 'repositoryName')
                needs_repo = step.tool_name.startswith(('repo_', 'advsec_'))
                has_repo = any(rp in tool_plan['args'] for rp in repo_param_names)
                if needs_repo and not has_repo:
                    project = tool_plan['args'].get('project')
                    if project:
                        try:
                            # FIX #32: Use instance-level repo cache instead of fresh MCP call
                            cache_key_27 = str(project).lower()
                            if cache_key_27 in self._fix30_repo_cache:
                                repos_27 = self._fix30_repo_cache[cache_key_27]
                                logger.info(f"[ORCHESTRATOR] FIX #27+32: Using cached repo list ({len(repos_27)} repos)")
                            else:
                                repo_list_raw = await asyncio.wait_for(
                                    self.mcp_connector.call_tool("repo_list_repos_by_project", {"project": project}),
                                    timeout=60.0
                                )
                                repo_list_result = repo_list_raw
                                if isinstance(repo_list_raw, str):
                                    try:
                                        repo_list_result = json.loads(repo_list_raw)
                                    except (json.JSONDecodeError, TypeError):
                                        repo_list_result = None
                                repos_27 = []
                                if isinstance(repo_list_result, list):
                                    repos_27 = repo_list_result
                                elif isinstance(repo_list_result, dict):
                                    repos_27 = repo_list_result.get('value', repo_list_result.get('items', []))
                                self._fix30_repo_cache[cache_key_27] = repos_27
                            
                            if repos_27 and len(repos_27) > 0:
                                # Use the first repository (or one matching the project name)
                                chosen_repo = repos_27[0]
                                for r in repos_27:
                                    rname = r.get('name', '')
                                    if rname.lower() == project.lower():
                                        chosen_repo = r
                                        break
                                repo_name = chosen_repo.get('name', '')
                                repo_id = chosen_repo.get('id', '')
                                
                                # Determine which param the tool needs from schema
                                tool_schema = {}
                                if hasattr(self.mcp_connector, 'tools_cache'):
                                    tool_def = self.mcp_connector.tools_cache.get(step.tool_name, {})
                                    tool_schema = tool_def.get('inputSchema', {}).get('properties', {})
                                
                                if 'repositoryName' in tool_schema:
                                    tool_plan['args']['repositoryName'] = repo_name
                                    logger.info(f"[ORCHESTRATOR] Auto-injected repositoryName='{repo_name}' for {step.tool_name}")
                                if 'repositoryId' in tool_schema and 'repositoryId' not in tool_plan['args']:
                                    tool_plan['args']['repositoryId'] = repo_id
                                    logger.info(f"[ORCHESTRATOR] Auto-injected repositoryId='{repo_id}' for {step.tool_name}")
                                if 'repository' in tool_schema and 'repository' not in tool_plan['args']:
                                    tool_plan['args']['repository'] = repo_name
                                    logger.info(f"[ORCHESTRATOR] Auto-injected repository='{repo_name}' for {step.tool_name}")
                            else:
                                logger.warning(f"[ORCHESTRATOR] repo_list_repos_by_project returned no repos for project {project}")
                        except (asyncio.CancelledError, asyncio.TimeoutError) as e:
                            # FIX #32: Catch CancelledError (BaseException in Python 3.9+)
                            logger.warning(f"[ORCHESTRATOR] FIX #32: FIX #27 repo fetch cancelled/timed out: {type(e).__name__}")
                        except Exception as e:
                            logger.warning(f"[ORCHESTRATOR] Could not auto-fetch repository for {step.tool_name}: {e}")

                # FIX #11: Fix parameter type conversions for specific tools
                # search_code needs project as array, not string
                if step.tool_name == "search_code" and 'project' in tool_plan['args']:
                    if isinstance(tool_plan['args']['project'], str):
                        tool_plan['args']['project'] = [tool_plan['args']['project']]
                        logger.info(f"[ORCHESTRATOR] Converted search_code project to array format")

                # wit_get_query_results_by_id needs id as string, not number
                if step.tool_name == "wit_get_query_results_by_id" and 'id' in tool_plan['args']:
                    tool_plan['args']['id'] = str(tool_plan['args']['id'])
                    logger.info(f"[ORCHESTRATOR] Converted wit_get_query_results_by_id id to string format")

                # FIX #18: wit_get_work_items_batch_by_ids needs ids as array of strings
                # This is especially important when IDs are extracted from previous step results
                if step.tool_name == "wit_get_work_items_batch_by_ids" and 'ids' in tool_plan['args']:
                    ids_value = tool_plan['args']['ids']
                    if isinstance(ids_value, str):
                        # Single ID string - convert to array
                        tool_plan['args']['ids'] = [ids_value]
                        logger.info(f"[ORCHESTRATOR] FIX #18: Converted wit_get_work_items_batch_by_ids ids from string to array")
                    elif isinstance(ids_value, (int, float)):
                        # Number - convert to array of string
                        tool_plan['args']['ids'] = [str(ids_value)]
                        logger.info(f"[ORCHESTRATOR] FIX #18: Converted wit_get_work_items_batch_by_ids ids from number to array of strings")
                    elif isinstance(ids_value, list):
                        # Already a list - ensure all elements are strings
                        tool_plan['args']['ids'] = [str(id_val) for id_val in ids_value]
                        logger.info(f"[ORCHESTRATOR] FIX #18: Ensured wit_get_work_items_batch_by_ids ids array contains only strings")


                # advsec_get_alerts needs confidenceLevels with proper casing (High, Other)
                # MCP server ONLY accepts ["High", "Other"] — reject everything else
                if step.tool_name == "advsec_get_alerts" and 'confidenceLevels' in tool_plan['args']:
                    levels = tool_plan['args']['confidenceLevels']
                    if isinstance(levels, list):
                        # VALID MCP enum values for confidenceLevels
                        VALID_CONFIDENCE_LEVELS = {"high", "other"}
                        normalized_levels = []
                        dropped_levels = []
                        for level in levels:
                            level_str = str(level).lower()
                            if level_str in VALID_CONFIDENCE_LEVELS:
                                normalized_levels.append(level_str.capitalize())
                            else:
                                dropped_levels.append(str(level))
                        if dropped_levels:
                            logger.info(f"[ORCHESTRATOR] Dropped invalid confidenceLevels: {dropped_levels} (MCP only accepts High, Other)")
                        # Ensure we have at least one valid level; default to both if all were invalid
                        if not normalized_levels:
                            normalized_levels = ["High", "Other"]
                            logger.info(f"[ORCHESTRATOR] All confidenceLevels were invalid, defaulting to {normalized_levels}")
                        tool_plan['args']['confidenceLevels'] = normalized_levels
                        logger.info(f"[ORCHESTRATOR] Normalized advsec_get_alerts confidenceLevels to {normalized_levels}")

                # DIAGNOSTIC LOGGING FOR FIX #3 & #4
                logger.info(f"[ORCHESTRATOR] ═══ Executing Tool: {step.tool_name} ═══")
                logger.info(f"[ORCHESTRATOR] Step ID: {step.step_id}")
                logger.info(f"[ORCHESTRATOR] Tool Args (BEFORE normalization): {json.dumps(tool_plan['args'], indent=2, default=str)}")
                
                # FIX #21 & #23: DYNAMIC ENUM AND TYPE NORMALIZATION
                # Normalize enum arguments: convert string statuses to numeric enums that MCP expects
                # This handles cases like statusFilter: "failed" → 8, statusFilter: "completed" → 2
                # Also converts types: buildId: "24098" (string) → 24098 (number) using FIX #23
                # Uses the NUMERIC_ENUM_MAPPINGS and inputSchema from tool_registry.py for all tools
                tool_schema = MCP_TOOL_REGISTRY.get(step.tool_name, {})
                normalized_args, normalization_log = normalize_tool_args(step.tool_name, tool_plan['args'], tool_schema=tool_schema)
                if normalization_log:
                    logger.info(f"[ORCHESTRATOR] FIX #21 & #23: Normalized arguments for {step.tool_name}:")
                    for arg_name, reason in normalization_log.items():
                        old_val = tool_plan['args'].get(arg_name)
                        new_val = normalized_args.get(arg_name)
                        logger.info(f"  {arg_name}: {old_val} → {new_val} ({reason})")
                    tool_plan['args'] = normalized_args
                
                logger.info(f"[ORCHESTRATOR] Tool Args (AFTER normalization): {json.dumps(tool_plan['args'], indent=2, default=str)}")
                
                # ══════════════════════════════════════════════════════════════
                # GENERIC FAN-OUT: When placeholder resolution produces a list
                # for a scalar parameter (number/integer/string), fan-out into
                # multiple individual MCP calls and aggregate the results.
                # This handles workItemId, pipelineId, buildId, alertId, etc.
                # ══════════════════════════════════════════════════════════════
                result = None
                fanout_arg_name = None
                fanout_arg_values = None
                
                if tool_schema:
                    properties = tool_schema.get('inputSchema', {}).get('properties', {})
                    if isinstance(properties, dict):
                        # FIRST PASS: Unwrap single-element lists to scalar (no fan-out needed)
                        for arg_name, arg_value in list(tool_plan['args'].items()):
                            if isinstance(arg_value, list) and len(arg_value) == 1 and arg_name in properties:
                                arg_schema = properties[arg_name]
                                expected_type = arg_schema.get('type') if isinstance(arg_schema, dict) else None
                                if expected_type in ("number", "integer", "string"):
                                    try:
                                        if expected_type == "integer":
                                            tool_plan['args'][arg_name] = int(arg_value[0])
                                        elif expected_type == "number":
                                            tool_plan['args'][arg_name] = float(arg_value[0])
                                        else:
                                            tool_plan['args'][arg_name] = str(arg_value[0])
                                    except Exception:
                                        tool_plan['args'][arg_name] = arg_value[0]
                                    logger.info(f"[ORCHESTRATOR] Unwrapped single-element list for {arg_name}: {arg_value} -> {tool_plan['args'][arg_name]}")
                        
                        # SECOND PASS: Detect multi-element list → scalar mismatch for fan-out
                        for arg_name, arg_value in tool_plan['args'].items():
                            if isinstance(arg_value, list) and arg_name in properties:
                                arg_schema = properties[arg_name]
                                expected_type = arg_schema.get('type') if isinstance(arg_schema, dict) else None
                                if expected_type in ("number", "integer", "string"):
                                    fanout_arg_name = arg_name
                                    fanout_arg_values = arg_value
                                    logger.info(f"[ORCHESTRATOR] Fan-out detected: {arg_name} has list of {len(arg_value)} values but schema expects {expected_type}")
                                    break

                if fanout_arg_name and fanout_arg_values:
                    # ══════════════════════════════════════════════════════════════
                    # SMART FAN-OUT LIMITING: Prevent timeout on large iteration lists
                    # For iteration-related fan-outs (iterationId), limit to the N
                    # most recent iterations. Extract N from query ("last 3 sprints")
                    # or default to MAX_FANOUT_CALLS.
                    # ══════════════════════════════════════════════════════════════
                    MAX_FANOUT_CALLS = 10  # Global safety cap
                    ITERATION_FANOUT_DEFAULT = 5  # Default for iteration fan-outs
                    
                    is_iteration_fanout = fanout_arg_name.lower() in ("iterationid", "iterationpath", "sprintid")
                    
                    if is_iteration_fanout and len(fanout_arg_values) > ITERATION_FANOUT_DEFAULT:
                        # Try to extract "last N" from the original query
                        last_n_match = re.search(r'(?:last|past|previous|recent)\s+(\d+)\s+(?:sprint|iteration)', plan.query or "", re.IGNORECASE)
                        fanout_limit = int(last_n_match.group(1)) if last_n_match else ITERATION_FANOUT_DEFAULT
                        fanout_limit = min(fanout_limit, MAX_FANOUT_CALLS)
                        
                        original_count = len(fanout_arg_values)
                        # Take the LAST N values (most recent iterations are typically at the end)
                        fanout_arg_values = fanout_arg_values[-fanout_limit:]
                        logger.info(
                            f"[ORCHESTRATOR] Smart fan-out limit: {fanout_arg_name} reduced from "
                            f"{original_count} → {len(fanout_arg_values)} (limit={fanout_limit}, "
                            f"query={'last '+last_n_match.group(1) if last_n_match else 'default'})"
                        )
                    elif len(fanout_arg_values) > MAX_FANOUT_CALLS:
                        original_count = len(fanout_arg_values)
                        fanout_arg_values = fanout_arg_values[:MAX_FANOUT_CALLS]
                        logger.warning(
                            f"[ORCHESTRATOR] Fan-out safety cap: {fanout_arg_name} truncated from "
                            f"{original_count} → {MAX_FANOUT_CALLS}"
                        )
                    
                    aggregated_items: List[Any] = []
                    aggregated_errors: List[str] = []
                    arg_schema = properties[fanout_arg_name]
                    expected_type = arg_schema.get('type', 'string')
                    
                    for single_val in fanout_arg_values:
                        single_args = dict(tool_plan['args'])
                        try:
                            if expected_type == "integer":
                                single_args[fanout_arg_name] = int(single_val)
                            elif expected_type == "number":
                                single_args[fanout_arg_name] = float(single_val)
                            else:
                                single_args[fanout_arg_name] = str(single_val)
                        except Exception:
                            single_args[fanout_arg_name] = single_val

                        single_plan = dict(tool_plan)
                        single_plan['args'] = single_args

                        single_result = await self.tool_executor.execute(single_plan, context=plan.context)
                        if single_result.get('success', False):
                            items = single_result.get('items') or single_result.get('value') or []
                            if isinstance(items, list):
                                for it in items:
                                    if isinstance(it, dict) and fanout_arg_name not in it:
                                        it[fanout_arg_name] = single_args[fanout_arg_name]
                            aggregated_items.extend(items if isinstance(items, list) else [items])
                        else:
                            aggregated_errors.append(single_result.get('error') or f"Error for {fanout_arg_name}={single_val}")

                    result = {
                        "tool": step.tool_name,
                        "success": len(aggregated_errors) == 0 or len(aggregated_items) > 0,
                        "count": len(aggregated_items),
                        "items": aggregated_items,
                    }
                    if aggregated_errors:
                        result["errors"] = aggregated_errors
                        result["failed_values"] = [str(v) for v in fanout_arg_values if str(v) not in [str(i.get(fanout_arg_name, '')) for i in aggregated_items]]
                    logger.info(f"[ORCHESTRATOR] Fan-out executed {len(fanout_arg_values)} calls for {step.tool_name} ({fanout_arg_name} list)")

                if result is None:
                    # ═══════════════════════════════════════════════════════════════
                    # LOCAL SKILL HANDLER: execute_wiql
                    # execute_wiql is a LOCAL Python skill (not an MCP tool).
                    # It calls the ADO REST API directly for WIQL execution.
                    # Must be intercepted here before tool_executor sends to MCP.
                    # ═══════════════════════════════════════════════════════════════
                    if step.tool_name == "execute_wiql":
                        try:
                            from agents.pm_agent.pm_skills.wiql_skill import execute_wiql as _exec_wiql, validate_wiql as _validate_wiql
                            wiql_args = tool_plan.get('args', {})
                            wiql_project = wiql_args.get('project') or (plan.context or {}).get('project', 'FracPro-OPS')
                            wiql_query = wiql_args.get('wiql', '')
                            wiql_top = wiql_args.get('top', 1000)
                            
                            if not wiql_query:
                                logger.warning(f"[ORCHESTRATOR] execute_wiql step {step.step_id}: No WIQL query provided")
                                result = {
                                    "tool": "execute_wiql",
                                    "success": False,
                                    "error": "No WIQL query provided by planner",
                                    "count": 0,
                                    "items": []
                                }
                            else:
                                # ═══════════════════════════════════════════════════════
                                # PRE-VALIDATION: Check for non-existent iteration date
                                # fields BEFORE executing. If found, auto-reroute to
                                # work_list_team_iterations (the correct tool for
                                # iteration date queries).
                                # ═══════════════════════════════════════════════════════
                                pre_validation = _validate_wiql(wiql_query)
                                iteration_date_error = (
                                    not pre_validation["valid"] and 
                                    pre_validation.get("error", "").find("work_list_team_iterations") != -1
                                )
                                
                                if iteration_date_error:
                                    logger.warning(f"[ORCHESTRATOR] execute_wiql step {step.step_id}: WIQL uses non-existent iteration date fields, auto-rerouting to work_list_team_iterations")
                                    # Extract team and project from WIQL args or plan context
                                    reroute_project = wiql_project
                                    reroute_team = (plan.context or {}).get('team', '')
                                    
                                    if reroute_team:
                                        logger.info(f"[ORCHESTRATOR] Auto-reroute: work_list_team_iterations(project={reroute_project}, team={reroute_team})")
                                        reroute_raw = await self.mcp_connector.call_tool(
                                            "work_list_team_iterations",
                                            {"project": reroute_project, "team": reroute_team}
                                        )
                                    else:
                                        logger.info(f"[ORCHESTRATOR] Auto-reroute: work_list_team_iterations(project={reroute_project})")
                                        reroute_raw = await self.mcp_connector.call_tool(
                                            "work_list_team_iterations",
                                            {"project": reroute_project}
                                        )
                                    
                                    # Parse the rerouted result
                                    try:
                                        if isinstance(reroute_raw, str):
                                            reroute_parsed = json.loads(reroute_raw)
                                        else:
                                            reroute_parsed = reroute_raw
                                    except (json.JSONDecodeError, TypeError):
                                        reroute_parsed = []
                                    
                                    if isinstance(reroute_parsed, list) and len(reroute_parsed) > 0:
                                        result = {
                                            "tool": "work_list_team_iterations",
                                            "success": True,
                                            "count": len(reroute_parsed),
                                            "items": reroute_parsed,
                                            "warnings": [f"Auto-rerouted from execute_wiql to work_list_team_iterations: {pre_validation['error']}"],
                                            "_rerouted": True
                                        }
                                        # Update the step's tool name so synthesis knows the data type
                                        step.tool_name = "work_list_team_iterations"
                                        logger.info(f"[ORCHESTRATOR] execute_wiql step {step.step_id}: ✅ Auto-rerouted to work_list_team_iterations - {len(reroute_parsed)} iterations")
                                    else:
                                        result = {
                                            "tool": "work_list_team_iterations",
                                            "success": False,
                                            "error": "Auto-rerouted to work_list_team_iterations but got no results",
                                            "count": 0,
                                            "items": []
                                        }
                                else:
                                    # ═══════════════════════════════════════════════════════
                                    # AREA PATH INJECTION: If the query resolved a team/area
                                    # path (via the pre-existing extraction logic in
                                    # light_planner._extract_entities_from_query) but the
                                    # LLM-generated WIQL omits [System.AreaPath], inject it.
                                    # This mirrors the areaPath injection done for
                                    # wit_get_work_items_for_iteration at line ~3004.
                                    # ═══════════════════════════════════════════════════════
                                    _ctx = plan.context if isinstance(plan.context, dict) else {}
                                    _resolved_ap = _ctx.get('resolved_area_path') or _ctx.get('areaPath')
                                    if _resolved_ap and '[System.AreaPath]' not in wiql_query and not _ctx.get('_team_defaulted') and not _ctx.get('_multi_team_query'):
                                        # Insert area path clause before ORDER BY (or at end of WHERE)
                                        _ap_clause = f" AND [System.AreaPath] UNDER '{_resolved_ap}'"
                                        if 'ORDER BY' in wiql_query.upper():
                                            _order_idx = wiql_query.upper().index('ORDER BY')
                                            wiql_query = wiql_query[:_order_idx].rstrip() + _ap_clause + ' ' + wiql_query[_order_idx:]
                                        else:
                                            wiql_query = wiql_query + _ap_clause
                                        logger.info(f"[ORCHESTRATOR] execute_wiql: Injected area path filter: {_resolved_ap}")
                                    
                                    logger.info(f"[ORCHESTRATOR] execute_wiql step {step.step_id}: Executing WIQL directly (local skill, NOT MCP)")
                                    logger.info(f"[ORCHESTRATOR] execute_wiql project={wiql_project}, top={wiql_top}")
                                    logger.info(f"[ORCHESTRATOR] execute_wiql WIQL: {wiql_query[:200]}...")
                                    
                                    wiql_result = await _exec_wiql(project=wiql_project, wiql=wiql_query, top=wiql_top)
                                    
                                    if wiql_result.get('success'):
                                        result = {
                                            "tool": "execute_wiql",
                                            "success": True,
                                            "count": wiql_result.get('count', 0),
                                            "items": wiql_result.get('items', []),
                                            "query": wiql_result.get('query', wiql_query),
                                            "warnings": wiql_result.get('warnings', [])
                                        }
                                        logger.info(f"[ORCHESTRATOR] execute_wiql step {step.step_id}: ✅ Success - {result['count']} work items")
                                    else:
                                        # ═══════════════════════════════════════════════
                                        # SMART FALLBACK: If WIQL fails with TF51005
                                        # (non-existent field) or TF51011 (bad iteration
                                        # path) and the query is about iterations, reroute
                                        # to work_list_team_iterations
                                        # ═══════════════════════════════════════════════
                                        wiql_error = wiql_result.get('error', '')
                                        is_iteration_field_error = (
                                            'TF51005' in wiql_error and 'Iteration' in wiql_error
                                        ) or (
                                            'TF51011' in wiql_error  # Bad iteration path
                                        )
                                        
                                        if is_iteration_field_error:
                                            logger.warning(f"[ORCHESTRATOR] execute_wiql step {step.step_id}: Iteration-related WIQL error, attempting auto-reroute to work_list_team_iterations")
                                            reroute_team = (plan.context or {}).get('team', '')
                                            try:
                                                reroute_args = {"project": wiql_project}
                                                if reroute_team:
                                                    reroute_args["team"] = reroute_team
                                                reroute_raw = await self.mcp_connector.call_tool("work_list_team_iterations", reroute_args)
                                                reroute_parsed = json.loads(reroute_raw) if isinstance(reroute_raw, str) else reroute_raw
                                                if isinstance(reroute_parsed, list) and len(reroute_parsed) > 0:
                                                    result = {
                                                        "tool": "work_list_team_iterations",
                                                        "success": True,
                                                        "count": len(reroute_parsed),
                                                        "items": reroute_parsed,
                                                        "warnings": [f"Auto-rerouted from execute_wiql: {wiql_error}"],
                                                        "_rerouted": True
                                                    }
                                                    step.tool_name = "work_list_team_iterations"
                                                    logger.info(f"[ORCHESTRATOR] execute_wiql step {step.step_id}: ✅ Fallback rerouted to work_list_team_iterations - {len(reroute_parsed)} iterations")
                                                else:
                                                    result = {
                                                        "tool": "execute_wiql",
                                                        "success": False,
                                                        "error": wiql_error,
                                                        "count": 0,
                                                        "items": []
                                                    }
                                            except Exception as reroute_err:
                                                logger.warning(f"[ORCHESTRATOR] Fallback reroute failed: {reroute_err}")
                                                result = {
                                                    "tool": "execute_wiql",
                                                    "success": False,
                                                    "error": wiql_error,
                                                    "count": 0,
                                                    "items": []
                                                }
                                        else:
                                            result = {
                                                "tool": "execute_wiql",
                                                "success": False,
                                                "error": wiql_result.get('error', 'WIQL execution failed'),
                                                "count": 0,
                                                "items": [],
                                                "query": wiql_result.get('query', wiql_query),
                                                "warnings": wiql_result.get('warnings', [])
                                            }
                                            logger.warning(f"[ORCHESTRATOR] execute_wiql step {step.step_id}: ❌ Failed - {result['error']}")
                        except Exception as wiql_err:
                            logger.exception(f"[ORCHESTRATOR] execute_wiql step {step.step_id}: Exception - {wiql_err}")
                            result = {
                                "tool": "execute_wiql",
                                "success": False,
                                "error": f"WIQL execution error: {wiql_err}",
                                "count": 0,
                                "items": []
                            }
                    else:
                        # FIX: Pass context to tool_executor.execute() so auto_inject_missing_args gets the full context
                        # This ensures team, project, and iteration parameters are properly injected
                        result = await self.tool_executor.execute(tool_plan, context=plan.context)
                
                # DIAGNOSTIC LOGGING: Log raw result
                logger.info(f"[ORCHESTRATOR] Tool Result Type: {type(result)}")
                if isinstance(result, dict):
                    success = result.get('success', False)
                    count = result.get('count', 0)
                    items = result.get('items', [])
                    logger.info(f"[ORCHESTRATOR] Result: success={success}, count={count}, items_len={len(items) if isinstance(items, list) else 'N/A'}")
                    
                    # CRITICAL VALIDATION: Warn if success but zero items
                    if success and count == 0:
                        logger.warning(f"[ORCHESTRATOR] ⚠️ Tool returned SUCCESS but 0 items - possible empty result or wrong parameters")
                        logger.warning(f"[ORCHESTRATOR] ⚠️ Verify: iteration={tool_plan['args'].get('iterationId')}, project={tool_plan['args'].get('project')}, team={tool_plan['args'].get('team')}")
                else:
                    logger.info(f"[ORCHESTRATOR] Result preview: {str(result)[:200]}")
                
                if result.get("success", False):
                    step.result = result
                    step.status = ExecutionStatus.SUCCESS
                    plan.intermediate_results[step.step_id] = result
                    
                    # B3: Store step output and extract data to execution context
                    plan.step_outputs[step.step_id] = result
                    if CONTEXT_RESOLVER_AVAILABLE:
                        resolver = get_context_resolver()
                        plan.execution_context = resolver.extract_step_data(
                            step_result=result,
                            step_id=step.step_id,
                            execution_context=plan.execution_context
                        )
                    
                    logger.info(f"[ORCHESTRATOR] Step {step.step_id} succeeded: {result.get('count', 0)} items")
                    if METRICS_AVAILABLE:
                        get_metrics_collector().increment(MetricNames.MULTI_TOOL_STEP_SUCCESS, {"step": step.step_id, "tool": step.tool_name})
                    break
                else:
                    raise Exception(result.get("error", "Unknown error"))
                    
            except Exception as e:
                step.retry_count += 1
                step.error = str(e)
                
                if step.retry_count <= step.max_retries:
                    step.status = ExecutionStatus.RETRYING
                    logger.warning(f"[ORCHESTRATOR] Step {step.step_id} failed, retry {step.retry_count}/{step.max_retries}: {e}")
                    await asyncio.sleep(self.config.retry_delay_seconds)
                else:
                    step.status = ExecutionStatus.FAILED
                    logger.error(f"[ORCHESTRATOR] Step {step.step_id} failed after {step.max_retries} retries: {e}")
                    
                    # Try fallback if enabled
                    if self.config.enable_fallback_strategies:
                        fallback_result = await self._try_fallback(step, plan)
                        if fallback_result:
                            step.result = fallback_result
                            step.status = ExecutionStatus.SUCCESS
                            plan.intermediate_results[step.step_id] = fallback_result
        
        step.execution_time_ms = int((datetime.utcnow() - start_time).total_seconds() * 1000)
        
        # Finalize Langfuse span with final status
        if step_span:
            finalize_span(step_span, output={
                "status": step.status.value,
                "execution_time_ms": step.execution_time_ms,
                "result_count": step.result.get("count", 0) if isinstance(step.result, dict) else None,
                "error": step.error if step.status == ExecutionStatus.FAILED else None
            }, status="success" if step.status == ExecutionStatus.SUCCESS else "error")
    
    async def _try_fallback(self, step: ToolStep, plan: ExecutionPlan) -> Optional[Dict[str, Any]]:
        """Try fallback strategies for a failed step."""
        logger.info(f"[ORCHESTRATOR] Attempting fallback for {step.tool_name}")
        
        # Fallback strategies by tool type
        fallback_map = {
            "wit_get_work_items_for_iteration": self._fallback_wiql_for_iteration,
            "search_workitem": self._fallback_wiql_for_search,
            "pr_list_pull_requests": self._fallback_repo_pr_search,
        }
        
        fallback_fn = fallback_map.get(step.tool_name)
        if fallback_fn:
            try:
                return await fallback_fn(step.args, plan.context)
            except Exception as e:
                logger.warning(f"[ORCHESTRATOR] Fallback failed: {e}")
        
        return None
    
    async def _fallback_wiql_for_iteration(self, args: Dict, context: Dict) -> Optional[Dict]:
        """Fallback: Use WIQL when iteration tool fails. Resolves sprint names to full paths first."""
        project = args.get("project", context.get("project", ""))
        iteration_id = args.get("iterationId", "")
        
        # ── Resolve iteration path: prefer already-resolved iterationPath from args ──
        iteration_path = args.get("iterationPath", "")
        
        if not iteration_path or iteration_path == iteration_id:
            # iterationPath not set or same as iterationId — need to resolve
            iteration_path = iteration_id
            # Check pre-resolved cache first
            cached = context.get("_iteration_cache", {}).get(iteration_id)
            if cached:
                iteration_path = cached.get("iterationPath", iteration_id)
                logger.info(f"[ORCHESTRATOR] Fallback WIQL: Using cached path '{iteration_path}' for '{iteration_id}'")
            elif iteration_id and not iteration_id.startswith("@"):
                # Try to resolve sprint name to full path
                resolved = await self._resolve_iteration_macro(iteration_id, args, context)
                if resolved and resolved.get("iterationPath"):
                    iteration_path = resolved["iterationPath"]
                    logger.info(f"[ORCHESTRATOR] Fallback WIQL: Resolved '{iteration_id}' -> '{iteration_path}'")
        else:
            logger.info(f"[ORCHESTRATOR] Fallback WIQL: Using pre-resolved iterationPath '{iteration_path}'")
        
        # Build WIQL query
        area_clause = ""
        if args.get('areaPath'):
            ap = args.get('areaPath')
            area_clause = f"\nAND [System.AreaPath] UNDER '{ap}'"
        
        # Add unassigned filter if requested
        unassigned_clause = ""
        if args.get('unassigned'):
            unassigned_clause = "\nAND [System.AssignedTo] = ''"
        
        # Add work item type filter if requested
        type_clause = ""
        wit_filter = args.get('workItemType')
        if wit_filter:
            if isinstance(wit_filter, list):
                types_str = "', '".join(wit_filter)
                type_clause = f"\nAND [System.WorkItemType] IN ('{types_str}')"
            elif isinstance(wit_filter, str):
                type_clause = f"\nAND [System.WorkItemType] = '{wit_filter}'"
        
        # Add state filter if requested
        state_clause = ""
        state_filter = args.get('state')
        if state_filter:
            if isinstance(state_filter, list):
                states_str = "', '".join(state_filter)
                state_clause = f"\nAND [System.State] IN ('{states_str}')"
            elif isinstance(state_filter, str):
                state_clause = f"\nAND [System.State] = '{state_filter}'"

        wiql = f"""SELECT [System.Id], [System.Title], [System.State], [System.AssignedTo], [System.WorkItemType], [Microsoft.VSTS.Common.Priority]
FROM WorkItems
WHERE [System.TeamProject] = '{project}'
AND [System.IterationPath] UNDER '{iteration_path}'{area_clause}{unassigned_clause}{type_clause}{state_clause}
ORDER BY [Microsoft.VSTS.Common.Priority] ASC, [System.ChangedDate] DESC"""
        
        try:
            from agents.pm_agent.pm_skills.wiql_skill import execute_wiql
            result = await execute_wiql(project=project, wiql=wiql)
            if result and result.get('success'):
                logger.info(f"[ORCHESTRATOR] Fallback WIQL succeeded: {result.get('count', 0)} items")
                return result
            else:
                logger.warning(f"[ORCHESTRATOR] Fallback WIQL failed: {result.get('error', 'unknown')}")
        except Exception as e:
            logger.warning(f"[ORCHESTRATOR] Fallback WIQL exception: {e}")
        return None
    
    async def _fallback_wiql_for_search(self, args: Dict, context: Dict) -> Optional[Dict]:
        """Fallback: Use WIQL when search fails."""
        project = args.get("project", context.get("project", ""))
        search_text = args.get("searchText", "")
        work_item_types = args.get("workItemType", [])
        
        type_filter = ""
        if work_item_types:
            types_str = "', '".join(work_item_types)
            type_filter = f"AND [System.WorkItemType] IN ('{types_str}')"
        
        wiql = f"""
        SELECT [System.Id], [System.Title], [System.State], [System.AssignedTo]
        FROM WorkItems
        WHERE [System.TeamProject] = '{project}'
        AND [System.Title] CONTAINS '{search_text}'
        {type_filter}
        ORDER BY [System.ChangedDate] DESC
        """
        
        try:
            from agents.pm_agent.pm_skills.wiql_skill import execute_wiql
            result = await execute_wiql(project=project, wiql=wiql)
            if result and result.get('success'):
                return result
        except Exception:
            pass
        return None
    
    async def _fallback_repo_pr_search(self, args: Dict, context: Dict) -> Optional[Dict]:
        """Fallback: Try different repository or status for PR search."""
        # Try with different status
        for status in ["active", "completed", "all"]:
            if status != args.get("status"):
                try:
                    new_args = {**args, "status": status}
                    result = await self.mcp_connector.call_tool("pr_list_pull_requests", new_args)
                    if result:
                        parsed = json.loads(result) if isinstance(result, str) else result
                        items = parsed if isinstance(parsed, list) else parsed.get("value", [])
                        if items:
                            return {"success": True, "items": items, "count": len(items)}
                except Exception:
                    continue
        return None
    
    async def _synthesize_results(self, plan: ExecutionPlan) -> str:
        """
        Synthesize results from all steps into a coherent response using LLM if available.
        
        Uses utilities.llm.synthesizer.synthesize_agent_outputs to provide:
        1. Intelligent synthesis (comparisons, summaries)
        2. Higher item limits (50 vs 10)
        3. Structured presentation
        """
        successful_steps = [s for s in plan.steps if s.status == ExecutionStatus.SUCCESS]
        failed_steps = [s for s in plan.steps if s.status == ExecutionStatus.FAILED]
        
        logger.info(f"[SYNTHESIZER] ═══ SYNTHESIS START ═══")
        logger.info(f"[SYNTHESIZER] Total steps: {len(plan.steps)} (Success: {len(successful_steps)}, Failed: {len(failed_steps)})")
        
        if not successful_steps and not failed_steps:
             return self._handle_plan_failure(plan, "No steps were executed")

        # Convert steps to agent_results format for synthesizer
        agent_results = []
        
        for step in successful_steps:
            # Format content to be friendly for synthesis
            result = step.result
            
            # If result is a dict with items, extracting them might be cleaner, 
            # but synthesize_agent_outputs handles dicts well.
            agent_results.append({
                "agent": "PM Agent",
                "skill": step.tool_name,
                "content": result,
                "_routing": {"skill": step.purpose}  # Hint for synthesizer
            })
            
        for step in failed_steps:
            agent_results.append({
                "agent": "PM Agent",
                "skill": step.tool_name,
                "content": f"❌ Execution Failed: {step.error or 'Unknown error'}",
                "_routing": {"skill": step.purpose}
            })
            
        # Generate synthesis
        try:
            # Derive style guidelines dynamically. Prefer planner-provided analysis_criteria
            # but fall back to an LLM-derived or heuristic guideline when not present.
            analysis_criteria = (plan.context or {}).get("analysis_criteria", {})
            step_context_parts = [f"- Step '{step.step_id}' ({step.tool_name}): {step.purpose}" for step in successful_steps]

            try:
                from utilities.llm.synthesizer import derive_style_guidelines
                style = (await derive_style_guidelines(user_query=plan.query, analysis_criteria=analysis_criteria, step_context=step_context_parts)) or ""
            except Exception:
                # If derive helper fails, use empty style (rely on synthesizer to use analysis_criteria or default behavior)
                style = ""
            
            # Call the centralized synthesizer
            # This handles both LLM synthesis and deterministic fallback (limit 50 items)
            logger.info(f"[SYNTHESIZER] Calling synthesize_agent_outputs with {len(agent_results)} results")
            if style:
                logger.info(f"[SYNTHESIZER] Style guidelines: {style[:300]}")
            
            final_output = await synthesize_agent_outputs(
                agent_results=agent_results,
                user_query=plan.query,
                style_guidelines=style,
                session_id=None # Can be passed if available in context
            )
            
            # Append global failure note if all steps failed (though synthesizer might handle it)
            if not successful_steps:
                final_output += "\n\n⚠️ **Note:** All steps failed to execute."
                
            return final_output
            
        except Exception as e:
            logger.error(f"[SYNTHESIZER] Synthesis failed: {e}")
            return self._synthesize_results_fallback(plan)

    def _synthesize_results_fallback(self, plan: ExecutionPlan) -> str:
        """Legacy deterministic synthesizer (fallback)."""
        successful_steps = [s for s in plan.steps if s.status == ExecutionStatus.SUCCESS]
        failed_steps = [s for s in plan.steps if s.status == ExecutionStatus.FAILED]
        
        lines = []
        lines.append("🔍 **Query Results (Fallback)**\n")
        
        for step in successful_steps:
            lines.append(f"### {step.purpose}")
            result = step.result or {}
            items = result.get("items", []) if isinstance(result, dict) else []
            
            if items:
                lines.append(f"*Found {len(items)} items*")
                # Increased limit for fallback
                for i, item in enumerate(items[:50], 1):
                    title = "Item"
                    if isinstance(item, dict):
                        fields = item.get("fields", item)
                        title = fields.get("System.Title", str(item))
                    lines.append(f"{i}. {str(title)[:100]}")
                if len(items) > 50:
                    lines.append(f"*...and {len(items)-50} more*")
            else:
                lines.append(str(result)[:500])
            lines.append("")
            
        if failed_steps:
            lines.append("⚠️ **Failures:**")
            for step in failed_steps:
                lines.append(f"- {step.purpose}: {step.error}")
                
        return "\n".join(lines)
    
    def _handle_plan_failure(self, plan: ExecutionPlan, error: str, rollback_info: str = None) -> str:
        """Generate a helpful error message when plan execution fails."""
        lines = [
            "❌ **Query Execution Failed**\n",
            f"Error: {error}\n",
        ]
        
        # B5: Add rollback status if available
        if rollback_info:
            lines.append(f"**Rollback Status:** {rollback_info}\n")
        
        lines.append("**What was attempted:**")
        
        for step in plan.steps:
            status_icon = {
                ExecutionStatus.SUCCESS: "✅",
                ExecutionStatus.FAILED: "❌",
                ExecutionStatus.SKIPPED: "⏭️",
                ExecutionStatus.PENDING: "⏳"
            }.get(step.status, "❓")
            lines.append(f"- {status_icon} {step.purpose}")
            if step.error:
                lines.append(f"  Error: {step.error}")
        
        lines.append("\n**Suggestions:**")
        lines.append("- Try rephrasing your query with simpler terms")
        lines.append("- Break down the request into smaller parts")
        lines.append("- Check if the project/iteration/team names are correct")
        
        return "\n".join(lines)
    
    # ═══════════════════════════════════════════════════════════════════════════
    # PLAN BUILDERS - Create execution plans for specific query patterns
    # ═══════════════════════════════════════════════════════════════════════════
    
    # Keyword to iteration ID mapping for natural language sprint references
    ITERATION_KEYWORD_MAP = {
        "current": "@CurrentIteration",
        "this": "@CurrentIteration",
        "my": "@CurrentIteration",
        "previous": "@PreviousIteration",
        "last": "@PreviousIteration",
        "prior": "@PreviousIteration",
    }
    
    def _build_iteration_comparison_from_keywords(self, match: re.Match, context: Dict) -> List[ToolStep]:
        """
        Build plan to compare two iterations using keyword references.
        
        Handles queries like:
        - "compare current sprint with previous sprint"
        - "compare this iteration with last iteration"
        - "compare my sprint with prior sprint"
        """
        # Extract named groups for keyword-based iteration references
        kw1 = match.group("kw1").lower()
        kw2 = match.group("kw2").lower()
        
        # Map keywords to ADO iteration macros
        iter1 = self.ITERATION_KEYWORD_MAP.get(kw1, "@CurrentIteration")
        iter2 = self.ITERATION_KEYWORD_MAP.get(kw2, "@PreviousIteration")
        
        project = context.get("project", "")
        team = context.get("team", project)
        
        logger.info(f"[ORCHESTRATOR] Building keyword-based iteration comparison: {kw1}={iter1}, {kw2}={iter2}")
        
        return [
            ToolStep(
                tool_name="wit_get_work_items_for_iteration",
                args={"project": project, "team": team, "iterationId": iter1},
                purpose=f"Fetch items from {kw1} sprint ({iter1})",
                dependency=ToolDependency.NONE
            ),
            ToolStep(
                tool_name="wit_get_work_items_for_iteration",
                args={"project": project, "team": team, "iterationId": iter2},
                purpose=f"Fetch items from {kw2} sprint ({iter2})",
                dependency=ToolDependency.NONE
            ),
        ]
    
    def _build_iteration_comparison_plan(self, match: re.Match, context: Dict) -> List[ToolStep]:
        """Build plan to compare two iterations."""
        iter1, iter2 = match.group(1), match.group(2)
        project = context.get("project", "")
        team = context.get("team", project)
        
        return [
            ToolStep(
                tool_name="wit_get_work_items_for_iteration",
                args={"project": project, "team": team, "iterationId": iter1},
                purpose=f"Fetch items from iteration {iter1}",
                dependency=ToolDependency.NONE
            ),
            ToolStep(
                tool_name="wit_get_work_items_for_iteration",
                args={"project": project, "team": team, "iterationId": iter2},
                purpose=f"Fetch items from iteration {iter2}",
                dependency=ToolDependency.NONE
            ),
        ]

    def _build_items_in_sprint_plan(self, match: re.Match, context: Dict) -> List[ToolStep]:
        """Build plan to fetch work items (e.g., bugs) in a specific sprint/iteration."""
        iter_id = match.group(1)
        project = context.get("project", "")
        team = context.get("team", project)

        return [
            ToolStep(
                tool_name="wit_get_work_items_for_iteration",
                args={"project": project, "team": team, "iterationId": iter_id},
                purpose=f"Fetch work items in iteration {iter_id}",
                dependency=ToolDependency.NONE
            )
        ]
    
    def _build_person_sprint_plan(self, match: re.Match, context: Dict) -> List[ToolStep]:
        """Build plan to get items for a person in a specific sprint."""
        person, sprint = match.group(1), match.group(2)
        project = context.get("project", "")
        team = context.get("team", project)
        
        return [
            ToolStep(
                tool_name="wit_get_work_items_for_iteration",
                args={"project": project, "team": team, "iterationId": sprint},
                purpose=f"Fetch all items from sprint {sprint}",
                dependency=ToolDependency.NONE
            ),
            # The filtering by person will be done post-execution
        ]
    
    def _build_conjunction_multi_query(self, match: re.Match, context: Dict) -> List[ToolStep]:
        """Build plan for conjunction-based multi-queries like 'get X, list Y, and show Z'.
        
        NOTE: This function is deprecated. Tool selection should come from the planner,
        not from hardcoded keyword matching. This is kept for backward compatibility only.
        The planner should provide explicit tool instructions instead.
        """
        logger.warning("[ORCHESTRATOR] _build_conjunction_multi_query called - this function should be replaced with planner-driven tool selection")
        # Return empty list - planner should handle this
        return []
        
        return steps if steps else []
    
    def _build_workload_analysis_plan(self, match: re.Match, context: Dict) -> List[ToolStep]:
        """Build plan to analyze workload across developers."""
        project = context.get("project", "")
        
        # Use centralized config for non-completed states
        from config import config as _cfg
        _not_started = _cfg.get_states_for_category('not_started')
        _in_progress = _cfg.get_states_for_category('in_progress')
        _active_states = _not_started + _in_progress
        
        return [
            ToolStep(
                tool_name="search_workitem",
                args={"searchText": "Bug", "project": [project], "workItemType": ["Bug"], "state": _active_states, "top": 1000},
                purpose="Fetch active bugs for workload analysis",
                dependency=ToolDependency.NONE
            ),
            ToolStep(
                tool_name="search_workitem",
                args={"searchText": "Task", "project": [project], "workItemType": ["Task"], "state": _active_states, "top": 1000},
                purpose="Fetch active tasks for workload analysis",
                dependency=ToolDependency.NONE
            ),
        ]
    
    def _build_stale_items_by_group_plan(self, match: re.Match, context: Dict) -> List[ToolStep]:
        """Build plan to find stale items grouped by area/assignee."""
        project = context.get("project", "")
        
        # Calculate date 14 days ago
        stale_date = (datetime.utcnow() - timedelta(days=14)).strftime("%Y-%m-%d")
        
        from config import config as _cfg_stale
        _completed_states = _cfg_stale.get_states_for_category('completed')
        _completed_sql = ", ".join(f"'{s}'" for s in _completed_states)
        
        wiql = f"""
        SELECT [System.Id], [System.Title], [System.State], [System.AssignedTo], [System.AreaPath], [System.ChangedDate]
        FROM WorkItems
        WHERE [System.TeamProject] = '{project}'
        AND [System.State] NOT IN ({_completed_sql})
        AND [System.ChangedDate] < '{stale_date}'
        ORDER BY [System.ChangedDate] ASC
        """
        
        return [
            ToolStep(
                tool_name="execute_wiql",
                args={"project": project, "wiql": wiql},
                purpose="Find stale items (not updated in 14+ days)",
                dependency=ToolDependency.NONE
            ),
        ]
    
    def _build_complete_sprint_report_plan(self, match: re.Match, context: Dict) -> List[ToolStep]:
        """Build plan for complete sprint report."""
        sprint = match.group(1)
        project = context.get("project", "")
        team = context.get("team", project)
        
        return [
            ToolStep(
                tool_name="wit_get_work_items_for_iteration",
                args={"project": project, "team": team, "iterationId": sprint},
                purpose=f"Fetch all work items in sprint {sprint}",
                dependency=ToolDependency.NONE
            ),
            ToolStep(
                tool_name="work_list_team_iterations",
                args={"project": project, "team": team},
                purpose="Get iteration details and dates",
                dependency=ToolDependency.NONE
            ),
        ]
    
    def _build_pr_with_workitems_plan(self, match: re.Match, context: Dict) -> List[ToolStep]:
        """Build plan to get PRs with their linked work items."""
        project = context.get("project", "")
        
        return [
            ToolStep(
                tool_name="pr_list_pull_requests",
                args={"project": project, "status": "active"},
                purpose="Fetch active pull requests",
                dependency=ToolDependency.NONE
            ),
            # Work item links would be extracted from PR details
        ]
    
    def _build_build_failures_with_bugs_plan(self, match: re.Match, context: Dict) -> List[ToolStep]:
        """Build plan to get build failures with related bugs."""
        project = context.get("project", "")
        
        return [
            ToolStep(
                tool_name="pipelines_get_builds",
                args={"project": project, "statusFilter": "completed", "resultFilter": "failed"},
                purpose="Fetch failed builds",
                dependency=ToolDependency.NONE
            ),
            ToolStep(
                tool_name="search_workitem",
                args={"searchText": "build failure", "project": [project], "workItemType": ["Bug"], "top": 100},
                purpose="Find bugs related to build failures",
                dependency=ToolDependency.NONE
            ),
        ]
    
    def _build_team_sprint_status_plan(self, match: re.Match, context: Dict) -> List[ToolStep]:
        """Build plan for team sprint status."""
        sprint = match.group(1)
        project = context.get("project", "")
        team = context.get("team", project)
        
        return [
            ToolStep(
                tool_name="wit_get_work_items_for_iteration",
                args={"project": project, "team": team, "iterationId": sprint},
                purpose=f"Fetch work items in sprint {sprint}",
                dependency=ToolDependency.NONE
            ),
            ToolStep(
                tool_name="work_list_team_iterations",
                args={"project": project, "team": team, "timeframe": "current"},
                purpose="Get current iteration capacity info",
                dependency=ToolDependency.NONE
            ),
        ]
    
    def _build_workitem_history_plan(self, match: re.Match, context: Dict) -> List[ToolStep]:
        """Build plan to get work item history."""
        work_item_id = int(match.group(1))
        project = context.get("project", "")
        
        return [
            ToolStep(
                tool_name="wit_get_work_item",
                args={"project": project, "id": work_item_id},
                purpose=f"Fetch work item #{work_item_id} details",
                dependency=ToolDependency.NONE
            ),
            ToolStep(
                tool_name="wit_get_work_item_updates",
                args={"project": project, "id": work_item_id},
                purpose=f"Fetch work item #{work_item_id} history/updates",
                dependency=ToolDependency.NONE
            ),
        ]
    
    def _build_person_commits_plan(self, match: re.Match, context: Dict) -> List[ToolStep]:
        """Build plan to get recent commits by a person."""
        person = match.group(1)
        project = context.get("project", "")
        
        return [
            ToolStep(
                tool_name="repo_list_repos_by_project",
                args={"project": project},
                purpose="List repositories in project",
                dependency=ToolDependency.NONE
            ),
            # Commits would be fetched per-repo in a follow-up
        ]
    
    def _decompose_query_heuristically(self, query: str, context: Dict) -> List[ToolStep]:
        """
        Heuristic decomposition is DEPRECATED.
        
        This function should NOT be used. Tool selection must come from the planner,
        not from hardcoded keyword matching in the orchestrator.
        
        Rationale:
        - Keyword-based tool selection is fragile and doesn't scale
        - The planner has access to full query context and tool metadata
        - This duplicates logic that should live in the planner
        - Hardcoded keyword lists are unmaintainable
        
        Return empty list to force planner-driven execution.
        """
        logger.warning(
            f"[ORCHESTRATOR] _decompose_query_heuristically called for query: {query[:50]}\n"
            f"This function should NOT be used. Tool selection must come from the planner.\n"
            f"Returning empty steps list to enforce planner-driven execution."
        )
        return []
    
    # ═══════════════════════════════════════════════════════════════════════════
    # BACKLOG ORCHESTRATION - Execute structured backlog plans
    # ═══════════════════════════════════════════════════════════════════════════
    
    async def execute_backlog_plan(
        self,
        plan: Dict[str, Any],
        tool_dispatcher,
        context: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        """
        Execute a structured backlog triaging plan generated by LLM planner.
        
        This method handles:
        - Parallel execution of independent steps
        - Sequential execution with dependency management
        - Variable resolution between steps
        - Error handling with partial results
        - Result aggregation
        
        Args:
            plan: Execution plan from backlog_planner.plan_backlog_query()
            tool_dispatcher: ToolDispatcher instance for routing tool calls
            context: Additional execution context
        
        Returns:
            Dict with:
                - success: bool
                - results: Dict[step_id, step_result]
                - final_output: Dict (final step outputs)
                - execution_time_ms: int
                - errors: List[str]
        """
        import time
        from collections import defaultdict
        
        start_time = time.time()
        
        logger.info(f"Executing backlog plan: {plan.get('intent')} with {len(plan.get('steps', []))} steps")
        
        # Initialize execution context
        execution_context = context.copy() if context else {}
        step_results = {}
        errors = []
        
        # Get steps and organize by parallel group
        steps = plan.get("steps", [])
        if not steps:
            return {
                "success": False,
                "results": {},
                "final_output": {},
                "execution_time_ms": 0,
                "errors": ["No steps in plan"]
            }
        
        # Group steps by parallel_group for efficient execution
        parallel_groups = defaultdict(list)
        for step in steps:
            group = step.get("parallel_group", 1)
            parallel_groups[group].append(step)
        
        # Execute each parallel group in order
        for group_num in sorted(parallel_groups.keys()):
            group_steps = parallel_groups[group_num]
            
            logger.info(f"Executing parallel group {group_num} with {len(group_steps)} steps")
            
            # Check if steps can run in parallel
            if len(group_steps) > 1 and plan.get("can_parallelize", False):
                # Execute in parallel
                tasks = [
                    self._execute_backlog_step(
                        step, tool_dispatcher, execution_context, step_results
                    )
                    for step in group_steps
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                
                # Process results
                for i, result in enumerate(results):
                    step = group_steps[i]
                    step_id = step["step_id"]
                    
                    if isinstance(result, Exception):
                        error_msg = f"Step {step_id} failed: {str(result)}"
                        errors.append(error_msg)
                        logger.error(error_msg)
                        
                        # Mark as failed but continue if not required
                        if not step.get("required", True):
                            logger.warning(f"Step {step_id} failed but is optional, continuing...")
                            step_results[step_id] = {"success": False, "error": str(result)}
                        else:
                            # Required step failed - abort execution
                            return {
                                "success": False,
                                "results": step_results,
                                "final_output": {},
                                "execution_time_ms": int((time.time() - start_time) * 1000),
                                "errors": errors + [f"Required step {step_id} failed, aborting"]
                            }
                    else:
                        # Success
                        step_results[step_id] = result
                        # Store result in execution context for next steps
                        output_var = step.get("output_var")
                        if output_var and result.get("success"):
                            execution_context[output_var] = result.get("result", {})
            else:
                # Execute sequentially
                for step in group_steps:
                    step_id = step["step_id"]
                    
                    try:
                        result = await self._execute_backlog_step(
                            step, tool_dispatcher, execution_context, step_results
                        )
                        
                        step_results[step_id] = result
                        
                        # Store result in execution context
                        output_var = step.get("output_var")
                        if output_var and result.get("success"):
                            execution_context[output_var] = result.get("result", {})
                        
                    except Exception as e:
                        error_msg = f"Step {step_id} failed: {str(e)}"
                        errors.append(error_msg)
                        logger.error(error_msg, exc_info=True)
                        
                        # Check if required
                        if step.get("required", True):
                            return {
                                "success": False,
                                "results": step_results,
                                "final_output": {},
                                "execution_time_ms": int((time.time() - start_time) * 1000),
                                "errors": errors + [f"Required step {step_id} failed, aborting"]
                            }
                        else:
                            step_results[step_id] = {"success": False, "error": str(e)}
        
        # Extract final outputs
        final_output_vars = plan.get("final_output_vars", [])
        final_output = {}
        for var_name in final_output_vars:
            if var_name in execution_context:
                final_output[var_name] = execution_context[var_name]
        
        execution_time_ms = int((time.time() - start_time) * 1000)
        
        logger.info(
            f"Backlog plan execution completed in {execution_time_ms}ms "
            f"({len(step_results)} steps, {len(errors)} errors)"
        )
        
        return {
            "success": len(errors) == 0 or all(not step.get("required", True) for step in steps if step["step_id"] in [e.split()[1] for e in errors]),
            "results": step_results,
            "final_output": final_output,
            "execution_time_ms": execution_time_ms,
            "errors": errors,
            "plan_metadata": plan.get("planner_metadata", {})
        }
    
    async def _execute_backlog_step(
        self,
        step: Dict[str, Any],
        tool_dispatcher,
        execution_context: Dict[str, Any],
        previous_results: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Execute a single step in a backlog plan.
        
        Args:
            step: Step definition from plan
            tool_dispatcher: ToolDispatcher for executing tools
            execution_context: Current execution context with previous outputs
            previous_results: Results from previously executed steps
        
        Returns:
            Dict with step execution result
        """
        step_id = step["step_id"]
        tool_name = step["tool"]
        args = step.get("args", {})
        
        logger.debug(f"Executing step {step_id}: {tool_name} with args: {args}")
        
        # Check skip condition if present
        skip_if = step.get("skip_if")
        if skip_if:
            # Simple evaluation of skip conditions
            # For now, we'll just log it - proper evaluation would need safe eval
            logger.debug(f"Step {step_id} has skip condition: {skip_if}")
        
        # Dispatch tool execution (will handle variable resolution)
        from agents.pm_agent.tool_dispatcher import ToolType
        
        # Detect tool type
        tool_type_str = step.get("tool_type", "pm_skill")
        if tool_type_str == "mcp":
            tool_type = ToolType.MCP
        elif tool_type_str == "pm_skill":
            tool_type = ToolType.PM_SKILL
        else:
            tool_type = None  # Auto-detect
        
        dispatch_result = await tool_dispatcher.dispatch(
            tool_name=tool_name,
            args=args,
            execution_context=execution_context,
            tool_type=tool_type
        )
        
        if dispatch_result.success:
            logger.info(
                f"Step {step_id} ({tool_name}) completed successfully "
                f"in {dispatch_result.execution_time_ms}ms"
            )
            return {
                "success": True,
                "step_id": step_id,
                "tool_name": tool_name,
                "result": dispatch_result.result,
                "execution_time_ms": dispatch_result.execution_time_ms,
                "metadata": dispatch_result.metadata
            }
        else:
            logger.error(
                f"Step {step_id} ({tool_name}) failed: {dispatch_result.error}"
            )
            return {
                "success": False,
                "step_id": step_id,
                "tool_name": tool_name,
                "error": dispatch_result.error,
                "execution_time_ms": dispatch_result.execution_time_ms,
                "metadata": dispatch_result.metadata
            }


# ═══════════════════════════════════════════════════════════════════════════════
# CONVENIENCE FUNCTION - Easy access from agent
# ═══════════════════════════════════════════════════════════════════════════════

async def execute_multi_tool_query(
    query: str,
    context: Dict[str, Any],
    tool_executor,
    mcp_connector,
    config: OrchestratorConfig = None
) -> str:
    """
    Convenience function to execute a multi-tool query.
    
    Args:
        query: User's natural language query
        context: Current context
        tool_executor: ToolExecutor instance
        mcp_connector: MCPConnector instance
        config: Optional orchestrator config
        
    Returns:
        Synthesized result string
    """
    orchestrator = MultiToolOrchestrator(tool_executor, mcp_connector, config)
    
    if not orchestrator.requires_multi_tool(query):
        # Single tool query - let normal flow handle it
        return None
    
    plan = await orchestrator.plan_execution(query, context)
    return await orchestrator.execute_plan(plan)


async def execute_planner_plan(
    planner_plan: Dict[str, Any],
    context: Dict[str, Any],
    tool_executor,
    mcp_connector,
    config: OrchestratorConfig = None,
    validate: bool = True,
    session_id: str = None
) -> str:
    """
    Execute a multi-step plan generated by the LLM planner.
    
    This function bridges the gap between the planner's dynamic plans
    and the orchestrator's execution engine.
    
    Args:
        planner_plan: Dict with 'action': 'execute_plan' and 'execution_plan' key
        context: Current context (project, team, iteration, etc.)
        tool_executor: ToolExecutor instance
        mcp_connector: MCPConnector instance
        config: Optional orchestrator config
        validate: Whether to validate plan before execution (default True)
        session_id: Session ID for trace tracking (optional)
        
    Returns:
        Synthesized result string
        
    Raises:
        ValueError: If plan validation fails and validate=True
    """
    if planner_plan.get("action") != "execute_plan":
        raise ValueError(f"Expected action='execute_plan', got '{planner_plan.get('action')}'")
    
    orchestrator = MultiToolOrchestrator(tool_executor, mcp_connector, config)
    
    # Convert planner's JSON plan to ExecutionPlan object
    execution_plan = orchestrator.convert_planner_plan_to_execution_plan(
        planner_plan, context
    )
    
    # Optionally validate the plan
    if validate:
        is_valid, errors = orchestrator.validate_plan(execution_plan)
        if not is_valid:
            error_msg = f"Plan validation failed: {'; '.join(errors)}"
            logger.error(f"[ORCHESTRATOR] {error_msg}")
            raise ValueError(error_msg)
    
    # Execute the plan
    return await orchestrator.execute_plan(execution_plan)
