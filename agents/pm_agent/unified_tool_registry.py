"""
Unified Tool Registry - Single source of truth for all tools and skills.

This module provides a unified view of:
1. MCP Tools from utilities/mcp/tool_registry.py
2. PM Skills from agents/pm_skill_agent/skills.py

The LLM planners (Light and Deep) use this registry to understand:
- What tools/skills are available
- How to route queries (call_tool vs call_skill)
- What parameters each tool/skill accepts

ARCHITECTURE:
=============
┌─────────────────────────────────────────────────────────────────┐
│                    UNIFIED_REGISTRY                             │
├─────────────────────────────────────────────────────────────────┤
│  ┌──────────────────────┐    ┌────────────────────────────────┐ │
│  │   MCP_TOOL_REGISTRY  │    │      SKILL_DEFINITIONS         │ │
│  │   (pm_agent tools)   │    │      (pm_skill_agent skills)   │ │
│  │   - search_workitem  │    │      - bug_areas_highlight     │ │
│  │   - wit_get_work_item│    │      - feedback_to_dev         │ │
│  │   - execute_wiql     │    │      - billing_deviation       │ │
│  │   - list_area_paths  │    │      - iteration_report        │ │
│  └──────────────────────┘    └────────────────────────────────┘ │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
              ┌─────────────────────────────────────────┐
              │         Light/Deep LLM Planner          │
              │  - Full visibility into ALL tools/skills│
              │  - Dynamically routes to correct agent  │
              └─────────────────────────────────────────┘

USAGE:
======
    from agents.pm_agent.unified_tool_registry import UNIFIED_REGISTRY
    
    # Get all tools
    all_tools = UNIFIED_REGISTRY.get_all_tools()
    
    # Get tools by category
    pm_skills = UNIFIED_REGISTRY.get_tools_by_category("pm_skill")
    pm_agent_tools = UNIFIED_REGISTRY.get_tools_by_category("pm_agent")
    
    # Check if a tool exists and what type it is
    tool_info = UNIFIED_REGISTRY.get_tool("bug_areas_highlight")
    if tool_info:
        if tool_info.get("category") == "pm_skill":
            # Use action="call_skill"
        else:
            # Use action="call_tool"
"""

import logging
from typing import Dict, Any, Optional, List

logger = logging.getLogger(__name__)


class UnifiedToolRegistry:
    """
    Unified registry combining MCP tools and PM Skills.
    
    Provides a single source of truth for all available tools/skills
    that the LLM planners can route to.
    """
    
    def __init__(self):
        self._tools: Dict[str, Dict[str, Any]] = {}
        self._categories: Dict[str, List[str]] = {}
        self._loaded = False
    
    def _ensure_loaded(self) -> None:
        """Lazy load tools and skills on first access."""
        if self._loaded:
            return
        
        self._load_mcp_tools()
        self._load_local_skills()
        self._load_pm_skills()
        self._loaded = True
        
        # Build category summary
        cat_summary = {cat: len(names) for cat, names in self._categories.items() if names}
        logger.info(f"[UNIFIED_REGISTRY] Loaded {len(self._tools)} total tools/skills. "
                    f"Categories: {cat_summary}")
    
    def _load_mcp_tools(self) -> None:
        """Load tools from MCP tool registry (auto-generated or fallback)."""
        try:
            from utilities.mcp.tool_registry import MCP_TOOL_REGISTRY
            
            for name, tool_info in MCP_TOOL_REGISTRY.items():
                # Skip tools marked as unavailable
                if tool_info.get("mcp_available") is False:
                    continue
                
                category = tool_info.get("category", "other")
                
                # Ensure category list exists
                if category not in self._categories:
                    self._categories[category] = []
                
                self._tools[name] = {
                    "name": name,
                    "description": tool_info.get("description", ""),
                    "args": tool_info.get("required_args", []),
                    "required_args": tool_info.get("required_args", []),
                    "optional_args": tool_info.get("optional_args", {}),
                    "arg_descriptions": tool_info.get("arg_descriptions", {}),
                    "category": category,
                    "agent": tool_info.get("agent", "pm_agent"),
                    "action_type": "call_tool",  # MCP tools use call_tool
                    "source": "mcp_tool_registry",
                    "priority": tool_info.get("priority", 5),
                    "pagination": tool_info.get("pagination", False),
                    "write": tool_info.get("write", False),
                    "keywords": tool_info.get("keywords", []),
                    "mcp_available": tool_info.get("mcp_available", True),
                }
                self._categories[category].append(name)
            
            logger.debug(f"[UNIFIED_REGISTRY] Loaded {len(MCP_TOOL_REGISTRY)} MCP tools")
            
        except ImportError as e:
            logger.warning(f"[UNIFIED_REGISTRY] Could not load MCP tools: {e}")
    
    def _load_local_skills(self) -> None:
        """Load local skills that are NOT MCP tools but execute locally (e.g., execute_wiql)."""
        local_skills = {
            "execute_wiql": {
                "name": "execute_wiql",
                "description": "Execute a WIQL query against Azure DevOps REST API. LOCAL skill for complex queries with date ranges, priority filters, field-specific conditions, aggregation, and comparison.",
                "args": ["wiql"],
                "required_args": ["wiql"],
                "optional_args": {"project": "str", "top": "int"},
                "arg_descriptions": {
                    "wiql": "WIQL query string",
                    "project": "Azure DevOps project name",
                    "top": "Maximum results"
                },
                "category": "work_items",
                "agent": "pm_agent",
                "action_type": "call_tool",
                "source": "local_skill",
                "priority": 10,
                "pagination": False,
                "write": False,
                "keywords": ["wiql", "query", "work items", "filter", "date", "priority"],
                "mcp_available": True,
            }
        }
        for name, info in local_skills.items():
            if name not in self._tools:
                self._tools[name] = info
                category = info.get("category", "other")
                if category not in self._categories:
                    self._categories[category] = []
                self._categories[category].append(name)
                logger.debug(f"[UNIFIED_REGISTRY] Loaded local skill: {name}")

    def _load_pm_skills(self) -> None:
        """Load skills from PM Skill Agent."""
        try:
            from agents.pm_skill_agent.skills import SKILL_DEFINITIONS
            
            for name, skill_def in SKILL_DEFINITIONS.items():
                # Skip if already loaded from MCP registry
                if name in self._tools:
                    # Update to mark as skill
                    self._tools[name]["action_type"] = "call_skill"
                    self._tools[name]["source"] = "skill_definitions"
                    continue
                
                # Extract skill info - handle both dataclass and dict
                if hasattr(skill_def, 'description'):
                    # SkillDefinition dataclass
                    description = skill_def.description
                    required_params = skill_def.required_params
                    optional_params = skill_def.optional_params
                    use_cases = skill_def.use_cases
                else:
                    # Dict format
                    description = skill_def.get("description", "")
                    required_params = skill_def.get("required_params", [])
                    optional_params = skill_def.get("optional_params", {})
                    use_cases = skill_def.get("use_cases", [])
                
                self._tools[name] = {
                    "name": name,
                    "description": description,
                    "args": required_params,
                    "optional_args": optional_params,
                    "use_cases": use_cases,
                    "category": "pm_skill",
                    "agent": "pm_skill_agent",
                    "action_type": "call_skill",  # Skills use call_skill
                    "source": "skill_definitions"
                }
                if "pm_skill" not in self._categories:
                    self._categories["pm_skill"] = []
                self._categories["pm_skill"].append(name)
            
            logger.debug(f"[UNIFIED_REGISTRY] Loaded {len(SKILL_DEFINITIONS)} PM Skills")
            
        except ImportError as e:
            logger.warning(f"[UNIFIED_REGISTRY] Could not load PM Skills: {e}")
    
    def get_all_tools(self) -> Dict[str, Dict[str, Any]]:
        """Get all registered tools and skills."""
        self._ensure_loaded()
        return self._tools.copy()
    
    def get_tool(self, name: str) -> Optional[Dict[str, Any]]:
        """Get a specific tool/skill by name."""
        self._ensure_loaded()
        return self._tools.get(name)
    
    def get_tools_by_category(self, category: str) -> Dict[str, Dict[str, Any]]:
        """Get all tools in a specific category."""
        self._ensure_loaded()
        tool_names = self._categories.get(category, [])
        return {name: self._tools[name] for name in tool_names if name in self._tools}
    
    def get_skill_names(self) -> List[str]:
        """Get names of all PM Skills (for call_skill action type)."""
        self._ensure_loaded()
        return [name for name, tool in self._tools.items() 
                if tool.get("action_type") == "call_skill"]
    
    def get_tool_names(self) -> List[str]:
        """Get names of all MCP Tools (for call_tool action type)."""
        self._ensure_loaded()
        return [name for name, tool in self._tools.items() 
                if tool.get("action_type") == "call_tool"]
    
    def is_skill(self, name: str) -> bool:
        """Check if a name refers to a PM Skill (vs MCP Tool)."""
        self._ensure_loaded()
        tool = self._tools.get(name)
        return tool.get("action_type") == "call_skill" if tool else False
    
    def get_action_type(self, name: str) -> str:
        """Get the action type for a tool/skill (call_tool or call_skill)."""
        self._ensure_loaded()
        tool = self._tools.get(name)
        return tool.get("action_type", "call_tool") if tool else "call_tool"
    
    def refresh(self) -> None:
        """Force reload of all tools and skills."""
        self._tools = {}
        self._categories = {}
        self._loaded = False
        self._ensure_loaded()
        logger.info("[UNIFIED_REGISTRY] Registry refreshed")


# Singleton instance
UNIFIED_REGISTRY = UnifiedToolRegistry()


def get_unified_registry() -> UnifiedToolRegistry:
    """Get the singleton unified registry instance."""
    return UNIFIED_REGISTRY
