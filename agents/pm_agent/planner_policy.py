"""
Planner Policy Engine for PM Agent.

This module provides deterministic post-processing of LLM-generated plans
to ensure correct tool selection for all query types. It acts as a safety
net and override layer that catches common LLM mistakes and applies
project-specific business rules.

CRITICAL: This module runs AFTER the LLM planner but BEFORE execution,
allowing us to fix tool selection errors before they cause incorrect results.

Design Principles:
1. Evidence-based: Only override when we have clear signal from the query
2. Transparent: Log all overrides with reasoning
3. Conservative: Prefer asking clarification over guessing
4. Deterministic: Same input always produces same output
"""

import re
from typing import Dict, Any, Optional, Tuple, List
from dataclasses import dataclass
from utilities.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class PolicyResult:
    """Result of applying planner policies."""
    plan: Dict[str, Any]
    was_modified: bool = False
    modifications: List[str] = None
    reasoning_summary: str = ""
    execution_path: str = "llm-as-is"
    
    def __post_init__(self):
        if self.modifications is None:
            self.modifications = []


class QueryIntent:
    """Detected intent from user query."""
    
    # Intent categories
    SPECIFIC_WORK_ITEM = "specific_work_item"
    CURRENT_SPRINT_ITEMS = "current_sprint_items"
    ITERATION_REPORT = "iteration_report"
    PERSON_ASSIGNED = "person_assigned"
    IDENTITY_LOOKUP = "identity_lookup"
    WORK_ITEM_SEARCH = "work_item_search"
    LIST_PROJECTS = "list_projects"
    LIST_TEAMS = "list_teams"
    LIST_ITERATIONS = "list_iterations"
    LIST_REPOS = "list_repos"
    LIST_AREA_PATHS = "list_area_paths"
    CODE_SEARCH = "code_search"
    PIPELINE_QUERY = "pipeline_query"
    GENERAL_QUESTION = "general_question"
    AMBIGUOUS = "ambiguous"


class PlannerPolicy:
    """
    Deterministic policy engine that validates and corrects LLM plans.
    
    This engine applies business rules to ensure:
    1. Sprint/iteration queries use the correct iteration-aware tools
    2. Work item ID queries use wit_get_work_item
    3. Person-scoped queries preserve assignedTo filters
    4. Ambiguous queries trigger clarification requests
    """
    
    # ══════════════════════════════════════════════════════════════
    # PATTERN DEFINITIONS
    # ══════════════════════════════════════════════════════════════
    
    # Sprint/Iteration patterns - MUST use iteration-aware tools
    SPRINT_PATTERNS = [
        r'\b(current|this|my)\s*(sprint|iteration)\b',
        r'\b(sprint|iteration)\s*\d+\b',
        r'\bsprint\s*\d+\.\d+\b',  # e.g., sprint 25.25
        r'\b@currentiteration\b',
        r'\bin\s*(this|current|my)\s*(sprint|iteration)\b',
        r'\b(active|ongoing)\s*sprint\b',
    ]
    
    # Work item ID patterns - MUST use wit_get_work_item
    WORK_ITEM_ID_PATTERNS = [
        r'\b(?:work\s*item|bug|story|task|feature|epic|item)?\s*#?(\d{4,6})\b',
        r'\bdetails?\s+(?:of|for|about)?\s*#?(\d{4,6})\b',
        r'\bget\s+#?(\d{4,6})\b',
        r'\bshow\s+(?:me\s+)?#?(\d{4,6})\b',
        r'^#?(\d{4,6})$',  # Just an ID
    ]
    
    # Identity lookup patterns - use core_get_identity_ids
    IDENTITY_LOOKUP_PATTERNS = [
        r'\bemail\s*(?:id|address)?\s*(?:of|for)\s+(.+)',
        r'\bwhat\s*(?:is)?\s*(?:the)?\s*email\s*(?:of|for)\s+(.+)',
        r'\bidentity\s*(?:of|for)\s+(.+)',
        r'\bwho\s+is\s+(.+)',
    ]
    
    # Person assignment patterns
    PERSON_PATTERNS = [
        r'\bassigned\s+to\s+([a-zA-Z][a-zA-Z\s\.@]+)',
        r'\bby\s+([a-zA-Z][a-zA-Z\s\.@]+)',
        r'\bfor\s+([a-zA-Z][a-zA-Z\s\.@]+)',
        r'\bof\s+([a-zA-Z][a-zA-Z\s\.@]+)\s*(?:$|in|with)',
    ]
    
    # List/enumeration patterns
    LIST_PROJECTS_PATTERNS = [
        r'^(?:list|show|get)\s+(?:all\s+)?(?:the\s+)?(?:ado\s+)?projects?$',
        r'^what\s+(?:are\s+)?(?:the\s+)?projects?\s*\??$',
    ]
    
    LIST_TEAMS_PATTERNS = [
        r'^(?:list|show|get)\s+(?:all\s+)?(?:the\s+)?teams?$',
        r'^what\s+(?:are\s+)?(?:the\s+)?teams?\s*\??$',
    ]
    
    LIST_ITERATIONS_PATTERNS = [
        r'^(?:list|show|get)\s+(?:all\s+)?(?:the\s+)?(?:iterations?|sprints?)$',
        r'^what\s+(?:are\s+)?(?:the\s+)?(?:iterations?|sprints?)\s*\??$',
    ]
    
    LIST_REPOS_PATTERNS = [
        r'^(?:list|show|get)\s+(?:all\s+)?(?:the\s+)?(?:git\s+)?repositor(?:y|ies)$',
        r'^(?:list|show|get)\s+(?:all\s+)?(?:the\s+)?repos?$',
    ]
    
    LIST_AREA_PATHS_PATTERNS = [
        r'^(?:list|show|get)\s+(?:all\s+)?(?:the\s+)?area\s*paths?$',
        r'^what\s+(?:are\s+)?(?:the\s+)?area\s*paths?\s*\??$',
    ]
    
    # Iteration report patterns
    ITERATION_REPORT_PATTERNS = [
        r'\b(?:iteration|sprint)\s*report\b',
        r'\breport\s*(?:for|of)\s*(?:this|current|my)?\s*(?:sprint|iteration)\b',
        r'\bstatus\s*(?:of|for)\s*(?:this|current|my)?\s*(?:sprint|iteration)\b',
    ]
    
    def __init__(self, context: Dict[str, Any] = None):
        """
        Initialize policy engine with project context.
        
        Args:
            context: Dict with project, team, iteration, etc.
        """
        self.context = context or {}
        self.project = context.get('project', 'FracPro-OPS')
        self.team = context.get('team', self.project)
        self.iteration = context.get('iteration', '@CurrentIteration')
        
        # Compile patterns for performance
        self._sprint_re = [re.compile(p, re.IGNORECASE) for p in self.SPRINT_PATTERNS]
        self._id_re = [re.compile(p, re.IGNORECASE) for p in self.WORK_ITEM_ID_PATTERNS]
        self._identity_re = [re.compile(p, re.IGNORECASE) for p in self.IDENTITY_LOOKUP_PATTERNS]
        self._person_re = [re.compile(p, re.IGNORECASE) for p in self.PERSON_PATTERNS]
        self._list_projects_re = [re.compile(p, re.IGNORECASE) for p in self.LIST_PROJECTS_PATTERNS]
        self._list_teams_re = [re.compile(p, re.IGNORECASE) for p in self.LIST_TEAMS_PATTERNS]
        self._list_iterations_re = [re.compile(p, re.IGNORECASE) for p in self.LIST_ITERATIONS_PATTERNS]
        self._list_repos_re = [re.compile(p, re.IGNORECASE) for p in self.LIST_REPOS_PATTERNS]
        self._list_area_paths_re = [re.compile(p, re.IGNORECASE) for p in self.LIST_AREA_PATHS_PATTERNS]
        self._iteration_report_re = [re.compile(p, re.IGNORECASE) for p in self.ITERATION_REPORT_PATTERNS]
    
    def detect_intent(self, query: str) -> Tuple[str, Dict[str, Any]]:
        """
        Detect the primary intent from the user query.
        
        Args:
            query: User's natural language query
            
        Returns:
            Tuple of (intent_type, extracted_params)
        """
        q = query.strip()
        q_lower = q.lower()
        
        # 1. Check for specific work item ID
        for pattern in self._id_re:
            match = pattern.search(q)
            if match:
                work_item_id = int(match.group(1))
                return QueryIntent.SPECIFIC_WORK_ITEM, {"id": work_item_id}
        
        # 2. Check for iteration report request
        for pattern in self._iteration_report_re:
            if pattern.search(q):
                return QueryIntent.ITERATION_REPORT, {}
        
        # 3. Check for current sprint/iteration scope
        for pattern in self._sprint_re:
            if pattern.search(q):
                return QueryIntent.CURRENT_SPRINT_ITEMS, {}
        
        # 4. Check for identity/email lookup
        for pattern in self._identity_re:
            match = pattern.search(q)
            if match:
                person = match.group(1).strip()
                return QueryIntent.IDENTITY_LOOKUP, {"person": person}
        
        # 5. Check for list commands
        for pattern in self._list_projects_re:
            if pattern.match(q_lower):
                return QueryIntent.LIST_PROJECTS, {}
        
        for pattern in self._list_teams_re:
            if pattern.match(q_lower):
                return QueryIntent.LIST_TEAMS, {}
        
        for pattern in self._list_iterations_re:
            if pattern.match(q_lower):
                return QueryIntent.LIST_ITERATIONS, {}
        
        for pattern in self._list_repos_re:
            if pattern.match(q_lower):
                return QueryIntent.LIST_REPOS, {}
        
        for pattern in self._list_area_paths_re:
            if pattern.match(q_lower):
                return QueryIntent.LIST_AREA_PATHS, {}
        
        # 6. Check for person-scoped queries
        for pattern in self._person_re:
            match = pattern.search(q)
            if match:
                person = match.group(1).strip()
                return QueryIntent.PERSON_ASSIGNED, {"person": person}
        
        # 7. Check for general work item search keywords
        work_item_keywords = ['bug', 'bugs', 'story', 'stories', 'task', 'tasks', 
                             'feature', 'features', 'epic', 'epics', 'item', 'items',
                             'work item', 'workitem']
        if any(kw in q_lower for kw in work_item_keywords):
            return QueryIntent.WORK_ITEM_SEARCH, {}
        
        # 8. Default to ambiguous
        return QueryIntent.AMBIGUOUS, {}
    
    def apply(self, query: str, plan: Dict[str, Any]) -> PolicyResult:
        """
        Apply policies to an LLM-generated plan.
        
        This is the main entry point. It:
        1. Detects user intent from query
        2. Validates LLM plan matches intent
        3. Overrides tool selection if needed
        4. Returns corrected plan with transparency metadata
        
        Args:
            query: Original user query
            plan: LLM-generated plan with action, tool, args, confidence
            
        Returns:
            PolicyResult with (possibly modified) plan and metadata
        """
        if not plan:
            return PolicyResult(
                plan={"action": "ask_clarification", "message": "Could not generate a plan for this request."},
                was_modified=True,
                modifications=["No plan generated"],
                reasoning_summary="LLM returned empty plan",
                execution_path="policy-fallback"
            )
        
        action = plan.get("action", "")
        
        # If LLM already asked for clarification or said no tool, pass through
        if action in ("ask_clarification", "no_tool"):
            return PolicyResult(
                plan=plan,
                was_modified=False,
                reasoning_summary=plan.get("reasoning_summary", "LLM decision"),
                execution_path="llm-clarification"
            )
        
        # Detect intent from query
        intent, params = self.detect_intent(query)
        logger.debug(f"[POLICY] Detected intent: {intent}, params: {params}")
        
        tool = plan.get("tool", "")
        args = dict(plan.get("args", {}) or {})
        confidence = plan.get("confidence", 0.8)
        modifications = []
        
        # ══════════════════════════════════════════════════════════════
        # POLICY RULE 1: Specific Work Item ID → wit_get_work_item
        # ══════════════════════════════════════════════════════════════
        if intent == QueryIntent.SPECIFIC_WORK_ITEM:
            work_item_id = params.get("id")
            if tool != "wit_get_work_item":
                logger.info(f"[POLICY] Overriding {tool} → wit_get_work_item for work item ID {work_item_id}")
                tool = "wit_get_work_item"
                args = {"id": work_item_id, "project": self.project}
                confidence = 0.99
                modifications.append(f"Changed tool to wit_get_work_item for ID {work_item_id}")
        
        # ══════════════════════════════════════════════════════════════
        # POLICY RULE 2: Current Sprint Items → wit_get_work_items_for_iteration
        # ONLY override if LLM chose a completely wrong tool (e.g., repo tools)
        # Do NOT override search_workitem or execute_wiql as they may be
        # intentionally chosen for specific filters (assignedTo, state, etc.)
        # ══════════════════════════════════════════════════════════════
        if intent == QueryIntent.CURRENT_SPRINT_ITEMS:
            # Tools that are appropriate for sprint-scoped queries
            valid_sprint_tools = (
                "wit_get_work_items_for_iteration", 
                "work_get_iteration_work_items", 
                "iteration_report",
                "search_workitem",       # Valid - can filter by assignedTo, state, etc.
                "execute_wiql",          # Valid - flexible WIQL with iteration filter
            )
            
            # Only override if LLM chose a completely irrelevant tool
            if tool and tool not in valid_sprint_tools:
                logger.info(f"[POLICY] Overriding {tool} → wit_get_work_items_for_iteration for sprint query")
                tool = "wit_get_work_items_for_iteration"
                # Preserve state/type filters from original plan if present
                preserved_args = {}
                if args.get("state"):
                    preserved_args["state"] = args.get("state")
                if args.get("workItemType"):
                    preserved_args["workItemType"] = args.get("workItemType")
                if args.get("assignedTo"):
                    preserved_args["assignedTo"] = args.get("assignedTo")
                    
                args = {
                    "project": args.get("project") or self.project,
                    "team": args.get("team") or self.team,
                    "iterationId": self.iteration,
                    **preserved_args  # Include any preserved filters
                }
                modifications.append(f"Changed tool to wit_get_work_items_for_iteration for sprint scoping")
                confidence = 0.90
        
        # ══════════════════════════════════════════════════════════════
        # POLICY RULE 3: Iteration Report → iteration_report skill
        # ══════════════════════════════════════════════════════════════
        if intent == QueryIntent.ITERATION_REPORT:
            if tool != "iteration_report":
                logger.info(f"[POLICY] Overriding {tool} → iteration_report for report request")
                tool = "iteration_report"
                args = {
                    "project": args.get("project") or self.project,
                    "iteration": self.iteration
                }
                modifications.append("Changed tool to iteration_report for sprint report")
                confidence = 0.95
        
        # ══════════════════════════════════════════════════════════════
        # POLICY RULE 4: Identity Lookup → core_get_identity_ids
        # ══════════════════════════════════════════════════════════════
        if intent == QueryIntent.IDENTITY_LOOKUP:
            person = params.get("person", "")
            if tool != "core_get_identity_ids":
                logger.info(f"[POLICY] Overriding {tool} → core_get_identity_ids for identity lookup")
                tool = "core_get_identity_ids"
                args = {"searchFilter": person}
                modifications.append(f"Changed tool to core_get_identity_ids for identity '{person}'")
                confidence = 0.95
        
        # ══════════════════════════════════════════════════════════════
        # POLICY RULE 5: List Commands → Correct list tools
        # ══════════════════════════════════════════════════════════════
        if intent == QueryIntent.LIST_PROJECTS and tool != "core_list_projects":
            tool = "core_list_projects"
            args = {}
            modifications.append("Changed tool to core_list_projects")
            confidence = 0.99
        
        if intent == QueryIntent.LIST_TEAMS and tool != "core_list_project_teams":
            tool = "core_list_project_teams"
            args = {"project": self.project}
            modifications.append("Changed tool to core_list_project_teams")
            confidence = 0.95
        
        if intent == QueryIntent.LIST_ITERATIONS and tool != "work_list_iterations":
            tool = "work_list_iterations"
            args = {"project": self.project}
            modifications.append("Changed tool to work_list_iterations")
            confidence = 0.95
        
        if intent == QueryIntent.LIST_REPOS and tool != "repo_list_repos_by_project":
            tool = "repo_list_repos_by_project"
            args = {"project": self.project}
            modifications.append("Changed tool to repo_list_repos_by_project")
            confidence = 0.95
        
        if intent == QueryIntent.LIST_AREA_PATHS and tool != "list_area_paths":
            tool = "list_area_paths"
            args = {"project": self.project}
            modifications.append("Changed tool to list_area_paths")
            confidence = 0.95
        
        # ══════════════════════════════════════════════════════════════
        # POLICY RULE 6: Person-scoped queries - preserve assignedTo
        # ══════════════════════════════════════════════════════════════
        if intent == QueryIntent.PERSON_ASSIGNED:
            person = params.get("person", "")
            if tool in ("search_workitem", "execute_wiql"):
                if "assignedTo" not in args or not args.get("assignedTo"):
                    args["assignedTo"] = [person]
                    modifications.append(f"Added assignedTo filter: {person}")
        
        # ══════════════════════════════════════════════════════════════
        # POLICY RULE 7: Prevent repo tools for work item queries
        # ══════════════════════════════════════════════════════════════
        if intent == QueryIntent.WORK_ITEM_SEARCH:
            repo_tools = ["repo_list_repos_by_project", "repo_get_repository", "repo_list_branches"]
            if tool in repo_tools:
                logger.info(f"[POLICY] Overriding repo tool {tool} → search_workitem for work item query")
                tool = "search_workitem"
                args = {
                    "project": [self.project],
                    "searchText": "Bug OR User Story OR Task"
                }
                modifications.append(f"Changed repo tool to search_workitem for work item query")
                confidence = 0.80
        
        # ══════════════════════════════════════════════════════════════
        # BUILD RESULT
        # ══════════════════════════════════════════════════════════════
        was_modified = len(modifications) > 0
        
        corrected_plan = {
            "action": "call_tool",
            "tool": tool,
            "args": args,
            "confidence": confidence
        }
        
        # Add reasoning from LLM if available
        if "reasoning_summary" in plan:
            corrected_plan["reasoning_summary"] = plan["reasoning_summary"]
        
        execution_path = "policy-override" if was_modified else "llm-as-is"
        reasoning = " | ".join(modifications) if modifications else plan.get("reasoning_summary", "LLM plan accepted")
        
        return PolicyResult(
            plan=corrected_plan,
            was_modified=was_modified,
            modifications=modifications,
            reasoning_summary=reasoning,
            execution_path=execution_path
        )
    
    def validate_filters(self, plan: Dict[str, Any], query: str) -> Dict[str, Any]:
        """
        Validate and preserve critical filters in the plan args.
        
        This ensures that filters like assignedTo, state, workItemType
        are not accidentally dropped or modified.
        
        Args:
            plan: Current plan to validate
            query: Original query for context
            
        Returns:
            Plan with validated/fixed filters
        """
        if plan.get("action") != "call_tool":
            return plan
        
        args = dict(plan.get("args", {}) or {})
        tool = plan.get("tool", "")
        q_lower = query.lower()
        
        # Validate state filter
        state_keywords = {
            "new": "New",
            "active": "Active",
            "resolved": "Resolved",
            "closed": "Closed",
            "done": "Done",
            "in progress": "Active",
            "open": "New"
        }
        
        for keyword, state_value in state_keywords.items():
            if keyword in q_lower and tool in ("search_workitem", "execute_wiql"):
                current_state = args.get("state", [])
                if not current_state:
                    args["state"] = [state_value]
                    logger.debug(f"[POLICY] Added missing state filter: {state_value}")
        
        # Validate workItemType filter
        type_keywords = {
            "bug": "Bug",
            "bugs": "Bug",
            "story": "User Story",
            "stories": "User Story",
            "user story": "User Story",
            "task": "Task",
            "tasks": "Task",
            "feature": "Feature",
            "features": "Feature",
            "epic": "Epic",
            "epics": "Epic"
        }
        
        for keyword, type_value in type_keywords.items():
            if keyword in q_lower and tool in ("search_workitem", "execute_wiql"):
                current_type = args.get("workItemType", [])
                if not current_type:
                    args["workItemType"] = [type_value]
                    logger.debug(f"[POLICY] Added missing workItemType filter: {type_value}")
                    break  # Only add first match
        
        plan["args"] = args
        return plan


def apply_planner_policy(query: str, plan: Dict[str, Any], context: Dict[str, Any] = None) -> PolicyResult:
    """
    Convenience function to apply planner policies.
    
    Args:
        query: User query
        plan: LLM-generated plan
        context: Project context
        
    Returns:
        PolicyResult with corrected plan and metadata
    """
    policy = PlannerPolicy(context)
    result = policy.apply(query, plan)
    
    # Also validate filters
    if result.plan.get("action") == "call_tool":
        result.plan = policy.validate_filters(result.plan, query)
    
    return result
