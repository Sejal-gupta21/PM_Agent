"""Offtrack item extraction utilities.

Provides functions to detect work items that require attention based on:
- UAT/PROD status is yellow or red
- Missing scheduled UAT or PROD deployment dates
- Overdue items (planned end date passed, state not closed)
- Blocked or failed items
- High-priority items not done
- Unassigned critical items

Each offtrack item gets a severity score for sorting and color-coding.
"""
from __future__ import annotations

import csv
import html as _html
from datetime import datetime, date
from typing import Any, Dict, List, Tuple, Optional
from pathlib import Path


# Severity score weights
SCORE_OVERDUE = 50
SCORE_BLOCKED = 40
SCORE_MISSING_DATES = 35
SCORE_HIGH_PRIORITY = 20
SCORE_UNASSIGNED_CRITICAL = 30
SCORE_STATUS_RED = 45
SCORE_STATUS_YELLOW = 25


def _get_field(record: Dict[str, Any], *keys: str) -> str:
    """Get field value with case-insensitive key matching."""
    for k in keys:
        v = record.get(k)
        if v is not None and str(v).strip() != "":
            return str(v).strip()
    # fallback: try matching keys case-insensitively
    for rk, rv in record.items():
        if rv is None:
            continue
        for k in keys:
            if rk.lower() == k.lower() and str(rv).strip() != "":
                return str(rv).strip()
    return ""


def _parse_date(s: str) -> Optional[date]:
    """Try to parse a date string in common formats."""
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y", "%d/%m/%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s.split("T")[0] if "T" in s else s, fmt.split("T")[0]).date()
        except Exception:
            pass
    return None


def _is_closed(state: str) -> bool:
    """Check if state indicates item is closed/done using centralized config."""
    from config import config as _cfg
    completed_states = _cfg.get_states_for_category('completed')
    return state.strip() in completed_states or state.lower().strip() in [s.lower() for s in completed_states]


def extract_offtrack_items(
    csv_path: str,
    max_items: int = 50,
    thresholds: Optional[Dict[str, Any]] = None
) -> Tuple[str, str, List[Dict[str, Any]]]:
    """
    Extract offtrack items from an iteration report CSV.
    
    Returns: (plain_text_summary, html_summary, items_list)
    
    items_list contains dicts with: id, title, assigned_to, state, priority,
    uat_status, prod_status, uat_sched, prod_sched, reasons, score
    """
    thresholds = thresholds or {}
    today = date.today()
    
    rows = []
    with open(csv_path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for r in reader:
            rows.append(r)
    
    offtrack_items = []
    
    for r in rows:
        score = 0
        reasons = []
        
        # Extract normalized fields
        wid = _get_field(r, "ID", "WI_ID", "S.No.", "S.No", "Work Item ID", "ID #") or "?"
        title = (_get_field(r, "Title") or "<no title>")[:200]
        assigned_to = _get_field(r, "Assigned To", "AssignedTo", "Owner") or ""
        state = _get_field(r, "State", "Status") or "Unknown"
        priority = _get_field(r, "Priority", "Prio") or ""
        
        uat_status = _get_field(r, "UAT Status", "UAT")
        prod_status = _get_field(r, "PROD Status", "PROD")
        uat_sched = _get_field(r, "UAT Scheduled Deployment Date", "UAT Scheduled Deployment", "UAT Scheduled")
        prod_sched = _get_field(r, "PROD Scheduled Deployment", "PROD Scheduled Deployment Date", "PROD Scheduled")
        
        planned_end = _get_field(r, "Planned End", "Target Date", "Planned End Date", "Iteration End")
        blocked = _get_field(r, "Blocked", "Is Blocked", "Blocked Reason")
        
        # Rule 1: UAT/PROD status is red
        if uat_status.lower() == "red":
            score += SCORE_STATUS_RED
            reasons.append("UAT status RED")
        elif uat_status.lower() == "yellow":
            score += SCORE_STATUS_YELLOW
            reasons.append("UAT status YELLOW")
            
        if prod_status.lower() == "red":
            score += SCORE_STATUS_RED
            reasons.append("PROD status RED")
        elif prod_status.lower() == "yellow":
            score += SCORE_STATUS_YELLOW
            reasons.append("PROD status YELLOW")
        
        # Rule 2: Missing scheduled dates (only if not closed)
        if not _is_closed(state):
            if not uat_sched:
                score += SCORE_MISSING_DATES
                reasons.append("Missing UAT scheduled date")
            if not prod_sched:
                score += SCORE_MISSING_DATES
                reasons.append("Missing PROD scheduled date")
        
        # Rule 3: Overdue
        if planned_end and not _is_closed(state):
            pe = _parse_date(planned_end)
            if pe and pe < today:
                score += SCORE_OVERDUE
                reasons.append(f"Overdue (planned: {pe})")
        
        # Rule 4: Blocked/Failed
        if blocked:
            blocked_lower = blocked.lower()
            if any(tok in blocked_lower for tok in ("block", "blocked", "fail", "failed", "imped")):
                score += SCORE_BLOCKED
                reasons.append(f"Blocked: {blocked[:50]}")
        
        # Check UAT/PROD status for blocked indicators
        for status_name, status_val in [("UAT", uat_status), ("PROD", prod_status)]:
            if status_val:
                status_lower = status_val.lower()
                if any(tok in status_lower for tok in ("block", "fail", "imped")):
                    score += SCORE_BLOCKED
                    reasons.append(f"{status_name} blocked/failed")
        
        # Rule 5: High priority not done
        if priority.lower() in ("p0", "p1", "high", "critical", "0", "1"):
            if not _is_closed(state):
                score += SCORE_HIGH_PRIORITY
                reasons.append("High priority item")
        
        # Rule 6: Unassigned critical
        if not assigned_to:
            if priority.lower() in ("p0", "p1", "high", "critical"):
                score += SCORE_UNASSIGNED_CRITICAL
                reasons.append("Unassigned critical item")
        
        if score > 0:
            offtrack_items.append({
                "id": wid,
                "title": title,
                "assigned_to": assigned_to,
                "state": state,
                "priority": priority,
                "uat_status": uat_status,
                "prod_status": prod_status,
                "uat_sched": uat_sched,
                "prod_sched": prod_sched,
                "reasons": reasons,
                "score": score,
            })
    
    # Sort by score descending
    offtrack_items.sort(key=lambda x: x["score"], reverse=True)
    top = offtrack_items[:max_items]
    
    # Build plain text summary
    plain_lines = []
    plain_lines.append(f"Offtrack items: {len(offtrack_items)} total, showing top {len(top)}")
    plain_lines.append("")
    for item in top:
        plain_lines.append(
            f"- [{item['id']}] {item['title'][:80]} | {item['assigned_to'] or 'UNASSIGNED'} | "
            f"Reasons: {', '.join(item['reasons'])}"
        )
    plain_text = "\n".join(plain_lines)
    
    # Build HTML summary with color-coded rows
    html = build_offtrack_html(top)
    
    return plain_text, html, offtrack_items


def _row_style(score: int) -> str:
    """Return inline CSS style based on severity score."""
    if score >= 80:
        return "background:#ffcccc;color:#800;"  # dark red
    if score >= 50:
        return "background:#ffdddd;color:#600;"  # red-ish
    if score >= 30:
        return "background:#fff0d9;color:#664400;"  # orange-ish
    if score >= 10:
        return "background:#fffde6;color:#666600;"  # yellow-ish
    return ""


def build_offtrack_html(items: List[Dict[str, Any]]) -> str:
    """Build color-coded HTML table for offtrack items."""
    if not items:
        return "<p>No offtrack items detected.</p>"
    
    rows = []
    for item in items:
        style = _row_style(item.get("score", 0))
        rows.append(
            f"<tr style='{style}'>"
            f"<td style='padding:6px;border:1px solid #ddd'>{_html.escape(str(item['id']))}</td>"
            f"<td style='padding:6px;border:1px solid #ddd'>{_html.escape(item['title'][:120])}</td>"
            f"<td style='padding:6px;border:1px solid #ddd'>{_html.escape(item['assigned_to'] or 'UNASSIGNED')}</td>"
            f"<td style='padding:6px;border:1px solid #ddd'>{_html.escape(item['state'])}</td>"
            f"<td style='padding:6px;border:1px solid #ddd'>{_html.escape(item['uat_status'] or item['uat_sched'] or '-')}</td>"
            f"<td style='padding:6px;border:1px solid #ddd'>{_html.escape(item['prod_status'] or item['prod_sched'] or '-')}</td>"
            f"<td style='padding:6px;border:1px solid #ddd'>{_html.escape(', '.join(item['reasons']))}</td>"
            "</tr>"
        )
    
    html = (
        "<div style='margin-top:12px;font-family:Arial,Helvetica,sans-serif;'>"
        "<h3>Offtrack items</h3>"
        "<p><strong>This requires attention</strong></p>"
        f"<p><strong>Total offtrack items:</strong> {len(items)}</p>"
        "<table style='border-collapse:collapse;width:100%;'>"
        "<thead style='background:#efefef;'>"
        "<tr>"
        "<th style='padding:6px;border:1px solid #ddd'>ID</th>"
        "<th style='padding:6px;border:1px solid #ddd'>Title</th>"
        "<th style='padding:6px;border:1px solid #ddd'>Owner</th>"
        "<th style='padding:6px;border:1px solid #ddd'>State</th>"
        "<th style='padding:6px;border:1px solid #ddd'>UAT</th>"
        "<th style='padding:6px;border:1px solid #ddd'>PROD</th>"
        "<th style='padding:6px;border:1px solid #ddd'>Reasons</th>"
        "</tr>"
        "</thead>"
        "<tbody>"
        + "".join(rows) +
        "</tbody>"
        "</table>"
        "</div>"
    )
    return html
