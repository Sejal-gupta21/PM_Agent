"""Backlog Health Logic - Check refined items vs capacity.

Implements Row 7 from spreadsheet:
1. Check refined items vs capacity
2. Define threshold for 'thin backlog'
3. Trigger PO warning mail
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from config import config

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[1]

# Backlog health thresholds
THIN_BACKLOG_THRESHOLD_DAYS = 14  # Less than 2 weeks of work = thin
HEALTHY_BACKLOG_DAYS = 21  # 3 weeks of refined work = healthy
ITEMS_PER_SPRINT = 20  # Average items per sprint


def get_ado_headers() -> Dict[str, str]:
    """Get ADO API headers with PAT authentication."""
    pat = os.getenv("ADO_PAT") or config.ado_pat
    import base64
    auth = base64.b64encode(f":{pat}".encode()).decode()
    return {
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/json"
    }


def get_backlog_items() -> List[Dict[str, Any]]:
    """Get all backlog items (not in current sprint)."""
    try:
        org_url = os.getenv("ADO_ORG_URL") or config.ado_org_url
        project = os.getenv("ADO_PROJECT") or config.ado_project
        
        # WIQL query to get backlog items
        _completed = config.get_states_for_category('completed')
        _completed_sql = ", ".join(f"'{s}'" for s in _completed)
        wiql = {
            "query": f"""
                SELECT [System.Id], [System.Title], [System.State], 
                       [System.AssignedTo], [System.WorkItemType],
                       [Microsoft.VSTS.Scheduling.StoryPoints],
                       [Microsoft.VSTS.Common.Priority],
                       [System.IterationPath], [System.CreatedDate],
                       [Microsoft.VSTS.Common.StateChangeDate],
                       [System.Tags]
                FROM WorkItems
                WHERE [System.WorkItemType] IN ('User Story', 'Bug', 'Task')
                AND [System.State] NOT IN ({_completed_sql})
                AND [System.IterationPath] = ''
                ORDER BY [Microsoft.VSTS.Common.Priority] ASC, [System.CreatedDate] ASC
            """
        }
        
        url = f"{org_url}/{project}/_apis/wit/wiql?api-version=7.0"
        response = requests.post(url, headers=get_ado_headers(), json=wiql, timeout=30)
        response.raise_for_status()
        
        work_item_refs = response.json().get("workItems", [])
        if not work_item_refs:
            return []
        
        # Get full details (batch by 200 for large backlogs)
        ids = [str(ref["id"]) for ref in work_item_refs]
        all_items = []
        
        for i in range(0, len(ids), 200):
            batch = ids[i:i+200]
            ids_str = ",".join(batch)
            url = f"{org_url}/{project}/_apis/wit/workitems?ids={ids_str}&api-version=7.0"
            response = requests.get(url, headers=get_ado_headers(), timeout=30)
            response.raise_for_status()
            all_items.extend(response.json().get("value", []))
        
        return all_items
    except Exception as e:
        logger.error(f"Failed to get backlog items: {e}")
        return []


def analyze_backlog_health(backlog_items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Analyze backlog health based on refined items and capacity."""
    total_items = len(backlog_items)
    
    refined_states = ["New", "Active", "Approved", "Ready"]
    refined_items = []
    unrefined_items = []
    
    total_story_points = 0
    refined_story_points = 0
    
    priority_distribution = {"1": 0, "2": 0, "3": 0, "4": 0}
    old_items = []  # Items older than 60 days
    
    for item in backlog_items:
        fields = item.get("fields", {})
        state = fields.get("System.State", "")
        item_id = item.get("id")
        title = fields.get("System.Title", "")
        
        # Check if refined (has story points or specific tags)
        story_points = fields.get("Microsoft.VSTS.Scheduling.StoryPoints", 0) or 0
        tags = fields.get("System.Tags", "")
        is_refined = story_points > 0 or "refined" in tags.lower()
        
        total_story_points += story_points
        
        if is_refined and state in refined_states:
            refined_items.append(item)
            refined_story_points += story_points
        else:
            unrefined_items.append(item)
        
        # Priority tracking
        priority = str(fields.get("Microsoft.VSTS.Common.Priority", "4"))
        priority_distribution[priority] = priority_distribution.get(priority, 0) + 1
        
        # Check age
        created_date = fields.get("System.CreatedDate")
        if created_date:
            try:
                created = datetime.fromisoformat(created_date.replace("Z", "+00:00"))
                age_days = (datetime.now(timezone.utc) - created).days
                if age_days > 60:
                    old_items.append({
                        "id": item_id,
                        "title": title,
                        "age_days": age_days,
                        "priority": priority
                    })
            except Exception:
                pass
    
    # Calculate backlog depth in sprints (assuming 30 story points per sprint)
    avg_story_points_per_sprint = 30
    refined_sprints = refined_story_points / avg_story_points_per_sprint if avg_story_points_per_sprint > 0 else 0
    refined_days = refined_sprints * 10  # 10 days per sprint
    
    # Health assessment
    is_thin = refined_days < THIN_BACKLOG_THRESHOLD_DAYS
    is_healthy = refined_days >= HEALTHY_BACKLOG_DAYS
    
    health_status = "THIN" if is_thin else ("HEALTHY" if is_healthy else "MODERATE")
    
    return {
        "total_items": total_items,
        "refined_count": len(refined_items),
        "unrefined_count": len(unrefined_items),
        "refined_percentage": round(len(refined_items) / total_items * 100, 2) if total_items > 0 else 0,
        "total_story_points": total_story_points,
        "refined_story_points": refined_story_points,
        "refined_sprints": round(refined_sprints, 1),
        "refined_days": round(refined_days, 1),
        "health_status": health_status,
        "priority_distribution": priority_distribution,
        "old_items_count": len(old_items),
        "old_items": old_items[:10],  # Top 10 oldest
        "needs_po_attention": is_thin or len(old_items) > 20
    }


def generate_backlog_health_report() -> str:
    """Generate human-readable backlog health report."""
    backlog_items = get_backlog_items()
    
    if not backlog_items:
        return "📋 Backlog is empty."
    
    analysis = analyze_backlog_health(backlog_items)
    
    health_emoji = {
        "THIN": "🔴",
        "MODERATE": "🟡",
        "HEALTHY": "🟢"
    }
    
    report = f"""{health_emoji.get(analysis['health_status'], '⚪')} **Backlog Health Report**

**Status: {analysis['health_status']}**

**Overview:**
- Total Backlog Items: {analysis['total_items']}
- Refined Items: {analysis['refined_count']} ({analysis['refined_percentage']}%)
- Unrefined Items: {analysis['unrefined_count']}

**Refined Work Capacity:**
- Story Points: {analysis['refined_story_points']} / {analysis['total_story_points']}
- Estimated Sprints: {analysis['refined_sprints']}
- Estimated Days: {analysis['refined_days']} days

**Priority Distribution:**
- P1 (Critical): {analysis['priority_distribution'].get('1', 0)}
- P2 (High): {analysis['priority_distribution'].get('2', 0)}
- P3 (Medium): {analysis['priority_distribution'].get('3', 0)}
- P4 (Low): {analysis['priority_distribution'].get('4', 0)}

"""
    
    if analysis['refined_days'] < THIN_BACKLOG_THRESHOLD_DAYS:
        report += f"\n⚠️ **THIN BACKLOG WARNING**: Only {analysis['refined_days']} days of refined work!\n"
        report += f"   Recommendation: Refine at least {HEALTHY_BACKLOG_DAYS - analysis['refined_days']:.0f} more days of work.\n"
    
    if analysis['old_items_count'] > 0:
        report += f"\n**⏳ Stale Items ({analysis['old_items_count']} older than 60 days):**\n"
        for item in analysis['old_items'][:5]:
            report += f"- #{item['id']}: {item['title']} ({item['age_days']} days old, P{item['priority']})\n"
    
    if analysis['needs_po_attention']:
        report += "\n⚠️ **PO ATTENTION REQUIRED** - Backlog needs grooming!\n"
    
    return report


def should_trigger_po_warning() -> Tuple[bool, str]:
    """Check if PO warning should be triggered based on backlog health."""
    backlog_items = get_backlog_items()
    
    if not backlog_items:
        return True, "Backlog is empty"
    
    analysis = analyze_backlog_health(backlog_items)
    
    reasons = []
    
    # Trigger conditions
    if analysis['health_status'] == "THIN":
        reasons.append(f"Thin backlog: only {analysis['refined_days']} days of refined work")
    
    if analysis['refined_percentage'] < 30:
        reasons.append(f"Low refinement rate: {analysis['refined_percentage']}%")
    
    if analysis['old_items_count'] > 20:
        reasons.append(f"{analysis['old_items_count']} items older than 60 days")
    
    # Check high-priority items without story points
    high_priority_unrefined = sum(
        1 for item in backlog_items 
        if item.get("fields", {}).get("Microsoft.VSTS.Common.Priority", 4) <= 2 
        and (item.get("fields", {}).get("Microsoft.VSTS.Scheduling.StoryPoints") or 0) == 0
    )
    
    if high_priority_unrefined > 5:
        reasons.append(f"{high_priority_unrefined} high-priority items need refinement")
    
    should_trigger = len(reasons) > 0
    reason_text = "; ".join(reasons) if reasons else "Backlog healthy"
    
    return should_trigger, reason_text


if __name__ == "__main__":
    # Test the module
    print(generate_backlog_health_report())
    should_warn, reason = should_trigger_po_warning()
    print(f"\nPO Warning: {should_warn} - {reason}")
