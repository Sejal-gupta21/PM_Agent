#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Assign Backlog Work Items to Underutilized Developers.

This script:
1. Fetches Ready backlog items from ADO (XOPS Bugs Enhancement) not in any sprint
2. Loads developer skills and current capacity from the latest sprint plan
3. Identifies underutilized developers (<70% capacity used)
4. Assigns backlog items based on:
   - Developer role (FE/BE) matched to task type
   - Commit evidence strength (LOC, languages, file patterns)
   - Available capacity (least utilized first)
5. Generates a separate "Backlog Assignments" sheet

Usage:
    python scripts/assign_backlog_to_underutilized.py --sprint "Sprint 2024-12-23"
"""
import argparse
import base64
import csv
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import yaml
from config import config

# =============================================================================
# CONFIGURATION - Load from config.yaml
# =============================================================================

def load_config() -> dict:
    """Load configuration from config.yaml."""
    config_file = ROOT / "config.yaml"
    if config_file.exists():
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            print(f"Warning: Could not load config.yaml: {e}")
    return {}


CONFIG = load_config()
SPRINT_CONFIG = CONFIG.get("sprint_planning", {})

# Excluded developers from config
EXCLUDED_DEVELOPERS = [d.lower() for d in SPRINT_CONFIG.get("excluded_developers", [])]

# Capacity settings from config
CAPACITY_CONFIG = SPRINT_CONFIG.get("capacity", {})
DEFAULT_SPRINT_DAYS = 10
DEFAULT_HOURS_PER_DAY = CAPACITY_CONFIG.get("hours_per_day", 8)
DEFAULT_UTILIZATION_TARGET = CAPACITY_CONFIG.get("utilization_target", 0.80)

# Underutilization threshold - developers below this are considered underutilized
# We now use 100% threshold since new sprint means full capacity is available
UNDERUTILIZATION_THRESHOLD = 1.0  # Any developer with capacity can receive work

# Cross-role assignment settings from config
CROSS_ROLE_CONFIG = SPRINT_CONFIG.get("cross_role_assignment", {})
ALLOW_CROSS_ROLE_ASSIGNMENT = CROSS_ROLE_CONFIG.get("enabled", False)
CROSS_ROLE_EVIDENCE_THRESHOLD = CROSS_ROLE_CONFIG.get("evidence_threshold", 0.30)

# Complexity to hours mapping
COMPLEXITY_HOURS = {
    "Small": 8,      # 1 day
    "Medium": 16,    # 2 days
    "Large": 32,     # 4 days
    "XLarge": 48,    # 6 days
}

# Backlog area paths
BACKLOG_AREA_PATHS = [
    "FracPro-OPS\\Global Management\\WTT Development\\XOPS Bugs Enhancement",
]

# FE/BE classification patterns
FE_SKILLS = {"angular", "react", "vue", "typescript", "javascript", "html", "css", "scss", "frontend", "ui", "ionic"}
BE_SKILLS = {"java", "python", "csharp", "spring", "backend", "api", "sql", "database", "node", "express", "gradle", "maven"}

FE_PATH_PATTERNS = ["ui", "frontend", "component", "angular", "react", "vue", "src/app", "pages", "views"]
BE_PATH_PATTERNS = ["api", "backend", "server", "service", "controller", "repository", "dao", "model"]

FE_TASK_INDICATORS = ["ui", "frontend", "screen", "component", "angular", "display", "form", "dialog", "modal", "button", "view", "page", "layout", "style"]
BE_TASK_INDICATORS = ["api", "backend", "service", "database", "query", "calculation", "integration", "sync", "endpoint", "server", "email", "notification"]

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def _get_ado_headers(pat: str) -> Dict[str, str]:
    """Build authorization headers for ADO REST API."""
    auth = base64.b64encode(f":{pat}".encode()).decode()
    return {
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/json",
    }


def classify_developer_role(dev_data: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """
    Classify a developer as frontend, backend, or fullstack based on commit evidence.
    Returns: (role, evidence_dict)
    """
    languages = dev_data.get("languages", [])
    all_languages = dev_data.get("all_languages", {})
    top_files = dev_data.get("top_files", [])
    
    fe_score = 0
    be_score = 0
    fe_langs = []
    be_langs = []
    fe_paths = []
    be_paths = []
    
    # Score from languages
    for lang in languages:
        lang_lower = lang.lower()
        count = all_languages.get(lang, 1)
        if lang_lower in FE_SKILLS:
            fe_score += count
            fe_langs.append(lang)
        elif lang_lower in BE_SKILLS:
            be_score += count
            be_langs.append(lang)
    
    # Score from file paths
    for file_info in top_files:
        if isinstance(file_info, (list, tuple)) and len(file_info) >= 2:
            path, count = file_info[0], file_info[1]
        else:
            path = str(file_info)
            count = 1
        
        path_lower = path.lower()
        
        if any(p in path_lower for p in FE_PATH_PATTERNS):
            fe_score += count
            if len(fe_paths) < 3:
                fe_paths.append(path)
        
        if any(p in path_lower for p in BE_PATH_PATTERNS):
            be_score += count
            if len(be_paths) < 3:
                be_paths.append(path)
    
    total = fe_score + be_score
    evidence = {
        "fe_score": round(fe_score / total, 2) if total > 0 else 0,
        "be_score": round(be_score / total, 2) if total > 0 else 0,
        "fe_languages": fe_langs[:3],
        "be_languages": be_langs[:3],
        "fe_paths": fe_paths[:3],
        "be_paths": be_paths[:3],
        "commits": dev_data.get("commits", 0),
        "loc_added": dev_data.get("loc_added", 0),
    }
    
    # Classification
    if total == 0:
        return "unknown", evidence
    
    fe_ratio = fe_score / total
    be_ratio = be_score / total
    
    if fe_ratio >= 0.70:
        return "frontend", evidence
    elif be_ratio >= 0.70:
        return "backend", evidence
    elif fe_ratio > 0.4 and be_ratio > 0.4:
        return "fullstack", evidence
    elif fe_ratio > be_ratio:
        return "frontend", evidence
    else:
        return "backend", evidence


def classify_task_type(title: str, tags: str = "", area_path: str = "") -> str:
    """Classify a task as FE, BE, or BOTH based on title and tags."""
    combined = f"{title} {tags} {area_path}".lower()
    
    has_fe = any(ind in combined for ind in FE_TASK_INDICATORS)
    has_be = any(ind in combined for ind in BE_TASK_INDICATORS)
    
    if has_fe and not has_be:
        return "frontend"
    elif has_be and not has_fe:
        return "backend"
    elif has_fe and has_be:
        return "both"
    else:
        # Default to backend for bugs/enhancements
        return "backend"


def get_developer_full_name(email: str) -> str:
    """Convert email to full name format like 'Vijay Kumar'."""
    if not email:
        return ""
    
    name_part = email.split("@")[0]
    
    # Handle GitHub-style usernames
    if "+" in name_part:
        name_part = name_part.split("+")[1]
    
    name_part = name_part.replace(".", " ").replace("_", " ").replace("-", " ")
    name_part = ''.join(c for c in name_part if not c.isdigit())
    
    words = [w.strip().title() for w in name_part.split() if w.strip()]
    return " ".join(words) if words else email.split("@")[0]


def estimate_complexity(title: str, tags: str = "") -> str:
    """Estimate task complexity from title and tags."""
    combined = f"{title} {tags}".lower()
    
    # Large/XLarge indicators
    if any(x in combined for x in ["module", "integration", "major", "redesign", "architecture"]):
        return "Large"
    
    # Small indicators
    if any(x in combined for x in ["fix", "typo", "minor", "update", "small", "dropdown", "button"]):
        return "Small"
    
    # Default to Medium
    return "Medium"


# =============================================================================
# TASK BREAKDOWN FUNCTIONS (LLM-based)
# =============================================================================

def clean_html(text: str) -> str:
    """Remove HTML tags and decode entities from ADO description."""
    import re
    from html import unescape
    
    if not text:
        return ""
    text = re.sub(r'<[^>]+>', ' ', text)
    text = unescape(text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def break_down_work_item_simple(task: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Simple fallback breakdown when LLM is not available or fails.
    Creates generic Frontend/Backend subtasks based on task type.
    """
    task_type = task.get("task_type", "backend")
    complexity = task.get("complexity", "Medium")
    total_hours = COMPLEXITY_HOURS.get(complexity, 16)
    
    subtasks = []
    
    if task_type == "frontend":
        subtasks.append({
            **task,
            "subtask_index": 1,
            "subtask_type": f"UI Implementation - {task.get('title', '')[:40]}",
            "subtask_hours": total_hours,
            "is_subtask": True,
            "parent_wi_id": task.get("id"),
            "subtask_role": "frontend",
            "llm_generated": False,
        })
    elif task_type == "backend":
        subtasks.append({
            **task,
            "subtask_index": 1,
            "subtask_type": f"API/Service Implementation - {task.get('title', '')[:40]}",
            "subtask_hours": total_hours,
            "is_subtask": True,
            "parent_wi_id": task.get("id"),
            "subtask_role": "backend",
            "llm_generated": False,
        })
    else:  # both
        half_hours = total_hours * 0.5
        subtasks.append({
            **task,
            "subtask_index": 1,
            "subtask_type": f"Frontend - {task.get('title', '')[:40]}",
            "subtask_hours": half_hours,
            "is_subtask": True,
            "parent_wi_id": task.get("id"),
            "subtask_role": "frontend",
            "llm_generated": False,
        })
        subtasks.append({
            **task,
            "subtask_index": 2,
            "subtask_type": f"Backend - {task.get('title', '')[:40]}",
            "subtask_hours": half_hours,
            "is_subtask": True,
            "parent_wi_id": task.get("id"),
            "subtask_role": "backend",
            "llm_generated": False,
        })
    
    return subtasks


def break_down_work_item_with_llm(task: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Use LLM to intelligently break down a work item into subtasks based on its description.
    
    This reads the ADO description field and uses OpenAI to:
    1. Analyze the work item content
    2. Identify logical subtasks
    3. Classify each subtask as Frontend/Backend
    4. Estimate hours for each subtask
    
    Returns:
        List of subtask dictionaries with intelligent breakdown
    """
    title = task.get("title", "")
    description = task.get("description", "") or ""
    acceptance_criteria = task.get("acceptance_criteria", "") or ""
    complexity = task.get("complexity", "Medium")
    total_hours = COMPLEXITY_HOURS.get(complexity, 16)
    
    clean_desc = clean_html(description)
    clean_ac = clean_html(acceptance_criteria)
    
    # If no description, fall back to simple breakdown
    if not clean_desc and not clean_ac:
        print(f"  [LLM] No description for WI-{task.get('id')}, using simple breakdown")
        return break_down_work_item_simple(task)
    
    try:
        from openai import OpenAI
        client = OpenAI(api_key=config.openai_api_key)
        
        prompt = f"""Analyze this work item and break it down into 2-5 logical subtasks.

Work Item ID: {task.get('id')}
Title: {title}
Description: {clean_desc[:1500] if clean_desc else 'Not provided'}
Acceptance Criteria: {clean_ac[:500] if clean_ac else 'Not provided'}
Complexity: {complexity}
Total Estimated Hours: {total_hours}

For each subtask, provide:
1. A clear, actionable task name (not just "Implementation" - be specific to the work)
2. The type: "Frontend" (UI/Angular/HTML/CSS work) or "Backend" (API/Service/Database/C#/Java work)
3. Estimated hours (distribute {total_hours} hours across subtasks)

Return a JSON array with this exact structure:
[
  {{"name": "Create API endpoint for XYZ", "type": "Backend", "hours": 8}},
  {{"name": "Build Angular component for ABC", "type": "Frontend", "hours": 6}}
]

RULES:
- Subtask names must be specific to the work item content
- Identify what needs UI work vs what needs API/service work
- Hours must sum to approximately {total_hours}
- Return ONLY valid JSON array, no markdown or explanations"""

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a technical project manager who breaks down software development work items into specific, actionable subtasks. Return only valid JSON."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3,
            max_tokens=500
        )
        
        result_text = response.choices[0].message.content.strip()
        
        # Clean up response - remove markdown code blocks if present
        if result_text.startswith("```json"):
            result_text = result_text[7:]
        if result_text.startswith("```"):
            result_text = result_text[3:]
        if result_text.endswith("```"):
            result_text = result_text[:-3]
        result_text = result_text.strip()
        
        llm_subtasks = json.loads(result_text)
        
        if not llm_subtasks or not isinstance(llm_subtasks, list):
            print(f"  [LLM] Invalid response for WI-{task.get('id')}, using simple breakdown")
            return break_down_work_item_simple(task)
        
        # Build subtask list from LLM response
        subtasks = []
        for i, llm_task in enumerate(llm_subtasks):
            subtask = task.copy()
            subtask["subtask_index"] = i + 1
            subtask["subtask_type"] = llm_task.get("name", f"Task {i+1}")
            subtask["subtask_hours"] = float(llm_task.get("hours", total_hours / len(llm_subtasks)))
            subtask["is_subtask"] = True
            subtask["parent_wi_id"] = task.get("id")
            # Store FE/BE classification for developer assignment
            task_type = llm_task.get("type", "Backend").lower()
            subtask["subtask_role"] = "frontend" if "front" in task_type else "backend"
            subtask["llm_generated"] = True
            subtasks.append(subtask)
        
        print(f"  [LLM] WI-{task.get('id')}: Broke down into {len(subtasks)} subtasks")
        for st in subtasks:
            print(f"       - {st['subtask_type']} ({st['subtask_role']}, {st['subtask_hours']}h)")
        
        return subtasks
        
    except Exception as e:
        print(f"  [LLM] Error breaking down WI-{task.get('id')}: {e}")
        return break_down_work_item_simple(task)


def break_down_work_item(task: Dict[str, Any], use_llm: bool = True) -> List[Dict[str, Any]]:
    """
    Break down a work item into subtasks.
    
    Args:
        task: Work item dictionary
        use_llm: If True, use LLM for intelligent breakdown; otherwise use simple breakdown
    
    Returns:
        List of subtask dictionaries
    """
    if use_llm:
        return break_down_work_item_with_llm(task)
    else:
        return break_down_work_item_simple(task)


def fetch_backlog_items() -> List[Dict[str, Any]]:
    """Fetch Ready backlog items from ADO that are not in any sprint."""
    org_url = config.ado_org_url
    project = config.ado_project
    pat = config.ado_pat
    
    if not pat:
        print("[ERROR] ADO_PAT not found in config")
        return []
    
    headers = _get_ado_headers(pat)
    
    # Build WIQL query
    area_conditions = " OR ".join([f"[System.AreaPath] UNDER '{ap}'" for ap in BACKLOG_AREA_PATHS])
    
    wiql = f"""
    SELECT [System.Id]
    FROM WorkItems
    WHERE [System.TeamProject] = '{project}'
      AND [System.State] = 'Ready'
      AND ([System.IterationPath] = '{project}' OR [System.IterationPath] = '')
      AND ({area_conditions})
    ORDER BY [Microsoft.VSTS.Common.Priority] ASC, [Microsoft.VSTS.Common.StackRank] ASC
    """
    
    wiql_url = f"{org_url}/{project}/_apis/wit/wiql?api-version=7.0"
    
    try:
        resp = requests.post(wiql_url, headers=headers, json={"query": wiql}, timeout=60)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"[ERROR] Failed to query backlog: {e}")
        return []
    
    wi_ids = [item["id"] for item in data.get("workItems", [])]
    
    if not wi_ids:
        print("[INFO] No Ready backlog items found")
        return []
    
    # Fetch details
    fields = [
        "System.Id", "System.Title", "System.State", "System.AreaPath",
        "System.IterationPath", "System.WorkItemType", "System.Tags",
        "System.AssignedTo", "Microsoft.VSTS.Common.Priority",
        "Microsoft.VSTS.Common.StackRank", "Microsoft.VSTS.Scheduling.StoryPoints",
        "System.Description", "Microsoft.VSTS.Common.AcceptanceCriteria"
    ]
    
    batch_url = f"{org_url}/_apis/wit/workitemsbatch?api-version=7.0"
    payload = {"ids": wi_ids, "fields": fields}
    
    try:
        resp = requests.post(batch_url, headers=headers, json=payload, timeout=60)
        resp.raise_for_status()
        work_items = resp.json().get("value", [])
    except Exception as e:
        print(f"[ERROR] Failed to fetch WI details: {e}")
        return []
    
    # Normalize
    normalized = []
    for wi in work_items:
        f = wi.get("fields", {})
        assigned_to = f.get("System.AssignedTo")
        if isinstance(assigned_to, dict):
            assigned_to = assigned_to.get("displayName", "")
        
        title = f.get("System.Title", "")
        tags = f.get("System.Tags", "")
        area_path = f.get("System.AreaPath", "")
        
        normalized.append({
            "id": f.get("System.Id"),
            "title": title,
            "state": f.get("System.State", ""),
            "area_path": area_path,
            "iteration_path": f.get("System.IterationPath", ""),
            "work_item_type": f.get("System.WorkItemType", ""),
            "tags": tags,
            "assigned_to": assigned_to or "",
            "priority": f.get("Microsoft.VSTS.Common.Priority", 4),
            "stack_rank": f.get("Microsoft.VSTS.Common.StackRank", 999999),
            "complexity": estimate_complexity(title, tags),
            "task_type": classify_task_type(title, tags, area_path),
            "description": f.get("System.Description", ""),
            "acceptance_criteria": f.get("Microsoft.VSTS.Common.AcceptanceCriteria", ""),
        })
    
    # Sort by priority then stack rank
    normalized.sort(key=lambda x: (x.get("priority", 4), x.get("stack_rank", 999999)))
    
    return normalized


def load_developer_skills() -> List[Dict[str, Any]]:
    """Load developer skills from the knowledge base."""
    skills_file = ROOT / "data" / "developer_skills.json"
    if not skills_file.exists():
        return []
    
    try:
        return json.loads(skills_file.read_text())
    except Exception:
        return []


def load_current_capacity(capacity_file: Optional[Path] = None) -> Dict[str, Dict[str, Any]]:
    """Load current developer capacity from the latest capacity report."""
    if capacity_file and capacity_file.exists():
        try:
            data = json.loads(capacity_file.read_text())
            return data.get("developers", {})
        except Exception:
            pass
    
    # Find latest capacity report
    outputs_dir = ROOT / "outputs"
    reports = sorted(outputs_dir.glob("capacity_report_*.json"), reverse=True)
    
    if reports:
        try:
            data = json.loads(reports[0].read_text())
            print(f"[STATS] Loaded capacity from: {reports[0].name}")
            return data.get("developers", {})
        except Exception:
            pass
    
    return {}


# =============================================================================
# MAIN ASSIGNMENT LOGIC
# =============================================================================

def assign_backlog_to_underutilized(
    sprint_name: str,
    sprint_days: int = DEFAULT_SPRINT_DAYS,
    fresh_sprint: bool = True,  # True = new sprint with full capacity
    sprint_start_date: str = None,  # Sprint start date (YYYY-MM-DD)
    sprint_end_date: str = None,  # Sprint end date (YYYY-MM-DD)
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Assign backlog items to developers based on capacity.
    
    Args:
        sprint_name: Name of the sprint
        sprint_days: Duration in days
        fresh_sprint: If True, all developers start with full capacity (new sprint)
        sprint_start_date: Sprint start date in YYYY-MM-DD format
        sprint_end_date: Sprint end date in YYYY-MM-DD format
    
    Returns:
        - List of assignment rows for the backlog sheet
        - Summary statistics
    """
    # Parse dates for task scheduling
    from datetime import datetime as dt, timedelta
    if sprint_start_date:
        task_start = dt.strptime(sprint_start_date, "%Y-%m-%d")
    else:
        task_start = dt.now()
    if sprint_end_date:
        task_end = dt.strptime(sprint_end_date, "%Y-%m-%d")
    else:
        task_end = task_start + timedelta(days=sprint_days)
    print("=" * 70)
    print("BACKLOG ASSIGNMENT TO DEVELOPERS")
    print("=" * 70)
    
    # Print configuration
    if EXCLUDED_DEVELOPERS:
        print(f"[EXCLUDED] Developers: {', '.join(EXCLUDED_DEVELOPERS)}")
    print(f"[CONFIG] Cross-role assignment: {'Enabled' if ALLOW_CROSS_ROLE_ASSIGNMENT else 'Disabled'}")
    if ALLOW_CROSS_ROLE_ASSIGNMENT:
        print(f"   Evidence threshold: {CROSS_ROLE_EVIDENCE_THRESHOLD:.0%}")
    
    # Step 1: Fetch backlog items
    print("\n[BACKLOG] Fetching Ready backlog items from ADO...")
    backlog_items = fetch_backlog_items()
    print(f"   Found {len(backlog_items)} Ready items not in any sprint")
    
    if not backlog_items:
        return [], {"error": "No backlog items found"}
    
    # Step 2: Load developer skills and classify roles
    print("\n[USERS] Loading developer skills and classifying roles...")
    dev_skills = load_developer_skills()
    
    if not dev_skills:
        print("   [WARN] No developer skills found. Run analyze_commits.py first.")
        return [], {"error": "No developer skills data"}
    
    # Build developer profiles with roles (excluding excluded developers)
    developers = {}
    excluded_count = 0
    for dev in dev_skills:
        email = dev.get("developer", "").lower()
        if email:
            # Check if developer is excluded
            is_excluded = any(
                excluded in email or email in excluded
                for excluded in EXCLUDED_DEVELOPERS
            )
            
            if is_excluded:
                excluded_count += 1
                continue
            
            role, evidence = classify_developer_role(dev)
            developers[email] = {
                "email": email,
                "name": get_developer_full_name(email),
                "role": role,
                "evidence": evidence,
                "total_hours": sprint_days * DEFAULT_HOURS_PER_DAY,
                "target_hours": sprint_days * DEFAULT_HOURS_PER_DAY * DEFAULT_UTILIZATION_TARGET,
                "assigned_hours": 0,  # Fresh sprint = 0 assigned
                "task_count": 0,
            }
    
    print(f"   Loaded {len(developers)} developers (excluded: {excluded_count})")
    
    # Step 3: Load current capacity (if not a fresh sprint)
    print("\n[STATS] Loading current capacity...")
    current_capacity = load_current_capacity()
    
    if current_capacity:
        # Update with current assignments
        for email, cap_data in current_capacity.items():
            email_lower = email.lower()
            if email_lower in developers:
                developers[email_lower]["assigned_hours"] = cap_data.get("assigned_hours", 0)
                developers[email_lower]["task_count"] = cap_data.get("task_count", 0)
    else:
        print("   [INFO] No existing capacity data - treating all developers as available")
    
    # Step 4: Identify underutilized developers
    print("\n[SEARCH] Identifying underutilized developers...")
    
    fe_underutilized = []
    be_underutilized = []
    fullstack_underutilized = []
    
    for email, dev in developers.items():
        utilization = dev["assigned_hours"] / dev["total_hours"] if dev["total_hours"] > 0 else 0
        dev["utilization"] = utilization
        dev["available_hours"] = dev["target_hours"] - dev["assigned_hours"]
        
        if utilization < UNDERUTILIZATION_THRESHOLD and dev["available_hours"] > 0:
            if dev["role"] == "frontend":
                fe_underutilized.append(dev)
            elif dev["role"] == "backend":
                be_underutilized.append(dev)
            elif dev["role"] == "fullstack":
                fullstack_underutilized.append(dev)
                # Fullstack can fill both pools
                fe_underutilized.append(dev.copy())
                be_underutilized.append(dev.copy())
    
    # Sort by utilization (least utilized first)
    fe_underutilized.sort(key=lambda x: x["utilization"])
    be_underutilized.sort(key=lambda x: x["utilization"])
    fullstack_underutilized.sort(key=lambda x: x["utilization"])
    
    print(f"   Underutilized FE developers: {len(fe_underutilized)}")
    print(f"   Underutilized BE developers: {len(be_underutilized)}")
    print(f"   Underutilized Fullstack developers: {len(fullstack_underutilized)}")
    
    if not fe_underutilized and not be_underutilized and not fullstack_underutilized:
        print("\n[OK] All developers are at capacity!")
        return [], {"message": "No developers with available capacity"}
    
    # Print available developers
    print("\n   Available developers:")
    all_available = set()
    for dev in fe_underutilized + be_underutilized:
        if dev["email"] not in all_available:
            all_available.add(dev["email"])
            print(f"   - {dev['name']} ({dev['role']}): {dev['utilization']:.0%} utilized, {dev['available_hours']:.0f}h available")
    
    # Step 5: Break down backlog items into subtasks using LLM
    print("\n[BREAKDOWN] Breaking down backlog items into subtasks using LLM...")
    
    all_subtasks = []
    for item in backlog_items:
        wi_id = item["id"]
        print(f"\n  Processing WI-{wi_id}: {item['title'][:50]}...")
        subtasks = break_down_work_item(item, use_llm=True)
        all_subtasks.extend(subtasks)
    
    print(f"\n[BREAKDOWN] Created {len(all_subtasks)} subtasks from {len(backlog_items)} work items")
    
    # Step 6: Assign subtasks to developers
    print("\n[ASSIGN] Assigning subtasks to developers...")
    
    assignments = []
    assigned_count = 0
    skipped_count = 0
    cross_role_count = 0
    parent_items_assigned = set()  # Track which parent WIs have been assigned
    
    # Track capacity updates
    capacity_tracker = {email: dev["assigned_hours"] for email, dev in developers.items()}
    
    # Helper function to find developer with capacity
    def find_developer_with_capacity(dev_pool, hours_needed):
        """Find a developer from pool with sufficient capacity (not exceeding target)."""
        for dev in dev_pool:
            current_hours = capacity_tracker.get(dev["email"], 0)
            available = dev["target_hours"] - current_hours
            if available >= hours_needed:
                return dev
        return None
    
    # Helper function for cross-role assignment with evidence check
    def find_fe_developer_for_be_task(hours_needed):
        """
        Find an FE developer who can handle a BE task.
        Only allows assignment if developer has strong BE evidence.
        """
        if not ALLOW_CROSS_ROLE_ASSIGNMENT:
            return None
        
        for dev in fe_underutilized:
            current_hours = capacity_tracker.get(dev["email"], 0)
            available = dev["target_hours"] - current_hours
            if available >= hours_needed:
                # Check if developer has sufficient BE evidence
                evidence = dev.get("evidence", {})
                be_score = evidence.get("be_score", 0)
                be_languages = evidence.get("be_languages", [])
                
                # Only allow if BE score meets threshold or has BE languages
                if be_score >= CROSS_ROLE_EVIDENCE_THRESHOLD or len(be_languages) >= 1:
                    return dev
        return None
    
    # Process subtasks instead of whole work items
    for subtask in all_subtasks:
        parent_wi_id = subtask.get("parent_wi_id", subtask.get("id"))
        wi_id = subtask.get("id")
        title = subtask.get("title", "")
        subtask_name = subtask.get("subtask_type", title)
        subtask_role = subtask.get("subtask_role", "backend")
        subtask_hours = subtask.get("subtask_hours", 8)
        subtask_index = subtask.get("subtask_index", 1)
        complexity = subtask.get("complexity", "Medium")
        is_llm_generated = subtask.get("llm_generated", False)
        
        fe_dev = None
        be_dev = None
        fe_evidence_str = ""
        be_evidence_str = ""
        cross_role_note = ""
        
        # Assign based on subtask role (derived from LLM analysis)
        if subtask_role == "frontend":
            # Find FE developer with capacity
            fe_dev = find_developer_with_capacity(fe_underutilized, subtask_hours)
            
            # If no FE, try fullstack
            if not fe_dev:
                fe_dev = find_developer_with_capacity(fullstack_underutilized, subtask_hours)
            
            if fe_dev:
                capacity_tracker[fe_dev["email"]] = capacity_tracker.get(fe_dev["email"], 0) + subtask_hours
        
        else:  # backend subtask
            # Find BE developer with capacity
            be_dev = find_developer_with_capacity(be_underutilized, subtask_hours)
            
            # If no BE, try fullstack
            if not be_dev:
                be_dev = find_developer_with_capacity(fullstack_underutilized, subtask_hours)
            
            # CROSS-ROLE: Only if enabled AND strong evidence exists
            if not be_dev:
                be_dev = find_fe_developer_for_be_task(subtask_hours)
                if be_dev:
                    cross_role_note = " (cross-role)"
                    cross_role_count += 1
            
            if be_dev:
                capacity_tracker[be_dev["email"]] = capacity_tracker.get(be_dev["email"], 0) + subtask_hours
        
        # Build evidence strings
        if fe_dev:
            ev = fe_dev.get("evidence", {})
            langs = ", ".join(ev.get("fe_languages", [])[:3])
            score = ev.get("fe_score", 0)
            fe_evidence_str = f"FE:{score:.0%} ({langs})" if langs else f"FE:{score:.0%}"
        
        if be_dev:
            ev = be_dev.get("evidence", {})
            langs = ", ".join(ev.get("be_languages", [])[:3])
            score = ev.get("be_score", 0)
            be_evidence_str = f"BE:{score:.0%} ({langs})" if langs else f"BE:{score:.0%}"
        
        # Skip if no developer available for the required role
        if subtask_role == "frontend" and not fe_dev:
            skipped_count += 1
            print(f"   [WARN] WI-{wi_id} Subtask {subtask_index}: Skipped (no FE capacity available)")
            continue
        elif subtask_role == "backend" and not be_dev:
            skipped_count += 1
            print(f"   [WARN] WI-{wi_id} Subtask {subtask_index}: Skipped (no BE capacity available)")
            continue
        
        assigned_count += 1
        parent_items_assigned.add(parent_wi_id)
        
        # Calculate task duration based on estimated hours (assume 8 hours/day)
        task_duration_days = max(1, int(subtask_hours / 8))
        task_end_date = task_start + timedelta(days=task_duration_days - 1)
        
        # Get assigned developer based on subtask role
        assigned_dev = fe_dev if subtask_role == "frontend" else be_dev
        assigned_dev_name = assigned_dev["name"] if assigned_dev else ""
        assigned_dev_email = assigned_dev["email"] if assigned_dev else ""
        
        # Format task name like sprint plan: "WI-{id}.{subtask_num}: {subtask_name}"
        task_name = f"WI-{parent_wi_id}.{subtask_index}: {subtask_name}"
        
        # Create assignment row matching sprint plan format
        row = {
            "Sprint": sprint_name,
            "Feature / User Story": title,  # Parent work item title
            "Task Name": task_name,  # Formatted like WI-73942.1: Task description
            "WI ID": wi_id,
            "Parent WI ID": parent_wi_id,
            "Subtask #": subtask_index,
            "Task Type": subtask_role.upper() + cross_role_note,
            "Priority": subtask.get("priority", 4),
            "Complexity": complexity,
            "Start Date": task_start.strftime("%Y-%m-%d"),
            "End Date": task_end_date.strftime("%Y-%m-%d"),
            "Duration (days)": task_duration_days,
            "Estimated Hours": subtask_hours,
            "Responsible - Frontend": fe_dev["name"] if fe_dev else "None",
            "Responsible - Backend": be_dev["name"] if be_dev else "None",
            "Developer Email": assigned_dev_email,
            "FE Email": fe_dev["email"] if fe_dev else "",
            "FE Evidence": fe_evidence_str,
            "FE Utilization Before": f"{fe_dev['utilization']:.0%}" if fe_dev else "",
            "BE Email": be_dev["email"] if be_dev else "",
            "BE Evidence": be_evidence_str,
            "BE Utilization Before": f"{be_dev['utilization']:.0%}" if be_dev else "",
            "Status": "Not Started",
            "Tags": subtask.get("tags", ""),
            "LLM Generated": "Yes" if is_llm_generated else "No",
        }
        assignments.append(row)
        
        # Log assignment
        print(f"   [OK] WI-{wi_id} #{subtask_index}: {subtask_name[:40]}... -> {assigned_dev_name} ({subtask_hours}h, {subtask_role}{cross_role_note})")
    
    # Summary
    summary = {
        "total_backlog_items": len(backlog_items),
        "total_subtasks": len(all_subtasks),
        "subtasks_assigned": assigned_count,
        "parent_items_assigned": len(parent_items_assigned),
        "skipped": skipped_count,
        "cross_role_assignments": cross_role_count,
        "available_developers": len(all_available),
    }
    
    print(f"\n[STATS] Assignment Summary:")
    print(f"   Total backlog items: {len(backlog_items)}")
    print(f"   Total subtasks created: {len(all_subtasks)}")
    print(f"   Subtasks assigned: {assigned_count}")
    print(f"   Parent items with assignments: {len(parent_items_assigned)}")
    print(f"   Skipped (no capacity): {skipped_count}")
    if cross_role_count > 0:
        print(f"   Cross-role assignments: {cross_role_count}")
    
    return assignments, summary


def save_backlog_sheet(
    assignments: List[Dict[str, Any]],
    sprint_name: str,
    timestamp: str,
) -> Path:
    """Save backlog assignments to a dedicated CSV sheet."""
    outputs_dir = ROOT / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)
    
    csv_path = outputs_dir / f"backlog_assignments_{timestamp}.csv"
    
    fieldnames = [
        "Sprint",
        "Feature / User Story",
        "Task Name",
        "WI ID",
        "Parent WI ID",
        "Subtask #",
        "Task Type",
        "Priority",
        "Complexity",
        "Start Date",
        "End Date",
        "Duration (days)",
        "Estimated Hours",
        "Responsible - Frontend",
        "Responsible - Backend",
        "Developer Email",
        "FE Email",
        "FE Evidence",
        "FE Utilization Before",
        "BE Email",
        "BE Evidence",
        "BE Utilization Before",
        "Status",
        "Tags",
        "LLM Generated",
    ]
    
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(assignments)
    
    return csv_path


def save_updated_capacity_report(
    developers: Dict[str, Dict[str, Any]],
    assignments: List[Dict[str, Any]],
    sprint_name: str,
    timestamp: str,
) -> Path:
    """Save updated capacity report after backlog assignments."""
    outputs_dir = ROOT / "outputs"
    
    # Calculate new utilization after assignments
    capacity_updates = defaultdict(lambda: {"added_hours": 0, "new_tasks": 0})
    
    for row in assignments:
        if row.get("FE Email"):
            fe_email = row["FE Email"].lower()
            hours = row["Estimated Hours"]
            if row.get("BE Email"):
                hours = hours * 0.5  # Split if both assigned
            capacity_updates[fe_email]["added_hours"] += hours
            capacity_updates[fe_email]["new_tasks"] += 1
        
        if row.get("BE Email"):
            be_email = row["BE Email"].lower()
            hours = row["Estimated Hours"]
            if row.get("FE Email"):
                hours = hours * 0.5
            capacity_updates[be_email]["added_hours"] += hours
            capacity_updates[be_email]["new_tasks"] += 1
    
    # Build capacity summary
    capacity_summary = {
        "generated_at": datetime.now().isoformat(),
        "sprint_name": sprint_name,
        "type": "backlog_assignment",
        "developers": {},
    }
    
    for email, dev in developers.items():
        update = capacity_updates.get(email, {"added_hours": 0, "new_tasks": 0})
        new_assigned = dev["assigned_hours"] + update["added_hours"]
        new_utilization = new_assigned / dev["total_hours"] if dev["total_hours"] > 0 else 0
        
        capacity_summary["developers"][email] = {
            "name": dev["name"],
            "role": dev["role"],
            "total_hours": dev["total_hours"],
            "previous_hours": dev["assigned_hours"],
            "backlog_hours_added": update["added_hours"],
            "new_total_hours": new_assigned,
            "new_utilization": round(new_utilization, 2),
            "backlog_tasks_added": update["new_tasks"],
        }
    
    json_path = outputs_dir / f"backlog_capacity_update_{timestamp}.json"
    with json_path.open("w", encoding="utf-8") as f:
        json.dump(capacity_summary, f, indent=2)
    
    return json_path


# =============================================================================
# MAIN
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Assign backlog items to underutilized developers")
    parser.add_argument("--sprint", type=str, default="Backlog Sprint", help="Sprint name")
    parser.add_argument("--start", type=str, required=True, help="Sprint start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, required=True, help="Sprint end date (YYYY-MM-DD)")
    parser.add_argument("--days", type=int, default=DEFAULT_SPRINT_DAYS, help="Sprint duration in days (overridden by --start/--end)")
    args = parser.parse_args()
    
    # Calculate sprint days from dates if provided
    from datetime import datetime as dt
    sprint_start = dt.strptime(args.start, "%Y-%m-%d")
    sprint_end = dt.strptime(args.end, "%Y-%m-%d")
    sprint_days = (sprint_end - sprint_start).days + 1
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    
    # Run assignment
    assignments, summary = assign_backlog_to_underutilized(
        sprint_name=args.sprint,
        sprint_days=sprint_days,
        sprint_start_date=args.start,
        sprint_end_date=args.end,
    )
    
    if not assignments:
        print("\n[ERROR] No assignments generated.")
        return 1
    
    # Save backlog sheet
    csv_path = save_backlog_sheet(assignments, args.sprint, timestamp)
    print(f"\n[SAVE] Backlog Assignments Sheet: {csv_path}")
    
    # Load developers for capacity update
    dev_skills = load_developer_skills()
    developers = {}
    for dev in dev_skills:
        email = dev.get("developer", "").lower()
        if email:
            role, evidence = classify_developer_role(dev)
            developers[email] = {
                "email": email,
                "name": get_developer_full_name(email),
                "role": role,
                "evidence": evidence,
                "total_hours": args.days * DEFAULT_HOURS_PER_DAY,
                "assigned_hours": 0,
            }
    
    # Load existing capacity
    current_capacity = load_current_capacity()
    for email, cap_data in current_capacity.items():
        email_lower = email.lower()
        if email_lower in developers:
            developers[email_lower]["assigned_hours"] = cap_data.get("assigned_hours", 0)
    
    # Save capacity update
    json_path = save_updated_capacity_report(developers, assignments, args.sprint, timestamp)
    print(f"[STATS] Capacity Update Report: {json_path}")
    
    # Try Excel export
    try:
        import pandas as pd
        df = pd.DataFrame(assignments)
        xlsx_path = csv_path.with_suffix(".xlsx")
        df.to_excel(xlsx_path, index=False, sheet_name="Backlog Assignments")
        print(f"[EXCEL] Excel File: {xlsx_path}")
    except ImportError:
        print("[INFO] openpyxl not available - Excel export skipped")
    
    print("\n" + "=" * 70)
    print("[OK] BACKLOG ASSIGNMENT COMPLETE")
    print("=" * 70)
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
