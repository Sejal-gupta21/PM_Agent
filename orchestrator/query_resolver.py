"""
Query Resolver - Deterministic intent resolution layer.

This is Step 2 in the orchestrator pipeline (after Guardrails):

    Controller → Orchestrator:
        1. Guardrails (input validation)
        2. Query Resolver (THIS) ← regex + semantic resolution
        3. Light LLM Planner (only if resolver fails)
        4. Router (agent selection based on resolved intent)
        5. Agent Dispatch

KEY RESPONSIBILITY SEPARATION:
- QueryResolver: Understand and resolve intent (regex → semantic → [light LLM in orchestrator])
- Router: Select the correct agent based on resolved intent (NO LLM calls)

These two must not overlap responsibilities.

RESOLUTION ORDER (Strict):
1. Regex pattern matching (fastest, most precise)
2. Semantic embedding matching (fuzzy, keyword-independent)
3. If both fail → return unresolved, Orchestrator invokes Light LLM

The Query Resolver does NOT call Light LLM - that is Orchestrator's responsibility.
"""

import re
import logging
from dataclasses import dataclass, field
from typing import Dict, Any, Optional, List, Tuple
from enum import Enum

logger = logging.getLogger("orchestrator.query_resolver")


class ResolutionMethod(Enum):
    """How the intent was resolved."""
    REGEX = "regex"
    SEMANTIC = "semantic"
    NORMALIZED = "normalized"
    UNRESOLVED = "unresolved"


@dataclass
class ResolverResult:
    """Result of query resolution."""
    is_resolved: bool = False
    confidence: float = 0.0
    method: ResolutionMethod = ResolutionMethod.UNRESOLVED
    
    # Resolved intent details
    resolved_agent: Optional[str] = None  # "pm_skill_agent", "pm_agent", etc.
    resolved_skill: Optional[str] = None  # Specific skill for PM Skill Agent
    resolved_params: Dict[str, Any] = field(default_factory=dict)
    
    # Context for Light LLM (if unresolved)
    routing_attempts: Dict[str, Any] = field(default_factory=dict)
    
    # Hints for downstream processing
    skill_hint: Optional[str] = None
    intent_hint: Optional[str] = None
    
    # Normalized query info
    normalized_query: Optional[str] = None
    transformations: List[str] = field(default_factory=list)
    
    # For hybrid flows
    is_hybrid: bool = False
    hybrid_steps: List[Dict[str, Any]] = field(default_factory=list)
    
    # Clarification handling
    should_request_clarification: bool = False
    clarification_message: Optional[str] = None


# Confidence thresholds
SEMANTIC_MATCH_HIGH_THRESHOLD = 0.65
SEMANTIC_MATCH_MEDIUM_THRESHOLD = 0.45


class QueryResolver:
    """
    Deterministic query resolution using regex and semantic matching.
    
    This component handles intent resolution BEFORE routing:
    - Step 1: Regex pattern matching (fastest, most precise)
    - Step 2: Semantic embedding matching (fuzzy, keyword-independent)
    
    If both fail or return low confidence, Orchestrator invokes Light LLM.
    QueryResolver does NOT call any LLMs - that's Orchestrator's job.
    """
    
    def __init__(self):
        # Import patterns from router (single source of truth)
        from .router import PM_SKILL_PATTERNS, PM_AGENT_PATTERNS, HYBRID_PATTERNS
        
        # Compile patterns
        self.skill_patterns = [
            (re.compile(pattern, re.IGNORECASE), skill, params)
            for pattern, skill, params in PM_SKILL_PATTERNS
        ]
        self.agent_patterns = [
            re.compile(pattern, re.IGNORECASE)
            for pattern in PM_AGENT_PATTERNS
        ]
        self.hybrid_patterns = [
            (re.compile(pattern, re.IGNORECASE), steps)
            for pattern, steps in HYBRID_PATTERNS
        ]
        
        logger.info(f"QueryResolver initialized with {len(self.skill_patterns)} skill patterns, "
                    f"{len(self.agent_patterns)} agent patterns")
    
    def resolve(self, query: str, context: Optional[Dict[str, Any]] = None) -> ResolverResult:
        """
        Resolve query intent using deterministic methods (NO LLM).
        
        Resolution order:
        1. Check for pending context (billing deviation follow-up, etc.)
        2. Normalize query (pre-processing)
        3. Regex pattern matching
        4. Semantic embedding matching
        
        If all fail, returns unresolved result with context for Light LLM.
        
        Args:
            query: User query string
            context: Optional context (session state, etc.)
            
        Returns:
            ResolverResult with resolution status and details
        """
        query_clean = query.strip()
        context = context or {}
        original_query = query_clean
        
        # Track resolution attempts for Light LLM context
        routing_attempts = {
            "regex_matched": False,
            "regex_skill": None,
            "semantic_matched": False,
            "semantic_skill": None,
            "semantic_score": 0.0,
            "normalized_intent": None,
            "normalized_confidence": 0.0,
        }
        
        result = ResolverResult(routing_attempts=routing_attempts)
        
        # ═══════════════════════════════════════════════════════════════════
        # STEP 0: CHECK FOR PENDING CONTEXT (Billing deviation follow-up, etc.)
        # ═══════════════════════════════════════════════════════════════════
        pending_result = self._check_pending_context(query_clean, context)
        if pending_result and pending_result.is_resolved:
            logger.info(f"[RESOLVER] Resolved via pending context: {pending_result.resolved_skill}")
            return pending_result
        
        # ═══════════════════════════════════════════════════════════════════
        # STEP 0.5: NORMALIZE QUERY (Pre-processing)
        # ═══════════════════════════════════════════════════════════════════
        # COMMENTED OUT FOR TESTING LLM PLANNER DECISION MAKING
        logger.info("[RESOLVER] Step 0.5: Hybrid pattern check DISABLED for LLM planner testing")
        # # FIX: Check hybrid patterns BEFORE normalization to prevent single-skill
        # # resolution of queries that require multi-agent flows.
        # # Example: "Get overlooked stories and generate iteration report" should
        # # trigger hybrid flow, not just overlooked_stories skill.
        # hybrid_match = self._match_hybrid(query_clean)
        # if hybrid_match:
        #     logger.info(f"[RESOLVER] ✓ Hybrid pattern matched with {len(hybrid_match)} steps")
        #     result.is_resolved = True
        #     result.confidence = 0.90  # High confidence for regex hybrid match
        #     result.method = ResolutionMethod.REGEX
        #     result.resolved_agent = "pm_agent"  # Start with PM Agent for data
        #     result.is_hybrid = True
        #     result.hybrid_steps = hybrid_match
        #     result.resolved_params = {"hybrid_match": True}
        #     return result
        
        # ═══════════════════════════════════════════════════════════════════
        # STEP 0.5b: NORMALIZE QUERY (Pre-processing)
        # ═══════════════════════════════════════════════════════════════════
        # COMMENTED OUT FOR TESTING LLM PLANNER DECISION MAKING
        logger.info("[RESOLVER] Step 0.5b: Query normalization DISABLED for LLM planner testing")
        routing_attempts["normalized_attempted"] = False
        routing_attempts["normalized_matched"] = False
        
        # # NOTE: Normalization runs BEFORE regex matching to transform casual language.
        # # If normalized intent has confidence >= 0.85, it returns early and SKIPS Step 1 (regex).
        # # This is by design: normalize is pre-processing, not resolution.
        # normalized_result = self._normalize_query(query_clean)
        # if normalized_result:
        #     # Track that normalization was attempted
        #     routing_attempts["normalized_attempted"] = True
        #     
        #     if normalized_result.get("transformations"):
        #         query_clean = normalized_result.get("normalized", query_clean)
        #         result.normalized_query = query_clean
        #         result.transformations = normalized_result.get("transformations", [])
        #         logger.debug(f"[RESOLVER] Normalized: '{original_query}' → '{query_clean}'")
        #     
        #     # Check for high-confidence normalized intent
        #     if normalized_result.get("detected_intent") and normalized_result.get("confidence", 0) >= 0.85:
        #         routing_attempts["normalized_matched"] = True
        #         routing_attempts["normalized_intent"] = normalized_result["detected_intent"]
        #         routing_attempts["normalized_confidence"] = normalized_result["confidence"]
        #         
        #         skill_info = self._get_skill_for_intent(normalized_result["detected_intent"])
        #         if skill_info:
        #             result.is_resolved = True
        #             result.confidence = normalized_result["confidence"]
        #             result.method = ResolutionMethod.NORMALIZED
        #             result.resolved_agent = skill_info.get("agent", "pm_agent")
        #             result.resolved_skill = skill_info.get("skill")
        #             result.resolved_params = {"normalized_match": True}
        #             logger.info(f"[RESOLVER] Resolved via normalized intent: {result.resolved_skill}")
        #             # EARLY EXIT: High-confidence normalized intent bypasses regex matching (Step 1)
        #             # This is correct behavior - normalize transforms casual queries into structured ones
        #             return result
        #     else:
        #         # Normalization attempted but didn't resolve
        #         routing_attempts["normalized_matched"] = False
        #     
        #     # Store normalized intent as hint even if low confidence
        #     if normalized_result.get("detected_intent"):
        #         routing_attempts["normalized_intent"] = normalized_result["detected_intent"]
        #         routing_attempts["normalized_confidence"] = normalized_result.get("confidence", 0)
        #         result.intent_hint = normalized_result["detected_intent"]
        # else:
        #     # No normalization result
        #     routing_attempts["normalized_attempted"] = False
        #     routing_attempts["normalized_matched"] = False
        
        # ═══════════════════════════════════════════════════════════════════
        # STEP 1: REGEX PATTERN MATCHING (Primary - Fastest)
        # ═══════════════════════════════════════════════════════════════════
        # COMMENTED OUT FOR TESTING LLM PLANNER DECISION MAKING
        logger.info("[RESOLVER] Step 1: Regex pattern matching DISABLED for LLM planner testing")
        routing_attempts["regex_failed"] = True
        
        # # 1a. Billing deviation priority check
        # if self._match_billing_deviation(query_clean):
        #     routing_attempts["regex_matched"] = True
        #     routing_attempts["regex_skill"] = "billing_deviation"
        #     result.is_resolved = True
        #     result.confidence = 0.95
        #     result.method = ResolutionMethod.REGEX
        #     result.resolved_agent = "pm_skill_agent"
        #     result.resolved_skill = "billing_deviation"
        #     result.resolved_params = {"regex_match": True, "requires_ui_form": False}
        #     result.routing_attempts = routing_attempts
        #     logger.info("[RESOLVER] Step 1 SUCCESS: Regex matched billing_deviation")
        #     return result
        # 
        # # 1b. Check PM Skills Agent patterns
        # skill_match = self._match_skill(query_clean)
        # if skill_match:
        #     skill_name, params = skill_match
        #     routing_attempts["regex_matched"] = True
        #     routing_attempts["regex_skill"] = skill_name
        #     result.is_resolved = True
        #     result.confidence = 0.95
        #     result.method = ResolutionMethod.REGEX
        #     result.resolved_agent = "pm_skill_agent"
        #     result.resolved_skill = skill_name
        #     result.resolved_params = {**params, "regex_match": True}
        #     result.routing_attempts = routing_attempts
        #     logger.info(f"[RESOLVER] Step 1 SUCCESS: Regex matched PM skill: {skill_name}")
        #     return result
        # 
        # # 1c. Check PM Agent patterns (ADO data queries)
        # if self._matches_pm_agent(query_clean) or self._matches_pm_agent(original_query):
        #     routing_attempts["regex_matched"] = True
        #     routing_attempts["regex_skill"] = "pm_agent_ado"
        #     result.is_resolved = True
        #     result.confidence = 0.95
        #     result.method = ResolutionMethod.REGEX
        #     result.resolved_agent = "pm_agent"
        #     result.resolved_params = {"regex_match": True}
        #     result.routing_attempts = routing_attempts
        #     logger.info("[RESOLVER] Step 1 SUCCESS: Regex matched ADO pattern")
        #     return result
        # 
        # # 1d. Check hybrid flow patterns (after skill patterns)
        # hybrid_match = self._match_hybrid(query_clean)
        # if hybrid_match:
        #     routing_attempts["regex_matched"] = True
        #     routing_attempts["regex_skill"] = "hybrid_flow"
        #     result.is_resolved = True
        #     result.confidence = 0.85
        #     result.method = ResolutionMethod.REGEX
        #     result.resolved_agent = "pm_agent"  # Start with PM Agent for data
        #     result.is_hybrid = True
        #     result.hybrid_steps = hybrid_match
        #     result.resolved_params = {"regex_match": True}
        #     result.routing_attempts = routing_attempts
        #     logger.info(f"[RESOLVER] Step 1 SUCCESS: Regex matched hybrid flow with {len(hybrid_match)} steps")
        #     return result
        # 
        # logger.debug("[RESOLVER] Step 1 FAILED: No regex patterns matched")
        
        # ═══════════════════════════════════════════════════════════════════
        # STEP 2: SEMANTIC SKILL MATCHING (Fallback - Embedding-based)
        # ═══════════════════════════════════════════════════════════════════
        # COMMENTED OUT FOR TESTING LLM PLANNER DECISION MAKING
        logger.info("[RESOLVER] Step 2: Semantic matching DISABLED for LLM planner testing")
        routing_attempts["semantic_failed"] = True
        
        # semantic_match = self._semantic_match_skill(query_clean)
        # if semantic_match:
        #     skill_id, confidence, confidence_level, requires_ui_form = semantic_match
        #     routing_attempts["semantic_matched"] = True
        #     routing_attempts["semantic_skill"] = skill_id
        #     routing_attempts["semantic_score"] = confidence
        #     
        #     if confidence_level == "high":
        #         # High confidence: resolved
        #         result.is_resolved = True
        #         result.confidence = confidence
        #         result.method = ResolutionMethod.SEMANTIC
        #         result.resolved_agent = "pm_skill_agent"
        #         result.resolved_skill = skill_id
        #         result.resolved_params = {"semantic_match": True, "requires_ui_form": requires_ui_form}
        #         result.routing_attempts = routing_attempts
        #         logger.info(f"[RESOLVER] Step 2 SUCCESS: Semantic match (HIGH): {skill_id} ({confidence:.3f})")
        #         return result
        #     
        #     elif confidence_level == "medium":
        #         # Special case: developer_skills at medium confidence
        #         if skill_id == "developer_skills":
        #             result.is_resolved = True
        #             result.confidence = confidence
        #             result.method = ResolutionMethod.SEMANTIC
        #             result.resolved_agent = "pm_skill_agent"
        #             result.resolved_skill = skill_id
        #             result.resolved_params = {"semantic_match": True, "requires_ui_form": requires_ui_form}
        #             result.routing_attempts = routing_attempts
        #             logger.info(f"[RESOLVER] Step 2 SUCCESS: Semantic match (MEDIUM): {skill_id} ({confidence:.3f})")
        #             return result
        #         
        #         # Medium confidence - store as hint, let Light LLM decide
        #         result.skill_hint = skill_id
        #         result.confidence = confidence
        #         result.resolved_params = {"requires_ui_form": requires_ui_form}
        #         logger.info(f"[RESOLVER] Step 2 PARTIAL: Semantic match (MEDIUM): {skill_id} ({confidence:.3f})")
        # else:
        #     logger.debug("[RESOLVER] Step 2 FAILED: No semantic match above threshold")
        
        # ═══════════════════════════════════════════════════════════════════
        # NOT RESOLVED - Return context for Light LLM
        # ═══════════════════════════════════════════════════════════════════
        result.is_resolved = False
        result.routing_attempts = routing_attempts
        result.routing_attempts["regex_failed"] = not routing_attempts["regex_matched"]
        result.routing_attempts["semantic_failed"] = not routing_attempts["semantic_matched"]
        
        # If this appears to be an ADO/PM data query and the 'team' context is missing,
        # ask the user for clarification rather than guessing. Use configured defaults
        # (e.g., default project) from `config` dynamically — do not hardcode values.
        try:
            from config import config

            pm_query_keywords = (
                "bug", "bugs", "user story", "user stories", "work item",
                "sprint", "iteration", "velocity", "backlog", "release", "wiql"
            )

            q_lower = original_query.lower()
            if any(k in q_lower for k in pm_query_keywords):
                # Check for team presence in context
                team_in_context = context.get("team") or context.get("team_name") or context.get("ado_team")
                if not team_in_context:
                    # Ask user for team/area path clarification before planning
                    result.should_request_clarification = True
                    default_project = config.pm_default_project if hasattr(config, 'pm_default_project') else None
                    proj_msg = f" Project will default to '{default_project}'." if default_project else ""
                    result.clarification_message = (
                        "I need the team (or area path) to run this query."
                        " Please reply with the team name or ask me to list available teams."
                        + proj_msg
                    )
                    # Provide a hint for downstream planners/agents
                    if default_project:
                        result.resolved_params.setdefault("project", default_project)
                    result.routing_attempts["missing_team"] = True

        except Exception:
            # If config cannot be loaded for any reason, fall back to unresolved without clarification
            logger.debug("[RESOLVER] Could not load config for default project; skipping clarification hint")

        logger.info(f"[RESOLVER] UNRESOLVED: regex={routing_attempts['regex_matched']}, "
                    f"semantic={routing_attempts['semantic_matched']}, "
                    f"skill_hint={result.skill_hint}")
        return result
    
    def _check_pending_context(self, query: str, context: Dict[str, Any]) -> Optional[ResolverResult]:
        """Check for pending context that should override normal resolution."""
        # Billing deviation follow-up
        if context.get("pending_billing_deviation") and context.get("is_billing_deviation_response"):
            pending_info = context["pending_billing_deviation"]
            # Support both 'area_path' (singular from skills.py) and 'area_paths' (plural) for compatibility
            area_path = pending_info.get("area_path")
            area_paths = pending_info.get("area_paths", [])
            if area_path and not area_paths:
                # If singular area_path is set, use it
                area_paths = [area_path]
            month = pending_info.get("month")
            year = pending_info.get("year")
            
            logger.info(f"[RESOLVER] Pending billing deviation context: area_path={area_path}, area_paths={area_paths}, month={month}, year={year}")
            
            # Extract target hours from query
            target_hours_match = re.search(r'(\d+)', query)
            if target_hours_match:
                target_hours = target_hours_match.group(1)
                
                # Build route params with proper area_path
                final_area_path = area_path or (", ".join(area_paths) if area_paths else None)
                route_params = {
                    "area_path": final_area_path,
                    "target_hours": target_hours,
                    "query": f"billing deviation for {final_area_path} with {target_hours} target hours" if final_area_path else f"billing deviation with {target_hours} target hours"
                }
                if month is not None:
                    route_params["month"] = month
                if year is not None:
                    route_params["year"] = year
                
                logger.info(f"[RESOLVER] Billing deviation follow-up route_params: {route_params}")
                
                return ResolverResult(
                    is_resolved=True,
                    confidence=0.95,
                    method=ResolutionMethod.REGEX,
                    resolved_agent="pm_skill_agent",
                    resolved_skill="billing_deviation",
                    resolved_params=route_params,
                )
        return None
    
    def _normalize_query(self, query: str) -> Optional[Dict[str, Any]]:
        """Normalize query using query_normalizer utility."""
        try:
            from utilities.query_normalizer import normalize_query_cached as normalize_query
            result = normalize_query(query)
            return {
                "normalized": result.normalized,
                "transformations": result.transformations,
                "detected_intent": result.detected_intent,
                "confidence": result.confidence,
            }
        except Exception as e:
            logger.debug(f"[RESOLVER] Query normalization failed: {e}")
            return None
    
    def _get_skill_for_intent(self, intent: str) -> Optional[Dict[str, Any]]:
        """Get skill info for a detected intent."""
        try:
            from utilities.query_normalizer import get_skill_for_intent
            skill_info = get_skill_for_intent(intent)
            if skill_info:
                # Add agent based on skill_id - PM Skill Agent owns these skills
                skill_id = skill_info.get("skill_id", "")
                pm_skill_agent_skills = [
                    "overlooked_stories", "bug_areas_highlight", "iteration_report",
                    "billing_deviation", "feedback_to_dev"
                ]
                if skill_id in pm_skill_agent_skills:
                    skill_info["agent"] = "pm_skill_agent"
                    skill_info["skill"] = skill_id
                else:
                    skill_info["agent"] = "pm_agent"
                return skill_info
            return None
        except Exception:
            return None
    
    def _match_billing_deviation(self, query: str) -> bool:
        """Check if query is specifically about billing deviation."""
        billing_patterns = [
            r"billing\s+deviaiton",
            r"billing\s+deviation",
            r"deviaiton\s+(report|for)",
            r"deviation\s+(report|for)",
            r"deviation\s+in\s+billing",
            r"(over|under)[-\s]?billing",
            r"billing\s+(status|hours|report)",
            r"actual\s+vs\s+target\s+hours",
            r"effort\s+(deviation|variance)",
            r"target\s+hours?\s+(deviation|variance)",
        ]
        query_lower = query.lower()
        for pattern in billing_patterns:
            if re.search(pattern, query_lower, re.IGNORECASE):
                return True
        return False
    
    def _match_skill(self, query: str) -> Optional[Tuple[str, Dict[str, Any]]]:
        """Match query to a PM skill pattern."""
        for regex, skill_name, params in self.skill_patterns:
            if regex.search(query):
                return (skill_name, params)
        return None
    
    def _matches_pm_agent(self, query: str) -> bool:
        """Check if query matches PM Agent patterns."""
        for regex in self.agent_patterns:
            if regex.search(query):
                return True
        return False
    
    def _match_hybrid(self, query: str) -> Optional[List[Dict[str, Any]]]:
        """Check if query matches hybrid flow patterns."""
        for regex, steps in self.hybrid_patterns:
            if regex.search(query):
                return steps
        return None
    
    def _semantic_match_skill(self, query: str) -> Optional[Tuple[str, float, str, bool]]:
        """Match query to a skill using semantic similarity."""
        try:
            from utilities.skill_registry import semantic_match_query_to_skill
            
            result = semantic_match_query_to_skill(
                query,
                confident_threshold=SEMANTIC_MATCH_HIGH_THRESHOLD,
                tentative_threshold=SEMANTIC_MATCH_MEDIUM_THRESHOLD
            )
            
            # Handle both old (3-tuple) and new (4-tuple) return formats
            if len(result) == 4:
                skill_id, score, confidence_level, requires_ui_form = result
            else:
                skill_id, score, confidence_level = result
                requires_ui_form = False
            
            if skill_id and confidence_level in ("high", "medium"):
                return (skill_id, score, confidence_level, requires_ui_form)
            
            return None
            
        except ImportError:
            logger.debug("semantic_match_query_to_skill not available")
            return None
        except Exception as e:
            logger.warning(f"Semantic skill matching failed: {e}")
            return None


# Singleton instance
_query_resolver: Optional[QueryResolver] = None


def get_query_resolver() -> QueryResolver:
    """Get the global query resolver instance."""
    global _query_resolver
    if _query_resolver is None:
        _query_resolver = QueryResolver()
    return _query_resolver
