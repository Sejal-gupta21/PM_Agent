"""
Bug Area Highlight Service

Core business logic for detecting recurring bugs by area path.
This module extracts detection and report logic from utilities.bug_areas_highlight.
"""

import os
import logging
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
from difflib import SequenceMatcher
import re
from collections import defaultdict
from html import unescape
import html
from config import config

from utilities.mcp.pat import get_pat
from utilities.emailer import send_report_attachment

logger = logging.getLogger("pm_agent.features.bug_area_highlight")
logger.setLevel(logging.INFO)
if not logger.handlers:
    os.makedirs("logs", exist_ok=True)
    fh = logging.FileHandler("logs/bug_areas_highlight.log")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(fh)

API_VERSION = "7.0"


def _create_requests_session(retries: int = 3, backoff_factor: float = 0.5) -> requests.Session:
    """Create a requests Session with retry/backoff behavior."""
    session = requests.Session()
    retry = Retry(
        total=retries, read=retries, connect=retries,
        backoff_factor=backoff_factor,
        status_forcelist=(429, 500, 502, 503, 504),
        raise_on_status=False
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


SESSION = _create_requests_session()


class BugAreaHighlightService:
    """Service for detecting and reporting recurring bugs by area."""

    def __init__(self, org_url: str = None, project: str = None, pat: str = None):
        self.org_url = org_url or config.ado_org_url
        self.project = project or config.ado_project
        self.pat = pat or get_pat()

    def run_wiql(self, wiql: str, project: Optional[str] = None, team: Optional[str] = None) -> List[int]:
        """Execute WIQL query and return work item IDs."""
        proj = project or self.project
        if proj and team:
            url = f"{self.org_url}/{proj}/{team}/_apis/wit/wiql?api-version={API_VERSION}"
        elif proj:
            url = f"{self.org_url}/{proj}/_apis/wit/wiql?api-version={API_VERSION}"
        else:
            url = f"{self.org_url}/_apis/wit/wiql?api-version={API_VERSION}"
        
        resp = SESSION.post(url, json={"query": wiql}, auth=("", self.pat), timeout=30)
        if resp.status_code >= 400:
            logger.error("WIQL query failed: %s %s", resp.status_code, resp.text)
            raise RuntimeError(f"WIQL request failed: {resp.status_code} {resp.text}")
        data = resp.json()
        return [item["id"] for item in data.get("workItems", [])]

    def fetch_workitems(self, ids: List[int]) -> List[Dict[str, Any]]:
        """Fetch work item details by IDs."""
        if not ids:
            return []
        batch = 200
        results: List[Dict[str, Any]] = []
        for i in range(0, len(ids), batch):
            chunk = ids[i: i + batch]
            ids_chunk = ",".join(map(str, chunk))
            url = f"{self.org_url}/_apis/wit/workitems?ids={ids_chunk}&$expand=all&api-version={API_VERSION}"
            resp = SESSION.get(url, auth=("", self.pat), timeout=30)
            try:
                resp.raise_for_status()
            except Exception as e:
                logger.exception("Failed fetching workitems chunk: %s", e)
                continue
            data = resp.json()
            results.extend(data.get("value", []))
        return results

    @staticmethod
    def normalize_title(t: str) -> str:
        """Normalize title for comparison."""
        if not t:
            return ""
        s = t.lower()
        s = re.sub(r"job#\s*\d+", "", s)
        s = re.sub(r"\(job#\s*\d+\)", "", s)
        s = re.sub(r"\d+", "", s)
        s = re.sub(r"[^a-z\s]", " ", s)
        s = re.sub(r"\s+", " ", s).strip()
        return s

    @staticmethod
    def normalize_description(d: str) -> str:
        """Normalize description for comparison."""
        if not d:
            return ""
        txt = re.sub(r"<[^>]+>", " ", d)
        txt = unescape(txt)
        return BugAreaHighlightService.normalize_title(txt)

    @staticmethod
    def title_similarity(a: str, b: str) -> float:
        """Calculate title similarity."""
        return SequenceMatcher(
            None, 
            BugAreaHighlightService.normalize_title(a), 
            BugAreaHighlightService.normalize_title(b)
        ).ratio()

    @staticmethod
    def text_similarity(a: str, b: str) -> float:
        """Calculate text similarity."""
        return SequenceMatcher(None, (a or ""), (b or "")).ratio()

    @staticmethod
    def get_bug_ref(wi: Dict[str, Any]) -> str:
        """Get bug reference from work item."""
        fields = wi.get("fields", {}) if isinstance(wi, dict) else {}
        candidates = ["Custom.BugNumber", "Custom.BugRef", "Custom.Ref", "Ref", "System.BugNumber"]
        for k in candidates:
            v = fields.get(k)
            if v:
                return str(v)
        wi_id = wi.get("id") or fields.get("System.Id")
        return str(wi_id) if wi_id else ""

    def detect_recurring(
        self,
        bugs: List[Dict[str, Any]],
        similarity_threshold: float = 0.75,
        recurrence_threshold: int = 3,
        area_grouping_depth: int = 3,
        use_tfidf: bool = False,
        app_context: str = "",
        no_area_label: str = "No Area",
    ) -> Dict[str, Any]:
        """Detect recurring bug patterns grouped by area."""
        by_area: Dict[str, List[Dict[str, Any]]] = {}
        
        for wi in bugs:
            fields = wi.get("fields", {})
            area = fields.get("System.AreaPath") or ""
            if not area or (isinstance(area, str) and not area.strip()):
                area = no_area_label
            
            # Apply area depth truncation
            parts = [p for p in area.split("\\") if p]
            area_key = "\\".join(parts[:area_grouping_depth]) if parts else no_area_label
            
            title = (fields.get("System.Title") or "").strip()
            wi_id = wi.get("id")
            bug_ref = self.get_bug_ref(wi)
            url = wi.get("_links", {}).get("html", {}).get("href") or ""
            created = fields.get("System.CreatedDate")
            tags = fields.get("System.Tags") or ""
            component = fields.get("Custom.Component") or fields.get("System.AreaPath") or ""
            repro = fields.get("System.ReproSteps") or fields.get("Microsoft.VSTS.TCM.ReproSteps") or ""
            assigned = fields.get("System.AssignedTo")
            if isinstance(assigned, dict):
                assigned_to = assigned.get("displayName") or assigned.get("uniqueName") or str(assigned)
            else:
                assigned_to = str(assigned) if assigned else ""
            priority = fields.get("Microsoft.VSTS.Common.Priority") or fields.get("System.Priority") or ""
            description = fields.get("System.Description") or fields.get("System.History") or ""
            
            by_area.setdefault(area_key, []).append({
                "id": wi_id,
                "bug_ref": bug_ref,
                "title": title,
                "url": url,
                "created": created,
                "raw_area": area,
                "tags": tags,
                "component": component,
                "assigned_to": assigned_to,
                "priority": priority,
                "description": description,
                "repro": repro,
            })

        recurring: Dict[str, Any] = {}
        
        for area, items in by_area.items():
            n = len(items)
            clusters: List[List[Dict[str, Any]]] = []
            
            if n < recurrence_threshold:
                # Check for exact title repeats
                title_counts: Dict[str, List[Dict[str, Any]]] = {}
                for it in items:
                    key = self.normalize_title(it["title"]) or it["title"]
                    title_counts.setdefault(key, []).append(it)
                exact_repeats = {t: l for t, l in title_counts.items() if len(l) >= 2}
                if exact_repeats:
                    for t, l in exact_repeats.items():
                        clusters.append({"reason": "exact", "title": t, "members": l})
                    recurring[area] = {
                        "items": items, 
                        "count": sum(len(l) for l in exact_repeats.values()), 
                        "pattern": "Exact title repeats", 
                        "clusters": clusters
                    }
                continue
            
            # Greedy pairwise clustering
            used = [False] * n
            for i in range(n):
                if used[i]:
                    continue
                base = items[i]
                cluster = [base]
                used[i] = True
                for j in range(i + 1, n):
                    if used[j]:
                        continue
                    title_sim = self.title_similarity(base["title"], items[j]["title"])
                    matched = False
                    if title_sim >= similarity_threshold:
                        matched = True
                    else:
                        desc_a = self.normalize_description(base.get("description") or "")
                        desc_b = self.normalize_description(items[j].get("description") or "")
                        if desc_a and desc_b:
                            desc_sim = self.text_similarity(desc_a, desc_b)
                            if desc_sim >= similarity_threshold:
                                matched = True
                    if matched:
                        cluster.append(items[j])
                        used[j] = True
                if len(cluster) > 1:
                    clusters.append(cluster)
            
            total_clustered = sum(len(c) if isinstance(c, list) else len(c.get("members", [])) for c in clusters)
            if total_clustered >= recurrence_threshold:
                recurring[area] = {"items": items, "count": total_clustered, "clusters": clusters}
            else:
                # Also include exact repeats
                title_counts: Dict[str, List[Dict[str, Any]]] = {}
                for it in items:
                    key = self.normalize_title(it["title"]) or it["title"]
                    title_counts.setdefault(key, []).append(it)
                exact_repeats = {t: l for t, l in title_counts.items() if len(l) >= 2}
                if exact_repeats:
                    for t, l in exact_repeats.items():
                        clusters.append({"reason": "exact", "title": t, "members": l})
                    recurring[area] = {
                        "items": items, 
                        "count": sum(len(l) for l in exact_repeats.values()), 
                        "pattern": "Exact title repeats", 
                        "clusters": clusters
                    }
        
        return recurring

    def build_html_summary(self, recurring: Dict[str, Any]) -> str:
        """Build HTML summary report with detailed area breakdown."""
        from datetime import datetime as _dt
        ts = _dt.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        
        table_style = "border-collapse:collapse;width:100%;border:1px solid #ddd;"
        th_style = "text-align:left;padding:8px;border:1px solid #ddd;background:#f8f8f8;"
        td_style = "padding:8px;border:1px solid #ddd;vertical-align:top;"
        
        parts = [
            "<html><head><meta charset='utf-8'><title>Bug Areas Highlight</title></head>",
            "<body style='font-family:Arial,Helvetica,sans-serif;color:#111;margin:20px'>",
            "<div style='max-width:1100px'>",
            "<h1 style='margin:0;padding:0'>Bug Areas Highlight</h1>",
            "<span style='color:#666;font-size:11px'>Automated report — for PM / TL / Stakeholders</span>",
            f"<div style='color:#666;font-size:13px;margin-top:4px'>Generated: {html.escape(ts)}</div>",
            "<hr style='margin:12px 0'>",
        ]
        
        if not recurring:
            parts.append("<p>No recurring bugs were detected in the configured lookback window.</p>")
            parts.append("</div></body></html>")
            return "\n".join(parts)
        
        # Executive summary
        total_areas = len(recurring)
        total_recurring = sum(info.get("count", 0) for info in recurring.values())
        parts.append("<h2>Executive summary</h2>")
        parts.append(f"<p>This report identifies <strong>{total_areas}</strong> application area(s) with recurring bug reports, "
                     f"containing <strong>{total_recurring}</strong> repeated occurrence(s) in the configured lookback window. "
                     "The table below highlights affected areas and representative work items for triage.</p>")
        
        # Main summary table
        parts.append("<h3>Affected areas</h3>")
        parts.append(f"<table style='{table_style}'>")
        parts.append("<thead><tr>")
        parts.append(f"<th style='{th_style}'>Area</th>")
        parts.append(f"<th style='{th_style}'>Occurrences</th>")
        parts.append(f"<th style='{th_style}'>Representative Bugs (top)</th>")
        parts.append(f"<th style='{th_style}'>Priority</th>")
        parts.append(f"<th style='{th_style}'>Assigned To</th>")
        parts.append(f"<th style='{th_style}'>First seen</th>")
        parts.append(f"<th style='{th_style}'>Last seen</th>")
        parts.append("</tr></thead><tbody>")
        
        for area, info in recurring.items():
            count = info.get("count", 0)
            items = info.get("items", [])
            
            # Get representative bugs (top 3 with details)
            top_bugs = []
            for it in items[:3]:
                link = it.get("url") or f"{self.org_url}/_workitems/edit/{it['id']}"
                bug_ref = it.get("bug_ref") or str(it.get("id", ""))
                title = it.get("title") or ""
                title_short = (title[:50] + "...") if len(title) > 50 else title
                top_bugs.append(f"<a href='{html.escape(link)}'>Bug {html.escape(bug_ref)}</a> — {html.escape(title_short)}")
            
            # Aggregate priority (use highest priority = lowest number)
            priorities = [it.get("priority") for it in items if it.get("priority")]
            priority_val = min([int(p) for p in priorities if str(p).isdigit()] or [0]) or ""
            
            # Get assigned to from most recent bug
            assigned = ""
            for it in items:
                if it.get("assigned_to"):
                    assigned = it.get("assigned_to")
                    break
            
            # First/last seen dates
            dates = []
            for it in items:
                created = it.get("created")
                if created:
                    dates.append(created)
            dates_sorted = sorted(dates) if dates else []
            first_seen = dates_sorted[0] if dates_sorted else ""
            last_seen = dates_sorted[-1] if dates_sorted else ""
            
            parts.append("<tr>")
            parts.append(f"<td style='{td_style}'>{html.escape(area)}</td>")
            parts.append(f"<td style='{td_style}'>{count}</td>")
            parts.append(f"<td style='{td_style}'>{', '.join(top_bugs)}</td>")
            parts.append(f"<td style='{td_style}'>{priority_val}</td>")
            parts.append(f"<td style='{td_style}'>{html.escape(str(assigned))}</td>")
            parts.append(f"<td style='{td_style}'>{html.escape(str(first_seen))}</td>")
            parts.append(f"<td style='{td_style}'>{html.escape(str(last_seen))}</td>")
            parts.append("</tr>")
        
        parts.append("</tbody></table>")
        
        # Detailed patterns per area
        for area, info in recurring.items():
            count = info.get("count", 0)
            clusters = info.get("clusters", [])
            items = info.get("items", [])
            
            parts.append(f"<h2 style='margin-top:30px'>Area: {html.escape(area)} — {count} repeated occurrence(s)</h2>")
            
            if clusters:
                pattern_num = 0
                for cluster in clusters:
                    pattern_num += 1
                    # Handle both list and dict clusters
                    if isinstance(cluster, dict):
                        members = cluster.get("members", [])
                    else:
                        members = cluster if isinstance(cluster, list) else []
                    
                    if not members:
                        continue
                    
                    parts.append(f"<h4>Pattern {pattern_num} — {len(members)} similar items</h4>")
                    parts.append(f"<table style='{table_style}'>")
                    parts.append(f"<thead><tr><th style='{th_style}'>Bug</th><th style='{th_style}'>Title</th><th style='{th_style}'>Created</th></tr></thead><tbody>")
                    
                    for member in members:
                        mid = member.get("id") or member.get("bug_ref") or ""
                        mtitle = member.get("title") or ""
                        mcreated = member.get("created") or ""
                        mlink = member.get("url") or f"{self.org_url}/_workitems/edit/{mid}"
                        parts.append("<tr>")
                        parts.append(f"<td style='{td_style}'><a href='{html.escape(mlink)}'>Bug {html.escape(str(mid))}</a></td>")
                        parts.append(f"<td style='{td_style}'>{html.escape(mtitle)}</td>")
                        parts.append(f"<td style='{td_style}'>{html.escape(str(mcreated))}</td>")
                        parts.append("</tr>")
                    
                    parts.append("</tbody></table>")
            else:
                # No clusters, list all items
                parts.append(f"<table style='{table_style}'>")
                parts.append(f"<thead><tr><th style='{th_style}'>Bug</th><th style='{th_style}'>Title</th><th style='{th_style}'>Created</th></tr></thead><tbody>")
                for it in items:
                    mid = it.get("id") or it.get("bug_ref") or ""
                    mtitle = it.get("title") or ""
                    mcreated = it.get("created") or ""
                    mlink = it.get("url") or f"{self.org_url}/_workitems/edit/{mid}"
                    parts.append("<tr>")
                    parts.append(f"<td style='{td_style}'><a href='{html.escape(mlink)}'>Bug {html.escape(str(mid))}</a></td>")
                    parts.append(f"<td style='{td_style}'>{html.escape(mtitle)}</td>")
                    parts.append(f"<td style='{td_style}'>{html.escape(str(mcreated))}</td>")
                    parts.append("</tr>")
                parts.append("</tbody></table>")
            
            # Root cause pattern insights
            parts.append("<h4>Root cause pattern insights</h4>")
            parts.append("<ul><li>Multiple similar titles suggest a recurring functional/regression issue in the area; "
                         "investigate recent changes and shared dependencies.</li></ul>")
        
        # Recommended actions
        parts.append("<h3 style='margin-top:30px'>Recommended actions</h3>")
        parts.append("<ol>")
        parts.append("<li>Assign the area to the component owner for triage and grouping of root cause.</li>")
        parts.append("<li>Prioritise fixes by occurrence frequency and business impact; escalate critical issues.</li>")
        parts.append("<li>Expand automated regression tests focused on the affected module(s).</li>")
        parts.append("<li>Review recent deployments and roll back if a clear regression is identified.</li>")
        parts.append("<li>Schedule a focused post-mortem for repeated issues and track corrective actions.</li>")
        parts.append("</ol>")
        
        parts.append("<hr><p style='color:#666;font-size:12px'>Automated report by PM-Agent</p>")
        parts.append("</div></body></html>")
        
        return "\n".join(parts)

    def run_analysis(
        self,
        lookback_days: int = 30,
        recurrence_threshold: int = 3,
        similarity_threshold: float = 0.75,
        no_area_label: str = "No Area",
    ) -> Tuple[Dict[str, Any], str, List[Dict[str, Any]]]:
        """
        Run the full analysis pipeline.
        
        Returns:
            (recurring_dict, html_summary, all_bugs)
        """
        if not self.org_url or not self.project or not self.pat:
            raise RuntimeError("ADO configuration missing (ADO_ORG_URL, ADO_PROJECT, PAT)")
        
        wiql = f"""
            SELECT [System.Id] FROM WorkItems
            WHERE [System.TeamProject] = '{self.project}'
              AND [System.WorkItemType] = 'Bug'
              AND [System.CreatedDate] >= @Today - {lookback_days}
            ORDER BY [System.CreatedDate] DESC
        """
        
        ids = self.run_wiql(wiql, project=self.project)
        logger.info("WIQL returned %d bug IDs (lookback_days=%d)", len(ids), lookback_days)
        
        if not ids:
            return {}, self.build_html_summary({}), []
        
        bugs = self.fetch_workitems(ids)
        recurring = self.detect_recurring(
            bugs,
            similarity_threshold=similarity_threshold,
            recurrence_threshold=recurrence_threshold,
            no_area_label=no_area_label,
        )
        
        html_summary = self.build_html_summary(recurring)
        return recurring, html_summary, bugs

    def send_report(
        self,
        recurring: Dict[str, Any],
        html_body: str,
        recipients: List[str],
    ) -> Tuple[bool, str]:
        """Send the report via email."""
        if not recipients:
            logger.error("No recipients configured for bug areas report")
            return False, "No recipients configured"
        
        areas_count = len(recurring) if isinstance(recurring, dict) else 0
        today = datetime.utcnow().date().isoformat()
        subject = f"Bug Areas Highlight — {areas_count} area(s) — {today}"
        
        # Save preview
        try:
            preview_path = os.path.join("logs", "bug_areas_preview.html")
            with open(preview_path, "w", encoding="utf-8") as pf:
                pf.write(html_body)
            logger.info("Wrote email preview to %s", preview_path)
        except Exception:
            logger.exception("Failed to write email preview file")
        
        ok, resp = send_report_attachment(recipients, subject, html_body, attachments=None)
        if ok:
            logger.info("Bug Areas Highlight email sent to %s", recipients)
        else:
            logger.error("Error sending Bug Areas Highlight: %s", resp)
        
        return ok, resp
