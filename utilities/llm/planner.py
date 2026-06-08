"""
LLM Planner - Centralized tool selection and query planning.

This module provides the heavy/main LLM planner that can be used by:
- PM Agent (primary user)
- Orchestrator (optional, for pre-planning)
- Future agents requiring tool selection

Key Functions:
- plan_mcp_call(): Main planner function for tool selection
- run_in_executor_with_context(): Helper for running blocking calls with context preservation

CRITICAL: Uses OpenAI API configured via config.yaml (api_keys.openai_api_key)
All LLM calls create spans within the current trace for unified observability.
"""

import os
import json
import re
import asyncio
import logging
import contextvars
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Dict, Any, List, Tuple

try:
    from openai import OpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False

# Import trace context helpers for unified observability
from utilities.langfuse_client import (
    get_langfuse_client, create_span, create_generation, finalize_span
)

# Import unified tool registry for PM Skills merging
try:
    from agents.pm_agent.unified_tool_registry import UNIFIED_REGISTRY
    UNIFIED_REGISTRY_AVAILABLE = True
except ImportError:
    UNIFIED_REGISTRY_AVAILABLE = False

# Logger for this module
logger = logging.getLogger(__name__)

# Thread pool for blocking LLM API calls
_executor = ThreadPoolExecutor(max_workers=2)

# Centralized Langfuse client
_langfuse_client = get_langfuse_client()

# Cache for planner instructions
_PLANNER_INSTRUCTIONS_CACHE: Optional[str] = None

# Cache for copilot instructions (PM Agent specific)
_COPILOT_INSTRUCTIONS_CACHE: Optional[str] = None


async def run_in_executor_with_context(func, *args):
    """
    Run a blocking function in thread pool while preserving ContextVars.
    
    This is critical for maintaining Langfuse trace context across thread boundaries.
    ContextVars don't automatically propagate to threads, so we must copy them explicitly.
    
    Args:
        func: The blocking function to run
        *args: Arguments to pass to the function
        
    Returns:
        The result of the function
    """
    loop = asyncio.get_event_loop()
    ctx = contextvars.copy_context()
    return await loop.run_in_executor(_executor, lambda: ctx.run(func, *args))


def clear_planner_cache() -> None:
    """Clear the cached instructions to force reload on next call."""
    global _PLANNER_INSTRUCTIONS_CACHE, _COPILOT_INSTRUCTIONS_CACHE
    _PLANNER_INSTRUCTIONS_CACHE = None
    _COPILOT_INSTRUCTIONS_CACHE = None
    logger.info("[LLM_PLANNER] Instructions cache cleared")


def _sanitize_json_text(text: str) -> str:
    """
    Attempt to sanitize malformed JSON from LLM output.
    Handles common issues: markdown wrappers, trailing commas, unescaped quotes.
    """
    if not text:
        return text
    
    # Strip markdown code blocks if present
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    if text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()
    
    # Remove trailing commas before } or ]
    text = re.sub(r',\s*([}\]])', r'\1', text)
    
    # Try to extract JSON object if there's prose around it
    match = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}', text, re.DOTALL)
    if match:
        text = match.group(0)
    
    return text


def _load_copilot_instructions() -> str:
    """Load PM Agent instructions from markdown file (cached)."""
    global _COPILOT_INSTRUCTIONS_CACHE
    if _COPILOT_INSTRUCTIONS_CACHE is not None:
        return _COPILOT_INSTRUCTIONS_CACHE
    
    # Load instructions from project-specific file in the agent folder
    # Try multiple potential locations for flexibility
    possible_paths = [
        Path(__file__).resolve().parent.parent.parent / "agents" / "pm_agent" / "pm-agent-instructions.md",
        Path(__file__).resolve().parent.parent / "agents" / "pm_agent" / "pm-agent-instructions.md",
        Path(__file__).resolve().parent.parent.parent / "pm-agent-instructions.md",
    ]
    
    for instructions_path in possible_paths:
        if instructions_path.exists():
            try:
                _COPILOT_INSTRUCTIONS_CACHE = instructions_path.read_text(encoding="utf-8")
                logger.info("[LLM_PLANNER] Loaded PM Agent instructions from %s", instructions_path)
                return _COPILOT_INSTRUCTIONS_CACHE
            except Exception as e:
                logger.warning("[LLM_PLANNER] Could not load %s: %s", instructions_path, e)
    
    # Fallback minimal instructions
    _COPILOT_INSTRUCTIONS_CACHE = """You are a PM assistant. Return ONLY valid JSON. Never invent tools or arguments."""
    return _COPILOT_INSTRUCTIONS_CACHE


def _load_planner_instructions() -> str:
    """Load planner instructions from markdown file (cached)."""
    global _PLANNER_INSTRUCTIONS_CACHE
    if _PLANNER_INSTRUCTIONS_CACHE is not None:
        return _PLANNER_INSTRUCTIONS_CACHE
    
    # Load instructions from prompts folder - try multiple potential locations
    possible_paths = [
        Path(__file__).resolve().parent.parent.parent / "agents" / "pm_agent" / "prompts" / "planner-instructions.md",
        Path(__file__).resolve().parent.parent / "agents" / "pm_agent" / "prompts" / "planner-instructions.md",
        Path(__file__).resolve().parent / "prompts" / "planner-instructions.md",
    ]
    
    for instructions_path in possible_paths:
        if instructions_path.exists():
            try:
                _PLANNER_INSTRUCTIONS_CACHE = instructions_path.read_text(encoding="utf-8")
                logger.info("[LLM_PLANNER] Loaded planner instructions from %s", instructions_path)
                return _PLANNER_INSTRUCTIONS_CACHE
            except Exception as e:
                logger.warning("[LLM_PLANNER] Could not load planner instructions %s: %s", instructions_path, e)
    
    # Fallback: return empty string (will use inline instructions)
    logger.warning("[LLM_PLANNER] Planner instructions file not found, using inline fallback")
    _PLANNER_INSTRUCTIONS_CACHE = ""
    return _PLANNER_INSTRUCTIONS_CACHE


def _build_tool_summary(tool_registry: Dict[str, Dict[str, Any]]) -> str:
    """
    Build a COMPREHENSIVE tool summary from the registry metadata.
    
    CRITICAL FIX: Now includes BOTH required AND optional parameters with types
    so LLM has full visibility into what each tool can do. This enables the LLM
    to make intelligent decisions about which tool to use without needing post-hoc
    capability matching.
    
    Example output format:
    • search_workitem
      Required: searchText (str)
      Optional: state (list[str]), project (list[str]), workItemType (list[str]), assignedTo (list[str]), ...
      
    vs old format that only showed:
    • search_workitem → required: ['searchText']
    """
    # Group tools by category
    categorized: Dict[str, List[tuple]] = {}
    uncategorized = []
    
    for tool_name, meta in tool_registry.items():
        category = meta.get("category", "")
        priority = meta.get("priority", 5)
        required = meta.get("required_args", [])
        optional = meta.get("optional_args", {})
        desc = meta.get("description", "")
        
        tool_info = (tool_name, required, optional, desc, priority)
        
        if category:
            if category not in categorized:
                categorized[category] = []
            categorized[category].append(tool_info)
        else:
            uncategorized.append(tool_info)
    
    lines = ["## Available Tools (grouped by category)\n"]
    lines.append("⚠️ IMPORTANT: Each tool shows required AND optional parameters so you can pick the right tool for the query.")
    lines.append("For example, search_workitem supports 'state' filter while wit_get_work_items_for_iteration does not.\n")
    
    # Category display order with emojis - dynamically built from registry categories
    CATEGORY_EMOJI_MAP = {
        "work_items": "📋 Work Items",
        "search": "🔍 Search",
        "iteration": "🔄 Sprint/Iteration",
        "pull_requests": "🔀 Pull Requests",
        "repository": "📂 Repositories",
        "build": "🛠️ Build/Pipeline",
        "testplan": "🧪 Test",
        "test": "🧪 Test",
        "wiki": "📖 Wiki",
        "core": "⚙️ Core/Admin",
        "security": "🔒 Security",
        "pm_skill": "🎯 PM Skills",
    }
    
    # Build category_order dynamically from categories found in the registry
    found_categories = set()
    for tool_name, meta in tool_registry.items():
        cat = meta.get("category", "")
        if cat:
            found_categories.add(cat)
    
    # Order: known categories first (in preferred order), then any unknown ones
    preferred_order = ["work_items", "search", "iteration", "repository", "build", 
                       "testplan", "test", "wiki", "core", "security", "pm_skill"]
    category_order = []
    for cat in preferred_order:
        if cat in found_categories:
            label = CATEGORY_EMOJI_MAP.get(cat, cat.replace('_', ' ').title())
            category_order.append((cat, label))
    # Add any remaining categories not in preferred order
    for cat in sorted(found_categories):
        if cat not in preferred_order:
            label = CATEGORY_EMOJI_MAP.get(cat, cat.replace('_', ' ').title())
            category_order.append((cat, label))
    
    for category, category_desc in category_order:
        if category in categorized:
            tools = categorized.pop(category)
            # Sort by priority (higher first)
            tools.sort(key=lambda x: -x[4])
            
            lines.append(f"### {category_desc}")
            for tool_name, required, optional, desc, priority in tools:
                lines.append(f"  • **{tool_name}**")
                
                # Format required parameters
                if required:
                    req_str = ", ".join(required)
                    lines.append(f"    Required: {req_str}")
                
                # Format optional parameters with types
                if optional:
                    opt_items = []
                    for param_name, param_type in optional.items():
                        opt_items.append(f"{param_name} ({param_type})")
                    opt_str = ", ".join(opt_items)
                    lines.append(f"    Optional: {opt_str}")
                
                # Add description
                if desc:
                    short_desc = desc[:120] + "..." if len(desc) > 120 else desc
                    lines.append(f"    {short_desc}")
                lines.append("")
    
    # Any remaining categories
    for category, tools in categorized.items():
        if tools:
            lines.append(f"### {category.replace('_', ' ').title()}")
            for tool_name, required, optional, desc, priority in tools:
                lines.append(f"  • **{tool_name}**")
                if required:
                    req_str = ", ".join(required)
                    lines.append(f"    Required: {req_str}")
                if optional:
                    opt_items = []
                    for param_name, param_type in optional.items():
                        opt_items.append(f"{param_name} ({param_type})")
                    opt_str = ", ".join(opt_items)
                    lines.append(f"    Optional: {opt_str}")
                if desc:
                    short_desc = desc[:100] + "..." if len(desc) > 100 else desc
                    lines.append(f"    {short_desc}")
                lines.append("")
    
    # Uncategorized tools
    if uncategorized:
        lines.append("### Other Tools")
        for tool_name, required, optional, desc, priority in uncategorized:
            lines.append(f"  • **{tool_name}**")
            if required:
                req_str = ", ".join(required)
                lines.append(f"    Required: {req_str}")
            if optional:
                opt_items = []
                for param_name, param_type in optional.items():
                    opt_items.append(f"{param_name} ({param_type})")
                opt_str = ", ".join(opt_items)
                lines.append(f"    Optional: {opt_str}")
            if desc:
                short_desc = desc[:100] + "..." if len(desc) > 100 else desc
                lines.append(f"    {short_desc}")
            lines.append("")
    
    return "\n".join(lines)


def _detect_multi_step_patterns(query: str) -> Tuple[bool, List[str]]:
    """
    Detect if a query requires multi-step execution (execute_plan).
    
    Returns:
        Tuple of (should_use_execute_plan, reasons_list)
    
    Patterns that require multi-step:
    1. Comparison: "compare X with Y", "versus", "vs"
    2. Cross-domain: "X AND Y" where X and Y are different entity types
    3. Multi-source aggregation: data from multiple sprints/iterations
    4. Aggregation/Counting: "count by", "group by", "average"
    5. Multiple work item types: "bugs AND tasks", "bugs and user stories"
    6. Action chains: "find X and then Y", "analyze and send"
    7. Dependency lookup: "X and their dependencies"
    """
    q = query.lower()
    reasons = []
    
    # Pattern 1: Comparison
    comparison_patterns = [
        r'\bcompare\b.*\bwith\b', r'\bcompare\b.*\bto\b', r'\bversus\b', r'\bvs\b',
        r'\bdifference\b.*\bbetween\b', r'\bcompared\s+to\b', r'\bcomparison\b'
    ]
    for pattern in comparison_patterns:
        if re.search(pattern, q):
            reasons.append("comparison query detected")
            break
    
    # Pattern 2: Cross-domain (different entity types with AND)
    # Entities: bugs, tasks, stories, PRs, repos, builds, sprints
    cross_domain_pairs = [
        (r'\bbugs?\b', r'\brepo|repository|branch|build|pipeline|pr|pull\s+request\b'),
        (r'\bwork\s*items?\b', r'\brepo|repository|branch|build|pipeline|pr|pull\s+request\b'),
        (r'\bstories?\b', r'\brepo|repository|branch|build|pipeline\b'),
        (r'\btasks?\b', r'\brepo|repository|branch|build|pipeline\b'),
    ]
    for entity1_pattern, entity2_pattern in cross_domain_pairs:
        if re.search(entity1_pattern, q) and re.search(entity2_pattern, q):
            if 'and ' in q or ' AND ' in q:
                reasons.append("cross-domain entities with AND connector")
                break
    
    # Pattern 3: Multi-source aggregation (multiple sprints/iterations)
    multi_source_patterns = [
        r'last\s+\d+\s+sprints?', r'past\s+\d+\s+iterations?', r'last\s+\d+\s+iterations?',
        r'over\s+time', r'trend', r'across\s+sprints?', r'between.*sprints?',
        r'sprint\s+\d+.*sprint\s+\d+', r'compare.*sprint'
    ]
    for pattern in multi_source_patterns:
        if re.search(pattern, q):
            reasons.append("multi-source aggregation detected")
            break
    
    # Pattern 4: Aggregation/Counting requiring post-processing
    aggregation_patterns = [
        r'\bcount\s+by\b', r'\bgroup\s+by\b', r'\baverage\b', r'\bsum\s+of\b',
        r'\btotal\s+by\b', r'\bbreakdown\b', r'\bper\s+\w+\b', r'\bby\s+assignee\b',
        r'\bcalculate\b', r'\bcompute\b', r'\baggregate\b',
        # NEW: "by X and Y" grouping patterns (e.g., "bugs by area and assignee")
        r'\bby\s+\w+\s+and\s+\w+\b',  # "by area and assignee"
        r'\bby\s+area\b',  # "by area" alone suggests grouping
        r'\bby\s+state\b',  # "by state" 
        r'\bby\s+priority\b',  # "by priority"
        r'\bby\s+developer\b',  # "by developer"
        r'\bper\s+developer\b',  # "per developer"
        r'\bper\s+team\b',  # "per team"
    ]
    for pattern in aggregation_patterns:
        if re.search(pattern, q):
            reasons.append("aggregation/counting operation detected")
            break
    
    # Pattern 5: Multiple work item types with AND (explicit dual-type request)
    # "bugs and tasks", "bugs AND user stories", "my bugs and my tasks"
    work_item_types = [r'bugs?', r'tasks?', r'user\s*stories?', r'stories?', r'features?', r'issues?', r'work\s*items?']
    type_matches = []
    for wit in work_item_types:
        if re.search(rf'\b{wit}\b', q):
            type_matches.append(wit)
    if len(type_matches) >= 2:
        # Check if they're connected with AND
        if re.search(r'\b(and|&)\s+(my\s+)?(bugs?|tasks?|stories?|features?)', q):
            reasons.append("multiple work item types requested explicitly")
    
    # Pattern 6: Action chains (analysis + action)
    action_chain_patterns = [
        r'\band\s+(then\s+)?(send|create|update|notify|email|alert|generate)\b',
        r'\band\s+(then\s+)?(give|provide)\s+feedback\b',
        r'\banalyze\b.*\band\b.*\b(send|create|report|notify)\b',
        r'\bfind\b.*\band\b.*\b(update|modify|change)\b',
        r'\breview\b.*\band\b.*\b(notify|send|email)\b',
        # NEW: "review/analyze X and identify Y" pattern
        r'\b(review|analyze)\b.*\band\s+(identify|find|detect|highlight|flag|list)\b',
    ]
    for pattern in action_chain_patterns:
        if re.search(pattern, q):
            reasons.append("action chain (analysis + action) detected")
            break
    
    # Pattern 7: Dependency lookup
    dependency_patterns = [
        r'\band\s+their\s+depend', r'\bwith\s+depend', r'\bblocked\b.*\bdepend',
        r'\bparent\b.*\bchild', r'\blinked\s+items?\b', r'\brelat(ed|ions)\b'
    ]
    for pattern in dependency_patterns:
        if re.search(pattern, q):
            reasons.append("dependency/relationship lookup detected")
            break
    
    # Pattern 8: "All teams" / "all projects" queries (need to enumerate first)
    all_entities_patterns = [
        r'\ball\s+teams?\b', r'\ball\s+projects?\b', r'\bevery\s+team\b',
        r'\bfor\s+all\b.*\b(teams?|projects?)\b', r'\bacross\s+all\b'
    ]
    for pattern in all_entities_patterns:
        if re.search(pattern, q):
            reasons.append("all-entities enumeration required")
            break
    
    # Pattern 9: Report generation with scope keywords
    report_patterns = [
        r'\b(velocity|status|health|progress|summary)\s+report\b',
        r'\breport\b.*\b(velocity|status|health|progress)\b',
        r'\bgenerate\b.*\breport\b', r'\bcreate\b.*\breport\b'
    ]
    for pattern in report_patterns:
        if re.search(pattern, q):
            # Only trigger multi-step if also has scope like "all teams" or "comparison"
            if re.search(r'\b(all|every|multiple|compare|trend)\b', q):
                reasons.append("report generation with multi-scope")
                break
    
    should_use_multi_step = len(reasons) > 0
    return should_use_multi_step, reasons


def _select_relevant_tools(tool_registry: Dict[str, Dict[str, Any]], query: str, max_tools: int = 12) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """Select a small subset of tools relevant to the query to keep prompts small.

    Heuristic: prefer tools whose name, description, or arg_descriptions contain
    any token from the query. Falls back to first N tools if no match found.
    
    CRITICAL: Always include execute_wiql and search_workitem for work item queries.
    
    NEW: Merges PM Skills from unified_tool_registry for complete tool visibility.
    
    Returns:
        Tuple of (selected_tools_dict, priority_tools_list) where priority_tools_list
        contains the names of tools that are recommended for this query type.
    """
    if not query or not tool_registry:
        return tool_registry, []
    
    # Merge PM Skills from unified registry into the tool registry for this request
    merged_registry = dict(tool_registry)  # Make a copy
    if UNIFIED_REGISTRY_AVAILABLE:
        try:
            pm_skills = UNIFIED_REGISTRY.get_tools_by_category("pm_skill")
            for name, tool_meta in pm_skills.items():
                if name not in merged_registry:
                    merged_registry[name] = {
                        "description": tool_meta.get("description", ""),
                        "arg_descriptions": {arg: f"{arg} parameter" for arg in tool_meta.get("args", [])},
                        "category": "pm_skill"
                    }
            logger.debug(f"[LLM_PLANNER] Merged {len(pm_skills)} PM Skills into tool registry")
        except Exception as e:
            logger.debug(f"[LLM_PLANNER] Failed to merge PM Skills: {e}")

    # CRITICAL: Filter out tools marked as not available in live MCP
    # Tools with mcp_available=False have fallback adapters in ToolExecutor
    # but should not be directly selected by the planner to avoid confusion
    unavailable_tools = [name for name, meta in merged_registry.items() 
                         if meta.get('mcp_available') is False]
    if unavailable_tools:
        logger.debug(f"[LLM_PLANNER] Filtering out {len(unavailable_tools)} tools not in live MCP: {unavailable_tools}")
        merged_registry = {k: v for k, v in merged_registry.items() if v.get('mcp_available') is not False}

    q = query.lower()
    tokens = [t for t in re.findall(r"[a-zA-Z0-9_]+", q) if len(t) > 2]
    if not tokens:
        # no useful tokens, return a small default slice
        return {k: merged_registry[k] for k in list(merged_registry.keys())[:max_tools]}

    # Initialize priority_tools list
    priority_tools = []
    
    # FIRST: Check if there's a skill_hint from semantic matching (highest priority)
    # This comes from router when semantic matching identifies a skill
    try:
        from agents.pm_agent.tool_registry import SKILL_TO_TOOLS_MAP, get_priority_tools_for_query
        
        # Get priority tools based on skill mapping
        skill_priority_tools = get_priority_tools_for_query(query)
        if skill_priority_tools:
            priority_tools.extend(skill_priority_tools)
            logger.debug(f"[LLM_PLANNER] Added priority tools from SKILL_TO_TOOLS_MAP: {skill_priority_tools}")
    except ImportError:
        pass
    except Exception as e:
        logger.debug(f"[LLM_PLANNER] SKILL_TO_TOOLS_MAP lookup failed: {e}")
    
    # ══════════════════════════════════════════════════════════════════════════
    # DYNAMIC QUERY-TO-CATEGORY MAPPING
    # Instead of hardcoding tool names, we dynamically detect query categories
    # and select tools from the registry by category.
    # ══════════════════════════════════════════════════════════════════════════
    
    # Define query-to-category patterns dynamically
    # Each entry maps a set of query keywords to one or more registry categories
    QUERY_CATEGORY_PATTERNS = [
        {
            "name": "work_item",
            "keywords": {'bug', 'bugs', 'story', 'stories', 'task', 'tasks', 'item', 'items',
                         'work', 'workitem', 'workitems', 'user', 'feature', 'epic', 'area',
                         'recurring', 'active', 'closed', 'resolved', 'path', 'assigned', 'created',
                         'derailing', 'risk', 'blocked', 'overdue', 'delayed'},
            "categories": ["work_items", "search"],
        },
        {
            "name": "sprint",
            "keywords": {'sprint', 'iteration', 'derailing', 'at risk', 'sprint status',
                         'sprint progress', 'sprint risk', 'current sprint'},
            "categories": ["iteration", "work_items", "search"],
        },
        {
            "name": "capacity",
            "keywords": {'capacity', 'bandwidth', 'workload', 'overloaded', 'underutilized',
                         'available hours', 'utilization', 'who has capacity'},
            "categories": ["iteration"],
        },
        {
            "name": "pr",
            "keywords": {'pull request', 'pr ', 'prs', 'code review', 'merge request',
                         'reviewer', 'approval', 'needs review', 'review required', 'merged pr'},
            "categories": ["repository"],
        },
        {
            "name": "build",
            "keywords": {'build', 'builds', 'pipeline', 'pipelines', 'ci', 'cd',
                         'deployment', 'release', 'build failed', 'build status',
                         'build log', 'pipeline run', 'failed build'},
            "categories": ["build"],
        },
        {
            "name": "repo",
            "keywords": {'repository', 'repositories', 'repo ', 'repos', 'git',
                         'branch', 'branches', 'commit', 'commits'},
            "categories": ["repository"],
            "exclude_keywords": {'pull request'},
        },
        {
            "name": "wiki",
            "keywords": {'wiki', 'documentation', 'docs'},
            "categories": ["wiki"],
        },
        {
            "name": "test",
            "keywords": {'test plan', 'test suite', 'test case', 'testing', 'qa', 'quality'},
            "categories": ["testplan"],
        },
        {
            "name": "security",
            "keywords": {'security', 'alerts', 'vulnerabilities', 'advsec', 'vulnerability',
                         'cve', 'sast', 'dast'},
            "categories": ["security"],
        },
        {
            "name": "time_based",
            "keywords": {'last week', 'last month', 'yesterday', 'today', 'past 7 days',
                         'past 30 days', 'since', 'between', 'this week', 'this month',
                         'created last', 'changed last', 'updated last'},
            "categories": ["work_items", "search"],
        },
        {
            "name": "aggregate",
            "keywords": {'how many', 'count', 'number of', 'total', 'statistics',
                         'stats', 'metrics', 'by area', 'per area', 'by assignee',
                         'per person', 'summary', 'overview'},
            "categories": ["search", "work_items"],
        },
        {
            "name": "knowledge",
            "keywords": {'who knows', 'who can work on', 'who has experience', 'find developer',
                         'developer skill', 'team expertise', 'skill matrix'},
            "categories": ["pm_skill"],
        },
        {
            "name": "backlog",
            "keywords": {'backlog', 'assign', 'distribute', 'balance workload', 'allocate'},
            "categories": ["work_items"],
        },
        {
            "name": "pm_skill",
            "keywords": {'overlooked', 'forgotten', 'stale', 'neglected', 'iteration report',
                         'sprint report', 'recurring bug', 'bug pattern', 'bug highlight',
                         'feedback to dev', 'capacity forecast', 'backlog health'},
            "categories": ["pm_skill"],
        },
    ]
    
    # Detect which categories match the query
    detected_categories = set()
    for pattern in QUERY_CATEGORY_PATTERNS:
        exclude = pattern.get("exclude_keywords", set())
        if exclude and any(ek in q for ek in exclude):
            continue
        if any(kw in q for kw in pattern["keywords"]):
            detected_categories.update(pattern["categories"])
            logger.debug(f"[LLM_PLANNER] Query matches pattern '{pattern['name']}' → categories: {pattern['categories']}")
    
    # Collect priority tools from detected categories dynamically from registry
    if detected_categories:
        for tool_name, tool_meta in merged_registry.items():
            tool_category = tool_meta.get("category", "other")
            if tool_category in detected_categories:
                priority_tools.append(tool_name)
    
    # Try to match against skill registry for better tool selection
    try:
        from utilities.skill_registry import match_skill_by_query
        matched_skill = match_skill_by_query(query, threshold=0.5)
        if matched_skill:
            skill_id = matched_skill.get("id")
            if skill_id:
                priority_tools.append(skill_id)
                logger.debug(f"[LLM_PLANNER] Matched query to skill: {skill_id}")
                
                # Also get tools from SKILL_TO_TOOLS_MAP for this skill
                try:
                    from agents.pm_agent.tool_registry import SKILL_TO_TOOLS_MAP
                    if skill_id in SKILL_TO_TOOLS_MAP:
                        mapping = SKILL_TO_TOOLS_MAP[skill_id]
                        priority_tools.extend(mapping.get("primary_tools", []))
                        priority_tools.extend(mapping.get("supporting_tools", []))
                except ImportError:
                    pass
    except ImportError:
        pass
    except Exception as e:
        logger.debug(f"[LLM_PLANNER] Skill registry match failed: {e}")
    
    # Remove duplicates while preserving order
    seen = set()
    priority_tools = [x for x in priority_tools if not (x in seen or seen.add(x))]
    
    # CRITICAL: Filter priority_tools to exclude tools not in live MCP
    # Only keep tools that are in merged_registry (which already excludes mcp_available=False)
    priority_tools = [t for t in priority_tools if t in merged_registry]

    scores = []
    for name, meta in merged_registry.items():
        score = 0
        # Boost priority tools
        if name in priority_tools:
            score += 10
        
        hay = " ".join(filter(None, [name.lower(), meta.get("description", "").lower()]))
        arg_descs = " ".join([str(v).lower() for v in meta.get("arg_descriptions", {}).values()])
        hay += " " + arg_descs
        for tok in tokens:
            if tok in hay:
                score += 1
        if score > 0:
            scores.append((score, name))

    if not scores:
        # If no matches but we detected categories, return tools from those categories
        if detected_categories:
            category_tools = [name for name, meta in merged_registry.items()
                             if meta.get("category") in detected_categories]
            if category_tools:
                return {name: merged_registry[name] for name in category_tools[:max_tools]}, priority_tools
        # Fallback: return top tools by priority
        return {k: merged_registry[k] for k in list(merged_registry.keys())[:max_tools]}, priority_tools

    scores.sort(reverse=True)
    selected = [name for _, name in scores[:max_tools]]

    # ══════════════════════════════════════════════════════════════════════════
    # ESSENTIAL TOOL GUARANTEE
    # Certain data-fetch tools MUST be present when their query category is
    # detected.  Without them the LLM cannot plan correctly even though the
    # static instructions reference them extensively.
    # ══════════════════════════════════════════════════════════════════════════
    _ESSENTIAL_TOOLS_BY_PATTERN = {
        "sprint": ["wit_get_work_items_for_iteration", "search_workitem"],
        "work_item": ["search_workitem", "wit_get_work_item", "execute_wiql"],
        "time_based": ["execute_wiql", "search_workitem"],
        "aggregate": ["execute_wiql", "search_workitem"],
    }
    for pattern in QUERY_CATEGORY_PATTERNS:
        p_name = pattern["name"]
        if p_name not in _ESSENTIAL_TOOLS_BY_PATTERN:
            continue
        # Check if this pattern was triggered
        exclude = pattern.get("exclude_keywords", set())
        if exclude and any(ek in q for ek in exclude):
            continue
        if any(kw in q for kw in pattern["keywords"]):
            for essential in _ESSENTIAL_TOOLS_BY_PATTERN[p_name]:
                if essential in merged_registry and essential not in selected:
                    selected.append(essential)
                    logger.debug(f"[LLM_PLANNER] Essential tool '{essential}' injected for pattern '{p_name}'")

    return {name: merged_registry[name] for name in selected}, priority_tools


def _sanitize_wiql(wiql: str, max_length: int = 10000) -> tuple:
    """
    Sanitize a WIQL query to prevent injection attacks and enforce constraints.
    
    Args:
        wiql: Raw WIQL query string from LLM
        max_length: Maximum allowed query length (default 10000 from inputSchema)
    
    Returns:
        (is_valid: bool, sanitized_wiql: str, error_msg: Optional[str])
    """
    if not wiql or not isinstance(wiql, str):
        return (False, "", "WIQL query must be a non-empty string")
    
    wiql = wiql.strip()
    
    # Check max length
    if len(wiql) > max_length:
        return (False, wiql[:max_length], f"WIQL query exceeds maximum length of {max_length} characters")
    
    # DO NOT escape single quotes — WIQL uses single quotes as part of its syntax.
    # e.g., WHERE [System.State] = 'Closed' is VALID WIQL.
    # Double-escaping ('') would break the query at the ADO REST API.
    sanitized = wiql
    
    # Check for obviously malicious patterns (lowercase for case-insensitive check)
    wiql_lower = wiql.lower()
    dangerous_patterns = [
        "drop table",
        "delete from",
        "truncate table",
        "exec(",
        "execute(",
        "xp_cmdshell",
        "--",  # SQL comment marker
        "/*",  # SQL block comment start
        "*/",  # SQL block comment end
        ";--",  # Common injection pattern
    ]
    
    for pattern in dangerous_patterns:
        if pattern in wiql_lower:
            return (False, "", f"WIQL query contains potentially dangerous pattern: '{pattern}'")
    
    # WIQL should start with SELECT (basic sanity check)
    if not wiql_lower.startswith("select"):
        return (False, sanitized, "WIQL query must start with 'SELECT'")
    
    return (True, sanitized, None)


def _validate_plan(plan: Dict[str, Any], tool_registry: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """
    Validate the LLM plan against the tool registry.
    Applies WIQL sanitization for execute_wiql.
    Returns ask_clarification if validation fails.
    """
    action = plan.get("action", "")
    
    # Auto-correct: if the LLM put a tool name as the action (e.g. "execute_wiql"
    # instead of "call_tool"), promote it to call_tool with that tool.
    if action not in ("call_tool", "ask_clarification", "no_tool", "execute_plan"):
        if action in tool_registry:
            logger.info(f"[_validate_plan] Auto-corrected action '{action}' → call_tool with tool='{action}'")
            plan["tool"] = action
            plan["action"] = "call_tool"
            action = "call_tool"
        else:
            return {"action": "ask_clarification", "message": f"Invalid action: {action}. Please rephrase your request."}
    
    if action != "call_tool":
        return plan
    
    tool = plan.get("tool")
    args = plan.get("args", {}) or {}
    
    # Validate tool exists in registry
    if tool not in tool_registry:
        return {"action": "ask_clarification", "message": f"Unknown tool: {tool}. Please rephrase your request."}
    
    # Apply WIQL sanitization if this is a WIQL query tool
    if tool == "execute_wiql" and "wiql" in args:
        is_valid, sanitized_wiql, error_msg = _sanitize_wiql(args["wiql"])
        if not is_valid:
            return {
                "action": "ask_clarification",
                "message": f"Invalid WIQL query: {error_msg}. Please provide a valid SELECT query."
            }
        # Update args with sanitized WIQL
        args["wiql"] = sanitized_wiql
        plan["args"] = args
    
    # Validate required args are present
    meta = tool_registry[tool]
    required_args = meta.get("required_args", [])
    missing = [arg for arg in required_args if arg not in args or args[arg] is None]
    
    if missing:
        arg_descs = meta.get("arg_descriptions", {})
        hints = [f"{arg}: {arg_descs.get(arg, 'required')}" for arg in missing]
        return {"action": "ask_clarification", "message": f"Missing required arguments: {', '.join(hints)}"}
    
    return plan


def _autocorrect_plan_for_iteration(query: str, plan: Dict[str, Any], tool_registry: Dict[str, Dict[str, Any]], context: Dict[str, Any]) -> Dict[str, Any]:
    """
    If the user query mentions a sprint/iteration but the LLM plan chose a non-iteration tool,
    attempt to rewrite the plan to use an iteration-aware tool discovered from the registry.

    This function avoids hardcoding tool names by discovering tools that accept an
    `iterationId` argument (required or optional) in the registry metadata.
    
    IMPORTANT: Do NOT autocorrect if the LLM chose a valid tool like search_workitem
    or execute_wiql that can handle the query with filters. Only autocorrect
    if the chosen tool is completely irrelevant to work item queries.

    Returns the original plan if no safe autocorrect can be made.
    """
    try:
        if not plan or plan.get("action") != "call_tool":
            return plan

        q = (query or "").lower()
        if "sprint" not in q and "iteration" not in q:
            return plan

        chosen_tool = plan.get("tool")
        if not chosen_tool:
            return plan

        # If chosen tool already supports iterationId, nothing to do
        meta = tool_registry.get(chosen_tool, {})
        req_args = meta.get("required_args", []) or []
        opt_args = list(meta.get("optional_args", {}).keys()) if meta.get("optional_args") else []
        if "iterationId" in req_args or "iterationId" in opt_args:
            return plan
        
        # CRITICAL: Do NOT autocorrect valid work item query tools
        # These tools can handle iteration filtering through other means (WIQL, filters)
        valid_work_item_tools = {
            "search_workitem",        # Can filter by iteration via WIQL-like queries
            "execute_wiql",           # WIQL can include iteration path filter
            "wit_get_work_items",     # Direct work item fetch (may be needed for specific IDs)
            "wit_get_work_item",      # Single work item fetch
            "wit_my_work_items",      # User's work items
        }
        
        if chosen_tool in valid_work_item_tools:
            # LLM chose a valid tool for work item queries - respect its decision
            logger.debug(f"[LLM_PLANNER] Not autocorrecting {chosen_tool} - valid work item tool")
            return plan

        # Find candidate tools from registry that accept iterationId
        candidates = []
        for name, m in tool_registry.items():
            r = m.get("required_args", []) or []
            o = list(m.get("optional_args", {}).keys()) if m.get("optional_args") else []
            if "iterationId" in r or "iterationId" in o:
                candidates.append((name, m))

        if not candidates:
            return plan

        # Extract explicit iteration identifier from the query (e.g., 'sprint 46')
        iter_id = None
        m = re.search(r"\b(?:sprint|iteration)\s+#?(\d{1,6})\b", query, re.IGNORECASE)
        if m:
            iter_id = m.group(1)
        else:
            # Handle relative references like 'current', 'this', 'previous', 'last'
            if re.search(r"\b(current|this)\s+(sprint|iteration)\b", q):
                iter_id = "@CurrentIteration"
            elif re.search(r"\b(previous|last|previous\s+sprint|last\s+sprint|last\s+iteration)\b", q):
                iter_id = "@PreviousIteration"

        if not iter_id:
            # No clear iteration; do not autocorrect to avoid unsafe assumptions
            return plan

        # Build corrected plan using the first candidate tool (registry order is stable)
        candidate_tool = candidates[0][0]
        new_args = dict(plan.get("args", {}) or {})

        # Ensure required fields for the candidate are populated from context when missing
        cand_meta = tool_registry.get(candidate_tool, {})
        for req in cand_meta.get("required_args", []) or []:
            if req == "iterationId":
                new_args["iterationId"] = iter_id
            elif req not in new_args:
                # Try to fill from provided context
                if context and req in context and context.get(req) is not None:
                    new_args[req] = context.get(req)

        # Also add project/team defaults if present in context and not already set
        if context:
            if "project" not in new_args and context.get("project"):
                new_args["project"] = context.get("project")
            if "team" not in new_args and context.get("team"):
                new_args["team"] = context.get("team")

        # Normalize project arg types: some tools expect a single string project,
        # while search_workitem and others use a list. If candidate expects a
        # single project string, convert list -> first element to avoid MCP type errors.
        try:
            proj_val = new_args.get("project")
            if isinstance(proj_val, (list, tuple)) and proj_val:
                # Inspect candidate metadata for project type hints
                proj_desc = cand_meta.get("arg_descriptions", {}).get("project", "").lower()
                opt_args_meta = cand_meta.get("optional_args", {}) or {}
                opt_proj_type = str(opt_args_meta.get("project", "")).lower()

                # If descriptions/types don't indicate a list, convert to single string
                if "list" not in proj_desc and "list" not in opt_proj_type:
                    new_args["project"] = proj_val[0]
        except Exception:
            pass

        corrected = dict(plan)
        corrected["tool"] = candidate_tool
        corrected["args"] = new_args
        # Lower confidence slightly because we're transforming the plan
        try:
            orig_conf = float(plan.get("confidence", 0.8))
        except Exception:
            orig_conf = 0.8
        corrected["confidence"] = max(0.5, min(orig_conf * 0.95, 0.99))
        rs = corrected.get("reasoning_summary", "")
        corrected["reasoning_summary"] = (rs + " | Autocorrected to iteration-scoped tool based on query mentioning sprint/iteration.") if rs else "Autocorrected to iteration-scoped tool based on query mentioning sprint/iteration."

        logger.debug("[LLM_PLANNER] Autocorrected plan to use iteration tool %s with iterationId=%s", candidate_tool, iter_id)
        return corrected
    except Exception as e:
        logger.debug(f"[LLM_PLANNER] Autocorrect failed: {e}")
        return plan


async def plan_mcp_call(query: str, context: Dict[str, Any], tool_registry: Dict[str, Dict[str, Any]], 
                        session_id: str = None) -> Optional[Dict[str, Any]]:
    """
    Use OpenAI LLM (gpt-4o-mini) to plan an MCP call based on user query and tool registry.

    Creates a span within the current trace for unified observability.

    This is the main heavy planner function that can be used by:
    - PM Agent (primary user)
    - Orchestrator (optional, for pre-planning)
    - Future agents requiring tool selection

    Args:
        query: User's natural language question.
        context: Dict with keys like 'project', 'team', 'iteration', 'area_paths', etc.
        tool_registry: MCP_TOOL_REGISTRY dict from utilities.mcp.tool_registry.
        session_id: Optional session ID for Langfuse trace tracking.

    Returns:
        Dict: {"action": str, "tool": str|null, "args": {...}, "confidence": float}
        or None if planning fails.
    """
    if not query:
        return None

    # Defensive: ensure query is always a string (caller may pass A2A dict)
    if isinstance(query, dict):
        # Extract text from A2A format: {'id':..., 'params':{'message':{'parts':[{'type':'text','text':'...'}]}}}
        _q = None
        try:
            parts = query.get('params', {}).get('message', {}).get('parts', [])
            for part in parts:
                if isinstance(part, dict) and part.get('type') == 'text' and part.get('text'):
                    _q = part['text']
                    break
        except (AttributeError, TypeError):
            pass
        if not _q and 'parts' in query and isinstance(query.get('parts'), list):
            for part in query['parts']:
                if isinstance(part, dict) and part.get('type') == 'text' and part.get('text'):
                    _q = part['text']
                    break
        if not _q:
            msg = query.get('message')
            _q = (msg if isinstance(msg, str) else None) or query.get('text') or str(query)
        query = _q
        logger.debug(f"[LLM_PLANNER] Converted dict query to text: {query[:200]}")

    # Step 0: Normalize the query to handle casual/ambiguous queries
    original_query = query
    try:
        from utilities.query_normalizer import enhance_query_for_llm, get_query_understanding_prompt
        query, context = enhance_query_for_llm(query, context)
        if query != original_query:
            logger.info(f"[LLM_PLANNER] Query normalized: '{original_query}' → '{query}'")
    except Exception as e:
        logger.debug(f"[LLM_PLANNER] Query normalization failed (using original): {e}")
        query = original_query

    if not OPENAI_AVAILABLE:
        logger.warning("[WARN] openai not available; using enhanced deterministic planner fallback")
        # Build a deterministic plan based on comprehensive heuristics
        # This fallback is generic and not tied to any particular query
        try:
            # Import config for default project
            from config import config
            
            # Get project from context, falling back to config default
            default_project = context.get('project') or config.pm_default_project or 'FracPro-OPS'
            
            # Try light-weight entity extraction if available
            extracted = None
            try:
                from utilities.entity_resolver import extract_entities
                extracted = extract_entities(query)
                logger.debug(f"[DETERMINISTIC_PLANNER] Entity extraction: persons={getattr(extracted, 'persons', [])}, types={getattr(extracted, 'work_item_types', [])}")
            except Exception as e:
                logger.debug(f"[DETERMINISTIC_PLANNER] Entity extraction unavailable: {e}")

            q_lower = (query or "").lower()
            
            # ══════════════════════════════════════════════════════════════════
            # PATTERN-BASED TOOL SELECTION (Priority Order)
            # Each pattern is generic and applies to any matching query
            # ══════════════════════════════════════════════════════════════════
            
            # Pattern 1: Specific work item ID lookup
            id_match = re.search(r'(?:^|[^\d])(\d{4,6})(?:[^\d]|$)', query)
            if id_match:
                wid = int(id_match.group(1))
                logger.info(f"[DETERMINISTIC_PLANNER] Detected work item ID {wid}, using wit_get_work_item")
                return {"action": "call_tool", "tool": "wit_get_work_item", "args": {"id": wid, "project": default_project}, "confidence": 0.95}
            
            # Pattern 2: List projects (no project-specific context needed)
            # CRITICAL: Exclude team and iteration queries that contain "in project" or "for project"
            if re.search(r'\b(list|show|get|all)\b.*\b(projects?|organizations?)\b', q_lower) and not re.search(r'\b(teams?|iterations?|sprints?)\b', q_lower):
                logger.info("[DETERMINISTIC_PLANNER] Detected project list query, using core_list_projects")
                return {"action": "call_tool", "tool": "core_list_projects", "args": {}, "confidence": 0.90}
            
            # Pattern 3: List teams (but NOT if asking about capacity/iterations)
            # CRITICAL: Exclude capacity queries which need iteration tools, not team listing
            if re.search(r'\b(list|show|get|all)\b.*\bteams?\b', q_lower) and not re.search(r'\b(capacity|capacities|iterations?|sprints?|workload|utilization)\b', q_lower):
                logger.info("[DETERMINISTIC_PLANNER] Detected team list query, using core_list_project_teams")
                return {"action": "call_tool", "tool": "core_list_project_teams", "args": {"project": default_project}, "confidence": 0.85}
            
            # Pattern 4a: Branch operations (higher priority - check before repo)
            # Must check branches BEFORE repos to avoid repo pattern matching first
            if re.search(r'\b(branches?|branching|branch\s*strategy)\b', q_lower):
                logger.info("[DETERMINISTIC_PLANNER] Detected branch query, using repo_list_branches_by_repo")
                # Try to extract repository name from context or query
                repo_name = context.get('repository') or default_project
                return {"action": "call_tool", "tool": "repo_list_branches_by_repo", "args": {"project": default_project, "repositoryId": repo_name}, "confidence": 0.80}
            
            # Pattern 4b: Repository operations (only if no branch or security keywords)
            # CRITICAL: Exclude security queries that mention "repository" - those should go to advsec_get_alerts
            if re.search(r'\b(repos?|repositories|repository)\b', q_lower) and not re.search(r'\b(branches?|branch|security|vulnerabilit|alert|cve)\b', q_lower):
                logger.info("[DETERMINISTIC_PLANNER] Detected repository query, using repo_list_repos_by_project")
                return {"action": "call_tool", "tool": "repo_list_repos_by_project", "args": {"project": default_project}, "confidence": 0.85}
            
            # Pattern 5: Pull request operations
            if re.search(r'\b(pull\s*requests?|prs?|merge\s*requests?)\b', q_lower):
                logger.info("[DETERMINISTIC_PLANNER] Detected PR query, using repo_list_pull_requests_by_repo_or_project")
                return {"action": "call_tool", "tool": "repo_list_pull_requests_by_repo_or_project", "args": {"project": default_project}, "confidence": 0.80}
            
            # Pattern 6: Build/Pipeline operations
            if re.search(r'\b(builds?|pipelines?|ci|cd|deployments?)\b', q_lower):
                if re.search(r'\b(logs?|output)\b', q_lower):
                    logger.info("[DETERMINISTIC_PLANNER] Detected build log query, using pipelines_list_builds first")
                    return {"action": "call_tool", "tool": "pipelines_list_builds", "args": {"project": default_project}, "confidence": 0.75, "chain_hint": "pipelines_get_build_logs"}
                if re.search(r'\b(definitions?|configured?)\b', q_lower):
                    logger.info("[DETERMINISTIC_PLANNER] Detected build definition query")
                    return {"action": "call_tool", "tool": "pipelines_list_build_definitions", "args": {"project": default_project}, "confidence": 0.80}
                if re.search(r'\b(history|runs?|run\s*history|executions?)\b', q_lower):
                    logger.info("[DETERMINISTIC_PLANNER] Detected pipeline history query")
                    return {"action": "call_tool", "tool": "pipelines_list_builds", "args": {"project": default_project}, "confidence": 0.80}
                if re.search(r'\b(failed|failures?|broken|red)\b', q_lower):
                    logger.info("[DETERMINISTIC_PLANNER] Detected failed builds query")
                    return {"action": "call_tool", "tool": "pipelines_list_builds", "args": {"project": default_project}, "confidence": 0.80, "chain_hint": "pipelines_get_build_changes"}
                if re.search(r'\b(changes?|triggered|commits?)\b', q_lower):
                    logger.info("[DETERMINISTIC_PLANNER] Detected build changes query")
                    return {"action": "call_tool", "tool": "pipelines_list_builds", "args": {"project": default_project}, "confidence": 0.75, "chain_hint": "pipelines_get_build_changes"}
                logger.info("[DETERMINISTIC_PLANNER] Detected build query, using pipelines_list_builds")
                return {"action": "call_tool", "tool": "pipelines_list_builds", "args": {"project": default_project}, "confidence": 0.80}
            
            # Pattern 7: Wiki operations
            if re.search(r'\b(wikis?|documentation|docs?)\b', q_lower):
                if re.search(r'\b(pages?|list|all)\b', q_lower):
                    logger.info("[DETERMINISTIC_PLANNER] Detected wiki pages query, using wiki_list_pages")
                    return {"action": "call_tool", "tool": "wiki_list_pages", "args": {"project": default_project}, "confidence": 0.80, "chain_hint": "wiki_get_page_content"}
                if re.search(r'\b(content|read|show|get)\b', q_lower):
                    logger.info("[DETERMINISTIC_PLANNER] Detected wiki content query, using wiki_list_wikis first")
                    return {"action": "call_tool", "tool": "wiki_list_wikis", "args": {"project": default_project}, "confidence": 0.75, "chain_hint": "wiki_get_page_content"}
                logger.info("[DETERMINISTIC_PLANNER] Detected wiki query, using wiki_list_wikis")
                return {"action": "call_tool", "tool": "wiki_list_wikis", "args": {"project": default_project}, "confidence": 0.80}
            
            # Pattern 8: Test plan/suite/case operations (HIGHER PRIORITY than generic patterns)
            # Check test-related patterns BEFORE other patterns to avoid falling through to repo_list
            if re.search(r'\b(test\s*plans?|test\s*suites?|test\s*cases?|testing|qa|quality)\b', q_lower):
                if re.search(r'\b(suites?)\b', q_lower):
                    logger.info("[DETERMINISTIC_PLANNER] Detected test suite query, using testplan_list_test_plans first")
                    return {"action": "call_tool", "tool": "testplan_list_test_plans", "args": {"project": default_project}, "confidence": 0.80}
                if re.search(r'\b(cases?)\b', q_lower):
                    logger.info("[DETERMINISTIC_PLANNER] Detected test case query, using testplan_list_test_plans first")
                    return {"action": "call_tool", "tool": "testplan_list_test_plans", "args": {"project": default_project}, "confidence": 0.80}
                if re.search(r'\b(results?|execution|run)\b', q_lower):
                    logger.info("[DETERMINISTIC_PLANNER] Detected test results query, using testplan_list_test_plans")
                    return {"action": "call_tool", "tool": "testplan_list_test_plans", "args": {"project": default_project}, "confidence": 0.80}
                logger.info("[DETERMINISTIC_PLANNER] Detected test plan query, using testplan_list_test_plans")
                return {"action": "call_tool", "tool": "testplan_list_test_plans", "args": {"project": default_project}, "confidence": 0.85}
            
            # Pattern 9: Security/Alerts operations
            # CRITICAL: advsec_get_alerts requires 'repository' and 'confidenceLevels'
            if re.search(r'\b(security|alerts?|vulnerabilities?|advsec|vulnerability|cve|sast|dast)\b', q_lower):
                logger.info("[DETERMINISTIC_PLANNER] Detected security query, using advsec_get_alerts")
                # Use project name as default repository if not specified
                repo_name = context.get('repository') or default_project
                # Default confidenceLevels to High and Critical for security queries
                return {
                    "action": "call_tool", 
                    "tool": "advsec_get_alerts", 
                    "args": {
                        "project": default_project, 
                        "repository": repo_name,
                        "confidenceLevels": ["High", "Critical"]  # Required arg with sensible default
                    }, 
                    "confidence": 0.80
                }
            
            # Pattern 10: Iteration/Sprint operations
            # CRITICAL: work_get_team_capacity requires 'iterationId' - use @CurrentIteration macro
            if re.search(r'\b(iterations?|sprints?)\b', q_lower):
                team = context.get('team') or 'XOPS 25'
                if re.search(r'\b(capacity|capacities|workload|utilization)\b', q_lower):
                    logger.info("[DETERMINISTIC_PLANNER] Detected capacity query - routing to work_list_team_iterations first (required for capacity)")
                    # NOTE: work_get_team_capacity requires iterationId which we don't have yet
                    # Route to iterations list first - let the agent chain tools
                    return {
                        "action": "call_tool", 
                        "tool": "work_list_team_iterations", 
                        "args": {"project": default_project, "team": team}, 
                        "confidence": 0.80,
                        "chain_hint": "work_get_team_capacity"  # Hint for multi-tool chaining
                    }
                # Check if asking for ALL/project iterations (not team-specific)
                if re.search(r'\b(all|project|list)\s+iterations?\b', q_lower) or re.search(r'\biterations?\s+(and|with)\s+(their|start|end|dates?)\b', q_lower):
                    logger.info("[DETERMINISTIC_PLANNER] Detected project iterations query, using work_list_iterations")
                    return {"action": "call_tool", "tool": "work_list_iterations", "args": {"project": default_project}, "confidence": 0.85}
                logger.info("[DETERMINISTIC_PLANNER] Detected iteration query, using work_list_team_iterations")
                return {"action": "call_tool", "tool": "work_list_team_iterations", "args": {"project": default_project, "team": team}, "confidence": 0.85}
            
            # Pattern 10b: Work item CREATION operations (NOT search) - CHECK BEFORE WIQL
            # CRITICAL: Must detect "create" intent before falling through to WIQL (which may match "link")
            # NOTE: Query normalizer may pluralize (bug→bugs), so use optional 's' suffix
            if re.search(r'\b(create|add|new|make)\s+(a\s+)?(new\s+)?(bugs?|tasks?|user\s*stor(y|ies)|stories?|features?|issues?|work\s*items?)\b', q_lower):
                # Determine the type to create
                if 'bug' in q_lower:
                    wi_type = 'Bug'
                elif 'task' in q_lower:
                    wi_type = 'Task'
                elif 'stor' in q_lower:  # story, stories, user story
                    wi_type = 'User Story'
                elif 'feature' in q_lower:
                    wi_type = 'Feature'
                else:
                    wi_type = 'Task'  # Default
                logger.info(f"[DETERMINISTIC_PLANNER] Detected work item creation request, type={wi_type}")
                return {
                    "action": "call_tool", 
                    "tool": "wit_create_work_item", 
                    "args": {"project": default_project, "type": wi_type}, 
                    "confidence": 0.90
                }
            
            # Pattern 11: Commits/Code search
            if re.search(r'\b(commits?|code|source)\b', q_lower):
                if re.search(r'\b(search|find|containing)\b', q_lower):
                    logger.info("[DETERMINISTIC_PLANNER] Detected code search query, using search_code")
                    return {"action": "call_tool", "tool": "search_code", "args": {"project": default_project, "searchText": query}, "confidence": 0.75}
                logger.info("[DETERMINISTIC_PLANNER] Detected commits query, using repo_search_commits")
                return {"action": "call_tool", "tool": "repo_search_commits", "args": {"project": default_project, "repository": default_project}, "confidence": 0.75}
            
            # Pattern 12: WIQL queries (time-based, complex filters, linked items, modifications)
            # CRITICAL: Route to execute_wiql for complex queries that search_workitem cannot handle
            wiql_indicators = [
                r'\b(created|modified|changed)\s*(in|during|within|today|yesterday|last)\b',  # Time-based
                r'\b(child|parent|linked|blocking|blocked)\s*(tasks?|items?|work\s*items?|stories?|bugs?)\b',  # Hierarchical
                r'\b(no\s*assignee|unassigned)\b',  # Unassigned filter
                r'\b(area\s*path|iteration\s*path)\b',  # Path-based
                r'\b(change\s*history|history|updates?)\b',  # History queries
                r'\bwhere\b',  # Explicit WIQL
                r'\b(closed|resolved)\s*(in|during|within|last|past|since|on|before|after)\b',  # Closed/resolved time-based
                r'\b(last|past)\s+\d+\s+(days?|weeks?|months?)\b',  # "last N days/weeks/months"
                r'\b(priority|p[1-4]|high\s*priority|critical\s*priority)\b',  # Priority-based
                r'\b(title|description)\s*(contains?|with|like|matching)\b',  # Field-specific search
                r'\b(on|before|after)\s+\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)',  # Absolute dates: "on 4 feb"
                r'\b(on|before|after)\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{1,2}',  # "on feb 4"
                r'\b(in|during)\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)',  # "in february"
                r'\b\d{1,2}\s+(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\s+\d{2,4}\b',  # "4 feb 2026"
            ]
            if any(re.search(pattern, q_lower) for pattern in wiql_indicators):
                logger.info("[DETERMINISTIC_PLANNER] Detected complex query requiring WIQL")
                return {"action": "call_tool", "tool": "execute_wiql", "args": {"project": default_project}, "confidence": 0.85}
            
            # Pattern 13: Backlog operations
            if re.search(r'\b(backlogs?)\b', q_lower):
                logger.info("[DETERMINISTIC_PLANNER] Detected backlog query, using wit_list_backlogs")
                team = context.get('team') or default_project
                return {"action": "call_tool", "tool": "wit_list_backlogs", "args": {"project": default_project, "team": team}, "confidence": 0.80}
            
            # Pattern 14: Query/WIQL operations (explicit WIQL requests)
            if re.search(r'\b(queries|wiql|shared\s*query)\b', q_lower):
                logger.info("[DETERMINISTIC_PLANNER] Detected query operation, using wit_get_query")
                return {"action": "call_tool", "tool": "wit_get_query", "args": {"project": default_project}, "confidence": 0.75}
            
            # ══════════════════════════════════════════════════════════════════
            # DEFAULT: Work item search (most common use case)
            # Build proper args with project as STRING not array
            # ══════════════════════════════════════════════════════════════════
            
            # Determine work item types from query
            work_item_types = []
            if 'bug' in q_lower:
                work_item_types.append('Bug')
            if 'story' in q_lower or 'stories' in q_lower or 'user story' in q_lower:
                work_item_types.append('User Story')
            if 'task' in q_lower:
                work_item_types.append('Task')
            if 'feature' in q_lower:
                work_item_types.append('Feature')
            if 'issue' in q_lower:
                work_item_types.append('Issue')
            
            # Build search text based on work item types or query
            if work_item_types:
                search_text = ' OR '.join(work_item_types)
            else:
                # Extract meaningful keywords from query (not the full query)
                search_text = "Bug"  # Default to Bug search
                
            # Build args with proper project format (string, not array with None)
            args = {
                "searchText": search_text,
                "project": [default_project]  # Must be array for search_workitem
            }
            
            # Add work item type filter if detected
            if work_item_types:
                args["workItemType"] = work_item_types
            
            # Add state filter for common state queries
            if 'active' in q_lower or 'open' in q_lower:
                args["state"] = ["Active", "New"]
            elif 'closed' in q_lower or 'done' in q_lower or 'completed' in q_lower:
                args["state"] = ["Closed", "Done", "Resolved"]
            
            # Add assignee if person was extracted
            if extracted and getattr(extracted, 'persons', None):
                args['assignedTo'] = [extracted.persons[0]]
                logger.info(f"[DETERMINISTIC_PLANNER] Added assignedTo filter: {extracted.persons[0]}")

            logger.info(f"[DETERMINISTIC_PLANNER] Using search_workitem with args: {args}")
            return {"action": "call_tool", "tool": "search_workitem", "args": args, "confidence": 0.60}
            
        except Exception as e:
            logger.error(f"[DETERMINISTIC_PLANNER] Fallback failed: {e}")
            return None

    from config import config
    api_key = config.openai_api_key  # Use OpenAI API key from config
    if not api_key:
        logger.warning("[WARN] openai_api_key not set in config; cannot use OpenAI planner")
        return None

    # Load system instructions
    system_instructions = _load_copilot_instructions()

    # Select a small subset of relevant tools to keep the prompt compact and fast
    # Also get priority tools that are recommended for this query type
    priority_tools_hint = []
    try:
        relevant_tools, priority_tools_hint = _select_relevant_tools(tool_registry, query, max_tools=config.llm_max_tool_summary)
    except Exception:
        relevant_tools = tool_registry
        priority_tools_hint = []

    # Build tool summary from the selected subset
    tool_summary = _build_tool_summary(relevant_tools)
    
    # Build priority tools hint for LLM (explicit guidance)
    priority_hint_section = ""
    if priority_tools_hint:
        # Filter to tools that are actually in the relevant_tools set
        available_priority = [t for t in priority_tools_hint if t in relevant_tools]
        if available_priority:
            priority_hint_section = f"\n\n### 🎯 Recommended Tools for This Query Type:\nBased on query analysis, consider these tools first: {', '.join(available_priority[:5])}\n(These are recommendations, not requirements - choose the best tool based on the actual query)"
    
    # Create span for LLM planning within current trace
    planner_span = create_span(
        name="llm_planner",
        input_data={"query": query[:200], "num_tools": len(relevant_tools), "priority_tools": priority_tools_hint[:5] if priority_tools_hint else []},
        metadata={"model": "gpt-4o-mini", "step": "planning", "layer": "shared"},
        session_id=session_id  # For unified session tracking
    )

    # Pre-process query to extract entities for better planning
    try:
        from utilities.entity_resolver import extract_entities, detect_query_intent
        extracted = extract_entities(query)
        intent = detect_query_intent(query)
        logger.debug(f"[LLM_PLANNER] Extracted entities: persons={extracted.persons}, types={extracted.work_item_types}, states={extracted.states}")
        logger.debug(f"[LLM_PLANNER] Detected intent: {intent.intent_type} (confidence={intent.confidence})")
    except Exception as e:
        logger.debug(f"[LLM_PLANNER] Entity extraction failed: {e}")
        extracted = None
        intent = None

    try:
        # Build entity context for LLM
        entity_context = ""
        if extracted:
            if extracted.persons:
                # Classify each person name as full name or first name only
                person_details = []
                for person in extracted.persons:
                    # Check if it's a full name (2+ parts) or username format (contains . or @)
                    is_full_name = (
                        ' ' in person.strip() or  # "First Last"
                        '.' in person or          # "first.last" or "first.last@email"
                        '@' in person             # "user@email.com"
                    )
                    classification = "FULL NAME (proceed without clarification)" if is_full_name else "first name only (may need clarification)"
                    person_details.append(f"'{person}' [{classification}]")
                entity_context += f"\nDetected person names: {', '.join(person_details)}"
            if extracted.work_item_types:
                entity_context += f"\nDetected work item types: {', '.join(extracted.work_item_types)}"
            if extracted.states:
                entity_context += f"\nDetected states: {', '.join(extracted.states)}"
            if extracted.work_item_ids:
                entity_context += f"\nDetected work item IDs: {', '.join(map(str, extracted.work_item_ids))}"

        # Load planner instructions from file (with fallback to inline)
        planner_instructions = _load_planner_instructions()
        
        # FIX #2: Build TAG_KEYWORDS context for tag detection
        tag_context = ""
        try:
            from agents.pm_agent.query_aware_filter import TAG_KEYWORDS
            tag_list = []
            for keyword, tags in TAG_KEYWORDS.items():
                tag_list.append(f"  • '{keyword}' → tags: {', '.join(tags)}")
            tag_context = "\n\n### 🏷️ TAG AWARENESS - User Keywords to ADO Tags Mapping:\n" + "\n".join(tag_list) + "\n\n**CRITICAL**: When user mentions 'blocked', 'critical', 'customer', etc., these are TAGS not STATES. Include them in analysis_criteria for filtering."
        except Exception as e:
            logger.debug(f"Could not load TAG_KEYWORDS: {e}")
        
        # Detect if query requires multi-step execution
        should_use_multi_step, multi_step_reasons = _detect_multi_step_patterns(query)
        multi_step_hint = ""
        if should_use_multi_step and multi_step_reasons:
            reasons_str = ", ".join(multi_step_reasons)
            multi_step_hint = f"""
### 🔥🔥🔥 MANDATORY MULTI-STEP EXECUTION 🔥🔥🔥
**YOU MUST USE `execute_plan` FOR THIS QUERY.**

Detected multi-step patterns: {reasons_str}

**REQUIRED STRUCTURE:**
```json
{{
  "action": "execute_plan",
  "execution_plan": {{
    "steps": [
      {{"step_id": "step_1", "type": "mcp", "tool": "...", "args": {{...}}, "produces": {{"data_1": "..."}}}},
      {{"step_id": "step_2", "type": "synthesis", "depends_on": ["step_1"]}}
    ]
  }},
  "reasoning_summary": "..."
}}
```

DO NOT use `call_tool` for this query. The patterns above indicate multi-step is REQUIRED.
"""
            logger.info(f"[LLM_PLANNER] Multi-step patterns detected: {reasons_str}")
        
        # Load PM Knowledge Base
        kb_section = ""
        try:
            from .instructions import load_kb_instructions
            _kb = load_kb_instructions()
            if _kb:
                kb_section = f"\n---\n\n## PM Knowledge Base\n\n{_kb}\n"
                logger.debug("[LLM_PLANNER] Injecting KB (%d chars) into planner prompt", len(_kb))
        except Exception as _kb_err:
            logger.debug("[LLM_PLANNER] KB not available: %s", _kb_err)

        # Build the prompt with dynamic parts
        if planner_instructions:
            # Use file-based instructions with dynamic context
            prompt = f"""{system_instructions}

---

{planner_instructions}
{kb_section}
---

## Runtime Context

{tool_summary}
{priority_hint_section}
{multi_step_hint}

### Project Context (use these values for args when applicable):
{json.dumps(context, indent=2)}
{entity_context}
{tag_context}

---

USER QUERY: {query}
"""
        else:
            # Fallback to inline instructions if file not available
            prompt = _build_inline_planner_prompt(system_instructions, tool_summary, context, entity_context, query)
        # End of else block for inline fallback prompt

        # Run OpenAI call in thread pool with context preservation
        
        def _call_openai_planner(p: str) -> str:
            """Call OpenAI API for planning."""
            from config import config
            import httpx
            client = OpenAI(
                api_key=config.openai_api_key,
                timeout=httpx.Timeout(30.0, connect=5.0)  # 30s total, 5s connect
            )
            
            try:
                response = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {"role": "system", "content": "You are a helpful assistant that responds only with valid JSON. Never include markdown formatting or explanations."},
                        {"role": "user", "content": p}
                    ],
                    temperature=0,
                    max_tokens=config.llm_max_output_tokens,
                    response_format={"type": "json_object"}
                )
                result = response.choices[0].message.content
                return result
            except Exception as e:
                logger.error(f"[LLM_PLANNER] OpenAI API call failed: {e}")
                raise

        # Retry loop with sanitization
        max_retries = 2
        last_error = None
        
        for attempt in range(max_retries + 1):
            try:
                # Use context-preserving executor to maintain Langfuse trace with 45-second timeout
                result_text = await asyncio.wait_for(
                    run_in_executor_with_context(_call_openai_planner, prompt),
                    timeout=45.0
                )
                if len(result_text) > 500:
                    logger.debug("[LLM_PLANNER] Raw response (attempt %d): %s...", attempt + 1, result_text[:500])
                else:
                    logger.debug("[LLM_PLANNER] Raw response (attempt %d): %s", attempt + 1, result_text)
                
                # Try direct parse first
                try:
                    plan = json.loads(result_text)
                except json.JSONDecodeError:
                    # Try sanitized parse
                    sanitized = _sanitize_json_text(result_text)
                    if len(sanitized) > 300:
                        logger.debug("[LLM_PLANNER] Sanitized JSON: %s...", sanitized[:300])
                    else:
                        logger.debug("[LLM_PLANNER] Sanitized JSON: %s", sanitized)
                    plan = json.loads(sanitized)
                
                logger.debug("[LLM_PLANNER] Parsed plan: action=%s, tool=%s, confidence=%s", plan.get('action'), plan.get('tool'), plan.get('confidence'))
                
                # Validate plan against the FULL tool registry + PM skills
                # relevant_tools is only a subset for keeping the prompt small;
                # the LLM may reference a valid tool that wasn't in the subset
                # (e.g. search_workitem scored below the top-N cutoff).
                # Merge PM skills into full registry for validation.
                full_validation_registry = dict(tool_registry)
                if UNIFIED_REGISTRY_AVAILABLE:
                    try:
                        pm_skills = UNIFIED_REGISTRY.get_tools_by_category("pm_skill")
                        for sk_name, sk_meta in pm_skills.items():
                            if sk_name not in full_validation_registry:
                                full_validation_registry[sk_name] = {
                                    "description": sk_meta.get("description", ""),
                                    "arg_descriptions": {arg: f"{arg} parameter" for arg in sk_meta.get("args", [])},
                                    "category": "pm_skill"
                                }
                    except Exception:
                        pass
                validated_plan = _validate_plan(plan, full_validation_registry)
                if validated_plan != plan:
                    logger.debug("[LLM_PLANNER] Validation adjusted plan: %s", validated_plan.get('action'))

                # Attempt to autocorrect sprint/iteration queries to iteration-aware tools
                try:
                    corrected_plan = _autocorrect_plan_for_iteration(query, validated_plan, tool_registry, context)
                    # Re-validate corrected plan to ensure required args are present
                    corrected_validated = _validate_plan(corrected_plan, full_validation_registry)
                    if corrected_validated != corrected_plan:
                        logger.debug("[LLM_PLANNER] Corrected plan failed validation, using original validated plan")
                    else:
                        logger.debug("[LLM_PLANNER] Using autocorrected iteration-aware plan: %s", corrected_plan.get('tool'))
                        validated_plan = corrected_plan
                except Exception as e:
                    logger.debug(f"[LLM_PLANNER] Autocorrect check failed: {e}")

                # Finalize planner span on success
                finalize_span(planner_span, output={"plan": validated_plan}, status="success")
                return validated_plan
                
            except json.JSONDecodeError as e:
                last_error = e
                logger.warning("[ERROR] LLM returned invalid JSON (attempt %d/%d): %s", attempt + 1, max_retries + 1, e)
                if attempt < max_retries:
                    logger.debug("[LLM_PLANNER] Retrying...")
                    await asyncio.sleep(0.5)  # Brief delay before retry
                continue
            except asyncio.TimeoutError:
                logger.error("[ERROR] LLM planner timed out after 45s (attempt %d)", attempt + 1)
                if attempt < max_retries:
                    logger.debug("[LLM_PLANNER] Retrying...")
                    await asyncio.sleep(1.0)
                    continue
                finalize_span(planner_span, output={"error": "timeout after 45s"}, status="error", level="ERROR")
                return None
            except Exception as e:
                logger.error("[ERROR] LLM planner failed (attempt %d): %s", attempt + 1, e)
                finalize_span(planner_span, output={"error": str(e)}, status="error", level="ERROR")
                return None
        
        # All retries exhausted
        logger.error("[ERROR] LLM JSON parsing failed after %d attempts: %s", max_retries + 1, last_error)
        finalize_span(planner_span, output={"error": str(last_error)}, status="error", level="WARNING")
        return {"action": "ask_clarification", "message": "I couldn't understand the request. Please rephrase."}

    except Exception as e:
        logger.error("[ERROR] LLM planner failed: %s", e)
        finalize_span(planner_span, output={"error": str(e)}, status="error", level="ERROR")
        return None


def _build_inline_planner_prompt(system_instructions: str, tool_summary: str, context: Dict[str, Any], entity_context: str, query: str) -> str:
    """Build inline planner prompt when file-based instructions are not available."""
    # FIX #2: Import TAG_KEYWORDS for tag detection
    tag_context = ""
    try:
        from agents.pm_agent.query_aware_filter import TAG_KEYWORDS
        tag_list = []
        for keyword, tags in TAG_KEYWORDS.items():
            tag_list.append(f"  • '{keyword}' → tags: {', '.join(tags)}")
        tag_context = "\n\n🏷️ TAG AWARENESS - User Keywords to ADO Tags Mapping:\n" + "\n".join(tag_list)
    except Exception as e:
        logger.debug(f"Could not load TAG_KEYWORDS: {e}")
    
    return f"""{system_instructions}

---

You are a Project Manager assistant with access to Azure DevOps MCP tools.

STRICT RULES:
1. Always use MCP tools to fetch data. Never assume or fabricate values.
2. Respond ONLY with valid JSON. No markdown, no prose, no explanations.
3. Use ONLY tools from the registry below. NEVER INVENT TOOLS - if a tool is not listed, it does not exist.
4. For work item queries, use search_workitem with proper filters OR execute_wiql for complex/date/priority queries.
5. DO NOT use repo_list_repos_by_project for bug/work item queries - that's for Git repositories only.
6. For person name queries (assigned to, created by), use the person name in assignedTo filter.
7. For identity/email lookup queries, use core_get_identity_ids with the person name.
8. Use searchText with the work item TYPE (e.g., "Bug") or meaningful keywords - NOT "*" as wildcard doesn't work.
9. For sprint/iteration queries, choose the right tool based on what filters are needed:
   - Simple "what's in the sprint" → wit_get_work_items_for_iteration
   - Queries with assignedTo, state filters → search_workitem or execute_wiql
10. For sprint reports, use iteration_report skill.
11. AREA PATH FILTERING: If context contains 'areaPath', INCLUDE it in tool args.
{tag_context}

CRITICAL IDENTITY RESOLUTION RULES:
When a user mentions a person by name:
1. **Full Name (2+ words)** - e.g., "John Doe", "Jane Smith": PROCEED without clarification
2. **Username format** - e.g., "john.doe", "jane_smith": PROCEED without clarification (treat as unique identifier)
3. **Email format** - e.g., "john@example.com": PROCEED without clarification (unique identifier)
4. **First Name Only (single word)** - e.g., "John", "Richa": ASK FOR CLARIFICATION
   - Return: {{"action": "ask_clarification", "message": "I found multiple users named '<first_name>'. Could you provide the full name or email address?"}}

DO NOT guess or assume which user when only a first name is provided - ask for clarification.

⚠️ CRITICAL TOOL SELECTION BY CAPABILITY (FIX FOR MULTI-STATE QUERIES):

**When the query mentions STATES (Active, New, Closed, Ready, etc.):**
- ✅ Use search_workitem (supports 'state' parameter)
- ✅ Use execute_wiql (can filter by state in WIQL)
- ❌ DO NOT use wit_get_work_items_for_iteration (does NOT support state filtering)

**When the query is about a SPECIFIC SPRINT/ITERATION:**
- Use wit_get_work_items_for_iteration (requires iterationId) - BUT ONLY if no state filtering needed
- If the query also mentions states or complex filters, use search_workitem instead
- Example: "bugs in current sprint that are Active" → use search_workitem, NOT wit_get_work_items_for_iteration

**When the query mentions AREA PATHS:**
- ✅ Use search_workitem (supports 'areaPath' parameter)
- ✅ Use execute_wiql (supports area path in WIQL)
- ⚠️ wit_get_work_items_for_iteration can use areaPath but consider search_workitem if multiple filters needed

**Decision Tree for Work Item Queries:**
1. Does query mention states (Active, New, Closed, Ready, etc.)? → Use search_workitem or execute_wiql
2. Does query mention time range (last week, past 30 days, etc.)? → Use execute_wiql (WIQL supports date filters)
3. Does query mention specific iteration/sprint AND no state filter? → Use wit_get_work_items_for_iteration
4. Does query need to group by something (by assignee, by area)? → Use search_workitem for simple grouping, execute_wiql for complex
5. Default for most work item searches → search_workitem (most flexible)

TOOL SELECTION GUIDELINES:
- wit_get_work_items_for_iteration: Use for simple sprint-scoped queries (no filters needed)
- search_workitem: Use when you need to filter by assignedTo, state, workItemType, etc.
- execute_wiql: Use for complex queries with date ranges, priority, multiple conditions, etc.
- core_get_identity_ids: Use for "what is the email for X" or "who is X" queries

CRITICAL ADO FIELD NAMES FOR execute_wiql:
- [Microsoft.VSTS.Common.ClosedDate] — when item was closed (NOT [System.ClosedDate])
- [Microsoft.VSTS.Common.ResolvedDate] — when item was resolved (NOT [System.ResolvedDate])
- [Microsoft.VSTS.Common.Priority] — priority 1-4 (NOT [System.Priority])
- [System.CreatedDate] — when item was created
- [System.ChangedDate] — when item was last modified

MULTI-STATE QUERIES (CRITICAL FIX):
When a query mentions multiple states (e.g., "New OR Ready OR Active bugs"), include them ALL in the state parameter:
- search_workitem: "state": ["New", "Ready", "Active"]
- execute_wiql: Use WIQL IN clause: [System.State] IN ('New', 'Ready', 'Active')

EXAMPLES OF CORRECT TOOL SELECTION:

1. Query: "bugs in current sprint that are active"
   Analysis: mentions "sprint" AND "active" (state) → needs state filter
   Tool: search_workitem (supports state), NOT wit_get_work_items_for_iteration
   Args: searchText="*", workItemType=["Bug"], state=["Active"], project=[default_project]

2. Query: "show me new or ready bugs"
   Analysis: mentions multiple states (New, Ready)
   Tool: search_workitem
   Args: searchText="*", workItemType=["Bug"], state=["New", "Ready"]

3. Query: "items in the sprint"
   Analysis: only asking for sprint scope, no filtering
   Tool: wit_get_work_items_for_iteration
   Args: iterationId="@CurrentIteration", project, team

CONFIDENCE CALIBRATION:
- 95–100%: Exact match with a clear tool choice and all arguments known (e.g., specific work item ID, full email)
- 85–94%: Good match but some inference required (e.g., full person name present, project inferred)
- 70–84%: Reasonable match but ambiguity exists (e.g., partial name, unclear filters)
- 50–69%: Significant ambiguity — prefer to ask for clarification (e.g., first name only, vague query)
- <50%: Very uncertain; ask for clarification before executing

CASUAL QUERY UNDERSTANDING:
Users often ask questions informally. Interpret intelligently:

1. SPRINT QUERIES:
   - "what's up?" / "how's it going?" / "status?" → Sprint status with @CurrentIteration
   - "any issues?" / "problems?" → Check for blocked items in @CurrentIteration
   - "how'd we do?" (past tense) → Use @PreviousIteration

2. WORK ITEM QUERIES:
   - "stuck stuff" / "stalled items" → blocked work items
   - "slow items" / "taking too long" → overdue/slow-moving items
   - "forgotten" / "neglected" / "stale" → overlooked_stories skill
   - "unassigned" / "orphan" / "no owner" → unassigned work items

3. SYNONYM INTERPRETATION:
   stuck/stalled/frozen/held up → blocked
   slow/lagging/behind/delayed/late → overdue
   forgotten/neglected/overlooked → stale (use overlooked_stories)
   done/finished/complete → closed state
   current/this/my sprint → @CurrentIteration
   last/previous sprint → @PreviousIteration

4. AMBIGUITY HANDLING:
   - For vague queries about "sprint", default to sprint status
   - For vague queries about "bugs", default to searching bugs
   - Only ask clarification when query could mean completely different actions
   - NEVER ask clarification for sprint-related casual queries

🚨 CRITICAL: analysis_criteria FOR WORK ITEM QUERIES
When using wit_get_work_items_for_iteration or search_workitem, you MUST include analysis_criteria.

**analysis_criteria structure:**
```json
{{
  "intent": "all|blocked|at-risk|overdue|filtered",
  "filter_logic": "Human-readable filter description",
  "evidence_fields": ["System.State", "System.WorkItemType"],
  "sort_priority": "state>last_changed"
}}
```

**CRITICAL: Setting intent correctly**
- "all": Use ONLY when user wants ALL items (e.g., "show me everything", "sprint status")
- "filtered": Use when user specifies TYPE or STATE filter (e.g., "active bugs", "my tasks")
- "blocked": Use for blocked/stuck items
- "at-risk": Use for at-risk items
- "overdue": Use for late/overdue items

**EXAMPLES with analysis_criteria:**
1. "active bugs in sprint" → 
   {{"intent": "filtered", "filter_logic": "workItemType=Bug AND state=Active", "evidence_fields": ["System.WorkItemType", "System.State"]}}

2. "show me all sprint items" → 
   {{"intent": "all", "filter_logic": "No filtering - all items"}}

3. "blocked items" → 
   {{"intent": "blocked", "filter_logic": "Tag contains 'Blocked' OR state='On Hold'"}}

4. "user stories not started" →
   {{"intent": "filtered", "filter_logic": "workItemType=User Story AND state=New", "evidence_fields": ["System.WorkItemType", "System.State"]}}

{tool_summary}

CONTEXT (use these values for args when applicable):
{json.dumps(context, indent=2)}
{entity_context}

JSON RESPONSE SCHEMA (return exactly one of these):

For tool calls (include reasoning_summary to explain your choice):
{{"action": "call_tool", "tool": "<tool_name>", "args": {{...}}, "confidence": 0.0-1.0, "reasoning_summary": "Brief explanation of why this tool and these args", "analysis_criteria": {{...}}}}

For clarification (ONLY when truly necessary):
{{"action": "ask_clarification", "message": "...", "reasoning_summary": "Why clarification is needed"}}

For no tool applicable (general knowledge questions):
{{"action": "no_tool", "message": "...", "reasoning_summary": "Why no tool applies"}}

---

USER QUERY: {query}
"""
