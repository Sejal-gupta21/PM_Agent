"""
Conversation Memory for PM Agent.

Provides session-based context tracking for multi-turn conversations:
- Remembers last project, team, iteration, area path
- Tracks conversation history for follow-up queries
- Enables context reuse across turns
"""

from typing import Dict, Any, Optional, List, Tuple
from dataclasses import dataclass, field
from datetime import datetime
import json
from utilities.logging_config import get_logger

logger = get_logger(__name__)

# ═══════════════════════════════════════════════════════════
# TEAM/PROJECT RESOLUTION
# 
# ⚠️ DYNAMIC RESOLUTION REQUIRED (Enterprise Contract)
# The system must resolve team→project mapping dynamically from ADO,
# not from static mappings. This mapping is DEPRECATED and kept only
# for fallback logging. Use get_project_for_team_dynamic() for live resolution.
#
# See: docs/AREA_PATH_DYNAMIC_RESOLUTION_PLAN.md
# ═══════════════════════════════════════════════════════════

# DEPRECATED: Static mapping kept for logging/debugging only
# DO NOT use this for live resolution - use ADO API instead
_LEGACY_TEAM_PROJECT_MAPPING = {
    # Format: 'team_name': 'project_name'
    # These are EXAMPLES only - live data should come from ADO
    'XOPS 25': 'FracPro-OPS',
    'XOPS': 'FracPro-OPS',
    'FracPro Suite': 'FracPro-OPS',
}


async def get_project_for_team_dynamic(team_name: str, mcp_connector=None) -> Tuple[str, str]:
    """
    Dynamically resolve team name to project using ADO API.
    
    This is the PREFERRED method for team→project resolution.
    No static mappings - queries live ADO data.
    
    Args:
        team_name: The team name to look up
        mcp_connector: Optional MCP connector for ADO queries
        
    Returns:
        Tuple of (project_name, resolution_method)
        - resolution_method: "dynamic" | "config_default" | "clarification_needed"
    """
    from config import config
    
    if not team_name:
        # No team specified - use config default
        default_project = getattr(config, 'pm_default_project', None) or getattr(config, 'ado_project', 'FracPro-OPS')
        logger.debug(f"No team specified, using config default: {default_project}")
        return (default_project, "config_default")
    
    # Try dynamic resolution via MCP if available
    if mcp_connector:
        try:
            import json
            # Search for work items with this team to find the project
            result = await mcp_connector.call_tool("search_workitem", {
                "searchText": team_name,
                "top": 5
            })
            
            if result:
                data = json.loads(result) if isinstance(result, str) else result
                results = data.get("results", [])
                
                if results:
                    # Extract project from first result
                    for item in results:
                        project = item.get("project", {}).get("name")
                        if project:
                            logger.info(f"Dynamically resolved team '{team_name}' to project '{project}'")
                            return (project, "dynamic")
        except Exception as e:
            logger.warning(f"Dynamic team→project resolution failed: {e}")
    
    # Fallback to config default (no static mapping used)
    default_project = getattr(config, 'pm_default_project', None) or getattr(config, 'ado_project', 'FracPro-OPS')
    logger.warning(f"Could not dynamically resolve team '{team_name}', using config default: {default_project}")
    return (default_project, "config_default")


def get_project_for_team(team_name: str) -> str:
    """
    DEPRECATED: Synchronous team→project lookup.
    
    ⚠️ WARNING: This function uses config defaults only.
    For dynamic resolution, use get_project_for_team_dynamic() instead.
    
    This prevents confusion when team names like 'XOPS 25' are mistakenly
    treated as project names.
    
    Args:
        team_name: The team name to look up
        
    Returns:
        Project name from config (NOT from static mapping)
    """
    from config import config
    
    # Log deprecation warning for static mapping usage
    if team_name in _LEGACY_TEAM_PROJECT_MAPPING:
        logger.debug(f"Team '{team_name}' found in legacy mapping (deprecated) - using config default instead")
    
    # Return config default - NO STATIC MAPPING
    default_project = getattr(config, 'pm_default_project', None) or getattr(config, 'ado_project', 'FracPro-OPS')
    return default_project


@dataclass
class ConversationContext:
    """Holds conversation state for a single session."""
    
    # Core PM context (sticky across turns)
    project: Optional[str] = None
    team: Optional[str] = None
    iteration: Optional[str] = None
    area_path: Optional[str] = None
    
    # Last query context (for follow-ups)
    last_query: Optional[str] = None
    last_tool: Optional[str] = None
    last_args: Optional[Dict[str, Any]] = None
    last_result_count: int = 0
    
    # Pending clarification context
    pending_clarification: Optional[Dict[str, Any]] = None  # Stores original query when clarification is requested
    
    # Conversation history (last N turns)
    history: List[Dict[str, str]] = field(default_factory=list)
    max_history: int = 10
    
    # Metadata
    created_at: datetime = field(default_factory=datetime.utcnow)
    last_active: datetime = field(default_factory=datetime.utcnow)
    
    def set_pending_clarification(self, original_query: str, clarification_type: str, ambiguous_token: str = None):
        """Store pending clarification context when agent asks for more info."""
        self.pending_clarification = {
            "original_query": original_query,
            "clarification_type": clarification_type,  # "identity", "project", "iteration", etc.
            "ambiguous_token": ambiguous_token,
            "timestamp": datetime.utcnow().isoformat()
        }
        logger.debug(f"Set pending clarification: {self.pending_clarification}")
    
    def get_pending_clarification(self) -> Optional[Dict[str, Any]]:
        """Get pending clarification if any."""
        return self.pending_clarification
    
    def clear_pending_clarification(self):
        """Clear pending clarification after it's resolved."""
        self.pending_clarification = None
    
    def resolve_clarification(self, user_response: str) -> Optional[str]:
        """
        Resolve a pending clarification by combining original query with user's response.
        Returns the reconstructed query, or None if no pending clarification.
        """
        if not self.pending_clarification:
            return None
        
        original = self.pending_clarification.get("original_query", "")
        clarification_type = self.pending_clarification.get("clarification_type", "")
        ambiguous_token = self.pending_clarification.get("ambiguous_token", "")
        
        # Clear the pending state
        self.clear_pending_clarification()
        
        # Reconstruct query based on clarification type
        if clarification_type == "identity" and ambiguous_token:
            # Replace the ambiguous token with the full name
            # e.g., "work items assigned to richa" + "Richa Rathore" -> "work items assigned to Richa Rathore"
            reconstructed = original.lower().replace(ambiguous_token.lower(), user_response)
            if reconstructed == original.lower():
                # If replacement didn't work, append context
                reconstructed = f"{original} (name: {user_response})"
            logger.info(f"Resolved identity clarification: '{original}' -> '{reconstructed}'")
            return reconstructed
        
        # For other types, just combine queries
        return f"{original} {user_response}"
    
    def update_from_query(self, query: str, tool: str = None, args: Dict = None, result_count: int = 0):
        """Update context after a query is processed."""
        self.last_query = query
        self.last_tool = tool
        self.last_args = args
        self.last_result_count = result_count
        self.last_active = datetime.utcnow()
        
        # Add to history
        self.history.append({
            "role": "user",
            "content": query,
            "timestamp": datetime.utcnow().isoformat()
        })
        
        # Trim history
        if len(self.history) > self.max_history * 2:
            self.history = self.history[-self.max_history * 2:]
        
        # Extract sticky context from args
        if args:
            if "project" in args:
                proj = args["project"]
                self.project = proj[0] if isinstance(proj, list) else proj
            if "team" in args:
                self.team = args["team"]
            if "iteration" in args:
                self.iteration = args["iteration"]
            if "areaPath" in args:
                ap = args["areaPath"]
                # Handle empty lists gracefully (Enterprise Contract - no assumptions)
                if isinstance(ap, list):
                    self.area_path = ap[0] if ap else ""
                else:
                    self.area_path = ap if ap else ""
    
    def add_response(self, response: str):
        """Add assistant response to history."""
        self.history.append({
            "role": "assistant",
            "content": response[:500],  # Truncate for memory efficiency
            "timestamp": datetime.utcnow().isoformat()
        })
    
    def get_context_for_llm(self) -> Dict[str, Any]:
        """Get context dict for LLM planner."""
        ctx = {}
        if self.project:
            ctx["project"] = self.project
        if self.team:
            ctx["team"] = self.team
        if self.iteration:
            ctx["iteration"] = self.iteration
        if self.area_path:
            ctx["area_path"] = self.area_path
        
        # Add last query info for follow-ups
        if self.last_query:
            ctx["last_query"] = self.last_query
        if self.last_tool:
            ctx["last_tool"] = self.last_tool
        if self.last_args:
            ctx["last_args"] = self.last_args
        
        return ctx
    
    def is_follow_up(self, query: str) -> bool:
        """Check if query is likely a follow-up to previous query."""
        follow_up_indicators = [
            "only", "just", "filter", "show me", "what about",
            "also", "and", "but", "instead", "more", "less",
            "high priority", "active", "closed", "bugs", "stories"
        ]
        query_lower = query.lower()
        
        # Short queries that reference previous context
        if len(query.split()) <= 5 and self.last_query:
            for indicator in follow_up_indicators:
                if indicator in query_lower:
                    return True
        
        return False
    
    def extract_team_from_query(self, query: str) -> Optional[str]:
        """
        Extract team name from query text using pattern matching.
        
        Supports patterns like:
        - "for the Development team"
        - "XOPS team backlog"
        - "team Platform sprint"
        - "team FracPro Suite" (multi-word teams)
        - Known team names (XOPS, Development, etc.)
        
        Returns:
            Extracted team name or None if not found
        """
        import re
        
        # FIX #1: Improved regex patterns that correctly capture multi-word team names
        
        # Pattern 0: "for X team" where team name comes BEFORE the word "team"
        # e.g., "iteration report for WTT Development team in FracPro-OPS"
        match = re.search(r'for\s+([A-Za-z0-9][A-Za-z0-9\s\-]+?)\s+team\b', query, re.IGNORECASE)
        if match:
            team = match.group(1).strip()
            team = re.sub(r'\s+(?:the|in|with|for)$', '', team, flags=re.IGNORECASE).strip()
            if team and len(team) > 1:
                logger.debug(f"[TEAM_EXTRACTION] Pattern 0 matched: '{team}'")
                return team
        
        # Pattern 1: "for team X" where X can be multiple words until sprint/iteration/current
        match = re.search(r'for\s+team\s+([A-Za-z0-9][A-Za-z0-9\s\-]+?)(?:\s+(?:sprint|iteration|current|in|with|last|previous)|$)', query, re.IGNORECASE)
        if match:
            team = match.group(1).strip()
            # Clean up trailing words
            team = re.sub(r'\s+(?:team|the|in|with|for)$', '', team, flags=re.IGNORECASE).strip()
            if team and len(team) > 1:
                logger.debug(f"[TEAM_EXTRACTION] Pattern 1 matched: '{team}'")
                return team
        
        # Pattern 2: "team X" where X is captured until sprint/iteration keywords
        match = re.search(r'team\s+([A-Za-z0-9][A-Za-z0-9\s\-]+?)(?:\s+(?:sprint|iteration|in|with|backlog|capacity|status|items)|$)', query, re.IGNORECASE)
        if match:
            team = match.group(1).strip()
            # Clean up trailing words
            team = re.sub(r'\s+(?:team|the|in|with|for)$', '', team, flags=re.IGNORECASE).strip()
            if team and len(team) > 1:
                logger.debug(f"[TEAM_EXTRACTION] Pattern 2 matched: '{team}'")
                return team
        
        # Pattern 3: Known team names (multi-word teams like "FracPro Suite", "XOPS 25")
        known_teams = [
            'FracPro Suite', 'XOPS 25', 'XOPS Bugs Enhancement', 'WTT Development',
            'WTT Estimation', 'BE Technical Leads', 'FE Technical Leads',
            'Global Management', 'DataPlatform', 'FracPro-OPS Team',
            'XOPS', 'Development', 'QA', 'DevOps', 
            'Platform', 'Frontend', 'Backend', 'Mobile', 'Kanban'
        ]
        for team in known_teams:
            if re.search(rf'\b{re.escape(team)}\b', query, re.IGNORECASE):
                logger.debug(f"[TEAM_EXTRACTION] Known team matched: '{team}'")
                return team
        
        logger.debug(f"[TEAM_EXTRACTION] No team found in: {query}")
        return None
    
    def extract_iteration_from_query(self, query: str) -> Optional[str]:
        """
        Extract iteration hints from query text.
        
        Supports patterns like:
        - "current sprint" → @CurrentIteration
        - "last sprint" → @PreviousIteration
        - "sprint 25.24" → "25.24"
        
        Returns:
            Iteration identifier or None if not found
        """
        import re
        
        q = query.lower()
        
        # Current sprint patterns
        if any(kw in q for kw in ['current sprint', 'this sprint', 'my sprint', 'our sprint', 'active sprint']):
            return '@CurrentIteration'
        
        # Previous sprint patterns
        if any(kw in q for kw in ['last sprint', 'previous sprint', 'prior sprint', 'prev sprint']):
            return '@PreviousIteration'
        
        # Numeric sprint patterns (e.g., "sprint 25.24" or "iteration 25")
        sprint_match = re.search(r'(?:sprint|iteration)\s*(\d+(?:\.\d+)?)', q)
        if sprint_match:
            return sprint_match.group(1)
        
        return None
    
    def update_context_from_query_text(self, query: str) -> Dict[str, Any]:
        """
        Extract and update context from the query text itself.
        
        Args:
            query: The user's natural language query
            
        Returns:
            Dict of extracted context that was updated
        """
        import re
        extracted = {}
        
        # Extract team
        team = self.extract_team_from_query(query)
        if team:
            self.team = team
            extracted['team'] = team
            logger.debug(f"[ConversationContext] Extracted team from query: {team}")
        
        # Extract iteration
        iteration = self.extract_iteration_from_query(query)
        if iteration:
            self.iteration = iteration
            extracted['iteration'] = iteration
            logger.debug(f"[ConversationContext] Extracted iteration from query: {iteration}")
        
        # Extract project (pattern: "in project X" or known project names)
        # STOP WORDS: Never extract common English words as project names
        _PROJECT_STOP_WORDS = {'and', 'or', 'the', 'a', 'an', 'for', 'in', 'on', 'to', 'with', 'from', 'by', 'at', 'of', 'is', 'are', 'was', 'were', 'be', 'been', 'being', 'this', 'that', 'it', 'its', 'my', 'our', 'your', 'their'}
        
        # First check known project names (higher priority - prevents bad regex matches)
        known_projects = ['FracPro-OPS', 'FracProOPS', 'XOPS', 'Platform']
        project_found = False
        for proj in known_projects:
            if re.search(rf'\b{re.escape(proj)}\b', query, re.IGNORECASE):
                self.project = proj
                extracted['project'] = proj
                project_found = True
                break
        
        # Also check for multi-word project names (e.g., "fracpro ops" → "FracPro-OPS")
        if not project_found:
            _PROJECT_ALIASES = {
                'fracpro ops': 'FracPro-OPS',
                'fracpro-ops': 'FracPro-OPS',
                'fracproops': 'FracPro-OPS',
            }
            q_lower = query.lower()
            for alias, canonical in _PROJECT_ALIASES.items():
                if alias in q_lower:
                    self.project = canonical
                    extracted['project'] = canonical
                    project_found = True
                    break
        
        # Fallback: regex pattern "project X"
        if not project_found:
            project_match = re.search(r'\b(?:in\s+)?project\s+([A-Za-z0-9_-]+(?:\s*-\s*[A-Za-z0-9_-]+)?)', query, re.IGNORECASE)
            if project_match:
                candidate = project_match.group(1).strip()
                if candidate.lower() not in _PROJECT_STOP_WORDS:
                    self.project = candidate
                    extracted['project'] = self.project
                else:
                    logger.debug(f"[ConversationContext] Rejected stop-word project candidate: '{candidate}'")
        
        return extracted


class ConversationMemory:
    """
    Manages conversation contexts across multiple sessions.
    
    Thread-safe in-memory store for conversation state.
    """
    
    def __init__(self, default_ttl_hours: int = 24):
        self._sessions: Dict[str, ConversationContext] = {}
        self.default_ttl_hours = default_ttl_hours
    
    def get_or_create(self, session_id: str) -> ConversationContext:
        """Get existing context or create new one."""
        if not session_id:
            session_id = "default"
        
        if session_id not in self._sessions:
            self._sessions[session_id] = ConversationContext()
            logger.debug(f"Created new session: {session_id}")
        
        return self._sessions[session_id]
    
    def get(self, session_id: str) -> Optional[ConversationContext]:
        """Get context if exists."""
        return self._sessions.get(session_id)
    
    def clear(self, session_id: str):
        """Clear a session's context."""
        if session_id in self._sessions:
            del self._sessions[session_id]
            logger.debug(f"Cleared session: {session_id}")
    
    def cleanup_stale(self):
        """Remove stale sessions (older than TTL)."""
        now = datetime.utcnow()
        stale = []
        for sid, ctx in self._sessions.items():
            age_hours = (now - ctx.last_active).total_seconds() / 3600
            if age_hours > self.default_ttl_hours:
                stale.append(sid)
        
        for sid in stale:
            del self._sessions[sid]
        
        if stale:
            logger.debug(f"Cleaned up {len(stale)} stale sessions")


async def validate_team_exists(team_name: str, project: str, mcp_connector) -> Tuple[bool, Optional[str]]:
    """
    Check if extracted team name actually exists in ADO.
    
    Args:
        team_name: The team name to validate
        project: The project to search in
        mcp_connector: MCP connector for calling ADO APIs
        
    Returns:
        Tuple of (is_valid: bool, matched_team_name: Optional[str])
    """
    try:
        response = await mcp_connector.call_tool('core_list_project_teams', {'project': project})
        if response and response != 'null':
            teams_data = json.loads(response) if isinstance(response, str) else response
            available_teams = []
            
            if isinstance(teams_data, dict) and 'value' in teams_data:
                available_teams = [t.get('name', '') for t in teams_data.get('value', [])]
            elif isinstance(teams_data, list):
                available_teams = [t.get('name', '') for t in teams_data if isinstance(t, dict)]
            
            logger.debug(f"[TEAM_VALIDATION] Available teams: {available_teams}")
            
            # Try exact match first (case-sensitive)
            if team_name in available_teams:
                logger.info(f"[TEAM_VALIDATION] Exact match found: {team_name}")
                return True, team_name
            
            # Try case-insensitive match
            team_lower = team_name.lower()
            for available in available_teams:
                if available.lower() == team_lower:
                    logger.info(f"[TEAM_VALIDATION] Case-insensitive match: {team_name} → {available}")
                    return True, available
            
            # Try partial match (extracted team is subset of available team)
            team_words = set(team_name.lower().split())
            for available in available_teams:
                available_words = set(available.lower().split())
                # If all words from extracted team appear in available team
                if team_words.issubset(available_words):
                    logger.info(f"[TEAM_VALIDATION] Partial match: {team_name} → {available}")
                    return True, available
            
            logger.warning(f"[TEAM_VALIDATION] No match found for '{team_name}' in project '{project}'")
            logger.debug(f"[TEAM_VALIDATION] Available teams were: {', '.join(available_teams[:10])}")
            return False, None
        
        logger.warning(f"[TEAM_VALIDATION] Could not fetch teams for project '{project}'")
        return False, None
    except Exception as e:
        logger.error(f"[TEAM_VALIDATION] Validation failed: {e}")
        return False, None


# Global instance for the PM Agent
_memory = ConversationMemory()


def get_conversation_memory() -> ConversationMemory:
    """Get the global conversation memory instance."""
    return _memory
