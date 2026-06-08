#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Analyze commits from Azure DevOps repositories.
This script fetches commit history and analyzes developer contributions.

Usage:
    python scripts/analyze_commits.py --days 30 --max-wi 100
"""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Load configuration
from config import config


def main():
    parser = argparse.ArgumentParser(description="Analyze commits from ADO")
    parser.add_argument("--days", type=int, default=30, help="Number of days to analyze")
    parser.add_argument("--max-wi", type=int, default=100, help="Maximum work items to analyze")
    args = parser.parse_args()

    print(f"Analyzing commits from last {args.days} days (max {args.max_wi} work items)...")
    
    # Get ADO credentials from config
    org_url = config.ado_org_url
    project = config.ado_project
    pat = config.ado_pat
    
    if not org_url or not project or not pat:
        print("Error: ADO credentials not configured (ado.org_url, ado.project, ado.pat)")
        print("Please set these in config.yaml")
        return 1
    
    try:
        from utilities.ado_commit_analyzer import (
            analyze_commits_from_ado,
            build_tech_stack_by_developer,
            save_analysis_outputs
        )
        
        # Run analysis
        print(f"Connecting to ADO: {org_url}/{project}")
        analysis = analyze_commits_from_ado(
            org_url=org_url,
            project=project,
            pat=pat,
            days=args.days,
            max_wi=args.max_wi
        )
        
        # Save outputs
        outputs_dir = ROOT / "outputs"
        outputs_dir.mkdir(parents=True, exist_ok=True)
        json_path, csv_path = save_analysis_outputs(analysis, outputs_dir)
        
        # Print summary
        print(f"\n=== Commit Analysis Summary ===")
        print(f"Total commits analyzed: {analysis.get('total_commits', 0)}")
        print(f"Developers found: {len(analysis.get('by_author', {}))}")
        print(f"Work items covered: {len(analysis.get('by_wi', {}))}")
        print(f"Repositories analyzed: {len(analysis.get('repos_analyzed', []))}")
        print(f"\nResults saved to:")
        print(f"  - {json_path}")
        print(f"  - {csv_path}")
        
        # Also update developer_skills.json for assignment pipeline
        data_dir = ROOT / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        
        tech_stack = build_tech_stack_by_developer(analysis)
        skills_list = []
        for developer, stack in tech_stack.items():
            skills_list.append({
                "developer": developer,
                "languages": stack.get("primary_languages", []),
                "all_languages": stack.get("all_languages", {}),
                "commits": stack.get("total_commits", 0),
                "loc_added": stack.get("loc_added", 0),
                "wi_count": stack.get("wi_count", 0),
                "top_files": analysis.get("by_author", {}).get(developer, {}).get("top_files", []),
            })
        
        skills_file = data_dir / "developer_skills.json"
        with skills_file.open("w", encoding="utf-8") as f:
            json.dump(skills_list, f, indent=2)
        print(f"\nDeveloper skills updated: {skills_file}")
        
    except ImportError as e:
        print(f"Error: ado_commit_analyzer module not available: {e}")
        print("Commit analysis requires the ADO connector to be configured.")
        return 1
    except Exception as e:
        print(f"Error during analysis: {e}")
        import traceback
        traceback.print_exc()
        return 1
    
    print("\nCommit analysis complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
