"""
Billing Deviation Tool Breakdown
Exposes 8 focused tools for LLM orchestration instead of one monolithic function.

This enables flexible workflows where the LLM can:
- Ask clarifying questions between steps
- Show intermediate results
- Handle errors granularly
- Compose different workflows based on user needs
"""

import logging
import re
from typing import Dict, List, Any, Optional
from datetime import datetime

logger = logging.getLogger(__name__)


# ============================================================================
# Phase 1: Query Understanding & Target Collection
# ============================================================================

def parse_billing_query(query: str) -> Dict[str, Any]:
    """
    Extract area path and month from user's natural language query.
    
    Args:
        query: User's natural language query
        
    Returns:
        Dictionary with extracted parameters:
        {
            "area_path": "xops 25" or null,
            "month": "current" or month name,
            "year": 2026,
            "has_area_path": boolean
        }
    """
    try:
        result = {
            "area_path": None,
            "month": "current",
            "year": datetime.now().year,
            "has_area_path": False
        }
        
        # Extract area path - look for common patterns
        # Pattern: "for <area>" or "in <area>" or area name after "deviation"
        area_patterns = [
            r"(?:for|in)\s+([a-zA-Z0-9\s\-_]+?)(?:\s+for|\s+in|\s+current|\s+this|\s+last|$)",
            r"deviation\s+report\s+(?:for|of)\s+([a-zA-Z0-9\s\-_]+?)(?:\s+for|\s+current|$)",
            r"billing.*?(?:for|in)\s+([a-zA-Z0-9\s\-_]+?)(?:\s+month|$)"
        ]
        
        for pattern in area_patterns:
            match = re.search(pattern, query, re.IGNORECASE)
            if match:
                area = match.group(1).strip()
                # Filter out common month/time words that shouldn't be area paths
                exclude_words = ['current', 'this', 'last', 'next', 'month', 'sprint', 
                               'year', 'week', 'day', 'today', 'yesterday', 'tomorrow',
                               'january', 'february', 'march', 'april', 'may', 'june',
                               'july', 'august', 'september', 'october', 'november', 'december']
                area_lower = area.lower().strip()
                
                # Skip if area is just a time keyword
                if area_lower in exclude_words:
                    continue
                    
                # Skip if area contains only time keywords (like "current month")
                area_words = area_lower.split()
                if all(word in exclude_words for word in area_words):
                    continue
                
                result["area_path"] = area
                result["has_area_path"] = True
                logger.info(f"Extracted area path: {area}")
                break
        
        # Extract month - look for month names or "current"/"this"
        months = ["january", "february", "march", "april", "may", "june", 
                  "july", "august", "september", "october", "november", "december"]
        
        query_lower = query.lower()
        for month in months:
            if month in query_lower:
                result["month"] = month.capitalize()
                logger.info(f"Extracted month: {month}")
                break
        
        # Check for "previous month" or "last month"
        if re.search(r"\b(previous|last|past)\s+month\b", query_lower):
            now = datetime.now()
            # Calculate previous month
            if now.month == 1:
                result["month"] = 12
                result["year"] = now.year - 1
            else:
                result["month"] = now.month - 1
                result["year"] = now.year
            logger.info(f"Extracted previous month: {result['month']}/{result['year']}")
        
        # Check for "current month" or "this month"
        elif re.search(r"\b(current|this)\s+month\b", query_lower):
            result["month"] = "current"
        
        # Extract year if present
        year_match = re.search(r"\b(20\d{2})\b", query)
        if year_match:
            result["year"] = int(year_match.group(1))
        
        logger.info(f"Parsed billing query: {result}")
        return result
        
    except Exception as e:
        logger.exception(f"Error parsing billing query: {e}")
        return {
            "area_path": None,
            "month": "current",
            "year": datetime.now().year,
            "has_area_path": False,
            "error": str(e)
        }


def prompt_for_target_hours(area_path: Optional[str] = None) -> Dict[str, Any]:
    """
    Determine target hours strategy based on whether area path is provided.
    
    Args:
        area_path: Area path from user query (or null)
        
    Returns:
        Dictionary with instructions:
        {
            "action": "ask_user" or "use_default",
            "target_hours": 4000 (if default),
            "message": "Prompt text for user",
            "needs_user_input": boolean
        }
    """
    try:
        if area_path:
            # User provided area path - MUST ask for target hours
            return {
                "action": "ask_user",
                "target_hours": None,
                "message": f"Please enter the Target Hours for area path '{area_path}'",
                "needs_user_input": True,
                "area_path": area_path
            }
        else:
            # No area path - use default 4000 hours
            return {
                "action": "use_default",
                "target_hours": 4000,
                "message": "Using default target of 4000 hours for all area paths",
                "needs_user_input": False,
                "area_path": None
            }
    except Exception as e:
        logger.exception(f"Error in prompt_for_target_hours: {e}")
        return {
            "action": "error",
            "error": str(e),
            "needs_user_input": False
        }


# ============================================================================
# Phase 2: Work Item Retrieval
# ============================================================================

def fetch_work_items_by_billing_date(
    month: Optional[int] = None,
    year: Optional[int] = None,
    area_path: Optional[str] = None,
    state: str = "Closed"
) -> Dict[str, Any]:
    """
    Fetch work items filtered by Estimated Billing Date field.
    
    CRITICAL: This uses the "Estimated Billing Date" field (not StateChangeDate)
    and only includes Closed items from the specified month.
    
    Args:
        month: Month number (1-12), defaults to current month
        year: Year (YYYY), defaults to current year
        area_path: Optional area path to filter by
        state: Work item state (default: Closed)
        
    Returns:
        Dictionary with:
        {
            "work_items": [...],
            "count": 142,
            "total_completed_work": 3850.5,
            "breakdown_by_area": {...},
            "month": 1,
            "year": 2026
        }
    """
    try:
        from datetime import datetime
        
        # Resolve month/year defaults
        now = datetime.now()
        
        if month is None or month == "current":
            month = now.month
        elif isinstance(month, str):
            # Handle month names if passed as string
            month_names = {
                'january': 1, 'february': 2, 'march': 3, 'april': 4,
                'may': 5, 'june': 6, 'july': 7, 'august': 8,
                'september': 9, 'october': 10, 'november': 11, 'december': 12,
                'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4,
                'jun': 6, 'jul': 7, 'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12
            }
            if month.lower() in month_names:
                month = month_names[month.lower()]
            else:
                try:
                    month = int(month)
                except ValueError:
                    month = now.month
                    
        if year is None:
            year = now.year
            
        # Use existing ADO fetcher
        from billing_deviation.ado_fetcher import ADOEffortFetcher
        
        fetcher = ADOEffortFetcher()
        
        # Fetch completed work items for the specified month
        # This already filters by Estimated Billing Date and Closed state
        work_items = fetcher.fetch_completed_work_items_current_month(
            area_paths=[area_path] if area_path else None,
            month=month,
            year=year
        )
        
        if not work_items:
            logger.warning(f"No work items found for month {month}/{year}, area={area_path}")
            return {
                "work_items": [],
                "count": 0,
                "total_completed_work": 0.0,
                "breakdown_by_area": {},
                "month": month or datetime.now().month,
                "year": year or datetime.now().year,
                "message": "No closed work items found for the specified period"
            }
        
        # Extract effort data
        effort_data = fetcher.extract_effort_data(work_items)
        
        total_completed = effort_data.get('total_completed_work', 0.0)
        by_area = effort_data.get('by_area', {})
        
        logger.info(f"Fetched {len(work_items)} work items, total completed work: {total_completed}")
        
        return {
            "work_items": work_items,
            "count": len(work_items),
            "total_completed_work": total_completed,
            "total_actual_hours": total_completed,  # Alias for compatibility
            "breakdown_by_area": by_area,
            "month": month or datetime.now().month,
            "year": year or datetime.now().year,
            "area_path": area_path
        }
        
    except Exception as e:
        logger.exception(f"Error fetching work items by billing date: {e}")
        return {
            "work_items": [],
            "count": 0,
            "total_completed_work": 0.0,
            "breakdown_by_area": {},
            "error": str(e)
        }


def get_area_paths_for_month(
    month: Optional[int] = None,
    year: Optional[int] = None
) -> Dict[str, Any]:
    """
    List available area paths that have closed work items in the specified month.
    Useful for validation and area path selection.
    
    Args:
        month: Month number (1-12), defaults to current month
        year: Year (YYYY), defaults to current year
        
    Returns:
        Dictionary with:
        {
            "area_paths": ["xops 25", "UI Team", "Backend"],
            "count": 3,
            "month": 1,
            "year": 2026
        }
    """
    try:
        from billing_deviation.ado_fetcher import ADOEffortFetcher
        
        fetcher = ADOEffortFetcher()
        
        # Fetch all work items for month (no area filter)
        work_items = fetcher.fetch_completed_work_items_current_month(
            area_paths=None,
            month=month,
            year=year
        )
        
        if not work_items:
            return {
                "area_paths": [],
                "count": 0,
                "month": month or datetime.now().month,
                "year": year or datetime.now().year
            }
        
        # Extract effort data and get area paths
        effort_data = fetcher.extract_effort_data(work_items)
        by_area = effort_data.get('by_area', {})
        area_paths = sorted(list(by_area.keys()))
        
        logger.info(f"Found {len(area_paths)} area paths for {month}/{year}")
        
        return {
            "area_paths": area_paths,
            "count": len(area_paths),
            "month": month or datetime.now().month,
            "year": year or datetime.now().year
        }
        
    except Exception as e:
        logger.exception(f"Error getting area paths: {e}")
        return {
            "area_paths": [],
            "count": 0,
            "error": str(e)
        }


# ============================================================================
# Phase 3: Deviation Calculation
# ============================================================================

def calculate_billing_deviation(
    target_hours: float,
    actual_hours: float,
    area_path: Optional[str] = None
) -> Dict[str, Any]:
    """
    Calculate deviation using the formula: Deviation = Target Hours - Actual Hours
    
    CRITICAL FORMULA:
    - Deviation = Target - Actual
    - Positive deviation = Under-billing (behind target, need more hours)
    - Negative deviation = Over-billing (exceeded target, too many hours)
    
    Args:
        target_hours: Target/planned hours
        actual_hours: Actual completed work hours
        area_path: Optional area path context
        
    Returns:
        Dictionary with:
        {
            "target_hours": 4000,
            "actual_hours": 3850.5,
            "deviation": 149.5,  # Positive = under-billing
            "deviation_percentage": 3.74,
            "status": "Under-billing" | "Over-billing" | "On Track",
            "message": "Human readable status",
            "is_critical": boolean
        }
    """
    try:
        # Calculate deviation: Target - Actual
        deviation = target_hours - actual_hours
        
        # Calculate percentage
        if target_hours > 0:
            deviation_percentage = (deviation / target_hours) * 100.0
        else:
            deviation_percentage = 0.0
        
        # Determine status based on absolute deviation percentage
        abs_dev_pct = abs(deviation_percentage)
        
        if abs_dev_pct <= 10.0:
            # Within 10% tolerance = On Track
            status = "On Track"
            is_critical = False
            message = f"Billing is on track (within 10% tolerance)"
        elif deviation > 0:
            # Positive = Under-billing (actual < target)
            is_critical = abs_dev_pct > 20.0
            status = "Under-billing"
            message = f"You are {abs(deviation):.1f} hours behind the target ({abs_dev_pct:.1f}% under-billing)"
        else:
            # Negative = Over-billing (actual > target)
            is_critical = abs_dev_pct > 20.0
            status = "Over-billing"
            message = f"You have exceeded the target by {abs(deviation):.1f} hours ({abs_dev_pct:.1f}% over-billing)"
        
        result = {
            "target_hours": target_hours,
            "actual_hours": actual_hours,
            "deviation": deviation,
            "deviation_percentage": deviation_percentage,
            "status": status,
            "message": message,
            "is_critical": is_critical,
            "area_path": area_path
        }
        
        logger.info(f"Calculated deviation: {result}")
        return result
        
    except Exception as e:
        logger.exception(f"Error calculating billing deviation: {e}")
        return {
            "target_hours": target_hours,
            "actual_hours": actual_hours,
            "error": str(e),
            "status": "Error"
        }


# ============================================================================
# Phase 4: Report Generation
# ============================================================================

def generate_billing_summary_text(
    deviation_result: Dict[str, Any],
    work_item_details: Optional[Dict[str, Any]] = None
) -> str:
    """
    Create text summary for chat display.
    
    Args:
        deviation_result: Result from calculate_billing_deviation
        work_item_details: Optional work item fetch results
        
    Returns:
        Formatted text string
    """
    try:
        lines = []
        lines.append("### 📊 Billing Deviation Report")
        lines.append("")
        
        # Period info
        if work_item_details:
            month = work_item_details.get('month', datetime.now().month)
            year = work_item_details.get('year', datetime.now().year)
            month_name = datetime(year, month, 1).strftime('%B')
            lines.append(f"- **Period:** {month_name} {year}")
            
            area_path = work_item_details.get('area_path')
            if area_path:
                lines.append(f"- **Area Path:** `{area_path}`")
            
            work_item_count = work_item_details.get('count', 0)
            lines.append(f"- **Work Items:** {work_item_count} closed items")
            lines.append("")
        
        # Deviation metrics
        target = deviation_result.get('target_hours', 0)
        actual = deviation_result.get('actual_hours', 0)
        deviation = deviation_result.get('deviation', 0)
        deviation_pct = deviation_result.get('deviation_percentage', 0)
        status = deviation_result.get('status', 'Unknown')
        
        # Status with emoji
        status_emoji = {
            "On Track": "✅",
            "Under-billing": "⚠️",
            "Over-billing": "🔴"
        }.get(status, "ℹ️")
        
        lines.append(f"#### Status: {status_emoji} {status}")
        
        # Message
        message = deviation_result.get('message', '')
        if message:
            lines.append(f"> {message}")
        lines.append("")

        lines.append("**Metrics:**")
        lines.append(f"- **Target Hours:** {target:,.0f}")
        lines.append(f"- **Actual Hours:** {actual:,.1f}")
        lines.append(f"- **Deviation:** {deviation:+,.1f} hours ({deviation_pct:+.1f}%)")
        lines.append("")
        
        # Area breakdown if available
        if work_item_details and work_item_details.get('breakdown_by_area'):
            lines.append("**Breakdown by Area:**")
            by_area = work_item_details['breakdown_by_area']
            for area, data in sorted(by_area.items(), key=lambda x: x[1].get('completed_work', 0), reverse=True):
                completed = data.get('completed_work', 0)
                count = data.get('count', 0)
                lines.append(f"- {area}: **{completed:.1f} hrs** ({count} items)")
        
        return "\n".join(lines)
        
    except Exception as e:
        logger.exception(f"Error generating billing summary text: {e}")
        return f"❌ Error generating summary: {str(e)}"


def generate_detailed_billing_report(
    deviation_result: Dict[str, Any],
    work_item_details: Optional[Dict[str, Any]] = None,
    format: str = "html"
) -> Dict[str, Any]:
    """
    Create detailed HTML or CSV report.
    
    Args:
        deviation_result: Result from calculate_billing_deviation
        work_item_details: Optional work item fetch results
        format: "html" or "csv"
        
    Returns:
        Dictionary with:
        {
            "format": "html" | "csv",
            "content": "...",
            "file_path": "/path/to/report.html"  (if saved)
        }
    """
    try:
        from billing_deviation.report_generator import BillingDeviationReporter
        
        reporter = BillingDeviationReporter()
        
        # Build analysis results structure expected by reporter
        analysis_results = {
            "total": {
                "target": deviation_result.get('target_hours', 0),
                "actual": deviation_result.get('actual_hours', 0),
                "deviation_abs": deviation_result.get('deviation', 0),
                "deviation_pct": deviation_result.get('deviation_percentage', 0),
                "status": deviation_result.get('status', 'Unknown')
            },
            "by_module": {},
            "by_user": {},
            "risks_recommendations": {
                "risks": [deviation_result.get('message', '')],
                "recommendations": []
            }
        }
        
        # Add area breakdown if available
        if work_item_details and work_item_details.get('breakdown_by_area'):
            by_area = work_item_details['breakdown_by_area']
            for area, data in by_area.items():
                analysis_results["by_module"][area] = {
                    "actual": data.get('completed_work', 0),
                    "target": 0,  # Not specified per area
                    "deviation_abs": 0,
                    "deviation_pct": 0,
                    "status": "N/A"
                }
        
        if format == "html":
            html_content = reporter.generate_html_report(analysis_results)
            
            # Optionally save to file
            from pathlib import Path
            from datetime import datetime
            
            output_dir = Path("outputs")
            output_dir.mkdir(exist_ok=True)
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            file_path = output_dir / f"billing_deviation_report_{timestamp}.html"
            
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(html_content)
            
            logger.info(f"Generated HTML report: {file_path}")
            
            return {
                "format": "html",
                "content": html_content,
                "file_path": str(file_path)
            }
        
        elif format == "csv":
            csv_path = reporter.generate_csv_details(analysis_results)
            
            with open(csv_path, 'r', encoding='utf-8') as f:
                csv_content = f.read()
            
            logger.info(f"Generated CSV report: {csv_path}")
            
            return {
                "format": "csv",
                "content": csv_content,
                "file_path": csv_path
            }
        
        else:
            return {
                "format": "error",
                "error": f"Unsupported format: {format}"
            }
        
    except Exception as e:
        logger.exception(f"Error generating detailed billing report: {e}")
        return {
            "format": "error",
            "error": str(e)
        }


def send_billing_report_email(
    recipient_email: str,
    text_summary: str,
    html_report: Optional[str] = None,
    csv_path: Optional[str] = None,
    subject: str = "Billing Deviation Report"
) -> Dict[str, Any]:
    """
    Email the billing report with attachments.
    
    Args:
        recipient_email: Email address to send to (validated against config)
        text_summary: Plain text summary
        html_report: Optional HTML report content
        csv_path: Optional CSV file path to attach
        subject: Email subject line
        
    Returns:
        Dictionary with:
        {
            "success": boolean,
            "message": "Status message",
            "action": "sent" | "validation_failed" | "error"
        }
    """
    try:
        from billing_deviation.emailer import BillingDeviationEmailer
        from billing_deviation.config_reader import BillingDeviationConfig
        
        config = BillingDeviationConfig()
        emailer = BillingDeviationEmailer(config=config)
        
        # Prepare attachments
        extra_attachments = []
        if csv_path:
            extra_attachments.append(csv_path)
        
        # Send email
        result = emailer.validate_and_send_report(
            recipient_email=recipient_email,
            text_summary=text_summary,
            html_report=html_report,
            subject=subject,
            extra_attachments=extra_attachments if extra_attachments else None
        )
        
        logger.info(f"Email send result: {result}")
        return result
        
    except Exception as e:
        logger.exception(f"Error sending billing report email: {e}")
        return {
            "success": False,
            "message": f"Error sending email: {str(e)}",
            "action": "error"
        }
