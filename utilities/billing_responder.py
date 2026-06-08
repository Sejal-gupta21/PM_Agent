"""Billing Deviation Responder for chatbot.

Handles queries classified as "billing_deviation" skill:
- Show billing deviation status
- Find work items with effort variance
- Preview/generate billing reports
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

def parse_billing_query_with_llm(query: str) -> Dict[str, Any]:
    """Use LLM to understand the user's billing deviation query intent.
    
    Returns a structured dict with:
    - intent: str (e.g., "summary", "by_area", "by_user", "deviation_details")
    - filters: dict (area_path, user, iteration, threshold)
    - limit: int (how many items to show)
    - wants_details: bool
    - confidence: float
    """
    try:
        import google.generativeai as genai
        
        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            return _fallback_parse_billing_query(query)
        
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-1.5-flash")
        
        prompt = f"""Analyze this query about billing deviation/effort tracking and extract the user's intent.

Query: "{query}"

Respond with a JSON object containing:
{{
  "intent": "<one of: summary, by_area, by_user, critical_deviations, deviation_details, generate_report, compare_targets, current_month, this_sprint, general>",
  "filters": {{
    "area_path": "<area path filter or null>",
    "user": "<user name filter or null>",
    "iteration": "<iteration/sprint filter or null>",
    "threshold": <deviation % threshold to highlight, default 20>,
    "current_month_only": <true if user wants only current month data>
  }},
  "limit": <number of items to return, default 10>,
  "wants_details": <true if user wants detailed info, false for summary>,
  "wants_email": <true if user wants to send email report>
}}

Intent definitions:
- summary: Overall billing deviation summary
- by_area: Show deviations grouped by area path
- by_user: Show deviations grouped by user/developer
- critical_deviations: Show only critical/major deviations (>20%)
- deviation_details: Detailed breakdown of a specific deviation
- generate_report: Generate and optionally send billing deviation report
- compare_targets: Compare actual vs target hours
- current_month: Show billing data for current month only
- this_sprint: Show billing data for current sprint
- general: General billing deviation query

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
        logger.warning("LLM query parsing failed for billing deviation: %s", e)
        return _fallback_parse_billing_query(query)


def _fallback_parse_billing_query(query: str) -> Dict[str, Any]:
    """Fallback keyword-based query parsing for billing deviation."""
    query_lower = query.lower()
    
    result = {
        "intent": "general",
        "filters": {
            "area_path": None,
            "user": None,
            "iteration": None,
            "threshold": 20,
            "target_hours": None,
            "current_month_only": False,
        },
        "limit": 10,
        "wants_details": False,
        "wants_email": False,
        "confidence": 0.6,
    }
    
    # Detect intent from keywords
    if any(term in query_lower for term in ["by area", "area path", "grouped by area", "area wise"]):
        result["intent"] = "by_area"
    elif any(term in query_lower for term in ["by user", "by person", "by developer", "who"]):
        result["intent"] = "by_user"
    elif any(term in query_lower for term in ["critical", "major", "significant", "worst", "highest"]):
        result["intent"] = "critical_deviations"
    elif any(term in query_lower for term in ["report", "generate", "create"]):
        result["intent"] = "generate_report"
        if any(term in query_lower for term in ["send", "email", "mail"]):
            result["wants_email"] = True
    elif any(term in query_lower for term in ["compare", "target", "vs", "versus"]):
        result["intent"] = "compare_targets"
    elif any(term in query_lower for term in ["current month", "this month"]):
        result["intent"] = "current_month"
        result["filters"]["current_month_only"] = True
    elif any(term in query_lower for term in ["this sprint", "current sprint", "current iteration"]):
        result["intent"] = "this_sprint"
    elif any(term in query_lower for term in ["summary", "overview", "status"]):
        result["intent"] = "summary"
    else:
        result["intent"] = "summary"
    
    # Check for current month filter
    if "current month" in query_lower or "this month" in query_lower:
        result["filters"]["current_month_only"] = True
    
    # Extract area path - look for common patterns
    area_patterns = [
        r"for\s+([A-Z][A-Za-z0-9-]*)",  # "for XOPS"
        r"for\s+area\s+(?:path\s+)?([A-Z][A-Za-z0-9-]*)",  # "for area XOPS" or "for area path XOPS"
        r"in\s+([A-Z][A-Za-z0-9-]*)\s+(?:area|module|project)",  # "in XOPS area"
        r"area\s+(?:path\s+)?([A-Z][A-Za-z0-9-]*)",  # "area XOPS" or "area path XOPS"
        r"(?:FracPro-[A-Z]+)",  # Match FracPro-DEV, FracPro-OPS, etc.
        r"(?:^|\s)(XOPS|X-OPS)(?:\s|$)",  # Match XOPS standalone
    ]
    
    for pattern in area_patterns:
        match = re.search(pattern, query, re.IGNORECASE)
        if match:
            result["filters"]["area_path"] = match.group(1) if len(match.groups()) > 0 else match.group(0).strip()
            break
    
    # Extract target hours
    target_patterns = [
        r"target\s+(\d+)\s*(?:hours?)?",  # "target 2000 hours"
        r"with\s+(\d+)\s*(?:target\s+)?hours?",  # "with 2000 hours" or "with 2000 target hours"
        r"(\d+)\s*(?:target\s+)?hours?\s+target",  # "2000 hours target"
        r"(\d+)\s+target\s+hours?",  # "1500 target hours"
    ]
    
    for pattern in target_patterns:
        match = re.search(pattern, query_lower)
        if match:
            result["filters"]["target_hours"] = int(match.group(1))
            break
    
    # Parse limit
    limit_match = re.search(r"(top|first|show me|show)\s+(\d+)", query_lower)
    if limit_match:
        result["limit"] = int(limit_match.group(2))
    
    # Parse threshold
    threshold_match = re.search(r"(\d+)\s*%", query_lower)
    if threshold_match:
        result["filters"]["threshold"] = int(threshold_match.group(1))
    
    # Check for detail requests
    if any(term in query_lower for term in ["detail", "all info", "full", "complete"]):
        result["wants_details"] = True
    
    return result


# =============================================================================
# LIVE ADO DATA FETCHING
# =============================================================================

def fetch_billing_data_from_ado(
    iteration: Optional[str] = None,
    area_paths: Optional[List[str]] = None,
    filter_current_month: bool = False
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Fetch billing/effort data from ADO.
    
    Args:
        iteration: Iteration path filter (default: @CurrentIteration)
        area_paths: List of area paths to filter
        filter_current_month: Only include items with billing date in current month
        
    Returns:
        Tuple of (effort data dict, error message or None)
    """
    try:
        from billing_deviation.ado_fetcher import ADOEffortFetcher
        
        fetcher = ADOEffortFetcher()
        
        # Fetch work items
        work_items = fetcher.fetch_work_items_by_iteration(
            iteration or "@CurrentIteration",
            area_paths=area_paths,
            filter_current_month=filter_current_month
        )
        
        if not work_items:
            # Try fallback: recent work items
            work_items = fetcher.fetch_recent_work_items(top=100)
        
        if not work_items:
            return None, "No work items found"
        
        # Extract effort data
        effort_data = fetcher.extract_effort_data(work_items)
        
        return effort_data, None
        
    except ImportError as e:
        logger.warning("billing_deviation module not available: %s", e)
        return None, "Billing deviation module not available"
    except Exception as e:
        logger.exception("Failed to fetch billing data from ADO")
        return None, str(e)


def analyze_billing_deviations(
    effort_data: Dict[str, Any],
    target_hours: Optional[float] = None,
    threshold_pct: float = 20.0
) -> Dict[str, Any]:
    """Analyze billing deviations from effort data.
    
    Args:
        effort_data: Effort data from ADO fetcher
        target_hours: Target hours (if provided by user)
        threshold_pct: Deviation % to consider critical
        
    Returns:
        Analysis dict with deviations, summaries, etc.
    """
    analysis = {
        "total_actual_hours": 0,
        "total_target_hours": 0,
        "total_deviation_pct": 0,
        "by_area": {},
        "by_user": {},
        "critical_areas": [],
        "on_track_areas": [],
        "work_item_count": 0,
    }
    
    by_area = effort_data.get("by_area", {})
    by_user = effort_data.get("by_user", {})
    
    # Calculate totals
    total_actual = sum(data.get("completed_work", 0) for data in by_area.values())
    analysis["total_actual_hours"] = round(total_actual, 2)
    analysis["work_item_count"] = sum(data.get("count", 0) for data in by_area.values())
    
    # If target provided, calculate deviations
    if target_hours:
        analysis["total_target_hours"] = target_hours
        if target_hours > 0:
            deviation_pct = ((total_actual - target_hours) / target_hours) * 100
            analysis["total_deviation_pct"] = round(deviation_pct, 1)
    
    # Analyze by area
    num_areas = len(by_area) or 1
    per_area_target = (target_hours / num_areas) if target_hours else None
    
    for area, data in by_area.items():
        actual = data.get("completed_work", 0)
        remaining = data.get("remaining_work", 0)
        count = data.get("count", 0)
        
        area_info = {
            "actual_hours": round(actual, 2),
            "remaining_hours": round(remaining, 2),
            "work_item_count": count,
            "target_hours": per_area_target,
            "deviation_pct": 0,
            "status": "unknown",
        }
        
        if per_area_target and per_area_target > 0:
            dev_pct = ((actual - per_area_target) / per_area_target) * 100
            area_info["deviation_pct"] = round(dev_pct, 1)
            
            if abs(dev_pct) <= threshold_pct:
                area_info["status"] = "on_track"
                analysis["on_track_areas"].append(area)
            elif dev_pct > threshold_pct:
                area_info["status"] = "over_budget"
                analysis["critical_areas"].append({"area": area, "deviation": dev_pct})
            else:
                area_info["status"] = "under_budget"
        
        analysis["by_area"][area] = area_info
    
    # Analyze by user
    for user, data in by_user.items():
        analysis["by_user"][user] = {
            "actual_hours": round(data.get("completed_work", 0), 2),
            "remaining_hours": round(data.get("remaining_work", 0), 2),
            "work_item_count": data.get("count", 0),
        }
    
    # Sort critical areas by deviation
    analysis["critical_areas"] = sorted(
        analysis["critical_areas"],
        key=lambda x: abs(x["deviation"]),
        reverse=True
    )
    
    return analysis


# =============================================================================
# LOCAL DATA LOADING (from generated reports)
# =============================================================================

def load_latest_billing_report() -> Tuple[Optional[Path], Optional[pd.DataFrame]]:
    """Load the most recent billing deviation CSV report.
    
    Returns:
        Tuple of (file path, DataFrame) or (None, None) if not found.
    """
    patterns = [
        "billing_deviation_*.csv",
        "billing_report_*.csv",
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
        logger.warning("Failed to load billing data from %s: %s", latest, e)
        return None, None


# =============================================================================
# MAIN QUERY HANDLER
# =============================================================================

def handle_billing_query(
    query: str,
    intent: Dict[str, Any],
) -> Dict[str, Any]:
    """Handle a billing deviation query and return response.
    
    CRITICAL BEHAVIOR: Always shows input form first, NEVER auto-generates reports.
    Reports are only generated after explicit form submission.
    
    Args:
        query: Original user query
        intent: Intent classification result from semantic_matcher
        
    Returns:
        Response dict with:
        - skill_id: "billing_deviation"
        - summary_text: Human-readable response with form prompt
        - evidence_paths: Relevant file paths
        - data: Pre-filled form values extracted from query
    """
    # Parse query with LLM for dynamic understanding
    parsed = parse_billing_query_with_llm(query)
    filters = parsed.get("filters", {})
    
    # Build response - ALWAYS return form, never auto-generate
    response = {
        "skill_id": "billing_deviation",
        "confidence": intent.get("score", 0),
        "evidence_paths": [],
        "data": {
            "show_form": True,
            "pre_filled_values": {
                "area_path": filters.get("area_path", ""),
                "target_hours": filters.get("target_hours", ""),
            }
        },
    }
    
    # Build summary text with form prompt
    summary_parts = []
    summary_parts.append("[BILLING] **Billing Deviation Report**")
    summary_parts.append("")
    summary_parts.append("To generate the billing deviation report, please provide the following information:")
    summary_parts.append("")
    summary_parts.append("[INPUTS] **Required Inputs:**")
    summary_parts.append("   - **Area Path**: The area path to analyze (e.g., XOPS, FracPro-OPS)")
    summary_parts.append("   - **Target Hours**: Expected hours for comparison")
    summary_parts.append("")
    
    # Show pre-filled values if available
    if filters.get("area_path") or filters.get("target_hours"):
        summary_parts.append("[DETECTED] **Detected from your query:**")
        if filters.get("area_path"):
            summary_parts.append(f"   - Area Path: `{filters['area_path']}`")
        if filters.get("target_hours"):
            summary_parts.append(f"   - Target Hours: `{filters['target_hours']}`")
        summary_parts.append("")
        summary_parts.append("[EDIT] You can modify these values in the form below.")
    else:
        summary_parts.append("[INFO] Fill in the form below to generate your report.")
    
    summary_parts.append("")
    summary_parts.append("---")
    summary_parts.append("")
    summary_parts.append("[OUTPUT] **What you'll get:**")
    summary_parts.append("   - Total actual hours vs target hours")
    summary_parts.append("   - Breakdown by area path and module")
    summary_parts.append("   - Deviation percentage and health status")
    summary_parts.append("   - Top contributors and work item analysis")
    summary_parts.append("")
    summary_parts.append("[FORM] **Please submit the form below to generate the report.**")
    
    response["summary_text"] = "\n".join(summary_parts)
    return response


def _convert_df_to_effort_data(df: pd.DataFrame) -> Dict[str, Any]:
    """Convert a billing report DataFrame to effort data format."""
    effort_data = {
        "by_area": {},
        "by_user": {},
        "total": {
            "completed_work": 0,
            "remaining_work": 0,
            "count": 0,
        }
    }
    
    # Try to aggregate by area
    if "Area Path" in df.columns or "AreaPath" in df.columns:
        area_col = "Area Path" if "Area Path" in df.columns else "AreaPath"
        for area in df[area_col].unique():
            area_df = df[df[area_col] == area]
            completed = 0
            remaining = 0
            
            for col in ["Completed Work", "CompletedWork", "Actual Hours", "ActualHours"]:
                if col in df.columns:
                    completed = area_df[col].fillna(0).sum()
                    break
            
            for col in ["Remaining Work", "RemainingWork", "Remaining Hours"]:
                if col in df.columns:
                    remaining = area_df[col].fillna(0).sum()
                    break
            
            effort_data["by_area"][area] = {
                "completed_work": completed,
                "remaining_work": remaining,
                "count": len(area_df),
            }
    
    # Try to aggregate by user
    if "Assigned To" in df.columns or "AssignedTo" in df.columns:
        user_col = "Assigned To" if "Assigned To" in df.columns else "AssignedTo"
        for user in df[user_col].dropna().unique():
            user_df = df[df[user_col] == user]
            completed = 0
            remaining = 0
            
            for col in ["Completed Work", "CompletedWork", "Actual Hours", "ActualHours"]:
                if col in df.columns:
                    completed = user_df[col].fillna(0).sum()
                    break
            
            for col in ["Remaining Work", "RemainingWork", "Remaining Hours"]:
                if col in df.columns:
                    remaining = user_df[col].fillna(0).sum()
                    break
            
            effort_data["by_user"][user] = {
                "completed_work": completed,
                "remaining_work": remaining,
                "count": len(user_df),
            }
    
    return effort_data
