#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Build knowledge base from ADO commit history.
This script analyzes commits and builds developer skill profiles.

Usage:
    python scripts/build_knowledge_base.py --days 30
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
from utilities.langfuse_client import trace_task


@trace_task("build_knowledge_base", metadata={"source": "pm_agent"})
def main():
    parser = argparse.ArgumentParser(description="Build knowledge base from ADO commits")
    parser.add_argument("--days", type=int, default=30, help="Number of days to analyze")
    parser.add_argument("--max-wi", type=int, default=200, help="Maximum work items to analyze")
    args = parser.parse_args()

    print(f"Building knowledge base from last {args.days} days of commits...")
    
    # Create data directory if it doesn't exist
    data_dir = ROOT / "data"
    outputs_dir = ROOT / "outputs"
    data_dir.mkdir(parents=True, exist_ok=True)
    outputs_dir.mkdir(parents=True, exist_ok=True)
    
    # Get ADO credentials from config
    org_url = config.ado_org_url
    project = config.ado_project
    pat = config.ado_pat
    
    if not org_url or not project or not pat:
        print("Error: ADO credentials not configured (ado.org_url, ado.project, ado.pat)")
        print("Please set these in config.yaml")
        return 1
    
    print(f"Connecting to ADO org: {org_url}, project: {project}")
    
    # Use ADO commit analyzer
    try:
        from utilities.ado_commit_analyzer import (
            analyze_commits_from_ado,
            build_tech_stack_by_developer,
            save_analysis_outputs
        )
        
        # Run analysis
        print(f"Analyzing commits from last {args.days} days (max {args.max_wi} work items)...")
        analysis = analyze_commits_from_ado(
            org_url=org_url,
            project=project,
            pat=pat,
            days=args.days,
            max_wi=args.max_wi
        )
        
        # Save full analysis to outputs folder
        save_analysis_outputs(analysis, outputs_dir)
        
        # Build developer skills from analysis
        tech_stack = build_tech_stack_by_developer(analysis)
        
        # Convert to list format expected by assignment.py
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
        
        # Save developer skills
        skills_file = data_dir / "developer_skills.json"
        with skills_file.open("w", encoding="utf-8") as f:
            json.dump(skills_list, f, indent=2)
        print(f"Saved {len(skills_list)} developer skill profiles to {skills_file}")
        
        # Build functionality docs from file paths
        funcs_list = []
        all_files = set()
        for author_data in analysis.get("by_author", {}).values():
            for file_path, count in author_data.get("top_files", []):
                if file_path not in all_files:
                    all_files.add(file_path)
                    funcs_list.append({
                        "path": file_path,
                        "touch_count": count,
                    })
        
        # Save functionality docs
        funcs_file = data_dir / "functionality_docs.json"
        with funcs_file.open("w", encoding="utf-8") as f:
            json.dump(funcs_list, f, indent=2)
        print(f"Saved {len(funcs_list)} functionality documents to {funcs_file}")
        
        # Print summary
        print("\n=== Knowledge Base Summary ===")
        print(f"Total commits analyzed: {analysis.get('total_commits', 0)}")
        print(f"Developers found: {len(skills_list)}")
        print(f"Repositories covered: {len(analysis.get('repos_analyzed', []))}")
        
        if skills_list:
            print("\nDeveloper profiles created:")
            for dev in skills_list[:10]:
                langs = ", ".join(dev.get("languages", [])[:3])
                print(f"  - {dev['developer']}: {dev['commits']} commits, langs: {langs}")
            if len(skills_list) > 10:
                print(f"  ... and {len(skills_list) - 10} more")
        
    except ImportError as e:
        print(f"Warning: ado_commit_analyzer module not available: {e}")
        print("Creating empty knowledge base files...")
        
        # Create empty files
        (data_dir / "developer_skills.json").write_text("[]")
        (data_dir / "functionality_docs.json").write_text("[]")
        return {"success": False, "error": "ado_commit_analyzer module not available"}
    except Exception as e:
        print(f"Error during analysis: {e}")
        import traceback
        traceback.print_exc()
        return {"success": False, "error": str(e)}
    
    print("\nKnowledge base build complete.")
    return {"success": True, "developers": len(skills_list), "total_commits": analysis.get('total_commits', 0)}


if __name__ == "__main__":
    result = main()
    # Return 0 for success, 1 for failure (for shell exit codes)
    sys.exit(0 if (isinstance(result, dict) and result.get("success")) else 1)
