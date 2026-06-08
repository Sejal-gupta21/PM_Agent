"""
Context Resolver - Dynamic resolution of iteration, project, and team context.

This module provides pre-planning context resolution for:
1. Iteration Resolution (Sprint name → iterationId + iterationPath)
2. Project/Team Validation and Normalization
3. Context enrichment for reliable plan execution

CRITICAL: All sprint/iteration queries MUST resolve actual ADO metadata BEFORE
planning. This prevents LLM hallucination of iteration paths.

Integration:
- Called by Orchestrator BEFORE Light LLM pglanning
- Results injected into planner context
- Agents receive pre-resolved metadata, not placeholders

Author: PM Agent System
"""

import re
import json
import logging
from typing import Dict, Any, Optional, List, Tuple
from dataclasses import dataclass, field

logger = logging.getLogger("orchestrator.context_resolver")

# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION: Valid teams from FracPro-OPS (from ADO screenshots)
# ══════════════════════════════════════════════════════════════════════════════

VALID_TEAMS = [
    "Agile Technical Leads",
    "Archive",
    "BE Technical Leads",
    "DataPlatform",
    "DevOps Stars - Kanban 1",
    "FE Technical Leads",
    "FracPro Suite",
    "FracPro UX - Kanban",
    "FracPro-OPS Team",  # Default team
    "Global FP",
    "Global Management",
    "Internal Feedback",
    "Profrac Feedback",
    "WTT Action Items",
    "WTT Development",
    "WTT Estimation",
    "WTT Unrefined Backlog",
    "WTT.LINQX Action Items",
    "XOPS 25",
    "XOPS Bugs Enhancement",
]

# Aliases for fuzzy matching
TEAM_ALIASES = {
    "xops": "XOPS 25",
    "xops25": "XOPS 25",
    "xops 25": "XOPS 25",
    "xops bugs": "XOPS Bugs Enhancement",
    "fracpro": "FracPro Suite",
    "fracpro suite": "FracPro Suite",
    "fracpro-suite": "FracPro Suite",
    "wtt": "WTT Development",
    "wtt dev": "WTT Development",
    "wtt development": "WTT Development",
    "wtt estimation": "WTT Estimation",
    "global": "Global Management",
    "global management": "Global Management",
    "devops": "DevOps Stars - Kanban 1",
    "ux": "FracPro UX - Kanban",
    "fracpro ux": "FracPro UX - Kanban",
    "be leads": "BE Technical Leads",
    "fe leads": "FE Technical Leads",
    "backend": "BE Technical Leads",
    "frontend": "FE Technical Leads",
    "default": "XOPS 25",  # Main development team with most iterations
    "fracpro-ops": "FracPro-OPS Team",
    "datapro": "DataPlatform",
    "data platform": "DataPlatform",
    "dataplatform": "DataPlatform",
}

DEFAULT_PROJECT = "FracPro-OPS"
# Use XOPS 25 as default - it has the most iterations (23 total) vs FracPro-OPS Team (2 only)
DEFAULT_TEAM = "XOPS 25"
# Default sprint is current iteration when not specified
DEFAULT_SPRINT = "current"  # Resolves to @CurrentIteration

# ══════════════════════════════════════════════════════════════════════════════
# TEAM TO AREA PATH MAPPING (ADO Azure DevOps)
# Static mapping for fast resolution - avoids MCP calls for known teams
# ══════════════════════════════════════════════════════════════════════════════

TEAM_TO_AREA_PATH = {
    "XOPS 25": "FracPro-OPS\\Global Management\\WTT Development\\XOPS 25",
    "WTT Development": "FracPro-OPS\\Global Management\\WTT Development",
    "FracPro Suite": "FracPro-OPS\\Global FP\\FracPro Suite",
    "WTT Estimation": "FracPro-OPS\\Global FP\\WTT Estimation",
    "Global Management": "FracPro-OPS\\Global Management",
    "Global FP": "FracPro-OPS\\Global FP",
    "Agile Technical Leads": "FracPro-OPS\\Agile Technical Leads",
    "BE Technical Leads": "FracPro-OPS\\BE Technical Leads",
    "FE Technical Leads": "FracPro-OPS\\FE Technical Leads",
    "DevOps Stars - Kanban 1": "FracPro-OPS\\DevOps Stars - Kanban 1",
    "FracPro UX - Kanban": "FracPro-OPS\\FracPro UX - Kanban",
    "FracPro-OPS Team": "FracPro-OPS\\FracPro-OPS Team",
    "WTT Action Items": "FracPro-OPS\\WTT Action Items",
    "WTT Unrefined Backlog": "FracPro-OPS\\WTT Unrefined Backlog",
    "WTT.LINQX Action Items": "FracPro-OPS\\WTT.LINQX Action Items",
    "XOPS Bugs Enhancement": "FracPro-OPS\\Global Management\\WTT Development\\XOPS Bugs Enhancement",
    "Profrac Feedback": "FracPro-OPS\\Profrac Feedback",
    "Internal Feedback": "FracPro-OPS\\Internal Feedback",
    "Archive": "FracPro-OPS\\Archive",
    "DataPlatform": "FracPro-OPS\\DataPlatform",
}


def get_area_path_for_team(team: str) -> Optional[str]:
    """
    Get area path for a team (fast static lookup, no MCP calls).
    
    Args:
        team: Team name (e.g., "XOPS 25", "WTT Development")
        
    Returns:
        Area path (e.g., "FracPro-OPS\\Global Management\\WTT Development\\XOPS 25")
        or None if team not found
    """
    if not team:
        return None
    
    # Direct lookup
    if team in TEAM_TO_AREA_PATH:
        return TEAM_TO_AREA_PATH[team]
    
    # Case-insensitive lookup
    for team_name, area_path in TEAM_TO_AREA_PATH.items():
        if team_name.lower() == team.lower():
            return area_path
    
    # Check aliases
    if team.lower() in TEAM_ALIASES:
        normalized = TEAM_ALIASES[team.lower()]
        if normalized in TEAM_TO_AREA_PATH:
            return TEAM_TO_AREA_PATH[normalized]
    
    logger.warning(f"[CONTEXT_RESOLVER] Team '{team}' not found in area path mapping")
    return None


@dataclass
class ResolvedContext:
    """Result of context resolution."""
    # Core resolved values
    project: str = DEFAULT_PROJECT
    team: Optional[str] = None
    
    # Iteration resolution
    iteration_id: Optional[str] = None  # GUID or numeric ID
    iteration_path: Optional[str] = None  # Full path e.g., "FracPro-OPS\2026\Q1\26.01"
    iteration_name: Optional[str] = None  # Display name e.g., "Sprint 26.01"
    iteration_finish_date: Optional[str] = None  # Sprint end date e.g., "2026-02-28T00:00:00Z"
    
    # Area path resolution (for team-scoped queries without iteration)
    area_path: Optional[str] = None  # Full area path e.g., "FracPro-OPS\\Global Management\\WTT Development\\XOPS 25"
    
    # Resolution status
    iteration_resolved: bool = False
    team_resolved: bool = False
    project_resolved: bool = False
    area_path_resolved: bool = False
    
    # Resolution warnings (non-blocking)
    warnings: List[str] = field(default_factory=list)
    
    # Original extracted values (before validation)
    extracted_sprint_ref: Optional[str] = None
    extracted_team_ref: Optional[str] = None
    extracted_project_ref: Optional[str] = None
    
    def to_context_dict(self) -> Dict[str, Any]:
        """Convert to context dict for planner injection.
        
        CRITICAL: This method injects defaults for team when not specified.
        - If team is None, inject DEFAULT_TEAM ("XOPS 25")
        - ⚠️ FIX: Sprint is ONLY set if user explicitly mentioned it (extracted_sprint_ref is not None)
        - If area_path is None but team is set, auto-resolve from TEAM_TO_AREA_PATH
        """
        # Inject default team if not specified
        effective_team = self.team or DEFAULT_TEAM
        
        # ⚠️ FIX: Do NOT default to "current" sprint if user never mentioned sprint/iteration
        # Previous behavior: always defaulted to DEFAULT_SPRINT ("current")
        # New behavior: only set sprint if user explicitly mentioned it
        effective_sprint = self.extracted_sprint_ref  # None if user didn't mention sprint
        
        # Auto-resolve area_path if not set but team is available
        effective_area_path = self.area_path
        if not effective_area_path and effective_team:
            effective_area_path = get_area_path_for_team(effective_team)
        
        ctx = {
            "project": self.project,
            "team": effective_team,
            "sprint": effective_sprint,  # Added: sprint reference for planner
            "resolved_iteration_id": self.iteration_id,
            "resolved_iteration_path": self.iteration_path,
            "resolved_iteration_name": self.iteration_name,
            "resolved_iteration_finish_date": self.iteration_finish_date,
            "resolved_area_path": effective_area_path,  # Auto-resolved from team
            "iteration_resolution_status": "resolved" if self.iteration_resolved else "partial",
            "team_resolution_status": "resolved" if self.team_resolved else "partial",
            "project_resolution_status": "resolved" if self.project_resolved else "partial",
            "area_path_resolution_status": "resolved" if (effective_area_path is not None) else "partial",
            "_context_warnings": self.warnings,
            "_defaults_applied": {
                "team_defaulted": self.team is None,
                "sprint_defaulted": self.extracted_sprint_ref is None,
                "area_path_auto_resolved": self.area_path is None and effective_area_path is not None,
            }
        }
        return {k: v for k, v in ctx.items() if v is not None}


class ContextResolver:
    """
    Resolve project/team/iteration context from query and session.
    
    This component runs BEFORE Light LLM planning to provide grounded
    metadata. It uses MCP tools to fetch real ADO values.
    """
    
    def __init__(self, mcp_connector=None):
        """
        Initialize resolver.
        
        Args:
            mcp_connector: Optional MCPConnector for live ADO resolution.
                          If None, uses static validation only.
        """
        self.mcp = mcp_connector
        self._iterations_cache: Dict[str, List[Dict]] = {}  # project -> iterations
        self._teams_cache: Dict[str, List[Dict]] = {}  # project -> teams
    
    # ══════════════════════════════════════════════════════════════════════════
    # SPRINT/ITERATION EXTRACTION
    # ══════════════════════════════════════════════════════════════════════════
    
    def extract_sprint_reference(self, query: str) -> Optional[str]:
        """
        Extract sprint/iteration reference from query text.
        
        Returns:
            Sprint reference string (e.g., "26.01", "25.19", "@CurrentIteration")
            or None if no sprint mentioned.
        """
        q = query.lower()
        
        # Pattern 1: @CurrentIteration / @Current / current sprint
        if "@currentiteration" in q or "current sprint" in q or "current iteration" in q or "this sprint" in q:
            return "@CurrentIteration"
        
        # Pattern 2: Previous sprint
        if "previous sprint" in q or "last sprint" in q or "prior sprint" in q:
            return "@PreviousIteration"
        
        # Pattern 3: Explicit sprint number (e.g., "Sprint 26.01", "sprint 25.19", "26.1")
        patterns = [
            r'sprint\s*(\d+\.?\d*)',  # Sprint 26.01, Sprint 26
            r'iteration\s*(\d+\.?\d*)',  # Iteration 26.01
            r'\b(\d{2}\.\d{1,2})\b',  # 26.01, 25.19 (standalone)
        ]
        
        for pattern in patterns:
            match = re.search(pattern, q, re.IGNORECASE)
            if match:
                return match.group(1)
        
        return None
    
    def extract_team_reference(self, query: str) -> Optional[str]:
        """
        Extract team name from query text.
        
        Returns:
            Extracted team reference or None.
        """
        q = query.lower()
        
        # PRIORITY 1: Check for exact known team names first (most reliable)
        for team in VALID_TEAMS:
            if team.lower() in q:
                return team
        
        # PRIORITY 2: Check aliases
        for alias, team in TEAM_ALIASES.items():
            if alias.lower() in q:
                return team
        
        # PRIORITY 3: Pattern extraction with strict boundaries
        # These patterns prevent extracting "WTT Development in current sprint" as team name
        team_patterns = [
            # "for team WTT Development" - stops at known delimiters
            r'\bfor\s+team\s+([a-zA-Z0-9]+(?:\s+[a-zA-Z0-9]+)?(?:\s+-\s+[a-zA-Z0-9\s]+)?)',
            # "team WTT Development's work items"
            r"\bteam\s+([a-zA-Z0-9]+(?:\s+[a-zA-Z0-9]+)?)'s\b",
        ]
        
        for pattern in team_patterns:
            match = re.search(pattern, query, re.IGNORECASE)
            if match:
                extracted = match.group(1).strip()
                # Filter out common false positives
                excluded_words = ['in', 'for', 'the', 'sprint', 'current', 'previous', 'items', 'work']
                if extracted.lower() not in excluded_words:
                    return extracted
        
        return None
    
    def extract_project_reference(self, query: str) -> Optional[str]:
        """
        Extract project name from query text.
        
        Returns:
            Extracted project reference or None.
        """
        q = query.lower()
        
        # Known projects
        if "fracpro-ops" in q or "fracpro ops" in q:
            return "FracPro-OPS"
        
        # Generic pattern: "in project X", "project X"
        patterns = [
            r'\bproject\s+([a-zA-Z0-9\-]+)',
            r'\bin\s+([a-zA-Z0-9\-]+)\s+project',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, query, re.IGNORECASE)
            if match:
                return match.group(1)
        
        return None
    
    # ══════════════════════════════════════════════════════════════════════════
    # TEAM VALIDATION & NORMALIZATION
    # ══════════════════════════════════════════════════════════════════════════
    
    def normalize_team(self, team_ref: str) -> Tuple[Optional[str], bool]:
        """
        Normalize team reference to valid ADO team name.
        
        Args:
            team_ref: Raw team reference from query
            
        Returns:
            Tuple of (normalized_team_name, is_valid)
        """
        if not team_ref:
            return None, False
        
        team_lower = team_ref.lower().strip()
        
        # Check aliases first
        if team_lower in TEAM_ALIASES:
            return TEAM_ALIASES[team_lower], True
        
        # Check exact match (case-insensitive)
        for team in VALID_TEAMS:
            if team.lower() == team_lower:
                return team, True
        
        # Check partial match
        for team in VALID_TEAMS:
            if team_lower in team.lower() or team.lower() in team_lower:
                return team, True
        
        # No match found
        return team_ref, False  # Return original with invalid flag
    
    # ══════════════════════════════════════════════════════════════════════════
    # AREA PATH RESOLUTION (MCP-based)
    # ══════════════════════════════════════════════════════════════════════════
    
    async def resolve_area_path(
        self,
        team: str,
        project: str
    ) -> Optional[str]:
        """
        Resolve team name to its area path by querying live ADO work items.
        
        This is needed for search_workitem when no iteration is specified,
        since teams work within specific area paths in ADO.
        
        Args:
            team: Team name (e.g., "XOPS 25")
            project: Project name (e.g., "FracPro-OPS")
            
        Returns:
            Area path string (e.g., "FracPro-OPS\\Global Management\\WTT Development\\XOPS 25")
            or None if not found
        """
        if not self.mcp:
            logger.warning("[CONTEXT_RESOLVER] No MCP connector, cannot resolve area path")
            return None
        
        if not team or team == project:
            # Default team or no team - use project root
            return project
        
        try:
            # Search for work items containing the team name and extract area paths
            result = await self.mcp.call_tool("search_workitem", {
                "searchText": team,  # Search for team name in work items
                "project": [project],
                "top": 10
            })
            
            if not result:
                return None
            
            # Parse result
            data = json.loads(result) if isinstance(result, str) else result
            results = data.get("results", [])
            
            if not results:
                logger.info(f"[CONTEXT_RESOLVER] No work items found for team '{team}'")
                return None
            
            # Get work item IDs to fetch full details with area paths
            item_ids = []
            for item in results[:5]:
                wi_id = item.get("fields", {}).get("system.id")
                if wi_id:
                    try:
                        item_ids.append(int(wi_id))
                    except (ValueError, TypeError):
                        pass
            
            if not item_ids:
                return None
            
            # Fetch full details to get area paths
            full_result = await self.mcp.call_tool("wit_get_work_items_batch_by_ids", {
                "ids": item_ids,
                "project": project
            })
            
            if not full_result:
                return None
            
            full_data = json.loads(full_result) if isinstance(full_result, str) else full_result
            work_items = full_data.get("value", []) if isinstance(full_data, dict) else full_data
            
            # Extract area paths and find the one matching team
            area_paths = set()
            team_lower = team.lower()
            
            for wi in work_items:
                fields = wi.get("fields", {})
                area_path = fields.get("System.AreaPath", "")
                if area_path:
                    area_paths.add(area_path)
                    # Check if this area path contains the team name
                    if team_lower in area_path.lower():
                        logger.info(f"[CONTEXT_RESOLVER] Resolved area path for team '{team}': {area_path}")
                        return area_path
            
            # If no exact match, return the most common area path
            if area_paths:
                # Count occurrences
                from collections import Counter
                path_counts = Counter()
                for wi in work_items:
                    ap = wi.get("fields", {}).get("System.AreaPath", "")
                    if ap:
                        path_counts[ap] += 1
                if path_counts:
                    most_common = path_counts.most_common(1)[0][0]
                    logger.info(f"[CONTEXT_RESOLVER] Using most common area path for team '{team}': {most_common}")
                    return most_common
            
            return None
            
        except Exception as e:
            logger.error(f"[CONTEXT_RESOLVER] Error resolving area path for team '{team}': {e}")
            return None
    
    # ══════════════════════════════════════════════════════════════════════════
    # ITERATION RESOLUTION (MCP-based)
    # ══════════════════════════════════════════════════════════════════════════
    
    async def resolve_iteration(
        self,
        sprint_ref: str,
        project: str,
        team: Optional[str] = None
    ) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
        """
        Resolve sprint reference to actual iteration metadata.
        
        Args:
            sprint_ref: Sprint reference (e.g., "26.01", "@CurrentIteration")
            project: Project name
            team: Optional team name (for team-specific iterations)
            
        Returns:
            Tuple of (iteration_id, iteration_path, iteration_name, iteration_finish_date) or (None, None, None, None)
        """
        if not self.mcp:
            logger.warning("[CONTEXT_RESOLVER] No MCP connector, cannot resolve iteration")
            return None, None, None, None
        
        try:
            # Fetch iterations for team/project
            # Use team name in cache key, or "all" for project-level iterations
            cache_key = f"{project}:{team or 'all'}"
            
            if cache_key not in self._iterations_cache:
                # Choose the right tool based on whether team is specified
                if team:
                    # Use work_list_team_iterations for team-specific iterations
                    tool_name = "work_list_team_iterations"
                    args = {"project": project, "team": team}
                    logger.info(f"[CONTEXT_RESOLVER] Fetching team iterations for {project}/{team}")
                else:
                    # Use work_list_iterations for project-level iterations (doesn't require team)
                    tool_name = "work_list_iterations"
                    args = {"project": project}
                    logger.info(f"[CONTEXT_RESOLVER] Fetching project iterations for {project}")
                
                result = await self.mcp.call_tool(tool_name, args)
                
                # Parse result - MCP returns JSON as STRING
                iterations = []
                
                # First, parse the string to JSON
                if isinstance(result, str):
                    # Handle null/empty/error responses
                    if not result or result == 'null' or result.startswith("No ") or "MCP error" in result:
                        logger.warning(f"[CONTEXT_RESOLVER] Empty/error result from {tool_name}: {result[:150] if result else 'None'}")
                        self._iterations_cache[cache_key] = []
                    else:
                        try:
                            parsed = json.loads(result)
                            # Handle both direct list and {"value": [...]} formats
                            if isinstance(parsed, list):
                                iterations = parsed
                            elif isinstance(parsed, dict):
                                iterations = parsed.get("value", [])
                                if not iterations and "iterations" in parsed:
                                    iterations = parsed.get("iterations", [])
                            logger.info(f"[CONTEXT_RESOLVER] Parsed {len(iterations)} iterations from string response")
                        except json.JSONDecodeError as e:
                            logger.error(f"[CONTEXT_RESOLVER] JSON parse error: {e}, raw: {result[:200]}")
                elif isinstance(result, dict):
                    iterations = result.get("value", [])
                elif isinstance(result, list):
                    iterations = result
                
                self._iterations_cache[cache_key] = iterations
                logger.info(f"[CONTEXT_RESOLVER] Cached {len(iterations)} iterations for {cache_key}")
            
            iterations = self._iterations_cache[cache_key]
            
            if not iterations:
                logger.warning(f"[CONTEXT_RESOLVER] No iterations found for {cache_key}")
                return None, None, None
            
            # Handle @CurrentIteration
            if sprint_ref == "@CurrentIteration":
                # Find current iteration (where timeFrame=1 or "current", or based on dates)
                from datetime import datetime
                now = datetime.now()
                
                for it in iterations:
                    # Check timeFrame flag - can be integer (0=past, 1=current, 2=future) or string "current"
                    attrs = it.get("attributes", {})
                    tf = attrs.get("timeFrame")
                    if tf == "current" or tf == 1 or tf == "1":
                        return (
                            it.get("id"),
                            it.get("path"),
                            it.get("name"),
                            attrs.get("finishDate") or it.get("finishDate")
                        )
                    
                    # Check date range as fallback
                    start = attrs.get("startDate") or it.get("startDate")
                    end = attrs.get("finishDate") or it.get("finishDate")
                    if start and end:
                        try:
                            start_dt = datetime.fromisoformat(start.replace("Z", "+00:00").split("T")[0])
                            end_dt = datetime.fromisoformat(end.replace("Z", "+00:00").split("T")[0])
                            if start_dt <= now.replace(tzinfo=None) <= end_dt:
                                return (
                                    it.get("id"),
                                    it.get("path"),
                                    it.get("name"),
                                    end
                                )
                        except (ValueError, TypeError):
                            pass
                
                # Fallback: return first iteration
                if iterations:
                    it = iterations[0]
                    logger.warning(f"[CONTEXT_RESOLVER] Could not determine current iteration, using first: {it.get('name')}")
                    _fd = it.get("attributes", {}).get("finishDate") or it.get("finishDate")
                    return (it.get("id"), it.get("path"), it.get("name"), _fd)
            
            # Handle @PreviousIteration
            elif sprint_ref == "@PreviousIteration":
                # Find current, then return the one before it
                from datetime import datetime
                now = datetime.now()
                
                # Sort by start date descending
                sorted_iters = sorted(
                    iterations,
                    key=lambda x: x.get("attributes", {}).get("startDate") or x.get("startDate") or "",
                    reverse=True
                )
                
                # Find current index
                current_idx = -1
                for idx, it in enumerate(sorted_iters):
                    attrs = it.get("attributes", {})
                    if attrs.get("timeFrame") == "current":
                        current_idx = idx
                        break
                
                if current_idx >= 0 and current_idx + 1 < len(sorted_iters):
                    prev = sorted_iters[current_idx + 1]
                    _fd = prev.get("attributes", {}).get("finishDate") or prev.get("finishDate")
                    return (prev.get("id"), prev.get("path"), prev.get("name"), _fd)
                
                # Fallback: return second iteration if available
                if len(sorted_iters) > 1:
                    prev = sorted_iters[1]
                    _fd = prev.get("attributes", {}).get("finishDate") or prev.get("finishDate")
                    return (prev.get("id"), prev.get("path"), prev.get("name"), _fd)
            
            # Handle explicit sprint number (e.g., "26.01", "25.19")
            else:
                # Normalize sprint reference - strip leading zeros after dot
                sprint_normalized = sprint_ref.strip()
                
                # Normalize: "26.01" -> "26.1", "25.09" -> "25.9"
                parts = sprint_normalized.split(".")
                if len(parts) == 2:
                    major = parts[0].lstrip('0') or '0'
                    minor = parts[1].lstrip('0') or '0'  # Strip leading zeros from minor
                    sprint_normalized = f"{major}.{minor}"
                
                for it in iterations:
                    name = it.get("name", "").strip()
                    path = it.get("path", "")
                    
                    # Dynamically extract the numeric portion from iteration name
                    # Handles any prefix: "Sprint 26.3" → "26.3", "Iteration 26.3" → "26.3", "26.3" → "26.3"
                    numeric_match = re.search(r'(\d+\.\d+)', name)
                    if numeric_match:
                        raw_numeric = numeric_match.group(1)
                        nm_parts = raw_numeric.split(".")
                        nm_major = nm_parts[0].lstrip('0') or '0'
                        nm_minor = nm_parts[1].lstrip('0') or '0'
                        name_normalized = f"{nm_major}.{nm_minor}"
                    else:
                        # Fallback: normalize the full name as before
                        name_parts = name.split(".")
                        if len(name_parts) == 2:
                            name_major = name_parts[0].lstrip('0') or '0'
                            name_minor = name_parts[1].lstrip('0') or '0'
                            name_normalized = f"{name_major}.{name_minor}"
                        else:
                            name_normalized = name
                    
                    # Check if normalized sprint ref matches normalized name
                    if sprint_normalized == name_normalized:
                        _fd = it.get("attributes", {}).get("finishDate") or it.get("finishDate")
                        return (it.get("id"), it.get("path"), it.get("name"), _fd)
                    
                    # Check if sprint ref is in path — dynamically extract numeric segments
                    # e.g., path "FracPro-OPS\FracPro Suite\Sprint 26.3" or "...\26.3"
                    path_numeric_segments = re.findall(r'(\d+\.\d+)', path)
                    for seg in path_numeric_segments:
                        seg_parts = seg.split(".")
                        seg_normalized = f"{seg_parts[0].lstrip('0') or '0'}.{seg_parts[1].lstrip('0') or '0'}"
                        if sprint_normalized == seg_normalized:
                            _fd = it.get("attributes", {}).get("finishDate") or it.get("finishDate")
                            return (it.get("id"), it.get("path"), it.get("name"), _fd)
                
                logger.warning(f"[CONTEXT_RESOLVER] Sprint '{sprint_ref}' (normalized: {sprint_normalized}) not found in team iterations")
                
                # ───────────────────────────────────────────────────────────
                # FALLBACK: If sprint not found in team iterations, try
                # project-level iterations which include ALL iterations.
                # This handles sprints not assigned to the specified team.
                # ───────────────────────────────────────────────────────────
                if team:
                    logger.info(f"[CONTEXT_RESOLVER] Trying project-level fallback for sprint '{sprint_ref}'")
                    proj_cache_key = f"{project}:all"
                    if proj_cache_key not in self._iterations_cache:
                        try:
                            proj_result = await self.mcp.call_tool("work_list_iterations", {"project": project})
                            proj_iters = []
                            if isinstance(proj_result, str) and proj_result.strip() and not proj_result.startswith("No ") and "MCP error" not in proj_result:
                                proj_parsed = json.loads(proj_result)
                                proj_iters = proj_parsed if isinstance(proj_parsed, list) else proj_parsed.get("value", proj_parsed.get("iterations", []))
                            self._iterations_cache[proj_cache_key] = proj_iters
                        except Exception as e:
                            logger.warning(f"[CONTEXT_RESOLVER] Project-level fallback fetch failed: {e}")
                            self._iterations_cache[proj_cache_key] = []
                    
                    for it in self._iterations_cache[proj_cache_key]:
                        name = it.get("name", "").strip()
                        path = it.get("path", "")
                        
                        numeric_match = re.search(r'(\d+\.\d+)', name)
                        if numeric_match:
                            raw_numeric = numeric_match.group(1)
                            nm_ps = raw_numeric.split(".")
                            name_normalized = f"{nm_ps[0].lstrip('0') or '0'}.{nm_ps[1].lstrip('0') or '0'}"
                        else:
                            name_ps = name.split(".")
                            if len(name_ps) == 2:
                                name_normalized = f"{name_ps[0].lstrip('0') or '0'}.{name_ps[1].lstrip('0') or '0'}"
                            else:
                                name_normalized = name
                        
                        if sprint_normalized == name_normalized:
                            logger.info(f"[CONTEXT_RESOLVER] Project-level fallback matched '{sprint_ref}' to '{name}'")
                            _fd = it.get("attributes", {}).get("finishDate") or it.get("finishDate")
                            return (it.get("id"), it.get("path"), it.get("name"), _fd)
                        
                        path_numeric_segments = re.findall(r'(\d+\.\d+)', path)
                        for seg in path_numeric_segments:
                            seg_parts = seg.split(".")
                            seg_normalized = f"{seg_parts[0].lstrip('0') or '0'}.{seg_parts[1].lstrip('0') or '0'}"
                            if sprint_normalized == seg_normalized:
                                logger.info(f"[CONTEXT_RESOLVER] Project-level fallback matched '{sprint_ref}' via path '{path}'")
                                _fd = it.get("attributes", {}).get("finishDate") or it.get("finishDate")
                                return (it.get("id"), it.get("path"), it.get("name"), _fd)
                    
                    logger.warning(f"[CONTEXT_RESOLVER] Sprint '{sprint_ref}' not found even in project-level iterations")
        
        except Exception as e:
            logger.error(f"[CONTEXT_RESOLVER] Error resolving iteration: {e}")
        
        return None, None, None, None
    
    # ══════════════════════════════════════════════════════════════════════════
    # MAIN RESOLUTION ENTRY POINT
    # ══════════════════════════════════════════════════════════════════════════
    
    async def resolve(
        self,
        query: str,
        existing_context: Optional[Dict[str, Any]] = None
    ) -> ResolvedContext:
        """
        Resolve full context from query and existing session context.
        
        This is the main entry point called by Orchestrator before planning.
        
        Args:
            query: User query string
            existing_context: Existing session context (project, team, etc.)
            
        Returns:
            ResolvedContext with all resolved values and status flags
        """
        existing_context = existing_context or {}
        result = ResolvedContext()
        
        # ──────────────────────────────────────────────────────────────────
        # STEP 1: Extract references from query
        # ──────────────────────────────────────────────────────────────────
        result.extracted_sprint_ref = self.extract_sprint_reference(query)
        result.extracted_team_ref = self.extract_team_reference(query)
        result.extracted_project_ref = self.extract_project_reference(query)
        
        logger.info(f"[CONTEXT_RESOLVER] Extracted: sprint={result.extracted_sprint_ref}, "
                    f"team={result.extracted_team_ref}, project={result.extracted_project_ref}")
        
        # ──────────────────────────────────────────────────────────────────
        # STEP 2: Resolve Project
        # ──────────────────────────────────────────────────────────────────
        if result.extracted_project_ref:
            result.project = result.extracted_project_ref
            result.project_resolved = True
        elif existing_context.get("project"):
            result.project = existing_context["project"]
            result.project_resolved = True
        else:
            result.project = DEFAULT_PROJECT
            result.project_resolved = True  # Using known default
            result.warnings.append(f"Project not specified, defaulting to '{DEFAULT_PROJECT}'")
        
        # ──────────────────────────────────────────────────────────────────
        # STEP 3: Resolve Team
        # ──────────────────────────────────────────────────────────────────
        team_ref = result.extracted_team_ref or existing_context.get("team")
        if team_ref:
            normalized_team, is_valid = self.normalize_team(team_ref)
            result.team = normalized_team
            result.team_resolved = is_valid
            if not is_valid:
                result.warnings.append(f"Team '{team_ref}' not recognized, using as-is")
        else:
            # No team specified - check if query implies team scope
            q_lower = query.lower()
            needs_team = any(kw in q_lower for kw in [
                "sprint", "iteration", "capacity", "team",
                "current sprint", "previous sprint"
            ])
            
            if needs_team:
                # Use default team for iteration resolution (required for work_list_team_iterations)
                result.team = DEFAULT_TEAM
                result.team_resolved = True
                result.warnings.append(f"Team not specified for team-scoped query, using '{DEFAULT_TEAM}'")
            # Don't set default team for non-team queries
        
        # ──────────────────────────────────────────────────────────────────
        # STEP 4: Resolve Iteration (if sprint mentioned)
        # ──────────────────────────────────────────────────────────────────
        if result.extracted_sprint_ref:
            if self.mcp:
                iter_id, iter_path, iter_name, iter_finish_date = await self.resolve_iteration(
                    result.extracted_sprint_ref,
                    result.project,
                    result.team  # Will use default team if needed
                )
                
                if iter_id or iter_path:
                    result.iteration_id = iter_id
                    result.iteration_path = iter_path
                    result.iteration_name = iter_name
                    result.iteration_finish_date = iter_finish_date
                    result.iteration_resolved = True
                    logger.info(f"[CONTEXT_RESOLVER] Resolved iteration: id={iter_id}, path={iter_path}, name={iter_name}")
                else:
                    result.warnings.append(f"Could not resolve sprint '{result.extracted_sprint_ref}' to iteration")
            else:
                result.warnings.append("MCP not available, iteration not resolved")
        
        # ──────────────────────────────────────────────────────────────────
        # STEP 5: Resolve Area Path (for team-scoped queries)
        # ──────────────────────────────────────────────────────────────────
        # When a team is specified, we need the area path for:
        # - search_workitem: Filter work items by area path
        # - wit_get_work_items_for_iteration: Filter iteration items by team's area path
        # This applies whether or not an iteration is specified.
        if result.team and result.team != result.project:
            if self.mcp:
                area_path = await self.resolve_area_path(
                    result.team,
                    result.project
                )
                if area_path:
                    result.area_path = area_path
                    result.area_path_resolved = True
                    logger.info(f"[CONTEXT_RESOLVER] Resolved area path for team '{result.team}': {area_path}")
                else:
                    result.warnings.append(f"Could not resolve area path for team '{result.team}'")
        
        return result


# ══════════════════════════════════════════════════════════════════════════════
# PLAN VALIDATION UTILITIES
# ══════════════════════════════════════════════════════════════════════════════

def validate_iteration_id_format(iteration_id: str) -> bool:
    """
    Validate that iterationId is in correct format (GUID or numeric, NOT a path).
    
    Args:
        iteration_id: Value to validate
        
    Returns:
        True if format is valid, False if it looks like a path
    """
    if not iteration_id:
        return False
    
    # Valid formats: GUID or numeric
    guid_pattern = r'^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$'
    numeric_pattern = r'^\d+$'
    
    if re.match(guid_pattern, iteration_id, re.IGNORECASE):
        return True
    if re.match(numeric_pattern, iteration_id):
        return True
    
    # Special macros are valid
    if iteration_id.startswith("@"):
        return iteration_id in ["@CurrentIteration"]
    
    # If it contains backslash, it's a path (INVALID for iterationId)
    if "\\" in iteration_id or "/" in iteration_id:
        return False
    
    return True


def fix_plan_iteration_semantics(plan: Dict[str, Any], resolved_context: ResolvedContext) -> Dict[str, Any]:
    """
    Fix common iteration semantic errors in plans.
    
    Fixes:
    - Path in iterationId field → move to iterationPath, replace with resolved ID
    - Missing iterationPath when we have resolved path
    - LLM argument format issues (list strings like "['Bug']" → "Bug")
    
    Args:
        plan: Execution plan from LLM
        resolved_context: Pre-resolved context with iteration metadata
        
    Returns:
        Corrected plan
    """
    if not plan or not isinstance(plan, dict):
        return plan
    
    steps = plan.get("steps", [])
    if not isinstance(steps, list):
        return plan
    
    for step in steps:
        if not isinstance(step, dict):
            continue
        
        args = step.get("args", {})
        if not isinstance(args, dict):
            continue
        
        # ──────────────────────────────────────────────────────────────────
        # FIX: Normalize LLM argument format issues
        # LLM sometimes outputs ["Bug"] or ['Bug'] instead of "Bug"
        # ──────────────────────────────────────────────────────────────────
        for key in list(args.keys()):
            val = args[key]
            if isinstance(val, str):
                # Check for list-like strings: "['Bug']", '["Bug"]', etc.
                val_stripped = val.strip()
                if (val_stripped.startswith("['") and val_stripped.endswith("']")) or \
                   (val_stripped.startswith('["') and val_stripped.endswith('"]')):
                    # Extract the value from the list string
                    try:
                        import ast
                        parsed = ast.literal_eval(val_stripped)
                        if isinstance(parsed, list) and len(parsed) == 1:
                            args[key] = parsed[0]
                            logger.info(f"[PLAN_FIX] Normalized {key}: {val} → {args[key]}")
                    except (ValueError, SyntaxError):
                        # Try simple regex extraction
                        import re
                        match = re.match(r"[\[\(]['\"](.+?)['\"][\]\)]", val_stripped)
                        if match:
                            args[key] = match.group(1)
                            logger.info(f"[PLAN_FIX] Regex normalized {key}: {val} → {args[key]}")
        
        # ──────────────────────────────────────────────────────────────────
        # FIX: Normalize "state" argument for blocked queries
        # "Blocked" is not a valid state - use System.BlockedReason filter instead
        # ──────────────────────────────────────────────────────────────────
        if args.get("state") in ["Blocked", "blocked", "['Blocked']"]:
            # Change to Active and add blocked reason filter
            args["state"] = "Active"
            # Note: Blocked items should be found via tags or description search
            logger.info(f"[PLAN_FIX] Changed state 'Blocked' to 'Active' for blocked item search")
        
        # Check iterationId
        iter_id = args.get("iterationId")
        if iter_id and not validate_iteration_id_format(iter_id):
            # It's likely a path, move it
            logger.warning(f"[PLAN_FIX] iterationId '{iter_id}' looks like path, correcting...")
            
            # Move to iterationPath
            if not args.get("iterationPath"):
                args["iterationPath"] = iter_id
            
            # Replace with resolved ID if available
            if resolved_context.iteration_id:
                args["iterationId"] = resolved_context.iteration_id
                logger.info(f"[PLAN_FIX] Replaced with resolved iterationId: {resolved_context.iteration_id}")
            else:
                # Remove invalid iterationId - let tool use iterationPath
                del args["iterationId"]
                logger.info(f"[PLAN_FIX] Removed invalid iterationId, using iterationPath: {args.get('iterationPath')}")
        
        # Inject resolved iteration if missing
        if resolved_context.iteration_resolved:
            if not args.get("iterationId") and resolved_context.iteration_id:
                args["iterationId"] = resolved_context.iteration_id
            if not args.get("iterationPath") and resolved_context.iteration_path:
                args["iterationPath"] = resolved_context.iteration_path
        
        # Inject project/team if missing
        if not args.get("project") and resolved_context.project:
            args["project"] = resolved_context.project
        if not args.get("team") and resolved_context.team:
            args["team"] = resolved_context.team
    
    return plan


def validate_multi_step_completeness(plan: Dict[str, Any], query: str) -> List[str]:
    """
    Validate that multi-step plans have required enrichment steps.
    
    Args:
        plan: Execution plan
        query: Original query
        
    Returns:
        List of validation warnings (empty if valid)
    """
    warnings = []
    q_lower = query.lower()
    
    if not plan or not isinstance(plan, dict):
        return warnings
    
    steps = plan.get("steps", [])
    if not isinstance(steps, list):
        return warnings
    
    # Get all tools used
    tools_used = [s.get("tool") for s in steps if isinstance(s, dict)]
    
    # Check: Query mentions blocked but no filter step
    if "blocked" in q_lower:
        has_blocked_filter = any(
            s.get("args", {}).get("state") or 
            "blocked" in str(s.get("args", {}).get("searchText", "")).lower() or
            "blocked" in str(s.get("args", {})).lower()
            for s in steps if isinstance(s, dict)
        )
        if not has_blocked_filter:
            warnings.append("Query mentions 'blocked' but plan lacks blocked filter")
    
    # Check: Query mentions comments but no comments tool
    if "comment" in q_lower or "why" in q_lower or "reason" in q_lower:
        if "wit_get_work_item_comments" not in tools_used:
            warnings.append("Query implies blocker reasons but missing wit_get_work_item_comments step")
    
    # Check: Query mentions PR/build but no relations/PR tools
    if any(kw in q_lower for kw in ["pr", "pull request", "build", "linked"]):
        if "wit_get_work_item_relations" not in tools_used:
            warnings.append("Query mentions linked items but missing wit_get_work_item_relations step")
    
    # Check: Multi-step plan should end with synthesis
    if len(steps) > 1:
        last_step = steps[-1] if steps else {}
        if isinstance(last_step, dict):
            action = last_step.get("action", last_step.get("type", ""))
            if action not in ["synthesize", "synthesis", "llm_synthesis"]:
                warnings.append("Multi-step plan does not end with synthesis step")
    
    return warnings
