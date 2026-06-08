"""
PM Skills Registry - All fixed PM skills.

Each skill is a deterministic operation that:
1. Takes structured parameters
2. Delegates data fetching to PM Agent (ADO Data Agent)
3. Applies business logic
4. Returns structured results

Skills available:
- bug_areas_highlight: Detect recurring bugs by area, send email report
- overlooked_stories: Find overlooked user stories, send reminder
- iteration_report: Generate iteration/sprint report
- send_email: Send email with content/attachments

IMPORTANT: Skills must NOT call ADO/WIQL/HTTP directly.
Use the injected pm_agent data adapter methods instead.
"""

import os
import json
import logging
import asyncio
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List, Callable, Awaitable
from datetime import datetime
import asyncio
import inspect

logger = logging.getLogger("pm_skill_agent.skills")


# Global reference to PM Agent for data access (set by orchestrator)
_pm_agent = None


def set_pm_agent(agent):
    """Set the PM Agent instance for data access."""
    global _pm_agent
    _pm_agent = agent
    logger.info("PM Agent adapter set for skills")


def get_pm_agent():
    """Get the PM Agent instance for data access."""
    global _pm_agent
    return _pm_agent


@dataclass
class SkillResult:
    """Result from executing a skill."""
    success: bool
    result: Any = None
    data: Dict[str, Any] = field(default_factory=dict)
    message: Optional[str] = None
    error: Optional[str] = None
    confidence: float = 1.0
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SkillDefinition:
    """Definition of a PM skill."""
    name: str
    description: str
    handler: Callable[..., Awaitable[SkillResult]]
    required_params: List[str] = field(default_factory=list)
    optional_params: Dict[str, Any] = field(default_factory=dict)
    use_cases: List[str] = field(default_factory=list)
    llm_visible: bool = True  # If False, skill is hidden from LLM prompt but still dispatchable


# ============================================================================
# SKILL HANDLERS
# ============================================================================


# Import feature skills at module level for use in skill handlers
try:
    from features.bug_area_highlight.skill import run_bug_areas_highlight as _feature_bug_areas_highlight
    from features.feedback_to_dev.skill import run_feedback_to_dev as _feature_feedback_to_dev
    _features_available = True
    logger.info("Feature skills loaded: bug_area_highlight, feedback_to_dev")
except ImportError as e:
    _features_available = False
    _feature_bug_areas_highlight = None
    _feature_feedback_to_dev = None
    logger.warning(f"Feature skills not available: {e}")


async def _run_bug_areas_highlight(params: Dict[str, Any]) -> SkillResult:
    """
    Execute Bug Areas Highlight skill.
    
    Detects recurring bugs by area path and sends email report.
    Delegates to the feature implementation if available.
    
    Params:
        - project: ADO project name
        - lookback_days: Days to look back (default 60)
        - recurrence_threshold: Min bugs to consider recurring (default 3)
        - similarity_threshold: Title similarity threshold (default 0.75)
        - recipients: Email recipients list (optional, uses config default)
        - send_email: Whether to send email (default True)
        - preview_only: Just preview, don't email (default False)
    """
    # Use feature implementation if available
    if _features_available and _feature_bug_areas_highlight:
        logger.info("Using feature implementation for bug_areas_highlight")
        return await _feature_bug_areas_highlight(params)
    
    # Fallback to inline implementation
    try:
        from utilities.bug_areas_highlight import (
            detect_recurring, build_html_summary
        )
        from utilities.emailer import send_report_attachment
        from config import config as app_config
        
        # Extract parameters
        project = params.get("project") or app_config.ado_project
        lookback_days = int(params.get("lookback_days", 60))
        recurrence_threshold = int(params.get("recurrence_threshold", 3))
        similarity_threshold = float(params.get("similarity_threshold", 0.75))
        recipients = params.get("recipients") or getattr(app_config, "reportEmailRecipients", [])
        send_email = params.get("send_email", True)
        preview_only = params.get("preview_only", False)
        
        # Get org URL for HTML links
        org_url = app_config.ado_org_url
        
        if not org_url:
            return SkillResult(
                success=False,
                error="ADO_ORG_URL not configured"
            )
        
        # Build WIQL query for bugs in last N days
        wiql = f"""
        SELECT [System.Id]
        FROM WorkItems
        WHERE [System.WorkItemType] = 'Bug'
          AND [System.TeamProject] = '{project}'
          AND [System.CreatedDate] >= @Today - {lookback_days}
        ORDER BY [System.CreatedDate] DESC
        """
        
        logger.info(f"Running bug areas highlight: project={project}, lookback={lookback_days}d")
        
        # Get PM Agent for data access
        pm_agent = get_pm_agent()
        
        if pm_agent:
            # Use PM Agent's data adapter methods (preferred)
            bug_ids = await pm_agent.run_wiql(wiql, project=project)
            logger.info(f"PM Agent adapter: Found {len(bug_ids)} bugs in last {lookback_days} days")
            
            if not bug_ids:
                return SkillResult(
                    success=True,
                    result={
                        "count": 0,
                        "recurring_count": 0,
                        "message": f"No bugs found in {project} in last {lookback_days} days",
                        "areas": {}
                    },
                    metadata={"project": project, "lookback_days": lookback_days, "adapter": "pm_agent"}
                )
            
            # Fetch work item details via PM Agent
            bugs = await pm_agent.fetch_workitems(bug_ids)
            logger.info(f"PM Agent adapter: Fetched details for {len(bugs)} bugs")
            
        else:
            # Fallback: Direct ADO access (deprecated, log warning)
            logger.warning("PM Agent not available, falling back to direct ADO access (deprecated)")
            from utilities.bug_areas_highlight import run_wiql, fetch_workitems
            from utilities.mcp.pat import get_pat
            
            pat = get_pat()
            if not pat:
                return SkillResult(success=False, error="PAT not configured")
            
            bug_ids = run_wiql(org_url, wiql, pat, project=project)
            logger.info(f"Direct ADO: Found {len(bug_ids)} bugs in last {lookback_days} days")
            
            if not bug_ids:
                return SkillResult(
                    success=True,
                    result={
                        "count": 0,
                        "recurring_count": 0,
                        "message": f"No bugs found in {project} in last {lookback_days} days",
                        "areas": {}
                    },
                    metadata={"project": project, "lookback_days": lookback_days, "adapter": "direct"}
                )
            
            bugs = fetch_workitems(org_url, bug_ids, pat)
            logger.info(f"Direct ADO: Fetched details for {len(bugs)} bugs")
        
        # Detect recurring patterns (business logic, no ADO access)
        recurring = detect_recurring(
            bugs,
            similarity_threshold=similarity_threshold,
            recurrence_threshold=recurrence_threshold
        )
        
        recurring_count = sum(
            len(area_data.get("clusters", []))
            for area_data in recurring.values()
            if isinstance(area_data, dict)
        )
        
        # Build HTML summary (business logic, no ADO access)
        html_content = build_html_summary(org_url, recurring)
        
        # Save preview file
        preview_path = None
        try:
            os.makedirs("logs", exist_ok=True)
            preview_path = "logs/bug_areas_preview.html"
            with open(preview_path, "w", encoding="utf-8") as f:
                f.write(html_content)
            logger.info(f"Preview saved to {preview_path}")
        except Exception as e:
            logger.warning(f"Could not save preview: {e}")
        
        # Send email if requested
        email_sent = False
        email_error = None
        if send_email and not preview_only and recipients:
            try:
                subject = f"Bug Areas Highlight - {project} ({datetime.now().strftime('%Y-%m-%d')})"
                ok, msg = send_report_attachment(
                    recipients,
                    subject,
                    html_content,
                    attachments=None
                )
                email_sent = ok
                if not ok:
                    email_error = msg
                logger.info(f"Email {'sent' if ok else 'failed'}: {msg}")
            except Exception as e:
                email_error = str(e)
                logger.exception(f"Email send error: {e}")
        
        return SkillResult(
            success=True,
            result={
                "count": len(bugs),
                "recurring_count": recurring_count,
                "areas": list(recurring.keys()),
                "preview_path": preview_path,
                "email_sent": email_sent,
                "email_error": email_error,
                "recipients": recipients if email_sent else None
            },
            metadata={
                "project": project,
                "lookback_days": lookback_days,
                "recurrence_threshold": recurrence_threshold,
                "similarity_threshold": similarity_threshold,
                "adapter": "pm_agent" if pm_agent else "direct"
            }
        )
        
    except Exception as e:
        logger.exception(f"Bug areas highlight error: {e}")
        return SkillResult(success=False, error=str(e))


async def _run_overlooked_stories(params: Dict[str, Any]) -> SkillResult:
    """
    Execute Overlooked Stories skill.
    
    Finds user stories that may have been overlooked (no activity, stale, etc.)
    and optionally sends reminder email.
    
    This skill:
    1. Runs the script with --dry-run to refresh reports (unless sending email)
    2. Uses the responder to fetch and format items for display
    3. Only sends email if explicitly requested with recipients
    
    Params:
        - project: ADO project name
        - stale_days: Days of inactivity to consider stale (default 14)
        - recipients: Email recipients (required for sending email)
        - send_email: Whether to send email (default False)
        - query: Optional query context for responder
    """
    try:
        import subprocess
        import sys
        from config import config
        from pathlib import Path
        
        project = params.get("project") or config.ado_project
        recipients = params.get("recipients", [])
        query = params.get("query", "overlooked user stories")
        
        # Determine send behavior: only send when user explicitly requests it
        # and provided recipients. Default is dry-run (no emails).
        send_email = bool(params.get("send_email", False))
        send_recipients = []
        if isinstance(recipients, list):
            send_recipients = [r for r in recipients if r]
        elif isinstance(recipients, str) and recipients.strip():
            send_recipients = [recipients.strip()]

        # Prepare environment and PYTHONPATH so the script can import project config
        run_env = os.environ.copy()
        # Resolve repo root from agents/pm_skill_agent location (2 levels up)
        repo_root = Path(__file__).resolve().parents[2]
        run_env["PYTHONPATH"] = str(repo_root)

        # Build command to run the script
        cmd = [sys.executable, "overlooked_user_stories/overlooked_stories_reminder.py"]
        
        if send_email and send_recipients:
            # User explicitly requested email with recipients - allow sending
            run_env["OVERLOOKED_SEND_TO"] = ",".join(send_recipients)
            logger.info(f"Overlooked stories: will send email to {send_recipients}")
        else:
            # Default: dry-run to avoid accidental emails from automated queries
            cmd.append("--dry-run")
            logger.info("Overlooked stories: running in dry-run mode (no email)")

        logger.info(f"Running overlooked stories: {' '.join(cmd)} (send_email={send_email}, recipients={send_recipients})")

        # Run the script to generate/refresh reports
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600, env=run_env, cwd=str(repo_root))
        
        if proc.returncode != 0:
            error = proc.stderr or ""
            return SkillResult(
                success=False,
                error=error or f"Script exited with code {proc.returncode}",
                result={"output": proc.stdout[:500], "error_output": error[:500]}
            )
        
        # Script succeeded - now use the responder to fetch and format data with items
        try:
            from utilities.overlooked_responder import handle_overlooked_query
            
            # Build a simple intent dict for the responder
            intent = {
                "skill_id": "overlooked_stories",
                "score": 0.95,
                "confidence": "high"
            }
            
            # Call responder to fetch data and format response with items
            response_data = handle_overlooked_query(query, intent)
            
            # Extract items and summary from responder
            summary_text = response_data.get("summary_text", "Overlooked stories analysis completed")
            data = response_data.get("data", {})
            items = data.get("items", [])
            download_path = data.get("download_path")
            
            # Parse script output for email status
            output = proc.stdout or ""
            sent_line = None
            for line in output.splitlines():
                if "sent" in line.lower() and ("email" in line.lower() or "report" in line.lower()):
                    sent_line = line.strip()
                    break
            
            # If email was sent, append that info to summary
            if sent_line:
                summary_text = f"{summary_text}\n\n[EMAIL] {sent_line}"
            
            # Return formatted result with items for UI display
            return SkillResult(
                success=True,
                result={
                    "message": summary_text,
                    "email_status": sent_line,
                    "items": items,  # Include items for UI rendering
                    "download_path": download_path,
                    "data_source": data.get("data_source", "ADO Live"),
                    "item_count": len(items)
                },
                metadata={
                    "project": project,
                    "send_email": send_email,
                    "recipients": send_recipients,
                    "item_count": len(items)
                }
            )
            
        except ImportError as e:
            logger.warning(f"Could not import responder, returning basic result: {e}")
            # Fallback to basic result if responder not available
            return SkillResult(
                success=True,
                result={
                    "message": "Overlooked stories script completed. Responder not available for detailed formatting.",
                    "output": proc.stdout[:1000]
                },
                metadata={"project": project}
            )
            
    except subprocess.TimeoutExpired:
        return SkillResult(success=False, error="Overlooked stories script timed out (10min)")
    except Exception as e:
        logger.exception(f"Overlooked stories error: {e}")
        return SkillResult(success=False, error=str(e))


async def _run_iteration_report(params: Dict[str, Any]) -> SkillResult:
    """
    Generate iteration/sprint report.
    
    Params:
        - project: ADO project name
        - iteration: Iteration path (default @CurrentIteration)
        - areas: Area paths to filter
        - wi_types: Work item types (default: User Story, Bug)
        - recipients: Email recipients
        - send_email: Whether to send email
    """
    try:
        from scripts.generate_iteration_report import generate_report
        from utilities.emailer import send_report_attachment
        from config import config
        
        project = params.get("project") or config.ado_project
        org_url = config.ado_org_url
        pat = config.ado_pat
        team = params.get("team") or config.ado_team
        iteration = params.get("iteration", "@CurrentIteration")
        areas = params.get("areas", [])
        wi_types = params.get("wi_types", ["User Story", "Bug"])
        recipients = params.get("recipients", [])
        send_email = params.get("send_email", False)
        
        if not org_url or not pat:
            return SkillResult(success=False, error="ADO_ORG_URL or ADO_PAT not configured")
        
        logger.info(f"Generating iteration report: project={project}, iteration={iteration}")
        
        out_file, filtered_file, rows, filtered_rows, html_file = generate_report(
            org_url=org_url,
            pat=pat,
            project=project,
            team=team or None,
            iteration=iteration,
            areas=areas,
            wi_types=wi_types,
            outputs_dir="outputs"
        )
        
        email_sent = False
        if send_email and recipients:
            try:
                from pathlib import Path
                attachments = [Path(filtered_file or out_file)]
                if html_file:
                    attachments.append(Path(html_file))
                subject = f"Iteration Report - {project} ({iteration})"
                ok, msg = send_report_attachment(recipients, subject, "Iteration report attached.", attachments)
                email_sent = ok
            except Exception as e:
                logger.warning(f"Email send failed: {e}")
        
        return SkillResult(
            success=True,
            result={
                "csv_file": out_file,
                "filtered_csv": filtered_file,
                "html_file": html_file,
                "total_items": len(rows),
                "filtered_items": len(filtered_rows),
                "email_sent": email_sent
            },
            metadata={"project": project, "iteration": iteration}
        )
        
    except Exception as e:
        logger.exception(f"Iteration report error: {e}")
        return SkillResult(success=False, error=str(e))



# NOTE: _run_iteration_info handler REMOVED — iteration_info skill was removed
# because it misrouted data-fetch queries. Sprint queries now go through LLM planner.


async def _send_email(params: Dict[str, Any]) -> SkillResult:
    """
    Send email with specified content.
    
    Params:
        - recipients: List of email addresses (required)
        - subject: Email subject (required)
        - body: Email body (HTML or plain text)
        - attachments: List of file paths to attach
    """
    try:
        from utilities.emailer import send_report_attachment
        from pathlib import Path
        
        recipients = params.get("recipients")
        subject = params.get("subject")
        body = params.get("body", "")
        attachments = params.get("attachments", [])
        
        if not recipients:
            return SkillResult(success=False, error="No recipients specified")
        if not subject:
            return SkillResult(success=False, error="No subject specified")
        
        # Convert attachment paths to Path objects
        attach_paths = [Path(p) for p in attachments] if attachments else None
        
        ok, msg = send_report_attachment(recipients, subject, body, attach_paths)
        
        if ok:
            return SkillResult(
                success=True,
                result={"message": f"Email sent to {', '.join(recipients)}", "status": msg}
            )
        else:
            return SkillResult(success=False, error=f"Email failed: {msg}")
            
    except Exception as e:
        logger.exception(f"Send email error: {e}")
        return SkillResult(success=False, error=str(e))


async def _list_area_paths(params: Dict[str, Any]) -> SkillResult:
    """
    List area paths for a project with optional team filtering.
    Uses PM Agent's unified list_area_paths method for ADO access.
    
    Params:
        - project: ADO project name
        - team: Optional team name to filter area paths
    """
    try:
        from config import config
        project = params.get("project") or config.ado_project
        team = params.get("team")  # NEW: Optional team parameter
        
        # Get PM Agent for data access
        pm_agent = get_pm_agent()
        
        if pm_agent:
            # Use PM Agent's unified method with team support
            result_text = await pm_agent.list_area_paths(project=project, team=team)
            logger.info(f"PM Agent method returned area paths (team={team})")
            
            # Parse paths from formatted result
            paths = [line.replace('- ', '').strip() for line in result_text.split('\n') if line.strip().startswith('-')]
            
            return SkillResult(
                success=True,
                result={
                    "project": project,
                    "team": team,  # NEW: Include team in result
                    "count": len(paths),
                    "area_paths": paths,
                    "adapter": "pm_agent"
                }
            )
        else:
            # Fallback: Direct ADO access (deprecated, log warning)
            logger.warning("PM Agent not available, falling back to direct ADO access (deprecated)")
            import requests
            from utilities.mcp.pat import get_pat
            
            org_url = config.ado_org_url
            pat = get_pat()
            
            if not org_url or not pat:
                return SkillResult(success=False, error="ADO_ORG_URL or PAT not configured")
            
            url = f"{org_url}/{project}/_apis/wit/classificationnodes/areas?$depth=10&api-version=7.0"
            resp = requests.get(url, auth=("", pat), timeout=30)
            resp.raise_for_status()
            
            data = resp.json()
            paths = []
            
            def visit(node, prefix=""):
                name = node.get("name")
                full = f"{prefix}{name}" if prefix else name
                paths.append(full)
                for child in node.get("children", []):
                    visit(child, full + "\\")
            
            if "value" in data:
                for node in data["value"]:
                    visit(node)
            elif "children" in data:
                for node in data["children"]:
                    visit(node)
            
            return SkillResult(
                success=True,
                result={
                    "project": project,
                    "count": len(paths),
                    "area_paths": paths,
                    "adapter": "direct"
                }
            )
        
    except Exception as e:
        logger.exception(f"List area paths error: {e}")
        return SkillResult(success=False, error=str(e))


# ============================================================================
# SKILL REGISTRY
# ============================================================================

# Import feature skills
try:
    from features.bug_area_highlight.skill import run_bug_areas_highlight as _feature_bug_areas_highlight
    from features.feedback_to_dev.skill import run_feedback_to_dev as _feature_feedback_to_dev
    _features_available = True
except ImportError:
    _features_available = False
    _feature_bug_areas_highlight = None
    _feature_feedback_to_dev = None


async def _run_feedback_to_dev(params: Dict[str, Any]) -> SkillResult:
    """
    Execute Feedback to Dev skill.
    
    Detects new bugs, finds similar historical bugs, extracts RCA content,
    and sends feedback notifications to developers.
    """
    if _features_available and _feature_feedback_to_dev:
        return await _feature_feedback_to_dev(params)
    
    # Fallback implementation


async def _run_get_sprint_status(params: Dict[str, Any]) -> SkillResult:
    """
    Get current sprint status with work item counts, completion percentage,
    planned vs completed comparison, and tracking status.
    
    Uses comprehensive sprint tracking with PM warning detection.
    
    Params:
        - project: ADO project name (optional)
        - iteration: Iteration path (optional)
    """
    try:
        from utilities.sprint_tracking import (
            generate_sprint_status_report,
            should_trigger_pm_warning,
            get_current_sprint_info
        )
        
        # Get sprint info and generate comprehensive report
        sprint_info = get_current_sprint_info()
        
        if not sprint_info:
            return SkillResult(
                success=False,
                data={},
                message="⚠️ No active sprint found",
                confidence=1.0
            )
        
        # Generate full status report
        status_report = generate_sprint_status_report()
        
        # Check if PM warning needed
        should_warn, warning_reason = should_trigger_pm_warning()
        
        result_data = {
            "sprint_name": sprint_info["name"],
            "sprint_path": sprint_info["path"],
            "start_date": sprint_info["start_date"],
            "finish_date": sprint_info["finish_date"],
            "status_report": status_report,
            "pm_warning_needed": should_warn,
            "warning_reason": warning_reason
        }
        
        return SkillResult(
            success=True,
            data=result_data,
            message=status_report,
            confidence=1.0
        )
    except Exception as e:
        logger.error(f"Error in get_sprint_status: {e}", exc_info=True)
        return SkillResult(
            success=False,
            data={},
            message=f"Failed to get sprint status: {str(e)}",
            confidence=0.5
        )


async def _run_get_backlog_health(params: Dict[str, Any]) -> SkillResult:
    """
    Backlog health entry point for UI — no business logic executed here.

    The UI should call `utilities.backlog_health` to run the actual analysis.
    This handler only signals the UI to present the backlog health form or view.
    """
    try:
        # Backlog health is implemented via the backlog triaging UI/report.
        # Return the UI-form sentinel targeting `backlog_triaging` so the
        # frontend will open the correct form.
        # auto_grant_access: True allows direct UI opening without permission check
        return SkillResult(
            success=True,
            result={
                "requires_ui_form": True,
                # Target the backlog triaging form which runs backlog health
                "skill": "backlog_triaging",
                "message": "📋 Open Backlog Triaging (Backlog Health) to run analysis and view results.",
                "alias": "get_backlog_health",
                # Grant automatic access to open UI directly
                "auto_grant_access": True,
                "open_ui_directly": True,
                "note": "The backlog health analysis is provided through the Backlog Triaging report/form, which needs to be accessed via the user interface.",
                "action_required": "Please open the Backlog Triaging form to proceed with the analysis."
            },
            message="📋 Open Backlog Triaging (Backlog Health) to run analysis and view results.",
            metadata={
                "requires_ui_form": True,
                "skill": "backlog_triaging",
                "alias": "get_backlog_health",
                "note": "Backlog health is provided by the Backlog Triaging report/form",
                "auto_grant_access": True,
                "open_ui_directly": True
            },
            confidence=1.0
        )
    except Exception as e:
        logger.error(f"Error initializing backlog health skill: {e}", exc_info=True)
        return SkillResult(
            success=False,
            data={},
            message=f"Failed to initialize backlog health: {str(e)}",
            confidence=0.5
        )


async def _run_get_capacity_forecast(params: Dict[str, Any]) -> SkillResult:
    """
    Handle capacity forecast skill - shows inline capacity check UI form.
    
    This skill triggers the inline capacity display that shows the latest
    capacity report data directly in the chat, similar to sprint_plan.
    
    Params (optional):
        - project: ADO project name
        - team: Team name
        - iterationId: Sprint/iteration identifier
    """
    try:
        # Return with requires_ui_form to trigger inline UI rendering
        # The render_capacity_check_inline() in chat_extensions.py will load and display the data
        return SkillResult(
            success=True,
            result={
                "requires_ui_form": True,
                "skill": "get_capacity_forecast",
                "auto_grant_access": True,
                "open_ui_directly": True,
                "message": "📊 Please use the Capacity Check form below to view capacity data."
            },
            message="📊 Please use the Capacity Check form below to view capacity data.",
            metadata={
                "requires_ui_form": True,
                "skill": "get_capacity_forecast",
                "auto_grant_access": True,
                "open_ui_directly": True
            }
        )
    except Exception as e:
        logger.error(f"Error in get_capacity_forecast: {e}", exc_info=True)
        return SkillResult(
            success=False,
            error=f"Failed to initialize capacity forecast: {str(e)}"
        )


async def _run_developer_skills(params: Dict[str, Any]) -> SkillResult:
    """
    Handle developer skills knowledge base - requires UI form for input.
    
    This skill returns a special response indicating that a UI form is needed.
    The actual knowledge base display happens through the developer_knowledge_base utility.
    
    Params:
        - developer: Filter by developer name (optional)
        - technology: Filter by technology/language (optional)
    """
    try:
        # This skill requires UI form input, so we return a special response
        # The UI (handle_chat_prompt) will detect the skill and show the form
        # auto_grant_access: True allows direct UI opening without permission check
        access_info = {
            "title": "Developer Knowledge Base Access",
            "permission_needed": "view_developer_knowledge_base",
            "permission_label": "Permission Needed: You must have the proper permissions to view the Developer Knowledge Base.",
            "allowed_roles": [
                {"role": "pm", "label": "PM"},
                {"role": "team_lead", "label": "Team Lead"},
                {"role": "admin", "label": "Admin"}
            ],
            "note": "Access to the Developer Knowledge Base requires a UI form and proper permissions. If you do not have the necessary role, please contact your administrator for access."
        }
        
        return SkillResult(
            success=True,
            result={
                "requires_ui_form": True,
                "skill": "developer_skills",
                "message": "📚 Please use the Developer Knowledge Base to view developer skills and expertise. Access requires proper permissions.",
                "access": access_info,
                # Grant automatic access to open UI directly
                "auto_grant_access": True,
                "open_ui_directly": True
            },
            message="📚 Please use the Developer Knowledge Base to view developer skills and expertise.",
            metadata={
                "requires_ui_form": True,
                "skill": "developer_skills",
                "access": access_info,
                "auto_grant_access": True,
                "open_ui_directly": True
            }
        )
    except Exception as e:
        logger.error(f"Error in developer_skills: {e}", exc_info=True)
        return SkillResult(
            success=False,
            result={},
            message=f"Failed to initialize developer skills: {str(e)}",
            confidence=0.5
        )


# ============================================================================
# BACKLOG ORCHESTRATION SKILLS (Modular Tools)
# ============================================================================

async def _run_get_team_area_path(params: Dict[str, Any]) -> SkillResult:
    """Discover team's area path for filtering backlog queries."""
    try:
        from agents.pm_skill_agent.backlog_handlers import handle_get_team_area_path
        
        project = params.get("project")
        team = params.get("team")
        pm_agent = get_pm_agent()
        
        if not project or not team:
            return SkillResult(
                success=False,
                error="Missing required params: project, team"
            )
        
        result = await handle_get_team_area_path(project, team, pm_agent)
        
        return SkillResult(
            success=True,
            data=result,
            message=f"Area path: {result.get('area_path')}",
            confidence=0.9 if result.get('confidence') == 'high' else 0.6
        )
    except Exception as e:
        logger.error(f"Error in get_team_area_path: {e}", exc_info=True)
        return SkillResult(success=False, error=str(e))


async def _run_get_backlog_items(params: Dict[str, Any]) -> SkillResult:
    """Fetch backlog items from Azure DevOps."""
    try:
        from agents.pm_skill_agent.backlog_handlers import handle_get_backlog_items
        
        project = params.get("project")
        team = params.get("team")
        pm_agent = get_pm_agent()
        
        if not project or not team:
            return SkillResult(
                success=False,
                error="Missing required params: project, team"
            )
        
        result = await handle_get_backlog_items(
            project=project,
            team=team,
            pm_agent=pm_agent,
            area_path=params.get("areaPath"),
            include_states=params.get("includeStates"),
            effort_field=params.get("effortField", "Custom.Effort3P"),
            max_items=params.get("maxItems", 1000)
        )
        
        return SkillResult(
            success=True,
            data=result,
            message=f"Found {result.get('backlog_items', 0)} items, {result.get('total_story_points', 0):.1f} points",
            confidence=1.0
        )
    except Exception as e:
        logger.error(f"Error in get_backlog_items: {e}", exc_info=True)
        return SkillResult(success=False, error=str(e))


async def _run_calculate_team_velocity(params: Dict[str, Any]) -> SkillResult:
    """Calculate team velocity from recent sprints."""
    try:
        from agents.pm_skill_agent.backlog_handlers import handle_calculate_team_velocity
        
        project = params.get("project")
        team = params.get("team")
        pm_agent = get_pm_agent()
        
        if not project or not team:
            return SkillResult(
                success=False,
                error="Missing required params: project, team"
            )
        
        result = await handle_calculate_team_velocity(
            project=project,
            team=team,
            pm_agent=pm_agent,
            sprint_count=params.get("sprintCount", 3),
            effort_field=params.get("effortField", "Custom.Effort3P"),
            fallback_capacity=params.get("fallbackCapacity", 8.0)
        )
        
        confidence_map = {"high": 0.9, "medium": 0.6, "low": 0.3}
        confidence = confidence_map.get(result.get("confidence", "medium"), 0.6)
        
        return SkillResult(
            success=True,
            data=result,
            message=f"Velocity: {result.get('velocity', 0):.1f} points/sprint (confidence: {result.get('confidence', 'medium')})",
            confidence=confidence
        )
    except Exception as e:
        logger.error(f"Error in calculate_team_velocity: {e}", exc_info=True)
        return SkillResult(success=False, error=str(e))


async def _run_estimate_backlog_items(params: Dict[str, Any]) -> SkillResult:
    """Use LLM to estimate unestimated backlog items."""
    try:
        from agents.pm_skill_agent.backlog_handlers import handle_estimate_backlog_items
        
        items = params.get("items", [])
        velocity = params.get("velocity", 100.0)
        pm_agent = get_pm_agent()
        
        if not items:
            return SkillResult(
                success=True,
                data={"estimated_items": [], "total_estimated_effort": 0.0, "average_confidence": 1.0},
                message="No items to estimate",
                confidence=1.0
            )
        
        result = await handle_estimate_backlog_items(
            items=items,
            velocity=velocity,
            pm_agent=pm_agent,
            model=params.get("model", "gpt-4o"),
            max_items_to_estimate=params.get("maxItemsToEstimate", 50),
            context_items=params.get("contextItems")
        )
        
        return SkillResult(
            success=True,
            data=result,
            message=f"Estimated {len(result.get('estimated_items', []))} items, {result.get('total_estimated_effort', 0):.1f} points",
            confidence=result.get("average_confidence", 0.5)
        )
    except Exception as e:
        logger.error(f"Error in estimate_backlog_items: {e}", exc_info=True)
        return SkillResult(success=False, error=str(e))


async def _run_calculate_backlog_health(params: Dict[str, Any]) -> SkillResult:
    """Calculate backlog health metrics and risk status."""
    try:
        from agents.pm_skill_agent.backlog_handlers import handle_calculate_backlog_health
        
        backlog_items = params.get("backlog_items", 0)
        total_story_points = params.get("total_story_points", 0.0)
        velocity = params.get("velocity", 100.0)
        pm_agent = get_pm_agent()
        
        result = await handle_calculate_backlog_health(
            backlog_items=backlog_items,
            total_story_points=total_story_points,
            velocity=velocity,
            pm_agent=pm_agent,
            thin_threshold=params.get("thin_threshold", 1.5),
            healthy_threshold=params.get("healthy_threshold", 3.0)
        )
        
        status = result.get("status", "UNKNOWN")
        depth = result.get("backlog_depth", 0)
        
        return SkillResult(
            success=True,
            data=result,
            message=f"Backlog status: {status} ({depth:.1f} sprints)",
            confidence=1.0
        )
    except Exception as e:
        logger.error(f"Error in calculate_backlog_health: {e}", exc_info=True)
        return SkillResult(success=False, error=str(e))


async def _run_generate_backlog_recommendations(params: Dict[str, Any]) -> SkillResult:
    """Generate actionable recommendations based on backlog health."""
    try:
        from agents.pm_skill_agent.backlog_handlers import handle_generate_backlog_recommendations
        
        health_metrics = params.get("health_metrics", {})
        backlog_items = params.get("backlog_items", [])
        velocity = params.get("velocity", 100.0)
        pm_agent = get_pm_agent()
        
        if not health_metrics:
            return SkillResult(
                success=False,
                error="Missing required param: health_metrics"
            )
        
        result = await handle_generate_backlog_recommendations(
            health_metrics=health_metrics,
            backlog_items=backlog_items,
            velocity=velocity,
            pm_agent=pm_agent,
            team_context=params.get("team_context"),
            historical_trends=params.get("historical_trends")
        )
        
        urgency = result.get("urgency", "none")
        rec_count = len(result.get("recommendations", []))
        
        return SkillResult(
            success=True,
            data=result,
            message=f"Generated {rec_count} recommendations (urgency: {urgency})",
            confidence=0.8
        )
    except Exception as e:
        logger.error(f"Error in generate_backlog_recommendations: {e}", exc_info=True)
        return SkillResult(success=False, error=str(e))


# REMOVED: Old form-based billing_deviation handler
# The new conversational handler is at line 1297


async def _run_sprint_plan(params: Dict[str, Any]) -> SkillResult:
    """
    Handle sprint plan skill - loads and displays actual sprint plan data.
    
    This skill loads the latest sprint plan and displays it as a table,
    similar to how developer_skills and backlog_assignments work.
    
    Params (optional):
        - sprint_name: Sprint name to filter by
    """
    try:
        # Import chat service to load sprint plan data
        import sys
        from pathlib import Path
        import pandas as pd
        
        app_path = Path(__file__).parent.parent.parent / "app"
        if str(app_path) not in sys.path:
            sys.path.insert(0, str(app_path))
        
        from chat_service import load_latest_sprint_plan_csv
        
        # Load the latest sprint plan CSV
        csv_path = load_latest_sprint_plan_csv()
        
        if not csv_path or not csv_path.exists():
            return SkillResult(
                success=True,
                result={
                    "message": "📅 No sprint plan found. Generate a sprint plan using the Sprint Plan Generator.",
                    "data": []
                },
                message="📅 No sprint plan found. Generate a sprint plan using the Sprint Plan Generator."
            )
        
        # Load CSV data
        df = pd.read_csv(csv_path, encoding='utf-8')
        
        # Get summary statistics
        total_tasks = len(df)
        total_hours = df['Estimated Hours'].sum() if 'Estimated Hours' in df.columns else 0
        fe_count = df['Responsible - Frontend'].notna().sum() if 'Responsible - Frontend' in df.columns else 0
        be_count = df['Responsible - Backend'].notna().sum() if 'Responsible - Backend' in df.columns else 0
        
        # Format the response with data table
        result = {
            "message": "📅 **Sprint Plan Report**",
            "summary": {
                "total_tasks": total_tasks,
                "total_hours": f"{total_hours:.0f}h",
                "fe_assigned": fe_count,
                "be_assigned": be_count
            },
            "tasks": df.to_dict('records')[:20]  # First 20 rows
        }
        
        # Build formatted message
        message_parts = [
            "📅 **Sprint Plan Report**\n",
            f"**Summary:**",
            f"- Total Tasks: {total_tasks}",
            f"- Total Hours: {total_hours:.0f}h",
            f"- FE Assigned: {fe_count}",
            f"- BE Assigned: {be_count}\n"
        ]
        
        # Show sample tasks
        if len(df) > 0:
            message_parts.append("**Sample Tasks (first 5):**")
            for idx, row in df.head(5).iterrows():
                task_name = row.get('Task Name', 'Unknown')
                fe = row.get('Responsible - Frontend', 'N/A')
                be = row.get('Responsible - Backend', 'N/A')
                hours = row.get('Estimated Hours', 'N/A')
                status = row.get('Status', 'Not Started')
                message_parts.append(f"- {task_name}: FE={fe}, BE={be}, {hours}h, Status={status}")
            
            if len(df) > 5:
                message_parts.append(f"\n... and {len(df) - 5} more tasks")
        
        return SkillResult(
            success=True,
            result=result,
            message="\n".join(message_parts)
        )
    except Exception as e:
        logger.error(f"Error in sprint_plan: {e}", exc_info=True)
        return SkillResult(
            success=False,
            error=f"Failed to load sprint plan: {str(e)}"
        )


async def _run_backlog_triaging(params: Dict[str, Any]) -> SkillResult:
    """
    Handle backlog triaging skill - intelligent routing based on query intent.
    
    For simple health queries (e.g., "show backlog health of XOPS 25"):
        - Executes backlog analysis directly without showing UI
        - Fetches backlog items from Azure DevOps
        - Calculates health metrics and AI-estimates missing effort
        - Returns formatted health report
    
    For assignment/planning queries:
        - Shows UI form for configuration
    
    Args:
        params: Dictionary with optional keys:
            - team: Team name or area identifier (e.g., "XOPS 25")
            - area_path: Full Azure DevOps area path
            - project: Project name (default: FracPro-OPS)
            - effort_field: Effort field name (default: Custom.Effort3P)
            - velocity: Team velocity override
            - query: Original user query for intent detection
    
    Returns:
        SkillResult with health report or UI form request
    """
    try:
        # Get the original user query for intent detection
        user_query = params.get("query", "").lower()
        team = params.get("team", "")
        
        logger.info(f"[BACKLOG] ========== TEAM EXTRACTION DEBUG ==========")
        logger.info(f"[BACKLOG] params.keys(): {list(params.keys())}")
        logger.info(f"[BACKLOG] params.get('query'): '{params.get('query', 'MISSING')}'")
        logger.info(f"[BACKLOG] params.get('team'): '{team}'")
        logger.info(f"[BACKLOG] Original user_query: '{params.get('query', '')}', lowercase: '{user_query}', team param: '{team}'")
        
        # RULE 1: If query explicitly asks for "form", show the UI form
        form_keywords = ["form", "triaging form", "triage form", "backlog form"]
        is_form_request = any(kw in user_query for kw in form_keywords)
        
        if is_form_request:
            logger.info("User explicitly requested backlog triaging form - showing UI")
            return SkillResult(
                success=True,
                result={
                    "requires_ui_form": True,
                    "skill": "backlog_triaging",
                    "auto_grant_access": True,
                    "open_ui_directly": True,
                    "message": "📋 Please use the Backlog Triaging form below to run the analysis."
                },
                message="📋 Please use the Backlog Triaging form below to run the analysis.",
                metadata={
                    "requires_ui_form": True,
                    "skill": "backlog_triaging",
                    "auto_grant_access": True,
                    "open_ui_directly": True
                }
            )
        
        # RULE 2: Otherwise, execute backlog health analysis directly
        # Team name should come from params (extracted by LLM planner) or query
        if not team:
            # Try to get from original query - LLM should have extracted this
            # but if it didn't, we'll do basic extraction as fallback
            original_query = params.get("query", "")
            if original_query:
                logger.info(f"[BACKLOG] No team in params, checking query: '{original_query}'")
                # Simple fallback: look for capitalized words that might be team names
                # This is a last resort - LLM should handle extraction
                import re
                # Match team names that start with capital and may have multiple words
                # Look for patterns after "for" or "of"
                match = re.search(r'(?:for|of)\s+([A-Z][A-Za-z0-9]*(?:\s+[A-Za-z0-9]+)*)', original_query)
                if match:
                    team = match.group(1).strip()
                    logger.info(f"[BACKLOG] Fallback extraction found team: '{team}'")
                else:
                    logger.info(f"[BACKLOG] No team found in query, will analyze entire project")
        
        # Now execute the backlog health analysis directly
        if True:  # Always execute directly unless form was requested
            logger.info(f"Backlog health query detected - executing direct analysis for team: {team or 'auto-detect'}")
            
            # Import the backlog triaging script
            import sys
            from pathlib import Path
            REPO_ROOT = Path(__file__).resolve().parents[2]
            sys.path.insert(0, str(REPO_ROOT / "scripts"))
            
            try:
                from backlog_triaging import load_config, run_backlog_triaging
                from utilities.mcp.pat import get_pat
                
                # Get configuration
                config = load_config()
                
                # Set environment variables for ADO if team was extracted
                project = params.get("project", "FracPro-OPS")
                logger.info(f"[BACKLOG] Setting ADO_TEAM='{team}', ADO_PROJECT='{project}'")
                if team:
                    os.environ["ADO_TEAM"] = team
                    logger.info(f"[BACKLOG] Environment variable ADO_TEAM set to: '{os.environ.get('ADO_TEAM')}'")
                if project:
                    os.environ["ADO_PROJECT"] = project
                
                # Run the backlog triaging analysis
                # Use force_send=False so it only emails if thin, but we always get the report
                options = {
                    "use_dummy_data": False,
                    "force_send": False,  # Don't send email, just analyze
                    "test_mode": False
                }
                
                result = await run_backlog_triaging(config, options, recipients=[])
                
                if not result.get("success", False):
                    error_msg = result.get("message", "Unknown error")
                    return SkillResult(
                        success=False,
                        error=f"Backlog analysis failed: {error_msg}"
                    )
                
                # Extract health analysis and backlog data
                health_analysis = result.get("health_analysis", {})
                is_thin = result.get("is_thin", False)
                backlog_data = result.get("backlog_data", {})
                items = backlog_data.get("items", [])
                
                # Build formatted markdown report
                backlog_items = health_analysis.get("backlog_items", 0)
                total_story_points = health_analysis.get("total_story_points", 0)
                velocity = health_analysis.get("velocity", 0)
                backlog_runway = health_analysis.get("backlog_runway_sprints", 0)
                
                # Determine status
                if backlog_runway >= 3:
                    status = "🟢 HEALTHY"
                    status_text = "healthy"
                elif backlog_runway >= 2:
                    status = "🟡 WARNING"
                    status_text = "moderate"
                else:
                    status = "🔴 THIN"
                    status_text = "thin"
                
                # Build formatted report matching the user's screenshot
                report_lines = [
                    f"# Backlog Health Report for {team or project}",
                    f"",
                    f"**Status:** {status}",
                    f"",
                    f"## Executive Summary",
                    f"",
                    f"Your backlog is currently running **{status_text}** with **{backlog_runway:.1f} sprints** of work remaining. "
                ]
                
                if is_thin:
                    report_lines.append(f"⚠️ **It is crucial to add more items to ensure continued productivity.**")
                
                report_lines.extend([
                    f"",
                    f"## 📊 Key Metrics",
                    f"",
                    f"- **Total Items:** {backlog_items}",
                    f"- **Total Effort:** {total_story_points:.1f} story points",
                    f"- **Team Velocity:** {velocity:.1f} points/sprint",
                    f"- **Backlog Runway:** {backlog_runway:.1f} sprints",
                    f"",
                    f"## 🏃 Velocity Analysis",
                    f"",
                    f"- **Average Velocity:** {velocity:.1f} points/sprint",
                    f"- **Based on:** 3 sprint(s)",
                    f"- **Trend:** {health_analysis.get('velocity_trend', 'Stable')}",
                    f"",
                ])
                
                # Add backlog items section (show up to 20 items)
                if items:
                    # Sort by story points descending to show most impactful items first
                    sorted_items = sorted(items, key=lambda x: x.get('story_points', 0), reverse=True)
                    
                    report_lines.extend([
                        f"## 📋 Current Backlog Items",
                        f"",
                    ])
                    
                    # Show up to 20 items
                    display_count = min(20, len(sorted_items))
                    for i, item in enumerate(sorted_items[:display_count], 1):
                        item_id = item.get('id', 'N/A')
                        title = item.get('title', 'Untitled')[:80]
                        effort = item.get('story_points', 0)
                        state = item.get('state', 'Unknown')
                        is_estimated = item.get('is_estimated', False)
                        
                        # Add AI badge for estimated items
                        effort_display = f"{effort:.0f} pts" if effort > 0 else "Not estimated"
                        if is_estimated and effort > 0:
                            effort_display += " ✨(AI)"
                        
                        report_lines.append(f"{i}. **#{item_id}** - {title}")
                        report_lines.append(f"   - **Effort:** {effort_display} | **State:** {state}")
                        report_lines.append(f"")
                    
                    # Show summary if there are more items
                    if len(sorted_items) > 20:
                        remaining = len(sorted_items) - 20
                        report_lines.append(f"*...and {remaining} more user stories*")
                        report_lines.append(f"")
                
                # Add recommendations based on health
                if is_thin or backlog_runway < 3:
                    report_lines.extend([
                        f"## ⚠️ Recommendations",
                        f"",
                        f"To improve backlog health, consider the following actions:",
                        f"",
                        f"1. **Immediate Brainstorming Session**",
                        f"   - **Action:** Conduct a session with the product team to generate potential backlog items",
                        f"   - **Impact:** Quickly populates the backlog with new ideas and ensures alignment with strategic objectives",
                        f"",
                        f"2. **Review and Refine Existing Ideas**",
                        f"   - **Action:** Identify any unrefined or low-priority items that could be elevated or broken down",
                        f"   - **Impact:** Increases the number of ready-to-work items without needing entirely new concepts",
                        f"",
                    ])
                
                report = "\n".join(report_lines)
                
                return SkillResult(
                    success=True,
                    result={
                        "report": report,
                        "total_items": backlog_items,
                        "total_effort": total_story_points,
                        "backlog_runway": backlog_runway,
                        "is_thin": is_thin
                    },
                    message=report,
                    metadata={
                        "skill": "backlog_triaging",
                        "team": team or project,
                        "status": status,
                        "health_analysis": health_analysis
                    }
                )
                
            except Exception as e:
                logger.error(f"Error executing backlog health analysis: {e}", exc_info=True)
                return SkillResult(
                    success=False,
                    error=f"Failed to analyze backlog health: {str(e)}"
                )
    except Exception as e:
        logger.error(f"Error in backlog_triaging: {e}", exc_info=True)
        return SkillResult(
            success=False,
            error=f"Failed to initialize backlog triaging: {str(e)}"
        )


async def _run_capacity_triaging(params: Dict[str, Any]) -> SkillResult:
    """
    Handle capacity triaging skill - requires UI form for input.
    
    This skill returns a special response indicating that a UI form is needed.
    The actual capacity triaging happens through the UI form.
    """
    try:
        return SkillResult(
            success=True,
            result={
                "requires_ui_form": True,
                "skill": "capacity_triaging",
                "auto_grant_access": True,
                "open_ui_directly": True,
                "message": "📊 Please use the Capacity Triaging form to configure and run the capacity analysis."
            },
            message="📊 Please use the Capacity Triaging form to configure and run the capacity analysis.",
            metadata={
                "requires_ui_form": True,
                "skill": "capacity_triaging",
                "auto_grant_access": True,
                "open_ui_directly": True
            }
        )
    except Exception as e:
        logger.error(f"Error in capacity_triaging: {e}", exc_info=True)
        return SkillResult(
            success=False,
            error=f"Failed to initialize capacity triaging: {str(e)}"
        )


async def _run_backlog_assignments(params: Dict[str, Any]) -> SkillResult:
    """
    Handle backlog assignments skill - loads and displays actual assignment data.
    
    This skill loads the latest backlog assignments and displays them as a table,
    similar to how developer_skills works.
    
    Params (optional):
        - sprint_name: Sprint name to filter by
    """
    try:
        # Import chat service to load backlog data
        import sys
        from pathlib import Path
        import pandas as pd
        
        app_path = Path(__file__).parent.parent.parent / "app"
        if str(app_path) not in sys.path:
            sys.path.insert(0, str(app_path))
        
        from chat_service import load_latest_backlog_assignments_csv, get_backlog_summary
        
        # Load the latest backlog assignments CSV
        csv_path = load_latest_backlog_assignments_csv()
        
        if not csv_path or not csv_path.exists():
            return SkillResult(
                success=True,
                result={
                    "message": "📋 No backlog assignments found. Generate assignments using the Backlog Assignments tool.",
                    "data": []
                },
                message="📋 No backlog assignments found. Generate assignments using the Backlog Assignments tool."
            )
        
        # Load CSV data
        df = pd.read_csv(csv_path, encoding='utf-8')
        summary = get_backlog_summary(df)
        
        # Format the response with data table
        result = {
            "message": "📋 **Backlog Assignments Report**",
            "summary": {
                "total_items": summary.get("parent_items", summary["total"]),
                "total_subtasks": summary["total"],
                "fe_assigned": summary["fe_assigned"],
                "be_assigned": summary["be_assigned"],
                "cross_role": summary["cross_role"]
            },
            "assignments": df.to_dict('records')[:20]  # First 20 rows
        }
        
        # Build formatted message
        message_parts = [
            "📋 **Backlog Assignments Report**\n",
            f"**Summary:**",
            f"- Work Items: {summary.get('parent_items', summary['total'])}",
            f"- Subtasks: {summary['total']}",
            f"- FE Assigned: {summary['fe_assigned']}",
            f"- BE Assigned: {summary['be_assigned']}",
            f"- Cross-Role: {summary['cross_role']}\n"
        ]
        
        # Show sample assignments
        if len(df) > 0:
            message_parts.append("**Sample Assignments (first 5):**")
            for idx, row in df.head(5).iterrows():
                task_name = row.get('Task Name', 'Unknown')
                fe = row.get('Responsible - Frontend', 'N/A')
                be = row.get('Responsible - Backend', 'N/A')
                hours = row.get('Estimated Hours', 'N/A')
                message_parts.append(f"- {task_name}: FE={fe}, BE={be}, {hours}h")
            
            if len(df) > 5:
                message_parts.append(f"\n... and {len(df) - 5} more assignments")
        
        return SkillResult(
            success=True,
            result=result,
            message="\n".join(message_parts)
        )
    except Exception as e:
        logger.error(f"Error in backlog_assignments: {e}", exc_info=True)
        return SkillResult(
            success=False,
            error=f"Failed to load backlog assignments: {str(e)}"
        )


async def _run_change_ado_assignee(params: Dict[str, Any]) -> SkillResult:
    """
    Change assignee of an ADO work item.
    
    Params:
        - work_item_id: Work item ID (required)
        - assignee: New assignee email (required)
        - comment: Optional comment
    """
    try:
        work_item_id = params.get("work_item_id")
        assignee = params.get("assignee")
        comment = params.get("comment", "")
        
        if not work_item_id:
            return SkillResult(success=False, error="work_item_id is required")
        if not assignee:
            return SkillResult(success=False, error="assignee email is required")
        
        pm_agent = get_pm_agent()
        if not pm_agent:
            return SkillResult(success=False, error="PM Agent not available")
        
        # Use PM Agent's MCP connector to update work item
        update_args = {
            "id": int(work_item_id),
            "assignedTo": assignee
        }
        if comment:
            update_args["comment"] = comment
        
        result = await pm_agent.mcp_connector.call_tool("wit_update_work_item", update_args)
        
        if result:
            return SkillResult(
                success=True,
                result={
                    "work_item_id": work_item_id,
                    "new_assignee": assignee,
                    "message": f"Work item #{work_item_id} reassigned to {assignee}"
                }
            )
        else:
            return SkillResult(success=False, error="Failed to update work item")
        
    except Exception as e:
        logger.exception(f"Change ADO assignee error: {e}")
        return SkillResult(success=False, error=str(e))


async def _run_detect_recurring_bugs(params: Dict[str, Any]) -> SkillResult:
    """
    Detect recurring bugs by area path. Alias for bug_areas_highlight.
    
    Params:
        - project: ADO project name
        - lookback_days: Days to look back (default 60)
        - recurrence_threshold: Min bugs to consider recurring (default 3)
    """
    # Delegate to bug_areas_highlight with send_email=False for detection only
    detection_params = {**params, "send_email": False, "preview_only": True}
    return await _run_bug_areas_highlight(detection_params)


async def _run_billing_deviation(params: Dict[str, Any]) -> SkillResult:
    """
    Calculate and generate billing deviation report using granular tools.
    
    Integrates the tool breakdown architecture:
    1. parse_billing_query
    2. prompt_for_target_hours
    3. fetch_work_items_by_billing_date
    4. calculate_billing_deviation
    5. generate_billing_summary_text
    6. generate_detailed_billing_report
    7. send_billing_report_email
    
    Params:
        - area_path: Area path to filter (optional)
        - target_hours: Target hours (required if area_path provided)
        - recipient_email: Email to send report to (optional)
        - month: Month number 1-12 (optional)
        - year: Year (optional)
        - query: Original user query
    
    Returns:
        SkillResult with deviation calculation and report
    """
    try:
        from billing_deviation.tools_breakdown import (
            parse_billing_query,
            prompt_for_target_hours,
            fetch_work_items_by_billing_date,
            calculate_billing_deviation,
            generate_billing_summary_text,
            generate_detailed_billing_report,
            send_billing_report_email
        )
        
        # 1. Parse Query & Parameters
        original_query = params.get("query", "").strip()
        logger.info(f"Tool-based Billing Deviation: Processing query='{original_query}'")
        
        # Use tool to parse query (extracted info might be overridden by explicit params)
        parsed_query = parse_billing_query(original_query) if original_query else {}
        
        # Resolve 'area_path'
        # Explicit param > Parsed from query
        area_path_param = params.get("area_path", "").strip()
        if area_path_param:
            area_path = area_path_param
        elif parsed_query.get("has_area_path"):
            area_path = parsed_query.get("area_path")
        else:
            area_path = None
            
        # Resolve 'month' and 'year'
        # Param > Parsed > Default (handled by tools)
        month = params.get("month") or parsed_query.get("month")
        year = params.get("year") or parsed_query.get("year")
        
        # 2. Determine Target Hours Strategy
        # The tool tells us if we need to ask the user
        strategy = prompt_for_target_hours(area_path)
        logger.info(f"Target Hours Strategy: {strategy['action']}")
        
        target_hours_param = params.get("target_hours")
        target_hours = None
        
        if strategy["action"] == "ask_user":
            # We strictly need target hours for specific areas
            if target_hours_param:
                try:
                    target_hours = float(target_hours_param)
                except (ValueError, TypeError):
                     return SkillResult(success=False, error=f"Invalid target_hours: {target_hours_param}")
            else:
                # Need to ask user - return prompt
                return SkillResult(
                    success=False,
                    error=f"Target hours required for {area_path}",
                    message=strategy["message"],
                    metadata={
                        "needs_target_hours": True,
                        "area_paths": [area_path],
                        "prompt": strategy["message"],
                        "pending_billing_deviation": {
                            "area_path": area_path,
                            "skill": "billing_deviation",
                            "month": month,
                            "year": year
                        }
                    }
                )
        else:
            # use_default strategy
            # Note: Even if use_default, user might have provided explicit target hours in params
            if target_hours_param:
                try:
                    target_hours = float(target_hours_param)
                except:
                     target_hours = strategy["target_hours"]
            else:
                target_hours = strategy["target_hours"]
        
        # 3. Fetch Work Items
        logger.info(f"Fetching work items: month={month}, area={area_path}")
        # Note: Valid states for billing are normally Closed/Completed. The tool defaults to 'Closed'.
        work_data = fetch_work_items_by_billing_date(
            month=month,
            year=year,
            area_path=area_path,
            state="Closed" 
        )
        
        # 4. Calculate Deviation
        calc_result = calculate_billing_deviation(
            target_hours=target_hours,
            actual_hours=work_data["total_actual_hours"],
            area_path=area_path
        )
        
        # 5. Generate Text Summary
        text_summary = generate_billing_summary_text(
            deviation_result=calc_result,
            work_item_details=work_data
        )
        
        # 6. Generate Detailed Reports (HTML/CSV)
        report_files = generate_detailed_billing_report(
            deviation_result=calc_result,
            work_item_details=work_data,
            format="both"
        )
        
        # 7. Send Email (if requested)
        recipient_email = params.get("recipient_email", "").strip()
        email_result = None
        
        if recipient_email:
            logger.info(f"Sending email to {recipient_email}")
            email_result = send_billing_report_email(
                recipient_email=recipient_email,
                text_summary=text_summary,
                html_report=report_files.get("html_report"),
                csv_path=report_files.get("csv_path"),
                subject=f"Billing Deviation Report - {area_path or 'All Areas'}"
            )
        else:
            logger.info("No recipient email provided, skipping email.")

        # Construct Validation/Output
        # We return the text summary as the primary result
        
        full_result = {
            "summary": text_summary,
            "deviation": calc_result,
            "reports": report_files,
            "email": email_result
        }
        
        return SkillResult(
            success=True,
            result=text_summary,  # The main text to display
            data=full_result,     # Structured data for UI/Agent
            metadata={
                "area_path": area_path,
                "target_hours": target_hours,
                "period": work_data.get("month_range"),
                "status": calc_result["status"],
                "tool_breakdown_used": True
            }
        )
        
    except Exception as e:
        logger.exception(f"Tool-based billing deviation failed: {e}")
        return SkillResult(success=False, error=str(e))


async def _run_workitem_comment_summary(params: Dict[str, Any]) -> SkillResult:
    """
    Get a comprehensive summary of a work item including comments, updates, and attachments.
    Returns an LLM-ready text summary suitable for context injection.
    
    Params:
        - work_item_id: Work item ID (required)
        - project: ADO project name (optional)
        - include_attachments: Whether to include attachment metadata (default True)
        - include_updates: Whether to include field change history (default True)
        - max_comments: Maximum number of comments to include (default 50)
    """
    try:
        work_item_id = params.get("work_item_id")
        if not work_item_id:
            return SkillResult(success=False, error="work_item_id is required")
        
        project = params.get("project")
        include_attachments = params.get("include_attachments", True)
        include_updates = params.get("include_updates", True)
        max_comments = params.get("max_comments", 50)
        
        pm_agent = get_pm_agent()
        if not pm_agent:
            return SkillResult(success=False, error="PM Agent not available")
        
        # Fetch work item details
        try:
            wi_result = await pm_agent.mcp_connector.call_tool("wit_get_work_item", {
                "id": int(work_item_id),
                "expand": "all"
            })
            
            if not wi_result:
                return SkillResult(success=False, error=f"Work item #{work_item_id} not found")
            
            wi_data = json.loads(wi_result) if isinstance(wi_result, str) else wi_result
            fields = wi_data.get("fields", {})
            
        except Exception as e:
            logger.error(f"Failed to fetch work item {work_item_id}: {e}")
            return SkillResult(success=False, error=f"Failed to fetch work item: {str(e)}")
        
        # Fetch comments
        comments_list = []
        try:
            comments_args = {"id": int(work_item_id), "top": max_comments}
            if project:
                comments_args["project"] = project
                
            comments_result = await pm_agent.mcp_connector.call_tool("wit_get_work_item_comments", comments_args)
            
            if comments_result:
                comments_data = json.loads(comments_result) if isinstance(comments_result, str) else comments_result
                # Handle both {"comments": [...]} and {"value": [...]} formats
                comments_list = comments_data.get("comments", comments_data.get("value", []))
                
        except Exception as e:
            logger.warning(f"Failed to fetch comments for {work_item_id}: {e}")
            comments_list = []
        
        # Fetch attachments metadata
        attachments_list = []
        if include_attachments:
            try:
                attach_args = {"id": int(work_item_id)}
                if project:
                    attach_args["project"] = project
                    
                attach_result = await pm_agent.mcp_connector.call_tool("wit_get_work_item_attachments", attach_args)
                
                if attach_result:
                    attach_data = json.loads(attach_result) if isinstance(attach_result, str) else attach_result
                    attachments_list = attach_data.get("value", attach_data.get("attachments", []))
                    
            except Exception as e:
                logger.warning(f"Failed to fetch attachments for {work_item_id}: {e}")
                attachments_list = []
        
        # Fetch updates/revisions (field change history)
        updates_list = []
        if include_updates:
            try:
                updates_args = {"id": int(work_item_id), "top": 50}
                if project:
                    updates_args["project"] = project
                    
                updates_result = await pm_agent.mcp_connector.call_tool("wit_get_work_item_updates", updates_args)
                
                if updates_result:
                    updates_data = json.loads(updates_result) if isinstance(updates_result, str) else updates_result
                    updates_list = updates_data.get("value", updates_data.get("updates", []))
                    
            except Exception as e:
                logger.warning(f"Failed to fetch updates for {work_item_id}: {e}")
                updates_list = []
        
        # Build LLM-ready summary
        summary_lines = []
        summary_lines.append(f"# Work Item #{work_item_id} Summary")
        summary_lines.append("")
        
        # Core fields
        summary_lines.append("## Core Details")
        summary_lines.append(f"- **Title**: {fields.get('System.Title', 'N/A')}")
        summary_lines.append(f"- **Type**: {fields.get('System.WorkItemType', 'N/A')}")
        summary_lines.append(f"- **State**: {fields.get('System.State', 'N/A')}")
        summary_lines.append(f"- **Assigned To**: {fields.get('System.AssignedTo', {}).get('displayName', 'Unassigned')}")
        summary_lines.append(f"- **Area Path**: {fields.get('System.AreaPath', 'N/A')}")
        summary_lines.append(f"- **Iteration Path**: {fields.get('System.IterationPath', 'N/A')}")
        summary_lines.append(f"- **Created**: {fields.get('System.CreatedDate', 'N/A')}")
        summary_lines.append(f"- **Changed**: {fields.get('System.ChangedDate', 'N/A')}")
        summary_lines.append("")
        
        # Description
        if "System.Description" in fields:
            summary_lines.append("## Description")
            # Strip HTML tags for LLM readability
            import re
            desc = fields["System.Description"] or ""
            desc_text = re.sub(r'<[^>]+>', '', desc).strip()
            summary_lines.append(desc_text or "(Empty)")
            summary_lines.append("")
        
        # Comments
        if comments_list:
            summary_lines.append(f"## Comments ({len(comments_list)})")
            for i, comment in enumerate(comments_list[:max_comments], 1):
                text = comment.get("text", "")
                author = comment.get("revisedBy", {}).get("displayName", "Unknown")
                date = comment.get("revisedDate", "Unknown date")
                summary_lines.append(f"### Comment {i} by {author} on {date}")
                summary_lines.append(text)
                summary_lines.append("")
        else:
            summary_lines.append("## Comments")
            summary_lines.append("(No comments)")
            summary_lines.append("")
        
        # Attachments
        if include_attachments and attachments_list:
            summary_lines.append(f"## Attachments ({len(attachments_list)})")
            for attach in attachments_list:
                name = attach.get("name", "Unknown")
                url = attach.get("url", "")
                summary_lines.append(f"- {name} ({url})")
            summary_lines.append("")
        elif include_attachments:
            summary_lines.append("## Attachments")
            summary_lines.append("(No attachments)")
            summary_lines.append("")
        
        # Updates (field changes)
        if include_updates and updates_list:
            summary_lines.append(f"## Recent Field Changes ({min(10, len(updates_list))} most recent)")
            for update in updates_list[:10]:  # Limit to 10 most recent
                rev = update.get("rev", "?")
                by = update.get("revisedBy", {}).get("displayName", "Unknown")
                date = update.get("revisedDate", "Unknown date")
                fields_changed = update.get("fields", {})
                
                if fields_changed:
                    changes_str = ", ".join([f"{k}: {v.get('oldValue', 'N/A')} → {v.get('newValue', 'N/A')}" 
                                             for k, v in fields_changed.items()])
                    summary_lines.append(f"- **Rev {rev}** by {by} on {date}: {changes_str}")
            summary_lines.append("")
        elif include_updates:
            summary_lines.append("## Recent Field Changes")
            summary_lines.append("(No updates)")
            summary_lines.append("")
        
        summary_text = "\n".join(summary_lines)
        
        return SkillResult(
            success=True,
            result={
                "work_item_id": work_item_id,
                "title": fields.get("System.Title", "N/A"),
                "type": fields.get("System.WorkItemType", "N/A"),
                "state": fields.get("System.State", "N/A"),
                "comments_count": len(comments_list),
                "attachments_count": len(attachments_list),
                "updates_count": len(updates_list),
                "summary": summary_text
            },
            metadata={
                "work_item_id": work_item_id,
                "project": project,
                "include_attachments": include_attachments,
                "include_updates": include_updates
            }
        )
        
    except Exception as e:
        logger.exception(f"Work item comment summary error: {e}")
        return SkillResult(success=False, error=str(e))


async def _run_wiql_query(params: Dict[str, Any]) -> SkillResult:
    """
    Execute WIQL query against Azure DevOps.

    Builds WIQL dynamically from structured parameters and delegates
    to the wiql_skill module for actual REST API execution.

    Params:
        - query: Natural language query (used for date intent detection)
        - date_filter: e.g. "last 7 days", "last month", "this week", "between 2026-01-15 and 2026-02-10"
        - priority: 1-4, High/Medium/Low, P1-P4
        - work_item_type: Bug, Task, User Story, etc.
        - state: Single state or comma-separated/list
        - assignedTo: User display name or email
        - project: ADO project name
        - team: Team name (for area path resolution)
        - raw_wiql: Pre-built WIQL (overrides all other params)
    """
    import re as _re
    from datetime import datetime, timedelta

    try:
        from agents.pm_agent.pm_skills.wiql_skill import execute_wiql as _direct_wiql_execute
        from utilities.wiql.builder import WIQLBuilder
        from config import config as app_config

        project = params.get("project") or getattr(app_config, "ado_project", "FracPro-OPS")
        raw_wiql = params.get("raw_wiql")
        query_text = params.get("query", "")

        # ── Extract LLM-generated WIQL from orchestrator plan ──────────
        # The Light LLM Planner generates correct WIQL in the plan step args,
        # but it arrives in orchestrator_plan rather than directly in params.
        # Check multiple locations where the WIQL may be stored:
        if not raw_wiql:
            # 1. Direct 'wiql' key in params (from plan step args merge)
            raw_wiql = params.get("wiql")
            if raw_wiql:
                logger.info("[WIQL_SKILL] Found WIQL in params['wiql']")

        if not raw_wiql:
            # 2. orchestrator_plan.skill_params.wiql (set by light_planner post-validation)
            orch_plan = params.get("orchestrator_plan")
            if isinstance(orch_plan, dict):
                skill_params = orch_plan.get("skill_params", {})
                if isinstance(skill_params, dict) and skill_params.get("wiql"):
                    raw_wiql = skill_params["wiql"]
                    logger.info("[WIQL_SKILL] Extracted WIQL from orchestrator_plan.skill_params")
                    # Also extract top from skill_params if available
                    if skill_params.get("top"):
                        params.setdefault("top", skill_params["top"])

        if not raw_wiql:
            # 3. orchestrator_plan.plan.steps[0].args.wiql (from LLM plan structure)
            orch_plan = params.get("orchestrator_plan")
            if isinstance(orch_plan, dict):
                plan_data = orch_plan.get("plan")
                if isinstance(plan_data, dict):
                    steps = plan_data.get("steps", [])
                    if steps and isinstance(steps[0], dict):
                        step_args = steps[0].get("args", {})
                        if isinstance(step_args, dict) and step_args.get("wiql"):
                            raw_wiql = step_args["wiql"]
                            logger.info("[WIQL_SKILL] Extracted WIQL from orchestrator_plan.plan.steps[0].args")
                            if step_args.get("top"):
                                params.setdefault("top", step_args["top"])

        # If raw WIQL provided (from any source), execute directly
        if raw_wiql:
            logger.info(f"[WIQL_SKILL] Executing raw WIQL directly (bypassing WIQLBuilder)")
            max_results = int(params.get("top", 1000))
            result = await _direct_wiql_execute(project=project, wiql=raw_wiql, top=max_results)
            return _wiql_result_to_skill_result(result, raw_wiql)

        # Build WIQL from structured parameters
        builder = WIQLBuilder().select().where_project(project)

        # Work item type — from params or extracted from query text
        wit = params.get("work_item_type")
        if not wit and query_text:
            # Extract work item type from query text
            wit_patterns = {
                r'\bbugs?\b': 'Bug',
                r'\btasks?\b': 'Task',
                r'\buser\s+stor(?:y|ies)\b': 'User Story',
                r'\bfeatures?\b': 'Feature',
                r'\bepics?\b': 'Epic',
                r'\bissues?\b': 'Issue',
            }
            for pattern, wit_type in wit_patterns.items():
                if _re.search(pattern, query_text.lower()):
                    wit = wit_type
                    break
        if wit:
            if isinstance(wit, list):
                builder.where_work_item_types(wit)
            else:
                builder.where_work_item_type(str(wit))

        # State filter
        state = params.get("state")
        if state:
            if isinstance(state, list):
                builder.where_state_in(state)
            elif "," in str(state):
                builder.where_state_in([s.strip() for s in str(state).split(",")])
            else:
                builder.where_state(str(state))

        # Priority filter — supports single or multiple values
        priority = params.get("priority")
        pri_values = []
        if priority is not None:
            # Handle list of priorities (e.g. [1, 2])
            if isinstance(priority, (list, tuple)):
                for p in priority:
                    v = _normalize_priority(p)
                    if v:
                        pri_values.append(v)
            else:
                v = _normalize_priority(priority)
                if v:
                    pri_values.append(v)
        
        # Also try to extract priorities from query text (e.g. "priority 1 or 2")
        if not pri_values and query_text:
            pri_match = _re.findall(r'\bpriority\s+(\d)\b|\b[pP](\d)\b', query_text.lower())
            for m in pri_match:
                d = m[0] or m[1]
                if d and 1 <= int(d) <= 4:
                    pri_values.append(int(d))
            # Also check "priority 1 or 2" pattern
            or_match = _re.search(r'\bpriority\s+(\d)\s+or\s+(\d)', query_text.lower())
            if or_match:
                for g in or_match.groups():
                    if g and 1 <= int(g) <= 4 and int(g) not in pri_values:
                        pri_values.append(int(g))
        
        if pri_values:
            pri_values = sorted(set(pri_values))
            if len(pri_values) == 1:
                builder.where_custom(f"[Microsoft.VSTS.Common.Priority] = {pri_values[0]}")
            else:
                in_clause = ", ".join(str(p) for p in pri_values)
                builder.where_custom(f"[Microsoft.VSTS.Common.Priority] IN ({in_clause})")

        # Assigned to
        assigned = params.get("assignedTo") or params.get("assigned_to")
        if assigned:
            builder.where_assigned_to(str(assigned))

        # Area path / team - only apply if explicitly requested
        # Don't auto-filter by team for broad WIQL queries (team comes from default context)
        area = params.get("area_path")
        if area:
            builder.where_area_under(area)
        # Only apply team filter if explicitly mentioned in query or params
        # (not from default context that always sets "team")
        elif params.get("filter_by_team") and params.get("team"):
            team_area = f"{project}\\{params['team']}"
            builder.where_area_under(team_area)

        # Date filter — the core differentiator for WIQL
        date_filter = params.get("date_filter", "")
        _apply_date_filter(builder, date_filter, query_text)

        # Order by most relevant date
        date_type = _detect_date_type(query_text)
        if date_type == "closed":
            if not state:
                builder.where_state_in(["Closed", "Done", "Resolved"])
            builder.order_by("Microsoft.VSTS.Common.ClosedDate", desc=True)
        elif date_type == "created":
            builder.order_by("System.CreatedDate", desc=True)
        else:
            builder.order_by("System.ChangedDate", desc=True)

        wiql_query = builder.build()
        logger.info(f"[WIQL_SKILL] Built WIQL: {wiql_query[:200]}")

        # Limit results to prevent ADO VS402337 error (>20K items)
        max_results = int(params.get("top", 1000))
        result = await _direct_wiql_execute(project=project, wiql=wiql_query, top=max_results)
        return _wiql_result_to_skill_result(result, wiql_query)

    except Exception as e:
        logger.exception(f"[WIQL_SKILL] Error: {e}")
        return SkillResult(success=False, error=str(e))


def _wiql_result_to_skill_result(result: Dict[str, Any], wiql_query: str) -> SkillResult:
    """Convert wiql_skill result dict to SkillResult."""
    if not result.get("success", False):
        return SkillResult(
            success=False,
            error=result.get("error", "WIQL query failed"),
            result={"wiql_query": wiql_query}
        )

    items = result.get("items", [])
    count = result.get("count", len(items))

    # Flatten work item fields for easier consumption
    flat_items = []
    for wi in items:
        flat = {"id": wi.get("id")}
        fields = wi.get("fields", {})
        flat["Title"] = fields.get("System.Title", "")
        flat["State"] = fields.get("System.State", "")
        flat["WorkItemType"] = fields.get("System.WorkItemType", "")
        flat["Priority"] = fields.get("Microsoft.VSTS.Common.Priority", "")
        assigned = fields.get("System.AssignedTo", "")
        if isinstance(assigned, dict):
            assigned = assigned.get("displayName", str(assigned))
        flat["AssignedTo"] = assigned
        flat["AreaPath"] = fields.get("System.AreaPath", "")
        flat["IterationPath"] = fields.get("System.IterationPath", "")
        flat["CreatedDate"] = fields.get("System.CreatedDate", "")
        flat["ChangedDate"] = fields.get("System.ChangedDate", "")
        flat_items.append(flat)

    return SkillResult(
        success=True,
        result={
            "work_items": flat_items,
            "count": count,
            "wiql_query": wiql_query
        },
        message=f"Found {count} work item(s)."
    )


def _normalize_priority(priority) -> Optional[int]:
    """Convert priority value to integer 1-4."""
    if priority is None:
        return None
    p = str(priority).strip().lower()
    mapping = {
        "1": 1, "2": 2, "3": 3, "4": 4,
        "p1": 1, "p2": 2, "p3": 3, "p4": 4,
        "critical": 1, "high": 2, "medium": 3, "low": 4,
    }
    return mapping.get(p, int(p) if p.isdigit() and 1 <= int(p) <= 4 else None)


def _detect_date_type(query: str) -> str:
    """Detect which date field the query refers to."""
    import re as _re
    q = query.lower()
    if _re.search(r'\b(closed|completed|resolved|done|finished)\b', q):
        return "closed"
    if _re.search(r'\b(created|opened|filed|new)\b', q):
        return "created"
    return "changed"  # default for updated/modified/changed


def _apply_date_filter(builder, date_filter: str, query_text: str):
    """Apply date filter to WIQLBuilder based on date_filter string or query text.
    
    Handles:
    - Relative: "last 7 days", "this week", "last month", "yesterday"
    - Absolute specific date: "on 4 feb 2026", "on February 4"
    - Absolute month: "in february 2026", "in feb"
    - Range: "between 1 jan 2026 and 15 feb 2026"
    - Before/after: "before march 2026", "after 15 jan 2026"
    
    ADO WIQL date format: ISO 8601 literal dates as 'YYYY-MM-DD' in single quotes.
    """
    import re as _re
    import calendar
    from datetime import datetime, timedelta

    if not date_filter and not query_text:
        return

    df = (date_filter or "").lower().strip()
    q = (query_text or "").lower().strip()
    date_type = _detect_date_type(q or df)

    # Map date_type to field
    field_map = {
        "closed": "Microsoft.VSTS.Common.ClosedDate",
        "created": "System.CreatedDate",
        "changed": "System.ChangedDate",
    }
    date_field = field_map.get(date_type, "System.ChangedDate")

    days = None
    start_date = None
    end_date = None

    combined = f"{df} {q}"

    # ── 1. Parse "between DATE1 and/to DATE2" ──────────────────────────
    between_match = _re.search(
        r'between\s+(.+?)\s+(?:and|to)\s+(.+)',
        combined
    )
    if between_match:
        start_raw = between_match.group(1).strip()
        end_raw = between_match.group(2).strip()
        end_raw = _re.sub(r'\s+(?:in|for|of|from|at|on|the|project|team)\s+.*$', '', end_raw, flags=_re.IGNORECASE)
        start_date = _parse_date_string(start_raw)
        end_date = _parse_date_string(end_raw)
        if start_date and end_date:
            builder.where_date_range(date_field, start=start_date, end=end_date)
            return

    # ── 2. Parse "on <specific date>" (absolute single day) ───────────
    # Matches: "on 4 feb 2026", "on feb 4 2026", "on February 4", "on 4th february 2026"
    on_date_match = _re.search(
        r'\bon\s+(.+?)(?:\s+(?:in|for|of|from|at|the|project|team)\s+|$)',
        combined
    )
    if on_date_match:
        date_str = on_date_match.group(1).strip()
        # Remove trailing non-date words
        date_str = _re.sub(r'\s+(?:in|for|of|from|at|the|project|team).*$', '', date_str, flags=_re.IGNORECASE)
        parsed = _parse_date_string(date_str)
        if parsed:
            # Single day: >= date AND < date+1
            try:
                dt = datetime.strptime(parsed, "%Y-%m-%d")
                next_day = (dt + timedelta(days=1)).strftime("%Y-%m-%d")
                builder.where_date_range(date_field, start=parsed, end=next_day)
                return
            except ValueError:
                pass

    # ── 3. Parse "in <month> [<year>]" (absolute month range) ─────────
    # Matches: "in february 2026", "in feb", "in march", "during february 2026"
    month_names = {
        'january': 1, 'jan': 1, 'february': 2, 'feb': 2, 'march': 3, 'mar': 3,
        'april': 4, 'apr': 4, 'may': 5, 'june': 6, 'jun': 6, 'july': 7, 'jul': 7,
        'august': 8, 'aug': 8, 'september': 9, 'sep': 9, 'october': 10, 'oct': 10,
        'november': 11, 'nov': 11, 'december': 12, 'dec': 12,
    }
    month_pattern = '|'.join(month_names.keys())
    month_match = _re.search(
        rf'\b(?:in|during)\s+({month_pattern})\s*(\d{{4}})?\b',
        combined
    )
    if month_match:
        month_str = month_match.group(1).lower()
        year_str = month_match.group(2)
        month_num = month_names.get(month_str)
        year = int(year_str) if year_str else datetime.utcnow().year
        if month_num:
            first_day = f"{year:04d}-{month_num:02d}-01"
            # Get last day of month
            last_day_num = calendar.monthrange(year, month_num)[1]
            next_month_first = (datetime(year, month_num, last_day_num) + timedelta(days=1)).strftime("%Y-%m-%d")
            builder.where_date_range(date_field, start=first_day, end=next_month_first)
            return

    # ── 4. Try to extract a bare absolute date from the combined text ─
    # This catches patterns like "closed 4 feb 2026" without "on" prefix
    bare_date_patterns = [
        # "4 feb 2026", "4th february 2026"
        rf'\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({month_pattern})\s+(\d{{2,4}})\b',
        # "feb 4 2026", "february 4, 2026"
        rf'\b({month_pattern})\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{2,4}})\b',
        # "4 feb" (no year - assume current)
        rf'\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({month_pattern})\b(?!\s+\d)',
        # "feb 4" (no year - assume current)
        rf'\b({month_pattern})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?!\s+\d)',
    ]
    for bp in bare_date_patterns:
        bare_match = _re.search(bp, combined)
        if bare_match:
            parsed = _parse_date_string(bare_match.group(0))
            if parsed:
                try:
                    dt = datetime.strptime(parsed, "%Y-%m-%d")
                    next_day = (dt + timedelta(days=1)).strftime("%Y-%m-%d")
                    builder.where_date_range(date_field, start=parsed, end=next_day)
                    return
                except ValueError:
                    pass

    # ── 5. Parse relative patterns ─────────────────────────────────────
    text_to_parse = df or q
    if text_to_parse:
        m = _re.search(r'last\s+(\d+)\s*days?', text_to_parse)
        if m:
            days = int(m.group(1))
        elif _re.search(r'last\s+(\d+)\s*weeks?', text_to_parse):
            weeks_m = _re.search(r'last\s+(\d+)\s*weeks?', text_to_parse)
            days = int(weeks_m.group(1)) * 7
        elif _re.search(r'last\s+(\d+)\s*months?', text_to_parse):
            months_m = _re.search(r'last\s+(\d+)\s*months?', text_to_parse)
            days = int(months_m.group(1)) * 30
        elif "last week" in text_to_parse or "last 1 week" in text_to_parse:
            days = 7
        elif "this week" in text_to_parse:
            days = 7
        elif "last month" in text_to_parse or "last 1 month" in text_to_parse:
            days = 30
        elif "this month" in text_to_parse:
            today = datetime.utcnow()
            start_date = today.replace(day=1).strftime("%Y-%m-%d")
            end_date = today.strftime("%Y-%m-%d")
        elif "last year" in text_to_parse:
            days = 365
        elif "this year" in text_to_parse:
            today = datetime.utcnow()
            start_date = today.replace(month=1, day=1).strftime("%Y-%m-%d")
            end_date = today.strftime("%Y-%m-%d")
        elif "yesterday" in text_to_parse:
            days = 1
        elif "recently" in text_to_parse:
            days = 7
        else:
            # ── 6. before/after with flexible date formats ─────────
            before_m = _re.search(r'before\s+(.+?)(?:\s+(?:in|for|project|team)\s+|$)', text_to_parse)
            after_m = _re.search(r'after\s+(.+?)(?:\s+(?:in|for|project|team)\s+|$)', text_to_parse)
            if before_m:
                parsed_before = _parse_date_string(before_m.group(1).strip())
                if parsed_before:
                    end_date = parsed_before
            if after_m:
                parsed_after = _parse_date_string(after_m.group(1).strip())
                if parsed_after:
                    start_date = parsed_after

    # Apply the computed filter
    if days is not None:
        builder.where_date_range(date_field, start=f"@Today - {days}", end="@Today")
    elif start_date or end_date:
        builder.where_date_range(date_field, start=start_date, end=end_date)


def _parse_date_string(s: str) -> Optional[str]:
    """Try to parse various date formats into YYYY-MM-DD.
    
    Handles:
    - ISO: "2026-02-04"
    - Day Month Year: "4 feb 2026", "4th february 2026", "15 january 26"
    - Month Day Year: "feb 4 2026", "February 4, 2026", "feb 4th, 2026"
    - Slash/dash: "04/02/2026", "4-2-2026", "2026/02/04"
    - Day Month (no year → current year): "4 feb", "february 4"
    - Month only: "february", "feb" → first day of that month
    """
    import re as _re
    from datetime import datetime

    if not s:
        return None

    s = s.strip().rstrip(".,:;")
    if not s:
        return None

    # Already ISO
    if _re.match(r'^\d{4}-\d{2}-\d{2}$', s):
        return s

    # Strip ordinal suffixes (15th → 15, 1st → 1, etc.)
    clean = _re.sub(r'(\d+)(st|nd|rd|th)\b', r'\1', s)

    # Handle 2-digit years: "15 january 26" → expand 26 → 2026
    year_2digit = _re.search(r'\b(\d{1,2})\s+(\w+)\s+(\d{2})$', clean)
    if year_2digit:
        yr = int(year_2digit.group(3))
        if yr < 100:
            full_year = 2000 + yr if yr < 70 else 1900 + yr
            clean = clean[:year_2digit.start(3)] + str(full_year)

    # Also handle: "february 4 26" or "feb 4 26"
    year_2digit_v2 = _re.search(r'\b(\w+)\s+(\d{1,2})\s+(\d{2})$', clean)
    if year_2digit_v2 and not year_2digit:
        yr = int(year_2digit_v2.group(3))
        if yr < 100:
            full_year = 2000 + yr if yr < 70 else 1900 + yr
            clean = clean[:year_2digit_v2.start(3)] + str(full_year)

    # Common formats (more comprehensive)
    for fmt in (
        "%d %B %Y", "%d %b %Y",       # 4 February 2026, 4 Feb 2026
        "%B %d %Y", "%B %d, %Y",       # February 4 2026, February 4, 2026
        "%b %d %Y", "%b %d, %Y",       # Feb 4 2026, Feb 4, 2026
        "%d/%m/%Y", "%m/%d/%Y",         # 04/02/2026, 02/04/2026
        "%Y/%m/%d",                      # 2026/02/04
        "%d-%m-%Y", "%m-%d-%Y",         # 04-02-2026, 02-04-2026
        "%d.%m.%Y",                      # 04.02.2026
        "%Y-%m-%dT%H:%M:%S",            # ISO with time
        "%Y-%m-%dT%H:%M:%SZ",           # ISO with time and Z
    ):
        try:
            dt = datetime.strptime(clean.strip(), fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue

    # Try with just month and day (assume current year)
    for fmt in ("%d %B", "%d %b", "%B %d", "%b %d"):
        try:
            dt = datetime.strptime(clean.strip(), fmt)
            dt = dt.replace(year=datetime.utcnow().year)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue

    # Try month name only → first day of that month in current year
    month_names = {
        'january': 1, 'jan': 1, 'february': 2, 'feb': 2, 'march': 3, 'mar': 3,
        'april': 4, 'apr': 4, 'may': 5, 'june': 6, 'jun': 6, 'july': 7, 'jul': 7,
        'august': 8, 'aug': 8, 'september': 9, 'sep': 9, 'october': 10, 'oct': 10,
        'november': 11, 'nov': 11, 'december': 12, 'dec': 12,
    }
    clean_lower = clean.strip().lower()
    if clean_lower in month_names:
        month_num = month_names[clean_lower]
        year = datetime.utcnow().year
        return f"{year:04d}-{month_num:02d}-01"

    return None


# Define all available skills
SKILL_DEFINITIONS: Dict[str, SkillDefinition] = {
    "bug_areas_highlight": SkillDefinition(
        name="bug_areas_highlight",
        description="Detect recurring bugs by area path and send email report with highlighted patterns",
        handler=_run_bug_areas_highlight,
        required_params=[],
        optional_params={
            "project": "ADO project name",
            "lookback_days": "Days to look back (default 60)",
            "recurrence_threshold": "Min bugs to consider recurring (default 3)",
            "similarity_threshold": "Title similarity threshold (default 0.75)",
            "recipients": "Email recipients list",
            "send_email": "Whether to send email (default True)",
            "preview_only": "Just preview without emailing"
        },
        use_cases=[
            "recurring bugs", "bug analysis", "bug patterns", 
            "bug areas", "highlight bugs", "bug report"
        ]
    ),
    "feedback_to_dev": SkillDefinition(
        name="feedback_to_dev",
        description="Detect new bugs, find similar historical bugs, extract RCA content, and send feedback notifications to developers",
        handler=_run_feedback_to_dev,
        required_params=[],
        optional_params={
            "project": "ADO project name",
            "lookback_minutes": "How far back to look for new bugs (default 1440 = 24h)",
            "historical_days": "How far back to look for historical bugs (default 30)",
            "embedding_threshold": "Similarity threshold for embeddings (default 0.82)",
            "recipients": "Email recipients list",
            "is_test": "Whether this is a test run (default False)"
        },
        use_cases=[
            "feedback to dev", "bug feedback", "rca feedback",
            "new bug notification", "developer feedback", "bug analysis feedback"
        ]
    ),
    "overlooked_stories": SkillDefinition(
        name="overlooked_stories",
        description="Find user stories that have been overlooked (stale, no activity) and send reminder",
        handler=_run_overlooked_stories,
        required_params=[],
        optional_params={
            "project": "ADO project name",
            "stale_days": "Days of inactivity (default 14)",
            "recipients": "Email recipients",
            "send_email": "Whether to send email"
        },
        use_cases=[
            "overlooked stories", "stale stories", "forgotten items",
            "overlooked user stories", "story reminder"
        ]
    ),
    "iteration_report": SkillDefinition(
        name="iteration_report",
        description="Generate iteration/sprint report with work item status",
        handler=_run_iteration_report,
        required_params=[],
        optional_params={
            "project": "ADO project name",
            "iteration": "Iteration path (default @CurrentIteration)",
            "areas": "Area paths to filter",
            "wi_types": "Work item types",
            "recipients": "Email recipients",
            "send_email": "Send email after generation"
        },
        use_cases=[
            "iteration report", "sprint report", "sprint status",
            "iteration status", "sprint summary"
        ]
    ),
    # NOTE: iteration_info skill REMOVED — its use_cases ("current sprint" etc.)
    # caused the fallback heuristic to misroute data-fetch queries to pm_skill_agent
    # instead of the LLM planner which can use MCP tools to fetch actual work items.
    # Sprint info queries are now handled by the LLM planner with @CurrentIteration macros.
    "send_email": SkillDefinition(
        name="send_email",
        description="Send email with specified content and attachments",
        handler=_send_email,
        required_params=["recipients", "subject"],
        optional_params={
            "body": "Email body (HTML or text)",
            "attachments": "File paths to attach"
        },
        use_cases=["send email", "email report", "send notification"]
    ),
    "list_area_paths": SkillDefinition(
        name="list_area_paths",
        description="List all area paths in an ADO project",
        handler=_list_area_paths,
        required_params=[],
        optional_params={"project": "ADO project name"},
        use_cases=["list area", "show area", "get area", "area path", "area paths"]
    ),
    "workitem_comment_summary": SkillDefinition(
        name="workitem_comment_summary",
        description="Get comprehensive summary of a work item including comments, updates, attachments. Returns LLM-ready text for context injection.",
        handler=_run_workitem_comment_summary,
        required_params=["work_item_id"],
        optional_params={
            "project": "ADO project name",
            "include_attachments": "Include attachment metadata (default True)",
            "include_updates": "Include field change history (default True)",
            "max_comments": "Maximum comments to include (default 50)"
        },
        use_cases=[
            "work item comments", "work item summary", "work item context",
            "work item details", "comments for work item", "work item history",
            "what comments", "show comments", "get comments"
        ]
    ),
    # NEW SKILLS from Image Requirements (Row 5-8)
    "get_sprint_status": SkillDefinition(
        name="get_sprint_status",
        description="Get current sprint status with work item counts, completion percentage, planned vs completed comparison, and tracking status (on_track/at_risk/off_track)",
        handler=_run_get_sprint_status,
        required_params=[],
        optional_params={
            "project": "ADO project name",
            "iteration": "Iteration path (default @CurrentIteration)"
        },
        use_cases=[
            "sprint status", "iteration status", "sprint progress",
            "how is the sprint", "sprint tracking", "planned vs completed",
            "off track tasks", "sprint summary"
        ]
    ),
    "get_backlog_health": SkillDefinition(
        name="get_backlog_health",
        description="DEPRECATED: Use backlog_triaging instead for backlog health analysis",
        handler=_run_backlog_triaging,  # Redirect to backlog_triaging
        required_params=[],
        optional_params={
            "project": "ADO project name",
            "team": "Team name"
        },
        use_cases=[
            # Empty - should route to backlog_triaging
        ]
    ),
    "backlog_triaging": SkillDefinition(
        name="backlog_triaging",
        description="Comprehensive backlog health analysis with AI effort estimation, runway calculation, and actionable recommendations",
        handler=_run_backlog_triaging,
        required_params=[],
        optional_params={
            "team": "Team name (e.g., 'XOPS 25', 'XOPS Bugs Enhancement')",
            "area_path": "Azure DevOps area path for filtering",
            "project": "ADO project name",
            "effort_field": "Custom effort field (default: Custom.Effort3P)",
            "velocity": "Team velocity override (default: auto-calculated)",
            "query": "Original user query for intent detection"
        },
        use_cases=[
            "backlog health", "backlog status", "thin backlog",
            "is backlog healthy", "refined items", "backlog check",
            "show backlog health", "backlog health report",
            "backlog health of XOPS 25", "list backlog items",
            "backlog runway", "grooming status"
        ]
    ),
    "get_capacity_forecast": SkillDefinition(
        name="get_capacity_forecast",
        description="Get capacity forecast and run capacity checks using utilities.capacity_manager",
        handler=_run_get_capacity_forecast,
        required_params=[],
        optional_params={
            "project": "ADO project name",
            "team": "Team name",
            "iteration": "Iteration path"
        },
        use_cases=[
            "capacity check", "capacity forecast", "capacity manager", "capacity report",
            "team capacity", "utilization", "capacity warning",
            "who is overloaded", "underutilized developers", "get capacity"
        ]
    ),
    "developer_skills": SkillDefinition(
        name="developer_skills",
        description="View developer skills, tech stack, expertise, code contributions and knowledge base",
        handler=_run_developer_skills,
        required_params=[],
        optional_params={
            "developer": "Developer name to filter",
            "technology": "Technology/language to search for"
        },
        use_cases=[
            "developer skills", "developer knowledge base", "tech stack",
            "who knows", "skill matrix", "developer expertise",
            "code contributions", "developer profile", "knowledge base"
        ]
    ),
    "change_ado_assignee": SkillDefinition(
        name="change_ado_assignee",
        description="Change assignee of an ADO work item. Wrapper for wit_update_work_item with assignedTo",
        handler=_run_change_ado_assignee,
        required_params=["work_item_id", "assignee"],
        optional_params={
            "comment": "Optional comment for the change"
        },
        use_cases=[
            "change assignee", "reassign work item", "assign to",
            "update assignee", "transfer work item", "reassign bug"
        ]
    ),
    "detect_recurring_bugs": SkillDefinition(
        name="detect_recurring_bugs",
        description="Detect recurring bugs by area path. Alias for bug_areas_highlight skill",
        handler=_run_detect_recurring_bugs,
        required_params=[],
        optional_params={
            "project": "ADO project name",
            "lookback_days": "Days to look back (default 60)",
            "recurrence_threshold": "Min bugs to consider recurring (default 3)"
        },
        use_cases=[
            "detect recurring bugs", "recurring bugs", "bug detection",
            "find repeated bugs", "bug patterns"
        ]
    ),
    "billing_deviation": SkillDefinition(
        name="billing_deviation",
        description="Calculate billing deviation dynamically. Deviation = Target Hours - Actual Hours. If area_path provided, requires target_hours; otherwise uses 4000 default. Actual hours from closed work items in current month.",
        handler=_run_billing_deviation,
        required_params=[],
        optional_params={
            "area_path": "Area path to filter (comma-separated for multiple)",
            "target_hours": "Target hours (required if area_path provided)",
            "recipient_email": "Email to send report to",
            "month": "Month number 1-12 (default current month)",
            "year": "Year (default current year)"
        },
        use_cases=[
            "billing deviation", "deviation in billing", "billing report", "billing analysis",
            "over-billing", "under-billing", "billing target",
            "billing hours", "deviation report", "billing calculation",
            "billing deviaiton", "deviaiton report"
        ]
    ),
    "backlog_assignments": SkillDefinition(
        name="backlog_assignments",
        description="View backlog assignment data with developer assignments and workload distribution",
        handler=_run_backlog_assignments,
        required_params=[],
        optional_params={
            "sprint_name": "Sprint name to filter by"
        },
        use_cases=[
            "backlog assignments", "show backlog assignments", "backlog tasks",
            "assigned backlog", "backlog workload", "backlog distribution",
            "show assignments", "task assignments"
        ]
    ),
    "sprint_plan": SkillDefinition(
        name="sprint_plan",
        description="View sprint plan with tasks, assignments, and estimates",
        handler=_run_sprint_plan,
        required_params=[],
        optional_params={
            "sprint_name": "Sprint name to filter by"
        },
        use_cases=[
            "sprint plan", "show sprint plan", "sprint tasks",
            "sprint schedule", "planned tasks", "sprint planning",
            "show plan", "task plan", "generate sprint plan"
        ]
    ),
    "execute_wiql": SkillDefinition(
        name="execute_wiql",
        description="Execute WIQL queries against Azure DevOps for advanced filtering by priority, date ranges, and custom fields. Use correct ADO field names: [Microsoft.VSTS.Common.ClosedDate] (not System.ClosedDate), [Microsoft.VSTS.Common.Priority] (not System.Priority).",
        handler=_run_wiql_query,
        required_params=[],
        llm_visible=True,  # LLM should see this tool and generate WIQL directly
        optional_params={
            "query": "Natural language query for auto-detection",
            "date_filter": "Date filter (last 7 days, last month, between DATE1 and DATE2, etc.)",
            "priority": "Priority filter (1-4, High/Medium/Low, P1-P4)",
            "work_item_type": "Work item type (Bug, Task, User Story, etc.)",
            "state": "State filter (single or comma-separated)",
            "assignedTo": "Assignee filter",
            "project": "ADO project name",
            "team": "Team name for area path resolution",
            "raw_wiql": "Pre-built WIQL query (overrides other params)"
        },
        use_cases=[
            "priority bugs", "high priority", "P1 items",
            "closed last month", "created last week", "modified this month",
            "work items between dates", "updated recently",
            "items closed yesterday", "bugs created this week"
        ]
    )
}


class SkillRegistry:
    """Registry of available PM skills."""
    
    def __init__(self):
        self._skills = SKILL_DEFINITIONS
    
    def get_skill(self, name: str) -> Optional[SkillDefinition]:
        """Get skill definition by name."""
        return self._skills.get(name)
    
    def get_all_skills(self) -> Dict[str, Dict[str, Any]]:
        """Get all skills as metadata dicts."""
        return {
            name: {
                "description": skill.description,
                "required_params": skill.required_params,
                "optional_params": skill.optional_params,
                "use_cases": skill.use_cases
            }
            for name, skill in self._skills.items()
        }
    
    def skill_exists(self, name: str) -> bool:
        """Check if a skill exists."""
        return name in self._skills
    
    def match_skill(self, query: str) -> Optional[str]:
        """
        Try to match a query to a skill by use cases.
        
        Returns skill name if matched, None otherwise.
        """
        query_lower = query.lower()
        
        for name, skill in self._skills.items():
            for use_case in skill.use_cases:
                if use_case in query_lower:
                    return name
        
        return None


async def execute_skill(skill_name: str, params: Dict[str, Any]) -> SkillResult:
    """
    Execute a skill by name.
    
    Args:
        skill_name: Name of the skill to execute
        params: Parameters for the skill
        
    Returns:
        SkillResult with success status and result/error
    """
    skill = SKILL_DEFINITIONS.get(skill_name)
    
    if not skill:
        return SkillResult(
            success=False,
            error=f"Unknown skill: {skill_name}. Available: {list(SKILL_DEFINITIONS.keys())}"
        )
    
    # Validate required params
    missing = [p for p in skill.required_params if p not in params]
    if missing:
        return SkillResult(
            success=False,
            error=f"Missing required parameters: {missing}"
        )
    
    # Execute the skill handler
    return await skill.handler(params)


def get_available_skills() -> Dict[str, Dict[str, Any]]:
    """Get list of all available skills with metadata."""
    registry = SkillRegistry()
    return registry.get_all_skills()
