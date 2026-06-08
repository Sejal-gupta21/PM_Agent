"""
Detect recurring bugs by area from Azure DevOps and email a 'Bug Areas Highlight' summary.

Behavior:
- Uses env vars: ADO_ORG_URL, ADO_PROJECT, and PAT via utilities.mcp.pat.get_pat()
- Reads options from a passed config dict (lookback_days, recurrence_threshold, similarity_threshold)
- Sends HTML email using utilities.emailer.send_report_attachment
- Logs detection, summary creation, scheduler triggers, and email delivery to logs/bug_areas_highlight.log
"""

import os
import logging
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from datetime import datetime
from typing import List, Dict, Any, Optional
from difflib import SequenceMatcher
import re
from collections import defaultdict
from html import unescape
from typing import Tuple
from utilities.mcp.pat import get_pat
from utilities.emailer import send_report_attachment
from utilities.langfuse_client import trace_task

logger = logging.getLogger("pm_agent.bug_areas_highlight")
logger.setLevel(logging.INFO)
if not logger.handlers:
	os.makedirs("logs", exist_ok=True)
	fh = logging.FileHandler("logs/bug_areas_highlight.log")
	fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
	logger.addHandler(fh)

API_VERSION = "7.0"


def _create_requests_session(retries: int = 3, backoff_factor: float = 0.5, status_forcelist=(429, 500, 502, 503, 504)) -> requests.Session:
	"""Create a requests Session with retry/backoff behavior for transient ADO errors."""
	session = requests.Session()
	retry = Retry(total=retries, read=retries, connect=retries, backoff_factor=backoff_factor,
				  status_forcelist=status_forcelist, raise_on_status=False)
	adapter = HTTPAdapter(max_retries=retry)
	session.mount("https://", adapter)
	session.mount("http://", adapter)
	return session


# shared session used for ADO requests with retries
SESSION = _create_requests_session()


def run_wiql(org_url: str, wiql: str, pat: str, project: Optional[str] = None, team: Optional[str] = None) -> List[int]:
	if project and team:
		url = f"{org_url}/{project}/{team}/_apis/wit/wiql?api-version={API_VERSION}"
	elif project:
		url = f"{org_url}/{project}/_apis/wit/wiql?api-version={API_VERSION}"
	else:
		url = f"{org_url}/_apis/wit/wiql?api-version={API_VERSION}"
	resp = SESSION.post(url, json={"query": wiql}, auth=("", pat), timeout=30)
	if resp.status_code >= 400:
		logger.error("WIQL query failed: %s %s", resp.status_code, resp.text)
		raise RuntimeError(f"WIQL request failed: {resp.status_code} {resp.text}")
	data = resp.json()
	return [item["id"] for item in data.get("workItems", [])]


def fetch_workitems(org_url: str, ids: List[int], pat: str) -> List[Dict[str, Any]]:
	if not ids:
		return []
	batch = 200
	results: List[Dict[str, Any]] = []
	for i in range(0, len(ids), batch):
		chunk = ids[i: i + batch]
		ids_chunk = ",".join(map(str, chunk))
		url = f"{org_url}/_apis/wit/workitems?ids={ids_chunk}&$expand=all&api-version={API_VERSION}"
		resp = SESSION.get(url, auth=("", pat), timeout=30)
		try:
			resp.raise_for_status()
		except Exception as e:
			logger.exception("Failed fetching workitems chunk: %s", e)
			continue
		data = resp.json()
		results.extend(data.get("value", []))
	return results


def normalize_title(t: str) -> str:
	if not t:
		return ""
	s = t.lower()
	# remove common job/IDs like 'job# 12345' or '(Job# 123)'
	s = re.sub(r"job#\s*\d+", "", s)
	s = re.sub(r"\(job#\s*\d+\)", "", s)
	# remove numeric ids and run numbers
	s = re.sub(r"\d+", "", s)
	# remove punctuation
	s = re.sub(r"[^a-z\s]", " ", s)
	# collapse whitespace
	s = re.sub(r"\s+", " ", s).strip()
	return s


def normalize_description(d: str) -> str:
	if not d:
		return ""
	# strip simple HTML tags and unescape entities
	txt = re.sub(r"<[^>]+>", " ", d)
	txt = unescape(txt)
	# collapse and normalize similar to title
	return normalize_title(txt)


def get_bug_ref(wi: Dict[str, Any]) -> str:
	"""
	Resolve bug reference from work item fields.
	Prefers explicit bug-ref fields, falls back to work item id.
	"""
	fields = wi.get("fields", {}) if isinstance(wi, dict) else {}
	# Priority order for bug reference fields
	candidates = [
		"Custom.BugNumber",
		"Custom.BugRef",
		"Custom.Ref",
		"Ref",
		"System.BugNumber",
	]
	for k in candidates:
		v = fields.get(k)
		if v:
			return str(v)
	# Fallback to work item ID
	wi_id = wi.get("id") or fields.get("System.Id")
	return str(wi_id) if wi_id else ""


def _text_similarity(a: str, b: str) -> float:
	return SequenceMatcher(None, (a or ""), (b or "")).ratio()


def title_similarity(a: str, b: str) -> float:
	return SequenceMatcher(None, normalize_title(a), normalize_title(b)).ratio()


def _area_prefix(area: str, depth: int, default_label: str = "No Area") -> str:
	if not area:
		return default_label
	parts = [p for p in area.split("\\") if p]
	if depth <= 0:
		return area
	return "\\".join(parts[:depth]) if parts else (area or default_label)


def detect_recurring(
	bugs: List[Dict[str, Any]],
	similarity_threshold: float = 0.75,
	recurrence_threshold: int = 3,
	area_grouping_depth: int = 3,
	use_tfidf: bool = False,
	app_context: str = "",
	no_area_label: str = "No Area",
) -> Dict[str, Any]:
	"""
	Group bugs by Area Path and detect recurring patterns.
	Returns a dict keyed by area with summary info when recurrence found.
	"""
	by_area: Dict[str, List[Dict[str, Any]]] = {}
	for wi in bugs:
		fields = wi.get("fields", {})
		area = fields.get("System.AreaPath") or ""
		# normalize missing/empty area into a clear bucket (configurable)
		if not area or (isinstance(area, str) and not area.strip()):
			area = no_area_label
		area_key = _area_prefix(area, area_grouping_depth, default_label=no_area_label)
		title = (fields.get("System.Title") or "").strip()
		wi_id = wi.get("id")
		bug_ref = get_bug_ref(wi)
		url = wi.get("_links", {}).get("html", {}).get("href") or ""
		created = fields.get("System.CreatedDate")
		tags = (fields.get("System.Tags") or "")
		component = (
			fields.get("Custom.Component")
			or fields.get("Microsoft.VSTS.Common.ChangedBy")
			or fields.get("System.AreaPath")
			or ""
		)
		# include common repro fields used in ADO work items
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

	# log if any work items lack an AreaPath
	if no_area_label in by_area:
		logger.info("Found %d work items without AreaPath; grouped under '%s'", len(by_area.get(no_area_label, [])), no_area_label)

	for area, items in by_area.items():
		# simple exact-title repeats for small areas
		if len(items) < recurrence_threshold:
			title_counts: Dict[str, List[Dict[str, Any]]] = {}
			for it in items:
				key = normalize_title(it["title"]) or it["title"]
				title_counts.setdefault(key, []).append(it)
			exact_repeats = {t: l for t, l in title_counts.items() if len(l) >= 2}
			if exact_repeats:
				clusters = []
				for t, l in exact_repeats.items():
					clusters.append({"reason": "exact", "title": t, "members": l})
				recurring[area] = {"items": items, "count": sum(len(l) for l in exact_repeats.values()), "pattern": "Exact title repeats", "clusters": clusters}
			continue

		# build clusters either via TF-IDF (optional) or greedy pairwise matching
		n = len(items)
		clusters: List[List[Dict[str, Any]]] = []

		if use_tfidf:
			try:
				from sklearn.feature_extraction.text import TfidfVectorizer
				from sklearn.metrics.pairwise import cosine_similarity

				docs: List[str] = []
				for it in items:
					# compose a richer document for semantic similarity: title, description, repro steps, tags, component, app context
					t = normalize_title(it.get("title") or "")
					d = normalize_description(it.get("description") or "")
					r = normalize_description(it.get("repro") or "")
					tags = normalize_title(it.get("tags") or "")
					comp = normalize_title(it.get("component") or "")
					docs.append(" ".join([t, d, r, tags, comp, app_context or ""]).strip())

				if any(docs):
					vec = TfidfVectorizer().fit_transform(docs)
					sim_matrix = cosine_similarity(vec)
					used = [False] * n
					for i in range(n):
						if used[i]:
							continue
						used[i] = True
						cluster = [items[i]]
						for j in range(i + 1, n):
							if used[j]:
								continue
							if sim_matrix[i, j] >= similarity_threshold:
								cluster.append(items[j])
								used[j] = True
						if len(cluster) > 1:
							clusters.append(cluster)
					# log TF-IDF usage summary
					logger.info("TF-IDF clustering produced %d cluster(s) for area '%s'", len(clusters), area)
				else:
					clusters = []
			except Exception as e:
				logger.info("TF-IDF clustering unavailable or failed (%s), falling back to title/description matching", e)

		# fallback greedy pairwise clustering if TF-IDF not used or failed
		if not clusters:
			logger.info("Using pairwise title/description matching for area '%s' (threshold=%.2f)", area, similarity_threshold)
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
					title_sim = title_similarity(base["title"], items[j]["title"])
					matched = False
					if title_sim >= similarity_threshold:
						matched = True
					else:
						desc_a = normalize_description(base.get("description") or "")
						desc_b = normalize_description(items[j].get("description") or "")
						if desc_a and desc_b:
							desc_sim = _text_similarity(desc_a, desc_b)
							if desc_sim >= similarity_threshold:
								matched = True
					if matched:
						cluster.append(items[j])
						used[j] = True
				if len(cluster) > 1:
					clusters.append(cluster)
			# log greedy clustering summary
			if clusters:
				logger.info("Greedy clustering found %d cluster(s) in area '%s' (total items=%d)", len(clusters), area, n)
				for ci, c in enumerate(clusters, start=1):
					titles = [normalize_title(m.get('title') or '')[:80] for m in c[:4]]
					logger.info("  cluster %d: size=%d sample_titles=%s", ci, len(c), titles)

		total_clustered = sum(len(c) for c in clusters)
		if total_clustered >= recurrence_threshold:
			recurring[area] = {"items": items, "count": total_clustered, "clusters": clusters}
		else:
			# also include exact repeats if present even when clusters are small
			title_counts: Dict[str, List[Dict[str, Any]]] = {}
			for it in items:
				key = normalize_title(it["title"]) or it["title"]
				title_counts.setdefault(key, []).append(it)
			exact_repeats = {t: l for t, l in title_counts.items() if len(l) >= 2}
			if exact_repeats:
				clusters = []
				for t, l in exact_repeats.items():
					clusters.append({"reason": "exact", "title": t, "members": l})
				recurring[area] = {"items": items, "count": sum(len(l) for l in exact_repeats.values()), "pattern": "Exact title repeats", "clusters": clusters}

	return recurring


def escape_html(s: str) -> str:
	import html

	return html.escape(s or "")


def build_html_summary(org_url: str, recurring: Dict[str, Any]) -> str:
	# Manager/PM-ready HTML report template
	from datetime import datetime as _dt
	ts = _dt.utcnow().strftime("%Y-%m-%d %H:%M UTC")
	header = [
		"<html><head><meta charset='utf-8'><title>Bug Areas Highlight</title></head>",
		"<body style='font-family:Arial,Helvetica,sans-serif;color:#111;margin:20px'>",
		"<div style='max-width:900px'>",
		"<div style='display:flex;justify-content:space-between;align-items:center'>",
		"<div><h1 style='margin:0;padding:0'>Bug Areas Highlight</h1>",
		f"<div style='color:#666;font-size:13px;margin-top:4px'>Generated: {escape_html(ts)}</div></div>",
		"<div style='text-align:right;color:#666;font-size:12px'>Automated report — for PM / TL / Stakeholders</div>",
		"</div>",
		"<hr style='margin:12px 0'>",
	]

	if not recurring:
		header.append("<p>No recurring bugs were detected in the configured lookback window.</p>")
		header.append("</div></body></html>")
		return "\n".join(header)

	parts: List[str] = header

	# Executive summary
	total_areas = len(recurring)
	total_recurring = sum(info.get("count", 0) for info in recurring.values())
	parts.append(f"<h2 style='margin-top:6px'>Executive summary</h2>")
	parts.append(f"<p style='color:#333'>This report identifies <strong>{total_areas}</strong> application area(s) with recurring bug reports, containing <strong>{total_recurring}</strong> repeated occurrence(s) in the configured lookback window. The table below highlights affected areas and representative work items for triage.</p>")

	# Summary table of affected areas
	parts.append("<h3 style='margin-top:18px'>Affected areas</h3>")
	parts.append("<table style='border-collapse:collapse;width:100%;border:1px solid #e6e6e6'>")
	parts.append("<thead style='background:#f7f7f7'><tr>")
	parts.append("<th style='text-align:left;padding:10px;border-bottom:1px solid #e6e6e6'>Area</th>")
	parts.append("<th style='text-align:left;padding:10px;border-bottom:1px solid #e6e6e6'>Occurrences</th>")
	parts.append("<th style='text-align:left;padding:10px;border-bottom:1px solid #e6e6e6'>Representative Bugs (top)</th>")
	parts.append("<th style='text-align:left;padding:10px;border-bottom:1px solid #e6e6e6'>Priority</th>")
	parts.append("<th style='text-align:left;padding:10px;border-bottom:1px solid #e6e6e6'>Assigned To</th>")
	parts.append("<th style='text-align:left;padding:10px;border-bottom:1px solid #e6e6e6'>First seen</th>")
	parts.append("<th style='text-align:left;padding:10px;border-bottom:1px solid #e6e6e6'>Last seen</th>")
	parts.append("</tr></thead>")
	parts.append("<tbody>")
	for area, info in recurring.items():
		count = info.get("count", 0)
		items = info.get("items") or []
		# sort by created date if available
		dates = [it.get("created") for it in items if it.get("created")]
		first_seen = min(dates) if dates else "-"
		last_seen = max(dates) if dates else "-"
		links_html = []
		for it in items[:3]:
			link = it.get("url") or f"{org_url}/_workitems/edit/{it['id']}"
			bug_ref = it.get("bug_ref") or str(it.get("id", ""))
			title_short = (it.get("title") or "")[:80]
			if len(it.get("title") or "") > 80:
				title_short += "..."
			links_html.append(f"<a href=\"{escape_html(link)}\">Bug {escape_html(bug_ref)}</a> — {escape_html(title_short)}")
		parts.append("<tr>")
		# aggregate top priority/assigned (take from first item if present)
		top_priority = items[0].get("priority") if items else ""
		top_assigned = items[0].get("assigned_to") if items else ""
		parts.append(f"<td style='padding:10px;border-top:1px solid #f0f0f0;vertical-align:top'>{escape_html(area)}</td>")
		parts.append(f"<td style='padding:10px;border-top:1px solid #f0f0f0;vertical-align:top'>{count}</td>")
		parts.append(f"<td style='padding:10px;border-top:1px solid #f0f0f0;vertical-align:top'>{', '.join(links_html)}</td>")
		parts.append(f"<td style='padding:10px;border-top:1px solid #f0f0f0;vertical-align:top'>{escape_html(str(top_priority or '-'))}</td>")
		parts.append(f"<td style='padding:10px;border-top:1px solid #f0f0f0;vertical-align:top'>{escape_html(str(top_assigned or '-'))}</td>")
		parts.append(f"<td style='padding:10px;border-top:1px solid #f0f0f0;vertical-align:top'>{escape_html(str(first_seen))}</td>")
		parts.append(f"<td style='padding:10px;border-top:1px solid #f0f0f0;vertical-align:top'>{escape_html(str(last_seen))}</td>")
		parts.append("</tr>")
	parts.append("</tbody></table>")

	# Detailed patterns per area
	for area, info in recurring.items():
		parts.append(f"<h3 style='margin-top:18px'>Area: {escape_html(area)} — {info.get('count', 0)} repeated occurrence(s)</h3>")
		items = info.get("items") or []
		if info.get("clusters"):
			for ci, cluster in enumerate(info["clusters"], start=1):
				parts.append(f"<h4 style='margin-bottom:6px;color:#222'>Pattern {ci} — {len(cluster)} similar items</h4>")
				parts.append("<table style='border-collapse:collapse;width:100%;margin-bottom:8px'>")
				parts.append("<thead><tr><th style='text-align:left;padding:6px;border-bottom:1px solid #e6e6e6'>Bug</th><th style='text-align:left;padding:6px;border-bottom:1px solid #e6e6e6'>Title</th><th style='text-align:left;padding:6px;border-bottom:1px solid #e6e6e6'>Created</th></tr></thead>")
				parts.append("<tbody>")
				# Handle both list clusters and dict clusters with 'members'
				cluster_items = cluster.get("members") if isinstance(cluster, dict) else cluster
				for it in (cluster_items or []):
					link = it.get("url") or f"{org_url}/_workitems/edit/{it['id']}"
					bug_ref = it.get("bug_ref") or str(it.get("id", ""))
					created = it.get("created") or "-"
					parts.append(f"<tr><td style='padding:6px;border-top:1px solid #f8f8f8'><a href=\"{escape_html(link)}\">Bug {escape_html(bug_ref)}</a></td><td style='padding:6px;border-top:1px solid #f8f8f8'>{escape_html(it.get('title') or '')}</td><td style='padding:6px;border-top:1px solid #f8f8f8'>{escape_html(str(created))}</td></tr>")
				parts.append("</tbody></table>")
			insights = infer_root_cause_patterns(info.get("clusters", []))
			parts.append("<p><strong>Root cause pattern insights</strong></p>")
			parts.append("<ul>")
			for ins in insights:
				parts.append(f"<li>{escape_html(ins)}</li>")
			parts.append("</ul>")
		else:
			parts.append("<p>Top items:</p>")
			parts.append("<ul>")
			for it in items[:10]:
				link = it.get("url") or f"{org_url}/_workitems/edit/{it['id']}"
				bug_ref = it.get("bug_ref") or str(it.get("id", ""))
				parts.append(f"<li><a href=\"{escape_html(link)}\">Bug {escape_html(bug_ref)}</a>: {escape_html(it.get('title') or '')} — {escape_html(str(it.get('created') or '-'))}</li>")
			parts.append("</ul>")
			if info.get("pattern"):
				parts.append(f"<p><strong>Pattern:</strong> {escape_html(info.get('pattern'))}</p>")

		parts.append("<p><strong>Recommended actions</strong></p>")
		parts.append("<ol>")
		parts.append("<li>Assign the area to the component owner for triage and grouping of root cause.</li>")
		parts.append("<li>Prioritise fixes by occurrence frequency and business impact; escalate critical issues.</li>")
		parts.append("<li>Expand automated regression tests focused on the affected module(s).</li>")
		parts.append("<li>Review recent deployments and roll back if a clear regression is identified.</li>")
		parts.append("<li>Schedule a focused post-mortem for repeated issues and track corrective actions.</li>")
		parts.append("</ol>")

	# Footer and contact
	parts.append("<hr style='margin-top:18px'>")
	parts.append("<p style='color:#666;font-size:12px'>For questions or to unsubscribe this report, contact the reliability engineering team or PM. This is an automated summary generated by PM-Agent.</p>")
	parts.append("</div></body></html>")
	return "\n".join(parts)


def infer_root_cause_patterns(clusters: List[Any]) -> List[str]:
	"""Generate simple root-cause hints from clusters.

	The function is intentionally conservative: it looks for keyword groups
	across cluster member titles/descriptions and returns short actionable
	hints. It accepts clusters as either lists of work-item dicts or
	dicts containing a 'members' key (backwards compatibility).
	"""
	hints: List[str] = []
	if not clusters:
		return hints

	kw_groups = {
		"database": ["db", "database", "sql", "deadlock"],
		"authentication": ["auth", "login", "token", "permission", "unauthorized"],
		"timeout": ["timeout", "timed out", "slow", "latency"],
		"api": ["api", "endpoint", "request", "response", "status code"],
		"ui": ["ui", "button", "layout", "css", "javascript"],
		"null": ["null", "none", "undefined", "nil"],
		"memory": ["memory", "out of memory", "oom"],
	}

	for cluster in clusters:
		members = cluster
		if isinstance(cluster, dict):
			members = cluster.get("members") or []

		texts: List[str] = []
		for it in (members or []):
			title = (it.get("title") or "")
			desc = (it.get("description") or "")
			texts.append(" ".join([title, desc]))

		combined = normalize_title(" ".join(texts))
		found = False
		for label, kws in kw_groups.items():
			for kw in kws:
				if kw in combined:
					hints.append(f"Investigate {label}-layer issues (found keywords like '{kw}').")
					found = True
					break
			if found:
				break

	if not hints:
		hints.append(
			"Multiple similar titles suggest a recurring functional/regression issue in the area; investigate recent changes and shared dependencies."
		)

	return hints


@trace_task("bug_areas_highlight", metadata={"source": "pm_agent"})
def run_task_from_config(config: dict):
	try:
		logger.info("Starting bug areas highlight detection run")

		options = config.get("options", {}) if config else {}
		logger.debug("Task options: %s", options)
		lookback_days = int(options.get("lookback_days", 30))
		recurrence_threshold = int(options.get("recurrence_threshold", 3))
		similarity_threshold = float(options.get("similarity_threshold", 0.75))
		area_grouping_depth = int(options.get("area_grouping_depth", 3))
		use_tfidf = bool(options.get("use_tfidf", False))
		app_context = ""
		# configurable label for items missing AreaPath
		no_area_label = str(options.get("no_area_label", "No Area"))
		# support inline app context or a file path
		if options.get("app_context"):
			app_context = str(options.get("app_context") or "")
		elif options.get("app_context_path"):
			p = options.get("app_context_path")
			try:
				with open(p, "r", encoding="utf-8") as f:
					app_context = f.read()
			except Exception:
				logger.info("Could not read app_context_path=%s", p)
		test_mode = bool(options.get("test_mode", False))
		logger.debug("test_mode=%s", test_mode)

		from config import config as app_config
		org_url = app_config.ado_org_url
		project = app_config.ado_project
		pat = get_pat()
		if not test_mode and (not org_url or not project or not pat):
			logger.error("Missing ADO configuration (ADO_ORG_URL, ADO_PROJECT, PAT). Aborting run.")
			return

		wiql = f"""
			SELECT [System.Id] FROM WorkItems
			WHERE [System.TeamProject] = '{project}'
			  AND [System.WorkItemType] = 'Bug'
			  AND [System.CreatedDate] >= @Today - {lookback_days}
			ORDER BY [System.CreatedDate] DESC
		"""

		if test_mode:
			logger.info("Test mode enabled: skipping ADO query and sending test report")
			workitems = []
		else:
			ids = run_wiql(org_url, wiql, pat, project=project)
			logger.info("WIQL returned %d bug ids (lookback_days=%d)", len(ids), lookback_days)
			if not ids:
				logger.info("No bugs found in lookback window; nothing to do")
				# If test_mode is False and no bugs found, nothing to do
				if not options.get("force_send", False):
					return
			workitems = fetch_workitems(org_url, ids, pat) if ids else []
		# allow forcing send for testing
		force_send = bool(options.get("force_send", False))
		recurring = detect_recurring(
			workitems,
			similarity_threshold,
			recurrence_threshold,
			area_grouping_depth,
			use_tfidf=use_tfidf,
			app_context=app_context,
			no_area_label=no_area_label,
		)
		if not recurring and not force_send:
			logger.info("No recurring bugs detected; skipping email send")
			return

		# build summary (if no recurring and force_send=True, summary will state none detected)
		html_body = build_html_summary(org_url, recurring)
		from config import config as app_config
		recipients = config.get("reportEmailRecipients") or app_config.report_email_recipients
		if not recipients:
			logger.error("No recipients configured; skipping email send")
			return
		if isinstance(recipients, str):
			to_list = [r.strip() for r in recipients.split(",") if r.strip()]
		else:
			to_list = recipients

		# compose subject with date and number of affected areas
		areas_count = len(recurring) if isinstance(recurring, dict) else 0
		today = datetime.utcnow().date().isoformat()
		subject = f"Bug Areas Highlight — {areas_count} area(s) — {today}"

		# write preview to file so it can be inspected before/after send
		try:
			preview_path = os.path.join("logs", "bug_areas_preview.html")
			with open(preview_path, "w", encoding="utf-8") as pf:
				pf.write(html_body)
			logger.info("Wrote email preview to %s", preview_path)
		except Exception:
			logger.exception("Failed to write email preview file")

		ok, resp = send_report_attachment(to_list, subject, html_body, attachments=None)
		if ok:
			logger.info("Bug Areas Highlight email sent to %s", to_list)
		else:
			logger.error("Error sending Bug Areas Highlight: %s", resp)
	except Exception as e:
		logger.exception("Exception during bug areas highlight run: %s", e)

