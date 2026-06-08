#!/usr/bin/env python3
"""Profile upcoming work items for sprint planning.

Fetches work items that are:
- State = "Ready"
- Not in any iteration/sprint
- Match configured area paths

Tags each with skills, area/module, and complexity.

Usage:
    python scripts/profile_upcoming_tasks.py --area-paths "FracPro-OPS/App/Live+"
    python scripts/profile_upcoming_tasks.py --max-wis 100 --use-llm
    python scripts/profile_upcoming_tasks.py --output outputs/upcoming_test.json
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

from config import config

import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
logger = logging.getLogger("pm_agent.profile_upcoming_tasks")

OUTPUTS_DIR = REPO_ROOT / "outputs"
DATA_DIR = REPO_ROOT / "data"


def load_area_paths_from_config() -> list:
    """Load area paths from config.yaml if available."""
    cfg_path = REPO_ROOT / "config.yaml"
    if not cfg_path.exists():
        return []
    try:
        with cfg_path.open("r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        return cfg.get("upcomingAreaPaths", [])
    except Exception:
        return []


def parse_args():
    parser = argparse.ArgumentParser(
        description="Profile upcoming work items for sprint planning"
    )
    parser.add_argument(
        "--area-paths",
        type=str,
        default="",
        help="Comma-separated list of ADO area paths to scan (e.g., 'FracPro-OPS/App/Live+,FracPro-OPS/App/Reporting')"
    )
    parser.add_argument(
        "--max-wis",
        type=int,
        default=500,
        help="Maximum number of work items to fetch (default: 500)"
    )
    parser.add_argument(
        "--use-llm",
        action="store_true",
        help="Use LLM for enhanced skill inference (requires OPENAI_API_KEY)"
    )
    parser.add_argument(
        "--output",
        type=str,
        default="",
        help="Output file path (default: outputs/upcoming_tasks_<timestamp>.json)"
    )
    return parser.parse_args()


def main():
    args = parse_args()
    
    # Determine area paths
    if args.area_paths:
        area_paths = [p.strip() for p in args.area_paths.split(",") if p.strip()]
    else:
        area_paths = load_area_paths_from_config()
    
    if not area_paths:
        # Default to project root if nothing specified
        project = config.ado_project
        area_paths = [project]
        logger.warning("No area paths specified, using project root: %s", project)
    
    logger.info("Area paths: %s", area_paths)
    logger.info("Max WIs: %d", args.max_wis)
    logger.info("Use LLM: %s", args.use_llm)
    
    # Import profiler
    try:
        from utilities.task_profiler import profile_upcoming_tasks, WI_TAGS_FILE
    except ImportError as e:
        logger.error("Failed to import task_profiler: %s", e)
        return 1
    
    # Run profiling
    try:
        profiled = profile_upcoming_tasks(
            area_paths=area_paths,
            max_wis=args.max_wis,
            use_llm=args.use_llm,
        )
    except Exception as e:
        logger.exception("Failed to profile tasks: %s", e)
        return 2
    
    if not profiled:
        logger.warning("No upcoming tasks found matching criteria")
        print("\nNo upcoming tasks found matching:")
        print(f"  - State: Ready")
        print(f"  - Not in any sprint")
        print(f"  - Area paths: {area_paths}")
        return 0
    
    # Determine output path
    if args.output:
        output_path = Path(args.output)
    else:
        OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        output_path = OUTPUTS_DIR / f"upcoming_tasks_{timestamp}.json"
    
    # Write output
    output_data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "area_paths": area_paths,
        "total_count": len(profiled),
        "use_llm": args.use_llm,
        "items": profiled,
    }
    
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump(output_data, f, indent=2)
    
    logger.info("Output written to: %s", output_path)
    logger.info("Tags persisted to: %s", WI_TAGS_FILE)
    
    # Print summary
    print("\n" + "="*60)
    print("UPCOMING TASKS PROFILE SUMMARY")
    print("="*60)
    print(f"Total tasks profiled: {len(profiled)}")
    print(f"Area paths scanned: {area_paths}")
    print(f"Output file: {output_path}")
    print(f"Tags file: {WI_TAGS_FILE}")
    print("-"*60)
    
    # Complexity breakdown
    complexity_counts = {}
    for item in profiled:
        c = item.get("complexity", "Unknown")
        complexity_counts[c] = complexity_counts.get(c, 0) + 1
    
    print("\nComplexity breakdown:")
    for c, count in sorted(complexity_counts.items()):
        print(f"  {c}: {count}")
    
    # Top skills
    skill_counts = {}
    for item in profiled:
        for skill_info in item.get("inferred_skills", []):
            skill = skill_info.get("skill", "unknown")
            skill_counts[skill] = skill_counts.get(skill, 0) + 1
    
    print("\nTop skills detected:")
    for skill, count in sorted(skill_counts.items(), key=lambda x: -x[1])[:10]:
        print(f"  {skill}: {count}")
    
    # Sample items
    print("\nSample profiled tasks (first 5):")
    for item in profiled[:5]:
        skills_str = ", ".join(s["skill"] for s in item.get("inferred_skills", [])[:3])
        print(f"  [{item.get('id')}] {item.get('title', '')[:50]}...")
        print(f"       Complexity: {item.get('complexity')} | Skills: {skills_str or 'none detected'}")
    
    print("="*60 + "\n")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
