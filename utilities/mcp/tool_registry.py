"""
MCP Tool Registry and Executor for PM Agent.

This module provides:
1. MCP_TOOL_REGISTRY - Metadata about available MCP tools (pagination, required args, etc.)
2. ToolExecutor - Executes tool calls with pagination handling and response normalization
3. Tool discovery functions - Query-to-tool matching with scoring and ranking
"""

from typing import Dict, Any, Optional, List, Tuple
import json
import asyncio
import random
import os
import logging
import difflib
import re

# Langfuse for MCP error logging
from utilities.langfuse_client import create_span, finalize_span

logger = logging.getLogger(__name__)


# Feature flags for progressive enablement
ENABLE_BATCH_RETRY = os.getenv("ENABLE_BATCH_RETRY", "true").lower() == "true"
MAX_BATCH_SIZE = int(os.getenv("MAX_BATCH_SIZE", "200"))  # ADO API limit
MIN_BATCH_SIZE = int(os.getenv("MIN_BATCH_SIZE", "10"))  # Minimum chunk size for retries


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# ENUM DEFINITIONS - Valid values for tool arguments with enum constraints
# This is GENERIC - applies to ANY tool with these argument types.
# The LLM may generate invalid values; these are normalized before execution.
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
TOOL_ARG_ENUM_VALUES: Dict[str, Dict[str, List[str]]] = {
    # Argument name -> list of valid values (case-insensitive matching)
    # These apply to any tool that has these argument names
    "expand": {
        # Different tools have different valid values for 'expand'
        "wit_get_query": ["none", "wiql", "clauses", "all", "minimal"],
        "wit_get_work_item": ["none", "relations", "fields", "links", "all"],
        "wit_get_work_items": ["none", "relations", "fields", "links", "all"],
        "wit_get_work_items_batch_by_ids": ["none", "relations", "fields", "links", "all"],
        "wit_list_work_item_revisions": ["None", "Relations", "Fields", "Links", "All"],  # Capitalized!
        "_default": ["none", "all"],  # Default for any tool with 'expand' arg
    },
    "linkType": {
        "_default": ["Branch", "Build", "Fixed in Commit", "Pull Request", "Related Workitem"],
    },
    "state": {
        "_default": [
            "New", "Ready", "Requested", "Scheduled", "In Planning", "Accepted",
            "Active", "Design", "Code Review", "Code Complete", "QA", "QA Complete",
            "UAT", "PRE-PROD", "Approved for Production", "In Progress", "Awaiting Approvals",
            "Closed", "Resolved", "Completed", "UAT Complete", "Released", "Removed",
            "Not a Bug", "Requirement Bug",
            "On Hold", "Issues Found", "Reopened", "Inactive"
        ],
    },
    "workItemType": {
        "_default": ["Bug", "User Story", "Task", "Feature", "Epic", "Issue", "Test Case"],
    },
}


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# NUMERIC ENUM MAPPINGS - For args where MCP expects number but LLM sends string
# These are Azure DevOps API enum values that are passed as numbers
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
NUMERIC_ENUM_MAPPINGS: Dict[str, Dict[str, int]] = {
    # BuildStatus enum values (for pipelines_get_builds statusFilter)
    # NOTE: The LLM may use result-like values here (failed, succeeded) so we include those too
    "statusFilter": {
        "none": 0,
        "inprogress": 1,
        "in progress": 1,
        "completed": 2,
        "cancelling": 4,
        "postponed": 8,
        "notstarted": 32,
        "not started": 32,
        "all": 47,  # All statuses combined
        # Result-like values that LLM may send to statusFilter (for result filtering)
        "succeeded": 2,
        "failed": 8,
        "partiallysucceeded": 4,
        "partially succeeded": 4,
        "canceled": 32,
    },
    # BuildReason enum values (for pipelines_get_builds reasonFilter)
    "reasonFilter": {
        "none": 0,
        "manual": 1,
        "individualci": 2,
        "individual ci": 2,
        "batchedci": 4,
        "batched ci": 4,
        "schedule": 8,
        "scheduleforced": 16,
        "schedule forced": 16,
        "pullrequest": 32,
        "pull request": 32,
        "all": 127,
    },
    # BuildResult enum values (for pipelines_get_builds resultFilter)
    "resultFilter": {
        "none": 0,
        "succeeded": 2,
        "partiallysucceeded": 4,
        "partially succeeded": 4,
        "failed": 8,
        "canceled": 32,
    },
    # QueryDeletedOption enum
    "deletedFilter": {
        "excludedeleted": 0,
        "exclude deleted": 0,
        "includedeleted": 1,
        "include deleted": 1,
        "onlydeleted": 2,
        "only deleted": 2,
    },
}


def coerce_numeric_enum(arg_name: str, value: Any) -> Tuple[Any, bool, str]:
    """
    Coerce string enum values to their numeric equivalents.
    
    Many Azure DevOps APIs expect enum arguments as numbers, but LLMs
    naturally produce string values like "completed" or "in progress".
    This function converts those to the correct numeric values.
    
    Args:
        arg_name: Name of the argument (e.g., "statusFilter")
        value: The value to potentially convert
        
    Returns:
        Tuple of (converted_value, was_converted, conversion_reason)
    """
    if value is None:
        return value, False, ""
    
    # Already a number, no conversion needed
    if isinstance(value, (int, float)):
        return value, False, ""
    
    # Check if this arg has a numeric mapping
    if arg_name not in NUMERIC_ENUM_MAPPINGS:
        return value, False, ""
    
    enum_map = NUMERIC_ENUM_MAPPINGS[arg_name]
    value_lower = str(value).lower().strip()
    
    if value_lower in enum_map:
        numeric_value = enum_map[value_lower]
        logger.info(f"[ToolRegistry] Coerced {arg_name}='{value}' â†’ {numeric_value} (numeric enum)")
        return numeric_value, True, f"string_to_enum:{value}â†’{numeric_value}"
    
    # Try fuzzy matching for close values
    for key in enum_map:
        if key in value_lower or value_lower in key:
            numeric_value = enum_map[key]
            logger.info(f"[ToolRegistry] Fuzzy coerced {arg_name}='{value}' â†’ {numeric_value} (matched '{key}')")
            return numeric_value, True, f"fuzzy_enum:{value}â†’{numeric_value}"
    
    logger.warning(f"[ToolRegistry] Could not coerce {arg_name}='{value}' to numeric enum. Valid values: {list(enum_map.keys())}")
    return value, False, f"coercion_failed:{value}"


def normalize_enum_arg(tool_name: str, arg_name: str, value: Any) -> Tuple[Any, bool, str]:
    """
    Normalize an enum argument value to a valid value.
    
    This function handles common LLM mistakes like:
    - Wrong case: "all" -> "All", "none" -> "None"
    - Misspellings: "WIQL" -> "Wiql"
    - Invalid values: Closest match using fuzzy matching
    
    Args:
        tool_name: Name of the tool being called
        arg_name: Name of the argument (e.g., "expand")
        value: The value to normalize
        
    Returns:
        Tuple of (normalized_value, was_normalized, normalization_reason):
        - normalized_value: The corrected value (or original if no match)
        - was_normalized: True if the value was changed
        - normalization_reason: Description of what was done
    """
    if value is None:
        return value, False, ""
    
    # Get valid values for this arg
    arg_enum = TOOL_ARG_ENUM_VALUES.get(arg_name, {})
    if not arg_enum:
        return value, False, ""  # No enum constraint for this arg
    
    # Get tool-specific values or default
    valid_values = arg_enum.get(tool_name, arg_enum.get("_default", []))
    if not valid_values:
        return value, False, ""
    
    # Handle Python list inputs (LLM may send workItemType as ["Bug", "Task", ...] instead of a string)
    if isinstance(value, list):
        # Validate each element against the enum, keep only valid ones
        validated = []
        for item in value:
            item_str = str(item).strip()
            for valid in valid_values:
                if valid.lower() == item_str.lower():
                    validated.append(valid)
                    break
        if validated:
            # If all/most values are valid, return as comma-separated string
            # (MCP tools typically accept single string values)
            if len(validated) == 1:
                return validated[0], True, f"list_single:{value}→{validated[0]}"
            else:
                # Multiple valid types - return comma-separated for post-filtering
                result = ",".join(validated)
                logger.info(f"[ToolRegistry] Normalized list {arg_name}={value} → '{result}' ({len(validated)} valid values)")
                return result, True, f"list_multi:{value}→{result}"
        else:
            logger.warning(f"[ToolRegistry] List {arg_name}={value} has no valid values. Using default: {valid_values[0]}")
            return valid_values[0], True, f"list_default:{value}→{valid_values[0]}"
    
    # Convert to string for comparison
    value_str = str(value).strip()
    value_lower = value_str.lower()
    
    # Handle comma-separated values (common LLM mistake)
    if ',' in value_str:
        # LLM generated something like "comments, relations, history"
        # Extract first valid value from the list
        parts = [p.strip().lower() for p in value_str.split(',')]
        for part in parts:
            for valid in valid_values:
                if valid.lower() == part:
                    logger.warning(f"[ToolRegistry] Normalized comma-separated {arg_name}='{value_str}' â†’ '{valid}' (took first valid)")
                    return valid, True, f"comma_separated:{value_str}â†’{valid}"
        logger.warning(f"[ToolRegistry] Invalid comma-separated {arg_name}='{value_str}' - no valid values found. Using default: {valid_values[0]}")
        return valid_values[0], True, f"comma_separated_default:{value_str}â†’{valid_values[0]}"
    
    # Direct case-insensitive match
    for valid in valid_values:
        if valid.lower() == value_lower:
            if valid != value_str:
                logger.info(f"[ToolRegistry] Normalized {arg_name}='{value_str}' â†’ '{valid}' (case fix)")
                return valid, True, f"case_fix:{value_str}â†’{valid}"
            return value_str, False, ""  # Already correct
    
    # Fuzzy match for close misspellings
    matches = difflib.get_close_matches(value_lower, [v.lower() for v in valid_values], n=1, cutoff=0.7)
    if matches:
        # Find the original case version
        matched_lower = matches[0]
        for valid in valid_values:
            if valid.lower() == matched_lower:
                logger.warning(f"[ToolRegistry] Fuzzy-normalized {arg_name}='{value_str}' â†’ '{valid}'")
                return valid, True, f"fuzzy_match:{value_str}â†’{valid}"
    
    # No match found - log warning but keep original (may cause API error)
    logger.warning(
        f"[ToolRegistry] Invalid {arg_name}='{value_str}' for {tool_name}. "
        f"Valid values: {valid_values}. Keeping original."
    )
    return value_str, False, f"invalid:{value_str}"


def convert_argument_types(tool_name: str, args: Dict[str, Any], tool_schema: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """
    FIX #23: Convert argument types based on tool schema (string->number, string->integer, etc.)
    
    This handles type conversions for arguments that need to be numeric but are received as strings.
    Examples:
    - buildId: "24098" (string) -> 24098 (number)
    - ids: ["1", "2"] (strings) -> [1, 2] (integers)
    
    Args:
        tool_name: Name of the tool being called
        args: Original arguments dict
        tool_schema: Optional tool schema with inputSchema field
        
    Returns:
        Tuple of (converted_args, conversion_log)
    """
    if not args or not tool_schema or "inputSchema" not in tool_schema:
        return args, {}
    
    converted = dict(args)
    log: Dict[str, str] = {}
    
    try:
        input_schema = tool_schema.get("inputSchema", {})
        properties = input_schema.get("properties", {})
        
        for arg_name, arg_value in args.items():
            if arg_name not in properties:
                continue
            
            prop_schema = properties[arg_name]
            expected_type = prop_schema.get("type")
            
            # Handle array type (e.g., ids: ["1", "2"] -> [1, 2])
            if expected_type == "array" and isinstance(arg_value, list):
                item_type = prop_schema.get("items", {}).get("type")
                if item_type in ["number", "integer"]:
                    try:
                        converted_list = []
                        for item in arg_value:
                            if isinstance(item, str):
                                converted_item = int(item) if item_type == "integer" else float(item)
                                converted_list.append(converted_item)
                            else:
                                converted_list.append(item)
                        if any(isinstance(orig, str) for orig in arg_value):
                            converted[arg_name] = converted_list
                            log[arg_name] = f"array_type_conversion:{arg_value}â†’{converted_list}"
                            logger.info(f"[ToolRegistry] FIX #23: Converted array {arg_name} items to {item_type}: {arg_value} â†’ {converted_list}")
                    except (ValueError, TypeError) as e:
                        logger.warning(f"[ToolRegistry] FIX #23: Failed to convert array {arg_name}: {e}")
            
            # Handle scalar numeric type (e.g., buildId: "24098" -> 24098)
            elif expected_type in ["number", "integer"] and isinstance(arg_value, str):
                try:
                    converted_value = int(arg_value) if expected_type == "integer" else float(arg_value)
                    converted[arg_name] = converted_value
                    log[arg_name] = f"scalar_type_conversion:{arg_value}â†’{converted_value}"
                    logger.info(f"[ToolRegistry] FIX #23: Converted {arg_name} to {expected_type}: '{arg_value}' â†’ {converted_value}")
                except (ValueError, TypeError) as e:
                    logger.warning(f"[ToolRegistry] FIX #23: Failed to convert scalar {arg_name} '{arg_value}' to {expected_type}: {e}")
    
    except Exception as e:
        logger.warning(f"[ToolRegistry] FIX #23: Error in convert_argument_types: {e}")
    
    return converted, log


def normalize_tool_args(tool_name: str, args: Dict[str, Any], tool_schema: Optional[Dict[str, Any]] = None) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """
    Normalize all arguments for a tool, including enum validation and numeric coercion.
    
    Args:
        tool_name: Name of the tool being called
        args: Original arguments dict
        tool_schema: Optional tool schema for type conversion (FIX #23)
        
    Returns:
        Tuple of (normalized_args, normalization_log):
        - normalized_args: Copy of args with normalized values
        - normalization_log: Dict of arg_name -> normalization_reason
    """
    if not args:
        return args, {}
    
    normalized = dict(args)
    log: Dict[str, str] = {}
    
    # FIX #23: First pass - convert types based on schema (string to number, etc.)
    if tool_schema:
        type_converted, type_log = convert_argument_types(tool_name, normalized, tool_schema)
        normalized = type_converted
        log.update(type_log)
    
    for arg_name, value in args.items():
        # First, check if this arg needs numeric enum coercion (API expects number, LLM sends string)
        coerced_value, was_coerced, coerce_reason = coerce_numeric_enum(arg_name, value)
        if was_coerced:
            normalized[arg_name] = coerced_value
            log[arg_name] = coerce_reason
            logger.info(f"[ToolRegistry] Coerced {arg_name}='{value}' â†’ {coerced_value} for {tool_name}")
            continue  # Skip string enum normalization for numeric enums
        
        # Check if this arg has string enum constraints
        if arg_name in TOOL_ARG_ENUM_VALUES:
            new_value, was_normalized, reason = normalize_enum_arg(tool_name, arg_name, value)
            if was_normalized:
                normalized[arg_name] = new_value
                log[arg_name] = reason
    
    return normalized, log


# Registry of MCP tools with metadata
# Keys are tool names, values are dicts with:
#   - pagination: bool - whether the tool supports pagination via continuationToken
#   - required_args: list - required argument names
#   - optional_args: dict - optional argument name -> type hint
#   - arg_descriptions: dict - argument name -> short description
#   - description: str - human-readable description
#   - use_cases: list - example use cases to help LLM understand when to use this tool
#   - examples: list - example invocations
#   - keywords: list - natural language keywords for query matching (discovery)
#   - query_patterns: list - regex patterns for query matching (discovery)
#   - category: str - tool category (work_items, pull_requests, etc.)
#   - priority: int - tool selection priority 1-10 (default 5)
#   - often_used_with: list - tools often combined with this one
#   - alternative_tools: list - fallback/equivalent tools
#   - write: bool - whether tool modifies data (default False)

# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# DYNAMIC TOOL REGISTRY
# This dict is populated at startup by initialize_registry() which auto-generates
# entries from the live MCP connector's tools_cache. This replaces the old approach
# of manually maintaining thousands of lines of static metadata.
#
# The registry is populated ONCE at startup and then used by:
# - Planner (for tool selection)
# - UnifiedToolRegistry (for validation)
# - ToolExecutor (for argument validation and context injection)
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
MCP_TOOL_REGISTRY: Dict[str, Dict[str, Any]] = {}

# Flag to track whether the registry has been initialized
_REGISTRY_INITIALIZED = False


def initialize_registry(tools_cache: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """
    Initialize MCP_TOOL_REGISTRY from live MCP tools_cache.
    
    Called once at startup after MCPConnector.initialize() populates tools_cache.
    Generates all metadata (category, priority, required/optional args, keywords, etc.)
    dynamically from the MCP tool definitions.
    
    Args:
        tools_cache: Dict from MCPConnector.tools_cache
                     {tool_name: {"name": str, "description": str, "inputSchema": {...}}}
    
    Returns:
        The populated MCP_TOOL_REGISTRY dict
    """
    global MCP_TOOL_REGISTRY, _REGISTRY_INITIALIZED
    
    from utilities.mcp.tool_registry_generator import (
        generate_registry_from_tools_cache,
        get_manual_overrides
    )
    
    overrides = get_manual_overrides()
    generated = generate_registry_from_tools_cache(tools_cache, overrides=overrides)
    
    # ════════════════════════════════════════════════════════════════    # ══════════════════════════════════════════════════════════════════════════
    # CRITICAL FIX: Preserve LOCAL skills that are NOT MCP tools.
    # execute_wiql is a local Python skill (REST API) — it will never appear
    # in the MCP tools_cache. We must re-inject it after clearing the registry.
    # Without this, the LLM planner never sees execute_wiql as an option and
    # always falls back to search_workitem for date/priority/field queries.
    # ══════════════════════════════════════════════════════════════════════════
    _LOCAL_SKILLS = {
        name: meta for name, meta in _FALLBACK_REGISTRY.items()
        if name not in generated  # Only inject if NOT already provided by live MCP
    }
    
    # Replace the registry contents
    MCP_TOOL_REGISTRY.clear()
    MCP_TOOL_REGISTRY.update(generated)
    
    # Re-inject local skills that were lost during clear()
    if _LOCAL_SKILLS:
        MCP_TOOL_REGISTRY.update(_LOCAL_SKILLS)
        logger.info(f"[TOOL_REGISTRY] Re-injected {len(_LOCAL_SKILLS)} local skills: {list(_LOCAL_SKILLS.keys())}")
    
    _REGISTRY_INITIALIZED = True
    
    logger.info(f"[TOOL_REGISTRY] Registry initialized with {len(MCP_TOOL_REGISTRY)} tools ({len(generated)} MCP + {len(_LOCAL_SKILLS)} local)")
    return MCP_TOOL_REGISTRY


def is_registry_initialized() -> bool:
    """Check if the registry has been initialized from live MCP."""
    return _REGISTRY_INITIALIZED


# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
# LEGACY COMPATIBILITY - Minimal fallback registry for when MCP is not yet initialized
# This ensures the application can start even if MCP connection is delayed.
# It will be overwritten by initialize_registry() once MCP is ready.
# â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
_FALLBACK_REGISTRY: Dict[str, Dict[str, Any]] = {
    "execute_wiql": {
        "pagination": False,
        "required_args": ["wiql"],
        "optional_args": {"project": "str", "top": "int"},
        "arg_descriptions": {
            "wiql": "WIQL query string (SELECT ... FROM WorkItems WHERE ...)",
            "project": "Azure DevOps project name",
            "top": "Maximum number of results (default 1000)"
        },
        "description": "Execute a WIQL query against Azure DevOps REST API. LOCAL skill - not an MCP tool. Used for complex queries with date ranges, priority filters, field-specific conditions, aggregation, and comparison.",
        "category": "work_items",
        "priority": 10,
        "mcp_available": True,
        "write": False,
        "keywords": ["wiql", "query", "work items", "filter", "date", "priority", "closed", "created", "state", "title", "contains"]
    },
    "core_list_projects": {
        "pagination": False,
        "required_args": [],
        "optional_args": {},
        "arg_descriptions": {},
        "description": "List all projects in the Azure DevOps organization",
        "category": "core",
        "priority": 8,
        "mcp_available": True,
        "write": False
    },
    "core_list_project_teams": {
        "pagination": True,
        "required_args": ["project"],
        "optional_args": {"top": "int", "skip": "int"},
        "arg_descriptions": {"project": "Azure DevOps project name"},
        "description": "List teams in an Azure DevOps project",
        "category": "core",
        "priority": 7,
        "mcp_available": True,
        "write": False
    },
    "search_workitem": {
        "pagination": False,
        "required_args": ["searchText"],
        "optional_args": {"project": "list", "areaPath": "list", "workItemType": "list", "state": "list", "assignedTo": "list", "top": "int"},
        "arg_descriptions": {"searchText": "Text to search for in work items"},
        "description": "Search for work items across projects using full-text search with filters.",
        "category": "search",
        "priority": 9,
        "mcp_available": True,
        "write": False
    },
    "wit_get_work_item": {
        "pagination": False,
        "required_args": ["id"],
        "optional_args": {"project": "str", "expand": "str"},
        "arg_descriptions": {"id": "Work item ID"},
        "description": "Get a single work item by ID with full details.",
        "category": "work_items",
        "priority": 8,
        "mcp_available": True,
        "write": False
    },
    "wit_get_work_items_for_iteration": {
        "pagination": False,
        "required_args": ["project", "iterationId"],
        "optional_args": {"team": "str", "workItemType": "str", "state": "str", "areaPath": "str", "assignedTo": "str", "unassigned": "bool"},
        "arg_descriptions": {
            "project": "Azure DevOps project name",
            "iterationId": "Iteration ID, GUID, or macro (@CurrentIteration, @PreviousIteration)",
            "team": "Team name for scoping the iteration"
        },
        "description": "Retrieve work items for a specified iteration/sprint. Essential for sprint-scoped queries.",
        "category": "work_items",
        "priority": 10,
        "mcp_available": True,
        "write": False
    },
    "work_list_team_iterations": {
        "pagination": False,
        "required_args": ["project", "team"],
        "optional_args": {"timeframe": "str"},
        "arg_descriptions": {
            "project": "Azure DevOps project name",
            "team": "Team name to list iterations for",
            "timeframe": "Optional: 'current' to get only current iteration"
        },
        "description": "List all iterations (sprints) for a specific team. Returns iteration names, paths, and start/end dates. Use for: listing iterations, finding iterations by date range, getting latest/oldest iterations.",
        "category": "work_items",
        "priority": 8,
        "mcp_available": True,
        "write": False,
        "keywords": ["iterations", "sprints", "list", "dates", "team iterations"]
    },
    "work_list_iterations": {
        "pagination": False,
        "required_args": ["project"],
        "optional_args": {"depth": "int"},
        "arg_descriptions": {
            "project": "Azure DevOps project name",
            "depth": "Depth of children to fetch (default 2)"
        },
        "description": "List all iterations in a project (not team-scoped). Returns iteration tree with names and dates.",
        "category": "work_items",
        "priority": 7,
        "mcp_available": True,
        "write": False,
        "keywords": ["iterations", "sprints", "project iterations"]
    },
}


def seed_fallback_registry():
    '''Seed MCP_TOOL_REGISTRY with fallback entries if not yet initialized.'''
    global MCP_TOOL_REGISTRY, _REGISTRY_INITIALIZED
    if not _REGISTRY_INITIALIZED and len(MCP_TOOL_REGISTRY) == 0:
        MCP_TOOL_REGISTRY.update(_FALLBACK_REGISTRY)
        logger.info(f'[TOOL_REGISTRY] Seeded fallback registry with {len(_FALLBACK_REGISTRY)} minimal entries')


# Seed fallback on module import so basic functionality works before MCP init
seed_fallback_registry()



def get_tool_info(tool_name: str) -> Optional[Dict[str, Any]]:
    """Get metadata for a tool from the registry."""
    return MCP_TOOL_REGISTRY.get(tool_name)


def supports_pagination(tool_name: str) -> bool:
    """Check if a tool supports pagination."""
    info = get_tool_info(tool_name)
    return info.get("pagination", False) if info else False


def get_required_args(tool_name: str) -> List[str]:
    """Get required arguments for a tool."""
    info = get_tool_info(tool_name)
    return info.get("required_args", []) if info else []


def validate_tool_args(tool_name: str, args: Dict[str, Any]) -> tuple[bool, str]:
    """
    Validate that required arguments are present for a tool.
    
    Returns:
        Tuple of (is_valid, error_message)
    """
    required = get_required_args(tool_name)
    missing = [arg for arg in required if arg not in args or args[arg] is None]
    
    if missing:
        return False, f"Missing required arguments for {tool_name}: {missing}"
    return True, ""


class ToolExecutor:
    """
    Executes MCP tool calls with pagination handling and response normalization.
    
    This layer sits between the PM Agent and the raw MCP connector, providing:
    - Tool validation against registry
    - Automatic pagination handling
    - Response normalization
    """
    
    def __init__(self, mcp_connector):
        """
        Initialize ToolExecutor.
        
        Args:
            mcp_connector: MCPConnector instance with call_tool method
        """
        self.mcp = mcp_connector
        # Track recent tool calls within this executor to help correlate retries/duplicates
        # Key: (tool, args_json) -> mcp_request_id
        self._recent_calls: Dict[str, str] = {}
    
    def _validate_alias_compatible(self, original_tool: str, alias_tool: str, args: Dict[str, Any]) -> bool:
        """
        Validate that an aliased tool has compatible parameter schema.
        
        Returns True if:
        - Parameters can be mapped between tools
        - Required parameters are present after mapping
        """
        original_meta = MCP_TOOL_REGISTRY.get(original_tool, {})
        alias_meta = MCP_TOOL_REGISTRY.get(alias_tool, {})
        
        # If either tool not in registry, allow (trust MCP validation)
        if not original_meta or not alias_meta:
            return True
        
        # Get required args for alias
        alias_required = set(alias_meta.get('required_args', []))
        if not alias_required:
            return True  # No required args, safe to alias
        
        # Check if all alias required args can be satisfied from original args
        # Account for common parameter name mappings
        param_mappings = {
            'id': ['workItemId', 'id', 'itemId'],
            'workItemId': ['id', 'workItemId'],
            'project': ['project', 'projectName'],
            'repository': ['repositoryId', 'repository', 'repo']
        }
        
        satisfied_count = 0
        for req_arg in alias_required:
            # Check if directly present
            if req_arg in args:
                satisfied_count += 1
                continue
            
            # Check if can be mapped from original args
            for orig_key, orig_val in args.items():
                possible_mappings = param_mappings.get(orig_key, [orig_key])
                if req_arg in possible_mappings:
                    satisfied_count += 1
                    break
        
        compatible = satisfied_count == len(alias_required)
        if not compatible:
            logger.warning(f"[ToolExecutor] Alias validation failed: {original_tool} \u2192 {alias_tool} (satisfied {satisfied_count}/{len(alias_required)} required args)")
        
        return compatible
    
    def _map_parameters(self, original_tool: str, alias_tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """
        Map parameters from original tool format to alias tool format.
        
        Common mappings:
        - id \u2192 workItemId
        - workItemId \u2192 id
        - project \u2192 projectName
        """
        alias_meta = MCP_TOOL_REGISTRY.get(alias_tool, {})
        if not alias_meta:
            return args  # No metadata, return as-is
        
        alias_required = set(alias_meta.get('required_args', []))
        alias_optional = set(alias_meta.get('optional_args', {}).keys())
        alias_all_params = alias_required.union(alias_optional)
        
        mapped_args = {}
        
        # Common parameter mappings
        param_mappings = {
            'id': 'workItemId',           # wit_get_work_item_comments (id) \u2192 wit_list_work_item_comments (workItemId)
            'workItemId': 'id',
            'project': 'projectName',
            'repositoryId': 'repository'
        }
        
        for key, value in args.items():
            # If key is directly accepted by alias, use it
            if key in alias_all_params:
                mapped_args[key] = value
            # Check if we need to map it
            elif key in param_mappings and param_mappings[key] in alias_all_params:
                mapped_key = param_mappings[key]
                mapped_args[mapped_key] = value
                logger.info(f"[ToolExecutor] Mapped parameter: {key} -> {mapped_key} = {value}")
            else:
                # Keep original parameter (may be accepted by alias)
                mapped_args[key] = value
        
        return mapped_args
    
    def _inject_context(self, tool: str, args: Dict[str, Any], context: Dict[str, Any]) -> None:
        """
        Inject missing arguments from context (project, team, etc.) if the tool supports them.
        """
        if not tool or not context:
            print(f"[_inject_context] SKIP: tool={tool}, context={'None' if not context else 'empty'}")
            return

        tool_info = get_tool_info(tool)
        if not tool_info:
            print(f"[_inject_context] SKIP: No tool_info for '{tool}'")
            # Without tool_info we can't know if the tool accepts project — skip force-injection
            # to avoid MCP additionalProperties:false rejection. The orchestrator (FIX #28, #30)
            # handles this at a higher level.
            return

        # Union of required and optional arg names
        supported_args = set(tool_info.get("required_args", []))
        if "optional_args" in tool_info:
            supported_args.update(tool_info["optional_args"].keys())

        print(f"[_inject_context] tool={tool}, project_in_supported={('project' in supported_args)}, project_in_args={('project' in args)}, project_in_ctx={('project' in context)}")

        # 1. Project Injection
        if "project" in supported_args and "project" not in args and "project" in context:
            args["project"] = context["project"]
            print(f"[_inject_context] Injected project='{args['project']}' for {tool}")
        # 2. Team Injection + Validation
        # Validate team names against known teams to catch LLM misinterpretations
        # (e.g., "capacity" extracted from "team capacity", "member" from "team member")
        if "team" in supported_args:
            from orchestrator.context_resolver import VALID_TEAMS, TEAM_ALIASES, DEFAULT_TEAM
            if "team" in args:
                # Team already set by planner — validate it
                team_val = args["team"]
                team_lower = team_val.lower() if isinstance(team_val, str) else ""
                is_valid = any(t.lower() == team_lower for t in VALID_TEAMS)
                if not is_valid:
                    alias_match = TEAM_ALIASES.get(team_lower)
                    if alias_match:
                        logger.info(f"[_inject_context] Team '{team_val}' resolved via alias to '{alias_match}'")
                        args["team"] = alias_match
                    else:
                        # Prefer DEFAULT_TEAM over context team — XOPS 25 has the
                        # most iterations (23+) and broadest coverage for queries.
                        logger.warning(f"[_inject_context] Invalid team '{team_val}', using default '{DEFAULT_TEAM}'")
                        args["team"] = DEFAULT_TEAM
            elif "team" in context:
                args["team"] = context["team"]
            else:
                args["team"] = DEFAULT_TEAM

        # 3. Iteration Injection
        # ⚠️ FIX: Only inject iteration from context if it was EXPLICITLY set
        # (not auto-defaulted). The context key "_iteration_explicit" indicates
        # the user actually mentioned sprint/iteration in their query.
        # This prevents silent scoping of all queries to @CurrentIteration.
        if "iteration" in context and context.get("_iteration_explicit", False):
            val = context["iteration"]
            # Support both 'iterationId' and 'iteration' args
            if "iterationId" in supported_args and "iterationId" not in args:
                args["iterationId"] = val
                print(f"[_inject_context] Injected iterationId='{val}' for {tool} (explicit)")
            elif "iteration" in supported_args and "iteration" not in args:
                args["iteration"] = val
                print(f"[_inject_context] Injected iteration='{val}' for {tool} (explicit)")
        elif "iteration" in context:
            print(f"[_inject_context] SKIPPED iteration injection for {tool}: not explicitly requested by user")

    
    async def execute(self, plan: Dict[str, Any], context: Dict[str, Any] = None) -> Dict[str, Any]:
        """
        Execute a tool based on a plan from the LLM planner.
        
        Args:
            plan: Dict with keys: action, tool, args, confidence
            context: Context dictionary for resolving missing args (project, team, etc.)
        
        Returns:
            Dict with: tool, count, items/result, success
        
        Raises:
            ValueError: If plan is invalid or tool is not supported
        """
        action = plan.get("action", "")
        tool = plan.get("tool")
        args = plan.get("args") or {}
        
        # Inject context if provided
        if context:
            self._inject_context(tool, args, context)
        
        # Create Langfuse span for MCP tool execution with rich attributes
        span = None
        # Build a stable key for duplicate detection (tool + args JSON)
        try:
            args_key = json.dumps(args, sort_keys=True, default=str)
        except Exception:
            args_key = str(args)

        mcp_request_id = f"mcp_{tool or 'unknown'}_{id(args)}"

        # If we've seen the exact (tool,args) before, mark this call as a retry/duplicate
        duplicate_of = self._recent_calls.get((tool, args_key))
        if not duplicate_of:
            # record first occurrence
            try:
                self._recent_calls[(tool, args_key)] = mcp_request_id
            except Exception:
                pass

        try:
            metadata = {
                "action": action,
                "confidence": plan.get("confidence"),
                "mcp_request_id": mcp_request_id,
                "tool_category": self._get_tool_category(tool),
                "operation": self._get_operation_type(tool, args)
            }
            if duplicate_of:
                metadata["is_retry"] = True
                metadata["retry_of"] = duplicate_of

            span = create_span(
                f"mcp_tool_{tool or 'unknown'}",
                input_data={"tool": tool, "args": args},
                metadata=metadata
            )
        except Exception as span_err:
            logger.debug(f"[ToolExecutor] Failed to create Langfuse span: {span_err}")
        
        try:
            if action != "call_tool":
                error_msg = f"ToolExecutor called with non-call_tool action: {action}"
                if span:
                    finalize_span(span, output={"error": error_msg}, status="error", level="ERROR")
                raise ValueError(error_msg)
            
            if not tool:
                error_msg = "ToolExecutor called without tool name"
                if span:
                    finalize_span(span, output={"error": error_msg}, status="error", level="ERROR")
                raise ValueError(error_msg)
            
            # Handle tool name aliases (e.g., work_get_iteration_work_items â†’ wit_get_work_items_for_iteration)
            tool_info = get_tool_info(tool)
            if tool_info and tool_info.get("alias_for"):
                original_tool = tool
                tool = tool_info["alias_for"]
                print(f"[ToolExecutor] Aliasing {original_tool} -> {tool}")
            
            # Validate tool exists in registry (or allow any tool if not in registry)
            tool_info = get_tool_info(tool)  # Re-get after potential alias resolution
            if tool_info:
                # Validate required arguments
                is_valid, error = validate_tool_args(tool, args)
                if not is_valid:
                    result = {
                        "tool": tool,
                        "success": False,
                        "error": error,
                        "count": 0,
                        "items": []
                    }
                    if span:
                        finalize_span(span, output=result, status="validation_failed", level="WARNING")
                    return result

            # ══════════════════════════════════════════════════════════════════════
            # WIQL ADAPTER REMOVED — execute_wiql skill handles WIQL directly via
            # REST API. No lossy WIQL→search_workitem translation needed.
            # ══════════════════════════════════════════════════════════════════════

            if False:  # WIQL adapter removed — keeping structure for diff clarity
                # Robust adapter: translate common WIQL clauses into search_workitem args
                wiql = args.get('wiql', '') if isinstance(args, dict) else ''
                import re
                import json as json_module
                
                area_path = None
                iteration_path = None
                work_item_types = []
                states = []
                search_text_parts = []
                project = args.get('project', 'FracPro-OPS')
                team = args.get('team')  # Optional team parameter

                # Extract AreaPath UNDER '...'
                m = re.search(r"\[System\.AreaPath\]\s+UNDER\s+'([^']+)'", wiql, re.IGNORECASE)
                if m:
                    area_path = m.group(1)
                
                # Extract IterationPath = '...'
                mit_path = re.search(r"\[System\.IterationPath\]\s*=\s*'([^']+)'", wiql, re.IGNORECASE)
                if mit_path:
                    iteration_path = mit_path.group(1)

                # Extract Work Item Type clause
                mit = re.findall(r"\[System\.WorkItemType\]\s*=\s*'([^']+)'", wiql, re.IGNORECASE)
                if mit:
                    work_item_types = [t.strip() for t in mit]

                # Extract State clause
                mst = re.findall(r"\[System\.State\]\s*=\s*'([^']+)'", wiql, re.IGNORECASE)
                if mst:
                    states = [s.strip() for s in mst]

                # Extract recent created/changed date like >= @Today - N
                mdate = re.search(r"\[System\.(CreatedDate|ChangedDate)\]\s*>=?\s*@Today\s*-\s*(\d+)", wiql, re.IGNORECASE)
                if mdate:
                    days = int(mdate.group(2))
                    search_text_parts.append(f"created:>={days}d")

                # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
                # CRITICAL FIX: Handle iteration path properly since search_workitem
                # does NOT support iterationPath filtering. We need to use the
                # wit_get_work_items_for_iteration API instead.
                # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
                if iteration_path and 'wit_get_work_items_for_iteration' in mcp_tools:
                    logger.info(f"[ToolExecutor] Iteration path detected: {iteration_path}. Using iteration-based API.")
                    
                    # Step 1: Look up the iteration GUID from iteration path
                    # Extract the iteration name (last part of the path)
                    iteration_parts = iteration_path.replace('\\', '/').split('/')
                    iteration_name = iteration_parts[-1] if iteration_parts else iteration_path
                    
                    # Get all iterations to find the matching one
                    iter_result = await self._execute_single('work_list_iterations', {
                        'project': project,
                        'depth': 5
                    })
                    
                    iteration_id = None
                    if iter_result.get('success'):
                        iter_items = iter_result.get('items', iter_result.get('value', []))
                        
                        # Helper function to find iteration by name recursively
                        def find_iteration(iterations, target_name):
                            if not iterations:
                                return None
                            for it in iterations:
                                if it.get('name') == target_name:
                                    return it.get('identifier')  # GUID
                                children = it.get('children', [])
                                if children:
                                    result = find_iteration(children, target_name)
                                    if result:
                                        return result
                            return None
                        
                        iteration_id = find_iteration(iter_items if isinstance(iter_items, list) else [iter_items], iteration_name)
                    
                    if not iteration_id:
                        logger.warning(f"[ToolExecutor] Could not find iteration ID for '{iteration_path}'. Falling back to search_workitem.")
                    else:
                        # Step 2: Get work items for this iteration
                        # Need team name - extract from area path or use default
                        if not team and area_path:
                            # Try to extract team from area path (e.g., "FracPro-OPS\\...\\XOPS 25")
                            area_parts = area_path.replace('\\', '/').split('/')
                            team = area_parts[-1] if area_parts else None
                        
                        if not team:
                            team = 'XOPS 25'  # Default team
                        
                        wi_iter_result = await self._execute_single('wit_get_work_items_for_iteration', {
                            'project': project,
                            'team': team,
                            'iterationId': iteration_id
                        })
                        
                        if wi_iter_result.get('success'):
                            raw_data = wi_iter_result.get('raw', wi_iter_result)
                            relations = []
                            if isinstance(raw_data, dict):
                                relations = raw_data.get('workItemRelations', [])
                            elif isinstance(raw_data, str):
                                try:
                                    parsed = json_module.loads(raw_data)
                                    relations = parsed.get('workItemRelations', [])
                                except:
                                    pass
                            
                            work_item_ids = [rel.get('target', {}).get('id') for rel in relations if rel.get('target', {}).get('id')]
                            
                            if work_item_ids:
                                # Step 3: Get full work item details
                                batch_result = await self._execute_single('wit_get_work_items_batch_by_ids', {
                                    'project': project,
                                    'ids': work_item_ids,
                                    'fields': [
                                        "System.Id", "System.Title", "System.WorkItemType",
                                        "System.State", "System.AssignedTo", "System.AreaPath",
                                        "System.IterationPath", "System.Parent", "System.Tags",
                                        "System.CreatedDate", "System.ChangedDate",
                                        "Microsoft.VSTS.Common.Priority",
                                        "Microsoft.VSTS.Common.StackRank",
                                        "Microsoft.VSTS.Scheduling.TargetDate",
                                    ],
                                })
                                
                                if batch_result.get('success'):
                                    items = batch_result.get('items', batch_result.get('value', []))
                                    
                                    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
                                    # DETECT "ALL" INTENT: If types/states list contains many items,
                                    # treat as "all" and skip filtering
                                    # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
                                    ALL_WORK_ITEM_TYPES = {'Bug', 'Dev Bug', 'Task', 'User Story', 'Feature', 'Initiatives', 'Test Case'}
                                    ALL_STATES = {
                                        'New', 'Ready', 'Requested', 'Scheduled', 'In Planning', 'Accepted',
                                        'Active', 'Design', 'Code Review', 'Code Complete', 'QA', 'QA Complete',
                                        'UAT', 'PRE-PROD', 'Approved for Production', 'In Progress', 'Awaiting Approvals',
                                        'Closed', 'Resolved', 'Completed', 'UAT Complete', 'Released', 'Removed',
                                        'Not a Bug', 'Requirement Bug',
                                        'On Hold', 'Issues Found', 'Reopened', 'Inactive'
                                    }
                                    
                                    # Check if workItemType filter is "all types" (4+ types means "all")
                                    effective_types = work_item_types
                                    if work_item_types and len(work_item_types) >= 4 and len(set(work_item_types) & ALL_WORK_ITEM_TYPES) >= 4:
                                        logger.info(f"[ToolExecutor] workItemTypes contains {len(work_item_types)} types - treating as 'all types', skipping filter")
                                        effective_types = None  # Skip type filter
                                    
                                    # Check if state filter is "all states" (4+ states means "all")
                                    effective_states = states
                                    if states and len(states) >= 4 and len(set(states) & ALL_STATES) >= 4:
                                        logger.info(f"[ToolExecutor] states contains {len(states)} states - treating as 'all states', skipping filter")
                                        effective_states = None  # Skip state filter
                                    
                                    # Step 4: Filter by type and state
                                    filtered_items = []
                                    for item in items:
                                        fields = item.get('fields', {})
                                        wi_type = fields.get('System.WorkItemType', '')
                                        wi_state = fields.get('System.State', '')
                                        
                                        # Check type filter
                                        type_match = not effective_types or wi_type in effective_types
                                        # Check state filter
                                        state_match = not effective_states or wi_state in effective_states
                                        # Check area path filter
                                        area_match = True
                                        if area_path:
                                            item_area = fields.get('System.AreaPath', '')
                                            area_match = item_area.startswith(area_path) or area_path in item_area
                                        
                                        if type_match and state_match and area_match:
                                            # Add top-level id
                                            item['id'] = item.get('id', fields.get('System.Id'))
                                            filtered_items.append(item)
                                    
                                    logger.info(f"[ToolExecutor] Iteration-based query: {len(work_item_ids)} total, {len(filtered_items)} after filtering (types={effective_types}, states={effective_states})")
                                    
                                    return {
                                        "tool": "wit_run_wiql_query",
                                        "success": True,
                                        "count": len(filtered_items),
                                        "items": filtered_items,
                                        "method": "iteration_api"
                                    }
                            else:
                                logger.info(f"[ToolExecutor] No work items found in iteration {iteration_name}")
                                return {
                                    "tool": "wit_run_wiql_query",
                                    "success": True,
                                    "count": 0,
                                    "items": [],
                                    "method": "iteration_api"
                                }

                # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
                # Fallback: Use search_workitem for non-iteration queries
                # NOTE: Do NOT pass iterationPath as it's not supported by the API!
                # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
                if 'search_workitem' in mcp_tools:
                    # FIX: Use "a OR e OR i OR o OR u" as "match all" pattern since "*" doesn't work
                    # for Azure DevOps Search API. This matches items containing any vowel (virtually all).
                    # If we have type/state filters, use those in searchText for better results.
                    default_search = "a OR e OR i OR o OR u"
                    if work_item_types:
                        # If we know the type, search for that type name
                        default_search = f"workitemtype:{work_item_types[0]}"
                    elif states:
                        # If we know the state, search for that state
                        default_search = f"state:{states[0]}"
                    
                    search_args = {"searchText": default_search, "project": [project]}
                    if area_path:
                        search_args['areaPath'] = [area_path]
                    # NOTE: Do NOT add iterationPath - it's not supported by search_workitem!
                    # if iteration_path:
                    #     search_args['iterationPath'] = [iteration_path]
                    if work_item_types:
                        search_args['workItemType'] = work_item_types
                    if states:
                        search_args['state'] = states
                    if search_text_parts:
                        search_args['searchText'] = " ".join([search_args.get('searchText', default_search)] + search_text_parts)

                    logger.info(f"[ToolExecutor] Translating WIQL to search_workitem: area={area_path}, iteration={iteration_path} (ignored), types={work_item_types}, states={states}, extras={search_text_parts}")
                    
                    # Execute search_workitem
                    result = await self._execute_single('search_workitem', search_args)
                    
                    # FIX: Normalize search results to add top-level 'id' field from fields.system.id
                    # This is critical because search_workitem returns lowercase 'system.id' in fields
                    if result.get('success'):
                        items = result.get('items', result.get('value', []))
                        normalized_items = []
                        for item in items:
                            if isinstance(item, dict):
                                fields = item.get('fields', {})
                                fields_lower = {k.lower(): v for k, v in fields.items()} if fields else {}
                                
                                # Extract numeric ID from fields (case-insensitive)
                                work_item_id = item.get('id')
                                if not work_item_id:
                                    work_item_id = fields_lower.get('system.id') or fields.get('System.Id')
                                
                                # Add id at top level for synthesizer
                                if work_item_id:
                                    try:
                                        item['id'] = int(work_item_id)
                                    except (ValueError, TypeError):
                                        item['id'] = work_item_id
                                
                                normalized_items.append(item)
                        
                        result['items'] = normalized_items
                        result['count'] = len(normalized_items)
                        logger.info(f"[ToolExecutor] Normalized {len(normalized_items)} items from WIQL->search_workitem translation")
                    
                    return result
                else:
                    print(f"[ToolExecutor] WIQL adapter unable to find 'search_workitem' tool to fall back to")
                    return {"tool": tool, "success": False, "error": "Connector missing WIQL runner and no search fallback available", "count": 0, "items": []}
            
            # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
            # ARGUMENT NORMALIZATION: Fix common LLM parameter format issues
            # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
            # search_workitem requires project, areaPath, workItemType, state as arrays
            if tool == 'search_workitem':
                array_fields = ['project', 'areaPath', 'workItemType', 'state', 'assignedTo']
                for field in array_fields:
                    if field in args:
                        value = args[field]
                        if isinstance(value, str):
                            # Handle comma-separated strings (from normalize_arg_value_with_enum collapsing lists)
                            if ',' in value:
                                args[field] = [v.strip() for v in value.split(',') if v.strip()]
                                logger.debug(f"[ToolExecutor] Normalized {field} from comma-separated string to array: {args[field]}")
                            else:
                                args[field] = [value]
                                logger.debug(f"[ToolExecutor] Normalized {field} from string to array: [{value}]")
                # Ensure top is an integer and always present for comprehensive results
                if 'top' in args:
                    if isinstance(args['top'], str):
                        try:
                            args['top'] = int(args['top'])
                        except ValueError:
                            args['top'] = 200  # Default on parse error
                    # FIX: LLM often confuses "top N areas" with "return N items".
                    # search_workitem needs enough raw items for grouping/aggregation.
                    # Enforce a minimum of 200 so synthesizer gets data to work with.
                    if isinstance(args['top'], int) and args['top'] < 200:
                        logger.info(f"[ToolExecutor] Overriding low top={args['top']} to 200 for search_workitem (need enough data for aggregation)")
                        args['top'] = 200
                else:
                    # CRITICAL FIX: LLM planners often omit 'top', causing ADO Search API
                    # to return only its default page size (~10 items). Always set a
                    # reasonable default to get comprehensive results.
                    args['top'] = 200
                    logger.info("[ToolExecutor] Injected default top=200 for search_workitem (LLM omitted it)")
            
            # Execute with pagination if supported
            supports_paging = supports_pagination(tool) if tool_info else False
            
            if supports_paging:
                result = await self._execute_with_pagination(tool, args)
            else:
                result = await self._execute_single(tool, args)
            
            # Normalize work item IDs: extract numeric ID from fields (prevent GUID display)
            if tool == 'search_workitem' and result.get('success'):
                items = result.get('items', result.get('value', []))
                normalized_items = []
                
                # Data quality tracking
                missing_ids = []
                missing_titles = []
                missing_fields = []
                
                logger.debug(f"[ToolExecutor] Normalizing IDs for {len(items)} items from search_workitem")
                
                for idx, item in enumerate(items):
                    if isinstance(item, dict):
                        # Log first item structure for debugging
                        if idx == 0:
                            import os
                            debug_file = os.path.join(os.path.dirname(__file__), "debug_search_item.json")
                            try:
                                with open(debug_file, "w") as f:
                                    json.dump(item, f, default=str, indent=2)
                                logger.info(f"[ToolExecutor] DEBUG - First item saved to {debug_file}")
                            except Exception as e:
                                logger.warning(f"[ToolExecutor] Could not save debug file: {e}")
                            logger.info(f"[ToolExecutor] First item keys: {list(item.keys())}")
                            logger.info(f"[ToolExecutor] First item fields keys: {list(item.get('fields', {}).keys())[:20]}")
                        
                        # DATA QUALITY CHECK: Verify item has fields object
                        fields = item.get('fields', {})
                        if not fields or not isinstance(fields, dict):
                            logger.warning(f"[ToolExecutor] Item {idx} has no fields object - data may be incomplete")
                            missing_fields.append(idx)
                            # Don't skip - may have top-level fields
                        
                        # IMPORTANT: Search API returns lowercase field names (system.id)
                        # while other APIs return PascalCase (System.Id)
                        # Create case-insensitive field lookup
                        fields_lower = {k.lower(): v for k, v in fields.items()} if fields else {}
                        
                        # Extract numeric ID from fields or top-level id
                        work_item_id = item.get('id')
                        
                        # If no direct id, check fields (case-insensitive)
                        if not work_item_id:
                            work_item_id = fields_lower.get('system.id') or fields.get('System.Id')
                        
                        # If still no ID, try workItemId but validate it's numeric
                        if not work_item_id:
                            work_item_id_raw = item.get('workItemId')
                            if work_item_id_raw:
                                try:
                                    work_item_id = int(work_item_id_raw)
                                except (ValueError, TypeError):
                                    # It's a GUID - this is expected for search_workitem
                                    # Keep the item but log that we couldn't extract numeric ID
                                    logger.debug(f"[ToolExecutor] Item {idx} has GUID workItemId={work_item_id_raw}, no numeric ID found in fields")
                                    # Still add item but without numeric ID (let synthesizer handle it)
                                    normalized_items.append(item)
                                    continue
                        
                        # DATA QUALITY CHECK: Track items missing IDs
                        if not work_item_id:
                            missing_ids.append(idx)
                            # Try to get title for logging (case-insensitive)
                            title_for_log = fields_lower.get('system.title', fields.get('System.Title', 'unknown'))
                            logger.warning(f"[ToolExecutor] Item {idx} has no numeric ID - title: {title_for_log[:50]}")
                        
                        # DATA QUALITY CHECK: Verify item has title (case-insensitive)
                        title = fields_lower.get('system.title') or fields.get('System.Title', '')
                        if not title or title == 'unknown':
                            missing_titles.append(idx)
                            logger.warning(f"[ToolExecutor] Item {idx} missing System.Title (ID={work_item_id})")
                        
                        # Ensure id is accessible at top level for synthesizer
                        if work_item_id:
                            item['id'] = int(work_item_id) if not isinstance(work_item_id, int) else work_item_id
                        
                        # NORMALIZE FIELD NAMES: search_workitem returns lowercase
                        # (system.state) but synthesizer expects PascalCase (System.State).
                        # Map common lowercase fields to PascalCase.
                        if fields_lower and fields:
                            FIELD_NAME_MAP = {
                                'system.id': 'System.Id',
                                'system.title': 'System.Title',
                                'system.workitemtype': 'System.WorkItemType',
                                'system.state': 'System.State',
                                'system.assignedto': 'System.AssignedTo',
                                'system.tags': 'System.Tags',
                                'system.createddate': 'System.CreatedDate',
                                'system.changeddate': 'System.ChangedDate',
                                'system.description': 'System.Description',
                                'system.history': 'System.History',
                                'system.areapath': 'System.AreaPath',
                                'system.iterationpath': 'System.IterationPath',
                                'system.rev': 'System.Rev',
                                'system.reason': 'System.Reason',
                                'system.createdby': 'System.CreatedBy',
                                'system.changedby': 'System.ChangedBy',
                                'microsoft.vsts.common.priority': 'Microsoft.VSTS.Common.Priority',
                                'microsoft.vsts.common.severity': 'Microsoft.VSTS.Common.Severity',
                                'microsoft.vsts.scheduling.storypoints': 'Microsoft.VSTS.Scheduling.StoryPoints',
                                'microsoft.vsts.scheduling.effort': 'Microsoft.VSTS.Scheduling.Effort',
                                'microsoft.vsts.scheduling.remainingwork': 'Microsoft.VSTS.Scheduling.RemainingWork',
                                'microsoft.vsts.scheduling.completedwork': 'Microsoft.VSTS.Scheduling.CompletedWork',
                            }
                            normalized_fields = {}
                            for k, v in fields.items():
                                pascal_key = FIELD_NAME_MAP.get(k.lower(), k)
                                normalized_fields[pascal_key] = v
                            item['fields'] = normalized_fields
                        
                        # Always append - but with quality metrics
                        normalized_items.append(item)
                        
                        if work_item_id:
                            logger.debug(f"[ToolExecutor] Item {idx} normalized with ID={work_item_id}")
                
                result['items'] = normalized_items
                result['count'] = len(normalized_items)
                
                # DATA QUALITY REPORT
                quality_pct = ((len(normalized_items) - len(missing_ids)) / len(normalized_items) * 100) if normalized_items else 0
                logger.info(f"[ToolExecutor] Normalized {len(normalized_items)} work items (quality: {quality_pct:.1f}% have IDs)")
                
                if missing_ids:
                    logger.warning(f"[ToolExecutor] âš ï¸ Data Quality Issue: {len(missing_ids)} items missing IDs (indices: {missing_ids[:10]}{'...' if len(missing_ids) > 10 else ''})")
                    result['data_quality_issues'] = {'missing_ids': len(missing_ids), 'indices': missing_ids[:20]}
                
                if missing_titles:
                    logger.warning(f"[ToolExecutor] âš ï¸ Data Quality Issue: {len(missing_titles)} items missing titles (indices: {missing_titles[:10]}{'...' if len(missing_titles) > 10 else ''})")
                    if 'data_quality_issues' not in result:
                        result['data_quality_issues'] = {}
                    result['data_quality_issues']['missing_titles'] = len(missing_titles)
                
                if missing_fields:
                    logger.warning(f"[ToolExecutor] âš ï¸ Data Quality Issue: {len(missing_fields)} items missing fields object")
                    if 'data_quality_issues' not in result:
                        result['data_quality_issues'] = {}
                    result['data_quality_issues']['missing_fields'] = len(missing_fields)
            
            # Finalize span with enriched result attributes
            if span:
                status = "success" if result.get("success") else "error"
                level = "ERROR" if not result.get("success") else None
                
                # Build enriched output with detailed attributes
                output_data = {
                    "success": result.get("success"),
                    "count": result.get("count"),
                    "mcp_request_id": mcp_request_id,
                    "retry_count": result.get("retry_count", 0),
                    "pages": result.get("pages"),
                }
                
                # Add error details if failed
                if not result.get("success"):
                    output_data["error"] = result.get("error")
                    output_data["http_status_code"] = result.get("http_status_code")
                
                # Add enrichment details if present
                if "failed_ids" in result:
                    output_data["failed_ids"] = result.get("failed_ids", [])
                    output_data["failed_count"] = len(result.get("failed_ids", []))
                    output_data["mcp_errors"] = result.get("mcp_errors", [])
                
                finalize_span(span, output=output_data, status=status, level=level)
            
            return result
            
        except Exception as e:
            error_msg = str(e)
            print(f"[ToolExecutor] Error executing {tool}: {error_msg}")
            result = {
                "tool": tool,
                "success": False,
                "error": error_msg,
                "count": 0,
                "items": []
            }
            if span:
                finalize_span(span, output=result, status="exception", level="ERROR")
            return result
    
    async def _execute_single(self, tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a tool that doesn't support pagination."""
        # ── AREA PATH GUARD for search_workitem ──────────────────────────────
        # The ADO Search API is unreliable with non-hierarchical area path values
        # (e.g., team names like "XOPS 25"). When the LLM planner auto-injects a
        # team name as areaPath, the API returns 0 results. Fix: remove areaPath
        # from the API call and use it as a client-side post-filter on enriched
        # items (which have proper System.AreaPath).
        _saved_area_path = None
        if tool == 'search_workitem' and 'areaPath' in args:
            _saved_area_path = args.pop('areaPath')
            logger.info(f"[ToolExecutor] Moved search_workitem areaPath={_saved_area_path} to post-filter (not sent to API)")
        
        # ── SEARCH TEXT WILDCARD GUARD ───────────────────────────────────────
        # ADO Search API does not support wildcard-only searchText like "*".
        # It returns 0 results. Replace with a broad match pattern that returns
        # virtually all work items (any item containing a vowel).
        if tool == 'search_workitem':
            st = args.get('searchText', '')
            if isinstance(st, str) and st.strip() in ('*', '**', '', '.', '.*'):
                _broad_search = "a OR e OR i OR o OR u"
                logger.info(f"[ToolExecutor] Replaced unsupported searchText={st!r} with broad pattern: {_broad_search}")
                args['searchText'] = _broad_search
        
        # ── FIX #22 + FIX #28: SMART searchText → STATE POST-FILTER ────────────
        # When the planner uses searchText=(state name) instead of a state filter,
        # the search API does a TEXT search. FIX #28: We keep searchText as-is
        # (the state name) so text search finds relevant items, and apply state
        # as a POST-FILTER only. The ADO search API's state param doesn't work
        # reliably for all states (e.g. "Blocked" returns 0 results).
        _fix22_state_filter = None
        if tool == 'search_workitem':
            _KNOWN_STATES = {
                'blocked', 'active', 'resolved', 'closed', 'done', 'new',
                'committed', 'completed', 'in progress', 'not started',
                'approved for production', 'design', 'code review',
                'qa', 'qa complete', 'uat', 'pre-prod', 'scheduled',
                'ready', 'requested', 'in planning', 'accepted',
            }
            _st_val = (args.get('searchText') or '').strip()
            _st_lower = _st_val.lower()
            if _st_lower in _KNOWN_STATES and not args.get('state'):
                # FIX #28: Save state for POST-FILTER only — don't send to MCP API
                _fix22_state_filter = [_st_val]
                logger.info(f"[ToolExecutor] FIX #28: searchText='{_st_val}' is a known state — will post-filter by state (not sent to API)")
                # Keep searchText as-is — text search will find items containing the state name
        
        # ── PARAMETER SANITIZATION ───────────────────────────────────────────
        # LLM planners sometimes embed metadata objects (analysis_criteria,
        # depends_on, description, step_id) inside tool args. These cause MCP
        # schema validation failures (additionalProperties: false). Strip them.
        _non_tool_keys = {"analysis_criteria", "depends_on", "description", "step_id", "confidence", "query_hash", "daysInState"}
        _stripped = {k for k in list(args.keys()) if k in _non_tool_keys or (isinstance(args.get(k), dict) and k not in ('fields',))}
        for k in _stripped:
            args.pop(k, None)
        if _stripped:
            logger.info(f"[ToolExecutor] Stripped non-tool params from {tool} args: {_stripped}")
        
        try:
            print(f"[ToolExecutor DEBUG] Calling MCP tool={tool} with args={json.dumps(args, default=str)[:500]}")
            response_str = await self.mcp.call_tool(tool, args)
        except Exception as e:
            err_text = str(e)
            # If MCP reports tool not found, try to find a close match in the connector's tool list
            if "not found" in err_text.lower() or "tool" in err_text.lower() and "not" in err_text.lower():
                try:
                    available = list(getattr(self.mcp, 'tools_cache', {}).keys())
                    if available:
                        # CRITICAL FIX: First check for exact match (case-insensitive)
                        exact_match = None
                        for avail_tool in available:
                            if avail_tool.lower() == tool.lower():
                                exact_match = avail_tool
                                break
                        
                        if exact_match:
                            print(f"[ToolExecutor] Found exact match (case): {tool} â†’ {exact_match}")
                            try:
                                response_str = await self.mcp.call_tool(exact_match, args)
                                tool = exact_match
                            except Exception as e2:
                                return {
                                    "tool": tool,
                                    "success": False,
                                    "error": f"Tool call failed after exact match: {e2}",
                                    "count": 0,
                                    "items": []
                                }
                        else:
                            # CRITICAL FIX: Stricter fuzzy matching (0.85 cutoff) to avoid bad aliases
                            matches = difflib.get_close_matches(tool, available, n=1, cutoff=0.85)
                            if matches:
                                alt = matches[0]
                                print(f"[ToolExecutor] MCP reported tool missing: {tool}. Retrying with close match: {alt}")
                                # Validate schemas match before aliasing
                                if self._validate_alias_compatible(tool, alt, args):
                                    try:
                                        # Map parameters if needed (e.g., id â†’ workItemId)
                                        mapped_args = self._map_parameters(tool, alt, args)
                                        response_str = await self.mcp.call_tool(alt, mapped_args)
                                        tool = alt
                                    except Exception as e2:
                                        return {
                                            "tool": tool,
                                            "success": False,
                                            "error": f"Tool call failed after alias attempt: {e2}",
                                            "count": 0,
                                            "items": []
                                        }
                                else:
                                    print(f"[ToolExecutor] Alias {alt} rejected: incompatible schema with {tool}")
                                    return {"tool": tool, "success": False, "error": f"No compatible tool found for {tool} (alias {alt} has incompatible schema)", "count": 0, "items": []}
                            else:
                                # No fuzzy matches found
                                return {"tool": tool, "success": False, "error": err_text, "count": 0, "items": []}
                    else:
                        return {"tool": tool, "success": False, "error": err_text, "count": 0, "items": []}
                except Exception:
                    return {"tool": tool, "success": False, "error": err_text, "count": 0, "items": []}
            else:
                return {"tool": tool, "success": False, "error": err_text, "count": 0, "items": []}
        
        # Handle textual "tool not found" responses from MCP and attempt alias fallback
        # Debug: show raw response string for troubleshooting
        try:
            print(f"[ToolExecutor DEBUG] raw response_str: {repr(response_str)[:500]}")
        except Exception:
            pass

        try:
            lower_resp = response_str.lower() if isinstance(response_str, str) else ""
        except Exception:
            lower_resp = ""

        if "not found" in lower_resp and "tool" in lower_resp:
            # Try to find a close match in the MCP connector's available tools and retry
            try:
                available = list(getattr(self.mcp, 'tools_cache', {}).keys())
                if available:
                    # CRITICAL FIX: First check for exact match (case-insensitive)
                    exact_match = None
                    for avail_tool in available:
                        if avail_tool.lower() == tool.lower():
                            exact_match = avail_tool
                            break
                    
                    if exact_match:
                        print(f"[ToolExecutor] Found exact match (case): {tool} â†’ {exact_match}")
                        try:
                            response_str = await self.mcp.call_tool(exact_match, args)
                            tool = exact_match
                        except Exception as e2:
                            return {
                                "tool": tool,
                                "success": False,
                                "error": f"Tool call failed after exact match: {e2}",
                                "count": 0,
                                "items": []
                            }
                    else:
                        # CRITICAL FIX: Stricter fuzzy matching (0.85 cutoff)
                        matches = difflib.get_close_matches(tool, available, n=1, cutoff=0.85)
                        if matches:
                            alt = matches[0]
                            print(f"[ToolExecutor] MCP returned 'tool not found' for {tool}. Retrying with close match: {alt}")
                            # Validate schemas match before aliasing
                            if self._validate_alias_compatible(tool, alt, args):
                                try:
                                    # Map parameters if needed
                                    mapped_args = self._map_parameters(tool, alt, args)
                                    response_str = await self.mcp.call_tool(alt, mapped_args)
                                    tool = alt
                                except Exception as e2:
                                    return {
                                        "tool": tool,
                                        "success": False,
                                        "error": f"Tool call failed after alias attempt: {e2}",
                                        "count": 0,
                                        "items": []
                                    }
                            else:
                                print(f"[ToolExecutor] Alias {alt} rejected: incompatible schema with {tool}")
                                return {"tool": tool, "success": False, "error": f"No compatible tool found for {tool}", "count": 0, "items": []}
                        else:
                            # No fuzzy matches - log available tools and their input schemas to help connector mapping
                            try:
                                print(f"[ToolExecutor DEBUG] Available MCP tools ({len(available)}): {available[:20]}")
                                # Show inputSchema for top close matches or requested tool if known
                                schema_info = {}
                                for t in available[:20]:
                                    meta = MCP_TOOL_REGISTRY.get(t)
                                    if meta and 'inputSchema' in meta:
                                        schema_info[t] = meta.get('inputSchema')
                                if schema_info:
                                    print(f"[ToolExecutor DEBUG] Tool input schemas (sample): {list(schema_info.keys())}")
                            except Exception:
                                pass
                            return {"tool": tool, "success": False, "error": response_str, "count": 0, "items": []}
                else:
                        try:
                            available = list(getattr(self.mcp, 'tools_cache', {}).keys())
                            print(f"[ToolExecutor DEBUG] No close match for '{tool}'. Available tools: {available[:30]}")
                            # Print schemas for first N tools to aid mapping
                            schema_map = {t: MCP_TOOL_REGISTRY.get(t, {}).get('inputSchema') for t in available[:30]}
                            print(f"[ToolExecutor DEBUG] Sample tool schemas: { {k:v for k,v in schema_map.items() if v is not None} }")
                        except Exception:
                            pass
                        return {"tool": tool, "success": False, "error": response_str, "count": 0, "items": []}
            except Exception:
                return {"tool": tool, "success": False, "error": response_str, "count": 0, "items": []}

        # Handle None response from MCP
        if response_str is None:
            print(f"[ToolExecutor] MCP returned None for {tool}")
            return {
                "tool": tool,
                "success": False,
                "error": "MCP server returned empty response",
                "count": 0,
                "items": []
            }
        
        # Check for error responses FIRST (before JSON parsing)
        # FIX #26: Only check first 500 chars to avoid false positives on large valid responses  
        _resp_head = response_str[:500] if isinstance(response_str, str) else str(response_str)[:500]
        if self._is_error_response(response_str) or (_resp_head.strip().startswith("Error") or '"Message":' in _resp_head):
            print(f"[ToolExecutor] Error response detected: {response_str[:200]}")
            return {
                "tool": tool,
                "success": False,
                "error": response_str,
                "count": 0,
                "items": []
            }
        
        # Parse response
        try:
            response = json.loads(response_str)
        except json.JSONDecodeError:
            # Not JSON, return as raw result
            return {
                "tool": tool,
                "success": True,
                "result": response_str,
                "count": 1,
                "items": [response_str]
            }

        # Detect ADO-style error payloads encoded as JSON (value: { Message: ... })
        try:
            if isinstance(response, dict):
                value = response.get('value') or response.get('results') or response.get('items')
                if isinstance(value, dict) and any(k in value for k in ("Message", "message", "error")):
                    # Treat as an error response instead of valid items
                    err_text = json.dumps(response)
                    return {"tool": tool, "success": False, "error": err_text, "count": 0, "items": []}
        except Exception:
            pass
        
        # Handle JSON null response
        if response is None:
            print(f"[ToolExecutor] MCP returned JSON null for {tool}")
            return {
                "tool": tool,
                "success": True,
                "count": 0,
                "items": [],
                "raw": None
            }
        
        # Normalize response
        items = self._extract_items(response)
        
        # ── FIX #28b: RETRY search_workitem WITHOUT STATE IF 0 RESULTS ──────
        # The ADO search API doesn't reliably honor the state param for all
        # values (e.g. "Blocked" returns 0 while items in that state exist).
        # If we got 0 results and a state filter was active, retry without it
        # and save the state for post-filtering after enrichment.
        _saved_state_for_retry = None
        if tool == 'search_workitem' and len(items) == 0 and args.get('state'):
            _saved_state_for_retry = args.pop('state')
            logger.info(f"[ToolExecutor] FIX #28b: 0 results with state={_saved_state_for_retry} — retrying without state filter")
            try:
                print(f"[ToolExecutor DEBUG] Retry MCP tool={tool} without state, args={json.dumps(args, default=str)[:500]}")
                _retry_str = await self.mcp.call_tool(tool, args)
                _retry_resp = json.loads(_retry_str) if _retry_str else None
                if _retry_resp is not None:
                    items = self._extract_items(_retry_resp)
                    response = _retry_resp
                    # Extract top-level IDs for search results (same as above)
                    if len(items) > 0 and isinstance(items[0], dict) and 'id' not in items[0]:
                        for item in items:
                            if isinstance(item, dict) and 'id' not in item:
                                _flds = item.get('fields', {})
                                if isinstance(_flds, dict):
                                    _rid = _flds.get('system.id') or _flds.get('System.Id')
                                    if _rid:
                                        try: item['id'] = int(_rid)
                                        except (ValueError, TypeError): item['id'] = _rid
                        logger.info(f"[ToolExecutor] Extracted top-level IDs for {len(items)} results (retry)")
                    logger.info(f"[ToolExecutor] FIX #28b: Retry found {len(items)} items (will post-filter by state={_saved_state_for_retry})")
            except Exception as e:
                logger.warning(f"[ToolExecutor] FIX #28b: Retry failed: {e}")
        
        # ══════════════════════════════════════════════════════════════════════════════
        # ENRICH WORK ITEMS: If items only have ID (no fields), fetch full details
        # This is needed for wit_get_work_items_for_iteration which returns
        # workItemRelations with target objects that only contain id and url.
        # Without enrichment, the synthesizer has no System.State/Title/Type data
        # and cannot reason about burn-down rates, velocity, or any state-based analysis.
        # Also enriches search_workitem results which lack AreaPath, Priority, etc.
        # ══════════════════════════════════════════════════════════════════════════════
        
        # PRE-STEP: For search_workitem results, extract top-level 'id' from
        # fields.system.id. Search API nests IDs inside fields, but the enrichment
        # and post-filter sections require a top-level 'id' key.
        if tool == 'search_workitem' and len(items) > 0 and isinstance(items[0], dict) and 'id' not in items[0]:
            for item in items:
                if isinstance(item, dict) and 'id' not in item:
                    flds = item.get('fields', {})
                    if isinstance(flds, dict):
                        raw_id = flds.get('system.id') or flds.get('System.Id')
                        if raw_id:
                            try:
                                item['id'] = int(raw_id)
                            except (ValueError, TypeError):
                                item['id'] = raw_id
            logger.info(f"[ToolExecutor] Extracted top-level IDs for {len(items)} search_workitem results")
        
        print(f"[CHECKPOINT-A] Reached enrichment section for tool: {tool}")
        _enrichable_tools = ("wit_get_work_items_for_iteration", "work_get_iteration_work_items", "search_workitem")
        if tool in _enrichable_tools and len(items) > 0 and isinstance(items[0], dict) and 'id' in items[0]:
            logger.info(f"[ToolExecutor] Checking enrichment for tool: {tool}, items_count: {len(items)}")
            # Iteration tools: enrich when items have no fields at all
            # Search tools: enrich when items lack critical fields (AreaPath, Priority)
            if tool in ("wit_get_work_items_for_iteration", "work_get_iteration_work_items"):
                items_need_enrichment = 'fields' not in items[0]
            else:
                # search_workitem: check if items lack AreaPath (common analysis field)
                first_fields = items[0].get('fields', {})
                items_need_enrichment = (
                    isinstance(first_fields, dict) and
                    'System.AreaPath' not in first_fields and
                    'system.areapath' not in first_fields
                )
            logger.info(f"[ToolExecutor] _execute_single enrichment check: tool={tool}, items={len(items)}, needs_enrichment={items_need_enrichment}")
            if items_need_enrichment:
                project = args.get('project', '') or 'FracPro-OPS'
                # Unwrap list form (search_workitem sends project as list per API schema)
                if isinstance(project, list):
                    project = project[0] if project else 'FracPro-OPS'
                logger.info(f"[ToolExecutor] Enriching {len(items)} work items with full details (from _execute_single)...")
                # Build a lookup of original items by ID for partial-enrichment fallback
                original_items_by_id = {}
                for item in items:
                    item_id = None
                    if isinstance(item, dict):
                        item_id = item.get('id') or (item.get('fields') or {}).get('System.Id') or (item.get('fields') or {}).get('system.id')
                    if item_id:
                        original_items_by_id[str(item_id)] = item
                
                enrichment_result = await self._enrich_work_items(project, items)
                if enrichment_result and enrichment_result.get('enriched_items'):
                    enriched_items = enrichment_result['enriched_items']
                    # Only use enriched items if they actually have fields
                    if enriched_items and isinstance(enriched_items[0], dict) and 'fields' in enriched_items[0]:
                        # Merge enriched items with originals for any that failed enrichment
                        enriched_ids = set()
                        for ei in enriched_items:
                            eid = ei.get('id') or (ei.get('fields') or {}).get('System.Id')
                            if eid:
                                enriched_ids.add(str(eid))
                        
                        # Add back original items that weren't enriched
                        # IMPORTANT: Normalize lowercase field keys to PascalCase
                        # because the post-normalization block only checks the first item.
                        # Since enriched items (PascalCase) come first, un-enriched
                        # originals (lowercase from search API) would be skipped.
                        _FIELD_NAME_MAP = {
                            'system.id': 'System.Id', 'system.title': 'System.Title',
                            'system.workitemtype': 'System.WorkItemType', 'system.state': 'System.State',
                            'system.assignedto': 'System.AssignedTo', 'system.tags': 'System.Tags',
                            'system.createddate': 'System.CreatedDate', 'system.changeddate': 'System.ChangedDate',
                            'system.description': 'System.Description', 'system.history': 'System.History',
                            'system.areapath': 'System.AreaPath', 'system.iterationpath': 'System.IterationPath',
                            'system.rev': 'System.Rev', 'system.reason': 'System.Reason',
                            'system.createdby': 'System.CreatedBy', 'system.changedby': 'System.ChangedBy',
                            'microsoft.vsts.common.priority': 'Microsoft.VSTS.Common.Priority',
                            'microsoft.vsts.common.severity': 'Microsoft.VSTS.Common.Severity',
                            'microsoft.vsts.common.statechangedate': 'Microsoft.VSTS.Common.StateChangeDate',
                            'microsoft.vsts.scheduling.storypoints': 'Microsoft.VSTS.Scheduling.StoryPoints',
                        }
                        unenriched_originals = []
                        for oid, orig_item in original_items_by_id.items():
                            if oid not in enriched_ids:
                                # Normalize lowercase field keys to PascalCase
                                if isinstance(orig_item, dict) and 'fields' in orig_item:
                                    orig_fields = orig_item['fields']
                                    first_key = next(iter(orig_fields), '')
                                    if isinstance(first_key, str) and first_key == first_key.lower() and '.' in first_key:
                                        normalized = {}
                                        for k, v in orig_fields.items():
                                            normalized[_FIELD_NAME_MAP.get(k.lower(), k)] = v
                                        orig_item['fields'] = normalized
                                    # ── FIX #23a: Add top-level 'id' for consistency with enriched items
                                    if 'id' not in orig_item and 'fields' in orig_item:
                                        _fid = orig_item['fields'].get('System.Id') or orig_item['fields'].get('system.id')
                                        if _fid:
                                            try:
                                                orig_item['id'] = int(_fid)
                                            except (ValueError, TypeError):
                                                orig_item['id'] = _fid
                                    # ── FIX #23b: Placeholder AreaPath for items lacking it
                                    # Search API doesn't return areapath; without it
                                    # the synthesizer may hallucinate area names.
                                    if 'fields' in orig_item:
                                        _f = orig_item['fields']
                                        if 'System.AreaPath' not in _f and 'system.areapath' not in _f:
                                            _f['System.AreaPath'] = 'FracPro-OPS (area not enriched)'
                                unenriched_originals.append(orig_item)
                        
                        if unenriched_originals:
                            items = enriched_items + unenriched_originals
                            logger.info(f"[ToolExecutor] Enriched {len(enriched_items)} + kept {len(unenriched_originals)} original items = {len(items)} total")
                        else:
                            items = enriched_items
                            logger.info(f"[ToolExecutor] Enriched {len(items)} work items with full details")
                    else:
                        logger.warning(f"[ToolExecutor] Enrichment returned items without fields, keeping original")
                    if enrichment_result.get('failed_ids'):
                        logger.warning(f"[ToolExecutor] Warning: {len(enrichment_result['failed_ids'])} items failed enrichment")
            
            # POST-FILTER: Apply workItemType/state/assignedTo filters after enrichment
            filters_to_apply = {}
            
            def _parse_filter_value(val):
                """Parse filter value into list, handling lists and comma-separated strings."""
                if isinstance(val, list):
                    return [t.strip() for t in val]
                elif isinstance(val, str):
                    # Handle comma-separated strings (e.g., "Bug,Task,User Story")
                    if ',' in val:
                        return [t.strip() for t in val.split(',') if t.strip()]
                    return [val.strip()]
                return []
            
            if args.get('workItemType'):
                filters_to_apply['workItemType'] = _parse_filter_value(args['workItemType'])
            # State filter (priority order: retry-saved > fix22-injected > args)
            if _saved_state_for_retry:
                filters_to_apply['state'] = _parse_filter_value(_saved_state_for_retry)
                logger.info(f"[ToolExecutor] FIX #28b: Applied post-filter state={_saved_state_for_retry} from retry fallback")
            elif _fix22_state_filter and 'state' not in filters_to_apply:
                filters_to_apply['state'] = _fix22_state_filter
                logger.info(f"[ToolExecutor] FIX #28: Applied post-filter state={_fix22_state_filter} from searchText conversion")
            elif args.get('state'):
                filters_to_apply['state'] = _parse_filter_value(args['state'])
            if args.get('assignedTo'):
                filters_to_apply['assignedTo'] = _parse_filter_value(args['assignedTo'])
            # Restore saved areaPath for post-filter (was popped before API call)
            if _saved_area_path is not None:
                filters_to_apply['areaPath'] = _parse_filter_value(_saved_area_path)
            elif args.get('areaPath'):
                filters_to_apply['areaPath'] = _parse_filter_value(args['areaPath'])
            # Check for unassigned filter (special case) - same logic as _execute_with_pagination
            _unassigned_check = args.get('unassigned')
            _unassigned_str_check = 'unassigned' in str(args).lower()
            if _unassigned_check is True or _unassigned_str_check:
                filters_to_apply['unassigned'] = True
            # ── FIX #24: STATE ALIAS EXPANSION ─────────────────────────────
            # When planner asks for "Resolved" bugs, ADO may have them in
            # Closed/Done states. Expand single terminal-state filters.
            _STATE_ALIASES = {
                'resolved': ['Resolved', 'Closed', 'Done'],
                'closed':   ['Closed', 'Resolved', 'Done'],
                'done':     ['Done', 'Closed', 'Resolved', 'Completed'],
                'completed':['Completed', 'Closed', 'Done', 'Resolved'],
            }
            if 'state' in filters_to_apply and len(filters_to_apply['state']) == 1:
                _sole = filters_to_apply['state'][0].strip()
                _expansion = _STATE_ALIASES.get(_sole.lower())
                if _expansion:
                    logger.info(f"[ToolExecutor] FIX #24: Expanded single state filter '{_sole}' → {_expansion}")
                    filters_to_apply['state'] = _expansion

            if filters_to_apply:
                pre_filter_items = list(items)  # Save for fallback
                pre_filter_count = len(items)
                items = self._apply_post_filters(items, filters_to_apply)
                post_filter_count = len(items)
                logger.info(f"[ToolExecutor] POST-FILTER (_execute_single): {pre_filter_count} → {post_filter_count} items (filters: {filters_to_apply})")
                
                # ── FIX #21: AREAPATH FALLBACK ──────────────────────────────
                # If areaPath filter zeroed out ALL results, the planner likely
                # injected an incorrect area path (e.g., a team name that doesn't
                # match any actual area path). Retry without areaPath so the
                # synthesizer can still group/analyze the data.
                if post_filter_count == 0 and pre_filter_count > 0 and 'areaPath' in filters_to_apply:
                    fallback_filters = {k: v for k, v in filters_to_apply.items() if k != 'areaPath'}
                    if fallback_filters:
                        items = self._apply_post_filters(pre_filter_items, fallback_filters)
                    else:
                        items = pre_filter_items
                    logger.warning(f"[ToolExecutor] FIX #21: areaPath filter '{filters_to_apply['areaPath']}' eliminated all {pre_filter_count} items — retried without areaPath, got {len(items)} items")
        
        return {
            "tool": tool,
            "success": True,
            "count": len(items),
            "items": items,
            "raw": response
        }
    
    async def _execute_with_pagination(self, tool: str, args: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a tool with automatic pagination handling."""
        all_items = []
        continuation_token = None
        page_count = 0
        max_pages = 10  # Safety limit
        
        while page_count < max_pages:
            # Add continuation token if we have one
            current_args = dict(args)
            if continuation_token:
                current_args["continuationToken"] = continuation_token
            
            # Call the tool
            response_str = await self.mcp.call_tool(tool, current_args)

            # Detect textual "tool not found" responses and attempt alias fallback
            try:
                lower_resp = response_str.lower() if isinstance(response_str, str) else ""
            except Exception:
                lower_resp = ""

            if "not found" in lower_resp and "tool" in lower_resp:
                try:
                    available = list(getattr(self.mcp, 'tools_cache', {}).keys())
                    if available:
                        # CRITICAL FIX: Check exact match first, then stricter fuzzy match (0.85)
                        exact_match = None
                        for avail_tool in available:
                            if avail_tool.lower() == tool.lower():
                                exact_match = avail_tool
                                break
                        
                        if exact_match:
                            matches = [exact_match]
                        else:
                            matches = difflib.get_close_matches(tool, available, n=1, cutoff=0.85)
                        if matches:
                            alt = matches[0]
                            print(f"[ToolExecutor] MCP returned 'tool not found' for {tool} during pagination. Retrying with close match: {alt}")
                            # Validate schemas match before aliasing
                            if self._validate_alias_compatible(tool, alt, current_args):
                                try:
                                    # Map parameters if needed (e.g., id â†’ workItemId)
                                    mapped_args = self._map_parameters(tool, alt, current_args)
                                    response_str = await self.mcp.call_tool(alt, mapped_args)
                                    tool = alt
                                except Exception as e2:
                                    return {
                                        "tool": tool,
                                        "success": False,
                                        "error": f"Tool call failed after alias attempt: {e2}",
                                        "count": 0,
                                        "items": []
                                    }
                            else:
                                print(f"[ToolExecutor] Alias {alt} rejected during pagination: incompatible schema with {tool}")
                                return {"tool": tool, "success": False, "error": f"No compatible tool found for {tool} (alias {alt} has incompatible schema)", "count": 0, "items": []}
                        else:
                            return {"tool": tool, "success": False, "error": response_str, "count": 0, "items": []}
                    else:
                        return {"tool": tool, "success": False, "error": response_str, "count": 0, "items": []}
                except Exception:
                    return {"tool": tool, "success": False, "error": response_str, "count": 0, "items": []}
            
            # Handle None response from MCP
            if response_str is None:
                print(f"[ToolExecutor] MCP returned None for {tool} during pagination")
                return {
                    "tool": tool,
                    "success": False,
                    "error": "MCP server returned empty response",
                    "count": 0,
                    "items": []
                }
            
            # Check for error responses FIRST
            if self._is_error_response(response_str) or (isinstance(response_str, str) and (response_str.strip().startswith("Error") or '"Message":' in response_str)):
                print(f"[ToolExecutor] Error response detected during pagination: {response_str[:200]}")
                # Attempt to decode double-encoded JSON error payloads
                try:
                    parsed = json.loads(response_str)
                    if isinstance(parsed, str):
                        try:
                            parsed2 = json.loads(parsed)
                            parsed = parsed2
                        except Exception:
                            pass
                    if isinstance(parsed, dict):
                        value = parsed.get('value') or parsed.get('results') or parsed.get('items')
                        if isinstance(value, dict) and any(k in value for k in ("Message", "message", "error")):
                            err_text = json.dumps(parsed)
                            return {"tool": tool, "success": False, "error": err_text, "count": 0, "items": []}
                except Exception:
                    # Not JSON - return as textual error
                    return {"tool": tool, "success": False, "error": response_str, "count": 0, "items": []}
            
            # Parse response
            try:
                response = json.loads(response_str)
            except json.JSONDecodeError:
                # Not JSON, can't paginate
                all_items.append(response_str)
                break
            
            # Detect ADO-style error payloads encoded as JSON (value: { Message: ... })
            try:
                if isinstance(response, dict):
                    value = response.get('value') or response.get('results') or response.get('items')
                    if isinstance(value, dict) and any(k in value for k in ("Message", "message", "error")):
                        err_text = json.dumps(response)
                        return {"tool": tool, "success": False, "error": err_text, "count": 0, "items": []}
            except Exception:
                pass
            # Handle JSON null or empty response
            if response is None:
                print(f"[ToolExecutor] MCP returned JSON null for {tool}")
                break
            
            # Extract items from this page
            page_items = self._extract_items(response)
            all_items.extend(page_items)
            
            # Check for continuation token (only if response is a dict)
            continuation_token = response.get("continuationToken") if isinstance(response, dict) else None
            
            if not continuation_token:
                break
            
            page_count += 1
            print(f"[ToolExecutor] Paginating {tool}, page {page_count + 1}")
        
        # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
        # ENRICH WORK ITEMS: If items only have ID (no fields), fetch full details
        # This is needed for wit_get_work_items_for_iteration which returns workItemRelations
        # with target objects that only contain id and url
        # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
        print(f"[CHECKPOINT-A] Reached enrichment section for tool: {tool}")
        logger.info(f"[ToolExecutor] Checking enrichment for tool: {tool}, items_count: {len(all_items)}")
        if tool in ("wit_get_work_items_for_iteration", "work_get_iteration_work_items"):
            print(f"[CHECKPOINT-B] Tool is wit_get_work_items_for_iteration")
            items_need_enrichment = (
                len(all_items) > 0 and 
                isinstance(all_items[0], dict) and 
                'id' in all_items[0] and 
                'fields' not in all_items[0]
            )
            logger.info(f"[ToolExecutor DEBUG] Enrichment check: len={len(all_items)}, needs_enrichment={items_need_enrichment}")
            print(f"[CHECKPOINT-C] Enrichment check: len={len(all_items)}, needs_enrichment={items_need_enrichment}")
            if all_items:
                logger.info(f"[ToolExecutor DEBUG] First item keys: {list(all_items[0].keys())}")
                print(f"[CHECKPOINT-D] First item keys: {list(all_items[0].keys())}")
            if items_need_enrichment:
                project = args.get('project', '')
                logger.info(f"[ToolExecutor] Enriching {len(all_items)} work items with full details...")
                print(f"[CHECKPOINT-E] Starting enrichment for {len(all_items)} items...")
                enrichment_result = await self._enrich_work_items(project, all_items)
                if enrichment_result and enrichment_result.get('enriched_items'):
                    all_items = enrichment_result['enriched_items']
                    logger.info(f"[ToolExecutor] Enriched {len(all_items)} work items with full details")
                    print(f"[CHECKPOINT-F] Enriched {len(all_items)} work items with full details")
                    
                    # Surface warnings about failed enrichment
                    if enrichment_result.get('failed_ids'):
                        logger.warning(f"[ToolExecutor] Warning: {len(enrichment_result['failed_ids'])} items failed enrichment")
                        print(f"[CHECKPOINT-G] Warning: {len(enrichment_result['failed_ids'])} items failed enrichment")
        
        # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
        # CRITICAL FIX: POST-FILTER WORK ITEMS BASED ON ARGS
        # ADO APIs don't always filter correctly - we must apply filters after retrieval
        # This handles: workItemType, state, assignedTo (including unassigned)
        # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
        if tool in ("wit_get_work_items_for_iteration", "work_get_iteration_work_items", "search_workitem"):
            filters_to_apply = {}
            
            # Extract filter parameters from args
            if args.get('workItemType'):
                wit_filter = args['workItemType']
                if isinstance(wit_filter, list):
                    filters_to_apply['workItemType'] = [t.strip() for t in wit_filter]
                elif isinstance(wit_filter, str):
                    # Handle comma-separated strings from normalization (e.g., "Bug,Task")
                    filters_to_apply['workItemType'] = [t.strip() for t in wit_filter.split(',')]
            
            if args.get('state'):
                state_filter = args['state']
                if isinstance(state_filter, list):
                    filters_to_apply['state'] = [s.strip() for s in state_filter]
                elif isinstance(state_filter, str):
                    # Handle comma-separated strings from normalization (e.g., "Active,New,Ready")
                    filters_to_apply['state'] = [s.strip() for s in state_filter.split(',')]
            
            if args.get('assignedTo'):
                assigned_filter = args['assignedTo']
                if isinstance(assigned_filter, list):
                    filters_to_apply['assignedTo'] = [a.strip() for a in assigned_filter]
                elif isinstance(assigned_filter, str):
                    # Handle comma-separated strings from normalization
                    filters_to_apply['assignedTo'] = [a.strip() for a in assigned_filter.split(',')]
            
            # âš ï¸ REMOVED: Team is an API parameter, not a work item filter
            # The 'team' parameter tells ADO which team's iteration view to use,
            # but work items don't have a 'team' field - they use AreaPath/IterationPath.
            # Filtering by team was incorrectly removing all work items.
            # if args.get('team'):
            #     team_filter = args['team']
            #     if isinstance(team_filter, list):
            #         filters_to_apply['team'] = [t.strip() for t in team_filter]
            #     elif isinstance(team_filter, str):
            #         filters_to_apply['team'] = [team_filter.strip()]
            
            # âœ… FIX: Extract areaPath filter for post-filtering
            if args.get('areaPath'):
                area_filter = args['areaPath']
                if isinstance(area_filter, list):
                    filters_to_apply['areaPath'] = [a.strip() for a in area_filter]
                elif isinstance(area_filter, str):
                    # Handle comma-separated strings from normalization
                    filters_to_apply['areaPath'] = [a.strip() for a in area_filter.split(',')]
            
            # Check for unassigned filter (special case)
            if args.get('unassigned') is True or 'unassigned' in str(args).lower():
                filters_to_apply['unassigned'] = True
            
            if filters_to_apply:
                pre_filter_count = len(all_items)
                all_items = self._apply_post_filters(all_items, filters_to_apply)
                post_filter_count = len(all_items)
                logger.info(f"[ToolExecutor] POST-FILTER applied: {pre_filter_count} â†’ {post_filter_count} items (filters: {filters_to_apply})")
        
        return {
            "tool": tool,
            "success": True,
            "count": len(all_items),
            "items": all_items,
            "pages": page_count + 1
        }
    
    def _apply_post_filters(self, items: List[Dict[str, Any]], filters: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        Apply post-retrieval filters to work items.
        
        ADO APIs don't always respect filters correctly, so we apply them after retrieval.
        This is CRITICAL for accurate query results.
        
        Args:
            items: List of work item dicts with 'fields' containing System.* fields
            filters: Dict with optional keys:
                - workItemType: List of allowed work item types (e.g., ["Bug", "Task"])
                - state: List of allowed states (e.g., ["Active", "New"])
                - assignedTo: List of allowed assignees (names or emails)
                - unassigned: Boolean - if True, only return unassigned items
                - priority: List of allowed priorities (e.g., [1, 2])
        
        Returns:
            Filtered list of work items
        """
        if not items or not filters:
            return items
        
        filtered = []
        filtered_out_reasons = {}  # Track why items were filtered
        
        # Normalize filter values for case-insensitive matching
        type_filter = None
        if 'workItemType' in filters:
            type_filter = [t.lower() for t in filters['workItemType']]
        
        state_filter = None
        if 'state' in filters:
            state_filter = [s.lower() for s in filters['state']]
        
        assigned_filter = None
        if 'assignedTo' in filters:
            assigned_filter = [a.lower() for a in filters['assignedTo']]
        
        unassigned_filter = filters.get('unassigned', False)
        
        priority_filter = None
        if 'priority' in filters:
            priority_filter = filters['priority']
        
        # âš ï¸ REMOVED: Team filter was incorrectly filtering work items
        # team_filter = None
        # if 'team' in filters:
        #     team_filter = [t.lower() for t in filters['team']]
        
        # âœ… FIX: AreaPath filter
        areapath_filter = None
        if 'areaPath' in filters:
            areapath_filter = [a.lower() for a in filters['areaPath']]
        
        for item_idx, item in enumerate(items):
            fields = item.get('fields', {})
            if not fields:
                # Try top-level fields for backward compatibility
                if 'System.Title' in item:
                    fields = item
            
            # Get item ID for logging
            item_id = item.get('id', f"unknown_{item_idx}")
            item_title = (fields.get('System.Title') or 
                         item.get('title') or 
                         fields.get('system.title') or '')[:40]
            filter_reasons = []
            
            # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
            # CHECK WORK ITEM TYPE FILTER
            # â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•â•
            if type_filter:
                # Try multiple field name variations (case-insensitive)
                item_type = (fields.get('System.WorkItemType') or 
                           item.get('workItemType') or 
                           fields.get('system.workitemtype') or 
                           fields.get('System.workItemType') or '')
                
                # If field is completely missing, log warning but INCLUDE item anyway
                # (missing enrichment field shouldn't cause rejection)
                if not item_type or item_type == '':
                    logger.warning(f"[FILTER-DEBUG] Item {item_id} has missing workItemType field - including anyway (enrichment may be incomplete)")
                    # Do NOT skip - continue with other filters
                else:
                    item_type_lower = item_type.lower()
                    # Use contains-based matching so "Bug" matches "Dev Bug",
                    # "Task" matches "Dev Task", etc. This is dynamic — any
                    # custom ADO type that contains the filter keyword passes.
                    exact_match = item_type_lower in type_filter
                    contains_match = any(tf in item_type_lower for tf in type_filter)
                    if not exact_match and not contains_match:
                        filter_reasons.append(f"type={item_type} not in {type_filter}")
                        logger.debug(f"[FILTER-DEBUG] Item {item_id} REJECTED: workItemType '{item_type}' not in {type_filter}")
                        if item_id not in filtered_out_reasons:
                            filtered_out_reasons[item_id] = f"Title: {item_title}, Type: {item_type}"
                        continue  # Skip - doesn't match type filter
                    elif not exact_match and contains_match:
                        logger.debug(f"[FILTER-DEBUG] Item {item_id} INCLUDED via contains match: workItemType '{item_type}' contains one of {type_filter}")
            
            # ═══════════════════════════════════════════════════════════════
            # CHECK STATE FILTER
            # ═══════════════════════════════════════════════════════════════
            if state_filter:
                item_state = (fields.get('System.State') or 
                            item.get('state') or
                            fields.get('system.state') or '')
                if item_state and item_state.lower() not in state_filter:
                    filter_reasons.append(f"state={item_state} not in {state_filter}")
                    logger.debug(f"[FILTER-DEBUG] Item {item_id} REJECTED: state '{item_state}' not in {state_filter}")
                    if item_id not in filtered_out_reasons:
                        filtered_out_reasons[item_id] = f"Title: {item_title}, State: {item_state}"
                    continue
            
            # ═══════════════════════════════════════════════════════════════
            # CHECK ASSIGNEE / UNASSIGNED FILTER
            # ═══════════════════════════════════════════════════════════════
            assigned_to_raw = (fields.get('System.AssignedTo') or
                             fields.get('system.assignedto') or '')
            if isinstance(assigned_to_raw, dict):
                assignee_name = (assigned_to_raw.get('displayName') or 
                               assigned_to_raw.get('uniqueName') or '')
            elif isinstance(assigned_to_raw, str):
                assignee_name = assigned_to_raw
            else:
                assignee_name = ''
            
            if unassigned_filter:
                if assignee_name.strip():
                    filter_reasons.append(f"assigned to {assignee_name}, but filter requires unassigned")
                    logger.debug(f"[FILTER-DEBUG] Item {item_id} REJECTED: assigned to '{assignee_name}' (unassigned filter)")
                    continue
            
            if assigned_filter:
                if not assignee_name.strip():
                    filter_reasons.append("no assignee, but filter requires specific assignee")
                    logger.debug(f"[FILTER-DEBUG] Item {item_id} REJECTED: no assignee (assignee filter)")
                    continue
                assignee_lower = assignee_name.lower()
                if not any(af in assignee_lower or assignee_lower in af for af in assigned_filter):
                    filter_reasons.append(f"assignee={assignee_name} not in {assigned_filter}")
                    logger.debug(f"[FILTER-DEBUG] Item {item_id} REJECTED: assignee '{assignee_name}' not in {assigned_filter}")
                    if item_id not in filtered_out_reasons:
                        filtered_out_reasons[item_id] = f"Title: {item_title}, Assignee: {assignee_name}"
                    continue
            
            # ═══════════════════════════════════════════════════════════════
            # CHECK AREAPATH FILTER
            # ═══════════════════════════════════════════════════════════════
            if areapath_filter:
                item_area = (fields.get('System.AreaPath') or
                           fields.get('system.areapath') or '')
                if item_area:
                    item_area_lower = item_area.lower()
                    if not any(item_area_lower.startswith(af) or af in item_area_lower for af in areapath_filter):
                        filter_reasons.append(f"areaPath={item_area} not matching {areapath_filter}")
                        logger.debug(f"[FILTER-DEBUG] Item {item_id} REJECTED: areaPath '{item_area}' not in {areapath_filter}")
                        if item_id not in filtered_out_reasons:
                            filtered_out_reasons[item_id] = f"Title: {item_title}, Area: {item_area}"
                        continue
            
            # ═══════════════════════════════════════════════════════════════
            # CHECK PRIORITY FILTER
            # ═══════════════════════════════════════════════════════════════
            if priority_filter:
                item_priority = (fields.get('Microsoft.VSTS.Common.Priority') or
                               fields.get('microsoft.vsts.common.priority') or '')
                if item_priority and str(item_priority) not in [str(p) for p in priority_filter]:
                    filter_reasons.append(f"priority={item_priority} not in {priority_filter}")
                    logger.debug(f"[FILTER-DEBUG] Item {item_id} REJECTED: priority '{item_priority}' not in {priority_filter}")
                    continue
            
            # Item passed all filters
            filtered.append(item)
        
        # Summary logging
        if filtered_out_reasons:
            logger.info(f"[FILTER-DEBUG] Filtered out {len(items) - len(filtered)} items. Sample: {dict(list(filtered_out_reasons.items())[:5])}")
        logger.info(f"[FILTER-DEBUG] Post-filter: {len(items)} -> {len(filtered)} items (filters: type={type_filter}, state={state_filter}, assigned={assigned_filter}, unassigned={unassigned_filter}, priority={priority_filter}, areaPath={areapath_filter})")
        
        return filtered
    
    async def _enrich_work_items(self, project: str, items: List[Dict]) -> Dict[str, Any]:
        """
        Enrich work items by fetching full field details.
        
        This is a wrapper that accepts items (with 'id' field) and extracts IDs
        before calling the batched enrichment method.
        
        Args:
            project: ADO project name
            items: List of partial work item dicts containing 'id' field
            
        Returns:
            Dict with 'enriched_items' and 'errors' keys
        """
        # Extract work item IDs from items
        work_item_ids = []
        for item in items:
            if isinstance(item, dict):
                item_id = item.get('id')
                if item_id:
                    work_item_ids.append(item_id)
        
        if not work_item_ids:
            return {'enriched_items': items, 'errors': []}
        
        # Call the actual enrichment method
        success, enriched_items, errors = await self._enrich_work_items_with_fields(work_item_ids, project)
        
        return {
            'enriched_items': enriched_items if success else items,  # Fallback to original if enrichment fails
            'errors': errors
        }
    
    async def _enrich_work_items_with_fields(self, work_item_ids: List[int], project: str) -> Tuple[bool, List[Dict], List[Dict]]:
        """
        Enrich work items by fetching full field details via wit_get_work_items_batch_by_ids.
        
        Chunks IDs into batches and fetches with retries.
        
        Args:
            work_item_ids: List of ADO work item IDs to enrich
            project: ADO project name
            
        Returns:
            Tuple of (success, enriched_items, errors)
        """
        if not work_item_ids:
            return (True, [], [])
        
        all_items = []
        errors = []
        batch_size = 200  # ADO batch limit
        
        for batch_start in range(0, len(work_item_ids), batch_size):
            batch_ids = work_item_ids[batch_start:batch_start + batch_size]
            success, items, batch_errors = await self._fetch_work_items_batch_with_retry(batch_ids, project)
            errors.extend(batch_errors)
            if success and items:
                all_items.extend(items)
            elif not success:
                logger.warning(f"[ToolExecutor] Failed to enrich batch starting at {batch_start} ({len(batch_ids)} items)")
        
        return (len(all_items) > 0, all_items, errors)
    
    async def _fetch_work_items_batch_with_retry(self, ids: List[int], project: str, max_retries: int = 3, initial_delay: float = 1.0) -> Tuple[bool, List[Dict], List[Dict]]:
        """
        Fetch work items batch with retry logic.
        
        Args:
            ids: List of work item IDs to fetch
            project: ADO project name
            max_retries: Maximum retry attempts
            initial_delay: Initial backoff delay in seconds
            
        Returns:
            Tuple of (success, items, errors)
        """
        errors = []
        
        for attempt in range(max_retries):
            try:
                response_str = await self.mcp.call_tool('wit_get_work_items_batch_by_ids', {
                    'project': project,
                    'ids': ids,
                    'fields': [
                        "System.Id",
                        "System.Title",
                        "System.WorkItemType",
                        "System.State",
                        "System.AssignedTo",
                        "System.AreaPath",
                        "System.IterationPath",
                        "System.Parent",
                        "System.Tags",
                        "System.CreatedDate",
                        "System.ChangedDate",
                    ],
                })
                
                # Handle None response
                if response_str is None:
                    error_detail = {
                        "attempt": attempt + 1,
                        "error_type": "null_response",
                        "message": "MCP returned None",
                        "ids": ids[:10]  # Sample of failed IDs
                    }
                    errors.append(error_detail)
                    print(f"[ToolExecutor] MCP returned None for wit_get_work_items_batch_by_ids (attempt {attempt+1}/{max_retries})")
                    if attempt < max_retries - 1:
                        await self._backoff_with_jitter(initial_delay, attempt)
                        continue
                    return (False, [], errors)
                
                # Check for error responses
                if self._is_error_response(response_str) or (isinstance(response_str, str) and ('"Message":' in response_str or response_str.strip().startswith('Error'))):
                    error_preview = response_str[:200] if isinstance(response_str, str) else str(response_str)[:200]
                    error_detail = {
                        "attempt": attempt + 1,
                        "error_type": "error_response",
                        "message": error_preview,
                        "ids": ids[:10]
                    }
                    errors.append(error_detail)
                    print(f"[ToolExecutor] Error enriching batch (attempt {attempt+1}/{max_retries}): {error_preview}")
                    if attempt < max_retries - 1:
                        await self._backoff_with_jitter(initial_delay, attempt)
                        continue
                    return (False, [])
                
                # Parse response
                try:
                    response = json.loads(response_str) if isinstance(response_str, str) else response_str
                    if response is None:
                        error_detail = {
                            "attempt": attempt + 1,
                            "error_type": "null_parsed_response",
                            "message": "Parsed response is None",
                            "ids": ids[:10]
                        }
                        errors.append(error_detail)
                        print(f"[ToolExecutor] Parsed response is None for batch (attempt {attempt+1}/{max_retries})")
                        if attempt < max_retries - 1:
                            await self._backoff_with_jitter(initial_delay, attempt)
                            continue
                        return (False, [], errors)
                    
                    # Handle both list and dict with value/items key
                    if isinstance(response, list):
                        batch_items = response
                    else:
                        batch_items = response.get('value', response.get('items', []))
                    
                    if isinstance(batch_items, list) and len(batch_items) > 0:
                        # DEBUG: Log the structure of the first enriched item
                        first_item = batch_items[0]
                        logger.debug(f"[ENRICH-DEBUG] First enriched item structure:")
                        logger.debug(f"[ENRICH-DEBUG]   Top-level keys: {list(first_item.keys())}")
                        if 'fields' in first_item:
                            fields = first_item['fields']
                            logger.debug(f"[ENRICH-DEBUG]   Fields keys (first 20): {list(fields.keys())[:20]}")
                            if 'System.WorkItemType' in fields:
                                logger.debug(f"[ENRICH-DEBUG]   System.WorkItemType found: {fields['System.WorkItemType']}")
                            if 'System.State' in fields:
                                logger.debug(f"[ENRICH-DEBUG]   System.State found: {fields['System.State']}")
                        return (True, batch_items, errors)
                    else:
                        error_detail = {
                            "attempt": attempt + 1,
                            "error_type": "empty_response",
                            "message": "Empty or invalid batch response",
                            "ids": ids[:10]
                        }
                        errors.append(error_detail)
                        print(f"[ToolExecutor] Empty or invalid batch response (attempt {attempt+1}/{max_retries})")
                        if attempt < max_retries - 1:
                            await self._backoff_with_jitter(initial_delay, attempt)
                            continue
                        return (False, [], errors)
                
                except Exception as parse_error:
                    error_detail = {
                        "attempt": attempt + 1,
                        "error_type": "parse_error",
                        "message": str(parse_error),
                        "ids": ids[:10]
                    }
                    errors.append(error_detail)
                    print(f"[ToolExecutor] Error parsing enriched batch (attempt {attempt+1}/{max_retries}): {parse_error}")
                    if attempt < max_retries - 1:
                        await self._backoff_with_jitter(initial_delay, attempt)
                        continue
                    return (False, [], errors)
            
            except Exception as e:
                error_detail = {
                    "attempt": attempt + 1,
                    "error_type": "exception",
                    "message": str(e),
                    "ids": ids[:10]
                }
                errors.append(error_detail)
                print(f"[ToolExecutor] Exception during batch fetch (attempt {attempt+1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    await self._backoff_with_jitter(initial_delay, attempt)
                    continue
                return (False, [], errors)
        
        # All retries exhausted
        return (False, [], errors)
    
    async def _backoff_with_jitter(self, initial_delay: float, attempt: int):
        """
        Sleep with exponential backoff + jitter to avoid thundering herd.
        
        Args:
            initial_delay: Base delay in seconds
            attempt: Current attempt number (0-indexed)
        """
        # Exponential backoff: delay * 2^attempt
        delay = initial_delay * (2 ** attempt)
        # Add jitter: random value between 0 and delay
        jittered_delay = delay * random.random()
        
        print(f"[ToolExecutor] Backing off for {jittered_delay:.2f}s before retry...")
        await asyncio.sleep(jittered_delay)
    
    def _is_error_response(self, response_str: str) -> bool:
        """
        Check if a response string indicates an error.
        
        Common error patterns from ADO/MCP:
        - "400 Bad Request"
        - "401 Unauthorized"
        - "403 Forbidden"
        - "404 Not Found"
        - "500 Internal Server Error"
        - "error:" prefix
        - "Azure DevOps ... error"
        """
        if not response_str or not isinstance(response_str, str):
            return False
        
        error_patterns = [
            "400 Bad Request",
            "401 Unauthorized",
            "403 Forbidden",
            "404 Not Found",
            "500 Internal Server Error",
            "502 Bad Gateway",
            "503 Service Unavailable",
            "error:",
            "Error:",
            "API error",
            "Azure DevOps Work Item Search API error",
            "TF400",  # Azure DevOps error codes
            "TF401",
            "VS403",
        ]
        
        for pattern in error_patterns:
            if pattern in response_str:
                return True
        
        return False
    
    def _extract_items(self, response: Any) -> List[Any]:
        """
        Extract items from various response formats.
        
        Handles:
        - {"items": [...]}
        - {"results": [...]}
        - {"value": [...]}
        - {"workItems": [...]}
        - {"workItemRelations": [{"target": {...}}, ...]}  (from wit_get_work_items_for_iteration)
        - [...]  (direct array)
        - Single object
        """
        if isinstance(response, list):
            return response
        
        if isinstance(response, dict):
            # Handle workItemRelations format (from wit_get_work_items_for_iteration)
            # This format has nested "target" objects that need extraction
            if "workItemRelations" in response and isinstance(response["workItemRelations"], list):
                relations = response["workItemRelations"]
                if len(relations) > 0:
                    # Check if items have 'target' structure (iteration work items format)
                    if isinstance(relations[0], dict) and 'target' in relations[0]:
                        items = [item['target'] for item in relations if isinstance(item, dict) and 'target' in item]
                        print(f"[ToolExecutor] Extracted {len(items)} items from workItemRelations.target")
                        return items
                    else:
                        return relations
                else:
                    # Empty workItemRelations means no work items in this iteration
                    print(f"[ToolExecutor] workItemRelations is empty - no work items in iteration")
                    return []
            
            # Try common keys for item arrays
            for key in ["items", "results", "value", "workItems", "testPlans", "testSuites", "testCases", "builds", "pullRequests", "repositories", "branches", "teams", "projects", "iterations"]:
                if key in response and isinstance(response[key], list):
                    return response[key]
            
            # If count is present but no items, return empty list
            if "count" in response and response.get("count") == 0:
                return []
            
            # Return the dict itself as a single item
            return [response]
        
        # Fallback: wrap in list
        return [response]
    
    def _get_tool_category(self, tool: str) -> str:
        """
        Classify tool into a category for observability.
        
        Returns:
            Category string like "work_items", "git", "builds", "test", etc.
        """
        if not tool:
            return "unknown"
        
        tool_lower = tool.lower()
        
        # Work item tracking
        if any(x in tool_lower for x in ["wit_", "work_", "workitem", "search_workitem"]):
            return "work_items"
        
        # Git operations
        if any(x in tool_lower for x in ["git_", "repos_", "repository", "pullrequest", "pr_"]):
            return "git"
        
        # Builds & Pipelines
        if any(x in tool_lower for x in ["build_", "pipeline_", "release_"]):
            return "builds"
        
        # Test management
        if any(x in tool_lower for x in ["test_", "testplan", "testsuite", "testcase", "testrun"]):
            return "test"
        
        # Project/Team structure
        if any(x in tool_lower for x in ["projects_", "teams_", "iteration", "area", "classification"]):
            return "structure"
        
        # Core services
        if any(x in tool_lower for x in ["core_", "process_"]):
            return "core"
        
        return "other"
    
    def _get_operation_type(self, tool: str, args: Dict[str, Any]) -> str:
        """
        Extract operation type from tool name and args for observability.
        
        Returns:
            Operation like "get", "list", "create", "update", "delete", "search", "query"
        """
        if not tool:
            return "unknown"
        
        tool_lower = tool.lower()
        
        # Operation from tool name - check for exact patterns first
        if "_list" in tool_lower or tool_lower.endswith("_list") or tool_lower.startswith("list_"):
            return "list"
        elif "_get_" in tool_lower or tool_lower.startswith("get_"):
            return "get"
        elif "_create_" in tool_lower or tool_lower.startswith("create_"):
            return "create"
        elif "_update_" in tool_lower or tool_lower.startswith("update_"):
            return "update"
        elif "_delete_" in tool_lower or tool_lower.startswith("delete_"):
            return "delete"
        elif "search" in tool_lower:
            return "search"
        elif "query" in tool_lower or "wiql" in tool_lower:
            return "query"
        
        # Default to read/write based on common patterns
        write_indicators = ["add", "remove", "set", "assign", "move", "close", "resolve"]
        if any(x in tool_lower for x in write_indicators):
            return "write"
        
        return "read"


# =============================================================================
# TOOL DISCOVERY FUNCTIONS (merged from ado_tool_catalog.py)
# =============================================================================

def find_tools_for_query(query: str, top_n: int = 5) -> List[str]:
    """
    Find the most relevant MCP tools for a query using scoring.
    
    Scores tools based on:
    - Keyword matches in query
    - Regex pattern matches
    - Category hints
    - Tool priority
    
    Args:
        query: Natural language query
        top_n: Number of top tools to return
    
    Returns:
        List of tool names ranked by relevance
    """
    q = query.lower()
    scored_tools = []
    
    for tool_name, tool_meta in MCP_TOOL_REGISTRY.items():
        score = 0
        
        # Score by keywords (if present in metadata)
        keywords = tool_meta.get("keywords", [])
        for keyword in keywords:
            if keyword.lower() in q:
                score += 3 * len(keyword.split())
        
        # Score by regex patterns (if present in metadata)
        patterns = tool_meta.get("query_patterns", [])
        for pattern in patterns:
            try:
                if re.search(pattern, q, re.IGNORECASE):
                    score += 10
            except re.error:
                pass  # Skip invalid patterns
        
        # Score by use_cases (legacy support)
        use_cases = tool_meta.get("use_cases", [])
        for use_case in use_cases:
            if use_case.lower() in q:
                score += 2
        
        # Category hints scoring
        category_hints = {
            "work_items": ["bug", "story", "task", "item", "work"],
            "pull_requests": ["pr", "pull", "merge", "review"],
            "pipelines": ["build", "pipeline", "ci", "deploy"],
            "repositories": ["repo", "commit", "branch", "code"],
            "iterations": ["sprint", "iteration"],
            "test": ["test", "qa"],
            "wiki": ["wiki", "doc"],
        }
        
        tool_category = tool_meta.get("category", "")
        if tool_category in category_hints:
            hints = category_hints[tool_category]
            if any(h in q for h in hints):
                score += 5
        
        # Priority weighting (default priority = 5)
        priority = tool_meta.get("priority", 5)
        score = score * (priority / 5.0)
        
        if score > 0:
            scored_tools.append((tool_name, score))
    
    # Sort by score descending
    scored_tools.sort(key=lambda x: x[1], reverse=True)
    return [name for name, _ in scored_tools[:top_n]]


def get_tools_by_category(category: str) -> List[str]:
    """
    Get all tools in a specific category.
    
    Args:
        category: Category name (e.g., "work_items", "pull_requests")
    
    Returns:
        List of tool names in that category
    """
    matching_tools = []
    
    for tool_name, tool_meta in MCP_TOOL_REGISTRY.items():
        tool_category = tool_meta.get("category", "")
        if tool_category == category:
            matching_tools.append(tool_name)
    
    return matching_tools


def get_tool_metadata(tool_name: str) -> Optional[Dict[str, Any]]:
    """
    Get complete metadata for a tool.
    
    Args:
        tool_name: Name of the tool
    
    Returns:
        Tool metadata dict or None if not found
    """
    return MCP_TOOL_REGISTRY.get(tool_name)


def validate_tool_args(tool_name: str, args: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """
    Validate arguments for a tool.
    
    Args:
        tool_name: Name of the tool
        args: Arguments dict
    
    Returns:
        Tuple of (is_valid, list_of_missing_args)
    """
    tool_meta = MCP_TOOL_REGISTRY.get(tool_name)
    if not tool_meta:
        return False, [f"Unknown tool: {tool_name}"]
    
    required_args = tool_meta.get("required_args", [])
    missing = [arg for arg in required_args if arg not in args or args[arg] is None]
    
    return len(missing) == 0, missing


def get_all_tool_names() -> List[str]:
    """Get all registered tool names."""
    return list(MCP_TOOL_REGISTRY.keys())
