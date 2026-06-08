"""
Parameter Extractor - Extract concrete parameter values from context and query text.

This module provides intelligent parameter extraction for the Light LLM Planner:
- Extracts project, team, iteration from context
- Parses date ranges from natural language
- Resolves @CurrentIteration / @PreviousIteration macros
- Extracts work item types, states, and assignees from query
- Provides default values when appropriate

Phase 2 of Light LLM Planner Enhancement.
"""

import re
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List, Tuple

logger = logging.getLogger("orchestrator.parameter_extractor")


# ══════════════════════════════════════════════════════════════════════════════
# WORK ITEM TYPE MAPPING
# ══════════════════════════════════════════════════════════════════════════════

WORK_ITEM_TYPE_ALIASES = {
    # Bug aliases
    "bug": "Bug",
    "bugs": "Bug",
    "defect": "Bug",
    "defects": "Bug",
    "issue": "Bug",
    "issues": "Bug",
    
    # User Story aliases
    "story": "User Story",
    "stories": "User Story",
    "user story": "User Story",
    "user stories": "User Story",
    "us": "User Story",
    
    # Task aliases
    "task": "Task",
    "tasks": "Task",
    "todo": "Task",
    "todos": "Task",
    
    # Feature aliases
    "feature": "Feature",
    "features": "Feature",
    
    # Epic aliases
    "epic": "Epic",
    "epics": "Epic",
    
    # Test Case aliases
    "test": "Test Case",
    "tests": "Test Case",
    "test case": "Test Case",
    "test cases": "Test Case",
}

# ══════════════════════════════════════════════════════════════════════════════
# STATE MAPPING
# ══════════════════════════════════════════════════════════════════════════════

STATE_ALIASES = {
    # ── Not Started states ──
    "new": "New",
    "ready": "Ready",
    "requested": "Requested",
    "request": "Requested",
    "scheduled": "Scheduled",
    "in planning": "In Planning",
    "planning": "In Planning",
    "accepted": "Accepted",

    # ── In Progress states ──
    "active": "Active",
    "open": "Active",
    "working": "Active",
    "in progress": "In Progress",
    "in-progress": "In Progress",
    "design": "Design",
    "code review": "Code Review",
    "code-review": "Code Review",
    "in review": "Code Review",
    "pr review": "Code Review",
    "code complete": "Code Complete",
    "codecomplete": "Code Complete",
    "qa": "QA",
    "testing": "QA",
    "qa complete": "QA Complete",
    "qacomplete": "QA Complete",
    "uat": "UAT",
    "user acceptance": "UAT",
    "user acceptance testing": "UAT",
    "pre-prod": "PRE-PROD",
    "preprod": "PRE-PROD",
    "pre prod": "PRE-PROD",
    "preproduction": "PRE-PROD",
    "approved for production": "Approved for Production",
    "approved": "Approved for Production",
    "awaiting approvals": "Awaiting Approvals",
    "awaiting approval": "Awaiting Approvals",
    "pending approval": "Awaiting Approvals",

    # ── Completed states ──
    "closed": "Closed",
    "resolved": "Resolved",
    "fixed": "Resolved",
    "completed": "Completed",
    "complete": "Completed",
    "done": "Completed",
    "finished": "Completed",
    "uat complete": "UAT Complete",
    "uatcomplete": "UAT Complete",
    "uat done": "UAT Complete",
    "released": "Released",
    "removed": "Removed",
    "not a bug": "Not a Bug",
    "notabug": "Not a Bug",
    "by design": "Not a Bug",
    "requirement bug": "Requirement Bug",

    # ── Blocked states ──
    "on hold": "On Hold",
    "onhold": "On Hold",
    "hold": "On Hold",
    "waiting": "On Hold",
    "issues found": "Issues Found",
    "issue found": "Issues Found",
    "reopened": "Reopened",
    "reopen": "Reopened",
    "inactive": "Inactive",
}


class ParameterExtractor:
    """
    Extract concrete parameter values from user queries and context.
    
    This class handles:
    1. Project/Team extraction from context and query
    2. Date range parsing from natural language
    3. Work item type detection
    4. State detection
    5. Assignee extraction
    6. Iteration macro resolution hints
    """
    
    def __init__(self, config: Optional[Dict[str, Any]] = None):
        """Initialize the parameter extractor.
        
        Args:
            config: Optional configuration override
        """
        self.config = config or {}
        self._load_defaults()
    
    def _load_defaults(self):
        """Load default values from config."""
        try:
            from config import config as app_config
            self.default_project = getattr(app_config, 'pm_default_project', 'FracPro-OPS')
        except Exception:
            self.default_project = 'FracPro-OPS'
    
    def extract_all(self, query: str, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Extract all parameters from query and context.
        
        Args:
            query: User's natural language query
            context: Conversation/session context
            
        Returns:
            Dict with extracted parameters:
            {
                "project": str or None,
                "team": str or None,
                "iteration": str or None (may be @CurrentIteration macro),
                "work_item_types": List[str],
                "states": List[str],
                "assignee": str or None,
                "date_range": {"start": str, "end": str} or None,
                "top": int (limit),
                "search_text": str or None,
                "extraction_confidence": float
            }
        """
        result = {
            "project": None,
            "team": None,
            "iteration": None,
            "work_item_types": [],
            "states": [],
            "assignee": None,
            "date_range": None,
            "top": None,
            "search_text": None,
            "extraction_confidence": 0.5,
        }
        
        # 1. Extract project
        result["project"] = self.extract_project(query, context)
        
        # 2. Extract team
        result["team"] = self.extract_team(query, context)
        
        # 3. Extract iteration
        result["iteration"] = self.extract_iteration(query, context)
        
        # 4. Extract work item types
        result["work_item_types"] = self.extract_work_item_types(query)
        
        # 5. Extract states
        result["states"] = self.extract_states(query)
        
        # 6. Extract assignee
        result["assignee"] = self.extract_assignee(query, context)
        
        # 7. Extract date range
        result["date_range"] = self.extract_date_range(query)
        
        # 8. Extract top/limit
        result["top"] = self.extract_limit(query)
        
        # 9. Extract search text
        result["search_text"] = self.extract_search_text(query, result)
        
        # 10. Calculate extraction confidence
        result["extraction_confidence"] = self._calculate_confidence(result)
        
        logger.info(f"[PARAM_EXTRACTOR] Extracted: project={result['project']}, "
                   f"team={result['team']}, iteration={result['iteration']}, "
                   f"types={result['work_item_types']}, confidence={result['extraction_confidence']:.2f}")
        
        return result
    
    def extract_project(self, query: str, context: Dict[str, Any]) -> Optional[str]:
        """Extract project name from query and context.
        
        Priority:
        1. Explicit mention in query ("in project X", "for project X")
        2. Context project
        3. Default project from config
        """
        # Check for explicit project in query
        patterns = [
            r'(?:in|for|from)\s+project\s+["\']?(\w[\w\-\.]+)["\']?',
            r'project\s*[=:]\s*["\']?(\w[\w\-\.]+)["\']?',
            r'(?:in|for)\s+(\w[\w\-\.]+)\s+project',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, query, re.IGNORECASE)
            if match:
                return match.group(1)
        
        # Check context
        for key in ['project', 'ado_project', 'project_name']:
            if context.get(key):
                return context[key]
        
        # Use default
        return self.default_project
    
    def extract_team(self, query: str, context: Dict[str, Any]) -> Optional[str]:
        """Extract team name from query and context.
        
        Priority:
        1. Explicit mention in query ("for team X", "team X")
        2. Known team name mentioned directly (NES, XOPS, etc.)
        3. Context team
        """
        query_lower = query.lower()
        
        # Check for explicit team in query
        patterns = [
            r'(?:for|in|of)\s+team\s+["\']?(\w[\w\s\-\.]+?)["\']?(?:\s|$|,)',
            r'team\s*[=:]\s*["\']?(\w[\w\s\-\.]+?)["\']?(?:\s|$|,)',
            r'\bteam\s+(\w+)',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, query, re.IGNORECASE)
            if match:
                team = match.group(1).strip()
                if len(team) > 1:  # Avoid single letter matches
                    return team
        
        # Check for known team names directly in query
        known_teams = ['NES', 'XOPS', 'UI', 'Backend', 'Frontend', 'QA', 'DevOps', 'XOPS 25', 'NES Team']
        for team in known_teams:
            if team.lower() in query_lower:
                return team
        
        # Check context
        for key in ['team', 'ado_team', 'team_name']:
            if context.get(key):
                return context[key]
        
        return None
    
    def extract_iteration(self, query: str, context: Dict[str, Any]) -> Optional[str]:
        """Extract iteration reference from query.
        
        Detects:
        - @CurrentIteration / @PreviousIteration macros
        - Specific iteration names (26.1, Sprint 25)
        - Relative references (current sprint, last sprint, previous iteration)
        """
        query_lower = query.lower()
        
        # Check for specific iteration numbers
        specific_patterns = [
            r'(?:sprint|iteration)\s*(\d+\.?\d*)',  # "sprint 26.1", "iteration 25"
            r'(\d+\.?\d*)\s*(?:sprint|iteration)',  # "26.1 sprint"
        ]
        
        for pattern in specific_patterns:
            match = re.search(pattern, query, re.IGNORECASE)
            if match:
                return match.group(1)
        
        # Check for relative references
        if any(kw in query_lower for kw in ['current sprint', 'current iteration', 'this sprint', 'my sprint']):
            return '@CurrentIteration'
        
        if any(kw in query_lower for kw in ['previous sprint', 'previous iteration', 'last sprint', 'prior sprint']):
            return '@PreviousIteration'
        
        # Check context
        if context.get('iteration'):
            return context['iteration']
        if context.get('iterationId'):
            return context['iterationId']
        if context.get('iterationPath'):
            return context['iterationPath']
        
        # Default to current iteration if sprint/iteration mentioned but no specific reference
        if 'sprint' in query_lower or 'iteration' in query_lower:
            return '@CurrentIteration'
        
        return None
    
    def extract_work_item_types(self, query: str) -> List[str]:
        """Extract work item types from query."""
        query_lower = query.lower()
        types = []
        
        for alias, wit_type in WORK_ITEM_TYPE_ALIASES.items():
            # Use word boundary matching
            pattern = rf'\b{re.escape(alias)}\b'
            if re.search(pattern, query_lower):
                if wit_type not in types:
                    types.append(wit_type)
        
        return types
    
    def extract_states(self, query: str) -> List[str]:
        """Extract work item states from query."""
        query_lower = query.lower()
        states = []
        
        for alias, state in STATE_ALIASES.items():
            # Use word boundary matching
            pattern = rf'\b{re.escape(alias)}\b'
            if re.search(pattern, query_lower):
                if state not in states:
                    states.append(state)
        
        return states
    
    def extract_assignee(self, query: str, context: Dict[str, Any]) -> Optional[str]:
        """Extract assignee from query.
        
        Patterns:
        - "assigned to X"
        - "for X"
        - "X's tasks/bugs"
        """
        # Check for explicit assignee patterns
        patterns = [
            r'assigned\s+to\s+["\']?(\w[\w\s\.]+?)["\']?(?:\s|$|,)',
            r'(?:for|by)\s+(\w+)(?:\s|$|,)',
            r"(\w+)'s\s+(?:bugs?|tasks?|stories?|items?)",
        ]
        
        for pattern in patterns:
            match = re.search(pattern, query, re.IGNORECASE)
            if match:
                name = match.group(1).strip()
                # Filter out common words that aren't names
                if name.lower() not in ['the', 'this', 'that', 'my', 'me', 'current', 'all', 'team', 'sprint', 'iteration']:
                    return name
        
        # Check context
        if context.get('assignee'):
            return context['assignee']
        
        return None
    
    def extract_date_range(self, query: str) -> Optional[Dict[str, str]]:
        """Extract date range from query.
        
        Supports:
        - "last X days" / "past X days"
        - "last week" / "this week"
        - "last month" / "this month"
        - "between X and Y"
        - Specific dates
        """
        query_lower = query.lower()
        now = datetime.utcnow()
        
        # Last/past X days
        match = re.search(r'(?:last|past)\s+(\d+)\s+days?', query_lower)
        if match:
            days = int(match.group(1))
            start = (now - timedelta(days=days)).strftime('%Y-%m-%dT00:00:00Z')
            end = now.strftime('%Y-%m-%dT23:59:59Z')
            return {"start": start, "end": end}
        
        # Last/past X weeks
        match = re.search(r'(?:last|past)\s+(\d+)\s+weeks?', query_lower)
        if match:
            weeks = int(match.group(1))
            start = (now - timedelta(weeks=weeks)).strftime('%Y-%m-%dT00:00:00Z')
            end = now.strftime('%Y-%m-%dT23:59:59Z')
            return {"start": start, "end": end}
        
        # Last week / this week
        if 'last week' in query_lower:
            # Start of last week (Monday)
            start_of_last_week = now - timedelta(days=now.weekday() + 7)
            end_of_last_week = start_of_last_week + timedelta(days=6)
            return {
                "start": start_of_last_week.strftime('%Y-%m-%dT00:00:00Z'),
                "end": end_of_last_week.strftime('%Y-%m-%dT23:59:59Z')
            }
        
        if 'this week' in query_lower:
            start_of_week = now - timedelta(days=now.weekday())
            return {
                "start": start_of_week.strftime('%Y-%m-%dT00:00:00Z'),
                "end": now.strftime('%Y-%m-%dT23:59:59Z')
            }
        
        # Last month / this month
        if 'last month' in query_lower:
            first_of_this_month = now.replace(day=1)
            last_of_prev_month = first_of_this_month - timedelta(days=1)
            first_of_prev_month = last_of_prev_month.replace(day=1)
            return {
                "start": first_of_prev_month.strftime('%Y-%m-%dT00:00:00Z'),
                "end": last_of_prev_month.strftime('%Y-%m-%dT23:59:59Z')
            }
        
        if 'this month' in query_lower:
            first_of_month = now.replace(day=1)
            return {
                "start": first_of_month.strftime('%Y-%m-%dT00:00:00Z'),
                "end": now.strftime('%Y-%m-%dT23:59:59Z')
            }
        
        # Yesterday
        if 'yesterday' in query_lower:
            yesterday = now - timedelta(days=1)
            return {
                "start": yesterday.strftime('%Y-%m-%dT00:00:00Z'),
                "end": yesterday.strftime('%Y-%m-%dT23:59:59Z')
            }
        
        # Today
        if 'today' in query_lower:
            return {
                "start": now.strftime('%Y-%m-%dT00:00:00Z'),
                "end": now.strftime('%Y-%m-%dT23:59:59Z')
            }
        
        # Between X and Y dates
        # Supports formats: "1st january 2026 and 15th january 2026"
        # or "2026-01-01 and 2026-01-15"
        between_pattern = r'between\s+(.+?)\s+and\s+(.+?)(?:\s|$)'
        match = re.search(between_pattern, query_lower)
        if match:
            start_str = match.group(1).strip()
            end_str = match.group(2).strip()
            start_date = self._parse_date(start_str)
            end_date = self._parse_date(end_str)
            if start_date and end_date:
                return {
                    "start": start_date.strftime('%Y-%m-%dT00:00:00Z'),
                    "end": end_date.strftime('%Y-%m-%dT23:59:59Z')
                }
        
        return None
    
    def _parse_date(self, date_str: str) -> Optional[datetime]:
        """Parse a date string into datetime."""
        # Common date patterns
        patterns = [
            (r'(\d{4})-(\d{2})-(\d{2})', '%Y-%m-%d'),  # 2026-01-15
            (r'(\d{2})/(\d{2})/(\d{4})', '%m/%d/%Y'),  # 01/15/2026
            (r'(\d{1,2})(?:st|nd|rd|th)?\s+(\w+)\s+(\d{4})', None),  # 1st january 2026
        ]
        
        # Try ISO format first
        try:
            return datetime.fromisoformat(date_str.replace('Z', '+00:00').replace('+00:00', ''))
        except ValueError:
            pass
        
        # Try common patterns
        for pattern, fmt in patterns:
            match = re.search(pattern, date_str, re.IGNORECASE)
            if match:
                if fmt:
                    try:
                        return datetime.strptime(f"{match.group(1)}-{match.group(2)}-{match.group(3)}", fmt)
                    except ValueError:
                        pass
                else:
                    # Handle "1st january 2026" format
                    day = int(match.group(1))
                    month_name = match.group(2).lower()
                    year = int(match.group(3))
                    
                    month_map = {
                        'january': 1, 'jan': 1,
                        'february': 2, 'feb': 2,
                        'march': 3, 'mar': 3,
                        'april': 4, 'apr': 4,
                        'may': 5,
                        'june': 6, 'jun': 6,
                        'july': 7, 'jul': 7,
                        'august': 8, 'aug': 8,
                        'september': 9, 'sep': 9,
                        'october': 10, 'oct': 10,
                        'november': 11, 'nov': 11,
                        'december': 12, 'dec': 12,
                    }
                    
                    month = month_map.get(month_name)
                    if month:
                        try:
                            return datetime(year, month, day)
                        except ValueError:
                            pass
        
        return None
    
    def extract_limit(self, query: str) -> Optional[int]:
        """Extract result limit from query."""
        patterns = [
            r'(?:top|first|limit)\s+(\d+)',
            r'(\d+)\s+(?:items?|results?|bugs?|tasks?|stories?)',
            r'give\s+me\s+(\d+)',
            r'show\s+(\d+)',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, query, re.IGNORECASE)
            if match:
                limit = int(match.group(1))
                if 1 <= limit <= 1000:  # Reasonable limits
                    return limit
        
        return None
    
    def extract_search_text(self, query: str, extracted_params: Dict[str, Any]) -> Optional[str]:
        """Extract search text - what remains after removing structured data."""
        # This is useful when the query contains a search term
        # that isn't covered by other extractors
        
        # Remove common keywords
        cleaned = query.lower()
        remove_patterns = [
            r'\b(?:list|show|get|give|find|fetch|display|retrieve)\b',
            r'\b(?:all|the|me|my|please|can you)\b',
            r'\b(?:bugs?|tasks?|stories?|items?|work\s*items?)\b',
            r'\b(?:active|open|closed|resolved|new)\b',
            r'\b(?:in|for|from|of|with|to)\b',
            r'\b(?:current|this|last|previous|next)\b',
            r'\b(?:sprint|iteration)\b',
            r'\b(?:project|team)\b',
            r'\b(?:assigned|created|updated|modified)\b',
            r'\b(?:last|past)\s+\d+\s+days?\b',
        ]
        
        for pattern in remove_patterns:
            cleaned = re.sub(pattern, '', cleaned, flags=re.IGNORECASE)
        
        # Clean up whitespace
        cleaned = ' '.join(cleaned.split()).strip()
        
        # Only return if there's meaningful text left
        if len(cleaned) > 2 and cleaned not in ['and', 'or', 'the']:
            return cleaned
        
        return None
    
    def _calculate_confidence(self, params: Dict[str, Any]) -> float:
        """Calculate extraction confidence based on parameters extracted."""
        confidence = 0.5  # Base confidence
        
        # Project found
        if params.get("project"):
            confidence += 0.15
        
        # Work item types found
        if params.get("work_item_types"):
            confidence += 0.1
        
        # Iteration or date range found
        if params.get("iteration") or params.get("date_range"):
            confidence += 0.1
        
        # States found
        if params.get("states"):
            confidence += 0.05
        
        # Team found
        if params.get("team"):
            confidence += 0.1
        
        return min(confidence, 1.0)


# ══════════════════════════════════════════════════════════════════════════════
# WIQL BUILDER
# ══════════════════════════════════════════════════════════════════════════════

class WIQLBuilder:
    """Build WIQL queries from extracted parameters."""
    
    @staticmethod
    def build_query(params: Dict[str, Any]) -> str:
        """Build a WIQL query from extracted parameters.
        
        Args:
            params: Extracted parameters from ParameterExtractor
            
        Returns:
            WIQL query string
        """
        project = params.get("project", "")
        work_item_types = params.get("work_item_types", [])
        states = params.get("states", [])
        assignee = params.get("assignee")
        date_range = params.get("date_range")
        iteration = params.get("iteration")
        top = params.get("top")
        
        # Build SELECT clause
        select = "SELECT [System.Id], [System.Title], [System.State], [System.AssignedTo], [System.WorkItemType], [System.CreatedDate], [System.ChangedDate]"
        
        # Build FROM clause
        from_clause = "FROM WorkItems"
        
        # Build WHERE clause
        conditions = []
        
        # Project
        if project:
            conditions.append(f"[System.TeamProject] = '{project}'")
        
        # Work item types
        if work_item_types:
            if len(work_item_types) == 1:
                conditions.append(f"[System.WorkItemType] = '{work_item_types[0]}'")
            else:
                types_str = "', '".join(work_item_types)
                conditions.append(f"[System.WorkItemType] IN ('{types_str}')")
        
        # States
        if states:
            if len(states) == 1:
                conditions.append(f"[System.State] = '{states[0]}'")
            else:
                states_str = "', '".join(states)
                conditions.append(f"[System.State] IN ('{states_str}')")
        
        # Assignee
        if assignee:
            conditions.append(f"[System.AssignedTo] CONTAINS '{assignee}'")
        
        # Date range
        if date_range:
            conditions.append(f"[System.ChangedDate] >= '{date_range['start']}'")
            conditions.append(f"[System.ChangedDate] <= '{date_range['end']}'")
        
        # Iteration
        if iteration:
            if iteration.startswith('@'):
                # Macro - use as-is
                conditions.append(f"[System.IterationPath] = '{iteration}'")
            else:
                # Specific iteration path
                conditions.append(f"[System.IterationPath] UNDER '{iteration}'")
        
        # Build WHERE
        where = " AND ".join(conditions) if conditions else "1=1"
        
        # Build ORDER BY
        order = "ORDER BY [System.ChangedDate] DESC"
        
        # Combine
        wiql = f"{select} {from_clause} WHERE {where} {order}"
        
        return wiql


# ══════════════════════════════════════════════════════════════════════════════
# SINGLETON ACCESS
# ══════════════════════════════════════════════════════════════════════════════

_parameter_extractor: Optional[ParameterExtractor] = None


def get_parameter_extractor() -> ParameterExtractor:
    """Get the global parameter extractor instance."""
    global _parameter_extractor
    if _parameter_extractor is None:
        _parameter_extractor = ParameterExtractor()
    return _parameter_extractor


__all__ = [
    "ParameterExtractor",
    "WIQLBuilder",
    "get_parameter_extractor",
    "WORK_ITEM_TYPE_ALIASES",
    "STATE_ALIASES",
]
