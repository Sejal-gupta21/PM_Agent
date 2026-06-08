#!/usr/bin/env python3
"""Export skill matrix into a readable table CSV grouped by categories.

Columns: Member, Domain/Product, Technical, Release/Ops/QA, Soft/Process, Other
Each skills cell contains semicolon-separated entries of the form `skill (level)`.
"""
from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, List

from utilities.skill_matrix import load

OUT_DIR = Path("outputs")
OUT_DIR.mkdir(exist_ok=True)

TECHNICAL = {
    "Python",
    "TypeScript",
    "React",
    "Streamlit",
    "SQL",
    "Azure DevOps",
    "Azure",
    "Docker",
    "CI/CD",
    "REST APIs",
    "Microservices",
    "Authentication",
    "Testing",
    "Performance",
    "Database Design",
    "Logging & Monitoring",
    "Release Engineering",
    "Frontend",
    "Backend",
    "Data Processing",
}

RELEASE_OPS_QA = {
    "CI/CD",
    "Docker",
    "Release Engineering",
    "Logging & Monitoring",
    "Testing",
}

SOFT_PROCESS = {
    # none explicitly present initially, keep set for later
    "Scrum",
    "Estimation",
    "Mentoring",
    "PO Support",
}

DOMAIN_TOKENS = ["fracpro", "xops", "global", "fp", "fracpro suite", "wtt"]


def categorize_skill(name: str) -> str:
    n = name.strip()
    if n in TECHNICAL:
        return "technical"
    if n in RELEASE_OPS_QA:
        return "release_ops_qa"
    if n in SOFT_PROCESS:
        return "soft_process"
    low = n.lower()
    for t in DOMAIN_TOKENS:
        if t in low:
            return "domain"
    # heuristic: single-word lower case tokens that came from title may be domain/other
    return "other"


def format_skill_list(skills: Dict[str, str], category: str) -> str:
    items = [f"{k} ({v})" for k, v in skills.items() if categorize_skill(k) == category]
    return "; ".join(sorted(items))


def build_table(data: Dict[str, Any]) -> List[Dict[str, str]]:
    rows = []
    members = data.get("members", {})
    for member, skills in members.items():
        # skills: mapping skill->level
        domain = format_skill_list(skills, "domain")
        technical = format_skill_list(skills, "technical")
        release_ops = format_skill_list(skills, "release_ops_qa")
        soft = format_skill_list(skills, "soft_process")
        other = format_skill_list(skills, "other")
        rows.append({
            "Member": member,
            "Domain/Product": domain,
            "Technical": technical,
            "Release/Ops/QA": release_ops,
            "Soft/Process": soft,
            "Other": other,
        })
    return rows


def write_csv(rows: List[Dict[str, str]], out_path: Path) -> None:
    import csv
    with out_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["Member", "Domain/Product", "Technical", "Release/Ops/QA", "Soft/Process", "Other"])
        writer.writeheader()
        for r in rows:
            writer.writerow(r)


def main():
    data = load()
    rows = build_table(data)
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    out_path = OUT_DIR / f"skill_matrix_table_{ts}.csv"
    write_csv(rows, out_path)
    print(f"Wrote {out_path} ({len(rows)} members)")
    # print preview
    import itertools, csv, sys
    with out_path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.reader(fh)
        for i, row in enumerate(reader):
            print(",".join(row))
            if i >= 15:
                break


if __name__ == '__main__':
    raise SystemExit(main())
