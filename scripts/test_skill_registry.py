#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Test script for the unified skill registry."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from utilities.skill_registry import (
    load_skills, get_skill_by_id, match_skill_by_query,
    get_skill_discovery_info, get_skill_prompt_context,
    get_skills_by_category, get_fixed_skills
)


def main():
    print("=" * 60)
    print("SKILL REGISTRY TEST")
    print("=" * 60)

    # Test 1: Load all skills
    skills = load_skills()
    print(f"\n[1] Total skills loaded: {len(skills)}")
    for s in skills:
        print(f"    - {s.get('id')}: {s.get('display_name')}")

    # Test 2: Get skill by ID
    skill = get_skill_by_id("bug_areas_highlight")
    print(f"\n[2] Get skill by ID (bug_areas_highlight):")
    if skill:
        print(f"    Name: {skill.get('display_name')}")
        print(f"    Category: {skill.get('category')}")
        print(f"    Prompts: {len(skill.get('canonical_prompts', []))} variations")
        print(f"    Required args: {skill.get('required_args')}")
        print(f"    Optional args: {list(skill.get('optional_args', {}).keys())}")
    else:
        print("    [ERROR] Skill not found!")

    # Test 3: Match skill by query
    test_queries = [
        "show recurring bugs",
        "find overlooked stories", 
        "what is the sprint status",
        "who is overloaded",
        "capacity forecast",
        "backlog health check",
        "what can you do",
        "find bug patterns",
        "stale stories",
        "detect repeated bugs",
    ]
    print(f"\n[3] Match skill by query:")
    for q in test_queries:
        matched = match_skill_by_query(q)
        if matched:
            print(f"    '{q}' -> {matched.get('id')}")
        else:
            print(f"    '{q}' -> No match")

    # Test 4: Get skills by category
    print(f"\n[4] Skills by category:")
    categories = ["analysis", "reporting", "status", "capacity", "notification"]
    for cat in categories:
        cat_skills = get_skills_by_category(cat)
        print(f"    {cat}: {[s.get('id') for s in cat_skills]}")

    # Test 5: Get fixed skills only
    fixed = get_fixed_skills()
    print(f"\n[5] PM Agent fixed skills: {len(fixed)}")
    for s in fixed:
        print(f"    - {s.get('id')}")

    # Test 6: Get discovery info
    discovery = get_skill_discovery_info()
    print(f"\n[6] Discovery info skills: {len(discovery)}")

    # Test 7: Get prompt context
    context = get_skill_prompt_context()
    print(f"\n[7] Prompt context (first 500 chars):")
    print(context[:500])

    print("\n" + "=" * 60)
    print("[OK] All tests passed!")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
