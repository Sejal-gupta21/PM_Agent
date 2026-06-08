"""
Bug Area Highlight Scheduler Integration

Task handler for scheduled execution.
"""

import os
import logging
from typing import Dict, Any
import yaml
from config import config as app_config

from .service import BugAreaHighlightService
from utilities.mcp.pat import get_pat
import requests
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

API_VERSION = "7.0"

logger = logging.getLogger("pm_agent.features.bug_area_highlight.scheduler")


def _get_current_iteration() -> Dict[str, Any] | None:
    """Get current iteration info from Azure DevOps.
    
    Returns:
        Dict with 'id', 'name', 'start', 'end' keys, or None if not found.
    """
    from config import config as cfg
    
    org_url = cfg.ado_org_url
    project = cfg.ado_project
    team = cfg.ado_team or project + " Team"
    pat = get_pat()
    
    if not all([org_url, project, pat]):
        logger.warning("Missing ADO configuration for iteration lookup")
        return None
    
    try:
        # Get current iteration for the team
        url = f"{org_url}/{project}/{team}/_apis/work/teamsettings/iterations?$timeframe=current&api-version={API_VERSION}"
        auth = ("", pat)
        resp = requests.get(url, auth=auth, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        
        iterations = data.get("value", [])
        if not iterations:
            logger.warning("No current iteration found")
            return None
        
        iter_data = iterations[0]
        attributes = iter_data.get("attributes", {})
        
        return {
            "id": iter_data.get("id"),
            "name": iter_data.get("name"),
            "start": attributes.get("startDate"),
            "end": attributes.get("finishDate"),
        }
    except Exception as e:
        logger.error("Failed to get current iteration: %s", e)
        return None


def _is_first_working_day(start_raw: str, timezone_str: str = "UTC") -> bool:
    """Check if today is the first working day from a given start date.
    
    Args:
        start_raw: ISO date string for iteration start (e.g., "2025-01-06T00:00:00Z")
        timezone_str: Timezone name (e.g., "UTC", "America/New_York")
        
    Returns:
        True if today is the first working day (Mon-Fri) on or after start_raw
    """
    try:
        tz = ZoneInfo(timezone_str)
    except Exception:
        tz = ZoneInfo("UTC")
    
    today = datetime.now(tz).date()
    
    # Parse start date
    if isinstance(start_raw, str):
        # Handle ISO format with optional 'Z' or timezone
        start_raw = start_raw.replace("Z", "+00:00")
        try:
            start_dt = datetime.fromisoformat(start_raw)
            start_date = start_dt.date()
        except ValueError:
            # Try date-only format
            start_date = date.fromisoformat(start_raw[:10])
    else:
        start_date = start_raw
    
    # Find first working day (Monday=0, Sunday=6)
    first_working = start_date
    while first_working.weekday() >= 5:  # Saturday or Sunday
        first_working += timedelta(days=1)
    
    return today == first_working


def _is_first_working_day_of_current_iteration(timezone_str: str = "UTC") -> bool:
    """Check if today is the first working day of the current iteration.
    
    Args:
        timezone_str: Timezone name
        
    Returns:
        True if today is the first working day of current iteration
    """
    iter_info = _get_current_iteration()
    if not iter_info:
        logger.warning("Cannot determine current iteration; returning False")
        return False
    
    start_raw = iter_info.get("start")
    if not start_raw:
        logger.warning("Iteration has no start date; returning False")
        return False
    
    return _is_first_working_day(start_raw, timezone_str)


def run_from_config(config: Dict[str, Any]) -> None:
    """
    Run bug areas highlight from scheduler config.
    
    This is the main entry point for scheduled execution.
    """
    try:
        logger.info("Starting bug areas highlight detection run")

        options = config.get("options", {}) if config else {}
        logger.debug("Task options: %s", options)
        
        lookback_days = int(options.get("lookback_days", 30))
        recurrence_threshold = int(options.get("recurrence_threshold", 3))
        similarity_threshold = float(options.get("similarity_threshold", 0.75))
        no_area_label = str(options.get("no_area_label", "No Area"))
        test_mode = bool(options.get("test_mode", False))
        force_send = bool(options.get("force_send", False))
        
        logger.debug("test_mode=%s, force_send=%s", test_mode, force_send)

        # Get recipients from scheduler config or fall back to app config
        recipients = config.get("reportEmailRecipients") or app_config.report_email_recipients
        if not recipients:
            logger.error("No recipients configured; skipping email send")
            return
        if isinstance(recipients, str):
            recipients = [r.strip() for r in recipients.split(",") if r.strip()]

        if test_mode:
            logger.info("Test mode enabled: skipping ADO query and sending test report")
            # Send a test email
            from utilities.emailer import send_report_attachment
            html_body = "<html><body><h1>Bug Areas Highlight - Test Email</h1><p>This is a test notification.</p></body></html>"
            ok, resp = send_report_attachment(recipients, "Bug Areas Highlight — Test Mode", html_body, attachments=None)
            if ok:
                logger.info("Test email sent to %s", recipients)
            else:
                logger.error("Test email failed: %s", resp)
            return

        # Initialize and run service
        service = BugAreaHighlightService()

        if not service.org_url or not service.project or not service.pat:
            logger.error("Missing ADO configuration (ADO_ORG_URL, ADO_PROJECT, PAT). Aborting run.")
            return

        # If configured to only run on the first working day of the sprint,
        # check the current iteration start date from ADO and skip otherwise.
        sprint_only = bool(options.get("sprint_first_working_day_only", False))
        # timezone may be passed in the task config; fallback to env or UTC
        timezone = config.get("timezone") if isinstance(config, dict) else None
        timezone = timezone or os.getenv("SCHEDULER_TIMEZONE") or "UTC"
        if sprint_only:
            try:
                # get current iteration info (id + start date)
                iter_info = _get_current_iteration()
                if not iter_info:
                    logger.info("Could not identify current iteration; skipping run")
                    return

                start_raw = iter_info.get("start")
                iter_id = iter_info.get("id")
                if not start_raw or not iter_id:
                    logger.info("Iteration data incomplete; skipping run")
                    return

                # compute first working day
                if not _is_first_working_day(start_raw, timezone):
                    logger.info("Today is not the first working day of the current sprint; skipping run.")
                    return

                # ensure we run only once per iteration
                os.makedirs("outputs", exist_ok=True)
                state_file = os.path.join("outputs", "bug_areas_last_run.json")
                try:
                    import json
                    last_id = None
                    if os.path.exists(state_file):
                        with open(state_file, "r", encoding="utf-8") as sf:
                            data = json.load(sf)
                            last_id = data.get("last_iteration_id")
                    if str(last_id) == str(iter_id):
                        logger.info("Bug areas highlight already run for iteration %s; skipping", iter_id)
                        return
                except Exception:
                    logger.exception("Failed to read last-run state; proceeding with run")

            except Exception:
                logger.exception("Failed to determine sprint first working day; skipping run")
                return

        # Note: sprint-first-working-day enforcement already performed earlier
        # (ensures we run once per iteration). No duplicate check needed here.

        recurring, html_body, bugs = service.run_analysis(
            lookback_days=lookback_days,
            recurrence_threshold=recurrence_threshold,
            similarity_threshold=similarity_threshold,
            no_area_label=no_area_label,
        )
        
        if not recurring and not force_send:
            logger.info("No recurring bugs detected; skipping email send")
            return
        
        # Send report
        ok, resp = service.send_report(recurring, html_body, recipients)
        if ok:
            logger.info("Bug Areas Highlight email sent successfully")
            # record last run iteration id when sprint-only mode used
            try:
                if sprint_only and iter_info and iter_info.get("id"):
                    import json
                    state_file = os.path.join("outputs", "bug_areas_last_run.json")
                    tmp_file = state_file + ".tmp"
                    # Write atomically to avoid concurrent-write duplications
                    with open(tmp_file, "w", encoding="utf-8") as sf:
                        json.dump({"last_iteration_id": str(iter_info.get("id"))}, sf)
                    try:
                        os.replace(tmp_file, state_file)
                    except Exception:
                        # Fallback to rename if replace not available
                        try:
                            os.rename(tmp_file, state_file)
                        except Exception:
                            logger.exception("Failed to atomically write last-run state; wrote fallback")
                    logger.info("Recorded bug areas highlight run for iteration %s", iter_info.get("id"))
            except Exception:
                logger.exception("Failed to record last-run iteration state")
        else:
            logger.error("Error sending Bug Areas Highlight: %s", resp)
            
    except Exception as e:
        logger.exception("Exception during bug areas highlight run: %s", e)


def bug_areas_highlight_scheduled_task(config: Dict[str, Any] = None) -> None:
    """
    Scheduler task entry point.
    
    Called by the scheduler with optional config dict.
    """
    logger.info("Starting scheduled bug_areas_highlight task")
    run_from_config(config or {})
    logger.info("Completed scheduled bug_areas_highlight task")


def load_config_and_run() -> None:
    """
    Load config from config.yaml and run the task.
    
    Useful for manual/CLI execution.
    """
    cfg_path = os.path.join(os.getcwd(), "config.yaml")
    cfg = {}
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as fh:
                cfg = yaml.safe_load(fh) or {}
        except Exception:
            logger.exception("Failed to load config.yaml")
    
    # Find task config
    default_timezone = cfg.get("timezone") or os.getenv("SCHEDULER_TIMEZONE") or "UTC"
    task_cfg = {"options": {}, "reportEmailRecipients": cfg.get("reportEmailRecipients", []), "timezone": default_timezone}
    if cfg.get("schedulerConfig"):
        tasks = cfg.get("schedulerConfig", {}).get("tasks", [])
        for t in tasks:
            if t.get("name") == "bug_areas_highlight":
                tz = t.get("timezone") or cfg.get("timezone") or default_timezone
                task_cfg = {
                    "options": t.get("options", {}),
                    "reportEmailRecipients": cfg.get("reportEmailRecipients", []),
                    "timezone": tz,
                }
                break
    
    run_from_config(task_cfg)


if __name__ == "__main__":
    load_config_and_run()
