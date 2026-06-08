"""
Entity Normalization - Standardize entities for consistent ADO queries.

This module handles:
1. Person name normalization (John -> john.smith@company.com)
2. Date/time normalization (last week -> date range)
3. State normalization (active -> ["Active", "In Progress"])
4. Work item type normalization (bug -> Bug)
5. Project/team resolution
6. Iteration path resolution

Use before executing queries to ensure consistent entity formats.
"""

import re
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class EntityType(Enum):
    """Types of entities that can be normalized."""
    PERSON = "person"
    DATE = "date"
    DATE_RANGE = "date_range"
    STATE = "state"
    WORK_ITEM_TYPE = "work_item_type"
    PROJECT = "project"
    TEAM = "team"
    ITERATION = "iteration"
    AREA_PATH = "area_path"
    PRIORITY = "priority"


@dataclass
class NormalizedEntity:
    """Result of entity normalization."""
    original: str
    normalized: Any
    entity_type: EntityType
    confidence: float = 1.0
    alternatives: List[Any] = field(default_factory=list)


@dataclass
class NormalizationContext:
    """Context for entity normalization."""
    project: str = ""
    team: str = ""
    iteration: str = ""
    known_persons: List[str] = field(default_factory=list)
    known_areas: List[str] = field(default_factory=list)
    known_iterations: List[str] = field(default_factory=list)


class EntityNormalizer:
    """
    Normalizes entities from natural language queries to ADO-compatible formats.
    """
    
    def __init__(self, context: NormalizationContext = None):
        self.context = context or NormalizationContext()
        
        # ═══════════════════════════════════════════════════════════════════════
        # REMOVED: Static state mappings dictionary
        # 
        # Previously had hardcoded state_mappings dict mapping user inputs to states.
        # This violated the dynamic state resolution requirement.
        # 
        # NOW: States are dynamically discovered and resolved via StateResolver
        #      See utilities/state_resolver.py for implementation
        # ═══════════════════════════════════════════════════════════════════════
        
        # Work item type mappings (these are standard ADO types - less variation)
        self.type_mappings = {
            "bug": "Bug",
            "bugs": "Bug",
            "defect": "Bug",
            "defects": "Bug",
            "issue": "Bug",
            "issues": "Bug",
            "story": "User Story",
            "stories": "User Story",
            "user story": "User Story",
            "user stories": "User Story",
            "feature": "Feature",
            "features": "Feature",
            "task": "Task",
            "tasks": "Task",
            "epic": "Epic",
            "epics": "Epic",
            "pbi": "Product Backlog Item",
            "product backlog item": "Product Backlog Item",
            "impediment": "Impediment",
            "test case": "Test Case",
        }
        
        # Priority mappings
        self.priority_mappings = {
            "critical": "1",
            "high": "1",
            "urgent": "1",
            "p1": "1",
            "p0": "1",
            "medium": "2",
            "normal": "2",
            "p2": "2",
            "low": "3",
            "p3": "3",
            "minor": "4",
            "trivial": "4",
            "p4": "4",
        }
        
        # Time expression patterns
        self.time_patterns = [
            (r"(?:last|past)\s+(\d+)\s+days?", "days"),
            (r"(?:last|past)\s+(\d+)\s+weeks?", "weeks"),
            (r"(?:last|past)\s+(\d+)\s+months?", "months"),
            (r"(?:last|past)\s+week", "1_week"),
            (r"(?:last|past)\s+month", "1_month"),
            (r"this\s+week", "this_week"),
            (r"this\s+month", "this_month"),
            (r"yesterday", "yesterday"),
            (r"today", "today"),
            (r"recent(?:ly)?", "7_days"),
        ]
    
    def normalize_all(self, query: str) -> Dict[EntityType, List[NormalizedEntity]]:
        """
        Normalize all entities found in a query.
        
        Args:
            query: Natural language query
            
        Returns:
            Dict mapping entity types to normalized values
        """
        results = {}
        
        # Normalize states
        states = self.normalize_states(query)
        if states:
            results[EntityType.STATE] = states
        
        # Normalize work item types
        types = self.normalize_work_item_types(query)
        if types:
            results[EntityType.WORK_ITEM_TYPE] = types
        
        # Normalize dates
        dates = self.normalize_dates(query)
        if dates:
            results[EntityType.DATE_RANGE] = dates
        
        # Normalize priorities
        priorities = self.normalize_priorities(query)
        if priorities:
            results[EntityType.PRIORITY] = priorities
        
        # Normalize persons
        persons = self.normalize_persons(query)
        if persons:
            results[EntityType.PERSON] = persons
        
        # Normalize iterations
        iterations = self.normalize_iterations(query)
        if iterations:
            results[EntityType.ITERATION] = iterations
        
        return results
    
    def normalize_states(self, query: str, project: str = None) -> List[NormalizedEntity]:
        """
        Normalize state references in query.
        
        DEPRECATED: This method is being phased out in favor of utilities.state_resolver.StateResolver
        
        For new code, use StateResolver.validate_state() instead which provides:
        - Dynamic state discovery from ADO
        - Semantic inference for non-existent states
        - Intent-based state resolution
        
        This method now passes states through without mapping to support legacy code.
        
        Args:
            query: User query
            project: ADO project (optional, for future dynamic resolution)
            
        Returns:
            List of NormalizedEntity with original state values (no mapping)
        """
        # Extract potential state keywords from query
        # Common state-related words
        state_keywords = [
            'active', 'new', 'open', 'closed', 'resolved', 'done', 'completed',
            'in progress', 'in-progress', 'blocked', 'ready', 'pending',
            'qa', 'code review', 'design', 'testing', 'removed'
        ]
        
        q = query.lower()
        results = []
        
        for keyword in state_keywords:
            if re.search(r'\b' + re.escape(keyword) + r'\b', q):
                # Return as-is without mapping - StateResolver will handle validation
                results.append(NormalizedEntity(
                    original=keyword,
                    normalized=[keyword],  # Pass through without mapping
                    entity_type=EntityType.STATE,
                    confidence=0.9
                ))
        
        return results
    
    def normalize_work_item_types(self, query: str) -> List[NormalizedEntity]:
        """Normalize work item type references in query."""
        q = query.lower()
        results = []
        
        for pattern, normalized_type in self.type_mappings.items():
            # Use word boundary matching
            if re.search(r'\b' + re.escape(pattern) + r'\b', q):
                results.append(NormalizedEntity(
                    original=pattern,
                    normalized=normalized_type,
                    entity_type=EntityType.WORK_ITEM_TYPE,
                    confidence=0.95
                ))
        
        return results
    
    def normalize_priorities(self, query: str) -> List[NormalizedEntity]:
        """Normalize priority references in query."""
        q = query.lower()
        results = []
        
        for pattern, normalized_priority in self.priority_mappings.items():
            if re.search(r'\b' + re.escape(pattern) + r'\b', q):
                results.append(NormalizedEntity(
                    original=pattern,
                    normalized=normalized_priority,
                    entity_type=EntityType.PRIORITY,
                    confidence=0.9
                ))
        
        return results
    
    def normalize_dates(self, query: str) -> List[NormalizedEntity]:
        """Normalize date/time expressions to date ranges."""
        q = query.lower()
        results = []
        now = datetime.utcnow()
        
        for pattern, unit in self.time_patterns:
            match = re.search(pattern, q)
            if match:
                from_date, to_date = self._calculate_date_range(match, unit, now)
                
                results.append(NormalizedEntity(
                    original=match.group(0),
                    normalized={"from": from_date, "to": to_date},
                    entity_type=EntityType.DATE_RANGE,
                    confidence=0.95
                ))
        
        return results
    
    def _calculate_date_range(self, match: re.Match, unit: str, now: datetime) -> Tuple[datetime, datetime]:
        """Calculate date range from time expression."""
        to_date = now
        
        if unit == "days":
            n = int(match.group(1))
            from_date = now - timedelta(days=n)
        elif unit == "weeks":
            n = int(match.group(1))
            from_date = now - timedelta(weeks=n)
        elif unit == "months":
            n = int(match.group(1))
            from_date = now - timedelta(days=n * 30)
        elif unit == "1_week":
            from_date = now - timedelta(weeks=1)
        elif unit == "1_month":
            from_date = now - timedelta(days=30)
        elif unit == "this_week":
            from_date = now - timedelta(days=now.weekday())
        elif unit == "this_month":
            from_date = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        elif unit == "yesterday":
            from_date = now - timedelta(days=1)
            to_date = now - timedelta(days=1)
        elif unit == "today":
            from_date = now.replace(hour=0, minute=0, second=0, microsecond=0)
        elif unit == "7_days":
            from_date = now - timedelta(days=7)
        else:
            from_date = now - timedelta(days=7)  # Default
        
        return from_date, to_date
    
    def normalize_persons(self, query: str) -> List[NormalizedEntity]:
        """Normalize person names in query."""
        results = []
        
        # Patterns to extract person references
        patterns = [
            r"(?:assigned\s+to|by|for|from)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)",
            r"([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)'s\s+(?:bugs?|items?|tasks?|work|stories?)",
        ]
        
        for pattern in patterns:
            for match in re.finditer(pattern, query):
                name = match.group(1)
                # Skip common words
                if name.lower() not in ["the", "a", "an", "this", "that", "sprint", "iteration", "project"]:
                    # Try to find in known persons
                    normalized = self._find_known_person(name)
                    
                    results.append(NormalizedEntity(
                        original=name,
                        normalized=normalized or name,
                        entity_type=EntityType.PERSON,
                        confidence=0.8 if normalized else 0.5
                    ))
        
        return results
    
    def _find_known_person(self, name: str) -> Optional[str]:
        """Find a matching known person for a name."""
        name_lower = name.lower()
        
        for known in self.context.known_persons:
            # Check various matching strategies
            known_lower = known.lower()
            
            # Exact match
            if name_lower == known_lower:
                return known
            
            # First name match
            known_first = known_lower.split()[0] if ' ' in known_lower else known_lower.split('@')[0]
            if name_lower == known_first:
                return known
            
            # Email prefix match
            if '@' in known_lower:
                email_prefix = known_lower.split('@')[0]
                if name_lower in email_prefix or email_prefix.startswith(name_lower):
                    return known
        
        return None
    
    def normalize_iterations(self, query: str) -> List[NormalizedEntity]:
        """Normalize iteration/sprint references in query."""
        q = query.lower()
        results = []
        
        # Current sprint keywords
        if any(kw in q for kw in ["current sprint", "current iteration", "this sprint", "this iteration"]):
            results.append(NormalizedEntity(
                original="current sprint/iteration",
                normalized="@CurrentIteration",
                entity_type=EntityType.ITERATION,
                confidence=1.0
            ))
        
        # Previous sprint keywords
        if any(kw in q for kw in ["previous sprint", "last sprint", "previous iteration", "last iteration"]):
            results.append(NormalizedEntity(
                original="previous sprint/iteration",
                normalized="@PreviousIteration",
                entity_type=EntityType.ITERATION,
                confidence=1.0
            ))
        
        # Specific sprint names
        patterns = [
            r"(?:sprint|iteration)\s+(\d+)",
            r"(?:sprint|iteration)\s+\"([^\"]+)\"",
            r"(?:sprint|iteration)\s+'([^']+)'",
            r"(?:sprint|iteration)\s+(\S+)",
        ]
        
        for pattern in patterns:
            match = re.search(pattern, q)
            if match:
                iteration_ref = match.group(1)
                # Try to find in known iterations
                normalized = self._find_known_iteration(iteration_ref)
                
                results.append(NormalizedEntity(
                    original=match.group(0),
                    normalized=normalized or iteration_ref,
                    entity_type=EntityType.ITERATION,
                    confidence=0.9 if normalized else 0.7
                ))
        
        return results
    
    def _find_known_iteration(self, iteration_ref: str) -> Optional[str]:
        """Find a matching known iteration for a reference."""
        ref_lower = iteration_ref.lower()
        
        for known in self.context.known_iterations:
            known_lower = known.lower()
            
            # Exact match
            if ref_lower == known_lower:
                return known
            
            # Partial match (e.g., "45" matches "Sprint 45")
            if ref_lower in known_lower:
                return known
            
            # Number match
            if ref_lower.isdigit():
                if f"sprint {ref_lower}" in known_lower or f"iteration {ref_lower}" in known_lower:
                    return known
        
        return None
    
    def build_wiql_conditions(self, entities: Dict[EntityType, List[NormalizedEntity]], project: str = None) -> List[str]:
        """
        Build WIQL WHERE conditions from normalized entities.
        
        Args:
            entities: Normalized entities from normalize_all()
            project: Project name for project filter
            
        Returns:
            List of WIQL condition strings
        """
        conditions = []
        
        if project:
            conditions.append(f"[System.TeamProject] = '{project}'")
        
        # States
        if EntityType.STATE in entities:
            all_states = set()
            for entity in entities[EntityType.STATE]:
                all_states.update(entity.normalized)
            if all_states:
                states_str = "', '".join(all_states)
                conditions.append(f"[System.State] IN ('{states_str}')")
        
        # Work item types
        if EntityType.WORK_ITEM_TYPE in entities:
            types = [e.normalized for e in entities[EntityType.WORK_ITEM_TYPE]]
            if types:
                types_str = "', '".join(types)
                conditions.append(f"[System.WorkItemType] IN ('{types_str}')")
        
        # Date range
        if EntityType.DATE_RANGE in entities:
            for entity in entities[EntityType.DATE_RANGE]:
                from_date = entity.normalized["from"]
                days_ago = (datetime.utcnow() - from_date).days
                conditions.append(f"[System.ChangedDate] >= @Today - {days_ago}")
        
        # Person
        if EntityType.PERSON in entities:
            for entity in entities[EntityType.PERSON]:
                conditions.append(f"[System.AssignedTo] CONTAINS '{entity.normalized}'")
        
        # Iteration
        if EntityType.ITERATION in entities:
            for entity in entities[EntityType.ITERATION]:
                if entity.normalized.startswith("@"):
                    conditions.append(f"[System.IterationPath] = {entity.normalized}")
                else:
                    conditions.append(f"[System.IterationPath] UNDER '{entity.normalized}'")
        
        # Priority
        if EntityType.PRIORITY in entities:
            priorities = [e.normalized for e in entities[EntityType.PRIORITY]]
            if priorities:
                priorities_str = "', '".join(priorities)
                conditions.append(f"[Microsoft.VSTS.Common.Priority] IN ('{priorities_str}')")
        
        return conditions
    
    def build_search_args(self, entities: Dict[EntityType, List[NormalizedEntity]], project: str = None) -> Dict[str, Any]:
        """
        Build search_workitem arguments from normalized entities.
        
        Args:
            entities: Normalized entities from normalize_all()
            project: Project name
            
        Returns:
            Dict of search_workitem arguments
        """
        args = {}
        
        if project:
            args["project"] = [project]
        
        # States
        if EntityType.STATE in entities:
            all_states = set()
            for entity in entities[EntityType.STATE]:
                all_states.update(entity.normalized)
            args["state"] = list(all_states)
        
        # Work item types
        if EntityType.WORK_ITEM_TYPE in entities:
            args["workItemType"] = [e.normalized for e in entities[EntityType.WORK_ITEM_TYPE]]
        
        # Person
        if EntityType.PERSON in entities:
            args["assignedTo"] = [e.normalized for e in entities[EntityType.PERSON]]
        
        return args


# ═══════════════════════════════════════════════════════════════════════════════
# CONVENIENCE FUNCTIONS
# ═══════════════════════════════════════════════════════════════════════════════

_normalizer: Optional[EntityNormalizer] = None

def get_entity_normalizer(context: NormalizationContext = None) -> EntityNormalizer:
    """Get the entity normalizer instance."""
    global _normalizer
    if _normalizer is None or context:
        _normalizer = EntityNormalizer(context)
    return _normalizer


def normalize_query_entities(query: str, project: str = None) -> Dict[str, Any]:
    """
    Convenience function to normalize all entities in a query.
    
    Args:
        query: Natural language query
        project: Optional project name
        
    Returns:
        Dict with normalized entities and suggested args
    """
    normalizer = get_entity_normalizer()
    entities = normalizer.normalize_all(query)
    
    return {
        "entities": {k.value: [e.normalized for e in v] for k, v in entities.items()},
        "wiql_conditions": normalizer.build_wiql_conditions(entities, project),
        "search_args": normalizer.build_search_args(entities, project),
    }


def normalize_date_expression(expression: str) -> Tuple[Optional[datetime], Optional[datetime]]:
    """
    Normalize a date expression to a date range.
    
    Args:
        expression: Date expression like "last week", "past 30 days"
        
    Returns:
        Tuple of (from_date, to_date)
    """
    normalizer = get_entity_normalizer()
    entities = normalizer.normalize_dates(expression)
    
    if entities:
        return entities[0].normalized["from"], entities[0].normalized["to"]
    
    return None, None


def normalize_state(state: str) -> List[str]:
    """
    Normalize a state to ADO state values.
    
    Args:
        state: State like "active", "open", "done"
        
    Returns:
        List of ADO state values
    """
    normalizer = get_entity_normalizer()
    entities = normalizer.normalize_states(state)
    
    if entities:
        return entities[0].normalized
    
    return [state]  # Return as-is if no mapping


def normalize_work_item_type(type_str: str) -> str:
    """
    Normalize a work item type to ADO type value.
    
    Args:
        type_str: Type like "bug", "story", "task"
        
    Returns:
        Normalized ADO type like "Bug", "User Story", "Task"
    """
    normalizer = get_entity_normalizer()
    entities = normalizer.normalize_work_item_types(type_str)
    
    if entities:
        return entities[0].normalized
    
    return type_str.title()  # Title case fallback
