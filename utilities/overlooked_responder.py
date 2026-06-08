"""Overlooked User Stories Responder for chatbot.

Handles queries classified as "overlooked_stories" skill:
- Show overlooked/stale user stories
- Find forgotten work items
- Preview/generate overlooked stories reports
- Dynamic LLM-based query understanding

This module follows the same pattern as sprint_responder.py for
consistent dynamic behavior.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone, timedelta
from glob import glob
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUTS_DIR = REPO_ROOT / "outputs"
DATA_DIR = REPO_ROOT / "data"


# =============================================================================
# LLM-BASED QUERY UNDERSTANDING
# =============================================================================

def parse_overlooked_query_with_llm(query: str) -> Dict[str, Any]:
    """Use LLM to understand the user's overlooked stories query intent.
    
    Returns a structured dict with:
    - intent: str (e.g., "find_overlooked", "count_overlooked", "report", "by_area")
    - filters: dict (area_path, assignee, age_days, state)
    - limit: int (how many items to show)
    - wants_details: bool
    - confidence: float
    """
    try:
        import google.generativeai as genai
        
        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            return _fallback_parse_overlooked_query(query)
        
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-1.5-flash")
        
        prompt = f"""Analyze this query about overlooked/stale user stories and extract the user's intent.

Query: "{query}"

Respond with a JSON object containing:
{{
  "intent": "<one of: find_overlooked, count_overlooked, report, by_area, by_assignee, summary, oldest_items, recent_activity, general>",
  "filters": {{
    "area_path": "<area path or null>",
    "assignee": "<name or null>",
    "age_days": <minimum days of inactivity to consider stale, default 14>,
    "state": "<state filter or null>",
    "filter_current_month": <true if query asks for current month / this month>,
    "time_range": "<'current_month', 'current_sprint', or null>"
  }},
  "limit": <number of items to return, default 10>,
  "wants_details": <true if user wants detailed info, false for summary>,
  "wants_email": <true if user wants to send email report, false otherwise>
}}

Intent definitions:
- find_overlooked: Find stale/overlooked user stories with no recent activity
- count_overlooked: Just count how many overlooked stories exist
- report: Generate a detailed report of overlooked stories
- by_area: Show overlooked stories grouped by area path
- by_assignee: Show overlooked stories grouped by assignee
- summary: Quick summary of overlooked stories status
- oldest_items: Find the oldest/most neglected items
- recent_activity: Find items with recent activity (inverse)
- general: General query about overlooked stories

Respond ONLY with valid JSON, no markdown or extra text."""

        response = model.generate_content(prompt)
        text = response.text.strip()
        
        # Clean up response
        if text.startswith("```"):
            text = re.sub(r"```json?\n?", "", text)
            text = re.sub(r"```\n?$", "", text)
        
        parsed = json.loads(text)
        parsed["confidence"] = 0.85
        return parsed
        
    except Exception as e:
        logger.warning("LLM query parsing failed for overlooked stories: %s", e)
        return _fallback_parse_overlooked_query(query)


def _fallback_parse_overlooked_query(query: str) -> Dict[str, Any]:
    """Fallback keyword-based query parsing for overlooked stories."""
    query_lower = query.lower()
    
    result = {
        "intent": "general",
        "filters": {
            "area_path": None,
            "assignee": None,
            "age_days": 14,  # Default 14 days of inactivity
            "state": None,
            "filter_current_month": False,
            "time_range": None,
        },
        "limit": 10,
        "wants_details": False,
        "wants_email": False,
        "confidence": 0.6,
    }
    
    # Detect intent from keywords
    if any(term in query_lower for term in ["count", "how many", "number of"]):
        result["intent"] = "count_overlooked"
    elif any(term in query_lower for term in ["report", "generate", "create", "send"]):
        result["intent"] = "report"
        if any(term in query_lower for term in ["send", "email", "mail"]):
            result["wants_email"] = True
    elif any(term in query_lower for term in ["by area", "area path", "grouped by area"]):
        result["intent"] = "by_area"
    elif any(term in query_lower for term in ["by assignee", "by person", "who has"]):
        result["intent"] = "by_assignee"
    elif any(term in query_lower for term in ["oldest", "most stale", "longest", "neglected"]):
        result["intent"] = "oldest_items"
    elif any(term in query_lower for term in ["summary", "overview", "status"]):
        result["intent"] = "summary"
    else:
        result["intent"] = "find_overlooked"
    
    # Parse limit
    limit_match = re.search(r"(top|first|show me|show)\s+(\d+)", query_lower)
    if limit_match:
        result["limit"] = int(limit_match.group(2))
    
    # Parse age threshold
    age_match = re.search(r"(\d+)\s*(days?|weeks?|months?)", query_lower)
    if age_match:
        num = int(age_match.group(1))
        unit = age_match.group(2).lower()
        if "week" in unit:
            num *= 7
        elif "month" in unit:
            num *= 30
        result["filters"]["age_days"] = num
    
    # Check for month-based filtering
    if any(term in query_lower for term in ["this month", "current month", "in the month"]):
        result["filters"]["filter_current_month"] = True
        result["filters"]["time_range"] = "current_month"
    elif any(term in query_lower for term in ["this sprint", "current sprint", "in the sprint"]):
        result["filters"]["time_range"] = "current_sprint"
    
    # Extract area path - look for common patterns (most specific first)
    # CRITICAL: Order matters! More specific patterns must come first.
    area_patterns = [
        (r"under\s+([A-Z][A-Za-z0-9-]+)", 1),  # "under XOPS"
        (r"for\s+area\s+(?:path\s+)?([A-Z][A-Za-z0-9-]+)", 1),  # "for area XOPS" or "for area path XOPS"
        (r"in\s+(?:the\s+)?(FracPro[A-Za-z0-9-]*)\s+(?:area|module|project)", 1),  # "in FracPro module" or "in the FracPro module"
        (r"in\s+(?:the\s+)?(XOPS|X-OPS)\s+(?:area|module|project)", 1),  # "in XOPS area" or "in the XOPS module"
        (r"area\s+path[:\s]+([A-Z][A-Za-z0-9-]+)", 1),  # "area path XOPS" or "area path: XOPS"
        (r"(FracPro-[A-Z]+)", 1),  # Match FracPro-DEV, FracPro-OPS, etc.
        (r"\b(XOPS|X-OPS)\b", 1),  # Match XOPS standalone (word boundary)
        (r"\b(FracPro)\b", 1),  # Match FracPro standalone (word boundary)
    ]
    
    for pattern, group_idx in area_patterns:
        match = re.search(pattern, query)
        if match:
            area_value = match.group(group_idx).strip()
            # Validate it's not a common word like "the", "current", "sprint"
            if area_value.lower() not in ["the", "current", "sprint", "this", "a", "an"]:
                result["filters"]["area_path"] = area_value
                break
    
    # Check for detail requests
    if any(term in query_lower for term in ["detail", "all info", "full", "complete"]):
        result["wants_details"] = True
    
    return result


# =============================================================================
# LIVE ADO DATA FETCHING
# =============================================================================

def fetch_overlooked_stories_from_ado(
    age_days: int = 14,
    area_path: Optional[str] = None,
    state: Optional[str] = None,
    filter_current_month: bool = False
) -> Tuple[Optional[List[Dict[str, Any]]], Optional[str]]:
    """Fetch overlooked user stories from ADO.
    
    Args:
        age_days: Minimum days since last update to consider "overlooked"
        area_path: Filter by area path (optional)
        state: Filter by state (optional)
        filter_current_month: If True, only return items that became stale in current month
        
    Returns:
        Tuple of (list of work items, error message or None)
    """
    try:
        from config import config
        import base64
        import requests
        
        org_url = config.ado_org_url
        project = config.ado_project
        pat = config.ado_pat
        
        if not org_url or not project or not pat:
            logger.warning("ADO credentials not configured")
            return None, "ADO credentials not configured"
        
        # Build auth header
        encoded = base64.b64encode(f":{pat}".encode()).decode()
        headers = {"Authorization": f"Basic {encoded}"}
        
        # Calculate date threshold
        now = datetime.now(timezone.utc)
        threshold_date = now - timedelta(days=age_days)
        threshold_str = threshold_date.strftime("%Y-%m-%d")
        
        # If filtering by current month, calculate month boundaries
        month_start_str = None
        month_end_str = None
        if filter_current_month:
            # Current month: first day to last day
            from calendar import monthrange
            year = now.year
            month = now.month
            month_start = datetime(year, month, 1, tzinfo=timezone.utc)
            last_day = monthrange(year, month)[1]
            month_end = datetime(year, month, last_day, 23, 59, 59, tzinfo=timezone.utc)
            month_start_str = month_start.strftime("%Y-%m-%d")
            month_end_str = month_end.strftime("%Y-%m-%d")
        
        # Build WIQL query - use centralized config for completed states
        from config import config as _cfg
        _completed = _cfg.get_states_for_category('completed')
        _completed_sql = ", ".join(f"'{s}'" for s in _completed)
        
        wiql_parts = [
            "SELECT [System.Id], [System.Title], [System.State], [System.AssignedTo], ",
            "[System.AreaPath], [System.ChangedDate], [System.CreatedDate] ",
            f"FROM WorkItems WHERE [System.TeamProject] = '{project}' ",
            "AND [System.WorkItemType] = 'User Story' ",
            f"AND [System.ChangedDate] < '{threshold_str}' ",
            f"AND [System.State] NOT IN ({_completed_sql}) "
        ]
        
        # Add month filter if specified
        if filter_current_month and month_start_str and month_end_str:
            # Items that became stale THIS month (last update was in current month but is now old)
            wiql_parts.append(f"AND [System.ChangedDate] >= '{month_start_str}' ")
            wiql_parts.append(f"AND [System.ChangedDate] <= '{month_end_str}' ")
        
        if area_path:
            wiql_parts.append(f"AND [System.AreaPath] UNDER '{area_path}' ")
        
        if state:
            wiql_parts.append(f"AND [System.State] = '{state}' ")
        
        wiql_parts.append("ORDER BY [System.ChangedDate] ASC")
        
        wiql = "".join(wiql_parts)
        
        # Execute WIQL query
        wiql_url = f"{org_url}/{project}/_apis/wit/wiql?api-version=7.0"
        resp = requests.post(
            wiql_url,
            headers=headers,
            json={"query": wiql},
            timeout=30
        )
        
        if resp.status_code != 200:
            logger.warning("WIQL query failed: %s", resp.text[:200])
            return None, f"WIQL query failed: {resp.status_code}"
        
        data = resp.json()
        work_item_ids = [item["id"] for item in data.get("workItems", [])]
        
        if not work_item_ids:
            return [], None
        
        # Fetch work item details in batches
        items = []
        batch_size = 200
        
        for i in range(0, len(work_item_ids), batch_size):
            batch_ids = work_item_ids[i:i + batch_size]
            ids_param = ",".join(str(id) for id in batch_ids)
            
            detail_url = f"{org_url}/_apis/wit/workitems?ids={ids_param}&$expand=all&api-version=7.0"
            detail_resp = requests.get(detail_url, headers=headers, timeout=30)
            
            if detail_resp.status_code == 200:
                batch_items = detail_resp.json().get("value", [])
                items.extend(batch_items)
        
        # Transform to simpler format
        result = []
        for item in items:
            fields = item.get("fields", {})
            result.append({
                "id": item.get("id"),
                "title": fields.get("System.Title", ""),
                "state": fields.get("System.State", ""),
                "assigned_to": _extract_display_name(fields.get("System.AssignedTo")),
                "area_path": fields.get("System.AreaPath", ""),
                "changed_date": fields.get("System.ChangedDate", ""),
                "created_date": fields.get("System.CreatedDate", ""),
                "days_stale": _calculate_days_stale(fields.get("System.ChangedDate")),
            })
        
        return result, None
        
    except Exception as e:
        logger.exception("Failed to fetch overlooked stories from ADO")
        return None, str(e)


def _extract_display_name(assignee_field: Any) -> str:
    """Extract display name from assignee field."""
    if not assignee_field:
        return "Unassigned"
    if isinstance(assignee_field, dict):
        return assignee_field.get("displayName", "Unknown")
    return str(assignee_field)


def _calculate_days_stale(changed_date: Optional[str]) -> int:
    """Calculate days since last change."""
    if not changed_date:
        return 0
    try:
        dt = datetime.fromisoformat(changed_date.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - dt
        return max(0, delta.days)
    except Exception:
        return 0


# =============================================================================
# LOCAL DATA LOADING (from generated reports)
# =============================================================================

def load_latest_overlooked_report() -> Tuple[Optional[Path], Optional[pd.DataFrame]]:
    """Load the most recent overlooked stories CSV report.
    
    Returns:
        Tuple of (file path, DataFrame) or (None, None) if not found.
    """
    patterns = [
        "overlooked_stories_*.csv",
    ]
    
    all_files = []
    for pattern in patterns:
        files = glob(str(OUTPUTS_DIR / pattern))
        all_files.extend(files)
    
    if not all_files:
        return None, None
    
    # Get the most recent file
    latest = max(all_files, key=os.path.getmtime)
    
    try:
        df = pd.read_csv(latest, encoding="utf-8")
        return Path(latest), df
    except Exception as e:
        logger.warning("Failed to load overlooked stories data from %s: %s", latest, e)
        return None, None


# =============================================================================
# ANALYSIS FUNCTIONS
# =============================================================================

def analyze_overlooked_items(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Analyze overlooked items and return summary statistics.
    
    Args:
        items: List of overlooked work items
        
    Returns:
        Analysis dict with counts, groupings, etc.
    """
    if not items:
        return {
            "total_count": 0,
            "by_area": {},
            "by_assignee": {},
            "by_state": {},
            "oldest_items": [],
            "avg_days_stale": 0,
        }
    
    # Group by area path
    by_area = {}
    for item in items:
        area = item.get("area_path", "Unknown")
        if area not in by_area:
            by_area[area] = []
        by_area[area].append(item)
    
    # Group by assignee
    by_assignee = {}
    for item in items:
        assignee = item.get("assigned_to", "Unassigned")
        if assignee not in by_assignee:
            by_assignee[assignee] = []
        by_assignee[assignee].append(item)
    
    # Group by state
    by_state = {}
    for item in items:
        state = item.get("state", "Unknown")
        if state not in by_state:
            by_state[state] = []
        by_state[state].append(item)
    
    # Find oldest items
    sorted_items = sorted(items, key=lambda x: x.get("days_stale", 0), reverse=True)
    oldest_items = sorted_items[:10]
    
    # Calculate average staleness
    total_days = sum(item.get("days_stale", 0) for item in items)
    avg_days = total_days / len(items) if items else 0
    
    return {
        "total_count": len(items),
        "by_area": {k: len(v) for k, v in by_area.items()},
        "by_assignee": {k: len(v) for k, v in by_assignee.items()},
        "by_state": {k: len(v) for k, v in by_state.items()},
        "oldest_items": oldest_items,
        "avg_days_stale": round(avg_days, 1),
        "items_by_area": by_area,
        "items_by_assignee": by_assignee,
    }


# =============================================================================
# MAIN QUERY HANDLER
# =============================================================================

def handle_overlooked_query(
    query: str,
    intent: Dict[str, Any],
) -> Dict[str, Any]:
    """Handle an overlooked stories query and return response.
    
    Uses LLM-based query understanding for dynamic, flexible responses.
    
    Args:
        query: Original user query
        intent: Intent classification result from semantic_matcher
        
    Returns:
        Response dict with:
        - skill_id: "overlooked_stories"
        - summary_text: Human-readable response
        - evidence_paths: Relevant file paths
        - data: Additional structured data
    """
    # Parse query with LLM for dynamic understanding
    parsed = parse_overlooked_query_with_llm(query)
    query_intent = parsed.get("intent", "general")
    filters = parsed.get("filters", {})
    limit = parsed.get("limit", 10)
    age_days = filters.get("age_days", 14)
    
    # Build response
    response = {
        "skill_id": "overlooked_stories",
        "confidence": intent.get("score", 0),
        "evidence_paths": [],
        "data": {},
    }
    
    summary_parts = []
    
    # Fetch data from ADO
    filter_month = filters.get("filter_current_month", False) or filters.get("time_range") == "current_month"
    area_path_filter = filters.get("area_path")
    
    # Add scope indicators to summary
    scope_indicators = []
    if area_path_filter:
        scope_indicators.append(f"[SCOPE] **Area: {area_path_filter}**")
    if filter_month:
        from datetime import datetime
        current_month_name = datetime.now().strftime("%B %Y")
        scope_indicators.append(f"[FILTER] **Time: {current_month_name}**")
    
    if scope_indicators:
        summary_parts.extend(scope_indicators)
        summary_parts.append("")  # Empty line for spacing
    
    items, error = fetch_overlooked_stories_from_ado(
        age_days=age_days,
        area_path=filters.get("area_path"),
        state=filters.get("state"),
        filter_current_month=filter_month,
    )
    
    if error:
        # Try to load from local reports
        report_path, df = load_latest_overlooked_report()
        if df is not None and not df.empty:
            items = df.to_dict('records')
            response["evidence_paths"].append(str(report_path))
            response["data"]["data_source"] = f"Local report: {report_path.name}"
        else:
            summary_parts.append(f"[WARNING] Could not fetch overlooked stories: {error}")
            summary_parts.append("\nNo local reports available. Run the overlooked stories job first.")
            response["summary_text"] = "\n".join(summary_parts)
            return response
    else:
        response["data"]["data_source"] = "ADO Live"
    
    if not items:
        summary_parts.append(f"[OK] **No Overlooked Stories Found**")
        summary_parts.append(f"\nNo user stories have been inactive for more than {age_days} days.")
        summary_parts.append("All stories have recent activity!")
        response["summary_text"] = "\n".join(summary_parts)
        return response
    
    # Analyze items
    analysis = analyze_overlooked_items(items)
    response["data"]["analysis"] = analysis

    # Attach a local CSV report preview/download if available
    try:
        report_path, df_preview = load_latest_overlooked_report()
        if report_path:
            response["evidence_paths"].append(str(report_path))
            response["data"]["download_path"] = str(report_path)
            # include top items for quick UI rendering
            response["data"]["items"] = items[: (parsed.get("limit", 10) or 10) ]
    except Exception:
        # ignore failures to attach local report
        pass
    
    # Handle different intents
    if query_intent == "count_overlooked":
        summary_parts.append(f"[COUNT] **Overlooked Stories Count**")
        summary_parts.append(f"\nFound **{analysis['total_count']}** overlooked user stories")
        summary_parts.append(f"(inactive for {age_days}+ days)")
        
        if analysis['by_state']:
            summary_parts.append("\n**By State:**")
            for state, count in sorted(analysis['by_state'].items(), key=lambda x: -x[1]):
                summary_parts.append(f"   • {state}: {count}")
    
    elif query_intent == "by_area":
        summary_parts.append(f"[BY AREA] **Overlooked Stories by Area Path**")
        summary_parts.append(f"\nTotal: **{analysis['total_count']}** overlooked stories\n")
        
        sorted_areas = sorted(analysis['by_area'].items(), key=lambda x: -x[1])
        for area, count in sorted_areas[:10]:
            summary_parts.append(f"   • **{area}**: {count} stories")
        
        if len(sorted_areas) > 10:
            summary_parts.append(f"   ... and {len(sorted_areas) - 10} more areas")
    
    elif query_intent == "by_assignee":
        summary_parts.append(f"[BY ASSIGNEE] **Overlooked Stories by Assignee**")
        summary_parts.append(f"\nTotal: **{analysis['total_count']}** overlooked stories\n")
        
        sorted_assignees = sorted(analysis['by_assignee'].items(), key=lambda x: -x[1])
        for assignee, count in sorted_assignees[:10]:
            summary_parts.append(f"   • **{assignee}**: {count} stories")
        
        if len(sorted_assignees) > 10:
            summary_parts.append(f"   ... and {len(sorted_assignees) - 10} more assignees")
    
    elif query_intent == "oldest_items":
        summary_parts.append(f"[OLDEST] **Most Neglected User Stories**")
        summary_parts.append(f"\nShowing the {min(limit, len(analysis['oldest_items']))} oldest overlooked stories:\n")
        
        for i, item in enumerate(analysis['oldest_items'][:limit], 1):
            item_id = item.get('id', 'N/A')
            item_title = item.get('title', 'Untitled')[:50] if item.get('title') else 'Untitled'
            days = item.get('days_stale', 0)
            assignee = item.get('assigned_to', 'Unassigned')
            summary_parts.append(
                f"**{i}. [{item_id}] {item_title}...**\n"
                f"   - Days stale: **{days}** | Assigned to: {assignee}"
            )
    
    elif query_intent == "summary":
        summary_parts.append(f"[SUMMARY] **Overlooked Stories Summary**")
        summary_parts.append(f"\n**Total overlooked:** {analysis['total_count']} stories")
        summary_parts.append(f"**Average staleness:** {analysis['avg_days_stale']} days")
        summary_parts.append(f"**Threshold:** {age_days} days of inactivity\n")
        
        if analysis['by_state']:
            summary_parts.append("**By State:**")
            for state, count in sorted(analysis['by_state'].items(), key=lambda x: -x[1])[:5]:
                summary_parts.append(f"   • {state}: {count}")
        
        if analysis['oldest_items']:
            summary_parts.append(f"\n**Top 3 Most Neglected:**")
            for item in analysis['oldest_items'][:3]:
                item_id = item.get('id', 'N/A')
                item_title = item.get('title', 'Untitled')[:40] if item.get('title') else 'Untitled'
                days = item.get('days_stale', 0)
                summary_parts.append(f"   - [{item_id}] {item_title}... ({days} days)")
    
    elif query_intent == "report":
        summary_parts.append(f"[REPORT] **Overlooked Stories Report**")
        summary_parts.append(f"\nFound **{analysis['total_count']}** overlooked user stories")
        summary_parts.append(f"(inactive for {age_days}+ days)\n")
        
        # Detailed breakdown
        summary_parts.append("**Summary:**")
        summary_parts.append(f"   • Total items: {analysis['total_count']}")
        summary_parts.append(f"   • Average days stale: {analysis['avg_days_stale']}")
        summary_parts.append(f"   • Areas affected: {len(analysis['by_area'])}")
        summary_parts.append(f"   • Assignees involved: {len(analysis['by_assignee'])}")
        
        if analysis['oldest_items']:
            summary_parts.append(f"\n**Most Critical (Oldest Items):**")
            for item in analysis['oldest_items'][:5]:
                item_id = item.get('id', 'N/A')
                item_title = item.get('title', 'Untitled')[:45] if item.get('title') else 'Untitled'
                days = item.get('days_stale', 0)
                summary_parts.append(f"   - [{item_id}] {item_title}... - {days} days")
        
        if parsed.get("wants_email"):
            summary_parts.append("\n[EMAIL] _To send this report via email, run:_")
            summary_parts.append("`python overlooked_user_stories/overlooked_stories_reminder.py`")
    
    else:  # find_overlooked or general
        summary_parts.append(f"[SEARCH] **Overlooked User Stories**")
        summary_parts.append(f"\nFound **{analysis['total_count']}** user stories with no activity for {age_days}+ days\n")
        
        # Show top items
        display_items = analysis['oldest_items'][:limit] if analysis['oldest_items'] else items[:limit]
        
        if display_items:
            summary_parts.append("**Top Overlooked Items:**")
            for i, item in enumerate(display_items, 1):
                # Safe access with validation
                item_id = item.get('id', 'N/A')
                item_title = item.get('title', 'Untitled')[:50] if item.get('title') else 'Untitled'
                days = item.get('days_stale', 0)
                assignee = item.get('assigned_to', 'Unassigned')
                state = item.get('state', 'Unknown')
                summary_parts.append(
                    f"**{i}. [{item_id}] {item_title}**\n"
                    f"   - State: {state} | Days stale: {days} | Assigned: {assignee}"
                )
        
        # Quick stats
        if analysis['by_area']:
            top_area = max(analysis['by_area'].items(), key=lambda x: x[1])
            summary_parts.append(f"\n[AREA] **Most affected area:** {top_area[0]} ({top_area[1]} items)")
    
    # Add suggestions
    summary_parts.append("\n**[TIP] You can also ask:**")
    summary_parts.append("   • _Show overlooked stories by area_")
    summary_parts.append("   • _Who has the most stale stories?_")
    summary_parts.append("   • _What are the oldest neglected items?_")
    summary_parts.append("   • _Generate overlooked stories report_")
    
    response["summary_text"] = "\n".join(summary_parts)
    
    return response
