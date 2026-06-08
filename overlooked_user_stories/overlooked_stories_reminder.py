#!/usr/bin/env python3
"""
Overlooked User Stories Reminder

Finds User Stories that have been sitting in the backlog for a long time
and emails a hierarchical report (Epic → Feature → Story) to configured recipients.

Usage:
  source .venv/bin/activate
  ADO_ORG_URL=... ADO_PROJECT=... ADO_PAT=... python overlooked_user_stories/overlooked_stories_reminder.py

Configuration:
  Email recipients are configured in config.yaml under reportEmailRecipients.
  Only emails listed in config.yaml will receive reports.

Options:
  --dry-run: Generate reports without sending emails
"""
from __future__ import annotations
import os
import sys
import csv
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional, Tuple
import argparse
import requests
import html
import textwrap
import re
import logging

logger = logging.getLogger(__name__)

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from utilities.mcp.pat import get_pat
from utilities.emailer import send_report_attachment
from config import config as app_config
from utilities.langfuse_client import trace_task

# Import hierarchy and summary modules from this package
from overlooked_user_stories.hierarchy import build_complete_hierarchy
from overlooked_user_stories.summary import (
    generate_categorized_summary,
    format_summary_text,
    format_summary_html,
    format_summary_for_ui,
    generate_hierarchical_html
)
from overlooked_user_stories.formatters import csv_write_with_hierarchy, html_for_rows_flat
from overlooked_user_stories.config_reader import load_email_recipients

API_VERSION = "7.0"

def run_wiql(org_url: str, wiql: str, pat: str, project: Optional[str] = None, team: Optional[str] = None) -> List[int]:
    if project and team:
        url = f"{org_url}/{project}/{team}/_apis/wit/wiql?api-version={API_VERSION}"
    elif project:
        url = f"{org_url}/{project}/_apis/wit/wiql?api-version={API_VERSION}"
    else:
        url = f"{org_url}/_apis/wit/wiql?api-version={API_VERSION}"
    resp = requests.post(url, json={"query": wiql}, auth=("", pat))
    if resp.status_code >= 400:
        raise RuntimeError(f"WIQL request failed: {resp.status_code} {resp.text}")
    data = resp.json()
    return [item["id"] for item in data.get("workItems", [])]


def fetch_workitems(org_url: str, ids: List[int], pat: str) -> List[Dict[str, Any]]:
    if not ids:
        return []
    batch = 200
    results: List[Dict[str, Any]] = []
    for i in range(0, len(ids), batch):
        chunk = ids[i : i + batch]
        ids_chunk = ",".join(map(str, chunk))
        url = f"{org_url}/_apis/wit/workitems?ids={ids_chunk}&$expand=all&api-version={API_VERSION}"
        resp = requests.get(url, auth=("", pat))
        resp.raise_for_status()
        data = resp.json()
        results.extend(data.get("value", []))
    return results


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_wiql(project: str, created_before: datetime, changed_before: datetime) -> str:
    # WIQL using date-only precision (YYYY-MM-DD) because some ADO instances
    # reject datetime precision in WIQL comparisons. Use the UTC date part.
    def date_only(d: datetime) -> str:
        return d.astimezone(timezone.utc).date().isoformat()

    created_str = date_only(created_before)
    changed_str = date_only(changed_before)
    wiql = textwrap.dedent(f"""
        SELECT [System.Id] FROM WorkItems
        WHERE [System.TeamProject] = '{project}'
          AND [System.WorkItemType] IN ('User Story','Product Backlog Item')
          AND (
                ([System.State] = 'New' AND [System.CreatedDate] <= '{created_str}')
                OR ([System.State] = 'Active' AND [System.ChangedDate] <= '{changed_str}')
              )
    """)
    return wiql.strip()


def extract_field(fields: Dict[str, Any], key: str) -> Any:
    return fields.get(key) or fields.get(key.replace('.', ' '))


def normalize_assigned(a: Any) -> Tuple[str, Optional[str]]:
    """Return display name and email if available."""
    if not a:
        return "", None
    if isinstance(a, dict):
        name = a.get("displayName") or a.get("uniqueName") or str(a)
        email = a.get("uniqueName") if re.match(r"[^@]+@[^@]+", str(a.get("uniqueName", ""))) else None
        return name, email
    s = str(a)
    # try to find email in string
    m = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", s)
    email = m.group(0) if m else None
    # try displayName extraction
    m2 = re.search(r"displayName\"?\s*[:=]\s*['\"]([^'\"]+)['\"]", s)
    name = m2.group(1) if m2 else s
    return name, email


def build_report_rows(items: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    rows = []
    now = datetime.now(timezone.utc)
    for wi in items:
        f = wi.get("fields", {})
        created = extract_field(f, "System.CreatedDate")
        changed = extract_field(f, "System.ChangedDate")
        state_changed = extract_field(f, "Microsoft.VSTS.Common.StateChangeDate") or changed
        created_dt = None
        changed_dt = None
        for dtval in (created, changed, state_changed):
            if isinstance(dtval, str):
                try:
                    # normalize
                    if dtval.endswith('Z'):
                        dtval = dtval.replace('Z', '+00:00')
                    parsed = datetime.fromisoformat(dtval)
                except Exception:
                    parsed = None
            elif isinstance(dtval, datetime):
                parsed = dtval
            else:
                parsed = None
            if parsed and created_dt is None:
                created_dt = parsed
            if parsed and changed_dt is None:
                changed_dt = parsed

        assigned_raw = extract_field(f, "System.AssignedTo")
        assigned_name, assigned_email = normalize_assigned(assigned_raw)
        area = extract_field(f, "System.AreaPath") or ""
        iteration = extract_field(f, "System.IterationPath") or ""
        tags = extract_field(f, "System.Tags") or ""

        row = {
            "ID": str(wi.get("id", "")),
            "Title": str(extract_field(f, "System.Title") or ""),
            "State": str(extract_field(f, "System.State") or ""),
            "Priority": str(extract_field(f, "Microsoft.VSTS.Common.Priority") or extract_field(f, "System.Priority") or ""),
            "CreatedDate": created_dt.isoformat() if created_dt else "",
            "ChangedDate": changed_dt.isoformat() if changed_dt else "",
            "StateChangeDate": state_changed if isinstance(state_changed, str) else (state_changed.isoformat() if isinstance(state_changed, datetime) else ""),
            # DaysStale will be computed below
            "DaysStale": "",
            "AssignedTo": assigned_name,
            "AssignedEmail": assigned_email or "",
            "AreaPath": area,
            "IterationPath": iteration,
            "Tags": tags,
            "Link": f"{app_config.ado_org_url.rstrip('/')}/{app_config.ado_project}/_workitems/edit/{wi.get('id')}"
        }

        # compute DaysStale
        try:
            state = (row.get("State") or "").lower()
            days = None
            if state == "new":
                if created_dt:
                    days = (now - created_dt).days
            else:
                # prefer StateChangeDate, then ChangedDate
                sc = None
                sc_raw = extract_field(f, "Microsoft.VSTS.Common.StateChangeDate") or state_changed
                if isinstance(sc_raw, str):
                    try:
                        if sc_raw.endswith('Z'):
                            sc_raw = sc_raw.replace('Z', '+00:00')
                        sc = datetime.fromisoformat(sc_raw)
                    except Exception:
                        sc = None
                elif isinstance(sc_raw, datetime):
                    sc = sc_raw
                if sc:
                    days = (now - sc).days
                elif changed_dt:
                    days = (now - changed_dt).days
            row["DaysStale"] = str(days) if days is not None else ""
        except Exception:
            row["DaysStale"] = ""

        rows.append(row)
    return rows


# Note: csv_write, html_for_rows, and group_by_recipient functions have been
# replaced by hierarchy-aware versions in formatters.py module


@trace_task("overlooked_stories", metadata={"source": "pm_agent"})
def main():
    # CLI args: dry-run only (no recipient override per security requirements)
    parser = argparse.ArgumentParser(description="Overlooked stories reminder with hierarchical categorization")
    parser.add_argument("--dry-run", action="store_true", help="Do not send emails; only write report files")
    parser.add_argument("--new-days", type=int, help="Override 'new' threshold days (default from config)")
    parser.add_argument("--active-days", type=int, help="Override 'active' threshold days (default from config)")
    parser.add_argument("--project", type=str, help="Override ADO project name (default from config)")
    args = parser.parse_args()
    
    # Allow disabling automated sending of overlooked stories via config.
    enabled_val = app_config.overlooked_enabled
    if not enabled_val:
        logger.info("OVERLOOKED notifications disabled via config; exiting.")
        return

    org = app_config.ado_org_url
    project = app_config.ado_project
    # allow CLI overrides for quick testing
    if args.project:
        project = args.project
    pat = get_pat()
    if not org or not project or not pat:
        logger.error("Required env vars: ADO_ORG_URL, ADO_PROJECT and ADO_PAT (or ADO_MCP_AUTH_TOKEN)")
        sys.exit(2)

    # Load allowed email recipients from config.yaml
    try:
        allowed_recipients = load_email_recipients()
        logger.info(f"Loaded {len(allowed_recipients)} allowed email recipients from config.yaml")
    except RuntimeError as e:
        logger.error(f"ERROR: {e}")
        sys.exit(2)

    # If a config override for a single consolidated recipient is provided,
    # only allow it if the address is present in config.yaml (security requirement).
    # First check environment variable (passed from chat handler), then fall back to config
    env_consolidated = os.environ.get("OVERLOOKED_SEND_TO")
    if not env_consolidated:
        if isinstance(app_config.overlooked_send_to, list):
            env_consolidated = ",".join(app_config.overlooked_send_to)
        else:
            env_consolidated = app_config.overlooked_send_to
    
    if env_consolidated:
        # normalize and validate - can be comma-separated list
        env_consolidated = env_consolidated.strip()
        if env_consolidated:
            from overlooked_user_stories.config_reader import validate_recipient
            # Split by comma and validate each email
            requested_emails = [e.strip() for e in env_consolidated.split(",") if e.strip()]
            validated_emails = []
            
            for email in requested_emails:
                if validate_recipient(email, allowed_recipients):
                    validated_emails.append(email)
                else:
                    logger.error(f"ERROR: Email {email} from OVERLOOKED_SEND_TO is not listed in config.yaml; aborting to preserve recipient safety.")
                    sys.exit(2)
            
            if validated_emails:
                allowed_recipients = validated_emails
                logger.info(f"OVERLOOKED_SEND_TO override in environment detected — sending to: {', '.join(validated_emails)}")
            else:
                logger.error(f"ERROR: No valid emails found in OVERLOOKED_SEND_TO")
                sys.exit(2)

    # thresholds (allow CLI overrides for testing)
    new_days = app_config.overlooked_new_threshold_days
    active_days = app_config.overlooked_active_threshold_days
    if args.new_days is not None:
        new_days = int(args.new_days)
    if args.active_days is not None:
        active_days = int(args.active_days)

    # compute boundary datetimes
    now = datetime.now(timezone.utc)
    created_before = now - timedelta(days=new_days)
    changed_before = now - timedelta(days=active_days)

    wiql = build_wiql(project, created_before, changed_before)
    logger.info("Running WIQL to find candidate work items...")
    ids = run_wiql(org, wiql, pat, project=project)
    logger.info(f"Found {len(ids)} candidate work items")
    workitems = fetch_workitems(org, ids, pat)
    rows = build_report_rows(workitems)

    # heuristic: filter out items that are in an iteration path that looks active (has backslash after project)
    shortlisted = []
    for r in rows:
        iterp = r.get("IterationPath","")
        # Treat as not assigned when iteration is empty or equals project root
        if not iterp or iterp.strip() == project:
            shortlisted.append(r)
        else:
            # still include items that are Active and stale even if iteration assigned
            state = r.get("State","")
            changed = r.get("ChangedDate", "")
            try:
                ch_dt = datetime.fromisoformat(changed)
            except Exception:
                ch_dt = None
            if state.lower() == "active" and ch_dt and ch_dt <= changed_before:
                shortlisted.append(r)

    if not shortlisted:
        logger.info("No overlooked stories found with current thresholds.")
        return

    logger.info(f"Shortlisted {len(shortlisted)} overlooked stories")
    
    # Build hierarchical structure
    logger.info("Building hierarchical structure (Epic → Feature → Story)...")
    enriched_rows, hierarchy = build_complete_hierarchy(shortlisted, workitems, org, pat)
    
    # Generate categorized summary
    logger.info("Generating categorized summary...")
    summary = generate_categorized_summary(hierarchy)
    summary_text = format_summary_text(summary, project)
    summary_html = format_summary_html(summary, project)
    summary_ui = format_summary_for_ui(summary, project)
    
    # Print summary to console
    logger.info("\n" + summary_text)
    
    # Prepare output directory and timestamp
    out_dir = Path("outputs")
    out_dir.mkdir(exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    
    # Write consolidated report with hierarchy
    logger.info(f"\nWriting consolidated report for {len(allowed_recipients)} recipients...")
    all_csv = out_dir / f"overlooked_stories_{project}_ALL_{ts}.csv"
    all_html = out_dir / f"overlooked_stories_{project}_ALL_{ts}.html"
    all_summary_txt = out_dir / f"overlooked_summary_{project}_ALL_{ts}.txt"
    
    # Write CSV with Epic and Feature columns
    csv_write_with_hierarchy(enriched_rows, all_csv)
    
    # Build header with project/recipient/total/ts similar to screenshot
    recipient_label = allowed_recipients[0] if len(allowed_recipients) == 1 else ", ".join(allowed_recipients)
    header_html = (
        '<div style="background:#efefef;padding:10px;border-radius:4px;margin-bottom:12px;">'
        + f"<p><strong>Project:</strong> {html.escape(project)}<br/>"
        + f"<strong>Consolidated Recipient:</strong> {html.escape(recipient_label)}<br/>"
        + f"<strong>Total Items:</strong> {summary['total_stories']}<br/>"
        + f"<strong>Generated:</strong> {ts}<br/></p>"
        + '</div>'
    )

    # Write hierarchical HTML report (include header_html before summary)
    hierarchical_html_full = generate_hierarchical_html(
        hierarchy,
        f"Overlooked User Stories — {project} (Hierarchical)",
        header_html + summary_html,
    )
    all_html.write_text(hierarchical_html_full, encoding="utf-8")
    
    # Write summary text file
    all_summary_txt.write_text(summary_text, encoding="utf-8")
    
    # Prepare email body (summary only, details in attachments)
    email_body_html = summary_html + (
        '<hr style="margin: 30px 0;"/>'
        '<p style="color: #666; font-size: 14px;">'
        'Full details are available in the attached CSV and HTML reports.'
        '</p>'
    )
    
    subject = f"Overlooked User Stories Report — {project} — {summary['total_stories']} stories ({summary['epic_count']} epics, {summary['feature_count']} features)"
    
    # Send to all allowed recipients from config.yaml
    if args.dry_run:
        logger.info(f"\n[DRY-RUN] Would send consolidated report to {len(allowed_recipients)} recipients:")
        for recipient in allowed_recipients:
            logger.info(f"  - {recipient}")
        logger.info(f"\nAttachments: {all_csv.name}, {all_html.name}, {all_summary_txt.name}")
        logger.info("\nEmail body preview:")
        logger.info("=" * 80)
        logger.info(email_body_html[:500] + "..." if len(email_body_html) > 500 else email_body_html)
        logger.info("=" * 80)
    else:
        try:
            logger.info(f"\nSending consolidated report to {len(allowed_recipients)} recipients...")
            ok, msg = send_report_attachment(
                allowed_recipients,
                subject,
                email_body_html,
                [all_csv, all_html, all_summary_txt]
            )
            logger.info(f"Consolidated report sent: success={ok} msg={msg}")
            
            # Print summary for UI consumption
            logger.info("\n" + "=" * 80)
            logger.info("CONSOLIDATED REPORT SUCCESS")
            logger.info("=" * 80)
            logger.info(summary_ui)
            logger.info(f"\nRecipients: {', '.join(allowed_recipients)}")
            logger.info(f"Files: {all_csv.name}, {all_html.name}")
            logger.info("=" * 80)
        except Exception as e:
            logger.error(f"Failed to send consolidated email: {e}")
            import traceback
            traceback.print_exc()


if __name__ == "__main__":
    main()
