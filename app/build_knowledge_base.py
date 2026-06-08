#!/usr/bin/env python3
"""Build developer knowledge base from ADO commits and store in vector DB.

This script:
1. Runs ADO commit analysis (or loads existing)
2. Builds functionality documents from commit data
3. Creates developer skill profiles
4. Stores everything in vector DB (Chroma or JSON fallback)

Usage:
  python3 scripts/build_knowledge_base.py [--days N] [--force]

Options:
  --days N    Analyze commits from last N days (default: 90)
  --force     Re-analyze commits even if recent analysis exists
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

# Setup paths
REPO_ROOT = Path(__file__).resolve().parent.parent
os.chdir(REPO_ROOT)
sys.path.insert(0, str(REPO_ROOT))

from config import config

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

DATA_DIR = REPO_ROOT / "data"
OUTPUTS_DIR = REPO_ROOT / "outputs"


def parse_args():
    p = argparse.ArgumentParser(description="Build developer knowledge base.")
    p.add_argument("--days", type=int, default=90, help="Analyze commits from last N days")
    p.add_argument("--force", action="store_true", help="Force re-analysis")
    p.add_argument("--skip-commits", action="store_true", help="Skip commit analysis, use existing data")
    return p.parse_args()


def find_latest_analysis() -> Path | None:
    """Find the most recent ADO commit analysis JSON."""
    analyses = sorted(OUTPUTS_DIR.glob("ado_commit_analysis_*.json"), reverse=True)
    return analyses[0] if analyses else None


def load_analysis(path: Path) -> Dict[str, Any]:
    """Load analysis JSON."""
    return json.loads(path.read_text())


def build_developer_skill_docs(analysis: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Build developer skill documents for vector DB."""
    docs = []
    
    for author, data in analysis.get("by_author", {}).items():
        # Build text representation for embedding
        langs = list(data.get("languages", {}).keys())
        wi_refs = list(data.get("wi_refs", {}).keys())
        top_files = [f[0] for f in data.get("top_files", [])[:10]]
        
        text_parts = [
            f"Developer: {author}",
            f"Primary languages: {', '.join(langs[:5]) if langs else 'Unknown'}",
            f"All languages: {', '.join(langs)}",
            f"Total commits: {data.get('commits', 0)}",
            f"Lines of code added: {data.get('loc_added', 0)}",
            f"Work items touched: {len(wi_refs)}",
            f"Top files: {', '.join(top_files)}",
        ]
        
        doc = {
            "id": f"dev_{author}",
            "type": "developer_skill",
            "developer": author,
            "text": "\n".join(text_parts),
            "languages": langs,
            "commits": data.get("commits", 0),
            "loc_added": data.get("loc_added", 0),
            "wi_count": len(wi_refs),
            "top_files": top_files,
        }
        docs.append(doc)
    
    return docs


def build_functionality_docs(analysis: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Build functionality documents from WI-commit mappings."""
    docs = []
    
    # Group by functionality inferred from file paths
    from utilities.functionality_mapper import heuristic_label_for_path
    
    functionality_map: Dict[str, Dict[str, Any]] = {}
    
    for author, data in analysis.get("by_author", {}).items():
        for path, count in data.get("top_files", []):
            label = heuristic_label_for_path(path)
            
            if label not in functionality_map:
                functionality_map[label] = {
                    "files": [],
                    "developers": {},
                    "total_touches": 0,
                }
            
            functionality_map[label]["files"].append(path)
            functionality_map[label]["developers"][author] = (
                functionality_map[label]["developers"].get(author, 0) + count
            )
            functionality_map[label]["total_touches"] += count
    
    for label, data in functionality_map.items():
        # Deduplicate files
        unique_files = list(set(data["files"]))[:20]
        
        # Sort developers by contribution
        devs_sorted = sorted(data["developers"].items(), key=lambda x: x[1], reverse=True)
        
        text_parts = [
            f"Functionality: {label}",
            f"Total file touches: {data['total_touches']}",
            f"Files: {', '.join(unique_files[:10])}",
            f"Top developers: {', '.join(f'{d[0]}({d[1]})' for d in devs_sorted[:5])}",
        ]
        
        doc = {
            "id": f"func_{label}",
            "type": "functionality",
            "functionality": label,
            "text": "\n".join(text_parts),
            "files": unique_files,
            "developers": dict(devs_sorted[:10]),
            "total_touches": data["total_touches"],
        }
        docs.append(doc)
    
    return docs


def main():
    args = parse_args()
    
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    
    # Step 1: Get or run commit analysis
    analysis = None
    
    if not args.skip_commits:
        existing = find_latest_analysis()
        
        if existing and not args.force:
            # Check if analysis is recent enough (within 24 hours)
            try:
                ts_str = existing.stem.split("_")[-1]
                ts = datetime.strptime(ts_str, "%Y%m%dT%H%M%SZ")
                age_hours = (datetime.utcnow() - ts).total_seconds() / 3600
                
                if age_hours < 24:
                    logger.info("Using existing analysis from %s (%.1f hours old)", existing, age_hours)
                    analysis = load_analysis(existing)
            except Exception as e:
                logger.warning("Failed to parse existing analysis timestamp: %s", e)
        
        if analysis is None:
            logger.info("Running new ADO commit analysis...")
            from utilities.ado_commit_analyzer import analyze_commits_from_ado, save_analysis_outputs
            
            org_url = config.ado_org_url
            project = config.ado_project
            pat = config.ado_pat
            
            if not org_url or not pat:
                logger.error("ADO_ORG_URL and ADO_PAT must be set")
                return 1
            
            analysis = analyze_commits_from_ado(
                org_url=org_url,
                project=project,
                pat=pat,
                days=args.days,
                max_wi=500,
            )
            
            save_analysis_outputs(analysis)
    else:
        existing = find_latest_analysis()
        if existing:
            logger.info("Loading existing analysis from %s", existing)
            analysis = load_analysis(existing)
        else:
            logger.error("No existing analysis found and --skip-commits specified")
            return 1
    
    if not analysis or not analysis.get("by_author"):
        logger.warning("No commit data found to build knowledge base")
        return 0
    
    # Step 2: Build documents
    logger.info("Building developer skill documents...")
    skill_docs = build_developer_skill_docs(analysis)
    logger.info("Built %d developer skill docs", len(skill_docs))
    
    logger.info("Building functionality documents...")
    func_docs = build_functionality_docs(analysis)
    logger.info("Built %d functionality docs", len(func_docs))
    
    # Step 3: Store in vector DB
    from utilities.vector_db import add_developer_skills, add_functionality_docs
    
    logger.info("Storing in vector database...")
    skills_added = add_developer_skills(skill_docs)
    funcs_added = add_functionality_docs(func_docs)
    
    logger.info("Added %d skill docs and %d functionality docs to vector DB", skills_added, funcs_added)
    
    # Step 4: Save summaries to JSON for reference
    summary = {
        "built_at": datetime.now(timezone.utc).isoformat(),
        "developers": len(skill_docs),
        "functionalities": len(func_docs),
        "source_analysis": str(find_latest_analysis()),
    }
    
    summary_path = DATA_DIR / "knowledge_base_summary.json"
    summary_path.write_text(json.dumps(summary, indent=2))
    
    # Also save the docs as JSON
    (DATA_DIR / "developer_skills.json").write_text(json.dumps(skill_docs, indent=2))
    (DATA_DIR / "functionality_docs.json").write_text(json.dumps(func_docs, indent=2))
    
    print()
    print("=" * 60)
    print("Knowledge Base Built Successfully")
    print("=" * 60)
    print(f"Developers:      {len(skill_docs)}")
    print(f"Functionalities: {len(func_docs)}")
    print()
    print("Files created:")
    print(f"  - {summary_path}")
    print(f"  - {DATA_DIR / 'developer_skills.json'}")
    print(f"  - {DATA_DIR / 'functionality_docs.json'}")
    print(f"  - Vector DB in {DATA_DIR / 'vector_db'}")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
