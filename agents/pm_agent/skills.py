"""
Fixed Skills Router for PM Agent.

╔══════════════════════════════════════════════════════════════════════════════╗
║                    ROUTING PATTERNS - SEE ROUTER.PY                          ║
╠══════════════════════════════════════════════════════════════════════════════╣
║ IMPORTANT: Routing patterns are defined in orchestrator/router.py            ║
║ This file previously contained pattern definitions that duplicated router.   ║
║                                                                              ║
║ When re-enabling fixed skills, import patterns from router:                  ║
║   from orchestrator.router import PM_SKILL_PATTERNS, PM_AGENT_PATTERNS       ║
║                                                                              ║
║ DO NOT define patterns here - router.py is the single source of truth.       ║
╚══════════════════════════════════════════════════════════════════════════════╝

Provides deterministic, non-LLM responses for common PM queries.
These are faster, more reliable, and avoid hallucination risks.

IMPORTANT: This router should only match SIMPLE, DETERMINISTIC queries.
Complex queries involving dates, area paths, filters, or multi-step operations
should fall through to the LLM planner.

CURRENT STATUS: Patterns are DISABLED for testing LLM planner decision making.
When re-enabled, patterns should be imported from orchestrator/router.py.
"""

import re
from typing import Optional, Tuple, Dict, Any
from dataclasses import dataclass
from utilities.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class SkillMatch:
    """Result of matching a query to a fixed skill."""
    skill_name: str
    confidence: float
    extracted_params: Dict[str, Any]


# Skill patterns: (pattern, skill_name, param_extractors)
# NOTE: Keep these patterns NARROW to avoid matching complex queries
# COMMENTED OUT FOR TESTING LLM PLANNER DECISION MAKING
# This allows us to test whether the LLM can select the correct tools
# based on natural language descriptions alone, without keyword matching
SKILL_PATTERNS = [
    # ALL PATTERNS TEMPORARILY DISABLED FOR LLM PLANNER TESTING
]

# ORIGINAL PATTERNS (COMMENTED OUT):
# SKILL_PATTERNS = [
#     # Area paths - ONLY match explicit list/show requests with no other context
#     (r"^(list|show|get)\s+(the\s+)?area\s*paths?$", "list_area_paths", {}),
#     (r"^what\s+are\s+(the\s+)?area\s*paths?\s*\??$", "list_area_paths", {}),
#     
#     # Projects - list ADO projects
#     (r"^(list|show|get)\s+(all\s+)?(the\s+)?(ado\s+)?projects?$", "list_projects", {}),
#     (r"^what\s+(are\s+)?(the\s+)?(ado\s+)?projects?\s*\??$", "list_projects", {}),
#     
#     # Teams - list project teams
#     (r"^(list|show|get)\s+(all\s+)?(the\s+)?teams?$", "list_teams", {}),
#     (r"^what\s+(are\s+)?(the\s+)?teams?\s*\??$", "list_teams", {}),
#     
#     # Iterations/Sprints - list iterations
#     (r"^(list|show|get)\s+(all\s+)?(the\s+)?(iterations?|sprints?)$", "list_iterations", {}),
#     (r"^(what\s+is\s+)?(the\s+)?(current|this)\s+(sprint|iteration)(\s+(name|number))?\s*(\?|(in\s+\w+[\s\w-]*))?$", "current_iteration", {}),
#     (r"^(sprint|iteration)\s+(name|number)\s*\??$", "current_iteration", {}),
#     (r"^sprint\s+iteration\s+number\s*\??$", "current_iteration", {}),
#     (r"^what'?s?\s+(the\s+)?(current\s+)?(sprint|iteration)\s+(name|number)\s*\??$", "current_iteration", {}),
#     (r"^current\s+(sprint|iteration)\s*\??$", "current_iteration", {}),
#     
#     # Repositories - list repos
#     (r"^(list|show|get)\s+(all\s+)?(the\s+)?(git\s+)?repositor(y|ies)$", "list_repositories", {}),
#     (r"^(list|show|get)\s+(all\s+)?(the\s+)?repos?$", "list_repositories", {}),
#     
#     # Builds/Pipelines
#     (r"^(list|show|get)\s+(all\s+)?(the\s+)?builds?$", "list_builds", {}),
#     (r"^(list|show|get)\s+(all\s+)?(the\s+)?pipelines?$", "list_builds", {}),
#     
#     # Test Plans
#     (r"^(list|show|get)\s+(all\s+)?(the\s+)?test\s*plans?$", "list_test_plans", {}),
#     
#     # My work items - simple requests only
#     (r"^(show\s+)?my\s+(work\s*items?|tasks?)\s*$", "my_work_items", {}),
#     (r"^what.*(assigned\s+to\s+me)\s*\??$", "my_work_items", {}),
#     
#     # Simple bug list (no filters)
#     (r"^(list|show|get)\s+(all\s+)?(the\s+)?bugs?$", "list_bugs", {}),
#     
#     # Simple user story list (no filters)
#     (r"^(list|show|get)\s+(all\s+)?(the\s+)?(user\s*)?stor(y|ies)$", "list_stories", {}),
#     
#     # Blocked items - items that are blocked or have blocker tags
#     (r"^(get|show|list|find)\s+(me\s+)?(the\s+)?(all\s+)?blocked\s*(items?|tasks?|work\s*items?|bugs?|stories?)?\s*$", "blocked_items", {}),
#     (r"^(what|which)\s+(are\s+)?(the\s+)?blocked\s*(items?|tasks?|work\s*items?)?\s*\??$", "blocked_items", {}),
#     (r"^blocked\s*(items?|tasks?|work\s*items?)?\s*$", "blocked_items", {}),
#     (r"^(items?|tasks?|work\s*items?)\s+(that\s+are\s+)?blocked\s*$", "blocked_items", {}),
#     (r"^(show|get|list)\s+blockers?\s*$", "blocked_items", {}),
#     
#     # Unassigned items - items without an assignee
#     (r"^(get|show|list|find)\s+(me\s+)?(the\s+)?(all\s+)?unassigned\s*(items?|tasks?|work\s*items?|bugs?|stories?)?\s*$", "unassigned_items", {}),
#     (r"^(what|which)\s+(are\s+)?(the\s+)?unassigned\s*(items?|tasks?|work\s*items?)?\s*\??$", "unassigned_items", {}),
#     (r"^unassigned\s*(items?|tasks?|work\s*items?)?\s*$", "unassigned_items", {}),
#     (r"^(items?|tasks?|work\s*items?)\s+(that\s+are\s+)?unassigned\s*$", "unassigned_items", {}),
#     (r"^(items?|tasks?|work\s*items?)\s+(without|with\s+no)\s+assign(ee|ment)?\s*$", "unassigned_items", {}),
#     
#     # At-risk items - items that are at risk of slipping
#     (r"^(get|show|list|find)\s+(me\s+)?(the\s+)?(all\s+)?(at[- ]?risk|risky)\s*(items?|tasks?|work\s*items?|bugs?|stories?)?\s*$", "at_risk_items", {}),
#     (r"^(what|which)\s+(are\s+)?(the\s+)?(items?|tasks?|work\s*items?)?\s+(at[- ]?risk|risky)\s*\??$", "at_risk_items", {}),
#     (r"^(items?|tasks?|work\s*items?)\s+(that\s+are\s+)?(at[- ]?risk|at\s+risk|risky)\s*$", "at_risk_items", {}),
#     (r"^(at[- ]?risk|risky)\s*(items?|tasks?|work\s*items?)?\s*$", "at_risk_items", {}),
#     (r"^(derailing|slipping|delayed|overdue)\s*(items?|tasks?|work\s*items?)?\s*$", "at_risk_items", {}),
#     
#     # How many projects - simple count queries
#     (r"^how\s+many\s+(ado\s+)?projects?\s*(do\s+we\s+have|are\s+there)?\s*\??$", "list_projects", {}),
#     (r"^(number|count)\s+of\s+(ado\s+)?projects?\s*\??$", "list_projects", {}),
#     
#     # NOTE: Backlog triaging removed from fixed skill patterns
#     # LLM planner should handle these queries intelligently with parameter extraction
#     # This allows flexible natural language like:
#     # - "is my backlog healthy for XOPS Bugs Enhancement"
#     # - "show backlog status of Team X"
#     # - "how many sprints of work do we have for XOPS 25"
#     # LLM will extract team name and route to backlog_triaging automatically
# ]


class SkillRouter:
    """
    Routes queries to fixed skills when applicable.
    
    Fixed skills are deterministic operations that don't need LLM planning:
    - Faster execution
    - No hallucination risk
    - Consistent output format
    
    IMPORTANT: Only simple queries should match. Complex queries with:
    - Date ranges ("last 60 days")
    - Area path filters
    - Multiple conditions
    Should all fall through to the LLM planner.
    """
    
    # Keywords that indicate the query is too complex for fixed skills
    # Note: Simple work item type queries ("show bugs", "list stories") are handled by patterns
    # and should NOT be blocked by this list
    # COMMENTED OUT FOR TESTING LLM PLANNER DECISION MAKING
    COMPLEX_QUERY_INDICATORS = [
        # ALL COMPLEXITY INDICATORS DISABLED FOR LLM PLANNER TESTING
        # 'days', 'week', 'month', 'date', 'created', 'updated',
        # 'area path', 'areapath', 'under', 'filter', 'where',
        # 'recurring', 'similar', 'duplicate', 'related',
        # 'email', 'report', 'send', 'notify',
        # 'last', 'recent', 'from', 'since', 'between',
        # 'wiql', 'query', 'search', 'assigned to'
    ]
    
    # Simple patterns that should NOT trigger complexity check (even if they contain work item types)
    # COMMENTED OUT FOR TESTING LLM PLANNER DECISION MAKING
    SIMPLE_PATTERNS = [
        # ALL SIMPLE PATTERNS DISABLED FOR LLM PLANNER TESTING
        # r'^(list|show|get)\s+(all\s+)?(the\s+)?bugs?$',
        # r'^(list|show|get)\s+(all\s+)?(the\s+)?(user\s*)?stor(y|ies)$',
        # r'^(list|show|get)\s+(all\s+)?(the\s+)?tasks?$',
        # r'^(list|show|get)\s+(all\s+)?(the\s+)?features?$',
        # r'.*backlog\s+',  # Any query with "backlog " should bypass complexity check
    ]
    
    def __init__(self, min_confidence: float = 0.85):
        self.min_confidence = min_confidence
        self.patterns = []
        self.simple_pattern_regexes = [re.compile(p, re.IGNORECASE) for p in self.SIMPLE_PATTERNS]
        
        # Compile patterns
        for pattern, skill_name, extractors in SKILL_PATTERNS:
            self.patterns.append({
                "regex": re.compile(pattern, re.IGNORECASE),
                "skill": skill_name,
                "extractors": extractors
            })
    
    def match(self, query: str) -> Optional[SkillMatch]:
        """
        Try to match query to a fixed skill.
        
        Returns:
            SkillMatch if a skill matches with sufficient confidence, None otherwise.
        """
        query_lower = query.lower().strip()
        
        # First, check if this is a simple pattern that should bypass complexity check
        is_simple_pattern = any(p.match(query_lower) for p in self.simple_pattern_regexes)
        
        # Check for complex query indicators - only if not a simple pattern
        if not is_simple_pattern:
            for indicator in self.COMPLEX_QUERY_INDICATORS:
                if indicator in query_lower:
                    logger.debug("[SkillRouter] Complex query detected ('%s'), routing to LLM", indicator)
                    return None
        
        # Check each pattern
        for p in self.patterns:
            match = p["regex"].search(query_lower)
            if match:
                # Extract parameters
                params = {}
                for param_name, group_idx in p["extractors"].items():
                    if group_idx <= len(match.groups()):
                        params[param_name] = match.group(group_idx)
                
                # Calculate confidence based on match coverage
                match_len = len(match.group(0))
                coverage = match_len / len(query)
                confidence = min(0.95, 0.7 + coverage * 0.25)
                
                if confidence >= self.min_confidence:
                    logger.debug(f"Matched skill '{p['skill']}' with confidence {confidence:.2f}")
                    return SkillMatch(
                        skill_name=p["skill"],
                        confidence=confidence,
                        extracted_params=params
                    )
        
        return None
    
    def get_skill_info(self) -> Dict[str, str]:
        """Get info about available fixed skills."""
        return {
            "list_area_paths": "List all area paths in a project",
            "list_projects": "List all ADO projects in the organization",
            "list_teams": "List all teams in the project",
            "list_iterations": "List all iterations/sprints",
            "current_iteration": "Get current sprint/iteration info",
            "list_repositories": "List all Git repositories",
            "list_builds": "List recent builds/pipelines",
            "list_test_plans": "List test plans in the project",
            "my_work_items": "Work items assigned to current user",
            "list_bugs": "List all bugs in the project",
            "list_stories": "List all user stories in the project",
            "blocked_items": "Find blocked items (by state or tag)",
            "unassigned_items": "Find items without an assignee",
            "at_risk_items": "Find items at risk of slipping",
            # backlog_triaging: Handled by LLM planner for intelligent team extraction
        }


# Global instance
_router = SkillRouter()


def get_skill_router() -> SkillRouter:
    """Get the global skill router instance."""
    return _router


def should_use_fixed_skill(query: str) -> Optional[SkillMatch]:
    """
    Quick check if query should use a fixed skill.
    
    Returns:
        SkillMatch if applicable, None if LLM planning needed.
    """
    return _router.match(query)

