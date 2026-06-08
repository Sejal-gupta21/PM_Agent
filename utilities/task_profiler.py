"""Task Profiler for Sprint Planning.

Identifies upcoming work items (not in any sprint, State=Ready) and tags them
with skills, area/module, and complexity for sprint planning.

Also supports profiling tasks from report generator output for compatibility.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

logger = logging.getLogger("pm_agent.task_profiler")

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"
OUTPUTS_DIR = REPO_ROOT / "outputs"
WI_TAGS_FILE = DATA_DIR / "wi_tags.json"

# Skill keywords to detect from text (lowercase)
SKILL_KEYWORDS = {
    # Frontend
    "react": ["react", "reactjs", "react.js", "jsx", "tsx"],
    "angular": ["angular", "angularjs", "ng-"],
    "vue": ["vue", "vuejs", "vue.js"],
    "typescript": ["typescript", "ts", ".ts", ".tsx"],
    "javascript": ["javascript", "js", ".js", "node", "nodejs"],
    "html": ["html", "html5", "markup"],
    "css": ["css", "scss", "sass", "less", "tailwind", "bootstrap"],
    # Backend
    "java": ["java", "spring", "springboot", "maven", "gradle"],
    "python": ["python", "django", "flask", "fastapi", ".py"],
    "csharp": ["c#", "csharp", ".net", "dotnet", "asp.net"],
    "nodejs": ["node", "express", "nestjs"],
    "go": ["golang", "go "],
    # Database
    "sql": ["sql", "mysql", "postgresql", "postgres", "mssql", "oracle", "database", "db"],
    "mongodb": ["mongodb", "mongo", "nosql"],
    "redis": ["redis", "cache"],
    # DevOps / Cloud
    "docker": ["docker", "container", "dockerfile"],
    "kubernetes": ["kubernetes", "k8s", "helm"],
    "azure": ["azure", "az ", "blob", "cosmos"],
    "aws": ["aws", "s3", "lambda", "ec2"],
    # Mobile
    "android": ["android", "kotlin"],
    "ios": ["ios", "swift", "objective-c"],
    # Testing
    "testing": ["test", "unit test", "integration test", "e2e", "cypress", "jest", "pytest"],
    # API
    "api": ["api", "rest", "graphql", "endpoint", "swagger"],
    # UI/UX
    "ui": ["ui", "ux", "frontend", "interface", "component", "screen", "form", "input"],
}

# Complexity thresholds
COMPLEXITY_RULES = {
    "hours": {
        "Small": (0, 4),
        "Medium": (4, 16),
        "Large": (16, 40),
        "XLarge": (40, float("inf")),
    },
    "story_points": {
        "Small": (0, 2),
        "Medium": (3, 5),
        "Large": (6, 13),
        "XLarge": (14, float("inf")),
    },
}


def _ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)


def _get_ado_headers(pat: str) -> Dict[str, str]:
    """Build authorization headers for ADO REST API."""
    auth = base64.b64encode(f":{pat}".encode()).decode()
    return {
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/json",
    }


def fetch_upcoming_wis(
    area_paths: List[str],
    org_url: Optional[str] = None,
    project: Optional[str] = None,
    pat: Optional[str] = None,
    max_wis: int = 500,
) -> List[Dict[str, Any]]:
    """Fetch upcoming work items: State=Ready, not in any iteration, matching area paths.
    
    Returns list of work item dicts with fields.
    """
    from config import config as app_config
    org_url = org_url or app_config.ado_org_url
    project = project or app_config.ado_project
    pat = pat or app_config.ado_pat
    
    if not org_url or not project or not pat:
        raise RuntimeError("ADO credentials not configured (ADO_ORG_URL, ADO_PROJECT, ADO_PAT)")
    
    headers = _get_ado_headers(pat)
    
    # Build WIQL query for Ready items not in any iteration
    area_conditions = " OR ".join([f"[System.AreaPath] UNDER '{ap}'" for ap in area_paths])
    
    wiql = f"""
    SELECT [System.Id]
    FROM WorkItems
    WHERE [System.TeamProject] = '{project}'
      AND [System.State] = 'Ready'
      AND ([System.IterationPath] = '{project}' OR [System.IterationPath] = '')
      AND ({area_conditions})
    ORDER BY [System.CreatedDate] DESC
    """
    
    # Query work item IDs
    wiql_url = f"{org_url}/{project}/_apis/wit/wiql?api-version=7.0&$top={max_wis}"
    
    try:
        resp = requests.post(wiql_url, headers=headers, json={"query": wiql}, timeout=60)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        logger.error("WIQL query failed: %s", e)
        raise
    
    wi_ids = [item["id"] for item in data.get("workItems", [])]
    
    if not wi_ids:
        logger.info("No upcoming work items found matching criteria")
        return []
    
    logger.info("Found %d upcoming work items", len(wi_ids))
    
    # Fetch work item details in batches of 200
    all_wis = []
    batch_size = 200
    
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
        "Microsoft.VSTS.Common.AcceptanceCriteria",
    ]
    
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
            logger.warning("Failed to fetch batch %d-%d: %s", i, i + batch_size, e)
            continue
        
        # Rate limiting
        if i + batch_size < len(wi_ids):
            time.sleep(0.2)
    
    return all_wis


def normalize_wi_fields(wi: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize work item fields to a flat structure."""
    fields = wi.get("fields", {})
    
    assigned_to = fields.get("System.AssignedTo")
    if isinstance(assigned_to, dict):
        assigned_to = assigned_to.get("displayName") or assigned_to.get("uniqueName", "")
    
    return {
        "id": wi.get("id"),
        "title": fields.get("System.Title", ""),
        "state": fields.get("System.State", ""),
        "area_path": fields.get("System.AreaPath", ""),
        "iteration_path": fields.get("System.IterationPath", ""),
        "work_item_type": fields.get("System.WorkItemType", ""),
        "description": fields.get("System.Description", ""),
        "assigned_to": assigned_to or "",
        "created_date": fields.get("System.CreatedDate", ""),
        "changed_date": fields.get("System.ChangedDate", ""),
        "tags": fields.get("System.Tags", ""),
        "remaining_work": fields.get("Microsoft.VSTS.Scheduling.RemainingWork"),
        "story_points": fields.get("Microsoft.VSTS.Scheduling.StoryPoints"),
        "priority": fields.get("Microsoft.VSTS.Common.Priority"),
        "acceptance_criteria": fields.get("Microsoft.VSTS.Common.AcceptanceCriteria", ""),
    }


def infer_skills(text: str, use_llm: bool = False) -> List[Dict[str, Any]]:
    """Infer skill tokens from text.
    
    Returns list of dicts with skill, confidence, evidence.
    """
    if not text:
        return []
    
    text_lower = text.lower()
    detected = []
    
    for skill, keywords in SKILL_KEYWORDS.items():
        for kw in keywords:
            if kw in text_lower:
                # Find evidence snippet
                idx = text_lower.find(kw)
                start = max(0, idx - 20)
                end = min(len(text), idx + len(kw) + 20)
                evidence = text[start:end].strip()
                
                detected.append({
                    "skill": skill,
                    "confidence": 0.8,
                    "evidence": f"...{evidence}...",
                    "matched_keyword": kw,
                })
                break  # Only one match per skill
    
    # Optional LLM enhancement
    from config import config as app_config
    if use_llm and app_config.openai_api_key:
        try:
            llm_skills = _infer_skills_llm(text)
            existing_skills = {s["skill"] for s in detected}
            for llm_skill in llm_skills:
                if llm_skill["skill"] not in existing_skills:
                    detected.append(llm_skill)
        except Exception as e:
            logger.warning("LLM skill inference failed: %s", e)
    
    # Sort by confidence and limit to top 5
    detected.sort(key=lambda x: x["confidence"], reverse=True)
    return detected[:5]


def _infer_skills_llm(text: str) -> List[Dict[str, Any]]:
    """Use LLM to infer skills from text."""
    try:
        import openai
        from config import config as app_config
        openai.api_key = app_config.openai_api_key
        
        prompt = f"""Analyze the following work item text and identify the technical skills/technologies required.
Return a JSON array of objects with: skill (string), confidence (0-1), evidence (brief quote from text).
Only include clearly relevant technical skills.

Text:
{text[:2000]}

Return only valid JSON array, no markdown."""

        response = openai.ChatCompletion.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=500,
            temperature=0.3,
        )
        content = response.choices[0].message.content.strip()
        if content.startswith("["):
            return json.loads(content)
    except Exception as e:
        logger.warning("LLM call failed: %s", e)
    
    return []


def infer_area(wi: Dict[str, Any]) -> Dict[str, Any]:
    """Infer area/module from work item."""
    area_path = wi.get("area_path", "")
    title = wi.get("title", "")
    
    # Extract module from area path
    parts = area_path.split("\\") if "\\" in area_path else area_path.split("/")
    module = parts[-1] if len(parts) > 1 else parts[0] if parts else "Unknown"
    
    # Try to extract feature area from title
    feature_patterns = [
        r"(Live\+)", r"(Reporting)", r"(Admin)", r"(Dashboard)",
        r"(API)", r"(Mobile)", r"(Integration)", r"(Auth)",
        r"(User)", r"(Settings)", r"(Input)", r"(Screen)",
    ]
    
    feature = None
    for pattern in feature_patterns:
        match = re.search(pattern, title, re.IGNORECASE)
        if match:
            feature = match.group(1)
            break
    
    return {
        "area": area_path,
        "module": module,
        "feature": feature or module,
        "confidence": 0.9 if feature else 0.7,
        "evidence": f"Area path: {area_path}, Title contains: {feature or 'N/A'}",
    }


def estimate_complexity(
    wi: Dict[str, Any],
    rules: Optional[Dict] = None,
) -> Dict[str, Any]:
    """Estimate complexity bucket from work item."""
    rules = rules or COMPLEXITY_RULES
    
    remaining_work = wi.get("remaining_work")
    story_points = wi.get("story_points")
    description = wi.get("description", "") or ""
    title = wi.get("title", "") or ""
    
    complexity = "Medium"
    confidence = 0.5
    evidence = "Default estimate"
    source = "default"
    
    # Try remaining work (hours) first
    if remaining_work is not None and remaining_work > 0:
        hours = float(remaining_work)
        for bucket, (low, high) in rules["hours"].items():
            if low <= hours < high:
                complexity = bucket
                confidence = 0.9
                evidence = f"Remaining Work: {hours}h"
                source = "remaining_work"
                break
    
    # Try story points if no hours
    elif story_points is not None and story_points > 0:
        sp = float(story_points)
        for bucket, (low, high) in rules["story_points"].items():
            if low <= sp <= high:
                complexity = bucket
                confidence = 0.85
                evidence = f"Story Points: {sp}"
                source = "story_points"
                break
    
    # Heuristic from text length and keywords
    else:
        text_len = len(description) + len(title)
        complexity_keywords = {
            "Small": ["minor", "simple", "quick", "typo", "small", "fix"],
            "Medium": ["update", "modify", "change", "add", "implement"],
            "Large": ["refactor", "redesign", "major", "complex", "integration"],
            "XLarge": ["migrate", "overhaul", "rewrite", "architecture", "platform"],
        }
        
        text_lower = (description + " " + title).lower()
        
        for bucket, keywords in complexity_keywords.items():
            if any(kw in text_lower for kw in keywords):
                complexity = bucket
                confidence = 0.6
                evidence = f"Keyword match in text"
                source = "heuristic"
                break
        
        if source == "default":
            if text_len < 100:
                complexity = "Small"
            elif text_len < 500:
                complexity = "Medium"
            elif text_len < 1500:
                complexity = "Large"
            else:
                complexity = "XLarge"
            evidence = f"Text length: {text_len} chars"
            source = "text_length"
            confidence = 0.4
    
    return {
        "complexity": complexity,
        "confidence": confidence,
        "evidence": evidence,
        "source": source,
    }


def profile_work_item(wi: Dict[str, Any], use_llm: bool = False) -> Dict[str, Any]:
    """Profile a single work item with skills, area, and complexity."""
    normalized = normalize_wi_fields(wi)
    
    # Combine text sources for skill inference
    text = " ".join(filter(None, [
        normalized.get("title", ""),
        normalized.get("description", ""),
        normalized.get("acceptance_criteria", ""),
        normalized.get("tags", ""),
    ]))
    
    skills = infer_skills(text, use_llm=use_llm)
    area_info = infer_area(normalized)
    complexity_info = estimate_complexity(normalized)
    
    return {
        **normalized,
        "inferred_skills": skills,
        "area_info": area_info,
        "complexity": complexity_info["complexity"],
        "complexity_confidence": complexity_info["confidence"],
        "complexity_evidence": complexity_info["evidence"],
        "complexity_source": complexity_info["source"],
        "profiled_at": datetime.now(timezone.utc).isoformat(),
    }


def load_wi_tags() -> Dict[str, Dict[str, Any]]:
    """Load existing WI tags from data/wi_tags.json."""
    if not WI_TAGS_FILE.exists():
        return {}
    try:
        with WI_TAGS_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
            return {str(item["id"]): item for item in data.get("items", [])}
    except Exception:
        return {}


def save_wi_tags(tags: Dict[str, Dict[str, Any]]):
    """Save WI tags to data/wi_tags.json, preserving manual overrides."""
    _ensure_dirs()
    
    data = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "items": list(tags.values()),
    }
    
    with WI_TAGS_FILE.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def merge_wi_tags(
    new_items: List[Dict[str, Any]],
    existing: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Dict[str, Any]]:
    """Merge new profiled items with existing tags, preserving manual overrides."""
    existing = existing or load_wi_tags()
    
    for item in new_items:
        wi_id = str(item["id"])
        
        if wi_id in existing:
            old = existing[wi_id]
            if old.get("complexity_manual_override"):
                item["complexity"] = old["complexity"]
                item["complexity_source"] = "manual_override"
                item["complexity_manual_override"] = old["complexity_manual_override"]
            if old.get("skills_manual_override"):
                item["inferred_skills"] = old["inferred_skills"]
                item["skills_manual_override"] = old["skills_manual_override"]
        
        existing[wi_id] = item
    
    return existing


def profile_upcoming_tasks(
    area_paths: List[str],
    max_wis: int = 500,
    use_llm: bool = False,
    org_url: Optional[str] = None,
    project: Optional[str] = None,
    pat: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Main function: fetch and profile upcoming work items."""
    logger.info("Fetching upcoming work items for area paths: %s", area_paths)
    
    raw_wis = fetch_upcoming_wis(
        area_paths=area_paths,
        org_url=org_url,
        project=project,
        pat=pat,
        max_wis=max_wis,
    )
    
    logger.info("Profiling %d work items...", len(raw_wis))
    
    profiled = []
    for wi in raw_wis:
        try:
            tagged = profile_work_item(wi, use_llm=use_llm)
            profiled.append(tagged)
        except Exception as e:
            logger.warning("Failed to profile WI %s: %s", wi.get("id"), e)
    
    # Merge with existing tags
    merged = merge_wi_tags(profiled)
    save_wi_tags(merged)
    
    logger.info("Profiled %d work items", len(profiled))
    return profiled


def update_complexity_override(
    wi_id: int,
    complexity: str,
    user: str = "unknown",
) -> bool:
    """Update complexity for a work item with manual override flag."""
    tags = load_wi_tags()
    wi_id_str = str(wi_id)
    
    if wi_id_str not in tags:
        return False
    
    tags[wi_id_str]["complexity"] = complexity
    tags[wi_id_str]["complexity_source"] = "manual_override"
    tags[wi_id_str]["complexity_manual_override"] = {
        "user": user,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    
    save_wi_tags(tags)
    return True


def get_all_profiled_tasks() -> List[Dict[str, Any]]:
    """Get all profiled tasks from the tags file."""
    tags = load_wi_tags()
    return list(tags.values())


# ============================================================================
# Legacy compatibility functions (for existing code)
# ============================================================================

def default_complexity_from_task(task: Dict[str, Any]) -> str:
    """Heuristic complexity: use Story Points if present, else use type/title heuristics.
    Returns one of: low, medium, high (legacy format)
    """
    sp = None
    for k in ("Story Points", "Effort", "Microsoft.VSTS.Scheduling.StoryPoints", "story_points"):
        v = task.get(k)
        if v:
            try:
                sp = float(v)
                break
            except Exception:
                pass
    if sp is not None:
        if sp <= 2:
            return "low"
        if sp <= 5:
            return "medium"
        return "high"

    t = (task.get("Work Item Type") or task.get("Type") or task.get("work_item_type") or "").lower()
    title = (task.get("Title") or task.get("title") or "")
    if "bug" in t:
        return "medium"
    if len(title) < 40:
        return "low"
    if len(title) < 120:
        return "medium"
    return "high"


def load_overrides(path) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    try:
        with p.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        logger.exception("Failed to load overrides from %s", p)
        return {}


def profile_tasks(tasks: List[Dict[str, Any]], overrides_path=None) -> List[Dict[str, Any]]:
    """Return augmented task dicts with tags: skills (list), complexity, matched_owner_suitability (score)

    This function does not modify persistent state. Manual overrides may be provided.
    Legacy compatibility function.
    """
    try:
        from utilities.skill_matrix import match_task_to_skills, list_members, get_member_skills
    except ImportError:
        # Fallback if skill_matrix not available
        def match_task_to_skills(t):
            return []
        def list_members():
            return []
        def get_member_skills(o):
            return {}

    overrides = load_overrides(overrides_path) if overrides_path else {}
    members = list_members()

    profiled = []
    for t in tasks:
        wid = str(t.get("WI_ID") or t.get("Id") or t.get("ID") or t.get("id") or "")
        item = dict(t)
        skills = match_task_to_skills(t)
        complexity = overrides.get(wid, {}).get("complexity") or default_complexity_from_task(t)
        skills = overrides.get(wid, {}).get("skills", skills)

        owner = t.get("Assigned To") or t.get("AssignedTo") or t.get("assigned_to") or None
        suitability = 0
        if owner and owner in members:
            member_skills = get_member_skills(owner)
            for s in skills:
                lvl = member_skills.get(s)
                if lvl in ("intermediate", "senior", "expert"):
                    suitability += 1
        item["profiled_skills"] = skills
        item["profiled_complexity"] = complexity
        item["profiled_owner_suitability"] = suitability
        profiled.append(item)
    return profiled

