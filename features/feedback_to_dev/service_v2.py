"""
Feedback to Dev Service - Optimized Version

Fast synchronous ADO queries using requests (same pattern as bug_areas_highlight).
Proper RCA extraction and email composition.
"""

import os
import json
import re
import html
import tempfile
import logging
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional, Tuple, Set
from difflib import SequenceMatcher
import yaml
from config import config

from utilities.mcp.pat import get_pat
from utilities.emailer import send_report_attachment

logger = logging.getLogger("pm_agent.features.feedback_to_dev")
logger.setLevel(logging.INFO)
if not logger.handlers:
    os.makedirs("logs", exist_ok=True)
    fh = logging.FileHandler("logs/feedback_to_dev.log")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    logger.addHandler(ch)

PROCESSED_FILE = os.path.join("outputs", "processed_bugs_feedback.json")
os.makedirs("outputs", exist_ok=True)

API_VERSION = "7.0"

STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "have", "has", "had", "do", "does", "did", "will", "would", "could",
    "should", "may", "might", "must", "shall", "can", "need", "to", "of",
    "in", "for", "on", "with", "at", "by", "from", "as", "into", "through",
    "during", "before", "after", "above", "below", "between", "under",
    "again", "further", "then", "once", "here", "there", "when", "where",
    "why", "how", "all", "each", "few", "more", "most", "other", "some",
    "such", "no", "nor", "not", "only", "own", "same", "so", "than", "too",
    "very", "just", "and", "but", "if", "or", "because", "until", "while",
}


def _create_session(retries: int = 3, backoff: float = 0.5) -> requests.Session:
    """Create requests session with retry logic."""
    session = requests.Session()
    retry = Retry(
        total=retries, read=retries, connect=retries,
        backoff_factor=backoff,
        status_forcelist=(429, 500, 502, 503, 504),
        raise_on_status=False
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


SESSION = _create_session()


def run_wiql(org_url: str, wiql: str, pat: str, project: Optional[str] = None) -> List[int]:
    """Execute WIQL and return work item IDs."""
    if project:
        url = f"{org_url}/{project}/_apis/wit/wiql?api-version={API_VERSION}"
    else:
        url = f"{org_url}/_apis/wit/wiql?api-version={API_VERSION}"
    
    resp = SESSION.post(url, json={"query": wiql}, auth=("", pat), timeout=30)
    if resp.status_code >= 400:
        logger.error("WIQL failed: %s %s", resp.status_code, resp.text[:200])
        raise RuntimeError(f"WIQL failed: {resp.status_code}")
    data = resp.json()
    return [item["id"] for item in data.get("workItems", [])]


def fetch_workitems(org_url: str, ids: List[int], pat: str) -> List[Dict[str, Any]]:
    """Fetch work items in batches of 200."""
    if not ids:
        return []
    results = []
    for i in range(0, len(ids), 200):
        chunk = ids[i:i+200]
        ids_str = ",".join(map(str, chunk))
        url = f"{org_url}/_apis/wit/workitems?ids={ids_str}&$expand=all&api-version={API_VERSION}"
        resp = SESSION.get(url, auth=("", pat), timeout=30)
        if resp.status_code >= 400:
            logger.warning("Fetch chunk failed: %s", resp.status_code)
            continue
        data = resp.json()
        results.extend(data.get("value", []))
    return results


def fetch_comments(org_url: str, wi_id: int, pat: str) -> str:
    """Fetch comments for a single work item."""
    url = f"{org_url}/_apis/wit/workItems/{wi_id}/comments?api-version={API_VERSION}"
    try:
        resp = SESSION.get(url, auth=("", pat), timeout=15)
        if resp.status_code >= 400:
            return ""
        data = resp.json()
        texts = []
        for c in data.get("comments", []):
            txt = c.get("text") or c.get("content") or ""
            if txt:
                texts.append(str(txt))
        return "\n\n".join(texts)
    except Exception:
        return ""


class FeedbackToDevService:
    """Fast feedback-to-dev service using synchronous requests."""
    
    def __init__(self):
        self.org_url = config.ado_org_url
        self.project = config.ado_project
        self._pat = None
    
    @property
    def pat(self):
        if self._pat is None:
            self._pat = get_pat()
        return self._pat
    
    def _load_config(self) -> Dict[str, Any]:
        cfg_path = os.path.join(os.getcwd(), "config.yaml")
        if os.path.exists(cfg_path):
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    return yaml.safe_load(f) or {}
            except Exception:
                pass
        return {}
    
    def _read_processed(self) -> List[int]:
        if not os.path.exists(PROCESSED_FILE):
            return []
        try:
            with open(PROCESSED_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return [int(x) for x in data] if isinstance(data, list) else []
        except Exception:
            return []
    
    def _write_processed(self, ids: List[int]):
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir="outputs", delete=False, suffix=".tmp") as tf:
                json.dump(ids, tf)
                tmp = tf.name
            os.replace(tmp, PROCESSED_FILE)
        except Exception:
            logger.exception("Failed to write processed file")
    
    @staticmethod
    def normalize_text(text: str) -> str:
        if not text:
            return ""
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text
    
    @staticmethod
    def extract_tokens(text: str) -> Set[str]:
        if not text:
            return set()
        tokens = re.findall(r"\b[a-zA-Z0-9_]+\b", text.lower())
        return {t for t in tokens if t not in STOPWORDS and len(t) > 2}
    
    def title_similarity(self, a: str, b: str) -> float:
        return SequenceMatcher(None, self.normalize_text(a), self.normalize_text(b)).ratio()
    
    def get_developer_email(self, fields: Dict[str, Any]) -> Tuple[str, str]:
        """Return (display_name, email)."""
        assigned = fields.get("System.AssignedTo") or {}
        
        if isinstance(assigned, str):
            m = re.search(r"([\w\.-]+@[\w\.-]+)", assigned)
            return (assigned, m.group(1) if m else "")
        
        if isinstance(assigned, dict):
            name = assigned.get("displayName", "")
            for key in ("uniqueName", "mail", "mailAddress", "email"):
                val = assigned.get(key, "")
                if "@" in val:
                    return (name, val)
            return (name, "")
        
        return ("Unassigned", "")
    
    def find_rca_content(self, fields: Dict[str, Any]) -> Optional[str]:
        """Extract RCA from fields."""
        rca_keywords = ["rca", "analysis", "root", "resolution", "cause", "fix"]
        
        # Check explicit RCA fields
        for key, value in fields.items():
            if not key or not value:
                continue
            key_lower = key.lower()
            for kw in rca_keywords:
                if kw in key_lower:
                    content = str(value).strip()
                    if len(content) > 20:
                        return content
        
        # Try patterns in history/comments
        history = fields.get("System.History") or ""
        comments = fields.get("System.Comments") or ""
        combined = f"{history}\n{comments}"
        
        patterns = [
            r"(?i)root\s*cause[:\s]+(.{30,500})",
            r"(?i)rca[:\s]+(.{30,500})",
            r"(?i)fix[:\s]+(.{30,500})",
            r"(?i)resolution[:\s]+(.{30,500})",
        ]
        for p in patterns:
            m = re.search(p, combined)
            if m:
                return m.group(1).strip()
        
        # Fallback to description
        desc = fields.get("System.Description") or ""
        if desc:
            clean = self.normalize_text(desc)
            if len(clean) > 30:
                return clean[:300]
        
        return None
    
    def find_similar_bugs(
        self,
        new_bug: Dict[str, Any],
        historical: List[Dict[str, Any]],
        threshold: float = 0.75
    ) -> List[Tuple[Dict[str, Any], float]]:
        """Find similar bugs using title/description similarity."""
        new_fields = new_bug.get("fields", {})
        new_title = new_fields.get("System.Title", "")
        new_desc = self.normalize_text(new_fields.get("System.Description", ""))
        new_area = new_fields.get("System.AreaPath", "")
        new_id = new_bug.get("id")
        
        results = []
        for hist in historical:
            hist_id = hist.get("id")
            if hist_id == new_id:
                continue
            
            hist_fields = hist.get("fields", {})
            hist_title = hist_fields.get("System.Title", "")
            hist_desc = self.normalize_text(hist_fields.get("System.Description", ""))
            hist_area = hist_fields.get("System.AreaPath", "")
            
            # Title similarity
            title_sim = self.title_similarity(new_title, hist_title)
            
            # Description similarity
            desc_sim = 0.0
            if new_desc and hist_desc:
                desc_sim = SequenceMatcher(None, new_desc[:500], hist_desc[:500]).ratio()
            
            # Area boost
            area_boost = 0.1 if new_area and hist_area and new_area == hist_area else 0.0
            
            # Combined score
            score = max(title_sim, desc_sim * 0.8) + area_boost
            
            if score >= threshold:
                results.append((hist, score))
        
        results.sort(key=lambda x: x[1], reverse=True)
        return results[:10]
    
    def compose_email(self, data: Dict[str, Any]) -> str:
        """Compose HTML email body."""
        org_url = self.org_url or ""
        new_id = data.get("new_bug_id")
        new_title = data.get("new_bug_title", "")
        dev_name = data.get("developer_name", "Unassigned")
        dev_email = data.get("developer_email", "")
        area = data.get("area_module", "Not specified")
        similar = data.get("similar_bugs", [])
        rca = data.get("probable_rca", "No RCA available.")
        
        new_link = f"{org_url}/_workitems/edit/{new_id}" if org_url else f"WI {new_id}"
        
        # Similar bugs HTML
        if similar:
            similar_html = "<ul>"
            for s in similar[:10]:
                sid = s.get("id")
                stitle = s.get("title", "")
                sscore = s.get("score", 0)
                slink = f"{org_url}/_workitems/edit/{sid}" if org_url else f"WI {sid}"
                similar_html += f"<li><a href='{slink}'>WI #{sid}</a> - {html.escape(stitle)} (score={sscore:.3f})</li>"
            similar_html += "</ul>"
        else:
            similar_html = "<p>None identified</p>"
        
        # Structured RCA section
        rca_html = f"""
        <h3>Structured RCA (Suggested)</h3>
        <table style='border-collapse:collapse;width:100%;border:1px solid #ddd'>
            <tr>
                <td style='padding:8px;border:1px solid #ddd;background:#f8f8f8;width:180px'><strong>Root Cause</strong></td>
                <td style='padding:8px;border:1px solid #ddd;color:#c00'>{html.escape(rca)}</td>
            </tr>
            <tr>
                <td style='padding:8px;border:1px solid #ddd;background:#f8f8f8'><strong>Gap in Previous RCA</strong></td>
                <td style='padding:8px;border:1px solid #ddd;color:#069'>No prior RCA content found in historical issues.</td>
            </tr>
            <tr>
                <td style='padding:8px;border:1px solid #ddd;background:#f8f8f8'><strong>Similar Historical Bugs</strong></td>
                <td style='padding:8px;border:1px solid #ddd'>{similar_html}</td>
            </tr>
            <tr>
                <td style='padding:8px;border:1px solid #ddd;background:#f8f8f8'><strong>Impact Analysis</strong></td>
                <td style='padding:8px;border:1px solid #ddd'>May affect user workflow and cause errors or degraded performance. If tags or title contain 'data' or 'loss', treat as data-impacting; if 'timeout' or 'slow' treat as performance.</td>
            </tr>
            <tr>
                <td style='padding:8px;border:1px solid #ddd;background:#f8f8f8'><strong>Fix Summary</strong></td>
                <td style='padding:8px;border:1px solid #ddd'>Apply input validation and guard conditions; add unit tests for the failing scenario; if reproducible, patch component to handle edge cases.</td>
            </tr>
            <tr>
                <td style='padding:8px;border:1px solid #ddd;background:#f8f8f8'><strong>Preventive Actions</strong></td>
                <td style='padding:8px;border:1px solid #ddd'>
                    <ul style='margin:0;padding-left:20px'>
                        <li>Add a unit/integration test covering the failing scenario</li>
                        <li>Improve monitoring/alerts for the error signature</li>
                        <li>Update RCA doc template to capture corrective action and owner</li>
                        <li>Run periodic reviews of recurring issues and add regression tests</li>
                    </ul>
                </td>
            </tr>
        </table>
        """
        
        return f"""
<html>
<body style='font-family:Arial,Helvetica,sans-serif;color:#111'>
    <h2>New Bug Feedback — Similar Past Issues & RCA Reference</h2>
    <p>A new bug has been reported. Below is a concise summary with related historical issues and suggested next steps.</p>
    
    <h3>New Bug</h3>
    <p><strong>ID:</strong> <a href="{new_link}">WI {new_id}</a></p>
    <p><strong>Title:</strong> {html.escape(new_title)}</p>
    <p><strong>Assigned Developer:</strong> {html.escape(dev_name)} <em>({html.escape(dev_email)})</em></p>
    <p><strong>Affected Area / Module:</strong> {html.escape(area)}</p>
    
    <h3>Similar Historical Bug(s)</h3>
    {similar_html}
    
    <h3>Probable Root Causes (Consolidated)</h3>
    <p>{html.escape(rca)}</p>
    
    <h3>Repeated Patterns</h3>
    <p>None identified</p>
    
    {rca_html}
    
    <h3>Suggested Next Steps</h3>
    <ol>
        <li>Review the linked historical issues and confirm whether they share a common stack/component.</li>
        <li>Assign an owner to validate the probable root causes and propose immediate mitigations.</li>
        <li>Update the canonical RCA document with any confirmed fixes and add monitoring/alerts to catch recurrence.</li>
    </ol>
    
    <hr/>
    <p style='font-size:0.9em;color:#666'>This message was generated automatically by the Project Management AI Agent.</p>
</body>
</html>
"""
    
    def run_workflow(
        self,
        lookback_minutes: int = 1440,
        historical_days: int = 180,
        similarity_threshold: float = 0.75,
        force_send: bool = False
    ) -> Dict[str, Any]:
        """Run the feedback workflow."""
        if not self.org_url or not self.project or not self.pat:
            logger.error("Missing ADO configuration")
            return {"error": "Missing ADO configuration"}
        
        config = self._load_config()
        recipients = config.get("reportEmailRecipients", [])
        if not recipients:
            logger.warning("No recipients configured")
        
        logger.info("Email recipients: %s", recipients)
        
        # Load processed IDs
        processed_ids = self._read_processed()
        processed_set = set(processed_ids)
        logger.info("Already processed %d bug IDs", len(processed_ids))
        
        # Query new bugs
        lookback_date = (datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)).strftime("%Y-%m-%d")
        wiql_new = f"""
            SELECT [System.Id] FROM WorkItems
            WHERE [System.TeamProject] = '{self.project}'
              AND [System.WorkItemType] = 'Bug'
              AND [System.CreatedDate] >= '{lookback_date}'
            ORDER BY [System.CreatedDate] DESC
        """
        
        logger.info("Querying new bugs since %s", lookback_date)
        new_ids = run_wiql(self.org_url, wiql_new, self.pat, self.project)
        logger.info("Found %d bugs in lookback window", len(new_ids))
        
        # Filter unprocessed
        unprocessed_ids = [bid for bid in new_ids if bid not in processed_set]
        logger.info("Unprocessed bugs: %d", len(unprocessed_ids))
        
        if not unprocessed_ids:
            logger.info("No new unprocessed bugs")
            return {"processed": 0, "emails_sent": 0}
        
        # Fetch new bugs
        new_bugs = fetch_workitems(self.org_url, unprocessed_ids, self.pat)
        logger.info("Fetched %d new bug work items", len(new_bugs))
        
        # Query historical bugs
        hist_date = (datetime.now(timezone.utc) - timedelta(days=historical_days)).strftime("%Y-%m-%d")
        wiql_hist = f"""
            SELECT [System.Id] FROM WorkItems
            WHERE [System.WorkItemType] = 'Bug'
              AND [System.CreatedDate] >= '{hist_date}'
            ORDER BY [System.CreatedDate] DESC
        """
        
        logger.info("Querying historical bugs since %s", hist_date)
        hist_ids = run_wiql(self.org_url, wiql_hist, self.pat)
        hist_ids = [hid for hid in hist_ids if hid not in unprocessed_ids]
        logger.info("Found %d historical bug IDs", len(hist_ids))
        
        # Fetch historical bugs
        hist_bugs = fetch_workitems(self.org_url, hist_ids, self.pat)
        logger.info("Fetched %d historical bug work items", len(hist_bugs))
        
        # Process each new bug
        emails_sent = 0
        processed_in_run = []
        
        for new_bug in new_bugs:
            bug_id = new_bug.get("id")
            fields = new_bug.get("fields", {})
            title = fields.get("System.Title", "")
            
            logger.info("Processing bug %s: %s", bug_id, title)
            
            # Find similar bugs
            similar = self.find_similar_bugs(new_bug, hist_bugs, similarity_threshold)
            
            if not similar and not force_send:
                logger.info("No similar bugs for %s, skipping", bug_id)
                continue
            
            # Get developer info
            dev_name, dev_email = self.get_developer_email(fields)
            
            # Fetch comments for RCA extraction from similar bugs
            rca_content = ""
            for hist, score in similar[:3]:
                hist_id = hist.get("id")
                hist_fields = hist.get("fields", {})
                
                # Fetch comments
                comments = fetch_comments(self.org_url, hist_id, self.pat)
                if comments:
                    hist_fields["System.Comments"] = comments
                
                rca = self.find_rca_content(hist_fields)
                if rca:
                    rca_content = rca
                    break
            
            if not rca_content:
                # Synthesize from keywords
                tokens = self.extract_tokens(f"{title} {fields.get('System.Description', '')}")
                area = fields.get("System.AreaPath", "")
                if tokens:
                    rca_content = f"Likely regressions or missing validation in {area}; observed keywords: {', '.join(list(tokens)[:8])}."
                else:
                    rca_content = "Unable to determine root cause; manual investigation required."
            
            # Build email data
            email_data = {
                "new_bug_id": bug_id,
                "new_bug_title": title,
                "developer_name": dev_name,
                "developer_email": dev_email,
                "area_module": fields.get("System.AreaPath", "Not specified"),
                "similar_bugs": [
                    {"id": h.get("id"), "title": h.get("fields", {}).get("System.Title", ""), "score": s}
                    for h, s in similar
                ],
                "probable_rca": rca_content,
            }
            
            # Compose and send email
            if recipients:
                body = self.compose_email(email_data)
                subject = "New Bug Feedback – Similar Past Issues & RCA Reference"
                
                try:
                    ok, msg = send_report_attachment(recipients, subject, body, attachments=None)
                    if ok:
                        emails_sent += 1
                        processed_in_run.append(bug_id)
                        logger.info("Email sent for bug %s to %s", bug_id, recipients)
                    else:
                        logger.error("Failed to send email for bug %s: %s", bug_id, msg)
                except Exception as e:
                    logger.exception("Error sending email: %s", e)
        
        # Update processed IDs (preserve existing)
        new_processed = list(set(processed_ids + processed_in_run))
        self._write_processed(new_processed)
        logger.info("Updated processed file: %d total IDs", len(new_processed))
        
        return {
            "processed": len(processed_in_run),
            "emails_sent": emails_sent,
            "new_bugs_found": len(new_bugs),
        }
