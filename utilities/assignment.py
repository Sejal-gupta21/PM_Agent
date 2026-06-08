"""Auto-Assignment Logic for Sprint Planning.

Matches upcoming tasks to developers based on:
- Skill match (WI tags vs developer tech profile)
- Historical familiarity (which developer worked on which functionality/module)
- Availability (optional, only if capacity data exists)

Does NOT modify ADO - all assignments are local suggestions only.
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("pm_agent.assignment")

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"
OUTPUTS_DIR = REPO_ROOT / "outputs"
ASSIGNMENT_SUGGESTIONS_FILE = DATA_DIR / "assignment_suggestions.json"

# LivePlus functionality module mapping
LIVEPLUS_MODULES = {
    "pads_and_wells": ["pads", "wells", "pad", "well", "wellbore"],
    "dashboards": ["dashboard", "dashboards", "overview", "summary"],
    "inputs": {
        "well_and_treatment": ["well-treatment", "treatment", "well input"],
        "channel_input": ["channel", "channel-input", "model-input"],
        "wellbore_config": ["wellbore", "wellbore-configuration", "wellbore-config"],
        "heat_transfer": ["heat", "heat-transfer", "thermal"],
        "reservoir_params": ["reservoir", "reservoir-parameters", "reservoir-params"],
        "material_selection": ["material", "material-selection", "materials"],
        "treatment_schedule": ["treatment-schedule", "schedule", "treatment schedule"],
    },
    "analysis": {
        "entry_friction": ["entry-friction", "friction", "analysis"],
    },
    "results": {
        "plot": ["plot", "chart", "graph", "visualization"],
        "report": ["report", "export", "pdf", "excel"],
    },
    "utilities": {
        "user_defined_channels": ["user-defined", "custom-channel", "user channels"],
        "version_control": ["version", "version-control", "versioning", "history"],
    },
}


def _ensure_dirs():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)


def load_developer_skills() -> List[Dict[str, Any]]:
    """Load developer skill profiles from data/developer_skills.json."""
    skills_file = DATA_DIR / "developer_skills.json"
    if not skills_file.exists():
        logger.warning("Developer skills file not found: %s", skills_file)
        return []
    
    try:
        with skills_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception as e:
        logger.error("Failed to load developer skills: %s", e)
        return []


def load_commit_summary() -> Dict[str, Any]:
    """Load latest commit summary for familiarity analysis."""
    # Find latest commit summary file
    pattern = OUTPUTS_DIR / "commit_summary_*.json"
    from glob import glob
    files = sorted(glob(str(pattern)), reverse=True)
    
    if not files:
        # Try ado_commit_analysis files
        pattern2 = OUTPUTS_DIR / "ado_commit_analysis_*.json"
        files = sorted(glob(str(pattern2)), reverse=True)
    
    if not files:
        logger.warning("No commit summary files found")
        return {}
    
    try:
        with open(files[0], "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error("Failed to load commit summary: %s", e)
        return {}


def extract_developer_list(dev_skills: List[Dict], commit_summary: Dict) -> List[str]:
    """Extract list of active developers from available data."""
    developers = set()
    
    # From developer skills
    for dev in dev_skills:
        email = dev.get("developer") or dev.get("id", "").replace("dev_", "")
        if email and "@" in email:
            developers.add(email.lower())
    
    # From commit summary authors
    authors = commit_summary.get("authors", {})
    for author in authors.keys():
        # Try to match to email format
        if "@" in author:
            developers.add(author.lower())
    
    return sorted(developers)


def build_developer_profiles(
    dev_skills: List[Dict],
    commit_summary: Dict,
) -> Dict[str, Dict[str, Any]]:
    """Build comprehensive developer profiles with skills and file familiarity.
    
    Returns dict: {developer_email: {skills, languages, top_files, commits, familiarity_score, modules}}
    """
    profiles = {}
    
    # From developer skills JSON
    for dev in dev_skills:
        email = dev.get("developer") or dev.get("id", "").replace("dev_", "")
        if not email:
            continue
        email = email.lower()
        
        profiles[email] = {
            "email": email,
            "skills": set(),
            "languages": dev.get("languages", []),
            "top_files": dev.get("top_files", []),
            "commits": dev.get("commits", 0),
            "loc_added": dev.get("loc_added", 0),
            "wi_count": dev.get("wi_count", 0),
            "modules": defaultdict(int),  # module -> count
        }
        
        # Extract skills from languages
        for lang in dev.get("languages", []):
            profiles[email]["skills"].add(lang.lower())
        
        # Infer skills from file paths
        for fp in dev.get("top_files", []):
            # Handle both formats: string or [path, count] tuple/list
            filepath = fp[0] if isinstance(fp, (list, tuple)) else fp
            inferred = _infer_skills_from_path(filepath)
            profiles[email]["skills"].update(inferred)
            
            # Map to functionality modules
            modules = _infer_modules_from_path(filepath)
            for mod in modules:
                profiles[email]["modules"][mod] += 1
    
    # Enrich from commit summary
    authors = commit_summary.get("authors", {})
    for author, data in authors.items():
        # Try to find matching email
        matched_email = None
        for email in profiles.keys():
            if author.lower() in email or email.split("@")[0] in author.lower():
                matched_email = email
                break
        
        if matched_email:
            # Add additional file familiarity
            for file_tuple in data.get("top_files", []):
                if isinstance(file_tuple, list) and len(file_tuple) >= 2:
                    filepath, count = file_tuple[0], file_tuple[1]
                    inferred = _infer_skills_from_path(filepath)
                    profiles[matched_email]["skills"].update(inferred)
                    
                    modules = _infer_modules_from_path(filepath)
                    for mod in modules:
                        profiles[matched_email]["modules"][mod] += count
    
    # Convert sets to lists for JSON serialization
    for email in profiles:
        profiles[email]["skills"] = sorted(profiles[email]["skills"])
        profiles[email]["modules"] = dict(profiles[email]["modules"])
    
    return profiles


def _infer_skills_from_path(filepath: str) -> List[str]:
    """Infer technical skills from file path."""
    skills = []
    fp_lower = filepath.lower()
    
    # Language detection
    if ".ts" in fp_lower or ".tsx" in fp_lower:
        skills.append("typescript")
    if ".js" in fp_lower or ".jsx" in fp_lower:
        skills.append("javascript")
    if ".java" in fp_lower:
        skills.append("java")
    if ".py" in fp_lower:
        skills.append("python")
    if ".cs" in fp_lower:
        skills.append("csharp")
    if ".html" in fp_lower:
        skills.append("html")
    if ".css" in fp_lower or ".scss" in fp_lower:
        skills.append("css")
    if ".sql" in fp_lower:
        skills.append("sql")
    
    # Framework detection
    if "angular" in fp_lower or "/ng-" in fp_lower or "/app/" in fp_lower:
        skills.append("angular")
    if "react" in fp_lower:
        skills.append("react")
    if "vue" in fp_lower:
        skills.append("vue")
    if "spring" in fp_lower:
        skills.append("spring")
    if "service" in fp_lower:
        skills.append("backend")
    if "component" in fp_lower:
        skills.append("frontend")
    if "docker" in fp_lower:
        skills.append("docker")
    if "test" in fp_lower:
        skills.append("testing")
    
    return skills


def _infer_modules_from_path(filepath: str) -> List[str]:
    """Infer LivePlus (or similar) functionality modules from file path."""
    modules = []
    fp_lower = filepath.lower()
    
    # Direct module matches
    module_patterns = {
        "dashboard": ["dashboard", "overview", "summary"],
        "report": ["report", "export", "pdf"],
        "chart": ["chart", "plot", "graph", "visualization"],
        "input": ["input", "form", "screen"],
        "well": ["well", "wellbore"],
        "treatment": ["treatment", "schedule"],
        "analysis": ["analysis", "friction", "calculation"],
        "completion": ["completion", "completion-report"],
        "pad": ["pad", "pads"],
        "proposal": ["proposal"],
        "field_ticket": ["field-ticket", "fieldticket", "ticket"],
        "chemicals": ["chemical", "chemicals", "inventory"],
        "bol": ["bol", "bill-of-lading"],
        "utilities": ["util", "utils", "helper", "service"],
    }
    
    for module, patterns in module_patterns.items():
        if any(p in fp_lower for p in patterns):
            modules.append(module)
    
    return modules


def compute_skill_match_score(
    wi_skills: List[str],
    dev_skills: List[str],
) -> Tuple[float, List[str]]:
    """Compute skill match score between WI and developer.
    
    Returns (score 0-1, list of matching skills)
    """
    if not wi_skills:
        return 0.5, []  # Neutral score if no skills detected
    
    wi_set = set(s.lower() for s in wi_skills)
    dev_set = set(s.lower() for s in dev_skills)
    
    matching = wi_set & dev_set
    
    if not matching:
        return 0.0, []
    
    # Score based on proportion of WI skills matched
    score = len(matching) / len(wi_set)
    
    return min(1.0, score), sorted(matching)


def compute_familiarity_score(
    wi_area: str,
    wi_title: str,
    dev_modules: Dict[str, int],
) -> Tuple[float, List[str]]:
    """Compute familiarity score based on module/area match.
    
    Returns (score 0-1, list of matching modules)
    """
    if not dev_modules:
        return 0.0, []
    
    # Infer modules from WI
    wi_text = f"{wi_area} {wi_title}".lower()
    wi_modules = set()
    
    module_patterns = {
        "dashboard": ["dashboard", "overview", "summary"],
        "report": ["report", "export", "pdf"],
        "chart": ["chart", "plot", "graph"],
        "input": ["input", "form", "screen"],
        "well": ["well", "wellbore"],
        "treatment": ["treatment", "schedule"],
        "analysis": ["analysis", "friction"],
        "completion": ["completion"],
        "pad": ["pad", "pads"],
        "proposal": ["proposal"],
        "field_ticket": ["field-ticket", "ticket", "field ticket"],
        "chemicals": ["chemical", "chemicals", "inventory"],
        "bol": ["bol", "bill-of-lading"],
        "utilities": ["util", "utilities"],
    }
    
    for module, patterns in module_patterns.items():
        if any(p in wi_text for p in patterns):
            wi_modules.add(module)
    
    if not wi_modules:
        return 0.0, []
    
    # Check developer familiarity
    matching = []
    total_weight = 0
    
    for mod in wi_modules:
        if mod in dev_modules:
            matching.append(mod)
            total_weight += dev_modules[mod]
    
    if not matching:
        return 0.0, []
    
    # Score based on proportion of modules matched, weighted by commits
    score = len(matching) / len(wi_modules)
    
    # Boost for high familiarity (many commits)
    if total_weight > 10:
        score = min(1.0, score * 1.3)
    elif total_weight > 5:
        score = min(1.0, score * 1.15)
    
    return score, matching


def generate_assignment_suggestions(
    profiled_tasks: List[Dict[str, Any]],
    developer_profiles: Dict[str, Dict],
    top_k: int = 3,
    min_score: float = 0.1,
) -> List[Dict[str, Any]]:
    """Generate assignment suggestions for profiled tasks.
    
    Args:
        profiled_tasks: List of profiled WI dicts with inferred_skills, complexity, etc.
        developer_profiles: Dict of developer profiles from build_developer_profiles()
        top_k: Number of top suggestions per WI
        min_score: Minimum score threshold
        
    Returns:
        List of dicts with wi_id, suggestions (list of {developer, score, breakdown, evidence})
    """
    if not developer_profiles:
        logger.warning("No developer profiles available for assignment")
        return []
    
    suggestions = []
    
    for task in profiled_tasks:
        wi_id = task.get("id")
        title = task.get("title", "")
        area_path = task.get("area_path", "")
        
        # Extract skills from task
        wi_skills = []
        for skill_info in task.get("inferred_skills", []):
            if isinstance(skill_info, dict):
                wi_skills.append(skill_info.get("skill", ""))
            else:
                wi_skills.append(str(skill_info))
        
        # Also add skills from text analysis
        complexity = task.get("complexity", "Medium")
        
        # Score each developer
        dev_scores = []
        
        for dev_email, profile in developer_profiles.items():
            dev_skills = profile.get("skills", [])
            dev_modules = profile.get("modules", {})
            
            # Skill match
            skill_score, skill_matches = compute_skill_match_score(wi_skills, dev_skills)
            
            # Familiarity score
            familiarity_score, module_matches = compute_familiarity_score(
                area_path, title, dev_modules
            )
            
            # Combine scores (weighted)
            # Skill match: 40%, Familiarity: 50%, Base: 10%
            combined_score = (
                skill_score * 0.4 +
                familiarity_score * 0.5 +
                0.1  # Base score for being in the pool
            )
            
            # Boost for developers with more commits
            commits = profile.get("commits", 0)
            if commits > 30:
                combined_score *= 1.1
            
            if combined_score >= min_score:
                dev_scores.append({
                    "developer": dev_email,
                    "score": round(combined_score, 3),
                    "breakdown": {
                        "skill_match": round(skill_score, 3),
                        "familiarity": round(familiarity_score, 3),
                        "skill_matches": skill_matches[:5],
                        "module_matches": module_matches[:5],
                    },
                    "evidence": {
                        "commits": commits,
                        "top_files": profile.get("top_files", [])[:3],
                    },
                })
        
        # Sort by score and take top k
        dev_scores.sort(key=lambda x: -x["score"])
        top_suggestions = dev_scores[:top_k]
        
        suggestions.append({
            "wi_id": wi_id,
            "title": title[:100],
            "area_path": area_path,
            "complexity": complexity,
            "wi_skills": wi_skills[:5],
            "suggestions": top_suggestions,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        })
    
    return suggestions


def save_assignment_suggestions(
    suggestions: List[Dict[str, Any]],
    output_path: Optional[Path] = None,
) -> Tuple[Path, Path]:
    """Save assignment suggestions to both timestamped and persistent files.
    
    Returns (timestamped_path, persistent_path)
    """
    _ensure_dirs()
    
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    
    # Timestamped output
    if output_path:
        ts_path = Path(output_path)
    else:
        ts_path = OUTPUTS_DIR / f"assignment_suggestions_{ts}.json"
    
    # Persistent file (updated each time)
    persistent_path = ASSIGNMENT_SUGGESTIONS_FILE
    
    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(suggestions),
        "suggestions": suggestions,
    }
    
    try:
        with ts_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        logger.info("Saved %d suggestions to %s", len(suggestions), ts_path)
    except Exception as e:
        logger.error("Failed to save timestamped suggestions: %s", e)
    
    try:
        with persistent_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        logger.info("Saved suggestions to persistent file: %s", persistent_path)
    except Exception as e:
        logger.error("Failed to save persistent suggestions: %s", e)
    
    return ts_path, persistent_path


def load_assignment_suggestions() -> Dict[str, Any]:
    """Load latest assignment suggestions from persistent file."""
    if not ASSIGNMENT_SUGGESTIONS_FILE.exists():
        return {"suggestions": [], "count": 0}
    
    try:
        with ASSIGNMENT_SUGGESTIONS_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error("Failed to load assignment suggestions: %s", e)
        return {"suggestions": [], "count": 0}


def get_suggestions_for_wi(wi_id: int) -> List[Dict[str, Any]]:
    """Get assignment suggestions for a specific work item."""
    data = load_assignment_suggestions()
    
    for item in data.get("suggestions", []):
        if item.get("wi_id") == wi_id:
            return item.get("suggestions", [])
    
    return []


def run_assignment_pipeline(
    profiled_tasks: Optional[List[Dict]] = None,
    input_path: Optional[Path] = None,
    output_path: Optional[Path] = None,
    top_k: int = 3,
) -> List[Dict[str, Any]]:
    """Run the full assignment suggestion pipeline.
    
    Args:
        profiled_tasks: Pre-loaded profiled tasks (optional)
        input_path: Path to profiled tasks JSON (if profiled_tasks not provided)
        output_path: Custom output path
        top_k: Number of suggestions per WI
        
    Returns:
        List of suggestion dicts
    """
    # Load profiled tasks if not provided
    if profiled_tasks is None:
        if input_path and Path(input_path).exists():
            with open(input_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                profiled_tasks = data
            else:
                profiled_tasks = data.get("items", data.get("profiled_tasks", []))
        else:
            # Try to load from wi_tags.json
            from utilities.task_profiler import load_wi_tags
            tags = load_wi_tags()
            profiled_tasks = list(tags.values())
    
    if not profiled_tasks:
        logger.warning("No profiled tasks to generate assignments for")
        return []
    
    logger.info("Generating assignment suggestions for %d tasks", len(profiled_tasks))
    
    # Build developer profiles
    dev_skills = load_developer_skills()
    commit_summary = load_commit_summary()
    
    developer_profiles = build_developer_profiles(dev_skills, commit_summary)
    logger.info("Built profiles for %d developers", len(developer_profiles))
    
    # Generate suggestions
    suggestions = generate_assignment_suggestions(
        profiled_tasks,
        developer_profiles,
        top_k=top_k,
    )
    
    # Save
    ts_path, persistent_path = save_assignment_suggestions(suggestions, output_path)
    
    logger.info("Assignment pipeline complete: %d WIs with suggestions", len(suggestions))
    
    return suggestions


# =============================================================================
# FRONTEND/BACKEND ROLE SEPARATION (STRICT)
# =============================================================================

# Skills that indicate frontend developer
FRONTEND_SKILLS = {
    "angular", "react", "vue", "typescript", "javascript", "html", "css",
    "scss", "sass", "frontend", "ui", "component", "ionic", "mobile",
    "ios", "android", "flutter", "react-native"
}

# Skills that indicate backend developer
BACKEND_SKILLS = {
    "java", "python", "csharp", "c#", "spring", "backend", "api",
    "rest", "sql", "database", "db", "node", "express", "django",
    "flask", "dotnet", ".net", "microservices", "kafka", "rabbitmq",
    "gradle", "maven"
}

# Minimum evidence thresholds for role classification
MIN_ROLE_SKILL_COUNT = 1  # At least 1 skill in the role
MIN_COMMITS_FOR_ROLE = 1  # At least 1 commit to be considered


def load_developer_role_overrides() -> Dict[str, str]:
    """Load manual role overrides from data/developer_roles.json."""
    roles_file = DATA_DIR / "developer_roles.json"
    if not roles_file.exists():
        return {}
    
    try:
        with roles_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
        # Filter out metadata keys starting with _
        return {k.lower(): v for k, v in data.items() if not k.startswith("_")}
    except Exception as e:
        logger.warning("Failed to load developer role overrides: %s", e)
        return {}


def classify_developer_role_strict(
    email: str,
    skills: List[str],
    languages: List[str],
    commits: int = 0,
    role_overrides: Optional[Dict[str, str]] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Classify developer with strict evidence requirements.
    
    Returns (role, evidence_dict) where role is 'frontend', 'backend', 'fullstack', or 'unknown'.
    Evidence dict contains fe_score, be_score, matched skills, etc.
    """
    # Check manual override first
    if role_overrides:
        email_lower = email.lower()
        for override_email, role in role_overrides.items():
            if email_lower == override_email or email_lower.split("@")[0] == override_email.split("@")[0]:
                return role, {"source": "manual_override", "override_email": override_email}
    
    # Combine skills and languages for analysis
    all_skills = set(s.lower() for s in skills) | set(l.lower() for l in languages)
    
    # Count frontend and backend evidence
    fe_matches = all_skills & FRONTEND_SKILLS
    be_matches = all_skills & BACKEND_SKILLS
    
    fe_count = len(fe_matches)
    be_count = len(be_matches)
    
    # Calculate role scores (0 to 1)
    total = fe_count + be_count
    if total == 0:
        return "unknown", {"fe_score": 0, "be_score": 0, "source": "no_evidence"}
    
    fe_score = fe_count / total
    be_score = be_count / total
    
    evidence = {
        "fe_score": round(fe_score, 2),
        "be_score": round(be_score, 2),
        "fe_skills": sorted(fe_matches),
        "be_skills": sorted(be_matches),
        "commits": commits,
        "source": "inferred",
    }
    
    # Strict classification: require clear majority (>= 70%)
    ROLE_THRESHOLD = 0.7
    
    if fe_score >= ROLE_THRESHOLD and fe_count >= MIN_ROLE_SKILL_COUNT:
        return "frontend", evidence
    elif be_score >= ROLE_THRESHOLD and be_count >= MIN_ROLE_SKILL_COUNT:
        return "backend", evidence
    elif fe_count >= MIN_ROLE_SKILL_COUNT and be_count >= MIN_ROLE_SKILL_COUNT:
        # Has significant evidence for both - true fullstack
        return "fullstack", evidence
    elif fe_count > be_count:
        return "frontend", evidence
    elif be_count > fe_count:
        return "backend", evidence
    else:
        return "unknown", evidence


def classify_developer_role(skills: List[str]) -> str:
    """Classify a developer as 'frontend', 'backend', or 'fullstack'.
    
    DEPRECATED: Use classify_developer_role_strict for better accuracy.
    """
    skills_lower = set(s.lower() for s in skills)
    
    has_fe = bool(skills_lower & FRONTEND_SKILLS)
    has_be = bool(skills_lower & BACKEND_SKILLS)
    
    if has_fe and has_be:
        return "fullstack"
    elif has_fe:
        return "frontend"
    elif has_be:
        return "backend"
    else:
        return "unknown"


# Complexity to estimated days mapping for workload balancing
COMPLEXITY_TO_DAYS = {
    "Small": 1,
    "Medium": 3,
    "Large": 5,
    "XLarge": 8,
}

# Default sprint duration in days
DEFAULT_SPRINT_DAYS = 10

# Maximum workload per developer (as fraction of sprint days)
MAX_WORKLOAD_FRACTION = 0.8  # Allow 80% utilization


def generate_role_based_suggestions(
    profiled_tasks: List[Dict[str, Any]],
    developer_profiles: Dict[str, Dict],
    top_k: int = 3,
    min_score: float = 0.1,
    sprint_days: int = DEFAULT_SPRINT_DAYS,
    balance_workload: bool = True,
) -> List[Dict[str, Any]]:
    """Generate assignment suggestions with separate frontend/backend recommendations.
    
    Uses STRICT role classification to prevent frontend devs from appearing in backend
    suggestions and vice versa.
    
    Includes workload balancing to prevent over-assigning the same developer.
    
    Args:
        profiled_tasks: Profiled WI list
        developer_profiles: Developer profile dict
        top_k: Number of suggestions per WI
        min_score: Minimum score threshold
        sprint_days: Sprint duration in days (default 10)
        balance_workload: If True, apply workload balancing
    
    Returns list of dicts with frontend_suggestions and backend_suggestions.
    """
    if not developer_profiles:
        logger.warning("No developer profiles available for assignment")
        return []
    
    # Load manual role overrides
    role_overrides = load_developer_role_overrides()
    logger.info("Loaded %d manual role overrides", len(role_overrides))
    
    # Classify developers by role using STRICT classification
    fe_developers = {}
    be_developers = {}
    role_assignments = {}  # Track role for each developer
    
    for email, profile in developer_profiles.items():
        # Use strict classification with evidence
        role, evidence = classify_developer_role_strict(
            email=email,
            skills=profile.get("skills", []),
            languages=profile.get("languages", []),
            commits=profile.get("commits", 0),
            role_overrides=role_overrides,
        )
        
        role_assignments[email] = {"role": role, "evidence": evidence}
        
        # STRICT: Only add to the appropriate pool (no crossover except explicit fullstack)
        if role == "frontend":
            fe_developers[email] = profile
        elif role == "backend":
            be_developers[email] = profile
        elif role == "fullstack":
            # Fullstack can be in both pools
            fe_developers[email] = profile
            be_developers[email] = profile
        # 'unknown' developers are not added to any pool
    
    logger.info("STRICT Role Classification: %d frontend-only, %d backend-only, %d in both pools",
                len([r for r in role_assignments.values() if r["role"] == "frontend"]),
                len([r for r in role_assignments.values() if r["role"] == "backend"]),
                len([r for r in role_assignments.values() if r["role"] == "fullstack"]))
    
    # Log role assignments for debugging
    for email, info in sorted(role_assignments.items()):
        name = email.split("@")[0]
        role = info["role"]
        source = info["evidence"].get("source", "unknown")
        logger.debug("  %s: %s (source=%s)", name, role.upper(), source)
    
    # Initialize workload tracking (days assigned per developer)
    max_capacity = sprint_days * MAX_WORKLOAD_FRACTION  # 8 days for 10-day sprint
    fe_workload: Dict[str, float] = defaultdict(float)
    be_workload: Dict[str, float] = defaultdict(float)
    
    # Initialize workload tracking (days assigned per developer)
    max_capacity = sprint_days * MAX_WORKLOAD_FRACTION  # 8 days for 10-day sprint
    fe_workload: Dict[str, float] = defaultdict(float)
    be_workload: Dict[str, float] = defaultdict(float)
    
    suggestions = []
    
    # Sort tasks by complexity (larger first to assign them better)
    sorted_tasks = sorted(
        profiled_tasks,
        key=lambda t: COMPLEXITY_TO_DAYS.get(t.get("complexity", "Medium"), 3),
        reverse=True
    )
    
    for task in sorted_tasks:
        wi_id = task.get("id")
        title = task.get("title", "")
        area_path = task.get("area_path", "")
        complexity = task.get("complexity", "Medium")
        task_days = COMPLEXITY_TO_DAYS.get(complexity, 3)
        
        # Extract skills from task
        wi_skills = []
        for skill_info in task.get("inferred_skills", []):
            if isinstance(skill_info, dict):
                wi_skills.append(skill_info.get("skill", ""))
            else:
                wi_skills.append(str(skill_info))
        
        # Score frontend developers
        fe_scores = _score_developers(
            wi_skills, area_path, title, fe_developers, min_score
        )
        
        # Score backend developers
        be_scores = _score_developers(
            wi_skills, area_path, title, be_developers, min_score
        )
        
        # Apply workload balancing penalty
        if balance_workload:
            fe_scores = _apply_workload_penalty(fe_scores, fe_workload, max_capacity, task_days)
            be_scores = _apply_workload_penalty(be_scores, be_workload, max_capacity, task_days)
        
        # Sort and take top k
        fe_scores.sort(key=lambda x: -x["adjusted_score"])
        be_scores.sort(key=lambda x: -x["adjusted_score"])
        
        top_fe = fe_scores[:top_k]
        top_be = be_scores[:top_k]
        
        # Track workload for the TOP assigned developer (first suggestion)
        if top_fe and balance_workload:
            assigned_fe = top_fe[0]["developer"]
            fe_workload[assigned_fe] += task_days
        
        if top_be and balance_workload:
            assigned_be = top_be[0]["developer"]
            be_workload[assigned_be] += task_days
        
        suggestions.append({
            "wi_id": wi_id,
            "title": title[:100],
            "area_path": area_path,
            "complexity": complexity,
            "wi_skills": wi_skills[:5],
            "frontend_suggestions": top_fe,
            "backend_suggestions": top_be,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        })
    
    # Log workload distribution
    logger.info("Frontend workload distribution: %s", dict(fe_workload))
    logger.info("Backend workload distribution: %s", dict(be_workload))
    
    return suggestions


def _apply_workload_penalty(
    scores: List[Dict],
    workload: Dict[str, float],
    max_capacity: float,
    task_days: float,
) -> List[Dict]:
    """Apply penalty to developers who are nearing capacity.
    
    Uses aggressive penalties to ensure workload is distributed evenly.
    When all devs are at capacity, uses LEAST LOADED developer to distribute evenly.
    
    Returns updated scores with adjusted_score field.
    """
    result = []
    
    # Find the developer with least current workload
    min_workload = float('inf')
    for s in scores:
        dev = s["developer"]
        current_load = workload.get(dev, 0)
        min_workload = min(min_workload, current_load)
    
    if min_workload == float('inf'):
        min_workload = 0
    
    for s in scores:
        dev = s["developer"]
        current_load = workload.get(dev, 0)
        remaining_capacity = max_capacity - current_load
        
        # Calculate penalty based on how close to capacity
        if remaining_capacity <= 0:
            # Developer is at/over capacity
            # Instead of excluding, use relative workload for even distribution
            # Penalize more the more overloaded they are
            overload_factor = (current_load - max_capacity) / max_capacity
            # Heavy penalty + additional for being more overloaded than others
            relative_overload = (current_load - min_workload) / max(max_capacity, 1)
            penalty = 0.9 + min(0.09, relative_overload * 0.3)  # Cap at 0.99
        elif remaining_capacity < task_days:
            # Developer can't fully take this task - very heavy penalty
            penalty = 0.85
        elif current_load > max_capacity * 0.75:
            # Developer is over 75% utilized - heavy penalty
            penalty = 0.6
        elif current_load > max_capacity * 0.5:
            # Developer is over 50% utilized - medium penalty
            penalty = 0.35
        elif current_load > max_capacity * 0.25:
            # Developer is over 25% utilized - light penalty
            penalty = 0.15
        else:
            # Developer has plenty of capacity - no penalty
            penalty = 0.0
        
        adjusted_score = s["score"] * (1 - penalty)
        
        result.append({
            **s,
            "adjusted_score": round(adjusted_score, 3),
            "current_workload_days": round(current_load, 1),
            "remaining_capacity_days": round(remaining_capacity, 1),
        })
    
    return result


def _score_developers(
    wi_skills: List[str],
    area_path: str,
    title: str,
    developer_profiles: Dict[str, Dict],
    min_score: float,
) -> List[Dict]:
    """Score a set of developers against a work item."""
    scores = []
    
    for dev_email, profile in developer_profiles.items():
        dev_skills = profile.get("skills", [])
        dev_modules = profile.get("modules", {})
        
        skill_score, skill_matches = compute_skill_match_score(wi_skills, dev_skills)
        familiarity_score, module_matches = compute_familiarity_score(
            area_path, title, dev_modules
        )
        
        combined_score = (
            skill_score * 0.4 +
            familiarity_score * 0.5 +
            0.1
        )
        
        commits = profile.get("commits", 0)
        if commits > 30:
            combined_score *= 1.1
        
        if combined_score >= min_score:
            scores.append({
                "developer": dev_email,
                "score": round(combined_score, 3),
                "adjusted_score": round(combined_score, 3),  # Same as score initially
                "breakdown": {
                    "skill_match": round(skill_score, 3),
                    "familiarity": round(familiarity_score, 3),
                    "skill_matches": skill_matches[:5],
                    "module_matches": module_matches[:5],
                },
                "evidence": {
                    "commits": commits,
                    "top_files": profile.get("top_files", [])[:3],
                },
            })
    
    return scores


def run_role_based_assignment_pipeline(
    profiled_tasks: Optional[List[Dict]] = None,
    input_path: Optional[Path] = None,
    output_path: Optional[Path] = None,
    top_k: int = 3,
) -> List[Dict[str, Any]]:
    """Run assignment pipeline with frontend/backend role separation."""
    # Load profiled tasks if not provided
    if profiled_tasks is None:
        if input_path and Path(input_path).exists():
            with open(input_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                profiled_tasks = data
            else:
                profiled_tasks = data.get("items", data.get("profiled_tasks", []))
        else:
            from utilities.task_profiler import load_wi_tags
            tags = load_wi_tags()
            profiled_tasks = list(tags.values())
    
    if not profiled_tasks:
        logger.warning("No profiled tasks to generate assignments for")
        return []
    
    logger.info("Generating role-based assignment suggestions for %d tasks", len(profiled_tasks))
    
    dev_skills = load_developer_skills()
    commit_summary = load_commit_summary()
    developer_profiles = build_developer_profiles(dev_skills, commit_summary)
    
    suggestions = generate_role_based_suggestions(
        profiled_tasks,
        developer_profiles,
        top_k=top_k,
    )
    
    # Save with role-based format
    _ensure_dirs()
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    
    if output_path:
        ts_path = Path(output_path)
    else:
        ts_path = OUTPUTS_DIR / f"role_assignment_suggestions_{ts}.json"
    
    persistent_path = DATA_DIR / "role_assignment_suggestions.json"
    
    data = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(suggestions),
        "suggestions": suggestions,
    }
    
    try:
        with ts_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        with persistent_path.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        logger.info("Saved role-based suggestions to %s", ts_path)
    except Exception as e:
        logger.error("Failed to save role-based suggestions: %s", e)
    
    return suggestions
