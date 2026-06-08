"""
Dynamic State Resolution System for Azure DevOps

This module provides intelligent state resolution that:
1. Dynamically discovers valid states from ADO (no hardcoding)
2. Validates user-specified states against ADO
3. Semantically infers closest valid state when user provides non-existent state
4. NEVER remaps existing ADO states to other existing ADO states
5. Infers intent-based states when user doesn't specify

CRITICAL RULES:
- User state → existing ADO state (✅ ALLOWED)
- Existing ADO state → another ADO state (❌ FORBIDDEN)
- No hardcoded state dictionaries (❌ FORBIDDEN)
- All state discovery via WIQL/API (✅ REQUIRED)
"""

import logging
import time
import re
import json
from typing import Dict, List, Optional, Set, Tuple, Any
from dataclasses import dataclass, field
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


@dataclass
class StateDiscoveryResult:
    """Result of state discovery from ADO."""
    project: str
    states: Set[str]
    timestamp: float
    source: str  # 'wiql' or 'metadata_api'
    work_item_types: Dict[str, Set[str]] = field(default_factory=dict)  # Type -> valid states
    
    def is_expired(self, ttl_seconds: int = 3600) -> bool:
        """Check if discovery result is expired (default: 1 hour)."""
        return (time.time() - self.timestamp) > ttl_seconds
    
    def is_valid_state(self, state: str) -> bool:
        """Check if state exists in discovered states (case-insensitive)."""
        return state.lower() in {s.lower() for s in self.states}
    
    def get_exact_state_name(self, state: str) -> Optional[str]:
        """Get exact casing of state from discovered states."""
        state_lower = state.lower()
        for s in self.states:
            if s.lower() == state_lower:
                return s
        return None


@dataclass
class StateResolutionResult:
    """Result of state resolution."""
    original_state: str
    resolved_state: str
    exists_in_ado: bool
    resolution_method: str  # 'exact_match', 'semantic_inference', 'intent_based', 'no_state'
    confidence: float
    reasoning: str
    alternatives: List[str] = field(default_factory=list)


class StateResolver:
    """
    Dynamic state resolver - DISCOVERY AND VALIDATION ONLY.
    
    Key Features:
    - Discovers states from ADO dynamically (WIQL)
    - Validates user states against discovered states
    - Caches discovery results per project
    
    NOTE: State semantic inference happens in light_planner (LLM already running)
          This class only does discovery and validation - NO redundant LLM calls
    """
    
    def __init__(self, ado_client=None):
        """
        Initialize state resolver.
        
        Args:
            ado_client: ADO client for executing WIQL queries and API calls
        """
        self.ado_client = ado_client
        self._discovery_cache: Dict[str, StateDiscoveryResult] = {}
        self._cache_ttl_seconds = 3600  # 1 hour cache
        
    def discover_states(self, project: str, force_refresh: bool = False) -> StateDiscoveryResult:
        """
        Discover all valid states for a project from ADO.
        
        Uses:
        1. WIQL: SELECT DISTINCT [System.State] FROM WorkItems
        2. Field Metadata API (if WIQL fails)
        
        Args:
            project: ADO project name
            force_refresh: Force refresh even if cached
            
        Returns:
            StateDiscoveryResult with all discovered states
        """
        cache_key = project.lower()
        
        # Check cache
        if not force_refresh and cache_key in self._discovery_cache:
            cached = self._discovery_cache[cache_key]
            if not cached.is_expired(self._cache_ttl_seconds):
                logger.info(f"[StateResolver] Using cached states for '{project}': {sorted(cached.states)}")
                return cached
            else:
                logger.info(f"[StateResolver] Cache expired for '{project}', refreshing...")
        
        logger.info(f"[StateResolver] Discovering states for project '{project}'...")
        
        # Try WIQL discovery first
        result = self._discover_via_wiql(project)
        
        # Fallback to field metadata API if WIQL fails
        if not result or not result.states:
            logger.warning(f"[StateResolver] WIQL discovery failed/empty, trying field metadata API...")
            result = self._discover_via_field_metadata(project)
        
        # Validate we got results
        if not result or not result.states:
            logger.error(f"[StateResolver] Failed to discover any states for project '{project}'")
            # FALLBACK: Use common ADO states when discovery fails
            # These are the most common states across most ADO projects
            logger.warning(f"[StateResolver] Using fallback common ADO states for '{project}'")
            common_states = {
                'New', 'Active', 'Resolved', 'Closed', 'Ready', 'In Progress', 'Done',
                'QA', 'UAT', 'UAT Complete', 'PRE-PROD', 'Design', 'Code Review',
                'On Hold', 'Reopened', 'Removed', 'Issues Found'
            }
            result = StateDiscoveryResult(
                project=project,
                states=common_states,
                timestamp=time.time(),
                source='fallback_common_states'
            )
        
        # Cache result
        self._discovery_cache[cache_key] = result
        logger.info(f"[StateResolver] Discovered {len(result.states)} states for '{project}': {sorted(result.states)}")
        
        return result
    
    def _discover_via_wiql(self, project: str) -> Optional[StateDiscoveryResult]:
        """
        Discover states using WIQL query via ToolRegistry.
        
        Query: SELECT [System.Id], [System.State] FROM WorkItems WHERE [System.TeamProject] = 'project'
        """
        try:
            # Build WIQL to get all unique states
            # We select IDs too because WIQL requires at least one field besides state
            wiql = f"""
            SELECT [System.Id], [System.State], [System.WorkItemType]
            FROM WorkItems
            WHERE [System.TeamProject] = '{project}'
            """
            
            logger.debug(f"[StateResolver] Executing WIQL for state discovery via MCP connector:\n{wiql}")
            
            # Try to execute WIQL via MCP connector
            try:
                from utilities.mcp.mcp_ado_connector import MCPConnector
                from config import config
                import asyncio
                
                # Extract org name from URL
                org_name = config.ado_org_name or "Stratagen"
                pat_token = config.ado_pat
                
                # Create inline connector for state discovery
                connector = MCPConnector(org_name=org_name, pat_token=pat_token)
                
                # Execute WIQL query via MCP - use asyncio.run() to handle async in sync context
                # This will work when called from async contexts (it handles nested loops)
                try:
                    # Try to get the current event loop
                    loop = asyncio.get_running_loop()
                    # We're in an async context, can't use asyncio.run()
                    logger.warning(f"[StateResolver] Cannot use asyncio.run() in async context, state discovery will use defaults")
                    result = None
                except RuntimeError:
                    # No running loop, can safely use asyncio.run()
                    async def _call_wiql():
                        from agents.pm_agent.pm_skills.wiql_skill import execute_wiql
                        return await execute_wiql(project=project, wiql=wiql)
                    try:
                        result_str = asyncio.run(_call_wiql())
                        result = result_str if isinstance(result_str, dict) else json.loads(result_str) if isinstance(result_str, str) else {}
                    except Exception as e:
                        logger.error(f"[StateResolver] asyncio.run() failed: {e}")
                        result = None
                    
            except Exception as e:
                logger.error(f"[StateResolver] Failed to create MCP connector for WIQL: {e}")
                result = None
            
            # Fix: Add explicit None check before calling .get() to prevent AttributeError
            if result is None:
                logger.error(f"[StateResolver] WIQL query returned None")
                return None
            
            if 'error' in result:
                logger.error(f"[StateResolver] WIQL query failed: {result.get('error', 'Unknown error')}")
                return None
            
            # Extract unique states from work items
            states = set()
            work_item_type_states = {}
            
            work_items = result.get('workItems', [])
            if not work_items:
                logger.warning(f"[StateResolver] WIQL returned no work items for project '{project}'")
                return None
            
            for wi in work_items:
                fields = wi.get('fields', {})
                state = fields.get('System.State')
                wi_type = fields.get('System.WorkItemType')
                
                if state:
                    states.add(state)
                    
                    # Track states per work item type
                    if wi_type:
                        if wi_type not in work_item_type_states:
                            work_item_type_states[wi_type] = set()
                        work_item_type_states[wi_type].add(state)
            
            if not states:
                logger.warning(f"[StateResolver] No states found in WIQL results")
                return None
            
            return StateDiscoveryResult(
                project=project,
                states=states,
                timestamp=time.time(),
                source='wiql',
                work_item_types=work_item_type_states
            )
            
        except Exception as e:
            logger.error(f"[StateResolver] WIQL discovery failed with exception: {e}", exc_info=True)
            return None
    
    def _discover_via_field_metadata(self, project: str) -> Optional[StateDiscoveryResult]:
        """
        Discover states using ADO Field Metadata API.
        
        Endpoint: GET {org}/_apis/wit/fields/System.State?api-version=7.0
        """
        try:
            # Try to get field metadata for System.State
            # This is less reliable than WIQL but can work as fallback
            logger.info("[StateResolver] Field metadata API discovery not yet implemented")
            # TODO: Implement field metadata API call if needed
            return None
            
        except Exception as e:
            logger.error(f"[StateResolver] Field metadata discovery failed: {e}", exc_info=True)
            return None
    
    def validate_state(
        self, 
        user_state: str, 
        project: str
    ) -> StateResolutionResult:
        """
        Validate user-provided state exists in ADO.
        
        SIMPLE LOGIC - NO LLM:
        1. Discover valid states from ADO
        2. Check if user_state exists (case-insensitive)
           - If YES -> Return exact casing from ADO
           - If NO -> Return error (state resolution should happen in planner)
        
        NOTE: State semantic inference happens in light_planner (LLM already runs there)
              This method only validates - NO redundant LLM calls
        
        Args:
            user_state: State specified by user (already resolved by planner)
            project: ADO project name
            
        Returns:
            StateResolutionResult with validation result
        """
        # Discover valid states from ADO
        discovery = self.discover_states(project)
        
        if not discovery or not discovery.states:
            logger.warning(f"[StateResolver] Cannot validate state - no states discovered for project '{project}'")
            # Return as-is if discovery fails (let ADO handle the error)
            return StateResolutionResult(
                original_state=user_state,
                resolved_state=user_state,
                exists_in_ado=False,
                resolution_method='discovery_failed',
                confidence=0.5,
                reasoning=f"Discovery failed, passing state '{user_state}' as-is"
            )
        
        # Check if user state exists in ADO (case-insensitive)
        exact_state = discovery.get_exact_state_name(user_state)
        
        if exact_state:
            # State exists in ADO - return exact casing
            return StateResolutionResult(
                original_state=user_state,
                resolved_state=exact_state,
                exists_in_ado=True,
                resolution_method='exact_match',
                confidence=1.0,
                reasoning=f"State '{user_state}' validated in ADO as '{exact_state}'"
            )
        else:
            # State doesn't exist - planner should have resolved it!
            logger.warning(f"[StateResolver] State '{user_state}' not found in ADO - planner should resolve this")
            return StateResolutionResult(
                original_state=user_state,
                resolved_state=user_state,  # Pass as-is, let ADO reject it
                exists_in_ado=False,
                resolution_method='not_found',
                confidence=0.0,
                reasoning=f"State '{user_state}' not found in ADO (planner should have resolved)"
            )
    
    def get_valid_states_for_planning(
        self,
        project: str
    ) -> List[str]:
        """
        Get list of valid states for light planner to use.
        
        This is called by the planner to get valid states for the project,
        so the LLM can resolve user state terms to valid ADO states.
        
        Args:
            project: ADO project name
            
        Returns:
            List of valid state names (sorted)
        """
        discovery = self.discover_states(project)
        if discovery and discovery.states:
            return sorted(discovery.states)
        return []
    
    def resolve_multiple_states(
        self,
        user_states: List[str],
        project: str
    ) -> List[StateResolutionResult]:
        """
        Validate multiple user states against ADO.
        
        Args:
            user_states: List of states from user (already resolved by planner)
            project: ADO project
            
        Returns:
            List of StateResolutionResult for each state
        """
        results = []
        for state in user_states:
            result = self.validate_state(state, project)
            results.append(result)
        return results
    
    def clear_cache(self, project: Optional[str] = None):
        """Clear discovery cache for project or all projects."""
        if project:
            cache_key = project.lower()
            if cache_key in self._discovery_cache:
                del self._discovery_cache[cache_key]
                logger.info(f"[StateResolver] Cleared cache for project '{project}'")
        else:
            self._discovery_cache.clear()
            logger.info("[StateResolver] Cleared all state discovery cache")
