#!/usr/bin/env python3
"""CLI to run commit analysis from Azure DevOps work item links.

This fetches commit data from ADO (not local git) to determine:
- Tech stack knowledge per developer
- Languages and file types worked on
- Work items linked to each developer

Usage:
  python3 scripts/analyze_commits.py [--days N] [--max-wi N] [--local]

Options:
  --days N     Analyze WIs changed in last N days (default: 90)
  --max-wi N   Max work items to query (default: 500)
  --local      Use local git log instead of ADO (for testing)

Outputs written to `outputs/ado_commit_analysis_*.json` and `outputs/tech_stack_by_dev_*.csv`.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from config import config

# Add repo root to path
REPO_ROOT = Path(__file__).resolve().parent.parent
os.chdir(REPO_ROOT)
sys.path.insert(0, str(REPO_ROOT))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def parse_args():
    p = argparse.ArgumentParser(description="Analyze commits from ADO work item links.")
    p.add_argument("--days", type=int, default=90, help="Analyze WIs changed in last N days")
    p.add_argument("--max-wi", type=int, default=500, help="Max work items to query")
    p.add_argument("--local", action="store_true", help="Use local git log instead of ADO")
    return p.parse_args()


def main():
    args = parse_args()
    
    if args.local:
        # Use local git log (original behavior)
        from utilities.commit_analyzer import parse_git_log, write_summary_outputs
        commits = parse_git_log(REPO_ROOT, max_commits=args.max_wi)
        out = write_summary_outputs(REPO_ROOT, commits)
        print("Wrote local commit summary to", out)
        return 0
    
    # Use ADO commit analyzer
    from utilities.ado_commit_analyzer import analyze_commits_from_ado, save_analysis_outputs
    
    org_url = config.ado_org_url
    project = config.ado_project
    pat = config.ado_pat
    
    if not org_url or not pat:
        logger.error("ADO_ORG_URL and ADO_PAT must be set in environment or .env")
        return 1
    
    logger.info("Analyzing commits from ADO project: %s", project)
    logger.info("Looking at WIs changed in last %d days (max %d)", args.days, args.max_wi)
    
    analysis = analyze_commits_from_ado(
        org_url=org_url,
        project=project,
        pat=pat,
        days=args.days,
        max_wi=args.max_wi,
    )
    
    json_path, csv_path = save_analysis_outputs(analysis)
    
    print()
    print("=" * 60)
    print("ADO Commit Analysis Complete")
    print("=" * 60)
    print(f"Repos analyzed:   {len(analysis.get('repos_analyzed', []))}")
    print(f"Total commits:    {analysis.get('total_commits', 0)}")
    print(f"Developers found: {len(analysis.get('by_author', {}))}")
    print()
    print(f"Output files:")
    print(f"  - {json_path}")
    print(f"  - {csv_path}")
    
    # Print summary
    print()
    print("Developer Tech Stack Summary:")
    print("-" * 50)
    for author, data in list(analysis.get("by_author", {}).items())[:10]:
        langs = list(data.get("languages", {}).keys())[:5]
        print(f"  {author}: {', '.join(langs) or 'N/A'} ({data.get('commits', 0)} commits)")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
