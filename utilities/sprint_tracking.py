"""Sprint Tracking Logic - Compare planned vs completed work.

Implements Row 6 from spreadsheet:
1. Compare planned vs completed
2. Identify off-track tasks
3. Trigger PM warning mail
"""
from __future__ import annotations

import logging
import os
import re
import difflib
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from config import config

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUTS_DIR = REPO_ROOT / "outputs"


def get_ado_headers() -> Dict[str, str]:
    """Get ADO API headers with PAT authentication."""
    pat = os.getenv("ADO_PAT") or config.ado_pat
    import base64
    auth = base64.b64encode(f":{pat}".encode()).decode()
    return {
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/json"
    }


def get_sprint_info(
    query: str = None,
    team: Optional[str] = None,
    state: str = "current",
    org_url: Optional[str] = None,
    project: Optional[str] = None,
    pat: Optional[str] = None
) -> Dict[str, Any]:
    """
    Get sprint/iteration information with advanced features.
    
    This is the main entry point for fetching sprint information with support for:
    - Natural language query parsing
    - Team name fuzzy matching
    - Current/previous/future sprints
    - All teams mode
    - 2-turn flow (prompts for team if missing)
    
    Args:
        query: Natural language query like "current sprint for xops 25"
        team: Team name (overrides query parsing)
        state: "current", "past", or "future"
        org_url: ADO org URL (uses config if not provided)
        project: Project name (uses config if not provided)
        pat: Personal Access Token (uses config if not provided)
        
    Returns:
        {
            "success": True/False,
            "needs_team_input": True/False,
            "prompt": "..." (if needs team),
            "message": "..." (formatted response),
            "data": {...},
            "available_teams": [...]
        }
    """
    try:
        org_url = org_url or os.getenv("ADO_ORG_URL") or config.ado_org_url
        project = project or os.getenv("ADO_PROJECT") or config.ado_project
        pat = pat or os.getenv("ADO_PAT") or config.ado_pat
        
        # Parse query if provided
        parsed = {"team": team, "state": state, "all_teams": False, "needs_team_input": False}
        if query:
            parsed = _parse_sprint_query(query)
            if team:  # Override if explicitly provided
                parsed["team"] = team
                parsed["needs_team_input"] = False
            if state != "current":  # Override if explicitly provided
                parsed["state"] = state
        
        # Case 1: All teams mode
        if parsed.get("all_teams"):
            iterations = _fetch_all_teams_iterations(org_url, project, pat)
            message = _format_all_teams_response(iterations)
            return {
                "success": bool(iterations),
                "needs_team_input": False,
                "message": message,
                "data": {"iterations": iterations}
            }
        
        # Case 2: Need team input
        if parsed.get("needs_team_input") and not parsed.get("team"):
            teams = _fetch_teams(org_url, project, pat)
            prompt = f"Please provide the **team name** to fetch the sprint information.\n\n"
            prompt += f"**Available teams:**\n" + "\n".join([f"  • {t}" for t in teams])
            return {
                "success": False,
                "needs_team_input": True,
                "prompt": prompt,
                "available_teams": teams,
                "data": {}
            }
        
        # Case 3: Fetch for specific team
        team_name = parsed.get("team")
        sprint_state = parsed.get("state", "current")
        
        # If no team provided, use default from config
        if not team_name:
            team_name = os.getenv("ADO_TEAM", "") or getattr(config, "ado_team", "")
            if not team_name:
                teams = _fetch_teams(org_url, project, pat)
                prompt = f"Please provide the **team name** to fetch the sprint information.\n\n"
                prompt += f"**Available teams:**\n" + "\n".join([f"  • {t}" for t in teams])
                return {
                    "success": False,
                    "needs_team_input": True,
                    "prompt": prompt,
                    "available_teams": teams,
                    "data": {}
                }
        
        # Resolve team name (fuzzy matching)
        resolved_team = _resolve_team_name(team_name, org_url, project, pat)
        if not resolved_team:
            teams = _fetch_teams(org_url, project, pat)
            prompt = f"Team '{team_name}' not found.\n\n" 
            prompt += f"**Available teams:**\n" + "\n".join([f"  • {t}" for t in teams])
            return {
                "success": False,
                "needs_team_input": True,
                "prompt": prompt,
                "available_teams": teams,
                "data": {}
            }
        
        # Fetch iteration
        iteration = _fetch_team_iteration(resolved_team, sprint_state, org_url, project, pat)
        
        if not iteration:
            return {
                "success": False,
                "needs_team_input": False,
                "message": f"❌ No {sprint_state} iteration found for team **{resolved_team}**",
                "data": {}
            }
        
        # Format response
        message = _format_single_team_response(resolved_team, iteration)
        
        return {
            "success": True,
            "needs_team_input": False,
            "message": message,
            "data": {"team": resolved_team, "iteration": iteration}
        }
        
    except Exception as e:
        logger.exception(f"Error in get_sprint_info: {e}")
        return {
            "success": False,
            "needs_team_input": False,
            "message": f"❌ Error: {str(e)}",
            "error": str(e),
            "data": {}
        }


def _parse_sprint_query(query: str) -> Dict[str, Any]:
    """Parse natural language query to extract team, state, and flags."""
    result = {
        "team": None,
        "state": "current",
        "all_teams": False,
        "needs_team_input": True
    }
    
    query_lower = query.lower()
    
    # Check for "all teams"
    all_teams_patterns = [
        r"\ball\s+(teams?|area\s*paths?)\b",
        r"\bfor\s+all\b",
        r"\ball\s+(current|active)\s+(sprints?|iterations?)\b",
    ]
    for pattern in all_teams_patterns:
        if re.search(pattern, query_lower):
            result["all_teams"] = True
            result["needs_team_input"] = False
            return result
    
    # Extract team name
    # Patterns ordered by specificity - most specific first
    team_patterns = [
        # Pattern 1: Team after for/in/of with punctuation or end (handles "for XOPS 25?")
        r"(?:for|in|of)\s+([a-zA-Z0-9\s\-_]+?)(?:\s*[\?\!\.;,:]|\s*$)",
        # Pattern 2: Team after for/in/of followed by keywords
        r"(?:for|in|of)\s+([a-zA-Z0-9\s\-_]+?)(?:\s+team|\s+sprint|\s+current|\s+previous|\s+next|$)",
        # Pattern 3: Team at start followed by current/previous sprint
        r"^([a-zA-Z0-9\s\-_]+?)\s+(?:current|previous|next|past|future)\s+(?:sprint|iteration)",
        # Pattern 4: Explicit "team X" pattern
        r"team\s+([a-zA-Z0-9\s\-_]+?)(?:\s*[\?\!\.;,:]|\s|$)",
    ]
    
    exclude_words = [
        'current', 'previous', 'next', 'past', 'future', 'sprint', 'iteration',
        'all', 'teams', 'team', 'area', 'paths', 'give', 'show', 'get', 'fetch', 'what',
        'is', 'the', 'me', 'my', 'number', 'and', 'its', 'start', 'end', 'date'
    ]
    
    for pattern in team_patterns:
        match = re.search(pattern, query, re.IGNORECASE)
        if match:
            team = match.group(1).strip()
            team_cleaned = ' '.join(w for w in team.split() if w.lower() not in exclude_words)
            if team_cleaned:
                result["team"] = team_cleaned
                result["needs_team_input"] = False
                break
    
    # Determine state
    if re.search(r"\b(previous|past|last)\s+(sprint|iteration)\b", query_lower):
        result["state"] = "past"
    elif re.search(r"\b(next|future|upcoming)\s+(sprint|iteration)\b", query_lower):
        result["state"] = "future"
    
    return result


def _fetch_teams(org_url: str, project: str, pat: str) -> List[str]:
    """Fetch all team names from ADO."""
    try:
        url = f"{org_url}/_apis/projects/{project}/teams?api-version=7.0"
        import base64
        auth = base64.b64encode(f":{pat}".encode()).decode()
        headers = {"Authorization": f"Basic {auth}"}
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code == 200:
            data = resp.json()
            teams = [t.get("name") for t in data.get("value", []) if t.get("name")]
            return sorted(teams)
        return []
    except Exception as e:
        logger.error(f"Error fetching teams: {e}")
        return []


def _resolve_team_name(team_input: str, org_url: str, project: str, pat: str) -> Optional[str]:
    """Resolve team name using fuzzy matching."""
    if not team_input:
        return None
    
    teams = _fetch_teams(org_url, project, pat)
    team_input_lower = team_input.lower().strip()
    
    # Exact match
    for team in teams:
        if team.lower() == team_input_lower:
            return team
    
    # Partial match
    for team in teams:
        if team_input_lower in team.lower() or team.lower() in team_input_lower:
            return team
    
    # Fuzzy match
    matches = difflib.get_close_matches(team_input, teams, n=1, cutoff=0.6)
    return matches[0] if matches else None


def _fetch_team_iteration(
    team: str,
    state: str,
    org_url: str,
    project: str,
    pat: str
) -> Optional[Dict[str, Any]]:
    """Fetch iteration for a specific team."""
    try:
        team_encoded = urllib.parse.quote(team)
        import base64
        auth = base64.b64encode(f":{pat}".encode()).decode()
        headers = {"Authorization": f"Basic {auth}"}
        
        # Fetch iterations
        if state == "current":
            url = f"{org_url}/{project}/{team_encoded}/_apis/work/teamsettings/iterations?$timeframe=current&api-version=7.0"
        else:
            url = f"{org_url}/{project}/{team_encoded}/_apis/work/teamsettings/iterations?api-version=7.0"
        
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code != 200:
            logger.error(f"Failed to fetch iterations: {resp.status_code}")
            return None
        
        iterations = resp.json().get("value", [])
        if not iterations:
            return None
        
        # Select iteration based on state
        if state == "current":
            return _format_iteration(iterations[0])
        elif state == "past":
            return _find_previous_iteration(iterations)
        elif state == "future":
            return _find_next_iteration(iterations)
        
        return None
        
    except Exception as e:
        logger.exception(f"Error fetching iteration: {e}")
        return None


def _find_previous_iteration(iterations: List[Dict]) -> Optional[Dict]:
    """Find previous iteration relative to current."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    parsed = []
    
    for it in iterations:
        attrs = it.get("attributes", {})
        try:
            start = datetime.fromisoformat(attrs.get("startDate", "").replace("Z", "+00:00")).replace(tzinfo=None)
            finish = datetime.fromisoformat(attrs.get("finishDate", "").replace("Z", "+00:00")).replace(tzinfo=None)
            parsed.append({"iteration": it, "start": start, "finish": finish, "is_current": start <= now <= finish})
        except:
            continue
    
    parsed.sort(key=lambda x: x["start"])
    
    # Find current
    current_idx = -1
    for idx, p in enumerate(parsed):
        if p["is_current"]:
            current_idx = idx
            break
    
    # Get previous
    if current_idx > 0:
        return _format_iteration(parsed[current_idx - 1]["iteration"])
    
    return None


def _find_next_iteration(iterations: List[Dict]) -> Optional[Dict]:
    """Find next iteration relative to current."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    parsed = []
    
    for it in iterations:
        attrs = it.get("attributes", {})
        try:
            start = datetime.fromisoformat(attrs.get("startDate", "").replace("Z", "+00:00")).replace(tzinfo=None)
            finish = datetime.fromisoformat(attrs.get("finishDate", "").replace("Z", "+00:00")).replace(tzinfo=None)
            parsed.append({"iteration": it, "start": start, "finish": finish, "is_current": start <= now <= finish})
        except:
            continue
    
    parsed.sort(key=lambda x: x["start"])
    
    # Find current
    current_idx = -1
    for idx, p in enumerate(parsed):
        if p["is_current"]:
            current_idx = idx
            break
    
    # Get next
    if current_idx >= 0 and current_idx < len(parsed) - 1:
        return _format_iteration(parsed[current_idx + 1]["iteration"])
    
    return None


def _format_iteration(iteration: Dict) -> Dict[str, Any]:
    """Format iteration to standard structure."""
    attrs = iteration.get("attributes", {})
    return {
        "id": iteration.get("id"),
        "name": iteration.get("name"),
        "path": iteration.get("path"),
        "start_date": attrs.get("startDate", "")[:10] if attrs.get("startDate") else None,
        "finish_date": attrs.get("finishDate", "")[:10] if attrs.get("finishDate") else None,
        "timeFrame": attrs.get("timeFrame", "unknown"),
    }


def _fetch_all_teams_iterations(org_url: str, project: str, pat: str) -> List[Dict]:
    """Fetch current iteration for all teams."""
    teams = _fetch_teams(org_url, project, pat)
    results = []
    
    for team in teams:
        iteration = _fetch_team_iteration(team, "current", org_url, project, pat)
        results.append({
            "team": team,
            "iteration": iteration,
            "success": iteration is not None
        })
    
    return results


def _format_single_team_response(team: str, iteration: Dict) -> str:
    """Format single team iteration response."""
    name = iteration.get("name", "Unknown")
    path = iteration.get("path", "")
    start = iteration.get("start_date", "N/A")
    finish = iteration.get("finish_date", "N/A")
    status = iteration.get("timeFrame", "unknown").capitalize()
    
    return f"""## 🏃 Sprint Information for **{team}**

| Property | Value |
|----------|-------|
| **Sprint Number** | {name} |
| **Sprint Path** | `{path}` |
| **Start Date** | {start} |
| **End Date** | {finish} |
| **Status** | {status} |"""


def _format_all_teams_response(iterations: List[Dict]) -> str:
    """Format all teams iterations response."""
    successful = [i for i in iterations if i.get("success")]
    total = len(iterations)
    
    lines = [
        f"## 🏃 Current Sprints for All Teams\n",
        f"**Successfully fetched:** {len(successful)}/{total} teams\n",
        f"| Team | Current Sprint | Start Date | End Date |",
        f"|------|----------------|------------|----------|",
    ]
    
    for item in iterations:
        team = item.get("team", "Unknown")
        it = item.get("iteration")
        
        if it:
            name = it.get("name", "N/A")
            start = it.get("start_date", "N/A")
            finish = it.get("finish_date", "N/A")
            lines.append(f"| {team} | **{name}** | {start} | {finish} |")
        else:
            lines.append(f"| {team} | ❌ No iteration | - | - |")
    
    return "\n".join(lines)


def get_current_sprint_info() -> Optional[Dict[str, Any]]:
    """Get current sprint information from ADO.
    
    Legacy function - maintained for backward compatibility.
    Uses default team from config or environment.
    """
    try:
        org_url = os.getenv("ADO_ORG_URL") or config.ado_org_url
        project = os.getenv("ADO_PROJECT") or config.ado_project
        team = os.getenv("ADO_TEAM", "") or getattr(config, "ado_team", "")
        
        if not team:
            logger.warning("No default team configured for get_current_sprint_info()")
            return None
        
        result = get_sprint_info(team=team, state="current")
        
        if result.get("success") and result.get("data", {}).get("iteration"):
            iteration = result["data"]["iteration"]
            # Return in legacy format for backward compatibility
            return {
                "id": iteration.get("id"),
                "name": iteration.get("name"),
                "path": iteration.get("path"),
                "start_date": iteration.get("start_date"),
                "finish_date": iteration.get("finish_date"),
            }
        return None
    except Exception as e:
        logger.error(f"Failed to get current sprint: {e}")
        return None


def get_sprint_work_items(sprint_path: str) -> List[Dict[str, Any]]:
    """Get all work items for a sprint."""
    try:
        org_url = os.getenv("ADO_ORG_URL") or config.ado_org_url
        project = os.getenv("ADO_PROJECT") or config.ado_project
        
        # WIQL query to get all items in the sprint
        wiql = {
            "query": f"""
                SELECT [System.Id], [System.Title], [System.State], 
                       [System.AssignedTo], [System.WorkItemType],
                       [Microsoft.VSTS.Scheduling.StoryPoints],
                       [Microsoft.VSTS.Scheduling.OriginalEstimate],
                       [Microsoft.VSTS.Scheduling.CompletedWork],
                       [System.IterationPath], [System.CreatedDate],
                       [Microsoft.VSTS.Common.StateChangeDate]
                FROM WorkItems
                WHERE [System.IterationPath] = '{sprint_path}'
                AND [System.WorkItemType] IN ('User Story', 'Bug', 'Task')
                ORDER BY [System.State] ASC
            """
        }
        
        url = f"{org_url}/{project}/_apis/wit/wiql?api-version=7.0"
        response = requests.post(url, headers=get_ado_headers(), json=wiql, timeout=30)
        response.raise_for_status()
        
        work_item_refs = response.json().get("workItems", [])
        if not work_item_refs:
            return []
        
        # Get full details
        ids = [str(ref["id"]) for ref in work_item_refs]
        ids_str = ",".join(ids)
        url = f"{org_url}/{project}/_apis/wit/workitems?ids={ids_str}&api-version=7.0"
        response = requests.get(url, headers=get_ado_headers(), timeout=30)
        response.raise_for_status()
        
        return response.json().get("value", [])
    except Exception as e:
        logger.error(f"Failed to get sprint work items: {e}")
        return []


def analyze_sprint_progress(work_items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Analyze sprint progress - planned vs completed."""
    from config import config
    total = len(work_items)
    
    states_count = {
        "completed": 0,
        "in_progress": 0,
        "not_started": 0,
        "blocked": 0
    }
    
    # Dynamic state lists from centralized config
    completed_states = config.get_states_for_category('completed')
    in_progress_states = config.get_states_for_category('in_progress')
    blocked_states = config.get_states_for_category('blocked')
    blocked_keywords = ["blocked", "block", "impediment"]
    
    offtrack_items = []
    blocked_items = []
    
    total_story_points = 0
    completed_story_points = 0
    
    for item in work_items:
        fields = item.get("fields", {})
        state = fields.get("System.State", "")
        title = fields.get("System.Title", "")
        item_id = item.get("id")
        
        # Story points
        story_points = fields.get("Microsoft.VSTS.Scheduling.StoryPoints", 0) or 0
        total_story_points += story_points
        
        if state in completed_states:
            states_count["completed"] += 1
            completed_story_points += story_points
        elif state in blocked_states:
            # Check blocked states from config BEFORE in_progress
            states_count["blocked"] += 1
            blocked_items.append({
                "id": item_id,
                "title": title,
                "state": state,
                "assignee": fields.get("System.AssignedTo", {}).get("displayName", "Unassigned")
            })
        elif state in in_progress_states:
            states_count["in_progress"] += 1
            
            # Also check if blocked by keywords in title/tags
            if any(kw in title.lower() for kw in blocked_keywords):
                states_count["blocked"] += 1
                blocked_items.append({
                    "id": item_id,
                    "title": title,
                    "state": state,
                    "assignee": fields.get("System.AssignedTo", {}).get("displayName", "Unassigned")
                })
            
            # Check if off-track (in progress > 3 days without completion)
            state_change_date = fields.get("Microsoft.VSTS.Common.StateChangeDate")
            if state_change_date:
                days_in_state = (datetime.now(timezone.utc) - datetime.fromisoformat(state_change_date.replace("Z", "+00:00"))).days
                if days_in_state > 3:
                    offtrack_items.append({
                        "id": item_id,
                        "title": title,
                        "state": state,
                        "days_in_state": days_in_state,
                        "assignee": fields.get("System.AssignedTo", {}).get("displayName", "Unassigned")
                    })
        else:
            states_count["not_started"] += 1
    
    completion_rate = (states_count["completed"] / total * 100) if total > 0 else 0
    story_points_completion = (completed_story_points / total_story_points * 100) if total_story_points > 0 else 0
    
    return {
        "total_items": total,
        "completed": states_count["completed"],
        "in_progress": states_count["in_progress"],
        "not_started": states_count["not_started"],
        "blocked": states_count["blocked"],
        "completion_rate": round(completion_rate, 2),
        "total_story_points": total_story_points,
        "completed_story_points": completed_story_points,
        "story_points_completion": round(story_points_completion, 2),
        "offtrack_items": offtrack_items[:10],  # Top 10
        "blocked_items": blocked_items,
        "needs_pm_attention": completion_rate < 50 or len(blocked_items) > 3 or len(offtrack_items) > 5
    }


def generate_sprint_status_report() -> str:
    """Generate human-readable sprint status report."""
    sprint = get_current_sprint_info()
    if not sprint:
        return "⚠️ No active sprint found."
    
    work_items = get_sprint_work_items(sprint["path"])
    if not work_items:
        return f"📋 Sprint '{sprint['name']}' has no work items."
    
    analysis = analyze_sprint_progress(work_items)
    
    report = f"""📊 **Sprint Status Report: {sprint['name']}**

**Overview:**
- Total Work Items: {analysis['total_items']}
- Completed: {analysis['completed']} ({analysis['completion_rate']}%)
- In Progress: {analysis['in_progress']}
- Not Started: {analysis['not_started']}
- Blocked: {analysis['blocked']}

**Story Points:**
- Total: {analysis['total_story_points']}
- Completed: {analysis['completed_story_points']} ({analysis['story_points_completion']}%)

"""
    
    if analysis['offtrack_items']:
        report += "\n**⚠️ Off-Track Items:**\n"
        for item in analysis['offtrack_items'][:5]:
            report += f"- #{item['id']}: {item['title']} (in {item['state']} for {item['days_in_state']} days)\n"
    
    if analysis['blocked_items']:
        report += "\n**🚫 Blocked Items:**\n"
        for item in analysis['blocked_items'][:5]:
            report += f"- #{item['id']}: {item['title']} - {item['assignee']}\n"
    
    if analysis['needs_pm_attention']:
        report += "\n⚠️ **PM ATTENTION REQUIRED** - Sprint needs intervention!\n"
    
    return report


def should_trigger_pm_warning() -> Tuple[bool, str]:
    """Check if PM warning should be triggered based on sprint health."""
    sprint = get_current_sprint_info()
    if not sprint:
        return False, "No active sprint"
    
    work_items = get_sprint_work_items(sprint["path"])
    if not work_items:
        return False, "No work items"
    
    analysis = analyze_sprint_progress(work_items)
    
    reasons = []
    
    # Trigger conditions
    if analysis['completion_rate'] < 30:
        reasons.append(f"Low completion rate: {analysis['completion_rate']}%")
    
    if analysis['blocked'] > 3:
        reasons.append(f"{analysis['blocked']} items blocked")
    
    if len(analysis['offtrack_items']) > 5:
        reasons.append(f"{len(analysis['offtrack_items'])} items off-track")
    
    # Check if sprint end is near (within 2 days)
    try:
        finish_date = datetime.fromisoformat(sprint["finish_date"].replace("Z", "+00:00"))
        days_remaining = (finish_date - datetime.now(timezone.utc)).days
        
        if days_remaining <= 2 and analysis['completion_rate'] < 70:
            reasons.append(f"Sprint ends in {days_remaining} days with only {analysis['completion_rate']}% completion")
    except Exception:
        pass
    
    should_trigger = len(reasons) > 0
    reason_text = "; ".join(reasons) if reasons else "Sprint healthy"
    
    return should_trigger, reason_text


if __name__ == "__main__":
    # Test the module
    print(generate_sprint_status_report())
    should_warn, reason = should_trigger_pm_warning()
    print(f"\nPM Warning: {should_warn} - {reason}")
