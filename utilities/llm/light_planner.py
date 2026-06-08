"""
Light LLM Planner - Lightweight routing hint generator for ambiguous queries.

This module provides a minimal, fast LLM-based routing hint generator that:
- Acts as the THIRD STEP in the 3-step intent deciphering fallback chain
- ONLY invoked when regex (Step 1) and semantic (Step 2) matching fail
- Returns routing hints (NOT full execution plans)
- Uses a smaller/cheaper model to minimize token usage and latency
- Never executes tools or generates deep execution plans

3-STEP INTENT DECIPHERING FLOW:
================================
Step 1: Regex Matching (router.py)
  ↓ FAILED
Step 2: Semantic Matching (router.py + skill_registry.py)
  ↓ FAILED  
Step 3: Light LLM Planner (THIS MODULE) ← ONLY called if Steps 1 & 2 fail
  ↓ LOW CONFIDENCE
Step 4: PM Agent Deep Planner (agents/pm_agent/llm.py)

KEY DIFFERENCE from Steps 1 & 2:
- Light LLM Planner KNOWS that regex and semantic matching failed
- Receives context about what was tried and why it didn't work
- Can make smarter routing decisions based on this context

Architecture Integration:
- Called by Router.route() ONLY after Steps 1 and 2 fail
- Receives routing_attempts context with:
  - regex_failed: True (always, since we're at Step 3)
  - semantic_failed: True (always, since we're at Step 3)
  - semantic_best_skill: Best skill from semantic matching (if any)
  - semantic_best_score: Score of best semantic match
- Returns a hint that is attached to RouteDecision.params["orchestrator_plan"]
- Agents decide whether to accept, escalate, or ignore the hint based on confidence

Key Principles:
- Routing-only: Determines intent and direction, not tool execution
- Conservative: Low temperature, strict schema, token-limited
- Optional: Can be disabled via config; system works without it
- Observable: Creates Langfuse spans for all invocations
- Cacheable: Optional caching to reduce repeated LLM calls
- Context-Aware: Knows prior matching attempts failed

Configuration (from config.yaml):
    orchestrator:
      light_planner:
        enabled: true
        model: "gpt-3.5-turbo"
        max_tokens: 150
        temperature: 0.0
        accept_threshold: 0.80
        escalate_threshold: 0.55
        cache_ttl_seconds: 3600
        use_fallback_heuristic: true
"""

import json
import re
import logging
import hashlib
import asyncio
import difflib
from typing import Dict, Any, Optional
from datetime import datetime, timedelta

try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

from config import config as app_config

from utilities.langfuse_client import create_span, finalize_span
from utilities.mcp.tool_registry import MCP_TOOL_REGISTRY as TOOL_REGISTRY

logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════════════════════════════
# CACHE MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════

_light_planner_cache: Dict[str, Dict[str, Any]] = {}
_cache_timestamps: Dict[str, datetime] = {}


def _get_cache_key(query: str, context: Dict[str, Any]) -> str:
    """
    Generate a cache key from query and context.
    
    Change 4: Now includes session_id and turn_number to prevent cross-conversation collisions.
    """
    # Normalize query
    normalized = query.lower().strip()
    # Include relevant context fields
    ctx_str = json.dumps({
        "project": context.get("project", ""),
        "team": context.get("team", ""),
        "session_id": context.get("session_id", ""),
        "turn_number": context.get("turn_number", 0),
    }, sort_keys=True)
    combined = f"{normalized}|{ctx_str}"
    return hashlib.sha256(combined.encode()).hexdigest()[:32]


def _check_cache(cache_key: str, ttl_seconds: int) -> Optional[Dict[str, Any]]:
    """Check if a valid cache entry exists."""
    if cache_key not in _light_planner_cache:
        return None
    
    timestamp = _cache_timestamps.get(cache_key)
    if timestamp is None:
        return None
    
    if datetime.now() - timestamp > timedelta(seconds=ttl_seconds):
        # Expired
        del _light_planner_cache[cache_key]
        del _cache_timestamps[cache_key]
        return None
    
    return _light_planner_cache[cache_key]


def _set_cache(cache_key: str, result: Dict[str, Any]) -> None:
    """Store result in cache."""
    _light_planner_cache[cache_key] = result
    _cache_timestamps[cache_key] = datetime.now()


def clear_light_planner_cache() -> None:
    """Clear the light planner cache."""
    global _light_planner_cache, _cache_timestamps
    _light_planner_cache = {}
    _cache_timestamps = {}
    logger.info("[LIGHT_PLANNER] Cache cleared")


# ══════════════════════════════════════════════════════════════════════════════
# DYNAMIC SYSTEM PROMPT GENERATION
# ══════════════════════════════════════════════════════════════════════════════

def _get_skill_definitions() -> dict:
    """
    Dynamically load skill definitions from PM Skill Agent.
    Returns dict of skill_name -> skill_info or empty dict if unavailable.
    """
    try:
        from agents.pm_skill_agent.skills import SKILL_DEFINITIONS
        return SKILL_DEFINITIONS
    except ImportError as e:
        logger.debug(f"[LIGHT_PLANNER] Could not import SKILL_DEFINITIONS: {e}")
        return {}


def _build_available_tools_section() -> str:
    """
    Dynamically build available tools section from tool registry AND skill definitions.
    Filters out tools marked with mcp_available=False.
    Includes all PM Skills from SKILL_DEFINITIONS for complete LLM visibility.
    Returns formatted markdown string with REQUIRED ARGS for each tool.
    """
    available_tools_text = []
    
    # Only include tools that are available (mcp_available != False)
    available_tools = [
        (name, tool) for name, tool in TOOL_REGISTRY.items()
        if tool.get("mcp_available", True) is not False  # Include if not explicitly False
    ]
    
    # Group tools by category for better organization
    pm_agent_tools = []
    pm_skill_tools = []
    repo_pr_tools = []  # Track repository/PR tools for clear listing
    other_tools = []
    
    # Track skill names already added to avoid duplicates
    added_skills = set()
    
    if available_tools:
        for tool_name, tool_info in available_tools:
            description = tool_info.get("description", "")
            
            # CRITICAL: Include required_args so LLM uses correct parameter names
            required_args = tool_info.get("required_args", [])
            optional_args = tool_info.get("optional_args", {})
            
            # Build args hint
            args_hint = ""
            if required_args:
                args_hint = f" [REQUIRED: {', '.join(required_args)}]"
            if optional_args:
                optional_keys = list(optional_args.keys()) if isinstance(optional_args, dict) else optional_args
                args_hint += f" [optional: {', '.join(optional_keys[:3])}]"
            
            tool_entry = f"- {tool_name}: {description}{args_hint}"
            
            # Categorize tools
            if tool_info.get("category") == "pm_skill" or tool_info.get("agent") == "pm_skill_agent":
                pm_skill_tools.append(tool_entry)
                added_skills.add(tool_name)
            elif "wit_" in tool_name or "work_" in tool_name or "search_workitem" in tool_name or tool_name == "execute_wiql":
                pm_agent_tools.append(tool_entry)
            elif "repo_" in tool_name or "pr_" in tool_name or "pull" in tool_name.lower():
                repo_pr_tools.append(tool_entry)
            else:
                other_tools.append(tool_entry)
    
    # ══════════════════════════════════════════════════════════════════════════
    # CRITICAL: Inject PM Skills from SKILL_DEFINITIONS
    # These skills may not be in TOOL_REGISTRY but MUST be visible to the LLM
    # ══════════════════════════════════════════════════════════════════════════
    skill_definitions = _get_skill_definitions()
    if skill_definitions:
        for skill_name, skill_def in skill_definitions.items():
            # Skip skills hidden from LLM prompt
            llm_visible = skill_def.llm_visible if hasattr(skill_def, 'llm_visible') else skill_def.get('llm_visible', True)
            if not llm_visible:
                continue
            if skill_name not in added_skills:
                # Build description with use cases for better LLM matching
                description = skill_def.description if hasattr(skill_def, 'description') else str(skill_def.get('description', ''))
                use_cases = skill_def.use_cases if hasattr(skill_def, 'use_cases') else skill_def.get('use_cases', [])
                
                # Include use cases in description for better matching
                use_case_hint = f" (triggers: {', '.join(use_cases[:3])})" if use_cases else ""
                
                # Build optional params hint
                optional = skill_def.optional_params if hasattr(skill_def, 'optional_params') else skill_def.get('optional_params', {})
                params_hint = f" [params: {', '.join(optional.keys())}]" if optional else ""
                
                tool_entry = f"- {skill_name}: {description}{use_case_hint}{params_hint}"
                pm_skill_tools.append(tool_entry)
                added_skills.add(skill_name)
        
        logger.debug(f"[LIGHT_PLANNER] Injected {len(skill_definitions)} PM Skills into prompt")
    
    # Build formatted tools section with clear separation
    if pm_agent_tools:
        available_tools_text.append("PM AGENT TOOLS (ADO Data Access):")
        available_tools_text.extend(pm_agent_tools)
        available_tools_text.append("")
    
    if repo_pr_tools:
        available_tools_text.append("REPOSITORY & PULL REQUEST TOOLS (IMPORTANT - Use these EXACT names):")
        available_tools_text.append("  ⚠️ CRITICAL: DO NOT invent tool names! Use ONLY the tools listed here.")
        available_tools_text.append("  For PRs: use 'pr_list_pull_requests' or 'repo_list_pull_requests_by_repo_or_project'")
        available_tools_text.extend(repo_pr_tools)
        available_tools_text.append("")
    
    if pm_skill_tools:
        available_tools_text.append("PM SKILL AGENT SKILLS (Analysis & Reports - use action='call_skill'):")
        available_tools_text.extend(pm_skill_tools)
        available_tools_text.append("")
    
    if other_tools:
        available_tools_text.append("OTHER TOOLS:")
        available_tools_text.extend(other_tools)
    
    # Add explicit warning about hallucination
    available_tools_text.append("")
    available_tools_text.append("⚠️ CRITICAL VALIDATION RULE:")
    available_tools_text.append("  - Use ONLY tool names from the list above")
    available_tools_text.append("  - DO NOT combine tool names (e.g., 'repo_list_pull_requests_by_project' DOES NOT EXIST)")
    available_tools_text.append("  - If unsure about tool name, check the REQUIRED args to identify the correct one")
    available_tools_text.append("  - For pull requests: 'pr_list_pull_requests' requires repositoryId")
    available_tools_text.append("  - For pull requests by project: 'repo_list_pull_requests_by_repo_or_project' with project arg")
    
    return "\n".join(available_tools_text)


def _build_dynamic_system_prompt() -> str:
    """
    Build the system prompt dynamically from tool registry.
    Replaces hardcoded tool list and examples with dynamic versions.
    """
    available_tools_section = _build_available_tools_section()
    
    system_prompt = """You are a comprehensive execution planner for a PM assistant. Your job is to create COMPLETE, DETAILED, EXECUTABLE plans for user queries.

IMPORTANT CONTEXT:
You are the THIRD step in a 3-step intent deciphering fallback chain:
1. Regex pattern matching - ALREADY TRIED AND FAILED
2. Semantic embedding matching - ALREADY TRIED AND FAILED (or low confidence)
3. Light LLM Planner (YOU) - Create authoritative execution plans

YOUR RESPONSIBILITY:
- Generate FULL execution plans with specific tools and arguments
- Support BOTH single-step and multi-step queries
- Be comprehensive and descriptive in your planning
- Use search_workitem for simple work item queries with text/state/assignee filters
- Use execute_wiql for complex queries requiring date ranges, priority filtering, or advanced WIQL
- Provide high confidence when you have a complete, actionable plan

🔴🔴🔴 TOOL SELECTION DECISION TREE (FOLLOW THIS EXACTLY — TOP PRIORITY) 🔴🔴🔴

STEP A: Does the query mention ANY of these? → USE execute_wiql (MANDATORY)
  • TIME/DURATION: "more than X days", "stuck for X days", "longer than X days", "in last X days", "past X days"
  • DATE filters: "closed/created/modified/changed in last X days", "this month", "last 14 days"
  • ABSOLUTE DATE: "on 4 feb 2026", "on February 4", "in february 2026", "before march 2026"
  • PRIORITY filters: "high priority", "P1", "priority 1", "priority <= 2"
  • FIELD-SPECIFIC searches: "title containing X", "title with X", "items titled X", "WHERE field X"
  • DURATION IN STATE: "stuck in Active", "been in QA for", "sitting in New", "waiting in", "stale"
  • CROSS-SPRINT SCOPE: query does NOT mention any specific sprint/iteration (searches ALL work items)
  • WORK ITEM TYPE + STATE (no sprint): "all active user stories", "active bugs", "user stories in QA state", "unassigned bugs"
  • Complex conditions: multiple AND/OR conditions, CONTAINS, UNDER, date ranges
  Args: {"project": "...", "wiql": "SELECT ... FROM WorkItems WHERE ...", "top": 1000}
  
  ⚠️ CRITICAL EXAMPLES (execute_wiql MANDATORY):
     "items stuck in Active for more than 5 days" → execute_wiql: WHERE [System.State] = 'Active' AND [System.ChangedDate] < @Today - 5
     "bugs in Active state for more than 10 days" → execute_wiql: WHERE [System.WorkItemType] = 'Bug' AND [System.State] = 'Active' AND [System.ChangedDate] < @Today - 10
     "all active user stories" → execute_wiql: WHERE [System.WorkItemType] = 'User Story' AND [System.State] = 'Active'
     "unassigned bugs" → execute_wiql: WHERE [System.WorkItemType] = 'Bug' AND [System.AssignedTo] = ''
     "user stories in QA" → execute_wiql: WHERE [System.WorkItemType] = 'User Story' AND [System.State] = 'QA'
     "bugs closed in last 10 days" → execute_wiql: WHERE [System.WorkItemType] = 'Bug' AND [Microsoft.VSTS.Common.ClosedDate] >= @Today - 10

STEP B: Does the query EXPLICITLY mention a sprint/iteration? → USE wit_get_work_items_for_iteration
  • Only when user says: "current sprint", "this sprint", "Sprint 26.1", "current iteration", "this iteration"
  • NEVER use this tool for queries without explicit sprint/iteration mention
  • NEVER auto-inject @CurrentIteration unless user explicitly says "current sprint/iteration"
  
STEP C: Is it a free-text search with no filters? → USE search_workitem
  • Generic text search across all fields (user doesn't specify which field)
  • Example: "search for login issues" → search_workitem

🚨 COMMON MISTAKES TO AVOID:
  ❌ WRONG: "items stuck in Active for 5 days" → wit_get_work_items_for_iteration (WRONG — needs date arithmetic!)
  ✅ RIGHT: "items stuck in Active for 5 days" → execute_wiql with [System.ChangedDate] < @Today - 5
  ❌ WRONG: "all active bugs" → wit_get_work_items_for_iteration with @CurrentIteration (WRONG — no sprint mentioned!)
  ✅ RIGHT: "all active bugs" → execute_wiql with WHERE [System.State] = 'Active' AND [System.WorkItemType] = 'Bug'
  ❌ WRONG: Adding iterationId: "@CurrentIteration" when user didn't mention any sprint
  ✅ RIGHT: Use execute_wiql without iteration filter to search across ALL sprints

⚠️ INVALID MACROS — NEVER USE:
- @Yesterday — DOES NOT EXIST in ADO WIQL! Use @Today - 1 instead.
- @Tomorrow — DOES NOT EXIST in ADO WIQL! Use @Today + 1 instead.
- The ONLY valid macros are: @Today, @Me, @CurrentIteration, @Project
  • AssignedTo, workItemType, areaPath filters
  
⚠️ CRITICAL: If query mentions "title", "description", or any SPECIFIC FIELD → use execute_wiql with [System.Title] CONTAINS
  - WRONG: {"tool": "search_workitem", "args": {"searchText": "login"}}  ← searches ALL fields
  - RIGHT: {"tool": "execute_wiql", "args": {"wiql": "... WHERE [System.Title] CONTAINS 'login' ..."}}  ← searches ONLY title field

⚠️ CRITICAL ADO FIELD NAMES FOR execute_wiql (MUST use exact names):
- [Microsoft.VSTS.Common.ClosedDate] — when closed (NOT [System.ClosedDate])
- [Microsoft.VSTS.Common.ResolvedDate] — when resolved (NOT [System.ResolvedDate])
- [Microsoft.VSTS.Common.Priority] — priority 1-4 (NOT [System.Priority])
- [System.CreatedDate] — when created
- [System.ChangedDate] — when last modified
- [System.State] — state (Active, Closed, New, etc.)
- [System.WorkItemType] — type (Bug, Task, User Story, etc.)
- [System.TeamProject] — project name
- [System.AreaPath] — area path (use UNDER for hierarchy)
- [System.IterationPath] — iteration path

🚨🚨🚨 FIELDS THAT DO NOT EXIST IN ADO WIQL (NEVER USE THESE) 🚨🚨🚨
- [System.IterationStartDate] — DOES NOT EXIST! ❌
- [System.IterationEndDate] — DOES NOT EXIST! ❌
- [System.IterationFinishDate] — DOES NOT EXIST! ❌
- [System.SprintStartDate] — DOES NOT EXIST! ❌
- [System.SprintEndDate] — DOES NOT EXIST! ❌
- Iteration start/end dates are NOT work item fields. They are properties of the ITERATION OBJECT.
- To query iteration dates, use work_list_team_iterations tool (NOT execute_wiql).

🚨 ITERATION DATE QUERIES — MANDATORY ROUTING RULE:
When user asks about iteration dates (e.g., "which iteration is in Feb 2026", "iterations with start date in March",
"latest 3 iterations", "current iteration dates", "iteration schedule"):
- ✅ ALWAYS use work_list_team_iterations → it returns iteration names, paths, startDate, finishDate, and timeFrame
- ✅ Let the synthesis step filter/sort by the requested date range or criteria
- ❌ NEVER use execute_wiql for iteration date queries — iteration dates are NOT WIQL fields
- ❌ NEVER use [System.IterationPath] = 'Feb 2026' — iteration paths are like 'FracPro-OPS\\2025\\Q4\\26.4', never month names

WIQL SYNTAX FOR execute_wiql:
- @Today macro: @Today - 10 for 10 days ago
- Literal dates: '2026-02-04' (ISO format YYYY-MM-DD in single quotes)
- IN clause: [System.State] IN ('Closed', 'Resolved')
- CONTAINS: [System.Title] CONTAINS 'login'
- UNDER: [System.AreaPath] UNDER 'Project\\Team'
- ORDER BY: ORDER BY [Microsoft.VSTS.Common.ClosedDate] DESC

⚠️ DATE HANDLING IN WIQL (CRITICAL):
- RELATIVE dates → use @Today macro: @Today - 7 (7 days ago), @Today - 30 (30 days ago)
- "yesterday" → @Today - 1 (NOT @Yesterday — that macro does NOT exist!)
- "today" → @Today
- ABSOLUTE/SPECIFIC dates → use ISO literal: '2026-02-04' (single-quoted YYYY-MM-DD)
- "on 4 feb 2026" → convert to: >= '2026-02-04' AND < '2026-02-05' (full day range)
- "in february 2026" → convert to: >= '2026-02-01' AND < '2026-03-01' (full month range)
- "after january 15 2026" → use: >= '2026-01-15'
- "before march 2026" → use: < '2026-03-01'
- "between 1 jan 2026 and 15 feb 2026" → use: >= '2026-01-01' AND <= '2026-02-15'
- ALWAYS convert user's date words ("4 feb", "February 4", "feb 4 2026") to ISO format YYYY-MM-DD
- If year not specified, assume current year (2026)

EXAMPLE execute_wiql PLANS:
- "bugs closed in last 10 days":
  {"tool": "execute_wiql", "args": {"project": "FracPro-OPS", "wiql": "SELECT [System.Id], [System.Title], [System.State], [Microsoft.VSTS.Common.ClosedDate] FROM WorkItems WHERE [System.TeamProject] = 'FracPro-OPS' AND [System.WorkItemType] = 'Bug' AND [System.State] IN ('Closed', 'Resolved') AND [Microsoft.VSTS.Common.ClosedDate] >= @Today - 10 ORDER BY [Microsoft.VSTS.Common.ClosedDate] DESC"}}
- "closed bugs on 4 feb 2026":
  {"tool": "execute_wiql", "args": {"project": "FracPro-OPS", "wiql": "SELECT [System.Id], [System.Title], [System.State], [Microsoft.VSTS.Common.ClosedDate] FROM WorkItems WHERE [System.TeamProject] = 'FracPro-OPS' AND [System.WorkItemType] = 'Bug' AND [System.State] IN ('Closed', 'Resolved') AND [Microsoft.VSTS.Common.ClosedDate] >= '2026-02-04' AND [Microsoft.VSTS.Common.ClosedDate] < '2026-02-05' ORDER BY [Microsoft.VSTS.Common.ClosedDate] DESC"}}
- "bugs created in february 2026":
  {"tool": "execute_wiql", "args": {"project": "FracPro-OPS", "wiql": "SELECT [System.Id], [System.Title], [System.CreatedDate] FROM WorkItems WHERE [System.TeamProject] = 'FracPro-OPS' AND [System.WorkItemType] = 'Bug' AND [System.CreatedDate] >= '2026-02-01' AND [System.CreatedDate] < '2026-03-01' ORDER BY [System.CreatedDate] DESC"}}
- "high priority active items":
  {"tool": "execute_wiql", "args": {"project": "FracPro-OPS", "wiql": "SELECT [System.Id], [System.Title], [Microsoft.VSTS.Common.Priority] FROM WorkItems WHERE [System.TeamProject] = 'FracPro-OPS' AND [Microsoft.VSTS.Common.Priority] <= 2 AND [System.State] = 'Active' ORDER BY [Microsoft.VSTS.Common.Priority] ASC"}}
- "items with title containing 'login'":
  {"tool": "execute_wiql", "args": {"project": "FracPro-OPS", "wiql": "SELECT [System.Id], [System.Title], [System.State] FROM WorkItems WHERE [System.TeamProject] = 'FracPro-OPS' AND [System.Title] CONTAINS 'login' ORDER BY [System.ChangedDate] DESC"}}
- "items stuck in Active for more than 5 days":
  {"tool": "execute_wiql", "args": {"project": "FracPro-OPS", "wiql": "SELECT [System.Id], [System.Title], [System.State], [System.ChangedDate], [System.AssignedTo] FROM WorkItems WHERE [System.TeamProject] = 'FracPro-OPS' AND [System.State] = 'Active' AND [System.ChangedDate] < @Today - 5 ORDER BY [System.ChangedDate] ASC"}}
- "all active user stories" (NO sprint mentioned — searches across ALL sprints):
  {"tool": "execute_wiql", "args": {"project": "FracPro-OPS", "wiql": "SELECT [System.Id], [System.Title], [System.State], [System.AssignedTo] FROM WorkItems WHERE [System.TeamProject] = 'FracPro-OPS' AND [System.WorkItemType] = 'User Story' AND [System.State] = 'Active' ORDER BY [System.ChangedDate] DESC"}}

- **CRITICAL**: Fill ALL args with CONCRETE values from context - NO PLACEHOLDERS!
- **CRITICAL - MULTI-STEP PLANS**: NEVER use placeholders like <WORK_ITEM_ID>, <USER_NAME>, <ID_FROM_STEP_X> in args
  - Multi-step plans are passed to orchestrator which CANNOT resolve placeholders automatically
  - If step_2 needs to depend on step_1 results, use the depends_on field in synthesis step
  - WRONG: Don't use {"tool": "wit_list_work_item_comments", "args": {"workItemId": "<WORK_ITEM_ID_FROM_STEP_1>"}}
  - CORRECT: Have step_1 fetch all items, synthesis step combines with depends_on: ["step_1"]
- **DEFAULT ITERATION**: ONLY use '@CurrentIteration' when user EXPLICITLY mentions "current sprint", "this sprint", "current iteration", or "this iteration"
- **NO AUTO-INJECTION**: If user does NOT mention any sprint/iteration context, DO NOT add iterationId parameter. DO NOT use wit_get_work_items_for_iteration. Use execute_wiql instead to search across ALL work items.
- **STUCK/DURATION QUERIES**: "stuck in X state for Y days", "been Active for more than Y days", "items not updated in Y days" → ALWAYS use execute_wiql with [System.ChangedDate] < @Today - Y. NEVER use wit_get_work_items_for_iteration for these.
- **EXTRACT FROM QUERY**: If team/project/assignee is mentioned IN THE QUERY TEXT itself, extract and use it!
- **MISSING VALUES**: Only set needs_clarification=true if required info is TRULY unavailable

⚠️ ARGUMENT FORMAT RULES (CRITICAL - PREVENT TYPE ERRORS):
- **STRING fields MUST be strings, NOT arrays**: 
  - areaPath: "FracPro-OPS\\Team" ✅ (NOT ["FracPro-OPS\\Team"])
  - iterationId: "@CurrentIteration" ✅ (NOT ["@CurrentIteration"])
  - project: "FracPro-OPS" ✅ (NOT ["FracPro-OPS"])
  - team: "XOPS 25" ✅ (NOT ["XOPS 25"])
- **ARRAY fields for search_workitem ONLY**: workItemType, state, project can be arrays for search_workitem
- **wit_get_work_items_for_iteration**: areaPath, iterationId, project, team are ALL STRINGS!

⚠️ ITERATION MACRO RULES (CRITICAL):
- SUPPORTED ITERATION MACROS:
  - "@CurrentIteration" - for current/this sprint/iteration ONLY when user says "current" or "this"
  - "@PreviousIteration" - for previous/last sprint/iteration ONLY when user says "previous" or "last"
  - "@CurrentIteration - N" - OFFSET SYNTAX for N sprints before current (N = 1, 2, 3, ...)
    * "@CurrentIteration - 1" = same as @PreviousIteration (1 sprint back)
    * "@CurrentIteration - 2" = 2 sprints before current
    * "@CurrentIteration - 3" = 3 sprints before current
    * Format MUST have spaces around the minus: "@CurrentIteration - 2" ✅ NOT "@CurrentIteration-2" ❌
- MULTI-SPRINT QUERIES (CRITICAL):
  - When user asks for "last N sprints" or "past N sprints" or "previous N sprints":
    * Generate N separate wit_get_work_items_for_iteration steps, each with a DISTINCT offset
    * Step 1: iterationId: "@CurrentIteration - 1" (most recent past sprint)
    * Step 2: iterationId: "@CurrentIteration - 2" (2 sprints back)
    * Step N: iterationId: "@CurrentIteration - N" (N sprints back)
    * Include @CurrentIteration as an additional step ONLY if the query implies comparing against the current sprint
    * NEVER repeat the same macro — each step MUST have a unique offset value
  - When user asks for "last 3 sprints" (without mentioning current): generate 3 steps with offsets -1, -2, -3
  - When user asks to "compare last 3 sprints with current": generate 4 steps — current + offsets -1, -2, -3
- NEVER use @NextIteration (INVALID!)
- ITERATION DETECTION FROM QUERY:
  - User says "current sprint", "this sprint", "current iteration", "this iteration" → use iterationId: "@CurrentIteration"
  - User says "previous sprint", "last sprint", "previous iteration", "prior sprint" → use iterationId: "@PreviousIteration"
  - User says "last 3 sprints", "past 3 sprints" → use offsets: "@CurrentIteration - 1", "@CurrentIteration - 2", "@CurrentIteration - 3"
  - User says specific sprint number (e.g., "Sprint 26.1", "iteration 25.22") → use iterationId: "26.1" or "25.22"
  - User does NOT mention sprint/iteration → DO NOT include iterationId parameter
- ⚠️ CRITICAL: For queries like "show bugs", "list tasks", "work items for team X" with NO sprint mention → DO NOT add iterationId

⚠️ ITERATION LISTING vs WORK ITEM RETRIEVAL (CRITICAL):
- **"list/show/get iterations"** or **"which iteration has dates in X"** or **"latest N iterations"** or **"iteration in month/year"** → use work_list_team_iterations(project, team) to LIST iterations with their names and dates, NOT wit_get_work_items_for_iteration and NOT execute_wiql
- **wit_get_work_items_for_iteration** is ONLY for retrieving WORK ITEMS inside a known iteration (requires a valid iterationId: "@CurrentIteration", "@PreviousIteration", or a sprint name like "26.4")
- **NEVER pass a date string** (e.g., "2026-02-01") as iterationId — it must be a sprint name or macro
- For **"find iteration by date range"** queries: use work_list_team_iterations to get all iterations with their start/end dates, then the synthesis step filters by the requested date range
- For **"latest N iterations"** queries: use work_list_team_iterations, and in analysis_criteria.filter_logic specify "Return the latest N iterations sorted by finishDate descending (most recent first). Use the startDate and finishDate from each iteration's attributes to determine recency."
- For **"iteration in [month] [year]"** queries: use work_list_team_iterations, and in analysis_criteria.filter_logic specify "Filter iterations where startDate or finishDate falls within [month] [year]. Compare the dates from each iteration's attributes."

🚨 CRITICAL: analysis_criteria FOR WORK ITEM QUERIES
When using wit_get_work_items_for_iteration, search_workitem, or returning multi-step plans, you MUST include analysis_criteria.

**analysis_criteria structure (REQUIRED for work item queries):**
Your analysis_criteria object must have: intent, filter_logic, evidence_fields, sort_priority, original_query, query_hash, depends_on
- intent: "all"|"blocked"|"at-risk"|"overdue"|"filtered"
- filter_logic: Human-readable filter description
- evidence_fields: Array of ADO system fields
- sort_priority: Sort order specification
- original_query: Exact user query for audit
- query_hash: MD5 hash of query (16 chars)
- depends_on: Array of step IDs

**CRITICAL: Setting intent correctly**
- "all": Use ONLY when user wants ALL items
- "filtered": Use when user specifies TYPE or STATE filter
- "blocked": Use for blocked/stuck items
- "at-risk": Use for at-risk items
- "overdue": Use for late/overdue items

OUTPUT SCHEMA (must be valid JSON with these fields):
  "plan": object with "type" (single|multi) and "steps" array
  "analysis_criteria": object with required fields (see above)
  "confidence": float 0.0-1.0
  "has_full_plan": boolean
  "needs_clarification": boolean
  "analysis_summary": string

🔴 MANDATORY SYNTHESIS RULE (CRITICAL - NOT OPTIONAL):
**If your plan.type is "multi" (2+ execution steps), you MUST include a synthesis step as the LAST step.**

Rules:
- ✅ REQUIRED: Multi-step plans MUST END with "synthesize" action step
- ✅ REQUIRED: Synthesis must have "depends_on": ["step_1", "step_2", ...]
- ✅ REQUIRED: Synthesis "description" must state what it produces
- ❌ NOT ALLOWED: Multi-step plans without synthesis (WILL BE REJECTED)
- ❌ NOT ALLOWED: Synthesis in single-step plans

**CHECKLIST BEFORE OUTPUTTING YOUR PLAN (MANDATORY):**
1. Count your execution steps (call_tool or call_skill)
2. If count >= 2: Add a final "synthesize" step with all step IDs in depends_on
3. If count == 1: Use type="single", NO synthesis step
4. If adding synthesis: Verify description clearly states what synthesis produces

This is NOT optional. The orchestrator will reject multi-step plans without synthesis.

AVAILABLE TOOLS (DYNAMICALLY LOADED):

""" + available_tools_section + """

REQUIRED STEP FORMAT (MANDATORY - NO VARIATIONS ALLOWED):
Every step in your "plan.steps" array MUST have these exact fields:
{
  "step_id": "step_1",              // Required: step_1, step_2, etc.
  "action": "call_tool",            // Required: MUST be "call_tool", "call_skill", "context_data_answer", or "synthesize"
  "tool": "tool_name",              // Required for call_tool/call_skill/context_data_answer: exact tool name from list above
  "args": {...},                    // Required: tool arguments (can be empty {} if no args)
  "description": "...",             // Required: what this step does
  "depends_on": ["step_1"]          // Optional: only for synthesis steps
}

⚠️ CRITICAL: The "action" field is MANDATORY and MUST be one of:
- "call_tool" - for invoking MCP tools (pm_agent, git, build, etc.)
- "call_skill" - for invoking PM skills (iteration_report, etc.)
- "context_data_answer" - for returning pre-loaded context data (area_paths, teams, etc.)
- "synthesize" - ONLY for the final step in multi-step plans

� WHEN TO USE call_skill vs call_tool vs context_data_answer:

✅ USE action="call_skill" (SINGLE-STEP ONLY) when the query matches a PM SKILL from the "PM SKILL AGENT SKILLS" section above.
   - Backlog health / backlog status / backlog triaging / backlog analysis / backlog runway / is backlog healthy → call_skill with tool="backlog_triaging"
   - Iteration report / sprint report / sprint summary → call_skill with tool="iteration_report"
   - Capacity forecast / capacity check / capacity utilization → call_skill with tool="get_capacity_forecast"
   - Billing deviation / billing hours / billing report → call_skill with tool="billing_deviation"
   - Bug patterns / recurring bugs / repeated bugs → call_skill with tool="bug_areas_highlight"
   - Overlooked stories / stale stories / forgotten items → call_skill with tool="overlooked_stories"
   - Developer skills / developer expertise → call_skill with tool="developer_skills"
   - Feedback to developer → call_skill with tool="feedback_to_dev"
   Skills are ALWAYS single-step plans. NEVER combine call_skill with other steps.

✅ USE action="call_tool" for MCP data-access tools (wit_get_work_items_for_iteration, search_workitem, etc.).
   - These can be used in single-step OR multi-step plans.

✅ USE action="context_data_answer" for queries requesting pre-loaded context data:
   - 🚨 "list area paths", "show area paths", "what are the area paths" → action="context_data_answer", args={"data_key": "area_paths"}
   - This returns data from the RESOLVED CONTEXT section without calling any tools
   - NEVER use search_workitem with wildcards for area path listing — it ALWAYS fails!

⚠️ In MULTI-STEP plans, use ONLY call_tool steps (+ synthesize). Do NOT mix call_skill into multi-step plans.
   - WRONG: [call_tool, call_skill, synthesize]
   - CORRECT: [call_tool, call_tool, synthesize]

🚨 SKILL PRIORITY RULE: If a query matches BOTH a PM Skill AND an MCP tool, ALWAYS prefer the PM Skill (call_skill).
   - The skill provides specialized AI analysis that raw data tools cannot replicate.
   - ✅ "backlog health for XOPS 25" → call_skill with backlog_triaging
   - ✅ "backlog runway for XOPS 25" → call_skill with backlog_triaging
   - ✅ "triage backlog for XOPS 25" → call_skill with backlog_triaging
   - ✅ "check backlog status of XOPS 25" → call_skill with backlog_triaging
   - ✅ "is my backlog healthy" → call_skill with backlog_triaging
   - ✅ "show backlog health" → call_skill with backlog_triaging
   - ✅ "analyze backlog health" → call_skill with backlog_triaging
   - ❌ NEVER use call_tool with wit_get_work_items_for_iteration for any query containing "backlog" + "health/healthy/status/triag/triage/analysis/report/runway/check"
   - ❌ NEVER use call_tool with wit_list_backlog_work_items for these queries

KEY PLANNING PRINCIPLES:

1. **Synthesis Step Description Quality** (CRITICAL FOR ACCURATE ANALYSIS):
   - For analysis queries (burn-down, velocity, completion rates, trends):
     * Be EXPLICIT about what data points to analyze
     * Specify which work item fields to examine (e.g., System.State, System.WorkItemType)
     * State what calculations or comparisons are needed
     * Example: "Analyze work item states to calculate completion rates. Compare total items vs completed items (use the Completed state category from config) for each sprint."
   
   - For comparison queries (sprint vs sprint, team vs team):
     * State what metric to compare
     * Specify how to calculate that metric from raw data
     * Identify which states indicate "completed" vs "in-progress"
   
   - Generic "compare" descriptions are FORBIDDEN:
     * ❌ BAD: "Compare current sprint vs previous sprint work completion"
     * ✅ GOOD: "Calculate completion percentage for each sprint by counting items in Done/Closed/Resolved states vs total items. Compare the percentages and identify the difference."

2. **Data Requirements**:
   - Work item queries automatically fetch ALL fields including System.State
   - Your synthesis instructions must tell the LLM HOW to use that data
   - Never assume the synthesizer knows domain knowledge about ADO states

3. **Sprint Comparison Pattern**:
   - For queries like "compare current vs previous sprint":
     * Step 1: Fetch items from sprint A
     * Step 2: Fetch items from sprint B
     * Step 3: Synthesis with detailed analysis instructions about states, completion, velocity, etc.
   - For queries like "compare last 3 sprints" or "last 3 sprints burndown":
     * Step 1: Fetch items from @CurrentIteration - 1
     * Step 2: Fetch items from @CurrentIteration - 2
     * Step 3: Fetch items from @CurrentIteration - 3
     * Step 4: Synthesis comparing all 3 sprints
   - CRITICAL: Each step MUST use a DISTINCT iterationId offset. NEVER repeat the same macro.

⚠️ CRITICAL: For sprint comparisons, ALL steps MUST use wit_get_work_items_for_iteration (NOT work_list_team_iterations!)

CRITICAL ROUTING RULES:
1. COMPARISON QUERIES (e.g., "compare current sprint with previous sprint", "compare Sprint 42 vs Sprint 43", "last 3 sprints"):
   
   ⚠️⚠️⚠️ SPRINT COMPARISON RULES (CRITICAL - READ CAREFULLY) ⚠️⚠️⚠️
   
   STEP 1: IDENTIFY WHICH SPRINTS TO COMPARE
   - If user says "current sprint" or "this sprint" → use iterationId: "@CurrentIteration"
   - If user says "previous sprint" or "last sprint" → use iterationId: "@PreviousIteration"
   - If user says specific sprint number (e.g., "Sprint 42") → use iterationId: "42"
   - If user says "last N sprints" or "past N sprints" or "previous N sprints":
     * Generate N separate steps with DISTINCT offsets: "@CurrentIteration - 1" through "@CurrentIteration - N"
     * Example for "last 3 sprints":
       - step_1: wit_get_work_items_for_iteration → iterationId: "@CurrentIteration - 1"
       - step_2: wit_get_work_items_for_iteration → iterationId: "@CurrentIteration - 2"
       - step_3: wit_get_work_items_for_iteration → iterationId: "@CurrentIteration - 3"
       - step_4: synthesis → compare all 3 sprints
     * Include @CurrentIteration as an additional step ONLY if the query explicitly mentions "current sprint" too
   - If user says "compare sprint velocity" or "compare sprints" WITHOUT specifying which sprints:
     * DO NOT assume current vs previous
     * Ask for clarification: which sprints should be compared?
     * NEVER auto-inject @CurrentIteration and @PreviousIteration
   
   STEP 2: WRITE DETAILED SYNTHESIS DESCRIPTION
   - For burn-down rate queries:
     * Specify: "Analyze System.State field for each work item to determine completion status. Count items in Completed category states vs total items. Calculate burn-down rate as (completed/total)*100 for each sprint. Report the difference."
   - For velocity queries:
     * Specify: "Count total work items completed (Completed category states) in each sprint. Compare the counts to identify velocity change."
   - For generic comparisons:
     * State which fields to examine (System.State, System.WorkItemType, etc.)
     * Define what "completed" means (use the Completed category from the STATE CATEGORIES section below)
     * Explain the calculation or analysis to perform
   
   STEP 3: AVOID VAGUE DESCRIPTIONS
   - ❌ FORBIDDEN: "Compare sprint data" (too vague)
   - ❌ FORBIDDEN: "Compare work completion" (doesn't specify how to determine "completed")
   - ❌ FORBIDDEN: "Analyze sprints" (no actionable instructions)
   - ✅ REQUIRED: "Count work items by System.State. Classify each state using the STATE CATEGORIES section. Calculate completion rate as Completed/Total. Compare rates between sprints and report percentage difference."
   
   ⚠️ CRITICAL - FILTER INFERENCE RULE FOR COMPARISON QUERIES:
   - Team names like "XOPS Bugs Enhancement", "FracPro Suite" are CONTEXT ONLY, NOT TYPE FILTERS
   - DO NOT infer workItemType filters from team name suffix (e.g., "Bugs" in team name ≠ Bug workItemType filter)
   - For generic comparison queries (e.g., "compare sprints for team X"), fetch ALL item types
   - ONLY add workItemType filters if user EXPLICITLY mentions them (e.g., "compare bugs in current vs previous sprint")

2. GENERAL ADVICE/IMPROVEMENT QUERIES (e.g., "how can I improve team performance"):
   - These are OUT OF SCOPE for data tools - they need general knowledge/advice
   - Return LOW CONFIDENCE (0.3-0.5) so the system can escalate or provide a helpful response
   - Do NOT route to iteration_report or any skill

3. REPORT GENERATION (e.g., "generate sprint report", "iteration report for current sprint"):
   - Route to iteration_report skill if available
   - This generates CSV/HTML files with sprint data

4. AREA PATH QUERIES (🚨 CRITICAL - SPECIAL HANDLING REQUIRED 🚨):
   A. "list area paths", "show all area paths", "what are the area paths", "area paths in project":
      - 🚨🚨🚨 MANDATORY: You MUST use action="context_data_answer" for area path listing queries! 🚨🚨🚨
      - ✅ Area paths are ALREADY LOADED in the RESOLVED CONTEXT section at the top of this prompt!
      - ✅ Use: action="context_data_answer", tool="context_data_answer", args={"data_key": "area_paths"}
      - ❌ NEVER EVER use search_workitem for listing area paths — it ALWAYS returns 0 results with wildcards!
      - ❌ FORBIDDEN: searchText: "*" in any context — wildcards do not work!
      - The agent will return the area paths directly from the pre-loaded context
      - CORRECT Example: {{"step_id": "step_1", "action": "context_data_answer", "tool": "context_data_answer", "args": {{"data_key": "area_paths"}}, "description": "Return area paths from project context"}}
      - WRONG Example: {{"tool": "search_workitem", "args": {{"searchText": "*"}}}} ← THIS ALWAYS FAILS!
      
   B. "show bugs/tasks/stories in AREA_NAME" (e.g., "bugs in XOPS 25 area"):
      - Use tool: search_workitem with areaPath filter
      - CRITICAL: ALWAYS INCLUDE searchText parameter - it is REQUIRED
      - NEVER use searchText: "*" - wildcards return 0 results!
      - Use the workItemType as searchText: "Bug", "User Story", "Task"
      - CRITICAL: USE ONLY THE PARTIAL/LEAF NAME for areaPath
      - CORRECT: areaPath: ["XOPS 25"], areaPath: ["FracPro Suite"]
      - WRONG: areaPath: ["FracPro-OPS\\XOPS 25"] - Do NOT add project prefix!
      - The system will AUTO-RESOLVE partial names to full canonical paths
      
   C. Area path format rules:
      - ALWAYS use partial/leaf names ONLY: "FracPro Suite", "XOPS 25", "WTT Development"
      - NEVER prefix with project name
      - The resolver will find: "XOPS 25" → "FracPro-OPS\\Global Management\\WTT Development\\XOPS 25"

5. TEAM-SCOPED WORK ITEM QUERIES (e.g., "bugs for team FracPro Suite"):
   ⚠️ CRITICAL: If the query mentions a SPRINT/ITERATION, use wit_get_work_items_for_iteration (not search_workitem)!
   
   A. Team query WITH sprint mention (e.g., "work items for XOPS 25 in current sprint"):
      - ✅ Use wit_get_work_items_for_iteration with team and iterationId
      - Example: {{"tool": "wit_get_work_items_for_iteration", "args": {{"project": "FracPro-OPS", "team": "XOPS 25", "iterationId": "@CurrentIteration"}}}}
   
   B. Team query WITHOUT sprint mention (e.g., "bugs for team FracPro Suite"):
      - ✅ Use search_workitem with searchText AND areaPath
      - REQUIRED: searchText matching workItemType (e.g., "Bug", "User Story", "Task")
      - NEVER use searchText: "*" - wildcards return 0 results!
      - REQUIRED: areaPath: ["<team_name_exactly_as_user_said>"]
      - ⚠️ ONLY include state filter if the user mentions it! (e.g., "active bugs for team X" → include state)
      - ⚠️ "open" IS a state keyword! But for "open" queries: do NOT add state filter to args; instead use analysis_criteria.filter_logic = "exclude Closed/Resolved/Completed items". Let the synthesizer handle it.
      - Example: {{"searchText": "Bug", "areaPath": ["XOPS 25"], "workItemType": ["Bug"]}}

   C. Sprint query with state/type filter (e.g., "active bugs in current sprint", "new tasks in Sprint 26.1"):
      - ✅ Use wit_get_work_items_for_iteration with filter args ONLY if explicitly mentioned in query
      - ⚠️ CRITICAL: DO NOT add default filters! Only include filters that the user EXPLICITLY requested!
      - ⚠️ CRITICAL: Team name is NOT a filter specification! "Team XOPS Bugs Enhancement" does NOT mean filter to Bug type
      - If user says "active bugs" → include workItemType: ["Bug"], state: ["Active"]
      - If user says "new user stories" → include workItemType: ["User Story"], state: ["New"]
      - If user says "ready tasks" → include workItemType: ["Task"], state: ["Ready"]
      - If user says "bugs" without ANY state qualifier → include workItemType: ["Bug"] ONLY (no state filter!)
      - ⚠️ "open bugs", "pending bugs", "outstanding bugs" → include workItemType: ["Bug"] ONLY + use analysis_criteria.filter_logic to exclude completed items. Do NOT add state filter!
      - ⚠️ If user says "items", "work items", "all items", or "compare sprints" (generic terms) → DO NOT include workItemType filter AT ALL!
      - ⚠️ If user says "resolved items", "active items", "new items" (state + generic term) → include ONLY state filter, NO workItemType!
      - If user doesn't mention state and no implicit state words (open/pending/outstanding) → DO NOT include state filter!
      - For "items with no assignee" → include unassigned: true
      - Example (WITH filters): {{"tool": "wit_get_work_items_for_iteration", "args": {{"project": "FracPro-OPS", "team": "XOPS 25", "iterationId": "@CurrentIteration", "workItemType": ["Bug"], "state": ["Active"]}}}}
      - Example (NO filters - all items): {{"tool": "wit_get_work_items_for_iteration", "args": {{"project": "FracPro-OPS", "team": "XOPS 25", "iterationId": "26.1"}}}}
      - Example (NO workItemType - comparing sprints): {{"tool": "wit_get_work_items_for_iteration", "args": {{"project": "FracPro-OPS", "team": "XOPS Bugs Enhancement", "iterationId": "@CurrentIteration"}}}} [NO workItemType, NO state unless user asked]
      - Example (state ONLY - resolved items): {{"tool": "wit_get_work_items_for_iteration", "args": {{"project": "FracPro-OPS", "team": "XOPS 25", "iterationId": "26.1", "state": ["Resolved"]}}}}

      
   D. Specific sprint number (e.g., "Sprint 26.1", "Sprint 25.22"):
      - ✅ Use the sprint number directly as iterationId - the system will resolve it to the actual GUID
      - Example for "bugs in Sprint 26.1": {{"tool": "wit_get_work_items_for_iteration", "args": {{"project": "FracPro-OPS", "team": "XOPS 25", "iterationId": "26.1", "workItemType": ["Bug"]}}}}

   E. STATE FILTER - DYNAMIC STATE RESOLUTION:
      ⚠️ CRITICAL: Check if user's state is in the VALID ADO STATES list first!
      - Step 1: Check if user's state (case-insensitive) is in VALID ADO STATES
      - Step 2a: If YES → Use the exact state from the list (e.g., "uat" → "UAT")
      - Step 2b: If NO → Map to closest synonym from the list (e.g., "done" → "Closed")
      - NEVER map one valid state to another (e.g., "UAT" → "QA" is FORBIDDEN)
      - Examples:
        - User says "UAT", "UAT" is in list → use state: ["UAT"]
        - User says "uat", "UAT" is in list → use state: ["UAT"] (case-insensitive match)
        - User says "done", "Closed" is in list → use state: ["Closed"] (synonym)
      - If user doesn't specify state, omit state parameter (system will infer from intent if needed)
      - States are validated against ADO dynamically - use the VALID ADO STATES list provided

   F. UNASSIGNED FILTER (CRITICAL):
      - For "items with no assignee", "unassigned bugs", "bugs without assignee":
      - ✅ Include unassigned: true in args
      - Example: {{"tool": "wit_get_work_items_for_iteration", "args": {{"iterationId": "@CurrentIteration", "workItemType": ["Bug"], "unassigned": true}}}}

6. USER-ASSIGNED WORK ITEM QUERIES (CRITICAL - COMMON MISTAKE):
   A. "work items assigned to [SPECIFIC PERSON NAME]", "tasks for [John Doe]":
      - NEVER use wit_my_work_items - that ONLY returns items for the AUTHENTICATED USER
      - ALWAYS use search_workitem with assignedTo filter
      - The assignedTo parameter accepts user display names or emails
      
   🚨 CRITICAL — assignedTo IDENTITY RULE:
      - Pass the EXACT name the user typed. Do NOT modify, expand, or guess.
      - ❌ NEVER fabricate email addresses (e.g., john.doe@walkingtree.tech) — you do NOT know real emails!
      - ❌ NEVER add a surname the user did NOT say (user says "Deepak" → use "Deepak", NOT "Deepak Yadav")
      - ❌ NEVER guess email domains (@walkingtree.tech, @intelloger.com, @fracpro.com, @stratagen.com)
      - ✅ User says "Deepak" → assignedTo: ["Deepak"]
      - ✅ User says "Deepak Yadav" → assignedTo: ["Deepak Yadav"]
      - ✅ User says "deepak.yadav@walkingtree.tech" → assignedTo: ["deepak.yadav@walkingtree.tech"]
      - ❌ WRONG: User says "Deepak" → assignedTo: ["deepak@walkingtree.tech"]  ← FABRICATED!
      - ❌ WRONG: User says "Deepak" → assignedTo: ["Deepak Yadav"]  ← SURNAME ADDED!
      - ❌ WRONG: User says "John" → assignedTo: ["john.doe@example.com"]  ← FABRICATED!
      - The downstream identity resolution system will handle name→email mapping.
      
   B. "my work items", "items assigned to me", "what's on my plate":
      - USE wit_my_work_items - this correctly returns current user's items
      
   C. Combined user + type query (e.g., "bugs assigned to John", "John's active bugs"):
      - Use search_workitem with BOTH assignedTo AND workItemType filters

7. MULTI-STEP WORK ITEM ANALYSIS QUERIES (e.g., "find reopened items and their reasons"):
   ⚠️ CRITICAL PATTERN TO AVOID - PLACEHOLDER TRAP:
   - DO NOT create plans like: Step 1: Find items → Step 2: Get details for <ITEM_ID_FROM_STEP_1>
   - The orchestrator CANNOT resolve <ITEM_ID_FROM_STEP_1> placeholders automatically
   
   ✅ CORRECT APPROACH:
   - Step 1: Search/fetch ALL items matching criteria (e.g., all reopened items)
   - Step 2: Use synthesis step with depends_on: ["step_1"] to analyze/explain results
   - Synthesis step description should clarify how results are combined

8. PIPELINE RUNS QUERIES (CRITICAL - MULTI-STEP REQUIRED):
   A. "show pipeline runs", "list pipeline runs for all pipelines":
      - ⚠️ pipelines_list_runs REQUIRES pipelineId - you cannot call it without knowing the pipeline ID!
      - ✅ CORRECT: First call pipelines_get_build_definitions to get pipeline list with IDs
      - ✅ Then synthesize - the orchestrator will iterate over results
      - Example multi-step plan:
        Step 1: {{"tool": "pipelines_get_build_definitions", "args": {{"project": "FracPro-OPS"}}}}
        Step 2: synthesize with depends_on: ["step_1"]
      - ❌ WRONG: Direct call to pipelines_list_runs without pipelineId
   
   B. "pipeline runs for a specific pipeline":
      - If pipeline ID/name is known, use pipelines_list_runs directly
      - Example: {{"tool": "pipelines_list_runs", "args": {{"project": "FracPro-OPS", "pipelineId": 123}}}}

9. WORK ITEM COMMENTS QUERIES (CRITICAL - MULTI-STEP REQUIRED):
   A. "show comments for my work items", "work item comments for items assigned to me":
      - ⚠️ wit_list_work_item_comments REQUIRES workItemId - cannot call without ID!
      - ✅ CORRECT multi-step approach:
        Step 1: Use wit_my_work_items to get user's work items with IDs
        Step 2: Synthesize to combine items with their comment counts
      - ❌ WRONG: Trying to use search_workitem (wrong tool for "my items")
      - ❌ WRONG: Direct call to wit_list_work_item_comments without workItemId
   
   B. "comments for specific work item #12345":
      - Single step: {{"tool": "wit_list_work_item_comments", "args": {{"project": "FracPro-OPS", "workItemId": 12345}}}}

10. TOOLS THAT DO NOT EXIST (CAPABILITY LIMITATIONS):
    A. list_area_paths: This tool does NOT exist in the MCP registry
       - For listing area paths, use search_workitem and extract unique area paths from results
       - Or indicate needs_clarification: true if area path listing is essential

11. TEST PLAN AND TEST SUITE QUERIES (CRITICAL - MULTI-STEP REQUIRED):
    A. "show test suites for all test plans", "list all test suites":
       - ⚠️ testplan_list_test_suites REQUIRES planId - you cannot call it without knowing the test plan ID!
       - ✅ CORRECT: First call testplan_list_test_plans to get test plan list with IDs
       - ✅ Then synthesize - the orchestrator will iterate over results
       - Example multi-step plan:
         Step 1: {{"tool": "testplan_list_test_plans", "args": {{"project": "FracPro-OPS"}}}}
         Step 2: synthesize with depends_on: ["step_1"]
       - ❌ WRONG: Direct call to testplan_list_test_suites without planId
    
    B. "test suites for a specific test plan":
       - If plan ID is known, use testplan_list_test_suites directly
       - Example: {{"tool": "testplan_list_test_suites", "args": {{"project": "FracPro-OPS", "planId": 123}}}}
    
    C. "show all test cases", "list test cases for all suites":
       - ⚠️ testplan_list_test_cases also REQUIRES suiteId (and planId)
       - Multi-step approach needed: plans → suites → cases

12. TEAM ITERATION QUERIES (CRITICAL - ALL vs SPECIFIC):
    A. "which teams have active iterations", "what teams have sprints", "all teams with iterations":
       - ⚠️ This is a MULTI-TEAM query - do NOT default to a single team!
       - ⚠️ work_list_team_iterations REQUIRES a specific team parameter
       - ✅ CORRECT: Use core_list_project_teams to get ALL teams first
       - ✅ Then synthesize - orchestrator will check iterations for each team
       - Example multi-step plan:
         Step 1: {{"tool": "core_list_project_teams", "args": {{"project": "FracPro-OPS"}}}}
         Step 2: synthesize with depends_on: ["step_1"] and description: "Check which teams have active iterations"
       - ❌ WRONG: Calling work_list_team_iterations with a single team like "XOPS 25"
    
    B. "show iterations for team XOPS 25", "sprints for FracPro Suite team":
       - ✅ CORRECT: Single-step with work_list_team_iterations
       - Example: {{"tool": "work_list_team_iterations", "args": {{"project": "FracPro-OPS", "team": "XOPS 25"}}}}

13. TEAM CAPACITY QUERIES (CRITICAL - ITERATION ID RESOLUTION):
    A. "capacity for team in current sprint", "show capacity for XOPS 25":
       - ⚠️ work_get_team_capacity REQUIRES actual iteration GUID, NOT "@CurrentIteration" macro
       - ✅ CORRECT: First get iterations, then get capacity for current iteration
       - Example multi-step plan:
         Step 1: {{"tool": "work_list_team_iterations", "args": {{"project": "FracPro-OPS", "team": "XOPS 25"}}}}
         Step 2: {{"tool": "work_get_team_capacity", "args": {{"project": "FracPro-OPS", "team": "XOPS 25", "iterationId": "<CURRENT_ITERATION_GUID_FROM_STEP_1>"}}}}
         Step 3: synthesize with depends_on: ["step_1", "step_2"]
       - ❌ WRONG: Direct call with iterationId: "@CurrentIteration" - this macro doesn't work for capacity API!

CRITICAL: Only use tools that are listed in AVAILABLE TOOLS above. Do NOT use tools that are not in the list. If a specific tool is not available, choose the closest available alternative or indicate needs_clarification."""
    
    # ══════════════════════════════════════════════════════════════════════════
    # Inject dynamic state categories from centralized config
    # ══════════════════════════════════════════════════════════════════════════
    from config import config
    cats = config.get_state_categories()
    state_cats_text = "\n\n═══ WORK ITEM STATE CATEGORIES (from config — use these EXACTLY) ═══\n"
    state_cats_text += "When referencing completed, in-progress, not-started, or blocked states in synthesis descriptions, use ONLY these mappings:\n"
    for label, states in cats.items():
        state_cats_text += f"  - {label}: {', '.join(states)}\n"
    state_cats_text += "DO NOT invent categories or guess which states belong where.\n"
    system_prompt += state_cats_text

    # Inject PM Knowledge Base so the light planner uses consistent PM vocabulary and thresholds
    try:
        from .instructions import load_kb_instructions
        _kb = load_kb_instructions()
        if _kb:
            system_prompt += f"\n\n═══ PM KNOWLEDGE BASE (apply these PM conventions, thresholds, and terminology) ═══\n{_kb}\n"
    except Exception:
        pass  # KB is optional — absence degrades gracefully

    return system_prompt


# ══════════════════════════════════════════════════════════════════════════════
# HEURISTIC WIQL PLAN BUILDER (for fallback when LLM times out)
# ══════════════════════════════════════════════════════════════════════════════

def _build_heuristic_wiql_plan(query: str, context: Dict[str, Any]) -> Dict[str, Any]:
    """
    Build an executable WIQL plan from heuristic pattern matching.
    Called when LLM times out but the query clearly needs execute_wiql.
    Extracts work item type, state, date constraints from the query text.
    """
    import re as _re_local
    q = query.lower()
    project = context.get("project", "FracPro-OPS")
    
    # Extract work item type
    wit_map = {
        "bug": "Bug", "bugs": "Bug",
        "task": "Task", "tasks": "Task",
        "user story": "User Story", "user stories": "User Story", "story": "User Story", "stories": "User Story",
        "feature": "Feature", "features": "Feature",
        "epic": "Epic", "epics": "Epic",
        "work item": None, "work items": None, "item": None, "items": None,
    }
    work_item_type = None
    for pattern, wit in wit_map.items():
        if pattern in q:
            work_item_type = wit
            break
    
    # Extract state
    state_map = {
        "active": "Active", "new": "New", "closed": "Closed",
        "resolved": "Resolved", "qa": "QA", "design": "Design",
        "ready": "Ready", "in progress": "Active",
    }
    state = None
    for pattern, st in state_map.items():
        if pattern in q:
            state = st
            break
    
    # Extract date/duration constraints
    duration_match = _re_local.search(r'(?:more\s+than|longer\s+than|over|exceeding|last|past|for)\s+(\d+)\s+(days?|weeks?|months?)', q)
    days_ago = None
    if duration_match:
        num = int(duration_match.group(1))
        unit = duration_match.group(2).rstrip('s')
        if unit == "week":
            days_ago = num * 7
        elif unit == "month":
            days_ago = num * 30
        else:
            days_ago = num
    
    # Detect "stuck in state" pattern → use ChangedDate
    is_stuck_query = bool(_re_local.search(r'\b(stuck|stale|sitting|waiting|been|not\s+updated|idle|dormant)\b', q))
    
    # Detect "closed/created in last X days" pattern
    is_closed_query = "closed" in q
    is_created_query = "created" in q
    
    # Detect unassigned
    is_unassigned = bool(_re_local.search(r'\b(unassigned|no\s+assignee)\b', q))
    
    # Detect priority
    priority_match = _re_local.search(r'\b(?:high\s*priority|p[12]|priority\s*[12]|critical)\b', q)
    is_high_priority = bool(priority_match)
    
    # Build WHERE clauses
    where_clauses = [f"[System.TeamProject] = '{project}'"]
    
    if work_item_type:
        where_clauses.append(f"[System.WorkItemType] = '{work_item_type}'")
    
    if state:
        where_clauses.append(f"[System.State] = '{state}'")
    
    if is_unassigned:
        where_clauses.append("[System.AssignedTo] = ''")
    
    if is_high_priority:
        where_clauses.append("[Microsoft.VSTS.Common.Priority] <= 2")
    
    # Area path from context (resolved by _extract_entities_from_query)
    _resolved_ap = context.get('resolved_area_path')
    if _resolved_ap and not context.get('_team_defaulted') and not context.get('_multi_team_query'):
        where_clauses.append(f"[System.AreaPath] UNDER '{_resolved_ap}'")
        logger.info(f"[LIGHT_PLANNER] Heuristic WIQL: Injected area path: {_resolved_ap}")
    
    # Date clause
    if days_ago:
        if is_stuck_query or (state and not is_closed_query and not is_created_query):
            # "stuck in Active for X days" → items changed more than X days ago
            where_clauses.append(f"[System.ChangedDate] < @Today - {days_ago}")
        elif is_closed_query:
            where_clauses.append(f"[Microsoft.VSTS.Common.ClosedDate] >= @Today - {days_ago}")
        elif is_created_query:
            where_clauses.append(f"[System.CreatedDate] >= @Today - {days_ago}")
        else:
            # Default: changed date
            where_clauses.append(f"[System.ChangedDate] < @Today - {days_ago}")
    
    # Build SELECT fields
    select_fields = [
        "[System.Id]", "[System.Title]", "[System.State]",
        "[System.WorkItemType]", "[System.AssignedTo]"
    ]
    if days_ago or is_stuck_query:
        select_fields.append("[System.ChangedDate]")
    if is_closed_query:
        select_fields.append("[Microsoft.VSTS.Common.ClosedDate]")
    if is_created_query:
        select_fields.append("[System.CreatedDate]")
    
    # Build ORDER BY
    if is_stuck_query or (days_ago and not is_closed_query):
        order_by = " ORDER BY [System.ChangedDate] ASC"
    elif is_closed_query:
        order_by = " ORDER BY [Microsoft.VSTS.Common.ClosedDate] DESC"
    else:
        order_by = " ORDER BY [System.ChangedDate] DESC"
    
    wiql = f"SELECT {', '.join(select_fields)} FROM WorkItems WHERE {' AND '.join(where_clauses)}{order_by}"
    
    logger.info(f"[LIGHT_PLANNER] Heuristic WIQL plan built: {wiql[:200]}")
    
    return {
        "plan": {
            "type": "single",
            "steps": [
                {
                    "step_id": "step_1",
                    "action": "call_tool",
                    "tool": "execute_wiql",
                    "args": {
                        "project": project,
                        "wiql": wiql,
                        "top": 1000
                    },
                    "description": f"Execute WIQL query (heuristic fallback): {query}",
                    "depends_on": []
                }
            ]
        },
        "confidence": 0.80,
        "has_full_plan": True,
        "needs_clarification": False,
        "source": "fallback_heuristic_wiql",
        "analysis_summary": f"Heuristic WIQL plan for: {query}"
    }


# ══════════════════════════════════════════════════════════════════════════════
# FALLBACK HEURISTIC (No LLM, pattern-based)
# ══════════════════════════════════════════════════════════════════════════════

def _fallback_heuristic_routing(query: str, context: Dict[str, Any]) -> Dict[str, Any]:
    """
    Fallback routing when LLM is unavailable or disabled.
    
    Uses simple keyword/pattern matching to produce a routing hint.
    Returns low confidence to encourage deep planner escalation.
    """
    q = query.lower()
    
    # ══════════════════════════════════════════════════════════════════════════
    # WIQL HEURISTIC ROUTING — Fallback for date/priority/field-specific queries
    # When LLM is unavailable, use pattern matching to route to execute_wiql
    # for queries that CANNOT be handled by search_workitem.
    # ══════════════════════════════════════════════════════════════════════════
    import re as _re
    
    wiql_date_patterns = [
        r'\b(closed|resolved|created|modified|changed|updated)\s+(in|during|within|last|past|since|on|before|after)\b',
        r'\b(last|past)\s+\d+\s+(days?|weeks?|months?)\b',
        r'\b(more\s+than|longer\s+than|over|exceeding)\s+\d+\s+(days?|weeks?|months?)\b',
        r'\b(stuck|stale|sitting|waiting|been)\s+(in|for|at)\b.*\b(days?|weeks?|months?)\b',
        r'\b(stuck|stale|sitting|waiting|idle|dormant)\s+(in|at|for)\s+\w+\s+(state|status)\b',
        r'\b(today|yesterday|this\s+week|this\s+month|last\s+week|last\s+month)\b',
        r'\b(before|after|between|since)\s+\d{4}',  # date references with year
        r'\b(before|after|between|since|on)\s+\d{1,2}\s+\w+',  # "on 4 feb", "after 15 january"
        r'\b(before|after|between|since|on)\s+\w+\s+\d{1,2}',  # "on feb 4", "after january 15"
        r'\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|january|february|march|april|june|july|august|september|october|november|december)\s+\d{1,2}',  # "feb 4", "february 4"
        r'\b\d{1,2}\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|january|february|march|april|june|july|august|september|october|november|december)',  # "4 feb", "4 february"
        r'\b(in|during)\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|january|february|march|april|may|june|july|august|september|october|november|december)',  # "in february", "during march"
        r'\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b',  # date formats like 04/02/2026, 4-2-2026
    ]
    wiql_field_patterns = [
        r'\b(priority|p[1-4]|high\s*priority|critical\s*priority)\b',
        r'\b(title|description)\s*(contains?|containing|with|like|matching)\b',
        r'\b(unassigned|no\s*assignee)\b',
        r'\b(area\s*path|iteration\s*path)\s*(under|=|contains)\b',
        r'\b(all|list|show|get)\s+(active|new|closed|resolved|qa|design|ready)\s+(bugs?|tasks?|user\s*stor(y|ies)|items?|features?|work\s*items?)\b',
        r'\b(bugs?|tasks?|user\s*stor(y|ies)|items?|features?)\s+in\s+(active|new|closed|resolved|qa|design|ready)\s+(state|status)\b',
        r'\b(active|new|closed|resolved|qa|design|ready)\s+(bugs?|tasks?|user\s*stor(y|ies)|items?|features?)\b',
    ]
    
    is_wiql_query = (
        any(_re.search(p, q) for p in wiql_date_patterns) or
        any(_re.search(p, q) for p in wiql_field_patterns)
    )
    
    if is_wiql_query:
        logger.info(f"[LIGHT_PLANNER] WIQL heuristic: date/priority/field query detected, building execute_wiql plan")
        wiql_plan = _build_heuristic_wiql_plan(query, context)
        return wiql_plan
    
    # ══════════════════════════════════════════════════════════════════════════
    # AREA PATH INTENT DETECTION (Enterprise Contract)
    # Area path queries need special handling - route to execute_wiql or search_workitem
    # ══════════════════════════════════════════════════════════════════════════
    area_path_keywords = [
        "area path", "areapath", "area-path",
        "under area", "in area",
        "team area", "area for team",
        "area filter", "filter by area"
    ]
    area_path_context_hints = [
        "nested under", "hierarchy", "child areas",
        "parent area", "sub-area", "subarea"
    ]
    
    # Check for area path specific queries
    if any(kw in q for kw in area_path_keywords) or any(kw in q for kw in area_path_context_hints):
        return {
            "route": "pm_agent",
            "skill": None,
            "action_hint": "Area path query detected - use search_workitem with areaPath filter",
            "tool_hint": "search_workitem",
            "area_path_query": True,
            "confidence": 0.65,
            "type": "routing_hint",
            "source": "fallback_heuristic",
            "has_full_plan": False,
            "areapath_hint": "Use areaPath parameter in search_workitem"
        }
    
    # Check for team-scoped work item queries (may need area path resolution)
    team_scoped_patterns = [
        ("for team", "in team", "team's"),
        ("bugs", "tasks", "stories", "items", "work items")
    ]
    if any(p in q for p in team_scoped_patterns[0]) and any(p in q for p in team_scoped_patterns[1]):
        return {
            "route": "pm_agent",
            "skill": None,
            "action_hint": "Team-scoped work item query - may need area path resolution",
            "tool_hint": "search_workitem",
            "team_scoped_query": True,
            "area_path_resolution_needed": True,
            "confidence": 0.60,
            "type": "routing_hint",
            "source": "fallback_heuristic",
            "has_full_plan": False
        }
    
    # ══════════════════════════════════════════════════════════════════════════
    # FIX #9: PRIORITIZE MCP TOOLS OVER UI-FORM SKILLS
    # When query clearly needs data (list, show, get, search), route to pm_agent
    # to trigger MCP tool calls. Skills should be for analysis/reports only.
    # ══════════════════════════════════════════════════════════════════════════
    data_fetch_keywords = ["list", "show", "get", "fetch", "retrieve", "search", "find", "what are", "how many"]
    entity_keywords = ["projects", "teams", "repos", "repositories", "builds", "pipelines", 
                       "wikis", "pages", "iterations", "sprints", "work items", "bugs", "tasks",
                       "stories", "pull requests", "prs", "commits", "branches", "alerts",
                       "test plans", "test cases", "capacity", "capacities"]
    
    is_data_fetch_query = any(k in q for k in data_fetch_keywords) and any(k in q for k in entity_keywords)
    
    if is_data_fetch_query:
        # Determine the best tool hint based on entity
        tool_hint = None
        if any(k in q for k in ["capacity", "capacities"]):
            tool_hint = "work_get_team_capacity"
        elif any(k in q for k in ["iteration", "sprint"]):
            # FIX #30: If query asks about WORK ITEMS in a sprint, use
            # wit_get_work_items_for_iteration, not work_list_team_iterations.
            # Only use work_list_team_iterations when asking about iterations themselves.
            work_item_indicators = ["work item", "items", "bugs", "tasks", "stories",
                                    "assigned", "completed", "blocked", "open", "overdue",
                                    "burndown", "story points", "count"]
            if any(wi in q for wi in work_item_indicators):
                tool_hint = "wit_get_work_items_for_iteration"
                logger.info(f"[LIGHT_PLANNER] FIX #30: Sprint query with work-item context → wit_get_work_items_for_iteration")
            else:
                tool_hint = "work_list_team_iterations"
        elif any(k in q for k in ["project"]):
            tool_hint = "core_list_projects"
        elif any(k in q for k in ["team"]):
            tool_hint = "core_list_project_teams"
        elif any(k in q for k in ["wiki"]):
            tool_hint = "wiki_list_wikis"
        elif any(k in q for k in ["build", "pipeline"]):
            tool_hint = "pipelines_get_builds"
        elif any(k in q for k in ["repo", "repositor"]):
            tool_hint = "repo_list_repos_by_project"
        
        logger.info(f"[LIGHT_PLANNER] Data fetch query detected, prioritizing MCP tools over skills (tool_hint: {tool_hint})")
        return {
            "route": "pm_agent",
            "skill": None,
            "action_hint": "Data fetch query - use MCP tools for actual data",
            "tool_hint": tool_hint,
            "confidence": 0.65,
            "type": "routing_hint",
            "source": "fallback_heuristic_data_priority",
            "has_full_plan": False
        }
    
    # PM Skill Agent patterns - DYNAMICALLY loaded from SKILL_DEFINITIONS
    # This ensures any new skills are automatically included in fallback routing
    skill_definitions = _get_skill_definitions()
    
    for skill_name, skill_def in skill_definitions.items():
        # Get use cases from skill definition
        use_cases = skill_def.use_cases if hasattr(skill_def, 'use_cases') else skill_def.get('use_cases', [])
        
        # Check if query matches any use case
        for use_case in use_cases:
            if use_case.lower() in q:
                return {
                    "route": "pm_skill_agent",
                    "skill": skill_name,
                    "action_hint": f"Matched skill use case: {use_case}",
                    "confidence": 0.70,
                    "type": "routing_hint",
                    "source": "fallback_heuristic_dynamic",
                    "has_full_plan": False
                }
    
    # Additional keyword patterns for common skills (backup matching)
    skill_keyword_patterns = [
        (["recurring", "repeated", "similar", "pattern"], ["bug"], "bug_areas_highlight", 0.65),
        (["overlooked", "forgotten", "stale", "neglected"], ["stor", "item"], "overlooked_stories", 0.65),
        (["iteration", "sprint"], ["report", "summary"], "iteration_report", 0.70),
        (["feedback"], ["dev", "developer"], "feedback_to_dev", 0.65),
        (["billing"], ["deviation", "report", "hours"], "billing_deviation", 0.70),
        (["backlog"], ["health", "triag", "analysis"], "backlog_triaging", 0.65),
        (["capacity"], ["check", "forecast", "utilization"], "get_capacity_forecast", 0.65),
        (["developer"], ["skill", "knowledge", "expertise"], "developer_skills", 0.65),
    ]
    
    for keywords1, keywords2, skill, conf in skill_keyword_patterns:
        if any(k in q for k in keywords1) and any(k in q for k in keywords2):
            return {
                "route": "pm_skill_agent",
                "skill": skill,
                "action_hint": f"Detected skill keyword pattern: {skill}",
                "confidence": conf,
                "type": "routing_hint",
                "source": "fallback_heuristic",
                "has_full_plan": False
            }
    
    # PM Agent patterns
    agent_keywords = ["bug", "task", "story", "work item", "iteration", "sprint", "assigned", "blocked"]
    if any(k in q for k in agent_keywords):
        return {
            "route": "pm_agent",
            "skill": None,
            "action_hint": "Detected ADO/work item query",
            "confidence": 0.50,
            "type": "routing_hint",
            "source": "fallback_heuristic",
            "has_full_plan": False
        }
    
    # Default: PM Agent with low confidence
    return {
        "route": "pm_agent",
        "skill": None,
        "action_hint": "No clear pattern, fallback to PM Agent",
        "confidence": 0.30,
        "type": "routing_hint",
        "source": "fallback_heuristic",
        "has_full_plan": False
    }


# ══════════════════════════════════════════════════════════════════════════════
# MAIN LIGHT PLANNER FUNCTION
# ══════════════════════════════════════════════════════════════════════════════

def _resolve_area_path_from_partial(partial_path: str, available_area_paths: list) -> Optional[str]:
    """
    Resolve a partial area path mention (like "XOPS 25") to the full area path.
    
    Args:
        partial_path: Partial area path mention from query or LLM plan
        available_area_paths: List of all available full area paths from context
    
    Returns:
        Full area path if matched, None otherwise
    
    Examples:
        "XOPS 25" → "Global Management\\WTT Development\\XOPS 25"
        "WTT Development" → "Global Management\\WTT Development"
        "FracPro Suite" → "Global FP\\FracPro Suite"
    """
    if not partial_path or not available_area_paths:
        return None
    
    partial_lower = partial_path.lower().strip()
    
    # 1. Exact match (case-insensitive)
    for full_path in available_area_paths:
        if full_path.lower() == partial_lower:
            logger.debug(f"[AREA_PATH_RESOLUTION] Exact match: '{partial_path}' → '{full_path}'")
            return full_path
    
    # 2. Ends with partial (most common case: "XOPS 25" matches path ending with "XOPS 25")
    for full_path in available_area_paths:
        # Get last segment of full path
        last_segment = full_path.split('\\')[-1].lower()
        if last_segment == partial_lower:
            logger.info(f"[AREA_PATH_RESOLUTION] Matched partial '{partial_path}' → full path '{full_path}'")
            return full_path
    
    # 3. Contains partial in any segment
    for full_path in available_area_paths:
        segments = [seg.lower() for seg in full_path.split('\\')]
        if partial_lower in segments:
            logger.info(f"[AREA_PATH_RESOLUTION] Segment match: '{partial_path}' → '{full_path}'")
            return full_path
    
    # 4. Fuzzy match on last segment (>0.85 similarity)
    best_match = None
    best_ratio = 0.0
    for full_path in available_area_paths:
        last_segment = full_path.split('\\')[-1].lower()
        ratio = difflib.SequenceMatcher(None, partial_lower, last_segment).ratio()
        if ratio > best_ratio and ratio >= 0.85:
            best_ratio = ratio
            best_match = full_path
    
    if best_match:
        logger.info(f"[AREA_PATH_RESOLUTION] Fuzzy match: '{partial_path}' → '{best_match}' (similarity: {best_ratio:.2%})")
        return best_match
    
    logger.warning(f"[AREA_PATH_RESOLUTION] Could not resolve partial path '{partial_path}' to any full path")
    return None


def _extract_entities_from_query(query: str, context: Dict[str, Any]) -> Dict[str, Any]:
    """
    Extract team/project/area path/sprint names mentioned in the query text.
    Also injects defaults from context_resolver for missing values.
    This helps resolve context before calling the LLM.
    
    Args:
        query: User query text
        context: Existing context dict
        
    Returns:
        Enhanced context with extracted entities and defaults
        
    Area Path Resolution (Enterprise Contract):
        - Area paths are NEVER guessed or hardcoded
        - If team is mentioned, it will be resolved to area path dynamically
        - Extracted values are marked for dynamic resolution
    """
    # Import context_resolver defaults and mapping
    try:
        from orchestrator.context_resolver import (
            DEFAULT_TEAM, DEFAULT_SPRINT, DEFAULT_PROJECT, 
            TEAM_ALIASES, VALID_TEAMS, TEAM_TO_AREA_PATH, get_area_path_for_team
        )
    except ImportError:
        # Fallback defaults if import fails
        DEFAULT_TEAM = "XOPS 25"
        DEFAULT_SPRINT = "current"
        DEFAULT_PROJECT = "FracPro-OPS"
        TEAM_ALIASES = {}
        VALID_TEAMS = []
        TEAM_TO_AREA_PATH = {}
        get_area_path_for_team = lambda t: None
    
    enhanced = context.copy()
    query_lower = query.lower()
    
    # ═══════════════════════════════════════════════════════════════════════════
    # EXTRACT SPRINT/ITERATION REFERENCE
    # ═══════════════════════════════════════════════════════════════════════════
    if not enhanced.get("sprint"):
        import re
        
        # Check for @CurrentIteration patterns
        if any(kw in query_lower for kw in ["current sprint", "current iteration", "@currentiteration", "this sprint"]):
            enhanced["sprint"] = "@CurrentIteration"
            logger.info(f"[LIGHT_PLANNER] Extracted sprint reference: @CurrentIteration")
        # Check for previous sprint patterns
        elif any(kw in query_lower for kw in ["previous sprint", "last sprint", "prior sprint"]):
            enhanced["sprint"] = "@PreviousIteration"
            logger.info(f"[LIGHT_PLANNER] Extracted sprint reference: @PreviousIteration (requires multi-step plan)")
        else:
            # Check for numeric sprint patterns
            sprint_patterns = [
                r'sprint\s*(\d+\.?\d*)',  # Sprint 26.01, Sprint 26
                r'iteration\s*(\d+\.?\d*)',  # Iteration 26.01
                r'\b(\d{2}\.\d{1,2})\b',  # 26.01, 25.19 (standalone)
            ]
            for pattern in sprint_patterns:
                match = re.search(pattern, query_lower, re.IGNORECASE)
                if match:
                    enhanced["sprint"] = match.group(1)
                    logger.info(f"[LIGHT_PLANNER] Extracted sprint reference: {enhanced['sprint']}")
                    break
    
    # ═══════════════════════════════════════════════════════════════════════════
    # EXTRACT TEAM REFERENCE WITH VALIDATION
    # ═══════════════════════════════════════════════════════════════════════════
    if not enhanced.get("team"):
        import re
        
        logger.debug(f"[LIGHT_PLANNER] Team extraction: query_lower='{query_lower}', VALID_TEAMS={VALID_TEAMS[:5]}...")
        
        # First check for exact team names from VALID_TEAMS
        for team in VALID_TEAMS:
            team_lower = team.lower()
            if team_lower in query_lower:
                enhanced["team"] = team
                enhanced["_area_path_resolution_needed"] = True
                logger.info(f"[LIGHT_PLANNER] Extracted exact team '{enhanced['team']}' from query (matched '{team_lower}')")
                break
            else:
                logger.debug(f"[LIGHT_PLANNER] Team '{team_lower}' not in query")
        
        # Check aliases if no exact match
        if not enhanced.get("team"):
            for alias, team in TEAM_ALIASES.items():
                if alias.lower() in query_lower:
                    enhanced["team"] = team
                    enhanced["_area_path_resolution_needed"] = True
                    logger.info(f"[LIGHT_PLANNER] Extracted team via alias '{alias}' → '{team}'")
                    break
        
        # Pattern-based extraction as fallback
        if not enhanced.get("team"):
            team_patterns = [
                r'\bfor\s+team\s+([a-zA-Z0-9\s\-]+?)(?:\s+(?:in|for|from|with|sprint|iteration|during)|$)',
                r'\bin\s+team\s+([a-zA-Z0-9\s\-]+?)(?:\s+(?:in|for|from|with|sprint|iteration|during)|$)',
                r'\bteam\s+([a-zA-Z0-9\s\-]+?)(?:\'s|\s+(?:in|for|from|with|sprint|iteration|during)|$)',
            ]
            # FIX #29: Stop-words that look like team names but aren't
            _TEAM_STOP_WORDS = {
                'member', 'members', 'lead', 'leads', 'leader', 'leaders',
                'engineer', 'engineers', 'manager', 'managers',
                'developer', 'developers', 'owner', 'owners',
                'capacity', 'velocity', 'performance', 'progress',
                'workload', 'assignment', 'assignments', 'status',
            }
            for pattern in team_patterns:
                match = re.search(pattern, query, re.IGNORECASE)
                if match:
                    extracted_team = match.group(1).strip()
                    # FIX #29: Skip common words that aren't team names
                    if extracted_team.lower() in _TEAM_STOP_WORDS:
                        logger.info(f"[LIGHT_PLANNER] FIX #29: Skipped stop-word '{extracted_team}' in team extraction")
                        continue
                    # Validate against known teams
                    if extracted_team.lower() in [t.lower() for t in VALID_TEAMS]:
                        enhanced["team"] = next(t for t in VALID_TEAMS if t.lower() == extracted_team.lower())
                    elif extracted_team.lower() in TEAM_ALIASES:
                        enhanced["team"] = TEAM_ALIASES[extracted_team.lower()]
                    else:
                        enhanced["team"] = extracted_team
                    enhanced["_area_path_resolution_needed"] = True
                    logger.info(f"[LIGHT_PLANNER] Extracted team '{enhanced['team']}' from query pattern")
                    break
    
    # ═══════════════════════════════════════════════════════════════════════════
    # EXTRACT STATE REFERENCE FROM QUERY (CRITICAL - USE USER'S EXACT STATE)
    # ═══════════════════════════════════════════════════════════════════════════
    if not enhanced.get("extracted_state"):
        import re
        # Known ADO states - match user's exact state term from the query
        # Order matters: more specific patterns first, then broader patterns
        state_patterns = [
            # UAT variants (most specific first)
            (r'\bUAT\s+complete\b', 'UAT Complete'),
            (r'\bin\s+UAT\s+state\b', 'UAT'),
            (r'\bUAT\s+state\b', 'UAT'),
            (r'\bin\s+UAT\b', 'UAT'),
            # QA variants
            (r'\bin\s+QA\s+state\b', 'QA'),
            (r'\bQA\s+state\b', 'QA'),
            (r'\bin\s+QA\b', 'QA'),
            # PRE-PROD variants
            (r'\bin\s+PRE-PROD\s+state\b', 'PRE-PROD'),
            (r'\bPRE-PROD\s+state\b', 'PRE-PROD'),
            (r'\bpre-?prod\b', 'PRE-PROD'),
            # Active variants
            (r'\bin\s+active\s+state\b', 'Active'),
            (r'\bactive\s+state\b', 'Active'),
            (r'\bactive\s+(?:bugs?|tasks?|items?|stories|work\s*items?)\b', 'Active'),
            # Resolved variants
            (r'\bin\s+resolved\s+state\b', 'Resolved'),
            (r'\bresolved\s+state\b', 'Resolved'),
            (r'\bresolved\s+(?:bugs?|tasks?|items?|stories|work\s*items?)\b', 'Resolved'),
            (r'\b(?:bugs?|tasks?|items?|stories|work\s*items?)\s+(?:in|with)\s+resolved\b', 'Resolved'),
            # Closed variants
            (r'\bin\s+closed\s+state\b', 'Closed'),
            (r'\bclosed\s+state\b', 'Closed'),
            (r'\bclosed\s+(?:bugs?|tasks?|items?|stories|work\s*items?)\b', 'Closed'),
            # New variants
            (r'\bin\s+new\s+state\b', 'New'),
            (r'\bnew\s+state\b', 'New'),
            # Design variants
            (r'\bin\s+design\s+state\b', 'Design'),
            (r'\bdesign\s+state\b', 'Design'),
            # Ready variants
            (r'\bin\s+ready\s+state\b', 'Ready'),
            (r'\bread[y]\s+state\b', 'Ready'),
            # Other states
            (r'\bon\s+hold\b', 'On Hold'),
            (r'\bin\s+code\s+review\b', 'Code Review'),
            (r'\breopened\b', 'Reopened'),
            (r'\bissues\s+found\b', 'Issues Found'),
        ]
        for pattern, state_value in state_patterns:
            if re.search(pattern, query, re.IGNORECASE):
                enhanced["extracted_state"] = state_value
                logger.info(f"[LIGHT_PLANNER] Extracted state from query: '{state_value}'")
                break

    # ═══════════════════════════════════════════════════════════════════════════
    # INJECT DEFAULTS FOR MISSING CONTEXT (CRITICAL FOR SPRINT QUERIES)
    # ═══════════════════════════════════════════════════════════════════════════
    
    # Inject default project if missing
    if not enhanced.get("project"):
        enhanced["project"] = DEFAULT_PROJECT
        enhanced["_project_defaulted"] = True
        logger.debug(f"[LIGHT_PLANNER] Injected default project: {DEFAULT_PROJECT}")
    
    # ⚠️ CRITICAL FIX: Check for "all teams", "which teams", "what teams" patterns
    # These queries should NOT have a default team injected
    multi_team_patterns = [
        r'\ball\s+teams?\b',
        r'\bwhich\s+teams?\b', 
        r'\bwhat\s+teams?\b',
        r'\bevery\s+team\b',
        r'\beach\s+team\b(?!\s+member)',  # FIX #31: "each team" but NOT "each team member"
        r'\blist\s+(of\s+)?teams?\b',
        r'\bshow\s+(all\s+)?teams?\b',
    ]
    is_multi_team_query = any(re.search(pattern, query_lower, re.IGNORECASE) for pattern in multi_team_patterns)
    
    # Inject default team if missing AND query seems team-scoped or sprint-related
    # BUT ONLY if NOT a multi-team query
    sprint_keywords = ["sprint", "iteration", "current sprint", "this sprint", "previous sprint", "last sprint"]
    if not enhanced.get("team") and not is_multi_team_query and any(kw in query_lower for kw in sprint_keywords):
        enhanced["team"] = DEFAULT_TEAM
        enhanced["_team_defaulted"] = True
        logger.info(f"[LIGHT_PLANNER] Injected default team: {DEFAULT_TEAM}")
    elif is_multi_team_query:
        enhanced["_multi_team_query"] = True
        enhanced["_no_team_filter"] = True
        logger.info(f"[LIGHT_PLANNER] Multi-team query detected, NOT injecting default team")
    
    # ⚠️ CRITICAL FIX: DO NOT auto-inject default sprint unless user mentions sprint/iteration
    # OLD BEHAVIOR: Auto-injected "current" sprint for ANY work item query
    # NEW BEHAVIOR: Only inject sprint if user EXPLICITLY mentions sprint/iteration keywords
    explicit_sprint_keywords = ["sprint", "iteration", "current sprint", "this sprint", "previous sprint", "last sprint"]
    has_sprint_mention = any(kw in query_lower for kw in explicit_sprint_keywords)
    
    if not enhanced.get("sprint") and has_sprint_mention:
        # User mentioned sprint but didn't specify which one - use current as reasonable default
        enhanced["sprint"] = DEFAULT_SPRINT  # "current"
        enhanced["_sprint_defaulted"] = True  
        logger.info(f"[LIGHT_PLANNER] User mentioned sprint context, injected default sprint: {DEFAULT_SPRINT}")
    elif not has_sprint_mention:
        logger.debug(f"[LIGHT_PLANNER] No sprint keywords detected, NOT injecting default sprint")
        # Explicitly mark that we should NOT filter by iteration
        enhanced["_no_iteration_filter"] = True
    
    # ═══════════════════════════════════════════════════════════════════════════
    # AUTO-RESOLVE AREA PATH FROM TEAM
    # ═══════════════════════════════════════════════════════════════════════════
    if enhanced.get("team") and not enhanced.get("resolved_area_path"):
        area_path = get_area_path_for_team(enhanced["team"])
        if area_path:
            enhanced["resolved_area_path"] = area_path
            logger.info(f"[LIGHT_PLANNER] Auto-resolved area path: {area_path}")
    
    # Extract explicit area path if mentioned
    # Pattern: "area path X\\Y\\Z", "under area X", "in area X\\Y"
    if not enhanced.get("area_path"):
        import re
        area_patterns = [
            r'area\s+path\s+["\']?([^"\']+)["\']?',
            r'under\s+area\s+["\']?([^"\']+)["\']?',
            r'in\s+area\s+["\']?([\\\/\w\s-]+)["\']?',
        ]
        for pattern in area_patterns:
            match = re.search(pattern, query, re.IGNORECASE)
            if match:
                raw_area = match.group(1).strip()
                # Normalize path separators
                enhanced["area_path"] = raw_area.replace("/", "\\")
                enhanced["_area_path_explicit"] = True  # Mark as explicitly provided
                logger.info(f"[LIGHT_PLANNER] Extracted explicit area path '{enhanced['area_path']}' from query")
                break
    
    # ═══════════════════════════════════════════════════════════════════════════
    # HELPER: Validate extracted project against known valid projects
    # ═══════════════════════════════════════════════════════════════════════════
    def _validate_project_candidate(candidate: str, valid_projects: list) -> Optional[str]:
        """
        Validate that extracted project actually exists.
        Uses data-driven validation against real ADO projects.
        
        Returns validated project name, or None if invalid/not found.
        """
        if not candidate or not valid_projects:
            return None
        
        candidate_lower = candidate.lower().strip()
        
        # 1. Check for exact match
        for proj in valid_projects:
            if proj.lower() == candidate_lower:
                return proj
        
        # 2. Check for case-insensitive match
        for proj in valid_projects:
            if proj.lower().startswith(candidate_lower):
                return proj
        
        # 3. Fuzzy match: If candidate is close to a real project (>80% similar)
        from difflib import SequenceMatcher
        best_match = None
        best_ratio = 0.0
        
        for proj in valid_projects:
            ratio = SequenceMatcher(None, candidate_lower, proj.lower()).ratio()
            if ratio > best_ratio and ratio >= 0.80:  # 80% similarity threshold
                best_ratio = ratio
                best_match = proj
        
        if best_match:
            logger.info(f"[LIGHT_PLANNER] Fuzzy matched project '{candidate}' → '{best_match}' (similarity: {best_ratio:.2%})")
            return best_match
        
        return None  # Not a valid project
    
    # ═══════════════════════════════════════════════════════════════════════════
    # EXTRACT PROJECT IF MENTIONED (with data-driven validation)
    # ═══════════════════════════════════════════════════════════════════════════
    import re
    project_patterns = [
        r'\bproject\s+(\w+)',
        r'\bin\s+(\w+)\s+project'
    ]
    for pattern in project_patterns:
        match = re.search(pattern, query, re.IGNORECASE)
        if match:
            candidate = match.group(1)
            
            # Get list of valid projects from context or defaults
            try:
                from orchestrator.context_resolver import VALID_PROJECTS
                valid_projects = VALID_PROJECTS
            except (ImportError, AttributeError):
                valid_projects = [DEFAULT_PROJECT]  # Fallback to default only
            
            # Validate the extracted candidate
            validated_project = _validate_project_candidate(candidate, valid_projects)
            
            if validated_project:
                # It's a real project!
                enhanced["project"] = validated_project
                enhanced["_project_defaulted"] = False
                enhanced["_project_source"] = "extracted_and_validated"
                logger.info(f"[LIGHT_PLANNER] Extracted & validated project '{validated_project}' from query")
                break
            else:
                # Not a valid project (was a common word like "has", "is", etc.)
                logger.debug(f"[LIGHT_PLANNER] Extracted project candidate '{candidate}' is not a valid ADO project, continuing")
                # Continue to next pattern or use default
    
    return enhanced


async def call_light_planner_for_routing(
    query: str,
    context: Dict[str, Any],
    session_id: str = None
) -> Dict[str, Any]:
    """
    Call the light LLM planner to generate a routing hint.
    
    This function:
    1. Checks if light planner is enabled in config
    2. Checks cache for existing result
    3. Calls a small/cheap LLM model to classify the query
    4. Returns a routing hint with confidence score
    5. Falls back to heuristic if LLM unavailable/fails
    
    Args:
        query: User query string
        context: Routing context dict (project, team, skill_hint, etc.)
        session_id: Optional session ID for tracing
    
    Returns:
        Dict with keys: route, skill, action_hint, confidence, type
        
        Example:
        {
            "route": "pm_agent",
            "skill": None,
            "action_hint": "Query about work items in current sprint",
            "confidence": 0.85,
            "type": "routing_hint",
            "source": "light_llm"
        }
    """
    from config import config
    
    # Check if light planner is enabled
    if not config.light_planner_enabled:
        logger.debug("[LIGHT_PLANNER] Disabled in config, skipping")
        return None
    
    # STEP 1: Extract team/project from query text if not in context
    context = _extract_entities_from_query(query, context)
    
    # NOTE: Span creation is handled by the CALLER (Orchestrator)
    # This function should NOT create its own span to avoid duplicates
    # The Orchestrator creates 'light_planner' span before calling this function
    
    try:
        # Check cache
        cache_key = _get_cache_key(query, context)
        cached_result = _check_cache(cache_key, config.light_planner_cache_ttl)
        
        if cached_result:
            logger.info("[LIGHT_PLANNER] Cache hit for query")
            cached_result["cached"] = True
            # Ensure has_full_plan is present in cached results
            if "has_full_plan" not in cached_result:
                cached_result["has_full_plan"] = False
            return cached_result
        
        # Check if OpenAI is available
        if not OPENAI_AVAILABLE or not config.openai_api_key:
            logger.warning("[LIGHT_PLANNER] OpenAI unavailable, using fallback heuristic")
            result = _fallback_heuristic_routing(query, context)
            return result
        
        # Extract routing attempts context for enhanced prompt
        routing_attempts = context.get("routing_attempts", {})
        regex_failed = context.get("regex_failed", True)
        semantic_failed = context.get("semantic_failed", True)
        semantic_best_skill = routing_attempts.get("semantic_skill") or context.get("semantic_best_skill")
        semantic_best_score = routing_attempts.get("semantic_score", 0.0) or context.get("semantic_best_score", 0.0)
        
        # Build enhanced prompt with context about prior matching attempts
        prior_attempts_context = ""
        if regex_failed or semantic_failed:
            prior_attempts_context = "\n\nPRIOR MATCHING ATTEMPTS (already tried, failed):"
            if regex_failed:
                prior_attempts_context += "\n- Regex pattern matching: FAILED (no explicit patterns matched)"
            if semantic_failed:
                if semantic_best_skill:
                    prior_attempts_context += f"\n- Semantic matching: LOW CONFIDENCE (best match: '{semantic_best_skill}' with score {semantic_best_score:.2f})"
                else:
                    prior_attempts_context += "\n- Semantic matching: FAILED (no skills matched above threshold)"
            prior_attempts_context += "\n\nSince both regex and semantic matching failed, use your understanding of the query to determine intent."
        
        # Build context information with resolved values
        project_info = context.get('project', 'FracPro-OPS')
        team_info = context.get('team', 'XOPS 25') if not context.get('_multi_team_query') else None
        sprint_info = context.get('sprint', 'current')
        
        # Mark defaults
        team_note = " (default)" if context.get('_team_defaulted') else ""
        sprint_note = " (default)" if context.get('_sprint_defaulted') else ""
        
        # CRITICAL: Handle multi-team queries vs single-team queries
        if context.get('_multi_team_query'):
            team_info_str = "NOT APPLICABLE - this is a multi-team query, do NOT filter by team"
            area_path_info = "NOT APPLICABLE - multi-team query"
        elif context.get('_team_defaulted'):
            # If team was defaulted, do NOT show area path - this prevents LLM from filtering by it
            team_info_str = f"{team_info}{team_note}"
            area_path_info = "DO NOT USE - team was defaulted, show ALL sprint items"
        else:
            # Team was explicitly mentioned
            team_info_str = f"{team_info}{team_note}"
            area_path_info = context.get('resolved_area_path', 'Not resolved')
        
        # ═══════════════════════════════════════════════════════════════════════════════
        # GET VALID STATES FROM ADO (STATE RESOLUTION DURING PLANNING)
        # This happens ONCE during planning - no redundant LLM calls later
        # ═══════════════════════════════════════════════════════════════════════════════
        valid_states_info = ""
        try:
            valid_states = app_config.get_all_known_states()
            if valid_states:
                valid_states_info = f"""\n\nVALID ADO STATES FOR PROJECT '{project_info}':
{', '.join(valid_states)}

STATE RESOLUTION RULES (CRITICAL - RESOLVE DURING PLANNING):
⚠️ CRITICAL: ONLY map synonyms to valid states, NEVER map valid states to other valid states!
- If user state is IN the valid states list above → USE IT EXACTLY (e.g., "UAT" → "UAT")
- If user state is NOT in the list → Map to closest synonym:
  - "done"/"completed"/"finished" → "Closed" or "Resolved"
  - "in progress"/"working" → "Active" or "In Progress"  
  - "open"/"pending"/"outstanding" → Do NOT add a state filter! Instead, set analysis_criteria.filter_logic to "exclude Closed/Resolved/Completed items" — the synthesizer will filter by state.
  - "started" → "Active" or "In Progress"
- ONLY use states from the valid states list above
- This resolves states at planning time - NO redundant LLM calls during execution
- If user doesn't mention states, omit state parameter (system will show all states)
"""
        except Exception as e:
            logger.warning(f"[LIGHT_PLANNER] Could not fetch valid states: {e}")
        
        # ═══════════════════════════════════════════════════════════════════════════════
        # EXTRACTED STATE OVERRIDE: If we extracted a specific state from the query,
        # inject it explicitly so the LLM doesn't guess/remap it
        # ═══════════════════════════════════════════════════════════════════════════════
        extracted_state_info = ""
        if context.get("extracted_state"):
            extracted_state = context["extracted_state"]
            extracted_state_info = f"""
🔴 MANDATORY STATE OVERRIDE (DO NOT CHANGE):
The user explicitly requested state: "{extracted_state}"
You MUST use state: ["{extracted_state}"] in your plan args.
DO NOT map, rename, or substitute this state. Use "{extracted_state}" EXACTLY."""
            logger.info(f"[LIGHT_PLANNER] Injecting extracted state override: '{extracted_state}'")
        
        # ═══════════════════════════════════════════════════════════════════════════════
        # INJECT AVAILABLE AREA PATHS FOR DYNAMIC RESOLUTION
        # ═══════════════════════════════════════════════════════════════════════════════
        available_area_paths_info = ""
        area_paths_list = context.get("area_paths", [])
        if area_paths_list:
            # Show top 10 most commonly used area paths to avoid token bloat
            display_paths = area_paths_list[:15] if len(area_paths_list) > 15 else area_paths_list
            area_paths_str = "\n".join([f"  - {ap}" for ap in display_paths])
            truncated_note = f" (showing {len(display_paths)} of {len(area_paths_list)})" if len(area_paths_list) > 15 else ""
            available_area_paths_info = f"""

AVAILABLE AREA PATHS IN PROJECT{truncated_note}:
{area_paths_str}

🔴 CRITICAL AREA PATH RULE:
- If user mentions a team name like "XOPS 25" or "WTT Development", you MUST use the FULL area path
- Example: User says "XOPS 25" → Use "Global Management\\WTT Development\\XOPS 25"
- Example: User says "FracPro Suite" → Use "Global FP\\FracPro Suite"
- NEVER use partial paths like "XOPS 25" alone - always use the complete hierarchical path
- Match the partial name to the area path list above and use the full path
"""
            logger.debug(f"[LIGHT_PLANNER] Injected {len(display_paths)} area paths into prompt")
        
        user_prompt = f"""Query: {query}

❗ SKILL CHECK: Does this query match a PM Skill? (backlog health/status/triaging/analysis/runway/triage → call_skill with backlog_triaging). Skills take priority over MCP tools.

RESOLVED CONTEXT (use these values directly in your plan):
- Project: {project_info}
- Team: {team_info_str}
- Sprint: {sprint_info}{sprint_note}
- Area Path: {area_path_info}
- Skill hint: {context.get('skill_hint', 'None')}
- Intent hint: {context.get('intent_hint', 'None')}
{available_area_paths_info}{valid_states_info}{extracted_state_info}
CRITICAL: For sprint/iteration work item queries:
1. Use wit_get_work_items_for_iteration with project, team, and iterationId (@CurrentIteration for current sprint)
2. For "previous sprint" queries, use iterationId: "@PreviousIteration"
3. IMPORTANT AREA PATH RULE: 
   - ONLY include areaPath parameter when the user EXPLICITLY mentions a team name (like "XOPS 25", "WTT Development")
   - If no team is mentioned, DO NOT include areaPath - this shows ALL work items in the sprint regardless of team
   - The team noted above as "(default)" means it was NOT in the query - do NOT use areaPath for default teams
4. workItemType FILTER RULE:
   - ONLY include workItemType if the user EXPLICITLY names a type (e.g., "bugs", "tasks", "user stories")
   - If user says generic terms like "items", "work items", "in-progress items", "all items", "sprint performance" → DO NOT include workItemType at all!
   - Valid types when needed: Bug, Dev Bug, Task, User Story, Feature
5. IMPORTANT STATE HANDLING:
   ⚠️ CRITICAL: If user's state exists in VALID ADO STATES list, use it EXACTLY
   - First check if user's state is in the valid states list
   - If YES → use it exactly (e.g., user says "UAT" and "UAT" is in list → use "UAT")
   - If NO → map to closest synonym (e.g., "done" → "Closed")
   - NEVER map valid states to other valid states (e.g., "UAT" → "QA" is WRONG)
6. ONLY include state or workItemType filters when the user EXPLICITLY or IMPLICITLY mentions them in the query. For generic queries like "all items", "group by state", "sprint summary" → do NOT add any filters.
   - "open bugs" → workItemType: ["Bug"] only (NO state filter!) + filter_logic: "exclude completed/closed/resolved items"
   - "pending tasks" → workItemType: ["Task"] only (NO state filter!) + filter_logic: "exclude completed/closed/resolved items"
   - "active bugs" → workItemType: ["Bug"] + state: ["Active"] (EXPLICIT state keyword)
   - "bugs" alone → workItemType: ["Bug"] only (no state filter)
{prior_attempts_context}

Generate a complete execution plan with CONCRETE VALUES from the context above. Return as JSON."""
        
        # Call OpenAI
        from .utils import run_in_executor_with_context
        
        def _call_light_llm() -> str:
            import httpx
            client = OpenAI(api_key=config.openai_api_key, timeout=httpx.Timeout(25.0, connect=5.0))
            # Use higher max_tokens to support detailed plans with WIQL queries
            # 1600 tokens minimum to avoid truncation of complex WIQL queries
            max_tokens = max(config.light_planner_max_tokens, 1600)  # Ensure at least 1600 tokens
            response = client.chat.completions.create(
                model=config.light_planner_model,
                messages=[
                    {"role": "system", "content": _build_dynamic_system_prompt()},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=config.light_planner_temperature,
                max_tokens=max_tokens,
                response_format={"type": "json_object"}
            )
            return response.choices[0].message.content
        
        # Run in executor to avoid blocking with 30-second timeout
        result_text = await asyncio.wait_for(
            run_in_executor_with_context(_call_light_llm),
            timeout=30.0
        )
        
        # Parse result - log raw response BEFORE any processing
        logger.info(f"[LIGHT_PLANNER] === RAW LLM RESPONSE (pre-parse) ===")
        logger.info(f"[LIGHT_PLANNER] Raw text ({len(result_text)} chars): {result_text[:1500]}")
        if len(result_text) > 1500:
            logger.info(f"[LIGHT_PLANNER] ... (truncated, full length: {len(result_text)})")
        
        try:
            result = json.loads(result_text)
            logger.info(f"[LIGHT_PLANNER] Parsed JSON successfully, keys: {list(result.keys())}")
        except json.JSONDecodeError as e:
            # Try to extract JSON from response - use greedy nested pattern
            logger.warning(f"[LIGHT_PLANNER] JSON parse error: {e}, attempting extraction")
            
            # Try to find the outermost JSON object (handles nested braces)
            brace_count = 0
            start_idx = -1
            end_idx = -1
            for i, char in enumerate(result_text):
                if char == '{':
                    if brace_count == 0:
                        start_idx = i
                    brace_count += 1
                elif char == '}':
                    brace_count -= 1
                    if brace_count == 0 and start_idx >= 0:
                        end_idx = i + 1
                        break
            
            if start_idx >= 0 and end_idx > start_idx:
                json_str = result_text[start_idx:end_idx]
                try:
                    result = json.loads(json_str)
                    logger.info(f"[LIGHT_PLANNER] Extracted JSON successfully from raw text")
                except json.JSONDecodeError:
                    logger.warning("[LIGHT_PLANNER] Failed to parse extracted JSON, using fallback")
                    result = _fallback_heuristic_routing(query, context)
                    return result
            else:
                logger.warning("[LIGHT_PLANNER] No valid JSON found in response, using fallback")
                result = _fallback_heuristic_routing(query, context)
                return result
        
        # POST-PROCESSING: Normalize common LLM schema mistakes
        result = _normalize_llm_response(result, query, context)
        logger.info(f"[LIGHT_PLANNER] After normalization: plan={'not None' if result.get('plan') else 'None'}, needs_clarification={result.get('needs_clarification')}")
        
        # ═══════════════════════════════════════════════════════════════════════════════
        # POST-PLAN AREA PATH CORRECTION: Resolve partial area paths to full paths
        # This is the definitive fix for partial path issues (e.g., "XOPS 25" → full path)
        # ═══════════════════════════════════════════════════════════════════════════════
        area_paths_list = context.get("area_paths", [])
        if area_paths_list and result.get("plan"):
            plan_obj = result["plan"]
            steps = plan_obj.get("steps", [])
            for step in steps:
                if not isinstance(step, dict):
                    continue
                args = step.get("args", {})
                if "areaPath" in args and args["areaPath"]:
                    partial_path = args["areaPath"]
                    # Handle list areaPath values (LLM may send ["XOPS 25"] instead of "XOPS 25")
                    if isinstance(partial_path, list):
                        resolved_list = []
                        for p in partial_path:
                            p_str = str(p).strip()
                            if "\\" not in p_str:
                                full_path = _resolve_area_path_from_partial(p_str, area_paths_list)
                                if full_path:
                                    logger.info(f"[LIGHT_PLANNER] POST-PLAN AREA PATH CORRECTION (list): '{p_str}' → '{full_path}'")
                                    resolved_list.append(full_path)
                                else:
                                    logger.warning(f"[LIGHT_PLANNER] Could not resolve partial area path '{p_str}' - keeping as-is")
                                    resolved_list.append(p_str)
                            else:
                                resolved_list.append(p_str)
                        args["areaPath"] = resolved_list
                    # Handle string areaPath values
                    elif isinstance(partial_path, str):
                        # Check if it's already a full path (contains backslashes)
                        if "\\" not in str(partial_path):
                            # Try to resolve partial to full path
                            full_path = _resolve_area_path_from_partial(partial_path, area_paths_list)
                            if full_path:
                                args["areaPath"] = full_path
                                logger.info(f"[LIGHT_PLANNER] POST-PLAN AREA PATH CORRECTION: '{partial_path}' → '{full_path}'")
                            else:
                                logger.warning(f"[LIGHT_PLANNER] Could not resolve partial area path '{partial_path}' - keeping as-is")
        
        # ═══════════════════════════════════════════════════════════════════════════════
        # POST-PLAN STATE CORRECTION: If we extracted a specific state from the query,
        # enforce it in the plan args regardless of what the LLM chose.
        # This is the definitive code-level fix — prompts alone are unreliable.
        # ═══════════════════════════════════════════════════════════════════════════════
        extracted_state = context.get("extracted_state")
        if extracted_state and result.get("plan"):
            plan_obj = result["plan"]
            steps = plan_obj.get("steps", [])
            for step in steps:
                if not isinstance(step, dict):
                    continue
                args = step.get("args", {})
                if "state" in args:
                    old_state = args["state"]
                    # Replace with the extracted state
                    args["state"] = [extracted_state]
                    if old_state != [extracted_state]:
                        logger.info(f"[LIGHT_PLANNER] POST-PLAN STATE CORRECTION: '{old_state}' → '[{extracted_state}]'")
        
        # ═══════════════════════════════════════════════════════════════════════════════
        # POST-PLAN WIQL CORRECTION REMOVED — LLM planner is now responsible for
        # choosing execute_wiql when date/priority filtering is needed.
        # The system prompt includes ADO field reference table and WIQL examples.
        # No post-hoc regex overrides. If LLM makes wrong choice, improve the prompt.
        # ═══════════════════════════════════════════════════════════════════════════════
        
        # Validate and normalize result
        result = _validate_light_planner_result(result)
        
        # ═══════════════════════════════════════════════════════════════════════════════
        # POST-VALIDATION: Convert call_skill plans to proper skill routing hints
        # When the LLM generates action="call_skill" with a PM skill (e.g., backlog_triaging),
        # we need to set route="pm_skill_agent" so the router sends it to the skill agent.
        # Without this, the plan goes to PM Agent where the deep planner fails validation.
        # ═══════════════════════════════════════════════════════════════════════════════
        plan_obj = result.get("plan")
        if plan_obj and isinstance(plan_obj, dict):
            plan_steps = plan_obj.get("steps", [])
            for step in plan_steps:
                if isinstance(step, dict) and step.get("action") == "call_skill" and step.get("tool"):
                    skill_name = step["tool"]
                    skill_args = step.get("args", {})
                    logger.info(f"[LIGHT_PLANNER] Detected call_skill plan for '{skill_name}' — converting to skill routing hint")
                    result["route"] = "pm_skill_agent"
                    result["skill"] = skill_name
                    result["action_hint"] = f"LLM selected PM skill: {skill_name}"
                    # Merge skill args into result params for the skill agent
                    if skill_args:
                        result["skill_params"] = skill_args
                    # DO NOT override LLM's confidence - respect its assessment.
                    # The config accept_threshold (0.80) gates acceptance in the router.
                    # Artificially boosting confidence bypasses proper threshold checks.
                    break  # Only process first skill step
        
        result["type"] = "execution_plan" if result.get("has_full_plan") else "routing_hint"
        result["source"] = "light_llm"
        result["cached"] = False
        # has_full_plan is set by validation, don't override
        
        # Include context enrichment flags in result for validation/debugging
        if context.get("_multi_team_query"):
            result["_multi_team_query"] = True
        if context.get("_team_defaulted"):
            result["_team_defaulted"] = True
        if context.get("_no_team_filter"):
            result["_no_team_filter"] = True
        
        # Change 2: Always ensure synthesis step for full plans with execution steps
        # This guarantees consistent multi-tool trace structure regardless of LLM non-determinism
        if result.get("has_full_plan") and result.get("plan"):
            result = _append_synthesis_step_if_needed(result, query)
        
        # Cache the result
        _set_cache(cache_key, result)
        
        # Safe logging - handle None plan
        plan = result.get("plan")
        if plan:
            plan_type = plan.get("type", "unknown")
            num_steps = len(plan.get("steps", []))
            logger.info(f"[LIGHT_PLANNER] Plan type={plan_type}, steps={num_steps}, has_full_plan={result.get('has_full_plan')}, confidence={result.get('confidence'):.2f}")
        else:
            logger.info(f"[LIGHT_PLANNER] Validated full plan: {result.get('has_full_plan')}, needs_clarification={result.get('needs_clarification')}, confidence={result.get('confidence'):.2f}")
        
        return result
    
    except asyncio.TimeoutError:
        logger.warning("[LIGHT_PLANNER] LLM call timed out after 30s, using fallback")
        result = _fallback_heuristic_routing(query, context)
        return result
        
    except Exception as e:
        import traceback
        logger.error(f"[LIGHT_PLANNER] Error: {e}")
        logger.error(f"[LIGHT_PLANNER] Traceback: {traceback.format_exc()}")
        
        # Fall back to heuristic
        if config.light_planner_use_fallback:
            result = _fallback_heuristic_routing(query, context)
            return result
        
        return None


def _normalize_llm_response(result: Dict[str, Any], query: str, context: Dict[str, Any]) -> Dict[str, Any]:
    """
    Post-process LLM response to normalize common schema mistakes.
    
    Fixes:
    - Tool name in 'action' field → move to 'tool' and set action='call_tool'
    - Missing needs_clarification field
    - null plan when should be clarification request
    
    Args:
        result: Raw LLM response
        query: Original query
        context: Query context
        
    Returns:
        Normalized result
    """
    # Ensure result is a dict
    if not isinstance(result, dict):
        return result
    
    # Fix missing needs_clarification field
    if "needs_clarification" not in result:
        result["needs_clarification"] = False
    if "clarification_questions" not in result:
        result["clarification_questions"] = []
    
    # Check if plan is None/null but should be clarification
    # IMPORTANT: Be conservative - only force clarification for sprint/iteration queries
    # that explicitly need team context. Trust LLM for other cases.
    plan = result.get("plan")
    if plan is None and not result.get("needs_clarification"):
        # Only force clarification for queries that MUST have team for @CurrentIteration
        requires_team_for_sprint = any(kw in query.lower() for kw in ["current sprint", "current iteration", "@currentiteration"])
        team_missing = not context.get("team")
        
        if requires_team_for_sprint and team_missing:
            logger.info(f"[LIGHT_PLANNER] Normalizing null plan to clarification request (sprint query needs team)")
            result["needs_clarification"] = True
            result["clarification_questions"] = ["Which team should I query for current sprint/iteration? (e.g., NES, XOPS, UI)"]
            result["plan"] = None
            result["has_full_plan"] = False
            result["confidence"] = result.get("confidence", 0.7)
            result["analysis_summary"] = "Current sprint/iteration queries require team context"
            return result
        
        # For other cases, trust the LLM - if it returned null plan without clarification,
        # it likely means the query is too ambiguous or needs Deep LLM analysis
        # Don't force clarification here - let orchestrator handle fallback
    
    # Normalize plan steps if present
    if plan and isinstance(plan, dict):
        steps = plan.get("steps", [])
        if isinstance(steps, list):
            normalized_steps = []
            validation_errors = []  # Track validation errors
            for idx, step in enumerate(steps):
                if not isinstance(step, dict):
                    continue
                
                # FIX: Tool name in action field
                action = step.get("action")
                tool = step.get("tool")
                
                # FIX: For call_skill actions, LLM may put skill name in "skill" field instead of "tool"
                if action == "call_skill" and not tool and step.get("skill"):
                    step["tool"] = step["skill"]
                    tool = step["tool"]
                    logger.info(f"[LIGHT_PLANNER] Step {idx}: Copied 'skill' field to 'tool' for call_skill action: {tool}")
                
                # If action is a tool name (not call_tool/call_skill/synthesize), fix it
                if action and action not in ("call_tool", "call_skill", "synthesize"):
                    # Action field has a tool name - normalize it
                    logger.info(f"[LIGHT_PLANNER] Normalizing step {idx}: action='{action}' -> determining action type")
                    
                    # LOCAL TOOLS that must be routed as call_tool (NOT call_skill)
                    # execute_wiql is a LOCAL REST API skill that calls ADO directly.
                    # It must route through call_tool -> orchestrator -> local handler,
                    # NOT through call_skill -> pm_skill_agent (which spins up new MCP).
                    _LOCAL_TOOL_OVERRIDES = {"execute_wiql"}
                    
                    if action in _LOCAL_TOOL_OVERRIDES:
                        step["action"] = "call_tool"
                        logger.info(f"[LIGHT_PLANNER] Step {idx}: '{action}' is a LOCAL tool override -> action='call_tool'")
                    else:
                        # Dynamically determine if it's a tool or skill from SKILL_DEFINITIONS
                        skill_definitions = _get_skill_definitions()
                        known_skill_names = set(skill_definitions.keys()) if skill_definitions else set()
                        
                        if action in known_skill_names:
                            step["action"] = "call_skill"
                            logger.info(f"[LIGHT_PLANNER] Step {idx}: '{action}' is a skill -> action='call_skill'")
                        else:
                            step["action"] = "call_tool"
                            logger.info(f"[LIGHT_PLANNER] Step {idx}: '{action}' is a tool -> action='call_tool'")
                    
                    step["tool"] = action
                
                # CRITICAL FIX: Validate tool exists in registry (prevent hallucination)
                if step.get("action") == "call_tool" and step.get("tool"):
                    tool_name = step["tool"]
                    if tool_name not in TOOL_REGISTRY:
                        # Tool doesn't exist - try to find close match
                        all_tools = list(TOOL_REGISTRY.keys())
                        close_matches = difflib.get_close_matches(tool_name, all_tools, n=1, cutoff=0.85)
                        if close_matches:
                            suggested_tool = close_matches[0]
                            logger.warning(f"[LIGHT_PLANNER] VALIDATION: Tool '{tool_name}' not found, suggesting '{suggested_tool}'")
                            step["tool"] = suggested_tool
                            step["_original_tool"] = tool_name  # Track for logging
                        else:
                            # No close match - this is a hallucinated tool
                            error_msg = f"Step {idx}: Tool '{tool_name}' does not exist in registry. Available similar tools: {', '.join([t for t in all_tools if any(word in t for word in tool_name.split('_'))])[:100]}"
                            validation_errors.append(error_msg)
                            logger.error(f"[LIGHT_PLANNER] VALIDATION FAILED: {error_msg}")
                            # Don't add this step - it will fail
                            continue
                
                normalized_steps.append(step)
            
            # If we had validation errors, add them to result
            if validation_errors:
                result["validation_errors"] = validation_errors
                result["has_full_plan"] = False
                result["confidence"] = max(0.3, result.get("confidence", 0.5) - 0.2)
                logger.error(f"[LIGHT_PLANNER] Plan validation failed with {len(validation_errors)} errors")
            
            plan["steps"] = normalized_steps
            
            # Check if plan has concrete args (WIQL, specific values) - if so, set has_full_plan=True
            # This ensures plans with proper WIQL queries aren't incorrectly marked as incomplete
            has_concrete_args = _check_plan_has_concrete_args(normalized_steps)
            if has_concrete_args and not result.get("has_full_plan"):
                logger.info("[LIGHT_PLANNER] Normalization: Detected concrete args in plan, setting has_full_plan=True")
                result["has_full_plan"] = True
    
    # ═══════════════════════════════════════════════════════════════════════════
    # POST-PROCESSING: Catch wrong tool selection (wit_get_work_items_for_iteration
    # used when user never mentioned a sprint/iteration → convert to execute_wiql)
    # ═══════════════════════════════════════════════════════════════════════════
    result = _post_process_tool_correction(result, query, context)
    
    return result


def _post_process_tool_correction(result: Dict[str, Any], query: str, context: Dict[str, Any]) -> Dict[str, Any]:
    """
    Post-processing guard: If LLM chose wit_get_work_items_for_iteration but the user
    never mentioned a sprint/iteration, convert the plan to execute_wiql instead.
    This prevents the LLM from auto-injecting @CurrentIteration scope.
    """
    if not isinstance(result, dict):
        return result
    
    plan = result.get("plan")
    if not plan or not isinstance(plan, dict):
        return result
    
    steps = plan.get("steps", [])
    if not steps:
        return result
    
    query_lower = query.lower()
    
    # Check if user explicitly mentioned a sprint/iteration
    sprint_keywords = [
        "current sprint", "this sprint", "current iteration", "this iteration",
        "previous sprint", "last sprint", "prior sprint",
        "@currentiteration", "@previousiteration",
    ]
    # Also check for numeric sprint references
    import re as _re_pp
    has_sprint_mention = (
        any(kw in query_lower for kw in sprint_keywords)
        or bool(_re_pp.search(r'\bsprint\s*\d', query_lower))
        or bool(_re_pp.search(r'\biteration\s*\d', query_lower))
        or bool(_re_pp.search(r'\b\d{2}\.\d{1,2}\b', query_lower))
    )
    
    if has_sprint_mention:
        # User mentioned sprint — wit_get_work_items_for_iteration is valid
        return result
    
    # ── Special case: "list/show iterations" queries ──
    # If user asks about iterations themselves (not work items in iterations),
    # the correct tool is work_list_team_iterations, NOT wit_get_work_items_for_iteration
    iteration_list_patterns = [
        r'\b(list|show|get|display|what\s+are)\s+(the\s+)?(all\s+)?iterations\b',
        r'\biterations\s+(for|of)\b',
        r'\b(list|show|get)\s+.*\bsprints?\b',
    ]
    work_item_indicators = ["work item", "bugs", "tasks", "stories", "assigned", "completed",
                            "blocked", "open", "overdue", "burndown", "story points", "count"]
    is_iteration_list_query = (
        any(_re_pp.search(p, query_lower) for p in iteration_list_patterns)
        and not any(wi in query_lower for wi in work_item_indicators)
    )
    
    if is_iteration_list_query:
        for step in steps:
            if not isinstance(step, dict):
                continue
            if step.get("tool") == "wit_get_work_items_for_iteration":
                args = step.get("args", {})
                project = args.get("project", context.get("project", "FracPro-OPS"))
                team = args.get("team", context.get("team", "XOPS 25"))
                
                logger.warning(f"[LIGHT_PLANNER] POST-PROCESS: Converting wit_get_work_items_for_iteration → work_list_team_iterations "
                               f"(user asked about iterations themselves, not work items)")
                
                step["tool"] = "work_list_team_iterations"
                step["action"] = "call_tool"
                step["args"] = {"project": project, "team": team}
                step["description"] = f"[Post-processed] List iterations for team: {query}"
                result["_post_processed"] = True
        return result
    
    # Check each step: if it uses wit_get_work_items_for_iteration, convert to execute_wiql
    modified = False
    for step in steps:
        if not isinstance(step, dict):
            continue
        if step.get("tool") != "wit_get_work_items_for_iteration":
            continue
        
        # This step wrongly uses wit_get_work_items_for_iteration without user mentioning sprint
        args = step.get("args", {})
        project = args.get("project", context.get("project", "FracPro-OPS"))
        
        # ─── ASSIGNEE QUERIES: Route to search_workitem with a:<name> prefix ───
        # WIQL CONTAINS on System.AssignedTo is unreliable for multi-word names
        # (e.g., "Vaibhav Garg", "Richa Yadav" return 0 results with CONTAINS).
        # ADO Search API with "a:<name>" prefix does proper fuzzy identity matching.
        assigned_to = args.get("assignedTo")
        if assigned_to:
            if isinstance(assigned_to, list) and len(assigned_to) > 0:
                assigned_to = assigned_to[0]
            if isinstance(assigned_to, str) and assigned_to.strip():
                person_name = assigned_to.strip()
                search_args = {
                    "searchText": f"a:{person_name}",
                    "project": [project] if isinstance(project, str) else project,
                    # NOTE: Do NOT pass assignedTo as a separate filter here!
                    # ADO Search API's assignedTo filter does EXACT match on display name,
                    # which fails for partial names like "Vivek" or "Vaibhav Garg".
                    # The "a:<name>" prefix in searchText does proper fuzzy identity matching.
                    "top": 200
                }
                
                # Forward area path for team scoping
                _resolved_ap = context.get('resolved_area_path')
                if _resolved_ap and not context.get('_team_defaulted') and not context.get('_multi_team_query'):
                    search_args["areaPath"] = [_resolved_ap]
                    logger.info(f"[LIGHT_PLANNER] POST-PROCESS: Injected area path into search_workitem: {_resolved_ap}")
                
                # Forward workItemType filter
                wit = args.get("workItemType")
                if wit:
                    if isinstance(wit, list):
                        search_args["workItemType"] = wit
                    elif isinstance(wit, str):
                        search_args["workItemType"] = [wit]
                
                # Forward state filter
                state_val = args.get("state")
                if state_val:
                    if isinstance(state_val, list):
                        search_args["state"] = state_val
                    elif isinstance(state_val, str):
                        search_args["state"] = [state_val]
                
                logger.warning(f"[LIGHT_PLANNER] POST-PROCESS: Converting wit_get_work_items_for_iteration → search_workitem "
                               f"(assignee query, user did NOT mention sprint). searchText: a:{person_name}")
                
                step["tool"] = "search_workitem"
                step["action"] = "call_tool"
                step["args"] = search_args
                step["description"] = f"[Post-processed] Converted to search_workitem for assignee '{person_name}': {query}"
                modified = True
                continue  # Skip WIQL conversion below
        
        # ─── NON-ASSIGNEE QUERIES: Convert to WIQL ───
        # Build WIQL WHERE clauses from the original args
        where_clauses = [f"[System.TeamProject] = '{project}'"]
        
        # Extract workItemType
        wit = args.get("workItemType")
        if wit:
            if isinstance(wit, list) and len(wit) == 1:
                where_clauses.append(f"[System.WorkItemType] = '{wit[0]}'")
            elif isinstance(wit, list) and len(wit) > 1:
                in_clause = ", ".join(f"'{w}'" for w in wit)
                where_clauses.append(f"[System.WorkItemType] IN ({in_clause})")
            elif isinstance(wit, str):
                where_clauses.append(f"[System.WorkItemType] = '{wit}'")
        
        # Extract state
        state_val = args.get("state")
        if state_val:
            if isinstance(state_val, list) and len(state_val) == 1:
                where_clauses.append(f"[System.State] = '{state_val[0]}'")
            elif isinstance(state_val, list) and len(state_val) > 1:
                in_clause = ", ".join(f"'{s}'" for s in state_val)
                where_clauses.append(f"[System.State] IN ({in_clause})")
            elif isinstance(state_val, str):
                where_clauses.append(f"[System.State] = '{state_val}'")
        
        # Extract unassigned
        if args.get("unassigned"):
            where_clauses.append("[System.AssignedTo] = ''")
        
        # Extract area path from context (resolved by _extract_entities_from_query)
        _resolved_ap = context.get('resolved_area_path')
        if _resolved_ap and not context.get('_team_defaulted') and not context.get('_multi_team_query'):
            where_clauses.append(f"[System.AreaPath] UNDER '{_resolved_ap}'")
            logger.info(f"[LIGHT_PLANNER] POST-PROCESS: Injected area path into WIQL: {_resolved_ap}")
        
        wiql = f"SELECT [System.Id], [System.Title], [System.State], [System.WorkItemType], [System.AssignedTo] FROM WorkItems WHERE {' AND '.join(where_clauses)} ORDER BY [System.ChangedDate] DESC"
        
        logger.warning(f"[LIGHT_PLANNER] POST-PROCESS: Converting wit_get_work_items_for_iteration → execute_wiql "
                       f"(user did NOT mention sprint). WIQL: {wiql[:200]}")
        
        step["tool"] = "execute_wiql"
        step["action"] = "call_tool"
        step["args"] = {
            "project": project,
            "wiql": wiql,
            "top": 1000
        }
        step["description"] = f"[Post-processed] Converted to WIQL (no sprint mentioned): {query}"
        modified = True
    
    if modified:
        result["_post_processed"] = True
        # ── Also fix analysis_criteria and analysis_summary that still reference "current sprint" ──
        ac = result.get("analysis_criteria", {})
        if isinstance(ac, dict):
            fl = ac.get("filter_logic", "")
            if fl and any(kw in fl.lower() for kw in ["current sprint", "current iteration", "@currentiteration"]):
                import re as _re_ac
                cleaned_fl = _re_ac.sub(r'\s*in the current (sprint|iteration)', '', fl, flags=_re_ac.IGNORECASE).strip()
                cleaned_fl = _re_ac.sub(r'\s*for the current (sprint|iteration)', '', cleaned_fl, flags=_re_ac.IGNORECASE).strip()
                if cleaned_fl != fl:
                    ac["filter_logic"] = cleaned_fl
                    logger.info(f"[LIGHT_PLANNER] POST-PROCESS: Cleaned analysis_criteria.filter_logic: '{fl}' → '{cleaned_fl}'")
        asummary = result.get("analysis_summary", "")
        if isinstance(asummary, str) and any(kw in asummary.lower() for kw in ["current sprint", "current iteration"]):
            import re as _re_as
            cleaned_as = _re_as.sub(r'\s*in the current (sprint|iteration)', '', asummary, flags=_re_as.IGNORECASE).strip()
            cleaned_as = _re_as.sub(r'\s*for the current (sprint|iteration)', '', cleaned_as, flags=_re_as.IGNORECASE).strip()
            if cleaned_as != asummary:
                result["analysis_summary"] = cleaned_as
                logger.info(f"[LIGHT_PLANNER] POST-PROCESS: Cleaned analysis_summary")
    
    # ── Final pass: strip @CurrentIteration from WIQL strings if no sprint mentioned ──
    if not has_sprint_mention:
        for step in plan.get("steps", []):
            if not isinstance(step, dict):
                continue
            if step.get("tool") == "execute_wiql":
                args = step.get("args", {})
                wiql_str = args.get("wiql", "")
                if "@CurrentIteration" in wiql_str or "@currentiteration" in wiql_str.lower():
                    import re as _re_wiql
                    # Remove iteration path clauses like: AND [System.IterationPath] = @CurrentIteration
                    cleaned = _re_wiql.sub(
                        r'\s*AND\s+\[System\.IterationPath\]\s*=\s*@CurrentIteration', 
                        '', wiql_str, flags=_re_wiql.IGNORECASE
                    )
                    # Also handle UNDER variant
                    cleaned = _re_wiql.sub(
                        r'\s*AND\s+\[System\.IterationPath\]\s+UNDER\s+@CurrentIteration',
                        '', cleaned, flags=_re_wiql.IGNORECASE
                    )
                    if cleaned != wiql_str:
                        args["wiql"] = cleaned
                        logger.warning(f"[LIGHT_PLANNER] POST-PROCESS: Stripped @CurrentIteration from WIQL "
                                       f"(user did NOT mention sprint)")
                        result["_post_processed"] = True
    
    return result


def _check_plan_has_concrete_args(steps: list) -> bool:
    """
    Check if plan steps have concrete, executable arguments.
    
    Returns True if:
    - Steps have WIQL queries with actual SELECT statements
    - Steps have project/work item IDs that look valid
    - No placeholder markers detected
    """
    if not steps:
        return False
    
    PLACEHOLDER_MARKERS = {
        "unknown", "<unknown>", "<from context>", 
        "current_iteration_id", "[from context]",
        "<team_name>", "<project_name>", ""
    }
    
    for step in steps:
        if not isinstance(step, dict):
            continue
        
        args = step.get("args", {})
        if not isinstance(args, dict):
            continue
        
        for key, value in args.items():
            # Skip None values
            if value is None:
                return False
            
            if isinstance(value, str):
                value_stripped = value.strip().lower()
                
                # Check for placeholder markers
                if value_stripped in {m.lower() for m in PLACEHOLDER_MARKERS}:
                    return False
                if "unknown" in value_stripped or "<from" in value_stripped:
                    return False
                
                # Positive indicators: concrete WIQL query
                if key == "wiql" and "SELECT" in value.upper() and "FROM" in value.upper():
                    logger.debug(f"[LIGHT_PLANNER] Found concrete WIQL in args.{key}")
                    return True
                
                # Positive indicators: project name looks real (not placeholder)
                if key == "project" and len(value) > 2 and value not in PLACEHOLDER_MARKERS:
                    continue  # Valid project, keep checking
    
    # If we have steps with args and no placeholders found, consider concrete
    for step in steps:
        args = step.get("args", {})
        if args and len(args) > 0:
            return True
    
    return False


def _validate_light_planner_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and normalize light planner result with comprehensive plan schema validation.
    
    Expected schema:
    {
        "plan": {
            "type": "single" | "multi",
            "steps": [
                {
                    "action": "call_tool" | "call_skill" | "synthesize",
                    "tool": str | None,
                    "args": dict,
                    "description": str,
                    "reasoning": str (optional)
                }
            ]
        },
        "confidence": float (0.0-1.0),
        "has_full_plan": bool,
        "analysis_summary": str (optional)
    }
    """
    # Validate result is a dictionary
    if not isinstance(result, dict):
        logger.warning(f"[LIGHT_PLANNER] Invalid result type: {type(result)}, using fallback")
        return {
            "plan": None,
            "confidence": 0.3,
            "has_full_plan": False,
            "type": "routing_hint",
            "source": "validation_fallback"
        }
    
    # Validate plan structure
    plan = result.get("plan")
    has_full_plan = False
    
    # Handle null/None plan (might be clarification request)
    if plan is None:
        logger.debug("[LIGHT_PLANNER] Plan is None - checking if clarification request")
        result["plan"] = None
        result["has_full_plan"] = False
        
        # Ensure needs_clarification fields exist
        if "needs_clarification" not in result:
            result["needs_clarification"] = False
        if "clarification_questions" not in result:
            result["clarification_questions"] = []
        
        # Validate confidence
        if "confidence" not in result or not isinstance(result.get("confidence"), (int, float)):
            result["confidence"] = 0.5
        
        # Ensure analysis_summary exists
        if "analysis_summary" not in result:
            if result.get("needs_clarification"):
                result["analysis_summary"] = "Clarification needed"
            else:
                result["analysis_summary"] = "No plan generated"
        
        return result
    
    if plan and isinstance(plan, dict):
        # Validate plan.type
        plan_type = plan.get("type", "single")
        if plan_type not in ("single", "multi"):
            logger.warning(f"[LIGHT_PLANNER] Invalid plan type '{plan_type}', defaulting to 'single'")
            plan["type"] = "single"
        
        # Validate plan.steps
        steps = plan.get("steps", [])
        if not isinstance(steps, list):
            logger.warning(f"[LIGHT_PLANNER] Invalid steps type: {type(steps)}, expected list")
            steps = []
            plan["steps"] = steps
        
        # Validate each step
        valid_steps = []
        for idx, step in enumerate(steps):
            if not isinstance(step, dict):
                logger.warning(f"[LIGHT_PLANNER] Step {idx} is not a dict, skipping")
                continue
            
            # Ensure required step fields
            if "action" not in step:
                logger.warning(f"[LIGHT_PLANNER] Step {idx} missing 'action' field, skipping")
                continue
            
            if step["action"] not in ("call_tool", "call_skill", "synthesize"):
                logger.warning(f"[LIGHT_PLANNER] Step {idx} invalid action '{step.get('action')}', skipping")
                continue
            
            # Validate tool field (for call_skill, also check 'skill' field)
            tool_field = step.get("tool") or (step.get("skill") if step["action"] == "call_skill" else None)
            if step["action"] in ("call_tool", "call_skill") and not tool_field:
                logger.warning(f"[LIGHT_PLANNER] Step {idx} action '{step['action']}' requires tool/skill, skipping")
                continue
            # Ensure tool field is populated (may have been in 'skill')
            if tool_field and not step.get("tool"):
                step["tool"] = tool_field
            
            # Ensure args is a dict
            if "args" not in step:
                step["args"] = {}
            elif not isinstance(step["args"], dict):
                logger.warning(f"[LIGHT_PLANNER] Step {idx} args is not a dict, converting")
                step["args"] = {}
            
            # Ensure description exists
            if "description" not in step or not step["description"]:
                step["description"] = f"Execute {step.get('tool', 'action')} with provided arguments"
            
            valid_steps.append(step)
        
        plan["steps"] = valid_steps
        
        # Check for placeholders in args (CRITICAL for executability)
        PLACEHOLDER_MARKERS = {"unknown", "Unknown", "<unknown>", "<from context>", "current_iteration_id", ""}
        has_placeholders = False
        for idx, step in enumerate(valid_steps):
            args = step.get("args", {})
            for key, value in args.items():
                if value is None:
                    logger.warning(f"[LIGHT_PLANNER] Step {idx} arg '{key}' is None - plan not executable")
                    has_placeholders = True
                elif isinstance(value, str) and value.strip() in PLACEHOLDER_MARKERS:
                    logger.warning(f"[LIGHT_PLANNER] Step {idx} arg '{key}' contains placeholder '{value}' - plan not executable")
                    has_placeholders = True
        
        # Determine if we have a full plan (must have steps AND no placeholders)
        has_full_plan = (
            len(valid_steps) > 0 
            and not has_placeholders
            and all(step.get("tool") or step.get("action") == "synthesize" for step in valid_steps)
        )
    else:
        logger.warning("[LIGHT_PLANNER] No valid plan structure found")
        result["plan"] = None
        has_full_plan = False
    
    # Validate and normalize confidence
    confidence = result.get("confidence", 0.5)
    try:
        confidence = float(confidence)
        confidence = max(0.0, min(1.0, confidence))
    except (ValueError, TypeError):
        logger.warning(f"[LIGHT_PLANNER] Invalid confidence value: {confidence}, using 0.5")
        confidence = 0.5
    result["confidence"] = confidence
    
    # Set has_full_plan flag (can be overridden by explicit value in result)
    if "has_full_plan" in result:
        # Trust the LLM's assessment but validate
        result["has_full_plan"] = bool(result["has_full_plan"]) and has_full_plan
    else:
        result["has_full_plan"] = has_full_plan
    
    # Ensure analysis_summary exists
    if "analysis_summary" not in result:
        if result["has_full_plan"] and plan:
            result["analysis_summary"] = f"Complete execution plan with {len(plan.get('steps', []))} step(s)"
        else:
            result["analysis_summary"] = "Incomplete plan or routing hint"
    
    # Log validation result
    if result["has_full_plan"] and plan:
        logger.info(f"[LIGHT_PLANNER] Validated full plan: {len(plan.get('steps', []))} steps, confidence={confidence:.2f}")
    else:
        logger.debug(f"[LIGHT_PLANNER] Validated partial/incomplete plan: confidence={confidence:.2f}")
    
    return result


def _append_synthesis_step_if_needed(result: Dict[str, Any], query: str) -> Dict[str, Any]:
    """
    Ensure synthesis step exists for plans with execution steps.
    
    For all plans with at least one tool/skill execution step, appends a synthesis
    step if not already present. This guarantees consistent multi-tool trace structure
    (execute_plan → multi_tool_plan_execution → llm_synthesis_multi_agent) regardless
    of LLM non-determinism in step count generation.
    
    Args:
        result: Light planner result dict (from _validate_light_planner_result)
        query: Original user query for query_hash generation
    
    Returns:
        Updated result with synthesis step appended if needed
    """
    import hashlib
    
    plan = result.get("plan")
    if not plan or not isinstance(plan, dict):
        return result
    
    steps = plan.get("steps", [])
    if not isinstance(steps, list) or len(steps) < 1:
        return result
    
    # Count non-synthesis steps (actual tool/skill execution steps)
    execution_steps = [s for s in steps if isinstance(s, dict) and s.get("action") in ("call_tool", "call_skill")]
    
    if len(execution_steps) < 1:
        return result
    
    # Check if synthesis step already exists
    has_synthesis = any(s.get("type") == "synthesis" or s.get("action") == "synthesize" for s in steps if isinstance(s, dict))
    if has_synthesis:
        logger.debug("[LIGHT_PLANNER] Synthesis step already present, skipping append")
        return result
    
    # Generate query hash for deduplication
    try:
        query_hash = hashlib.md5(query.lower().strip().encode()).hexdigest()[:16]
    except Exception as e:
        logger.warning(f"[LIGHT_PLANNER] Failed to generate query_hash: {e}, using timestamp-based fallback")
        import time
        query_hash = str(int(time.time() * 1000))[-8:]
    
    # Get all step IDs that this synthesis depends on
    step_ids = []
    for step in steps:
        if isinstance(step, dict) and step.get("step_id"):
            step_ids.append(step["step_id"])
    
    if not step_ids:
        logger.warning("[LIGHT_PLANNER] No step IDs found, cannot generate synthesis step")
        return result
    
    # Create synthesis step
    synthesis_step = {
        "step_id": f"step_{len(steps) + 1}",
        "type": "synthesis",
        "action": "synthesize",
        "depends_on": step_ids,
        "description": "Synthesize and summarize results from all execution steps",
        "reasoning": "Multi-step plan requires result aggregation to provide coherent response to user"
    }
    
    # Append to steps
    steps.append(synthesis_step)
    plan["steps"] = steps
    
    # Ensure plan type is "multi" now that we have execution + synthesis steps
    # This is needed for PM Agent to take the execute_plan → multi_tool_plan_execution path
    plan["type"] = "multi"
    
    # Update analysis_criteria to include depends_on if not already present
    analysis_criteria = result.get("analysis_criteria", {})
    if not analysis_criteria.get("depends_on"):
        analysis_criteria["depends_on"] = step_ids
    if not analysis_criteria.get("query_hash"):
        analysis_criteria["query_hash"] = query_hash
    if not analysis_criteria.get("original_query"):
        analysis_criteria["original_query"] = query
    result["analysis_criteria"] = analysis_criteria
    
    logger.info(f"[LIGHT_PLANNER] Appended synthesis step for plan ({len(execution_steps)} execution step(s), now {len(steps)} total steps)")
    return result


# ══════════════════════════════════════════════════════════════════════════════
# UTILITY FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

def should_invoke_light_planner(
    decision_confidence: float,
    has_skill_hint: bool,
    has_intent_hint: bool
) -> bool:
    """
    Determine if light planner should be invoked based on current routing state.
    
    Light planner is invoked when:
    - Confidence is below threshold (< 0.60)
    - No strong skill or intent hint from previous steps
    
    Args:
        decision_confidence: Current routing confidence
        has_skill_hint: Whether a skill hint exists
        has_intent_hint: Whether an intent hint exists
    
    Returns:
        True if light planner should be invoked
    """
    from config import config
    
    if not config.light_planner_enabled:
        return False
    
    # If confidence is already high, no need for light planner
    if decision_confidence >= 0.60:
        return False
    
    # If we have a strong hint, skip light planner
    if has_skill_hint or has_intent_hint:
        return False
    
    return True


def get_light_planner_thresholds() -> Dict[str, float]:
    """Get the configured light planner thresholds."""
    from config import config
    return {
        "accept_threshold": config.light_planner_accept_threshold,
        "escalate_threshold": config.light_planner_escalate_threshold
    }


# ══════════════════════════════════════════════════════════════════════════════
# EXPORTS
# ══════════════════════════════════════════════════════════════════════════════

__all__ = [
    "call_light_planner_for_routing",
    "should_invoke_light_planner",
    "get_light_planner_thresholds",
    "clear_light_planner_cache",
]
