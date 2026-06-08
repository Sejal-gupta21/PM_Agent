"""
Scheduler task for Billing Deviation reports.

Provides `run_task_from_config(config)` which generates the report and
sends it to the recipients provided in `config['reportEmailRecipients']`.
"""
from __future__ import annotations
import logging
from typing import List, Dict, Any
from pathlib import Path
import asyncio
import os
import requests
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

from .billing_orchestrator import BillingDeviationOrchestrator


def _is_last_working_day_of_sprint() -> bool:
    """
    Check if today is the last working day of the current sprint.
    Returns True if today is the last working day (Monday-Friday) before sprint ends.
    """
    try:
        # Get ADO configuration from environment or config
        org_url = os.getenv("ADO_ORG_URL")
        project = os.getenv("ADO_PROJECT")
        team = os.getenv("ADO_TEAM", "")
        pat = os.getenv("ADO_PAT")
        
        # Fallback to config.yaml if env vars not set
        if not org_url or not project or not pat:
            try:
                from config import config as cfg
                org_url = org_url or cfg.ado_org_url
                project = project or cfg.ado_project
                team = team or cfg.ado_team or ""
                pat = pat or cfg.ado_pat
            except Exception as e:
                logger.debug(f"Could not load from config: {e}")
        
        if not all([org_url, project, pat]):
            logger.error("Missing ADO configuration for sprint check")
            return False
        
        # Fetch current iteration
        url = f"{org_url}/{project}/{team}/_apis/work/teamsettings/iterations?$timeframe=current&api-version=7.0"
        response = requests.get(url, auth=("", pat))
        
        if response.status_code != 200:
            logger.error(f"Failed to fetch current iteration: {response.status_code}")
            return False
        
        data = response.json()
        iterations = data.get("value", [])
        
        if not iterations:
            logger.warning("No current iteration found")
            return False
        
        current_iter = iterations[0]
        finish_date_str = current_iter.get("attributes", {}).get("finishDate")
        
        if not finish_date_str:
            logger.warning("Current iteration has no finish date")
            return False
        
        # Parse finish date
        finish_date = datetime.fromisoformat(finish_date_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        
        # Get today's date at start of day
        today = now.date()
        sprint_end_date = finish_date.date()
        
        # Check if we're within the last few days of sprint
        days_until_end = (sprint_end_date - today).days
        
        if days_until_end < 0:
            logger.info("Sprint has already ended")
            return False
        
        if days_until_end > 5:
            logger.info(f"Not last working day - {days_until_end} days until sprint end")
            return False
        
        # Find the last working day (Monday-Friday) before or on sprint end
        last_working_day = sprint_end_date
        while last_working_day.weekday() >= 5:  # 5=Saturday, 6=Sunday
            last_working_day = last_working_day.replace(day=last_working_day.day - 1)
        
        is_last_day = (today == last_working_day) and (today.weekday() < 5)
        
        if is_last_day:
            logger.info(f"Today ({today}) is the last working day of the sprint (ends {sprint_end_date})")
        else:
            logger.info(f"Today ({today}) is not the last working day. Last working day is {last_working_day}")
        
        return is_last_day
        
    except Exception as e:
        logger.exception(f"Error checking if last working day of sprint: {e}")
        return False


def _is_5_working_days_before_month_end() -> bool:
    """
    Check if today is exactly 5 working days before the end of the current month.
    Working days are Monday-Friday.
    
    Returns:
        True if today is the 5th working day before month end.
    """
    try:
        from calendar import monthrange
        
        now = datetime.now(timezone.utc)
        today = now.date()
        year = today.year
        month = today.month
        
        # Get the last day of the current month
        _, last_day_num = monthrange(year, month)
        month_end = today.replace(day=last_day_num)
        
        # Count working days backwards from month end to find the 5th working day
        working_days_count = 0
        check_date = month_end
        
        while working_days_count < 5:
            if check_date.weekday() < 5:  # Monday=0 to Friday=4
                working_days_count += 1
                if working_days_count == 5:
                    break
            check_date = check_date.replace(day=check_date.day - 1)
        
        # check_date is now the 5th working day before month end
        fifth_working_day_before_end = check_date
        
        is_target_day = (today == fifth_working_day_before_end)
        
        if is_target_day:
            logger.info(f"Today ({today}) is 5 working days before month end ({month_end})")
        else:
            logger.info(f"Today ({today}) is not 5 working days before month end. Target day is {fifth_working_day_before_end}")
        
        return is_target_day
        
    except Exception as e:
        logger.exception(f"Error checking if 5 working days before month end: {e}")
        return False


def run_task_from_config(config: Dict[str, Any]) -> None:
    """
    Generate billing deviation report and email to configured recipients.

    Args:
        config: Dict containing key 'reportEmailRecipients' -> List[str]
                and optional 'options' dict with 'sprint_last_working_day_only' flag
    """
    # Check trigger conditions from options
    options = config.get("options", {})
    sprint_last_day_only = options.get("sprint_last_working_day_only", False)
    month_end_trigger = options.get("month_end_5_working_days", True)  # Default enabled per requirements
    
    # Billing deviation triggers on:
    # 1. Last working day of every sprint (if sprint_last_working_day_only=True)
    # 2. 5 working days before month end (if month_end_5_working_days=True)
    # 3. Always (if both flags are False - for testing)
    should_run = False
    trigger_reason = ""
    
    # If both checks are disabled, run unconditionally (testing mode)
    if not sprint_last_day_only and not month_end_trigger:
        should_run = True
        trigger_reason = "testing mode (no date restrictions)"
    else:
        if sprint_last_day_only:
            if _is_last_working_day_of_sprint():
                should_run = True
                trigger_reason = "last working day of sprint"
        
        if month_end_trigger and not should_run:
            if _is_5_working_days_before_month_end():
                should_run = True
                trigger_reason = "5 working days before month end"
    
    if not should_run:
        logger.info("Skipping billing deviation report - not a scheduled trigger day")
        return
    
    logger.info(f"Billing deviation report triggered: {trigger_reason}")
    
    recipients: List[str] = config.get("reportEmailRecipients", []) or []

    if not recipients:
        logger.warning("No recipients provided for billing deviation scheduled task")
        return

    logger.info(f"Starting billing deviation scheduled report for {len(recipients)} recipient(s)")

    # Create orchestrator and generate report once (without sending email)
    # IMPORTANT: scheduler_mode=True enables:
    # - Target Hours = 4000 (hardcoded)
    # - Current month billing period
    # - Only Completed/Closed work items
    orchestrator = BillingDeviationOrchestrator()
    try:
        result = orchestrator.generate_billing_deviation_report(
            recipient_email=None,
            scheduler_mode=True  # Enable scheduler-specific logic
        )
    except Exception as e:
        logger.exception(f"Failed to generate billing deviation report: {e}")
        return

    if not result.get("success"):
        logger.error(f"Billing deviation generation failed: {result.get('message')}")
        return

    text_summary = result.get("text_summary")
    html_report = result.get("html_report")
    # The reporter may have created a CSV attachment in outputs; attempt to find it
    csv_path = None
    try:
        csv_path = result.get("analysis", {}).get("_csv_path")
    except Exception:
        csv_path = None

    # If the reporter didn't return CSV path in result, try to find by pattern
    if not csv_path:
        try:
            # The reporter generates a CSV in outputs/ with billing_deviation prefix
            out_dir = Path("outputs")
            if out_dir.exists():
                files = sorted(out_dir.glob("billing_deviation_*.csv"), reverse=True)
                if files:
                    csv_path = str(files[0])
        except Exception:
            csv_path = None

    # Send to each recipient individually using the BillingDeviationEmailer
    emailer = orchestrator.emailer
    for rcpt in recipients:
        try:
            logger.info(f"Sending billing deviation report to {rcpt}")
            extra_attachments = [csv_path] if csv_path else None
            res = emailer.validate_and_send_report(
                recipient_email=rcpt,
                text_summary=text_summary or "",
                html_report=html_report or None,
                subject="Billing Deviation Report",
                extra_attachments=extra_attachments,
            )
            logger.info(f"Email send result for {rcpt}: {res}")
        except Exception:
            logger.exception(f"Failed to send billing deviation report to {rcpt}")

    logger.info("Billing deviation scheduled task completed")
