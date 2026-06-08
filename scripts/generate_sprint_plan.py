#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Generate sprint plan with frontend/backend role assignments and capacity checking.
This script creates a sprint plan CSV/Excel from profiled tasks.

Features:
- Role-based (FE/BE) developer assignment based on git commit evidence
- Capacity tracking (80 hours per 10-day sprint)
- Overload detection and warnings
- Redistribution suggestions for balanced workload
- Backlog items from ADO (XOPS Bugs Enhancement backlog)
- Evidence-based FE/BE classification using commit history
- Excluded developers list (configurable in config.yaml)
- Task breakdown for complex work items

Usage:
    python scripts/generate_sprint_plan.py --sprint "Sprint 2024-01-01" --start 2024-01-01 --end 2024-01-14
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
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import config
from utilities.task_profiler import profile_upcoming_tasks, load_wi_tags
from utilities.langfuse_client import trace_task


# =============================================================================
# ADO DATA FETCHING (for fresh systems without cached data)
# =============================================================================

def fetch_team_members_from_ado() -> List[Dict[str, Any]]:
    """Fetch team members from ADO when developer_skills.json doesn't exist.
    
    Returns a basic developer list with email and name.
    """
    org_url = config.ado_org_url
    project = config.ado_project
    pat = config.ado_pat
    team = os.environ.get("ADO_TEAM", "")
    
    if not org_url or not project or not pat:
        print("Warning: ADO credentials not configured")
        return []
    
    headers = _get_ado_headers(pat)
    
    try:
        # Try to get team members if team is specified
        if team:
            team_url = f"{org_url}/_apis/projects/{project}/teams/{team}/members?api-version=7.1"
            resp = requests.get(team_url, headers=headers, timeout=30)
            if resp.status_code == 200:
                members = resp.json().get("value", [])
                return [
                    {
                        "developer": m.get("identity", {}).get("uniqueName", ""),
                        "name": m.get("identity", {}).get("displayName", ""),
                        "languages": [],
                        "all_languages": {},
                        "top_files": [],
                        "commits": 0,
                    }
                    for m in members
                    if m.get("identity", {}).get("uniqueName")
                ]
        
        # Fallback: Get all project teams and their members
        teams_url = f"{org_url}/_apis/projects/{project}/teams?api-version=7.1"
        resp = requests.get(teams_url, headers=headers, timeout=30)
        if resp.status_code != 200:
            return []
        
        all_members = {}
        for team_info in resp.json().get("value", []):
            team_id = team_info.get("id")
            if team_id:
                members_url = f"{org_url}/_apis/projects/{project}/teams/{team_id}/members?api-version=7.1"
                resp = requests.get(members_url, headers=headers, timeout=30)
                if resp.status_code == 200:
                    for m in resp.json().get("value", []):
                        email = m.get("identity", {}).get("uniqueName", "")
                        if email and email not in all_members:
                            all_members[email] = {
                                "developer": email,
                                "name": m.get("identity", {}).get("displayName", ""),
                                "languages": [],
                                "all_languages": {},
                                "top_files": [],
                                "commits": 0,
                            }
        
        return list(all_members.values())
    
    except Exception as e:
        print(f"Warning: Could not fetch team members from ADO: {e}")
        return []


def get_area_paths_from_config() -> List[str]:
    """Get area paths from config.yaml for profiling upcoming tasks."""
    try:
        config_file = ROOT / "config.yaml"
        if config_file.exists():
            with open(config_file, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
            
            # Try different config keys
            area_paths = cfg.get("upcomingAreaPaths", [])
            if not area_paths:
                area_paths = cfg.get("sprint_planning", {}).get("area_paths", [])
            if not area_paths:
                # Default to project root
                area_paths = [config.ado_project]
            return area_paths
    except Exception:
        pass
    return [config.ado_project] if config.ado_project else []


def fetch_developer_skills_from_ado(days: int = 90) -> List[Dict[str, Any]]:
    """Fetch developer skills by analyzing commit history from ADO.
    
    This provides proper FE/BE classification based on actual code contributions.
    Ensures no duplicate developers by normalizing email addresses.
    """
    try:
        from utilities.ado_commit_analyzer import (
            query_recent_work_items,
            fetch_work_items_batch,
            fetch_commits_for_work_items,
            build_tech_stack_by_developer,
        )
        
        org_url = config.ado_org_url
        project = config.ado_project
        pat = config.ado_pat
        
        if not all([org_url, project, pat]):
            print("  Warning: ADO credentials not configured")
            return []
        
        print(f"  Fetching commits from last {days} days...")
        
        # Query recent work items
        wi_ids = query_recent_work_items(org_url, project, pat, days=days, max_items=200)
        if not wi_ids:
            print("  No recent work items found")
            return []
        
        print(f"  Found {len(wi_ids)} recent work items")
        
        # Fetch work items with relations (to get commit links)
        work_items = fetch_work_items_batch(org_url, project, pat, wi_ids)
        print(f"  Fetched {len(work_items)} work items with relations")
        
        # Fetch commits and analyze
        analysis = fetch_commits_for_work_items(org_url, project, pat, work_items)
        print(f"  Analyzed {analysis.get('total_commits', 0)} commits")
        
        # Build developer skills list with deduplication
        developers_by_email: Dict[str, Dict[str, Any]] = {}  # email -> merged data
        tech_stack = build_tech_stack_by_developer(analysis)
        
        for author, data in analysis.get("by_author", {}).items():
            # Parse email from author (format: "Name <email>")
            email = author
            if "<" in author and ">" in author:
                email = author.split("<")[1].split(">")[0]
            
            # Normalize email to lowercase for deduplication
            email_key = email.lower().strip()
            
            if not email_key:
                continue
            
            # If developer already exists, merge the data
            if email_key in developers_by_email:
                existing = developers_by_email[email_key]
                # Merge languages
                for lang, count in data.get("languages", {}).items():
                    if lang in existing["all_languages"]:
                        existing["all_languages"][lang] += count
                    else:
                        existing["all_languages"][lang] = count
                # Merge commits
                existing["commits"] += data.get("commits", 0)
                # Merge top files
                existing_files = set(existing.get("top_files", []))
                existing_files.update(data.get("top_files", [])[:20])
                existing["top_files"] = list(existing_files)[:20]
                # Update languages list
                existing["languages"] = list(existing["all_languages"].keys())[:10]
            else:
                # New developer
                developers_by_email[email_key] = {
                    "developer": email_key,
                    "name": author.split("<")[0].strip() if "<" in author else author,
                    "languages": list(data.get("languages", {}).keys())[:10],
                    "all_languages": data.get("languages", {}),
                    "top_files": data.get("top_files", [])[:20],
                    "commits": data.get("commits", 0),
                }
        
        developers = list(developers_by_email.values())
        print(f"  Built skills profile for {len(developers)} unique developers")
        return developers
        
    except ImportError as e:
        print(f"  Warning: Could not import commit analyzer: {e}")
        return []
    except Exception as e:
        print(f"  Warning: Failed to fetch developer skills: {e}")
        return []


def classify_work_item_role(task: Dict[str, Any]) -> str:
    """Classify a work item as frontend, backend, or both based on content.
    
    Analyzes title, description, and area path to determine role.
    """
    title = (task.get("title", "") or "").lower()
    description = (task.get("description", "") or "").lower()
    area_path = (task.get("area_path", "") or "").lower()
    tags = (task.get("tags", "") or "").lower()
    
    # Combine all text for analysis
    text = f"{title} {description} {area_path} {tags}"
    
    # Frontend indicators
    fe_keywords = [
        "ui", "frontend", "front-end", "angular", "react", "vue", "component",
        "screen", "form", "input", "button", "modal", "dialog", "display",
        "layout", "style", "css", "html", "template", "view", "page",
        "dashboard", "chart", "graph", "visualization", "responsive",
        "mobile", "ionic", "typescript", "tsx", "jsx"
    ]
    
    # Backend indicators  
    be_keywords = [
        "api", "backend", "back-end", "server", "database", "db", "sql",
        "query", "endpoint", "service", "repository", "controller",
        "java", "spring", "python", "flask", "django", "node",
        "microservice", "kafka", "rabbitmq", "queue", "cache", "redis",
        "authentication", "auth", "security", "token", "jwt",
        "integration", "sync", "batch", "job", "scheduler", "cron"
    ]
    
    fe_count = sum(1 for kw in fe_keywords if kw in text)
    be_count = sum(1 for kw in be_keywords if kw in text)
    
    if fe_count > 0 and be_count > 0:
        return "both"
    elif fe_count > be_count:
        return "frontend"
    elif be_count > fe_count:
        return "backend"
    else:
        return "both"  # Default to both if unclear


def generate_dynamic_assignments(
    tasks: List[Dict[str, Any]],
    developer_capacity: Dict[str, Dict[str, Any]],
    developer_roles: Dict[str, List[str]],
) -> Dict[int, Dict[str, Any]]:
    """Generate assignment suggestions dynamically based on task content and developer skills.
    
    This is used when pre-computed assignment suggestions don't exist.
    """
    suggestions = {}
    
    # Build skill index for developers
    dev_skills = {}
    for email, cap in developer_capacity.items():
        evidence = cap.get("evidence", {})
        dev_skills[email] = {
            "role": cap.get("role", "unknown"),
            "fe_score": evidence.get("fe_score", 0),
            "be_score": evidence.get("be_score", 0),
            "languages": evidence.get("fe_languages", []) + evidence.get("be_languages", []),
            "assigned_hours": cap.get("assigned_hours", 0),
            "target_hours": cap.get("target_hours", 64),
        }
    
    for task in tasks:
        wi_id = task.get("id")
        if not wi_id:
            continue
        
        # Classify task
        task_role = classify_work_item_role(task)
        
        # Find best FE developer
        fe_candidates = []
        be_candidates = []
        
        for email, skills in dev_skills.items():
            remaining = skills["target_hours"] - skills["assigned_hours"]
            if remaining <= 0:
                continue
            
            role = skills["role"]
            fe_score = skills["fe_score"]
            be_score = skills["be_score"]
            
            # FE candidates
            if role in ("frontend", "fullstack", "unknown") or fe_score > 0.3:
                fe_candidates.append({
                    "developer": email,
                    "score": fe_score if fe_score > 0 else 0.3,
                    "remaining": remaining,
                })
            
            # BE candidates
            if role in ("backend", "fullstack", "unknown") or be_score > 0.3:
                be_candidates.append({
                    "developer": email,
                    "score": be_score if be_score > 0 else 0.3,
                    "remaining": remaining,
                })
        
        # Sort by score (descending), then by remaining capacity (descending)
        fe_candidates.sort(key=lambda x: (-x["score"], -x["remaining"]))
        be_candidates.sort(key=lambda x: (-x["score"], -x["remaining"]))
        
        suggestions[wi_id] = {
            "task_role": task_role,
            "frontend": fe_candidates[:3] if fe_candidates else [],
            "backend": be_candidates[:3] if be_candidates else [],
        }
    
    return suggestions


# =============================================================================
# CONFIGURATION LOADING
# =============================================================================

def load_config() -> Dict[str, Any]:
    """Load configuration from config.yaml."""
    config_file = ROOT / "config.yaml"
    if config_file.exists():
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            print(f"Warning: Could not load config.yaml: {e}")
    return {}


# Load config at module level
CONFIG = load_config()
SPRINT_CONFIG = CONFIG.get("sprint_planning", {})

# Excluded developers (normalized to lowercase email)
EXCLUDED_DEVELOPERS = [d.lower() for d in SPRINT_CONFIG.get("excluded_developers", [])]

# Task breakdown settings
TASK_BREAKDOWN_CONFIG = SPRINT_CONFIG.get("task_breakdown", {})
TASK_BREAKDOWN_ENABLED = TASK_BREAKDOWN_CONFIG.get("enabled", True)
TASK_BREAKDOWN_USE_LLM = TASK_BREAKDOWN_CONFIG.get("use_llm", True)
COMPLEXITY_BREAKDOWN = TASK_BREAKDOWN_CONFIG.get("complexity_breakdown", {
    "Small": 1, "Medium": 2, "Large": 3, "XLarge": 4
})
TASK_TYPES = TASK_BREAKDOWN_CONFIG.get("task_types", [
    "Analysis & Design", "Implementation", "Testing & QA", "Code Review & Integration"
])

# Capacity settings from config
CAPACITY_CONFIG = SPRINT_CONFIG.get("capacity", {})
DEFAULT_HOURS_PER_DAY = CAPACITY_CONFIG.get("hours_per_day", 8)
DEFAULT_UTILIZATION_TARGET = CAPACITY_CONFIG.get("utilization_target", 0.80)

# Cross-role assignment settings
CROSS_ROLE_CONFIG = SPRINT_CONFIG.get("cross_role_assignment", {})
ALLOW_CROSS_ROLE = CROSS_ROLE_CONFIG.get("enabled", False)
CROSS_ROLE_EVIDENCE_THRESHOLD = CROSS_ROLE_CONFIG.get("evidence_threshold", 0.30)

# Complexity to duration mapping (in days) - from config.yaml
COMPLEXITY_DAYS = SPRINT_CONFIG.get("complexity_duration", {
    "Small": 1,
    "Medium": 3,
    "Large": 5,
    "XLarge": 8,
})

# Complexity to hours mapping - from config.yaml
COMPLEXITY_HOURS = SPRINT_CONFIG.get("complexity_hours", {
    "Small": 8,      # 1 day
    "Medium": 24,    # 3 days
    "Large": 40,     # 5 days
    "XLarge": 64,    # 8 days
})

# Backlog team name from ADO backlog URL
# URL: https://dev.azure.com/Stratagen/FracPro-OPS/_backlogs/backlog/XOPS%20Bugs%20Enhancement/Stories
BACKLOG_TEAM = "XOPS Bugs Enhancement"

# Backlog area path for xops bugs enhancement
BACKLOG_AREA_PATHS = [
    "FracPro-OPS\\Global Management\\WTT Development\\XOPS Bugs Enhancement",
]

# Skills that indicate frontend developer (from commit evidence)
FRONTEND_SKILLS = {
    "angular", "react", "vue", "typescript", "javascript", "html", "css",
    "scss", "sass", "frontend", "ui", "component", "ionic", "mobile",
    "ios", "android", "flutter", "react-native"
}

# Skills that indicate backend developer (from commit evidence)
BACKEND_SKILLS = {
    "java", "python", "csharp", "c#", "spring", "backend", "api",
    "rest", "sql", "database", "db", "node", "express", "django",
    "flask", "dotnet", ".net", "microservices", "kafka", "rabbitmq",
    "gradle", "maven"
}

# File path patterns for FE/BE classification
FE_PATH_PATTERNS = ["ui", "frontend", "component", "angular", "react", "vue", "src/app", "pages", "views"]
BE_PATH_PATTERNS = ["api", "backend", "server", "service", "controller", "repository", "dao", "model"]


def _get_ado_headers(pat: str) -> Dict[str, str]:
    """Build authorization headers for ADO REST API."""
    auth = base64.b64encode(f":{pat}".encode()).decode()
    return {
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/json",
    }


def classify_developer_from_commits(dev_data: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """
    Classify a developer as frontend, backend, or fullstack based on commit evidence.
    
    Uses:
    - Languages from commits (TypeScript, JavaScript, HTML, CSS = FE; Java, Python, C# = BE)
    - File paths touched (ui/, frontend/ = FE; api/, backend/, service/ = BE)
    
    Returns: (role, evidence_dict)
    """
    languages = dev_data.get("languages", [])
    all_languages = dev_data.get("all_languages", {})
    top_files = dev_data.get("top_files", [])
    
    # Count FE and BE evidence from languages
    fe_lang_count = 0
    be_lang_count = 0
    fe_lang_matches = []
    be_lang_matches = []
    
    for lang in languages:
        lang_lower = lang.lower()
        if lang_lower in FRONTEND_SKILLS:
            fe_lang_count += all_languages.get(lang, 1)
            fe_lang_matches.append(lang)
        elif lang_lower in BACKEND_SKILLS:
            be_lang_count += all_languages.get(lang, 1)
            be_lang_matches.append(lang)
    
    # Count FE and BE evidence from file paths
    fe_path_count = 0
    be_path_count = 0
    fe_path_matches = []
    be_path_matches = []
    
    for file_info in top_files:
        if isinstance(file_info, (list, tuple)) and len(file_info) >= 2:
            path, count = file_info[0], file_info[1]
        else:
            path = str(file_info)
            count = 1
        
        path_lower = path.lower()
        
        # Check for FE patterns
        if any(p in path_lower for p in FE_PATH_PATTERNS):
            fe_path_count += count
            if path not in fe_path_matches[:3]:
                fe_path_matches.append(path)
        
        # Check for BE patterns
        if any(p in path_lower for p in BE_PATH_PATTERNS):
            be_path_count += count
            if path not in be_path_matches[:3]:
                be_path_matches.append(path)
    
    # Calculate total evidence scores
    total_fe = fe_lang_count + fe_path_count
    total_be = be_lang_count + be_path_count
    total = total_fe + total_be
    
    evidence = {
        "fe_score": round(total_fe / total, 2) if total > 0 else 0,
        "be_score": round(total_be / total, 2) if total > 0 else 0,
        "fe_languages": fe_lang_matches,
        "be_languages": be_lang_matches,
        "fe_paths": fe_path_matches[:3],
        "be_paths": be_path_matches[:3],
        "commits": dev_data.get("commits", 0),
        "source": "git_commits",
    }
    
    # Classification threshold: 70% for clear role, else based on majority
    THRESHOLD = 0.70
    
    if total == 0:
        return "unknown", evidence
    
    fe_ratio = total_fe / total
    be_ratio = total_be / total
    
    if fe_ratio >= THRESHOLD:
        return "frontend", evidence
    elif be_ratio >= THRESHOLD:
        return "backend", evidence
    elif total_fe > 0 and total_be > 0:
        # Has evidence for both - consider fullstack or lean towards stronger
        if fe_ratio > be_ratio:
            return "frontend", evidence
        else:
            return "backend", evidence
    elif total_fe > 0:
        return "frontend", evidence
    elif total_be > 0:
        return "backend", evidence
    else:
        return "unknown", evidence


def fetch_backlog_items(
    area_paths: List[str],
    max_items: int = 100,
) -> List[Dict[str, Any]]:
    """
    Fetch backlog items from ADO that are:
    - In Ready state
    - Not assigned to any sprint (iteration)
    - Ordered by backlog priority (Stack Rank)
    
    Returns list of normalized work item dicts.
    """
    org_url = config.ado_org_url
    project = config.ado_project
    pat = config.ado_pat
    
    if not org_url or not project or not pat:
        print("Warning: ADO credentials not configured in config.yaml, skipping backlog fetch")
        return []
    
    headers = _get_ado_headers(pat)
    
    # Build WIQL query for Ready items not in any sprint, ordered by Stack Rank
    area_conditions = " OR ".join([f"[System.AreaPath] UNDER '{ap}'" for ap in area_paths])
    
    # Query for items in Ready state, not in any sprint iteration
    wiql = f"""
    SELECT [System.Id]
    FROM WorkItems
    WHERE [System.TeamProject] = '{project}'
      AND [System.State] = 'Ready'
      AND ([System.IterationPath] = '{project}' OR [System.IterationPath] = '')
      AND ({area_conditions})
    ORDER BY [Microsoft.VSTS.Common.StackRank] ASC, [Microsoft.VSTS.Common.Priority] ASC, [System.CreatedDate] ASC
    """
    
    wiql_url = f"{org_url}/{project}/_apis/wit/wiql?api-version=7.0&$top={max_items}"
    
    try:
        resp = requests.post(wiql_url, headers=headers, json={"query": wiql}, timeout=60)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"Warning: Failed to query backlog items: {e}")
        return []
    
    wi_ids = [item["id"] for item in data.get("workItems", [])]
    
    if not wi_ids:
        print(f"No backlog items found in area paths: {area_paths}")
        return []
    
    print(f"Found {len(wi_ids)} backlog items in xops bugs enhancement")
    
    # Fetch work item details
    fields = [
        "System.Id",
        "System.Title",
        "System.State",
        "System.AreaPath",
        "System.IterationPath",
        "System.WorkItemType",
        "System.Description",
        "System.AssignedTo",
        "System.CreatedDate",
        "System.ChangedDate",
        "System.Tags",
        "Microsoft.VSTS.Scheduling.RemainingWork",
        "Microsoft.VSTS.Scheduling.StoryPoints",
        "Microsoft.VSTS.Common.Priority",
        "Microsoft.VSTS.Common.StackRank",
    ]
    
    all_wis = []
    batch_size = 200
    
    for i in range(0, len(wi_ids), batch_size):
        batch_ids = wi_ids[i:i + batch_size]
        ids_str = ",".join(str(x) for x in batch_ids)
        fields_str = ",".join(fields)
        
        details_url = f"{org_url}/{project}/_apis/wit/workitems?ids={ids_str}&fields={fields_str}&api-version=7.0"
        
        try:
            resp = requests.get(details_url, headers=headers, timeout=60)
            resp.raise_for_status()
            batch_data = resp.json()
            all_wis.extend(batch_data.get("value", []))
        except requests.RequestException as e:
            print(f"Warning: Failed to fetch batch {i}-{i + batch_size}: {e}")
            continue
    
    # Normalize and return
    normalized = []
    for wi in all_wis:
        fields = wi.get("fields", {})
        assigned_to = fields.get("System.AssignedTo")
        if isinstance(assigned_to, dict):
            assigned_to = assigned_to.get("displayName") or assigned_to.get("uniqueName", "")
        
        normalized.append({
            "id": wi.get("id"),
            "title": fields.get("System.Title", ""),
            "state": fields.get("System.State", ""),
            "area_path": fields.get("System.AreaPath", ""),
            "iteration_path": fields.get("System.IterationPath", ""),
            "work_item_type": fields.get("System.WorkItemType", ""),
            "description": fields.get("System.Description", ""),
            "assigned_to": assigned_to or "",
            "created_date": fields.get("System.CreatedDate", ""),
            "priority": fields.get("Microsoft.VSTS.Common.Priority", 2),
            "stack_rank": fields.get("Microsoft.VSTS.Common.StackRank", 999999),
            "remaining_work": fields.get("Microsoft.VSTS.Scheduling.RemainingWork"),
            "story_points": fields.get("Microsoft.VSTS.Scheduling.StoryPoints"),
            "tags": fields.get("System.Tags", ""),
            # Default complexity based on priority
            "complexity": "Small" if fields.get("Microsoft.VSTS.Common.Priority", 2) <= 1 else "Medium",
            "source": "backlog",
        })
    
    # Sort by stack rank to maintain backlog order
    normalized.sort(key=lambda x: (x.get("stack_rank", 999999), x.get("priority", 2)))
    
    return normalized


def get_developer_name(email: str) -> str:
    """Extract readable name from email address.
    
    Returns lowercase format like 'ananya.gupta' matching reference image.
    """
    if not email:
        return ""
    # Extract name part before @ and return as-is (lowercase with dots)
    name = email.split("@")[0]
    return name.lower()


def get_developer_full_name(email: str) -> str:
    """Convert email to full name format like 'Vijay Kumar'.
    
    Handles formats like:
    - vijay.kumar@company.com -> Vijay Kumar
    - v_narendra.bandhamneni@company.com -> V Narendra Bandhamneni
    - 86947981+bhanuvinay-wt@users.noreply.github.com -> Bhanu Vinay
    """
    if not email:
        return ""
    
    # Extract name part before @
    name_part = email.split("@")[0]
    
    # Handle GitHub-style usernames (123456+username)
    if "+" in name_part:
        name_part = name_part.split("+")[1]
    
    # Replace common separators with spaces
    name_part = name_part.replace(".", " ").replace("_", " ").replace("-", " ")
    
    # Remove numbers
    name_part = ''.join(c for c in name_part if not c.isdigit())
    
    # Title case each word
    words = [w.strip().title() for w in name_part.split() if w.strip()]
    
    return " ".join(words) if words else email.split("@")[0]


def has_sufficient_description(task: Dict[str, Any]) -> bool:
    """
    Check if work item has sufficient description for task breakdown and assignment.
    
    Returns True if description or acceptance criteria has meaningful content.
    Returns False if work item lacks details ("Need more info" case).
    """
    import re
    from html import unescape
    
    def clean_html(text):
        """Remove HTML tags and decode entities."""
        if not text:
            return ""
        text = re.sub(r'<[^>]+>', ' ', text)
        text = unescape(text)
        text = re.sub(r'\s+', ' ', text)
        return text.strip()
    
    description = task.get("description", "") or ""
    acceptance_criteria = task.get("acceptance_criteria", "") or ""
    
    # Clean HTML from both fields
    clean_desc = clean_html(description)
    clean_ac = clean_html(acceptance_criteria)
    
    # Minimum thresholds for meaningful content
    # At least 50 chars in description OR 30 chars in acceptance criteria
    return len(clean_desc) >= 50 or len(clean_ac) >= 30


def get_evidence_summary(evidence: Dict[str, Any], role: str) -> str:
    """Generate a human-readable evidence summary for assignment."""
    if not evidence:
        return "No evidence"
    
    parts = []
    
    if role == "frontend":
        if evidence.get("fe_languages"):
            parts.append(f"Languages: {', '.join(evidence['fe_languages'][:3])}")
        if evidence.get("fe_paths"):
            paths = [p.split("/")[-1] for p in evidence.get("fe_paths", [])[:2]]
            parts.append(f"Paths: {', '.join(paths)}")
        score = evidence.get("fe_score", 0)
        parts.append(f"Score: {score:.0%}")
    else:  # backend
        if evidence.get("be_languages"):
            parts.append(f"Languages: {', '.join(evidence['be_languages'][:3])}")
        if evidence.get("be_paths"):
            paths = [p.split("/")[-1] for p in evidence.get("be_paths", [])[:2]]
            parts.append(f"Paths: {', '.join(paths)}")
        score = evidence.get("be_score", 0)
        parts.append(f"Score: {score:.0%}")
    
    if evidence.get("commits"):
        parts.append(f"Commits: {evidence['commits']}")
    
    return " | ".join(parts) if parts else "Inferred from commits"


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
    import re
    from html import unescape
    
    title = task.get("title", "")
    description = task.get("description", "") or ""
    acceptance_criteria = task.get("acceptance_criteria", "") or ""
    complexity = task.get("complexity", "Medium")
    total_hours = COMPLEXITY_HOURS.get(complexity, 24)
    
    # Clean HTML from description
    def clean_html(text):
        """Remove HTML tags and decode entities."""
        if not text:
            return ""
        text = re.sub(r'<[^>]+>', ' ', text)
        text = unescape(text)
        text = re.sub(r'\s+', ' ', text)
        return text.strip()
    
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
2. The type: "Frontend" (UI/Angular/HTML/CSS work) or "Backend" (API/Service/Database/C# work)
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
        
        # Clean up response
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
            subtask["llm_generated"] = True  # Mark as LLM-generated
            subtasks.append(subtask)
        
        print(f"  [LLM] WI-{task.get('id')}: Broke down into {len(subtasks)} subtasks")
        for st in subtasks:
            print(f"       - {st['subtask_type']} ({st['subtask_role']}, {st['subtask_hours']}h)")
        
        return subtasks
        
    except Exception as e:
        print(f"  [LLM] Error breaking down WI-{task.get('id')}: {e}")
        return break_down_work_item_simple(task)


def break_down_work_item_simple(task: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Simple task breakdown based on complexity (fallback when LLM unavailable).
    Uses generic task types like Design, Implementation, Testing.
    """
    complexity = task.get("complexity", "Medium")
    num_subtasks = COMPLEXITY_BREAKDOWN.get(complexity, 2)
    
    if num_subtasks <= 1:
        return [task]
    
    # Calculate hours per subtask
    total_hours = COMPLEXITY_HOURS.get(complexity, 24)
    hours_per_task = total_hours / num_subtasks
    
    subtasks = []
    task_type_cycle = TASK_TYPES[:num_subtasks]
    
    for i, task_type in enumerate(task_type_cycle):
        subtask = task.copy()
        subtask["subtask_index"] = i + 1
        subtask["subtask_type"] = task_type
        subtask["subtask_hours"] = hours_per_task
        subtask["is_subtask"] = True
        subtask["parent_wi_id"] = task.get("id")
        subtasks.append(subtask)
    
    return subtasks


def break_down_work_item(task: Dict[str, Any], use_llm: Optional[bool] = None) -> List[Dict[str, Any]]:
    """
    Break down a work item into smaller sub-tasks.
    
    Uses LLM-based intelligent breakdown when:
    - use_llm is True (or config.yaml sprint_planning.task_breakdown.use_llm is True)
    - OpenAI API key is configured
    - Work item has description or acceptance criteria
    
    Falls back to simple complexity-based breakdown otherwise.
    
    Duration and hours are calculated based on complexity settings in config.yaml:
    - complexity_duration: Maps complexity to days (Small=1, Medium=3, Large=5, XLarge=8)
    - complexity_hours: Maps complexity to total hours (Small=8, Medium=24, Large=40, XLarge=64)
    
    Returns a list of sub-tasks with appropriate assignments.
    Small items remain as single task; Medium/Large/XLarge are split.
    """
    if not TASK_BREAKDOWN_ENABLED:
        return [task]
    
    complexity = task.get("complexity", "Medium")
    num_subtasks = COMPLEXITY_BREAKDOWN.get(complexity, 2)
    
    if num_subtasks <= 1:
        return [task]
    
    # Use config value if not explicitly specified
    if use_llm is None:
        use_llm = TASK_BREAKDOWN_USE_LLM
    
    # Try LLM-based breakdown if enabled and API key is available
    if use_llm and config.openai_api_key:
        description = task.get("description", "") or ""
        acceptance_criteria = task.get("acceptance_criteria", "") or ""
        
        # Only use LLM if there's meaningful content to analyze
        if len(description) > 50 or len(acceptance_criteria) > 30:
            return break_down_work_item_with_llm(task)
    
    # Fall back to simple breakdown
    return break_down_work_item_simple(task)


def classify_task_fe_be(title: str, tags: str = "", area_path: str = "") -> str:
    """Classify a task as frontend, backend, or both based on content."""
    combined = f"{title} {tags} {area_path}".lower()
    
    fe_indicators = ["ui", "frontend", "screen", "component", "angular", "display", 
                     "form", "dialog", "modal", "button", "view", "page", "style", "layout"]
    be_indicators = ["api", "backend", "service", "database", "query", "calculation", 
                     "integration", "sync", "endpoint", "server", "email", "notification", "sql"]
    
    has_fe = any(ind in combined for ind in fe_indicators)
    has_be = any(ind in combined for ind in be_indicators)
    
    if has_fe and has_be:
        return "both"
    elif has_fe:
        return "frontend"
    elif has_be:
        return "backend"
    else:
        return "backend"  # Default to backend


@trace_task("sprint_plan_generation", metadata={"source": "pm_agent"})
def main():
    parser = argparse.ArgumentParser(description="Generate sprint plan with capacity checking")
    parser.add_argument("--sprint", type=str, required=True, help="Sprint name")
    parser.add_argument("--start", type=str, required=True, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", type=str, required=True, help="End date (YYYY-MM-DD)")
    parser.add_argument("--balance", action="store_true", default=True, help="Enable workload balancing")
    args = parser.parse_args()

    print(f"Generating sprint plan: {args.sprint}")
    print(f"Period: {args.start} to {args.end}")
    
    # Print configuration
    if EXCLUDED_DEVELOPERS:
        print(f"[EXCLUDED] Developers: {', '.join(EXCLUDED_DEVELOPERS)}")
    if TASK_BREAKDOWN_ENABLED:
        print(f"[CONFIG] Task breakdown enabled: {COMPLEXITY_BREAKDOWN}")
    
    # Calculate sprint duration
    sprint_start = datetime.strptime(args.start, "%Y-%m-%d")
    sprint_end = datetime.strptime(args.end, "%Y-%m-%d")
    sprint_days = (sprint_end - sprint_start).days
    if sprint_days <= 0:
        sprint_days = 10  # Default to 10 days
    
    print(f"Sprint duration: {sprint_days} days")
    
    # Create output directory
    outputs_dir = ROOT / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)
    data_dir = ROOT / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    
    # Load profiled tasks - fetch from ADO if not cached locally
    tasks_file = data_dir / "wi_tags.json"
    tasks = []
    
    if tasks_file.exists():
        try:
            tasks_data = json.loads(tasks_file.read_text())
            tasks = tasks_data.get("items", [])
            print(f"Loaded {len(tasks)} profiled tasks from cache")
        except Exception as e:
            print(f"Warning: Could not load cached tasks: {e}")
    
    # If no cached tasks, fetch and profile from ADO directly
    if not tasks:
        print("No cached profiled tasks found. Fetching from ADO...")
        area_paths = get_area_paths_from_config()
        print(f"  Area paths: {area_paths}")
        
        try:
            tasks = profile_upcoming_tasks(
                area_paths=area_paths,
                max_wis=200,
                use_llm=False,
            )
            if tasks:
                print(f"  Fetched and profiled {len(tasks)} work items from ADO")
            else:
                print("  No Ready work items found in ADO backlog")
        except Exception as e:
            print(f"  Error fetching from ADO: {e}")
    
    if not tasks:
        print("No tasks found to plan. Check your ADO configuration and area paths.")
        return 1
    
    print(f"Found {len(tasks)} profiled tasks")
    
    # Load developer skills for capacity tracking AND role classification
    developer_capacity = {}  # email -> {total_hours, assigned_hours, tasks, role, evidence}
    developer_roles = {"frontend": [], "backend": [], "fullstack": [], "unknown": []}
    dev_skills_raw = []
    
    skills_file = data_dir / "developer_skills.json"
    
    # Try to load developer skills from cache, or fetch from ADO
    dev_skills_raw = []
    if skills_file.exists():
        try:
            dev_skills_raw = json.loads(skills_file.read_text())
            print(f"Loaded developer skills from cache")
            
            # Deduplicate cached skills by email
            seen_emails = set()
            deduplicated = []
            for dev in dev_skills_raw:
                email = dev.get("developer", "").lower().strip()
                if email and email not in seen_emails:
                    seen_emails.add(email)
                    deduplicated.append(dev)
            
            if len(deduplicated) < len(dev_skills_raw):
                print(f"  Removed {len(dev_skills_raw) - len(deduplicated)} duplicate entries")
                dev_skills_raw = deduplicated
                
        except Exception as e:
            print(f"Warning: Could not load cached developer skills: {e}")
    
    # If no cached skills, fetch developer skills from ADO commit history
    if not dev_skills_raw:
        print("No cached developer skills. Fetching from ADO commit history...")
        dev_skills_raw = fetch_developer_skills_from_ado(days=90)
        
        # If commit analysis failed, fall back to team members
        if not dev_skills_raw:
            print("  Commit analysis unavailable. Fetching team members...")
            dev_skills_raw = fetch_team_members_from_ado()
        
        if dev_skills_raw:
            print(f"  Fetched skills for {len(dev_skills_raw)} developers")
            # Save for future use
            try:
                skills_file.write_text(json.dumps(dev_skills_raw, indent=2))
                print(f"  Saved to {skills_file}")
            except Exception as e:
                print(f"  Warning: Could not save skills file: {e}")
        else:
            print("  Could not fetch developer info. Capacity tracking will be limited.")
    
    if dev_skills_raw:
        max_hours = sprint_days * DEFAULT_HOURS_PER_DAY * DEFAULT_UTILIZATION_TARGET
        
        for dev in dev_skills_raw:
            email = dev.get("developer", "").lower()
            if email:
                # Classify developer role based on commit evidence
                role, evidence = classify_developer_from_commits(dev)
                
                # Check if developer is excluded
                is_excluded = any(
                    excluded in email or email in excluded
                    for excluded in EXCLUDED_DEVELOPERS
                )
                
                if is_excluded:
                    print(f"   [EXCLUDED] Developer: {email}")
                    continue
                
                developer_capacity[email] = {
                    "name": get_developer_name(email),
                    "full_name": get_developer_full_name(email),
                    "total_hours": sprint_days * DEFAULT_HOURS_PER_DAY,
                    "target_hours": max_hours,
                    "assigned_hours": 0,
                    "task_count": 0,
                    "role": role,
                    "evidence": evidence,
                }
                
                # Add to role pools
                developer_roles[role].append(email)
        
        print(f"Loaded capacity for {len(developer_capacity)} developers ({sprint_days * DEFAULT_HOURS_PER_DAY}h each)")
        if EXCLUDED_DEVELOPERS:
            print(f"   [EXCLUDED] Developers: {', '.join(EXCLUDED_DEVELOPERS)}")
        print(f"  [STATS] Role classification from git commits:")
        print(f"     Frontend: {len(developer_roles['frontend'])} developers")
        print(f"     Backend: {len(developer_roles['backend'])} developers")
        print(f"     Fullstack: {len(developer_roles['fullstack'])} developers")
        print(f"     Unknown: {len(developer_roles['unknown'])} developers")
    
    # Load role-based assignment suggestions (preferred)
    role_suggestions = {}
    role_suggestions_file = data_dir / "role_assignment_suggestions.json"
    if role_suggestions_file.exists():
        try:
            sug_data = json.loads(role_suggestions_file.read_text())
            for s in sug_data.get("suggestions", []):
                wi_id = s.get("wi_id")
                if wi_id:
                    role_suggestions[wi_id] = {
                        "frontend": s.get("frontend_suggestions", []),
                        "backend": s.get("backend_suggestions", []),
                    }
            print(f"Loaded role-based suggestions for {len(role_suggestions)} WIs")
        except Exception as e:
            print(f"Warning: Could not load role suggestions: {e}")
    
    # Fallback: Load regular assignment suggestions
    suggestions = {}
    suggestions_file = data_dir / "assignment_suggestions.json"
    if suggestions_file.exists():
        try:
            sug_data = json.loads(suggestions_file.read_text())
            for s in sug_data.get("suggestions", []):
                wi_id = s.get("wi_id")
                if wi_id:
                    suggestions[wi_id] = s.get("suggestions", [])
            print(f"Loaded regular suggestions for {len(suggestions)} WIs")
        except Exception:
            pass
    
    # DYNAMIC ASSIGNMENT: If no pre-computed suggestions exist, generate them dynamically
    dynamic_suggestions = {}
    if not role_suggestions and developer_capacity:
        print("\n[DYNAMIC] No pre-computed suggestions found. Generating dynamic assignments...")
        dynamic_suggestions = generate_dynamic_assignments(tasks, developer_capacity, developer_roles)
        print(f"  Generated dynamic assignments for {len(dynamic_suggestions)} work items")
    
    # Load overrides if available
    overrides_file = data_dir / "sprint_plan_overrides.json"
    overrides = {}
    if overrides_file.exists():
        try:
            overrides = json.loads(overrides_file.read_text())
        except Exception:
            pass
    
    # Helper function to get best available developer considering capacity with balanced distribution
    def get_best_developer(dev_list, role_type):
        """Select best developer from list considering capacity with balanced workload distribution."""
        if not dev_list:
            return ""
        
        # Filter to candidates under capacity and sort by remaining capacity (most available first)
        candidates = []
        for dev_info in dev_list:
            email = dev_info.get("developer", "").lower()
            if email and email in developer_capacity:
                cap = developer_capacity[email]
                remaining = cap["target_hours"] - cap["assigned_hours"]
                if remaining > 0:
                    candidates.append({
                        "email": email,
                        "remaining": remaining,
                        "utilization": cap["assigned_hours"] / cap["total_hours"] if cap["total_hours"] > 0 else 0,
                        "score": dev_info.get("score", 0.5),  # Skill match score
                    })
        
        if not candidates:
            # If all at capacity, return first one anyway (but it will be flagged as overloaded)
            return dev_list[0].get("developer", "").lower() if dev_list else ""
        
        # Sort by: lowest utilization first (balances workload), then by highest skill score
        candidates.sort(key=lambda x: (x["utilization"], -x["score"]))
        return candidates[0]["email"]
    
    def get_least_loaded_developer(role: str) -> str:
        """Get the developer with the most remaining capacity for balanced workload."""
        pool = developer_roles.get(role, []) + developer_roles.get("fullstack", []) + developer_roles.get("unknown", [])
        
        candidates = []
        for email in pool:
            if email in developer_capacity:
                cap = developer_capacity[email]
                remaining = cap["target_hours"] - cap["assigned_hours"]
                if remaining > 0:
                    candidates.append({
                        "email": email,
                        "remaining": remaining,
                        "utilization": cap["assigned_hours"] / cap["total_hours"] if cap["total_hours"] > 0 else 0,
                    })
        
        if not candidates:
            return ""
        
        # Sort by utilization (lowest first = least loaded)
        candidates.sort(key=lambda x: x["utilization"])
        return candidates[0]["email"]
    
    def assign_hours_to_dev(email, hours, wi_id):
        """Track hours assigned to developer."""
        if email and email in developer_capacity:
            developer_capacity[email]["assigned_hours"] += hours
            developer_capacity[email]["task_count"] += 1
    
    # Build sprint plan rows with capacity-aware assignment
    rows = []
    assigned_fe = 0
    assigned_be = 0
    needs_more_info_count = 0
    current_date = sprint_start
    
    # Sort tasks by complexity (larger first for better distribution)
    sorted_tasks = sorted(tasks, key=lambda t: COMPLEXITY_DAYS.get(t.get("complexity", "Medium"), 3), reverse=True)

    for task in sorted_tasks:
        wi_id = task.get("id")
        override = overrides.get(str(wi_id), {})
        complexity = task.get("complexity", "Medium")
        task_hours = COMPLEXITY_HOURS.get(complexity, 24)
        
        # ============================================================
        # CHECK: If work item has no description, mark as "Need more info"
        # Don't break down into tasks or assign developers
        # ============================================================
        if not has_sufficient_description(task):
            needs_more_info_count += 1
            print(f"  [INFO] WI-{wi_id}: No description - marking 'Need more info'")
            
            # Calculate dates for the task
            task_duration = COMPLEXITY_DAYS.get(complexity, 3)
            task_start = current_date
            task_end = current_date + timedelta(days=task_duration)
            if task_end > sprint_end:
                task_end = sprint_end
            current_date = task_start + timedelta(days=1)
            if current_date > sprint_end:
                current_date = sprint_start
            
            # Create row with "Need more info" for assignment fields
            row = {
                "Sprint": args.sprint,
                "WI ID": wi_id,
                "Feature / User Story": task.get("title", ""),
                "Task Name": f"WI-{wi_id}",
                "Subtask Type": "",
                "Responsible - Frontend": "Need more info",
                "Responsible - Backend": "Need more info",
                "FE Email": "",
                "BE Email": "",
                "Start Date": task_start.strftime("%Y-%m-%d"),
                "End Date": task_end.strftime("%Y-%m-%d"),
                "Duration (days)": task_duration,
                "Estimated Hours": task_hours,
                "Status": "Need more info",
                "Priority": override.get("priority", "Medium"),
                "Complexity": complexity,
                "Evidence_FE": "No description provided",
                "Evidence_BE": "No description provided",
                "Assignment_Confidence": "N/A - Need description",
            }
            rows.append(row)
            continue  # Skip to next task
        
        # Get suggested developers
        frontend = override.get("frontend", "")
        backend = override.get("backend", "")
        fe_email = ""
        be_email = ""
        
        # Try role-based suggestions first (with capacity awareness)
        if (not frontend or not backend) and wi_id in role_suggestions:
            role_sug = role_suggestions[wi_id]
            if not frontend and role_sug.get("frontend"):
                fe_email = get_best_developer(role_sug["frontend"], "frontend")
                frontend = get_developer_name(fe_email)
                assigned_fe += 1
            if not backend and role_sug.get("backend"):
                be_email = get_best_developer(role_sug["backend"], "backend")
                backend = get_developer_name(be_email)
                assigned_be += 1
        
        # Try dynamic suggestions if no role suggestions exist
        if (not frontend or not backend) and wi_id in dynamic_suggestions:
            dyn_sug = dynamic_suggestions[wi_id]
            task_role = dyn_sug.get("task_role", "both")
            
            if not frontend and dyn_sug.get("frontend"):
                fe_email = get_best_developer(dyn_sug["frontend"], "frontend")
                if fe_email:
                    frontend = get_developer_name(fe_email)
                    assigned_fe += 1
            
            if not backend and dyn_sug.get("backend"):
                be_email = get_best_developer(dyn_sug["backend"], "backend")
                if be_email:
                    backend = get_developer_name(be_email)
                    assigned_be += 1
        
        # Fallback to regular suggestions with skill-based heuristic
        if (not frontend and not backend) and wi_id in suggestions:
            sug_list = suggestions[wi_id]
            if sug_list:
                first_email = sug_list[0].get("developer", "").lower()
                first_dev = get_developer_name(first_email)
                # Check skills for FE/BE classification
                skills = task.get("inferred_skills", [])
                skill_names = [s.get("skill", "").lower() if isinstance(s, dict) else str(s).lower() for s in skills]
                
                fe_indicators = ["frontend", "react", "angular", "vue", "html", "css", "typescript", "ui", "component"]
                be_indicators = ["backend", "api", "database", "sql", "java", "spring", "node", "python", "server"]
                
                has_fe = any(any(ind in s for ind in fe_indicators) for s in skill_names)
                has_be = any(any(ind in s for ind in be_indicators) for s in skill_names)
                
                if has_fe and not has_be:
                    frontend = first_dev
                    fe_email = first_email
                    assigned_fe += 1
                elif has_be and not has_fe:
                    backend = first_dev
                    be_email = first_email
                    assigned_be += 1
                elif has_fe and has_be:
                    frontend = first_dev
                    backend = first_dev
                    fe_email = be_email = first_email
                    assigned_fe += 1
                    assigned_be += 1
                else:
                    area_path = (task.get("area_path", "") + " " + task.get("title", "")).lower()
                    if any(x in area_path for x in ["ui", "frontend", "screen", "component", "angular"]):
                        frontend = first_dev
                        fe_email = first_email
                        assigned_fe += 1
                    else:
                        backend = first_dev
                        be_email = first_email
                        assigned_be += 1
        
        # Track hours for capacity (split between FE/BE if both assigned)
        if fe_email and be_email and fe_email != be_email:
            assign_hours_to_dev(fe_email, task_hours * 0.5, wi_id)
            assign_hours_to_dev(be_email, task_hours * 0.5, wi_id)
        elif fe_email:
            assign_hours_to_dev(fe_email, task_hours, wi_id)
        elif be_email:
            assign_hours_to_dev(be_email, task_hours, wi_id)
        
        # ENSURE BOTH FE AND BE ARE ASSIGNED WITH BALANCED WORKLOAD
        # If only one role is assigned, find the LEAST LOADED developer for the missing role
        if fe_email and not be_email:
            # Find least loaded BE developer for balanced workload
            be_email = get_least_loaded_developer("backend")
            if be_email and be_email != fe_email:
                backend = get_developer_name(be_email)
                assign_hours_to_dev(be_email, task_hours * 0.5, wi_id)
                assigned_be += 1
        
        if be_email and not fe_email:
            # Find least loaded FE developer for balanced workload
            fe_email = get_least_loaded_developer("frontend")
            if fe_email and fe_email != be_email:
                frontend = get_developer_name(fe_email)
                assign_hours_to_dev(fe_email, task_hours * 0.5, wi_id)
                assigned_fe += 1
        
        # FINAL FALLBACK: If still no developers assigned, pick from least loaded
        if not fe_email and not be_email and developer_capacity:
            # Get all available developers sorted by utilization (least loaded first for balanced distribution)
            available_devs = [
                (email, cap["target_hours"] - cap["assigned_hours"], cap["assigned_hours"] / cap["total_hours"] if cap["total_hours"] > 0 else 0)
                for email, cap in developer_capacity.items()
                if cap["target_hours"] > cap["assigned_hours"]
            ]
            # Sort by utilization (lowest first = least loaded = most available)
            available_devs.sort(key=lambda x: x[2])
            
            if len(available_devs) >= 2:
                # Assign two least loaded developers for balanced distribution
                fe_email = available_devs[0][0]
                be_email = available_devs[1][0]
                frontend = get_developer_name(fe_email)
                backend = get_developer_name(be_email)
                assign_hours_to_dev(fe_email, task_hours * 0.5, wi_id)
                assign_hours_to_dev(be_email, task_hours * 0.5, wi_id)
                assigned_fe += 1
                assigned_be += 1
            elif len(available_devs) == 1:
                # Assign same developer to both roles
                fe_email = be_email = available_devs[0][0]
                frontend = backend = get_developer_name(fe_email)
                assign_hours_to_dev(fe_email, task_hours, wi_id)
                assigned_fe += 1
                assigned_be += 1
        
        # Get evidence for assigned developers
        fe_evidence = ""
        be_evidence = ""
        if fe_email and fe_email in developer_capacity:
            fe_evidence = get_evidence_summary(developer_capacity[fe_email].get("evidence", {}), "frontend")
        if be_email and be_email in developer_capacity:
            be_evidence = get_evidence_summary(developer_capacity[be_email].get("evidence", {}), "backend")
        
        # Calculate assignment confidence
        confidence = "High"
        if not fe_email or not be_email:
            confidence = "Low"
        elif fe_email == be_email:
            confidence = "Medium"  # Same person for both roles
        
        # Calculate staggered dates based on complexity
        complexity = task.get("complexity", "Medium")
        task_duration = COMPLEXITY_DAYS.get(complexity, 3)
        
        # Calculate start and end dates for this task
        task_start = current_date
        task_end = current_date + timedelta(days=task_duration)
        
        # Make sure we don't exceed sprint end
        if task_end > sprint_end:
            task_end = sprint_end
            task_duration = (task_end - task_start).days
        
        # Move current_date forward for next task (with some overlap allowed)
        current_date = task_start + timedelta(days=1)
        if current_date > sprint_end:
            current_date = sprint_start  # Loop back to allow concurrent assignments
        
        # Use full names for display
        fe_full_name = get_developer_full_name(fe_email) if fe_email else ""
        be_full_name = get_developer_full_name(be_email) if be_email else ""
        
        # Break down work item into subtasks if enabled
        subtasks = break_down_work_item(task)
        
        for subtask in subtasks:
            is_subtask = subtask.get("is_subtask", False)
            subtask_type = subtask.get("subtask_type", "")
            subtask_hours = subtask.get("subtask_hours", task_hours) if is_subtask else task_hours
            subtask_index = subtask.get("subtask_index", 0)
            
            # For LLM-generated subtasks, use the specific role assignment
            # Each subtask has its own FE/BE classification
            subtask_role = subtask.get("subtask_role", "")
            if subtask_role and is_subtask:
                # LLM broke this down with specific role - assign only that role's developer
                if subtask_role == "frontend":
                    subtask_fe_name = fe_full_name
                    subtask_be_name = ""  # Not a backend task
                    subtask_fe_email = fe_email
                    subtask_be_email = ""
                else:  # backend
                    subtask_fe_name = ""  # Not a frontend task
                    subtask_be_name = be_full_name
                    subtask_fe_email = ""
                    subtask_be_email = be_email
            else:
                # Non-LLM breakdown or full task - use both roles
                subtask_fe_name = fe_full_name
                subtask_be_name = be_full_name
                subtask_fe_email = fe_email
                subtask_be_email = be_email
            
            # Create task name with subtask info
            if is_subtask:
                task_name = f"WI-{wi_id}.{subtask_index}: {subtask_type}"
            else:
                task_name = f"WI-{wi_id}"
            
            row = {
                "Sprint": args.sprint,
                "WI ID": wi_id,
                "Feature / User Story": task.get("title", ""),
                "Task Name": task_name,
                "Subtask Type": subtask_type if is_subtask else "",
                "Responsible - Frontend": subtask_fe_name,
                "Responsible - Backend": subtask_be_name,
                "FE Email": subtask_fe_email,
                "BE Email": subtask_be_email,
                "Start Date": task_start.strftime("%Y-%m-%d"),
                "End Date": task_end.strftime("%Y-%m-%d"),
                "Duration (days)": task_duration,
                "Estimated Hours": subtask_hours,
                "Status": override.get("status", "Not Started"),
                "Priority": override.get("priority", "Medium"),
                "Complexity": complexity,
                "Evidence_FE": fe_evidence if subtask_fe_email else "",
                "Evidence_BE": be_evidence if subtask_be_email else "",
                "Assignment_Confidence": confidence,
            }
            rows.append(row)
    
    # ========================================================
    # BACKLOG ITEMS SECTION - Fetch from ADO and assign
    # ========================================================
    backlog_rows = []
    print("\n" + "=" * 60)
    print("[BACKLOG] FETCHING BACKLOG ITEMS (XOPS Bugs Enhancement)")
    print("=" * 60)
    
    try:
        backlog_items = fetch_backlog_items(BACKLOG_AREA_PATHS, max_items=50)
        
        if backlog_items:
            print(f"Processing {len(backlog_items)} backlog items...")
            
            # Build separate pools for FE and BE developers with remaining capacity
            fe_available = []
            be_available = []
            
            for email, cap in developer_capacity.items():
                remaining = cap["target_hours"] - cap["assigned_hours"]
                if remaining > 0:
                    dev_info = {
                        "email": email,
                        "name": cap["name"],
                        "remaining_hours": remaining,
                        "assigned_hours": cap["assigned_hours"],
                        "utilization": cap["assigned_hours"] / cap["total_hours"] if cap["total_hours"] > 0 else 0,
                        "role": cap.get("role", "unknown"),
                        "evidence": cap.get("evidence", {}),
                    }
                    
                    role = cap.get("role", "unknown")
                    if role == "frontend":
                        fe_available.append(dev_info)
                    elif role == "backend":
                        be_available.append(dev_info)
                    elif role == "fullstack":
                        # Fullstack developers can be added to both pools
                        fe_available.append(dev_info.copy())
                        be_available.append(dev_info.copy())
                    # Skip unknown role developers for now
            
            # Sort by utilization (assign to least utilized first)
            fe_available.sort(key=lambda x: x["utilization"])
            be_available.sort(key=lambda x: x["utilization"])
            
            print(f"  Available FE developers: {len(fe_available)}")
            print(f"  Available BE developers: {len(be_available)}")
            
            backlog_assigned = 0
            backlog_skipped = 0
            backlog_fe_assigned = 0
            backlog_be_assigned = 0
            backlog_needs_info = 0
            
            for item in backlog_items:
                wi_id = item.get("id")
                title = item.get("title", "")
                complexity = item.get("complexity", "Medium")
                task_hours = COMPLEXITY_HOURS.get(complexity, 24)
                
                # ============================================================
                # CHECK: If backlog item has no description, mark as "Need more info"
                # ============================================================
                if not has_sufficient_description(item):
                    backlog_needs_info += 1
                    print(f"  [INFO] WI-{wi_id}: No description - marking 'Need more info'")
                    
                    # Calculate dates for the task
                    task_duration = COMPLEXITY_DAYS.get(complexity, 3)
                    task_start = current_date
                    task_end = current_date + timedelta(days=task_duration)
                    if task_end > sprint_end:
                        task_end = sprint_end
                    current_date = task_start + timedelta(days=1)
                    if current_date > sprint_end:
                        current_date = sprint_start
                    
                    # Create backlog row with "Need more info"
                    backlog_row = {
                        "Sprint": args.sprint,
                        "Feature / User Story": title,
                        "Task Name": f"WI-{wi_id}",
                        "Responsible - Frontend": "Need more info",
                        "Responsible - Backend": "Need more info",
                        "Evidence_FE": "No description provided",
                        "Evidence_BE": "No description provided",
                        "Start Date": task_start.strftime("%Y-%m-%d"),
                        "End Date": task_end.strftime("%Y-%m-%d"),
                        "Duration (days)": task_duration,
                        "Estimated Hours": task_hours,
                        "Status": "Need more info",
                        "Priority": item.get("priority", "Medium"),
                        "Complexity": complexity,
                        "Section": "Backlog Items",
                        "FE_Email": "",
                        "BE_Email": "",
                    }
                    backlog_rows.append(backlog_row)
                    continue  # Skip to next backlog item
                
                # Determine if this is a FE or BE task based on title and tags
                title_lower = title.lower()
                tags = (item.get("tags", "") + " " + item.get("area_path", "") + " " + title).lower()
                
                fe_indicators = ["ui", "frontend", "screen", "component", "angular", "display", "form", "dialog", "modal", "button", "view", "page"]
                be_indicators = ["api", "backend", "service", "database", "query", "calculation", "integration", "sync", "endpoint", "server"]
                
                is_fe_task = any(ind in tags for ind in fe_indicators)
                is_be_task = any(ind in tags for ind in be_indicators)
                
                # Default to backend if unclear (most bugs are backend related)
                if not is_fe_task and not is_be_task:
                    is_be_task = True
                
                frontend = ""
                backend = ""
                assigned_dev = None
                
                # Try to assign based on task type
                if is_fe_task and not is_be_task:
                    # Pure FE task - find FE developer
                    for dev in fe_available:
                        if dev["remaining_hours"] >= task_hours:
                            assigned_dev = dev
                            frontend = dev["name"]
                            break
                elif is_be_task and not is_fe_task:
                    # Pure BE task - find BE developer first
                    for dev in be_available:
                        if dev["remaining_hours"] >= task_hours:
                            assigned_dev = dev
                            backend = dev["name"]
                            break
                    
                    # CROSS-ROLE FALLBACK: Only if enabled in config AND FE developer has strong BE evidence
                    if not assigned_dev and ALLOW_CROSS_ROLE:
                        for dev in fe_available:
                            if dev["remaining_hours"] >= task_hours:
                                # Check if this FE dev has sufficient BE evidence
                                evidence = dev.get("evidence", {})
                                be_score = evidence.get("be_score", 0)
                                be_skills = evidence.get("be_languages", [])
                                
                                # Only allow if BE score meets threshold or has BE languages
                                if be_score >= CROSS_ROLE_EVIDENCE_THRESHOLD or len(be_skills) >= 1:
                                    assigned_dev = dev
                                    backend = dev["name"]
                                    print(f"  [INFO] Cross-role: FE dev {dev['name']} assigned to BE task (BE score: {be_score:.0%}, skills: {be_skills})")
                                    break
                        
                        # If still no assignment and cross-role is off, just skip
                        if not assigned_dev:
                            print(f"  [WARN] WI-{wi_id}: Skipped (no BE capacity, cross-role disabled or insufficient evidence)")
                else:
                    # Needs both FE and BE - split the work
                    fe_dev = None
                    be_dev = None
                    half_hours = task_hours * 0.5
                    
                    for dev in fe_available:
                        if dev["remaining_hours"] >= half_hours:
                            fe_dev = dev
                            break
                    
                    for dev in be_available:
                        if dev["remaining_hours"] >= half_hours:
                            be_dev = dev
                            break
                    
                    if fe_dev and be_dev:
                        frontend = fe_dev["name"]
                        backend = be_dev["name"]
                        
                        # Update FE dev capacity
                        fe_dev["remaining_hours"] -= half_hours
                        fe_dev["assigned_hours"] += half_hours
                        fe_dev["utilization"] = fe_dev["assigned_hours"] / developer_capacity[fe_dev["email"]]["total_hours"]
                        developer_capacity[fe_dev["email"]]["assigned_hours"] += half_hours
                        developer_capacity[fe_dev["email"]]["task_count"] += 1
                        backlog_fe_assigned += 1
                        
                        # Update BE dev capacity
                        be_dev["remaining_hours"] -= half_hours
                        be_dev["assigned_hours"] += half_hours
                        be_dev["utilization"] = be_dev["assigned_hours"] / developer_capacity[be_dev["email"]]["total_hours"]
                        developer_capacity[be_dev["email"]]["assigned_hours"] += half_hours
                        developer_capacity[be_dev["email"]]["task_count"] += 1
                        backlog_be_assigned += 1
                        
                        assigned_dev = {"name": f"{frontend}/{backend}"}  # For logging
                
                # If we found a single developer, update their capacity
                if assigned_dev and not frontend and not backend:
                    # This shouldn't happen, but just in case
                    pass
                elif assigned_dev and (frontend or backend) and not (frontend and backend):
                    # Single developer assigned
                    email = None
                    for dev in (fe_available + be_available):
                        if dev["name"] == (frontend or backend):
                            email = dev["email"]
                            dev["remaining_hours"] -= task_hours
                            dev["assigned_hours"] += task_hours
                            dev["utilization"] = dev["assigned_hours"] / developer_capacity[email]["total_hours"]
                            break
                    
                    if email:
                        developer_capacity[email]["assigned_hours"] += task_hours
                        developer_capacity[email]["task_count"] += 1
                        if frontend:
                            backlog_fe_assigned += 1
                        else:
                            backlog_be_assigned += 1
                
                if not assigned_dev and not (frontend and backend):
                    # No developer has enough capacity
                    backlog_skipped += 1
                    task_type = "FE" if is_fe_task else "BE" if is_be_task else "FE+BE"
                    print(f"  [WARN] WI-{wi_id}: Skipped ({task_type} task, no capacity, needs {task_hours}h)")
                    continue
                
                backlog_assigned += 1
                
                # Use current_date for backlog items too (staggered)
                task_start = current_date
                task_duration = COMPLEXITY_DAYS.get(complexity, 3)
                task_end = task_start + timedelta(days=task_duration)
                
                if task_end > sprint_end:
                    task_end = sprint_end
                
                # Move date forward
                current_date = task_start + timedelta(days=1)
                if current_date > sprint_end:
                    current_date = sprint_start
                
                # ============================================================
                # BACKLOG: Ensure BOTH FE and BE are assigned (same as profiled tasks)
                # ============================================================
                fe_email = None
                be_email = None
                fe_evidence_str = ""
                be_evidence_str = ""
                
                # Find the FE developer email
                if frontend:
                    for dev in fe_available:
                        if dev["name"] == frontend:
                            fe_email = dev.get("email", "")
                            evidence = dev.get("evidence", {})
                            fe_langs = evidence.get("fe_languages", [])[:3]
                            fe_score = evidence.get("fe_score", 0)
                            fe_evidence_str = f"FE:{fe_score} ({', '.join(fe_langs)})" if fe_langs else f"FE:{fe_score}"
                            break
                
                # Find the BE developer email
                if backend:
                    for dev in be_available + fe_available:  # Check both pools
                        if dev["name"] == backend:
                            be_email = dev.get("email", "")
                            evidence = dev.get("evidence", {})
                            be_langs = evidence.get("be_languages", [])[:3]
                            be_score = evidence.get("be_score", 0)
                            be_evidence_str = f"BE:{be_score} ({', '.join(be_langs)})" if be_langs else f"BE:{be_score}"
                            break
                
                # If missing FE, find one from the pool
                if not frontend and fe_available:
                    for dev in fe_available:
                        if dev["remaining_hours"] >= task_hours * 0.3:  # Need at least 30% capacity
                            frontend = dev["name"]
                            fe_email = dev.get("email", "")
                            evidence = dev.get("evidence", {})
                            fe_langs = evidence.get("fe_languages", [])[:3]
                            fe_score = evidence.get("fe_score", 0)
                            fe_evidence_str = f"FE:{fe_score} ({', '.join(fe_langs)})" if fe_langs else f"FE:{fe_score}"
                            # Don't update capacity here, already handled above
                            break
                
                # If missing BE, find one from the pool
                if not backend and be_available:
                    for dev in be_available:
                        if dev["remaining_hours"] >= task_hours * 0.3:
                            backend = dev["name"]
                            be_email = dev.get("email", "")
                            evidence = dev.get("evidence", {})
                            be_langs = evidence.get("be_languages", [])[:3]
                            be_score = evidence.get("be_score", 0)
                            be_evidence_str = f"BE:{be_score} ({', '.join(be_langs)})" if be_langs else f"BE:{be_score}"
                            break
                
                # Convert to full names
                frontend_full = get_developer_full_name(fe_email) if fe_email else frontend
                backend_full = get_developer_full_name(be_email) if be_email else backend
                
                # Get role info for logging
                role_info = []
                if frontend_full:
                    role_info.append(f"FE:{frontend_full}")
                if backend_full:
                    role_info.append(f"BE:{backend_full}")
                
                backlog_row = {
                    "Sprint": args.sprint,
                    "Feature / User Story": item.get("title", ""),
                    "Task Name": f"WI-{wi_id}",
                    "Responsible - Frontend": frontend_full,
                    "Responsible - Backend": backend_full,
                    "Evidence_FE": fe_evidence_str,
                    "Evidence_BE": be_evidence_str,
                    "Start Date": task_start.strftime("%Y-%m-%d"),
                    "End Date": task_end.strftime("%Y-%m-%d"),
                    "Duration (days)": task_duration,
                    "Estimated Hours": task_hours,
                    "Status": "Not Started",
                    "Priority": item.get("priority", "Medium"),
                    "Complexity": complexity,
                    "Section": "Backlog Items",  # Mark as backlog
                    "FE_Email": fe_email or "",
                    "BE_Email": be_email or "",
                }
                backlog_rows.append(backlog_row)
                
                print(f"  [OK] WI-{wi_id}: {', '.join(role_info)} ({task_hours}h)")
            
            print(f"\n[STATS] Backlog Assignment Summary:")
            print(f"   Total assigned: {backlog_assigned}")
            print(f"   Need more info: {backlog_needs_info}")
            print(f"   FE assignments: {backlog_fe_assigned}")
            print(f"   BE assignments: {backlog_be_assigned}")
            print(f"   Skipped (no capacity): {backlog_skipped}")
        else:
            print("No backlog items found (or ADO not configured)")
    except Exception as e:
        import traceback
        print(f"Warning: Error fetching backlog items: {e}")
        traceback.print_exc()
    
    # ========================================================
    # Write CSV with both profiled tasks and backlog items
    # ========================================================
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = outputs_dir / f"sprint_plan_{timestamp}.csv"
    
    # Add Section column to regular rows
    for row in rows:
        row["Section"] = "Profiled Tasks"
    
    # Combine all rows
    all_rows = rows + backlog_rows
    
    # Define explicit fieldnames to ensure consistent column order
    fieldnames = [
        "Section",
        "Sprint",
        "Feature / User Story",
        "Task Name",
        "Subtask Type",
        "Responsible - Frontend",
        "Responsible - Backend",
        "Evidence_FE",
        "Evidence_BE",
        "Start Date",
        "End Date",
        "Duration (days)",
        "Estimated Hours",
        "Status",
        "Priority",
        "Complexity",
        "FE_Email",
        "BE_Email",
    ]
    
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(all_rows)
    
    print(f"\nSprint plan saved to {csv_path}")
    print(f"Total tasks: {len(all_rows)} (Profiled: {len(rows)}, Backlog: {len(backlog_rows)})")
    print(f"Frontend assignments: {assigned_fe}")
    print(f"Backend assignments: {assigned_be}")
    print(f"Need more info (no description): {needs_more_info_count}")
    
    # Print capacity report
    print("\n" + "=" * 60)
    print("[STATS] CAPACITY REPORT")
    print("=" * 60)
    
    # Calculate and display capacity summary
    overloaded = []
    warning = []
    optimal = []
    underutilized = []
    
    for email, cap in sorted(developer_capacity.items(), key=lambda x: -x[1]["assigned_hours"]):
        if cap["assigned_hours"] == 0:
            continue  # Skip developers with no assignments
        
        util = cap["assigned_hours"] / cap["total_hours"] if cap["total_hours"] > 0 else 0
        status_emoji = ""
        
        if util >= 1.0:
            overloaded.append((email, cap, util))
            status_emoji = "[!!]"
        elif util >= 0.85:
            warning.append((email, cap, util))
            status_emoji = "[!]"
        elif util >= 0.50:
            optimal.append((email, cap, util))
            status_emoji = "[+]"
        else:
            underutilized.append((email, cap, util))
            status_emoji = "[-]"
        
        print(f"{status_emoji} {cap['name']}: {cap['assigned_hours']:.0f}h / {cap['total_hours']:.0f}h ({util:.0%}) - {cap['task_count']} tasks")
    
    print()
    print(f"Summary: [!!] Overloaded: {len(overloaded)} | [!] Warning: {len(warning)} | [+] Optimal: {len(optimal)} | [-] Underutilized: {len(underutilized)}")
    
    # Save capacity report
    capacity_report = {
        "generated_at": datetime.now().isoformat(),
        "sprint_name": args.sprint,
        "sprint_days": sprint_days,
        "hours_per_day": DEFAULT_HOURS_PER_DAY,
        "total_tasks": len(all_rows),
        "profiled_tasks": len(rows),
        "backlog_tasks": len(backlog_rows),
        "developers": {
            email: {
                **cap,
                "utilization": cap["assigned_hours"] / cap["total_hours"] if cap["total_hours"] > 0 else 0,
                "status": "overloaded" if cap["assigned_hours"] / cap["total_hours"] >= 1.0 else 
                         "warning" if cap["assigned_hours"] / cap["total_hours"] >= 0.85 else
                         "optimal" if cap["assigned_hours"] / cap["total_hours"] >= 0.50 else "underutilized"
            }
            for email, cap in developer_capacity.items()
            if cap["assigned_hours"] > 0
        },
        "summary": {
            "overloaded_count": len(overloaded),
            "overloaded_developers": [e for e, _, _ in overloaded],
            "warning_count": len(warning),
            "optimal_count": len(optimal),
            "underutilized_count": len(underutilized),
        }
    }
    
    # Suggest redistributions if there are overloads
    if overloaded and underutilized:
        print("\n[HINT] REDISTRIBUTION SUGGESTIONS:")
        for over_email, over_cap, over_util in overloaded:
            excess = over_cap["assigned_hours"] - over_cap["target_hours"]
            for under_email, under_cap, under_util in underutilized:
                available = under_cap["target_hours"] - under_cap["assigned_hours"]
                if available > 0:
                    movable = min(excess, available)
                    print(f"   -> Move ~{movable:.0f}h from {over_cap['name']} ({over_util:.0%}) to {under_cap['name']} ({under_util:.0%})")
                    break
        
        capacity_report["redistribution_needed"] = True
    
    capacity_path = outputs_dir / f"capacity_report_{timestamp}.json"
    with capacity_path.open("w", encoding="utf-8") as f:
        json.dump(capacity_report, f, indent=2)
    print(f"\nCapacity report saved to {capacity_path}")
    
    # Try to create Excel file too
    try:
        import pandas as pd
        df = pd.DataFrame(all_rows)
        xlsx_path = csv_path.with_suffix(".xlsx")
        df.to_excel(xlsx_path, index=False)
        print(f"Excel file saved to {xlsx_path}")
    except ImportError:
        print("Note: openpyxl not available, Excel export skipped")
    except Exception as e:
        print(f"Warning: Could not create Excel file: {e}")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
