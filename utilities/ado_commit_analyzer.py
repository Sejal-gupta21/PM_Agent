"""ADO Commit Analyzer - Fetch developer commits from Azure DevOps work item links.

Instead of scanning local git history, this module fetches commit data from
Azure DevOps via the REST API by:
1. Querying work items in the project
2. Fetching linked commits for each work item
3. Aggregating commit details per developer

This gives us the tech stack knowledge of team members based on their actual
work item commits in ADO.
"""
from __future__ import annotations

import os
import re
import logging
import json
import base64
from collections import defaultdict, Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from urllib.parse import quote
import requests

logger = logging.getLogger(__name__)

# Extension to language mapping
EXT_LANG_MAP = {
    '.py': 'Python', '.ts': 'TypeScript', '.js': 'JavaScript', '.jsx': 'JavaScript',
    '.tsx': 'TypeScript', '.java': 'Java', '.go': 'Go', '.rs': 'Rust', '.rb': 'Ruby',
    '.php': 'PHP', '.html': 'HTML', '.css': 'CSS', '.scss': 'CSS', '.yml': 'YAML',
    '.yaml': 'YAML', '.sql': 'SQL', '.json': 'JSON', '.md': 'Markdown', '.cs': 'C#',
    '.cpp': 'C++', '.c': 'C', '.h': 'C/C++ Header', '.swift': 'Swift', '.kt': 'Kotlin',
    '.vue': 'Vue', '.svelte': 'Svelte', '.graphql': 'GraphQL', '.prisma': 'Prisma',
    '.tf': 'Terraform', '.sh': 'Shell', '.bash': 'Shell', '.ps1': 'PowerShell',
}


def _get_auth_header(pat: str) -> Dict[str, str]:
    """Build Authorization header for ADO REST API."""
    encoded = base64.b64encode(f":{pat}".encode()).decode()
    return {"Authorization": f"Basic {encoded}"}


def fetch_work_items_batch(
    org_url: str,
    project: str,
    pat: str,
    wi_ids: List[int],
    expand: str = "Relations"
) -> List[Dict[str, Any]]:
    """Fetch work items in batch with relations expanded."""
    if not wi_ids:
        return []
    
    # ADO allows max 200 IDs per request
    all_items = []
    batch_size = 200
    
    for i in range(0, len(wi_ids), batch_size):
        batch = wi_ids[i:i+batch_size]
        ids_str = ",".join(str(wid) for wid in batch)
        url = f"{org_url}/{project}/_apis/wit/workitems?ids={ids_str}&$expand={expand}&api-version=7.1"
        
        try:
            resp = requests.get(url, headers=_get_auth_header(pat), timeout=30)
            resp.raise_for_status()
            data = resp.json()
            all_items.extend(data.get("value", []))
        except Exception as e:
            logger.warning("Failed to fetch WI batch: %s", e)
    
    return all_items


def query_recent_work_items(
    org_url: str,
    project: str,
    pat: str,
    days: int = 90,
    max_items: int = 1000
) -> List[int]:
    """Query work items changed in the last N days using WIQL."""
    wiql = f"""
    SELECT [System.Id]
    FROM WorkItems
    WHERE [System.TeamProject] = @project
      AND [System.ChangedDate] >= @today - {days}
    ORDER BY [System.ChangedDate] DESC
    """
    
    # Use $top query parameter to limit results (WIQL doesn't support TOP in query)
    url = f"{org_url}/{project}/_apis/wit/wiql?$top={max_items}&api-version=7.1"
    body = {"query": wiql}
    
    try:
        resp = requests.post(url, json=body, headers=_get_auth_header(pat), timeout=60)
        resp.raise_for_status()
        data = resp.json()
        items = data.get("workItems", [])[:max_items]
        return [item["id"] for item in items]
    except Exception as e:
        logger.error("WIQL query failed: %s", e)
        return []


def extract_commit_links(wi: Dict[str, Any]) -> List[str]:
    """Extract commit URLs from work item relations."""
    links = []
    for rel in wi.get("relations", []) or []:
        rel_type = rel.get("rel", "")
        url = rel.get("url", "")
        
        # ADO commit links have rel types like:
        # "ArtifactLink" with url containing vstfs:///Git/Commit/...
        # Or direct links to commit resources
        if "commit" in url.lower() or "vstfs" in url.lower():
            links.append(url)
        elif rel_type in ("ArtifactLink", "Fixed in Commit", "Commit"):
            links.append(url)
    return links


def parse_vstfs_commit_url(vstfs_url: str) -> Optional[Tuple[str, str, str]]:
    """Parse vstfs:///Git/Commit/{projectId}/{repoId}/{commitId} URL.
    
    Returns (projectId, repoId, commitId) or None.
    URLs may be URL-encoded (e.g., %2f for /).
    """
    from urllib.parse import unquote
    
    # URL-decode the string first (handles %2f -> /)
    decoded_url = unquote(vstfs_url)
    
    # Example: vstfs:///Git/Commit/abc123-def-456/repo-id-789/commit-sha
    match = re.search(r"Git/Commit/([^/]+)/([^/]+)/([a-f0-9]+)", decoded_url, re.IGNORECASE)
    if match:
        return match.group(1), match.group(2), match.group(3)
    return None


def fetch_commit_details(
    org_url: str,
    project: str,
    repo_id: str,
    commit_id: str,
    pat: str
) -> Optional[Dict[str, Any]]:
    """Fetch commit details including changed files."""
    # Get commit with changes
    url = f"{org_url}/{project}/_apis/git/repositories/{repo_id}/commits/{commit_id}?changeCount=100&api-version=7.1"
    
    try:
        resp = requests.get(url, headers=_get_auth_header(pat), timeout=30)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.debug("Failed to fetch commit %s: %s", commit_id, e)
        return None


def fetch_commit_changes(
    org_url: str,
    project: str,
    repo_id: str,
    commit_id: str,
    pat: str
) -> List[Dict[str, Any]]:
    """Fetch detailed file changes for a commit."""
    url = f"{org_url}/{project}/_apis/git/repositories/{repo_id}/commits/{commit_id}/changes?api-version=7.1"
    
    try:
        resp = requests.get(url, headers=_get_auth_header(pat), timeout=30)
        resp.raise_for_status()
        data = resp.json()
        return data.get("changes", [])
    except Exception as e:
        logger.debug("Failed to fetch commit changes %s: %s", commit_id, e)
        return []


def analyze_commits_from_ado(
    org_url: str,
    project: str,
    pat: str,
    days: int = 90,
    max_wi: int = 500
) -> Dict[str, Any]:
    """
    Main function: analyze commits linked to work items in ADO.
    
    Returns a dict with:
    - by_author: {author_email: {commits, loc_added, loc_removed, languages, wi_refs, files}}
    - by_wi: {wi_id: [commit_info, ...]}
    - repos_analyzed: list of repo IDs
    """
    logger.info("Querying recent work items from ADO (last %d days)...", days)
    wi_ids = query_recent_work_items(org_url, project, pat, days=days, max_items=max_wi)
    logger.info("Found %d work items to analyze", len(wi_ids))
    
    if not wi_ids:
        return {"by_author": {}, "by_wi": {}, "repos_analyzed": []}
    
    # Fetch work items with relations
    logger.info("Fetching work item relations...")
    work_items = fetch_work_items_batch(org_url, project, pat, wi_ids)
    
    # Extract commit links
    commit_urls_by_wi: Dict[int, List[str]] = {}
    for wi in work_items:
        wid = wi.get("id")
        links = extract_commit_links(wi)
        if links:
            commit_urls_by_wi[wid] = links
    
    logger.info("Found %d WIs with commit links", len(commit_urls_by_wi))
    
    # Count total commits to process for progress reporting
    total_commit_urls = sum(len(urls) for urls in commit_urls_by_wi.values())
    logger.info("Processing up to %d commit URLs...", total_commit_urls)
    print(f"Processing commits from {len(commit_urls_by_wi)} work items (up to {total_commit_urls} commits)...")
    
    # Fetch and analyze commits
    by_author = defaultdict(lambda: {
        "commits": 0,
        "loc_added": 0,
        "loc_removed": 0,
        "languages": Counter(),
        "files": Counter(),
        "wi_refs": Counter(),
    })
    by_wi = defaultdict(list)
    repos_seen = set()
    commits_seen = set()
    processed_count = 0
    
    for wid, urls in commit_urls_by_wi.items():
        for url in urls:
            parsed = parse_vstfs_commit_url(url)
            if not parsed:
                continue
            
            proj_id, repo_id, commit_id = parsed
            if commit_id in commits_seen:
                continue
            commits_seen.add(commit_id)
            repos_seen.add(repo_id)
            
            # Progress output every 10 commits
            processed_count += 1
            if processed_count % 10 == 0:
                print(f"  Processed {processed_count} commits...")
            
            # Fetch commit details
            commit = fetch_commit_details(org_url, project, repo_id, commit_id, pat)
            if not commit:
                continue
            
            author_email = commit.get("author", {}).get("email", "unknown")
            author_name = commit.get("author", {}).get("name", author_email)
            author_key = author_email or author_name
            
            by_author[author_key]["commits"] += 1
            by_author[author_key]["wi_refs"][str(wid)] += 1
            
            # Fetch file changes to compute LOC and languages
            changes = fetch_commit_changes(org_url, project, repo_id, commit_id, pat)
            for change in changes:
                item = change.get("item", {})
                path = item.get("path", "")
                change_type = change.get("changeType", "")
                
                if not path:
                    continue
                
                ext = Path(path).suffix.lower()
                lang = EXT_LANG_MAP.get(ext, ext.lstrip('.') or 'other')
                
                by_author[author_key]["files"][path] += 1
                by_author[author_key]["languages"][lang] += 1
                
                # Estimate LOC from change type (ADO doesn't give numstat directly)
                # This is a rough estimate
                if change_type in ("add", "edit"):
                    by_author[author_key]["loc_added"] += 50  # rough estimate
                elif change_type == "delete":
                    by_author[author_key]["loc_removed"] += 50
            
            by_wi[str(wid)].append({
                "commit_id": commit_id,
                "author": author_key,
                "date": commit.get("author", {}).get("date", ""),
                "message": commit.get("comment", "")[:200],
                "repo_id": repo_id,
            })
    
    # Convert Counters to serializable dicts
    result_by_author = {}
    for author, data in by_author.items():
        result_by_author[author] = {
            "commits": data["commits"],
            "loc_added": data["loc_added"],
            "loc_removed": data["loc_removed"],
            "languages": dict(data["languages"].most_common(20)),
            "top_files": data["files"].most_common(20),
            "wi_refs": dict(data["wi_refs"].most_common(50)),
        }
    
    return {
        "by_author": result_by_author,
        "by_wi": dict(by_wi),
        "repos_analyzed": list(repos_seen),
        "total_commits": len(commits_seen),
        "analysis_date": datetime.now(timezone.utc).isoformat(),
    }


def build_tech_stack_by_developer(analysis: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Extract tech stack summary per developer from analysis results."""
    tech_stack = {}
    for author, data in analysis.get("by_author", {}).items():
        langs = data.get("languages", {})
        tech_stack[author] = {
            "primary_languages": list(langs.keys())[:5],
            "all_languages": langs,
            "total_commits": data.get("commits", 0),
            "loc_added": data.get("loc_added", 0),
            "wi_count": len(data.get("wi_refs", {})),
        }
    return tech_stack


def save_analysis_outputs(analysis: Dict[str, Any], out_dir: Path = Path("outputs")) -> Tuple[Path, Path]:
    """Save analysis results to JSON and CSV files."""
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    
    # Full analysis JSON
    json_path = out_dir / f"ado_commit_analysis_{ts}.json"
    with json_path.open("w", encoding="utf-8") as fh:
        json.dump(analysis, fh, indent=2)
    
    # Tech stack summary CSV
    csv_path = out_dir / f"tech_stack_by_dev_{ts}.csv"
    tech_stack = build_tech_stack_by_developer(analysis)
    with csv_path.open("w", encoding="utf-8") as fh:
        fh.write("Developer,Primary Languages,Total Commits,LOC Added,WI Count\n")
        for dev, data in tech_stack.items():
            langs = "|".join(data.get("primary_languages", []))
            fh.write(f"{dev},{langs},{data.get('total_commits', 0)},{data.get('loc_added', 0)},{data.get('wi_count', 0)}\n")
    
    logger.info("Saved analysis to %s and %s", json_path, csv_path)
    return json_path, csv_path
