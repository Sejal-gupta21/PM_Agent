"""Report Generation - Daily, Weekly, and Module-wise reports.

Implements Row 12 from spreadsheet:
1. Build daily sprint report
2. Build weekly summary
3. Build module-wise bug report
"""
from __future__ import annotations

import logging
import os
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from config import config

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUTS_DIR = REPO_ROOT / "outputs"
OUTPUTS_DIR.mkdir(exist_ok=True)


def get_ado_headers() -> Dict[str, str]:
    """Get ADO API headers with PAT authentication."""
    pat = os.getenv("ADO_PAT") or config.ado_pat
    import base64
    auth = base64.b64encode(f":{pat}".encode()).decode()
    return {
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/json"
    }


def get_sprint_work_items(sprint_path: str, include_completed: bool = False) -> List[Dict[str, Any]]:
    """Get work items for a sprint."""
    try:
        org_url = os.getenv("ADO_ORG_URL") or config.ado_org_url
        project = os.getenv("ADO_PROJECT") or config.ado_project
        
        state_filter = ""
        if not include_completed:
            _completed = config.get_states_for_category('completed')
            _states_sql = ", ".join(f"'{s}'" for s in _completed)
            state_filter = f"AND [System.State] NOT IN ({_states_sql})"
        
        wiql = {
            "query": f"""
                SELECT [System.Id], [System.Title], [System.State], 
                       [System.AssignedTo], [System.WorkItemType],
                       [Microsoft.VSTS.Scheduling.StoryPoints],
                       [System.AreaPath], [System.Tags],
                       [Microsoft.VSTS.Common.Priority],
                       [System.CreatedDate], [System.ChangedDate]
                FROM WorkItems
                WHERE [System.IterationPath] = '{sprint_path}'
                {state_filter}
                ORDER BY [System.ChangedDate] DESC
            """
        }
        
        url = f"{org_url}/{project}/_apis/wit/wiql?api-version=7.0"
        response = requests.post(url, headers=get_ado_headers(), json=wiql, timeout=30)
        response.raise_for_status()
        
        work_item_refs = response.json().get("workItems", [])
        if not work_item_refs:
            return []
        
        ids = [str(ref["id"]) for ref in work_item_refs]
        ids_str = ",".join(ids)
        url = f"{org_url}/{project}/_apis/wit/workitems?ids={ids_str}&api-version=7.0"
        response = requests.get(url, headers=get_ado_headers(), timeout=30)
        response.raise_for_status()
        
        return response.json().get("value", [])
    except Exception as e:
        logger.error(f"Failed to get sprint work items: {e}")
        return []


def build_daily_sprint_report(sprint_path: str) -> Dict[str, Any]:
    """Build daily sprint report with today's changes."""
    work_items = get_sprint_work_items(sprint_path, include_completed=True)
    
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    
    today_changed = []
    today_completed = []
    today_started = []
    
    stats = {
        "total": len(work_items),
        "completed": 0,
        "in_progress": 0,
        "not_started": 0,
        "blocked": 0
    }
    
    for item in work_items:
        fields = item.get("fields", {})
        state = fields.get("System.State", "")
        changed_date_str = fields.get("System.ChangedDate", "")
        
        # Check if changed today
        if changed_date_str:
            changed_date = datetime.fromisoformat(changed_date_str.replace("Z", "+00:00"))
            if changed_date >= today:
                today_changed.append({
                    "id": item.get("id"),
                    "title": fields.get("System.Title", ""),
                    "state": state,
                    "assignee": fields.get("System.AssignedTo", {}).get("displayName", "Unassigned"),
                    "work_item_type": fields.get("System.WorkItemType", "")
                })
                
                if state in config.get_states_for_category('completed'):
                    today_completed.append(item)
                elif state in config.get_states_for_category('in_progress'):
                    today_started.append(item)
        
        # Update stats - use centralized config
        _cat = config.classify_state(state)
        if _cat == "Completed":
            stats["completed"] += 1
        elif _cat == "In Progress":
            stats["in_progress"] += 1
        elif _cat == "Blocked":
            stats["blocked"] += 1
        else:
            stats["not_started"] += 1
        
        if "blocked" in fields.get("System.Title", "").lower():
            stats["blocked"] += 1
    
    return {
        "date": today.isoformat(),
        "sprint_path": sprint_path,
        "stats": stats,
        "today_changed_count": len(today_changed),
        "today_completed_count": len(today_completed),
        "today_started_count": len(today_started),
        "today_changed": today_changed[:20],  # Limit to 20
        "completion_rate": round(stats["completed"] / stats["total"] * 100, 2) if stats["total"] > 0 else 0
    }


def build_weekly_summary(sprint_path: str) -> Dict[str, Any]:
    """Build weekly summary report."""
    work_items = get_sprint_work_items(sprint_path, include_completed=True)
    
    week_ago = datetime.now(timezone.utc) - timedelta(days=7)
    
    weekly_completed = []
    weekly_created = []
    weekly_changes = []
    
    assignee_stats = {}
    
    for item in work_items:
        fields = item.get("fields", {})
        state = fields.get("System.State", "")
        changed_date_str = fields.get("System.ChangedDate", "")
        created_date_str = fields.get("System.CreatedDate", "")
        assignee = fields.get("System.AssignedTo", {}).get("displayName", "Unassigned")
        
        # Track assignee stats
        if assignee not in assignee_stats:
            assignee_stats[assignee] = {"total": 0, "completed": 0, "in_progress": 0}
        
        assignee_stats[assignee]["total"] += 1
        
        _cat = config.classify_state(state)
        if _cat == "Completed":
            assignee_stats[assignee]["completed"] += 1
            
            # Check if completed this week
            if changed_date_str:
                changed_date = datetime.fromisoformat(changed_date_str.replace("Z", "+00:00"))
                if changed_date >= week_ago:
                    weekly_completed.append(item)
        elif _cat == "In Progress":
            assignee_stats[assignee]["in_progress"] += 1
        
        # Created this week
        if created_date_str:
            created_date = datetime.fromisoformat(created_date_str.replace("Z", "+00:00"))
            if created_date >= week_ago:
                weekly_created.append(item)
        
        # Changed this week
        if changed_date_str:
            changed_date = datetime.fromisoformat(changed_date_str.replace("Z", "+00:00"))
            if changed_date >= week_ago:
                weekly_changes.append(item)
    
    # Top performers
    top_performers = sorted(
        [{"assignee": k, **v} for k, v in assignee_stats.items()],
        key=lambda x: x["completed"],
        reverse=True
    )[:5]
    
    return {
        "week_start": week_ago.isoformat(),
        "week_end": datetime.now(timezone.utc).isoformat(),
        "sprint_path": sprint_path,
        "weekly_completed_count": len(weekly_completed),
        "weekly_created_count": len(weekly_created),
        "weekly_changes_count": len(weekly_changes),
        "top_performers": top_performers,
        "assignee_stats": assignee_stats,
        "total_items": len(work_items)
    }


def build_module_wise_bug_report() -> Dict[str, Any]:
    """Build module-wise bug report grouped by area path."""
    try:
        org_url = os.getenv("ADO_ORG_URL") or config.ado_org_url
        project = os.getenv("ADO_PROJECT") or config.ado_project
        
        # Get all active bugs
        wiql = {
            "query": """
                SELECT [System.Id], [System.Title], [System.State],
                       [System.AssignedTo], [System.AreaPath],
                       [System.Tags], [Microsoft.VSTS.Common.Priority],
                       [System.CreatedDate], [System.ChangedDate]
                FROM WorkItems
                WHERE [System.WorkItemType] = 'Bug'
                AND [System.State] NOT IN ({', '.join(f"'{s}'" for s in config.get_states_for_category('completed'))})
                ORDER BY [System.AreaPath], [Microsoft.VSTS.Common.Priority]
            """
        }
        
        url = f"{org_url}/{project}/_apis/wit/wiql?api-version=7.0"
        response = requests.post(url, headers=get_ado_headers(), json=wiql, timeout=30)
        response.raise_for_status()
        
        work_item_refs = response.json().get("workItems", [])
        if not work_item_refs:
            return {"modules": {}, "total_bugs": 0}
        
        # Get full details
        ids = [str(ref["id"]) for ref in work_item_refs]
        ids_str = ",".join(ids)
        url = f"{org_url}/{project}/_apis/wit/workitems?ids={ids_str}&api-version=7.0"
        response = requests.get(url, headers=get_ado_headers(), timeout=30)
        response.raise_for_status()
        
        bugs = response.json().get("value", [])
        
        # Group by module (area path)
        module_bugs = {}
        
        for bug in bugs:
            fields = bug.get("fields", {})
            area_path = fields.get("System.AreaPath", "Unknown")
            
            # Extract module name from area path (last segment)
            module = area_path.split("\\")[-1] if "\\" in area_path else area_path
            
            if module not in module_bugs:
                module_bugs[module] = {
                    "count": 0,
                    "critical": 0,
                    "high": 0,
                    "medium": 0,
                    "low": 0,
                    "bugs": []
                }
            
            priority = fields.get("Microsoft.VSTS.Common.Priority", 4)
            
            module_bugs[module]["count"] += 1
            
            if priority == 1:
                module_bugs[module]["critical"] += 1
            elif priority == 2:
                module_bugs[module]["high"] += 1
            elif priority == 3:
                module_bugs[module]["medium"] += 1
            else:
                module_bugs[module]["low"] += 1
            
            module_bugs[module]["bugs"].append({
                "id": bug.get("id"),
                "title": fields.get("System.Title", ""),
                "state": fields.get("System.State", ""),
                "priority": priority,
                "assignee": fields.get("System.AssignedTo", {}).get("displayName", "Unassigned")
            })
        
        # Sort modules by bug count
        sorted_modules = dict(sorted(module_bugs.items(), key=lambda x: x[1]["count"], reverse=True))
        
        return {
            "modules": sorted_modules,
            "total_bugs": len(bugs),
            "module_count": len(sorted_modules),
            "generated_at": datetime.now(timezone.utc).isoformat()
        }
    except Exception as e:
        logger.error(f"Failed to build module-wise bug report: {e}")
        return {"modules": {}, "total_bugs": 0, "error": str(e)}


def save_report(report_type: str, data: Dict[str, Any]) -> str:
    """Save report to outputs directory."""
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"{report_type}_{timestamp}.json"
    filepath = OUTPUTS_DIR / filename
    
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    
    logger.info(f"Report saved: {filepath}")
    return str(filepath)


def generate_daily_report_html(report_data: Dict[str, Any]) -> str:
    """Generate HTML format for daily report."""
    html = f"""
    <html>
    <head>
        <title>Daily Sprint Report</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 20px; }}
            h1 {{ color: #0078d4; }}
            .stats {{ background: #f3f2f1; padding: 15px; border-radius: 5px; margin: 10px 0; }}
            .item {{ border-left: 3px solid #0078d4; padding: 10px; margin: 5px 0; }}
            .completed {{ border-color: #107c10; }}
            .blocked {{ border-color: #d13438; }}
        </style>
    </head>
    <body>
        <h1>Daily Sprint Report</h1>
        <p><strong>Date:</strong> {report_data['date']}</p>
        <p><strong>Sprint:</strong> {report_data['sprint_path']}</p>
        
        <div class="stats">
            <h2>Sprint Statistics</h2>
            <p>Total Items: {report_data['stats']['total']}</p>
            <p>Completed: {report_data['stats']['completed']} ({report_data['completion_rate']}%)</p>
            <p>In Progress: {report_data['stats']['in_progress']}</p>
            <p>Not Started: {report_data['stats']['not_started']}</p>
            <p>Blocked: {report_data['stats']['blocked']}</p>
        </div>
        
        <h2>Today's Activity ({report_data['today_changed_count']} changes)</h2>
        <div>
    """
    
    for item in report_data['today_changed']:
        css_class = "completed" if config.classify_state(item['state']) == "Completed" else "item"
        html += f"""
            <div class="{css_class}">
                <strong>#{item['id']}</strong> - {item['title']}<br>
                <small>State: {item['state']} | Assignee: {item['assignee']}</small>
            </div>
        """
    
    html += """
        </div>
    </body>
    </html>
    """
    
    return html


if __name__ == "__main__":
    # Test report generation
    print("Testing report generation...")
    
    # Need sprint_path from config or env
    sprint_path = "ProjectName\\Sprint 1"  # Example
    
    daily = build_daily_sprint_report(sprint_path)
    print(f"\nDaily Report: {daily['today_changed_count']} changes today")
    
    weekly = build_weekly_summary(sprint_path)
    print(f"\nWeekly Summary: {weekly['weekly_completed_count']} completed this week")
    
    bugs = build_module_wise_bug_report()
    print(f"\nModule-wise Bugs: {bugs['total_bugs']} active bugs across {bugs.get('module_count', 0)} modules")
