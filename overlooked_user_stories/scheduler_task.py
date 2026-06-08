"""
Scheduler task for sending overlooked user stories reports automatically.

This module provides a task function that can be registered with the
project's scheduler to send overlooked user stories reports on a schedule.
"""
import os
import sys
import logging
import subprocess
import requests
from pathlib import Path
from typing import List
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


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


def run_task_from_config(config: dict) -> None:
    """
    Run the overlooked user stories report and send to configured recipients.
    
    Args:
        config: Task configuration dict with 'reportEmailRecipients' key
                and optional 'options' dict with 'sprint_last_working_day_only' flag
    """
    # Check if task should only run on last working day of sprint
    options = config.get("options", {})
    sprint_last_day_only = options.get("sprint_last_working_day_only", False)
    
    if sprint_last_day_only:
        if not _is_last_working_day_of_sprint():
            logger.info("Skipping overlooked user stories report - not the last working day of sprint")
            return
    
    recipients = config.get("reportEmailRecipients", [])
    
    if not recipients:
        logger.warning("No recipients configured for overlooked user stories report")
        return
    
    logger.info(f"Starting overlooked user stories scheduled report for {len(recipients)} recipient(s)")
    
    # Get the script path relative to the project root
    script_path = Path(__file__).resolve().parent / "overlooked_stories_reminder.py"
    
    if not script_path.exists():
        logger.error(f"Overlooked stories script not found at: {script_path}")
        return
    
    # Send consolidated report to all configured recipients
    # The script reads from config.yaml
    consolidated_emails = ",".join(recipients)
    
    try:
        # Note: The script will read recipients from config, this is just for logging
        logger.info(f"Running overlooked stories script with recipients: {consolidated_emails}")
        
        proc = subprocess.run(
            [sys.executable, str(script_path)],
            capture_output=True,
            text=True,
            timeout=600
        )
        
        # Log output for debugging
        if proc.stdout:
            logger.info(f"Script output: {proc.stdout[:500]}")
        if proc.stderr:
            logger.warning(f"Script stderr: {proc.stderr[:500]}")
        
        if proc.returncode == 0:
            logger.info("Overlooked user stories report completed successfully")
        else:
            logger.error(f"Script failed with exit code {proc.returncode}")
            
    except subprocess.TimeoutExpired:
        logger.error("Overlooked stories script timed out after 600 seconds")
    except Exception as e:
        logger.exception(f"Failed to run overlooked stories report: {e}")
