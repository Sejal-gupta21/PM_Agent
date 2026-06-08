"""Sprint Tracking Responder for chatbot.

Handles queries classified as "sprint_tracking" skill:
- Show offtrack items
- Show blocked items
- Find slowest moving items
- Preview/generate reports
- Send reports (with confirmation)
- Show send history
- Dynamic LLM-based query understanding
"""
from __future__ import annotations

import csv
import json
import logging
import os
import re
import subprocess
import sys
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

def parse_query_with_llm(query: str) -> Dict[str, Any]:
    """Use LLM to understand the user's query intent dynamically.
    
    Returns a structured dict with:
    - intent: str (e.g., "slowest_item", "blocked_items", "offtrack", "status", "send_report")
    - filters: dict of any filters mentioned (assignee, date range, state, etc.)
    - limit: int (how many items to show)
    - sort_by: str (what to sort by)
    - confidence: float
    """
    try:
        import google.generativeai as genai
        
        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            return _fallback_parse_query(query)
        
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-1.5-flash")
        
        prompt = f"""Analyze this sprint/project management query and extract the user's intent.

Query: "{query}"

Respond with a JSON object containing:
{{
  "intent": "<one of: slowest_item, blocked_items, stuck_items, offtrack, delayed, status, progress, send_report, generate_report, show_history, assignee_status, urgent_items, stale_items, general>",
  "filters": {{
    "assignee": "<name or null>",
    "state": "<state filter or null>",
    "days_threshold": <number of days for stale/slow items, default 3>,
    "area_path": "<area path or null>"
  }},
  "limit": <number of items to return, default 5>,
  "sort_by": "<time_in_status, priority, created_date, or null>",
  "wants_details": <true if user wants detailed info, false for summary>
}}

Intent definitions:
- slowest_item: Items that have been in the same status the longest, not making progress
- blocked_items: Items explicitly marked as blocked or with blockers
- stuck_items: Similar to slowest, items not progressing
- offtrack: Items behind schedule, missing deployment dates
- delayed: Items past their due date
- status: General sprint status overview
- progress: Progress report
- stale_items: Items not updated recently
- urgent_items: Items needing immediate attention
- general: General sprint question

Respond ONLY with valid JSON, no markdown or extra text."""

        response = model.generate_content(prompt)
        text = response.text.strip()
        
        # Clean up response
        if text.startswith("```"):
            text = re.sub(r"```json?\n?", "", text)
            text = re.sub(r"```\n?$", "", text)
        
        parsed = json.loads(text)
        parsed["confidence"] = 0.85  # LLM parsing confidence
        return parsed
        
    except Exception as e:
        logger.warning("LLM query parsing failed: %s", e)
        return _fallback_parse_query(query)


def _fallback_parse_query(query: str) -> Dict[str, Any]:
    """Fallback keyword-based query parsing."""
    query_lower = query.lower()
    
    result = {
        "intent": "general",
        "filters": {
            "assignee": None,
            "state": None,
            "days_threshold": 3,
            "area_path": None,
        },
        "limit": 5,
        "sort_by": None,
        "wants_details": False,
        "confidence": 0.6,
    }
    
    # Detect intent from keywords
    if any(term in query_lower for term in ["slowest", "slow", "stuck", "stale", "not moving", "no progress"]):
        result["intent"] = "slowest_item"
        result["sort_by"] = "time_in_status"
    elif any(term in query_lower for term in ["block", "blocked", "blocker", "impediment"]):
        result["intent"] = "blocked_items"
    elif any(term in query_lower for term in ["offtrack", "off-track", "off track", "behind"]):
        result["intent"] = "offtrack"
    elif any(term in query_lower for term in ["delay", "delayed", "overdue", "past due"]):
        result["intent"] = "delayed"
    elif any(term in query_lower for term in ["urgent", "critical", "attention", "priority"]):
        result["intent"] = "urgent_items"
    elif any(term in query_lower for term in ["send", "email", "mail"]):
        result["intent"] = "send_report"
    elif any(term in query_lower for term in ["generate", "create", "run report"]):
        result["intent"] = "generate_report"
    elif any(term in query_lower for term in ["history", "sent", "previous"]):
        result["intent"] = "show_history"
    elif any(term in query_lower for term in ["status", "progress", "how", "what"]):
        result["intent"] = "status"
    
    # Check for limit mentions
    limit_match = re.search(r"(top|first|show me)\s+(\d+)", query_lower)
    if limit_match:
        result["limit"] = int(limit_match.group(2))
    
    # Check for assignee mentions
    if "assigned to" in query_lower:
        match = re.search(r"assigned to\s+([a-z]+)", query_lower)
        if match:
            result["filters"]["assignee"] = match.group(1)
    
    return result


# =============================================================================
# LIVE ADO DATA FETCHING (for current sprint queries)
# =============================================================================

def fetch_current_sprint_items() -> Tuple[Optional[str], Optional[pd.DataFrame]]:
    """Fetch work items from the current sprint in ADO.
    
    Returns:
        Tuple of (sprint_name, DataFrame of work items) or (None, None) on failure.
    """
    try:
        from config import config
        import base64
        import requests
        
        org_url = config.ado_org_url
        project = config.ado_project
        pat = config.ado_pat
        team = os.getenv("ADO_TEAM", "")
        
        if not org_url or not project or not pat:
            logger.warning("ADO credentials not configured")
            return None, None
        
        # Build auth header
        encoded = base64.b64encode(f":{pat}".encode()).decode()
        headers = {"Authorization": f"Basic {encoded}"}
        
        # Get current iteration for the team
        if team:
            iter_url = f"{org_url}/{project}/{team}/_apis/work/teamsettings/iterations?$timeframe=current&api-version=7.1"
        else:
            # Try to get iterations for the project without team
            iter_url = f"{org_url}/{project}/_apis/work/teamsettings/iterations?$timeframe=current&api-version=7.1"
        
        resp = requests.get(iter_url, headers=headers, timeout=30)
        if resp.status_code != 200:
            logger.warning("Failed to fetch current iteration: %s", resp.text[:200])
            return None, None
        
        iterations = resp.json().get("value", [])
        if not iterations:
            logger.info("No current iteration found")
            return None, None
        
        current_iter = iterations[0]
        iter_path = current_iter.get("path", "")
        iter_name = current_iter.get("name", "Current Sprint")
        
        logger.info("Found current iteration: %s", iter_name)
        
        # Query work items in this iteration using WIQL
        wiql = f"""
        SELECT [System.Id], [System.Title], [System.State], [System.AssignedTo],
               [System.WorkItemType], [System.ChangedDate], [System.CreatedDate],
               [Microsoft.VSTS.Common.Priority], [System.Tags]
        FROM WorkItems
        WHERE [System.TeamProject] = @project
          AND [System.IterationPath] UNDER '{iter_path}'
        ORDER BY [System.ChangedDate] DESC
        """
        
        wiql_url = f"{org_url}/{project}/_apis/wit/wiql?api-version=7.1"
        resp = requests.post(wiql_url, json={"query": wiql}, headers=headers, timeout=30)
        if resp.status_code != 200:
            logger.warning("WIQL query failed: %s", resp.text[:200])
            return None, None
        
        work_item_refs = resp.json().get("workItems", [])
        if not work_item_refs:
            logger.info("No work items in current iteration")
            return iter_name, pd.DataFrame()
        
        # Fetch work item details (batch of 200)
        wi_ids = [wi["id"] for wi in work_item_refs[:200]]
        ids_str = ",".join(str(wid) for wid in wi_ids)
        
        fields = "System.Id,System.Title,System.State,System.AssignedTo,System.WorkItemType,System.ChangedDate,System.CreatedDate,Microsoft.VSTS.Common.Priority,System.Tags,Microsoft.VSTS.Scheduling.RemainingWork,Microsoft.VSTS.Scheduling.OriginalEstimate,Microsoft.VSTS.Scheduling.CompletedWork"
        wi_url = f"{org_url}/{project}/_apis/wit/workitems?ids={ids_str}&fields={fields}&api-version=7.1"
        
        resp = requests.get(wi_url, headers=headers, timeout=30)
        if resp.status_code != 200:
            logger.warning("Failed to fetch work item details: %s", resp.text[:200])
            return None, None
        
        work_items = resp.json().get("value", [])
        
        # Convert to DataFrame
        rows = []
        for wi in work_items:
            fields_data = wi.get("fields", {})
            assigned_to = fields_data.get("System.AssignedTo", {})
            if isinstance(assigned_to, dict):
                assigned_to = assigned_to.get("displayName", "Unassigned")
            
            rows.append({
                "ID": wi.get("id"),
                "Title": fields_data.get("System.Title", ""),
                "State": fields_data.get("System.State", ""),
                "Assigned To": assigned_to,
                "Work Item Type": fields_data.get("System.WorkItemType", ""),
                "Changed Date": fields_data.get("System.ChangedDate", ""),
                "Created Date": fields_data.get("System.CreatedDate", ""),
                "Priority": fields_data.get("Microsoft.VSTS.Common.Priority", ""),
                "Tags": fields_data.get("System.Tags", ""),
                "Remaining Work": fields_data.get("Microsoft.VSTS.Scheduling.RemainingWork", 0),
                "Original Estimate": fields_data.get("Microsoft.VSTS.Scheduling.OriginalEstimate", 0),
                "Completed Work": fields_data.get("Microsoft.VSTS.Scheduling.CompletedWork", 0),
            })
        
        df = pd.DataFrame(rows)
        logger.info("Fetched %d work items from current sprint '%s'", len(df), iter_name)
        return iter_name, df
        
    except Exception as e:
        logger.exception("Error fetching current sprint items: %s", e)
        return None, None


# =============================================================================
# DATA LOADING & ANALYSIS
# =============================================================================

def load_latest_sprint_data() -> Tuple[Optional[Path], Optional[pd.DataFrame]]:
    """Load the latest sprint/iteration data from outputs.
    
    Searches for:
    - iteration_report_*.csv
    - sprint_plan_*.csv
    - backlog_assignments_*.csv
    """
    patterns = [
        "iteration_report_*.csv",
        "iteration_report_filtered_*.csv",
        "sprint_plan_*.csv",
        "backlog_assignments_*.csv",
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
        logger.warning("Failed to load sprint data from %s: %s", latest, e)
        return None, None


def _ensure_datetime_cols(df: pd.DataFrame) -> pd.DataFrame:
    """Convert date columns to datetime (timezone-naive for consistent comparison)."""
    date_cols = [
        "Start Date", "End Date", "Last Updated", "Created Date",
        "Changed Date", "State Change Date", "Activated Date",
        "UAT Scheduled Deployment", "PROD Scheduled Deployment",
        "UAT Deploy Date", "PROD Deploy Date"
    ]
    for col in date_cols:
        if col in df.columns:
            df[col] = pd.to_datetime(df[col], errors="coerce", utc=True)
            # Convert to naive datetime for consistent comparisons
            if df[col].dt.tz is not None:
                df[col] = df[col].dt.tz_localize(None)
    return df


def compute_item_metrics(df: pd.DataFrame) -> pd.DataFrame:
    """Compute metrics for sprint items.
    
    Adds columns:
    - time_in_status_days: Days since last update
    - pct_complete: Percentage complete (if hours data available)
    - is_stale: True if not updated in 3+ days
    - priority_score: Computed priority based on various factors
    """
    df = _ensure_datetime_cols(df.copy())
    # Use UTC now, then make naive for consistent comparison with naive datetime columns
    now = pd.Timestamp.utcnow().tz_localize(None)
    
    # Determine last activity date
    activity_cols = ["Last Updated", "Changed Date", "State Change Date"]
    for col in activity_cols:
        if col in df.columns and df[col].notna().any():
            df["_last_activity"] = df[col]
            break
    else:
        # Fallback to Created Date
        if "Created Date" in df.columns:
            df["_last_activity"] = df["Created Date"]
        else:
            df["_last_activity"] = pd.NaT
    
    # Compute time in status
    df["time_in_status_days"] = (now - df["_last_activity"]).dt.total_seconds() / 86400.0
    df["time_in_status_days"] = df["time_in_status_days"].fillna(0).clip(lower=0)
    
    # Compute percentage complete
    if "Remaining Work" in df.columns and "Original Estimate" in df.columns:
        df["pct_complete"] = 100.0 * (1 - (df["Remaining Work"].fillna(0) / df["Original Estimate"].replace(0, 1)))
    elif "Completed Work" in df.columns and "Original Estimate" in df.columns:
        df["pct_complete"] = 100.0 * (df["Completed Work"].fillna(0) / df["Original Estimate"].replace(0, 1))
    else:
        df["pct_complete"] = 0.0
    
    df["pct_complete"] = df["pct_complete"].clip(0, 100)
    
    # Mark stale items (not updated in 3+ days)
    df["is_stale"] = df["time_in_status_days"] >= 3
    
    # Priority score (lower = higher priority)
    df["priority_score"] = 0
    if "Priority" in df.columns:
        priority_map = {"1": 1, "2": 2, "3": 3, "4": 4, "Critical": 1, "High": 2, "Medium": 3, "Low": 4}
        df["priority_score"] = df["Priority"].astype(str).map(priority_map).fillna(5)
    
    # Clean up temp column
    df = df.drop(columns=["_last_activity"], errors="ignore")
    
    return df


def find_slowest_items(df: pd.DataFrame, limit: int = 5, days_threshold: int = 0) -> List[Dict[str, Any]]:
    """Find the slowest moving items (longest time in status).
    
    Args:
        df: Sprint DataFrame with computed metrics
        limit: Max items to return
        days_threshold: Minimum days in status to consider
        
    Returns:
        List of item dicts with id, title, status, time_in_status, assigned_to
    """
    # Filter to active items only - use centralized config
    from config import config as _cfg
    active_states = _cfg.get_states_for_category('not_started') + _cfg.get_states_for_category('in_progress')
    if "State" in df.columns:
        mask = df["State"].isin(active_states)
        active_df = df[mask].copy()
    else:
        active_df = df.copy()
    
    if active_df.empty:
        return []
    
    # Filter by days threshold
    if days_threshold > 0:
        active_df = active_df[active_df["time_in_status_days"] >= days_threshold]
    
    # Sort by time in status (descending)
    active_df = active_df.sort_values(
        by=["time_in_status_days", "pct_complete"],
        ascending=[False, True]
    )
    
    # Build result
    items = []
    for _, row in active_df.head(limit).iterrows():
        item = {
            "id": row.get("ID", row.get("WI ID", row.get("Work Item ID", ""))),
            "title": str(row.get("Title", ""))[:60],
            "state": row.get("State", "Unknown"),
            "time_in_status_days": round(row.get("time_in_status_days", 0), 1),
            "pct_complete": round(row.get("pct_complete", 0), 0),
            "assigned_to": _shorten_name(row.get("Assigned To", "Unassigned")),
            "priority": row.get("Priority", ""),
        }
        items.append(item)
    
    return items


def find_blocked_items(df: pd.DataFrame, limit: int = 10) -> List[Dict[str, Any]]:
    """Find blocked or impeded items.
    
    Checks:
    - State contains 'Blocked'
    - Tags contain 'blocked' or 'impediment'
    - Explicit blocker fields
    """
    mask = pd.Series([False] * len(df), index=df.index)
    
    # Check State
    if "State" in df.columns:
        mask = mask | df["State"].str.contains("Blocked|Impeded", case=False, na=False)
    
    # Check Tags
    for col in ["Tags", "Labels", "System.Tags"]:
        if col in df.columns:
            mask = mask | df[col].astype(str).str.contains("blocked|impediment|blocker", case=False, na=False)
    
    # Check Blocked field
    if "Blocked" in df.columns:
        mask = mask | (df["Blocked"].astype(str).str.lower().isin(["yes", "true", "1"]))
    
    blocked_df = df[mask].copy()
    
    if blocked_df.empty:
        return []
    
    # Sort by time in status
    if "time_in_status_days" in blocked_df.columns:
        blocked_df = blocked_df.sort_values("time_in_status_days", ascending=False)
    
    items = []
    for _, row in blocked_df.head(limit).iterrows():
        item = {
            "id": row.get("ID", row.get("WI ID", row.get("Work Item ID", ""))),
            "title": str(row.get("Title", ""))[:60],
            "state": row.get("State", "Unknown"),
            "time_in_status_days": round(row.get("time_in_status_days", 0), 1),
            "assigned_to": _shorten_name(row.get("Assigned To", "Unassigned")),
            "tags": row.get("Tags", ""),
        }
        items.append(item)
    
    return items


def find_offtrack_items(df: pd.DataFrame, limit: int = 10) -> List[Dict[str, Any]]:
    """Find offtrack items (missing dates, past due, etc.)."""
    mask = pd.Series([False] * len(df), index=df.index)
    
    # Missing UAT/PROD scheduled dates for active items
    from config import config as _cfg
    active_states = _cfg.get_states_for_category('not_started') + _cfg.get_states_for_category('in_progress')
    if "State" in df.columns:
        is_active = df["State"].isin(active_states)
    else:
        is_active = pd.Series([True] * len(df), index=df.index)
    
    # Check for missing deployment dates
    if "UAT Scheduled Deployment" in df.columns and "PROD Scheduled Deployment" in df.columns:
        missing_dates = df["UAT Scheduled Deployment"].isna() & df["PROD Scheduled Deployment"].isna()
        mask = mask | (is_active & missing_dates)
    
    # Check for explicit offtrack status
    for col in ["OffTrack", "Off Track", "IsOffTrack", "Off-Track"]:
        if col in df.columns:
            mask = mask | df[col].astype(str).str.lower().isin(["true", "yes", "1", "offtrack"])
    
    # Check UAT/PROD status for failures
    if "UAT Status" in df.columns:
        mask = mask | df["UAT Status"].str.contains("fail|block|issue", case=False, na=False)
    if "PROD Status" in df.columns:
        mask = mask | df["PROD Status"].str.contains("fail|block|issue", case=False, na=False)
    
    offtrack_df = df[mask].copy()
    
    items = []
    for _, row in offtrack_df.head(limit).iterrows():
        item = {
            "id": row.get("ID", row.get("WI ID", row.get("Work Item ID", ""))),
            "title": str(row.get("Title", ""))[:60],
            "state": row.get("State", "Unknown"),
            "assigned_to": _shorten_name(row.get("Assigned To", "Unassigned")),
            "uat_date": str(row.get("UAT Scheduled Deployment", ""))[:10] or "Missing",
            "prod_date": str(row.get("PROD Scheduled Deployment", ""))[:10] or "Missing",
        }
        items.append(item)
    
    return items


def _shorten_name(name: str) -> str:
    """Shorten a name/email for display."""
    if not name or pd.isna(name):
        return "Unassigned"
    name = str(name)
    if "@" in name:
        return name.split("@")[0]
    if len(name) > 15:
        return name[:15] + "..."
    return name


def get_report_send_history() -> List[Dict[str, Any]]:
    """Load report send history from outputs/report_send_history.json."""
    history_file = OUTPUTS_DIR / "report_send_history.json"
    if not history_file.exists():
        return []
    
    try:
        with history_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("sends", [])
    except Exception as e:
        logger.warning("Failed to load send history: %s", e)
        return []


def get_latest_iteration_report() -> Optional[Dict[str, Any]]:
    """Find the latest iteration report files.
    
    Returns dict with csv_path, html_path, timestamp, row_count
    """
    # Find latest CSV
    csv_pattern = str(OUTPUTS_DIR / "iteration_report_*.csv")
    csv_files = sorted(glob(csv_pattern), reverse=True)
    
    # Also check for filtered versions
    filtered_pattern = str(OUTPUTS_DIR / "iteration_report_filtered_*.csv")
    filtered_files = sorted(glob(filtered_pattern), reverse=True)
    
    # Use filtered if newer, else regular
    all_csvs = csv_files + filtered_files
    if not all_csvs:
        return None
    
    latest_csv = max(all_csvs, key=os.path.getmtime)
    
    # Find matching HTML
    html_path = None
    base = Path(latest_csv).stem
    # Try exact match first
    potential_html = Path(latest_csv).with_suffix(".html")
    if potential_html.exists():
        html_path = str(potential_html)
    else:
        # Look for any HTML with similar timestamp
        html_pattern = str(OUTPUTS_DIR / "iteration_report_*.html")
        html_files = sorted(glob(html_pattern), reverse=True)
        if html_files:
            html_path = html_files[0]
    
    # Count rows
    row_count = 0
    try:
        with open(latest_csv, "r", encoding="utf-8") as f:
            row_count = sum(1 for _ in f) - 1  # Exclude header
    except Exception:
        pass
    
    # Extract timestamp from filename
    ts_match = re.search(r"(\d{8}T\d{6}Z)", latest_csv)
    timestamp = ts_match.group(1) if ts_match else None
    
    return {
        "csv_path": latest_csv,
        "html_path": html_path,
        "timestamp": timestamp,
        "row_count": row_count,
    }


def get_offtrack_count() -> Dict[str, Any]:
    """Get count of offtrack items from latest report.
    
    Returns dict with offtrack_count, ontrack_count, total_count, report_path, offtrack_items, ontrack_items
    """
    from config import config
    report = get_latest_iteration_report()
    if not report:
        return {"offtrack_count": 0, "ontrack_count": 0, "total_count": 0, "report_path": None, "offtrack_items": [], "ontrack_items": []}
    
    csv_path = report["csv_path"]
    
    offtrack_count = 0
    ontrack_count = 0
    total_count = 0
    offtrack_items = []
    ontrack_items = []
    
    try:
        import csv
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                total_count += 1
                wi_id = row.get("ID", "")
                title = row.get("Title", "")[:60]
                assigned = row.get("Assigned To", "Unassigned")
                state = row.get("State", "")
                
                # Build item summary
                item_summary = {
                    "id": wi_id,
                    "title": title,
                    "assigned_to": assigned,
                    "state": state,
                }
                
                # Check various possible column names for offtrack status
                is_offtrack = False
                
                # Check UAT Status, PROD Status columns
                uat_status = row.get("UAT Status", "").lower()
                prod_status = row.get("PROD Status", "").lower()
                
                if any(x in uat_status for x in ["fail", "block", "issue"]) or \
                   any(x in prod_status for x in ["fail", "block", "issue"]):
                    is_offtrack = True
                
                # Check State for blocked/removed
                from config import config as _cfg2
                _blocked_states = [s.lower() for s in _cfg2.get_states_for_category('blocked')]
                if state.lower() in _blocked_states or state.lower() == "removed":
                    is_offtrack = True
                
                # Check explicit OffTrack column
                for col in ["OffTrack", "Off Track", "offtrack", "IsOffTrack"]:
                    val = row.get(col, "").lower()
                    if val in ("true", "yes", "1", "offtrack", "off-track"):
                        is_offtrack = True
                        break
                
                # Check if dates are missing (indicates delays)
                if row.get("PROD Deploy Date", "").strip() == "" and config.classify_state(state) != "Completed":
                    # Not deployed yet, check if past scheduled date
                    scheduled = row.get("PROD Scheduled Deployment", "")
                    if scheduled:
                        # Simple check - if scheduled is in the past
                        is_offtrack = True
                
                if is_offtrack:
                    offtrack_count += 1
                    offtrack_items.append(item_summary)
                else:
                    ontrack_count += 1
                    ontrack_items.append(item_summary)
                    
    except Exception as e:
        logger.warning("Failed to parse report for offtrack count: %s", e)
        # Try HTML parsing as fallback
        if report.get("html_path"):
            try:
                html_content = Path(report["html_path"]).read_text()
                # Count red/offtrack rows
                offtrack_count = html_content.lower().count('offtrack') + \
                                 html_content.lower().count('class="danger"') + \
                                 html_content.lower().count('background-color: #ff')
                ontrack_count = total_count - offtrack_count
            except Exception:
                pass
    
    return {
        "offtrack_count": offtrack_count,
        "ontrack_count": ontrack_count,
        "total_count": total_count,
        "report_path": csv_path,
        "html_path": report.get("html_path"),
        "offtrack_items": offtrack_items[:20],  # Limit for response size
        "ontrack_items": ontrack_items[:20],
    }


def get_upcoming_tasks_summary() -> Dict[str, Any]:
    """Get summary of profiled upcoming tasks."""
    tags_file = DATA_DIR / "wi_tags.json"
    if not tags_file.exists():
        return {"count": 0, "file_path": None}
    
    try:
        with tags_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
        
        items = data.get("items", [])
        
        # Complexity breakdown
        complexity_counts = {}
        for item in items:
            c = item.get("complexity", "Unknown")
            complexity_counts[c] = complexity_counts.get(c, 0) + 1
        
        return {
            "count": len(items),
            "file_path": str(tags_file),
            "complexity_breakdown": complexity_counts,
            "updated_at": data.get("updated_at"),
        }
    except Exception as e:
        logger.warning("Failed to load upcoming tasks: %s", e)
        return {"count": 0, "file_path": None}


def run_daily_report(dry_run: bool = True) -> Dict[str, Any]:
    """Run the daily report script.
    
    Args:
        dry_run: If True, only preview without sending
        
    Returns:
        Dict with success, output, error
    """
    script_path = REPO_ROOT / "scripts" / "run_daily_report.py"
    if not script_path.exists():
        return {"success": False, "error": "Daily report script not found"}
    
    cmd = [sys.executable, str(script_path)]
    if dry_run:
        cmd.append("--dry-run")
    
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
            cwd=str(REPO_ROOT),
        )
        return {
            "success": result.returncode == 0,
            "output": result.stdout,
            "error": result.stderr if result.returncode != 0 else None,
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "Report generation timed out"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def handle_sprint_query(
    query: str,
    intent: Dict[str, Any],
    require_confirmation_callback: Optional[callable] = None,
) -> Dict[str, Any]:
    """Handle a sprint-tracking query and return response.
    
    Uses LLM-based query understanding for dynamic, flexible responses.
    
    Args:
        query: Original user query
        intent: Intent classification result from semantic_matcher
        require_confirmation_callback: Function to call for confirmation on sensitive ops
        
    Returns:
        Response dict with:
        - skill_id: "sprint_tracking"
        - summary_text: Human-readable response
        - actions: List of available actions
        - evidence_paths: Relevant file paths
        - data: Additional structured data
        - requires_confirmation: If True, user must confirm before proceeding
        - pending_action: Action ID that needs confirmation
    """
    # Parse query with LLM for dynamic understanding
    parsed = parse_query_with_llm(query)
    query_intent = parsed.get("intent", "general")
    filters = parsed.get("filters", {})
    limit = parsed.get("limit", 5)
    
    # Build response
    response = {
        "skill_id": "sprint_tracking",
        "confidence": intent.get("score", 0),
        "evidence_paths": [],
        "data": {},
        "requires_confirmation": False,
        "pending_action": None,
    }
    
    summary_parts = []
    actions = []
    
    # Check if user is asking about "current sprint" - fetch live data from ADO
    query_lower = query.lower()
    use_live_data = any(term in query_lower for term in [
        "current sprint", "my sprint", "this sprint", "active sprint",
        "current iteration", "this iteration"
    ])
    
    df = None
    data_source = None
    sprint_name = None
    
    if use_live_data:
        # Fetch live ADO data for current sprint queries
        logger.info("Fetching live sprint data from ADO...")
        sprint_name, df = fetch_current_sprint_items()
        if df is not None and not df.empty:
            df = compute_item_metrics(df)
            data_source = f"ADO Live ({sprint_name})"
            response["data"]["data_source"] = data_source
            response["data"]["sprint_name"] = sprint_name
            logger.info("Loaded %d items from live ADO sprint", len(df))
    
    # Fall back to local CSV data if live data not available or not requested
    if df is None or df.empty:
        data_path, df = load_latest_sprint_data()
        if df is not None and not df.empty:
            df = compute_item_metrics(df)
            data_source = str(data_path)
            response["data"]["data_source"] = data_source
            response["evidence_paths"].append(str(data_path))
    
    # Handle different intents dynamically
    if query_intent in ("slowest_item", "stuck_items", "stale_items"):
        # Find slowest moving items
        summary_parts.append("🐢 **Slowest Moving Items**\n")
        
        if df is not None and not df.empty:
            days_threshold = filters.get("days_threshold", 0)
            slowest = find_slowest_items(df, limit=limit, days_threshold=days_threshold)
            
            if slowest:
                summary_parts.append(f"Found **{len(slowest)}** items with the longest time in current status:\n")
                for i, item in enumerate(slowest, 1):
                    summary_parts.append(
                        f"**{i}. [{item['id']}] {item['title']}**\n"
                        f"   • Status: {item['state']} for **{item['time_in_status_days']:.1f} days**\n"
                        f"   • Progress: {item['pct_complete']:.0f}% complete\n"
                        f"   • Assigned to: {item['assigned_to']}"
                    )
                
                response["data"]["slowest_items"] = slowest
                
                # Save to CSV for download
                csv_path = OUTPUTS_DIR / f"slowest_items_{datetime.now().strftime('%Y%m%d%H%M%S')}.csv"
                pd.DataFrame(slowest).to_csv(csv_path, index=False)
                response["data"]["download_path"] = str(csv_path)
                summary_parts.append(f"\n📥 **Download:** `{csv_path.name}`")
            else:
                summary_parts.append("✅ No significantly slow items found. All items are progressing well!")
        else:
            summary_parts.append("⚠️ No sprint data available. Generate a report first.")
    
    elif query_intent == "blocked_items":
        # Find blocked items
        summary_parts.append("🚧 **Blocked Items**\n")
        
        if df is not None and not df.empty:
            blocked = find_blocked_items(df, limit=limit)
            
            if blocked:
                summary_parts.append(f"Found **{len(blocked)}** blocked/impeded items:\n")
                for i, item in enumerate(blocked, 1):
                    summary_parts.append(
                        f"**{i}. [{item['id']}] {item['title']}**\n"
                        f"   • Status: {item['state']} for {item['time_in_status_days']:.1f} days\n"
                        f"   • Assigned to: {item['assigned_to']}"
                    )
                    if item.get('tags'):
                        summary_parts.append(f"   • Tags: {item['tags']}")
                
                response["data"]["blocked_items"] = blocked
                
                # Save to CSV
                csv_path = OUTPUTS_DIR / f"blocked_items_{datetime.now().strftime('%Y%m%d%H%M%S')}.csv"
                pd.DataFrame(blocked).to_csv(csv_path, index=False)
                response["data"]["download_path"] = str(csv_path)
                summary_parts.append(f"\n📥 **Download:** `{csv_path.name}`")
            else:
                summary_parts.append("✅ No blocked items found. All clear!")
        else:
            summary_parts.append("⚠️ No sprint data available. Generate a report first.")
    
    elif query_intent in ("offtrack", "delayed"):
        # Find offtrack items
        summary_parts.append("⚠️ **Off-Track & Delayed Items**\n")
        
        offtrack_info = get_offtrack_count()
        response["data"]["offtrack"] = offtrack_info
        
        offtrack_count = offtrack_info.get("offtrack_count", 0)
        total_count = offtrack_info.get("total_count", 0)
        
        summary_parts.append(f"📊 **Summary:** {offtrack_count} off-track out of {total_count} total items\n")
        
        offtrack_items = offtrack_info.get("offtrack_items", [])
        if offtrack_items:
            summary_parts.append("**Off-Track Items:**")
            for item in offtrack_items[:limit]:
                wi_id = item.get("id", "")
                title = item.get("title", "")[:50]
                assigned = _shorten_name(item.get("assigned_to", ""))
                summary_parts.append(f"   • [{wi_id}] {title} ({assigned})")
            
            if len(offtrack_items) > limit:
                summary_parts.append(f"   ... and {len(offtrack_items) - limit} more")
        else:
            summary_parts.append("✅ All items are on track!")
        
        if offtrack_info.get("report_path"):
            response["evidence_paths"].append(offtrack_info["report_path"])
            response["data"]["download_path"] = offtrack_info["report_path"]
            summary_parts.append(f"\n📥 **Full report:** `{offtrack_info['report_path']}`")
    
    elif query_intent == "urgent_items":
        # Find items needing urgent attention
        summary_parts.append("🚨 **Urgent Items Needing Attention**\n")
        
        if df is not None and not df.empty:
            # Combine blocked and very stale items
            blocked = find_blocked_items(df, limit=limit)
            slowest = find_slowest_items(df, limit=limit, days_threshold=5)
            
            urgent_items = []
            seen_ids = set()
            
            for item in blocked:
                if item['id'] not in seen_ids:
                    item['reason'] = 'Blocked'
                    urgent_items.append(item)
                    seen_ids.add(item['id'])
            
            for item in slowest:
                if item['id'] not in seen_ids:
                    item['reason'] = f"Stale ({item['time_in_status_days']:.0f} days)"
                    urgent_items.append(item)
                    seen_ids.add(item['id'])
            
            if urgent_items:
                summary_parts.append(f"Found **{len(urgent_items)}** items needing attention:\n")
                for i, item in enumerate(urgent_items[:limit], 1):
                    summary_parts.append(
                        f"**{i}. [{item['id']}] {item['title']}**\n"
                        f"   • Reason: {item.get('reason', 'Unknown')}\n"
                        f"   • Assigned to: {item['assigned_to']}"
                    )
                
                response["data"]["urgent_items"] = urgent_items
            else:
                summary_parts.append("✅ No urgent items requiring attention!")
        else:
            summary_parts.append("⚠️ No sprint data available.")
    
    elif query_intent == "send_report":
        # Handle send report request
        response["requires_confirmation"] = True
        response["pending_action"] = "send_report"
        
        summary_parts.append("📧 **Send Daily Report**\n")
        summary_parts.append("⚠️ This will send the iteration report to configured recipients.")
        summary_parts.append("\n**Reply 'confirm' to send, or 'cancel' to abort.**")
        
        actions.append({
            "id": "send_report",
            "label": "Send report email",
            "requires_confirmation": True,
        })
    
    elif query_intent == "generate_report":
        # Generate new report
        summary_parts.append("📋 **Generate Daily Report**\n")
        summary_parts.append("Running report generation (dry-run)...")
        
        result = run_daily_report(dry_run=True)
        if result["success"]:
            summary_parts.append("✅ Report generated successfully!")
            if result.get("output"):
                # Show last few lines of output
                output_lines = result["output"].strip().split("\n")[-5:]
                summary_parts.append("\n**Output:**")
                for line in output_lines:
                    summary_parts.append(f"   {line}")
        else:
            summary_parts.append(f"❌ Report generation failed: {result.get('error', 'Unknown error')}")
    
    elif query_intent == "show_history":
        # Show send history
        summary_parts.append("📜 **Report Send History**\n")
        
        history = get_report_send_history()
        response["data"]["send_history"] = history[-5:]
        
        if history:
            summary_parts.append(f"Found **{len(history)}** report sends:\n")
            for send in reversed(history[-5:]):
                ts = send.get("timestamp", "Unknown")
                recipients = ", ".join(send.get("recipients", []))[:50]
                summary_parts.append(f"   • {ts}: {recipients}")
        else:
            summary_parts.append("No reports have been sent yet.")
    
    else:
        # General status / fallback
        summary_parts.append("📊 **Sprint Status Overview**\n")
        
        offtrack_info = get_offtrack_count()
        response["data"]["offtrack"] = offtrack_info
        
        offtrack_count = offtrack_info.get("offtrack_count", 0)
        ontrack_count = offtrack_info.get("ontrack_count", 0)
        total_count = offtrack_info.get("total_count", 0)
        
        summary_parts.append(f"**Current Sprint Health:**")
        summary_parts.append(f"   • ✅ On-track: {ontrack_count}")
        summary_parts.append(f"   • ⚠️ Off-track: {offtrack_count}")
        summary_parts.append(f"   • 📊 Total: {total_count}\n")
        
        if df is not None and not df.empty:
            # Quick stats
            stale_count = df["is_stale"].sum() if "is_stale" in df.columns else 0
            blocked = find_blocked_items(df, limit=3)
            slowest = find_slowest_items(df, limit=3)
            
            if stale_count > 0:
                summary_parts.append(f"**⏰ Stale items (>3 days):** {stale_count}")
            
            if blocked:
                summary_parts.append(f"\n**🚧 Top Blocked Items:**")
                for item in blocked[:3]:
                    summary_parts.append(f"   • [{item['id']}] {item['title']} ({item['assigned_to']})")
            
            if slowest:
                summary_parts.append(f"\n**🐢 Slowest Items:**")
                for item in slowest[:3]:
                    summary_parts.append(f"   • [{item['id']}] {item['title']} - {item['time_in_status_days']:.1f} days")
        
        # Show offtrack items if any
        offtrack_items = offtrack_info.get("offtrack_items", [])
        if offtrack_items:
            summary_parts.append(f"\n**⚠️ Off-Track Items ({offtrack_count}):**")
            for item in offtrack_items[:5]:
                wi_id = item.get("id", "")
                title = item.get("title", "")[:40]
                assigned = _shorten_name(item.get("assigned_to", ""))
                summary_parts.append(f"   • [{wi_id}] {title} ({assigned})")
        
        if offtrack_info.get("report_path"):
            response["evidence_paths"].append(offtrack_info["report_path"])
            response["data"]["download_path"] = offtrack_info["report_path"]
    
    # Add available actions
    if not actions:
        actions = [
            {"id": "show_slowest", "label": "Show slowest items"},
            {"id": "show_blocked", "label": "Show blocked items"},
            {"id": "show_offtrack", "label": "Show off-track items"},
            {"id": "generate_report", "label": "Generate report"},
            {"id": "send_report", "label": "Send report (requires confirmation)", "requires_confirmation": True},
        ]
    
    summary_parts.append("\n**💡 You can also ask:**")
    summary_parts.append("   • _What items are blocked?_")
    summary_parts.append("   • _Which item is moving the slowest?_")
    summary_parts.append("   • _Show me stale items_")
    summary_parts.append("   • _What needs urgent attention?_")
    
    response["summary_text"] = "\n".join(summary_parts)
    response["actions"] = actions
    
    return response


def execute_action(
    action_id: str,
    confirmed: bool = False,
) -> Dict[str, Any]:
    """Execute a sprint-tracking action.
    
    Args:
        action_id: The action to execute
        confirmed: Whether user has confirmed (for sensitive actions)
        
    Returns:
        Result dict with success, message, data
    """
    if action_id == "show_offtrack":
        offtrack = get_offtrack_count()
        return {
            "success": True,
            "message": f"Found {offtrack['offtrack_count']} offtrack items",
            "data": offtrack,
        }
    
    elif action_id == "preview_report":
        report = get_latest_iteration_report()
        return {
            "success": True,
            "message": f"Latest report: {report['csv_path'] if report else 'None'}",
            "data": report,
        }
    
    elif action_id == "run_report":
        result = run_daily_report(dry_run=True)
        return {
            "success": result["success"],
            "message": "Report generated (dry-run)" if result["success"] else result["error"],
            "data": {"output": result.get("output", "")[:1000]},
        }
    
    elif action_id == "send_report":
        if not confirmed:
            return {
                "success": False,
                "message": "Confirmation required to send report. Reply 'confirm' to proceed.",
                "requires_confirmation": True,
            }
        
        result = run_daily_report(dry_run=False)
        return {
            "success": result["success"],
            "message": "Report sent!" if result["success"] else result["error"],
            "data": {"output": result.get("output", "")[:1000]},
        }
    
    elif action_id == "show_history":
        history = get_report_send_history()
        return {
            "success": True,
            "message": f"Found {len(history)} report sends",
            "data": {"history": history[-10:]},
        }
    
    else:
        return {
            "success": False,
            "message": f"Unknown action: {action_id}",
        }
