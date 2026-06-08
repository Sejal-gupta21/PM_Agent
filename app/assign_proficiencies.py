#!/usr/bin/env python3
"""Assign proficiencies to members based on historical activity in CSV(s).

Usage:
  python3 scripts/assign_proficiencies.py [--force] <csv1> [csv2 ...]

By default this will NOT overwrite existing skill levels in `data/skill_matrix.json`.
Set `--force` to overwrite.

Heuristic thresholds (counts of matched tasks):
  >= 20 -> expert
  >= 10 -> senior
  >= 4  -> intermediate
  >= 1  -> junior
"""
import sys
from pathlib import Path
import json
from collections import defaultdict

from utilities.skill_matrix import load, save, match_task_to_skills, list_members

LEVELS = [
    (20, "expert"),
    (10, "senior"),
    (4, "intermediate"),
    (1, "junior"),
]


def decide_level(count: int) -> str | None:
    for thresh, lvl in LEVELS:
        if count >= thresh:
            return lvl
    return None


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    force = False
    args = argv[1:]
    if args[0] == "--force":
        force = True
        args = args[1:]
    if not args:
        print("No input CSVs provided.")
        return 2

    skills_data = load()
    skills = set(skills_data.get("skills", {}).keys())
    members_map = skills_data.get("members", {})

    # counters: member -> skill -> count
    counts = defaultdict(lambda: defaultdict(int))

    for csvp in args:
        p = Path(csvp)
        if not p.exists():
            print("CSV not found:", csvp)
            continue
        import csv
        with p.open("r", encoding="utf-8", newline="") as fh:
            reader = csv.DictReader(fh)
            for r in reader:
                owner = (r.get("Assigned To") or r.get("AssignedTo") or "").strip()
                if not owner:
                    continue
                # find matched skills using the skill list
                matched = []
                title = (r.get("Title") or "").lower()
                area = (r.get("Area Path") or "").lower()
                for s in skills:
                    sk = s.lower()
                    if sk in title or sk in area:
                        matched.append(s)
                for s in matched:
                    counts[owner][s] += 1

    # apply decisions
    changed = 0
    for member, skill_counts in counts.items():
        # ensure member is present in members_map
        if member not in members_map:
            members_map[member] = {}
        for skill, cnt in skill_counts.items():
            lvl = decide_level(cnt)
            if not lvl:
                continue
            existing = members_map[member].get(skill)
            if existing and not force:
                # skip
                continue
            members_map[member][skill] = lvl
            changed += 1

    skills_data["members"] = members_map
    save(skills_data)
    print(f"Wrote skill matrix ({changed} assignments applied). Saved to data/skill_matrix.json")
    # print sample of changed assignments (first 30)
    out = []
    for m, skills in members_map.items():
        if skills:
            out.append({"member": m, "skills": {k: v for k, v in skills.items()}})
    print(json.dumps(out[:30], indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
