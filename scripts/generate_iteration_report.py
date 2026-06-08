#!/usr/bin/env python3
"""Generate iteration report CSV for specified project, teams (areas), and work item types.

Usage: set environment variables and run:
        ADO_ORG_URL=https://dev.azure.com/yourorg \
        ADO_PROJECT="FracPro-OPS" \
        ADO_TEAM="MyTeam" \
        ITERATION_PATH="FracPro-OPS\\Sprint 12" \
        AREAS="FracPro-OPS\\Xops bugs enhancement,FracPro-OPS\\Xops 25" \
    ADO_PAT=xxxx python scripts/generate_iteration_report.py

Notes:
- When using WIQL macros like `@CurrentIteration`, you must provide the `ADO_TEAM` (team) so the WIQL endpoint can resolve the macro correctly.
- If `@CurrentIteration` (or other team-scoped macros) is used and no team is provided, the script will raise an informative error.

The script writes CSV to outputs/iteration_report_<iteration>_<timestamp>.csv
"""
import os
import sys
import csv
from pathlib import Path
import requests
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional, Tuple
import html
import textwrap

SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from utilities.mcp.pat import get_pat
from config import config
from utilities.langfuse_client import trace_task

API_VERSION = "7.0"

# Default WIQL requested by the stakeholder; used whenever no custom WIQL
# override is supplied via environment variables.
DEFAULT_WIQL = textwrap.dedent(r"""
    SELECT
        [System.Id],
        [System.AreaPath],
        [System.WorkItemType],
        [System.Title],
        [System.AssignedTo],
        [System.State],
        [Microsoft.VSTS.Common.ActivatedDate],
        [Custom.QADeployDate],
        [Custom.UATScheduledDeploymentDate],
        [Custom.UATDeployDate],
        [Custom.PreProdDeployDate],
        [Custom.PRODScheduledDeployment],
        [Custom.PRODDeployDate]
    FROM workitems
    WHERE
        [System.TeamProject] = 'FracPro-OPS'
        AND (
            [System.IterationPath] UNDER @currentIteration('[FracPro-OPS]\XOPS Bugs Enhancement <id:554c7ad2-f718-45d5-8ace-62655d672cc8>')
            OR [System.IterationPath] UNDER @currentIteration('[FracPro-OPS]\XOPS 25 <id:f3d2da3c-6c37-4c08-a478-5f398b3e5123>')
            AND [System.WorkItemType] IN ('Bug', 'User Story')
        )
    """).strip()


def build_wiql(project: str, iteration: str, areas: List[str], wi_types: List[str]) -> str:
    area_clause = ""
    if areas:
        # AreaPath values in ADO are often fully qualified; use explicit equality
        clauses = [f"[System.AreaPath] = '{a}'" for a in areas]
        area_clause = " AND (" + " OR ".join(clauses) + ")"
    types_esc = ",".join(f"'{t}'" for t in wi_types)
    wiql = (
        f"SELECT [System.Id] FROM WorkItems "
        f"WHERE [System.TeamProject] = '{project}' "
        f"AND [System.IterationPath] = '{iteration}' "
        f"{area_clause} AND [System.WorkItemType] IN ({types_esc})"
    )
    return wiql


def run_wiql(org_url: str, wiql: str, pat: str, project: Optional[str] = None, team: Optional[str] = None) -> List[int]:
    # Use the most specific WIQL endpoint available so macros like @CurrentIteration resolve.
    if project and team:
        url = f"{org_url}/{project}/{team}/_apis/wit/wiql?api-version={API_VERSION}"
    elif project:
        url = f"{org_url}/{project}/_apis/wit/wiql?api-version={API_VERSION}"
    else:
        url = f"{org_url}/_apis/wit/wiql?api-version={API_VERSION}"

    resp = requests.post(url, json={"query": wiql}, auth=("", pat))
    # include response body on error to aid debugging (Azure DevOps returns helpful messages)
    if resp.status_code >= 400:
        body = resp.text
        proj_info = f" for project '{project}'" if project else ""
        team_info = f" team '{team}'" if team else ""
        raise RuntimeError(f"WIQL request failed{proj_info}{team_info}: {resp.status_code} {body}")
    data = resp.json()
    ids = [item["id"] for item in data.get("workItems", [])]
    return ids


def fetch_workitems(org_url: str, project: str, ids: List[int], pat: str) -> List[Dict[str, Any]]:
    if not ids:
        return []
    # Azure DevOps limits -- batch if large
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


def pick_date_field(fields: Dict[str, Any], candidates: List[str]) -> Optional[str]:
    for c in candidates:
        if c in fields and fields[c]:
            return fields[c]
    # fallback: search any key containing candidate substring
    for key, val in fields.items():
        kl = key.lower()
        for sub in candidates:
            if sub.lower() in kl and val:
                return val
    return None


def extract_row(wi: Dict[str, Any]) -> Dict[str, str]:
    f = wi.get("fields", {})
    # direct fields requested in your sample
    area = f.get("System.AreaPath", "")
    wi_type = f.get("System.WorkItemType", "")
    title = f.get("System.Title", "")
    assigned = f.get("System.AssignedTo", "")
    state = f.get("System.State", "")

    # explicit custom fields from your WIQL
    activated = f.get("Microsoft.VSTS.Common.ActivatedDate") or f.get("System.ActivatedDate")
    qa_deploy = f.get("Custom.QADeployDate") or f.get("Custom.QADeploy") or f.get("QA Deploy")
    uat_sched = f.get("Custom.UATScheduledDeploymentDate") or f.get("Custom.UATSchedule") or f.get("UAT Schedule")
    uat_deploy = f.get("Custom.UATDeployDate") or f.get("Custom.UATDeploy") or f.get("UAT Deploy")
    preprod = f.get("Custom.PreProdDeployDate")
    prod_sched = f.get("Custom.PRODScheduledDeployment")
    prod_deploy = f.get("Custom.PRODDeployDate") or f.get("Custom.PRODDeploy")

    def fmt_local(d):
        if not d:
            return ""
        try:
            if isinstance(d, str):
                dt = datetime.fromisoformat(d.replace("Z", "+00:00"))
                # convert to local timezone and format like: 11/20/2025 11:46 PM
                local = dt.astimezone()
                return local.strftime("%m/%d/%Y %I:%M %p")
            return str(d)
        except Exception:
            return str(d)

    def parse_dt(d):
        if not d:
            return None
        try:
            if isinstance(d, str):
                return datetime.fromisoformat(d.replace("Z", "+00:00"))
            if isinstance(d, datetime):
                return d
        except Exception:
            try:
                return datetime.fromisoformat(str(d))
            except Exception:
                return None
        return None

    def normalize_assigned(a):
        # Assigned can be a dict, or a stringified dict/JSON. Extract displayName when possible.
        if not a:
            return ""
        if isinstance(a, dict):
            return a.get("displayName") or a.get("uniqueName") or str(a)
        if isinstance(a, str):
            # Try JSON first
            import json
            import ast
            try:
                parsed = json.loads(a)
            except Exception:
                try:
                    parsed = ast.literal_eval(a)
                except Exception:
                    parsed = None
            if isinstance(parsed, dict):
                return parsed.get("displayName") or parsed.get("uniqueName") or str(parsed)
            # Regex fallback if not parsed
            import re
            m = re.search(r"displayName\"?\s*[:=]\s*['\"]([^'\"}]+)", a)
            if m:
                return m.group(1).strip()
        return str(a)

    # compute status flags
    now = datetime.now(timezone.utc)
    uat_dt = parse_dt(uat_sched)
    uat_actual_dt = parse_dt(uat_deploy)
    prod_dt = parse_dt(prod_sched)
    prod_actual_dt = parse_dt(prod_deploy)

    def status_for(sched_dt, actual_dt, state_val):
        # Only flag if not closed and scheduled date exists and actual missing
        if not sched_dt:
            return ""
        if actual_dt:
            return ""
        if isinstance(state_val, str) and state_val.lower() == "closed":
            return ""
        # normalize sched_dt to UTC-aware datetime
        try:
            if isinstance(sched_dt, datetime):
                if sched_dt.tzinfo is None:
                    sched_dt = sched_dt.replace(tzinfo=timezone.utc)
                else:
                    sched_dt = sched_dt.astimezone(timezone.utc)
        except Exception:
            pass
        days = (now - sched_dt).days if isinstance(sched_dt, datetime) else 0
        if days > 5:
            return "red"
        if days > 2:
            return "yellow"
        return ""

    uat_status = status_for(uat_dt, uat_actual_dt, state)
    prod_status = status_for(prod_dt, prod_actual_dt, state)

    return {
        "WI_ID": str(wi.get("id", "")),
        "Area Path": area,
        "Work Item Type": wi_type,
        "Title": title,
        "Assigned To": normalize_assigned(assigned),
        "State": state,
        "Activated Date": fmt_local(activated),
        "QA Deploy Date": fmt_local(qa_deploy),
        "UAT Scheduled Deployment Date": fmt_local(uat_sched),
        "UAT Deploy Date": fmt_local(uat_deploy),
        "Pre Prod Deploy Date": fmt_local(preprod),
        "PROD Scheduled Deployment": fmt_local(prod_sched),
        "PROD Deploy Date": fmt_local(prod_deploy),
        # compute status flags (empty / yellow / red) for scheduled fields when actual date missing
        "UAT Status": uat_status,
        "PROD Status": prod_status,
    }


def generate_report(
    org_url: str,
    pat: str,
    project: str = "FracPro-OPS",
    team: Optional[str] = None,
    iteration: Optional[str] = None,
    areas: Optional[List[str]] = None,
    wi_types: Optional[List[str]] = None,
    wiql_text: Optional[str] = None,
    wiql_file: Optional[str] = None,
    outputs_dir: str = "outputs",
    areas_filter: Optional[List[str]] = None,
    types_filter: Optional[List[str]] = None,
) -> Tuple[str, Optional[str], List[Dict[str, str]], List[Dict[str, str]], Optional[str]]:
    """
    Programmatic API for generating iteration reports.

    Returns (full_csv_path, filtered_csv_path_or_None, all_rows, filtered_rows).
    """

    areas = areas or []
    wi_types = wi_types or ["User Story", "Bug"]
    areas_filter = areas_filter or []
    types_filter = types_filter or []

    # Runtime sanity check: log project being used and warn on suspicious values
    print(f"Using project value: '{project}'")
    if project and " " in project:
        print("WARNING: project contains spaces which may be invalid in ADO project names.")
        print("If this is unexpected, set the ADO_PROJECT env var or the Project field in the UI to the correct value (e.g. 'FracPro-OPS').")

    # If WIQL provided directly, use it. Otherwise construct WIQL from project/iteration.
    if wiql_text:
        wiql = wiql_text
    elif wiql_file:
        with open(wiql_file, "r", encoding="utf-8") as f:
            wiql = f.read()
    else:
        wiql = DEFAULT_WIQL

    ids = run_wiql(org_url, wiql, pat, project=project, team=team)
    workitems = fetch_workitems(org_url, project, ids, pat)
    rows = [extract_row(wi) for wi in workitems]

    def ts() -> str:
        return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    def safe(name: str) -> str:
        return name.replace("/", "_").replace("\\", "_").replace(" ", "_")

    os.makedirs(outputs_dir, exist_ok=True)
    safe_iter = safe(iteration) if iteration else "custom_wiql"

    filtered_rows: List[Dict[str, str]] = []
    filtered_file: Optional[str] = None
    if areas_filter or types_filter:
        def matches(r: Dict[str, str]) -> bool:
            ok_area = True
            ok_type = True
            if areas_filter:
                ok_area = any(r.get("Area Path", "").startswith(a) or r.get("Area Path", "") == a for a in areas_filter)
            if types_filter:
                ok_type = r.get("Work Item Type", "") in types_filter
            return ok_area and ok_type

        filtered_rows = [r for r in rows if matches(r)]
        if filtered_rows:
            filtered_file = os.path.join(outputs_dir, f"iteration_report_filtered_{safe_iter}_{ts()}.csv")
            generate_csv(filtered_rows, filtered_file)

    out_file = os.path.join(outputs_dir, f"iteration_report_{safe_iter}_{ts()}.csv")
    generate_csv(rows, out_file)
    # also produce a colorized HTML report so colors persist in outputs/
    try:
        html_file = os.path.join(outputs_dir, f"iteration_report_{safe_iter}_{ts()}.html")
        generate_html_report(rows, html_file)
    except Exception:
        html_file = None
    return out_file, filtered_file, rows, filtered_rows, html_file


def cell_style_for(status: str) -> str:
    if status == 'yellow':
        return 'background-color:#fff3cd'
    if status == 'red':
        return 'background-color:#f8d7da'
    return ''


def generate_html_report(rows: List[Dict[str, str]], out_path: str):
        # Simple HTML table with inline styles for UAT/PROD status cells
        headers = [
                'S.No.', 'ID', 'Area Path', 'Work Item Type', 'Title', 'Assigned To', 'State',
                'Activated Date', 'QA Deploy Date', 'UAT Scheduled Deployment Date', 'UAT Deploy Date',
                'UAT Status', 'Pre Prod Deploy Date', 'PROD Scheduled Deployment', 'PROD Deploy Date', 'PROD Status'
        ]
        rows_html = []
        for i, r in enumerate(rows, start=1):
                cells = []
                cells.append(f"<td>{i}</td>")
                cells.append(f"<td>{html.escape(r.get('WI_ID',''))}</td>")
                cells.append(f"<td>{html.escape(r.get('Area Path',''))}</td>")
                cells.append(f"<td>{html.escape(r.get('Work Item Type',''))}</td>")
                cells.append(f"<td>{html.escape(r.get('Title',''))}</td>")
                cells.append(f"<td>{html.escape(r.get('Assigned To',''))}</td>")
                cells.append(f"<td>{html.escape(r.get('State',''))}</td>")
                cells.append(f"<td>{html.escape(r.get('Activated Date',''))}</td>")
                cells.append(f"<td>{html.escape(r.get('QA Deploy Date',''))}</td>")

                # UAT scheduled + status cell with style
                uat_sched = html.escape(r.get('UAT Scheduled Deployment Date',''))
                uat_status = r.get('UAT Status','')
                uat_style = cell_style_for(uat_status)
                cells.append(f"<td style=\"{uat_style}\">{uat_sched}</td>")
                cells.append(f"<td>{html.escape(r.get('UAT Deploy Date',''))}</td>")
                cells.append(f"<td>{html.escape(uat_status)}</td>")

                cells.append(f"<td>{html.escape(r.get('Pre Prod Deploy Date',''))}</td>")

                # PROD scheduled + status cell with style
                prod_sched = html.escape(r.get('PROD Scheduled Deployment',''))
                prod_status = r.get('PROD Status','')
                prod_style = cell_style_for(prod_status)
                cells.append(f"<td style=\"{prod_style}\">{prod_sched}</td>")
                cells.append(f"<td>{html.escape(r.get('PROD Deploy Date',''))}</td>")
                cells.append(f"<td>{html.escape(prod_status)}</td>")

                rows_html.append('<tr>' + '\n'.join(cells) + '</tr>')

        html_doc = (
                '<!doctype html>'
                '<html>'
                '<head>'
                "<meta charset='utf-8'/>"
                '<title>Iteration Report</title>'
                '<style>table { border-collapse: collapse; width: 100%; font-family: Arial, sans-serif; font-size:12px } '
                'th, td { border: 1px solid #ddd; padding: 6px; text-align: left } '
                'th { background: #f2f2f2 }</style>'
                '</head>'
                '<body>'
                '<h2>Iteration Report</h2>'
                '<table>'
                '<thead>' + ''.join(f'<th>{h}</th>' for h in headers) + '</thead>'
                '<tbody>' + ''.join(rows_html) + '</tbody>'
                '</table>'
                '</body>'
                '</html>'
        )

        with open(out_path, 'w', encoding='utf-8') as fh:
                fh.write(html_doc)


def generate_csv(rows: List[Dict[str, str]], out_path: str):
    headers = [
        "S.No.",
        "ID",
        "Area Path",
        "Work Item Type",
        "Title",
        "Assigned To",
        "State",
        "Activated Date",
        "QA Deploy Date",
        "UAT Scheduled Deployment Date",
        "UAT Deploy Date",
        "UAT Status",
        "Pre Prod Deploy Date",
        "PROD Scheduled Deployment",
        "PROD Deploy Date",
        "PROD Status",
    ]
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for i, r in enumerate(rows, start=1):
            w.writerow([
                i,
                r.get("WI_ID", ""),
                r.get("Area Path", ""),
                r.get("Work Item Type", ""),
                r.get("Title", ""),
                r.get("Assigned To", ""),
                r.get("State", ""),
                r.get("Activated Date", ""),
                r.get("QA Deploy Date", ""),
                r.get("UAT Scheduled Deployment Date", ""),
                r.get("UAT Deploy Date", ""),
                r.get("UAT Status", ""),
                r.get("Pre Prod Deploy Date", ""),
                r.get("PROD Scheduled Deployment", ""),
                r.get("PROD Deploy Date", ""),
                r.get("PROD Status", ""),
            ])


@trace_task("iteration_report_generation", metadata={"source": "pm_agent"})
def main():
    org_url = config.ado_org_url
    project = config.ado_project
    team = config.ado_team
    iteration = config.query_iteration_path
    areas = config.query_areas if config.query_areas else []
    pat = get_pat()
    wi_types = config.query_wi_types if config.query_wi_types else ["User Story", "Bug"]

    # PAT and org URL always required. ITERATION_PATH is only required when WIQL isn't provided.
    if not org_url or not pat:
        print("Required config: ado.org_url, ado.pat")
        print("Optional: ado.project, query.areas, query.wi_types, query.wiql_text, query.wiql_file")
        sys.exit(2)

    wiql_text = config.query_wiql_text
    wiql_file = config.query_wiql_file
    try:
        out_file, filtered_file, rows, filtered_rows, html_file = generate_report(
            org_url=org_url,
            pat=pat,
            project=project,
            team=team,
            iteration=iteration,
            areas=areas,
            wi_types=wi_types,
            wiql_text=wiql_text,
            wiql_file=wiql_file,
            outputs_dir="outputs",
            areas_filter=areas,
            types_filter=wi_types,
        )
    except Exception as e:
        print(f"Error generating report: {e}")
        sys.exit(2)

    print(f"Found {len(rows)} work items.")
    if filtered_file:
        print(f"Filtered report written: {filtered_file} ({len(filtered_rows)} rows)")
    print(f"Report written: {out_file}")


if __name__ == "__main__":
    main()
