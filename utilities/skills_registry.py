"""Skill matrix utilities

Provides simple storage and manipulation for skills, proficiency levels,
and mapping skills to team members. Data stored as JSON to avoid touching
existing DBs or configs.
"""
from __future__ import annotations

import json
from typing import Dict, List, Any
from pathlib import Path

SKILL_LEVELS = ["novice", "junior", "intermediate", "senior", "expert"]

DEFAULT_PATH = Path(__file__).resolve().parent.parent / "data" / "skill_matrix.json"
DEFAULT_PATH.parent.mkdir(parents=True, exist_ok=True)


def _ensure_schema(d: Dict[str, Any]) -> Dict[str, Any]:
    # Basic normalization/compat for older files
    return {
        "skills": d.get("skills", {}),
        "members": d.get("members", {}),
    }


def load(path: str | Path = DEFAULT_PATH) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        # return empty skeleton
        return _ensure_schema({})
    with p.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    return _ensure_schema(data)


def save(data: Dict[str, Any], path: str | Path = DEFAULT_PATH) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as fh:
        json.dump(_ensure_schema(data), fh, indent=2, ensure_ascii=False)


def define_skill(name: str, description: str = "") -> None:
    d = load()
    d["skills"][name] = {"description": description}
    save(d)


def set_member_skills(member_email: str, skills: Dict[str, str]) -> None:
    """Set skills for a member.

    skills: mapping skill_name -> level (one of SKILL_LEVELS)
    """
    d = load()
    # validate
    for k, v in skills.items():
        if v not in SKILL_LEVELS:
            raise ValueError(f"Invalid level {v} for skill {k}; valid: {SKILL_LEVELS}")
    d["members"][member_email] = skills
    save(d)


def get_member_skills(member_email: str) -> Dict[str, str]:
    d = load()
    return d.get("members", {}).get(member_email, {})


def list_skills() -> Dict[str, Any]:
    d = load()
    return d.get("skills", {})


def list_members() -> Dict[str, Dict[str, str]]:
    d = load()
    return d.get("members", {})


def match_task_to_skills(task: Dict[str, Any]) -> List[str]:
    """Naive heuristic: match by area path and title keywords to skills defined.

    Returns a list of skill names (may be empty).
    """
    skills = list(list_skills().keys())
    if not skills:
        return []
    title = (task.get("Title") or "").lower()
    area = (task.get("Area Path") or "").lower()
    matches = set()
    for s in skills:
        sk = s.lower()
        if sk in title or sk in area:
            matches.add(s)
    return sorted(matches)
