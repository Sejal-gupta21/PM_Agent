"""
Entity Resolver - Flexible entity resolution for PM Agent.

Provides:
1. Name normalization and fuzzy matching
2. Entity type detection (person, work item, project, etc.)
3. ADO identity lookup via MCP/Graph API
4. Query entity extraction with confidence scoring

This module enables the chatbot to understand queries like:
- "bugs assigned to Richa" → resolve "Richa" to full identity
- "work items for John Doe" → fuzzy match against team members
- "email id for richa yadav" → identity lookup query
"""

import re
import logging
from difflib import SequenceMatcher
from typing import Optional, Dict, Any, List, Tuple
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class EntityType(Enum):
    """Types of entities that can be resolved."""
    PERSON = "person"          # User/developer identity
    WORK_ITEM = "work_item"    # Bug, Story, Task, etc.
    PROJECT = "project"        # ADO project
    AREA_PATH = "area_path"    # Area path
    ITERATION = "iteration"    # Sprint/iteration
    REPOSITORY = "repository"  # Git repo
    UNKNOWN = "unknown"


@dataclass
class ResolvedEntity:
    """Result of entity resolution."""
    entity_type: EntityType
    original_text: str
    resolved_value: Optional[str] = None
    confidence: float = 0.0
    alternatives: List[Dict[str, Any]] = field(default_factory=list)
    needs_clarification: bool = False
    clarification_message: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ExtractedEntities:
    """Collection of entities extracted from a query."""
    persons: List[str] = field(default_factory=list)
    work_item_ids: List[int] = field(default_factory=list)
    work_item_types: List[str] = field(default_factory=list)
    states: List[str] = field(default_factory=list)
    area_paths: List[str] = field(default_factory=list)
    projects: List[str] = field(default_factory=list)
    date_ranges: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)


# ============================================================================
# NAME NORMALIZATION AND MATCHING
# ============================================================================

def normalize_name(s: str) -> str:
    """
    Normalize a name/identity for comparison.
    
    All these formats normalize to the same result:
    - "John Smith" → "john smith"
    - "john.smith" → "john smith"
    - "john.smith@example.com" → "john smith"
    - "john_smith" → "john smith"
    
    This ensures consistent matching regardless of input format.
    """
    if not s:
        return ""
    
    # Lowercase and strip
    normalized = s.lower().strip()
    
    # Handle "Name <email>" format - extract name part
    match = re.match(r'^(.+?)\s*<[^>]+>$', normalized)
    if match:
        normalized = match.group(1).strip()
    
    # If it's an email, extract the local part (before @)
    if "@" in normalized:
        normalized = normalized.split("@")[0]
    
    # Replace dots, underscores, and hyphens with spaces (unify separators)
    normalized = re.sub(r'[._-]+', ' ', normalized)
    
    # Collapse multiple spaces
    normalized = re.sub(r'\s+', ' ', normalized).strip()
    
    return normalized


def similarity(a: str, b: str) -> float:
    """
    Calculate similarity ratio between two strings.
    
    Uses normalized comparison with SequenceMatcher for fuzzy matching.
    Returns value between 0.0 (no match) and 1.0 (exact match).
    """
    if not a or not b:
        return 0.0
    
    a_norm = normalize_name(a)
    b_norm = normalize_name(b)
    
    # Exact match after normalization
    if a_norm == b_norm:
        return 1.0
    
    # One is substring of other (e.g., "john" matches "john smith")
    if a_norm in b_norm or b_norm in a_norm:
        shorter = min(len(a_norm), len(b_norm))
        longer = max(len(a_norm), len(b_norm))
        if longer > 0:
            # Boost score for substring matches
            ratio = shorter / longer
            if ratio >= 0.4:
                return 0.80 + (ratio * 0.15)
    
    # Fuzzy match using SequenceMatcher
    return SequenceMatcher(None, a_norm, b_norm).ratio()


def looks_like_email(s: str) -> bool:
    """Check if string looks like an email address."""
    if not s:
        return False
    return bool(re.match(r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', s.strip()))


def extract_email_from_identity(identity: str) -> Optional[str]:
    """Extract email from identity string like 'Name <email@domain.com>'."""
    if not identity:
        return None
    # Try to extract email in angle brackets
    match = re.search(r'<([^>]+@[^>]+)>', identity)
    if match:
        return match.group(1)
    # Check if it's already an email
    if looks_like_email(identity):
        return identity.strip()
    return None


def extract_display_name(identity: str) -> str:
    """Extract display name from identity string."""
    if not identity:
        return ""
    # Remove email in angle brackets
    cleaned = re.sub(r'\s*<[^>]+>', '', identity).strip()
    return cleaned if cleaned else identity


# ============================================================================
# ENTITY EXTRACTION FROM QUERIES
# ============================================================================

# Patterns for person names in queries
PERSON_PATTERNS = [
    # "assigned to <name>" - handles various terminators including ?, !, end of string
    r'assigned\s+to\s+([a-zA-Z][a-zA-Z\s\.]+?)(?:\s+in|\s+for|\s+from|$|\s*[,\.\?\!])',
    # "created by <name>"
    r'created\s+by\s+([a-zA-Z][a-zA-Z\s\.]+?)(?:\s+in|\s+for|\s+from|$|\s*[,\.\?\!])',
    # "for <name>" (e.g., "bugs for John")
    r'(?:bugs?|items?|tasks?|stories?|work)\s+for\s+([a-zA-Z][a-zA-Z\s\.]+?)(?:\s+in|\s+from|$|\s*[,\.\?\!])',
    # "email/username/id for <name>"
    r'(?:email|username|id|mail)\s+(?:id\s+)?(?:for|of)\s+([a-zA-Z][a-zA-Z\s\.]+?)(?:\s*$|\s*[,\.\?\!])',
    # "<name>'s bugs/items"
    r"([a-zA-Z][a-zA-Z\s\.]+?)'s\s+(?:bugs?|items?|tasks?|work)",
]

# Patterns for work item types
WORK_ITEM_TYPE_PATTERNS = {
    "Bug": [r'\bbugs?\b', r'\bdefects?\b', r'\bissues?\b'],
    "User Story": [r'\buser\s*stor(?:y|ies)\b', r'\bstor(?:y|ies)\b'],
    "Task": [r'\btasks?\b'],
    "Feature": [r'\bfeatures?\b'],
    "Epic": [r'\bepics?\b'],
}

# Patterns for states - covers all 29 real ADO states
STATE_PATTERNS = {
    # Not Started
    "New": [r'\bnew\b'],
    "Ready": [r'\bready\b'],
    "Requested": [r'\brequested\b'],
    "Scheduled": [r'\bscheduled\b'],
    "In Planning": [r'\bin\s*planning\b'],
    "Accepted": [r'\baccepted\b'],
    # In Progress
    "Active": [r'\bactive\b', r'\bopen\b'],
    "Design": [r'\bdesign\b'],
    "Code Review": [r'\bcode\s*review\b', r'\bin\s*review\b'],
    "Code Complete": [r'\bcode\s*complete\b'],
    "QA": [r'\bqa\b', r'\btesting\b'],
    "QA Complete": [r'\bqa\s*complete\b'],
    "UAT": [r'\buat\b', r'\buser\s*acceptance\b'],
    "PRE-PROD": [r'\bpre[\s-]?prod\b'],
    "Approved for Production": [r'\bapproved\s*for\s*production\b'],
    "In Progress": [r'\bin\s*progress\b'],
    "Awaiting Approvals": [r'\bawaiting\s*approvals?\b'],
    # Completed
    "Closed": [r'\bclosed\b'],
    "Resolved": [r'\bresolved\b', r'\bfixed\b'],
    "Completed": [r'\bcompleted?\b', r'\bdone\b', r'\bfinished\b'],
    "UAT Complete": [r'\buat\s*complete\b'],
    "Released": [r'\breleased\b'],
    "Removed": [r'\bremoved\b'],
    "Not a Bug": [r'\bnot\s*a\s*bug\b', r'\bby\s*design\b'],
    "Requirement Bug": [r'\brequirement\s*bug\b'],
    # Blocked
    "On Hold": [r'\bon\s*hold\b'],
    "Issues Found": [r'\bissues?\s*found\b'],
    "Reopened": [r'\breopened?\b'],
    "Inactive": [r'\binactive\b'],
}

# Patterns for work item IDs
WORK_ITEM_ID_PATTERN = r'(?:^|[^\d])(\d{4,6})(?:[^\d]|$)'


def extract_entities(query: str) -> ExtractedEntities:
    """
    Extract all entities from a user query.
    
    Args:
        query: User's natural language query
        
    Returns:
        ExtractedEntities with all detected entities
    """
    entities = ExtractedEntities()
    query_lower = query.lower()
    
    # Extract person names
    for pattern in PERSON_PATTERNS:
        matches = re.findall(pattern, query_lower, re.IGNORECASE)
        for match in matches:
            name = match.strip()
            # Filter out common false positives
            if name and len(name) > 2 and name not in ['the', 'all', 'any', 'some']:
                entities.persons.append(name)
    
    # Extract work item IDs
    id_matches = re.findall(WORK_ITEM_ID_PATTERN, query)
    entities.work_item_ids = [int(m) for m in id_matches]
    
    # Extract work item types
    for wi_type, patterns in WORK_ITEM_TYPE_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, query_lower):
                if wi_type not in entities.work_item_types:
                    entities.work_item_types.append(wi_type)
                break
    
    # Extract states
    for state, patterns in STATE_PATTERNS.items():
        for pattern in patterns:
            if re.search(pattern, query_lower):
                if state not in entities.states:
                    entities.states.append(state)
                break
    
    # Extract keywords (remaining meaningful words)
    # Remove common stop words and already extracted entities
    stop_words = {
        'the', 'a', 'an', 'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by',
        'from', 'is', 'are', 'was', 'were', 'be', 'been', 'being',
        'have', 'has', 'had', 'do', 'does', 'did', 'will', 'would', 'could',
        'should', 'may', 'might', 'can', 'this', 'that', 'these', 'those',
        'i', 'me', 'my', 'we', 'our', 'you', 'your', 'he', 'she', 'it',
        'show', 'list', 'get', 'find', 'search', 'give', 'tell', 'what',
        'which', 'who', 'where', 'when', 'how', 'all', 'any', 'some',
        'assigned', 'created', 'updated', 'email', 'username', 'id',
    }
    
    words = re.findall(r'\b[a-zA-Z]{3,}\b', query_lower)
    for word in words:
        if word not in stop_words and word not in [p.lower() for p in entities.persons]:
            # Check if it's not part of a work item type or state
            is_type_or_state = False
            for patterns in list(WORK_ITEM_TYPE_PATTERNS.values()) + list(STATE_PATTERNS.values()):
                for pattern in patterns:
                    if re.match(pattern.replace(r'\b', ''), word):
                        is_type_or_state = True
                        break
            if not is_type_or_state and word not in entities.keywords:
                entities.keywords.append(word)
    
    return entities


# ============================================================================
# QUERY INTENT DETECTION
# ============================================================================

@dataclass
class QueryIntent:
    """Detected intent from user query."""
    intent_type: str  # 'search_work_items', 'get_work_item', 'identity_lookup', 'list_entities', 'general'
    confidence: float
    entities: ExtractedEntities
    requires_tool: bool = True
    suggested_tool: Optional[str] = None
    suggested_args: Dict[str, Any] = field(default_factory=dict)


def detect_query_intent(query: str) -> QueryIntent:
    """
    Detect the intent of a user query.
    
    Returns QueryIntent with detected intent type and extracted entities.
    """
    query_lower = query.lower().strip()
    entities = extract_entities(query)
    
    # Check for identity lookup queries
    identity_patterns = [
        r'(?:what\s+is|get|find|show)\s+(?:the\s+)?(?:email|username|id|mail).*(?:for|of)',
        r'email\s+(?:id\s+)?(?:for|of)',
        r'username\s+(?:for|of)',
        r'ado\s+(?:email|username|id)',
    ]
    for pattern in identity_patterns:
        if re.search(pattern, query_lower):
            return QueryIntent(
                intent_type='identity_lookup',
                confidence=0.9,
                entities=entities,
                requires_tool=True,
                suggested_tool='core_get_identity_ids',
                suggested_args={'searchFilter': entities.persons[0] if entities.persons else ''}
            )
    
    # Check for specific work item by ID
    if entities.work_item_ids:
        return QueryIntent(
            intent_type='get_work_item',
            confidence=0.95,
            entities=entities,
            requires_tool=True,
            suggested_tool='wit_get_work_item',
            suggested_args={'id': entities.work_item_ids[0]}
        )
    
    # Check for work item search
    if entities.work_item_types or entities.persons or entities.states:
        args: Dict[str, Any] = {'project': ['FracPro-OPS']}
        
        # Build search text from meaningful parts of query
        search_parts = []
        if entities.keywords:
            search_parts.extend(entities.keywords[:3])
        if not search_parts and entities.work_item_types:
            search_parts.append(entities.work_item_types[0])
        
        args['searchText'] = ' '.join(search_parts) if search_parts else 'Bug'
        
        if entities.work_item_types:
            args['workItemType'] = entities.work_item_types
        if entities.states:
            args['state'] = entities.states
        if entities.persons:
            # Will need to resolve person to email first
            args['_pending_person_resolution'] = entities.persons[0]
        
        return QueryIntent(
            intent_type='search_work_items',
            confidence=0.85,
            entities=entities,
            requires_tool=True,
            suggested_tool='search_workitem',
            suggested_args=args
        )
    
    # Check for simple list queries
    list_patterns = {
        'list_projects': [r'list\s+(?:all\s+)?projects?', r'show\s+(?:all\s+)?projects?'],
        'list_area_paths': [r'list\s+(?:all\s+)?area\s*paths?', r'show\s+(?:all\s+)?area\s*paths?'],
        'list_teams': [r'list\s+(?:all\s+)?teams?', r'show\s+(?:all\s+)?teams?'],
        'list_iterations': [r'list\s+(?:all\s+)?(?:iterations?|sprints?)', r'show\s+(?:all\s+)?(?:iterations?|sprints?)'],
    }
    
    for intent, patterns in list_patterns.items():
        for pattern in patterns:
            if re.search(pattern, query_lower):
                tool_map = {
                    'list_projects': 'core_list_projects',
                    'list_area_paths': 'list_area_paths',
                    'list_teams': 'core_list_project_teams',
                    'list_iterations': 'work_list_iterations',
                }
                return QueryIntent(
                    intent_type=intent,
                    confidence=0.9,
                    entities=entities,
                    requires_tool=True,
                    suggested_tool=tool_map.get(intent),
                    suggested_args={}
                )
    
    # Default: general query
    return QueryIntent(
        intent_type='general',
        confidence=0.5,
        entities=entities,
        requires_tool=True,
        suggested_tool=None,
        suggested_args={}
    )


# ============================================================================
# RESULT FILTERING
# ============================================================================

def filter_results_by_person(
    results: List[Dict[str, Any]],
    person_name: str,
    min_similarity: float = 0.65
) -> List[Dict[str, Any]]:
    """
    Filter work item results by assigned-to person using fuzzy matching.
    
    Args:
        results: List of work item dicts
        person_name: Target person name to match
        min_similarity: Minimum similarity threshold (0-1)
        
    Returns:
        Filtered list of matching work items
    """
    if not results or not person_name:
        return results
    
    target = normalize_name(person_name)
    filtered = []
    
    for item in results:
        # Try various locations for assigned-to field
        assigned_to = None
        
        # Direct field
        if 'assignedTo' in item:
            assigned_to = item['assignedTo']
        # In fields dict
        elif 'fields' in item:
            fields = item['fields']
            assigned_to = fields.get('System.AssignedTo') or fields.get('system.assignedto')
        # In nested workItem
        elif 'workItem' in item:
            wi_fields = item['workItem'].get('fields', {})
            assigned_to = wi_fields.get('System.AssignedTo')
        
        # Extract display name if identity object
        if isinstance(assigned_to, dict):
            assigned_to = assigned_to.get('displayName') or assigned_to.get('uniqueName') or ''
        
        if assigned_to:
            sim = similarity(str(assigned_to), target)
            if sim >= min_similarity:
                item['_match_score'] = sim
                filtered.append(item)
    
    # Sort by match score
    filtered.sort(key=lambda x: x.get('_match_score', 0), reverse=True)
    return filtered


def filter_results_by_state(
    results: List[Dict[str, Any]],
    state: str
) -> List[Dict[str, Any]]:
    """
    Filter work item results by state.
    
    Args:
        results: List of work item dicts
        state: Target state (e.g., "New", "Active")
        
    Returns:
        Filtered list
    """
    if not results or not state:
        return results
    
    state_lower = state.lower()
    filtered = []
    
    for item in results:
        item_state = None
        
        if 'state' in item:
            item_state = item['state']
        elif 'fields' in item:
            fields = item['fields']
            item_state = fields.get('System.State') or fields.get('system.state')
        elif 'workItem' in item:
            wi_fields = item['workItem'].get('fields', {})
            item_state = wi_fields.get('System.State')
        
        if item_state and item_state.lower() == state_lower:
            filtered.append(item)
    
    return filtered


# ============================================================================
# CLARIFICATION HELPERS
# ============================================================================

def needs_clarification(entities: ExtractedEntities, results: List[Dict] = None) -> Tuple[bool, Optional[str]]:
    """
    Determine if clarification is needed based on extracted entities.
    
    Returns:
        Tuple of (needs_clarification, clarification_message)
    """
    # Multiple person names detected
    if len(entities.persons) > 1:
        return True, f"I found multiple people mentioned: {', '.join(entities.persons)}. Which one did you mean?"
    
    # Ambiguous work item type
    if not entities.work_item_types and not entities.work_item_ids and not entities.persons:
        # Query is too vague
        return False, None  # Let LLM handle it
    
    return False, None


def build_clarification_question(
    entity_type: EntityType,
    matches: List[Dict[str, Any]],
    original_query: str
) -> str:
    """
    Build a clarification question for ambiguous entity matches.
    
    Args:
        entity_type: Type of entity being resolved
        matches: List of possible matches
        original_query: Original user query
        
    Returns:
        Clarification question string
    """
    if entity_type == EntityType.PERSON:
        if len(matches) <= 5:
            options = [f"- {m.get('displayName', m.get('name', '?'))}" for m in matches]
            return f"I found multiple people matching your query. Did you mean:\n" + "\n".join(options)
        else:
            return f"I found {len(matches)} people matching your query. Could you provide the full name or email address?"
    
    return "Could you provide more details about your request?"
