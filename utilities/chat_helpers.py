"""
Chat Helpers - Extracted utilities from chat_ai.py.

This module provides reusable helper functions for the Streamlit chat UI:
- Response rendering
- MCP output parsing
- Email extraction
- Work item ID extraction
"""

import os
import re
import json
import logging
from typing import Dict, Any, Optional, List, Tuple
from pathlib import Path
import pandas as pd

logger = logging.getLogger("chat_helpers")


def extract_email_recipients(text: str) -> List[str]:
    """
    Extract email addresses from text.
    
    Args:
        text: User input text
        
    Returns:
        List of email addresses found
    """
    pattern = r"[\w.+-]+@[\w-]+\.[\w.-]+"
    return re.findall(pattern, text)


def extract_work_item_id(text: str) -> Optional[int]:
    """
    Extract work item ID from text.
    
    Supports formats:
    - Bare number: 12345
    - Hash prefix: #12345
    - With label: WI-12345, Bug-12345
    
    Args:
        text: User input text
        
    Returns:
        Work item ID as int, or None
    """
    # Pattern 1: WI/Bug/Story prefix
    match = re.search(r'(?:WI|Bug|Story|Task)[-#]?\s*(\d{4,6})', text, re.IGNORECASE)
    if match:
        return int(match.group(1))
    
    # Pattern 2: Hash prefix
    match = re.search(r'#(\d{4,6})', text)
    if match:
        return int(match.group(1))
    
    # Pattern 3: Standalone number (4-6 digits)
    match = re.search(r'(?:^|[^\d])(\d{4,6})(?:[^\d]|$)', text)
    if match:
        return int(match.group(1))
    
    return None


def parse_mcp_response(response: Any) -> Tuple[Optional[Dict], Optional[str]]:
    """
    Parse MCP response into structured dict.
    
    Args:
        response: Raw MCP response (str, dict, or list)
        
    Returns:
        Tuple of (parsed_dict, error_message)
    """
    if response is None:
        return None, "No response"
    
    if isinstance(response, dict):
        return response, None
    
    if isinstance(response, list):
        return {"items": response, "count": len(response)}, None
    
    if isinstance(response, str):
        text = response.strip()
        if not text or text.lower() == "null":
            return None, "Empty response"
        
        try:
            parsed = json.loads(text)
            return parsed, None
        except json.JSONDecodeError as e:
            return None, f"JSON parse error: {e}"
    
    return None, f"Unknown response type: {type(response)}"


def format_work_items_table(items: List[Dict]) -> pd.DataFrame:
    """
    Format work items into a pandas DataFrame for display.
    
    Args:
        items: List of work item dicts
        
    Returns:
        DataFrame with standardized columns
    """
    rows = []
    for item in items:
        # Handle nested fields structure
        fields = item.get("fields", item)
        if isinstance(fields, dict):
            row = {
                "id": fields.get("system.id") or fields.get("System.Id") or item.get("id"),
                "title": fields.get("system.title") or fields.get("System.Title") or fields.get("title", ""),
                "state": fields.get("system.state") or fields.get("System.State") or "",
                "assignedTo": fields.get("system.assignedto") or fields.get("System.AssignedTo") or "",
                "type": fields.get("system.workitemtype") or fields.get("System.WorkItemType") or ""
            }
        else:
            row = {"id": item.get("id"), "title": str(item)}
        rows.append(row)
    
    return pd.DataFrame(rows)


def format_time_logs_table(time_logs: List[Dict]) -> pd.DataFrame:
    """
    Format time logs into a pandas DataFrame.
    
    Args:
        time_logs: List of time log entries
        
    Returns:
        DataFrame with Date, User, Hours, Comment columns
    """
    rows = []
    for entry in time_logs:
        rows.append({
            "Date": entry.get("date") or entry.get("loggedDate") or entry.get("timestamp") or "",
            "User": entry.get("user") or entry.get("author") or entry.get("displayName") or "",
            "Hours": entry.get("hours") or entry.get("time") or entry.get("spent") or "",
            "Comment": entry.get("comment") or entry.get("notes") or entry.get("description") or ""
        })
    return pd.DataFrame(rows)


def format_deployments_table(deployments: Any) -> pd.DataFrame:
    """
    Format deployments into a pandas DataFrame.
    
    Args:
        deployments: Dict or list of deployment info
        
    Returns:
        DataFrame with Environment, ScheduledUTC columns
    """
    rows = []
    if isinstance(deployments, dict):
        for env, date in deployments.items():
            rows.append({"Environment": str(env), "ScheduledUTC": date})
    elif isinstance(deployments, list):
        for item in deployments:
            if isinstance(item, dict):
                env = item.get("Environment") or item.get("environment") or item.get("env")
                date = item.get("ScheduledUTC") or item.get("scheduled") or item.get("date")
                rows.append({"Environment": env, "ScheduledUTC": date})
    return pd.DataFrame(rows)


def detect_query_intent(query: str) -> Dict[str, bool]:
    """
    Detect intent categories from user query.
    
    Args:
        query: User query text
        
    Returns:
        Dict of intent flags
    """
    q = query.lower()
    
    time_log_phrases = [
        "time log", "time spent", "hours spent", "time tracking",
        "work log", "how much time", "extension entries", "time entry",
        "time entries", "logged time", "tracked time"
    ]
    
    deployment_phrases = [
        "deployment", "deployment schedule", "deployment dates", "scheduled",
        "qa", "uat", "pre-prod", "pre prod", "prod", "deploy"
    ]
    
    bug_analysis_phrases = [
        "recurring bug", "bug pattern", "bug area", "bug highlight",
        "analyze bug", "bug analysis", "similar bug"
    ]
    
    overlooked_phrases = [
        "overlooked", "forgotten", "stale", "neglected"
    ]
    
    report_phrases = [
        "iteration report", "sprint report", "generate report"
    ]
    
    return {
        "time_log": any(p in q for p in time_log_phrases),
        "deployment": any(p in q for p in deployment_phrases),
        "bug_analysis": any(p in q for p in bug_analysis_phrases),
        "overlooked": any(p in q for p in overlooked_phrases),
        "report": any(p in q for p in report_phrases),
        "email": "email" in q or "send" in q,
        "area_paths": "area path" in q or "area paths" in q
    }


def build_summary_message(result: Dict[str, Any], skill: str) -> str:
    """
    Build a human-readable summary from skill result.
    
    Args:
        result: Skill result dict
        skill: Skill name
        
    Returns:
        Summary message string
    """
    if not result:
        return "No result available."
    
    if isinstance(result, str):
        return result
    
    success = result.get("success", True)
    
    if not success:
        error = result.get("error", "Unknown error")
        return f"❌ {skill} failed: {error}"
    
    inner = result.get("result", result)
    
    # Bug areas highlight
    if skill == "bug_areas_highlight":
        count = inner.get("count", 0)
        recurring = inner.get("recurring_count", 0)
        email_sent = inner.get("email_sent", False)
        preview = inner.get("preview_path", "")
        
        msg = f"✅ Bug Areas Analysis Complete\n"
        msg += f"• Analyzed {count} bugs\n"
        msg += f"• Found {recurring} recurring patterns\n"
        if email_sent:
            recipients = inner.get("recipients", [])
            msg += f"• Email sent to: {', '.join(recipients)}\n"
        if preview:
            msg += f"• Preview: {preview}"
        return msg
    
    # Overlooked stories
    if skill == "overlooked_stories":
        message = inner.get("message", "Completed")
        email_status = inner.get("email_status", "")
        msg = f"✅ {message}"
        if email_status:
            msg += f"\n{email_status}"
        return msg
    
    # Iteration report
    if skill == "iteration_report":
        total = inner.get("total_items", 0)
        filtered = inner.get("filtered_items", 0)
        csv_file = inner.get("csv_file", "")
        msg = f"✅ Iteration Report Generated\n"
        msg += f"• Total items: {total}\n"
        msg += f"• Filtered items: {filtered}\n"
        if csv_file:
            msg += f"• File: {csv_file}"
        return msg
    
    # Generic
    if isinstance(inner, dict):
        return json.dumps(inner, indent=2)[:1000]
    
    return str(inner)[:500]


def save_preview_html(html_content: str, filename: str = "preview.html") -> Optional[str]:
    """
    Save HTML content to preview file.
    
    Args:
        html_content: HTML string
        filename: Output filename
        
    Returns:
        Full path to saved file, or None on error
    """
    try:
        os.makedirs("logs", exist_ok=True)
        path = os.path.join("logs", filename)
        with open(path, "w", encoding="utf-8") as f:
            f.write(html_content)
        return path
    except Exception as e:
        logger.error(f"Failed to save preview: {e}")
        return None


# ============================================================================
# ORCHESTRATOR INTEGRATION
# ============================================================================

async def process_via_orchestrator(query: str, session_id: str = "default") -> Dict[str, Any]:
    """
    Process a query through the Orchestrator.
    
    This is the main entry point for chat_ai to use the orchestrator.
    The orchestrator handles:
    - Routing to appropriate agent (PM Agent or PM Skills Agent)
    - Invoking LLM planner when confidence < threshold
    - Executing hybrid flows
    
    Args:
        query: User query
        session_id: Session identifier
        
    Returns:
        Response dict with content and metadata
    """
    try:
        from orchestrator.router import get_orchestrator
        
        orchestrator = get_orchestrator()
        result = await orchestrator.process(query, session_id)
        
        return result
        
    except Exception as e:
        logger.exception(f"Orchestrator error: {e}")
        return {
            "is_task_complete": True,
            "content": f"Error processing query: {e}",
            "error": str(e)
        }


def process_query_sync(query: str, session_id: str = "default") -> Dict[str, Any]:
    """
    Synchronous wrapper for process_via_orchestrator.
    
    For use in Streamlit which doesn't play well with async.
    
    Args:
        query: User query
        session_id: Session identifier
        
    Returns:
        Response dict
    """
    import asyncio
    
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    
    return loop.run_until_complete(process_via_orchestrator(query, session_id))
