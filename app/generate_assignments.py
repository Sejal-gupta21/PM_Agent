#!/usr/bin/env python3
"""Generate assignment suggestions for upcoming work items.

Reads profiled tasks and generates 2-3 developer suggestions per WI based on:
- Skill match (WI tags vs developer tech profile)
- Historical familiarity (which developer worked on which functionality)

Does NOT modify ADO - all assignments are local suggestions only.

Usage:
    python scripts/generate_assignments.py --input outputs/upcoming_tasks.json
    python scripts/generate_assignments.py --top-k 3
    python scripts/generate_assignments.py --output outputs/custom_assignments.json
"""
import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Allow running from repo root
REPO_ROOT = Path(__file__).resolve().parent.parent
os.chdir(REPO_ROOT)
sys.path.insert(0, str(REPO_ROOT))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("pm_agent.generate_assignments")

OUTPUTS_DIR = REPO_ROOT / "outputs"
DATA_DIR = REPO_ROOT / "data"


def parse_args():
    parser = argparse.ArgumentParser(
        description="Generate assignment suggestions for upcoming work items"
    )
    parser.add_argument(
        "--input", "-i",
        type=str,
        default=None,
        help="Path to profiled tasks JSON file. If not provided, reads from data/wi_tags.json"
    )
    parser.add_argument(
        "--output", "-o",
        type=str,
        default=None,
        help="Output path for suggestions JSON. Default: outputs/assignment_suggestions_<ts>.json"
    )
    parser.add_argument(
        "--top-k", "-k",
        type=int,
        default=3,
        help="Number of top suggestions per work item (default: 3)"
    )
    parser.add_argument(
        "--min-score",
        type=float,
        default=0.1,
        help="Minimum score threshold for suggestions (default: 0.1)"
    )
    return parser.parse_args()


def main():
    args = parse_args()
    
    from utilities.assignment import (
        run_assignment_pipeline,
        load_developer_skills,
        load_commit_summary,
        build_developer_profiles,
    )
    
    # Determine input
    input_path = None
    if args.input:
        input_path = Path(args.input)
        if not input_path.exists():
            logger.error("Input file not found: %s", input_path)
            return 1
    
    # Determine output
    output_path = None
    if args.output:
        output_path = Path(args.output)
    
    logger.info("=" * 60)
    logger.info("GENERATING ASSIGNMENT SUGGESTIONS")
    logger.info("=" * 60)
    logger.info("Input: %s", input_path or "data/wi_tags.json")
    logger.info("Output: %s", output_path or "outputs/assignment_suggestions_<ts>.json")
    logger.info("Top K: %d", args.top_k)
    logger.info("Min Score: %.2f", args.min_score)
    
    # Show developer info
    dev_skills = load_developer_skills()
    commit_summary = load_commit_summary()
    profiles = build_developer_profiles(dev_skills, commit_summary)
    
    logger.info("-" * 60)
    logger.info("Developer Profiles Loaded: %d", len(profiles))
    for email, profile in list(profiles.items())[:5]:
        skills_str = ", ".join(profile.get("skills", [])[:5])
        modules = list(profile.get("modules", {}).keys())[:3]
        logger.info("  %s: skills=[%s], modules=%s", email, skills_str, modules)
    if len(profiles) > 5:
        logger.info("  ... and %d more developers", len(profiles) - 5)
    
    # Run pipeline
    suggestions = run_assignment_pipeline(
        input_path=input_path,
        output_path=output_path,
        top_k=args.top_k,
    )
    
    if not suggestions:
        logger.warning("No suggestions generated")
        return 1
    
    # Print summary
    print("\n" + "=" * 60)
    print("ASSIGNMENT SUGGESTIONS SUMMARY")
    print("=" * 60)
    print(f"Total WIs processed: {len(suggestions)}")
    
    # Count suggestions with at least one match
    with_matches = sum(1 for s in suggestions if s.get("suggestions"))
    print(f"WIs with suggestions: {with_matches}")
    
    # Print sample
    print("\n" + "-" * 60)
    print("SAMPLE SUGGESTIONS (first 5)")
    print("-" * 60)
    
    for item in suggestions[:5]:
        wi_id = item.get("wi_id")
        title = item.get("title", "")[:50]
        print(f"\n[{wi_id}] {title}...")
        
        for i, sug in enumerate(item.get("suggestions", [])[:3], 1):
            dev = sug.get("developer", "")
            score = sug.get("score", 0)
            breakdown = sug.get("breakdown", {})
            skill_matches = breakdown.get("skill_matches", [])
            module_matches = breakdown.get("module_matches", [])
            
            print(f"  {i}. {dev}")
            print(f"     Score: {score:.2f} (skill: {breakdown.get('skill_match', 0):.2f}, familiarity: {breakdown.get('familiarity', 0):.2f})")
            if skill_matches:
                print(f"     Skills: {', '.join(skill_matches)}")
            if module_matches:
                print(f"     Modules: {', '.join(module_matches)}")
    
    print("\n" + "=" * 60)
    print("OUTPUTS")
    print("=" * 60)
    print(f"Suggestions file: outputs/assignment_suggestions_*.json")
    print(f"Persistent file: data/assignment_suggestions.json")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
