"""
Feedback to Dev Scheduler Integration

Task handler for scheduled execution - using optimized sync service.
"""

import os
import logging
import asyncio
from typing import Dict, Any, Optional
import yaml

# Use the optimized v2 service with sync requests
from .service_v2 import FeedbackToDevService
from .service_v2 import run_wiql, fetch_workitems
from datetime import datetime, timezone, timedelta

logger = logging.getLogger("pm_agent.features.feedback_to_dev.scheduler")


async def run_from_config(config: Dict[str, Any]) -> None:
    """
    Run feedback to dev from scheduler config.
    
    This is the main entry point for scheduled execution.
    """
    try:
        logger.info("Starting feedback_to_dev scheduled run")

        options = config.get("options", {}) if config else {}
        logger.debug("Task options: %s", options)
        
        lookback_minutes = int(options.get("lookback_minutes", 1440))
        # Support historical_days = 'all' (use 365 as a reasonable max)
        hist_days_raw = options.get("historical_days", 180)
        if str(hist_days_raw).lower() == "all":
            historical_days = 365
        else:
            historical_days = int(hist_days_raw)
        similarity_threshold = float(options.get("similarity_threshold", 0.75))
        force_send = bool(options.get("force_send", False))
        
        # Initialize and run service
        service = FeedbackToDevService()
        
        if not service.org_url or not service.project or not service.pat:
            logger.error("Missing ADO configuration. Aborting run.")
            return

        # If configured to trigger only when a new client bug is created,
        # do a light-weight check for recent bugs and whether any were reported
        # by a client before running the heavy workflow.
        trigger_on_new = bool(options.get("trigger_on_new_bug_event", False))
        new_filter = options.get("new_bug_filter", {}) or {}
        if trigger_on_new:
            try:
                lookback_dt = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
                lookback_date = lookback_dt.strftime("%Y-%m-%d")
                wiql_new = f"""
                    SELECT [System.Id] FROM WorkItems
                    WHERE [System.TeamProject] = '{service.project}'
                      AND [System.WorkItemType] = 'Bug'
                      AND [System.CreatedDate] >= '{lookback_date}'
                    ORDER BY [System.CreatedDate] DESC
                """
                new_ids = run_wiql(service.org_url, wiql_new, service.pat, service.project)
                if not new_ids:
                    logger.info("No new bugs found in lookback window; skipping workflow run")
                    return

                # Fetch details and check reporter
                new_bugs = fetch_workitems(service.org_url, new_ids, service.pat)
                reported_by_filter = (new_filter.get("reported_by") or "").lower()
                found_client = False
                for nb in new_bugs:
                    fields = nb.get("fields", {})
                    created_by = fields.get("System.CreatedBy") or fields.get("System.CreatedByName") or ""
                    if isinstance(created_by, dict):
                        # Try common keys
                        created_by = (
                            created_by.get("displayName")
                            or created_by.get("uniqueName")
                            or created_by.get("mail")
                            or ""
                        )
                    if created_by and reported_by_filter:
                        if reported_by_filter in str(created_by).lower():
                            found_client = True
                            break

                if not found_client:
                    logger.info("No new client-reported bugs found; skipping workflow run")
                    return
            except Exception:
                logger.exception("Error while checking for new client bugs; proceeding to run workflow")

        # Run synchronously (the v2 service is sync, so wrap in executor)
        loop = asyncio.get_event_loop()
        result = await loop.run_in_executor(
            None,
            lambda: service.run_workflow(
                lookback_minutes=lookback_minutes,
                historical_days=historical_days,
                similarity_threshold=similarity_threshold,
                force_send=force_send,
            )
        )
        
        logger.info("Feedback to dev run completed: %s", result)
            
    except Exception as e:
        logger.exception("Exception during feedback_to_dev run: %s", e)


async def feedback_to_dev_scheduled_task(config: Optional[Dict[str, Any]] = None) -> None:
    """
    Scheduler task entry point (async).
    
    Called by the scheduler with optional config dict.
    """
    logger.info("Starting scheduled feedback_to_dev task")
    await run_from_config(config or {})
    logger.info("Completed scheduled feedback_to_dev task")


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
    task_cfg = {
        "options": {
            "lookback_minutes": 10080,  # 7 days for manual run
            "historical_days": 30,
            "is_test": True,
        },
        "reportEmailRecipients": cfg.get("reportEmailRecipients", [])
    }
    
    # Check for feedback_to_dev config in scheduler tasks
    if cfg.get("schedulerConfig"):
        tasks = cfg.get("schedulerConfig", {}).get("tasks", [])
        for t in tasks:
            if t.get("name") == "feedback_to_dev":
                task_cfg = {
                    "options": t.get("options", {}),
                    "reportEmailRecipients": cfg.get("reportEmailRecipients", [])
                }
                break
    
    asyncio.run(run_from_config(task_cfg))


async def run_once() -> None:
    """
    Run the workflow once immediately for testing.
    
    Uses a larger lookback window (7 days).
    """
    logger.info("Starting run_once (test mode)")
    
    service = FeedbackToDevService()
    config = service._load_config()
    
    feedback_cfg = config.get("feedback_agent", {})
    lookback_minutes = int(feedback_cfg.get("test_lookback_minutes", 10080))  # 7 days
    historical_days = int(feedback_cfg.get("historical_days", 30))
    
    try:
        result = await service.run_workflow(
            lookback_minutes=lookback_minutes,
            historical_days=historical_days,
            is_test=True
        )
        logger.info("run_once completed: %s", result)
    except Exception as e:
        logger.exception("Exception in run_once: %s", e)
    
    logger.info("Completed run_once")


if __name__ == "__main__":
    load_config_and_run()
