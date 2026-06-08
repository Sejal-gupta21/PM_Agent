"""
Orchestrator Router - Deterministic routing engine with LLM Planner hook.

╔══════════════════════════════════════════════════════════════════════════════╗
║                    ROUTING PATTERN REGISTRY                                  ║
║                    SINGLE SOURCE OF TRUTH                                    ║
╠══════════════════════════════════════════════════════════════════════════════╣
║ This file is the ONLY place where routing patterns are defined.             ║
║ All other components MUST import from this file:                            ║
║   - orchestrator/query_resolver.py ✅ (imports PM_SKILL_PATTERNS, etc.)     ║
║   - agents/pm_agent/skills.py ⚠️ (disabled, redirects here when re-enabled) ║
║   - utilities/llm/light_planner.py (fallback heuristic only)                ║
║                                                                              ║
║ DO NOT duplicate pattern definitions elsewhere.                             ║
║ If you need patterns in another file, IMPORT from here.                     ║
╚══════════════════════════════════════════════════════════════════════════════╝

This is a RULE-BASED router with an optional LLM Planner for ambiguous queries.
The router matches patterns and routes to appropriate agents based on intent.

ROUTING LOGIC (3-Step Intent Deciphering):
=====================================
The router uses a cascading fallback approach to decipher query intent:

1. REGEX MATCHING (Fastest, Most Precise)
   - Explicit pattern matching for known query types
   - PM Skill patterns (bug areas, overlooked stories, iteration reports, etc.)
   - PM Agent patterns (work items, WIQL, ADO operations)
   - Hybrid flow patterns (multi-agent sequences)
   - If matched: route directly with high confidence

2. SEMANTIC MATCHING (Fallback for Fuzzy Queries)
   - Embedding-based similarity matching to skill descriptions
   - Uses OpenAI embeddings or TF-IDF hash fallback
   - Heuristic boosts for keyword/phrase overlap
   - High confidence (>= 0.65): route directly
   - Medium confidence (>= 0.45): route with skill hint

3. LIGHT LLM PLANNER (Final Fallback)
   - ONLY invoked when regex AND semantic matching fail
   - Receives context about what was tried and why it failed
   - Uses a smaller/cheaper LLM model (gpt-3.5-turbo) for routing hints
   - Returns route, skill, confidence, and action hint
   - High confidence (>= 0.80): accept routing hint
   - Medium confidence (>= 0.55): pass hint to deep planner
   - Low confidence (< 0.55): fall through to PM Agent deep planner

KEY PRINCIPLE: The Light LLM Planner knows that regex and semantic matching
already failed, allowing it to make a more informed routing decision.

Routes:
- PM Skills Agent: Bug areas, overlooked stories, iteration reports, emails
- PM Agent (ADO Data): Work items, bugs, queries, ADO API operations
"""

import re
import json
import logging
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List, Tuple, TYPE_CHECKING
from enum import Enum

# Import resolver types for type hints
if TYPE_CHECKING:
    from orchestrator.query_resolver import ResolverResult, ResolutionMethod

logger = logging.getLogger("orchestrator.router")

# Confidence thresholds (can be overridden by config.yaml)
SEMANTIC_MATCH_HIGH_THRESHOLD = 0.65  # Route directly to skill
SEMANTIC_MATCH_MEDIUM_THRESHOLD = 0.45  # Route with skill hint
PLANNER_CONFIDENCE_THRESHOLD = 0.7

# Light LLM full plan acceptance threshold - DEPRECATED: Use config.light_planner_full_plan_threshold
# Kept for backwards compatibility, but prefer config-based threshold
LIGHT_FULL_PLAN_CONF_THRESHOLD = 0.75  # Default, overridden by config at runtime


class AgentType(Enum):
    """Available agents for routing."""
    PM_SKILL_AGENT = "pm_skill_agent"  # Business logic, SOPs, reports
    PM_AGENT = "pm_agent"              # ADO data access, LLM planner
    HOST_AGENT = "host_agent"          # Multi-agent coordination (future)


@dataclass
class RouteDecision:
    """Result of routing decision."""
    agent: AgentType
    skill: Optional[str] = None        # Specific skill for PM Skills Agent
    confidence: float = 1.0
    params: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    needs_planner: bool = False        # True if LLM planner should be invoked
    is_hybrid: bool = False            # True if hybrid flow (multi-agent)
    hybrid_steps: List[Dict[str, Any]] = field(default_factory=list)  # Hybrid flow steps
    requires_confidence_indicator: bool = False  # True if response should indicate uncertainty
    should_request_clarification: bool = False   # True if router suggests asking user for clarification
    clarification_message: Optional[str] = None  # Suggested clarification question for user


# ============================================================================
# ROUTING PATTERNS
# ============================================================================

# PM Skills Agent patterns - match these first (business logic)
PM_SKILL_PATTERNS: List[Tuple[str, str, Dict[str, Any]]] = [
    # Bug Areas Highlight
    (r"(recurring|repeated|similar)\s+(bugs?|issues?)", "bug_areas_highlight", {}),
    (r"bug\s+(areas?|patterns?|clusters?|highlight)", "bug_areas_highlight", {}),
    (r"(highlight|analyze|detect)\s+bugs?", "bug_areas_highlight", {}),
    (r"bugs?\s+(by|per)\s+area", "bug_areas_highlight", {}),
    (r"bug\s+analysis\s+report", "bug_areas_highlight", {}),
    (r"recurring\s+bug\s+(report|analysis)", "bug_areas_highlight", {}),
    
    # Feedback to Dev
    (r"feedback\s+(to\s+)?dev(eloper)?s?", "feedback_to_dev", {}),
    (r"(send|give)\s+feedback\s+", "feedback_to_dev", {}),
    (r"(rca|root\s*cause)\s+(feedback|analysis)", "feedback_to_dev", {}),
    (r"notify\s+dev(eloper)?s?\s+(about\s+)?(new\s+)?bugs?", "feedback_to_dev", {}),
    (r"developer\s+feedback\s+notification", "feedback_to_dev", {}),
    (r"new\s+bug\s+(notification|feedback)", "feedback_to_dev", {}),
    
    # Overlooked Stories - keep explicit patterns only
    # REMOVED casual patterns - these are now handled by utilities/query_normalizer.py
    (r"overlooked\s+(stories?|items?)", "overlooked_stories", {}),
    (r"forgotten\s+(stories?|items?)", "overlooked_stories", {}),
    (r"stale\s+(stories?|user\s+stories?|items?)", "overlooked_stories", {}),
    (r"stories?\s+(that|which)\s+(are|have been)\s+overlooked", "overlooked_stories", {}),
    
    # Iteration Report
    (r"(iteration|sprint)\s+report", "iteration_report", {}),
    (r"(generate|create)\s+(iteration|sprint)\s+(report|summary)", "iteration_report", {}),
    
    # WIQL routing patterns REMOVED — the LLM planner dynamically decides
    # when to use execute_wiql vs search_workitem. No regex-based routing.
    
    # Billing Deviation - NOW DYNAMIC (no UI form required)
    # IMPORTANT: These patterns route to PM Skill Agent for dynamic calculation
    (r"billing\s+deviaiton", "billing_deviation", {}),  # Typo variant
    (r"billing\s+deviation", "billing_deviation", {}),
    (r"deviaiton\s+report", "billing_deviation", {}),  # Typo variant  
    (r"deviation\s+in\s+billing", "billing_deviation", {}),  # Reversed order
    (r"(show|generate|create|give|get)\s+(the\s+)?billing\s+(report|deviation)", "billing_deviation", {}),
    (r"(over|under)[-\s]?billing", "billing_deviation", {}),
    (r"billing\s+(status|hours|report)", "billing_deviation", {}),
    (r"effort\s+(deviation|variance|tracking)", "billing_deviation", {}),
    (r"actual\s+vs\s+target\s+hours", "billing_deviation", {}),
    
    # Sprint Status - REMOVED sprint tracking patterns
    # These queries need ADO data via wit_get_work_items_for_iteration tool
    # They should go through PM Agent's LLM planner for proper tool execution
    # Keeping only iteration_report which is a true PM Skill Agent function
    
    # Backlog Health - REMOVED: Route to PM Agent + WIQL for evidence-based analysis
    # These queries need actual work item data from ADO, not a pre-built skill
    # PM Agent will use execute_wiql to fetch data, then synthesizer analyzes health
    # (r"backlog\s+(health|status)", "get_backlog_health", {}),
    # (r"(thin|healthy|sick)\s+backlog", "get_backlog_health", {}),
    # (r"(check|is)\s+backlog\s+(healthy|ok|thin)", "get_backlog_health", {}),
    # (r"refined\s+items?\s+vs\s+capacity", "get_backlog_health", {}),
    
    # Capacity Check / Capacity Forecast - UI Form skill
    (r"capacity\s+(forecast|status|warning)", "get_capacity_forecast", {"requires_ui_form": True}),
    (r"(available|team)\s+(hours|capacity)", "get_capacity_forecast", {"requires_ui_form": True}),
    (r"(developer|team)\s+utilization", "get_capacity_forecast", {"requires_ui_form": True}),
    (r"who\s+is\s+(overloaded|underutilized)", "get_capacity_forecast", {"requires_ui_form": True}),
    (r"capacity\s+check", "get_capacity_forecast", {"requires_ui_form": True}),
    (r"check\s+capacity", "get_capacity_forecast", {"requires_ui_form": True}),
    
    # Developer Knowledge Base / Developer Skills
    (r"developer\s+(knowledge\s+base|skills?)", "developer_skills", {}),
    (r"(show|get|list)\s+developer\s+(skills?|expertise)", "developer_skills", {}),
    (r"who\s+knows\s+", "developer_skills", {}),
    (r"skill\s+matrix", "developer_skills", {}),
    (r"tech\s+stack", "developer_skills", {}),
    (r"knowledge\s+base", "developer_skills", {}),
    
    # Change Assignee (NEW - Image Row 5)
    (r"(reassign|change\s+assignee|assign\s+to)\s+", "change_ado_assignee", {}),
    (r"(move|transfer)\s+work\s*item\s+to\s+", "change_ado_assignee", {}),
    (r"(assign|reassign)\s+(bug|story|task|work\s*item)\s+.*\s+to\s+", "change_ado_assignee", {}),
    
    # Detect Recurring Bugs (NEW - Image Row 9)
    (r"detect\s+(recurring|repeated)\s+bugs?", "detect_recurring_bugs", {}),
    (r"find\s+(recurring|repeated)\s+(bugs?|issues?)", "detect_recurring_bugs", {}),
    (r"recurring\s+bug\s+detection", "detect_recurring_bugs", {}),
    
    # Send Email (explicit)
    (r"send\s+(an?\s+)?email\s+to\s+", "send_email", {}),
    (r"email\s+(this|the\s+report)\s+to\s+", "send_email", {}),
    
    # List Area Paths (simple, deterministic)
    (r"^(list|show|get)\s+(the\s+)?area\s*paths?$", "list_area_paths", {}),
    (r"^what\s+are\s+(the\s+)?area\s*paths?", "list_area_paths", {}),
    
    # UI Form Skills - these require interactive forms for input
    # Sprint Plan
    (r"(generate|create)\s+sprint\s+plan", "sprint_plan", {"requires_ui_form": True}),
    (r"sprint\s+plan(ning)?", "sprint_plan", {"requires_ui_form": True}),
    (r"plan\s+(the\s+)?sprint", "sprint_plan", {"requires_ui_form": True}),
    (r"sprint\s+generator", "sprint_plan", {"requires_ui_form": True}),
    
    # Backlog Triaging
    (r"backlog\s+triag(e|ing)", "backlog_triaging", {"requires_ui_form": True}),
    (r"triage\s+(the\s+)?backlog", "backlog_triaging", {"requires_ui_form": True}),
    (r"backlog\s+analysis", "backlog_triaging", {"requires_ui_form": True}),
    (r"backlog\s+report", "backlog_triaging", {"requires_ui_form": True}),
    
    # Capacity Triaging  
    (r"capacity\s+triag(e|ing)", "capacity_triaging", {"requires_ui_form": True}),
    (r"triage\s+capacity", "capacity_triaging", {"requires_ui_form": True}),
    (r"sprint\s+risk\s+analysis", "capacity_triaging", {"requires_ui_form": True}),
    (r"capacity\s+risk\s+analysis", "capacity_triaging", {"requires_ui_form": True}),
    
    # Backlog Assignments
    (r"backlog\s+assignment", "backlog_assignments", {"requires_ui_form": True}),
    (r"assign\s+backlog", "backlog_assignments", {"requires_ui_form": True}),
    (r"task\s+assignment", "backlog_assignments", {"requires_ui_form": True}),
    (r"workload\s+assignment", "backlog_assignments", {"requires_ui_form": True}),
]

# Hybrid flow patterns - these need data from PMAgent then analysis by PMSkillAgent
# These patterns trigger multi-agent workflows where one agent fetches data and another analyzes/reports
HYBRID_PATTERNS: List[Tuple[str, List[Dict[str, Any]]]] = [
    # === EXISTING PATTERNS ===
    # "analyze bugs and send report" - fetch bugs first, then analyze
    (r"(analyze|report|summarize)\s+(on\s+)?(all\s+)?(bugs?|issues?).*(send|email)", [
        {"agent": "pm_agent", "action": "fetch_bugs"},
        {"agent": "pm_skill_agent", "skill": "bug_areas_highlight", "action": "analyze"},
    ]),
    # "get work items and create iteration report"
    (r"(get|fetch|list)\s+(all\s+)?(work\s*items?|stories?).*(create|generate).*(report|summary)", [
        {"agent": "pm_agent", "action": "fetch_items"},
        {"agent": "pm_skill_agent", "skill": "iteration_report", "action": "report"},
    ]),
    
    # === COMPARISON PATTERNS ===
    # "compare sprint 25.24 with sprint 25.25" - fetch both sprints, then compare
    # NOTE: PM Agent multi-tool execution handles the synthesis, no need for separate comparison skill
    (r"compare\s+(sprint|iteration)\s*[\d\.]+\s*(with|and|vs|to)\s*(sprint|iteration)?\s*[\d\.]+", [
        {"agent": "pm_agent", "action": "fetch_sprint_a"},
        {"agent": "pm_agent", "action": "fetch_sprint_b"},
    ]),
    # "compare current sprint with previous sprint" - handles @CurrentIteration macros
    # The PM Agent execute_plan flow already synthesizes comparison results, no third step needed
    # FIX: Added optional "the" after "compare" to match "Compare the current sprint..."
    (r"compare\s+(the\s+)?(current|this|previous|last)\s+(sprint|iteration)\s*(with|and|vs|to)\s*(the\s+)?(current|this|previous|last)\s+(sprint|iteration)", [
        {"agent": "pm_agent", "action": "fetch_current_sprint"},
        {"agent": "pm_agent", "action": "fetch_previous_sprint"},
    ]),
    # "is my current sprint better than previous sprint" - comparison phrased as question
    # FIX: Added pattern for comparative questions about sprint performance
    (r"(is|are)\s+(my\s+)?(current|this)\s+(sprint|iteration)\s+(better|worse|faster|slower|improved|declining)\s+(than|compared\s+to)", [
        {"agent": "pm_agent", "action": "fetch_current_sprint"},
        {"agent": "pm_agent", "action": "fetch_previous_sprint"},
    ]),
    # "compare team A with team B performance"
    (r"compare\s+(team|developer)s?\s+.*(with|and|vs|to)\s+(team|developer)?", [
        {"agent": "pm_agent", "action": "fetch_team_a_data"},
        {"agent": "pm_agent", "action": "fetch_team_b_data"},
        {"agent": "pm_skill_agent", "skill": "team_comparison", "action": "analyze"},
    ]),
    
    # === ANALYSIS + ACTION PATTERNS ===
    # "backlog health and create action items"
    (r"(backlog|sprint)\s+health.*(create|add|generate)\s+(action|task|item)", [
        {"agent": "pm_agent", "action": "fetch_backlog"},
        {"agent": "pm_skill_agent", "skill": "backlog_health", "action": "analyze"},
        {"agent": "pm_agent", "action": "create_action_items"},
    ]),
    # "analyze data and generate/create/send report"
    (r"analyze\s+.+\s+(and\s+)?(generate|create|send)\s+(report|email|summary)", [
        {"agent": "pm_agent", "action": "fetch_data"},
        {"agent": "pm_skill_agent", "skill": "analyze_and_report", "action": "report"},
    ]),
    
    # === TEAM PERFORMANCE PATTERNS ===
    # "team performance and send feedback"
    (r"(team|developer)\s+performance.*(feedback|report|notification|email)", [
        {"agent": "pm_agent", "action": "fetch_team_data"},
        {"agent": "pm_skill_agent", "skill": "team_performance", "action": "analyze"},
        {"agent": "pm_skill_agent", "skill": "feedback_to_dev", "action": "notify"},
    ]),
    
    # === OVERLOOKED STORIES + ACTION PATTERNS ===
    # "overlooked stories and create follow-up tasks" - FIXED: added "action" to pattern
    (r"overlooked\s+stor(y|ies).*(create|add)\s+(task|item|reminder|follow|action)", [
        {"agent": "pm_skill_agent", "skill": "overlooked_stories", "action": "identify"},
        {"agent": "pm_agent", "action": "create_follow_up_tasks"},
    ]),
    # NEW: "get overlooked stories and generate iteration report" - multi-skill flow
    (r"(get\s+)?overlooked\s+stor(y|ies).*(and|then).*(generate|create)\s+.*(report|summary)", [
        {"agent": "pm_skill_agent", "skill": "overlooked_stories", "action": "identify"},
        {"agent": "pm_skill_agent", "skill": "iteration_report", "action": "report"},
    ]),
    # NEW: "overlooked stories...then create action items" - flexible action pattern
    (r"overlooked\s+stor(y|ies).*(then|and).*(create|add|generate)\s+.*(action|task|item|follow)", [
        {"agent": "pm_skill_agent", "skill": "overlooked_stories", "action": "identify"},
        {"agent": "pm_agent", "action": "create_action_items"},
    ]),
    
    # === RISK ANALYSIS + REPORT PATTERNS ===
    # "sprint risk analysis and send email"
    (r"(sprint|iteration|capacity)\s+risk\s+analysis.*(send|email|report)", [
        {"agent": "pm_skill_agent", "skill": "capacity_triaging", "action": "analyze"},
        {"agent": "pm_skill_agent", "skill": "risk_report", "action": "report"},
    ]),
]

# PM Agent (ADO Data) patterns - these need LLM planning or ADO API
# IMPORTANT: These patterns take priority over skill patterns for ADO data queries
PM_AGENT_PATTERNS: List[str] = [
    # =========================================================================
    # TIME-BASED WIQL QUERIES (HIGHEST PRIORITY - must match before semantic)
    # These MUST be matched first to prevent incorrect routing to overlooked_stories
    # =========================================================================
    # "bugs closed in last 10 days", "user stories created in past 30 days"
    r"(bugs?|stories?|user\s+stories?|tasks?|features?|items?|work\s*items?)\s+(closed|created|completed|changed|modified|updated)\s+(in\s+)?(the\s+)?(last|past)\s+\d+\s*(days?|weeks?|months?)",
    # "closed bugs in last 10 days", "created tasks this week"
    r"(closed|created|completed|changed|modified|updated)\s+(bugs?|stories?|user\s+stories?|tasks?|features?|items?|work\s*items?)\s+(in\s+)?(the\s+)?(last|past)\s+\d+\s*(days?|weeks?|months?)",
    # "items created this week", "bugs closed yesterday"
    r"(bugs?|stories?|user\s+stories?|tasks?|features?|items?|work\s*items?)\s+(closed|created|completed|changed|modified|updated)\s+(this\s+week|yesterday|today|this\s+month)",
    # "list bugs closed in last 7 days", "show items created this week"
    r"(list|show|get|give|find|fetch|display)\s+.*(closed|created|completed|changed|modified|updated)\s+(in\s+)?(the\s+)?(last|past)\s+\d+\s*(days?|weeks?|months?)",
    # "give me all bugs closed recently", "show features completed in last 60 days"
    r"(list|show|get|give|find|fetch|display)\s+.*(bugs?|stories?|user\s+stories?|tasks?|features?|items?|work\s*items?).*(closed|created|completed|changed|modified|updated)\s+(in\s+)?(the\s+)?(last|past|recently)",
    # "items with no activity in last X days" - THIS IS FOR WIQL, NOT OVERLOOKED STORIES
    r"(items?|work\s*items?|bugs?|tasks?|stories?)\s+(with\s+)?(no\s+)?activity\s+(in\s+)?(the\s+)?(last|past)\s+\d+\s*(days?|weeks?|months?)",
    # "what bugs were closed in last 10 days"
    r"what\s+(bugs?|stories?|user\s+stories?|tasks?|features?|items?|work\s*items?)\s+(were\s+)?(closed|created|completed|changed|modified|updated)\s+(in\s+)?(the\s+)?(last|past)\s+\d+",
    # "how many bugs closed this week", "count of items created in past 30 days"
    r"(how\s+many|count|number\s+of)\s+(bugs?|stories?|user\s+stories?|tasks?|features?|items?|work\s*items?)\s+(closed|created|completed|changed|modified|updated)",
    
    # Work item queries with assignee (HIGHEST PRIORITY)
    # "list all bugs assigned to X", "show work items for deepak", etc.
    r"(list|show|get|give|find|fetch|display)\s+.*(assigned\s+to|for)\s+\w+",
    
    # Work item queries with optional modifiers (e.g., "list all dev bugs", "show active tasks")
    # This allows words like "dev", "active", "open", "closed" before the work item type
    # Added "give" to the verb list for queries like "give all active bugs"
    r"(list|show|get|give|find|fetch|display|retrieve)\s+(me\s+)?(all\s+)?(\w+\s+)?(bugs?|stories?|tasks?|items?|work\s*items?)",
    
    # Simple "all bugs" or "all work items" patterns
    r"(all|every)\s+(the\s+)?(bugs?|stories?|tasks?|items?|work\s*items?)",
    
    # State-prefixed work item queries (e.g., "active bugs", "open tasks")
    r"(active|open|closed|resolved|new|in\s*progress|done)\s+(bugs?|stories?|tasks?|items?)",
    
    # Work item type queries with filters
    r"(bugs?|stories?|tasks?|work\s*items?)\s+(in|for|from|of|assigned\s+to)\s+",
    r"work\s*items?\s+(assigned|created|updated)",
    
    # Sprint/iteration queries asking for WORK ITEMS or STATUS
    # IMPORTANT: These match when asking for work/items/status
    r"(current|this|my)\s+(sprint|iteration)\s+(items?|work|status|progress|tracking)",
    r"(sprint|iteration)\s+(status|progress|tracking|items?|work)",
    r"(items?|work)\s+(in|for)\s+(the\s+)?(current|this|my|\d+\.?\d*)\s*(sprint|iteration)",
    r"how\s+is\s+(the\s+)?(sprint|iteration)\s+(going|doing|progressing)",
    r"how\s+(are\s+)?(we|things|it)\s+(doing|going)\s+(in|for|with)\s+.*(sprint|iteration)",  # "how we are doing in 26.1 sprint"
    r"(doing|going)\s+in\s+.*(sprint|iteration)",  # "doing in X sprint"
    r"off.?track\s+(tasks?|items?)",
    r"sprint\s+summary",
    r"what.*(in\s+)?(the\s+)?(current|my|this|\d+\.?\d*)\s*(sprint|iteration)",  # Support numeric sprint names
    r"items?\s+(in|for)\s+(the\s+)?(current|this|\d+\.?\d*)\s*sprint",  # Support numeric sprint names
    r"\d+\.\d+\s*sprint",  # Explicit numeric sprint reference like "26.1 sprint"
    
    # At-risk / derailing / blocked / slowing work items (need WIQL/iteration query)
    r"(derailing|at.?risk|blocked|overdue|delayed)\s+",
    r"what\s+(is|are)\s+(the\s+)?(derailing|blocked|at.?risk|slowing)",
    r"(slowing\s+down|slow\s+down|impeding|hindering)",
    r"what.*(slowing|impeding|hindering|blocking)",
    r"(items?|tasks?|bugs?|stories?)\s+(that\s+are\s+)?(slowing|impeding|blocking)",
    r"sprint\s+(risk|issues?|problems?|blockers?)",
    
    # Specific work item by ID
    r"(work\s*item|bug|story|task)\s*#?\s*\d{4,6}",
    r"^#?\d{4,6}\s*$",
    
    # ADO entities
    r"(list|show|get)\s+(projects?|teams?|repos?|pipelines?|builds?)",
    r"my\s+(work\s*items?|tasks?|bugs?)",
    
    # WIQL queries
    r"wiql",
    r"run\s+query",
]

# ════════════════════════════════════════════════════════════════════════════════
# AREA PATH QUERY PATTERNS (Enterprise Contract for Dynamic Resolution)
# ════════════════════════════════════════════════════════════════════════════════
# These patterns detect area path specific queries for proper routing.
# Area paths are resolved DYNAMICALLY via ADO classification nodes API.
# NO static mappings, NO hardcoded values, NO fallbacks are allowed.
#
# IMPORTANT: Currently regex/semantic matching is DISABLED.
# When re-enabled, these patterns will route to execute_wiql tool
# with [System.AreaPath] UNDER clause for hierarchical filtering.
#
# See: docs/AREA_PATH_DYNAMIC_RESOLUTION_PLAN.md
# See: utilities/area_path_resolver.py
# ════════════════════════════════════════════════════════════════════════════════

# COMMENTED FOR FUTURE ENABLEMENT (per user request: "keep regex and semantic commented or disabled")
# When enabled, uncomment and add to PM_AGENT_PATTERNS
#
# AREA_PATH_PATTERNS: List[str] = [
#     # Explicit area path queries
#     r"(bugs?|stories?|tasks?|items?|work\s*items?)\s+(in|under|for)\s+area\s+(path\s+)?",
#     r"area\s+path\s+.*(bugs?|stories?|tasks?|items?|work\s*items?)",
#     r"(list|show|get|find)\s+.*(under|in)\s+area\s+",
#     
#     # Team-scoped area path queries
#     r"(bugs?|stories?|tasks?|items?)\s+(for|in)\s+team\s+\w+",
#     r"team\s+\w+\s+(bugs?|stories?|tasks?|items?)",
#     
#     # Hierarchical area queries
#     r"(nested|child|sub)\s+area\s*(path)?",
#     r"(parent|root)\s+area\s*(path)?",
#     r"area\s+hierarchy",
#     
#     # Area path with UNDER operator intent
#     r"(all\s+)?(items?|work)\s+under\s+\w+",
#     r"everything\s+(under|in|for)\s+\w+\s+area",
# ]
#
# AREA_PATH_INTENT_HINTS = {
#     "wiql_operator": "[System.AreaPath] UNDER",
#     "fallback_tool": "search_workitem with areaPath filter",
#     "resolution_method": "utilities.area_path_resolver.resolve_area_path()",
#     "classification_api": "/_apis/wit/classificationnodes/areas",
# }


class Router:
    """
    Deterministic routing engine with LLM Planner hook.
    
    Routes requests to appropriate agents using a 3-step fallback approach:
    1. Regex pattern matching (fastest, most precise)
    2. Semantic embedding matching (fuzzy matching)
    3. Light LLM planner (intelligent fallback)
    
    Supports hybrid flows (multi-agent sequences).
    """
    
    def __init__(self, mock_mcp_connector=None):
        # Compile patterns
        self.skill_patterns = [
            (re.compile(pattern, re.IGNORECASE), skill, params)
            for pattern, skill, params in PM_SKILL_PATTERNS
        ]
        self.agent_patterns = [
            re.compile(pattern, re.IGNORECASE)
            for pattern in PM_AGENT_PATTERNS
        ]
        self.hybrid_patterns = [
            (re.compile(pattern, re.IGNORECASE), steps)
            for pattern, steps in HYBRID_PATTERNS
        ]
        
        # Store mock connector for dependency injection into agents
        self.mock_mcp_connector = mock_mcp_connector
        if mock_mcp_connector:
            logger.info("Router initialized with MockMCPConnector for testing")
        
        logger.info(f"Router initialized with {len(self.skill_patterns)} skill patterns, {len(self.agent_patterns)} agent patterns, {len(self.hybrid_patterns)} hybrid patterns")
    
    # NOTE: The old async route() method has been removed (formerly lines 324-690).
    # Intent resolution is now handled by QueryResolver in query_resolver.py.
    # This Router class now uses route_from_resolver() which accepts pre-resolved intent.
    # See Orchestrator.process() for the new architecture flow:
    # QueryResolver → (Light LLM if unresolved) → Router.route_from_resolver() → Agent dispatch
    
    def _semantic_match_skill(self, query: str) -> Optional[Tuple[str, float, str, bool]]:
        """Match query to a skill using semantic similarity.
        
        Uses embeddings and heuristics for flexible, keyword-independent matching.
        
        Args:
            query: User query string
            
        Returns:
            Tuple of (skill_id, confidence_score, confidence_level, requires_ui_form) or None
        """
        try:
            from utilities.skill_registry import semantic_match_query_to_skill
            
            result = semantic_match_query_to_skill(
                query,
                confident_threshold=SEMANTIC_MATCH_HIGH_THRESHOLD,
                tentative_threshold=SEMANTIC_MATCH_MEDIUM_THRESHOLD
            )
            
            # Handle both old (3-tuple) and new (4-tuple) return formats
            if len(result) == 4:
                skill_id, score, confidence_level, requires_ui_form = result
            else:
                skill_id, score, confidence_level = result
                requires_ui_form = False
            
            if skill_id and confidence_level in ("high", "medium"):
                return (skill_id, score, confidence_level, requires_ui_form)
            
            return None
            
        except ImportError:
            logger.debug("semantic_match_query_to_skill not available, skipping semantic match")
            return None
        except Exception as e:
            logger.warning(f"Semantic skill matching failed: {e}")
            return None
    
    def _match_billing_deviation(self, query: str) -> bool:
        """
        Check if query is specifically about billing deviation.
        This is a priority check to avoid false matches from broad ADO patterns.
        
        Matches patterns like:
        - "give the billing deviation report for XOPS 25"
        - "billing deviaiton for xops bugs enhancement"
        - "deviation in billing for current month"
        """
        billing_patterns = [
            r"billing\s+deviaiton",  # Typo variant
            r"billing\s+deviation",
            r"deviaiton\s+(report|for)",  # Typo variant
            r"deviation\s+(report|for)",
            r"deviation\s+in\s+billing",
            r"(over|under)[-\s]?billing",
            r"billing\s+(status|hours|report)",
            r"actual\s+vs\s+target\s+hours",
            r"effort\s+(deviation|variance)",
            r"target\s+hours?\s+(deviation|variance)",
        ]
        query_lower = query.lower()
        for pattern in billing_patterns:
            if re.search(pattern, query_lower, re.IGNORECASE):
                return True
        return False
    
    def _match_skill(self, query: str) -> Optional[Tuple[str, Dict[str, Any]]]:
        """Match query to a PM skill pattern."""
        for regex, skill_name, params in self.skill_patterns:
            if regex.search(query):
                return (skill_name, params)
        return None
    
    def _matches_pm_agent(self, query: str) -> bool:
        """Check if query matches PM Agent patterns."""
        for regex in self.agent_patterns:
            if regex.search(query):
                return True
        return False
    
    async def _try_light_planner(
        self, 
        query: str, 
        context: Dict[str, Any],
        session_id: Optional[str] = None
    ) -> Optional[RouteDecision]:
        """
        Try the light LLM planner for routing hints.
        
        This is the THIRD step in the 3-step fallback approach:
        1. Regex patterns → FAILED (called only if this failed)
        2. Semantic matching → FAILED (called only if this failed)
        3. Light LLM Planner → THIS FUNCTION
        
        The LLM planner receives context about what matching was already tried,
        allowing it to make a more informed routing decision.
        
        Context includes:
        - routing_attempts: Dict with regex_matched, semantic_matched, etc.
        - regex_failed: True if regex patterns didn't match
        - semantic_failed: True if semantic matching didn't return high confidence
        - semantic_best_skill: The skill that had the best semantic score (if any)
        - semantic_best_score: The score of the best semantic match
        
        Returns:
            RouteDecision if light planner provides a confident route, None otherwise
        """
        from config import config
        
        # Check if light planner is enabled
        if not config.light_planner_enabled:
            logger.debug("[ROUTER] Step 3: Light planner disabled, skipping")
            return None
        
        # Extract routing attempts context
        routing_attempts = context.get("routing_attempts", {})
        regex_failed = context.get("regex_failed", True)
        semantic_failed = context.get("semantic_failed", True)
        
        # Log what was tried before this step
        logger.info(
            f"[ROUTER] Step 3: Light planner context - "
            f"regex_failed={regex_failed}, semantic_failed={semantic_failed}, "
            f"semantic_best={routing_attempts.get('semantic_skill')}, "
            f"semantic_score={routing_attempts.get('semantic_score', 0):.2f}"
        )
        
        # Determine if we should invoke light planner based on context
        from utilities.llm import should_invoke_light_planner
        
        has_skill_hint = bool(context.get("skill_hint"))
        has_intent_hint = bool(context.get("intent_hint"))
        current_confidence = context.get("semantic_score", 0.0)
        
        # Override: always invoke if regex and semantic both failed
        should_invoke = (regex_failed and semantic_failed) or should_invoke_light_planner(current_confidence, has_skill_hint, has_intent_hint)
        
        if not should_invoke and not (regex_failed and semantic_failed):
            logger.debug("[ROUTER] Step 3: Skipping light planner (conditions not met)")
            return None
        
        try:
            from utilities.llm import call_light_planner_for_routing, get_light_planner_thresholds
            
            logger.info("[ROUTER] Step 3: Invoking light LLM planner for routing hint")
            
            # Call light planner with full context
            light_result = await call_light_planner_for_routing(
                query=query,
                context=context,  # Includes routing_attempts, regex_failed, semantic_failed
                session_id=session_id
            )
            
            if not light_result:
                logger.debug("[ROUTER] Step 3: Light planner returned no result")
                return None
            
            # Get thresholds
            thresholds = get_light_planner_thresholds()
            confidence = light_result.get("confidence", 0.0)
            route = light_result.get("route", "pm_agent")
            skill = light_result.get("skill")
            action_hint = light_result.get("action_hint", "")
            
            logger.info(
                f"[ROUTER] Step 3 result: route={route}, skill={skill}, "
                f"confidence={confidence:.2f}, hint={action_hint[:50]}"
            )
            
            # Check confidence thresholds
            if confidence >= thresholds["accept_threshold"]:
                # High confidence - accept the routing hint
                logger.info(f"[ROUTER] Step 3 SUCCESS: Accepting light planner route (conf={confidence:.2f} >= {thresholds['accept_threshold']})")
                
                if route == "pm_skill_agent" and skill:
                    return RouteDecision(
                        agent=AgentType.PM_SKILL_AGENT,
                        skill=skill,
                        confidence=confidence,
                        params={"orchestrator_plan": light_result, "routing_attempts": routing_attempts},
                        reason=f"[Step 3: Light LLM] High-confidence route to {skill}: {action_hint}"
                    )
                else:
                    return RouteDecision(
                        agent=AgentType.PM_AGENT,
                        confidence=confidence,
                        params={"orchestrator_plan": light_result, "routing_attempts": routing_attempts},
                        needs_planner=False,  # Light planner already provided hint
                        reason=f"[Step 3: Light LLM] High-confidence route to PM Agent: {action_hint}"
                    )
            
            elif confidence >= thresholds["escalate_threshold"]:
                # Medium confidence - pass hint to deep planner
                logger.info(
                    f"[ROUTER] Step 3 PARTIAL: Medium confidence ({confidence:.2f}), passing hint to PM Agent deep planner"
                )
                return RouteDecision(
                    agent=AgentType.PM_AGENT,
                    confidence=confidence,
                    params={"orchestrator_plan": light_result, "light_plan_hint": True, "routing_attempts": routing_attempts},
                    needs_planner=True,  # Let deep planner refine
                    reason=f"[Step 3: Light LLM] Medium confidence ({confidence:.2f}), escalating to deep planner: {action_hint}"
                )
            
            else:
                # Low confidence - let it fall through to default fallback
                logger.info(
                    f"[ROUTER] Step 3 LOW CONFIDENCE: ({confidence:.2f} < {thresholds['escalate_threshold']}), "
                    "falling through to final fallback"
                )
                return None
            
        except Exception as e:
            logger.warning(f"[ROUTER] Light planner failed: {e}")
            return None
    
    def _match_hybrid(self, query: str) -> Optional[List[Dict[str, Any]]]:
        """Check if query matches hybrid flow patterns."""
        for regex, steps in self.hybrid_patterns:
            if regex.search(query):
                return steps
        return None
    
    def route_from_resolver(
        self,
        query: str,
        resolver_result: "ResolverResult",
        light_plan: Optional[Dict[str, Any]] = None,
        context: Optional[Dict[str, Any]] = None,
        plan_executability: Optional[Dict[str, Any]] = None
    ) -> RouteDecision:
        """
        Route a query to the appropriate agent based on pre-resolved intent.
        
        This is the NEW architecture-compliant method that:
        - Accepts pre-resolved intent from QueryResolver
        - Accepts optional Light LLM plan from Orchestrator
        - Does NOT call any LLMs (that's Orchestrator's job)
        - Only maps resolved intent → agent selection
        
        Args:
            query: Original user query
            resolver_result: Result from QueryResolver
            light_plan: Optional Light LLM plan from Orchestrator
            context: Optional routing context
            
        Returns:
            RouteDecision with agent selection
        """
        from .query_resolver import ResolverResult, ResolutionMethod
        
        context = context or {}
        
        # ═══════════════════════════════════════════════════════════════════
        # CASE 1: QueryResolver resolved the intent
        # ═══════════════════════════════════════════════════════════════════
        if resolver_result.is_resolved:
            logger.info(f"[ROUTER] Routing from resolved intent: agent={resolver_result.resolved_agent}, "
                        f"skill={resolver_result.resolved_skill}, method={resolver_result.method.value}")
            
            # Handle hybrid flows
            if resolver_result.is_hybrid:
                return RouteDecision(
                    agent=AgentType.PM_AGENT,
                    confidence=resolver_result.confidence,
                    is_hybrid=True,
                    hybrid_steps=resolver_result.hybrid_steps,
                    params=resolver_result.resolved_params,
                    reason=f"[Resolver: {resolver_result.method.value}] Hybrid flow"
                )
            
            # Route to PM Skill Agent
            if resolver_result.resolved_agent == "pm_skill_agent":
                return RouteDecision(
                    agent=AgentType.PM_SKILL_AGENT,
                    skill=resolver_result.resolved_skill,
                    confidence=resolver_result.confidence,
                    params=resolver_result.resolved_params,
                    reason=f"[Resolver: {resolver_result.method.value}] Resolved to {resolver_result.resolved_skill}"
                )
            
            # Route to PM Agent
            return RouteDecision(
                agent=AgentType.PM_AGENT,
                skill=resolver_result.resolved_skill,
                confidence=resolver_result.confidence,
                params=resolver_result.resolved_params,
                reason=f"[Resolver: {resolver_result.method.value}] Resolved to PM Agent"
            )
        
        # ═══════════════════════════════════════════════════════════════════
        # CASE 2: Light LLM provided a plan (routing hint or full execution plan)
        # ═══════════════════════════════════════════════════════════════════
        if light_plan:
            confidence = light_plan.get("confidence", 0.0)
            has_full_plan = light_plan.get("has_full_plan", False)
            plan_data = light_plan.get("plan")
            
            from config import config
            thresholds = {
                "accept_threshold": config.light_planner_accept_threshold,
                "escalate_threshold": config.light_planner_escalate_threshold,
                "full_plan_threshold": config.light_planner_full_plan_threshold
            }
            
            full_plan_threshold = thresholds["full_plan_threshold"]
            
            logger.info(f"[ROUTER] Light LLM plan: has_full_plan={has_full_plan}, "
                        f"confidence={confidence:.2f}, threshold={full_plan_threshold}")
            
            # ──────────────────────────────────────────────────────────────
            # CRITICAL: Check plan executability FIRST
            # If plan has placeholders or unresolvable dependencies, REJECT IT
            # and escalate to Deep LLM for refinement
            # ──────────────────────────────────────────────────────────────
            if plan_executability and not plan_executability.get("is_executable", True):
                diagnostics = plan_executability.get("diagnostics", [])
                logger.warning(f"[ROUTER] REJECTING non-executable Light LLM plan (has placeholders): {diagnostics[:5]}")
                
                # Don't accept this plan - escalate to Deep LLM for refinement
                # Build a hint from the failed plan so Deep LLM understands what Light LLM tried
                return RouteDecision(
                    agent=AgentType.PM_AGENT,
                    confidence=0.5,  # Lower confidence due to executability issues
                    params={"orchestrator_plan": light_plan, "light_plan_hint": True, 
                            "execution_failures": diagnostics, "routing_attempts": resolver_result.routing_attempts},
                    needs_planner=True,  # Must call Deep LLM to fix the plan
                    reason=f"[Light LLM Rejected] Plan has {len(diagnostics)} execution issues (placeholders/unresolvable deps), escalating to Deep planner"
                )
            
            # ──────────────────────────────────────────────────────────────
            # CASE 2a: Light LLM requests clarification (missing required context)
            # Return clarification request immediately without agent execution
            # ──────────────────────────────────────────────────────────────
            if light_plan.get("needs_clarification"):
                clarification_questions = light_plan.get("clarification_questions", [])
                clarification_msg = "\n".join(f"- {q}" for q in clarification_questions) if clarification_questions else "Could you provide more details about your request?"
                
                logger.info(f"[ROUTER] Light LLM requests clarification: {clarification_questions}")
                
                return RouteDecision(
                    agent=AgentType.PM_AGENT,
                    confidence=confidence,
                    params={"light_plan_hint": light_plan, "routing_attempts": resolver_result.routing_attempts},
                    should_request_clarification=True,
                    clarification_message=f"I need more information:\n{clarification_msg}",
                    reason=f"[Light LLM Clarification] Missing context, asking user for details"
                )
            
            # ──────────────────────────────────────────────────────────────
            # CASE 2a: Light LLM provided a FULL, EXECUTABLE plan
            # Accept it as authoritative, skip Deep LLM planner
            # ──────────────────────────────────────────────────────────────
            if has_full_plan and confidence >= full_plan_threshold and plan_data:
                num_steps = len(plan_data.get("steps", []))
                plan_type = plan_data.get("type", "unknown")
                
                logger.info(f"[ROUTER] Accepting Light LLM FULL PLAN: {plan_type} with {num_steps} step(s), "
                            f"confidence={confidence:.2f} >= {full_plan_threshold}")
                
                # Analyze plan for mixed action types
                steps = plan_data.get("steps", [])
                first_step = steps[0] if steps else {}
                step_action = first_step.get("action")
                step_tool = first_step.get("tool")
                
                # Check for mixed plans (both call_tool and call_skill in same plan)
                action_types = set(step.get("action", "call_tool") for step in steps)
                has_skills = "call_skill" in action_types
                has_tools = "call_tool" in action_types
                
                if has_skills and has_tools:
                    # Mixed plan detected - check if it contains execute_wiql skill
                    wiql_steps = [s for s in steps if s.get("action") == "call_skill" and s.get("tool") == "execute_wiql"]
                    if wiql_steps:
                        # WIQL skill handles ALL filtering internally, convert to single-step
                        wiql_step = wiql_steps[0]
                        logger.warning(f"[ROUTER] Mixed plan with execute_wiql detected - converting to single-step skill plan")
                        plan_data["steps"] = [wiql_step]
                        steps = [wiql_step]
                        first_step = wiql_step
                        step_action = "call_skill"
                        step_tool = "execute_wiql"
                        has_tools = False
                    else:
                        # Non-WIQL mixed plan - standard handling
                        skill_count = sum(1 for s in steps if s.get("action") == "call_skill")
                        tool_count = sum(1 for s in steps if s.get("action") == "call_tool")
                        logger.warning(f"[ROUTER] Mixed plan detected: {skill_count} skill steps, {tool_count} tool steps")
                        logger.warning(f"[ROUTER] Routing to PM_AGENT - skill steps will be skipped by orchestrator")
                
                # Check if this is a PM Skill Agent call (all steps are skills, or first step is skill)
                if step_action == "call_skill" and not has_tools:
                    return RouteDecision(
                        agent=AgentType.PM_SKILL_AGENT,
                        skill=step_tool,
                        confidence=confidence,
                        params={"orchestrator_plan": light_plan, "light_full_plan": True, "routing_attempts": resolver_result.routing_attempts},
                        needs_planner=False,  # Already have full plan from Light LLM
                        reason=f"[Light LLM Full Plan] {plan_type.capitalize()} plan with {num_steps} step(s) for skill {step_tool}"
                    )
                else:
                    # PM Agent with full execution plan (may have mixed steps - skills will be skipped)
                    return RouteDecision(
                        agent=AgentType.PM_AGENT,
                        confidence=confidence,
                        params={"orchestrator_plan": light_plan, "light_full_plan": True, "routing_attempts": resolver_result.routing_attempts},
                        needs_planner=False,  # Already have full plan from Light LLM
                        reason=f"[Light LLM Full Plan] {plan_type.capitalize()} plan with {num_steps} step(s), tool={step_tool}"
                    )
            
            # ──────────────────────────────────────────────────────────────
            # CASE 2b: Light LLM provided incomplete plan or lower confidence
            # Pass as hint to Deep LLM planner for refinement
            # ──────────────────────────────────────────────────────────────
            elif confidence >= thresholds["escalate_threshold"]:
                logger.info(f"[ROUTER] Light LLM partial plan (conf={confidence:.2f}), "
                            f"escalating to Deep LLM for refinement")
                
                # Extract route/skill from old-style response for backward compatibility
                route = light_plan.get("route", "pm_agent")
                skill = light_plan.get("skill")
                action_hint = light_plan.get("action_hint", light_plan.get("analysis_summary", ""))
                
                if route == "pm_skill_agent" and skill:
                    return RouteDecision(
                        agent=AgentType.PM_SKILL_AGENT,
                        skill=skill,
                        confidence=confidence,
                        params={"orchestrator_plan": light_plan, "light_plan_hint": True, "routing_attempts": resolver_result.routing_attempts},
                        needs_planner=True,  # Incomplete, needs Deep LLM refinement
                        reason=f"[Light LLM Hint] Partial confidence ({confidence:.2f}), escalating to Deep planner: {action_hint[:50]}"
                    )
                else:
                    return RouteDecision(
                        agent=AgentType.PM_AGENT,
                        confidence=confidence,
                        params={"orchestrator_plan": light_plan, "light_plan_hint": True, "routing_attempts": resolver_result.routing_attempts},
                        needs_planner=True,  # Incomplete, needs Deep LLM refinement
                        reason=f"[Light LLM Hint] Partial confidence ({confidence:.2f}), escalating to Deep planner: {action_hint[:50]}"
                    )
            
            else:
                # Very low confidence - treat as no plan
                logger.info(f"[ROUTER] Light LLM low confidence ({confidence:.2f}), falling through to default routing")
        
        # ═══════════════════════════════════════════════════════════════════
        # CASE 3: Fallback - Use hints or default to PM Agent
        # ═══════════════════════════════════════════════════════════════════
        
        # Check for skill hint from semantic matching
        if resolver_result.skill_hint:
            skill_id = resolver_result.skill_hint
            semantic_score = resolver_result.confidence
            
            if semantic_score < 0.6:
                # Very low confidence - request clarification
                logger.info(f"[ROUTER] Fallback: Low confidence ({semantic_score:.2f}) for skill: {skill_id}")
                return RouteDecision(
                    agent=AgentType.PM_AGENT,
                    skill=skill_id,
                    confidence=semantic_score,
                    params={"skill_hint": skill_id, "routing_attempts": resolver_result.routing_attempts},
                    should_request_clarification=True,
                    clarification_message=f"I think you're asking about {skill_id.replace('_', ' ')}, but I'm not sure. Could you provide more details?",
                    reason=f"[Fallback] Low-confidence hint: {skill_id}"
                )
            
            return RouteDecision(
                agent=AgentType.PM_AGENT,
                skill=skill_id,
                confidence=semantic_score,
                params={"skill_hint": skill_id, "routing_attempts": resolver_result.routing_attempts},
                reason=f"[Fallback] Medium-confidence hint: {skill_id}"
            )
        
        # Check for intent hint from normalization
        if resolver_result.intent_hint:
            intent = resolver_result.intent_hint
            conf = resolver_result.routing_attempts.get("normalized_confidence", 0.5)
            
            if conf < 0.5:
                return RouteDecision(
                    agent=AgentType.PM_AGENT,
                    confidence=conf,
                    params={"intent_hint": intent, "routing_attempts": resolver_result.routing_attempts},
                    should_request_clarification=True,
                    clarification_message="I'm not quite sure what you're looking for. Could you please rephrase?",
                    reason=f"[Fallback] Very low-confidence intent: {intent}"
                )
            
            return RouteDecision(
                agent=AgentType.PM_AGENT,
                confidence=conf,
                params={"intent_hint": intent, "routing_attempts": resolver_result.routing_attempts},
                needs_planner=True,
                requires_confidence_indicator=True,
                reason=f"[Fallback] Low-confidence intent: {intent}, needs deep planner"
            )
        
        # ═══════════════════════════════════════════════════════════════════
        # ABSOLUTE FINAL FALLBACK → PM Agent with Deep Planner
        # ═══════════════════════════════════════════════════════════════════
        logger.info(f"[ROUTER] ABSOLUTE FALLBACK: Routing to PM Agent deep planner")
        return RouteDecision(
            agent=AgentType.PM_AGENT,
            confidence=0.3,
            needs_planner=True,
            params={"routing_attempts": resolver_result.routing_attempts},
            reason="[Fallback] All resolution steps failed, needs deep LLM planner",
            requires_confidence_indicator=True
        )
    
    def get_routing_info(self) -> Dict[str, Any]:
        """Get information about routing rules."""
        return {
            "pm_skill_patterns": [
                {"pattern": p.pattern, "skill": s, "params": pr}
                for p, s, pr in self.skill_patterns
            ],
            "pm_agent_patterns": [p.pattern for p in self.agent_patterns],
            "hybrid_patterns": [
                {"pattern": p.pattern, "steps": s}
                for p, s in self.hybrid_patterns
            ],
            "planner_threshold": PLANNER_CONFIDENCE_THRESHOLD
        }


# ============================================================================
# PLANNER INTEGRATION
# ============================================================================

# REMOVED: invoke_planner() function - orchestrator no longer invokes planner directly
# The PM Agent has its own internal LLM planner (agents/pm_agent/llm.py:plan_mcp_call)
# Architecture flow: Controller -> Orchestrator -> PM Agent -> LLM Planner (inside agent)
# This eliminates duplicate planner invocations


import os  # Add os import for env vars
from typing import Callable

# Import trace context helpers for unified observability
from utilities.langfuse_client import (
    create_span, finalize_span, get_current_trace, set_current_trace
)

# NOTE: Synthesis is handled by agents themselves (PMAgent, PMSkillAgent)
# Orchestrator only routes queries and passes through agent results
# No LLM operations should happen at orchestrator level to avoid duplicacy

# Import Guardrails and State Manager
from .guardrails import get_guardrails, GuardrailAction
from .state_manager import get_state_manager

# Import Synthesis layer (new architecture)
from .synthesis import (
    synthesize_response,
    SynthesisStatus,
    AgentResult,
    AgentStatus,
)

# Import Deep LLM (existing plan_mcp_call from planner.py)
# Called by Orchestrator ONLY when agent returns NEEDS_DEEP_ANALYSIS
from utilities.llm import plan_mcp_call
from utilities.mcp.tool_registry import MCP_TOOL_REGISTRY


def _sanitize_and_truncate_reasoning(text: Optional[str], max_len: int = 1000) -> Optional[str]:
    """Sanitize and truncate planner reasoning for safe logging.

    - Removes obvious secret patterns (api_key, pat, token, secret)
    - Redacts very long tokens that look like keys
    - Truncates to `max_len` characters
    """
    if not text:
        return None
    try:
        # Remove common secret assignments like api_key=..., pat: ..., token=...
        redacted = re.sub(r"(?i)(api[_-]?key|pat|token|secret)[\s:=]+[^\s,;]+", "<REDACTED>", text)
        # Redact very long alphanumeric tokens (likely keys)
        redacted = re.sub(r"[A-Za-z0-9_\-]{40,}", "<REDACTED_TOKEN>", redacted)
        # Collapse whitespace and limit length
        redacted = " ".join(redacted.split())
        if len(redacted) > max_len:
            return redacted[:max_len] + "..."
        return redacted
    except Exception:
        return (text[:max_len] + "...") if text else None


# ============================================================================
# ORCHESTRATOR
# ============================================================================

class Orchestrator:
    """
    Main orchestrator that coordinates routing and agent execution.
    
    Architecture Components:
    - Guardrails: Input validation and safety (runs FIRST)
    - State Manager: Conversation state tracking
    - Router: Deterministic routing engine
    - Agents: PM Agent (ADO Data), PM Skills Agent (Business Logic)
    
    Flow:
    1. Receive user query
    2. [GUARDRAILS] Validate and sanitize input
    3. [STATE MANAGER] Check for pending clarifications, get context
    4. [ROUTER] Determine target agent
    5. If confidence < threshold, invoke LLM Planner
    6. Dispatch to appropriate agent
    7. For hybrid flows: execute multi-agent sequence
    8. [STATE MANAGER] Update session state
    9. Collect results and return
    
    CRITICAL: All operations create spans under the controller's trace
    for unified single-trace observability.
    """
    
    def __init__(self, mock_mcp_connector=None):
        self.mock_mcp_connector = mock_mcp_connector  # Store for later use
        self.router = Router(mock_mcp_connector=mock_mcp_connector)
        self.guardrails = get_guardrails(strict_mode=False)
        self.state_manager = get_state_manager()
        self._pm_skill_agent = None
        self._pm_agent = None
        if mock_mcp_connector:
            logger.info("Orchestrator initialized with MockMCPConnector for testing")
        else:
            logger.info("Orchestrator initialized with Guardrails and State Manager")
        
    async def get_pm_skill_agent(self):
        """Lazy load PM Skills Agent and inject PM Agent for data access."""
        if self._pm_skill_agent is None:
            from agents.pm_skill_agent import PMSkillAgent
            from agents.pm_skill_agent.skills import set_pm_agent
            
            self._pm_skill_agent = PMSkillAgent()
            
            # Inject PM Agent for data access
            pm_agent = await self.get_pm_agent()
            set_pm_agent(pm_agent)
            logger.info("Injected PM Agent into PM Skills Agent for data access")
            
        return self._pm_skill_agent
    
    async def get_pm_agent(self):
        """Lazy load PM Agent."""
        if self._pm_agent is None:
            from agents.pm_agent.agent import PMAgent
            # Inject mock connector if available (for testing)
            self._pm_agent = PMAgent(mcp_connector=self.mock_mcp_connector)
            await self._pm_agent.create()
        return self._pm_agent
    
    async def reload_pm_agent(self):
        """
        Force reload PM Agent - useful when instructions are updated.
        Clears cached instance and creates new one with fresh instructions.
        """
        logger.info("Reloading PM Agent (clearing cached instance and instructions)")
        
        # Clean up existing instance
        if self._pm_agent is not None:
            try:
                # Close MCP connector if it exists
                if hasattr(self._pm_agent, 'mcp_connector') and self._pm_agent.mcp_connector:
                    # MCP connector will clean up subprocess on next initialization
                    pass
            except Exception as e:
                logger.warning(f"Error during PM Agent cleanup: {e}")
            finally:
                self._pm_agent = None
        
        # Create fresh instance (will clear instructions cache in __init__)
        self._pm_agent = None
        return await self.get_pm_agent()
    
    async def process(self, query: str, session_id: str = "default") -> Dict[str, Any]:
        """
        Process a user query through the orchestration pipeline.
        
        NEW ARCHITECTURE (Strict Execution Order):
        ═══════════════════════════════════════════════════════════════════
        1. [GUARDRAILS] Input validation and safety
        2. [STATE MANAGER] Check for pending clarifications, get context
        3. [QUERY RESOLVER] Deterministic resolution (regex → semantic)
        4. [LIGHT LLM] Only if resolver fails (final orchestrator fallback)
        5. [ROUTER] Agent selection based on resolved intent
        6. [AGENT DISPATCH] Execute via appropriate agent
        7. [AGENT DEEP LLM] Only inside agent for local reasoning
        ═══════════════════════════════════════════════════════════════════
        
        CRITICAL BOUNDARIES:
        - Light LLM: Orchestrator-level fallback ONLY
        - Deep LLM: Agent-level reasoning ONLY
        - These NEVER overlap or call each other
        - Light LLM must complete BEFORE agent invocation
        
        Creates unified trace for the entire request lifecycle.
        """
        from utilities.langfuse_client import create_trace, create_span, finalize_trace, finalize_span, get_current_trace
        from .query_resolver import get_query_resolver
        
        existing_trace = get_current_trace()
        
        if existing_trace:
            logger.info("Orchestrator: Found existing controller trace, creating span")
            orchestrator_span = create_span(
                name="orchestrator.process",
                input_data={"query": query[:500], "session_id": session_id},
                metadata={"step": "orchestration", "provenance": "orchestrator"},
                parent_trace=existing_trace,
                session_id=session_id
            )
            trace = None
        else:
            logger.info("Orchestrator: No controller trace found, creating orchestrator_request trace")
            trace = create_trace(
                name="orchestrator_request",
                input_data={"query": query[:500], "session_id": session_id},
                metadata={"step": "orchestration", "provenance": "orchestrator"},
                session_id=session_id
            )
            orchestrator_span = None
            if trace:
                set_current_trace(trace)
        
        parent_for_spans = trace or orchestrator_span
        
        try:
            # ═══════════════════════════════════════════════════════════
            # STEP 1: GUARDRAILS - Input validation and safety
            # ═══════════════════════════════════════════════════════════
            guardrails_span = create_span(
                name="orchestrator.guardrails",
                input_data={"query": query[:200]},
                metadata={"step": "guardrails"},
                parent_trace=parent_for_spans,
                session_id=session_id
            )
            
            guardrail_result = self.guardrails.check(query, session_id)
            
            if guardrail_result.action == GuardrailAction.BLOCK:
                logger.warning(f"[GUARDRAILS] Query blocked: {guardrail_result.blocked_reason}")
                finalize_span(guardrails_span, output={"action": "blocked"}, status="warning")
                
                # Route through synthesis layer for consistent response formatting
                return synthesize_response(
                    status=SynthesisStatus.BLOCKED,
                    error=guardrail_result.blocked_reason,
                    metadata={
                        "action": "blocked",
                        "reason": guardrail_result.blocked_reason,
                        "risk_score": guardrail_result.risk_score
                    },
                    parent_trace=parent_for_spans,
                    session_id=session_id
                )
            
            processed_query = guardrail_result.sanitized_query or query
            if guardrail_result.warnings:
                logger.info(f"[GUARDRAILS] Warnings: {guardrail_result.warnings}")
            
            finalize_span(guardrails_span, output={"action": "allow", "sanitized": bool(guardrail_result.sanitized_query)}, status="success")
            
            # ═══════════════════════════════════════════════════════════
            # STEP 2: STATE MANAGER - Check for pending clarifications
            # ═══════════════════════════════════════════════════════════
            routing_context = self.state_manager.get_context_for_routing(session_id, processed_query)
            
            if routing_context.get("is_clarification_response") and routing_context.get("pending_clarification"):
                session = self.state_manager.get_or_create(session_id)
                resolved_query = session.resolve_clarification(processed_query)
                if resolved_query:
                    logger.info(f"[STATE_MANAGER] Resolved clarification: '{processed_query}' -> '{resolved_query}'")
                    processed_query = resolved_query
            
            # ═══════════════════════════════════════════════════════════
            # STEP 3: QUERY RESOLVER - Deterministic resolution
            # (regex → semantic, NO LLM here)
            # ═══════════════════════════════════════════════════════════
            resolver_span = create_span(
                name="orchestrator.intent.resolver",
                input_data={"query": processed_query[:200]},
                metadata={"step": "resolver", "substeps": "regex+semantic"},
                parent_trace=parent_for_spans,
                session_id=session_id
            )
            
            resolver = get_query_resolver()
            resolver_result = resolver.resolve(processed_query, routing_context)
            
            finalize_span(resolver_span, output={
                "is_resolved": resolver_result.is_resolved,
                "method": resolver_result.method.value,
                "confidence": resolver_result.confidence,
                "resolved_agent": resolver_result.resolved_agent,
                "resolved_skill": resolver_result.resolved_skill
            }, status="success")
            
            logger.info(f"[RESOLVER] Result: resolved={resolver_result.is_resolved}, "
                        f"method={resolver_result.method.value}, "
                        f"agent={resolver_result.resolved_agent}, "
                        f"skill={resolver_result.resolved_skill}")
            
            # ═══════════════════════════════════════════════════════════
            # STEP 4: LIGHT LLM PLANNER - Only if resolver failed
            # (This is the FINAL orchestrator-level fallback)
            # 
            # ARCHITECTURE CHANGE (2026-01-23): Light LLM is now the PRIMARY planner.
            # Light LLM generates COMPLETE execution plans with tools, args, and WIQL.
            # Deep LLM is ONLY called when Light LLM fails or produces incomplete plans.
            # ═══════════════════════════════════════════════════════════
            light_plan = None
            
            if not resolver_result.is_resolved:
                from config import config
                
                if config.light_planner_enabled:
                    light_span = create_span(
                        name="orchestrator.intent.light_llm",
                        input_data={"query": processed_query[:200], "resolver_context": resolver_result.routing_attempts},
                        metadata={"step": "light_planner", "layer": "orchestrator"},
                        parent_trace=parent_for_spans,
                        session_id=session_id
                    )
                    
                    try:
                        from utilities.llm import call_light_planner_for_routing
                        
                        # Prepare CLEAN context for light planner (no poisoning flags)
                        light_context = _prepare_light_llm_context(
                            routing_context=routing_context,
                            resolver_result=resolver_result,
                            state_manager=self.state_manager
                        )
                        
                        logger.info(f"[ORCHESTRATOR] Invoking Light LLM Planner (resolver failed) with context: project={light_context.get('project')}, team={light_context.get('team')}")
                        light_plan = await call_light_planner_for_routing(
                            query=processed_query,
                            context=light_context,
                            session_id=session_id
                        )
                        
                        if light_plan:
                            has_full_plan = light_plan.get("has_full_plan", False)
                            confidence = light_plan.get("confidence", 0.0)
                            plan_data = light_plan.get("plan")
                            needs_clarification = light_plan.get("needs_clarification", False)
                            
                            # Check plan executability (no placeholders)
                            is_executable = False
                            plan_diagnostics = []
                            if has_full_plan and plan_data:
                                is_executable, plan_diagnostics = _plan_is_executable(light_plan)
                            
                            logger.info(f"[ORCHESTRATOR] Light LLM plan: has_full_plan={has_full_plan}, "
                                        f"confidence={confidence:.2f}, executable={is_executable}, needs_clarification={needs_clarification}")
                            
                            if plan_diagnostics:
                                logger.warning(f"[ORCHESTRATOR] Light LLM plan diagnostics: {plan_diagnostics}")
                            
                            if has_full_plan and plan_data:
                                num_steps = len(plan_data.get("steps", []))
                                plan_type = plan_data.get("type", "unknown")
                                logger.info(f"[ORCHESTRATOR] Light LLM produced full {plan_type} plan with {num_steps} step(s)")
                            else:
                                logger.info("[ORCHESTRATOR] Light LLM produced partial plan or routing hint only")
                            
                            # Build trace output with diagnostics and raw response info
                            out = {
                                "has_full_plan": has_full_plan,
                                "confidence": confidence,
                                "plan_type": plan_data.get("type") if plan_data else None,
                                "num_steps": len(plan_data.get("steps", [])) if plan_data else 0,
                                "source": light_plan.get("source", "light_llm"),
                                "is_executable": is_executable,
                                "plan_diagnostics": plan_diagnostics,
                                "needs_clarification": needs_clarification,
                                "clean_context_keys": list(light_context.keys()),
                                "clean_context_project": light_context.get("project"),
                                "clean_context_team": light_context.get("team")
                            }
                            # Include full plan payload when available so traces show executable steps
                            if has_full_plan and plan_data:
                                out["plan"] = plan_data
                            
                            # Include clarification questions if any
                            if needs_clarification:
                                out["clarification_questions"] = light_plan.get("clarification_questions", [])

                            finalize_span(light_span, output=out, status="success")
                        else:
                            logger.info("[ORCHESTRATOR] Light LLM returned no result")
                            finalize_span(light_span, output={"result": "no_decision"}, status="success")
                            
                    except Exception as e:
                        logger.warning(f"[ORCHESTRATOR] Light LLM failed: {e}")
                        finalize_span(light_span, output={"error": str(e)}, status="warning")
                else:
                    logger.debug("[ORCHESTRATOR] Light planner disabled, skipping")
            
            # ═══════════════════════════════════════════════════════════
            # STEP 5: ROUTER - Agent selection based on resolved intent
            # (Router does NOT call any LLMs - pure mapping)
            # ═══════════════════════════════════════════════════════════
            router_span = create_span(
                name="orchestrator.routing",
                input_data={
                    "query": processed_query[:200],
                    "resolver_resolved": resolver_result.is_resolved,
                    "light_plan_provided": bool(light_plan)
                },
                metadata={"step": "routing"},
                parent_trace=parent_for_spans,
                session_id=session_id
            )
            
            # Pass plan executability status to router
            plan_executability = None
            if light_plan and light_plan.get("has_full_plan"):
                is_executable, plan_diagnostics = _plan_is_executable(light_plan)
                plan_executability = {
                    "is_executable": is_executable,
                    "diagnostics": plan_diagnostics
                }
                if not is_executable:
                    logger.warning(f"[ORCHESTRATOR] Plan is NOT executable - passing to router for rejection: {plan_diagnostics[:3]}")
            
            decision = self.router.route_from_resolver(
                query=processed_query,
                resolver_result=resolver_result,
                light_plan=light_plan,
                context=routing_context,
                plan_executability=plan_executability
            )
            
            finalize_span(router_span, output={
                "agent": decision.agent.value,
                "skill": decision.skill,
                "confidence": decision.confidence,
                "needs_planner": decision.needs_planner,
                "reason": decision.reason[:100] if decision.reason else None
            }, status="success")
            
            logger.info(f"[ROUTER] Decision: agent={decision.agent.value}, skill={decision.skill}, "
                        f"confidence={decision.confidence:.2f}, needs_planner={decision.needs_planner}")
            
            # ═══════════════════════════════════════════════════════════
            # NOTE: Deep LLM (plan_mcp_call) is ONLY called when:
            # 1. Light LLM failed or was disabled
            # 2. Light LLM produced incomplete plan (has_full_plan=false)
            # 3. Agent execution fails and needs re-planning (fallback in agent)
            # 
            # The orchestrator does NOT automatically call Deep LLM anymore.
            # Light LLM is the PRIMARY planner; Deep LLM is FALLBACK only.
            # ═══════════════════════════════════════════════════════════
            
            # ═══════════════════════════════════════════════════════════
            # STEP 6: AGENT DISPATCH
            # ═══════════════════════════════════════════════════════════
            result = None
            
            if decision.is_hybrid:
                result = await self._execute_hybrid_flow(decision.hybrid_steps, processed_query, session_id)
            
            elif decision.agent == AgentType.PM_SKILL_AGENT:
                # Create span for PM Skills Agent execution
                has_light_plan = light_plan and light_plan.get("has_full_plan", False)
                skill_span = create_span(
                    name="agent.execute",
                    input_data={
                        "skill": decision.skill, 
                        "params": decision.params,
                        "has_orchestrator_plan": has_light_plan
                    },
                    metadata={"agent": "pm_skill_agent", "agent_type": "skill"},
                    parent_trace=parent_for_spans,
                    session_id=session_id
                )
                try:
                    # Execute via PM Skills Agent - pass parent trace
                    agent = await self.get_pm_skill_agent()
                    
                    # Pass orchestrator_plan from decision params (contains light_plan if available)
                    orchestrator_plan = decision.params.get("orchestrator_plan") if decision.params else None
                    
                    if orchestrator_plan and orchestrator_plan.get("has_full_plan"):
                        logger.info(f"[ORCHESTRATOR] Passing Light LLM full plan to PM Skill Agent")
                    elif orchestrator_plan:
                        logger.info("[ORCHESTRATOR] Passing Light LLM routing hint to PM Skill Agent (no full plan)")
                    else:
                        logger.info("[ORCHESTRATOR] No orchestrator plan available for PM Skill Agent")
                    
                    invoke_payload = {
                        "skill": decision.skill,
                        "query": processed_query,
                        "params": decision.params,
                        "session_id": session_id,
                        "parent_trace": parent_for_spans,
                        "orchestrator_plan": orchestrator_plan
                    }
                    # PM Skills Agent runs deterministic logic (no LLM planner needed)

                    async for response in agent.invoke(invoke_payload):
                        result = response
                    
                    # Extract structured data from PM skill agent response for UI rendering
                    # PM skill agent wraps the result in {"content": {...}}
                    if result and isinstance(result, dict) and "content" in result:
                        content_data = result.get("content")
                        if isinstance(content_data, dict):
                            # Lift items, paths, and other structured data to top level for UI
                            for key in ["items", "download_path", "evidence_paths", "data"]:
                                if key in content_data and key not in result:
                                    result[key] = content_data[key]
                                    logger.debug(f"Lifted {key} from PM skill agent content to result")
                            
                            # CRITICAL: Preserve skill metadata (auto_grant_access, open_ui_directly, access info)
                            # This is needed for UI form handling and direct UI opening
                            if "metadata" in content_data:
                                skill_metadata = content_data["metadata"]
                                if isinstance(skill_metadata, dict):
                                    # Merge skill metadata into result metadata
                                    if "metadata" not in result:
                                        result["metadata"] = {}
                                    result["metadata"].update(skill_metadata)
                                    logger.debug(f"Preserved skill metadata: {list(skill_metadata.keys())}")
                            
                            # Also check if the skill result directly contains UI flags
                            if "result" in content_data and isinstance(content_data["result"], dict):
                                skill_result = content_data["result"]
                                for ui_key in ["requires_ui_form", "auto_grant_access", "open_ui_directly", "access", "skill", "alias"]:
                                    if ui_key in skill_result:
                                        if "metadata" not in result:
                                            result["metadata"] = {}
                                        result["metadata"][ui_key] = skill_result[ui_key]
                                        # Also set at top level for easy UI access
                                        result[ui_key] = skill_result[ui_key]
                                        logger.debug(f"Lifted UI flag {ui_key} to result and metadata")
                    
                    finalize_span(skill_span, output={"success": True}, status="success")
                    
                    # Update state manager
                    context_update = {"skill": decision.skill}
                    
                    # Handle billing_deviation pending state
                    if result and isinstance(result, dict):
                        content_data = result.get("content")
                        if isinstance(content_data, dict) and "metadata" in content_data:
                            skill_metadata = content_data["metadata"]
                            if isinstance(skill_metadata, dict):
                                # Store pending context if skill is waiting for target hours
                                if "pending_billing_deviation" in skill_metadata:
                                    pending_info = skill_metadata["pending_billing_deviation"]
                                    context_update["pending_billing_deviation"] = pending_info
                                    logger.info(f"[STATE_MANAGER] Stored pending billing_deviation context: {pending_info}")
                                # Clear pending context if skill completed successfully (no needs_target_hours flag)
                                elif decision.skill == "billing_deviation" and not skill_metadata.get("needs_target_hours"):
                                    context_update["pending_billing_deviation"] = None
                                    logger.info(f"[STATE_MANAGER] Cleared pending billing_deviation - task complete")
                    
                    self.state_manager.update_session(
                        session_id, "pm_skill_agent",
                        context_update,
                        query=processed_query,
                        result_summary=str(result.get("content", ""))[:200] if result else None
                    )
                except Exception as e:
                    finalize_span(skill_span, output={"error": str(e)}, status="error", level="ERROR")
                    raise
            
            elif decision.agent == AgentType.PM_AGENT:
                # Create span for PM Agent execution
                # ═══════════════════════════════════════════════════════════
                # ARCHITECTURE CHANGE (2026-01-23): Light LLM is now PRIMARY planner.
                # If orchestrator has a light_plan with full execution details,
                # pass it to PM Agent so it executes directly WITHOUT calling Deep LLM.
                # Deep LLM is only used as fallback inside agent when Light plan fails.
                # ═══════════════════════════════════════════════════════════
                has_light_plan = light_plan and light_plan.get("has_full_plan", False)
                pm_span = create_span(
                    name="agent.execute",
                    input_data={
                        "query": processed_query[:200], 
                        "needs_planner": decision.needs_planner,
                        "has_orchestrator_plan": has_light_plan
                    },
                    metadata={"agent": "pm_agent", "agent_type": "data", "layer": "agent"},
                    parent_trace=parent_for_spans,
                    session_id=session_id
                )
                try:
                    # Execute via PM Agent (ADO Data Agent)
                    agent = await self.get_pm_agent()
                    
                    # Pass orchestrator_plan from decision params (contains light_plan if available)
                    orchestrator_plan = decision.params.get("orchestrator_plan") if decision.params else None
                    
                    if orchestrator_plan and orchestrator_plan.get("has_full_plan"):
                        logger.info(f"[ORCHESTRATOR] Passing Light LLM full plan to PM Agent")
                    elif orchestrator_plan:
                        logger.info("[ORCHESTRATOR] Passing Light LLM routing hint to PM Agent (may use Deep LLM for planning)")
                    else:
                        logger.info("[ORCHESTRATOR] No orchestrator plan - PM Agent will use Deep LLM for planning")
                    
                    async for response in agent.invoke(
                        processed_query, 
                        session_id, 
                        parent_trace=pm_span,
                        orchestrator_plan=orchestrator_plan
                    ):
                        result = response
                    finalize_span(pm_span, output={"success": True}, status="success")
                    
                    # Update state manager
                    self.state_manager.update_session(
                        session_id, "pm_agent",
                        {"agent": "pm_agent"},
                        query=processed_query,
                        result_summary=str(result.get("content", ""))[:200] if result else None
                    )
                except Exception as e:
                    finalize_span(pm_span, output={"error": str(e)}, status="error", level="ERROR")
                    raise
            
            else:
                result = {
                    "is_task_complete": True,
                    "content": f"Unknown agent: {decision.agent}"
                }
            
            # ═══════════════════════════════════════════════════════════
            # STEP 7: CHECK AGENT STATUS AND REPLAN IF NEEDED
            # (Deep LLM replan - max 1 per request)
            # ═══════════════════════════════════════════════════════════
            was_replanned = False
            was_escalated = False
            was_self_corrected = False
            
            if result and isinstance(result, dict):
                # Check for NEEDS_DEEP_ANALYSIS status
                status_str = result.get("status", "")
                needs_replan = (
                    status_str == AgentStatus.NEEDS_DEEP_ANALYSIS.value or
                    result.get("deep_analysis_context") is not None
                )
                
                # B4: Check for REQUEST_REPLAN status (self-correction)
                if status_str == AgentStatus.REQUEST_REPLAN.value:
                    logger.info(f"[ORCHESTRATOR] Agent requested replan: {result.get('replan_context', {})}")
                    replan_context = result.get("replan_context", {})
                    result, was_replanned = await self._handle_replan_request(
                        result=result,
                        replan_context=replan_context,
                        query=processed_query,
                        decision=decision,
                        session_id=session_id,
                        parent_trace=parent_for_spans
                    )
                    was_self_corrected = was_replanned
                
                # B4: Check for REQUEST_ESCALATION status (agent handoff)
                elif status_str == AgentStatus.REQUEST_ESCALATION.value:
                    logger.info(f"[ORCHESTRATOR] Agent requested escalation: {result.get('escalation_context', {})}")
                    escalation_context = result.get("escalation_context", {})
                    result, was_escalated = await self._handle_escalation_request(
                        result=result,
                        escalation_context=escalation_context,
                        query=processed_query,
                        session_id=session_id,
                        parent_trace=parent_for_spans
                    )
                
                elif needs_replan:
                    agent_type = decision.agent.value
                    result, was_replanned = await self._check_and_replan_if_needed(
                        result=result,
                        agent_type=agent_type,
                        query=processed_query,
                        decision=decision,
                        session_id=session_id,
                        parent_trace=parent_for_spans,
                        already_replanned=False
                    )
            
            # ═══════════════════════════════════════════════════════════
            # STEP 8: SYNTHESIS - Route final result through synthesis layer
            # ═══════════════════════════════════════════════════════════
            # Determine synthesis status from agent result
            synthesis_status = SynthesisStatus.SUCCESS
            
            if result and isinstance(result, dict):
                status_str = result.get("status", "SUCCESS")
                
                # B4: Handle new synthesis statuses
                if was_self_corrected:
                    synthesis_status = SynthesisStatus.SELF_CORRECTED
                elif was_escalated:
                    synthesis_status = SynthesisStatus.ESCALATED
                elif status_str == AgentStatus.FAILED.value or result.get("error"):
                    synthesis_status = SynthesisStatus.FAILED
                elif was_replanned:
                    synthesis_status = SynthesisStatus.REPLANNED
                else:
                    synthesis_status = SynthesisStatus.SUCCESS
            
            # Add routing metadata (exclude raw planner JSON to avoid UI leakage)
            if result:
                routing_meta = {
                    "agent": decision.agent.value,
                    "skill": decision.skill,
                    "confidence": decision.confidence,
                    "reason": decision.reason
                }
                # Propagate any routing params (e.g., requires_ui_form) so the UI can react
                try:
                    if getattr(decision, 'params', None):
                        routing_meta["params"] = decision.params
                        if isinstance(decision.params, dict) and "requires_ui_form" in decision.params:
                            routing_meta["requires_ui_form"] = decision.params.get("requires_ui_form", False)
                except Exception:
                    # Don't let metadata propagation break orchestration
                    pass

                result["_routing"] = routing_meta

            # Build agent_outputs list (only include structured agent outputs)
            agent_outputs: List[Dict[str, Any]] = []

            # Hybrid flow outputs are under 'hybrid_results'
            if isinstance(result, dict) and result.get("hybrid_results"):
                for hr in result.get("hybrid_results", []):
                    step_result = hr.get("result")
                    content = None
                    if isinstance(step_result, dict):
                        content = step_result.get("content")
                    else:
                        content = step_result

                    agent_outputs.append({
                        "agent": hr.get("agent"),
                        "skill": hr.get("skill"),
                        "content": content,
                        "_routing": {"agent": hr.get("agent"), "skill": hr.get("skill")}
                    })
            else:
                # Single-agent result
                content = result.get("content") if isinstance(result, dict) else result
                routing = result.get("_routing") if isinstance(result, dict) else {"agent": decision.agent.value}
                agent_outputs.append({
                    "agent": routing.get("agent") if isinstance(routing, dict) else routing,
                    "skill": routing.get("skill") if isinstance(routing, dict) else None,
                    "content": content,
                    "_routing": routing
                })

            # ═══════════════════════════════════════════════════════════
            # STEP 9: SYNTHESIS SPAN (for observability)
            # ═══════════════════════════════════════════════════════════
            # NOTE: Actual synthesis is handled by agents themselves
            # PMAgent calls summarize_mcp_result() internally after tool execution
            # PMSkillAgent formats results deterministically
            # Orchestrator only passes through agent results without modification
            # This span is for OBSERVABILITY to match the target trace structure
            synthesis_span = create_span(
                name="orchestrator.synthesis",
                input_data={"status": synthesis_status.value, "was_replanned": was_replanned},
                metadata={"step": "synthesis", "pass_through": True},
                parent_trace=parent_for_spans,
                session_id=session_id
            )

            # CRITICAL FIX: Extract and combine content from hybrid_results if present
            # Hybrid flows should combine results from all steps, not return generic message
            if isinstance(result, dict) and result.get("hybrid_results"):
                hybrid_results = result.get("hybrid_results", [])
                combined_contents = []
                
                for hr in hybrid_results:
                    step_result = hr.get("result")
                    step_num = hr.get("step", "?")
                    skill_name = hr.get("skill", "unknown")
                    
                    extracted_content = None
                    
                    if isinstance(step_result, dict):
                        # Try multiple extraction strategies
                        step_content = step_result.get("content")
                        
                        # Strategy 1: Content is a dict with 'result' key (PM Skill Agent format)
                        if isinstance(step_content, dict) and "result" in step_content:
                            extracted_content = step_content.get("result")
                        # Strategy 2: Content is a plain string
                        elif isinstance(step_content, str) and len(step_content) > 20:
                            extracted_content = step_content
                        # Strategy 3: Top-level 'result' key (some skill formats)
                        elif "result" in step_result and isinstance(step_result["result"], str):
                            extracted_content = step_result["result"]
                        # Strategy 4: 'message' key
                        elif "message" in step_result and isinstance(step_result["message"], str):
                            extracted_content = step_result["message"]
                    elif isinstance(step_result, str) and len(step_result) > 20:
                        extracted_content = step_result
                    
                    if extracted_content and len(extracted_content) > 20:
                        combined_contents.append(f"### Step {step_num}: {skill_name}\n{extracted_content}")
                        logger.info(f"[ORCHESTRATOR] Extracted content from hybrid step {step_num}/{skill_name} ({len(extracted_content)} chars)")
                
                # Combine all extracted contents
                if combined_contents:
                    result["content"] = "\n\n---\n\n".join(combined_contents)
                    logger.info(f"[ORCHESTRATOR] Combined {len(combined_contents)} hybrid step results")
            
            # Final safety: ensure `content` is always a user-facing string.
            try:
                content_val = result.get("content") if isinstance(result, dict) else result
                # If content is structured (dict/list), avoid leaking internal planner/tool shapes
                if isinstance(content_val, (dict, list)):
                    # ══════════════════════════════════════════════════════════════════
                    # FIX #8: INTERNAL TOOL RESULT EXPOSURE
                    # Distinguish between PLANNER dicts and RESULT dicts
                    # Planner dict: has "action" + "args" or "tool" + "args" + "confidence"
                    # Result dict: has "tool" + "success" + "items"/"count"
                    # Only suppress planner dicts, not legitimate MCP results
                    # ══════════════════════════════════════════════════════════════════
                    def _is_planner_dict(d):
                        """Check if dict is a PLANNER dict (not a RESULT dict)."""
                        if not isinstance(d, dict):
                            return False
                        # Result dict has success + data fields
                        is_result = "success" in d and ("items" in d or "count" in d or "results" in d)
                        # Planner dict has action + args or tool + args + confidence
                        is_planner = (
                            ("action" in d and "args" in d) or 
                            ("tool" in d and "args" in d and "confidence" in d)
                        )
                        return is_planner and not is_result

                    if _is_planner_dict(content_val) or (isinstance(content_val, list) and any(_is_planner_dict(x) for x in content_val if isinstance(x, dict))):
                        logger.error("[ORCHESTRATOR] Suppressing planner dict from content to avoid UI leakage")
                        result["content"] = "⚠️ The system encountered an internal planning issue. Please try rephrasing your query."
                    elif isinstance(content_val, dict):
                        # Check if content is a skill result with 'result' field that's already formatted
                        # This handles PM Skills Agent which returns {"success": True, "result": "# Report...", ...}
                        if "result" in content_val and isinstance(content_val["result"], str):
                            # Use the formatted result string directly
                            result["content"] = content_val["result"]
                            # Preserve metadata for UI
                            if "metadata" in content_val:
                                result["metadata"] = content_val["metadata"]
                            logger.debug("[ORCHESTRATOR] Extracted 'result' string from content dict")
                        elif "message" in content_val and isinstance(content_val["message"], str):
                            # Fallback to message field if available
                            result["content"] = content_val["message"]
                            logger.debug("[ORCHESTRATOR] Extracted 'message' string from content dict")
                        else:
                            # Safe to stringify structured results for UI (truncate to reasonable size)
                            try:
                                s = json.dumps(content_val, indent=2)
                            except Exception:
                                s = str(content_val)
                            result["content"] = s[:4000]
                    else:
                        # List - stringify it
                        try:
                            s = json.dumps(content_val, indent=2)
                        except Exception:
                            s = str(content_val)
                        result["content"] = s[:4000]
            except Exception:
                # If anything goes wrong, ensure we still return a friendly message
                try:
                    result["content"] = result.get("content", "No response available")
                except Exception:
                    result = {"is_task_complete": True, "content": "No response available"}

            # Finalize synthesis span
            if synthesis_span:
                finalize_span(
                    synthesis_span,
                    output={
                        "status": synthesis_status.value,
                        "content_length": len(str(result.get("content", ""))),
                        "synthesized_output": str(result.get("content", ""))[:1000]  # Include first 1000 chars of output
                    },
                    status="success"
                )

            # Finalize trace or span depending on which was created
            if orchestrator_span:
                # We created a span under controller trace
                finalize_span(
                    orchestrator_span,
                    output={"agent": decision.agent.value, "success": True},
                    status="success"
                )
            elif trace:
                # We created a standalone trace
                finalize_trace(
                    trace,
                    output={"agent": decision.agent.value, "success": True},
                    status="success"
                )
            
            return result
            
        except Exception as e:
            logger.exception(f"Orchestrator process error: {e}")
            
            # Finalize trace or span with error
            if orchestrator_span:
                finalize_span(
                    orchestrator_span,
                    output={"error": str(e)},
                    status="error",
                    level="ERROR"
                )
            elif trace:
                finalize_trace(
                    trace,
                    output={"error": str(e)},
                    status="error",
                    level="ERROR"
                )
            
            return {
                "is_task_complete": True,
                "content": f"Error processing request: {e}",
                "error": str(e)
            }
    
    async def _check_and_replan_if_needed(
        self,
        result: Dict[str, Any],
        agent_type: str,
        query: str,
        decision: RouteDecision,
        session_id: str,
        parent_trace: Any,
        already_replanned: bool = False
    ) -> tuple[Dict[str, Any], bool]:
        """
        Check agent result status and handle NEEDS_DEEP_ANALYSIS.
        
        ═══════════════════════════════════════════════════════════
        ARCHITECTURE CHANGE (2024): Light LLM is now PRIMARY planner.
        Deep LLM no longer calls plan_mcp_call for re-planning.
        
        If agent returns NEEDS_DEEP_ANALYSIS, it means:
        1. The orchestrator's Light LLM plan was incomplete/missing
        2. We should NOT re-plan here (that would defeat the purpose)
        3. Instead, return a clear failure message so issues are visible
        
        The fix is in Step 4: Light LLM should generate complete plans
        BEFORE dispatching to agents, not after they fail.
        ═══════════════════════════════════════════════════════════
        
        Args:
            result: Agent result dict (must have 'status' key)
            agent_type: "pm_agent" or "pm_skill_agent"
            query: Original user query
            decision: Original route decision
            session_id: Session ID
            parent_trace: Parent trace for Langfuse
            already_replanned: Whether we already replanned (prevents infinite loop)
            
        Returns:
            Tuple of (final_result, was_replanned)
        """
        # Convert result to AgentResult for type safety
        agent_result = AgentResult.from_dict(result) if isinstance(result, dict) else AgentResult(
            status=AgentStatus.SUCCESS, content=result
        )
        
        # Check if we need deep analysis
        if agent_result.status != AgentStatus.NEEDS_DEEP_ANALYSIS:
            # No replan needed
            return result, False
        
        # ═══════════════════════════════════════════════════════════
        # ARCHITECTURE CHANGE: Don't re-plan, log the issue clearly
        # This helps identify queries where Light LLM planning failed
        # ═══════════════════════════════════════════════════════════
        deep_context = agent_result.deep_analysis_context or result.get("deep_analysis_context", {})
        
        logger.warning(
            f"[REPLAN] Agent {agent_type} returned NEEDS_DEEP_ANALYSIS. "
            f"This indicates Light LLM planning was incomplete. "
            f"Context: {deep_context}"
        )
        
        # Create span to track this situation for observability
        issue_span = create_span(
            name="orchestrator.planning_incomplete",
            input_data={
                "agent": agent_type, 
                "query": query[:200],
                "deep_context": deep_context
            },
            metadata={
                "step": "planning_incomplete", 
                "layer": "orchestrator",
                "issue": "light_llm_plan_missing"
            },
            parent_trace=parent_trace,
            session_id=session_id
        )
        
        # For now, return a clear message about the issue
        # In production, we could still fallback to Deep LLM if needed
        # But we want to surface these issues to improve Light LLM planning
        
        # Check if we should still fallback to Deep LLM (configurable)
        from config import config
        if getattr(config, 'allow_deep_llm_fallback', True):
            logger.info("[REPLAN] Fallback enabled - invoking Deep LLM for recovery")
            finalize_span(issue_span, output={"action": "fallback_to_deep_llm"}, status="warning")
            
            # Fall back to original Deep LLM planning for compatibility
            return await self._deep_llm_fallback_replan(
                result=result,
                agent_type=agent_type,
                query=query,
                decision=decision,
                session_id=session_id,
                parent_trace=parent_trace,
                deep_context=deep_context
            )
        else:
            # Return failure without Deep LLM fallback
            finalize_span(issue_span, output={"action": "returned_error"}, status="error")
            result["status"] = AgentStatus.FAILED.value
            result["error"] = "Light LLM planning was incomplete. Please try rephrasing your query."
            result["content"] = "I couldn't fully understand your request. Please try rephrasing or be more specific."
            return result, False
    
    async def _deep_llm_fallback_replan(
        self,
        result: Dict[str, Any],
        agent_type: str,
        query: str,
        decision: RouteDecision,
        session_id: str,
        parent_trace: Any,
        deep_context: Dict[str, Any]
    ) -> tuple[Dict[str, Any], bool]:
        """
        Fallback to Deep LLM planning when Light LLM plan is incomplete.
        
        This preserves backward compatibility while we improve Light LLM planning.
        Eventually, this should rarely be called as Light LLM improves.
        """
        logger.info(f"[DEEP_LLM_FALLBACK] Agent {agent_type} needs deep analysis, invoking Deep LLM")
        
        # Create span for deep LLM
        replan_span = create_span(
            name="orchestrator.deep_llm",
            input_data={"agent": agent_type, "query": query[:200]},
            metadata={"step": "deep_llm_fallback", "layer": "orchestrator"},
            parent_trace=parent_trace,
            session_id=session_id
        )
        
        # CRITICAL: Set this span as current context so plan_mcp_call's "llm_planner" span nests correctly
        previous_trace = get_current_trace()
        if replan_span:
            set_current_trace(replan_span)
        
        try:
            # Call Deep LLM (plan_mcp_call from planner.py) to get tool plan
            plan_result = await plan_mcp_call(
                query=query,
                context=deep_context,
                tool_registry=MCP_TOOL_REGISTRY,
                session_id=session_id
            )
            
            if not plan_result:
                logger.warning(f"[DEEP_LLM_FALLBACK] plan_mcp_call returned no plan")
                finalize_span(replan_span, output={"success": False, "error": "No plan returned"}, status="warning")
                # Return original result with failure status
                result["status"] = AgentStatus.FAILED.value
                result["error"] = "Deep LLM returned no plan"
                return result, False
            
            logger.info(f"[DEEP_LLM_FALLBACK] Plan generated: action={plan_result.get('action')}, tool={plan_result.get('tool')}")

            # ═══════════════════════════════════════════════════════════
            # Validate the deep-LLM plan (with retry loop) before re-executing
            # the agent.  If the plan is structurally or semantically invalid
            # the ValidationOrchestrator calls plan_mcp_call again with
            # structured feedback — up to MAX_RETRIES times.
            # CircuitBreakerError is raised only when every attempt fails.
            # ═══════════════════════════════════════════════════════════
            try:
                from agents.pm_agent.result_validator import (
                    get_validation_orchestrator,
                    CircuitBreakerError,
                )
                _vo = get_validation_orchestrator()

                async def _plan_generator(q: str, feedback: dict) -> dict:
                    enhanced_ctx = {**deep_context, "validation_feedback": feedback}
                    return await plan_mcp_call(
                        query=q,
                        context=enhanced_ctx,
                        tool_registry=MCP_TOOL_REGISTRY,
                        session_id=session_id,
                    )

                plan_result = await _vo.validate_with_retry(
                    query=query,
                    initial_plan=plan_result,
                    plan_generator=_plan_generator,
                    session_id=session_id,
                )
                logger.info(
                    f"[DEEP_LLM_FALLBACK] Plan passed validation "
                    f"(tool={plan_result.get('tool') if plan_result else 'none'})"
                )
            except CircuitBreakerError as _cbe:
                logger.error(
                    f"[DEEP_LLM_FALLBACK] Plan validation circuit breaker triggered "
                    f"after {_cbe.attempts} attempt(s). "
                    f"Last feedback: {str(_cbe.last_feedback)[:300]}"
                )
                finalize_span(replan_span, output={"success": False, "error": "circuit_breaker"}, status="error")
                if previous_trace:
                    set_current_trace(previous_trace)
                result["status"] = AgentStatus.FAILED.value
                result["error"] = "Plan validation failed after maximum retries"
                return result, False
            except Exception as _vex:
                # Validation layer unavailable or raised unexpectedly — proceed
                # without validation so the agent can still attempt execution.
                logger.warning(f"[DEEP_LLM_FALLBACK] Plan validation skipped: {_vex}")

            # Check if Deep LLM suggests a different agent for execution
            suggested_agent = (plan_result.get("suggested_agent") or "").lower()
            original_agent_type = agent_type
            
            # Validate and switch agent if suggested agent differs from original
            if suggested_agent and suggested_agent != agent_type:
                # Map suggested agent to AgentType enum values
                agent_map = {
                    "pm_agent": "pm_agent",
                    "pm_skill_agent": "pm_skill_agent",
                }
                
                if suggested_agent in agent_map:
                    agent_type = agent_map[suggested_agent]
                    logger.info(f"[REPLAN] Deep LLM suggests agent switch: {original_agent_type} → {agent_type}")
                    logger.info(f"[REPLAN] Routing reason: {plan_result.get('reasoning_summary', 'Not provided')}")
                else:
                    logger.warning(f"[REPLAN] Deep LLM suggested unknown agent '{suggested_agent}', ignoring and using original agent '{original_agent_type}'")
                    agent_type = original_agent_type
            
            # ═══════════════════════════════════════════════════════════
            # CHANGE 1: Create agent.reexecute span for re-execution visibility
            # This provides clear boundary between deep LLM planning and agent re-execution
            # ═══════════════════════════════════════════════════════════
            agent_reexec_span = create_span(
                name="agent.reexecute",
                input_data={
                    "query": query[:200],
                    "agent_type": agent_type,
                    "has_revised_plan": bool(plan_result),
                    "plan_action": plan_result.get("action") if plan_result else None,
                    "plan_tool": plan_result.get("tool") if plan_result else None,
                    "plan_skill": plan_result.get("skill") if plan_result else None,
                },
                metadata={
                    "agent": agent_type,
                    "layer": "agent_re_execution",
                    "session": session_id,
                    "is_replan": True,
                    "has_parent_trace": parent_trace is not None,
                    "reexec_source": "deep_llm_replan",
                    "original_agent": original_agent_type,
                    "agent_switched": (original_agent_type != agent_type),
                },
                parent_trace=replan_span,
                session_id=session_id
            )
            
            # ═══════════════════════════════════════════════════════════
            # CHANGE 2: Manage trace context for proper span nesting
            # Pattern from synthesizer.py: save → set → execute → restore
            # This ensures tools and synthesis nest under agent.reexecute span
            # ═══════════════════════════════════════════════════════════
            reexec_previous_trace = get_current_trace()
            set_current_trace(agent_reexec_span)
            
            try:
                # Re-execute agent with the plan from Deep LLM
                # Pass agent_reexec_span as parent for correct nesting
                revised_result = await self._re_execute_agent_with_plan(
                    agent_type=agent_type,
                    query=query,
                    revised_plan=plan_result,
                    additional_context=deep_context,
                    decision=decision,
                    session_id=session_id,
                    parent_trace=agent_reexec_span  # Pass re-execution span as parent
                )
                
                # Finalize re-execution span
                finalize_span(agent_reexec_span, output={
                    "success": True,
                    "result_status": revised_result.get("status") if isinstance(revised_result, dict) else "unknown"
                }, status="success")
                
            except Exception as reexec_error:
                # Finalize re-execution span on error before re-raising
                logger.error(f"[REPLAN] Agent re-execution failed: {reexec_error}", exc_info=True)
                finalize_span(agent_reexec_span, output={"error": str(reexec_error)}, status="error", level="ERROR")
                raise  # Re-raise for outer handler to finalize deep_llm span
            
            finally:
                # Restore previous trace context (following synthesizer.py pattern)
                if reexec_previous_trace:
                    set_current_trace(reexec_previous_trace)
            
            # ═══════════════════════════════════════════════════════════
            # CHANGE 3: Log agent switch decision with reasoning
            # Add comprehensive logging and metadata for switch visibility
            # ═══════════════════════════════════════════════════════════
            if original_agent_type != agent_type:
                switch_reasoning = plan_result.get("reasoning_summary", "Not provided") if plan_result else "Plan not available"
                logger.info(
                    f"[REPLAN] Agent switch executed: {original_agent_type} → {agent_type}. "
                    f"Reasoning: {switch_reasoning}"
                )
            
            # Finalize deep_llm span with agent switching info
            finalize_output = {
                "success": True,
                "replanned": True,
                "original_agent": original_agent_type,
                "executed_agent": agent_type,
                "agent_switched": (original_agent_type != agent_type),
                "switch_reasoning": plan_result.get("reasoning_summary") if original_agent_type != agent_type else None,
            }
            finalize_span(replan_span, output=finalize_output, status="success")
            
            # Mark result as replanned
            if isinstance(revised_result, dict):
                revised_result["_replanned"] = True
                revised_result["status"] = AgentStatus.SUCCESS.value if not revised_result.get("error") else AgentStatus.FAILED.value
            
            # Restore previous trace context
            if previous_trace:
                set_current_trace(previous_trace)
            
            return revised_result, True
            
        except Exception as e:
            logger.exception(f"[REPLAN] Error during replan: {e}")
            finalize_span(replan_span, output={"error": str(e)}, status="error", level="ERROR")
            # Restore previous trace context
            if previous_trace:
                set_current_trace(previous_trace)
            # Return original result
            return result, False
    
    async def _re_execute_agent_with_plan(
        self,
        agent_type: str,
        query: str,
        revised_plan: Optional[Dict[str, Any]],
        additional_context: Dict[str, Any],
        decision: RouteDecision,
        session_id: str,
        parent_trace: Any
    ) -> Dict[str, Any]:
        """
        Re-execute an agent with a revised plan from Deep LLM.
        
        Args:
            agent_type: "pm_agent" or "pm_skill_agent"
            query: Query to execute (may be revised)
            revised_plan: Revised plan from Deep LLM
            additional_context: Additional context from Deep LLM
            decision: Original route decision
            session_id: Session ID
            parent_trace: Parent trace for Langfuse
            
        Returns:
            Agent result dict
        """
        result = None
        
        if agent_type == "pm_skill_agent":
            agent = await self.get_pm_skill_agent()
            
            # Use revised skill if provided, else original
            skill = decision.skill
            if revised_plan and "skill" in revised_plan:
                skill = revised_plan["skill"]
            
            invoke_payload = {
                "skill": skill,
                "query": query,
                "params": {**(decision.params or {}), **additional_context},
                "session_id": session_id,
                "parent_trace": parent_trace,
                "_is_replan": True  # Signal to agent that this is a replan
            }
            
            async for response in agent.invoke(invoke_payload):
                result = response
                
        elif agent_type == "pm_agent":
            agent = await self.get_pm_agent()
            
            # Create orchestrator plan from revised plan
            orchestrator_plan = None
            if revised_plan:
                orchestrator_plan = {
                    "route": "pm_agent",
                    **revised_plan,
                    "hints": revised_plan.get("hints", []),
                    "_is_replan": True
                }
            
            async for response in agent.invoke(
                query,
                session_id,
                parent_trace=parent_trace,
                orchestrator_plan=orchestrator_plan
            ):
                result = response
        
        return result or {"is_task_complete": True, "content": "No result from replanned execution"}

    async def _execute_hybrid_flow(self, steps: List[Dict[str, Any]], query: str, session_id: str) -> Dict[str, Any]:
        """
        Execute a hybrid flow (multi-agent sequence).
        
        Args:
            steps: List of steps to execute
            query: Original query
            session_id: Session ID
            
        Returns:
            Combined result from all steps
        """
        logger.info(f"Executing hybrid flow with {len(steps)} steps")
        
        # Create span for hybrid flow
        # Create hybrid span under the current orchestrator trace/span if available
        hybrid_span = create_span(
            name="orchestrator.hybrid_flow",
            input_data={"steps": len(steps), "query": query[:100]},
            metadata={"flow": "hybrid"},
            parent_trace=get_current_trace()
        )
        
        all_results = []
        context = {"query": query}
        
        try:
            for i, step in enumerate(steps):
                agent_name = step.get("agent")
                skill = step.get("skill")
                action = step.get("action")
                
                logger.info(f"Hybrid step {i+1}/{len(steps)}: agent={agent_name}, skill={skill}, action={action}")
                
                step_result = None
                
                if agent_name == "pm_agent":
                    agent = await self.get_pm_agent()
                    
                    # First attempt: invoke without plan (agent may return NEEDS_DEEP_ANALYSIS)
                    async for response in agent.invoke(query, session_id, parent_trace=get_current_trace()):
                        step_result = response
                    
                    # Check if agent needs Deep LLM planning
                    if step_result and isinstance(step_result, dict):
                        status_str = step_result.get("status", "")
                        if status_str == AgentStatus.NEEDS_DEEP_ANALYSIS.value:
                            logger.info(f"[HYBRID_FLOW] Step {i+1} needs Deep LLM, invoking planner")
                            
                            # Get deep analysis context
                            deep_context = step_result.get("deep_analysis_context", {})
                            
                            # Create span for deep LLM planning
                            deep_llm_span = create_span(
                                name="orchestrator.deep_llm",
                                input_data={"step": i+1, "query": query[:200]},
                                metadata={"hybrid_step": True, "layer": "orchestrator"},
                                parent_trace=hybrid_span,
                                session_id=session_id
                            )
                            
                            # Save and set trace context
                            previous_trace = get_current_trace()
                            set_current_trace(deep_llm_span)
                            
                            try:
                                # Call Deep LLM to get plan
                                plan_result = await plan_mcp_call(
                                    query=query,
                                    context=deep_context,
                                    tool_registry=MCP_TOOL_REGISTRY,
                                    session_id=session_id
                                )
                                
                                if plan_result:
                                    logger.info(f"[HYBRID_FLOW] Deep LLM provided plan: action={plan_result.get('action')}, tool={plan_result.get('tool')}")
                                    
                                    # Re-invoke agent with plan
                                    async for response in agent.invoke(
                                        query,
                                        session_id,
                                        parent_trace=deep_llm_span,
                                        orchestrator_plan=plan_result
                                    ):
                                        step_result = response
                                    
                                    finalize_span(deep_llm_span, output={"success": True}, status="success")
                                else:
                                    logger.warning(f"[HYBRID_FLOW] Deep LLM returned no plan for step {i+1}")
                                    finalize_span(deep_llm_span, output={"error": "No plan"}, status="warning")
                            finally:
                                # Restore trace context
                                set_current_trace(previous_trace)
                        
                elif agent_name == "pm_skill_agent":
                    agent = await self.get_pm_skill_agent()
                    # Include data from previous steps
                    params = step.get("params", {})
                    params["_prior_context"] = context
                    async for response in agent.invoke({
                        "skill": skill,
                        "params": params,
                        "session_id": session_id,
                        "parent_trace": get_current_trace()  # Pass current orchestrator trace/span
                    }):
                        step_result = response
                
                if step_result:
                    all_results.append({
                        "step": i + 1,
                        "agent": agent_name,
                        "skill": skill,
                        "result": step_result
                    })
                    # Update context with result
                    context[f"step_{i+1}"] = step_result.get("content") if isinstance(step_result, dict) else step_result
            
            # Combine results
            combined = {
                "is_task_complete": True,
                "content": f"Completed hybrid flow with {len(steps)} steps",
                "hybrid_results": all_results
            }
            
            finalize_span(hybrid_span, output={"steps_completed": len(all_results)}, status="success")
            return combined
            
        except Exception as e:
            logger.exception(f"Hybrid flow error: {e}")
            finalize_span(hybrid_span, output={"error": str(e)}, status="error", level="ERROR")
            return {
                "is_task_complete": True,
                "content": f"Error in hybrid flow: {e}",
                "error": str(e)
            }

    # ═══════════════════════════════════════════════════════════════════════════
    # B4: SELF-CORRECTION HANDLERS
    # ═══════════════════════════════════════════════════════════════════════════
    
    async def _handle_replan_request(
        self,
        result: Dict[str, Any],
        replan_context: Dict[str, Any],
        query: str,
        decision: RouteDecision,
        session_id: str,
        parent_trace: Any
    ) -> tuple[Dict[str, Any], bool]:
        """
        Handle REQUEST_REPLAN status from agent self-correction.
        
        This differs from NEEDS_DEEP_ANALYSIS in that the agent has already
        analyzed the failure and is providing specific guidance for replanning.
        
        Args:
            result: Original result with replan_context
            replan_context: Context from agent's failure analysis
            query: Original user query
            decision: Original route decision
            session_id: Session ID
            parent_trace: Parent trace for Langfuse
            
        Returns:
            Tuple of (new_result, was_replanned)
        """
        logger.info(f"[SELF_CORRECTION] Handling replan request: {replan_context}")
        
        # Extract guidance from replan context
        exclude_tools = replan_context.get("exclude_tools", [])
        suggest_tools = replan_context.get("suggest_tools", [])
        failed_tool = replan_context.get("failed_tool")
        
        # Create enhanced context for replanning
        enhanced_context = {
            "self_correction": True,
            "failed_tool": failed_tool,
            "exclude_tools": exclude_tools,
            "prefer_tools": suggest_tools,
            "failure_reason": replan_context.get("reason", "unknown"),
            "original_error": replan_context.get("error", "")[:500]
        }
        
        # Create span for self-correction replan
        replan_span = create_span(
            name="orchestrator.self_correction",
            input_data={"query": query[:200], "failed_tool": failed_tool},
            metadata={"step": "self_correction", "exclude": exclude_tools, "suggest": suggest_tools},
            parent_trace=parent_trace,
            session_id=session_id
        )
        
        try:
            # Call Deep LLM with self-correction context
            plan_result = await plan_mcp_call(
                query=query,
                context=enhanced_context,
                tool_registry=MCP_TOOL_REGISTRY,
                session_id=session_id
            )
            
            if not plan_result:
                logger.warning("[SELF_CORRECTION] Deep LLM returned no alternative plan")
                finalize_span(replan_span, output={"success": False}, status="warning")
                return result, False
            
            # Re-execute agent with new plan
            agent = await self.get_pm_agent()
            orchestrator_plan = {
                "route": "pm_agent",
                **plan_result,
                "_is_self_correction": True
            }
            
            new_result = None
            async for response in agent.invoke(
                query,
                session_id,
                parent_trace=replan_span,
                orchestrator_plan=orchestrator_plan
            ):
                new_result = response
            
            if new_result:
                new_result["_self_corrected"] = True
                finalize_span(replan_span, output={"success": True, "new_tool": plan_result.get("tool")}, status="success")
                return new_result, True
            
            finalize_span(replan_span, output={"success": False}, status="warning")
            return result, False
            
        except Exception as e:
            logger.exception(f"[SELF_CORRECTION] Replan failed: {e}")
            finalize_span(replan_span, output={"error": str(e)}, status="error", level="ERROR")
            return result, False
    
    async def _handle_escalation_request(
        self,
        result: Dict[str, Any],
        escalation_context: Dict[str, Any],
        query: str,
        session_id: str,
        parent_trace: Any
    ) -> tuple[Dict[str, Any], bool]:
        """
        Handle REQUEST_ESCALATION status - route to a different agent.
        
        Args:
            result: Original result with escalation_context
            escalation_context: Context specifying target agent
            query: Original user query
            session_id: Session ID
            parent_trace: Parent trace for Langfuse
            
        Returns:
            Tuple of (new_result, was_escalated)
        """
        target_agent = escalation_context.get("target_agent", "pm_skill_agent")
        reason = escalation_context.get("reason", "unknown")
        
        logger.info(f"[ESCALATION] Escalating to {target_agent}: {reason}")
        
        # Create span for escalation
        escalation_span = create_span(
            name="orchestrator.escalation",
            input_data={"query": query[:200], "target": target_agent},
            metadata={"step": "escalation", "reason": reason},
            parent_trace=parent_trace,
            session_id=session_id
        )
        
        try:
            if target_agent == "pm_skill_agent":
                agent = await self.get_pm_skill_agent()
                
                # Try to determine appropriate skill based on query
                # This uses the router's skill matching logic
                decision = await self.router.route(query, session_id)
                skill = decision.skill if decision.agent == AgentType.PM_SKILL_AGENT else None
                
                invoke_payload = {
                    "skill": skill,
                    "query": query,
                    "params": {"_escalated_from": "pm_agent", "_escalation_reason": reason},
                    "session_id": session_id,
                    "parent_trace": escalation_span
                }
                
                new_result = None
                async for response in agent.invoke(invoke_payload):
                    new_result = response
                
                if new_result:
                    new_result["_escalated"] = True
                    new_result["_escalated_from"] = "pm_agent"
                    finalize_span(escalation_span, output={"success": True, "skill": skill}, status="success")
                    return new_result, True
            
            elif target_agent == "pm_agent":
                # Escalate from skill agent to PM agent (less common)
                agent = await self.get_pm_agent()
                
                new_result = None
                async for response in agent.invoke(query, session_id, parent_trace=escalation_span):
                    new_result = response
                
                if new_result:
                    new_result["_escalated"] = True
                    new_result["_escalated_from"] = "pm_skill_agent"
                    finalize_span(escalation_span, output={"success": True}, status="success")
                    return new_result, True
            
            logger.warning(f"[ESCALATION] Unknown target agent: {target_agent}")
            finalize_span(escalation_span, output={"success": False, "error": "unknown_target"}, status="warning")
            return result, False
            
        except Exception as e:
            logger.exception(f"[ESCALATION] Failed: {e}")
            finalize_span(escalation_span, output={"error": str(e)}, status="error", level="ERROR")
            return result, False


# ============================================================================
# HELPER FUNCTIONS
# ============================================================================

def _prepare_light_llm_context(
    routing_context: Dict[str, Any],
    resolver_result,
    state_manager
) -> Dict[str, Any]:
    """
    Build a clean, grounded context for Light LLM.
    
    CRITICAL: Do NOT forward resolver defensive flags like missing_team/missing_project.
    These flags poison the LLM into "can't plan" mode.
    
    Instead, provide:
    - Grounded defaults (project, current iteration)
    - Discovery results (known teams, area paths)
    - Neutral hints (semantic skill/score)
    
    This allows the LLM to:
    - Extract team from query text
    - Default to @CurrentIteration
    - Make its own decision about clarification
    """
    from config import config
    
    # Defensive flags to NEVER pass to LLM (these poison planning)
    POISONING_FLAGS = {
        "missing_team", "missing_project", "missing_iteration",
        "requires_clarification", "resolver_failed", "routing_attempts"
    }
    
    # Start with base routing context, filtering out poisoning flags
    clean_context = {
        k: v for k, v in routing_context.items() 
        if k not in POISONING_FLAGS and v is not None
    }
    
    # Add grounded defaults
    if "project" not in clean_context or not clean_context.get("project"):
        clean_context["project"] = getattr(config, "pm_default_project", "FracPro-OPS")
    
    # Add discovery results from state manager if available
    state_context = {}
    if hasattr(state_manager, 'get_all_context'):
        state_context = state_manager.get_all_context() or {}
    elif hasattr(state_manager, 'get_context'):
        state_context = state_manager.get_context() or {}
    
    # Grounding data from state manager
    if "area_paths" not in clean_context and state_context.get("area_paths"):
        clean_context["area_paths"] = state_context["area_paths"]
    if "known_teams" not in clean_context and state_context.get("known_teams"):
        clean_context["known_teams"] = state_context["known_teams"]
    if "current_iteration" not in clean_context and state_context.get("current_iteration"):
        clean_context["current_iteration"] = state_context["current_iteration"]
    
    # Add iteration fallback for @CurrentIteration support
    if "iteration" not in clean_context:
        clean_context["iteration"] = "@CurrentIteration"
    
    # Add NEUTRAL hints (what failed, but not defensive flags)
    clean_context["regex_failed"] = True
    clean_context["semantic_failed"] = True
    if resolver_result:
        clean_context["semantic_best_skill"] = resolver_result.skill_hint
        clean_context["semantic_best_score"] = resolver_result.confidence
    
    logger.debug(f"[ROUTER] Prepared clean context for Light LLM: project={clean_context.get('project')}, "
                 f"team={clean_context.get('team')}, keys={list(clean_context.keys())}")
    
    return clean_context


# ==============================================================================
# GENERIC PLACEHOLDER DETECTION PATTERNS (for _plan_is_executable)
# ==============================================================================
# These are compiled regex patterns for detecting placeholder values in plans.
# They are designed to be GENERIC and not tied to any specific tool or query.
# ==============================================================================

# Pattern 1: Angle-bracket placeholders like <query_id>, <ID of saved query>, <user_name>
_ANGLE_BRACKET_PLACEHOLDER_PATTERN = re.compile(r'<[^>]+>')

# Pattern 2: Square-bracket placeholders like [PROJECT_NAME], [UNKNOWN], [from context]
_SQUARE_BRACKET_PLACEHOLDER_PATTERN = re.compile(r'\[[A-Z_\s]+\]|\[from\s+\w+\]', re.IGNORECASE)

# Pattern 3: Step references like "$step_1.result", "from previous step", "result of step"
_STEP_REFERENCE_PATTERN = re.compile(
    r'\$step_\d+|from\s+(previous|prior|earlier|step\s*\d+)|result\s+of\s+step|'
    r'output\s+of\s+step|use\s+result|TBD|TO_BE_FILLED|TO_FILL|PLACEHOLDER',
    re.IGNORECASE
)

# Pattern 4: Common placeholder keywords (case-insensitive)
_PLACEHOLDER_KEYWORDS = frozenset({
    'unknown', 'undefined', 'null', 'none', 'tbd', 'todo', 'fixme',
    'placeholder', 'to_be_filled', 'needs_value', 'required',
    'current_iteration_id', 'current_iteration', 
})


def _plan_is_executable(plan: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """
    Check if a plan is executable (has no placeholders, all required args filled).
    
    This function uses GENERIC pattern matching (not hardcoded to specific tools)
    to detect placeholder values that indicate the plan cannot be executed.
    
    Detection categories:
    1. Angle-bracket placeholders: <query_id>, <user_name>, <ID of saved query>
    2. Square-bracket placeholders: [PROJECT_NAME], [UNKNOWN], [from context]
    3. Step references: $step_1.result, "from previous step", "result of step"
    4. Placeholder keywords: unknown, undefined, TBD, PLACEHOLDER
    5. Empty/None values
    
    Returns:
        (is_executable, diagnostics)
        - is_executable: True if plan can be executed as-is
        - diagnostics: List of issues found (empty if executable)
    """
    diagnostics = []
    
    logger.debug("[ROUTER] _plan_is_executable: Starting validation")
    
    # Check plan structure
    if not plan or not isinstance(plan, dict):
        logger.warning("[ROUTER] _plan_is_executable: Plan is missing or not a dict")
        return False, ["plan-missing-or-not-dict"]
    
    # Extract steps - handle nested structure (plan may contain {"plan": {...}})
    plan_data = plan.get("plan", plan)  # Handle nested or flat structure
    if isinstance(plan_data, dict):
        steps = plan_data.get("steps", [])
    else:
        steps = []
    
    if not steps:
        # Check if this is a clarification request (valid case for no steps)
        if plan.get("needs_clarification"):
            logger.debug("[ROUTER] _plan_is_executable: Clarification request detected")
            return True, ["clarification-request"]
        logger.warning("[ROUTER] _plan_is_executable: No steps found in plan")
        return False, ["no-steps"]
    
    # Explicit placeholder markers (exact matches, case-sensitive for some)
    PLACEHOLDER_MARKERS = {
        "unknown", "Unknown", "<unknown>", "UNKNOWN",
        "current_iteration_id", "CURRENT_ITERATION",
        "<from context>", "[from context]",
        "<team_name>", "<project_name>",
        "<project>", "<team>", "<iteration>",
        "TBD", "TODO", "FIXME", "PLACEHOLDER",
        "", None
    }
    
    def _is_placeholder_value(value: str, arg_name: str = "") -> Tuple[bool, str]:
        """
        Generic placeholder detection for any string value.
        
        Args:
            value: The string value to check
            arg_name: Name of the argument (for logging context)
            
        Returns:
            (is_placeholder, reason): Tuple of detection result and reason
        """
        if not value:
            return True, "empty"
        
        value_stripped = value.strip()
        value_lower = value_stripped.lower()
        
        # Check 1: Exact placeholder marker match
        if value_stripped in PLACEHOLDER_MARKERS:
            return True, "exact-marker"
        
        # Check 2: Angle-bracket placeholder pattern <...>
        # BUT exclude valid macros like @CurrentIteration and @Me
        if _ANGLE_BRACKET_PLACEHOLDER_PATTERN.search(value_stripped):
            # These angle-bracket patterns are placeholders
            return True, "angle-bracket-placeholder"
        
        # Check 3: Square-bracket placeholder pattern [...]
        if _SQUARE_BRACKET_PLACEHOLDER_PATTERN.search(value_stripped):
            return True, "square-bracket-placeholder"
        
        # Check 4: Step reference pattern (indicates dependency on previous step)
        if _STEP_REFERENCE_PATTERN.search(value_stripped):
            return True, "step-reference"
        
        # Check 5: Placeholder keyword check
        if value_lower in _PLACEHOLDER_KEYWORDS:
            return True, "keyword-match"
        
        # Check 6: Partial keyword check (e.g., "unknown user" contains "unknown")
        for keyword in ['unknown', 'undefined', 'placeholder', 'tbd']:
            if keyword in value_lower and len(value_stripped) < 50:  # Short strings only
                return True, f"contains-{keyword}"
        
        return False, ""
    
    # Check each step for placeholders
    for idx, step in enumerate(steps):
        if not isinstance(step, dict):
            diagnostics.append(f"step[{idx}]-not-dict")
            logger.warning(f"[ROUTER] _plan_is_executable: step[{idx}] is not a dict")
            continue
        
        # Check action and tool
        action = step.get("action")
        tool = step.get("tool")
        step_id = step.get("step_id", idx + 1)
        
        if not action:
            diagnostics.append(f"step[{idx}].action-missing")
            logger.warning(f"[ROUTER] _plan_is_executable: step[{idx}] missing action")
        elif action not in ("call_tool", "call_skill", "synthesize"):
            # Check if action is actually a tool name (common LLM mistake)
            diagnostics.append(f"step[{idx}].action='{action}'(should-be-call_tool)")
            logger.warning(f"[ROUTER] _plan_is_executable: step[{idx}] has invalid action '{action}'")
        
        if action == "call_tool" and not tool:
            diagnostics.append(f"step[{idx}].tool-missing")
            logger.warning(f"[ROUTER] _plan_is_executable: step[{idx}] call_tool but no tool specified")
        
        # Check args for placeholders
        args = step.get("args", {})
        if not isinstance(args, dict):
            diagnostics.append(f"step[{idx}].args-not-dict")
            logger.warning(f"[ROUTER] _plan_is_executable: step[{idx}] args is not a dict")
            continue
        
        for key, value in args.items():
            # Check for None values
            if value is None:
                diagnostics.append(f"step[{idx}].args.{key}=None")
                logger.debug(f"[ROUTER] _plan_is_executable: step[{idx}].args.{key}=None (placeholder)")
            # Check for placeholder strings
            elif isinstance(value, str):
                is_placeholder, reason = _is_placeholder_value(value, key)
                if is_placeholder:
                    diagnostics.append(f"step[{idx}].args.{key}='{value[:50]}'({reason})")
                    logger.info(f"[ROUTER] _plan_is_executable: PLACEHOLDER DETECTED step[{idx}].args.{key}='{value[:50]}' reason={reason}")
            # Check for lists with placeholder values
            elif isinstance(value, list):
                for i, item in enumerate(value):
                    if isinstance(item, str):
                        is_placeholder, reason = _is_placeholder_value(item, f"{key}[{i}]")
                        if is_placeholder:
                            diagnostics.append(f"step[{idx}].args.{key}[{i}]='{item[:50]}'({reason})")
                            logger.info(f"[ROUTER] _plan_is_executable: PLACEHOLDER DETECTED step[{idx}].args.{key}[{i}]='{item[:50]}' reason={reason}")
    
    is_executable = len(diagnostics) == 0
    
    if is_executable:
        logger.debug(f"[ROUTER] _plan_is_executable: Plan IS executable ({len(steps)} steps)")
    else:
        logger.warning(f"[ROUTER] _plan_is_executable: Plan NOT executable - {len(diagnostics)} issues: {diagnostics[:5]}")
    
    return is_executable, diagnostics


# Singleton instances
_router: Optional[Router] = None
_orchestrator: Optional[Orchestrator] = None


def get_router() -> Router:
    """Get the global router instance."""
    global _router
    if _router is None:
        _router = Router()
    return _router


def get_orchestrator() -> Orchestrator:
    """Get the global orchestrator instance."""
    global _orchestrator
    if _orchestrator is None:
        _orchestrator = Orchestrator()
    return _orchestrator
