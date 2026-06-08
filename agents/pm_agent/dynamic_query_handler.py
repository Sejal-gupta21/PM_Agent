"""
Dynamic Query Handler - Intelligent query analysis and routing for PM Agent.

This module extends query handling capabilities beyond sprint items to support:
- Pull Request queries (status, reviews, approvals)
- Build/Pipeline queries (failures, status, logs)
- Comparative queries ("compare sprint X with Y")
- Time-based queries ("bugs created last week")
- Aggregate/count queries ("how many bugs per area")
- Team workload analysis
- Cross-entity relationships

The handler analyzes query intent and can:
1. Transform queries into appropriate tool calls
2. Combine multiple tool results for complex queries
3. Post-process results for better answers
"""

import re
import logging
from datetime import datetime, timedelta
from typing import Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class QueryDomain(Enum):
    """Primary domain of the query."""
    WORK_ITEMS = "work_items"
    PULL_REQUESTS = "pull_requests"
    BUILDS = "builds"
    PIPELINES = "pipelines"
    REPOSITORIES = "repositories"
    TEAMS = "teams"
    ITERATIONS = "iterations"
    IDENTITIES = "identities"
    WIKI = "wiki"
    TEST_PLANS = "test_plans"
    MIXED = "mixed"  # Cross-domain queries
    UNKNOWN = "unknown"


class QueryComplexity(Enum):
    """Complexity level of the query."""
    SIMPLE = "simple"  # Single tool call
    COMPOUND = "compound"  # Multiple filters on same domain
    MULTI_TOOL = "multi_tool"  # Requires multiple tool calls
    AGGREGATE = "aggregate"  # Requires aggregation/counting
    COMPARATIVE = "comparative"  # Requires comparison


@dataclass
class TimeRange:
    """Represents a time range for filtering."""
    start: Optional[datetime] = None
    end: Optional[datetime] = None
    description: str = ""


@dataclass
class AdvancedQueryIntent:
    """
    Comprehensive query intent analysis result.
    
    Extends QueryIntent from query_aware_filter.py with additional capabilities:
    - Multi-domain detection
    - Time range extraction
    - Aggregation detection
    - Comparison detection
    """
    # Domain classification
    domain: QueryDomain = QueryDomain.UNKNOWN
    complexity: QueryComplexity = QueryComplexity.SIMPLE
    
    # Time-based filters
    time_range: Optional[TimeRange] = None
    relative_time: Optional[str] = None  # "last week", "yesterday", "past 7 days"
    
    # Aggregation intent
    wants_count: bool = False
    wants_group_by: Optional[str] = None  # "area", "assignee", "state", "type"
    wants_summary: bool = False
    
    # Comparison intent
    compare_iterations: List[str] = field(default_factory=list)
    compare_time_periods: List[Tuple[datetime, datetime]] = field(default_factory=list)
    
    # PR-specific filters
    pr_status: Optional[str] = None  # "active", "completed", "abandoned"
    pr_reviewer: Optional[str] = None
    pr_author: Optional[str] = None
    pr_needs_review: bool = False
    pr_has_conflicts: bool = False
    
    # Build/Pipeline filters
    build_status: Optional[str] = None  # "succeeded", "failed", "inProgress"
    build_definition: Optional[str] = None
    pipeline_name: Optional[str] = None
    
    # Team/capacity filters
    team_member: Optional[str] = None
    wants_workload: bool = False
    wants_capacity: bool = False
    
    # Derived tool recommendations
    recommended_tools: List[str] = field(default_factory=list)
    tool_sequence: List[Dict[str, Any]] = field(default_factory=list)
    
    # Original query
    original_query: str = ""


def analyze_advanced_intent(query: str) -> AdvancedQueryIntent:
    """
    Perform comprehensive query analysis to extract advanced intent.
    
    Args:
        query: User's natural language query
        
    Returns:
        AdvancedQueryIntent with full analysis
    """
    q = query.lower()
    intent = AdvancedQueryIntent(original_query=query)
    
    # ═══════════════════════════════════════════════════════════════════════════
    # DOMAIN DETECTION
    # ═══════════════════════════════════════════════════════════════════════════
    
    # Pull Request domain
    pr_keywords = {'pull request', 'pr ', 'prs', 'code review', 'merge request', 
                   'reviewer', 'approval', 'needs review', 'review required'}
    if any(kw in q for kw in pr_keywords):
        intent.domain = QueryDomain.PULL_REQUESTS
        intent.recommended_tools.append('pr_list_pull_requests')
    
    # Build/Pipeline domain
    build_keywords = {'build', 'builds', 'pipeline', 'pipelines', 'ci', 'cd',
                      'deployment', 'release', 'artifact', 'build failed',
                      'build status', 'build log', 'pipeline run'}
    if any(kw in q for kw in build_keywords):
        if intent.domain == QueryDomain.UNKNOWN:
            intent.domain = QueryDomain.BUILDS
        else:
            intent.domain = QueryDomain.MIXED
        intent.recommended_tools.extend(['pipelines_get_builds', 'pipelines_list_pipelines'])
    
    # Repository domain
    repo_keywords = {'repository', 'repositories', 'repo ', 'repos', 'git', 
                     'branch', 'branches', 'commit', 'commits', 'clone'}
    if any(kw in q for kw in repo_keywords) and 'pull request' not in q:
        if intent.domain == QueryDomain.UNKNOWN:
            intent.domain = QueryDomain.REPOSITORIES
        else:
            intent.domain = QueryDomain.MIXED
        intent.recommended_tools.extend(['repo_list_repos_by_project', 'repo_list_branches'])
    
    # Wiki domain
    wiki_keywords = {'wiki', 'documentation', 'docs', 'wiki page'}
    if any(kw in q for kw in wiki_keywords):
        if intent.domain == QueryDomain.UNKNOWN:
            intent.domain = QueryDomain.WIKI
        else:
            intent.domain = QueryDomain.MIXED
        intent.recommended_tools.append('wiki_list_wikis')
    
    # Test domain
    test_keywords = {'test plan', 'test case', 'test suite', 'test result', 
                     'test run', 'testing', 'test coverage'}
    if any(kw in q for kw in test_keywords):
        if intent.domain == QueryDomain.UNKNOWN:
            intent.domain = QueryDomain.TEST_PLANS
        else:
            intent.domain = QueryDomain.MIXED
        intent.recommended_tools.extend(['testplan_list_plans', 'testplan_list_suites'])
    
    # Work Items domain - check FIRST for bug/story/task queries
    # This should take priority over team/identity when work item types are mentioned
    work_item_keywords = {'bug', 'bugs', 'story', 'stories', 'task', 'tasks',
                          'work item', 'work items', 'feature', 'epic', 'issue',
                          'backlog', 'sprint', 'iteration'}
    has_work_item_keyword = any(kw in q for kw in work_item_keywords)
    if intent.domain == QueryDomain.UNKNOWN and has_work_item_keyword:
        intent.domain = QueryDomain.WORK_ITEMS
        intent.recommended_tools.extend(['search_workitem', 'execute_wiql'])
    
    # Team/Identity domain - only if NOT already work items
    team_keywords = {'team member', 'team members', 'who on my team', 'team capacity',
                     'developer', 'developers'}
    # Note: 'assignee' and 'assigned to' are not included as they're usually work item filters
    identity_keywords = {'email', 'user id', 'identity', 'who is'}
    if any(kw in q for kw in team_keywords):
        if intent.domain == QueryDomain.UNKNOWN:
            intent.domain = QueryDomain.TEAMS
        elif not has_work_item_keyword:  # Add team tool only if not a work item query
            intent.recommended_tools.append('core_list_project_teams')
    if any(kw in q for kw in identity_keywords):
        if intent.domain == QueryDomain.UNKNOWN:
            intent.domain = QueryDomain.IDENTITIES
        intent.recommended_tools.append('core_get_identity_ids')
    
    # ═══════════════════════════════════════════════════════════════════════════
    # TIME RANGE EXTRACTION
    # ═══════════════════════════════════════════════════════════════════════════
    
    time_patterns = [
        # Relative days
        (r'(?:in\s+)?(?:the\s+)?(?:last|past)\s+(\d+)\s+days?', lambda m: 
         TimeRange(start=datetime.utcnow() - timedelta(days=int(m.group(1))), 
                   end=datetime.utcnow(), description=f"last {m.group(1)} days")),
        
        # Relative weeks
        (r'(?:in\s+)?(?:the\s+)?(?:last|past)\s+(\d+)\s+weeks?', lambda m: 
         TimeRange(start=datetime.utcnow() - timedelta(weeks=int(m.group(1))), 
                   end=datetime.utcnow(), description=f"last {m.group(1)} weeks")),
        
        # Relative months
        (r'(?:in\s+)?(?:the\s+)?(?:last|past)\s+(\d+)\s+months?', lambda m: 
         TimeRange(start=datetime.utcnow() - timedelta(days=30 * int(m.group(1))), 
                   end=datetime.utcnow(), description=f"last {m.group(1)} months")),
        
        # Named relative times
        (r'\b(?:this|current)\s+week\b', lambda m: 
         TimeRange(start=datetime.utcnow() - timedelta(days=datetime.utcnow().weekday()), 
                   end=datetime.utcnow(), description="this week")),
        
        (r'\blast\s+week\b', lambda m: 
         TimeRange(start=datetime.utcnow() - timedelta(days=datetime.utcnow().weekday() + 7), 
                   end=datetime.utcnow() - timedelta(days=datetime.utcnow().weekday()), 
                   description="last week")),
        
        (r'\byesterday\b', lambda m: 
         TimeRange(start=datetime.utcnow() - timedelta(days=1), 
                   end=datetime.utcnow(), description="yesterday")),
        
        (r'\btoday\b', lambda m: 
         TimeRange(start=datetime.utcnow().replace(hour=0, minute=0, second=0), 
                   end=datetime.utcnow(), description="today")),
        
        # Since date pattern
        (r'since\s+(\d{4}-\d{2}-\d{2})', lambda m: 
         TimeRange(start=datetime.fromisoformat(m.group(1)), 
                   end=datetime.utcnow(), description=f"since {m.group(1)}")),
        
        # Between dates pattern
        (r'between\s+(\d{4}-\d{2}-\d{2})\s+(?:and|to)\s+(\d{4}-\d{2}-\d{2})', lambda m: 
         TimeRange(start=datetime.fromisoformat(m.group(1)), 
                   end=datetime.fromisoformat(m.group(2)), 
                   description=f"between {m.group(1)} and {m.group(2)}")),
    ]
    
    for pattern, handler in time_patterns:
        match = re.search(pattern, q)
        if match:
            try:
                intent.time_range = handler(match)
                intent.relative_time = intent.time_range.description
                logger.debug(f"[QUERY] Detected time range: {intent.time_range.description}")
                break
            except Exception as e:
                logger.warning(f"[QUERY] Time pattern match failed: {e}")
    
    # ═══════════════════════════════════════════════════════════════════════════
    # AGGREGATION DETECTION
    # ═══════════════════════════════════════════════════════════════════════════
    
    # Count patterns
    count_patterns = [
        r'\bhow\s+many\b', r'\bcount\b', r'\bnumber\s+of\b', r'\btotal\b',
        r'\bstatistics\b', r'\bstats\b', r'\bmetrics\b'
    ]
    if any(re.search(p, q) for p in count_patterns):
        intent.wants_count = True
        intent.complexity = QueryComplexity.AGGREGATE
        logger.debug("[QUERY] Detected count/aggregate intent")
    
    # Group by patterns
    group_patterns = [
        (r'\bby\s+area\b|\bper\s+area\b|\beach\s+area\b', 'area'),
        (r'\bby\s+assignee\b|\bper\s+assignee\b|\beach\s+person\b|\bper\s+person\b|\bby\s+developer\b|\bper\s+developer\b|\bby\s+user\b|\bper\s+user\b', 'assignee'),
        (r'\bby\s+state\b|\bper\s+state\b|\bby\s+status\b|\bper\s+status\b', 'state'),
        (r'\bby\s+type\b|\bper\s+type\b|\beach\s+type\b', 'type'),
        (r'\bby\s+sprint\b|\bper\s+sprint\b|\bper\s+iteration\b', 'iteration'),
        (r'\bby\s+priority\b|\bper\s+priority\b', 'priority'),
    ]
    for pattern, group_field in group_patterns:
        if re.search(pattern, q):
            intent.wants_group_by = group_field
            intent.complexity = QueryComplexity.AGGREGATE
            logger.debug(f"[QUERY] Detected group by: {group_field}")
            break
    
    # Summary patterns
    summary_patterns = [r'\bsummary\b', r'\boverview\b', r'\bdashboard\b', r'\breport\b']
    if any(re.search(p, q) for p in summary_patterns):
        intent.wants_summary = True
    
    # ═══════════════════════════════════════════════════════════════════════════
    # COMPARISON DETECTION
    # ═══════════════════════════════════════════════════════════════════════════
    
    compare_patterns = [
        r'\bcompare\b', r'\bversus\b', r'\bvs\.?\b', r'\bdifference\b',
        r'\bchanged?\s+from\b', r'\btrend\b', r'\bprogress\b'
    ]
    if any(re.search(p, q) for p in compare_patterns):
        intent.complexity = QueryComplexity.COMPARATIVE
        logger.debug("[QUERY] Detected comparison intent")
    
    # Extract iteration comparison (e.g., "compare sprint 25.24 with 25.25")
    iteration_compare = re.search(
        r'compare\s+(?:sprint|iteration)\s+(\d+\.\d+)\s+(?:with|to|and|vs\.?)\s+(?:sprint|iteration\s+)?(\d+\.\d+)', q
    )
    if iteration_compare:
        intent.compare_iterations = [iteration_compare.group(1), iteration_compare.group(2)]
        intent.complexity = QueryComplexity.COMPARATIVE
        logger.debug(f"[QUERY] Comparing iterations: {intent.compare_iterations}")
    
    # ═══════════════════════════════════════════════════════════════════════════
    # PULL REQUEST FILTERS
    # ═══════════════════════════════════════════════════════════════════════════
    
    if intent.domain == QueryDomain.PULL_REQUESTS:
        # PR status
        if any(kw in q for kw in ['active pr', 'open pr', 'pending pr']):
            intent.pr_status = 'active'
        elif any(kw in q for kw in ['completed pr', 'merged pr', 'closed pr']):
            intent.pr_status = 'completed'
        elif 'abandoned' in q:
            intent.pr_status = 'abandoned'
        
        # PR review status
        if any(kw in q for kw in ['needs review', 'awaiting review', 'pending review']):
            intent.pr_needs_review = True
        
        # PR conflicts
        if any(kw in q for kw in ['conflict', 'merge conflict', 'has conflict']):
            intent.pr_has_conflicts = True
        
        # Extract reviewer name
        reviewer_match = re.search(r'(?:review|approved)\s+by\s+([a-zA-Z]+(?:\s+[a-zA-Z]+)?)', q)
        if reviewer_match:
            intent.pr_reviewer = reviewer_match.group(1)
        
        # Extract author name
        author_match = re.search(r'(?:created|authored|from|by)\s+([a-zA-Z]+(?:\s+[a-zA-Z]+)?)', q)
        if author_match and 'review' not in q[:author_match.start()]:
            intent.pr_author = author_match.group(1)
    
    # ═══════════════════════════════════════════════════════════════════════════
    # BUILD/PIPELINE FILTERS
    # ═══════════════════════════════════════════════════════════════════════════
    
    if intent.domain == QueryDomain.BUILDS or 'build' in q or 'pipeline' in q:
        # Build status
        if any(kw in q for kw in ['failed', 'failure', 'broken', 'red']):
            intent.build_status = 'failed'
        elif any(kw in q for kw in ['succeeded', 'success', 'passed', 'green']):
            intent.build_status = 'succeeded'
        elif any(kw in q for kw in ['running', 'in progress', 'building', 'pending']):
            intent.build_status = 'inProgress'
        elif any(kw in q for kw in ['cancelled', 'canceled', 'stopped']):
            intent.build_status = 'cancelled'
        
        # Extract pipeline/definition name
        pipeline_match = re.search(r'(?:pipeline|definition|build)\s+(?:named?\s+)?["\']?([a-zA-Z0-9_-]+)["\']?', q)
        if pipeline_match:
            intent.pipeline_name = pipeline_match.group(1)
    
    # ═══════════════════════════════════════════════════════════════════════════
    # TEAM/WORKLOAD FILTERS
    # ═══════════════════════════════════════════════════════════════════════════
    
    if any(kw in q for kw in ['workload', 'how much work', 'work assigned', 'load']):
        intent.wants_workload = True
        intent.recommended_tools.append('get_capacity_forecast')
    
    if any(kw in q for kw in ['capacity', 'bandwidth', 'available', 'free time']):
        intent.wants_capacity = True
        intent.recommended_tools.append('get_capacity_forecast')
    
    # Extract team member name
    member_patterns = [
        r'(?:work|assigned|capacity|workload)\s+(?:for|of)\s+([a-zA-Z]+(?:\s+[a-zA-Z]+)?)',
        r"([a-zA-Z]+(?:\s+[a-zA-Z]+)?)'s\s+(?:work|assigned|capacity|workload)"
    ]
    for pattern in member_patterns:
        match = re.search(pattern, q)
        if match:
            intent.team_member = match.group(1)
            break
    
    # ═══════════════════════════════════════════════════════════════════════════
    # COMPLEXITY ANALYSIS
    # ═══════════════════════════════════════════════════════════════════════════
    
    # Determine if multi-tool approach is needed
    if intent.domain == QueryDomain.MIXED:
        intent.complexity = QueryComplexity.MULTI_TOOL
    
    # If we need counts with groups, it's aggregate
    if intent.wants_count and intent.wants_group_by:
        intent.complexity = QueryComplexity.AGGREGATE
    
    # Build recommended tool sequence for complex queries
    intent.tool_sequence = _build_tool_sequence(intent)
    
    # Remove duplicates from recommended tools while preserving order
    seen = set()
    intent.recommended_tools = [t for t in intent.recommended_tools 
                                 if not (t in seen or seen.add(t))]
    
    return intent


def _build_tool_sequence(intent: AdvancedQueryIntent) -> List[Dict[str, Any]]:
    """
    Build a sequence of tool calls for complex queries.
    
    Args:
        intent: Analyzed query intent
        
    Returns:
        List of tool call specifications
    """
    sequence = []
    
    if intent.complexity == QueryComplexity.COMPARATIVE and intent.compare_iterations:
        # For iteration comparison, fetch both iterations
        for iteration in intent.compare_iterations:
            sequence.append({
                "tool": "wit_get_work_items_for_iteration",
                "args": {"iterationId": iteration},
                "purpose": f"Fetch items for iteration {iteration}"
            })
    
    elif intent.domain == QueryDomain.PULL_REQUESTS:
        # PR query
        pr_args = {}
        if intent.pr_status:
            pr_args["status"] = intent.pr_status
        if intent.pr_reviewer:
            pr_args["reviewerId"] = intent.pr_reviewer
        if intent.pr_author:
            pr_args["creatorId"] = intent.pr_author
        
        sequence.append({
            "tool": "pr_list_pull_requests",
            "args": pr_args,
            "purpose": "Fetch pull requests"
        })
    
    elif intent.domain == QueryDomain.BUILDS:
        # Build/Pipeline query
        build_args = {}
        if intent.build_status:
            build_args["statusFilter"] = intent.build_status
        if intent.pipeline_name:
            build_args["definitions"] = intent.pipeline_name
        
        sequence.append({
            "tool": "pipelines_get_builds",
            "args": build_args,
            "purpose": "Fetch builds"
        })
    
    return sequence


def generate_wiql_for_time_filter(
    intent: AdvancedQueryIntent,
    work_item_types: List[str] = None,
    states: List[str] = None,
    assigned_to: str = None
) -> Optional[str]:
    """
    Generate a WIQL query for time-based filtering.
    
    Args:
        intent: Query intent with time range
        work_item_types: Optional list of work item types to filter
        states: Optional list of states to filter
        assigned_to: Optional assignee filter
        
    Returns:
        WIQL query string or None if no time filter
    """
    if not intent.time_range:
        return None
    
    # Build SELECT clause
    wiql = """SELECT [System.Id], [System.Title], [System.State], [System.AssignedTo], 
               [System.CreatedDate], [System.ChangedDate], [System.WorkItemType]
       FROM WorkItems WHERE [System.TeamProject] = @project"""
    
    # Add time filter
    if intent.time_range.start:
        start_str = intent.time_range.start.strftime("%Y-%m-%d")
        wiql += f" AND [System.CreatedDate] >= '{start_str}'"
    
    if intent.time_range.end:
        end_str = intent.time_range.end.strftime("%Y-%m-%d")
        wiql += f" AND [System.CreatedDate] <= '{end_str}'"
    
    # Add type filter
    if work_item_types:
        types_str = "', '".join(work_item_types)
        wiql += f" AND [System.WorkItemType] IN ('{types_str}')"
    
    # Add state filter
    if states:
        states_str = "', '".join(states)
        wiql += f" AND [System.State] IN ('{states_str}')"
    
    # Add assignee filter
    if assigned_to:
        wiql += f" AND [System.AssignedTo] CONTAINS '{assigned_to}'"
    
    # Add ordering
    wiql += " ORDER BY [System.CreatedDate] DESC"
    
    return wiql


def aggregate_work_items(
    items: List[Dict],
    group_by: str,
    count_only: bool = False
) -> Dict[str, Any]:
    """
    Aggregate work items by a specified field.
    
    Args:
        items: List of work item dictionaries
        group_by: Field to group by ('area', 'assignee', 'state', 'type', 'priority')
        count_only: If True, return only counts
        
    Returns:
        Dictionary with aggregated results
    """
    if not items:
        return {"groups": {}, "total": 0, "group_by": group_by}
    
    # Field mapping
    field_map = {
        'area': 'System.AreaPath',
        'assignee': 'System.AssignedTo',
        'state': 'System.State',
        'type': 'System.WorkItemType',
        'priority': 'Microsoft.VSTS.Common.Priority',
        'iteration': 'System.IterationPath',
    }
    
    field_name = field_map.get(group_by, group_by)
    
    groups: Dict[str, List[Dict]] = {}
    
    for item in items:
        fields = item.get('fields', {})
        value = fields.get(field_name, 'Unknown')
        
        # Handle complex field types (like AssignedTo)
        if isinstance(value, dict):
            value = value.get('displayName', value.get('uniqueName', 'Unknown'))
        elif not value:
            value = 'Unassigned' if 'Assigned' in field_name else 'Unknown'
        
        # Truncate long area paths to last 2 segments
        if group_by == 'area' and '\\' in str(value):
            parts = str(value).split('\\')
            value = '\\'.join(parts[-2:]) if len(parts) > 2 else value
        
        if value not in groups:
            groups[value] = []
        
        if not count_only:
            groups[value].append(item)
        else:
            groups[value].append(item.get('id', None))
    
    # Build result
    result = {
        "group_by": group_by,
        "total": len(items),
        "group_count": len(groups),
        "groups": {}
    }
    
    # Sort groups by count (descending)
    sorted_groups = sorted(groups.items(), key=lambda x: len(x[1]), reverse=True)
    
    for key, group_items in sorted_groups:
        if count_only:
            result["groups"][key] = len(group_items)
        else:
            result["groups"][key] = {
                "count": len(group_items),
                "items": group_items[:10]  # Limit items per group
            }
    
    return result


def format_aggregate_results(aggregation: Dict[str, Any], query: str) -> str:
    """
    Format aggregated results into a human-readable summary.
    
    Args:
        aggregation: Aggregation result from aggregate_work_items
        query: Original query for context
        
    Returns:
        Formatted markdown string
    """
    group_by = aggregation.get("group_by", "category")
    total = aggregation.get("total", 0)
    group_count = aggregation.get("group_count", 0)
    groups = aggregation.get("groups", {})
    
    lines = [
        f"📊 **Work Items by {group_by.title()}**",
        "",
        f"**Total Items:** {total}",
        f"**Categories:** {group_count}",
        "",
        "---",
        ""
    ]
    
    # Format each group
    for i, (key, data) in enumerate(groups.items(), 1):
        if isinstance(data, int):
            count = data
            lines.append(f"{i}. **{key}**: {count} items")
        else:
            count = data.get("count", 0)
            percentage = (count / total * 100) if total > 0 else 0
            bar = "█" * int(percentage / 5) + "░" * (20 - int(percentage / 5))
            lines.append(f"{i}. **{key}**")
            lines.append(f"   {bar} {count} ({percentage:.1f}%)")
            
            # Show sample items
            items = data.get("items", [])[:3]
            for item in items:
                fields = item.get("fields", {})
                item_id = item.get("id", "?")
                title = fields.get("System.Title", "Untitled")[:50]
                lines.append(f"   - [{item_id}] {title}...")
            
            if count > 3:
                lines.append(f"   *...and {count - 3} more*")
        
        lines.append("")
    
    return "\n".join(lines)


def compare_iterations(
    iteration1_items: List[Dict],
    iteration2_items: List[Dict],
    iteration1_name: str,
    iteration2_name: str
) -> str:
    """
    Compare two iterations and generate a comparison report.
    
    Args:
        iteration1_items: Work items from first iteration
        iteration2_items: Work items from second iteration
        iteration1_name: Name of first iteration
        iteration2_name: Name of second iteration
        
    Returns:
        Formatted comparison report
    """
    def analyze_iteration(items: List[Dict]) -> Dict[str, Any]:
        """Analyze an iteration's items."""
        from config import config
        stats = {
            "total": len(items),
            "by_type": {},
            "by_state": {},
            "completed": 0,
            "in_progress": 0,
            "not_started": 0,
            "blocked": 0,
        }
        
        # Dynamic state lists from centralized config
        completed_states = set(config.get_states_for_category('completed'))
        in_progress_states = set(config.get_states_for_category('in_progress'))
        blocked_states = set(config.get_states_for_category('blocked'))
        not_started_states = set(config.get_states_for_category('not_started'))
        
        for item in items:
            fields = item.get("fields", {})
            
            # By type
            wi_type = fields.get("System.WorkItemType", "Unknown")
            stats["by_type"][wi_type] = stats["by_type"].get(wi_type, 0) + 1
            
            # By state
            state = fields.get("System.State", "Unknown")
            stats["by_state"][state] = stats["by_state"].get(state, 0) + 1
            
            # Completion tracking using centralized config
            if state in completed_states:
                stats["completed"] += 1
            elif state in blocked_states:
                stats["blocked"] += 1
            elif state in in_progress_states:
                stats["in_progress"] += 1
            elif state in not_started_states:
                stats["not_started"] += 1
            
            # Also check blocked by tags
            tags = fields.get("System.Tags", "") or ""
            if "blocked" in tags.lower() or "blocker" in tags.lower():
                if state not in blocked_states:  # Avoid double counting
                    stats["blocked"] += 1
        
        stats["completion_rate"] = (stats["completed"] / stats["total"] * 100) if stats["total"] > 0 else 0
        
        return stats
    
    stats1 = analyze_iteration(iteration1_items)
    stats2 = analyze_iteration(iteration2_items)
    
    lines = [
        f"📊 **Sprint Comparison: {iteration1_name} vs {iteration2_name}**",
        "",
        "| Metric | " + iteration1_name + " | " + iteration2_name + " | Change |",
        "|--------|---------|---------|--------|",
    ]
    
    # Total items
    total_diff = stats2["total"] - stats1["total"]
    diff_sign = "+" if total_diff >= 0 else ""
    lines.append(f"| Total Items | {stats1['total']} | {stats2['total']} | {diff_sign}{total_diff} |")
    
    # Completed
    completed_diff = stats2["completed"] - stats1["completed"]
    diff_sign = "+" if completed_diff >= 0 else ""
    lines.append(f"| Completed | {stats1['completed']} | {stats2['completed']} | {diff_sign}{completed_diff} |")
    
    # Completion rate
    rate_diff = stats2["completion_rate"] - stats1["completion_rate"]
    diff_sign = "+" if rate_diff >= 0 else ""
    lines.append(f"| Completion Rate | {stats1['completion_rate']:.1f}% | {stats2['completion_rate']:.1f}% | {diff_sign}{rate_diff:.1f}% |")
    
    # Blocked
    blocked_diff = stats2["blocked"] - stats1["blocked"]
    diff_sign = "+" if blocked_diff >= 0 else ""
    lines.append(f"| Blocked | {stats1['blocked']} | {stats2['blocked']} | {diff_sign}{blocked_diff} |")
    
    lines.append("")
    lines.append("### By Work Item Type")
    lines.append("")
    
    # Combine types from both iterations
    all_types = set(stats1["by_type"].keys()) | set(stats2["by_type"].keys())
    for wi_type in sorted(all_types):
        count1 = stats1["by_type"].get(wi_type, 0)
        count2 = stats2["by_type"].get(wi_type, 0)
        diff = count2 - count1
        diff_sign = "+" if diff >= 0 else ""
        lines.append(f"- **{wi_type}**: {count1} → {count2} ({diff_sign}{diff})")
    
    lines.append("")
    lines.append("### Analysis")
    lines.append("")
    
    # Generate insights
    if stats2["completion_rate"] > stats1["completion_rate"]:
        lines.append(f"✅ Completion rate improved by {rate_diff:.1f} percentage points")
    elif stats2["completion_rate"] < stats1["completion_rate"]:
        lines.append(f"⚠️ Completion rate decreased by {abs(rate_diff):.1f} percentage points")
    
    if stats2["blocked"] > stats1["blocked"]:
        lines.append(f"🔴 Blocked items increased from {stats1['blocked']} to {stats2['blocked']}")
    elif stats2["blocked"] < stats1["blocked"]:
        lines.append(f"✅ Blocked items decreased from {stats1['blocked']} to {stats2['blocked']}")
    
    return "\n".join(lines)


def filter_prs_by_intent(prs: List[Dict], intent: AdvancedQueryIntent) -> List[Dict]:
    """
    Filter pull requests based on query intent.
    
    Args:
        prs: List of PR dictionaries from pr_list_pull_requests
        intent: Query intent with PR-specific filters
        
    Returns:
        Filtered list of PRs
    """
    filtered = prs.copy()
    
    if intent.pr_status:
        status_map = {
            'active': 'active',
            'completed': 'completed',
            'abandoned': 'abandoned'
        }
        target_status = status_map.get(intent.pr_status, intent.pr_status)
        filtered = [pr for pr in filtered if pr.get('status', '').lower() == target_status]
    
    if intent.pr_needs_review:
        # Filter for PRs that need review (no approvals yet or waiting)
        def needs_review(pr: Dict) -> bool:
            reviewers = pr.get('reviewers', [])
            if not reviewers:
                return True
            # Check if any reviewer has not voted or voted 0 (no response)
            return any(r.get('vote', 0) == 0 for r in reviewers)
        
        filtered = [pr for pr in filtered if needs_review(pr)]
    
    if intent.pr_has_conflicts:
        filtered = [pr for pr in filtered if pr.get('mergeStatus', '') == 'conflicts']
    
    if intent.pr_reviewer:
        reviewer_name = intent.pr_reviewer.lower()
        def has_reviewer(pr: Dict) -> bool:
            reviewers = pr.get('reviewers', [])
            return any(reviewer_name in r.get('displayName', '').lower() 
                      for r in reviewers)
        filtered = [pr for pr in filtered if has_reviewer(pr)]
    
    if intent.pr_author:
        author_name = intent.pr_author.lower()
        def matches_author(pr: Dict) -> bool:
            creator = pr.get('createdBy', {})
            display_name = creator.get('displayName', '').lower()
            unique_name = creator.get('uniqueName', '').lower()
            return author_name in display_name or author_name in unique_name
        filtered = [pr for pr in filtered if matches_author(pr)]
    
    # Time filter
    if intent.time_range and intent.time_range.start:
        def in_time_range(pr: Dict) -> bool:
            created_str = pr.get('creationDate', '')
            if not created_str:
                return True
            try:
                created = datetime.fromisoformat(created_str.replace('Z', '+00:00'))
                created = created.replace(tzinfo=None)
                return created >= intent.time_range.start
            except Exception:
                return True
        filtered = [pr for pr in filtered if in_time_range(pr)]
    
    return filtered


def filter_builds_by_intent(builds: List[Dict], intent: AdvancedQueryIntent) -> List[Dict]:
    """
    Filter builds based on query intent.
    
    Args:
        builds: List of build dictionaries from pipelines_get_builds
        intent: Query intent with build-specific filters
        
    Returns:
        Filtered list of builds
    """
    filtered = builds.copy()
    
    if intent.build_status:
        status_map = {
            'failed': ['failed'],
            'succeeded': ['succeeded'],
            'inProgress': ['inProgress', 'notStarted'],
            'cancelled': ['cancelled', 'canceled']
        }
        target_statuses = status_map.get(intent.build_status, [intent.build_status])
        filtered = [b for b in filtered 
                   if b.get('result', b.get('status', '')).lower() in [s.lower() for s in target_statuses]]
    
    if intent.pipeline_name:
        name_lower = intent.pipeline_name.lower()
        def matches_pipeline(build: Dict) -> bool:
            definition = build.get('definition', {})
            def_name = definition.get('name', '').lower()
            return name_lower in def_name
        filtered = [b for b in filtered if matches_pipeline(b)]
    
    # Time filter
    if intent.time_range and intent.time_range.start:
        def in_time_range(build: Dict) -> bool:
            start_str = build.get('startTime', build.get('queueTime', ''))
            if not start_str:
                return True
            try:
                start = datetime.fromisoformat(start_str.replace('Z', '+00:00'))
                start = start.replace(tzinfo=None)
                return start >= intent.time_range.start
            except Exception:
                return True
        filtered = [b for b in filtered if in_time_range(b)]
    
    return filtered


def format_pr_results(prs: List[Dict], intent: AdvancedQueryIntent) -> str:
    """
    Format PR results into a human-readable summary.
    
    Args:
        prs: List of PR dictionaries
        intent: Query intent for context
        
    Returns:
        Formatted markdown string
    """
    if not prs:
        status_desc = f" with status '{intent.pr_status}'" if intent.pr_status else ""
        return f"✅ No pull requests found{status_desc}."
    
    lines = [
        f"🔀 **Found {len(prs)} Pull Request(s)**",
        ""
    ]
    
    for i, pr in enumerate(prs[:20], 1):
        pr_id = pr.get('pullRequestId', '?')
        title = pr.get('title', 'Untitled')[:60]
        status = pr.get('status', 'Unknown')
        
        creator = pr.get('createdBy', {})
        author = creator.get('displayName', 'Unknown')
        
        source = pr.get('sourceRefName', '').replace('refs/heads/', '')
        target = pr.get('targetRefName', '').replace('refs/heads/', '')
        
        # Status emoji
        status_emoji = {
            'active': '🟡',
            'completed': '✅',
            'abandoned': '❌'
        }.get(status.lower(), '❓')
        
        lines.append(f"{i}. {status_emoji} **PR #{pr_id}**: {title}")
        lines.append(f"   📌 {source} → {target}")
        lines.append(f"   👤 {author} | {status.title()}")
        
        # Show reviewers
        reviewers = pr.get('reviewers', [])
        if reviewers:
            reviewer_str = ", ".join([r.get('displayName', '?')[:15] for r in reviewers[:3]])
            lines.append(f"   👀 Reviewers: {reviewer_str}")
        
        # Show merge status if conflicts
        merge_status = pr.get('mergeStatus', '')
        if merge_status == 'conflicts':
            lines.append("   ⚠️ **Has merge conflicts**")
        
        lines.append("")
    
    if len(prs) > 20:
        lines.append(f"*...and {len(prs) - 20} more PRs*")
    
    return "\n".join(lines)


def format_build_results(builds: List[Dict], intent: AdvancedQueryIntent) -> str:
    """
    Format build results into a human-readable summary.
    
    Args:
        builds: List of build dictionaries
        intent: Query intent for context
        
    Returns:
        Formatted markdown string
    """
    if not builds:
        status_desc = f" with status '{intent.build_status}'" if intent.build_status else ""
        return f"✅ No builds found{status_desc}."
    
    lines = [
        f"🔧 **Found {len(builds)} Build(s)**",
        ""
    ]
    
    for i, build in enumerate(builds[:15], 1):
        build_id = build.get('id', '?')
        build_number = build.get('buildNumber', 'Unknown')
        
        definition = build.get('definition', {})
        def_name = definition.get('name', 'Unknown Pipeline')
        
        result = build.get('result', build.get('status', 'Unknown'))
        
        # Result emoji
        result_emoji = {
            'succeeded': '✅',
            'failed': '❌',
            'canceled': '⏹️',
            'cancelled': '⏹️',
            'partiallySucceeded': '⚠️',
            'inProgress': '🔄',
            'notStarted': '⏳'
        }.get(result.lower(), '❓')
        
        lines.append(f"{i}. {result_emoji} **Build #{build_number}**")
        lines.append(f"   📦 {def_name}")
        lines.append(f"   Status: {result.title()}")
        
        # Show timing
        start_time = build.get('startTime', '')
        if start_time:
            try:
                start_dt = datetime.fromisoformat(start_time.replace('Z', '+00:00'))
                lines.append(f"   🕐 Started: {start_dt.strftime('%Y-%m-%d %H:%M')}")
            except Exception:
                pass
        
        # Show reason
        reason = build.get('reason', '')
        if reason:
            lines.append(f"   📋 Reason: {reason.title()}")
        
        lines.append("")
    
    if len(builds) > 15:
        lines.append(f"*...and {len(builds) - 15} more builds*")
    
    return "\n".join(lines)
