#!/usr/bin/env python3
"""Query the developer knowledge base.

Uses the vector DB (Chroma or JSON fallback) to find:
- Developers with expertise in a functionality
- Functionalities a developer knows
- Best match for a task/question

Usage:
  python3 scripts/query_knowledge.py "who knows Live+ best?"
  python3 scripts/query_knowledge.py --developer "john.doe@example.com"
  python3 scripts/query_knowledge.py --functionality "Reporting"
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Setup
REPO_ROOT = Path(__file__).resolve().parent.parent
os.chdir(REPO_ROOT)
sys.path.insert(0, str(REPO_ROOT))


def parse_args():
    p = argparse.ArgumentParser(description="Query developer knowledge base.")
    p.add_argument("query", nargs="*", help="Free-text query")
    p.add_argument("--developer", "-d", help="Search for a specific developer")
    p.add_argument("--functionality", "-f", help="Search for a specific functionality")
    p.add_argument("--top-k", "-k", type=int, default=5, help="Number of results")
    p.add_argument("--list-all", action="store_true", help="List all developers")
    return p.parse_args()


def main():
    args = parse_args()
    
    from utilities.vector_db import (
        query_developer_skills,
        query_functionality,
        get_all_developers,
    )
    
    if args.list_all:
        print("\n=== All Developers in Knowledge Base ===\n")
        devs = get_all_developers()
        if not devs:
            print("No developers found. Run scripts/build_knowledge_base.py first.")
            return 0
        
        for dev in devs:
            meta = dev.get("metadata", {})
            print(f"  - {meta.get('developer', dev.get('id', 'Unknown'))}")
            print(f"    Languages: {meta.get('languages', 'N/A')}")
            print(f"    Commits: {meta.get('commits', 'N/A')}")
            print()
        return 0
    
    if args.developer:
        print(f"\n=== Searching for developer: {args.developer} ===\n")
        results = query_developer_skills(args.developer, top_k=args.top_k)
        
        if not results:
            print("No matching developers found.")
            return 0
        
        for r in results:
            meta = r.get("metadata", {})
            sim = r.get("similarity", r.get("distance", "N/A"))
            print(f"Developer: {meta.get('developer', r.get('id'))}")
            print(f"  Score: {sim}")
            print(f"  Languages: {meta.get('languages', 'N/A')}")
            print(f"  Commits: {meta.get('commits', 'N/A')}")
            print(f"  WI Count: {meta.get('wi_count', 'N/A')}")
            print()
        return 0
    
    if args.functionality:
        print(f"\n=== Searching for functionality: {args.functionality} ===\n")
        results = query_functionality(args.functionality, top_k=args.top_k)
        
        if not results:
            print("No matching functionalities found.")
            return 0
        
        for r in results:
            meta = r.get("metadata", {})
            sim = r.get("similarity", r.get("distance", "N/A"))
            print(f"Functionality: {meta.get('functionality', r.get('id'))}")
            print(f"  Score: {sim}")
            print(f"  Developers: {meta.get('developers', 'N/A')}")
            print(f"  Total touches: {meta.get('total_touches', 'N/A')}")
            print()
        return 0
    
    # Free-text query
    query = " ".join(args.query) if args.query else ""
    if not query:
        print("Please provide a query. Use --help for usage.")
        return 1
    
    print(f"\n=== Query: {query} ===\n")
    
    # Search both collections
    print("--- Developer Skills ---")
    skill_results = query_developer_skills(query, top_k=args.top_k)
    
    if skill_results:
        for r in skill_results:
            meta = r.get("metadata", {})
            sim = r.get("similarity", r.get("distance", "N/A"))
            print(f"  {meta.get('developer', r.get('id'))}: score={sim}")
            print(f"    Languages: {meta.get('languages', 'N/A')[:50]}...")
    else:
        print("  No matching developers found.")
    
    print("\n--- Functionalities ---")
    func_results = query_functionality(query, top_k=args.top_k)
    
    if func_results:
        for r in func_results:
            meta = r.get("metadata", {})
            sim = r.get("similarity", r.get("distance", "N/A"))
            print(f"  {meta.get('functionality', r.get('id'))}: score={sim}")
            print(f"    Developers: {meta.get('developers', 'N/A')[:50]}...")
    else:
        print("  No matching functionalities found.")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
