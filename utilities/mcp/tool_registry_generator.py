"""
Tool Registry Generator - Auto-generates MCP_TOOL_REGISTRY metadata from live MCP tools.

This module provides functions to:
1. Query the live MCP connector's tools_cache (populated at startup via JSON-RPC tools/list)
2. Convert each tool's inputSchema into the MCP_TOOL_REGISTRY format (required_args, optional_args, etc.)
3. Infer category, priority, and keywords dynamically from tool name prefixes and descriptions
4. Merge with any manually-defined overrides (e.g., pagination flags, write flags, alias_for)

Architecture:
    MCP Server (live) → MCPConnector.tools_cache → generate_registry() → MCP_TOOL_REGISTRY
    
    Called ONCE at startup after MCP connector initializes.
    The generated registry is cached in-memory and used by:
    - Planner (for tool selection and LLM prompt building)
    - UnifiedToolRegistry (for validation)
    - ToolExecutor (for argument validation and context injection)
    
    This replaces the old approach of manually maintaining 4000+ lines of static metadata.
"""

import logging
import re
from typing import Dict, Any, List, Optional, Tuple

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
# CATEGORY INFERENCE - Maps tool name prefixes to categories dynamically
# ══════════════════════════════════════════════════════════════════════════════
PREFIX_TO_CATEGORY: Dict[str, str] = {
    "wit_": "work_items",
    "work_": "iteration",
    "search_": "search",
    "core_": "core",
    "repo_": "repository",
    "pipelines_": "build",
    "testplan_": "testplan",
    "wiki_": "wiki",
    "advsec_": "security",
}

# Fallback category patterns (checked if prefix doesn't match)
DESCRIPTION_CATEGORY_PATTERNS: List[Tuple[str, str]] = [
    (r"\b(work\s*item|bug|task|story|feature|epic)\b", "work_items"),
    (r"\b(iteration|sprint|capacity)\b", "iteration"),
    (r"\b(pull\s*request|branch|commit|repository|repo)\b", "repository"),
    (r"\b(build|pipeline|ci|cd|deploy)\b", "build"),
    (r"\b(test\s*plan|test\s*case|test\s*suite|qa)\b", "testplan"),
    (r"\b(wiki|documentation)\b", "wiki"),
    (r"\b(security|alert|vulnerab)\b", "security"),
    (r"\b(search|find|query)\b", "search"),
    (r"\b(project|team|identity)\b", "core"),
]


# ══════════════════════════════════════════════════════════════════════════════
# PRIORITY INFERENCE - Assigns priority scores based on tool characteristics
# ══════════════════════════════════════════════════════════════════════════════
HIGH_PRIORITY_PATTERNS = [
    (r"^search_workitem$", 8),
    (r"^wit_get_work_item$", 8),
    (r"^wit_create_work_item$", 7),
    (r"^wit_update_work_item$", 7),
    (r"^core_list_projects$", 7),
    (r"^core_list_project_teams$", 7),
    (r"list|get", 6),
    (r"create|update|add", 5),
    (r"delete|remove", 4),
]


# ══════════════════════════════════════════════════════════════════════════════
# KEYWORD GENERATION - Extracts keywords from tool names and descriptions
# ══════════════════════════════════════════════════════════════════════════════
def _generate_keywords(tool_name: str, description: str) -> List[str]:
    """Generate search keywords from tool name and description."""
    keywords = set()
    
    # Extract meaningful words from tool name (split on _ and camelCase)
    name_parts = tool_name.replace("_", " ").split()
    for part in name_parts:
        if len(part) > 2 and part.lower() not in {"get", "set", "the", "for", "and", "wit", "mcp"}:
            keywords.add(part.lower())
    
    # Extract keywords from description
    if description:
        desc_words = re.findall(r'\b[a-zA-Z]{3,}\b', description.lower())
        stop_words = {"the", "and", "for", "with", "from", "that", "this", "are", "was", "has",
                      "have", "been", "will", "can", "not", "all", "its", "any", "may", "use",
                      "used", "using", "which", "when", "into", "than"}
        for word in desc_words:
            if word not in stop_words and len(word) > 2:
                keywords.add(word)
    
    return list(keywords)[:15]  # Cap at 15 keywords


def _infer_category(tool_name: str, description: str) -> str:
    """Infer tool category from its name prefix and description."""
    # Check prefix mapping first
    for prefix, category in PREFIX_TO_CATEGORY.items():
        if tool_name.startswith(prefix):
            return category
    
    # Fallback: check description patterns
    if description:
        for pattern, category in DESCRIPTION_CATEGORY_PATTERNS:
            if re.search(pattern, description, re.IGNORECASE):
                return category
    
    return "other"


def _infer_priority(tool_name: str) -> int:
    """Infer tool priority from its name pattern."""
    for pattern, priority in HIGH_PRIORITY_PATTERNS:
        if re.search(pattern, tool_name, re.IGNORECASE):
            return priority
    return 5  # Default priority


def _is_write_tool(tool_name: str, description: str) -> bool:
    """Infer if a tool performs write operations."""
    write_indicators = ["create", "update", "add", "delete", "remove", "set", "assign",
                        "link", "unlink", "close", "resolve", "move"]
    name_lower = tool_name.lower()
    desc_lower = (description or "").lower()
    
    return any(ind in name_lower or ind in desc_lower for ind in write_indicators)


def _infer_pagination(tool_name: str, input_schema: Dict[str, Any]) -> bool:
    """Infer if a tool supports pagination based on its inputSchema."""
    properties = input_schema.get("properties", {})
    # Tools supporting pagination typically have 'top', 'skip', or 'continuationToken' params
    pagination_params = {"top", "skip", "continuationToken", "continuationtoken", "$top", "$skip"}
    schema_params = set(k.lower() for k in properties.keys())
    return bool(pagination_params & schema_params)


def _extract_args_from_schema(input_schema: Dict[str, Any]) -> Tuple[List[str], Dict[str, str], Dict[str, str]]:
    """
    Extract required_args, optional_args, and arg_descriptions from an MCP inputSchema.
    
    Args:
        input_schema: JSON Schema object from MCP tool definition
        
    Returns:
        Tuple of (required_args, optional_args, arg_descriptions)
        - required_args: List of required parameter names
        - optional_args: Dict of {param_name: type_string}
        - arg_descriptions: Dict of {param_name: description_string}
    """
    properties = input_schema.get("properties", {})
    required_set = set(input_schema.get("required", []))
    
    required_args = []
    optional_args = {}
    arg_descriptions = {}
    
    for param_name, param_schema in properties.items():
        # Determine type string
        param_type = param_schema.get("type", "str")
        if param_type == "array":
            items_type = param_schema.get("items", {}).get("type", "str")
            type_str = f"list[{items_type}]" if items_type != "str" else "list"
        elif param_type == "integer":
            type_str = "int"
        elif param_type == "number":
            type_str = "float"
        elif param_type == "boolean":
            type_str = "bool"
        elif param_type == "object":
            type_str = "dict"
        else:
            type_str = "str"
        
        # Classify as required or optional
        if param_name in required_set:
            required_args.append(param_name)
        else:
            optional_args[param_name] = type_str
        
        # Extract description
        desc = param_schema.get("description", "")
        if desc:
            arg_descriptions[param_name] = desc
        else:
            arg_descriptions[param_name] = f"The {param_name} parameter"
    
    return required_args, optional_args, arg_descriptions


def generate_tool_entry(tool_name: str, tool_def: Dict[str, Any]) -> Dict[str, Any]:
    """
    Generate a single MCP_TOOL_REGISTRY entry from a live MCP tool definition.
    
    Args:
        tool_name: The tool name (e.g., "search_workitem")
        tool_def: The tool definition from MCP tools_cache:
                  {"name": str, "description": str, "inputSchema": {...}}
    
    Returns:
        Registry entry dict with keys: pagination, required_args, optional_args,
        arg_descriptions, description, category, priority, mcp_available, write, keywords
    """
    description = tool_def.get("description", "")
    input_schema = tool_def.get("inputSchema", {})
    
    # Extract args from schema
    required_args, optional_args, arg_descriptions = _extract_args_from_schema(input_schema)
    
    # Infer metadata
    category = _infer_category(tool_name, description)
    priority = _infer_priority(tool_name)
    is_write = _is_write_tool(tool_name, description)
    pagination = _infer_pagination(tool_name, input_schema)
    keywords = _generate_keywords(tool_name, description)
    
    entry = {
        "pagination": pagination,
        "required_args": required_args,
        "optional_args": optional_args,
        "arg_descriptions": arg_descriptions,
        "description": description,
        "category": category,
        "priority": priority,
        "mcp_available": True,  # All live MCP tools are available
        "write": is_write,
        "keywords": keywords,
        "inputSchema": input_schema,  # Preserve original schema
    }
    
    return entry


def generate_registry_from_tools_cache(tools_cache: Dict[str, Any],
                                        overrides: Optional[Dict[str, Dict[str, Any]]] = None) -> Dict[str, Dict[str, Any]]:
    """
    Generate complete MCP_TOOL_REGISTRY from live MCP tools_cache.
    
    This is the main entry point called during application startup.
    
    Args:
        tools_cache: Dict from MCPConnector.tools_cache 
                     {tool_name: {"name": str, "description": str, "inputSchema": {...}}}
        overrides: Optional dict of manual overrides per tool.
                   Keys are tool names, values are partial dicts that override generated values.
                   Example: {"search_workitem": {"priority": 10, "pagination": True}}
    
    Returns:
        Complete MCP_TOOL_REGISTRY dict with all tools and their metadata
    """
    registry = {}
    
    if not tools_cache:
        logger.warning("[REGISTRY_GENERATOR] tools_cache is empty - no tools to generate registry from")
        return registry
    
    for tool_name, tool_def in tools_cache.items():
        try:
            entry = generate_tool_entry(tool_name, tool_def)
            
            # Apply manual overrides if provided
            if overrides and tool_name in overrides:
                override = overrides[tool_name]
                for key, value in override.items():
                    entry[key] = value
                logger.debug(f"[REGISTRY_GENERATOR] Applied overrides for '{tool_name}': {list(override.keys())}")
            
            registry[tool_name] = entry
            
        except Exception as e:
            logger.error(f"[REGISTRY_GENERATOR] Failed to generate entry for '{tool_name}': {e}")
            # Still add a minimal entry so the tool is available
            registry[tool_name] = {
                "pagination": False,
                "required_args": [],
                "optional_args": {},
                "arg_descriptions": {},
                "description": tool_def.get("description", ""),
                "category": _infer_category(tool_name, tool_def.get("description", "")),
                "priority": 5,
                "mcp_available": True,
                "write": False,
                "keywords": [],
            }
    
    logger.info(f"[REGISTRY_GENERATOR] Generated registry with {len(registry)} tools from live MCP")
    
    # Log category distribution
    category_counts = {}
    for entry in registry.values():
        cat = entry.get("category", "other")
        category_counts[cat] = category_counts.get(cat, 0) + 1
    logger.info(f"[REGISTRY_GENERATOR] Category distribution: {category_counts}")
    
    return registry


# ══════════════════════════════════════════════════════════════════════════════
# MANUAL OVERRIDES - Critical metadata that cannot be auto-inferred
# These are applied on top of auto-generated entries.
# Keep this minimal - only what CANNOT be determined from inputSchema.
# ══════════════════════════════════════════════════════════════════════════════
MANUAL_OVERRIDES: Dict[str, Dict[str, Any]] = {
    # Pagination corrections (inputSchema may not indicate pagination support)
    "wiki_list_pages": {"pagination": True},
    "wit_list_work_item_comments": {"pagination": True},
    "wit_list_work_item_revisions": {"pagination": True},
    
    # Priority boosts for commonly used tools
    "search_workitem": {"priority": 9, "category": "search", "pagination": False},  # 'top' limits results, not pagination
    "wit_get_work_item": {"priority": 8},
    "wit_create_work_item": {"priority": 7},
    "wit_update_work_item": {"priority": 7},
    "core_list_projects": {"priority": 8, "category": "core"},
    "core_list_project_teams": {"priority": 7, "category": "core"},
    "core_get_identity_ids": {"priority": 7, "category": "core"},
    
    # Category corrections where prefix-based inference may be wrong
    "search_code": {"category": "search"},
    "search_wiki": {"category": "search"},
    "work_list_iterations": {"category": "iteration"},
    "work_list_team_iterations": {"category": "iteration"},
    "work_get_iteration_capacities": {"category": "iteration"},
    "work_get_team_capacity": {"category": "iteration"},
    "work_update_team_capacity": {"category": "iteration"},
    "work_assign_iterations": {"category": "iteration"},
    "work_create_iterations": {"category": "iteration"},
}


def get_manual_overrides() -> Dict[str, Dict[str, Any]]:
    """Return the manual overrides dict. Separated for testability."""
    return MANUAL_OVERRIDES
