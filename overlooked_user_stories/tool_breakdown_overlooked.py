"""
Overlooked User Stories Tool Breakdown
Exposes 8 focused tools for LLM orchestration instead of one monolithic subprocess.

This enables flexible workflows where the LLM can:
- Ask clarifying questions between steps
- Show intermediate results
- Handle errors granularly
- Compose different workflows based on user needs

Architecture mirrors billing_deviation/tools_breakdown.py pattern.
"""

import logging
import re
import os
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime, timezone, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


# ============================================================================
# Phase 1: Query Understanding & Parameter Extraction
# ============================================================================

def parse_overlooked_query(query: str) -> Dict[str, Any]:
    """
    Extract project, stale days threshold, area paths, and other filters from user's query.
    Resolves area paths to canonical ADO format.
    
    Args:
        query: User's natural language query
        
    Returns:
        Dictionary with extracted parameters:
        {
            "project": "FracPro-OPS" or None,
            "new_threshold_days": 90,
            "active_threshold_days": 60,
            "area_paths": ["Global Management\\WTT Development\\XOPS 25"],
            "send_email": False,
            "recipient_email": None,
            "preview_only": False
        }
    """
    try:
        from config import config as app_config
        from utilities.area_path_resolver import resolve_area_path
        
        result = {
            "project": app_config.ado_project,
            "new_threshold_days": app_config.overlooked_new_threshold_days,
            "active_threshold_days": app_config.overlooked_active_threshold_days,
            "area_paths": [],
            "send_email": False,
            "recipient_email": None,
            "preview_only": False
        }
        
        query_lower = query.lower()
        
        # Extract area paths - look for patterns like "for xops", "in ui team"
        area_patterns = [
            r"(?:for|in)\s+([a-zA-Z0-9\s\-_]+?)(?:\s+send|\s+email|\s+report|$)",
            r"area\s+(?:path\s+)?([a-zA-Z0-9\s\-_]+?)(?:\s+send|$)",
        ]
        
        for pattern in area_patterns:
            match = re.search(pattern, query, re.IGNORECASE)
            if match:
                area_raw = match.group(1).strip()
                # Filter out keywords that aren't area paths
                exclude_words = ['send', 'email', 'report', 'preview', 'generate', 
                               'show', 'list', 'get', 'find', 'the', 'all']
                area_lower = area_raw.lower().strip()
                
                if area_lower not in exclude_words:
                    logger.info(f"Extracted raw area path: {area_raw}")
                    
                    # Resolve to canonical ADO area path
                    try:
                        org_name = app_config.ado_org_url.split("/")[-1].strip() if app_config.ado_org_url else "Stratagen"
                        resolved = resolve_area_path(
                            org=org_name,
                            project=app_config.ado_project,
                            pat=app_config.ado_pat,
                            user_text=area_raw,
                            top_k=1
                        )
                        
                        if resolved.get("status") in ["ok", "likely"]:
                            canonical_path = resolved.get("choice")
                            if canonical_path:
                                result["area_paths"].append(canonical_path)
                                logger.info(f"Resolved area path: '{area_raw}' -> '{canonical_path}'")
                            else:
                                logger.warning(f"Area path resolution succeeded but no choice returned: {area_raw}")
                        elif resolved.get("status") == "ambiguous":
                            # Use top match
                            top_matches = resolved.get("top_matches", [])
                            if top_matches:
                                canonical_path = top_matches[0][0]
                                result["area_paths"].append(canonical_path)
                                logger.info(f"Resolved ambiguous area path: '{area_raw}' -> '{canonical_path}' (score: {top_matches[0][1]})\")")
                        else:
                            logger.warning(f"Could not resolve area path: '{area_raw}' (status: {resolved.get('status')})")
                    except Exception as e:
                        logger.error(f"Area path resolution failed for '{area_raw}': {e}")
                    break
        
        # Check for email addresses in query
        emails = re.findall(r"[\w.+-]+@[\w-]+\.[\w.-]+", query)
        if emails:
            result["recipient_email"] = emails[0]
            result["send_email"] = True
            logger.info(f"Extracted recipient email: {emails[0]}")
        
        # Check for "send email" or "email to" keywords
        if re.search(r"\b(send|email)\b", query_lower):
            result["send_email"] = True
        
        # Check for preview/dry-run mode
        if re.search(r"\b(preview|dry[-\s]?run|no\s+email)\b", query_lower):
            result["preview_only"] = True
            result["send_email"] = False
            logger.info("Preview mode enabled (no email)")
        
        # Extract custom stale days threshold if specified
        days_match = re.search(r"(\d+)\s+days?", query_lower)
        if days_match:
            custom_days = int(days_match.group(1))
            # Use for both thresholds if specified
            result["new_threshold_days"] = custom_days
            result["active_threshold_days"] = custom_days
            logger.info(f"Custom stale threshold: {custom_days} days")
        
        logger.info(f"Parsed overlooked query: {result}")
        return result
        
    except Exception as e:
        logger.exception(f"Error parsing overlooked query: {e}")
        return {
            "project": None,
            "new_threshold_days": 90,
            "active_threshold_days": 60,
            "area_paths": [],
            "send_email": False,
            "recipient_email": None,
            "preview_only": False,
            "error": str(e)
        }


# ============================================================================
# Phase 2: WIQL Construction
# ============================================================================

def build_overlooked_wiql(
    project: str,
    new_threshold_days: int = 90,
    active_threshold_days: int = 60,
    area_paths: Optional[List[str]] = None
) -> Dict[str, Any]:
    """
    Build WIQL query to find overlooked user stories.
    
    CRITICAL CRITERIA:
    - New items created >= new_threshold_days ago
    - Active items changed >= active_threshold_days ago
    - Work item types: User Story, Product Backlog Item
    
    Args:
        project: Azure DevOps project name
        new_threshold_days: Threshold for New items (default 90)
        active_threshold_days: Threshold for Active items (default 60)
        area_paths: Optional list of area paths to filter
        
    Returns:
        Dictionary with:
        {
            "wiql": "SELECT [System.Id] FROM WorkItems WHERE ...",
            "created_before": datetime,
            "changed_before": datetime,
            "new_threshold_days": 90,
            "active_threshold_days": 60
        }
    """
    try:
        now = datetime.now(timezone.utc)
        created_before = now - timedelta(days=new_threshold_days)
        changed_before = now - timedelta(days=active_threshold_days)
        
        # Date-only precision for WIQL compatibility
        def date_only(d: datetime) -> str:
            return d.astimezone(timezone.utc).date().isoformat()
        
        created_str = date_only(created_before)
        changed_str = date_only(changed_before)
        
        # Build WIQL query
        wiql_parts = [
            "SELECT [System.Id] FROM WorkItems",
            f"WHERE [System.TeamProject] = '{project}'",
            "AND [System.WorkItemType] IN ('User Story','Product Backlog Item')",
            "AND (",
            f"  ([System.State] = 'New' AND [System.CreatedDate] <= '{created_str}')",
            "  OR",
            f"  ([System.State] = 'Active' AND [System.ChangedDate] <= '{changed_str}')",
            ")"
        ]
        
        # Add area path filter if specified (area_paths are already canonical/resolved)
        if area_paths:
            area_conditions = []
            for area in area_paths:
                # Area paths are already in canonical format (e.g., "Global Management\WTT Development\XOPS 25")
                # Use UNDER to include child paths
                area_conditions.append(f"[System.AreaPath] UNDER '{area}'")
            if area_conditions:
                wiql_parts.append(f"AND ({' OR '.join(area_conditions)})")
        
        wiql = "\n".join(wiql_parts)
        
        logger.info(f"Built WIQL for overlooked stories: new_days={new_threshold_days}, active_days={active_threshold_days}")
        
        return {
            "wiql": wiql,
            "created_before": created_before,
            "changed_before": changed_before,
            "new_threshold_days": new_threshold_days,
            "active_threshold_days": active_threshold_days,
            "area_paths": area_paths
        }
        
    except Exception as e:
        logger.exception(f"Error building overlooked WIQL: {e}")
        return {
            "wiql": None,
            "error": str(e)
        }


# ============================================================================
# Phase 3: Work Item Retrieval
# ============================================================================

def fetch_overlooked_work_items(
    wiql: str,
    project: str,
    org_url: str,
    pat: str
) -> Dict[str, Any]:
    """
    Execute WIQL query and fetch full work item details.
    
    Args:
        wiql: WIQL query string
        project: Azure DevOps project name
        org_url: ADO organization URL
        pat: Personal Access Token
        
    Returns:
        Dictionary with:
        {
            "work_items": [...],  # Full work item objects
            "count": 42,
            "ids": [123, 456, ...]
        }
    """
    try:
        import requests
        
        API_VERSION = "7.0"
        
        # Run WIQL query
        url = f"{org_url}/{project}/_apis/wit/wiql?api-version={API_VERSION}"
        resp = requests.post(url, json={"query": wiql}, auth=("", pat), timeout=30)
        
        if resp.status_code >= 400:
            error_msg = f"WIQL request failed: {resp.status_code} {resp.text}"
            logger.error(error_msg)
            return {
                "work_items": [],
                "count": 0,
                "ids": [],
                "error": error_msg
            }
        
        data = resp.json()
        ids = [item["id"] for item in data.get("workItems", [])]
        
        if not ids:
            logger.info("No overlooked work items found")
            return {
                "work_items": [],
                "count": 0,
                "ids": []
            }
        
        logger.info(f"Found {len(ids)} candidate work items")
        
        # Fetch full work item details in batches
        batch_size = 200
        all_items = []
        
        for i in range(0, len(ids), batch_size):
            chunk = ids[i : i + batch_size]
            ids_str = ",".join(map(str, chunk))
            fetch_url = f"{org_url}/_apis/wit/workitems?ids={ids_str}&$expand=all&api-version={API_VERSION}"
            
            fetch_resp = requests.get(fetch_url, auth=("", pat), timeout=30)
            fetch_resp.raise_for_status()
            
            fetch_data = fetch_resp.json()
            all_items.extend(fetch_data.get("value", []))
        
        logger.info(f"Fetched {len(all_items)} work items with full details")
        
        return {
            "work_items": all_items,
            "count": len(all_items),
            "ids": ids
        }
        
    except Exception as e:
        logger.exception(f"Error fetching overlooked work items: {e}")
        return {
            "work_items": [],
            "count": 0,
            "ids": [],
            "error": str(e)
        }


# ============================================================================
# Phase 4: Filtering & Data Enrichment
# ============================================================================

def filter_by_iteration(
    work_items: List[Dict[str, Any]],
    project: str,
    active_threshold_days: int
) -> Dict[str, Any]:
    """
    Filter out items assigned to active iterations (unless they're stale Active items).
    
    CRITICAL LOGIC:
    - Items with no iteration or iteration = project root → INCLUDE
    - Items in iterations but State=Active and stale → INCLUDE
    - Items in active iterations and not stale → EXCLUDE
    
    Args:
        work_items: List of work item dictionaries
        project: Project name for iteration path comparison
        active_threshold_days: Threshold for active staleness
        
    Returns:
        Dictionary with:
        {
            "filtered_items": [...],
            "count": 35,
            "excluded_count": 7,
            "rows": [...]  # Formatted row dicts for reporting
        }
    """
    try:
        from datetime import datetime, timezone, timedelta
        
        now = datetime.now(timezone.utc)
        changed_before = now - timedelta(days=active_threshold_days)
        
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
        
        filtered = []
        excluded = []
        rows = []
        
        from config import config as app_config
        
        for wi in work_items:
            f = wi.get("fields", {})
            iterp = extract_field(f, "System.IterationPath") or ""
            state = extract_field(f, "System.State") or ""
            
            # Iteration filtering logic
            should_include = False
            
            if not iterp or iterp.strip() == project:
                # No iteration or project root → INCLUDE
                should_include = True
            else:
                # Has iteration assignment
                if state.lower() == "active":
                    # Check if stale (ChangedDate check)
                    changed = extract_field(f, "System.ChangedDate")
                    try:
                        if isinstance(changed, str):
                            if changed.endswith('Z'):
                                changed = changed.replace('Z', '+00:00')
                            ch_dt = datetime.fromisoformat(changed)
                        elif isinstance(changed, datetime):
                            ch_dt = changed
                        else:
                            ch_dt = None
                        
                        if ch_dt and ch_dt <= changed_before:
                            # Active and stale → INCLUDE
                            should_include = True
                    except Exception:
                        pass
            
            if should_include:
                filtered.append(wi)
                
                # Build row dictionary for reporting
                created = extract_field(f, "System.CreatedDate")
                changed = extract_field(f, "System.ChangedDate")
                state_changed = extract_field(f, "Microsoft.VSTS.Common.StateChangeDate") or changed
                
                created_dt = None
                changed_dt = None
                for dtval in (created, changed, state_changed):
                    if isinstance(dtval, str):
                        try:
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
                    "State": state,
                    "Priority": str(extract_field(f, "Microsoft.VSTS.Common.Priority") or extract_field(f, "System.Priority") or ""),
                    "CreatedDate": created_dt.isoformat() if created_dt else "",
                    "ChangedDate": changed_dt.isoformat() if changed_dt else "",
                    "StateChangeDate": state_changed if isinstance(state_changed, str) else (state_changed.isoformat() if isinstance(state_changed, datetime) else ""),
                    "DaysStale": "",
                    "AssignedTo": assigned_name,
                    "AssignedEmail": assigned_email or "",
                    "AreaPath": area,
                    "IterationPath": iteration,
                    "Tags": tags,
                    "Link": f"{app_config.ado_org_url.rstrip('/')}/{app_config.ado_project}/_workitems/edit/{wi.get('id')}"
                }
                
                # Compute DaysStale
                try:
                    days = None
                    if state.lower() == "new":
                        if created_dt:
                            days = (now - created_dt).days
                    else:
                        # Prefer StateChangeDate, then ChangedDate
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
            else:
                excluded.append(wi)
        
        logger.info(f"Filtered {len(filtered)} items, excluded {len(excluded)} items in active iterations")
        
        return {
            "filtered_items": filtered,
            "count": len(filtered),
            "excluded_count": len(excluded),
            "rows": rows
        }
        
    except Exception as e:
        logger.exception(f"Error filtering by iteration: {e}")
        return {
            "filtered_items": work_items,
            "count": len(work_items),
            "excluded_count": 0,
            "rows": [],
            "error": str(e)
        }


# ============================================================================
# Phase 5: Hierarchy Building
# ============================================================================

def build_hierarchy(
    rows: List[Dict[str, str]],
    work_items: List[Dict[str, Any]],
    org_url: str,
    pat: str
) -> Dict[str, Any]:
    """
    Build Epic → Feature → Story hierarchy structure.
    
    Uses overlooked_user_stories.hierarchy module logic.
    
    Args:
        rows: Report row dictionaries
        work_items: Full work item objects
        org_url: ADO organization URL
        pat: Personal Access Token
        
    Returns:
        Dictionary with:
        {
            "enriched_rows": [...],  # Rows with EpicTitle, FeatureTitle
            "hierarchy": {...},  # Nested dict {epic: {feature: [rows]}}
            "epic_count": 3,
            "feature_count": 12
        }
    """
    try:
        from overlooked_user_stories.hierarchy import build_complete_hierarchy
        
        enriched_rows, hierarchy = build_complete_hierarchy(rows, work_items, org_url, pat)
        
        epic_count = len(hierarchy)
        feature_count = sum(len(features) for features in hierarchy.values())
        
        logger.info(f"Built hierarchy: {epic_count} epics, {feature_count} features, {len(enriched_rows)} stories")
        
        return {
            "enriched_rows": enriched_rows,
            "hierarchy": hierarchy,
            "epic_count": epic_count,
            "feature_count": feature_count
        }
        
    except Exception as e:
        logger.exception(f"Error building hierarchy: {e}")
        return {
            "enriched_rows": rows,
            "hierarchy": {},
            "epic_count": 0,
            "feature_count": 0,
            "error": str(e)
        }


# ============================================================================
# Phase 6: Summary Generation
# ============================================================================

def generate_summary(
    hierarchy: Dict[str, Dict[str, List[Dict[str, str]]]],
    project: str
) -> Dict[str, Any]:
    """
    Generate categorized summary from hierarchical structure.
    
    Args:
        hierarchy: Nested dict {epic: {feature: [rows]}}
        project: Project name
        
    Returns:
        Dictionary with:
        {
            "summary": {...},  # Summary statistics
            "text": "...",  # Plain text summary
            "html": "...",  # HTML summary
            "ui": "..."  # UI-friendly markdown
        }
    """
    try:
        from overlooked_user_stories.summary import (
            generate_categorized_summary,
            format_summary_text,
            format_summary_html,
            format_summary_for_ui
        )
        
        summary = generate_categorized_summary(hierarchy)
        summary_text = format_summary_text(summary, project)
        summary_html = format_summary_html(summary, project)
        summary_ui = format_summary_for_ui(summary, project)
        
        logger.info(f"Generated summary: {summary['total_stories']} stories, {summary['epic_count']} epics")
        
        return {
            "summary": summary,
            "text": summary_text,
            "html": summary_html,
            "ui": summary_ui
        }
        
    except Exception as e:
        logger.exception(f"Error generating summary: {e}")
        return {
            "summary": {"total_stories": 0, "epic_count": 0, "feature_count": 0},
            "text": f"Error generating summary: {str(e)}",
            "html": f"<p>Error: {str(e)}</p>",
            "ui": f"Error: {str(e)}",
            "error": str(e)
        }


# ============================================================================
# Phase 7: Report File Generation
# ============================================================================

def generate_report_files(
    enriched_rows: List[Dict[str, str]],
    hierarchy: Dict[str, Dict[str, List[Dict[str, str]]]],
    summary_text: str,
    summary_html: str,
    project: str
) -> Dict[str, Any]:
    """
    Generate CSV and HTML report files.
    
    Args:
        enriched_rows: Rows with hierarchy information
        hierarchy: Epic → Feature → Story structure
        summary_text: Plain text summary
        summary_html: HTML summary
        project: Project name
        
    Returns:
        Dictionary with:
        {
            "csv_path": "/path/to/report.csv",
            "html_path": "/path/to/report.html",
            "summary_path": "/path/to/summary.txt",
            "timestamp": "20260116T120000Z"
        }
    """
    try:
        from overlooked_user_stories.formatters import csv_write_with_hierarchy
        from overlooked_user_stories.summary import generate_hierarchical_html
        
        # Prepare output directory
        out_dir = Path("outputs")
        out_dir.mkdir(exist_ok=True)
        
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        
        # Write CSV
        csv_path = out_dir / f"overlooked_stories_{project}_ALL_{ts}.csv"
        csv_write_with_hierarchy(enriched_rows, csv_path)
        
        # Write HTML report with hierarchy
        from config import config as app_config
        
        # Build header
        header_html = (
            '<div style="background:#efefef;padding:10px;border-radius:4px;margin-bottom:12px;">'
            + f"<p><strong>Project:</strong> {project}<br/>"
            + f"<strong>Total Items:</strong> {len(enriched_rows)}<br/>"
            + f"<strong>Generated:</strong> {ts}<br/></p>"
            + '</div>'
        )
        
        hierarchical_html = generate_hierarchical_html(
            hierarchy,
            f"Overlooked User Stories — {project} (Hierarchical)",
            header_html + summary_html,
        )
        
        html_path = out_dir / f"overlooked_stories_{project}_ALL_{ts}.html"
        html_path.write_text(hierarchical_html, encoding="utf-8")
        
        # Write summary text file
        summary_path = out_dir / f"overlooked_summary_{project}_ALL_{ts}.txt"
        summary_path.write_text(summary_text, encoding="utf-8")
        
        logger.info(f"Generated report files: CSV={csv_path.name}, HTML={html_path.name}")
        
        return {
            "csv_path": str(csv_path),
            "html_path": str(html_path),
            "summary_path": str(summary_path),
            "timestamp": ts
        }
        
    except Exception as e:
        logger.exception(f"Error generating report files: {e}")
        return {
            "csv_path": None,
            "html_path": None,
            "summary_path": None,
            "error": str(e)
        }


# ============================================================================
# Phase 8: Email Sending
# ============================================================================

def send_overlooked_email(
    recipient_emails: List[str],
    text_summary: str,
    html_report_path: Optional[str] = None,
    csv_path: Optional[str] = None,
    summary_path: Optional[str] = None,
    project: str = None,
    total_stories: int = 0,
    epic_count: int = 0,
    feature_count: int = 0
) -> Dict[str, Any]:
    """
    Send overlooked stories email with attachments.
    
    Validates recipients against config.yaml allowlist.
    
    Args:
        recipient_emails: List of email addresses (validated against config)
        text_summary: Plain text summary
        html_report_path: Path to HTML report file
        csv_path: Path to CSV file
        summary_path: Path to summary text file
        project: Project name for subject
        total_stories: Total story count for subject
        epic_count: Epic count for subject
        feature_count: Feature count for subject
        
    Returns:
        Dictionary with:
        {
            "success": boolean,
            "message": "Status message",
            "action": "sent" | "validation_failed" | "error",
            "sent_to": [...]
        }
    """
    try:
        from overlooked_user_stories.config_reader import load_email_recipients, validate_recipient
        from utilities.emailer import send_report_attachment
        
        # Load allowed recipients from config
        allowed_recipients = load_email_recipients()
        
        # Validate each recipient
        validated_recipients = []
        invalid_recipients = []
        
        for email in recipient_emails:
            if validate_recipient(email, allowed_recipients):
                validated_recipients.append(email)
            else:
                invalid_recipients.append(email)
                logger.warning(f"Invalid recipient (not in config.yaml): {email}")
        
        if invalid_recipients:
            error_msg = f"Recipients not in config.yaml: {', '.join(invalid_recipients)}"
            logger.error(error_msg)
            return {
                "success": False,
                "message": error_msg,
                "action": "validation_failed",
                "sent_to": [],
                "invalid": invalid_recipients
            }
        
        if not validated_recipients:
            return {
                "success": False,
                "message": "No valid recipients provided",
                "action": "validation_failed",
                "sent_to": []
            }
        
        # Prepare email
        subject = f"Overlooked User Stories Report — {project} — {total_stories} stories ({epic_count} epics, {feature_count} features)"
        
        # Build email body with summary
        email_body_html = text_summary.replace('\n', '<br/>') + (
            '<hr style="margin: 30px 0;"/>'
            '<p style="color: #666; font-size: 14px;">'
            'Full details are available in the attached CSV and HTML reports.'
            '</p>'
        )
        
        # Prepare attachments
        attachments = []
        if csv_path and os.path.exists(csv_path):
            attachments.append(csv_path)
        if html_report_path and os.path.exists(html_report_path):
            attachments.append(html_report_path)
        if summary_path and os.path.exists(summary_path):
            attachments.append(summary_path)
        
        # Send to each recipient
        sent_count = 0
        for recipient in validated_recipients:
            try:
                send_report_attachment(
                    recipient_email=recipient,
                    subject=subject,
                    body_text=text_summary,
                    body_html=email_body_html,
                    attachment_paths=attachments
                )
                sent_count += 1
                logger.info(f"Sent overlooked stories email to {recipient}")
            except Exception as e:
                logger.error(f"Failed to send email to {recipient}: {e}")
        
        if sent_count > 0:
            return {
                "success": True,
                "message": f"Email sent successfully to {sent_count} recipient(s)",
                "action": "sent",
                "sent_to": validated_recipients
            }
        else:
            return {
                "success": False,
                "message": "Failed to send email to any recipients",
                "action": "error",
                "sent_to": []
            }
        
    except Exception as e:
        logger.exception(f"Error sending overlooked email: {e}")
        return {
            "success": False,
            "message": f"Error sending email: {str(e)}",
            "action": "error",
            "sent_to": []
        }
