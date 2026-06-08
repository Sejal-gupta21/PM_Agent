"""
Query-Aware Filter - Intelligent post-filtering for sprint work items.

This module analyzes the user's query intent and filters work items accordingly.
It handles queries like:
- "items with 'Blocked' tag" → filter by tags
- "items with no activity in 7 days" → filter by ChangedDate
- "items at risk of spilling" → analyze state vs remaining time
- "customer-impacting items" → filter by priority, tags, area path
- "blocked or unassigned items" → filter by state/tags and assignment
"""

import re
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass

logger = logging.getLogger(__name__)
# ═══════════════════════════════════════════════════════════
# STATE NORMALIZATION - Maps user input to exact ADO states
# Covers all 29 real ADO states
# ═══════════════════════════════════════════════════════════
STATE_NORMALIZATION = {
    # Not Started
    "new": "New",
    "ready": "Ready",
    "requested": "Requested",
    "request": "Requested",
    "scheduled": "Scheduled",
    "in planning": "In Planning",
    "planning": "In Planning",
    "accepted": "Accepted",
    # In Progress
    "active": "Active",
    "open": "Active",
    "working": "Active",
    "design": "Design",
    "code review": "Code Review",
    "codereview": "Code Review",
    "in review": "Code Review",
    "code complete": "Code Complete",
    "codecomplete": "Code Complete",
    "qa": "QA",
    "testing": "QA",
    "qa complete": "QA Complete",
    "qacomplete": "QA Complete",
    "uat": "UAT",
    "user acceptance": "UAT",
    "uat complete": "UAT Complete",
    "uatcomplete": "UAT Complete",
    "uat done": "UAT Complete",
    "pre-prod": "PRE-PROD",
    "preprod": "PRE-PROD",
    "pre prod": "PRE-PROD",
    "in progress": "In Progress",
    "in-progress": "In Progress",
    "approved for production": "Approved for Production",
    "approved": "Approved for Production",
    "awaiting approvals": "Awaiting Approvals",
    "awaiting approval": "Awaiting Approvals",
    "pending approval": "Awaiting Approvals",
    # Completed
    "closed": "Closed",
    "resolved": "Resolved",
    "fixed": "Resolved",
    "completed": "Completed",
    "done": "Completed",
    "finished": "Completed",
    "released": "Released",
    "removed": "Removed",
    "not a bug": "Not a Bug",
    "notabug": "Not a Bug",
    "requirement bug": "Requirement Bug",
    # Blocked
    "on hold": "On Hold",
    "onhold": "On Hold",
    "hold": "On Hold",
    "waiting": "On Hold",
    "issues found": "Issues Found",
    "issuesfound": "Issues Found",
    "issues": "Issues Found",
    "reopened": "Reopened",
    "reopen": "Reopened",
    "inactive": "Inactive",
}

# ═══════════════════════════════════════════════════════════
# TAG NORMALIZATION - Maps user input to ADO tags
# ═══════════════════════════════════════════════════════════
TAG_KEYWORDS = {
    # Blocking/Impediment tags
    "blocked": ["Blocked", "Blocker", "Impediment"],
    "blocker": ["Blocked", "Blocker", "Impediment"],
    "blocking": ["Blocked", "Blocker", "Impediment"],
    "impediment": ["Blocked", "Blocker", "Impediment"],
    "stuck": ["Blocked", "Blocker", "Impediment"],
    # Customer/Priority tags
    "customer": ["Customer", "Customer-Impact", "Customer-Facing"],
    "client": ["Customer", "Client", "Customer-Impact"],
    "critical": ["Critical", "P1", "Sev1"],
    "urgent": ["Urgent", "Critical", "P1"],
    "high priority": ["P1", "Critical", "High-Priority"],
    "p1": ["P1", "Critical"],
    "p2": ["P2"],
    # Technical tags
    "bug": ["Bug"],
    "enhancement": ["Enhancement"],
    "feature": ["Feature"],
    "technical debt": ["Tech-Debt", "Technical-Debt"],
    "tech debt": ["Tech-Debt", "Technical-Debt"],
    # Process tags
    "hotfix": ["Hotfix", "Emergency"],
    "production": ["Production", "Prod"],
    "prod": ["Production", "Prod"],
}


def normalize_state(state_input: str) -> str:
    """Normalize user state input to exact ADO state name."""
    if not state_input:
        return ""
    normalized = STATE_NORMALIZATION.get(state_input.lower().strip())
    return normalized if normalized else state_input


def get_tags_for_keyword(keyword: str) -> List[str]:
    """Get ADO tags for a user keyword."""
    return TAG_KEYWORDS.get(keyword.lower().strip(), [])

@dataclass
class QueryIntent:
    """Represents the analyzed intent from user query."""
    filter_by_tags: List[str]  # Tags to filter by (e.g., ["Blocked", "Blocker"])
    filter_by_states: List[str]  # States to filter by (e.g., ["Active", "New"])
    filter_by_types: List[str]  # Work item types (e.g., ["Bug", "User Story"])
    filter_by_priority: Optional[int]  # Priority threshold (1=highest)
    filter_unassigned: bool  # Filter for unassigned items
    filter_by_activity_days: Optional[int]  # Items with no activity in N days
    filter_at_risk: bool  # Items at risk of missing sprint
    filter_blocked: bool  # Items that are blocked
    filter_customer_impacting: bool  # Customer-impacting items
    filter_high_priority: bool  # High priority items only
    filter_stale: bool  # Stale/stuck items
    filter_awaiting_review: bool  # Items awaiting PR/review
    sort_by: Optional[str]  # "age", "priority", "changed_date"
    limit: Optional[int]  # Top N items
    original_query: str
    
    def has_active_filters(self) -> bool:
        """Check if any meaningful filters are active."""
        return (
            len(self.filter_by_tags) > 0 or
            len(self.filter_by_states) > 0 or
            len(self.filter_by_types) > 0 or
            self.filter_by_priority is not None or
            self.filter_unassigned or
            self.filter_by_activity_days is not None or
            self.filter_at_risk or
            self.filter_blocked or
            self.filter_customer_impacting or
            self.filter_high_priority or
            self.filter_stale or
            self.filter_awaiting_review
        )


def analyze_query_intent(query: str) -> QueryIntent:
    """
    Analyze user query to extract filtering intent.
    
    FIX #4: Filters are now applied independently with relaxed fallback.
    When state='On Hold' filters out all items, we relax the filter instead of failing.
    
    Args:
        query: User's natural language query
        
    Returns:
        QueryIntent with extracted filters
    """
    q = query.lower()
    
    # Initialize intent
    intent = QueryIntent(
        filter_by_tags=[],
        filter_by_states=[],
        filter_by_types=[],
        filter_by_priority=None,
        filter_unassigned=False,
        filter_by_activity_days=None,
        filter_at_risk=False,
        filter_blocked=False,
        filter_customer_impacting=False,
        filter_high_priority=False,
        filter_stale=False,
        filter_awaiting_review=False,
        sort_by=None,
        limit=None,
        original_query=query
    )
    
    # ═══════════════════════════════════════════════════════════
    # DETECT TAG FILTER (Blocked, Customer, Critical, etc.)
    # ═══════════════════════════════════════════════════════════
    # Check all known tag keywords
    for keyword, tags in TAG_KEYWORDS.items():
        # Create pattern for this keyword
        keyword_patterns = [
            rf"\b{re.escape(keyword)}\b",
            rf"{re.escape(keyword)}\s+tag",
            rf"tag.*{re.escape(keyword)}",
            rf"with.*{re.escape(keyword)}",
            rf"have.*{re.escape(keyword)}",
        ]
        
        for pattern in keyword_patterns:
            if re.search(pattern, q, re.IGNORECASE):
                intent.filter_by_tags.extend(tags)
                if keyword in ["blocked", "blocker", "blocking", "impediment", "stuck"]:
                    intent.filter_blocked = True
                if keyword in ["customer", "client", "critical", "urgent", "high priority", "p1"]:
                    intent.filter_customer_impacting = True
                logger.debug(f"[QUERY_INTENT] Detected tag keyword '{keyword}' → tags: {tags}")
                break
    
    # ═══════════════════════════════════════════════════════════
    # DETECT UNASSIGNED FILTER
    # ═══════════════════════════════════════════════════════════
    unassigned_patterns = [
        r"unassigned",
        r"no\s+owner",
        r"no\s+assignee",
        r"without\s+owner",
        r"not\s+assigned",
        r"missing\s+assignee"
    ]
    for pattern in unassigned_patterns:
        if re.search(pattern, q):
            intent.filter_unassigned = True
            break
    
    # ═══════════════════════════════════════════════════════════
    # DETECT ACTIVITY/STALE FILTER
    # ═══════════════════════════════════════════════════════════
    # Patterns like "no activity in X days", "inactive for X days"
    activity_patterns = [
        r"no\s+activity\s+(?:in\s+)?(?:the\s+)?(?:last\s+)?(\d+)\s+days?",
        r"inactive\s+(?:for\s+)?(\d+)\s+days?",
        r"haven'?t\s+(?:been\s+)?(?:updated|changed|progressed)\s+(?:in|for)\s+(?:more\s+than\s+)?(\d+)\s+days?",
        r"no\s+(?:update|change|progress)\s+(?:in|for)\s+(\d+)\s+days?",
        r"stale\s+(?:for\s+)?(\d+)\s+days?",
        r"not\s+progressed\s+(?:for\s+)?(?:more\s+than\s+)?(\d+)\s+days?"
    ]
    for pattern in activity_patterns:
        m = re.search(pattern, q)
        if m:
            intent.filter_by_activity_days = int(m.group(1))
            intent.filter_stale = True
            break
    
    # Generic stale detection without specific days
    if not intent.filter_stale:
        stale_patterns = [r"\bstale\b", r"stuck", r"no\s+progress", r"inactive"]
        for pattern in stale_patterns:
            if re.search(pattern, q):
                intent.filter_stale = True
                intent.filter_by_activity_days = 7  # Default to 7 days
                break
    
    # ═══════════════════════════════════════════════════════════
    # DETECT AT-RISK/SPILLING FILTER
    # ═══════════════════════════════════════════════════════════
    risk_patterns = [
        r"at\s+risk",
        r"risk\s+of\s+spill",
        r"spill(?:ing)?(?:\s+to)?(?:\s+next)?",
        r"carry\s*over",
        r"won'?t\s+(?:be\s+)?(?:complete|finish)",
        r"might\s+not\s+(?:complete|finish)",
        r"slipping",
        r"behind\s+schedule",
        r"overdue",
        r"delayed"
    ]
    for pattern in risk_patterns:
        if re.search(pattern, q):
            intent.filter_at_risk = True
            break
    
    # ═══════════════════════════════════════════════════════════
    # DETECT CUSTOMER-IMPACTING/CRITICAL FILTER
    # ═══════════════════════════════════════════════════════════
    critical_patterns = [
        r"customer[\s-]?impact",
        r"client[\s-]?impact",
        r"critical",
        r"urgent",
        r"high[\s-]?priority",
        r"priority\s*1",
        r"p1",
        r"sev\s*1",
        r"severity\s*1",
        r"production[\s-]?issue",
        r"prod\s+issue"
    ]
    for pattern in critical_patterns:
        if re.search(pattern, q):
            intent.filter_customer_impacting = True
            intent.filter_high_priority = True
            break
    
    # ═══════════════════════════════════════════════════════════
    # DETECT AWAITING REVIEW/PR FILTER
    # ═══════════════════════════════════════════════════════════
    review_patterns = [
        r"awaiting\s+(?:code\s+)?review",
        r"pending\s+(?:code\s+)?review",
        r"awaiting\s+pr",
        r"pending\s+pr",
        r"in\s+(?:code\s+)?review",
        r"pull\s+request",
        r"pr\s+pending"
    ]
    for pattern in review_patterns:
        if re.search(pattern, q):
            intent.filter_awaiting_review = True
            intent.filter_by_states.extend(["Code Review"])
            break
    
    # ═══════════════════════════════════════════════════════════
    # DETECT WORK ITEM TYPE FILTER
    # ═══════════════════════════════════════════════════════════
    if re.search(r"\bbug(?:s)?\b", q):
        intent.filter_by_types.append("Bug")
    if re.search(r"\buser\s*stor(?:y|ies)\b|\bstories\b", q):
        intent.filter_by_types.append("User Story")
    if re.search(r"\btask(?:s)?\b", q):
        intent.filter_by_types.append("Task")
    if re.search(r"\bfeature(?:s)?\b", q):
        intent.filter_by_types.append("Feature")
    
    # ═══════════════════════════════════════════════════════════
    # DETECT STATE FILTER
    # ═══════════════════════════════════════════════════════════
    state_mappings = {
        r"\bnew\b": "New",
        r"\bactive\b": "Active",
        r"\bin\s*progress\b": "In Progress",
        r"\bclosed\b": "Closed",
        r"\bresolved\b": "Resolved",
        r"\bqa\b": "QA",
        r"\buat\b": "UAT",
        r"\bpre[\s-]?prod\b": "PRE-PROD",
        r"\bon\s*hold\b": "On Hold",
        r"\bdesign\b": "Design",
        r"\bcode\s*review\b": "Code Review",
        r"\bcode\s*complete\b": "Code Complete",
        r"\bqa\s*complete\b": "QA Complete",
        r"\breopened?\b": "Reopened",
        r"\breleased\b": "Released",
        r"\binactive\b": "Inactive",
        r"\brequested\b": "Requested",
        r"\bscheduled\b": "Scheduled",
        r"\baccepted\b": "Accepted",
        r"\bin\s*planning\b": "In Planning",
        r"\bawaiting\s*approvals?\b": "Awaiting Approvals",
        r"\bapproved\s*for\s*production\b": "Approved for Production",
        r"\bissues?\s*found\b": "Issues Found",
    }
    for pattern, state in state_mappings.items():
        if re.search(pattern, q):
            intent.filter_by_states.append(state)
    
    # ═══════════════════════════════════════════════════════════
    # DETECT SORT/LIMIT
    # ═══════════════════════════════════════════════════════════
    if re.search(r"slowest|oldest|longest", q):
        intent.sort_by = "age"
    elif re.search(r"highest[\s-]?priority|most\s+critical", q):
        intent.sort_by = "priority"
    elif re.search(r"recently\s+(?:updated|changed)|latest", q):
        intent.sort_by = "changed_date"
    
    # Extract limit (top N, first N)
    limit_match = re.search(r"(?:top|first|show)\s+(\d+)", q)
    if limit_match:
        intent.limit = int(limit_match.group(1))
    
    return intent


def filter_work_items_by_intent(items: List[Dict], intent: QueryIntent) -> List[Dict]:
    """
    Filter work items based on analyzed query intent.
    
    Args:
        items: List of work item dicts (from wit_get_work_items_for_iteration or search)
        intent: Analyzed query intent
        
    Returns:
        Filtered list of work items
    """
    if not items:
        return []
    
    filtered = items.copy()
    
    # ═══════════════════════════════════════════════════════════
    # FILTER BY TAGS (Blocked, etc.)
    # ═══════════════════════════════════════════════════════════
    if intent.filter_by_tags:
        def has_matching_tag(item: Dict) -> bool:
            fields = item.get("fields", {})
            tags_str = fields.get("System.Tags", "") or ""
            item_tags = [t.strip().lower() for t in tags_str.split(";") if t.strip()]
            target_tags = [t.lower() for t in intent.filter_by_tags]
            return any(tag in item_tags or any(tag in t for t in item_tags) for tag in target_tags)
        
        filtered = [item for item in filtered if has_matching_tag(item)]
        logger.info(f"[QUERY_FILTER] After tag filter ({intent.filter_by_tags}): {len(filtered)} items")
    
    # ═══════════════════════════════════════════════════════════
    # FILTER BY UNASSIGNED
    # ═══════════════════════════════════════════════════════════
    if intent.filter_unassigned:
        def is_unassigned(item: Dict) -> bool:
            fields = item.get("fields", {})
            assigned_to = fields.get("System.AssignedTo")
            if not assigned_to:
                return True
            if isinstance(assigned_to, dict):
                return not assigned_to.get("displayName") and not assigned_to.get("uniqueName")
            return not assigned_to
        
        filtered = [item for item in filtered if is_unassigned(item)]
        logger.info(f"[QUERY_FILTER] After unassigned filter: {len(filtered)} items")
    
    # ═══════════════════════════════════════════════════════════
    # FILTER BY ACTIVITY DAYS (stale items)
    # ═══════════════════════════════════════════════════════════
    if intent.filter_by_activity_days:
        cutoff_date = datetime.utcnow() - timedelta(days=intent.filter_by_activity_days)
        
        def is_stale(item: Dict) -> bool:
            fields = item.get("fields", {})
            changed_date_str = fields.get("System.ChangedDate", "")
            if not changed_date_str:
                return True  # No date = assume stale
            try:
                # Parse ISO date format
                if isinstance(changed_date_str, str):
                    changed_date = datetime.fromisoformat(changed_date_str.replace("Z", "+00:00"))
                    changed_date = changed_date.replace(tzinfo=None)
                    return changed_date < cutoff_date
            except Exception:
                return False
            return False
        
        # Also exclude completed items - dynamic from config
        from config import config as _cfg
        completed_states = _cfg.get_states_for_category('completed')
        
        def is_stale_and_not_completed(item: Dict) -> bool:
            fields = item.get("fields", {})
            state = fields.get("System.State", "")
            if state in completed_states:
                return False
            return is_stale(item)
        
        filtered = [item for item in filtered if is_stale_and_not_completed(item)]
        logger.info(f"[QUERY_FILTER] After stale filter ({intent.filter_by_activity_days} days): {len(filtered)} items")
    
    # ═══════════════════════════════════════════════════════════
    # FILTER BY AT-RISK (items likely to spill)
    # ═══════════════════════════════════════════════════════════
    if intent.filter_at_risk:
        # Items at risk are:
        # 1. Not in completed states (Closed, Resolved, Done)
        # 2. Have low progress or are stuck
        # 3. Have blocking issues (tags, impediments)
        from config import config as _cfg
        completed_states = _cfg.get_states_for_category('completed')
        blocked_cfg = _cfg.get_states_for_category('blocked')
        not_started_cfg = _cfg.get_states_for_category('not_started')
        in_progress_cfg = _cfg.get_states_for_category('in_progress')
        risky_states = not_started_cfg + blocked_cfg + in_progress_cfg
        
        def is_at_risk(item: Dict) -> bool:
            fields = item.get("fields", {})
            state = fields.get("System.State", "")
            
            # Completed items are not at risk
            if state in completed_states:
                return False
            
            # Items in risky states are at risk
            if state in risky_states:
                # Check if it has blockers
                tags = fields.get("System.Tags", "") or ""
                if any(blocker in tags.lower() for blocker in ["blocked", "blocker", "impediment"]):
                    return True
                
                # Check remaining capacity (if available)
                remaining = fields.get("Microsoft.VSTS.Scheduling.RemainingWork", 0) or 0
                completed = fields.get("Microsoft.VSTS.Scheduling.CompletedWork", 0) or 0
                
                # If significant work remains, it's at risk
                if remaining > 0 and completed == 0:
                    return True
                
                # Check changed date - if not updated recently, at risk
                changed_date_str = fields.get("System.ChangedDate", "")
                if changed_date_str:
                    try:
                        changed_date = datetime.fromisoformat(changed_date_str.replace("Z", "+00:00"))
                        changed_date = changed_date.replace(tzinfo=None)
                        if changed_date < datetime.utcnow() - timedelta(days=3):
                            return True
                    except Exception:
                        pass
                
                return True  # All non-completed items in risky states are potentially at risk
            
            return False
        
        filtered = [item for item in filtered if is_at_risk(item)]
        logger.info(f"[QUERY_FILTER] After at-risk filter: {len(filtered)} items")
    
    # ═══════════════════════════════════════════════════════════
    # FILTER BY CUSTOMER-IMPACTING / HIGH PRIORITY
    # ═══════════════════════════════════════════════════════════
    if intent.filter_customer_impacting or intent.filter_high_priority:
        def is_high_priority(item: Dict) -> bool:
            fields = item.get("fields", {})
            
            # Check priority (1 = highest)
            priority = fields.get("Microsoft.VSTS.Common.Priority", 99)
            if isinstance(priority, (int, float)) and priority <= 2:
                return True
            
            # Check severity
            severity = fields.get("Microsoft.VSTS.Common.Severity", "")
            if isinstance(severity, str) and any(s in severity.lower() for s in ["1", "critical", "high"]):
                return True
            
            # Check tags for customer-related indicators
            tags = fields.get("System.Tags", "") or ""
            customer_tags = ["customer", "client", "production", "critical", "urgent", "p1", "sev1"]
            if any(tag in tags.lower() for tag in customer_tags):
                return True
            
            # Check area path for customer-related paths
            area_path = fields.get("System.AreaPath", "") or ""
            customer_areas = ["profrac", "client", "customer", "feedback"]
            if any(area in area_path.lower() for area in customer_areas):
                return True
            
            return False
        
        filtered = [item for item in filtered if is_high_priority(item)]
        logger.info(f"[QUERY_FILTER] After high-priority/customer filter: {len(filtered)} items")
    
    # ═══════════════════════════════════════════════════════════
    # FILTER BY WORK ITEM TYPE
    # ═══════════════════════════════════════════════════════════
    if intent.filter_by_types:
        def matches_type(item: Dict) -> bool:
            fields = item.get("fields", {})
            item_type = fields.get("System.WorkItemType", "")
            return item_type in intent.filter_by_types
        
        filtered = [item for item in filtered if matches_type(item)]
        logger.info(f"[QUERY_FILTER] After type filter ({intent.filter_by_types}): {len(filtered)} items")
    
    # ═══════════════════════════════════════════════════════════
    # FILTER BY STATE (with normalization)
    # ═══════════════════════════════════════════════════════════
    if intent.filter_by_states:
        # Normalize all target states
        normalized_targets = [normalize_state(s) for s in intent.filter_by_states]
        
        def matches_state(item: Dict) -> bool:
            fields = item.get("fields", {})
            item_state = fields.get("System.State", "")
            
            # Exact match after normalization
            for target_state in normalized_targets:
                if item_state == target_state:
                    return True
                # Fallback: case-insensitive partial match
                if target_state.lower() in item_state.lower() or item_state.lower() in target_state.lower():
                    return True
            return False
        
        filtered = [item for item in filtered if matches_state(item)]
        logger.info(f"[QUERY_FILTER] After state filter ({normalized_targets}): {len(filtered)} items")
    
    # ═══════════════════════════════════════════════════════════
    # FILTER BY AWAITING REVIEW
    # ═══════════════════════════════════════════════════════════
    if intent.filter_awaiting_review:
        def is_in_review(item: Dict) -> bool:
            fields = item.get("fields", {})
            state = fields.get("System.State", "").lower()
            tags = fields.get("System.Tags", "").lower()
            
            review_keywords = ["review", "pr", "pull request", "code review", "qa"]
            return (any(kw in state for kw in review_keywords) or 
                    any(kw in tags for kw in review_keywords))
        
        filtered = [item for item in filtered if is_in_review(item)]
        logger.info(f"[QUERY_FILTER] After review filter: {len(filtered)} items")
    
    # ═══════════════════════════════════════════════════════════
    # SORT RESULTS
    # ═══════════════════════════════════════════════════════════
    if intent.sort_by:
        if intent.sort_by == "age":
            # Sort by changed date (oldest first)
            def get_changed_date(item: Dict) -> datetime:
                fields = item.get("fields", {})
                changed_date_str = fields.get("System.ChangedDate", "")
                if changed_date_str:
                    try:
                        return datetime.fromisoformat(changed_date_str.replace("Z", "+00:00"))
                    except Exception:
                        pass
                return datetime.max
            
            filtered.sort(key=get_changed_date)
            logger.info(f"[QUERY_FILTER] Sorted by age (oldest first)")
        
        elif intent.sort_by == "priority":
            # Sort by priority (1 = highest = first)
            def get_priority(item: Dict) -> int:
                fields = item.get("fields", {})
                priority = fields.get("Microsoft.VSTS.Common.Priority", 99)
                return priority if isinstance(priority, int) else 99
            
            filtered.sort(key=get_priority)
            logger.info(f"[QUERY_FILTER] Sorted by priority (highest first)")
        
        elif intent.sort_by == "changed_date":
            # Sort by most recently changed (newest first)
            def get_changed_date_desc(item: Dict) -> datetime:
                fields = item.get("fields", {})
                changed_date_str = fields.get("System.ChangedDate", "")
                if changed_date_str:
                    try:
                        return datetime.fromisoformat(changed_date_str.replace("Z", "+00:00"))
                    except Exception:
                        pass
                return datetime.min
            
            filtered.sort(key=get_changed_date_desc, reverse=True)
            logger.info(f"[QUERY_FILTER] Sorted by changed date (newest first)")
    
    # ═══════════════════════════════════════════════════════════
    # APPLY LIMIT
    # ═══════════════════════════════════════════════════════════
    if intent.limit and len(filtered) > intent.limit:
        filtered = filtered[:intent.limit]
        logger.info(f"[QUERY_FILTER] Limited to top {intent.limit} items")
    
    return filtered


def format_filtered_results(
    items: List[Dict], 
    intent: QueryIntent, 
    original_count: int
) -> str:
    """
    Format filtered results into a human-readable summary.
    
    Args:
        items: Filtered work items
        intent: The query intent used for filtering
        original_count: Original count before filtering
        
    Returns:
        Formatted string response
    """
    if not items:
        # Generate helpful message based on intent
        filter_desc = []
        if intent.filter_by_tags:
            filter_desc.append(f"with tags '{', '.join(intent.filter_by_tags)}'")
        if intent.filter_unassigned:
            filter_desc.append("that are unassigned")
        if intent.filter_by_activity_days:
            filter_desc.append(f"with no activity in {intent.filter_by_activity_days} days")
        if intent.filter_at_risk:
            filter_desc.append("at risk of spilling")
        if intent.filter_customer_impacting:
            filter_desc.append("that are customer-impacting")
        if intent.filter_by_types:
            filter_desc.append(f"of type {', '.join(intent.filter_by_types)}")
        if intent.filter_by_states:
            filter_desc.append(f"in state {', '.join(intent.filter_by_states)}")
        
        filter_str = " and ".join(filter_desc) if filter_desc else "matching your criteria"
        return f"✅ No items found {filter_str} in the current sprint.\n\nTotal sprint items scanned: {original_count}"
    
    # Build summary header
    lines = []
    
    # Add context about what was filtered
    filter_desc = []
    if intent.filter_by_tags:
        filter_desc.append(f"tagged '{', '.join(intent.filter_by_tags)}'")
    if intent.filter_unassigned:
        filter_desc.append("unassigned")
    if intent.filter_by_activity_days:
        filter_desc.append(f"inactive for {intent.filter_by_activity_days}+ days")
    if intent.filter_at_risk:
        filter_desc.append("at risk of spilling")
    if intent.filter_customer_impacting:
        filter_desc.append("customer-impacting/high-priority")
    if intent.filter_by_types:
        filter_desc.append(f"{', '.join(intent.filter_by_types)}s")
    if intent.filter_by_states:
        filter_desc.append(f"in {', '.join(intent.filter_by_states)} state")
    
    if filter_desc:
        filter_str = ", ".join(filter_desc)
        lines.append(f"🔍 Found **{len(items)} items** {filter_str}:")
    else:
        lines.append(f"Found {len(items)} items:")
    
    lines.append("")
    
    # Format each item
    display_limit = 50
    for i, item in enumerate(items[:display_limit], 1):
        fields = item.get("fields", {})
        item_id = item.get("id", "?")
        title = fields.get("System.Title", "Untitled")
        state = fields.get("System.State", "")
        assigned_to = fields.get("System.AssignedTo", {})
        if isinstance(assigned_to, dict):
            assigned_to = assigned_to.get("displayName", "Unassigned")
        elif not assigned_to:
            assigned_to = "Unassigned"
        
        tags = fields.get("System.Tags", "")
        priority = fields.get("Microsoft.VSTS.Common.Priority", "")
        work_item_type = fields.get("System.WorkItemType", "")
        
        # Build item line
        type_emoji = {
            "Bug": "🐛",
            "User Story": "📖",
            "Task": "✅",
            "Feature": "⭐",
            "Epic": "🏔️"
        }.get(work_item_type, "📋")
        
        line = f"{i}. {type_emoji} **[{item_id}]** {title}"
        
        # Add state
        if state:
            line += f" ({state})"
        
        lines.append(line)
        
        # Add details on second line
        details = []
        if assigned_to and assigned_to != "Unassigned":
            details.append(f"👤 {assigned_to[:20]}...")
        elif assigned_to == "Unassigned":
            details.append("👤 ⚠️ Unassigned")
        
        if priority and isinstance(priority, (int, str)):
            priority_label = {1: "🔴 P1", 2: "🟠 P2", 3: "🟡 P3", 4: "🟢 P4"}.get(int(priority), f"P{priority}")
            details.append(priority_label)
        
        if tags:
            # Show first 2-3 tags
            tag_list = [t.strip() for t in tags.split(";") if t.strip()][:3]
            if tag_list:
                details.append(f"🏷️ {', '.join(tag_list)}")
        
        if details:
            lines.append(f"   {' | '.join(details)}")
        
        lines.append("")
    
    if len(items) > display_limit:
        lines.append(f"📋 *Showing first {display_limit} of {len(items)} items.*")
    
    # Add summary stats
    lines.append("")
    lines.append(f"---")
    lines.append(f"📊 **Summary:** {len(items)} matching items out of {original_count} total sprint items")
    
    return "\n".join(lines)


def should_apply_query_filtering(query: str, tool: str) -> bool:
    """
    Determine if query-aware filtering should be applied.
    
    Args:
        query: User's query
        tool: The tool that was used
        
    Returns:
        True if filtering should be applied
    """
    # Only apply to sprint/iteration work item queries
    if tool not in ("wit_get_work_items_for_iteration", "work_get_iteration_work_items"):
        return False
    
    # Check if query has specific filter criteria
    q = query.lower()
    
    filter_indicators = [
        "blocked", "blocker", "unassigned", "no owner",
        "no activity", "inactive", "stale", "stuck",
        "at risk", "spill", "carryover", "delayed",
        "customer", "critical", "high priority", "urgent",
        "awaiting", "review", "pr ", "pull request",
        "oldest", "slowest", "top ", "first ",
        "with tag", "have tag", "tagged"
    ]
    
    return any(indicator in q for indicator in filter_indicators)
