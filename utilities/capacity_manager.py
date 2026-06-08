"""Capacity Manager for Sprint Planning.

Provides:
- Developer capacity calculation (available hours per sprint)
- Workload tracking based on task complexity
- Overload detection with threshold alerts
- Redistribution suggestions for balanced workload

Uses 10-day sprint with 8 hours/day as default = 80 hours capacity.
"""
from __future__ import annotations

import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("pm_agent.capacity_manager")

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"
OUTPUTS_DIR = REPO_ROOT / "outputs"

# Default capacity settings
DEFAULT_SPRINT_DAYS = 10
DEFAULT_HOURS_PER_DAY = 8
DEFAULT_UTILIZATION_TARGET = 0.80  # 80% target utilization

# Complexity to hours mapping
COMPLEXITY_TO_HOURS = {
    "Small": 8,      # 1 day
    "Medium": 24,    # 3 days
    "Large": 40,     # 5 days
    "XLarge": 64,    # 8 days
}

# Overload thresholds
OVERLOAD_THRESHOLD = 1.0      # 100% capacity - overloaded
WARNING_THRESHOLD = 0.85      # 85% capacity - warning
UNDERLOAD_THRESHOLD = 0.50    # 50% capacity - underutilized


class DeveloperCapacity:
    """Tracks a developer's capacity and workload."""
    
    def __init__(
        self,
        email: str,
        name: str = "",
        role: str = "fullstack",
        sprint_days: int = DEFAULT_SPRINT_DAYS,
        hours_per_day: float = DEFAULT_HOURS_PER_DAY,
        availability_factor: float = 1.0,  # For PTO, part-time, etc.
    ):
        self.email = email
        self.name = name or email.split("@")[0].replace(".", " ").title()
        self.role = role
        self.sprint_days = sprint_days
        self.hours_per_day = hours_per_day
        self.availability_factor = availability_factor
        
        # Calculated capacity
        self.total_capacity = sprint_days * hours_per_day * availability_factor
        self.target_capacity = self.total_capacity * DEFAULT_UTILIZATION_TARGET
        
        # Workload tracking
        self.assigned_tasks: List[Dict[str, Any]] = []
        self.assigned_hours: float = 0.0
    
    @property
    def available_hours(self) -> float:
        """Remaining available hours."""
        return max(0, self.total_capacity - self.assigned_hours)
    
    @property
    def utilization(self) -> float:
        """Current utilization percentage (0-1+)."""
        if self.total_capacity == 0:
            return 0.0
        return self.assigned_hours / self.total_capacity
    
    @property
    def status(self) -> str:
        """Capacity status: 'overloaded', 'warning', 'optimal', 'underutilized'."""
        util = self.utilization
        if util >= OVERLOAD_THRESHOLD:
            return "overloaded"
        elif util >= WARNING_THRESHOLD:
            return "warning"
        elif util < UNDERLOAD_THRESHOLD:
            return "underutilized"
        else:
            return "optimal"
    
    @property
    def overload_hours(self) -> float:
        """Hours over capacity (negative if under)."""
        return self.assigned_hours - self.total_capacity
    
    def assign_task(self, task: Dict[str, Any], hours: float) -> None:
        """Assign a task to this developer."""
        self.assigned_tasks.append({
            "wi_id": task.get("id"),
            "title": task.get("title", "")[:60],
            "hours": hours,
            "complexity": task.get("complexity", "Medium"),
        })
        self.assigned_hours += hours
    
    def unassign_task(self, wi_id: int) -> Optional[float]:
        """Remove a task assignment. Returns hours freed or None if not found."""
        for i, task in enumerate(self.assigned_tasks):
            if task.get("wi_id") == wi_id:
                hours = task.get("hours", 0)
                self.assigned_hours -= hours
                self.assigned_tasks.pop(i)
                return hours
        return None
    
    def can_accept_task(self, hours: float, allow_overload: bool = False) -> bool:
        """Check if developer can accept a task of given hours."""
        if allow_overload:
            return True
        return self.available_hours >= hours
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "email": self.email,
            "name": self.name,
            "role": self.role,
            "total_capacity_hours": round(self.total_capacity, 1),
            "target_capacity_hours": round(self.target_capacity, 1),
            "assigned_hours": round(self.assigned_hours, 1),
            "available_hours": round(self.available_hours, 1),
            "utilization": round(self.utilization, 2),
            "status": self.status,
            "overload_hours": round(self.overload_hours, 1),
            "task_count": len(self.assigned_tasks),
            "assigned_tasks": self.assigned_tasks,
        }


class CapacityManager:
    """Manages capacity for all developers in a sprint."""
    
    def __init__(
        self,
        sprint_days: int = DEFAULT_SPRINT_DAYS,
        hours_per_day: float = DEFAULT_HOURS_PER_DAY,
    ):
        self.sprint_days = sprint_days
        self.hours_per_day = hours_per_day
        self.developers: Dict[str, DeveloperCapacity] = {}
        self.unassigned_tasks: List[Dict[str, Any]] = []
    
    def add_developer(
        self,
        email: str,
        name: str = "",
        role: str = "fullstack",
        availability_factor: float = 1.0,
    ) -> DeveloperCapacity:
        """Add a developer to the capacity tracker."""
        dev = DeveloperCapacity(
            email=email,
            name=name,
            role=role,
            sprint_days=self.sprint_days,
            hours_per_day=self.hours_per_day,
            availability_factor=availability_factor,
        )
        self.developers[email.lower()] = dev
        return dev
    
    def get_developer(self, email: str) -> Optional[DeveloperCapacity]:
        """Get developer by email."""
        return self.developers.get(email.lower())
    
    def get_developers_by_role(self, role: str) -> List[DeveloperCapacity]:
        """Get developers matching a role (frontend, backend, fullstack)."""
        if role == "fullstack":
            return list(self.developers.values())
        return [d for d in self.developers.values() if d.role in (role, "fullstack")]
    
    def assign_task(
        self,
        task: Dict[str, Any],
        developer_email: str,
        hours: Optional[float] = None,
        force: bool = False,
    ) -> Tuple[bool, str]:
        """
        Assign a task to a developer.
        
        Returns (success, message).
        """
        dev = self.get_developer(developer_email)
        if not dev:
            return False, f"Developer {developer_email} not found"
        
        # Calculate hours from complexity if not provided
        if hours is None:
            complexity = task.get("complexity", "Medium")
            hours = COMPLEXITY_TO_HOURS.get(complexity, 24)
        
        # Check capacity
        if not force and not dev.can_accept_task(hours):
            return False, f"{dev.name} is at capacity ({dev.utilization:.0%})"
        
        dev.assign_task(task, hours)
        return True, f"Assigned {hours}h to {dev.name}"
    
    def get_capacity_summary(self) -> Dict[str, Any]:
        """Get overall capacity summary for all developers."""
        total_capacity = sum(d.total_capacity for d in self.developers.values())
        total_assigned = sum(d.assigned_hours for d in self.developers.values())
        total_available = sum(d.available_hours for d in self.developers.values())
        
        overloaded = [d for d in self.developers.values() if d.status == "overloaded"]
        warning = [d for d in self.developers.values() if d.status == "warning"]
        optimal = [d for d in self.developers.values() if d.status == "optimal"]
        underutilized = [d for d in self.developers.values() if d.status == "underutilized"]
        
        return {
            "total_developers": len(self.developers),
            "total_capacity_hours": round(total_capacity, 1),
            "total_assigned_hours": round(total_assigned, 1),
            "total_available_hours": round(total_available, 1),
            "overall_utilization": round(total_assigned / total_capacity, 2) if total_capacity > 0 else 0,
            "overloaded_count": len(overloaded),
            "overloaded_developers": [d.email for d in overloaded],
            "warning_count": len(warning),
            "warning_developers": [d.email for d in warning],
            "optimal_count": len(optimal),
            "underutilized_count": len(underutilized),
            "underutilized_developers": [d.email for d in underutilized],
            "developers": {d.email: d.to_dict() for d in self.developers.values()},
        }
    
    def get_overloaded_developers(self) -> List[DeveloperCapacity]:
        """Get list of overloaded developers."""
        return [d for d in self.developers.values() if d.status == "overloaded"]
    
    def get_underutilized_developers(self, role: Optional[str] = None) -> List[DeveloperCapacity]:
        """Get list of underutilized developers, optionally filtered by role."""
        devs = self.developers.values()
        if role:
            devs = [d for d in devs if d.role in (role, "fullstack")]
        return sorted(
            [d for d in devs if d.status in ("underutilized", "optimal")],
            key=lambda d: d.utilization
        )
    
    def suggest_redistribution(self) -> List[Dict[str, Any]]:
        """
        Suggest task redistributions to balance workload.
        
        Returns list of suggestions with:
        - from_developer: overloaded developer
        - to_developer: underutilized developer
        - task: task to move
        - hours: hours to redistribute
        - reason: explanation
        """
        suggestions = []
        
        overloaded = self.get_overloaded_developers()
        
        for dev in overloaded:
            # Find tasks that could be moved
            for task in reversed(dev.assigned_tasks):  # Start with most recent
                task_hours = task.get("hours", 0)
                task_role = self._infer_task_role(task)
                
                # Find underutilized developers with matching role
                candidates = self.get_underutilized_developers(role=task_role)
                
                for candidate in candidates:
                    if candidate.email == dev.email:
                        continue
                    
                    if candidate.can_accept_task(task_hours):
                        # Calculate impact
                        overload_reduction = min(task_hours, dev.overload_hours)
                        
                        suggestions.append({
                            "type": "redistribute",
                            "from_developer": dev.email,
                            "from_developer_name": dev.name,
                            "from_utilization": round(dev.utilization, 2),
                            "to_developer": candidate.email,
                            "to_developer_name": candidate.name,
                            "to_utilization": round(candidate.utilization, 2),
                            "task_wi_id": task.get("wi_id"),
                            "task_title": task.get("title"),
                            "hours": task_hours,
                            "overload_reduction": round(overload_reduction, 1),
                            "reason": f"Move {task_hours}h from overloaded {dev.name} ({dev.utilization:.0%}) "
                                     f"to underutilized {candidate.name} ({candidate.utilization:.0%})",
                        })
                        break  # One suggestion per task
        
        return suggestions
    
    def _infer_task_role(self, task: Dict[str, Any]) -> Optional[str]:
        """Infer if task is frontend or backend based on title/skills."""
        title = task.get("title", "").lower()
        
        fe_keywords = ["ui", "screen", "component", "frontend", "angular", "react", "form"]
        be_keywords = ["api", "backend", "service", "database", "server", "sql"]
        
        has_fe = any(kw in title for kw in fe_keywords)
        has_be = any(kw in title for kw in be_keywords)
        
        if has_fe and not has_be:
            return "frontend"
        elif has_be and not has_fe:
            return "backend"
        return None  # Fullstack or unknown
    
    def apply_redistribution(self, suggestion: Dict[str, Any]) -> Tuple[bool, str]:
        """Apply a redistribution suggestion."""
        from_dev = self.get_developer(suggestion["from_developer"])
        to_dev = self.get_developer(suggestion["to_developer"])
        
        if not from_dev or not to_dev:
            return False, "Developer not found"
        
        wi_id = suggestion["task_wi_id"]
        hours = from_dev.unassign_task(wi_id)
        
        if hours is None:
            return False, f"Task {wi_id} not found in {from_dev.name}'s assignments"
        
        # Create task dict for reassignment
        task = {"id": wi_id, "title": suggestion.get("task_title", ""), "complexity": "Medium"}
        to_dev.assign_task(task, hours)
        
        return True, f"Moved {hours}h from {from_dev.name} to {to_dev.name}"
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "sprint_days": self.sprint_days,
            "hours_per_day": self.hours_per_day,
            "summary": self.get_capacity_summary(),
            "redistribution_suggestions": self.suggest_redistribution(),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
    
    def save(self, output_path: Optional[Path] = None) -> Path:
        """Save capacity data to JSON file."""
        if output_path is None:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = OUTPUTS_DIR / f"capacity_report_{ts}.json"
        
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        with output_path.open("w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)
        
        logger.info("Saved capacity report to %s", output_path)
        return output_path


def load_developer_availability() -> Dict[str, float]:
    """Load developer availability factors from config file.
    
    Returns dict of email -> availability_factor (0-1).
    """
    avail_file = DATA_DIR / "developer_availability.json"
    if not avail_file.exists():
        return {}
    
    try:
        with avail_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
        # Filter out metadata keys
        return {k.lower(): v for k, v in data.items() if not k.startswith("_") and isinstance(v, (int, float))}
    except Exception as e:
        logger.warning("Failed to load developer availability: %s", e)
        return {}


def load_developer_roles() -> Dict[str, str]:
    """Load developer role assignments from config file."""
    roles_file = DATA_DIR / "developer_roles.json"
    if not roles_file.exists():
        return {}
    
    try:
        with roles_file.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return {k.lower(): v for k, v in data.items() if not k.startswith("_")}
    except Exception as e:
        logger.warning("Failed to load developer roles: %s", e)
        return {}


def build_capacity_manager_from_skills(
    sprint_days: int = DEFAULT_SPRINT_DAYS,
    hours_per_day: float = DEFAULT_HOURS_PER_DAY,
) -> CapacityManager:
    """
    Build a CapacityManager initialized with developers from developer_skills.json.
    """
    cm = CapacityManager(sprint_days=sprint_days, hours_per_day=hours_per_day)
    
    # Load developer skills
    skills_file = DATA_DIR / "developer_skills.json"
    if not skills_file.exists():
        logger.warning("Developer skills file not found: %s", skills_file)
        return cm
    
    try:
        with skills_file.open("r", encoding="utf-8") as f:
            developers = json.load(f)
    except Exception as e:
        logger.error("Failed to load developer skills: %s", e)
        return cm
    
    # Load availability and roles
    availability = load_developer_availability()
    roles = load_developer_roles()
    
    # Import role classification
    try:
        from utilities.assignment import classify_developer_role_strict
    except ImportError:
        classify_developer_role_strict = None
    
    for dev in developers:
        email = dev.get("developer", "").lower()
        if not email:
            continue
        
        # Get availability factor (default 1.0 = full time)
        avail_factor = availability.get(email, 1.0)
        
        # Get role from manual override or infer
        role = roles.get(email)
        if not role and classify_developer_role_strict:
            role, _ = classify_developer_role_strict(
                email=email,
                skills=dev.get("skills", []) + dev.get("languages", []),
                languages=dev.get("languages", []),
                commits=dev.get("commits", 0),
            )
        role = role or "fullstack"
        
        # Add developer
        cm.add_developer(
            email=email,
            name=email.split("@")[0].replace(".", " ").title(),
            role=role,
            availability_factor=avail_factor,
        )
    
    logger.info("Built capacity manager with %d developers", len(cm.developers))
    return cm


def calculate_sprint_capacity(
    tasks: List[Dict[str, Any]],
    assignments: Dict[int, Dict[str, str]],  # wi_id -> {"frontend": email, "backend": email}
    sprint_days: int = DEFAULT_SPRINT_DAYS,
    hours_per_day: float = DEFAULT_HOURS_PER_DAY,
) -> CapacityManager:
    """
    Calculate capacity for a sprint given tasks and assignments.
    
    Args:
        tasks: List of profiled task dicts with id, complexity, etc.
        assignments: Dict mapping WI ID to frontend/backend developer emails
        sprint_days: Sprint duration in days
        hours_per_day: Working hours per day
    
    Returns:
        CapacityManager with all assignments loaded
    """
    cm = build_capacity_manager_from_skills(sprint_days, hours_per_day)
    
    for task in tasks:
        wi_id = task.get("id")
        if wi_id not in assignments:
            cm.unassigned_tasks.append(task)
            continue
        
        assignment = assignments[wi_id]
        complexity = task.get("complexity", "Medium")
        hours = COMPLEXITY_TO_HOURS.get(complexity, 24)
        
        # Split hours between FE and BE (50/50 by default)
        fe_email = assignment.get("frontend", "")
        be_email = assignment.get("backend", "")
        
        if fe_email and be_email and fe_email != be_email:
            # Different devs - split hours
            fe_hours = hours * 0.5
            be_hours = hours * 0.5
        else:
            # Same dev or only one assigned - full hours
            fe_hours = hours if fe_email else 0
            be_hours = hours if be_email and be_email != fe_email else 0
        
        # Assign frontend
        if fe_email:
            cm.assign_task(task, fe_email, hours=fe_hours, force=True)
        
        # Assign backend (avoid double-counting if same person)
        if be_email and be_email != fe_email:
            cm.assign_task(task, be_email, hours=be_hours, force=True)
    
    return cm


def generate_capacity_report(
    tasks: List[Dict[str, Any]],
    assignments: Dict[int, Dict[str, str]],
    sprint_days: int = DEFAULT_SPRINT_DAYS,
    output_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """
    Generate a comprehensive capacity report.
    
    Returns dict with:
    - summary: overall capacity stats
    - developers: per-developer breakdown
    - overloads: list of overloaded developers
    - suggestions: redistribution suggestions
    """
    cm = calculate_sprint_capacity(tasks, assignments, sprint_days)
    
    report = cm.to_dict()
    report["unassigned_task_count"] = len(cm.unassigned_tasks)
    report["unassigned_tasks"] = [
        {"wi_id": t.get("id"), "title": t.get("title", "")[:60]}
        for t in cm.unassigned_tasks[:10]  # First 10 only
    ]
    
    # Save if path provided
    if output_path:
        cm.save(output_path)
    
    return report


def get_developer_workload_for_sprint_plan(
    tasks: List[Dict[str, Any]],
    role_suggestions: Dict[int, Dict[str, List[Dict]]],
    sprint_days: int = DEFAULT_SPRINT_DAYS,
) -> Tuple[Dict[str, float], Dict[str, float]]:
    """
    Pre-calculate workload distribution for sprint plan generation.
    
    Returns (fe_workload, be_workload) dicts mapping email -> assigned hours.
    """
    fe_workload: Dict[str, float] = defaultdict(float)
    be_workload: Dict[str, float] = defaultdict(float)
    
    for task in tasks:
        wi_id = task.get("id")
        complexity = task.get("complexity", "Medium")
        hours = COMPLEXITY_TO_HOURS.get(complexity, 24)
        
        if wi_id in role_suggestions:
            sug = role_suggestions[wi_id]
            
            # Get top FE suggestion
            fe_list = sug.get("frontend", [])
            if fe_list:
                fe_email = fe_list[0].get("developer", "").lower()
                if fe_email:
                    fe_workload[fe_email] += hours * 0.5  # Split hours
            
            # Get top BE suggestion
            be_list = sug.get("backend", [])
            if be_list:
                be_email = be_list[0].get("developer", "").lower()
                if be_email:
                    be_workload[be_email] += hours * 0.5
    
    return dict(fe_workload), dict(be_workload)


def format_capacity_for_display(capacity_report: Dict[str, Any]) -> str:
    """Format capacity report for text display."""
    lines = []
    summary = capacity_report.get("summary", {})
    
    lines.append("📊 SPRINT CAPACITY REPORT")
    lines.append("=" * 50)
    lines.append(f"Total Developers: {summary.get('total_developers', 0)}")
    lines.append(f"Total Capacity: {summary.get('total_capacity_hours', 0):.0f} hours")
    lines.append(f"Total Assigned: {summary.get('total_assigned_hours', 0):.0f} hours")
    lines.append(f"Total Available: {summary.get('total_available_hours', 0):.0f} hours")
    lines.append(f"Overall Utilization: {summary.get('overall_utilization', 0):.0%}")
    lines.append("")
    
    # Status breakdown
    lines.append("📈 STATUS BREAKDOWN:")
    lines.append(f"  🔴 Overloaded: {summary.get('overloaded_count', 0)}")
    lines.append(f"  🟡 Warning: {summary.get('warning_count', 0)}")
    lines.append(f"  🟢 Optimal: {summary.get('optimal_count', 0)}")
    lines.append(f"  ⚪ Underutilized: {summary.get('underutilized_count', 0)}")
    lines.append("")
    
    # Overloaded developers
    overloaded = summary.get("overloaded_developers", [])
    if overloaded:
        lines.append("⚠️ OVERLOADED DEVELOPERS:")
        devs = summary.get("developers", {})
        for email in overloaded:
            dev = devs.get(email, {})
            lines.append(f"  - {dev.get('name', email)}: {dev.get('assigned_hours', 0):.0f}h / "
                        f"{dev.get('total_capacity_hours', 0):.0f}h ({dev.get('utilization', 0):.0%})")
        lines.append("")
    
    # Redistribution suggestions
    suggestions = capacity_report.get("redistribution_suggestions", [])
    if suggestions:
        lines.append("💡 REDISTRIBUTION SUGGESTIONS:")
        for i, sug in enumerate(suggestions[:5], 1):
            lines.append(f"  {i}. {sug.get('reason', '')}")
        lines.append("")
    
    return "\n".join(lines)
