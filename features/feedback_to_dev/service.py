"""
Feedback to Dev Service

Core business logic for detecting new bugs, finding similar historical bugs,
extracting RCA content, and generating feedback notifications.
"""

import os
import asyncio
import logging
import json
import re
import html
import tempfile
from datetime import datetime, timezone, timedelta
from typing import Dict, Any, List, Optional, Tuple, Set
import yaml
from config import config

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

# Paths
PROCESSED_FILE = os.path.join("outputs", "processed_bugs_feedback.json")
os.makedirs("outputs", exist_ok=True)

# Stopwords for keyword overlap
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
    "although", "since", "unless", "that", "this", "these", "those", "it",
    "its", "i", "me", "my", "myself", "we", "our", "ours", "ourselves",
    "you", "your", "yours", "yourself", "yourselves", "he", "him", "his",
    "himself", "she", "her", "hers", "herself", "they", "them", "their",
    "theirs", "themselves", "what", "which", "who", "whom"
}


class FeedbackToDevService:
    """Service for detecting new bugs and sending RCA feedback to developers."""

    def __init__(self):
        self.org_url = config.ado_org_url
        self.project = config.ado_project
        self._pat = None

    @property
    def pat(self):
        if self._pat is None:
            from utilities.mcp.pat import get_pat
            self._pat = get_pat()
        return self._pat

    def _load_config(self) -> Dict[str, Any]:
        """Load top-level config.yaml."""
        cfg_path = os.path.join(os.getcwd(), "config.yaml")
        if not os.path.exists(cfg_path):
            return {}
        try:
            with open(cfg_path, "r", encoding="utf-8") as fh:
                return yaml.safe_load(fh) or {}
        except Exception:
            logger.exception("Failed to load config.yaml")
            return {}

    def _read_processed(self) -> List[int]:
        """Read list of already-processed bug IDs."""
        if not os.path.exists(PROCESSED_FILE):
            return []
        try:
            with open(PROCESSED_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
                return [int(x) for x in data] if isinstance(data, list) else []
        except Exception:
            logger.exception("Failed to read processed bugs file")
            return []

    def _write_processed(self, ids: List[int]):
        """Atomically write list of processed bug IDs."""
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                dir=os.path.dirname(PROCESSED_FILE) or ".",
                delete=False,
                suffix=".tmp"
            ) as tf:
                json.dump(ids, tf)
                tmp_path = tf.name
            os.replace(tmp_path, PROCESSED_FILE)
        except Exception:
            logger.exception("Failed to write processed bugs file")

    @staticmethod
    def normalize_text(text: str) -> str:
        """Normalize text by removing HTML tags and extra whitespace."""
        if not text:
            return ""
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def extract_tokens(text: str) -> Set[str]:
        """Extract non-stopword tokens from text."""
        if not text:
            return set()
        tokens = re.findall(r"\b[a-zA-Z0-9_]+\b", text.lower())
        return {t for t in tokens if t not in STOPWORDS and len(t) > 2}

    @staticmethod
    def get_field(fields: Dict[str, Any], *keys: str, default: str = "") -> str:
        """Get field value from fields dict, trying multiple keys."""
        for key in keys:
            val = fields.get(key)
            if val:
                return str(val)
        return default

    def get_developer_email(self, fields: Dict[str, Any], config: Dict[str, Any]) -> str:
        """Determine assigned developer email from bug fields or config mapping."""
        assigned_to = fields.get("System.AssignedTo") or {}
        
        if isinstance(assigned_to, str):
            if "@" in assigned_to:
                return assigned_to
            display_name = assigned_to
        elif isinstance(assigned_to, dict):
            unique_name = assigned_to.get("uniqueName") or ""
            if "@" in unique_name:
                return unique_name
            display_name = assigned_to.get("displayName") or ""
        else:
            display_name = ""
        
        # Try to find mapping in config
        if display_name:
            for key in ["users", "user_emails", "people", "userMapping"]:
                mapping = config.get(key, {})
                if isinstance(mapping, dict) and display_name in mapping:
                    return mapping[display_name]
        
        # Try to extract email from display strings like 'Name <email@host>' or similar
        try:
            import re as _re
            # Look in the assigned_to string fields
            if isinstance(assigned_to, str):
                m = _re.search(r"([\w\.-]+@[\w\.-]+)", assigned_to)
                if m:
                    return m.group(1)
            elif isinstance(assigned_to, dict):
                for k in ("uniqueName", "mail", "mailAddress", "email"):
                    v = assigned_to.get(k)
                    if isinstance(v, str) and "@" in v:
                        return v
        except Exception:
            pass

        # Try CreatedBy/ChangedBy fields as fallback
        for key in ("System.CreatedBy", "System.ChangedBy"):
            val = fields.get(key)
            if isinstance(val, str):
                m = re.search(r"([\w\.-]+@[\w\.-]+)", val)
                if m:
                    return m.group(1)
            elif isinstance(val, dict):
                for k in ("uniqueName", "mail", "mailAddress", "email"):
                    v = val.get(k)
                    if isinstance(v, str) and "@" in v:
                        return v

        return ""

    def find_rca_content(self, fields: Dict[str, Any]) -> Optional[str]:
        """Extract RCA-like content from bug fields with smarter field detection."""
        rca_keywords = ["rca", "analysis", "root", "resolution", "cause", "fix", "reason", "solution"]
        
        # First try explicit RCA fields
        for key, value in fields.items():
            if not key or not value:
                continue
            key_lower = key.lower()
            for kw in rca_keywords:
                if kw in key_lower:
                    content = str(value).strip()
                    if len(content) > 20:  # Must have meaningful content
                        return content
        
        # Try to extract from history/comments for RCA-like entries
        history = fields.get("System.History") or ""
        comments = fields.get("System.Comments") or ""
        
        # Look for RCA patterns in history/comments
        combined = f"{history}\n{comments}"
        rca_patterns = [
            r"(?i)root\s*cause[:\s]+(.{30,500})",
            r"(?i)rca[:\s]+(.{30,500})",
            r"(?i)cause[:\s]+(.{30,500})",
            r"(?i)fix[:\s]+(.{30,500})",
            r"(?i)resolution[:\s]+(.{30,500})",
            r"(?i)issue was[:\s]+(.{30,300})",
            r"(?i)problem was[:\s]+(.{30,300})",
        ]
        
        for pattern in rca_patterns:
            match = re.search(pattern, combined)
            if match:
                return match.group(1).strip()
        
        # Fallback: use description if it contains relevant keywords
        description = fields.get("System.Description") or ""
        if description:
            desc_lower = description.lower()
            if any(kw in desc_lower for kw in ["error", "exception", "failed", "issue", "bug", "problem"]):
                # Normalize and return first 300 chars
                clean = self.normalize_text(description)
                if len(clean) > 30:
                    return clean[:300]
        
        return None

    def synthesize_rca_from_bugs(
        self, 
        new_entry: Dict[str, Any], 
        similar_entries: List[Tuple[Dict[str, Any], float, List[str], Optional[str]]]
    ) -> Dict[str, str]:
        """
        Synthesize structured RCA insights from new bug and similar historical bugs.
        
        Returns dict with: probable_cause, gap_analysis, impact, fix_summary, preventive_actions
        """
        result = {
            "probable_cause": "",
            "gap_in_rca": "",
            "similar_bugs_detail": "",
            "impact_analysis": "",
            "fix_summary": "",
            "preventive_actions": "",
        }
        
        new_title = new_entry.get("title", "")
        new_area = new_entry.get("area", "")
        new_tags = new_entry.get("tags", "")
        new_desc = new_entry.get("desc_norm", "")
        
        # Extract key tokens from new bug
        all_tokens = self.extract_tokens(f"{new_title} {new_desc} {new_tags}")
        
        # Collect RCA content from historical bugs
        rca_contents = []
        similar_details = []
        
        for hist_entry, score, boosts, verdict in similar_entries[:5]:
            hist_id = hist_entry.get("id")
            hist_title = hist_entry.get("title", "")
            rca = self.find_rca_content(hist_entry.get("fields", {}))
            
            similar_details.append(f"- WI #{hist_id}: {hist_title} (score={score:.3f})")
            
            if rca:
                rca_contents.append(f"Bug #{hist_id}: {rca[:200]}")
            
            # Extract tokens from historical bugs
            hist_tokens = self.extract_tokens(f"{hist_title} {hist_entry.get('desc_norm', '')}")
            all_tokens.update(hist_tokens)
        
        result["similar_bugs_detail"] = "\n".join(similar_details) if similar_details else ""
        
        # Synthesize probable cause
        if rca_contents:
            result["probable_cause"] = " | ".join(rca_contents[:3])
            result["gap_in_rca"] = "Review the linked historical RCAs for common root cause themes."
        else:
            # No explicit RCA found - synthesize from patterns
            key_terms = list(all_tokens)[:15]
            if key_terms:
                area_mention = f"in {new_area}" if new_area else ""
                result["probable_cause"] = (
                    f"Likely regressions or missing validation {area_mention}; "
                    f"observed keywords: {', '.join(key_terms[:8])}."
                )
            else:
                result["probable_cause"] = "Unable to determine probable cause; manual investigation needed."
            result["gap_in_rca"] = "No prior RCA content found in historical issues."
        
        # Impact analysis based on tags and title patterns
        impact_keywords = {
            "data": "data-impacting",
            "loss": "data-impacting", 
            "timeout": "performance",
            "slow": "performance",
            "error": "functional",
            "crash": "critical",
            "security": "security",
            "auth": "security",
        }
        
        detected_impacts = []
        combined_text = f"{new_title} {new_tags}".lower()
        for kw, impact in impact_keywords.items():
            if kw in combined_text and impact not in detected_impacts:
                detected_impacts.append(impact)
        
        if detected_impacts:
            result["impact_analysis"] = (
                f"May affect user workflow and cause errors or degraded performance. "
                f"If tags or title contain 'data' or 'loss', treat as data-impacting; "
                f"if 'timeout' or 'slow' treat as performance."
            )
        else:
            result["impact_analysis"] = "Impact unclear; requires manual assessment of severity."
        
        # Fix summary
        result["fix_summary"] = (
            "Apply input validation and guard conditions; add unit tests for the failing scenario; "
            "if reproducible, patch component to handle edge cases."
        )
        
        # Preventive actions
        result["preventive_actions"] = (
            "- Add a unit/integration test covering the failing scenario\n"
            "- Improve monitoring/alerts for the error signature\n"
            "- Update RCA doc template to capture corrective action and owner\n"
            "- Run periodic reviews of recurring issues and add regression tests"
        )
        
        return result

    def build_bug_text(self, entry: Dict[str, Any]) -> str:
        """Build combined text from bug entry for embedding/comparison."""
        from utilities.llm_embeddings import build_embedding_text
        return build_embedding_text({
            "title": entry.get("title", ""),
            "desc_norm": entry.get("desc_norm", ""),
            "repro_norm": entry.get("repro_norm", ""),
            "tags": entry.get("tags", ""),
            "area": entry.get("area", ""),
            "error": entry.get("error", ""),
            "module": entry.get("module", ""),
        })

    def compute_boosts(
        self, 
        new_entry: Dict[str, Any], 
        hist_entry: Dict[str, Any]
    ) -> Tuple[float, List[str]]:
        """Compute similarity boosts based on field matches."""
        boosts = []
        total = 0.0
        
        # Same AreaPath or Module: +0.10
        new_area = (new_entry.get("area") or "").lower().strip()
        hist_area = (hist_entry.get("area") or "").lower().strip()
        new_module = (new_entry.get("module") or "").lower().strip()
        hist_module = (hist_entry.get("module") or "").lower().strip()
        
        if new_area and hist_area and new_area == hist_area:
            total += 0.10
            boosts.append("same_area:+0.10")
        elif new_module and hist_module and new_module == hist_module:
            total += 0.10
            boosts.append("same_module:+0.10")
        
        # Tags overlap: +0.05
        new_tags = new_entry.get("tags") or ""
        hist_tags = hist_entry.get("tags") or ""
        if new_tags and hist_tags:
            new_tag_set = {t.strip().lower() for t in re.split(r"[;,]", new_tags) if t.strip()}
            hist_tag_set = {t.strip().lower() for t in re.split(r"[;,]", hist_tags) if t.strip()}
            if new_tag_set & hist_tag_set:
                total += 0.05
                boosts.append("tags_overlap:+0.05")
        
        # Keyword overlap: +0.05
        new_tokens = self.extract_tokens(
            new_entry.get("title", "") + " " + new_entry.get("desc_norm", "")
        )
        hist_tokens = self.extract_tokens(
            hist_entry.get("title", "") + " " + hist_entry.get("desc_norm", "")
        )
        if len(new_tokens & hist_tokens) >= 1:
            total += 0.05
            boosts.append("keyword_overlap:+0.05")
        
        # Error signature match: +0.10
        new_error = (new_entry.get("error") or "").strip()
        hist_error = (hist_entry.get("error") or "").strip()
        if new_error and hist_error and new_error == hist_error:
            total += 0.10
            boosts.append("error_match:+0.10")
        
        return total, boosts

    async def find_similar_bugs(
        self,
        new_bug_entry: Dict[str, Any],
        historical_entries: List[Dict[str, Any]],
        hist_cache: Dict[str, Dict[str, Any]],
        embedding_threshold: float = 0.82,
        borderline_threshold: float = 0.70,
        final_threshold: float = 0.85
    ) -> List[Tuple[Dict[str, Any], float, List[str], Optional[str]]]:
        """
        Find similar historical bugs using hybrid similarity.
        
        Returns list of (hist_entry, final_score, boost_list, llm_verdict).
        """
        from utilities.llm_embeddings import embed_texts_with_cache, _cosine
        
        results = []
        new_id = str(new_bug_entry.get("id"))
        new_text = self.build_bug_text(new_bug_entry)
        
        loop = asyncio.get_event_loop()
        try:
            new_cache = await loop.run_in_executor(
                None,
                embed_texts_with_cache,
                {new_id: {"text": new_text}}
            )
            new_emb = new_cache.get(new_id, {}).get("embedding")
        except Exception as e:
            logger.exception("Failed to embed new bug %s: %s", new_id, e)
            new_emb = None
        
        for hist_entry in historical_entries:
            hist_id = str(hist_entry.get("id"))
            
            if hist_id == new_id:
                continue
            
            hist_emb = hist_cache.get(hist_id, {}).get("embedding")
            
            # Compute base embedding score
            base_score = 0.0
            if new_emb and hist_emb:
                try:
                    base_score = _cosine(new_emb, hist_emb)
                except Exception:
                    base_score = 0.0
            
            # Compute boosts
            total_boost, boost_list = self.compute_boosts(new_bug_entry, hist_entry)
            final_score = base_score + total_boost
            
            llm_verdict = None
            is_related = False
            
            # Primary rules
            if base_score >= embedding_threshold:
                is_related = True
                logger.debug("Bug %s similar to %s: base_score=%.3f >= %.2f", 
                           new_id, hist_id, base_score, embedding_threshold)
            elif borderline_threshold <= base_score < embedding_threshold:
                # Borderline case - could use LLM for disambiguation
                # For now, rely on final score with boosts
                if final_score >= final_threshold:
                    is_related = True
                    llm_verdict = "boosted_match"
            
            # Final threshold check
            if not is_related and final_score >= final_threshold:
                is_related = True
                logger.debug(
                    "Bug %s related to %s via final_score: base=%.3f + boosts=%s = %.3f >= %.2f",
                    new_id, hist_id, base_score, boost_list, final_score, final_threshold
                )
            
            if is_related:
                results.append((hist_entry, final_score, boost_list, llm_verdict))
                logger.info(
                    "MATCH: New bug %s related to historical %s (base=%.3f, boosts=%s, final=%.3f)",
                    new_id, hist_id, base_score, boost_list, final_score
                )
        
        results.sort(key=lambda x: x[1], reverse=True)
        return results

    async def synthesize_rcas(self, rca_summaries: List[str]) -> Tuple[str, str]:
        """Synthesize multiple RCA summaries into consolidated root causes and patterns."""
        if not rca_summaries:
            return "No RCA information available from similar bugs.", ""
        
        # Simple concatenation for now - could use LLM for better synthesis
        probable_causes = " | ".join(rca_summaries[:3])
        patterns = ""
        
        # Extract common keywords as patterns
        all_tokens = set()
        for rca in rca_summaries:
            tokens = self.extract_tokens(rca)
            all_tokens.update(tokens)
        
        if all_tokens:
            patterns = ", ".join(list(all_tokens)[:10])
        
        return probable_causes, patterns

    def compose_email_body(self, email_data: Dict[str, Any]) -> str:
        """Compose email body from email data with structured RCA section."""
        org_url = self.org_url or ""
        new_id = email_data.get("new_bug_id")
        new_title = email_data.get("new_bug_title") or ""
        dev_name = email_data.get("developer_name") or "Unassigned"
        dev_email = email_data.get("developer_email") or ""
        area_module = email_data.get("area_module") or "Not specified"

        similar = email_data.get("similar_bug_ids") or []
        if similar:
            similar_html = "<ul>"
            for item in similar:
                try:
                    if isinstance(item, dict):
                        sid = item.get("id")
                        stitle = item.get("title") or ""
                    else:
                        sid = item
                        stitle = ""

                    link = f"{org_url}/_workitems/edit/{sid}" if org_url else f"WI {sid}"
                    title_html = f" - {stitle}" if stitle else ""
                    similar_html += f"<li><a href=\"{link}\">WI {sid}</a>{title_html}</li>"
                except Exception:
                    similar_html += f"<li>WI {item}</li>"
            similar_html += "</ul>"
        else:
            similar_html = "<p>None identified</p>"

        # Get structured RCA data
        rca_data = email_data.get("structured_rca") or {}
        probable = rca_data.get("probable_cause") or email_data.get("probable_root_causes") or "No consolidated RCA available."
        gap_in_rca = rca_data.get("gap_in_rca") or ""
        similar_detail = rca_data.get("similar_bugs_detail") or ""
        impact = rca_data.get("impact_analysis") or ""
        fix_summary = rca_data.get("fix_summary") or ""
        preventive = rca_data.get("preventive_actions") or ""
        patterns = email_data.get("repeated_patterns") or "None identified"

        new_link = f"{org_url}/_workitems/edit/{new_id}" if org_url else f"WI {new_id}"

        suggested = (
            "<ol>"
            "<li>Review the linked historical issues and confirm whether they share a common stack/component.</li>"
            "<li>Assign an owner to validate the probable root causes and propose immediate mitigations.</li>"
            "<li>Update the canonical RCA document with any confirmed fixes and add monitoring/alerts to catch recurrence.</li>"
            "</ol>"
        )

        # Build structured RCA section
        structured_rca_html = ""
        if probable or gap_in_rca or impact or fix_summary or preventive:
            structured_rca_html = """
    <h3>Structured RCA (Suggested)</h3>
    <table style='border-collapse:collapse;width:100%;border:1px solid #ddd;'>
"""
            if probable:
                structured_rca_html += f"""
      <tr>
        <td style='padding:8px;border:1px solid #ddd;background:#f8f8f8;width:180px;vertical-align:top;'><strong>Root Cause</strong></td>
        <td style='padding:8px;border:1px solid #ddd;color:#c00'>{html.escape(probable)}</td>
      </tr>
"""
            if gap_in_rca:
                structured_rca_html += f"""
      <tr>
        <td style='padding:8px;border:1px solid #ddd;background:#f8f8f8;vertical-align:top;'><strong>Gap in Previous RCA</strong></td>
        <td style='padding:8px;border:1px solid #ddd;color:#069'>{html.escape(gap_in_rca)}</td>
      </tr>
"""
            if similar_detail:
                lines = similar_detail.replace("\n", "<br/>")
                structured_rca_html += f"""
      <tr>
        <td style='padding:8px;border:1px solid #ddd;background:#f8f8f8;vertical-align:top;'><strong>Similar Historical Bugs</strong></td>
        <td style='padding:8px;border:1px solid #ddd;'>{lines}</td>
      </tr>
"""
            if impact:
                structured_rca_html += f"""
      <tr>
        <td style='padding:8px;border:1px solid #ddd;background:#f8f8f8;vertical-align:top;'><strong>Impact Analysis</strong></td>
        <td style='padding:8px;border:1px solid #ddd;'>{html.escape(impact)}</td>
      </tr>
"""
            if fix_summary:
                structured_rca_html += f"""
      <tr>
        <td style='padding:8px;border:1px solid #ddd;background:#f8f8f8;vertical-align:top;'><strong>Fix Summary</strong></td>
        <td style='padding:8px;border:1px solid #ddd;'>{html.escape(fix_summary)}</td>
      </tr>
"""
            if preventive:
                preventive_html = preventive.replace("\n", "<br/>")
                structured_rca_html += f"""
      <tr>
        <td style='padding:8px;border:1px solid #ddd;background:#f8f8f8;vertical-align:top;'><strong>Preventive Actions</strong></td>
        <td style='padding:8px;border:1px solid #ddd;'><ul style='margin:0;padding-left:20px;'>{preventive_html}</ul></td>
      </tr>
"""
            structured_rca_html += "</table>"

        email_html = f"""
<html>
  <body style='font-family:Arial,Helvetica,sans-serif;color:#111'>
    <h2>New Bug Feedback — Similar Past Issues & RCA Reference</h2>
    <p>A new bug has been reported. Below is a concise summary with related historical issues and suggested next steps.</p>

    <h3>New Bug</h3>
    <p><strong>ID:</strong> <a href=\"{new_link}\">WI {new_id}</a></p>
    <p><strong>Title:</strong> {html.escape(new_title)}</p>
    <p><strong>Assigned Developer:</strong> {html.escape(dev_name)} <em>({html.escape(dev_email)})</em></p>
    <p><strong>Affected Area / Module:</strong> {html.escape(area_module)}</p>

    <h3>Similar Historical Bug(s)</h3>
    {similar_html}

    <h3>Probable Root Causes (Consolidated)</h3>
    <p>{html.escape(probable)}</p>

    <h3>Repeated Patterns</h3>
    <p>{html.escape(patterns)}</p>

    {structured_rca_html}

    <h3>Suggested Next Steps</h3>
    {suggested}

    <hr/>
    <p style='font-size:0.9em;color:#666'>This message was generated automatically by the Project Management AI Agent.</p>
  </body>
</html>
"""
        return email_html

    async def process_bug(
        self,
        bug: Dict[str, Any],
        historical_entries: List[Dict[str, Any]],
        hist_cache: Dict[str, Dict[str, Any]],
        config: Dict[str, Any],
        embedding_threshold: float = 0.82
    ) -> Tuple[int, Optional[Dict[str, Any]]]:
        """
        Process a single new bug and find related historical bugs.
        
        Returns (bug_id, email_data_dict or None).
        """
        bug_id = bug.get("id")
        fields = bug.get("fields", {})
        
        logger.info("Processing new bug %s: %s", bug_id, fields.get("System.Title", ""))
        
        # Build normalized entry for new bug
        new_entry = {
            "id": bug_id,
            "raw": bug,
            "title": self.get_field(fields, "System.Title"),
            "desc_norm": self.normalize_text(self.get_field(fields, "System.Description")),
            "repro_norm": self.normalize_text(self.get_field(fields, "System.ReproSteps")),
            "tags": self.get_field(fields, "System.Tags"),
            "area": self.get_field(fields, "System.AreaPath"),
            "module": self.get_field(fields, "Custom.Module", "Custom.Component"),
            "error": self.get_field(fields, "Custom.ErrorText", "System.History"),
            "fields": fields,
        }
        
        # Determine developer info
        dev_email = self.get_developer_email(fields, config)
        dev_name = ""
        assigned_to = fields.get("System.AssignedTo")
        if isinstance(assigned_to, dict):
            dev_name = assigned_to.get("displayName", "")
        elif isinstance(assigned_to, str):
            dev_name = assigned_to
        if not dev_name:
            dev_name = dev_email or "Unassigned"
        
        logger.info("Bug %s assigned to: %s (email: %s)", bug_id, dev_name, dev_email)
        
        # Find similar historical bugs
        similar_bugs = await self.find_similar_bugs(
            new_entry, 
            historical_entries, 
            hist_cache,
            embedding_threshold=embedding_threshold
        )
        
        if not similar_bugs:
            logger.info("No similar historical bugs found for bug %s", bug_id)
            return bug_id, None
        
        logger.info("Found %d similar historical bugs for bug %s", len(similar_bugs), bug_id)
        
        # Extract RCA from similar bugs using improved logic
        rca_summaries = []
        similar_ids = []
        
        for hist_entry, score, boosts, llm_verdict in similar_bugs[:10]:
            hist_id = hist_entry.get("id")
            hist_title = hist_entry.get("title") or ""
            similar_ids.append({"id": hist_id, "title": hist_title})
            
            rca_content = self.find_rca_content(hist_entry.get("fields", {}))
            if rca_content:
                summary = rca_content[:200]  # Truncate
                if summary:
                    rca_summaries.append(f"Bug #{hist_id}: {summary}")
        
        # Synthesize structured RCA
        structured_rca = self.synthesize_rca_from_bugs(new_entry, similar_bugs)
        
        # Also get simple synthesis for backward compat
        probable_causes, patterns = await self.synthesize_rcas(rca_summaries)
        
        # Merge structured RCA probable_cause if no explicit RCA was found
        if not rca_summaries and structured_rca.get("probable_cause"):
            probable_causes = structured_rca.get("probable_cause")
        
        # Build email data
        email_data = {
            "new_bug_id": bug_id,
            "new_bug_title": new_entry.get("title"),
            "developer_name": dev_name,
            "developer_email": dev_email,
            "similar_bug_ids": similar_ids,
            "probable_root_causes": probable_causes,
            "repeated_patterns": patterns,
            "area_module": new_entry.get("area") or new_entry.get("module") or "Not specified",
            "structured_rca": structured_rca,
        }
        
        return bug_id, email_data

    async def run_workflow(
        self,
        lookback_minutes: int = 1440,
        historical_days: Optional[int] = 30,
        embedding_threshold: float = 0.82,
        historical_scope: Optional[str] = None,
        is_test: bool = False,
        force_send: bool = False
    ) -> Dict[str, Any]:
        """
        Main workflow to detect new bugs and send feedback emails.
        
        Args:
            lookback_minutes: How far back to look for new bugs
            historical_days: How far back to look for historical bugs (None = all)
            embedding_threshold: Similarity threshold for matching
            historical_scope: 'previous_sprint' to use sprint dates, else use historical_days
            is_test: Test mode flag
            force_send: Send emails even if no similar bugs found
        
        Returns summary dict with processing results.
        """
        from utilities.ado_async import run_wiql_async, fetch_workitems_async, fetch_comments_async
        from utilities.llm_embeddings import embed_texts_with_cache
        from utilities.emailer import send_report_attachment
        
        config = self._load_config()
        
        if not self.org_url or not self.project or not self.pat:
            logger.error("Missing ADO configuration (ADO_ORG_URL, ADO_PROJECT, ADO_PAT)")
            return {"error": "Missing ADO configuration"}
        
        # Get recipients from config
        recipients = config.get("reportEmailRecipients", [])
        if not recipients:
            logger.warning("No reportEmailRecipients configured in config.yaml")
        
        logger.info("Email recipients: %s", recipients)
        
        # Read processed bug IDs
        processed_ids = self._read_processed()
        processed_set = set(processed_ids)
        logger.info("Already processed %d bug IDs", len(processed_ids))
        
        # Query for new bugs
        lookback_time = datetime.now(timezone.utc) - timedelta(minutes=lookback_minutes)
        iso_date = lookback_time.strftime("%Y-%m-%d")
        
        wiql_new = f"""
            SELECT [System.Id] FROM WorkItems
            WHERE [System.TeamProject] = '{self.project}'
              AND [System.WorkItemType] = 'Bug'
              AND [System.CreatedDate] >= '{iso_date}'
            ORDER BY [System.CreatedDate] DESC
        """
        
        logger.info("Querying new bugs created since %s", iso_date)
        
        try:
            new_bug_ids = await run_wiql_async(self.org_url, wiql_new, project=self.project)
            logger.info("Found %d bug(s) in lookback window", len(new_bug_ids))
        except Exception as e:
            logger.exception("Failed to query new bugs: %s", e)
            return {"error": str(e)}
        
        # Filter out already-processed bugs
        unprocessed_ids = [bid for bid in new_bug_ids if bid not in processed_set]
        logger.info("Unprocessed bugs: %d", len(unprocessed_ids))
        
        if not unprocessed_ids and not is_test:
            logger.info("No new unprocessed bugs found")
            return {"processed": 0, "emails_sent": 0}
        
        # Fetch new bug details
        new_bugs = []
        if unprocessed_ids:
            try:
                new_bugs = await fetch_workitems_async(self.org_url, unprocessed_ids)
                logger.info("Fetched %d new bug work items", len(new_bugs))
            except Exception as e:
                logger.exception("Failed to fetch new bugs: %s", e)
        
        # Fetch historical bugs for comparison. 
        # If historical_days is None (meaning 'all'), query ALL bugs regardless of historical_scope.
        # Otherwise, if historical_scope == 'previous_sprint', try to determine sprint dates.
        wiql_hist = None
        
        # Handle "all" case first - overrides historical_scope
        if historical_days is None:
            wiql_hist = """
                SELECT [System.Id] FROM WorkItems
                WHERE [System.WorkItemType] = 'Bug'
                ORDER BY [System.CreatedDate] DESC
            """
            logger.info("Querying ALL historical bugs (historical_days='all')")
        elif historical_scope == 'previous_sprint':
            # Attempt to determine previous sprint dates using ADO Iterations API
            try:
                team = config.ado_team or None
                pat = self.pat
                if not pat:
                    raise RuntimeError('PAT not available')
                # Build iterations API URL
                if team:
                    iterations_url = f"{self.org_url}/{self.project}/{team}/_apis/work/teamsettings/iterations?api-version=7.0"
                else:
                    iterations_url = f"{self.org_url}/{self.project}/_apis/work/teamsettings/iterations?api-version=7.0"

                import requests
                resp = requests.get(iterations_url, auth=("", pat), timeout=30)
                if resp.status_code == 200:
                    data = resp.json()
                    iters = data.get('value', [])
                    # Find the current iteration and pick the immediate previous finished iteration
                    prev_iter = None
                    # Sort by start date
                    sorted_iters = sorted(
                        [it for it in iters if it.get('attributes')],
                        key=lambda x: x['attributes'].get('startDate') or ''
                    )
                    # Find the last iteration that finished before now
                    now_iso = datetime.now(timezone.utc).isoformat()
                    for it in reversed(sorted_iters):
                        finish = it['attributes'].get('finishDate')
                        if finish and finish < now_iso:
                            prev_iter = it
                            break

                    if prev_iter:
                        start_date = prev_iter['attributes'].get('startDate')
                        finish_date = prev_iter['attributes'].get('finishDate')
                        if start_date and finish_date:
                            # Use created date range for previous sprint
                            wiql_hist = f"""
                                SELECT [System.Id] FROM WorkItems
                                WHERE [System.WorkItemType] = 'Bug'
                                  AND [System.CreatedDate] >= '{start_date[:10]}'
                                  AND [System.CreatedDate] <= '{finish_date[:10]}'
                                ORDER BY [System.CreatedDate] DESC
                            """
                            logger.info("Querying historical bugs for previous sprint: %s - %s", start_date, finish_date)
                    else:
                        logger.warning("Could not determine previous iteration; falling back to historical_days")
                else:
                    logger.warning("Failed to fetch iterations: %s %s", resp.status_code, resp.text[:200])
            except Exception as e:
                logger.exception("Error determining previous sprint: %s", e)

        if wiql_hist is None:
            if historical_days is None:
                # Query ALL historical bugs (no date filter)
                wiql_hist = """
                    SELECT [System.Id] FROM WorkItems
                    WHERE [System.WorkItemType] = 'Bug'
                    ORDER BY [System.CreatedDate] DESC
                """
                logger.info("Querying ALL historical bugs (no date limit)")
            else:
                hist_date = (datetime.now(timezone.utc) - timedelta(days=historical_days)).strftime("%Y-%m-%d")
                wiql_hist = f"""
                    SELECT [System.Id] FROM WorkItems
                    WHERE [System.WorkItemType] = 'Bug'
                      AND [System.CreatedDate] >= '{hist_date}'
                    ORDER BY [System.CreatedDate] DESC
                """
                logger.info("Querying historical bugs created since %s", hist_date)
        
        try:
            hist_bug_ids = await run_wiql_async(self.org_url, wiql_hist, project=None)
            hist_bug_ids = [hid for hid in hist_bug_ids if hid not in unprocessed_ids]
            logger.info("Found %d historical bug IDs", len(hist_bug_ids))
        except Exception as e:
            logger.exception("Failed to query historical bugs: %s", e)
            hist_bug_ids = []
        
        # Fetch historical bugs in batches
        hist_bugs = []
        BATCH_SIZE = 200
        for i in range(0, len(hist_bug_ids), BATCH_SIZE):
            chunk = hist_bug_ids[i:i + BATCH_SIZE]
            try:
                chunk_items = await fetch_workitems_async(self.org_url, chunk)
                if chunk_items:
                    hist_bugs.extend(chunk_items)
            except Exception as e:
                logger.exception("Failed to fetch historical bugs chunk: %s", e)
        
        # Also fetch comments for historical bugs (helps extract RCAs present in comments)
        try:
            hist_ids = [h.get("id") for h in hist_bugs if h.get("id")]
            if hist_ids:
                comments_map = await fetch_comments_async(self.org_url, hist_ids)
            else:
                comments_map = {}
        except Exception:
            comments_map = {}

        logger.info("Fetched %d historical bug work items", len(hist_bugs))
        
        # Build normalized entries for historical bugs
        hist_entries = []
        for h in hist_bugs:
            fields = h.get("fields", {})
            # Attach concatenated comments into a field for RCA extraction
            try:
                cid = h.get("id")
                ctext = comments_map.get(cid) if isinstance(comments_map, dict) else None
                if ctext:
                    # Prefer an explicit field so find_rca_content can detect it
                    fields["System.Comments"] = ctext
            except Exception:
                pass
            hist_entries.append({
                "id": h.get("id"),
                "raw": h,
                "title": self.get_field(fields, "System.Title"),
                "desc_norm": self.normalize_text(self.get_field(fields, "System.Description")),
                "repro_norm": self.normalize_text(self.get_field(fields, "System.ReproSteps")),
                "tags": self.get_field(fields, "System.Tags"),
                "area": self.get_field(fields, "System.AreaPath"),
                "module": self.get_field(fields, "Custom.Module", "Custom.Component"),
                "error": self.get_field(fields, "Custom.ErrorText", "System.History"),
                "fields": fields,
            })
        
        # Build embedding cache for historical bugs
        logger.info("Building embeddings for %d historical bugs...", len(hist_entries))
        hist_map = {}
        for entry in hist_entries:
            hid = str(entry.get("id"))
            hist_map[hid] = {"text": self.build_bug_text(entry)}
        
        loop = asyncio.get_event_loop()
        try:
            hist_cache = await loop.run_in_executor(None, embed_texts_with_cache, hist_map)
            logger.info("Built embedding cache with %d entries", len(hist_cache))
        except Exception as e:
            logger.exception("Failed to build historical embeddings: %s", e)
            hist_cache = {}
        
        # Process each new bug
        emails_to_send = []
        processed_in_run = []
        
        for bug in new_bugs:
            bug_id = bug.get("id")
            try:
                bid, email_data = await self.process_bug(
                    bug, hist_entries, hist_cache, config, embedding_threshold
                )
                
                if email_data:
                    processed_in_run.append(bid)
                    emails_to_send.append(email_data)
                else:
                    logger.info(
                        "Bug %s had no similar historical bugs — leaving unprocessed for future runs",
                        bid,
                    )
            except Exception as e:
                logger.exception("Error processing bug %s: %s", bug_id, e)
        
        # Send emails
        emails_sent = 0
        if (emails_to_send or force_send) and recipients:
            for email_data in emails_to_send:
                subject = "New Bug Feedback – Similar Past Issues & RCA Reference"
                body = self.compose_email_body(email_data)
                
                try:
                    ok, msg = await loop.run_in_executor(
                        None,
                        lambda: send_report_attachment(recipients, subject, body, attachments=None)
                    )
                    if ok:
                        emails_sent += 1
                        logger.info(
                            "Email sent for bug %s to %s",
                            email_data.get("new_bug_id"), recipients
                        )
                    else:
                        logger.error(
                            "Failed to send email for bug %s: %s",
                            email_data.get("new_bug_id"), msg
                        )
                except Exception as e:
                    logger.exception("Exception sending email: %s", e)
        
        # Update processed IDs
        new_processed = list(set(processed_ids + processed_in_run))
        self._write_processed(new_processed)
        logger.info("Updated processed bugs file with %d total IDs", len(new_processed))
        
        return {
            "processed": len(processed_in_run),
            "emails_sent": emails_sent,
            "new_bugs_found": len(new_bugs),
            "similar_matches": len(emails_to_send),
        }
