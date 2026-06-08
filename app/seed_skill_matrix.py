#!/usr/bin/env python3
"""Seed `data/skill_matrix.json` from an iteration CSV.

Usage:
  python3 scripts/seed_skill_matrix.py <csv_file>

This will create `data/skill_matrix.json` with:
 - skills inferred from area paths and common title tokens
 - members found in the CSV with empty skill maps (you can populate them later)

This is conservative: it will not assign proficiency levels automatically.
"""
import sys
from pathlib import Path
import csv
import re
from collections import Counter

from utilities.skill_matrix import save, _ensure_schema

STOPWORDS = set(["the","and","for","with","from","that","this","into","using","use","new","add","feature","user","record","screen","page"])


def candidate_skills_from_title(title: str):
    title = (title or "").lower()
    # extract words longer than 3 chars and not stopwords
    tokens = re.findall(r"[a-zA-Z]{4,}", title)
    return [t for t in tokens if t not in STOPWORDS]


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    p = Path(argv[1])
    if not p.exists():
        print("file not found", p)
        return 2

    area_terms = Counter()
    title_terms = Counter()
    members = Counter()

    with p.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for r in reader:
            area = r.get("Area Path") or r.get("AreaPath") or ""
            # take last segment after backslash
            last = area.split('\\')[-1].strip() if area else None
            if last:
                area_terms[last] += 1
            title = r.get("Title") or ""
            for tok in candidate_skills_from_title(title):
                title_terms[tok] += 1
            owner = r.get("Assigned To") or r.get("AssignedTo") or r.get("Assigned") or ""
            if owner:
                members[owner] += 1

    # choose top candidates
    skills = {}
    for k, _ in area_terms.most_common(50):
        skills[k] = {"description": "inferred from area path"}
    # add top title tokens
    for k, _ in title_terms.most_common(50):
        if k not in skills:
            skills[k] = {"description": "inferred from title tokens"}

    members_map = {m: {} for m in members.keys()}

    data = {"skills": skills, "members": members_map}
    save(data)
    print("Wrote skill matrix to data/skill_matrix.json with %d skills and %d members" % (len(skills), len(members_map)))
    return 0


if __name__ == '__main__':
    raise SystemExit(main(sys.argv))
