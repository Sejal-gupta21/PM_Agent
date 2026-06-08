"""Capacity Warning Logic - Enhanced with leave/meetings tracking.

Implements Row 8 from spreadsheet:
1. Pull individual capacity (leave/meetings)
2. Compute available hours
3. Trigger advanced off-track warning
"""
from __future__ import annotations

import logging
import os
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from config import config

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"

# Capacity constants
HOURS_PER_DAY = 8
SPRINT_DAYS = 10
DEFAULT_CAPACITY = HOURS_PER_DAY * SPRINT_DAYS  # 80 hours

# Warning thresholds
OVERLOAD_THRESHOLD = 1.0  # 100%+ capacity
CAPACITY_WARNING_THRESHOLD = 0.85  # 85%+ capacity


def get_ado_headers() -> Dict[str, str]:
    """Get ADO API headers with PAT authentication."""
    pat = os.getenv("ADO_PAT") or config.ado_pat
    import base64
    auth = base64.b64encode(f":{pat}".encode()).decode()
    return {
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/json"
    }


def get_team_capacity(iteration_id: str) -> List[Dict[str, Any]]:
    """Get team capacity from ADO for specific iteration."""
    try:
        org_url = os.getenv("ADO_ORG_URL") or config.ado_org_url
        project = os.getenv("ADO_PROJECT") or config.ado_project
        team = os.getenv("ADO_TEAM", "") or getattr(config, "ado_team", "")
        
        url = f"{org_url}/{project}/{team}/_apis/work/teamsettings/iterations/{iteration_id}/capacities?api-version=7.0"
        response = requests.get(url, headers=get_ado_headers(), timeout=30)
        response.raise_for_status()
        
        return response.json().get("value", [])
    except Exception as e:
        logger.error(f"Failed to get team capacity: {e}")
        return []


def get_developer_capacity_with_adjustments(email: str, iteration_id: str) -> Dict[str, Any]:
    """Get developer capacity with leave/meetings adjustments."""
    capacities = get_team_capacity(iteration_id)
    
    for cap in capacities:
        team_member = cap.get("teamMember", {})
        if team_member.get("uniqueName", "").lower() == email.lower():
            # Base capacity per day
            activities = cap.get("activities", [])
            capacity_per_day = 0
            for activity in activities:
                capacity_per_day += activity.get("capacityPerDay", 0)
            
            # Days off (leave)
            days_off = cap.get("daysOff", [])
            total_days_off = 0
            for day_off in days_off:
                start = datetime.fromisoformat(day_off.get("start", "").replace("Z", "+00:00"))
                end = datetime.fromisoformat(day_off.get("end", "").replace("Z", "+00:00"))
                total_days_off += (end - start).days + 1
            
            # Calculate adjusted capacity
            working_days = SPRINT_DAYS - total_days_off
            total_capacity = capacity_per_day * working_days
            
            return {
                "email": email,
                "name": team_member.get("displayName", email),
                "capacity_per_day": capacity_per_day,
                "working_days": working_days,
                "days_off": total_days_off,
                "total_capacity_hours": total_capacity,
                "activities": [a.get("name", "Development") for a in activities]
            }
    
    # Fallback if not found
    return {
        "email": email,
        "name": email.split("@")[0],
        "capacity_per_day": HOURS_PER_DAY,
        "working_days": SPRINT_DAYS,
        "days_off": 0,
        "total_capacity_hours": DEFAULT_CAPACITY,
        "activities": ["Development"]
    }


def get_developer_workload(email: str, sprint_path: str) -> Dict[str, Any]:
    """Get developer's current workload in sprint."""
    try:
        org_url = os.getenv("ADO_ORG_URL") or config.ado_org_url
        project = os.getenv("ADO_PROJECT") or config.ado_project
        
        # WIQL to get assigned items
        _completed = config.get_states_for_category('completed')
        _completed_sql = ", ".join(f"'{s}'" for s in _completed)
        wiql = {
            "query": f"""
                SELECT [System.Id], [System.Title], [System.State],
                       [Microsoft.VSTS.Scheduling.RemainingWork],
                       [Microsoft.VSTS.Scheduling.OriginalEstimate],
                       [Microsoft.VSTS.Scheduling.StoryPoints]
                FROM WorkItems
                WHERE [System.IterationPath] = '{sprint_path}'
                AND [System.AssignedTo] = '{email}'
                AND [System.State] NOT IN ({_completed_sql})
            """
        }
        
        url = f"{org_url}/{project}/_apis/wit/wiql?api-version=7.0"
        response = requests.post(url, headers=get_ado_headers(), json=wiql, timeout=30)
        response.raise_for_status()
        
        work_item_refs = response.json().get("workItems", [])
        if not work_item_refs:
            return {"assigned_hours": 0, "remaining_hours": 0, "task_count": 0, "tasks": []}
        
        # Get full details
        ids = [str(ref["id"]) for ref in work_item_refs]
        ids_str = ",".join(ids)
        url = f"{org_url}/{project}/_apis/wit/workitems?ids={ids_str}&api-version=7.0"
        response = requests.get(url, headers=get_ado_headers(), timeout=30)
        response.raise_for_status()
        
        items = response.json().get("value", [])
        
        total_remaining = 0
        total_original = 0
        tasks = []
        
        for item in items:
            fields = item.get("fields", {})
            remaining = fields.get("Microsoft.VSTS.Scheduling.RemainingWork", 0) or 0
            original = fields.get("Microsoft.VSTS.Scheduling.OriginalEstimate", 0) or 0
            story_points = fields.get("Microsoft.VSTS.Scheduling.StoryPoints", 0) or 0
            
            # Estimate hours if not set
            if remaining == 0 and story_points > 0:
                remaining = story_points * 4  # 1 story point = 4 hours estimate
            if original == 0 and remaining > 0:
                original = remaining
            
            total_remaining += remaining
            total_original += original
            
            tasks.append({
                "id": item.get("id"),
                "title": fields.get("System.Title", ""),
                "state": fields.get("System.State", ""),
                "remaining_hours": remaining,
                "original_hours": original
            })
        
        return {
            "assigned_hours": total_original,
            "remaining_hours": total_remaining,
            "task_count": len(tasks),
            "tasks": tasks
        }
    except Exception as e:
        logger.error(f"Failed to get developer workload: {e}")
        return {"assigned_hours": 0, "remaining_hours": 0, "task_count": 0, "tasks": []}


def analyze_team_capacity(sprint_path: str, iteration_id: str) -> Dict[str, Any]:
    """Analyze entire team capacity with warnings."""
    capacities = get_team_capacity(iteration_id)
    
    if not capacities:
        return {"error": "No capacity data available"}
    
    team_analysis = []
    overloaded_devs = []
    at_risk_devs = []
    
    for cap in capacities:
        team_member = cap.get("teamMember", {})
        email = team_member.get("uniqueName", "")
        
        if not email:
            continue
        
        # Get capacity with adjustments
        capacity_info = get_developer_capacity_with_adjustments(email, iteration_id)
        
        # Get workload
        workload = get_developer_workload(email, sprint_path)
        
        # Calculate utilization
        total_capacity = capacity_info["total_capacity_hours"]
        assigned_hours = workload["assigned_hours"]
        remaining_hours = workload["remaining_hours"]
        
        utilization = (remaining_hours / total_capacity) if total_capacity > 0 else 0
        
        # Determine status
        if utilization >= OVERLOAD_THRESHOLD:
            status = "OVERLOADED"
            overloaded_devs.append(email)
        elif utilization >= CAPACITY_WARNING_THRESHOLD:
            status = "AT_RISK"
            at_risk_devs.append(email)
        elif utilization < 0.5:
            status = "UNDERUTILIZED"
        else:
            status = "HEALTHY"
        
        team_analysis.append({
            "email": email,
            "name": capacity_info["name"],
            "total_capacity_hours": total_capacity,
            "working_days": capacity_info["working_days"],
            "days_off": capacity_info["days_off"],
            "assigned_hours": assigned_hours,
            "remaining_hours": remaining_hours,
            "available_hours": max(0, total_capacity - remaining_hours),
            "utilization": round(utilization, 2),
            "status": status,
            "task_count": workload["task_count"]
        })
    
    # Sort by utilization descending
    team_analysis.sort(key=lambda x: x["utilization"], reverse=True)
    
    return {
        "team_members": team_analysis,
        "total_members": len(team_analysis),
        "overloaded_count": len(overloaded_devs),
        "at_risk_count": len(at_risk_devs),
        "overloaded_devs": overloaded_devs,
        "at_risk_devs": at_risk_devs,
        "needs_rebalancing": len(overloaded_devs) > 0 or len(at_risk_devs) > 2
    }


def generate_capacity_warning_report(sprint_path: str, iteration_id: str) -> str:
    """Generate human-readable capacity warning report."""
    analysis = analyze_team_capacity(sprint_path, iteration_id)
    
    if "error" in analysis:
        return f"⚠️ {analysis['error']}"
    
    report = """⚡ **Team Capacity Report**\n\n"""
    
    if analysis["overloaded_count"] > 0:
        report += f"🔴 **{analysis['overloaded_count']} Developer(s) OVERLOADED:**\n"
        for member in analysis["team_members"]:
            if member["status"] == "OVERLOADED":
                report += f"- {member['name']}: {member['remaining_hours']:.0f}h / {member['total_capacity_hours']:.0f}h ({member['utilization']*100:.0f}% capacity)\n"
        report += "\n"
    
    if analysis["at_risk_count"] > 0:
        report += f"🟡 **{analysis['at_risk_count']} Developer(s) AT RISK:**\n"
        for member in analysis["team_members"]:
            if member["status"] == "AT_RISK":
                report += f"- {member['name']}: {member['remaining_hours']:.0f}h / {member['total_capacity_hours']:.0f}h ({member['utilization']*100:.0f}% capacity)\n"
                if member["days_off"] > 0:
                    report += f"  ({member['days_off']} days off)\n"
        report += "\n"
    
    # Show healthy/underutilized
    healthy = [m for m in analysis["team_members"] if m["status"] in ["HEALTHY", "UNDERUTILIZED"]]
    if healthy:
        report += "✅ **Available Capacity:**\n"
        for member in healthy[:5]:
            report += f"- {member['name']}: {member['available_hours']:.0f}h available ({member['utilization']*100:.0f}% utilized)\n"
    
    if analysis["needs_rebalancing"]:
        report += "\n⚠️ **ACTION REQUIRED**: Rebalance workload to prevent delays!\n"
    
    return report


def should_trigger_capacity_warning(sprint_path: str, iteration_id: str) -> Tuple[bool, str]:
    """Check if capacity warning should be triggered."""
    analysis = analyze_team_capacity(sprint_path, iteration_id)
    
    if "error" in analysis:
        return False, "No capacity data"
    
    reasons = []
    
    if analysis["overloaded_count"] > 0:
        reasons.append(f"{analysis['overloaded_count']} developer(s) overloaded")
    
    if analysis["at_risk_count"] > 2:
        reasons.append(f"{analysis['at_risk_count']} developer(s) at risk")
    
    # Check if any single dev is >120% capacity
    critical_overload = [m for m in analysis["team_members"] if m["utilization"] > 1.2]
    if critical_overload:
        reasons.append(f"{len(critical_overload)} developer(s) critically overloaded (>120%)")
    
    should_trigger = len(reasons) > 0
    reason_text = "; ".join(reasons) if reasons else "Team capacity healthy"
    
    return should_trigger, reason_text


def analyze_team_capacity_simple(capacity_data: Dict[str, Any], work_items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Simplified capacity analysis using MCP capacity data and work items.
    
    Args:
        capacity_data: Raw ADO capacity API response
        work_items: List of work items in the sprint
    
    Returns:
        Analysis dict with team members, utilization, and warnings
    """
    try:
        members = capacity_data.get("value", []) if isinstance(capacity_data, dict) else capacity_data
        if not members:
            return {
                "error": "No capacity data available",
                "total_members": 0,
                "team_members": [],
                "underutilized_developers": []
            }
        
        # Build workload map from work items
        workload_map = {}
        for item in work_items:
            if not isinstance(item, dict):
                continue
            
            fields = item.get("fields", {})
            assignee = fields.get("System.AssignedTo", {})
            email = assignee.get("uniqueName", "") if isinstance(assignee, dict) else ""
            
            if not email:
                continue
            
            # Extract effort fields
            remaining = fields.get("Microsoft.VSTS.Scheduling.RemainingWork", 0) or 0
            original = fields.get("Microsoft.VSTS.Scheduling.OriginalEstimate", 0) or 0
            story_points = fields.get("Microsoft.VSTS.Scheduling.StoryPoints", 0) or 0
            
            # Estimate hours if not set (1 story point = 4 hours)
            if remaining == 0 and story_points > 0:
                remaining = story_points * 4
            if original == 0 and remaining > 0:
                original = remaining
            
            if email not in workload_map:
                workload_map[email] = {"remaining": 0, "original": 0, "count": 0}
            
            workload_map[email]["remaining"] += remaining
            workload_map[email]["original"] += original
            workload_map[email]["count"] += 1
        
        # Analyze each team member
        team_analysis = []
        underutilized = []
        overloaded = []
        at_risk = []
        
        for member in members:
            team_member = member.get("teamMember", {})
            email = team_member.get("uniqueName", "")
            name = team_member.get("displayName", email)
            
            if not email:
                continue
            
            # Get capacity per day
            activities = member.get("activities", [])
            capacity_per_day = sum(a.get("capacityPerDay", 0) for a in activities)
            
            # Get days off
            days_off_list = member.get("daysOff", [])
            total_days_off = 0
            for day_off in days_off_list:
                try:
                    start = datetime.fromisoformat(day_off.get("start", "").replace("Z", "+00:00"))
                    end = datetime.fromisoformat(day_off.get("end", "").replace("Z", "+00:00"))
                    total_days_off += (end - start).days + 1
                except:
                    pass
            
            # Calculate total capacity
            working_days = max(0, SPRINT_DAYS - total_days_off)
            total_capacity = capacity_per_day * working_days
            
            # Get workload
            workload = workload_map.get(email, {"remaining": 0, "original": 0, "count": 0})
            remaining_hours = workload["remaining"]
            assigned_hours = workload["original"]
            task_count = workload["count"]
            
            # Calculate utilization and available hours
            utilization = (remaining_hours / total_capacity) if total_capacity > 0 else 0
            available_hours = max(0, total_capacity - remaining_hours)
            
            # Determine status
            if utilization >= OVERLOAD_THRESHOLD:
                status = "OVERLOADED"
                overloaded.append(email)
            elif utilization >= CAPACITY_WARNING_THRESHOLD:
                status = "AT_RISK"
                at_risk.append(email)
            elif utilization < 0.5:
                status = "UNDERUTILIZED"
                underutilized.append(name)
            else:
                status = "HEALTHY"
            
            team_analysis.append({
                "email": email,
                "name": name,
                "total_capacity_hours": round(total_capacity, 1),
                "working_days": working_days,
                "days_off": total_days_off,
                "assigned_hours": round(assigned_hours, 1),
                "remaining_hours": round(remaining_hours, 1),
                "available_hours": round(available_hours, 1),
                "utilization": round(utilization, 2),
                "utilization_percent": f"{round(utilization * 100)}%",
                "status": status,
                "task_count": task_count
            })
        
        # Sort by available hours descending (those with most capacity first)
        team_analysis.sort(key=lambda x: x["available_hours"], reverse=True)
        
        return {
            "team_members": team_analysis,
            "total_members": len(team_analysis),
            "overloaded_count": len(overloaded),
            "at_risk_count": len(at_risk),
            "underutilized_count": len(underutilized),
            "underutilized_developers": underutilized,
            "summary": {
                "message": f"Team capacity analysis: {len(underutilized)} underutilized, {len(at_risk)} at risk, {len(overloaded)} overloaded out of {len(team_analysis)} members",
                "needs_attention": len(overloaded) > 0 or len(at_risk) > 0
            }
        }
    except Exception as e:
        logger.error(f"Error in analyze_team_capacity_simple: {e}", exc_info=True)
        return {
            "error": f"Failed to analyze capacity: {str(e)}",
            "total_members": 0,
            "team_members": [],
            "underutilized_developers": []
        }


if __name__ == "__main__":
    # Test requires sprint_path and iteration_id
    print("Use from chatbot or pass sprint_path and iteration_id")
