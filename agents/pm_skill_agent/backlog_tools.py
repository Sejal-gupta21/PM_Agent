"""
Backlog Triaging Tools - Modular components for orchestrated backlog analysis.

This module provides granular, reusable tools for backlog health assessment:
- get_backlog_items: Fetch unrefined backlog items from ADO
- calculate_team_velocity: Calculate team velocity from recent sprints
- estimate_backlog_items: Use LLM to estimate unestimated work items
- calculate_backlog_health: Compute health metrics and risk status
- generate_backlog_recommendations: Create actionable recommendations

Each tool is atomic and composable for use in orchestrated workflows.
"""

import logging
from typing import Dict, Any, List, Optional
from dataclasses import dataclass

logger = logging.getLogger("pm_skill_agent.backlog_tools")


# ============================================================================
# TOOL DEFINITIONS
# ============================================================================

@dataclass
class BacklogToolDefinition:
    """Definition of a backlog triaging tool."""
    name: str
    description: str
    required_args: List[str]
    optional_args: Dict[str, Any]
    output_schema: Dict[str, Any]
    dependencies: List[str]
    is_llm_tool: bool = False
    use_cases: List[str] = None
    
    def __post_init__(self):
        if self.use_cases is None:
            self.use_cases = []


# Tool registry for backlog triaging operations
BACKLOG_TOOL_DEFINITIONS = {
    "get_backlog_items": BacklogToolDefinition(
        name="get_backlog_items",
        description=(
            "Fetch unrefined backlog work items from Azure DevOps. "
            "Returns User Stories/PBIs in not-started states that are not yet in a sprint. "
            "Automatically filters by team's area path."
        ),
        required_args=["project", "team"],
        optional_args={
            "areaPath": "Team area path (auto-detected if not provided)",
            "includeStates": ["New", "Ready", "Requested", "Scheduled", "In Planning", "Accepted"],
            "effortField": "Custom.Effort3P",
            "maxItems": 1000
        },
        output_schema={
            "backlog_items": "int - Count of backlog items",
            "total_story_points": "float - Sum of all effort values",
            "items": "List[Dict] - Full item details with ID, title, state, story_points",
            "unestimated_count": "int - Number of items without effort estimate",
            "area_path": "str - Actual area path used for filtering"
        },
        dependencies=["core_get_team_area_path", "search_workitem", "wit_get_work_items_batch_by_ids"],
        use_cases=[
            "Check current backlog size",
            "Identify unestimated items",
            "Prepare data for capacity planning"
        ]
    ),
    
    "calculate_team_velocity": BacklogToolDefinition(
        name="calculate_team_velocity",
        description=(
            "Calculate team velocity from recent completed sprints. "
            "Analyzes the last N sprints (default 3) to determine average story points completed per sprint. "
            "Falls back to capacity-based estimate if no historical data available."
        ),
        required_args=["project", "team"],
        optional_args={
            "sprintCount": 3,
            "effortField": "Custom.Effort3P",
            "fallbackCapacity": 8.0  # hrs/day per team member
        },
        output_schema={
            "velocity": "float - Average story points per sprint",
            "sprints_analyzed": "int - Number of sprints used in calculation",
            "velocity_trend": "str - 'increasing' | 'stable' | 'decreasing'",
            "confidence": "str - 'high' | 'medium' | 'low'",
            "fallback_used": "bool - True if capacity-based estimate was used"
        },
        dependencies=["work_list_team_iterations", "search_workitem", "work_get_team_capacity"],
        use_cases=[
            "Estimate sprint capacity",
            "Calculate backlog runway",
            "Forecast completion dates"
        ]
    ),
    
    "estimate_backlog_items": BacklogToolDefinition(
        name="estimate_backlog_items",
        description=(
            "Use LLM (GPT-4) to estimate effort for unestimated backlog items. "
            "Analyzes item title, description, and acceptance criteria to provide story point estimates. "
            "Returns confidence scores and reasoning for each estimate."
        ),
        required_args=["items", "velocity"],
        optional_args={
            "model": "gpt-4o",
            "maxItemsToEstimate": 50,
            "contextItems": []  # Previously estimated items for reference
        },
        output_schema={
            "estimated_items": "List[Dict] - Items with LLM-generated estimates",
            "total_estimated_effort": "float - Sum of all estimates",
            "average_confidence": "float - Average confidence score (0-1)",
            "estimation_metadata": "Dict - LLM model, tokens used, cost"
        },
        dependencies=[],  # Pure LLM operation, no MCP dependencies
        is_llm_tool=True,
        use_cases=[
            "Estimate new backlog items",
            "Quick capacity planning",
            "Pre-refinement sizing"
        ]
    ),
    
    "calculate_backlog_health": BacklogToolDefinition(
        name="calculate_backlog_health",
        description=(
            "Calculate backlog health metrics and risk status. "
            "Compares refined backlog (estimated items) against team velocity to determine "
            "if backlog is thin (<1.5 sprints), healthy (1.5-3 sprints), or overstocked (>3 sprints)."
        ),
        required_args=["backlog_items", "total_story_points", "velocity"],
        optional_args={
            "thin_threshold": 1.5,  # sprints
            "healthy_threshold": 3.0  # sprints
        },
        output_schema={
            "status": "str - 'THIN' | 'HEALTHY' | 'OVERSTOCKED'",
            "backlog_depth": "float - Number of sprints of refined work",
            "risk_level": "str - 'critical' | 'warning' | 'ok'",
            "metrics": {
                "total_items": "int",
                "estimated_items": "int",
                "unestimated_items": "int",
                "total_story_points": "float",
                "velocity": "float"
            },
            "alerts": "List[str] - Specific warnings or issues"
        },
        dependencies=[],  # Pure calculation, no external dependencies
        use_cases=[
            "Monitor backlog health",
            "Trigger refinement alerts",
            "Product owner dashboard"
        ]
    ),
    
    "generate_backlog_recommendations": BacklogToolDefinition(
        name="generate_backlog_recommendations",
        description=(
            "Generate actionable recommendations based on backlog health status. "
            "Uses LLM to create contextual, prioritized recommendations for product owners. "
            "Includes specific actions, timelines, and impact assessments."
        ),
        required_args=["health_metrics", "backlog_items", "velocity"],
        optional_args={
            "team_context": {},  # Additional team-specific context
            "historical_trends": []  # Previous backlog health data
        },
        output_schema={
            "recommendations": "List[Dict] - Prioritized action items",
            "urgency": "str - 'immediate' | 'this_sprint' | 'next_sprint' | 'none'",
            "estimated_impact": "str - Expected outcome of following recommendations",
            "next_refinement_date": "str - Suggested date for next refinement session"
        },
        dependencies=[],  # Pure LLM operation
        is_llm_tool=True,
        use_cases=[
            "Guide product owner actions",
            "Automate backlog planning",
            "Provide sprint planning insights"
        ]
    ),
    
    "get_team_area_path": BacklogToolDefinition(
        name="get_team_area_path",
        description=(
            "Discover the actual area path for a team by querying work items. "
            "Azure DevOps teams have nested area paths (e.g., 'FracPro-OPS\\\\Global Management\\\\WTT Development\\\\XOPS 25'). "
            "This tool finds the correct path for filtering backlog items."
        ),
        required_args=["project", "team"],
        optional_args={},
        output_schema={
            "area_path": "str - Full area path for the team",
            "confidence": "str - 'high' | 'medium' | 'low'",
            "discovery_method": "str - How the path was found"
        },
        dependencies=["search_workitem"],
        use_cases=[
            "Initialize backlog queries",
            "Validate team configuration",
            "Debug area path issues"
        ]
    ),
    
    "analyze_backlog_trends": BacklogToolDefinition(
        name="analyze_backlog_trends",
        description=(
            "Analyze backlog health trends over time. "
            "Compares current backlog metrics with historical data to identify patterns: "
            "growing backlog, declining velocity, estimation accuracy, etc."
        ),
        required_args=["current_metrics", "historical_data"],
        optional_args={
            "lookback_weeks": 4,
            "include_forecasts": True
        },
        output_schema={
            "trend": "str - 'improving' | 'stable' | 'declining'",
            "key_insights": "List[str] - Notable patterns or changes",
            "forecast": "Dict - Predicted backlog health for next 2-3 sprints",
            "anomalies": "List[Dict] - Unusual patterns detected"
        },
        dependencies=[],  # Operates on provided data
        is_llm_tool=True,  # Uses LLM for insights generation
        use_cases=[
            "Executive reporting",
            "Identify process issues",
            "Proactive planning"
        ]
    )
}


# ============================================================================
# EXECUTION PLAN TEMPLATES
# ============================================================================

def get_backlog_health_plan_template() -> Dict[str, Any]:
    """
    Get the standard execution plan template for backlog health queries.
    
    This template defines the default sequence of tools to execute for
    queries like "how is my backlog health" or "give me backlog triage report".
    
    Returns:
        Dict with steps, dependencies, and execution metadata
    """
    return {
        "name": "backlog_health_analysis",
        "description": "Standard backlog health assessment workflow",
        "steps": [
            {
                "step_id": "1",
                "tool": "get_team_area_path",
                "args_template": {
                    "project": "${project}",
                    "team": "${team}"
                },
                "purpose": "Discover team's area path for filtering",
                "output_var": "area_path_result",
                "dependencies": [],
                "parallel_group": 1
            },
            {
                "step_id": "2",
                "tool": "get_backlog_items",
                "args_template": {
                    "project": "${project}",
                    "team": "${team}",
                    "areaPath": "${area_path_result.area_path}"
                },
                "purpose": "Fetch current backlog items",
                "output_var": "backlog_data",
                "dependencies": ["1"],
                "parallel_group": 2
            },
            {
                "step_id": "3",
                "tool": "calculate_team_velocity",
                "args_template": {
                    "project": "${project}",
                    "team": "${team}"
                },
                "purpose": "Calculate team velocity from recent sprints",
                "output_var": "velocity_data",
                "dependencies": [],  # Can run in parallel with step 2
                "parallel_group": 2
            },
            {
                "step_id": "4",
                "tool": "estimate_backlog_items",
                "args_template": {
                    "items": "${backlog_data.items}",
                    "velocity": "${velocity_data.velocity}"
                },
                "purpose": "Estimate unestimated items using LLM",
                "output_var": "estimation_result",
                "dependencies": ["2", "3"],
                "parallel_group": 3,
                "skip_if": "${backlog_data.unestimated_count} == 0"
            },
            {
                "step_id": "5",
                "tool": "calculate_backlog_health",
                "args_template": {
                    "backlog_items": "${backlog_data.backlog_items}",
                    "total_story_points": "${backlog_data.total_story_points}",
                    "velocity": "${velocity_data.velocity}"
                },
                "purpose": "Calculate health metrics and risk status",
                "output_var": "health_metrics",
                "dependencies": ["2", "3", "4"],
                "parallel_group": 4
            },
            {
                "step_id": "6",
                "tool": "generate_backlog_recommendations",
                "args_template": {
                    "health_metrics": "${health_metrics}",
                    "backlog_items": "${backlog_data.items}",
                    "velocity": "${velocity_data.velocity}"
                },
                "purpose": "Generate actionable recommendations",
                "output_var": "recommendations",
                "dependencies": ["5"],
                "parallel_group": 5
            }
        ],
        "expected_duration_seconds": 25,
        "can_parallelize": True,
        "final_output_vars": ["health_metrics", "recommendations"],
        "synthesis_required": True
    }


def get_quick_backlog_check_plan_template() -> Dict[str, Any]:
    """
    Get a lightweight execution plan for quick backlog checks.
    
    Skips LLM estimation and recommendations for faster results.
    Useful for dashboard queries or frequent monitoring.
    """
    return {
        "name": "quick_backlog_check",
        "description": "Fast backlog health check without LLM operations",
        "steps": [
            {
                "step_id": "1",
                "tool": "get_team_area_path",
                "args_template": {"project": "${project}", "team": "${team}"},
                "purpose": "Get area path",
                "output_var": "area_path_result",
                "dependencies": [],
                "parallel_group": 1
            },
            {
                "step_id": "2",
                "tool": "get_backlog_items",
                "args_template": {
                    "project": "${project}",
                    "team": "${team}",
                    "areaPath": "${area_path_result.area_path}"
                },
                "purpose": "Fetch backlog",
                "output_var": "backlog_data",
                "dependencies": ["1"],
                "parallel_group": 2
            },
            {
                "step_id": "3",
                "tool": "calculate_team_velocity",
                "args_template": {"project": "${project}", "team": "${team}"},
                "purpose": "Get velocity",
                "output_var": "velocity_data",
                "dependencies": [],
                "parallel_group": 2
            },
            {
                "step_id": "4",
                "tool": "calculate_backlog_health",
                "args_template": {
                    "backlog_items": "${backlog_data.backlog_items}",
                    "total_story_points": "${backlog_data.total_story_points}",
                    "velocity": "${velocity_data.velocity}"
                },
                "purpose": "Calculate health",
                "output_var": "health_metrics",
                "dependencies": ["2", "3"],
                "parallel_group": 3
            }
        ],
        "expected_duration_seconds": 10,
        "can_parallelize": True,
        "final_output_vars": ["health_metrics"],
        "synthesis_required": False
    }


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def get_tool_definition(tool_name: str) -> Optional[BacklogToolDefinition]:
    """Get tool definition by name."""
    return BACKLOG_TOOL_DEFINITIONS.get(tool_name)


def list_all_tools() -> List[str]:
    """List all available backlog tool names."""
    return list(BACKLOG_TOOL_DEFINITIONS.keys())


def get_tools_by_category(is_llm_tool: bool = None) -> List[str]:
    """
    Filter tools by category.
    
    Args:
        is_llm_tool: If True, return only LLM tools. If False, return only non-LLM tools.
                     If None, return all tools.
    """
    if is_llm_tool is None:
        return list_all_tools()
    
    return [
        name for name, defn in BACKLOG_TOOL_DEFINITIONS.items()
        if defn.is_llm_tool == is_llm_tool
    ]


def validate_tool_args(tool_name: str, args: Dict[str, Any]) -> tuple[bool, Optional[str]]:
    """
    Validate that provided args match tool definition.
    
    Returns:
        (is_valid, error_message) - error_message is None if valid
    """
    tool_def = get_tool_definition(tool_name)
    if not tool_def:
        return False, f"Unknown tool: {tool_name}"
    
    # Check required args
    missing_args = [arg for arg in tool_def.required_args if arg not in args]
    if missing_args:
        return False, f"Missing required args for {tool_name}: {missing_args}"
    
    return True, None


def get_tool_dependencies(tool_name: str) -> List[str]:
    """Get list of dependency tools (MCP or PM Skills) needed by this tool."""
    tool_def = get_tool_definition(tool_name)
    if not tool_def:
        return []
    return tool_def.dependencies


def build_tool_registry_for_planner() -> Dict[str, Dict[str, Any]]:
    """
    Build a simplified tool registry format for LLM planner consumption.
    
    Returns dict suitable for injection into planner prompts.
    """
    registry = {}
    for name, defn in BACKLOG_TOOL_DEFINITIONS.items():
        registry[name] = {
            "description": defn.description,
            "required_args": defn.required_args,
            "optional_args": list(defn.optional_args.keys()),
            "output": list(defn.output_schema.keys()),
            "is_llm_tool": defn.is_llm_tool,
            "use_cases": defn.use_cases
        }
    return registry
