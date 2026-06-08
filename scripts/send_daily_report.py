#!/usr/bin/env python3
"""
Send Daily Report — scheduled email with iteration summary, off-track items, and attachment.

Usage:
    python scripts/send_daily_report.py                 # Send email to recipients in config.yaml
    python scripts/send_daily_report.py --dry-run      # Save MIME to outputs/ instead of sending
    python scripts/send_daily_report.py --attachment PATH  # Use specific attachment file
    python scripts/send_daily_report.py --date 2025-01-20  # Use specific date for report

Features:
- Reads recipients from config.yaml (reportEmailRecipients)
- Uses SMTP credentials from .env (ALERTMANAGER_SMTP_*)
- Attaches latest sprint_plan_*.csv or iteration_report_*.csv from outputs/
- Generates iteration summary and off-track items table
- Supports dry-run mode for testing

Scheduler integration:
    Add to config.yaml under schedulerConfig.tasks:
        - name: "daily_report"
          schedule: "0 10 * * 1-5"   # 10:00 AM weekdays
          timezone: "Asia/Kolkata"
          enabled: true
"""
import argparse
import csv
import json
import logging
import os
import smtplib
import sys
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from glob import glob
from pathlib import Path
from typing import Dict, List, Optional, Tuple, Any
import mimetypes

# Ensure repo root is on sys.path
SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# Load configuration
from config import config
from utilities.langfuse_client import trace_task

try:
    import yaml
except ImportError:
    yaml = None

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger("daily_report")

# Constants
CONFIG_PATH = ROOT_DIR / "config.yaml"
OUTPUTS_DIR = ROOT_DIR / "outputs"
DATA_DIR = ROOT_DIR / "data"
TIMEZONE = "Asia/Kolkata"

# Status categories
COMPLETED_STATUSES = {"Done", "Closed", "Resolved", "Completed"}
IN_PROGRESS_STATUSES = {"Active", "In Progress", "Development", "In Review"}
READY_STATUSES = {"Ready", "New", "Approved", "To Do", "Not Started"}
BLOCKED_STATUSES = {"Blocked", "On Hold", "Waiting"}


def load_config() -> Dict:
    """Load config.yaml."""
    if not CONFIG_PATH.exists():
        logger.warning(f"Config file not found: {CONFIG_PATH}")
        return {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return yaml.safe_load(f) if yaml else {}
    except Exception as e:
        logger.error(f"Failed to load config: {e}")
        return {}


def get_recipients(config: Dict) -> List[str]:
    """Get email recipients from config."""
    recipients = config.get("reportEmailRecipients", [])
    if not recipients:
        logger.warning("No recipients found in config.yaml under 'reportEmailRecipients'")
    return recipients


def get_smtp_config() -> Dict[str, str]:
    """Get SMTP configuration from config."""
    return {
        "host": config.smtp_host,
        "port": config.smtp_port,
        "username": config.smtp_username,
        "password": config.smtp_password,
        "from_email": config.smtp_from,
    }


def generate_fresh_iteration_report() -> Tuple[Optional[Path], Optional[Path]]:
    """Generate a fresh iteration report from ADO.
    
    Returns:
        Tuple of (csv_path, html_path) - paths to generated files, or None if failed
    """
    try:
        from scripts.generate_iteration_report import generate_report
        from utilities.mcp.pat import get_pat
        
        org_url = config.ado_org_url
        project = config.ado_project
        team = config.ado_team
        pat = get_pat()
        
        if not all([org_url, project, pat]):
            logger.warning("ADO configuration incomplete, cannot generate fresh report")
            return None, None
        
        logger.info(f"Generating fresh iteration report for {project}...")
        
        # Generate the report
        csv_file, filtered_file, rows, filtered_rows, html_file = generate_report(
            org_url=org_url,
            pat=pat,
            project=project,
            team=team,
            outputs_dir=str(OUTPUTS_DIR),
        )
        
        logger.info(f"Generated iteration report: {csv_file} ({len(rows)} items)")
        if html_file:
            logger.info(f"Generated HTML report: {html_file}")
        
        csv_path = Path(csv_file) if csv_file else None
        html_path = Path(html_file) if html_file else None
        
        return csv_path, html_path
        
    except Exception as e:
        logger.error(f"Failed to generate fresh iteration report: {e}", exc_info=True)
        return None, None


def find_latest_report(attachment_override: Optional[str] = None, generate_if_stale: bool = True) -> Tuple[Optional[Path], Optional[Path]]:
    """Find the latest iteration report files to attach.
    
    Returns:
        Tuple of (csv_path, html_path) - paths to files, or None if not found
    
    Priority: 
    1. Generate fresh report if generate_if_stale=True and no recent report exists
    2. Use existing iteration_report_*.csv and matching .html
    """
    csv_path = None
    html_path = None
    
    if attachment_override:
        path = Path(attachment_override)
        if path.exists():
            csv_path = path
            # Try to find matching HTML file
            html_candidate = path.with_suffix('.html')
            if html_candidate.exists():
                html_path = html_candidate
            return csv_path, html_path
        logger.warning(f"Specified attachment not found: {attachment_override}")
    
    # Check for existing reports - prefer custom_wiql format
    patterns = [
        "iteration_report_custom_wiql_*.csv",
        "iteration_report_*.csv",
    ]
    
    latest_csv = None
    for pattern in patterns:
        files = sorted(glob(str(OUTPUTS_DIR / pattern)), key=os.path.getmtime, reverse=True)
        if files:
            latest_csv = Path(files[0])
            break
    
    # Check if latest report is stale (more than 12 hours old) or doesn't exist
    is_stale = True
    if latest_csv and latest_csv.exists():
        age_hours = (datetime.now() - datetime.fromtimestamp(latest_csv.stat().st_mtime)).total_seconds() / 3600
        is_stale = age_hours > 12
        logger.info(f"Latest report: {latest_csv.name} (age: {age_hours:.1f} hours, stale: {is_stale})")
    else:
        logger.warning("No existing iteration report found")
    
    # Generate fresh report if stale or not found
    if generate_if_stale and is_stale:
        logger.info("Generating fresh iteration report...")
        fresh_csv, fresh_html = generate_fresh_iteration_report()
        if fresh_csv and fresh_csv.exists():
            csv_path = fresh_csv
            html_path = fresh_html
            return csv_path, html_path
        else:
            logger.warning("Fresh report generation failed, falling back to cached report")
    
    # Use existing report if available
    if latest_csv and latest_csv.exists():
        csv_path = latest_csv
        # Find matching HTML file
        html_candidates = [
            latest_csv.with_suffix('.html'),
            Path(str(latest_csv).replace('.csv', '.html')),
        ]
        for html_candidate in html_candidates:
            if html_candidate.exists():
                html_path = html_candidate
                break
        
        # Also look for any HTML files with similar name pattern
        if not html_path:
            html_pattern = latest_csv.stem.replace('_custom_wiql', '') + "*.html"
            html_files = sorted(glob(str(OUTPUTS_DIR / html_pattern)), key=os.path.getmtime, reverse=True)
            if html_files:
                html_path = Path(html_files[0])
    
    return csv_path, html_path


def load_report_data(report_path: Optional[Path]) -> Tuple[List[Dict], str]:
    """Load report data from CSV/JSON file."""
    if not report_path or not report_path.exists():
        # Try to load from data/wi_tags.json as fallback
        wi_tags_path = DATA_DIR / "wi_tags.json"
        if wi_tags_path.exists():
            try:
                with open(wi_tags_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    return data if isinstance(data, list) else [], "wi_tags.json"
            except Exception:
                pass
        return [], "none"
    
    suffix = report_path.suffix.lower()
    
    if suffix == ".csv":
        try:
            with open(report_path, "r", encoding="utf-8", newline="") as f:
                reader = csv.DictReader(f)
                return list(reader), report_path.name
        except Exception as e:
            logger.error(f"Failed to read CSV: {e}")
            return [], report_path.name
    
    elif suffix == ".json":
        try:
            with open(report_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data, report_path.name
                elif isinstance(data, dict):
                    # Handle capacity report format
                    items = []
                    for dev, info in data.items():
                        if isinstance(info, dict):
                            items.append({"developer": dev, **info})
                    return items, report_path.name
        except Exception as e:
            logger.error(f"Failed to read JSON: {e}")
            return [], report_path.name
    
    return [], report_path.name if report_path else "none"


def categorize_status(status: str) -> str:
    """Categorize a status into Completed/In Progress/Ready/Blocked/Unknown."""
    status_lower = status.lower() if status else ""
    status_title = status.title() if status else ""
    
    if status_title in COMPLETED_STATUSES or "done" in status_lower or "closed" in status_lower:
        return "Completed"
    elif status_title in IN_PROGRESS_STATUSES or "progress" in status_lower or "active" in status_lower:
        return "In Progress"
    elif status_title in READY_STATUSES or "ready" in status_lower or "new" in status_lower:
        return "Ready"
    elif status_title in BLOCKED_STATUSES or "block" in status_lower or "hold" in status_lower:
        return "Blocked"
    else:
        return "Unknown"


def compute_iteration_summary(rows: List[Dict]) -> Dict[str, Any]:
    """Compute iteration summary metrics from report data."""
    total = len(rows)
    completed = 0
    in_progress = 0
    ready = 0
    blocked = 0
    unknown = 0
    
    total_estimated_hours = 0.0
    total_assigned_hours = 0.0
    
    for row in rows:
        # Get status from various possible column names
        status = (
            row.get("Status") or 
            row.get("State") or 
            row.get("System.State") or 
            row.get("status") or 
            ""
        )
        
        category = categorize_status(status)
        if category == "Completed":
            completed += 1
        elif category == "In Progress":
            in_progress += 1
        elif category == "Ready":
            ready += 1
        elif category == "Blocked":
            blocked += 1
        else:
            unknown += 1
        
        # Sum hours
        try:
            est = float(row.get("Estimated Hours") or row.get("estimated_hours") or 0)
            total_estimated_hours += est
        except (ValueError, TypeError):
            pass
        
        try:
            assigned = float(row.get("assigned_hours") or row.get("Assigned Hours") or 0)
            total_assigned_hours += assigned
        except (ValueError, TypeError):
            pass
    
    # Calculate utilization
    utilization = 0
    if total_estimated_hours > 0:
        utilization = round((total_assigned_hours / total_estimated_hours) * 100, 1)
    
    return {
        "total_tasks": total,
        "completed": completed,
        "in_progress": in_progress,
        "ready": ready,
        "blocked": blocked,
        "unknown": unknown,
        "total_estimated_hours": round(total_estimated_hours, 1),
        "total_assigned_hours": round(total_assigned_hours, 1),
        "utilization": utilization,
    }


def find_off_track_items(rows: List[Dict], report_date: datetime) -> List[Dict]:
    """Find items that are off-track based on missing critical dates.
    
    Off-track criteria (ONLY these two):
    - Missing UAT Scheduled Deployment Date
    - Missing PROD Scheduled Deployment Date
    """
    off_track = []
    
    for row in rows:
        # Get work item ID - handle iteration report format
        wi_id = (
            row.get("ID") or  # Iteration report format
            row.get("S.No.") or
            row.get("Task Name") or 
            row.get("WI ID") or 
            row.get("System.Id") or 
            row.get("id") or 
            "Unknown"
        )
        
        # Get title
        title = (
            row.get("Title") or 
            row.get("Feature / User Story") or 
            row.get("System.Title") or 
            row.get("title") or 
            "Untitled"
        )
        
        # Get assigned to
        assigned_to = (
            row.get("Assigned To") or  # Iteration report format
            row.get("AssignedTo") or 
            row.get("System.AssignedTo") or 
            row.get("Responsible - Frontend") or 
            row.get("Responsible - Backend") or 
            row.get("assigned_to") or 
            "Unassigned"
        )
        
        # Get state
        state = (
            row.get("State") or  # Iteration report format
            row.get("Status") or 
            row.get("System.State") or 
            row.get("status") or 
            ""
        )
        
        # Get work item type
        wi_type = (
            row.get("Work Item Type") or
            row.get("System.WorkItemType") or
            ""
        )
        
        # Get critical date fields from iteration report
        uat_scheduled = row.get("UAT Scheduled Deployment Date") or row.get("Custom.UATScheduledDeploymentDate") or ""
        prod_scheduled = row.get("PROD Scheduled Deployment") or row.get("Custom.PRODScheduledDeployment") or ""
        
        # Skip completed/closed items
        state_lower = state.lower() if state else ""
        if state_lower in ["done", "closed", "resolved", "completed", "removed"]:
            continue
        
        # Check ONLY for missing UAT Scheduled or PROD Scheduled dates
        missing_dates = []
        
        if not uat_scheduled.strip():
            missing_dates.append("UAT Scheduled Date")
        
        if not prod_scheduled.strip():
            missing_dates.append("PROD Scheduled Date")
        
        # Only add if missing UAT or PROD scheduled dates
        if missing_dates:
            reason = "Missing: " + ", ".join(missing_dates)
            
            off_track.append({
                "wi_id": wi_id,
                "title": title[:55] + "..." if len(title) > 55 else title,
                "assigned_to": assigned_to,
                "state": state,
                "wi_type": wi_type,
                "missing_dates": missing_dates,
                "reason": reason,
                "uat_scheduled": uat_scheduled or "❌ Missing",
                "prod_scheduled": prod_scheduled or "❌ Missing",
            })
    
    # Sort by number of missing dates (most critical first), then by WI ID
    off_track.sort(key=lambda x: (-len(x.get("missing_dates", [])), str(x.get("wi_id", ""))))
    return off_track


def build_plain_text_body(
    summary: Dict[str, Any],
    off_track: List[Dict],
    attachment_name: str,
    project: str,
    report_date: datetime,
) -> str:
    """Build plain text email body."""
    date_str = report_date.strftime("%Y-%m-%d")
    
    lines = [
        f"Daily Report — {project} — {date_str}",
        "",
        "=" * 50,
        "ITERATION SUMMARY",
        "=" * 50,
        f"• Total tasks: {summary['total_tasks']}",
        f"• Completed: {summary['completed']}",
        f"• In Progress: {summary['in_progress']}",
        f"• Ready: {summary['ready']}",
        f"• Blocked: {summary['blocked']}",
        f"• Total estimated hours: {summary['total_estimated_hours']}",
        f"• Total assigned hours: {summary['total_assigned_hours']}",
        f"• Overall utilization: {summary['utilization']}%",
        "",
    ]
    
    if off_track:
        lines.extend([
            "=" * 50,
            "OFF-TRACK ITEMS (Missing Critical Dates)",
            "=" * 50,
        ])
        for i, item in enumerate(off_track[:15], 1):  # Limit to 15 items
            lines.append(
                f"{i}) WI-{item['wi_id']} — {item['title']}\n"
                f"   Assigned: {item['assigned_to']} | State: {item.get('state', 'N/A')}\n"
                f"   UAT Scheduled: {item.get('uat_scheduled', 'N/A')} | PROD Scheduled: {item.get('prod_scheduled', 'N/A')}\n"
                f"   Issue: {item['reason']}"
            )
        if len(off_track) > 15:
            lines.append(f"\n... and {len(off_track) - 15} more items (see attachment for full list)")
        lines.append("")
    else:
        lines.extend([
            "=" * 50,
            "OFF-TRACK ITEMS",
            "=" * 50,
            "✓ All work items have required dates filled. No off-track items!",
            "",
        ])
    
    lines.extend([
        "-" * 50,
        f"Attachment: {attachment_name}",
        "",
        "This is an automated daily report from PM Agent.",
    ])
    
    return "\n".join(lines)


def build_html_body(
    summary: Dict[str, Any],
    off_track: List[Dict],
    attachment_name: str,
    project: str,
    report_date: datetime,
) -> str:
    """Build HTML email body with styled tables."""
    date_str = report_date.strftime("%Y-%m-%d")
    
    html = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <style>
        body {{ font-family: Arial, sans-serif; line-height: 1.6; color: #333; max-width: 900px; margin: 0 auto; }}
        h1 {{ color: #2c5282; border-bottom: 2px solid #4299e1; padding-bottom: 10px; }}
        h2 {{ color: #2d3748; margin-top: 25px; }}
        .summary-box {{ background: #f7fafc; border: 1px solid #e2e8f0; border-radius: 8px; padding: 20px; margin: 20px 0; }}
        .summary-grid {{ display: grid; grid-template-columns: repeat(2, 1fr); gap: 10px; }}
        .metric {{ padding: 10px; background: white; border-radius: 4px; border-left: 4px solid #4299e1; }}
        .metric-label {{ font-size: 12px; color: #718096; text-transform: uppercase; }}
        .metric-value {{ font-size: 24px; font-weight: bold; color: #2d3748; }}
        .completed {{ border-left-color: #48bb78; }}
        .in-progress {{ border-left-color: #4299e1; }}
        .blocked {{ border-left-color: #f56565; }}
        .ready {{ border-left-color: #ecc94b; }}
        table {{ width: 100%; border-collapse: collapse; margin: 20px 0; font-size: 13px; }}
        th {{ background: #4a5568; color: white; padding: 10px; text-align: left; }}
        td {{ padding: 8px; border-bottom: 1px solid #e2e8f0; }}
        tr:hover {{ background: #f7fafc; }}
        .missing-date {{ color: #c53030; font-weight: bold; background: #fff5f5; }}
        .footer {{ margin-top: 30px; padding-top: 20px; border-top: 1px solid #e2e8f0; font-size: 12px; color: #718096; }}
        .attachment-note {{ background: #ebf8ff; border: 1px solid #bee3f8; border-radius: 4px; padding: 10px; margin: 20px 0; }}
        .no-issues {{ background: #f0fff4; border: 1px solid #9ae6b4; border-radius: 4px; padding: 15px; text-align: center; color: #276749; }}
    </style>
</head>
<body>
    <h1>📊 Daily Report — {project}</h1>
    <p><strong>Date:</strong> {date_str}</p>
    
    <h2>📈 Iteration Summary</h2>
    <div class="summary-box">
        <div class="summary-grid">
            <div class="metric">
                <div class="metric-label">Total Tasks</div>
                <div class="metric-value">{summary['total_tasks']}</div>
            </div>
            <div class="metric completed">
                <div class="metric-label">Completed</div>
                <div class="metric-value">{summary['completed']}</div>
            </div>
            <div class="metric in-progress">
                <div class="metric-label">In Progress</div>
                <div class="metric-value">{summary['in_progress']}</div>
            </div>
            <div class="metric ready">
                <div class="metric-label">Ready</div>
                <div class="metric-value">{summary['ready']}</div>
            </div>
            <div class="metric blocked">
                <div class="metric-label">Blocked</div>
                <div class="metric-value">{summary['blocked']}</div>
            </div>
            <div class="metric">
                <div class="metric-label">Utilization</div>
                <div class="metric-value">{summary['utilization']}%</div>
            </div>
        </div>
        <div style="margin-top: 15px; padding-top: 15px; border-top: 1px solid #e2e8f0;">
            <strong>Hours:</strong> {summary['total_assigned_hours']} assigned / {summary['total_estimated_hours']} estimated
        </div>
    </div>
    
    <h2>⚠️ Off-Track Items (Missing Critical Dates)</h2>
"""
    
    if off_track:
        html += """
    <p style="color: #718096; font-size: 14px;">Work items missing UAT Scheduled Date, PROD Scheduled Date, or other critical deployment dates:</p>
    <table>
        <thead>
            <tr>
                <th>#</th>
                <th>WI ID</th>
                <th>Title</th>
                <th>Assigned To</th>
                <th>State</th>
                <th>UAT Scheduled</th>
                <th>PROD Scheduled</th>
                <th>Issue</th>
            </tr>
        </thead>
        <tbody>
"""
        for i, item in enumerate(off_track[:20], 1):  # Limit to 20 in HTML
            uat_class = "missing-date" if "❌" in str(item.get('uat_scheduled', '')) else ""
            prod_class = "missing-date" if "❌" in str(item.get('prod_scheduled', '')) else ""
            html += f"""
            <tr>
                <td>{i}</td>
                <td><strong>WI-{item['wi_id']}</strong></td>
                <td>{item['title']}</td>
                <td>{item['assigned_to']}</td>
                <td>{item.get('state', 'N/A')}</td>
                <td class="{uat_class}">{item.get('uat_scheduled', 'N/A')}</td>
                <td class="{prod_class}">{item.get('prod_scheduled', 'N/A')}</td>
                <td style="color: #c53030;">{item['reason']}</td>
            </tr>
"""
        html += """
        </tbody>
    </table>
"""
        if len(off_track) > 20:
            html += f'<p><em>... and {len(off_track) - 20} more items (see attached iteration report for full list)</em></p>'
    else:
        html += """
    <div class="no-issues">
        ✅ All work items have required dates filled. No off-track items!
    </div>
"""
    
    html += f"""
    <div class="attachment-note">
        📎 <strong>Attachment:</strong> {attachment_name}
    </div>
    
    <div class="footer">
        <p>This is an automated daily report generated by PM Agent.</p>
        <p>Report generated at: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")} IST</p>
    </div>
</body>
</html>
"""
    return html


def compose_email(
    recipients: List[str],
    subject: str,
    plain_body: str,
    html_body: str,
    attachments: List[Path],
    from_email: str,
) -> MIMEMultipart:
    """Compose multipart email with plain text, HTML, and attachments.
    
    Args:
        recipients: List of email addresses
        subject: Email subject
        plain_body: Plain text body
        html_body: HTML body
        attachments: List of file paths to attach (CSV, HTML, etc.)
        from_email: Sender email address
    """
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = ", ".join(recipients)
    
    # Create alternative part for plain text and HTML
    alt_part = MIMEMultipart("alternative")
    
    # Plain text version
    text_part = MIMEText(plain_body, "plain", "utf-8")
    alt_part.attach(text_part)
    
    # HTML version
    html_part = MIMEText(html_body, "html", "utf-8")
    alt_part.attach(html_part)
    
    msg.attach(alt_part)
    
    # Attach all files
    for attachment_path in attachments:
        if attachment_path and attachment_path.exists():
            ctype, _ = mimetypes.guess_type(str(attachment_path))
            maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
            
            with open(attachment_path, "rb") as f:
                attachment = MIMEBase(maintype, subtype)
                attachment.set_payload(f.read())
                encoders.encode_base64(attachment)
                attachment.add_header(
                    "Content-Disposition",
                    "attachment",
                    filename=attachment_path.name,
                )
                msg.attach(attachment)
    
    
    return msg


def send_email(msg: MIMEMultipart, smtp_config: Dict) -> Tuple[bool, str]:
    """Send email via SMTP."""
    try:
        with smtplib.SMTP(smtp_config["host"], smtp_config["port"]) as server:
            server.starttls()
            server.login(smtp_config["username"], smtp_config["password"])
            server.send_message(msg)
        return True, "Email sent successfully"
    except Exception as e:
        return False, str(e)


def save_mime_file(msg: MIMEMultipart, output_dir: Path) -> Path:
    """Save email as .eml file for dry-run mode."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = output_dir / f"dryrun_email_{timestamp}.eml"
    output_dir.mkdir(parents=True, exist_ok=True)
    
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(msg.as_string())
    
    return output_path


@trace_task("daily_report", metadata={"source": "pm_agent"})
def main():
    parser = argparse.ArgumentParser(
        description="Send Daily Report email with iteration summary and off-track items",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Save email as .eml file instead of sending",
    )
    parser.add_argument(
        "--attachment",
        type=str,
        default=None,
        help="Path to specific attachment file (default: auto-detect latest)",
    )
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="Report date in YYYY-MM-DD format (default: today)",
    )
    parser.add_argument(
        "--project",
        type=str,
        default=None,
        help="Project name for email subject (default: from config.yaml)",
    )
    parser.add_argument(
        "--no-generate",
        action="store_true",
        help="Don't generate fresh report, use cached only",
    )
    
    args = parser.parse_args()
    
    # Set project from config if not provided
    if not args.project:
        from config import config as cfg
        args.project = cfg.ado_project or "FracPro-OPS"
    
    logger.info("=" * 60)
    logger.info("DAILY REPORT GENERATOR")
    logger.info("=" * 60)
    
    # Parse report date
    if args.date:
        try:
            report_date = datetime.strptime(args.date, "%Y-%m-%d")
        except ValueError:
            logger.error(f"Invalid date format: {args.date}. Use YYYY-MM-DD")
            sys.exit(1)
    else:
        report_date = datetime.now()
    
    logger.info(f"Report date: {report_date.strftime('%Y-%m-%d')}")
    
    # Load config
    config = load_config()
    recipients = get_recipients(config)
    
    if not recipients:
        logger.error("No recipients configured. Add 'reportEmailRecipients' to config.yaml")
        sys.exit(1)
    
    logger.info(f"Recipients: {recipients}")
    
    # Get SMTP config
    smtp_config = get_smtp_config()
    
    if not args.dry_run:
        if not smtp_config["username"] or not smtp_config["password"]:
            logger.error("SMTP not configured. Set ALERTMANAGER_SMTP_* environment variables or use --dry-run")
            sys.exit(1)
    
    # Find attachments (generates fresh report if stale, unless --no-generate)
    generate_if_stale = not getattr(args, 'no_generate', False)
    csv_path, html_path = find_latest_report(args.attachment, generate_if_stale=generate_if_stale)
    
    # Build list of attachments
    attachments: List[Path] = []
    if csv_path and csv_path.exists():
        attachments.append(csv_path)
    if html_path and html_path.exists():
        attachments.append(html_path)
    
    attachment_names = ", ".join([a.name for a in attachments]) if attachments else "No attachments"
    logger.info(f"Attachments: {attachment_names}")
    
    # Validate we have at least a CSV
    if not csv_path or not csv_path.exists():
        logger.error("No iteration report available. Cannot send daily report.")
        sys.exit(1)
    
    # Load report data from CSV
    rows, source = load_report_data(csv_path)
    logger.info(f"Loaded {len(rows)} items from {source}")
    
    # Compute summary
    summary = compute_iteration_summary(rows)
    logger.info(f"Summary: {summary['total_tasks']} total, {summary['completed']} completed, {summary['blocked']} blocked")
    
    # Find off-track items
    off_track = find_off_track_items(rows, report_date)
    logger.info(f"Off-track items: {len(off_track)}")
    
    # Build email content
    plain_body = build_plain_text_body(summary, off_track, attachment_names, args.project, report_date)
    html_body = build_html_body(summary, off_track, attachment_names, args.project, report_date)
    
    # Email subject
    date_str = report_date.strftime("%Y-%m-%d")
    subject = f"Daily Report — {args.project} — {date_str}"
    
    # Compose email with all attachments
    from_email = smtp_config.get("from_email") or smtp_config.get("username") or "noreply@example.com"
    msg = compose_email(recipients, subject, plain_body, html_body, attachments, from_email)
    
    if args.dry_run:
        # Save to file instead of sending
        output_path = save_mime_file(msg, OUTPUTS_DIR)
        logger.info(f"Dry-run mode: Email saved to {output_path}")
        print(f"\n[OK] Dry-run completed!")
        print(f"Email saved to: {output_path}")
        print(f"Subject: {subject}")
        print(f"Recipients: {', '.join(recipients)}")
        print(f"Attachments: {attachment_names}")
        print(f"Summary: {summary['total_tasks']} tasks, {len(off_track)} off-track")
    else:
        # Send email
        logger.info("Sending email...")
        success, message = send_email(msg, smtp_config)
        
        if success:
            logger.info(f"✅ Email sent successfully to: {recipients}")
            print(f"\n✅ Email sent successfully!")
            print(f"👥 Recipients: {', '.join(recipients)}")
            print(f"📋 Subject: {subject}")
            print(f"📎 Attachments: {attachment_names}")
        else:
            logger.error(f"❌ Failed to send email: {message}")
            print(f"\n❌ Failed to send email: {message}")
            sys.exit(1)
    
    logger.info("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
