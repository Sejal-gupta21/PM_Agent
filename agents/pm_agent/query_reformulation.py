"""
Query Reformulation Handler - Intelligent query retry and reformulation.

This module handles:
1. Empty result detection and query reformulation
2. Entity synonym expansion (e.g., "bug" → ["Bug", "Defect", "Issue"])
3. Time expression normalization (e.g., "last week" → date range)
4. Query relaxation strategies (remove constraints progressively)
5. Alternative tool suggestions

Use when initial query returns no results or incomplete results.
"""

import re
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class ReformulationStrategy(Enum):
    """Types of query reformulation strategies."""
    EXPAND_ENTITY = "expand_entity"  # Use synonyms
    RELAX_TIME = "relax_time"  # Expand time range
    RELAX_STATE = "relax_state"  # Include more states
    RELAX_TYPE = "relax_type"  # Include more work item types
    REMOVE_FILTER = "remove_filter"  # Remove a constraint
    BROADEN_SEARCH = "broaden_search"  # Use broader search terms
    ALTERNATIVE_TOOL = "alternative_tool"  # Try different tool


@dataclass
class ReformulationResult:
    """Result of a query reformulation."""
    strategy: ReformulationStrategy
    original_args: Dict[str, Any]
    reformulated_args: Dict[str, Any]
    explanation: str
    confidence: float = 0.8


@dataclass
class QueryReformulationContext:
    """Context for reformulation decisions."""
    original_query: str
    tool_name: str
    tool_args: Dict[str, Any]
    result_count: int
    attempt_number: int = 1
    max_attempts: int = 3
    tried_strategies: List[ReformulationStrategy] = field(default_factory=list)


class QueryReformulationHandler:
    """
    Handles intelligent query reformulation when queries return poor results.
    """
    
    def __init__(self):
        # Entity synonyms for expansion
        self.entity_synonyms = {
            # Work item types
            "bug": ["Bug", "Defect", "Issue", "Error"],
            "defect": ["Bug", "Defect", "Issue"],
            "story": ["User Story", "Story", "Feature"],
            "user story": ["User Story", "Story", "Feature"],
            "feature": ["Feature", "User Story", "Epic"],
            "task": ["Task", "Sub-task", "Work Item"],
            "epic": ["Epic", "Feature", "Initiative"],
            
            # States - use only exact ADO states
            "active": ["Active", "In Progress"],
            "in progress": ["In Progress", "Active"],
            "new": ["New", "Ready", "Requested"],
            "open": ["New", "Active", "In Progress"],
            "closed": ["Closed", "Resolved", "Completed"],
            "done": ["Closed", "Resolved", "Completed"],
            "resolved": ["Resolved", "Closed", "Completed"],
            
            # Priorities
            "high": ["1", "1 - Critical", "Critical", "High"],
            "critical": ["1", "1 - Critical", "Critical"],
            "medium": ["2", "2 - High", "Medium", "Normal"],
            "low": ["3", "3 - Medium", "Low"],
            
            # PR statuses
            "pending": ["active", "notSet"],
            "open pr": ["active"],
            "merged": ["completed"],
        }
        
        # Time expression patterns and defaults
        self.time_relaxations = {
            7: 14,    # 1 week → 2 weeks
            14: 30,   # 2 weeks → 1 month
            30: 60,   # 1 month → 2 months
            60: 90,   # 2 months → 3 months
            90: 180,  # 3 months → 6 months
        }
    
    def should_reformulate(self, result_count: int, expected_min: int = 1) -> bool:
        """Determine if reformulation is needed based on result count."""
        return result_count < expected_min
    
    def reformulate(self, context: QueryReformulationContext) -> Optional[ReformulationResult]:
        """
        Generate a reformulated query based on context.
        
        Args:
            context: Current query context
            
        Returns:
            ReformulationResult if reformulation is possible, None otherwise
        """
        if context.attempt_number >= context.max_attempts:
            logger.info("[REFORMULATE] Max attempts reached")
            return None
        
        # Select strategy based on what hasn't been tried
        strategy = self._select_strategy(context)
        if not strategy:
            logger.info("[REFORMULATE] No more strategies to try")
            return None
        
        logger.info(f"[REFORMULATE] Trying strategy: {strategy.value}")
        
        # Apply the selected strategy
        result = self._apply_strategy(strategy, context)
        
        if result:
            context.tried_strategies.append(strategy)
        
        return result
    
    def _select_strategy(self, context: QueryReformulationContext) -> Optional[ReformulationStrategy]:
        """Select the best reformulation strategy."""
        args = context.tool_args
        q = context.original_query.lower()
        
        # Priority order of strategies based on likelihood of success
        strategies_to_try = []
        
        # 1. If there's a time filter, try relaxing it first
        if self._has_time_filter(args) or self._has_time_expression(q):
            if ReformulationStrategy.RELAX_TIME not in context.tried_strategies:
                strategies_to_try.append(ReformulationStrategy.RELAX_TIME)
        
        # 2. If there's a state filter, try relaxing it
        if "state" in args or "State" in args or any(s in q for s in ["active", "new", "closed", "done"]):
            if ReformulationStrategy.RELAX_STATE not in context.tried_strategies:
                strategies_to_try.append(ReformulationStrategy.RELAX_STATE)
        
        # 3. Try entity expansion for search text
        if "searchText" in args:
            if ReformulationStrategy.EXPAND_ENTITY not in context.tried_strategies:
                strategies_to_try.append(ReformulationStrategy.EXPAND_ENTITY)
        
        # 4. Try type relaxation
        if "workItemType" in args:
            if ReformulationStrategy.RELAX_TYPE not in context.tried_strategies:
                strategies_to_try.append(ReformulationStrategy.RELAX_TYPE)
        
        # 5. Broaden search as fallback
        if ReformulationStrategy.BROADEN_SEARCH not in context.tried_strategies:
            strategies_to_try.append(ReformulationStrategy.BROADEN_SEARCH)
        
        # 6. Try alternative tool
        if ReformulationStrategy.ALTERNATIVE_TOOL not in context.tried_strategies:
            strategies_to_try.append(ReformulationStrategy.ALTERNATIVE_TOOL)
        
        return strategies_to_try[0] if strategies_to_try else None
    
    def _apply_strategy(self, strategy: ReformulationStrategy, context: QueryReformulationContext) -> Optional[ReformulationResult]:
        """Apply a specific reformulation strategy."""
        args = context.tool_args.copy()
        
        if strategy == ReformulationStrategy.RELAX_TIME:
            return self._relax_time_filter(args, context)
        
        elif strategy == ReformulationStrategy.RELAX_STATE:
            return self._relax_state_filter(args, context)
        
        elif strategy == ReformulationStrategy.EXPAND_ENTITY:
            return self._expand_entity(args, context)
        
        elif strategy == ReformulationStrategy.RELAX_TYPE:
            return self._relax_type_filter(args, context)
        
        elif strategy == ReformulationStrategy.BROADEN_SEARCH:
            return self._broaden_search(args, context)
        
        elif strategy == ReformulationStrategy.ALTERNATIVE_TOOL:
            return self._suggest_alternative_tool(args, context)
        
        return None
    
    def _has_time_filter(self, args: Dict) -> bool:
        """Check if args contain a time filter."""
        time_keys = ["fromDate", "toDate", "searchCriteria.fromDate", "searchCriteria.toDate", "sinceDate"]
        return any(k in args for k in time_keys)
    
    def _has_time_expression(self, query: str) -> bool:
        """Check if query contains time expressions."""
        time_patterns = [
            r"last\s+\d+\s+(?:day|week|month)s?",
            r"past\s+\d+\s+(?:day|week|month)s?",
            r"recent(?:ly)?",
            r"this\s+(?:week|month|sprint)",
            r"yesterday",
            r"today",
        ]
        return any(re.search(p, query) for p in time_patterns)
    
    def _relax_time_filter(self, args: Dict, context: QueryReformulationContext) -> Optional[ReformulationResult]:
        """Relax time filter by expanding the date range."""
        new_args = args.copy()
        explanation = ""
        
        # Handle WIQL queries
        if "wiql" in new_args:
            wiql = new_args["wiql"]
            
            # Find and relax @Today - N patterns
            match = re.search(r"@Today\s*-\s*(\d+)", wiql)
            if match:
                current_days = int(match.group(1))
                new_days = self.time_relaxations.get(current_days, current_days * 2)
                new_wiql = re.sub(r"@Today\s*-\s*\d+", f"@Today - {new_days}", wiql)
                new_args["wiql"] = new_wiql
                explanation = f"Expanded time range from {current_days} to {new_days} days"
            else:
                return None
        
        # Handle date parameters
        for date_key in ["fromDate", "searchCriteria.fromDate"]:
            if date_key in new_args:
                try:
                    current_date = datetime.fromisoformat(new_args[date_key].replace("Z", ""))
                    days_diff = (datetime.utcnow() - current_date).days
                    new_days = self.time_relaxations.get(days_diff, days_diff * 2)
                    new_date = datetime.utcnow() - timedelta(days=new_days)
                    new_args[date_key] = new_date.strftime("%Y-%m-%dT00:00:00Z")
                    explanation = f"Expanded time range from {days_diff} to {new_days} days"
                except (ValueError, AttributeError):
                    continue
        
        if not explanation:
            return None
        
        return ReformulationResult(
            strategy=ReformulationStrategy.RELAX_TIME,
            original_args=args,
            reformulated_args=new_args,
            explanation=explanation,
            confidence=0.7
        )
    
    def _relax_state_filter(self, args: Dict, context: QueryReformulationContext) -> Optional[ReformulationResult]:
        """
        Relax state filter to include more states.
        
        NOTE: This now uses dynamic state discovery instead of hardcoded state lists.
        """
        new_args = args.copy()
        
        # Handle WIQL queries
        if "wiql" in new_args:
            wiql = new_args["wiql"]
            
            # Remove state filter from WIQL
            new_wiql = re.sub(r"\s+AND\s+\[System\.State\]\s*(?:=|IN\s*\([^)]+\))\s*'[^']*'", "", wiql, flags=re.IGNORECASE)
            new_wiql = re.sub(r"\[System\.State\]\s*(?:=|IN\s*\([^)]+\))\s*'[^']*'\s*AND\s+", "", new_wiql, flags=re.IGNORECASE)
            
            if new_wiql != wiql:
                new_args["wiql"] = new_wiql
                return ReformulationResult(
                    strategy=ReformulationStrategy.RELAX_STATE,
                    original_args=args,
                    reformulated_args=new_args,
                    explanation="Removed state filter to include all states",
                    confidence=0.6
                )
        
        # Handle search tool state parameter
        if "state" in new_args:
            original_states = new_args.get("state", [])
            
            # Use centralized config for all known states
            try:
                from config import config as _cfg
                all_known = _cfg.get_all_known_states()
                
                if all_known:
                    new_args["state"] = list(all_known)
                    logger.info(f"[REFORMULATION] Expanded to {len(all_known)} known states from config")
                else:
                    # Fallback: remove state filter entirely
                    del new_args["state"]
                    logger.warning(f"[REFORMULATION] No known states in config, removing state filter")
            except Exception as e:
                logger.error(f"[REFORMULATION] State expansion from config failed: {e}, removing state filter")
                del new_args["state"]
            
            return ReformulationResult(
                strategy=ReformulationStrategy.RELAX_STATE,
                original_args=args,
                reformulated_args=new_args,
                explanation=f"Expanded states from {original_states} using dynamic discovery",
                confidence=0.6
            )
        
        return None
    
    def _expand_entity(self, args: Dict, context: QueryReformulationContext) -> Optional[ReformulationResult]:
        """Expand entity names using synonyms."""
        new_args = args.copy()
        search_text = new_args.get("searchText", "")
        
        if not search_text:
            return None
        
        expanded = False
        new_search_parts = []
        
        for word in search_text.lower().split():
            if word in self.entity_synonyms:
                synonyms = self.entity_synonyms[word]
                new_search_parts.append(f"({' OR '.join(synonyms)})")
                expanded = True
            else:
                new_search_parts.append(word)
        
        if expanded:
            new_args["searchText"] = " ".join(new_search_parts)
            return ReformulationResult(
                strategy=ReformulationStrategy.EXPAND_ENTITY,
                original_args=args,
                reformulated_args=new_args,
                explanation=f"Expanded search terms with synonyms: {search_text} → {new_args['searchText']}",
                confidence=0.7
            )
        
        return None
    
    def _relax_type_filter(self, args: Dict, context: QueryReformulationContext) -> Optional[ReformulationResult]:
        """Relax work item type filter."""
        new_args = args.copy()
        
        if "workItemType" in new_args:
            original_types = new_args.get("workItemType", [])
            # Expand to include related types
            expanded_types = set(original_types) if isinstance(original_types, list) else {original_types}
            
            for t in list(expanded_types):
                if t.lower() in ["bug", "defect"]:
                    expanded_types.update(["Bug", "Defect", "Issue"])
                elif t.lower() in ["user story", "story"]:
                    expanded_types.update(["User Story", "Feature"])
                elif t.lower() == "task":
                    expanded_types.update(["Task", "Sub-task"])
            
            new_args["workItemType"] = list(expanded_types)
            return ReformulationResult(
                strategy=ReformulationStrategy.RELAX_TYPE,
                original_args=args,
                reformulated_args=new_args,
                explanation=f"Expanded work item types: {original_types} → {list(expanded_types)}",
                confidence=0.65
            )
        
        # Handle WIQL
        if "wiql" in new_args:
            wiql = new_args["wiql"]
            new_wiql = re.sub(r"\s+AND\s+\[System\.WorkItemType\]\s*=\s*'[^']*'", "", wiql, flags=re.IGNORECASE)
            
            if new_wiql != wiql:
                new_args["wiql"] = new_wiql
                return ReformulationResult(
                    strategy=ReformulationStrategy.RELAX_TYPE,
                    original_args=args,
                    reformulated_args=new_args,
                    explanation="Removed work item type filter",
                    confidence=0.6
                )
        
        return None
    
    def _broaden_search(self, args: Dict, context: QueryReformulationContext) -> Optional[ReformulationResult]:
        """Broaden search by using simpler/broader terms."""
        new_args = args.copy()
        
        # Increase result limit
        if "top" in new_args:
            new_args["top"] = max(new_args["top"] * 2, 100)
        else:
            new_args["top"] = 200
        
        # Simplify search text
        if "searchText" in new_args:
            search = new_args["searchText"]
            # Remove special characters and simplify
            simplified = re.sub(r"[^\w\s]", " ", search)
            # Take only first 2 words
            words = simplified.split()[:2]
            if words:
                new_args["searchText"] = " ".join(words)
        
        return ReformulationResult(
            strategy=ReformulationStrategy.BROADEN_SEARCH,
            original_args=args,
            reformulated_args=new_args,
            explanation="Broadened search with simpler terms and larger limit",
            confidence=0.5
        )
    
    def _suggest_alternative_tool(self, args: Dict, context: QueryReformulationContext) -> Optional[ReformulationResult]:
        """Suggest an alternative tool for the query."""
        tool_alternatives = {
            "search_workitem": "execute_wiql",
            "wit_get_work_items_for_iteration": "execute_wiql",
            "pr_list_pull_requests": "repo_list_commits",  # For tracking changes
        }
        
        alternative = tool_alternatives.get(context.tool_name)
        if not alternative:
            return None
        
        # Transform args for alternative tool
        new_args = self._transform_args_for_tool(args, context.tool_name, alternative, context)
        
        return ReformulationResult(
            strategy=ReformulationStrategy.ALTERNATIVE_TOOL,
            original_args=args,
            reformulated_args={"_alternative_tool": alternative, **new_args},
            explanation=f"Try alternative tool: {alternative}",
            confidence=0.5
        )
    
    def _transform_args_for_tool(self, args: Dict, from_tool: str, to_tool: str, context: QueryReformulationContext) -> Dict:
        """Transform arguments from one tool format to another."""
        project = args.get("project", "")
        
        if to_tool == "execute_wiql":
            # Build a WIQL query from search params
            conditions = [f"[System.TeamProject] = '{project}'"]
            
            if "workItemType" in args:
                types = args["workItemType"]
                if isinstance(types, list):
                    types_str = "', '".join(types)
                    conditions.append(f"[System.WorkItemType] IN ('{types_str}')")
                else:
                    conditions.append(f"[System.WorkItemType] = '{types}'")
            
            if "state" in args:
                states = args["state"]
                if isinstance(states, list):
                    states_str = "', '".join(states)
                    conditions.append(f"[System.State] IN ('{states_str}')")
            
            if "searchText" in args:
                search = args["searchText"]
                conditions.append(f"[System.Title] CONTAINS '{search}'")
            
            wiql = f"""
            SELECT [System.Id], [System.Title], [System.State], [System.AssignedTo], [System.WorkItemType]
            FROM WorkItems
            WHERE {' AND '.join(conditions)}
            ORDER BY [System.ChangedDate] DESC
            """
            
            return {"project": project, "wiql": wiql.strip()}
        
        return args


# ═══════════════════════════════════════════════════════════════════════════════
# CONVENIENCE FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

_handler: Optional[QueryReformulationHandler] = None

def get_reformulation_handler() -> QueryReformulationHandler:
    """Get the singleton reformulation handler."""
    global _handler
    if _handler is None:
        _handler = QueryReformulationHandler()
    return _handler


def attempt_reformulation(
    original_query: str,
    tool_name: str,
    tool_args: Dict[str, Any],
    result_count: int,
    attempt_number: int = 1,
    previous_strategies: List[ReformulationStrategy] = None
) -> Optional[ReformulationResult]:
    """
    Attempt to reformulate a query that returned poor results.
    
    Args:
        original_query: The user's original natural language query
        tool_name: The MCP tool that was called
        tool_args: Arguments passed to the tool
        result_count: Number of results returned
        attempt_number: Current attempt number
        previous_strategies: Strategies already tried
        
    Returns:
        ReformulationResult if reformulation is possible
    """
    handler = get_reformulation_handler()
    
    if not handler.should_reformulate(result_count):
        return None
    
    context = QueryReformulationContext(
        original_query=original_query,
        tool_name=tool_name,
        tool_args=tool_args,
        result_count=result_count,
        attempt_number=attempt_number,
        tried_strategies=previous_strategies or []
    )
    
    return handler.reformulate(context)


def normalize_time_expression(query: str) -> Tuple[Optional[datetime], Optional[datetime]]:
    """
    Normalize time expressions in a query to concrete dates.
    
    Args:
        query: Query containing time expressions
        
    Returns:
        Tuple of (from_date, to_date)
    """
    q = query.lower()
    now = datetime.utcnow()
    from_date = None
    to_date = now
    
    # Match "last N days/weeks/months"
    match = re.search(r"(?:last|past)\s+(\d+)\s+(day|week|month)s?", q)
    if match:
        n = int(match.group(1))
        unit = match.group(2)
        if unit == "day":
            from_date = now - timedelta(days=n)
        elif unit == "week":
            from_date = now - timedelta(weeks=n)
        elif unit == "month":
            from_date = now - timedelta(days=n * 30)
        return from_date, to_date
    
    # Match specific time words
    if "yesterday" in q:
        from_date = now - timedelta(days=1)
        to_date = now
    elif "today" in q:
        from_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif "this week" in q:
        from_date = now - timedelta(days=now.weekday())
    elif "this month" in q:
        from_date = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    elif "this sprint" in q or "current sprint" in q:
        # Default to 2 weeks for current sprint
        from_date = now - timedelta(days=14)
    elif "recent" in q:
        from_date = now - timedelta(days=7)
    
    return from_date, to_date
