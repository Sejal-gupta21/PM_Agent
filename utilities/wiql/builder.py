"""
WIQL Query Builder for Azure DevOps.

Provides a fluent API for constructing WIQL (Work Item Query Language) queries
with proper support for ADO-native date macros and field names.

Key Features:
- ADO date macros: @Today, @StartOfDay, @StartOfWeek, @StartOfMonth, @StartOfYear
- Relative date offsets: @Today - 10, @StartOfDay('-7d')
- Proper field names: Microsoft.VSTS.Common.ClosedDate, System.ChangedDate, etc.
- Type-safe query construction
- Automatic sanitization and validation

Usage:
    from utilities.wiql.builder import WIQLBuilder, parse_query_for_date_intent
    
    # Build query for bugs closed in last 10 days
    wiql = WIQLBuilder() \\
        .select(['System.Id', 'System.Title', 'System.State']) \\
        .where_project('FracPro-OPS') \\
        .where_work_item_type('Bug') \\
        .where_state_in(['Closed', 'Resolved', 'Completed']) \\
        .where_changed_date_range(days_ago=10) \\
        .order_by('System.ChangedDate', desc=True) \\
        .build()

    # Parse natural language for date intent
    intent = parse_query_for_date_intent("bugs closed in last 10 days")
    # Returns: {'relative_days': 10, 'date_type': 'closed', 'has_date_filter': True}
"""

import re
import logging
from enum import Enum
from typing import List, Dict, Any, Optional, Union
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


class DateMacro(Enum):
    """ADO WIQL date macros."""
    TODAY = "@Today"
    START_OF_DAY = "@StartOfDay"
    START_OF_WEEK = "@StartOfWeek"
    START_OF_MONTH = "@StartOfMonth"
    START_OF_YEAR = "@StartOfYear"


class WIQLBuilder:
    """
    Fluent API for building WIQL queries with ADO macro support.
    
    Example:
        query = WIQLBuilder() \\
            .select(['System.Id', 'System.Title']) \\
            .where_project('FracPro-OPS') \\
            .where_work_item_type('Bug') \\
            .where_closed_date_range(days_ago=10) \\
            .order_by('System.ChangedDate', desc=True) \\
            .build()
    """
    
    # Standard fields for work item queries
    DEFAULT_FIELDS = [
        'System.Id',
        'System.Title',
        'System.State',
        'System.WorkItemType',
        'System.AssignedTo',
        'System.Tags',
        'System.AreaPath',
        'System.IterationPath',
        'System.ChangedDate',
        'System.CreatedDate'
    ]
    
    def __init__(self):
        self._select_fields: List[str] = []
        self._where_clauses: List[str] = []
        self._order_by: Optional[str] = None
        self._top: Optional[int] = None
    
    def select(self, fields: Optional[List[str]] = None) -> 'WIQLBuilder':
        """
        Add fields to SELECT clause.
        
        Args:
            fields: List of field names. If None, uses DEFAULT_FIELDS.
        """
        if fields:
            self._select_fields.extend(fields)
        else:
            self._select_fields.extend(self.DEFAULT_FIELDS)
        return self
    
    def where_project(self, project: str) -> 'WIQLBuilder':
        """Add project filter."""
        if project:
            self._where_clauses.append(f"[System.TeamProject] = '{project}'")
        return self
    
    def where_work_item_type(self, work_item_type: str) -> 'WIQLBuilder':
        """
        Add work item type filter (Bug, Task, User Story, etc.).
        
        Args:
            work_item_type: Type name (case-insensitive, normalized)
        """
        # Normalize common variations
        type_map = {
            'bug': 'Bug',
            'bugs': 'Bug',
            'task': 'Task',
            'tasks': 'Task',
            'user story': 'User Story',
            'userstory': 'User Story',
            'story': 'User Story',
            'stories': 'User Story',
            'feature': 'Feature',
            'features': 'Feature',
            'epic': 'Epic',
            'epics': 'Epic',
        }
        normalized = type_map.get(work_item_type.lower(), work_item_type)
        self._where_clauses.append(f"[System.WorkItemType] = '{normalized}'")
        return self
    
    def where_work_item_types(self, types: List[str]) -> 'WIQLBuilder':
        """Add filter for multiple work item types."""
        if types:
            types_str = ", ".join([f"'{t}'" for t in types])
            self._where_clauses.append(f"[System.WorkItemType] IN ({types_str})")
        return self
    
    def where_state(self, state: str) -> 'WIQLBuilder':
        """Add state filter."""
        if state:
            self._where_clauses.append(f"[System.State] = '{state}'")
        return self
    
    def where_state_in(self, states: List[str]) -> 'WIQLBuilder':
        """Add state IN filter for multiple states."""
        if states:
            states_str = ", ".join([f"'{s}'" for s in states])
            self._where_clauses.append(f"[System.State] IN ({states_str})")
        return self
    
    def where_state_not_in(self, states: List[str]) -> 'WIQLBuilder':
        """Exclude specific states."""
        if states:
            states_str = ", ".join([f"'{s}'" for s in states])
            self._where_clauses.append(f"[System.State] NOT IN ({states_str})")
        return self
    
    def where_iteration_path(self, iteration_path: str) -> 'WIQLBuilder':
        """Add iteration path filter."""
        if iteration_path:
            self._where_clauses.append(f"[System.IterationPath] = '{iteration_path}'")
        return self
    
    def where_iteration_under(self, iteration_path: str) -> 'WIQLBuilder':
        """Add hierarchical iteration filter (includes children)."""
        if iteration_path:
            self._where_clauses.append(f"[System.IterationPath] UNDER '{iteration_path}'")
        return self
    
    def where_area_path(self, area_path: str) -> 'WIQLBuilder':
        """Add area path filter."""
        if area_path:
            self._where_clauses.append(f"[System.AreaPath] = '{area_path}'")
        return self
    
    def where_area_under(self, area_path: str) -> 'WIQLBuilder':
        """Add hierarchical area filter (includes children)."""
        if area_path:
            self._where_clauses.append(f"[System.AreaPath] UNDER '{area_path}'")
        return self
    
    def where_assigned_to(self, user: str) -> 'WIQLBuilder':
        """
        Add assignee filter. 
        
        Uses CONTAINS for partial name matching (e.g., "Vivek" matches "Vivek Sharma").
        Uses '=' only for @me macro or full email addresses (exact identity match).
        
        Args:
            user: User name/email, or '@me' for current user
        """
        if user:
            if user.lower() == '@me':
                self._where_clauses.append("[System.AssignedTo] = @Me")
            elif '@' in user:
                # Full email — use exact match
                self._where_clauses.append(f"[System.AssignedTo] = '{user}'")
            else:
                # Partial name — use CONTAINS for fuzzy matching
                self._where_clauses.append(f"[System.AssignedTo] CONTAINS '{user}'")
        return self
    
    def where_unassigned(self) -> 'WIQLBuilder':
        """Add filter for unassigned items."""
        self._where_clauses.append("[System.AssignedTo] = ''")
        return self
    
    def where_has_assignee(self) -> 'WIQLBuilder':
        """Add filter for items with an assignee."""
        self._where_clauses.append("[System.AssignedTo] <> ''")
        return self
    
    def where_tags_contain(self, tag: str) -> 'WIQLBuilder':
        """Add single tag filter using CONTAINS."""
        if tag:
            self._where_clauses.append(f"[System.Tags] CONTAINS '{tag}'")
        return self
    
    def where_tags_contain_any(self, tags: List[str]) -> 'WIQLBuilder':
        """Add filter for any of multiple tags (OR condition)."""
        if tags:
            tag_conditions = [f"[System.Tags] CONTAINS '{tag}'" for tag in tags]
            self._where_clauses.append(f"({' OR '.join(tag_conditions)})")
        return self
    
    def where_date_range(
        self, 
        field: str, 
        start: Optional[str] = None, 
        end: Optional[str] = None
    ) -> 'WIQLBuilder':
        """
        Add date range filter with ADO macro support.
        
        Args:
            field: Date field name (e.g., 'System.ChangedDate')
            start: Start date - ADO macro (no quotes) or ISO string
            end: End date - ADO macro (no quotes) or ISO string
        
        ADO Macros (use without quotes):
            @Today, @Today - 10
            @StartOfDay, @StartOfDay('-10d')
            @StartOfWeek, @StartOfMonth, @StartOfYear
        
        Examples:
            .where_date_range('System.ChangedDate', 
                             start='@Today - 10', 
                             end='@Today')
        """
        if start:
            if self._is_ado_macro(start):
                # ADO macro - no quotes
                self._where_clauses.append(f"[{field}] >= {start}")
            else:
                # Literal date - with quotes
                self._where_clauses.append(f"[{field}] >= '{start}'")
        
        if end:
            if self._is_ado_macro(end):
                # ADO macro - no quotes
                self._where_clauses.append(f"[{field}] <= {end}")
            else:
                # Literal date - with quotes
                self._where_clauses.append(f"[{field}] <= '{end}'")
        
        return self
    
    def where_changed_date_range(
        self, 
        days_ago: Optional[int] = None,
        start: Optional[str] = None,
        end: Optional[str] = None
    ) -> 'WIQLBuilder':
        """
        Filter by System.ChangedDate with relative or absolute dates.
        
        Args:
            days_ago: Number of days in the past (uses @Today - N)
            start: Explicit start (overrides days_ago)
            end: Explicit end (default: @Today)
        
        Examples:
            .where_changed_date_range(days_ago=10)  # Last 10 days
        """
        field = 'System.ChangedDate'
        
        if days_ago is not None and start is None:
            start = f"@Today - {days_ago}"
        
        if end is None:
            end = "@Today"
        
        return self.where_date_range(field, start=start, end=end)
    
    def where_created_date_range(
        self, 
        days_ago: Optional[int] = None,
        start: Optional[str] = None,
        end: Optional[str] = None
    ) -> 'WIQLBuilder':
        """
        Filter by System.CreatedDate with relative or absolute dates.
        
        Args:
            days_ago: Number of days in the past
            start: Explicit start
            end: Explicit end
        """
        field = 'System.CreatedDate'
        
        if days_ago is not None and start is None:
            start = f"@Today - {days_ago}"
        
        if end is None:
            end = "@Today"
        
        return self.where_date_range(field, start=start, end=end)
    
    def where_closed_date_range(
        self, 
        days_ago: Optional[int] = None,
        start: Optional[str] = None,
        end: Optional[str] = None
    ) -> 'WIQLBuilder':
        """
        Filter by Microsoft.VSTS.Common.ClosedDate with relative or absolute dates.
        
        IMPORTANT: For "bugs closed in last N days", use this method.
        The ClosedDate field is set when a work item transitions to Closed state.
        
        Args:
            days_ago: Number of days in the past
            start: Explicit start
            end: Explicit end
        """
        field = 'Microsoft.VSTS.Common.ClosedDate'
        
        if days_ago is not None and start is None:
            start = f"@Today - {days_ago}"
        
        if end is None:
            end = "@Today"
        
        return self.where_date_range(field, start=start, end=end)
    
    def where_custom(self, condition: str) -> 'WIQLBuilder':
        """
        Add custom WHERE condition.
        
        Use for complex conditions not covered by helper methods.
        
        Args:
            condition: Raw WIQL condition string
        """
        if condition:
            self._where_clauses.append(condition)
        return self
    
    def order_by(self, field: str, desc: bool = False) -> 'WIQLBuilder':
        """
        Add ORDER BY clause.
        
        Args:
            field: Field name to order by
            desc: If True, order descending
        """
        direction = "DESC" if desc else "ASC"
        self._order_by = f"ORDER BY [{field}] {direction}"
        return self
    
    def top(self, limit: int) -> 'WIQLBuilder':
        """
        Set the maximum number of results.
        
        NOTE: This stores the limit for later use but does NOT add "SELECT TOP N" 
        to the WIQL query. ADO REST API's $top parameter should be used instead.
        Use get_top() to retrieve the value for the API call.
        """
        self._top = limit
        return self
    
    def get_top(self) -> Optional[int]:
        """Get the top limit value (for passing to API's $top parameter)."""
        return self._top
    
    def build(self) -> str:
        """
        Build the final WIQL query string.
        
        Note: Does NOT include TOP clause - use API's $top parameter instead.
        
        Returns:
            Valid WIQL query string
        """
        # Use default fields if none specified
        if not self._select_fields:
            self._select_fields = self.DEFAULT_FIELDS.copy()
        
        # Build SELECT clause (no TOP - use API $top parameter)
        select_str = ", ".join([f"[{f}]" for f in self._select_fields])
        query = f"SELECT {select_str}\n"
        
        query += "FROM WorkItems\n"
        
        # Build WHERE clause
        if self._where_clauses:
            where_str = "\n    AND ".join(self._where_clauses)
            query += f"WHERE\n    {where_str}\n"
        
        # Add ORDER BY
        if self._order_by:
            query += f"{self._order_by}\n"
        
        return query.strip()
    
    def _is_ado_macro(self, value: str) -> bool:
        """Check if value is an ADO macro (starts with @)."""
        if not value:
            return False
        value = value.strip()
        return value.startswith('@') or value.startswith("@")


def build_date_filter(
    date_type: str,
    days_ago: Optional[int] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None
) -> str:
    """
    Build a WIQL date filter clause.
    
    Args:
        date_type: 'closed', 'changed', 'created'
        days_ago: Number of days in the past
        start_date: Explicit start date (ADO macro or ISO string)
        end_date: Explicit end date
    
    Returns:
        WIQL condition string for date filtering
    """
    field_map = {
        'closed': 'Microsoft.VSTS.Common.ClosedDate',
        'changed': 'System.ChangedDate',
        'created': 'System.CreatedDate',
        'updated': 'System.ChangedDate',
        'modified': 'System.ChangedDate',
    }
    
    field = field_map.get(date_type.lower(), 'System.ChangedDate')
    
    if days_ago is not None:
        start = f"@Today - {days_ago}"
        end = "@Today"
    else:
        start = start_date
        end = end_date or "@Today"
    
    conditions = []
    if start:
        if start.startswith('@'):
            conditions.append(f"[{field}] >= {start}")
        else:
            conditions.append(f"[{field}] >= '{start}'")
    
    if end:
        if end.startswith('@'):
            conditions.append(f"[{field}] <= {end}")
        else:
            conditions.append(f"[{field}] <= '{end}'")
    
    return " AND ".join(conditions)


# ═══════════════════════════════════════════════════════════════════════════════
# REMOVED: parse_query_for_date_intent() and build_wiql_from_intent()
#
# These regex-based NL→WIQL converters have been removed as part of the
# architectural cleanup. The LLM planner is now solely responsible for
# generating WIQL strings. The WIQLBuilder class above remains as a
# programmatic utility for code that needs to construct WIQL (e.g. scripts),
# but is NOT used in the agent's planning/routing pipeline.
# ═══════════════════════════════════════════════════════════════════════════════