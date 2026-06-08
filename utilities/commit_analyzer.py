"""Commit analyzer utilities

Provides functions to scan git history, summarize per-author activity,
extract languages used (by file extension), and map commits to work item
IDs when they appear in commit messages (e.g., "WI#12345" or "#12345").

This is intentionally offline and non-invasive.
"""
from __future__ import annotations

import subprocess
import re
from collections import defaultdict, Counter
from typing import List, Dict, Any, Tuple
from pathlib import Path
import json
import datetime

WI_REGEX = re.compile(r"\b(?:WI#|WI:|#)(\d{2,7})\b", flags=re.IGNORECASE)

EXT_LANG_MAP = {
    '.py': 'Python', '.ts': 'TypeScript', '.js': 'JavaScript', '.jsx': 'JavaScript',
    '.tsx': 'TypeScript', '.java': 'Java', '.go': 'Go', '.rs': 'Rust', '.rb': 'Ruby',
    '.php': 'PHP', '.html': 'HTML', '.css': 'CSS', '.scss': 'CSS', '.yml': 'YAML',
    '.yaml': 'YAML', '.sql': 'SQL', '.json': 'JSON', '.md': 'Markdown'
}


def _run_git(args: List[str], repo: Path) -> str:
    p = subprocess.run(["git"] + args, cwd=str(repo), capture_output=True, text=True, check=True)
    return p.stdout


def parse_git_log(repo: Path, max_commits: int | None = None) -> List[Dict[str, Any]]:
    """Parse git log with numstat to extract per-commit details.

    Returns a list of commits with keys: hash, author, date, message, files=[(added,removed,path)], raw_patch
    """
    args = ["log", "--pretty=format:%H%x1f%an%x1f%ad%x1f%s", "--numstat", "--date=iso"]
    if max_commits:
        args.insert(1, f"-n{max_commits}")
    out = _run_git(args, repo)
    commits = []
    cur = None
    for line in out.splitlines():
        if '\x1f' in line:
            # header
            parts = line.split('\x1f')
            if len(parts) >= 4:
                if cur:
                    commits.append(cur)
                cur = {
                    'hash': parts[0],
                    'author': parts[1],
                    'date': parts[2],
                    'message': parts[3],
                    'files': []
                }
        elif line.strip() == '':
            continue
        else:
            # numstat line: added\tremoved\tpath
            toks = line.split('\t')
            if len(toks) == 3 and cur is not None:
                added, removed, path = toks
                try:
                    a = int(added) if added != '-' else 0
                    r = int(removed) if removed != '-' else 0
                except Exception:
                    a, r = 0, 0
                cur['files'].append((a, r, path))
    if cur:
        commits.append(cur)
    return commits


def summarize_commits_by_author(commits: List[Dict[str, Any]]) -> Dict[str, Any]:
    authors = defaultdict(lambda: {'commits': 0, 'loc_added': 0, 'loc_removed': 0, 'files': Counter(), 'langs': Counter(), 'wi_refs': Counter()})
    for c in commits:
        author = c.get('author') or 'Unknown'
        authors[author]['commits'] += 1
        for a, r, path in c.get('files', []):
            authors[author]['loc_added'] += a
            authors[author]['loc_removed'] += r
            authors[author]['files'][path] += 1
            ext = Path(path).suffix.lower()
            lang = EXT_LANG_MAP.get(ext) or ext.lstrip('.') or 'other'
            authors[author]['langs'][lang] += 1
        # WI references
        msg = c.get('message') or ''
        for m in WI_REGEX.findall(msg):
            authors[author]['wi_refs'][m] += 1
    # convert counters to dicts
    out = {}
    for a, v in authors.items():
        out[a] = {
            'commits': v['commits'],
            'loc_added': v['loc_added'],
            'loc_removed': v['loc_removed'],
            'top_files': v['files'].most_common(10),
            'languages': v['langs'].most_common(),
            'wi_refs': v['wi_refs'].most_common()
        }
    return out


def map_commits_to_wi(commits: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Return mapping wi_id -> list of commit dicts referencing it (by message)."""
    mapping = defaultdict(list)
    for c in commits:
        msg = c.get('message') or ''
        for m in WI_REGEX.findall(msg):
            mapping[m].append(c)
    return mapping


def write_summary_outputs(repo: Path, commits: List[Dict[str, Any]], out_dir: Path = Path('outputs')) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')
    summary_path = out_dir / f'commit_summary_{ts}.json'
    wi_map_path = out_dir / f'commit_wi_map_{ts}.json'
    by_author = summarize_commits_by_author(commits)
    wi_map = map_commits_to_wi(commits)
    with summary_path.open('w', encoding='utf-8') as fh:
        json.dump(by_author, fh, indent=2)
    with wi_map_path.open('w', encoding='utf-8') as fh:
        json.dump({k: [{'hash': c['hash'], 'author': c['author'], 'message': c['message'], 'files': c['files']} for c in v] for k, v in wi_map.items()}, fh, indent=2)
    return summary_path
