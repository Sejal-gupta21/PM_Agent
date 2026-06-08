#!/usr/bin/env python3
"""Run the daily iteration report once: generate, build summary, and send.

This script is intended for CI/cloud runners (GitHub Actions). It:
 - loads environment variables from .env if present
 - reads `config.yaml` to determine recipients
 - generates the iteration report (or uses latest cached CSV)
 - builds the plain+HTML summary and sends the report via configured email provider

Usage:
    python scripts/run_daily_report.py              # normal run
    python scripts/run_daily_report.py --send-now   # force send immediately
    python scripts/run_daily_report.py --dry-run    # build report, don't send email

Exit codes: 0 on success, non-zero on error.
"""
import argparse
import json
import os
import sys
import glob
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

# allow running from repo root
REPO_ROOT = Path(__file__).resolve().parent.parent
os.chdir(REPO_ROOT)

from config import config
sys.path.insert(0, str(REPO_ROOT))

import yaml

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("pm_agent.run_daily_report")

# Send history stored in outputs/report_send_history.json per requirements
OUTPUTS_DIR = REPO_ROOT / "outputs"
SEND_HISTORY_FILE = OUTPUTS_DIR / "report_send_history.json"


def load_recipients_from_config() -> List[str]:
    cfg_path = REPO_ROOT / "config.yaml"
    if not cfg_path.exists():
        return []
    try:
        with cfg_path.open("r", encoding="utf-8") as fh:
            cfg = yaml.safe_load(fh) or {}
        return cfg.get("reportEmailRecipients") or []
    except Exception:
        return []


def _ensure_outputs_dir():
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)


def load_send_history() -> dict:
    """Load send history from JSON."""
    if not SEND_HISTORY_FILE.exists():
        return {"sends": []}
    try:
        with SEND_HISTORY_FILE.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {"sends": []}


def record_send_attempt(recipients: List[str], subject: str, ok: bool, error_msg: str = "", dry_run: bool = False):
    """Append an entry to the send history."""
    _ensure_outputs_dir()
    hist = load_send_history()
    hist["sends"].append({
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "recipients": recipients,
        "subject": subject,
        "success": ok,
        "error": error_msg,
        "dry_run": dry_run,
    })
    # keep at most 500 entries
    hist["sends"] = hist["sends"][-500:]
    with SEND_HISTORY_FILE.open("w", encoding="utf-8") as fh:
        json.dump(hist, fh, indent=2)


def send_with_retry(
    recipients: List[str],
    subject: str,
    body: Tuple[str, str],
    attachments: List[str],
    max_attempts: int = 3,
    delay_seconds: int = 5,
):
    """Send email with retry logic. Returns (success: bool, message: str).
    
    Args:
        body: Tuple of (plain_text, html_body) - we use html_body for sending
    """
    from utilities.emailer import send_report_attachment

    # Extract HTML body from tuple (plain_text, html_body)
    plain_text, html_body = body

    last_error = ""
    for attempt in range(1, max_attempts + 1):
        try:
            ok, msg = send_report_attachment(recipients, subject, html_body, attachments)
            if ok:
                record_send_attempt(recipients, subject, True)
                return True, msg
            else:
                last_error = msg
                logger.warning("Attempt %d/%d failed: %s", attempt, max_attempts, msg)
        except Exception as exc:
            last_error = str(exc)
            logger.exception("Attempt %d/%d exception: %s", attempt, max_attempts, exc)
        if attempt < max_attempts:
            time.sleep(delay_seconds)

    record_send_attempt(recipients, subject, False, last_error)
    return False, last_error


def parse_args():
    p = argparse.ArgumentParser(description="Run daily iteration report.")
    p.add_argument("--dry-run", action="store_true", help="Generate report but don't send email.")
    p.add_argument("--send-now", action="store_true", help="Force send immediately (ignore scheduler).")
    p.add_argument("--max-offtrack", type=int, default=50, help="Max offtrack items to include.")
    p.add_argument("--retry", type=int, default=3, help="Number of send retries on failure.")
    return p.parse_args()


def main():
    args = parse_args()

    # Prefer recipients from config.yaml
    recipients = config.report_email_recipients
    if not recipients:
        # Fallback to legacy load function
        recipients = load_recipients_from_config()

    if not recipients:
        logger.error("No recipients configured (REPORT_RECIPIENTS env or config.yaml.reportEmailRecipients)")
        return 2

    # lazy imports used for runtime
    try:
        from scripts.generate_iteration_report import generate_report
        from utilities.report_summary import build_iteration_summary
        from utilities.offtrack import extract_offtrack_items
        from utilities.mcp.pat import get_pat
    except Exception as e:
        logger.exception("Failed to import report/email utilities: %s", e)
        return 3

    org = config.ado_org_url
    project = config.ado_project
    team = config.ado_team

    pat = get_pat()

    out_file: Optional[str] = None
    html_file: Optional[str] = None

    try:
        out_file, filtered_file, rows, filtered_rows, html_file = generate_report(
            org_url=org,
            pat=pat,
            project=project,
            team=team,
            outputs_dir="outputs",
        )
        logger.info("Report generated: %s", out_file)
    except Exception:
        logger.exception("Report generation failed; attempting to use latest cached report")
        cached = sorted(glob.glob(str(REPO_ROOT / 'outputs' / 'iteration_report_*.csv')), reverse=True)
        if cached:
            out_file = cached[0]
            html_file = out_file.rsplit('.', 1)[0] + '.html'
            logger.info("Using cached report: %s", out_file)
        else:
            logger.error("No report available to send")
            return 4

    # Build iteration summary
    try:
        plain_summary, html_summary = build_iteration_summary(out_file)
    except Exception:
        logger.exception("Failed to build iteration summary")
        plain_summary, html_summary = ("", "")

    # Extract offtrack items (criteria: missing UAT & PROD scheduled deployment dates)
    offtrack_plain = ""
    offtrack_html = ""
    offtrack_count = 0
    try:
        offtrack_plain, offtrack_html, offtrack_items = extract_offtrack_items(out_file, max_items=args.max_offtrack)
        offtrack_count = len(offtrack_items)
        logger.info("Offtrack items detected: %d", offtrack_count)
    except Exception:
        logger.exception("Failed to extract offtrack items")

    # Build the email body matching the expected format from screenshot
    today_str = datetime.now().strftime("%Y-%m-%d")
    
    # Build full HTML email body with proper formatting
    full_html = f'''<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        body {{ font-family: Arial, Helvetica, sans-serif; margin: 20px; color: #333; }}
        h1 {{ color: #2c3e50; border-bottom: 2px solid #3498db; padding-bottom: 10px; }}
        h2 {{ color: #2980b9; margin-top: 24px; }}
        .summary-box {{ background: #f8f9fa; border-left: 4px solid #3498db; padding: 15px; margin: 15px 0; }}
        .metric {{ display: inline-block; margin: 10px 20px 10px 0; }}
        .metric-value {{ font-size: 28px; font-weight: bold; }}
        .metric-label {{ font-size: 12px; color: #666; text-transform: uppercase; }}
        .metric-completed .metric-value {{ color: #27ae60; }}
        .metric-inprogress .metric-value {{ color: #3498db; }}
        .metric-ready .metric-value {{ color: #f39c12; }}
        .metric-blocked .metric-value {{ color: #e74c3c; }}
        .metric-total .metric-value {{ color: #2c3e50; }}
        table {{ border-collapse: collapse; width: 100%; margin: 15px 0; }}
        th {{ background: #3498db; color: white; padding: 10px; text-align: left; }}
        td {{ padding: 8px; border: 1px solid #ddd; }}
        tr:nth-child(even) {{ background: #f9f9f9; }}
        .offtrack-section {{ margin-top: 30px; }}
        .note {{ font-size: 12px; color: #666; font-style: italic; }}
    </style>
</head>
<body>
    <h1>📊 Daily Report — {project}</h1>
    <p><strong>Date:</strong> {today_str}</p>
    
    <h2>📈 Iteration Summary</h2>
    <div class="summary-box">
        {html_summary if html_summary else '<p>No summary data available.</p>'}
    </div>
    
    <div class="offtrack-section">
        <h2>⚠️ Offtrack Items</h2>
        <p class="note"><strong>Criteria:</strong> UAT & PROD scheduled deployment dates are missing, blocked items, or status issues.</p>
        {offtrack_html if offtrack_html else '<p>No offtrack items detected.</p>'}
    </div>
    
    <hr style="margin-top: 30px; border: none; border-top: 1px solid #ddd;">
    <p class="note">
        <strong>Attachments:</strong> Full iteration report attached in CSV and HTML formats with color coding.
    </p>
</body>
</html>'''

    # Build plain text version
    plain_text = f"""Daily Report — {project}
Date: {today_str}

=== ITERATION SUMMARY ===
{plain_summary if plain_summary else 'No summary data available.'}

=== OFFTRACK ITEMS ===
Criteria: UAT & PROD scheduled deployment dates are missing constitute offtrack items.

{offtrack_plain if offtrack_plain else 'No offtrack items detected.'}

---
Full iteration report attached in CSV and HTML formats.
"""

    subject = f'Daily Report — {project}'
    attachments = [out_file]
    if html_file and os.path.exists(html_file):
        attachments.append(html_file)

    if args.dry_run:
        # Print required dry-run info
        print("\n" + "="*60)
        print("[DRY-RUN] Daily Report Email Preview")
        print("="*60)
        print(f"Next scheduled run: 10:00 IST (04:30 UTC) Mon-Fri")
        print(f"Recipients: {recipients}")
        print(f"Subject: {subject}")
        print(f"Offtrack count: {offtrack_count}")
        print(f"Attachments: {attachments}")
        print("-"*60)
        print("Summary preview:")
        print(plain_text[:2000])
        print("="*60 + "\n")
        
        # Record dry-run entry to outputs/report_send_history.json
        record_send_attempt(recipients, subject, ok=True, error_msg="", dry_run=True)
        logger.info("[DRY-RUN] Recorded to %s", SEND_HISTORY_FILE)
        return 0

    ok, msg = send_with_retry(recipients, subject, (plain_text, full_html), attachments, max_attempts=args.retry)
    if ok:
        logger.info("Report sent to %s", recipients)
        return 0
    else:
        logger.error("Report send failed after %d attempts: %s", args.retry, msg)
        return 5


if __name__ == '__main__':
    sys.exit(main())
