"""Functionality mapper

Heuristically map repository files to high-level functionality labels and
aggregate per-developer familiarity using commit evidence. Optionally, when
OPENAI_API_KEY is set, call the OpenAI API to produce a short human-friendly
label for file contents and to create embeddings (not implemented by default).
"""
from __future__ import annotations

import json
from pathlib import Path
from collections import Counter, defaultdict
from typing import Dict, List, Any

REPO_ROOT = Path(__file__).resolve().parents[1]

# simple path token -> functionality label map
PATH_LABELS = {
    'app': 'UI / App',
    'agents/host_agent': 'Host Agent',
    'agents/post_design_agent': 'Post Design Agent',
    'utilities/mcp': 'MCP / ADO integration',
    'utilities/scheduler': 'Scheduler',
    'utilities/emailer.py': 'Email / Notifications',
    'scripts': 'Automation scripts',
    'mcp_server': 'MCP Server',
    'src/mcp': 'MCP / Connectors',
}

KEYWORD_LABELS = {
    'live+': 'Live+ feature',
    'minifrac': 'Minifrac Analysis',
    'proppant': 'Proppants',
    'bol': 'BOL / Delivery',
    'schedule': 'Scheduling / Deployments',
    'report': 'Reporting',
    'streamlit': 'UI / Streamlit',
}


def heuristic_label_for_path(path: str) -> str:
    p = path.replace('\\', '/')
    # exact mappings
    for token, label in PATH_LABELS.items():
        if token in p:
            return label
    # fallback: top directory
    parts = p.split('/')
    if parts:
        return parts[0].capitalize()
    return 'Unknown'


def keyword_label_for_content(content: str) -> List[str]:
    s = content.lower()
    labels = []
    for k, lab in KEYWORD_LABELS.items():
        if k in s:
            labels.append(lab)
    return labels


def build_file_documents(commit_summary: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Create a document per file containing path, candidate labels and evidence.

    commit_summary: output of commit_analyzer.summarize_commits_by_author
    """
    docs: Dict[str, Dict[str, Any]] = {}
    # walk top files across authors
    for author, info in commit_summary.items():
        for path, cnt in info.get('top_files', []) + []:
            p = Path(path)
            if not p.exists():
                # try repo-relative
                rp = REPO_ROOT / path
                if rp.exists():
                    p = rp
            label = heuristic_label_for_path(str(path))
            content_labels = []
            try:
                if p.exists() and p.is_file():
                    txt = p.read_text(encoding='utf-8', errors='ignore')
                    content_labels = keyword_label_for_content(txt)
            except Exception:
                txt = ''
            doc = docs.setdefault(path, {
                'path': path,
                'heuristic_label': label,
                'content_labels': Counter(),
                'authors': Counter(),
                'evidence': [],
            })
            doc['content_labels'].update(content_labels)
            doc['authors'][author] += cnt
            doc['evidence'].append({'author': author, 'count': cnt})
    return docs


def aggregate_dev_functionality(docs: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Aggregate per-developer counts per functionality label.

    Returns mapping author -> {functionality: {'files':count, 'evidence':[...]}}
    """
    dev_map: Dict[str, Dict[str, Any]] = {}
    for path, doc in docs.items():
        # determine final labels (heuristic + content)
        labels = [doc.get('heuristic_label')] if doc.get('heuristic_label') else []
        # include top content labels
        for lab, c in doc.get('content_labels', {}).items():
            labels.append(lab)
        labels = list(dict.fromkeys([l for l in labels if l]))
        for author, cnt in doc.get('authors', {}).items():
            am = dev_map.setdefault(author, defaultdict(lambda: {'files': 0, 'evidence': []}))
            for lab in labels:
                am[lab]['files'] += cnt
                am[lab]['evidence'].append({'path': path, 'count': cnt})
    # convert defaultdicts to normal dicts
    return {a: dict(map(lambda kv: (kv[0], dict(kv[1])), v.items())) for a, v in dev_map.items()}


def map_wi_to_functionality(commit_wi_map_path: Path, docs: Dict[str, Any]) -> Dict[str, Any]:
    """Map WIs to functionality by inspecting commits that referenced them and files in those commits.

    commit_wi_map_path: path to JSON produced by commit analyzer
    docs: docs mapping path -> doc
    """
    if not commit_wi_map_path.exists():
        return {}
    with commit_wi_map_path.open('r', encoding='utf-8') as fh:
        wi_map = json.load(fh)
    wi_func = {}
    for wi, commits in wi_map.items():
        label_counts = Counter()
        evidence = []
        for c in commits:
            for a, r, path in c.get('files', []):
                doc = docs.get(path)
                if doc:
                    lab = doc.get('heuristic_label')
                    label_counts[lab] += 1
                    evidence.append({'path': path, 'author': c.get('author'), 'lab': lab})
                else:
                    lab = heuristic_label_for_path(path)
                    label_counts[lab] += 1
                    evidence.append({'path': path, 'author': c.get('author'), 'lab': lab})
        if label_counts:
            primary = label_counts.most_common(1)[0][0]
        else:
            primary = 'Unknown'
        wi_func[wi] = {'functionality': primary, 'label_counts': label_counts.most_common(), 'evidence': evidence}
    return wi_func

