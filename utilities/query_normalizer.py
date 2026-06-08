# -*- coding: utf-8 -*-
"""Query Normalizer for Natural Language Understanding.

This module provides intelligent query normalization to handle casual,
ambiguous, and imprecise user queries. It uses a combination of:
1. Pattern-based normalization for common casual phrases
2. Synonym expansion for technical terms
3. Intent classification for ambiguous queries

The goal is to transform user queries into clearer, more structured queries
that the LLM planner can understand better, WITHOUT hardcoding responses.

Examples:
- "what's up with the sprint?" → "what is the current sprint status?"
- "any issues?" → "are there any blocked or at-risk work items?"
- "stuff that's stuck" → "show me blocked or stale work items"
- "things taking too long" → "show me overdue or slow-moving work items"
"""
from __future__ import annotations

import re
import logging
from functools import lru_cache
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# =============================================================================
# SYNONYM MAPPINGS - Map casual terms to technical terms
# =============================================================================

# Sprint/Iteration synonyms
SPRINT_SYNONYMS = {
    r'\b(sprint|iteration|cycle|release)\b': 'sprint',
    r'\b(current|this|ongoing|active)\s+(sprint|iteration)\b': 'current sprint',
    r'\b(last|previous|prior|past)\s+(sprint|iteration)\b': 'previous sprint',
    r'\b(next|upcoming|following)\s+(sprint|iteration)\b': 'next sprint',
}

# Status/State synonyms  
STATUS_SYNONYMS = {
    r'\b(stuck|stalled|halted|stopped|frozen|held up)\b': 'blocked',
    r'\b(slow|lagging|behind|delayed|overdue|late)\b': 'slow-moving',
    r'\b(risky|at.?risk|concerning|worrying|problematic)\b': 'at-risk',
    r'\b(done|finished|complete|completed)\b': 'closed',
    r'\b(new|fresh|just.?created|recently.?added)\b': 'new',
    r'\b(in.?progress|ongoing|working.?on|active)\b': 'active',
    r'\b(not.?started|waiting|pending|queued)\b': 'not started',
    r'\b(forgotten|neglected|overlooked|ignored|abandoned)\b': 'stale',
}

# Work item type synonyms
WORK_ITEM_SYNONYMS = {
    r'\b(bugs?|defects?|issues?|errors?|problems?)\b': 'bugs',
    r'\b(stories|user.?stories|features?|requirements?)\b': 'user stories',
    r'\b(tasks?|to.?dos?|work.?items?|items?|things?|stuff)\b': 'work items',
    r'\b(epics?|initiatives?)\b': 'epics',
    r'\b(blockers?|impediments?|obstacles?)\b': 'blockers',
}

# Query intent patterns - map casual questions to structured queries
CASUAL_QUERY_PATTERNS = [
    # Sprint status queries
    (r"^what'?s?\s+up(\s+with)?(\s+the)?(\s+sprint)?(\?)?$", "what is the current sprint status"),
    (r"^how('?s|.?is)?\s+(the\s+)?sprint(\s+going)?(\?)?$", "what is the current sprint status"),
    (r"^how'?s?\s+it\s+going(\?)?$", "what is the current sprint status"),  # "how's it going" / "hows it going"
    (r"^how\s+are\s+we\s+doing(\s+in\s+(the\s+)?sprint)?(\?)?$", "what is the current sprint status"),
    (r"^sprint\s+status(\?)?$", "what is the current sprint status"),
    (r"^(show|give|get)\s+(me\s+)?sprint(\s+status)?(\?)?$", "what is the current sprint status"),
    (r"^status(\?)?$", "what is the current sprint status"),  # Single word "status"
    (r"^sprint(\?)?$", "what is the current sprint status"),  # Single word "sprint"
    (r"^progress(\?)?$", "what is the current sprint progress"),  # Single word "progress"
    
    # Issue/Problem queries
    (r"^any\s+(issues?|problems?)(\?)?$", "are there any blocked or at-risk work items in the current sprint"),
    (r"^(are\s+there\s+)?any\s+blockers?(\?)?$", "show me blocked work items in the current sprint"),
    (r"^what('s|.?is)?\s+blocking(\s+us)?(\?)?$", "show me blocked work items in the current sprint"),
    
    # Stuck/Stale queries
    (r"^(stuff|things?|items?)\s+(that'?s?|that\s+are)\s+stuck(\?)?$", "show me blocked or stale work items"),
    (r"^stuck\s+(items?|stuff|things?)(\?)?$", "show me blocked work items"),
    (r"^(what'?s?|show)\s+stuck(\?)?$", "show me blocked work items"),
    (r"^stuck(\?)?$", "show me blocked work items"),  # Single word "stuck"
    (r"^stale\s+(items?|stories?|stuff)(\?)?$", "show me overlooked or stale user stories"),
    (r"^(things?|items?|stuff)\s+not\s+moving(\?)?$", "show me blocked work items"),  # Added pattern
    
    # Slow/Delayed queries
    (r"^(things?|stuff|items?)\s+taking\s+too\s+long(\?)?$", "show me slow-moving or overdue work items"),
    (r"^(what'?s?|show)\s+(running\s+)?late(\?)?$", "show me overdue or delayed work items"),
    (r"^(slow|delayed)\s+(items?|stuff|things?)(\?)?$", "show me slow-moving work items"),
    
    # Progress/Status queries
    (r"^(where\s+are\s+we|what'?s?\s+the\s+progress)(\?)?$", "what is the current sprint status"),
    (r"^(how\s+much\s+is\s+)?done(\?)?$", "how many work items are completed in the current sprint"),
    (r"^(what'?s?\s+)?left(\s+to\s+do)?(\?)?$", "how many work items are remaining in the current sprint"),
    
    # Unassigned queries
    (r"^(any\s+)?unassigned(\s+items?)?(\?)?$", "show me unassigned work items in the current sprint"),
    (r"^(what'?s?|show)\s+unassigned(\?)?$", "show me unassigned work items in the current sprint"),
    (r"^(items?|stuff|things?)\s+without\s+(an?\s+)?owner(\?)?$", "show me unassigned work items"),
    
    # Recurring/Pattern queries  
    (r"^recurring(\s+bugs?)?(\?)?$", "show me recurring bugs by area path"),
    (r"^(same|similar)\s+bugs?(\s+again)?(\?)?$", "show me recurring bugs by area path"),
    (r"^bug\s+patterns?(\?)?$", "analyze bug patterns and recurring issues"),
    
    # Report/Summary queries
    (r"^(give|show|get)\s+(me\s+)?(a\s+)?report(\?)?$", "generate the iteration report"),
    (r"^summary(\?)?$", "show me a summary of the current sprint"),
    (r"^(what'?s?\s+the\s+)?overview(\?)?$", "what is the current sprint overview"),
]


@dataclass
class NormalizedQuery:
    """Result of query normalization."""
    original: str
    normalized: str
    transformations: List[str] = field(default_factory=list)
    detected_intent: Optional[str] = None
    confidence: float = 1.0
    context_hints: Dict[str, Any] = field(default_factory=dict)


def normalize_query(query: str) -> NormalizedQuery:
    """
    Normalize a user query to improve understanding.
    
    This function applies multiple transformations:
    1. Trim and lowercase for pattern matching
    2. Apply casual query pattern matching
    3. Expand synonyms for technical terms
    4. Extract implicit context (sprint, time references)
    
    Args:
        query: The raw user query
        
    Returns:
        NormalizedQuery with the normalized query and metadata
    """
    if not query:
        return NormalizedQuery(original=query, normalized=query)
    
    original = query.strip()
    normalized = original
    transformations: List[str] = []
    context_hints: Dict[str, Any] = {}
    detected_intent: Optional[str] = None
    confidence = 1.0
    
    # Lowercase for pattern matching, but preserve original for comparison
    query_lower = original.lower().strip()
    
    # Step 1: Apply casual query patterns first (highest priority)
    for pattern, replacement in CASUAL_QUERY_PATTERNS:
        if re.match(pattern, query_lower, re.IGNORECASE):
            normalized = replacement
            transformations.append(f"Casual pattern: '{pattern}' → '{replacement}'")
            detected_intent = _infer_intent_from_normalized(replacement)
            logger.debug(f"[QueryNormalizer] Matched casual pattern: {original} → {normalized}")
            break
    
    # Step 2: If no casual pattern matched, apply synonym expansion
    if not transformations:
        temp_normalized = query_lower
        
        # Expand sprint synonyms
        for pattern, replacement in SPRINT_SYNONYMS.items():
            if re.search(pattern, temp_normalized, re.IGNORECASE):
                temp_normalized = re.sub(pattern, replacement, temp_normalized, flags=re.IGNORECASE)
                transformations.append(f"Sprint synonym: '{pattern}' → '{replacement}'")
        
        # Expand status synonyms
        for pattern, replacement in STATUS_SYNONYMS.items():
            if re.search(pattern, temp_normalized, re.IGNORECASE):
                temp_normalized = re.sub(pattern, replacement, temp_normalized, flags=re.IGNORECASE)
                transformations.append(f"Status synonym: '{pattern}' → '{replacement}'")
        
        # Expand work item synonyms
        for pattern, replacement in WORK_ITEM_SYNONYMS.items():
            if re.search(pattern, temp_normalized, re.IGNORECASE):
                temp_normalized = re.sub(pattern, replacement, temp_normalized, flags=re.IGNORECASE)
                transformations.append(f"Work item synonym: '{pattern}' → '{replacement}'")
        
        if transformations:
            normalized = temp_normalized
            logger.debug(f"[QueryNormalizer] Applied synonyms: {original} → {normalized}")
    
    # Step 3: Extract implicit context hints
    context_hints = _extract_context_hints(original)
    
    # Step 4: Detect intent if not already set
    if not detected_intent:
        detected_intent = _infer_intent_from_normalized(normalized)
    
    # Step 5: Calculate confidence based on transformations
    if transformations:
        # Pattern match is high confidence, synonym expansion is medium
        has_pattern_match = any("Casual pattern" in t for t in transformations)
        confidence = 0.95 if has_pattern_match else 0.85
    else:
        # No normalization applied - original query is used as-is
        confidence = 1.0
    
    return NormalizedQuery(
        original=original,
        normalized=normalized,
        transformations=transformations,
        detected_intent=detected_intent,
        confidence=confidence,
        context_hints=context_hints
    )


def _extract_context_hints(query: str) -> Dict[str, Any]:
    """Extract implicit context from the query."""
    hints: Dict[str, Any] = {}
    query_lower = query.lower()
    
    # Sprint/Iteration context
    if re.search(r'\b(current|this|my)\s+(sprint|iteration)\b', query_lower):
        hints['iteration'] = '@CurrentIteration'
    elif re.search(r'\b(last|previous|prior)\s+(sprint|iteration)\b', query_lower):
        hints['iteration'] = '@PreviousIteration'
    elif re.search(r'\bsprint\s*#?\d+\b', query_lower):
        match = re.search(r'\bsprint\s*#?(\d+)\b', query_lower)
        if match:
            hints['sprint_number'] = int(match.group(1))
    
    # Time context
    if re.search(r'\b(today|now|currently)\b', query_lower):
        hints['time_scope'] = 'today'
    elif re.search(r'\b(this week|week)\b', query_lower):
        hints['time_scope'] = 'this_week'
    elif re.search(r'\b(yesterday|last\s+day)\b', query_lower):
        hints['time_scope'] = 'yesterday'
    
    # Work item type hints
    if 'bug' in query_lower or 'defect' in query_lower:
        hints['work_item_type'] = 'Bug'
    elif 'story' in query_lower or 'stories' in query_lower:
        hints['work_item_type'] = 'User Story'
    elif 'task' in query_lower:
        hints['work_item_type'] = 'Task'
    
    # State hints
    if re.search(r'\b(blocked|stuck|stalled)\b', query_lower):
        hints['state_filter'] = 'blocked'
    elif re.search(r'\b(closed|done|completed|resolved)\b', query_lower):
        hints['state_filter'] = 'closed'
    elif re.search(r'\b(active|in\s*progress|ongoing)\b', query_lower):
        hints['state_filter'] = 'active'
    elif re.search(r'\b(new|not\s*started)\b', query_lower):
        hints['state_filter'] = 'new'
    
    # Area path extraction: capture patterns like
    # - "in the xops bugs enhancements area path"
    # - "area path XOPS\\Bugs\\Enhancement"
    # Strategy: Find words IMMEDIATELY before "area" or "area path", but exclude common query terms
    # Look for 1-4 words (with allowed separators) before "area"
    area_match = re.search(
        r"\b(?!(?:current|sprint|iteration|the|in|of|from|for|with)\s)((?:[A-Za-z0-9]+(?:[\s\\/\\\\_-][A-Za-z0-9]+){0,3}))\s+area(?:\s+path)?(?:\s|$)",
        query,
        re.IGNORECASE
    )
    if not area_match:
        # Try alternative pattern: "area path <name>"
        area_match = re.search(
            r"area(?:\s+path)?\s+([A-Za-z0-9](?:[A-Za-z0-9\\/\\\\ _-]){1,60})(?:\s|$)",
            query,
            re.IGNORECASE
        )
    
    if area_match:
        raw_area = area_match.group(1).strip()
        # Remove common leading/trailing words as a safety net
        raw_area = re.sub(r'^(?:current|sprint|iteration|the|in|of|from|for|with)\s+', '', raw_area, flags=re.IGNORECASE)
        raw_area = re.sub(r'\s+(?:in|for|with|on|state|assigned|and|the|a|an)$', '', raw_area, flags=re.IGNORECASE)
        raw_area = raw_area.strip()
        
        # Store the cleaned user text for resolver (don't normalize yet - resolver will do fuzzy matching)
        if raw_area:  # Only add if non-empty after cleanup
            hints['areaPath'] = raw_area

    return hints


    # Area path extraction (try to capture area path like 'XOPS Bugs Enhancement' or 'XOPS\\Bugs\\Enhancements')


def _infer_intent_from_normalized(normalized_query: str) -> Optional[str]:
    """Infer the intent from a normalized query string."""
    q = normalized_query.lower()
    
    # Sprint status/overview
    if 'sprint status' in q or 'sprint overview' in q or 'how are we doing' in q:
        return 'sprint_status'
    
    # Blocked items
    if 'blocked' in q or 'blocker' in q:
        return 'blocked_items'
    
    # Stale/Overlooked items
    if 'stale' in q or 'overlooked' in q or 'forgotten' in q:
        return 'stale_items'
    
    # Slow/Overdue items
    if 'slow' in q or 'overdue' in q or 'delayed' in q or 'late' in q:
        return 'slow_items'
    
    # Unassigned items
    if 'unassigned' in q:
        return 'unassigned_items'
    
    # Recurring bugs
    if 'recurring' in q or 'pattern' in q:
        return 'recurring_bugs'
    
    # Iteration report
    if 'iteration report' in q or 'sprint report' in q:
        return 'iteration_report'
    
    # Completed items
    if 'completed' in q or 'done' in q or 'closed' in q:
        return 'completed_items'
    
    # Remaining items
    if 'remaining' in q or 'left' in q:
        return 'remaining_items'
    
    return None


def enhance_query_for_llm(query: str, context: Optional[Dict[str, Any]] = None) -> Tuple[str, Dict[str, Any]]:
    """
    Enhance a query for better LLM understanding.
    
    This is the main entry point for query preprocessing.
    It normalizes the query and enriches the context.
    
    Args:
        query: The user's raw query
        context: Optional existing context to enrich
        
    Returns:
        Tuple of (enhanced_query, enriched_context)
    """
    if context is None:
        context = {}
    
    # Normalize the query
    normalized = normalize_query(query)
    
    # Enrich context with hints from normalization
    enriched_context = dict(context)
    if normalized.context_hints:
        enriched_context.update(normalized.context_hints)
    
    # Add normalization metadata to context for LLM
    if normalized.transformations:
        enriched_context['_query_normalized'] = True
        enriched_context['_original_query'] = normalized.original
        enriched_context['_detected_intent'] = normalized.detected_intent
    
    # Use normalized query if significant changes were made
    enhanced_query = normalized.normalized if normalized.transformations else query
    
    logger.debug(f"[QueryNormalizer] Enhanced: '{query}' → '{enhanced_query}' (intent: {normalized.detected_intent})")
    
    return enhanced_query, enriched_context


def get_query_understanding_prompt() -> str:
    """
    Get additional prompt text to help LLM understand casual queries.
    
    This returns instructions to be appended to the LLM system prompt
    to improve its ability to handle ambiguous queries.
    """
    return """
QUERY UNDERSTANDING RULES:
When users ask casual or informal questions, interpret them intelligently:

1. SPRINT STATUS QUERIES:
   - "what's up?" / "how's it going?" / "where are we?" → Sprint status overview
   - "any issues?" / "any problems?" → Check for blocked or at-risk items
   - "are we on track?" → Sprint progress and potential blockers

2. WORK ITEM QUERIES:
   - "stuff that's stuck" / "things not moving" → Blocked work items
   - "what's taking too long?" / "slow items" → Overdue or slow-moving items
   - "forgotten items" / "neglected stories" → Stale or overlooked user stories
   - "unowned items" / "orphan tasks" → Unassigned work items

3. STATUS INTERPRETATION:
   - stuck/stalled/halted/frozen → blocked
   - slow/lagging/behind/delayed/late → overdue or slow-moving
   - forgotten/neglected/overlooked → stale
   - done/finished/complete → closed
   - in progress/ongoing/working on → active

4. TIME REFERENCE HANDLING:
   - "this sprint" / "current sprint" / "my sprint" → @CurrentIteration
   - "last sprint" / "previous sprint" → @PreviousIteration
   - No explicit sprint reference with sprint-related query → assume @CurrentIteration

5. AMBIGUITY RESOLUTION:
   - For vague queries, choose the most likely interpretation based on context
   - If truly ambiguous between 2-3 options, prefer sprint status as the default
   - Only ask for clarification when the query could mean completely different things

EXAMPLES OF CASUAL → STRUCTURED:
- "what's up?" → current sprint status
- "any blockers?" → blocked items in current sprint
- "things that are stuck" → blocked work items
- "what's left?" → remaining incomplete items in sprint
- "how'd we do?" (past tense) → previous sprint summary
"""


# =============================================================================
# INTENT-TO-SKILL MAPPING
# =============================================================================

INTENT_TO_SKILL_MAP = {
    'sprint_status': {
        'skill_id': 'get_sprint_status',
        'tool': 'wit_get_work_items_for_iteration',
        'description': 'Get current sprint status with counts and blockers'
    },
    'blocked_items': {
        'skill_id': 'get_sprint_status',
        'tool': 'wit_get_work_items_for_iteration',
        'description': 'Get blocked work items in current sprint'
    },
    'stale_items': {
        'skill_id': 'overlooked_stories',
        'tool': 'overlooked_stories',
        'description': 'Find overlooked or stale user stories'
    },
    'slow_items': {
        'skill_id': 'get_sprint_status',
        'tool': 'wit_get_work_items_for_iteration',
        'description': 'Find slow-moving or overdue items'
    },
    'unassigned_items': {
        'skill_id': 'get_sprint_status',
        'tool': 'wit_get_work_items_for_iteration',
        'description': 'Find unassigned work items'
    },
    'recurring_bugs': {
        'skill_id': 'bug_areas_highlight',
        'tool': 'bug_areas_highlight',
        'description': 'Analyze recurring bugs by area path'
    },
    'iteration_report': {
        'skill_id': 'iteration_report',
        'tool': 'iteration_report',
        'description': 'Generate full iteration/sprint report'
    },
    'completed_items': {
        'skill_id': 'get_sprint_status',
        'tool': 'wit_get_work_items_for_iteration',
        'description': 'Get completed items in sprint'
    },
    'remaining_items': {
        'skill_id': 'get_sprint_status',
        'tool': 'wit_get_work_items_for_iteration',
        'description': 'Get remaining incomplete items in sprint'
    },
}


def get_skill_for_intent(intent: str) -> Optional[Dict[str, Any]]:
    """Get the recommended skill/tool for a detected intent."""
    return INTENT_TO_SKILL_MAP.get(intent)


# =============================================================================
# CACHED NORMALIZATION (Performance Optimization)
# =============================================================================

@lru_cache(maxsize=256)
def normalize_query_cached(query: str) -> NormalizedQuery:
    """
    Cached version of normalize_query for performance optimization.
    
    Uses LRU cache to avoid re-normalizing the same queries repeatedly.
    Cache size is 256 to handle typical conversation patterns without
    excessive memory usage.
    
    Args:
        query: The raw user query (must be hashable string)
        
    Returns:
        NormalizedQuery with the normalized query and metadata
    """
    return normalize_query(query)


def clear_normalization_cache():
    """Clear the normalization cache. Use when testing or if cache gets stale."""
    normalize_query_cached.cache_clear()
    logger.debug("Cleared query normalization cache")
