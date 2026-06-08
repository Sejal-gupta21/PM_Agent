#!/usr/bin/env python3
"""Start the Scheduler and register the daily iteration-report email job.

Run this in the project's venv (or via a process manager)

    python3 scripts/start_scheduler.py

The job will:
 - run daily at 10:00 (cron: "0 10 * * *") in the configured timezone
 - generate the iteration report using `generate_report`
 - produce an HTML summary listing items that require attention (UAT/PROD status)
 - send an email with the summary and attach the CSV (and HTML) outputs

Configuration via environment (.env supported):
 - ADO_ORG_URL, ADO_PROJECT, ADO_TEAM, ADO_PAT (or ADO_MCP_AUTH_TOKEN)
 - REPORT_RECIPIENTS (comma-separated) or DEFAULT_PM_EMAIL or PM_EMAIL
 - SCHEDULER_TIMEZONE (e.g. "UTC" or "Asia/Kolkata") defaults to UTC
 - REPORT_SEND_ATTACH_HTML (true/1 to attach generated HTML as well)
"""

import os
import sys
import asyncio
import logging
import atexit
import signal
from pathlib import Path
from typing import List
from utilities.scheduler.scheduler import Scheduler
import yaml
from config import config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("pm_agent.start_scheduler")

# PID lock file to prevent multiple scheduler instances
REPO_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = REPO_ROOT / "logs"
LOGS_DIR.mkdir(exist_ok=True)
LOCK_FILE = LOGS_DIR / "start_scheduler.pid"


def _is_process_running(pid: int) -> bool:
    """Return True if a process with given PID is running."""
    try:
        import psutil
        return psutil.pid_exists(pid)
    except ImportError:
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False


def _cleanup_lock():
    """Remove the PID lock file."""
    try:
        if LOCK_FILE.exists():
            LOCK_FILE.unlink()
            logger.info("Cleaned up PID lock file")
    except Exception as e:
        logger.warning(f"Failed to cleanup lock file: {e}")


def _acquire_scheduler_lock() -> bool:
    """Acquire the global scheduler PID lock. Returns True if lock acquired."""
    if LOCK_FILE.exists():
        try:
            existing_pid = int(LOCK_FILE.read_text().strip())
        except (ValueError, IOError):
            existing_pid = None
        
        if existing_pid and _is_process_running(existing_pid):
            logger.error(f"Another start_scheduler instance is running (pid={existing_pid}). Exiting.")
            return False
        else:
            # Stale lock file, remove it
            try:
                LOCK_FILE.unlink()
            except Exception:
                pass
    
    try:
        LOCK_FILE.write_text(str(os.getpid()))
        atexit.register(_cleanup_lock)
        
        def signal_handler(signum, frame):
            _cleanup_lock()
            sys.exit(0)
        
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, signal_handler)
        
        logger.info(f"Scheduler lock acquired (pid={os.getpid()})")
        return True
        
    except Exception as e:
        logger.warning(f"Could not create PID lock file: {e}")
        return True  # Continue anyway if lock file fails


# Job implementation (synchronous) — scheduler will run it in an executor

def send_iteration_report_job():
    """Generate the iteration report and email a summary + attachments."""
    try:
        # Import lazily to avoid import-time issues
        from scripts.generate_iteration_report import generate_report
        from utilities.emailer import send_report_attachment
        from utilities.mcp.pat import get_pat
    except Exception as e:
        logger.exception("Failed to import report/email utilities: %s", e)
        return

    org_url = config.ado_org_url
    project = config.ado_project
    team = config.ado_team
    pat = get_pat()

    if not org_url or not pat:
        logger.warning("Skipping scheduled report: ADO_ORG_URL or PAT not configured")
        return

    areas = config.query_areas if config.query_areas else []
    wi_types = config.query_wi_types if config.query_wi_types else ["User Story", "Bug"]

    try:
        out_file, filtered_file, rows, filtered_rows, html_file = generate_report(
            org_url=org_url,
            pat=pat,
            project=project,
            team=team,
            iteration=config.query_iteration_path,
            areas=areas,
            wi_types=wi_types,
            wiql_text=config.query_wiql_text,
            wiql_file=config.query_wiql_file,
            outputs_dir="outputs",
            areas_filter=areas,
            types_filter=wi_types,
        )
    except Exception as e:
        logger.exception("Failed to generate iteration report: %s", e)
        return

    # Build rich summary using helper (falls back to simple summary on error)
    html_body = "<html><body>"
    html_body += "<h3>Daily Iteration Report Summary</h3>"
    try:
        from utilities.report_summary import build_iteration_summary

        plain_summary, html_summary = build_iteration_summary(out_file)
        # Include the HTML summary snippet and a short note
        html_body += html_summary
        html_body += "<p>This email includes the generated CSV (and HTML) report as attachments.</p>"
    except Exception:
        # Fallback: recreate a compact summary
        attention_items = [r for r in rows if (r.get("UAT Status") in ("yellow", "red")) or (r.get("PROD Status") in ("yellow", "red"))]
        summary_lines: List[str] = []
        if not rows:
            summary_lines.append("No work items found in the report.")
        else:
            summary_lines.append(f"Total work items: {len(rows)}")
            if attention_items:
                summary_lines.append(f"Items requiring attention: {len(attention_items)}")
                for r in attention_items[:20]:
                    wid = r.get("WI_ID", "?")
                    title = r.get("Title", "<no title>")
                    area = r.get("Area Path", "")
                    uat = r.get("UAT Status", "")
                    prod = r.get("PROD Status", "")
                    summary_lines.append(f"- {wid}: {title} | area={area} | UAT={uat or '-'} PROD={prod or '-'}")
            else:
                summary_lines.append("No scheduled UAT/PROD deployments appear to require attention (no yellow/red statuses).")

        html_body += "<p>Below is a brief summary of issues that may require attention.</p>"
        html_body += "<pre style=\"font-family:monospace\">"
        html_body += "\n".join(summary_lines)
        html_body += "</pre>"
        html_body += "<p>This email includes the generated CSV (and HTML) report as attachments.</p>"

    html_body += "</body></html>"

    # recipients: from config report_recipients or default_pm_email or pm_email
    recipients = config.report_recipients if config.report_recipients else []
    if not recipients:
        pm = config.default_pm_email or config.pm_email
        recipients = [pm] if pm else []

    if not recipients:
        logger.warning("No report recipients configured (REPORT_RECIPIENTS or DEFAULT_PM_EMAIL/PM_EMAIL missing). Skipping send.")
        return

    attachments = [out_file]
    attach_html_flag = config.report_send_attach_html
    if (html_file and os.path.exists(html_file)) and attach_html_flag:
        attachments.append(html_file)

    try:
        ok, msg = send_report_attachment(recipients, "Daily Iteration Report", html_body, attachments)
        if ok:
            logger.info("Scheduled report sent to %s (%s)", recipients, msg)
        else:
            logger.warning("Scheduled report send failed: %s", msg)
    except Exception as e:
        logger.exception("Error sending scheduled email: %s", e)


async def main():
    # Acquire global scheduler lock to prevent multiple instances
    if not _acquire_scheduler_lock():
        logger.error("Could not acquire scheduler lock. Exiting.")
        return
    
    # Load config.yaml if present and populate env defaults from it.
    cfg_path = os.path.join(os.path.dirname(__file__), '..', 'config.yaml')
    cfg = {}
    try:
        cfg_path_abs = os.path.abspath(cfg_path)
        if os.path.exists(cfg_path_abs):
            with open(cfg_path_abs, 'r', encoding='utf-8') as cf:
                cfg = yaml.safe_load(cf) or {}
            # schedule: prefer explicit schedulerConfig.tasks[0] then reportSchedule
            # Note: we get these from config now, but this logic is kept for backward compatibility
            sc = cfg.get('schedulerConfig', {}).get('tasks')
            if sc and isinstance(sc, list) and len(sc) > 0:
                cron_from_cfg = sc[0].get('schedule')
            else:
                cron_from_cfg = cfg.get('reportSchedule', {}).get('schedule')
            
            tz = cfg.get('schedulerConfig', {}).get('tasks')
            if tz and isinstance(tz, list) and len(tz) > 0:
                tz_from_cfg = tz[0].get('timezone')
            else:
                tz_from_cfg = cfg.get('reportSchedule', {}).get('timezone')
    except Exception:
        # non-fatal; proceed with env or defaults below
        pass

    # Cron expression: minute hour day month weekday -> 0 10 * * *
    cron = config.report_cron
    timezone = config.report_timezone

    sched = Scheduler()
    
    # NOTE: daily_iteration_report is registered via config.yaml in _register_feature_tasks
    # Do NOT register it here to avoid duplicate task registration
    
    # Register feature tasks from config.yaml (includes daily_iteration_report)
    _register_feature_tasks(sched, cfg)
    
    sched.start_all()

    logger.info("Scheduler started; iteration report will trigger per cron '%s' in tz '%s'", cron, timezone)

    # Wait forever
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        logger.info("Scheduler main cancelled; shutting down")


def _register_feature_tasks(sched: Scheduler, cfg: dict) -> None:
    """Register feature scheduled tasks from config.yaml."""
    tasks_cfg = cfg.get('schedulerConfig', {}).get('tasks', [])
    
    for task_cfg in tasks_cfg:
        task_name = task_cfg.get('name', '')
        enabled = task_cfg.get('enabled', True)
        cron_expr = task_cfg.get('schedule', '0 9 * * *')
        tz = task_cfg.get('timezone', 'UTC')
        
        # Daily Iteration Report (Sprint Tracking Report)
        if task_name == 'daily_iteration_report':
            sched.register_task(
                "daily_iteration_report",
                cron=cron_expr,
                timezone=tz,
                coro=send_iteration_report_job,
                kwargs=None,
                enabled=enabled
            )
            logger.info("Registered daily_iteration_report task: cron='%s' tz='%s' enabled=%s",
                       cron_expr, tz, enabled)
        
        elif task_name == 'bug_areas_highlight':
            try:
                from features.bug_area_highlight.scheduler import bug_areas_highlight_scheduled_task
                sched.register_task(
                    "bug_areas_highlight",
                    cron=cron_expr,
                    timezone=tz,
                    coro=bug_areas_highlight_scheduled_task,
                    kwargs={'config': task_cfg},
                    enabled=enabled
                )
                logger.info("Registered bug_areas_highlight task: cron='%s' tz='%s' enabled=%s", 
                           cron_expr, tz, enabled)
            except ImportError as e:
                logger.warning("Could not register bug_areas_highlight task: %s", e)
        
        # Accept either the new name 'feedback_to_dev' or legacy 'bug_rca_feedback'
        elif task_name in ('feedback_to_dev', 'bug_rca_feedback'):
            try:
                from features.feedback_to_dev.scheduler import feedback_to_dev_scheduled_task
                sched.register_task(
                    "feedback_to_dev",
                    cron=cron_expr,
                    timezone=tz,
                    coro=feedback_to_dev_scheduled_task,
                    kwargs={'config': task_cfg},
                    enabled=enabled
                )
                logger.info("Registered feedback_to_dev task (from %s): cron='%s' tz='%s' enabled=%s",
                            task_name, cron_expr, tz, enabled)
            except ImportError as e:
                logger.warning("Could not register feedback_to_dev task: %s", e)
                
        # Overlooked User Stories - triggers on last working day of sprint
        elif task_name == 'overlooked_user_stories':
            try:
                from overlooked_user_stories.scheduler_task import run_task_from_config as overlooked_task
                
                # Wrap sync function for async scheduler
                def overlooked_scheduled_task(config: dict = None):
                    cfg_with_recipients = config or {}
                    # Merge reportEmailRecipients from root config if not in task config
                    if 'reportEmailRecipients' not in cfg_with_recipients:
                        cfg_with_recipients['reportEmailRecipients'] = cfg.get('reportEmailRecipients', [])
                    overlooked_task(cfg_with_recipients)
                
                sched.register_task(
                    "overlooked_user_stories",
                    cron=cron_expr,
                    timezone=tz,
                    coro=overlooked_scheduled_task,
                    kwargs={'config': task_cfg},
                    enabled=enabled
                )
                logger.info("Registered overlooked_user_stories task: cron='%s' tz='%s' enabled=%s",
                           cron_expr, tz, enabled)
            except ImportError as e:
                logger.warning("Could not register overlooked_user_stories task: %s", e)
        
        # Billing Deviation - triggers on last working day of sprint AND 5 working days before month end
        elif task_name == 'billing_deviation':
            try:
                from billing_deviation.scheduler_task import run_task_from_config as billing_task
                
                # Wrap sync function for async scheduler
                def billing_scheduled_task(config: dict = None):
                    cfg_with_recipients = config or {}
                    # Merge reportEmailRecipients from root config if not in task config
                    if 'reportEmailRecipients' not in cfg_with_recipients:
                        cfg_with_recipients['reportEmailRecipients'] = cfg.get('reportEmailRecipients', [])
                    billing_task(cfg_with_recipients)
                
                sched.register_task(
                    "billing_deviation",
                    cron=cron_expr,
                    timezone=tz,
                    coro=billing_scheduled_task,
                    kwargs={'config': task_cfg},
                    enabled=enabled
                )
                logger.info("Registered billing_deviation task: cron='%s' tz='%s' enabled=%s",
                           cron_expr, tz, enabled)
            except ImportError as e:
                logger.warning("Could not register billing_deviation task: %s", e)
        
        # Backlog Triaging - triggers on Fridays at 10am
        elif task_name == 'backlog_triaging':
            try:
                import scripts.backlog_triaging as backlog_module
                
                # Wrap sync function for async scheduler
                def backlog_scheduled_task(config: dict = None):
                    cfg_with_recipients = config or {}
                    # Merge reportEmailRecipients from root config if not in task config
                    if 'reportEmailRecipients' not in cfg_with_recipients:
                        cfg_with_recipients['reportEmailRecipients'] = cfg.get('reportEmailRecipients', [])
                    backlog_module.run_task_from_config(cfg_with_recipients)
                
                sched.register_task(
                    "backlog_triaging",
                    cron=cron_expr,
                    timezone=tz,
                    coro=backlog_scheduled_task,
                    kwargs={'config': task_cfg},
                    enabled=enabled
                )
                logger.info("Registered backlog_triaging task: cron='%s' tz='%s' enabled=%s",
                           cron_expr, tz, enabled)
            except ImportError as e:
                logger.warning("Could not register backlog_triaging task: %s", e)

        # Developer Knowledge Base Sync - Nightly update of developer skills vector DB
        elif task_name == 'developer_kb_sync':
            try:
                from utilities.developer_knowledge_base import developer_kb_sync_scheduled_task
                sched.register_task(
                    "developer_kb_sync",
                    cron=cron_expr,
                    timezone=tz,
                    coro=developer_kb_sync_scheduled_task,
                    kwargs={'config': task_cfg},
                    enabled=enabled
                )
                logger.info("Registered developer_kb_sync task: cron='%s' tz='%s' enabled=%s",
                            cron_expr, tz, enabled)
            except ImportError as e:
                logger.warning("Could not register developer_kb_sync task: %s", e)

    # _register_feature_tasks is a synchronous helper; do not block here.
    return


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Scheduler stopped by user")
