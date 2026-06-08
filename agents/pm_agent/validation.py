"""
Plan Validation Layer for PM Agent.

Validates LLM-generated plans before execution:
- Tool existence check (MCP server tools AND local skills)
- Required argument validation
- Confidence threshold enforcement
- Security checks
- Intent alignment validation (query ↔ plan consistency)
"""

import os
from typing import Dict, Any, Tuple, Optional, List
from dataclasses import dataclass, field
import logging

# Import the tool registry that contains BOTH MCP tools and local skills
from utilities.mcp.tool_registry import MCP_TOOL_REGISTRY

# Import enum normalization and type conversion for tool arguments
try:
    from utilities.mcp.tool_registry import normalize_tool_args
    NORMALIZE_ARGS_AVAILABLE = True
except ImportError:
    NORMALIZE_ARGS_AVAILABLE = False

logger = logging.getLogger(__name__)

# Local skills that are handled directly by the agent (not via MCP server)
# ⚠️ FIX: Removed wit_get_work_items_for_iteration and work_get_iteration_work_items
# These are REAL MCP tools loaded virtually at startup, NOT local skills.
# Including them here caused validation to skip MCP arg validation, creating
# false positives where plans pass validation but fail at MCP execution.
LOCAL_SKILLS = {
    "iteration_report", "bug_areas_highlight", "overlooked_stories",
    "list_area_paths", "send_email",
    "get_capacity_forecast",
    "context_data_answer"
}

# Dynamically extend LOCAL_SKILLS with all PM skill definitions so the
# validator doesn't reject them as "hallucinated tools".
try:
    from agents.pm_skill_agent.skills import SKILL_DEFINITIONS as _SD
    LOCAL_SKILLS.update(_SD.keys())
except ImportError:
    pass

# Feature flags for progressive enablement
ENABLE_REGISTRY_VALIDATION = os.getenv("ENABLE_REGISTRY_VALIDATION", "true").lower() == "true"
ENABLE_PERMISSION_PRECHECK = os.getenv("ENABLE_PERMISSION_PRECHECK", "false").lower() == "true"


@dataclass
class ValidationResult:
    """Result of plan validation."""
    is_valid: bool
    error: Optional[str] = None
    warnings: List[str] = None
    sanitized_plan: Optional[Dict[str, Any]] = None
    intent_alignment: Optional[Dict[str, Any]] = field(default=None)
    
    def __post_init__(self):
        if self.warnings is None:
            self.warnings = []


class PlanValidator:
    """
    Validates LLM-generated plans before execution.
    
    Catches:
    - Hallucinated tools
    - Missing required arguments
    - Low confidence plans
    - Potentially dangerous operations
    """
    
    # Tools that modify data (require higher confidence)
    WRITE_TOOLS = {
        "wit_create_work_item",
        "wit_update_work_item",
        "wit_update_work_items_batch",
        "wit_add_work_item_comment",
        "wit_add_artifact_link",
        "wit_work_items_link",
        "wit_work_item_unlink",
        "pr_create_pull_request",
        "wiki_create_or_update_page",
        "repo_create_branch",
    }
    
    def __init__(
        self,
        tools_cache: Dict[str, Any],
        min_confidence: float = 0.5,
        write_min_confidence: float = 0.8,
        enable_registry_validation: Optional[bool] = None,
        enable_permission_precheck: Optional[bool] = None,
        mcp_connector: Optional[Any] = None
    ):
        """
        Initialize validator.
        
        Args:
            tools_cache: Dict of available MCP tools
            min_confidence: Minimum confidence for read operations
            write_min_confidence: Minimum confidence for write operations
            enable_registry_validation: Use registry write flags instead of WRITE_TOOLS (default from env)
            enable_permission_precheck: Enable core_get_permissions precheck (default from env)
            mcp_connector: MCPConnector instance for permission prechecks (optional)
        """
        self.tools_cache = tools_cache
        self.min_confidence = min_confidence
        self.write_min_confidence = write_min_confidence
        self.mcp_connector = mcp_connector
        
        # Feature flags (use env vars if not explicitly set)
        self.enable_registry_validation = (
            enable_registry_validation 
            if enable_registry_validation is not None 
            else ENABLE_REGISTRY_VALIDATION
        )
        self.enable_permission_precheck = (
            enable_permission_precheck 
            if enable_permission_precheck is not None 
            else ENABLE_PERMISSION_PRECHECK
        )
    
    def _is_write_operation(self, tool: str) -> bool:
        """
        Check if a tool is a write operation.
        
        Uses registry write flags if enable_registry_validation=True, otherwise
        falls back to hardcoded WRITE_TOOLS set for backward compatibility.
        
        Args:
            tool: Tool name to check
        
        Returns:
            True if tool performs write operations, False otherwise
        """
        if self.enable_registry_validation:
            # Registry-driven: check write flag in MCP_TOOL_REGISTRY
            tool_entry = MCP_TOOL_REGISTRY.get(tool, {})
            # Default to False (read-only) if write flag missing
            return tool_entry.get("write", False)
        else:
            # Legacy: use hardcoded WRITE_TOOLS set
            return tool in self.WRITE_TOOLS
    
    def validate(self, plan: Dict[str, Any], query: Optional[str] = None) -> ValidationResult:
        """
        Validate a plan from the LLM planner.
        
        Args:
            plan: Dict with action, tool, args, confidence
            query: Original user query for intent alignment validation (optional)
        
        Returns:
            ValidationResult with validation outcome
        """
        if not plan:
            return ValidationResult(
                is_valid=False,
                error="No plan provided"
            )
        
        action = plan.get("action", "")
        
        # Handle non-tool actions
        if action == "ask_clarification":
            return ValidationResult(
                is_valid=True,
                sanitized_plan=plan
            )
        
        if action == "no_tool":
            return ValidationResult(
                is_valid=True,
                warnings=["LLM determined no tool needed"],
                sanitized_plan=plan
            )
        
        if action != "call_tool":
            return ValidationResult(
                is_valid=False,
                error=f"Unknown action: {action}"
            )
        
        # Validate tool call
        tool = plan.get("tool")
        args = plan.get("args") or {}
        confidence = plan.get("confidence", 0)
        
        warnings = []
        
        # Check tool exists
        if not tool:
            return ValidationResult(
                is_valid=False,
                error="No tool specified in plan"
            )
        
        # Check tool exists in EITHER MCP server tools_cache OR local MCP_TOOL_REGISTRY
        # Local skills (iteration_report, bug_areas_highlight, etc.) are in MCP_TOOL_REGISTRY
        # but not in the MCP server's tools_cache
        is_local_skill = tool in LOCAL_SKILLS
        is_mcp_tool = tool in self.tools_cache
        is_in_registry = tool in MCP_TOOL_REGISTRY
        
        if not (is_mcp_tool or is_local_skill or is_in_registry):
            # Tool doesn't exist anywhere - suggest alternatives
            available_tools = list(self.tools_cache.keys())[:10]
            return ValidationResult(
                is_valid=False,
                error=f"Tool '{tool}' not found. Did you mean one of: {', '.join(available_tools)}?"
            )
        
        # ══════════════════════════════════════════════════════════════
        # PERMISSION PRECHECK (if enabled)
        # ══════════════════════════════════════════════════════════════
        if self.enable_permission_precheck and self.mcp_connector:
            # Check if plan includes project argument (indicates ADO data access)
            project_arg = args.get("project") or plan.get("context", {}).get("project")
            
            if project_arg and "core_get_permissions" in self.tools_cache:
                # Call core_get_permissions to verify PAT has access
                try:
                    import asyncio
                    # Use mcp_connector to call permission check
                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        # Already in async context, call directly
                        perm_result = asyncio.create_task(
                            self.mcp_connector.call_tool("core_get_permissions", {"project": project_arg})
                        )
                    else:
                        perm_result = loop.run_until_complete(
                            self.mcp_connector.call_tool("core_get_permissions", {"project": project_arg})
                        )
                    
                    # Parse result
                    if isinstance(perm_result, str):
                        import json
                        try:
                            perm_result = json.loads(perm_result)
                        except:
                            pass
                    
                    # Check if permission check failed
                    if isinstance(perm_result, dict):
                        if not perm_result.get("success", True):
                            return ValidationResult(
                                is_valid=False,
                                error=f"Permission denied: Your PAT does not have access to project '{project_arg}'. "
                                      f"Please verify your Azure DevOps Personal Access Token has the required scopes."
                            )
                        # Extract permissions if available
                        permissions = perm_result.get("permissions", [])
                        if permissions:
                            warnings.append(f"Verified PAT permissions for '{project_arg}': {', '.join(permissions[:3])}")
                
                except Exception as e:
                    # Permission precheck failed, but don't block execution
                    warnings.append(f"Permission precheck failed (non-blocking): {str(e)[:100]}")
        
        # Check confidence threshold
        is_write_op = self._is_write_operation(tool)
        required_confidence = self.write_min_confidence if is_write_op else self.min_confidence
        
        if confidence < required_confidence:
            if is_write_op:
                return ValidationResult(
                    is_valid=False,
                    error=f"Write operation '{tool}' requires confidence >= {required_confidence}, got {confidence}"
                )
            else:
                warnings.append(f"Low confidence ({confidence:.2f}) for tool '{tool}'")
        
        # Validate required arguments
        # For local skills, get schema from MCP_TOOL_REGISTRY
        # For MCP tools, get schema from tools_cache
        if is_local_skill or (is_in_registry and not is_mcp_tool):
            # Local skill - use MCP_TOOL_REGISTRY schema
            registry_info = MCP_TOOL_REGISTRY.get(tool, {})
            required_args = registry_info.get("required_args", [])
            input_schema = {"properties": registry_info.get("optional_args", {})}
        else:
            # MCP tool - use tools_cache schema
            tool_schema = self.tools_cache.get(tool, {})
            input_schema = tool_schema.get("inputSchema", {})
            required_args = input_schema.get("required", [])
        
        # ══════════════════════════════════════════════════════════════
        # AUTO-INJECT COMMON ARGUMENTS FROM CONTEXT/ENVIRONMENT
        # ══════════════════════════════════════════════════════════════
        # If 'project' is required but missing, inject from config
        if "project" in required_args and ("project" not in args or args.get("project") is None):
            from config import config
            project_value = config.ado_project
            if project_value:
                args["project"] = project_value
                warnings.append(f"Auto-injected 'project' from config: {project_value}")
        
        # Some LLM responses use 'workItemId' but MCP requires 'id' - normalize
        if "id" in required_args and "id" not in args:
            if "workItemId" in args and args["workItemId"] is not None:
                args["id"] = args["workItemId"]
                del args["workItemId"]
                warnings.append("Auto-mapped 'workItemId' to 'id'")
        
        missing_args = [arg for arg in required_args if arg not in args or args[arg] is None]
        if missing_args:
            return ValidationResult(
                is_valid=False,
                error=f"Missing required arguments for '{tool}': {missing_args}"
            )
        
        # ══════════════════════════════════════════════════════════════════
        # FIX #22 & #23: ENUM AND TYPE NORMALIZATION IN VALIDATION
        # Convert string enum values to numeric enums and string numbers to actual numbers
        # This happens BEFORE type checking to prevent validation errors like:
        # - "statusFilter: 'failed' must be number"  (FIX #22)
        # - "buildId: '24098' expected number, got string"  (FIX #23)
        # ══════════════════════════════════════════════════════════════════
        if NORMALIZE_ARGS_AVAILABLE:
            try:
                tool_schema = MCP_TOOL_REGISTRY.get(tool, {})
                normalized_args, normalization_log = normalize_tool_args(tool, args, tool_schema=tool_schema)
                if normalization_log:
                    for arg_name, reason in normalization_log.items():
                        old_val = args.get(arg_name)
                        new_val = normalized_args.get(arg_name)
                        logger.info(f"[VALIDATION] FIX #22/#23: Normalized '{arg_name}': {old_val} → {new_val} ({reason})")
                        warnings.append(f"Normalized argument '{arg_name}': {old_val} → {new_val}")
                    args = normalized_args  # Use normalized arguments
            except Exception as e:
                logger.warning(f"[VALIDATION] FIX #22/#23: Normalization failed (non-blocking): {e}")
        
        # Validate argument types
        properties = input_schema.get("properties", {})
        for arg_name, arg_value in args.items():
            if arg_name in properties:
                prop_def = properties[arg_name]
                # Handle both dict-style {"type": "..."} and string-style property definitions
                if isinstance(prop_def, dict):
                    expected_type = prop_def.get("type")
                elif isinstance(prop_def, str):
                    expected_type = prop_def  # Property is just the type string directly
                else:
                    expected_type = None
                    
                if expected_type == "array" and not isinstance(arg_value, list):
                    # Auto-fix: wrap in list
                    args[arg_name] = [arg_value]
                    warnings.append(f"Auto-wrapped '{arg_name}' in array")
                elif expected_type == "number" and isinstance(arg_value, str):
                    # Auto-fix: convert to int
                    try:
                        args[arg_name] = int(arg_value)
                        warnings.append(f"Auto-converted '{arg_name}' to number")
                    except ValueError:
                        return ValidationResult(
                            is_valid=False,
                            error=f"Argument '{arg_name}' must be a number, got '{arg_value}'"
                        )
        
        # Sanitize the plan with fixed args
        # CRITICAL: Preserve analysis_criteria and reasoning_summary for synthesizer
        sanitized = {
            "action": action,
            "tool": tool,
            "args": args,
            "confidence": confidence
        }
        
        # Preserve analysis_criteria if present (required for intelligent synthesis)
        if "analysis_criteria" in plan:
            sanitized["analysis_criteria"] = plan["analysis_criteria"]
            
        # Preserve reasoning_summary if present (useful for tracing)
        if "reasoning_summary" in plan:
            sanitized["reasoning_summary"] = plan["reasoning_summary"]
        
        # ══════════════════════════════════════════════════════════════════
        # INTENT ALIGNMENT VALIDATION
        # Verify plan's tool choice and args are consistent with the user query.
        # This is a non-blocking check: misalignment produces warnings, not errors,
        # because the LLM planner may have valid reasoning we can't infer from
        # simple pattern matching.
        # ══════════════════════════════════════════════════════════════════
        intent_alignment = None
        if query:
            intent_alignment = self._validate_intent_alignment(query, tool, args, plan)
            if intent_alignment:
                for w in intent_alignment.get("warnings", []):
                    warnings.append(w)
                logger.info(f"[VALIDATION] Intent alignment: {intent_alignment.get('alignment_score', 'N/A')}")
        
        return ValidationResult(
            is_valid=True,
            warnings=warnings,
            sanitized_plan=sanitized,
            intent_alignment=intent_alignment,
        )
    
    def _validate_intent_alignment(
        self, query: str, tool: str, args: Dict[str, Any], plan: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Validate that the plan aligns with the user's query intent.
        
        Uses the existing analyze_query_intent() infrastructure to extract
        intent from the query, then cross-references against the planned tool
        and arguments. This is a lightweight, non-LLM check.
        
        Args:
            query: Original user query
            tool: Selected tool name
            args: Tool arguments
            plan: Full plan dict (for analysis_criteria)
            
        Returns:
            Dict with alignment_score (0.0-1.0), warnings, and details
        """
        try:
            from .query_aware_filter import analyze_query_intent
        except ImportError:
            return {"alignment_score": 1.0, "warnings": [], "details": "query_aware_filter not available"}
        
        intent = analyze_query_intent(query)
        alignment_warnings = []
        alignment_details = {}
        score = 1.0  # Start with perfect alignment
        
        # ── Check 1: Tool category vs query intent ──
        # If user asks about iterations/sprints, tool should be iteration-related
        q_lower = query.lower()
        iteration_keywords = ["sprint", "iteration", "current sprint", "this sprint"]
        iteration_tools = {
            "wit_get_work_items_for_iteration", "work_get_iteration_work_items",
            "work_list_iterations", "work_list_team_iterations", "execute_wiql"
        }
        is_iteration_query = any(kw in q_lower for kw in iteration_keywords)
        
        if is_iteration_query and tool not in iteration_tools:
            score -= 0.2
            alignment_warnings.append(
                f"Intent mismatch: query mentions sprint/iteration but tool '{tool}' is not iteration-related"
            )
        
        # ── Check 2: Filter intent vs analysis_criteria ──
        # If user has specific filter intent, analysis_criteria should reflect it
        analysis_criteria = plan.get("analysis_criteria", {})
        
        if intent.has_active_filters() and not analysis_criteria:
            score -= 0.15
            active_filters = []
            if intent.filter_blocked:
                active_filters.append("blocked")
            if intent.filter_at_risk:
                active_filters.append("at-risk")
            if intent.filter_unassigned:
                active_filters.append("unassigned")
            if intent.filter_customer_impacting:
                active_filters.append("customer-impacting")
            if intent.filter_stale:
                active_filters.append("stale")
            if intent.filter_by_types:
                active_filters.append(f"types:{','.join(intent.filter_by_types)}")
            if intent.filter_by_states:
                active_filters.append(f"states:{','.join(intent.filter_by_states)}")
            
            alignment_warnings.append(
                f"Missing analysis_criteria: query has active filters ({', '.join(active_filters)}) "
                f"but plan has no analysis_criteria for the synthesizer"
            )
        
        # ── Check 3: Work item type consistency ──
        # If user asks for bugs, tool args should reflect that
        if intent.filter_by_types:
            wiql_query = args.get("query", "")
            search_text = args.get("searchText", "")
            work_item_type_arg = args.get("workItemType", [])
            
            types_in_args = False
            for wit_type in intent.filter_by_types:
                if (wit_type.lower() in wiql_query.lower() or 
                    wit_type.lower() in search_text.lower() or
                    wit_type in (work_item_type_arg if isinstance(work_item_type_arg, list) else [work_item_type_arg])):
                    types_in_args = True
                    break
            
            filter_logic = analysis_criteria.get("filter_logic", "") if analysis_criteria else ""
            types_in_criteria = any(t.lower() in filter_logic.lower() for t in intent.filter_by_types) if filter_logic else False
            
            if not types_in_args and not types_in_criteria:
                score -= 0.1
                alignment_warnings.append(
                    f"Type filter gap: query mentions {intent.filter_by_types} but neither "
                    f"tool args nor analysis_criteria include type filtering"
                )
        
        # ── Check 4: Empty analysis_criteria.filter_logic for filtered intent ──
        if analysis_criteria:
            ac_intent = analysis_criteria.get("intent", "")
            filter_logic = analysis_criteria.get("filter_logic", "")
            
            if ac_intent in ("filtered", "blocked", "at-risk", "overdue") and not filter_logic:
                score -= 0.1
                alignment_warnings.append(
                    f"analysis_criteria has intent='{ac_intent}' but empty filter_logic"
                )
        
        # ── Check 5: WIQL Content Alignment (execute_wiql specific) ──
        # When tool is execute_wiql, validate that the WIQL query actually
        # contains the filters required by the user's intent.
        if tool == "execute_wiql":
            wiql = (args.get("wiql") or args.get("query") or "").lower()
            if wiql:
                wiql_issues = self._validate_wiql_content_vs_intent(
                    wiql, query, intent, analysis_criteria
                )
                for issue_score, issue_msg in wiql_issues:
                    score -= issue_score
                    alignment_warnings.append(issue_msg)
        
        alignment_details = {
            "detected_intent_filters": {
                "blocked": intent.filter_blocked,
                "at_risk": intent.filter_at_risk,
                "unassigned": intent.filter_unassigned,
                "customer_impacting": intent.filter_customer_impacting,
                "stale": intent.filter_stale,
                "types": intent.filter_by_types,
                "states": intent.filter_by_states,
            },
            "has_analysis_criteria": bool(analysis_criteria),
            "tool": tool,
        }
        
        score = max(0.0, min(1.0, score))  # Clamp
        
        return {
            "alignment_score": round(score, 2),
            "warnings": alignment_warnings,
            "details": alignment_details,
            "query_intent": {
                "has_active_filters": intent.has_active_filters(),
                "original_query": query[:100],
            }
        }

    def _validate_wiql_content_vs_intent(
        self, wiql_lower: str, query: str, intent, analysis_criteria: dict
    ) -> list:
        """
        Check WIQL query content against the user's query intent.
        Returns list of (score_penalty, warning_message) tuples.
        """
        import re
        issues = []
        q_lower = query.lower()

        # ── 5a: Scope / Area Path check ──
        # If query mentions a team or area scope, WIQL should have AreaPath filter
        scope_name = self._detect_scope_qualifier(query)
        has_area_filter = ("areapath" in wiql_lower or "area path" in wiql_lower)
        if scope_name and not has_area_filter:
            issues.append((
                0.25,
                f"WIQL scope gap: query mentions '{scope_name}' but WIQL has no "
                f"[System.AreaPath] filter. Results will include items from ALL areas, "
                f"not just '{scope_name}'."
            ))

        # ── 5b: Date range check ──
        # If query implies a time constraint, WIQL should have a date filter
        date_intent_patterns = [
            r"\blast\s+\d+\s+days?",  r"\bpast\s+\d+\s+days?",
            r"\brecent\b", r"\bcreated\s+(in|within|during|last|past)",
            r"\bclosed\s+(in|within|during|last|past)",
            r"\bmodified\s+(in|within|during|last|past)",
            r"\bin\s+the\s+last\s+\d+", r"\bthis\s+(week|month)",
            r"\bsince\b", r"\bafter\b", r"\bbefore\b",
        ]
        has_date_intent = any(re.search(p, q_lower) for p in date_intent_patterns)
        has_date_filter = any(
            tok in wiql_lower for tok in
            ["createddate", "changeddate", "closeddate", "resolveddate", "@today"]
        )
        if has_date_intent and not has_date_filter:
            issues.append((
                0.2,
                "WIQL date gap: query implies a time-based filter but WIQL has no "
                "date condition (CreatedDate/ChangedDate/@Today)."
            ))

        # ── 5c: Type filter presence in WIQL ──
        if intent.filter_by_types:
            has_type_in_wiql = "workitemtype" in wiql_lower
            if not has_type_in_wiql:
                # Also check for inline type values like 'Bug' in the WIQL
                has_type_in_wiql = any(
                    t.lower() in wiql_lower for t in intent.filter_by_types
                )
            if not has_type_in_wiql:
                issues.append((
                    0.1,
                    f"WIQL type gap: query mentions {intent.filter_by_types} but WIQL "
                    f"has no [System.WorkItemType] filter."
                ))

        return issues

    @staticmethod
    def _detect_scope_qualifier(query: str) -> str:
        """
        Detect if a query specifies a team/area scope qualifier.

        Looks for patterns like:
          'for FracPro Suite', 'in XOPS 25', 'FracPro Suite bugs'
        Returns the scope name or empty string.
        """
        import re

        # Generic words that are NOT team/area names
        _SKIP = {
            "the", "this", "that", "all", "my", "our", "some", "any", "each",
            "current", "last", "next", "previous", "active", "new", "open",
            "every", "a", "an",
        }

        # Pattern 1: "for <ProperNoun+>" at end or before temporal/filter clauses
        m = re.search(
            r'\bfor\s+'
            r'((?:[A-Z][A-Za-z0-9]*(?:\s+(?:[A-Z0-9][A-Za-z0-9]*|\d+))*))',
            query
        )
        if m:
            candidate = m.group(1).strip()
            if candidate.split()[0].lower() not in _SKIP:
                return candidate

        # Pattern 2: "in <ProperNoun+> team/area"
        m = re.search(
            r'\bin\s+'
            r'((?:[A-Z][A-Za-z0-9]*(?:\s+(?:[A-Z0-9][A-Za-z0-9]*|\d+))*))',
            query
        )
        if m:
            candidate = m.group(1).strip()
            if candidate.split()[0].lower() not in _SKIP:
                return candidate

        return ""

    def get_clarification_prompt(self, validation_result: ValidationResult) -> str:
        """Generate a user-friendly clarification prompt for invalid plans."""
        if validation_result.is_valid:
            return ""
        
        error = validation_result.error or "Unknown error"
        
        if "Missing required arguments" in error:
            return f"I need more information to complete this request. {error.replace('Missing required arguments', 'Please provide')}"
        
        if "not found" in error:
            return "I'm not sure how to handle this request. Could you rephrase or provide more details?"
        
        if "confidence" in error:
            return "I'm not confident enough to proceed with this change. Could you confirm what you'd like me to do?"
        
        return f"I encountered an issue: {error}"


def validate_plan(
    plan: Dict[str, Any], 
    tools_cache: Dict[str, Any],
    mcp_connector: Optional[Any] = None,
    query: Optional[str] = None
) -> ValidationResult:
    """
    Quick validation of a plan.
    
    Args:
        plan: LLM-generated plan
        tools_cache: Available MCP tools
        mcp_connector: MCPConnector instance for permission prechecks (optional)
        query: Original user query for intent alignment validation (optional)
    
    Returns:
        ValidationResult
    """
    validator = PlanValidator(tools_cache, mcp_connector=mcp_connector)
    return validator.validate(plan, query=query)
