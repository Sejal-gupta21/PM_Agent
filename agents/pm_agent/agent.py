import os
import logging
from typing import List, Dict, Any
from utilities.mcp.mcp_ado_connector import MCPConnector
from utilities.mcp.tool_registry import ToolExecutor
from collections.abc import AsyncIterable
import requests
import json
import re

from .llm import summarize_mcp_result, clear_instructions_cache
from .conversation import get_conversation_memory, ConversationContext
from .skills import should_use_fixed_skill, SkillMatch
from .validation import validate_plan, ValidationResult
from .self_correction import (
    analyze_failure as analyze_tool_failure,
    FailureType, RecoveryAction, FailureAnalysis
)
from .result_validator import (
    ToolResultValidator, ValidationSeverity,
    ValidationOrchestrator, get_validation_orchestrator,
    PlanValidationResult,
)
from utilities.emailer import send_report_attachment, is_email_ready

# Import metrics for observability (B1)
try:
    from utilities.metrics import get_metrics_collector, MetricNames
    METRICS_AVAILABLE = True
except ImportError:
    METRICS_AVAILABLE = False

# Query-aware filtering for intelligent result filtering
try:
    from .query_aware_filter import (
        analyze_query_intent, filter_work_items_by_intent,
        format_filtered_results, should_apply_query_filtering
    )
    QUERY_AWARE_FILTER_AVAILABLE = True
except ImportError:
    QUERY_AWARE_FILTER_AVAILABLE = False

from utilities.langfuse_client import get_langfuse_client
LANGFUSE_AVAILABLE = True  # Presence checked by get_langfuse_client()
try:
    from langfuse import get_client as get_langfuse_client
    LANGFUSE_AVAILABLE = True
except Exception:
    LANGFUSE_AVAILABLE = False

logger = logging.getLogger(__name__)

_langfuse_client = get_langfuse_client()


def _finalize_langfuse_trace(trace, output: dict, status: str = "success", level: str = None):
    """Helper to properly finalize and flush Langfuse traces/spans."""
    if trace and _langfuse_client:
        try:
            update_kwargs = {"output": output, "status_message": status}
            if level:
                update_kwargs["level"] = level
            trace.update(**update_kwargs)
            trace.end()  # End the span before flushing
            _langfuse_client.flush()
            logger.debug("[AGENT] Langfuse trace finalized and flushed")
        except Exception as e:
            logger.debug(f"[AGENT] Langfuse trace finalization failed: {e}")


def _make_agent_response(
    content: Any,
    status: str = "SUCCESS",
    error: str = None,
    deep_analysis_context: dict = None,
    replan_context: dict = None,
    escalation_context: dict = None
) -> dict:
    """
    Create standardized agent response with status codes.
   
    Status values:
    - SUCCESS: Agent completed task successfully
    - FAILED: Agent encountered an error
    - NEEDS_DEEP_ANALYSIS: Agent needs orchestrator to invoke Deep LLM
    - REQUEST_REPLAN: Agent requests orchestrator to replan with different tool
    - REQUEST_ESCALATION: Agent requests escalation to different agent
   
    Args:
        content: Response content (string or dict)
        status: Status code (SUCCESS, FAILED, NEEDS_DEEP_ANALYSIS, REQUEST_REPLAN, REQUEST_ESCALATION)
        error: Error message if status is FAILED
        deep_analysis_context: Context for Deep LLM if status is NEEDS_DEEP_ANALYSIS
        replan_context: Context for replanning if status is REQUEST_REPLAN
        escalation_context: Context for escalation if status is REQUEST_ESCALATION
       
    Returns:
        Standardized response dict
    """
    response = {
        "is_task_complete": True,
        "status": status,
        "content": content
    }
    if error:
        response["error"] = error
    if deep_analysis_context:
        response["deep_analysis_context"] = deep_analysis_context
    if replan_context:
        response["replan_context"] = replan_context
    if escalation_context:
        response["escalation_context"] = escalation_context
    return response


class PMAgent:
    """
    PM-focused agent implementing the full workflow:
   
    1. User Request → Intent capture
    2. Context Resolution → Project, team, iteration from session + env
    3. Fixed Skill Check → Deterministic responses where possible
    4. LLM Planner → OpenAI gpt-4o-mini plans MCP call
    5. Plan Validation → Validate tool, args, confidence
    6. Tool Execution → MCP with pagination
    7. Post-Tool Decision → Fixed skill vs LLM summary
    8. Response Assembly → Formatted output
    9. Memory Update → Store context for follow-ups
    """
   
    # Default context values (auto-injected when missing)
    DEFAULT_PROJECT = "FracPro-OPS"
    DEFAULT_TEAM = "XOPS 25"
    DEFAULT_ITERATION = "@CurrentIteration"
   
    # System prompt loaded from instructions.txt
    SYSTEM_PROMPT = ""
   
    def __init__(self, mcp_connector=None):
        # Clear LLM instructions cache to ensure fresh instructions are loaded
        clear_instructions_cache()
       
        from config import config
        
        # Allow dependency injection of MCP connector (for testing/mocking)
        if mcp_connector is not None:
            self.mcp_connector = mcp_connector
            logger.info("PM Agent using injected MCP connector (likely MockMCPConnector)")
        else:
            # Normal initialization with live MCP
            ado_org = config.ado_org_name
            ado_pat = config.ado_pat
            self.mcp_connector = MCPConnector(org_name=ado_org, pat_token=ado_pat)
            logger.info(f"PM Agent using live MCPConnector for org={ado_org}")
        
        self.tool_executor = ToolExecutor(self.mcp_connector)
        self.memory = get_conversation_memory()
       
        # B1: Initialize metrics collector
        self.metrics = get_metrics_collector() if METRICS_AVAILABLE else None
       
        # Load system instructions from instructions.txt
        instr_path = os.path.join(os.path.dirname(__file__), "instructions.txt")
        if os.path.exists(instr_path):
            try:
                with open(instr_path, 'r') as f:
                    self.SYSTEM_PROMPT = f.read().strip()
                    logger.info(f"Loaded system prompt from instructions.txt ({len(self.SYSTEM_PROMPT)} chars)")
            except Exception as e:
                logger.warning(f"Could not load instructions.txt: {e}")
                self.SYSTEM_PROMPT = "You are a Project Manager assistant. You have access to Azure DevOps MCP tools."
        else:
            self.SYSTEM_PROMPT = "You are a Project Manager assistant. You have access to Azure DevOps MCP tools."

    async def create(self):
        """Initialize the MCP connector and auto-generate tool registry."""
        await self.mcp_connector.initialize()
        logger.info(f"MCP connector initialized with {len(self.mcp_connector.tools_cache)} tools")
        
        # Auto-generate tool registry from live MCP tools
        try:
            from utilities.mcp.tool_registry import initialize_registry
            registry = initialize_registry(self.mcp_connector.tools_cache)
            logger.info(f"Tool registry auto-generated with {len(registry)} tools from live MCP")
            
            # Refresh unified registry to pick up the newly generated metadata
            try:
                from agents.pm_agent.unified_tool_registry import UNIFIED_REGISTRY
                UNIFIED_REGISTRY.refresh()
                logger.info("Unified registry refreshed with auto-generated tool metadata")
            except Exception as e:
                logger.warning(f"Could not refresh unified registry: {e}")
        except Exception as e:
            logger.warning(f"Could not auto-generate tool registry: {e}. Falling back to static registry.")

    async def get_project_context(self, project: str = None) -> dict:
        """Fetch project context (area paths, team, iteration, etc.) for LLM planning.
       
        Args:
            project: Project name (defaults to            if not merged_context.get('team'):
                # Only ask if we couldn't find team
                session_ctx.set_pending_clarification(...)or FracPro-OPS).
       
        Returns:
            Dict with keys: project, area_paths, team, iteration, etc.
        """
        if not project:
            from config import config
            project = config.pm_default_project
       
        context = {"project": project}
       
        # Fetch area paths
        try:
            area_list = await self.list_area_paths(project)
            if area_list:
                # Extract just the area path names (not the full formatted output)
                lines = area_list.split('\n')[1:]  # Skip header
                area_paths = [line.replace('- ', '').strip() for line in lines if line.strip()]
                context["area_paths"] = area_paths
        except Exception as e:
            logger.warning(f"Could not fetch area paths: {e}")
            context["area_paths"] = []
       
        # TODO: fetch team, iteration, recent work items, etc. as needed
        from config import config
        context["team"] = config.ado_team
        context["iteration"] = config.ado_iteration
       
        return context

    async def invoke(self, query, session_id: str = None, parent_trace: Any = None, orchestrator_plan: dict = None) -> AsyncIterable[dict]:
        """
        Execute a query using the full PM workflow:
       
        1. Intent Capture → Parse query
        2. Context Resolution → Merge session + env context
        3. Fixed Skill Check → Use deterministic skill if applicable
        4. LLM Planner → Plan MCP call (SKIPPED if orchestrator_plan provided)
        5. Plan Validation → Validate before execution
        6. Tool Execution → Execute via MCP with pagination
        7. Post-Tool Decision → Fixed format vs LLM summary
        8. Response Assembly → Format output
        9. Memory Update → Store context for follow-ups

        Args:
            query: User query (string or dict with 'message' key).
            session_id: Optional session identifier for conversation memory.
            parent_trace: Parent trace from orchestrator for unified session tracking.
            orchestrator_plan: Pre-computed plan from orchestrator's LLM planner (skips Step 4).

        Yields:
            {"is_task_complete": True, "content": result}
        """
        # DO NOT create own trace - use parent trace from orchestrator
        trace = parent_trace  # Use parent for unified session
       
        # B1: Track agent invocation
        invoke_start_time = None
        if self.metrics:
            invoke_start_time = self.metrics.timer_start("agent_invoke")
            self.metrics.increment(MetricNames.AGENT_INVOCATION, {"agent": "pm_agent"})
       
        try:
            # ═══════════════════════════════════════════════════════════
            # STEP 1: INTENT CAPTURE
            # ═══════════════════════════════════════════════════════════
            if isinstance(query, dict):
                # Handle nested A2A format: {'id':..., 'params':{'message':{'parts':[{'type':'text','text':'...'}]}}}
                q = None
                try:
                    parts = query.get('params', {}).get('message', {}).get('parts', [])
                    for part in parts:
                        if isinstance(part, dict) and part.get('type') == 'text' and part.get('text'):
                            q = part['text']
                            break
                except (AttributeError, TypeError):
                    pass
                # Fallback: direct parts format {'role':'user', 'parts':[...]}
                if not q and 'parts' in query and isinstance(query.get('parts'), list):
                    for part in query['parts']:
                        if isinstance(part, dict) and part.get('type') == 'text' and part.get('text'):
                            q = part['text']
                            break
                # Fallback: simple key formats
                if not q:
                    msg = query.get("message")
                    if isinstance(msg, str) and msg:
                        q = msg
                    else:
                        q = query.get("text") or str(query)
            else:
                q = str(query)

            logger.info("=== STEP 1: Intent Capture ===")
            logger.info(f"Query: {q[:100]}")

            # Quick heuristic: detect explicit email requests
            # Patterns: "send email to X", "send an email to X", "email this to X", "email X"
            # COMMENTED OUT FOR TESTING LLM PLANNER DECISION MAKING
            email_recipients = []
            # email_patterns = [
            #     r'send\s+(?:an\s+)?email\s+to\s+"?([^\"\s,>]+@[^\"\s,>]+)"?',
            #     r'email\s+(?:this\s+)?to\s+"?([^\"\s,>]+@[^\"\s,>]+)"?',
            #     r'email\s+"?([^\"\s,>]+@[^\"\s,>]+)"?',
            # ]
            # for pattern in email_patterns:
            #     m = re.search(pattern, q, flags=re.IGNORECASE)
            #     if m:
            #         email_recipients = [m.group(1).strip()]
            #         logger.info(f"Detected email request to: {email_recipients}")
            #         break
           
            # ═══════════════════════════════════════════════════════════
            # STEP 2: CONTEXT RESOLUTION
            # ═══════════════════════════════════════════════════════════
            logger.info("=== STEP 2: Context Resolution ===")
           
            # Get or create session context
            session_ctx = self.memory.get_or_create(session_id or "default")
           
            # Get project context from environment
            project_context = await self.get_project_context()
           
            # Merge with session context (session takes precedence for sticky values)
            merged_context = {**project_context, **session_ctx.get_context_for_llm()}
           
            # Check if this is a follow-up query
            is_follow_up = session_ctx.is_follow_up(q)
            if is_follow_up:
                logger.info(f"Detected follow-up query, reusing context from: {session_ctx.last_tool}")
                merged_context["is_follow_up"] = True
           
            logger.debug(f"Project context: {project_context}")
            logger.debug(f"Session context: {session_ctx.get_context_for_llm()}")
            logger.debug(f"Merged context keys: {list(merged_context.keys())}")
           
            # Auto-fill missing context with defaults (with validation and warnings)
            if not merged_context.get('project'):
                # Validate default project exists before applying
                try:
                    from utilities.mcp.mcp_ado_connector import MCPConnector
                    if self.mcp_connector:
                        # Check if default project exists in ADO
                        projects = await self.mcp_connector.call_tool("core_list_projects", {})
                        if projects and isinstance(projects, list):
                            project_names = [p.get('name') for p in projects if isinstance(p, dict)]
                            if self.DEFAULT_PROJECT in project_names:
                                merged_context['project'] = self.DEFAULT_PROJECT
                                logger.warning(f"[CONTEXT_DEFAULT] ⚠️  Project defaulted to {self.DEFAULT_PROJECT} (query did not specify project)")
                            else:
                                logger.error(f"[CONTEXT_DEFAULT] ❌ DEFAULT_PROJECT '{self.DEFAULT_PROJECT}' does not exist in ADO. Available: {project_names[:5]}")
                                # Don't apply invalid default - let query fail with clear error
                        else:
                            # Fallback: apply default anyway but warn heavily
                            merged_context['project'] = self.DEFAULT_PROJECT
                            logger.warning(f"[CONTEXT_DEFAULT] ⚠️  Could not validate DEFAULT_PROJECT, applying '{self.DEFAULT_PROJECT}' anyway")
                    else:
                        merged_context['project'] = self.DEFAULT_PROJECT
                        logger.warning(f"[CONTEXT_DEFAULT] ⚠️  Project defaulted to {self.DEFAULT_PROJECT} (no MCP connector to validate)")
                except Exception as e:
                    logger.warning(f"[CONTEXT_DEFAULT] Failed to validate DEFAULT_PROJECT: {e}, applying default anyway")
                    merged_context['project'] = self.DEFAULT_PROJECT
           
            # ═══════════════════════════════════════════════════════════
            # STEP 2.5: TEAM CONTEXT RESOLUTION (ENABLED)
            # ═══════════════════════════════════════════════════════════
            logger.info("=== STEP 2.5: Team Context Resolution ===")
           
            q_lower = q.lower()
            # ⚠️ FIX: Narrowed SPRINT_KEYWORDS to only match queries that EXPLICITLY
            # reference sprint/iteration context. Previously included broad terms like
            # "backlog", "active items", "in progress" which matched non-sprint queries
            # and caused silent @CurrentIteration injection for ALL work item queries.
            SPRINT_KEYWORDS = [
                'sprint', 'iteration', 'current sprint', 'this sprint',
                'previous sprint', 'last sprint', 'next sprint',
                'sprint status', 'sprint plan', 'sprint items',
                'iteration items', 'iteration report',
                'bugs in sprint', 'stories in sprint', 'tasks in sprint',
                'what\'s in the sprint', 'my sprint', 'our sprint',
                'sprint velocity', 'sprint capacity'
            ]
            is_sprint_query = any(kw in q_lower for kw in SPRINT_KEYWORDS)
           
            if is_sprint_query:
                extracted_ctx = session_ctx.update_context_from_query_text(q)
               
                if extracted_ctx.get('iteration'):
                    merged_context['iteration'] = extracted_ctx['iteration']
                    merged_context['_iteration_explicit'] = True  # Mark as user-requested
                    logger.info(f"[CONTEXT_DEFAULT] Iteration extracted from query: {extracted_ctx['iteration']}")
                elif not merged_context.get('iteration'):
                    merged_context['iteration'] = self.DEFAULT_ITERATION
                    merged_context['_iteration_explicit'] = True  # Sprint query → iteration is intentional
                    logger.info(f"[CONTEXT_DEFAULT] Iteration defaulted to {self.DEFAULT_ITERATION}")
               
                if extracted_ctx.get('team'):
                    team_extracted = extracted_ctx['team']
                    logger.info(f"[TEAM_RESOLUTION] Extracted team from query: {team_extracted}")
                   
                    from agents.pm_agent.conversation import validate_team_exists
                    project = merged_context.get('project', self.DEFAULT_PROJECT)
                    is_valid, matched_team = await validate_team_exists(team_extracted, project, self.mcp_connector)
                   
                    if is_valid and matched_team:
                        merged_context['team'] = matched_team
                        session_ctx.team = matched_team
                        logger.info(f"[TEAM_RESOLUTION] ✅ Validated team: {matched_team}")
                    else:
                        logger.warning(f"[TEAM_RESOLUTION] ❌ Extracted team '{team_extracted}' not found")
                        merged_context['team'] = self.DEFAULT_TEAM
                        session_ctx.team = self.DEFAULT_TEAM
                        logger.info(f"[TEAM_RESOLUTION] Using default team: {self.DEFAULT_TEAM}")
                elif session_ctx.team:
                    merged_context['team'] = session_ctx.team
                    logger.info(f"[TEAM_RESOLUTION] Using team from session: {session_ctx.team}")
                elif not merged_context.get('team'):
                    # Validate default team before applying
                    default_team = self.DEFAULT_TEAM
                    try:
                        from agents.pm_agent.conversation import validate_team_exists
                        project = merged_context.get('project', self.DEFAULT_PROJECT)
                        is_valid, matched_team = await validate_team_exists(default_team, project, self.mcp_connector)
                        
                        if is_valid and matched_team:
                            session_ctx.team = matched_team
                            merged_context['team'] = matched_team
                            logger.warning(f"[TEAM_RESOLUTION] ⚠️  Team defaulted to: {matched_team} (query did not specify team)")
                        else:
                            logger.error(f"[TEAM_RESOLUTION] ❌ DEFAULT_TEAM '{default_team}' does not exist in project '{project}'")
                            # Don't apply invalid default - let query proceed without team filter
                            session_ctx.team = None
                            merged_context['team'] = None
                    except Exception as e:
                        logger.warning(f"[TEAM_RESOLUTION] Failed to validate DEFAULT_TEAM: {e}, applying default anyway")
                        session_ctx.team = default_team
                        merged_context['team'] = default_team
                        logger.warning(f"[TEAM_RESOLUTION] ⚠️  Team defaulted to: {default_team} (validation failed)")
            else:
                # Non-sprint query: apply default team but NOT default iteration
                # ⚠️ FIX: Do NOT inject @CurrentIteration for non-sprint queries!
                # Previous behavior silently added @CurrentIteration to ALL queries,
                # causing tools like wit_get_work_items_for_iteration to be scoped
                # to current sprint even when user asked for ALL bugs/tasks.
                
                # Extract team from query text (same logic as sprint branch)
                # so that WIQL queries also get the correct area path filter.
                extracted_ctx = session_ctx.update_context_from_query_text(q)
                if extracted_ctx.get('team'):
                    team_extracted = extracted_ctx['team']
                    logger.info(f"[TEAM_RESOLUTION] Extracted team from non-sprint query: {team_extracted}")
                    from agents.pm_agent.conversation import validate_team_exists
                    project = merged_context.get('project', self.DEFAULT_PROJECT)
                    is_valid, matched_team = await validate_team_exists(team_extracted, project, self.mcp_connector)
                    if is_valid and matched_team:
                        merged_context['team'] = matched_team
                        session_ctx.team = matched_team
                        logger.info(f"[TEAM_RESOLUTION] ✅ Validated team: {matched_team}")
                    else:
                        logger.warning(f"[TEAM_RESOLUTION] ❌ Extracted team '{team_extracted}' not found in non-sprint query")
                        if not merged_context.get('team'):
                            merged_context['team'] = self.DEFAULT_TEAM
                            logger.info(f"[TEAM_RESOLUTION] Team defaulted to: {self.DEFAULT_TEAM}")
                elif not merged_context.get('team'):
                    merged_context['team'] = self.DEFAULT_TEAM
                    merged_context['_team_defaulted'] = True
                    logger.info(f"[TEAM_RESOLUTION] Team defaulted to: {self.DEFAULT_TEAM}")
               
                # Iteration is intentionally NOT defaulted for non-sprint queries
                logger.debug(f"[TEAM_RESOLUTION] Non-sprint query - NOT injecting default iteration")
           
            logger.info(f"[TEAM_RESOLUTION] Final context → project={merged_context.get('project')}, team={merged_context.get('team')}, iteration={merged_context.get('iteration')}")

            # ═══════════════════════════════════════════════════════════
            # AREA PATH RESOLUTION: Resolve area path from validated team
            # Uses the pre-existing TEAM_TO_AREA_PATH mapping in context_resolver.
            # This ensures execute_wiql and all other tools can use the area path.
            # ═══════════════════════════════════════════════════════════
            _final_team = merged_context.get('team')
            if _final_team and not merged_context.get('resolved_area_path'):
                try:
                    from orchestrator.context_resolver import get_area_path_for_team
                    _resolved_ap = get_area_path_for_team(_final_team)
                    if _resolved_ap:
                        merged_context['resolved_area_path'] = _resolved_ap
                        # Mark if team was defaulted (not explicitly mentioned)
                        if not extracted_ctx.get('team') and not session_ctx.team:
                            merged_context['_team_defaulted'] = True
                        logger.info(f"[TEAM_RESOLUTION] Resolved area path: {_resolved_ap}")
                    else:
                        logger.debug(f"[TEAM_RESOLUTION] No area path mapping for team: {_final_team}")
                except Exception as e:
                    logger.warning(f"[TEAM_RESOLUTION] Failed to resolve area path: {e}")

            # ═══════════════════════════════════════════════════════════
            # STEP 3: FIXED SKILL CHECK (configurable)
            # ═══════════════════════════════════════════════════════════
            logger.info("=== STEP 3: Fixed Skill Check ===")

            # Controlled via config `pm_use_fixed_skills`.
            # Default: disabled (false) to avoid accidental short-circuits.
            from config import config
            use_fixed = config.pm_use_fixed_skills
            if use_fixed:
                skill_match = should_use_fixed_skill(q)
            #     logger.debug("[PM_AGENT] skill_match = %s", skill_match)
            #     if skill_match:
            #         logger.info(f"Using fixed skill: {skill_match.skill_name} (confidence: {skill_match.confidence:.2f})")
            #        
            #         # Plan Compliance Logging: Log when fixed skill overrides orchestrator plan
            #         if orchestrator_plan:
            #             orch_tool = orchestrator_plan.get("tool")
            #             orch_action = orchestrator_plan.get("action")
            #             if orch_tool or orch_action:
            #                 logger.warning(
            #                     f"[PLAN_COMPLIANCE] Fixed skill '{skill_match.skill_name}' is overriding orchestrator plan! "
            #                     f"Orchestrator requested: tool={orch_tool}, action={orch_action}. "
            #                     f"Fixed skill confidence: {skill_match.confidence:.2f}"
            #                 )
            #        
            #         # Create Langfuse span for fixed skill execution
            #         skill_span = None
            #         if trace and _langfuse_client:
            #             try:
            #                 skill_span = trace.start_observation(
            #                     as_type="SPAN",
            #                     name=f"fixed_skill_{skill_match.skill_name}",
            #                     input={
            #                         "skill": skill_match.skill_name,
            #                         "confidence": skill_match.confidence,
            #                         "extracted_params": skill_match.extracted_params,
            #                         "query": q[:200]
            #                     },
            #                     metadata={
            #                         "skill_type": "fixed",
            #                         "workflow_step": "3_fixed_skill_check"
            #                     }
            #                 )
            #             except Exception as e:
            #                 logger.debug(f"[AGENT] Failed to create skill span: {e}")
            #        
            #         result = await self._execute_fixed_skill(skill_match, merged_context)
            #         logger.debug("[PM_AGENT] Fixed skill result: %s", str(result)[:200] if result else 'None')
            #        
            #         # Finalize skill span
            #         if skill_span:
            #             try:
            #                 skill_span.update(
            #                     output={"result": result[:500] if result else None},
            #                     status_message="success" if result else "no_result"
            #                 )
            #                 skill_span.end()
            #             except Exception as e:
            #                 logger.debug(f"[AGENT] Failed to finalize skill span: {e}")
            #        
            #         if result:
            #             # Update memory and return
            #             session_ctx.update_from_query(q, skill_match.skill_name, skill_match.extracted_params)
            #             session_ctx.add_response(result)
            #             _finalize_langfuse_trace(trace, {"result": result[:500], "skill": skill_match.skill_name})
            #             safe_result = self._sanitize_response(result, "fixed_skill_result")
            #             yield _make_agent_response(content=safe_result, status="SUCCESS")
            #             return
            #         # Skill didn't produce result, fall through to LLM
            #         logger.debug("[PM_AGENT] Fixed skill returned None/empty, falling through to LLM")
            #     else:
            #         logger.info("No fixed skill matched, proceeding to LLM planner")
            # else:
            #     logger.info("Fixed skills disabled via PM_USE_FIXED_SKILLS; proceeding to LLM planner")
           
            # ═══════════════════════════════════════════════════════════
            # STEP 3.5: DETERMINISTIC SHORT-CIRCUITS (bypass LLM for simple cases)
            # ═══════════════════════════════════════════════════════════
            # COMMENTED OUT FOR TESTING LLM PLANNER DECISION MAKING
            # # Check for specific work item ID (4-6 digit number)
            # id_match = re.search(r'(?:^|[^\d])(\d{4,6})(?:[^\d]|$)', q)
            # if id_match:
            #     work_item_id = int(id_match.group(1))
            #     logger.info(f"Detected work item ID {work_item_id}, using direct lookup (bypassing LLM)")
            #     try:
            #         from config import config
            #         project = merged_context.get('project', config.ado_project)
            #         mcp_result = await self.mcp_connector.call_tool("wit_get_work_item", {
            #             "id": work_item_id,
            #             "project": project
            #         })
            #         if mcp_result and mcp_result.strip() and mcp_result.strip() != 'null':
            #             summary = await summarize_mcp_result(q, mcp_result, self.SYSTEM_PROMPT, session_id=session_id, plan=None)
            #             # Sanitize before using in UI or email
            #             summary = self._sanitize_response(summary, "work_item_summary")
            #             # Handle email if requested
            #             if email_recipients:
            #                 try:
            #                     subject = f"PM Agent: Work Item #{work_item_id}"
            #                     html_body = f"<pre>{summary}</pre>"
            #                     ok, send_msg = send_report_attachment(email_recipients, subject, html_body, attachments=None)
            #                     if ok:
            #                         logger.info(f"Email sent to {email_recipients}")
            #                         summary = summary + f"\n\n(Email sent to {email_recipients})"
            #                     else:
            #                         logger.warning(f"Email failed: {send_msg}")
            #                 except Exception as e:
            #                     logger.exception(f"Email error: {e}")
            #             session_ctx.update_from_query(q)
            #             session_ctx.add_response(summary)
            #             _finalize_langfuse_trace(trace, {"result": summary[:500], "work_item_id": work_item_id})
            #             yield _make_agent_response(content=summary, status="SUCCESS")
            #             return
            #     except Exception as e:
            #         logger.warning(f"Direct work item lookup failed for ID {work_item_id}: {e}, falling through to LLM}")
            logger.info("Deterministic short-circuits TEMPORARILY DISABLED for LLM planner testing")
           
            # ═══════════════════════════════════════════════════════════
            # STEP 4: USE ORCHESTRATOR PLAN (agents cannot call Deep LLM)
            # ═══════════════════════════════════════════════════════════
            logger.info("=== STEP 4: Execute with Orchestrator Plan ===")
           
            # ARCHITECTURE RULE: Agents CANNOT call Deep LLM (plan_mcp_call)
            # If we don't have a full orchestrator plan, we return NEEDS_DEEP_ANALYSIS
            # The Orchestrator will then call plan_mcp_call and re-invoke us with the plan
           
            plan = None
           
            if orchestrator_plan:
                plan_type = orchestrator_plan.get("type", "unknown")
                is_light_hint = plan_type == "routing_hint" or orchestrator_plan.get("light_plan_hint", False)
                has_tool = orchestrator_plan.get("tool") is not None
                has_action = orchestrator_plan.get("action") is not None
                
                # ═══════════════════════════════════════════════════════════
                # LIGHT LLM FULL PLAN HANDLING (Phase 3 Enhancement)
                # Light LLM returns plans with structure: {plan: {steps: [...]}, has_full_plan: true}
                # We need to convert this to the format PM Agent expects
                # ═══════════════════════════════════════════════════════════
                has_full_plan = orchestrator_plan.get("has_full_plan", False)
                light_plan_obj = orchestrator_plan.get("plan")
                
                if has_full_plan and light_plan_obj:
                    # Light LLM provided a complete plan - convert to executable format
                    steps = light_plan_obj.get("steps", [])
                    if steps:
                        logger.info(f"[PM_AGENT] Light LLM full plan detected: {len(steps)} step(s), confidence={orchestrator_plan.get('confidence', 0):.2f}")
                        
                        if len(steps) == 1:
                            # Single-step plan - extract and convert
                            step = steps[0]
                            plan = {
                                "action": step.get("action", "call_tool"),
                                "tool": step.get("tool"),
                                "args": step.get("args", {}),
                                "confidence": orchestrator_plan.get("confidence", 0.85),
                                "description": step.get("description", ""),
                                "reasoning": step.get("reasoning", ""),
                                "analysis_criteria": orchestrator_plan.get("analysis_criteria", {}),
                                "_source": "light_llm_single_step",
                            }
                            logger.info(f"[PM_AGENT] Converted Light LLM single-step plan: tool={plan['tool']}")
                        else:
                            # Multi-step plan - convert to execute_plan format
                            execution_plan_steps = []
                            for idx, step in enumerate(steps):
                                step_action = step.get("action", "call_tool")
                                if step_action == "synthesize":
                                    synth_step = {
                                        "step_id": f"step_{idx + 1}",
                                        "type": "synthesize",
                                        "instruction": step.get("args", {}).get("instruction", ""),
                                        "description": step.get("description", ""),
                                    }
                                    # Preserve depends_on from LLM plan
                                    if step.get("depends_on"):
                                        synth_step["depends_on"] = step["depends_on"]
                                    execution_plan_steps.append(synth_step)
                                else:
                                    tool_step = {
                                        "step_id": f"step_{idx + 1}",
                                        "type": step_action,
                                        "tool_name": step.get("tool"),
                                        "args": step.get("args", {}),
                                        "description": step.get("description", ""),
                                        "reasoning": step.get("reasoning", ""),
                                    }
                                    # Preserve depends_on from LLM plan for sequential execution
                                    if step.get("depends_on"):
                                        tool_step["depends_on"] = step["depends_on"]
                                    execution_plan_steps.append(tool_step)
                            
                            plan = {
                                "action": "execute_plan",
                                "execution_plan": {
                                    "steps": execution_plan_steps,
                                    "query": q,
                                    "context": merged_context,
                                },
                                "analysis_criteria": orchestrator_plan.get("analysis_criteria", {}),
                                "confidence": orchestrator_plan.get("confidence", 0.85),
                                "_source": "light_llm_multi_step",
                            }
                            logger.info(f"[PM_AGENT] Converted Light LLM multi-step plan: {len(execution_plan_steps)} steps")
                    else:
                        logger.warning("[PM_AGENT] Light LLM has_full_plan=true but no steps!")
                        plan = None
                
                elif is_light_hint or (not has_tool and not has_action):
                    # Light planner hint OR incomplete plan - need Deep LLM for full planning
                    # Return NEEDS_DEEP_ANALYSIS so Orchestrator can call Deep LLM
                    logger.info(f"[PM_AGENT] Received light hint or incomplete plan, requesting Deep LLM")
                    _finalize_langfuse_trace(trace, {"status": "needs_deep_analysis", "reason": "incomplete_plan"})
                    yield _make_agent_response(
                        content="I need the LLM planner to determine the right approach for this query.",
                        status="NEEDS_DEEP_ANALYSIS",
                        deep_analysis_context={
                            "reason": "no_complete_plan",
                            "query": q,
                            "context": merged_context,
                            "orchestrator_hint": orchestrator_plan
                        }
                    )
                    return
                else:
                    # Full orchestrator plan with tool/action - use directly
                    logger.info(f"[PM_AGENT] Using orchestrator's full plan (tool={orchestrator_plan.get('tool')})")
                    plan = orchestrator_plan
                    logger.debug(f"Orchestrator plan: {json.dumps(plan, indent=2)[:500]}")
            else:
                # No orchestrator plan - need Deep LLM for planning
                # Return NEEDS_DEEP_ANALYSIS so Orchestrator can call Deep LLM
                logger.info(f"[PM_AGENT] No orchestrator plan provided, requesting Deep LLM")
                _finalize_langfuse_trace(trace, {"status": "needs_deep_analysis", "reason": "no_plan"})
                yield _make_agent_response(
                    content="I need the LLM planner to determine the right approach for this query.",
                    status="NEEDS_DEEP_ANALYSIS",
                    deep_analysis_context={
                        "reason": "no_plan_provided",
                        "query": q,
                        "context": merged_context
                    }
                )
                return
           
            if not plan:
                logger.warning("LLM planner returned no plan; falling back to execute_query")
                mcp_result = await self.mcp_connector.execute_query(q)
                summary = await summarize_mcp_result(q, mcp_result, self.SYSTEM_PROMPT, session_id=session_id, plan=None, sprint_context=merged_context)
                # Sanitize before using
                summary = self._sanitize_response(summary, "fallback_summary")
                # If an explicit email was requested, attempt to send the summary
                if 'email_recipients' in locals() and email_recipients:
                    try:
                        subject = f"PM Agent: Results for your request"
                        html_body = f"<pre>{summary}</pre>"
                        ok, send_msg = send_report_attachment(email_recipients, subject, html_body, attachments=None)
                        if ok:
                            logger.info(f"Email sent to {email_recipients}: {send_msg}")
                            summary = summary + f"\n\n(Email sent to {email_recipients})"
                        else:
                            logger.warning(f"Email failed to send to {email_recipients}: {send_msg}")
                            summary = summary + f"\n\n(Email failed: {send_msg})"
                    except Exception as e:
                        logger.exception(f"Error sending email in fallback path: {e}")
                        summary = summary + f"\n\n(Email send error: {e})"

                session_ctx.update_from_query(q)
                session_ctx.add_response(summary)
                _finalize_langfuse_trace(trace, {"result": summary[:500], "fallback": "execute_query"})
                yield _make_agent_response(content=summary, status="SUCCESS")
                return
           
            action = plan.get('action', 'no_tool')
            tool = plan.get('tool')
            args = plan.get('args', {}) or {}
            confidence = plan.get('confidence', 0)
            logger.info(f"LLM plan: action={action}, tool={tool}, confidence={confidence}")
            logger.debug(f"Full plan: {json.dumps(plan, indent=2)}")
           
            # Handle clarification requests
            if action == 'ask_clarification':
                clarification = plan.get('message', 'Could you provide more details?')
                _finalize_langfuse_trace(trace, {"clarification": clarification}, "clarification_requested")
                # Return as NEEDS_DEEP_ANALYSIS so orchestrator can optionally replan
                yield _make_agent_response(
                    content=clarification,
                    status="NEEDS_DEEP_ANALYSIS",
                    deep_analysis_context={
                        "reason": "clarification_needed",
                        "clarification_message": clarification,
                        "original_query": q
                    }
                )
                return
           
            # Handle multi-step execution plans
            if action == 'execute_plan':
                logger.info("=== Executing Multi-Step Plan ===")
                try:
                    from agents.pm_agent.multi_tool_orchestrator import execute_planner_plan
                   
                    # Execute the multi-step plan
                    result = await execute_planner_plan(
                        planner_plan=plan,
                        context=merged_context,
                        tool_executor=self.tool_executor,
                        mcp_connector=self.mcp_connector,
                        session_id=session_id
                    )
                    
                    # [DEBUG] Log exactly what execute_planner_plan returned
                    logger.info(f"[AGENT_RESPONSE_CHAIN] execute_planner_plan returned:")
                    logger.info(f"[AGENT_RESPONSE_CHAIN]   Type: {type(result)}")
                    logger.info(f"[AGENT_RESPONSE_CHAIN]   Length: {len(result) if isinstance(result, str) else 'N/A'} chars")
                    logger.info(f"[AGENT_RESPONSE_CHAIN]   First 300 chars: {result[:300] if isinstance(result, str) else str(result)[:300]}")
                    logger.info(f"[AGENT_RESPONSE_CHAIN]   Last 200 chars: {result[-200:] if isinstance(result, str) and len(result) > 200 else ''}")
                   
                    # FIX #SYNTH-3: Skip double synthesis - execute_planner_plan already
                    # synthesizes results via _synthesize_results() → synthesize_agent_outputs()
                    # Calling summarize_mcp_result again would re-process already-synthesized text
                    summary = result
                    
                    # [DEBUG] Log that we skipped double synthesis
                    logger.info(f"[AGENT_RESPONSE_CHAIN] Using orchestrator synthesis directly (no double synthesis)")
                    
                    summary = self._sanitize_response(summary, "multi_step_result")
                    
                    # [DEBUG] Log what _sanitize_response returned
                    logger.info(f"[AGENT_RESPONSE_CHAIN] _sanitize_response returned:")
                    logger.info(f"[AGENT_RESPONSE_CHAIN]   Type: {type(summary)}")
                    logger.info(f"[AGENT_RESPONSE_CHAIN]   Length: {len(summary) if isinstance(summary, str) else 'N/A'} chars")
                    logger.info(f"[AGENT_RESPONSE_CHAIN]   First 300 chars: {summary[:300]}")
                   
                    session_ctx.update_from_query(q)
                    session_ctx.add_response(summary)
                    _finalize_langfuse_trace(trace, {"result": summary[:500], "action": "execute_plan"})
                    
                    # [DEBUG] Log final response being yielded
                    logger.info(f"[AGENT_RESPONSE_CHAIN] Final response being yielded:")
                    logger.info(f"[AGENT_RESPONSE_CHAIN]   Length: {len(summary)} chars")
                    logger.info(f"[AGENT_RESPONSE_CHAIN]   Content type: {type(summary)}")
                    
                    yield _make_agent_response(content=summary, status="SUCCESS")
                    return
                except Exception as e:
                    logger.exception(f"Multi-step plan execution failed: {e}")
                    error_msg = f"I encountered an error executing the multi-step plan: {str(e)}"
                    _finalize_langfuse_trace(trace, {"error": str(e)}, "execute_plan_failed", level="ERROR")
                    yield _make_agent_response(content=error_msg, status="FAILED", error=str(e))
                    return
           
            # ═══════════════════════════════════════════════════════════
            # STEP 4.5: CONTEXT DATA ANSWER (no tool needed)
            # For queries answerable from already-loaded context data
            # (e.g., "list area paths" - area_paths already in context)
            # ═══════════════════════════════════════════════════════════
            if action == 'context_data_answer' or tool == 'context_data_answer':
                logger.info("=== Context Data Answer (no MCP call needed) ===")
                data_key = args.get('data_key', '')
                context_data = merged_context.get(data_key)
                
                if context_data:
                    if data_key == 'area_paths' and isinstance(context_data, list):
                        project = merged_context.get('project', 'FracPro-OPS')
                        lines = [f"**Area Paths for project '{project}' ({len(context_data)}):**\n"]
                        for i, ap in enumerate(context_data, 1):
                            lines.append(f"{i}. {ap}")
                        summary = "\n".join(lines)
                    elif isinstance(context_data, list):
                        lines = [f"**{data_key.replace('_', ' ').title()} ({len(context_data)}):**\n"]
                        for i, item in enumerate(context_data, 1):
                            lines.append(f"{i}. {item}")
                        summary = "\n".join(lines)
                    elif isinstance(context_data, str):
                        summary = context_data
                    else:
                        summary = str(context_data)
                    
                    session_ctx.update_from_query(q)
                    session_ctx.add_response(summary)
                    _finalize_langfuse_trace(trace, {"result": summary[:500], "action": "context_data_answer", "data_key": data_key})
                    yield _make_agent_response(content=summary, status="SUCCESS")
                    return
                else:
                    logger.warning(f"[CONTEXT_DATA] Requested key '{data_key}' not found in context, falling through to tool execution")

            # ═══════════════════════════════════════════════════════════
            # STEP 5: PLAN VALIDATION (Unified ValidationOrchestrator)
            # ═══════════════════════════════════════════════════════════
            logger.info("=== STEP 5: Plan Validation (Unified Orchestrator) ===")
            
            from utilities.langfuse_client import create_span as _create_span, finalize_span as _finalize_span
            plan_val_span = _create_span(
                name="plan_validation_unified",
                input_data={"tool": plan.get("tool"), "query": q[:200], "has_analysis_criteria": bool(plan.get("analysis_criteria"))},
                metadata={"step": "5_plan_validation_unified"},
                session_id=session_id
            )
           
            # Use unified ValidationOrchestrator (Stages A + B)
            _vo = get_validation_orchestrator(
                tools_cache=self.mcp_connector.tools_cache,
                mcp_connector=self.mcp_connector,
            )
            validation = _vo.validate_plan(query=q, plan=plan, session_id=session_id)
            
            # Finalize plan validation span
            _finalize_span(plan_val_span, output={
                "is_valid": validation.is_valid,
                "alignment_score": validation.alignment_score,
                "issues_count": len(validation.issues),
                "warnings_count": len(validation.warnings) if validation.warnings else 0,
                "intent_alignment": validation.intent_alignment,
                "error": validation.error_message,
            }, status="success" if validation.is_valid else "error")
           
            if not validation.is_valid:
                logger.warning(f"Plan validation failed: {validation.error_message}")
                # Include structured feedback for diagnostics / future retry integration
                feedback_ctx = validation.feedback or {"error": validation.error_message}
                error_msg = f"I couldn't process that request: {validation.error_message}"
                _finalize_langfuse_trace(trace, {
                    "error": validation.error_message,
                    "feedback": feedback_ctx,
                    "alignment_score": validation.alignment_score,
                }, "validation_failed", level="WARNING")
                yield {"is_task_complete": True, "content": error_msg}
                return
           
            if validation.warnings:
                for warning in validation.warnings:
                    logger.warning(f"Validation warning: {warning}")
            
            # Log unified alignment score and any issues
            logger.info(f"[VALIDATION] Alignment score: {validation.alignment_score:.2f}")
            if validation.issues:
                for issue in validation.issues:
                    if issue.severity == ValidationSeverity.WARNING:
                        logger.warning(f"[VALIDATION] {issue.code}: {issue.message}")
                    elif issue.severity == ValidationSeverity.INFO:
                        logger.info(f"[VALIDATION] {issue.code}: {issue.message}")
            
            # Legacy: log intent_alignment if present (from PlanStructuralValidator delegation)
            if validation.intent_alignment:
                alignment = validation.intent_alignment
                score = alignment.get("alignment_score", "N/A")
                logger.info(f"[VALIDATION] Legacy intent alignment score: {score}")
                if alignment.get("warnings"):
                    for w in alignment["warnings"]:
                        logger.warning(f"[VALIDATION] Intent: {w}")
           
            # Use sanitized plan
            plan = validation.sanitized_plan
            tool = plan.get('tool')
            args = plan.get('args', {})
           
            # ═══════════════════════════════════════════════════════════
            # FIX: ADO search API doesn't support '*' wildcard
            # Replace '*' with the workItemType or extract keywords from query
            # ═══════════════════════════════════════════════════════════
            if tool == 'search_workitem':
                search_text = args.get('searchText', '')
                if search_text in ('*', '', None):
                    # Try to extract meaningful search term
                    # Priority 1: Use workItemType if available
                    work_item_types = args.get('workItemType', [])
                    if work_item_types and isinstance(work_item_types, list) and work_item_types[0]:
                        args['searchText'] = work_item_types[0]  # e.g., "Bug"
                        logger.info(f"Replaced searchText='*' with workItemType '{args['searchText']}'")
                    else:
                        # Priority 2: Extract keywords from original query
                        keywords = []
                        q_lower = q.lower()
                        if 'bug' in q_lower:
                            keywords.append('Bug')
                        if 'story' in q_lower or 'stories' in q_lower:
                            keywords.append('User Story')
                        if 'task' in q_lower:
                            keywords.append('Task')
                        if 'feature' in q_lower:
                            keywords.append('Feature')
                        if 'epic' in q_lower:
                            keywords.append('Epic')
                        if 'client' in q_lower:
                            keywords.append('client')
                        if 'customer' in q_lower:
                            keywords.append('customer')
                       
                        if keywords:
                            args['searchText'] = ' OR '.join(keywords)
                        else:
                            # Default fallback
                            args['searchText'] = 'Bug OR Story OR Task'
                        logger.info(f"Replaced searchText='*' with extracted keywords: '{args['searchText']}'")
           
            # ═══════════════════════════════════════════════════════════
            # STEP 5.3: REMOVED - Tool Capability Matching (DEPRECATED)
            # 
            # NOTE: This step is no longer needed because the LLM planner
            # now has COMPLETE tool metadata (required + optional params)
            # in its context, enabling it to make intelligent tool selections.
            #
            # Previously, the LLM could only see required_args without knowing
            # what optional parameters each tool supported. This caused the
            # planner to select wit_get_work_items_for_iteration (no state support)
            # when queries asked for state filters.
            # 
            # FIX: Enhanced _build_tool_summary() in planner.py to include
            # both required and optional parameters with types, so LLM can make
            # intelligent decisions about which tool supports the needed filters.
            # ═══════════════════════════════════════════════════════════
            logger.info("=== STEP 5.3: Skipped (LLM planner has full context) ===")
            
            # Trust the planner's decision (it now has full capability info)
           
            # ═══════════════════════════════════════════════════════════
            # STEP 5.5: IDENTITY RESOLUTION FOR PERSON NAMES
            # Check if any arg contains a person name that needs resolution
            # ═══════════════════════════════════════════════════════════
            if action == 'call_tool' and tool and args:
                person_args = ['assignedTo', 'createdBy', 'searchFilter', 'author']
                for person_arg in person_args:
                    if person_arg in args and args[person_arg]:
                        person_value = args[person_arg]
                        
                        # FIX: Handle array case - unwrap if single-element array
                        person_value_str = person_value
                        if isinstance(person_value, list):
                            if len(person_value) == 1:
                                person_value_str = person_value[0]
                                logger.info(f"[IDENTITY_RESOLUTION] Unwrapped {person_arg} array: {person_value} -> {person_value_str}")
                            elif len(person_value) == 0:
                                continue  # Empty array, skip
                            else:
                                person_value_str = person_value[0]  # Use first element for resolution
                        
                        # FIX: Handle "Unassigned" as a special case - remove the filter
                        # Querying for unassigned items should use WIQL with IS NULL or ISEMPTY
                        if str(person_value_str).lower() in ('unassigned', 'no assignee', 'not assigned', 'nobody'):
                            logger.info(f"[IDENTITY_RESOLUTION] Special case: '{person_value_str}' - removing assignedTo filter (will use WIQL)")
                            # Remove this arg - we'll handle unassigned separately via WIQL
                            del args[person_arg]
                            plan['args'] = args
                            continue
                        
                        # DOMAIN-AWARE EMAIL CHECK: Only skip if it's a REAL org email, not fabricated
                        # Known org domains - emails from these domains are trusted
                        KNOWN_ORG_DOMAINS = ['walkingtree.tech', 'intelloger.com', 'fracpro.com', 'stratagen.com', 'linqx.io']
                        if '@' in str(person_value_str):
                            email_domain = str(person_value_str).split('@')[-1].lower()
                            if email_domain in KNOWN_ORG_DOMAINS:
                                # Looks like a known org email — but verify it's real via ADO API
                                try:
                                    verify_response = await self.mcp_connector.call_tool(
                                        'core_get_identity_ids', {'searchFilter': str(person_value_str)}
                                    )
                                    if verify_response and 'No identities found' not in str(verify_response):
                                        logger.info(f"[IDENTITY_RESOLUTION] Verified real email: '{person_value_str}'")
                                        continue  # Real email, skip resolution
                                    else:
                                        # Fabricated email! Strip to raw name
                                        raw_name = str(person_value_str).split('@')[0].replace('.', ' ').replace('_', ' ').title()
                                        logger.warning(f"[IDENTITY_RESOLUTION] FABRICATED email detected: '{person_value_str}' → using raw name: '{raw_name}'")
                                        person_value_str = raw_name
                                        if isinstance(person_value, list):
                                            args[person_arg] = [raw_name]
                                        else:
                                            args[person_arg] = raw_name
                                        plan['args'] = args
                                except Exception as verify_err:
                                    logger.warning(f"[IDENTITY_RESOLUTION] Email verification failed: {verify_err}, proceeding with resolution")
                                    raw_name = str(person_value_str).split('@')[0].replace('.', ' ').replace('_', ' ').title()
                                    person_value_str = raw_name
                            else:
                                # Unknown domain (external) — skip resolution, pass through
                                logger.info(f"[IDENTITY_RESOLUTION] External email domain '{email_domain}', passing through")
                                continue
                       
                        try:
                            from utilities.identity_resolution import resolve_person_name, is_full_name_or_email
                           
                            # Check if this looks like a first-name-only query
                            if not is_full_name_or_email(str(person_value_str)):
                                # Single word name - may need clarification
                                logger.info(f"[IDENTITY_RESOLUTION] Resolving ambiguous name: '{person_value_str}'")
                                resolution = await resolve_person_name(str(person_value_str), self.mcp_connector)
                               
                                if resolution.needs_clarification:
                                    # Need to ask user for clarification
                                    logger.info(f"[IDENTITY_RESOLUTION] Clarification needed for '{person_value_str}'")
                                    _finalize_langfuse_trace(trace, {
                                        "clarification": resolution.clarification_message,
                                        "person_name": person_value_str
                                    }, "identity_clarification")
                                    yield {"is_task_complete": True, "content": resolution.clarification_message}
                                    return
                                elif resolution.error_message:
                                    # Identity API didn't find a match — DON'T stop execution.
                                    # Proceed with the raw name and let the SEARCH FALLBACK
                                    # (after tool execution) handle it using `a:<name>` prefix
                                    # which searches across ALL iterations/areas/teams.
                                    logger.info(f"[IDENTITY_RESOLUTION] No match for '{person_value_str}' via identity API. "
                                                f"Proceeding with raw name; search fallback will handle it.")
                                elif resolution.resolved:
                                    # Successfully resolved - update the arg
                                    logger.info(f"[IDENTITY_RESOLUTION] Resolved '{person_value_str}' → '{resolution.identity_email}'")
                                    args[person_arg] = resolution.identity_email
                                    plan['args'] = args
                        except ImportError as ie:
                            logger.warning(f"Identity resolution module not available: {ie}")
                        except Exception as e:
                            logger.warning(f"Identity resolution failed for '{person_value_str}': {e}")
           
            # ═══════════════════════════════════════════════════════════
            # STEP 6: TOOL EXECUTION
            # ═══════════════════════════════════════════════════════════
            logger.info("=== STEP 6: Tool Execution ===")
           
            mcp_result = None
            result_count = 0
           
            if action == 'call_tool' and tool:
                if tool == 'list_area_paths':
                    project_arg = args.get('project') or merged_context.get('project')
                    logger.info(f"Executing fixed skill: list_area_paths(project={project_arg})")
                    mcp_result = await self.list_area_paths(project_arg)
                elif tool == 'bug_areas_highlight':
                    # Local wrapper: call features.bug_area_highlight.skill.run_bug_areas_highlight
                    # Normalize args into a params dict expected by the skill handler
                    try:
                        from features.bug_area_highlight.skill import run_bug_areas_highlight

                        if args is None:
                            args = {}

                        # Build params dict from args
                        params = dict(args)
                        # Include project from context if not specified
                        if 'project' not in params:
                            params['project'] = merged_context.get('project')

                        logger.info(f"Executing bug_areas_highlight skill with params: {params}")
                        exec_result = await run_bug_areas_highlight(params)
                       
                        # exec_result is a SkillResult dataclass with success, result, error, metadata
                        if exec_result.success:
                            result_count = exec_result.result.get('recurring_count', exec_result.result.get('count', 0)) if isinstance(exec_result.result, dict) else 0
                            mcp_result = json.dumps({
                                'success': True,
                                'result': exec_result.result,
                                'metadata': exec_result.metadata
                            }, indent=2)
                            logger.debug(f"bug_areas_highlight successful: count={result_count}")
                        else:
                            mcp_result = f"Tool execution failed: {exec_result.error}"
                            logger.debug(f"bug_areas_highlight failed: {exec_result.error}")
                    except Exception as e:
                        mcp_result = f"Local tool error: {e}"
                        logger.exception("Error running bug_areas_highlight: %s", e)
                elif tool == 'iteration_report':
                    # Local skill: Generate iteration/sprint report
                    try:
                        from agents.pm_skill_agent.skills import SKILL_DEFINITIONS
                       
                        if args is None:
                            args = {}
                       
                        params = dict(args)
                        if 'project' not in params:
                            params['project'] = merged_context.get('project')
                        if 'iteration' not in params:
                            params['iteration'] = merged_context.get('iteration', '@CurrentIteration')
                        if 'team' not in params and merged_context.get('team'):
                            params['team'] = merged_context.get('team')
                       
                        logger.info(f"Executing iteration_report skill with params: {params}")
                        skill_def = SKILL_DEFINITIONS.get('iteration_report')
                        if skill_def and skill_def.handler:
                            exec_result = await skill_def.handler(params)
                            if exec_result.success:
                                mcp_result = json.dumps({
                                    'success': True,
                                    'result': exec_result.result,
                                    'metadata': exec_result.metadata
                                }, indent=2)
                                # iteration_report returns 'total_items' and 'filtered_items', not 'count'
                                if isinstance(exec_result.result, dict):
                                    result_count = exec_result.result.get('total_items', 0) or exec_result.result.get('count', 0)
                                else:
                                    result_count = 0
                            else:
                                mcp_result = f"Iteration report failed: {exec_result.error}"
                        else:
                            mcp_result = "iteration_report skill not available"
                    except Exception as e:
                        mcp_result = f"Error running iteration_report: {e}"
                        logger.exception("Error running iteration_report: %s", e)
               
                elif tool == 'overlooked_stories':
                    # Local skill: Find overlooked/stale user stories via SKILL_DEFINITIONS
                    try:
                        from agents.pm_skill_agent.skills import SKILL_DEFINITIONS
                       
                        if args is None:
                            args = {}
                       
                        params = dict(args)
                        if 'project' not in params:
                            params['project'] = merged_context.get('project')
                        if 'team' not in params and merged_context.get('team'):
                            params['team'] = merged_context.get('team')
                       
                        logger.info(f"Executing overlooked_stories skill with params: {params}")
                        skill_def = SKILL_DEFINITIONS.get('overlooked_stories')
                        if skill_def and skill_def.handler:
                            exec_result = await skill_def.handler(params)
                            if exec_result.success:
                                mcp_result = json.dumps({
                                    'success': True,
                                    'result': exec_result.result,
                                    'metadata': exec_result.metadata
                                }, indent=2)
                                result_count = exec_result.result.get('count', 0) if isinstance(exec_result.result, dict) else 0
                            else:
                                mcp_result = f"Overlooked stories failed: {exec_result.error}"
                        else:
                            mcp_result = "overlooked_stories skill not available"
                    except Exception as e:
                        mcp_result = f"Error running overlooked_stories: {e}"
                        logger.exception("Error running overlooked_stories: %s", e)
               
                elif tool == 'search_developers_by_skill':
                    # Local skill: Search developer knowledge base using Milvus vector DB
                    try:
                        from .developer_kb_handler import search_developers_by_skill, format_developer_search_results
                       
                        if args is None:
                            args = {}
                       
                        params = dict(args)
                        # Extract technology from args or query
                        technology = params.get('technology')
                        skill_query = params.get('skill_query', q)  # Use original query if not specified
                        top_k = params.get('top_k')
                        include_evidence = params.get('include_evidence', True)
                       
                        # Get parent trace for Langfuse
                        parent_trace = trace
                       
                        logger.info(f"Executing search_developers_by_skill with technology={technology}, query={skill_query[:50]}...")
                       
                        result = await search_developers_by_skill(
                            skill_query=skill_query,
                            technology=technology,
                            top_k=top_k,
                            include_evidence=include_evidence,
                            parent_trace=parent_trace
                        )
                       
                        if result.get('success'):
                            # Format the results for display
                            formatted = format_developer_search_results(result, query=q)
                            mcp_result = formatted
                            result_count = result.get('total_found', 0)
                        else:
                            mcp_result = f"Developer search failed: {result.get('error', 'Unknown error')}"
                    except Exception as e:
                        mcp_result = f"Error running search_developers_by_skill: {e}"
                        logger.exception("Error running search_developers_by_skill: %s", e)
               
                elif tool == 'execute_wiql':
                    # ═══════════════════════════════════════════════════════════
                    # CANONICAL WIQL EXECUTION — Single path, no rebuilding
                    # LLM generates WIQL → execute_wiql → REST API → hydrate
                    # ═══════════════════════════════════════════════════════════
                    try:
                        from agents.pm_agent.pm_skills.wiql_skill import execute_wiql
                       
                        if args is None:
                            args = {}
                       
                        project = args.get('project') or merged_context.get('project', 'FracPro-OPS')
                        wiql = args.get('wiql')
                        top = args.get('top', 1000)
                       
                        if not wiql:
                            mcp_result = "No WIQL query provided by planner"
                            logger.warning("[execute_wiql] No WIQL in args")
                        else:
                            logger.info(f"[execute_wiql] Executing LLM-generated WIQL for project={project}")
                            # Pass merged_context so enrichment can resolve
                            # area paths, team, iteration macros, etc.
                            wiql_context = {
                                "team": merged_context.get("team", ""),
                                "area_paths": merged_context.get("area_paths", []),
                                "mcp_connector": getattr(self, '_mcp_connector', None) or getattr(self, 'mcp_connector', None),
                            }
                            result = await execute_wiql(project=project, wiql=wiql, top=top, context=wiql_context)
                           
                            if result.get('success'):
                                result_count = result.get('count', 0)
                                mcp_result = json.dumps(result, indent=2)
                                logger.info(f"[execute_wiql] Found {result_count} work items")
                            else:
                                mcp_result = f"WIQL query failed: {result.get('error', 'Unknown error')}"
                                logger.warning(f"[execute_wiql] Query failed: {result.get('error')}")
                    except Exception as e:
                        mcp_result = f"Error running execute_wiql: {e}"
                        logger.exception("Error running execute_wiql: %s", e)
               
                elif tool in ('work_get_iteration_work_items', 'wit_get_work_items_for_iteration'):
                    # This is an MCP tool - execute via ToolExecutor but ensure all required args are present
                    try:
                        if args is None:
                            args = {}
                        
                        # ═══════════════════════════════════════════════════════════
                        # FIX: Unwrap array values that MUST be single strings
                        # Only unwrap project, team, iterationId, areaPath - these must be single values
                        # DO NOT unwrap workItemType and state - these can legitimately be arrays
                        # when querying for multiple types/states (e.g., "show all work items")
                        # ═══════════════════════════════════════════════════════════
                        for field in ['project', 'team', 'iterationId', 'areaPath']:
                            if field in args and isinstance(args[field], list):
                                original = args[field]
                                args[field] = args[field][0] if args[field] else None
                                logger.info(f"[FIX] Unwrapped {field} array: {original} -> {args[field]}")
                        
                        # For workItemType and state: only unwrap if it's a single-element list
                        # to avoid losing "all types/states" intent when LLM sends full list
                        for field in ['workItemType', 'state']:
                            if field in args and isinstance(args[field], list):
                                if len(args[field]) == 1:
                                    # Single element list - unwrap to string
                                    original = args[field]
                                    args[field] = args[field][0]
                                    logger.info(f"[FIX] Unwrapped single-element {field} array: {original} -> {args[field]}")
                                # Otherwise keep as array - this means "all of these types/states"
                       
                        # ═══════════════════════════════════════════════════════════
                        # STATE VALIDATION: Validate states against centralized config
                        # ═══════════════════════════════════════════════════════════
                        state_filter = args.get('state')
                        if state_filter:
                            try:
                                from config import config as _cfg
                                all_known = _cfg.get_all_known_states()
                                all_known_lower = {s.lower(): s for s in all_known}
                                
                                # Convert to list if single value
                                states_to_validate = state_filter if isinstance(state_filter, list) else [state_filter]
                                
                                logger.info(f"[STATE_VALIDATION] Validating planner-resolved states: {states_to_validate}")
                                
                                # Validate each state against known states
                                validated_states = []
                                for user_state in states_to_validate:
                                    canonical = all_known_lower.get(user_state.lower().strip())
                                    if canonical:
                                        validated_states.append(canonical)
                                        logger.info(f"[STATE_VALIDATION] '{user_state}' → '{canonical}' (valid)")
                                    else:
                                        logger.warning(f"[STATE_VALIDATION] State '{user_state}' not in known states, skipping")
                                
                                # Update state filter with validated states
                                if validated_states:
                                    args['state'] = validated_states if len(validated_states) > 1 else validated_states[0]
                                    logger.info(f"[STATE_VALIDATION] Final validated states: {args['state']}")
                                else:
                                    logger.warning(f"[STATE_VALIDATION] No states validated, removing state filter")
                                    args.pop('state', None)
                            except Exception as e:
                                logger.error(f"[STATE_VALIDATION] Error validating states: {e}")
                       
                        # Auto-inject required args from context
                        if 'project' not in args:
                            args['project'] = merged_context.get('project')
                        if 'team' not in args:
                            args['team'] = merged_context.get('team', args.get('project'))  # Default team = project name
                        if 'iterationId' not in args:
                            # Get current iteration if not specified
                            args['iterationId'] = merged_context.get('iteration', '@CurrentIteration')
                       
                        # ═══════════════════════════════════════════════════════════
                        # NORMALIZE ITERATION ID: Handle sprint names like "26.1", "26.01"
                        # Convert these to @CurrentIteration since the tool needs a GUID
                        # ═══════════════════════════════════════════════════════════
                        iteration_id_raw = args.get('iterationId', '')
                        if iteration_id_raw and iteration_id_raw not in ('@CurrentIteration', '@PreviousIteration'):
                            # Check if it's a sprint name pattern (e.g., "26.1", "26.01", "Sprint 26.1")
                            import re
                            sprint_name_pattern = re.compile(r'^(?:Sprint\s*)?(\d+\.\d+|\d+)$', re.IGNORECASE)
                            if sprint_name_pattern.match(str(iteration_id_raw)):
                                # It's a sprint name, not a GUID - resolve it by looking up iterations
                                logger.info(f"Detected sprint name '{iteration_id_raw}' instead of GUID - will resolve from team iterations")
                                # For now, treat as @CurrentIteration since we need iteration lookup
                                # The actual resolution will happen below
                                args['iterationId'] = '@CurrentIteration'
                                args['_original_sprint_name'] = iteration_id_raw
                        
                        # ═══════════════════════════════════════════════════════════
                        # RESOLVE @CurrentIteration to actual iteration ID
                        # The MCP wit_get_work_items_for_iteration needs actual iteration ID, not macro
                        # ═══════════════════════════════════════════════════════════
                        if args.get('iterationId') in ('@CurrentIteration', '@PreviousIteration'):
                            target_iteration_type = args.get('iterationId')  # Save which one we're resolving
                            try:
                                project = args.get('project', merged_context.get('project', 'FracPro-OPS'))
                                team = args.get('team') or merged_context.get('team') or ''
                               
                                # ═══════════════════════════════════════════════════════════
                                # RESOLVE TEAM: Try to get default team from project if not specified
                                # Uses smart selection to find team with iterations
                                # ═══════════════════════════════════════════════════════════
                                if not team or team == project:
                                    logger.info(f"Team not specified or same as project, attempting to resolve default team for project={project}")
                                    try:
                                        # Try to get teams for the project
                                        teams_response = await self.mcp_connector.call_tool('core_list_project_teams', {'project': project})
                                        if teams_response and teams_response != 'null':
                                            teams_data = json.loads(teams_response) if isinstance(teams_response, str) else teams_response
                                            teams_list = teams_data.get('value', []) if isinstance(teams_data, dict) else teams_data
                                           
                                            if isinstance(teams_list, list) and len(teams_list) > 0:
                                                # SMART TEAM SELECTION
                                                team_found = None
                                               
                                                # Priority 1: Team matching project name
                                                for t in teams_list:
                                                    if t.get('name', '').lower() == project.lower():
                                                        team_found = t.get('name')
                                                        logger.info(f"Found team matching project name: {team_found}")
                                                        break
                                               
                                                # Priority 2: Team with "Suite" in name or project prefix (avoid Technical teams)
                                                if not team_found:
                                                    prefix = project.split('-')[0] if '-' in project else project
                                                    for t in teams_list:
                                                        name = t.get('name', '')
                                                        # Prefer Suite teams or teams with project prefix, avoid technical teams
                                                        if ('suite' in name.lower() or prefix.lower() in name.lower()) and 'technical' not in name.lower() and 'archive' not in name.lower():
                                                            team_found = name
                                                            logger.info(f"Found team with Suite/project prefix: {team_found}")
                                                            break
                                               
                                                # Fallback to first team
                                                team = team_found or teams_list[0].get('name', project)
                                                logger.info(f"Resolved default team from project: {team}")
                                                args['team'] = team
                                            else:
                                                # Fallback: Use project name as team (common in ADO)
                                                team = project
                                                logger.warning(f"No teams found, using project name as team: {team}")
                                                args['team'] = team
                                    except Exception as team_err:
                                        # Fallback: Use project name as team
                                        team = project
                                        logger.warning(f"Failed to resolve team, using project name as fallback: {team} (error: {team_err})")
                                        args['team'] = team
                               
                                logger.info(f"Resolving {target_iteration_type} for project={project}, team={team}")

                               
                                # For @PreviousIteration, we need all iterations to find the previous one
                                # For @CurrentIteration, timeframe=current is sufficient
                                # For specific sprint names, we need all iterations to search
                                original_sprint_name = args.get('_original_sprint_name')
                                timeframe = None if target_iteration_type == '@PreviousIteration' or original_sprint_name else 'current'
                               
                                # Call work_list_team_iterations
                                iter_response = await self.mcp_connector.call_tool('work_list_team_iterations', {
                                    'project': project,
                                    'team': team,
                                    **(({'timeframe': timeframe} if timeframe else {}))
                                })
                               
                                if iter_response and iter_response != 'null' and iter_response != 'No iterations found':
                                    iter_data = json.loads(iter_response) if isinstance(iter_response, str) else iter_response
                                   
                                    # ═══════════════════════════════════════════════════════════
                                    # RESOLVE SPECIFIC SPRINT NAME (e.g., "26.1", "25.22")
                                    # This takes priority over @CurrentIteration if set
                                    # ═══════════════════════════════════════════════════════════
                                    if original_sprint_name and isinstance(iter_data, list):
                                        # Normalize sprint name: "26.01" -> "26.1", "Sprint 26.1" -> "26.1"
                                        sprint_normalized = re.sub(r'^(?:Sprint\s*)?', '', str(original_sprint_name), flags=re.IGNORECASE).strip()
                                        parts = sprint_normalized.split('.')
                                        if len(parts) == 2:
                                            major = parts[0].lstrip('0') or '0'
                                            minor = parts[1].lstrip('0') or '0'
                                            sprint_normalized = f"{major}.{minor}"
                                        
                                        logger.info(f"Resolving specific sprint name '{original_sprint_name}' (normalized: {sprint_normalized}) from {len(iter_data)} iterations")
                                        
                                        # Search for matching iteration by name
                                        found_iter = None
                                        for it in iter_data:
                                            iter_name = it.get('name', '')
                                            # Normalize iteration name the same way
                                            iter_name_parts = iter_name.split('.')
                                            if len(iter_name_parts) == 2:
                                                name_major = iter_name_parts[0].lstrip('0') or '0'
                                                name_minor = iter_name_parts[1].lstrip('0') or '0'
                                                iter_name_normalized = f"{name_major}.{name_minor}"
                                            else:
                                                iter_name_normalized = iter_name
                                            
                                            if sprint_normalized == iter_name_normalized:
                                                found_iter = it
                                                break
                                            
                                            # Also check if sprint is in the path
                                            iter_path = it.get('path', '')
                                            if sprint_normalized in iter_path:
                                                found_iter = it
                                                break
                                        
                                        if found_iter:
                                            iter_id = found_iter.get('id') or found_iter.get('identifier')
                                            iter_path = found_iter.get('path')
                                            if iter_id:
                                                args['iterationId'] = str(iter_id)
                                                if iter_path:
                                                    args['iterationPath'] = iter_path
                                                logger.info(f"✅ Resolved sprint '{original_sprint_name}' to: {iter_id} ({found_iter.get('name', 'Unknown')}), path={iter_path}")
                                                # Clear the marker
                                                args.pop('_original_sprint_name', None)
                                        else:
                                            logger.warning(f"❌ Could not find sprint '{original_sprint_name}' in team iterations. Available: {[it.get('name') for it in iter_data[:10]]}")
                                            # Fall back to current iteration
                                    
                                    elif target_iteration_type == '@PreviousIteration':
                                        # ═══════════════════════════════════════════════════════════
                                        # RESOLVE @PreviousIteration - find the iteration before current
                                        # ═══════════════════════════════════════════════════════════
                                        if isinstance(iter_data, list) and len(iter_data) > 0:
                                            # Sort iterations by startDate descending to find current and previous
                                            from datetime import datetime as dt
                                            now = dt.now()
                                           
                                            # Parse and sort iterations
                                            sorted_iterations = []
                                            current_iter_idx = -1
                                           
                                            for idx, it in enumerate(iter_data):
                                                attrs = it.get('attributes', {})
                                                start_str = attrs.get('startDate', '')
                                                finish_str = attrs.get('finishDate', '')
                                               
                                                try:
                                                    start_dt = dt.fromisoformat(start_str.replace('Z', '+00:00')) if start_str else None
                                                    finish_dt = dt.fromisoformat(finish_str.replace('Z', '+00:00')) if finish_str else None
                                                except:
                                                    start_dt = None
                                                    finish_dt = None
                                               
                                                sorted_iterations.append({
                                                    'iteration': it,
                                                    'start': start_dt,
                                                    'finish': finish_dt,
                                                    'id': it.get('id') or it.get('identifier'),
                                                    'name': it.get('name', 'Unknown')
                                                })
                                               
                                                # Check if this is the current iteration
                                                if start_dt and finish_dt:
                                                    if start_dt.replace(tzinfo=None) <= now <= finish_dt.replace(tzinfo=None):
                                                        current_iter_idx = len(sorted_iterations) - 1
                                           
                                            # Sort by start date ascending
                                            sorted_iterations.sort(key=lambda x: x['start'] or dt.min)
                                           
                                            # Find current iteration index in sorted list
                                            if current_iter_idx == -1:
                                                for idx, it in enumerate(sorted_iterations):
                                                    if it['start'] and it['finish']:
                                                        if it['start'].replace(tzinfo=None) <= now <= it['finish'].replace(tzinfo=None):
                                                            current_iter_idx = idx
                                                            break
                                           
                                            # Get previous iteration (one before current)
                                            if current_iter_idx > 0:
                                                prev_iter = sorted_iterations[current_iter_idx - 1]
                                                iter_id = prev_iter['id']
                                                iter_obj = prev_iter.get('iteration', {})
                                                iter_path = iter_obj.get('path')
                                                if iter_id:
                                                    args['iterationId'] = str(iter_id)
                                                    # Store iteration path for WIQL queries
                                                    if iter_path:
                                                        args['iterationPath'] = iter_path
                                                    logger.info(f"Resolved @PreviousIteration to: {iter_id} ({prev_iter['name']}), path={iter_path}")
                                            elif current_iter_idx == -1 and len(sorted_iterations) >= 2:
                                                # If no current iteration found, use the second-to-last completed one
                                                # Find the last completed iteration
                                                completed = [it for it in sorted_iterations if it['finish'] and it['finish'].replace(tzinfo=None) < now]
                                                if len(completed) >= 2:
                                                    prev_iter = completed[-2]  # Second to last completed
                                                    iter_id = prev_iter['id']
                                                    iter_obj = prev_iter.get('iteration', {})
                                                    iter_path = iter_obj.get('path')
                                                    if iter_id:
                                                        args['iterationId'] = str(iter_id)
                                                        # Store iteration path for WIQL queries
                                                        if iter_path:
                                                            args['iterationPath'] = iter_path
                                                        logger.info(f"Resolved @PreviousIteration to: {iter_id} ({prev_iter['name']}), path={iter_path} [fallback: 2nd-to-last completed]")
                                                elif len(completed) == 1:
                                                    # Only one completed, use it as previous
                                                    prev_iter = completed[0]
                                                    iter_id = prev_iter['id']
                                                    iter_obj = prev_iter.get('iteration', {})
                                                    iter_path = iter_obj.get('path')
                                                    if iter_id:
                                                        args['iterationId'] = str(iter_id)
                                                        # Store iteration path for WIQL queries
                                                        if iter_path:
                                                            args['iterationPath'] = iter_path
                                                        logger.info(f"Resolved @PreviousIteration to: {iter_id} ({prev_iter['name']}), path={iter_path} [only completed iteration]")
                                            else:
                                                logger.warning("Could not find previous iteration - current is the first iteration")
                                    else:
                                        # ═══════════════════════════════════════════════════════════
                                        # RESOLVE @CurrentIteration - existing logic
                                        # ═══════════════════════════════════════════════════════════
                                        if isinstance(iter_data, list) and len(iter_data) > 0:
                                            current_iter = iter_data[0]
                                            iter_id = current_iter.get('id') or current_iter.get('identifier')
                                            iter_path = current_iter.get('path')
                                            if iter_id:
                                                args['iterationId'] = str(iter_id)
                                                # Store iteration path for WIQL queries (they expect path string, not GUID)
                                                if iter_path:
                                                    args['iterationPath'] = iter_path
                                                logger.info(f"Resolved @CurrentIteration to: {iter_id} ({current_iter.get('name', 'Unknown')}), path={iter_path}")
                                        elif isinstance(iter_data, dict):
                                            iter_id = iter_data.get('id') or iter_data.get('identifier')
                                            iter_path = iter_data.get('path')
                                            if iter_id:
                                                args['iterationId'] = str(iter_id)
                                                # Store iteration path for WIQL queries
                                                if iter_path:
                                                    args['iterationPath'] = iter_path
                                                logger.info(f"Resolved @CurrentIteration to: {iter_id}, path={iter_path}")
                                else:
                                    # Fallback: Use WIQL to find work items in current iteration
                                    logger.warning(f"Could not resolve {target_iteration_type} via MCP, falling back to WIQL")
                            except Exception as e:
                                logger.warning(f"Failed to resolve {target_iteration_type}: {e}")
                       
                        # ═══════════════════════════════════════════════════════════
                        # RESOLVE AREA PATH if present
                        # Use area path resolver to find canonical path and build WIQL if needed
                        # ═══════════════════════════════════════════════════════════
                        area_path_raw = args.get('areaPath') or merged_context.get('areaPath')
                        area_path_resolved = None
                        use_wiql_for_area = False
                        
                        # FIX: Handle case where LLM wraps areaPath in an array
                        if isinstance(area_path_raw, list):
                            area_path_raw = area_path_raw[0] if area_path_raw else None
                            logger.info(f"[FIX] Unwrapped areaPath array: {area_path_raw}")
                       
                        if area_path_raw:
                            try:
                                from utilities.area_path_resolver import resolve_area_path
                                from config import config
                               
                                logger.info(f"Resolving area path: '{area_path_raw}'")
                               
                                # Resolve area path using ADO classification tree
                                resolution = resolve_area_path(
                                    org=config.ado_org_url.split("/")[-1].strip(),
                                    project=args.get('project', 'FracPro-OPS'),
                                    pat=config.ado_pat,
                                    user_text=area_path_raw,
                                    top_k=3
                                )
                               
                                status = resolution.get('status')
                               
                                if status in ('ok', 'likely', 'ambiguous'):
                                    # Use the top resolved choice without prompting the user.
                                    choice = resolution.get('choice')
                                    if choice:
                                        area_path_resolved = choice
                                    else:
                                        top_matches = resolution.get('top_matches', [])
                                        if top_matches:
                                            area_path_resolved = top_matches[0][0]

                                    confidence = resolution.get('confidence', 0)
                                    logger.info(f"Area path resolved (auto-accepted): '{area_path_raw}' -> '{area_path_resolved}' (confidence={confidence:.2f})")
                                    use_wiql_for_area = True
                               
                                elif status in ('no_match', 'no_good_match'):
                                    logger.warning(f"Could not resolve area path '{area_path_raw}' - searching without area filter")
                                   
                            except Exception as e:
                                logger.error(f"Area path resolution failed: {e}", exc_info=True)
                       
                        # If area path was resolved, use WIQL instead of wit_get_work_items_for_iteration
                        if use_wiql_for_area and area_path_resolved:
                            try:
                                from utilities.area_path_resolver import build_area_path_wiql_clause
                               
                                project = args.get('project', 'FracPro-OPS')
                                iteration_id = args.get('iterationId', '@CurrentIteration')
                                # Use iteration path if available (WIQL expects path string, not GUID)
                                iteration_path = args.get('iterationPath')
                                
                                # ═══════════════════════════════════════════════════════════
                                # CRITICAL FIX: Include workItemType and state filters in WIQL
                                # Filtering MUST happen at the ADO query level, NOT in synthesis
                                # ═══════════════════════════════════════════════════════════
                                work_item_type = args.get('workItemType')
                                state_filter = args.get('state')
                                
                                # ═══════════════════════════════════════════════════════════
                                # STATE VALIDATION: Validate states against centralized config
                                # ═══════════════════════════════════════════════════════════
                                if state_filter:
                                    from config import config as _cfg
                                    all_known = _cfg.get_all_known_states()
                                    all_known_lower = {s.lower(): s for s in all_known}
                                    
                                    # Convert to list if single value
                                    states_to_validate = state_filter if isinstance(state_filter, list) else [state_filter]
                                    
                                    logger.info(f"[STATE_VALIDATION] Validating planner-resolved states: {states_to_validate}")
                                    
                                    # Validate each state against known states
                                    validated_states = []
                                    for user_state in states_to_validate:
                                        canonical = all_known_lower.get(user_state.lower().strip())
                                        if canonical:
                                            validated_states.append(canonical)
                                            logger.info(f"[STATE_VALIDATION] '{user_state}' → '{canonical}' (valid)")
                                        else:
                                            logger.warning(f"[STATE_VALIDATION] State '{user_state}' not in known states, skipping")
                                    
                                    # Update state_filter with validated states
                                    if validated_states:
                                        state_filter = validated_states
                                        logger.info(f"[STATE_VALIDATION] Final validated states: {state_filter}")
                                    else:
                                        logger.warning(f"[STATE_VALIDATION] No states validated, removing state filter")
                                        state_filter = None
                                
                                # ═══════════════════════════════════════════════════════════
                                # DETECT "ALL" INTENT: If LLM sends a list containing all/most types,
                                # it means "don't filter by this" - skip adding the filter clause
                                # NOTE: ALL_STATES removed - now dynamically discovered per project
                                # ═══════════════════════════════════════════════════════════
                                ALL_WORK_ITEM_TYPES = {'Bug', 'Dev Bug', 'Task', 'User Story', 'Feature', 'Initiatives', 'Test Case'}
                                
                                # Check if workItemType is "all types" (4+ types in the list means "all")
                                work_item_type_is_all = (
                                    isinstance(work_item_type, list) and 
                                    len(work_item_type) >= 4 and 
                                    len(set(work_item_type) & ALL_WORK_ITEM_TYPES) >= 4
                                )
                                if work_item_type_is_all:
                                    logger.info(f"[WIQL_FILTER] workItemType contains {len(work_item_type)} types - treating as 'all types', skipping filter")
                                    work_item_type = None  # Don't add filter
                                
                                # Build dynamic WHERE clauses
                                where_clauses = [f"[System.TeamProject] = '{project}'"]
                                where_clauses.append(f"[System.AreaPath] UNDER '{area_path_resolved}'")
                                
                                if iteration_path:
                                    where_clauses.append(f"[System.IterationPath] = '{iteration_path}'")
                                
                                # Add workItemType filter if specified (and not "all types")
                                if work_item_type:
                                    # Handle both single value and list
                                    if isinstance(work_item_type, list):
                                        types_clause = " OR ".join([f"[System.WorkItemType] = '{t}'" for t in work_item_type])
                                        where_clauses.append(f"({types_clause})")
                                    else:
                                        where_clauses.append(f"[System.WorkItemType] = '{work_item_type}'")
                                    logger.info(f"[WIQL_FILTER] Adding workItemType filter: {work_item_type}")
                                
                                # Add state filter if specified (after resolution)
                                if state_filter:
                                    # Handle both single value and list
                                    if isinstance(state_filter, list):
                                        states_clause = " OR ".join([f"[System.State] = '{s}'" for s in state_filter])
                                        where_clauses.append(f"({states_clause})")
                                    else:
                                        where_clauses.append(f"[System.State] = '{state_filter}'")
                                    logger.info(f"[WIQL_FILTER] Adding resolved state filter: {state_filter}")

                                
                                # Build the full WIQL query
                                where_sql = "\n                                      AND ".join(where_clauses)
                                wiql = f"""
                                    SELECT [System.Id], [System.Title], [System.State], [System.WorkItemType],
                                           [System.AssignedTo], [System.AreaPath], [System.IterationPath]
                                    FROM WorkItems
                                    WHERE {where_sql}
                                    ORDER BY [System.ChangedDate] DESC
                                    """
                                
                                if not iteration_path:
                                    logger.warning(f"Iteration path not available for {iteration_id}, querying area path only")
                               
                                logger.info(f"Using WIQL with filters: area={area_path_resolved}, iteration={iteration_path}, type={work_item_type}, state={state_filter}")
                               
                                # Execute WIQL directly via canonical execute_wiql skill
                                from agents.pm_agent.pm_skills.wiql_skill import execute_wiql as _exec_wiql
                                exec_result = await _exec_wiql(project=project, wiql=wiql)
                               
                                # Convert WIQL result to expected format
                                if exec_result.get('success', False):
                                    wiql_items = exec_result.get('items', [])
                                    result_count = len(wiql_items)
                                    logger.info(f"WIQL returned {result_count} items filtered by area path '{area_path_resolved}'")
                                    mcp_result = json.dumps(exec_result, indent=2)
                                else:
                                    mcp_result = f"Failed: {exec_result.get('error', 'Unknown error')}"
                                   
                            except Exception as e:
                                logger.error(f"WIQL fallback for area path failed: {e}", exc_info=True)
                                mcp_result = f"Error executing area path filter: {e}"
                        else:
                            # No area path or resolution failed - execute normal iteration tool
                            logger.info(f"Executing {tool} with args: {args}")
                            updated_plan = {'action': 'call_tool', 'tool': tool, 'args': args, 'confidence': plan.get('confidence', 0.9)}
                            exec_result = await self.tool_executor.execute(updated_plan)
                           
                            if exec_result.get('success', False):
                                result_count = exec_result.get('count', 0)
                               
                                # Apply query-aware filtering for sprint/iteration queries
                                if QUERY_AWARE_FILTER_AVAILABLE and should_apply_query_filtering(q, tool):
                                    try:
                                        items = exec_result.get('items', exec_result.get('work_items', []))
                                        if items and isinstance(items, list):
                                            original_count = len(items)
                                            intent = analyze_query_intent(q)
                                            filtered_items = filter_work_items_by_intent(items, intent)
                                           
                                            # Only use filtering if it actually filters something meaningful
                                            if intent.has_active_filters():
                                                logger.info(f"Query-aware filter: {original_count} -> {len(filtered_items)} items for query: {q[:50]}...")
                                                formatted_result = format_filtered_results(filtered_items, intent, original_count)
                                                mcp_result = formatted_result
                                            else:
                                                # No specific filters detected, return all items
                                                mcp_result = json.dumps(exec_result, indent=2)
                                        else:
                                            mcp_result = json.dumps(exec_result, indent=2)
                                    except Exception as filter_error:
                                        logger.warning(f"Query-aware filtering failed: {filter_error}, using unfiltered result")
                                        mcp_result = json.dumps(exec_result, indent=2)
                                else:
                                    mcp_result = json.dumps(exec_result, indent=2)
                            else:
                                mcp_result = f"Failed: {exec_result.get('error', 'Unknown error')}"
                    except Exception as e:
                        mcp_result = f"Error executing {tool}: {e}"
                        logger.exception("Error executing %s: %s", tool, e)
                else:
                    # ═══════════════════════════════════════════════════════════
                    # GENERIC PM SKILL DISPATCH — Dynamic handler for any skill
                    # registered in SKILL_DEFINITIONS that doesn't have an
                    # explicit branch above. This covers: billing_deviation,
                    # detect_recurring_bugs, backlog_triaging, sprint_plan,
                    # developer_skills, capacity_triaging, etc.
                    # Falls through to MCP dispatch if tool is not a PM skill.
                    # ═══════════════════════════════════════════════════════════
                    _handled_as_pm_skill = False
                    try:
                        from agents.pm_skill_agent.skills import SKILL_DEFINITIONS
                        skill_def = SKILL_DEFINITIONS.get(tool)
                        if skill_def and skill_def.handler:
                            if args is None:
                                args = {}
                            params = dict(args)
                            if 'project' not in params:
                                params['project'] = merged_context.get('project')
                            if 'team' not in params and merged_context.get('team'):
                                params['team'] = merged_context.get('team')
                            if 'query' not in params:
                                params['query'] = q
                            
                            logger.info(f"[GENERIC_PM_SKILL] Executing skill '{tool}' with params: {params}")
                            exec_result = await skill_def.handler(params)
                            _handled_as_pm_skill = True
                            
                            if exec_result.success:
                                result_data = exec_result.result
                                if isinstance(result_data, dict):
                                    result_count = result_data.get('count', result_data.get('total_items', 0))
                                mcp_result = json.dumps({
                                    'success': True,
                                    'result': result_data,
                                    'metadata': exec_result.metadata
                                }, indent=2)
                                logger.info(f"[GENERIC_PM_SKILL] '{tool}' succeeded: count={result_count}")
                            else:
                                mcp_result = f"{tool} failed: {exec_result.error}"
                                logger.warning(f"[GENERIC_PM_SKILL] '{tool}' failed: {exec_result.error}")
                    except ImportError:
                        pass  # SKILL_DEFINITIONS not available - fall through to MCP
                    except Exception as e:
                        mcp_result = f"Error running {tool}: {e}"
                        logger.exception(f"[GENERIC_PM_SKILL] Error running '{tool}': %s", e)
                        _handled_as_pm_skill = True  # Don't fall through on error
                    
                    if not _handled_as_pm_skill:
                        # ═══════════════════════════════════════════════════════════
                        # GENERIC MCP TOOL DISPATCH — For tools not matching any
                        # explicit branch or PM skill definition.
                        # ═══════════════════════════════════════════════════════════

                        # RESOLVE @CurrentIteration for ANY tool with iterationId
                        if args and args.get('iterationId') in ('@CurrentIteration', '@PreviousIteration'):
                            try:
                                _target_macro = args['iterationId']
                                _project = args.get('project') or merged_context.get('project', 'FracPro-OPS')
                                _team = args.get('team') or merged_context.get('team') or _project
                                logger.info(f"[ITER-RESOLVE] Resolving {_target_macro} for tool={tool}, project={_project}, team={_team}")

                                _tf = 'current' if 'Current' in _target_macro else None
                                _iter_resp = await self.mcp_connector.call_tool(
                                    'work_list_team_iterations',
                                    {'project': _project, 'team': _team, **(({'timeframe': _tf} if _tf else {}))}
                                )
                                if _iter_resp and _iter_resp not in ('null', 'No iterations found'):
                                    _iter_data = json.loads(_iter_resp) if isinstance(_iter_resp, str) else _iter_resp
                                    if isinstance(_iter_data, list) and _iter_data:
                                        _chosen = _iter_data[0]
                                        _iter_id = _chosen.get('id', '')
                                        _iter_path = _chosen.get('path', '')
                                        args['iterationId'] = _iter_id
                                        if 'iterationPath' not in args:
                                            args['iterationPath'] = _iter_path
                                        # Update plan args too so ToolExecutor sees resolved value
                                        if isinstance(plan, dict) and isinstance(plan.get('args'), dict):
                                            plan['args']['iterationId'] = _iter_id
                                            if 'iterationPath' not in plan['args']:
                                                plan['args']['iterationPath'] = _iter_path
                                        logger.info(f"[ITER-RESOLVE] Resolved {_target_macro} → {_iter_id} (path={_iter_path})")
                                    else:
                                        logger.warning(f"[ITER-RESOLVE] No iterations returned for {_team}")
                                else:
                                    logger.warning(f"[ITER-RESOLVE] Could not resolve {_target_macro} - API returned: {_iter_resp}")
                            except Exception as _iter_err:
                                logger.error(f"[ITER-RESOLVE] Error resolving {_target_macro}: {_iter_err}")

                        logger.info(f"Executing via ToolExecutor: {tool} with args: {args}")
                        try:
                            exec_result = await self.tool_executor.execute(plan)
                            if exec_result.get('success', False):
                                result_count = exec_result.get('count', 0)
                                mcp_result = json.dumps(exec_result, indent=2)
                                logger.debug(f"Tool execution successful: count={result_count}")
                            else:
                                _err_msg = exec_result.get('error', 'Unknown error')
                                mcp_result = f"Tool execution failed: {_err_msg}"
                                logger.debug(f"Tool execution failed: {_err_msg}")

                                # ═══════════════════════════════════════════════════════
                                # FALLBACK: If capacity/workload tool fails, retry with
                                # wit_get_work_items_for_iteration to at least show items
                                # grouped by assignee (workload distribution alternative)
                                # ═══════════════════════════════════════════════════════
                                if tool in ('work_get_team_capacity', 'work_get_iteration_capacities') and args.get('iterationId'):
                                    logger.info(f"[CAPACITY-FALLBACK] {tool} failed, falling back to wit_get_work_items_for_iteration for workload data")
                                    try:
                                        fallback_plan = {
                                            'action': 'call_tool',
                                            'tool': 'wit_get_work_items_for_iteration',
                                            'args': {
                                                'project': args.get('project', 'FracPro-OPS'),
                                                'team': args.get('team', ''),
                                                'iterationId': args['iterationId']
                                            },
                                            'confidence': plan.get('confidence', 0.9)
                                        }
                                        fb_result = await self.tool_executor.execute(fallback_plan)
                                        if fb_result.get('success', False):
                                            result_count = fb_result.get('count', 0)
                                            # Add context for synthesizer
                                            fb_result['_fallback_note'] = (
                                                "Capacity API was unavailable. Showing workload distribution "
                                                "based on work item assignments in the current sprint."
                                            )
                                            mcp_result = json.dumps(fb_result, indent=2)
                                            logger.info(f"[CAPACITY-FALLBACK] Got {result_count} items as fallback workload data")
                                    except Exception as fb_err:
                                        logger.error(f"[CAPACITY-FALLBACK] Fallback also failed: {fb_err}")

                        except Exception as e:
                            mcp_result = f"MCP error: {e}"
                            logger.debug(f"MCP exception: {e}")
                
                # ═══════════════════════════════════════════════════════════
                # SEARCH FALLBACK: When iteration-based search + multi-team
                # retry both return 0 for a person query, fall back to
                # search_workitem with "a:<name>" prefix which searches
                # across ALL iterations, areas, and teams.
                # ═══════════════════════════════════════════════════════════
                if result_count == 0 and args and args.get('assignedTo'):
                    person_filter = args.get('assignedTo')
                    person_name_for_search = person_filter
                    if isinstance(person_filter, list):
                        person_name_for_search = person_filter[0] if person_filter else None
                    
                    if person_name_for_search:
                        logger.info(f"[SEARCH-FALLBACK] Iteration-based search returned 0 for '{person_name_for_search}', trying search_workitem with a: prefix")
                        try:
                            project = args.get('project', merged_context.get('project', 'FracPro-OPS'))
                            fallback_args = {
                                'searchText': f'a:{person_name_for_search}',
                                'project': [project] if isinstance(project, str) else project,
                                'top': 200
                            }
                            # Forward state filter from original args so "active work items" doesn't return Closed ones
                            state_filter = args.get('state')
                            if state_filter:
                                if isinstance(state_filter, str):
                                    state_filter = [state_filter]
                                fallback_args['state'] = state_filter
                                logger.info(f"[SEARCH-FALLBACK] Including state filter: {state_filter}")
                            # Forward workItemType filter if present
                            type_filter = args.get('workItemType')
                            if type_filter:
                                if isinstance(type_filter, str):
                                    type_filter = [type_filter]
                                fallback_args['workItemType'] = type_filter
                                logger.info(f"[SEARCH-FALLBACK] Including type filter: {type_filter}")
                            fallback_plan = {
                                'action': 'call_tool',
                                'tool': 'search_workitem',
                                'args': fallback_args,
                                'confidence': plan.get('confidence', 0.9)
                            }
                            
                            fallback_result = await self.tool_executor.execute(fallback_plan)
                            
                            if fallback_result.get('success', False):
                                fallback_count = fallback_result.get('count', 0)
                                if fallback_count > 0:
                                    logger.info(f"[SEARCH-FALLBACK] ✅ Found {fallback_count} items via search_workitem for '{person_name_for_search}'!")
                                    result_count = fallback_count
                                    exec_result = fallback_result
                                    mcp_result = json.dumps(exec_result, indent=2)
                                    # Update the plan so synthesizer knows the tool used
                                    plan['tool'] = 'search_workitem'
                                    plan['args'] = fallback_plan['args']
                                else:
                                    logger.info(f"[SEARCH-FALLBACK] search_workitem also returned 0 items for '{person_name_for_search}'")
                        except Exception as search_err:
                            logger.warning(f"[SEARCH-FALLBACK] Error in search fallback: {search_err}")
            else:
                # FIX #5: ENFORCE NO_TOOL DECISIONS - Do NOT silently fallback to random tools
                if action == "no_tool":
                    logger.warning(f"[PM_AGENT] Orchestrator explicitly said no_tool is needed - enforcing decision")
                    _finalize_langfuse_trace(trace, {"status": "cannot_answer", "reason": "no_tool_available", "action": "no_tool"})
                    yield _make_agent_response(
                        content="I don't have the capability to answer this query. The question requires information or actions not available through my current tools.",
                        status="FAILED",
                        error="no_tool_available"
                    )
                    return
                else:
                    logger.warning("No tool to execute - this should not happen after orchestrator planning")
                    # FIX #5: Return explicit error instead of silently calling random tool
                    _finalize_langfuse_trace(trace, {"status": "failed", "reason": "no_tool_in_plan"})
                    yield _make_agent_response(
                        content="I couldn't determine the right approach to answer this query. Please rephrase or provide more context.",
                        status="FAILED",
                        error="no_tool_determined"
                    )
                    return
           
            # ═══════════════════════════════════════════════════════════
            # STEP 7: POST-TOOL DECISION (Unified Execution Validation)
            # ═══════════════════════════════════════════════════════════
            logger.info("=== STEP 7: Post-Tool Decision (Unified Execution Validation) ===")
           
            # B2: RESULT VALIDATION via ValidationOrchestrator (Stage C)
            validation_result = None  # initialise so it's always in scope at Step 8
            if mcp_result and tool:
                try:
                    _vo = get_validation_orchestrator(
                        tools_cache=self.mcp_connector.tools_cache,
                        mcp_connector=self.mcp_connector,
                    )
                    validation_result = _vo.validate_execution(
                        query=q, plan=plan, result=mcp_result, session_id=session_id,
                    )
                   
                    if not validation_result.is_valid:
                        logger.warning(f"[VALIDATION] Execution validation failed: {[e.message for e in validation_result.errors]}")
                        if METRICS_AVAILABLE:
                            get_metrics_collector().increment(MetricNames.VALIDATION_FAILURE, {"tool": tool})
                        # Use sanitized result if available, otherwise continue with original
                        if validation_result.sanitized_result:
                            mcp_result = validation_result.sanitized_result
                            logger.info("[VALIDATION] Using sanitized result")
                   
                    if validation_result.warnings:
                        logger.info(f"[VALIDATION] Warnings: {[w.message for w in validation_result.warnings]}")
                    
                    if validation_result.info:
                        for info_issue in validation_result.info:
                            logger.info(f"[VALIDATION] Info: {info_issue.message}")
                       
                except Exception as ve:
                    logger.warning(f"[VALIDATION] Validation error (non-fatal): {ve}")
                    validation_result = None
           
            # SAFETY NET: Detect if mcp_result contains planner JSON instead of actual data
            # Primary detection is now handled by ExecutionResultValidator (PLANNER_JSON_LEAK code)
            # This secondary check ensures flow control (use_llm_summary) is correct
            if mcp_result:
                try:
                    parsed_result = json.loads(mcp_result) if isinstance(mcp_result, str) else mcp_result
                    if isinstance(parsed_result, dict) and ("action" in parsed_result or ("tool" in parsed_result and "args" in parsed_result)):
                        logger.error("[CRITICAL] mcp_result contains planner JSON instead of execution result!")
                        logger.error(f"[CRITICAL] Plan JSON: {json.dumps(parsed_result, indent=2)[:500]}")
                        mcp_result = None  # Force fallback to error message
                        summary = "⚠️ The system generated an internal plan but failed to execute it. This is likely a bug. Please try rephrasing your query or contact support."
                        use_llm_summary = False
                    else:
                        use_llm_summary = True
                except (json.JSONDecodeError, TypeError):
                    use_llm_summary = True
            else:
                use_llm_summary = False
           
            # Decide: use LLM summarization or fixed format?
            # For now, always use LLM summarization (can add fixed formats later)
            if use_llm_summary and mcp_result:
                logger.info("Using LLM summarization")
               
                # ═══════════════════════════════════════════════════════════
                # TEAM DATA ENRICHMENT - Add dynamic assignments & EOD estimates
                # ═══════════════════════════════════════════════════════════
                team_enrichment = None
                if mcp_result:
                    try:
                        parsed_for_enrich = json.loads(mcp_result) if isinstance(mcp_result, str) else mcp_result
                        # Check if result contains work items (from search_workitem or WIQL)
                        work_items = []
                        if isinstance(parsed_for_enrich, dict):
                            work_items = parsed_for_enrich.get('items') or parsed_for_enrich.get('results') or parsed_for_enrich.get('workItems', [])
                        elif isinstance(parsed_for_enrich, list):
                            work_items = parsed_for_enrich
                       
                        if work_items and len(work_items) > 0:
                            logger.info(f"[TeamEnrichment] Enriching {len(work_items)} work items with team data")
                            from .team_enrichment import enrich_work_items_with_team_data
                            team_enrichment = await enrich_work_items_with_team_data(
                                work_items=work_items,
                                mcp_connector=self.mcp_connector,
                                project=merged_context.get('project', 'FracPro-OPS'),
                                iteration_id=merged_context.get('iteration'),
                                team=merged_context.get('team')
                            )
                            if team_enrichment:
                                logger.info(f"[TeamEnrichment] Added: {len(team_enrichment.get('developers', []))} developers, {len(team_enrichment.get('suggested_assignments', []))} suggestions")
                    except Exception as e:
                        logger.warning(f"[TeamEnrichment] Enrichment failed (non-fatal): {e}")
               
                summary = await summarize_mcp_result(q, mcp_result, self.SYSTEM_PROMPT, session_id=session_id, plan=plan, team_enrichment=team_enrichment, sprint_context=merged_context)
                # Sanitize after LLM summary (in case LLM returns unexpected JSON)
                summary = self._sanitize_response(summary, "llm_summary")
                logger.info(f"Summary generated ({len(summary)} chars)")
            elif not mcp_result or mcp_result is None:
                summary = "No results found."
                summary = self._sanitize_response(summary, "direct_mcp_result")
            else:
                summary = mcp_result or "No results found."
                summary = self._sanitize_response(summary, "direct_mcp_result")
           
            # ═══════════════════════════════════════════════════════════
            # STEP 8: RESPONSE ASSEMBLY
            # ═══════════════════════════════════════════════════════════
            logger.info("=== STEP 8: Response Assembly ===")
           
            final_response = summary

            # ── Surface significant validation warnings to user ──
            _validation_disclaimers: list[str] = []

            # Plan-level warnings (from Step 5) — unified issues with codes
            _SURFACED_PLAN_CODES = {
                "CONSTRAINT_NOT_COVERED", "SCOPE_NOT_COVERED",
                "IDENTITY_NOT_COVERED", "TEMPORAL_NOT_COVERED",
            }
            try:
                if validation and validation.issues:
                    for pi in validation.issues:
                        if pi.code in _SURFACED_PLAN_CODES:
                            _validation_disclaimers.append(pi.message)
                # Also check legacy intent_alignment warnings
                if validation and validation.intent_alignment:
                    for pw in validation.intent_alignment.get("warnings", []):
                        pw_lower = pw.lower()
                        if any(tag in pw_lower for tag in (
                            "wiql scope gap", "wiql date gap", "wiql type gap",
                        )):
                            _validation_disclaimers.append(pw)
            except Exception:
                pass  # best-effort, never block response

            # Result-level warnings (from Step 7) — scope / date / sprint mismatch
            _SURFACED_CODES = {"SCOPE_MISMATCH", "SCOPE_LEAKAGE", "DATE_RANGE_MISMATCH", "TYPE_MISMATCH", "ITERATION_MISMATCH"}
            try:
                if validation_result and validation_result.warnings:
                    for rw in validation_result.warnings:
                        if rw.code in _SURFACED_CODES:
                            _validation_disclaimers.append(rw.message)
            except Exception:
                pass

            if _validation_disclaimers:
                disclaimer_block = "\n\n---\n⚠️ **Validation Notes:**\n"
                for _disc in _validation_disclaimers:
                    disclaimer_block += f"- {_disc}\n"
                final_response += disclaimer_block
                logger.info(f"[VALIDATION] Surfaced {len(_validation_disclaimers)} validation warning(s) to user")

            # CRITICAL: Sanitize response - never return raw planner JSON
            final_response = self._sanitize_response(final_response, "final_response")

            # If user asked to "send an email to ...", attempt to send the final response
            if email_recipients:
                ready, msg = is_email_ready()
                try:
                    subject = f"PM Agent: Results for your request"
                    html_body = f"<pre>{final_response}</pre>"
                    ok, send_msg = send_report_attachment(email_recipients, subject, html_body, attachments=None)
                    if ok:
                        logger.info(f"Email sent to {email_recipients}: {send_msg}")
                        final_response = final_response + f"\n\n(Email sent to {email_recipients})"
                    else:
                        logger.warning(f"Email failed to send to {email_recipients}: {send_msg}")
                        final_response = final_response + f"\n\n(Email failed: {send_msg})"
                except Exception as e:
                    logger.exception(f"Error sending email: {e}")
                    final_response = final_response + f"\n\n(Email send error: {e})"
           
            # ═══════════════════════════════════════════════════════════
            # STEP 9: MEMORY UPDATE
            # ═══════════════════════════════════════════════════════════
            logger.info("=== STEP 9: Memory Update ===")
           
            session_ctx.update_from_query(q, tool, args, result_count)
            session_ctx.add_response(final_response[:500])
            logger.debug(f"Session context updated: project={session_ctx.project}, last_tool={session_ctx.last_tool}")
           
            # End Langfuse trace with success
            _finalize_langfuse_trace(trace, {"result": final_response[:500], "tool": tool})
           
            # B1: Track successful completion
            if self.metrics:
                self.metrics.increment(MetricNames.AGENT_SUCCESS, {"agent": "pm_agent", "tool": tool or "none"})
                if invoke_start_time:
                    self.metrics.timer_stop("agent_invoke", invoke_start_time)
           
            yield _make_agent_response(content=final_response, status="SUCCESS")

        except Exception as e:
            logger.exception(f"PM Agent error: {e}")
            error_msg = f"PM Agent error: {e}"
           
            # ═══════════════════════════════════════════════════════════
            # SELF-CORRECTION: Analyze failure and determine recovery
            # ═══════════════════════════════════════════════════════════
            try:
                # Build tool plan for analysis (use locals if available)
                tool_plan = {"tool": tool if 'tool' in dir() else "unknown", "args": args if 'args' in dir() else {}}
                query_str = q if 'q' in dir() else str(query)
               
                analysis = analyze_tool_failure(
                    error=str(e),
                    tool_plan=tool_plan,
                    query=query_str
                )
               
                logger.info(f"[SELF_CORRECTION] Analysis: type={analysis.failure_type.value}, action={analysis.recovery_action.value}")
               
                if analysis.recovery_action == RecoveryAction.ESCALATE_TO_SKILL_AGENT:
                    # Escalate to PM Skills Agent
                    _finalize_langfuse_trace(trace, {"error": str(e), "escalation": "pm_skill_agent"}, "escalating", level="WARNING")
                    yield _make_agent_response(
                        content=None,
                        status="REQUEST_ESCALATION",
                        error=error_msg,
                        escalation_context=analysis.to_escalation_context()
                    )
                    return
               
                elif analysis.recovery_action in (RecoveryAction.REPLAN_DIFFERENT_TOOL, RecoveryAction.REPLAN_FIXED_ARGS):
                    # Request replan with context
                    _finalize_langfuse_trace(trace, {"error": str(e), "replan": True}, "replanning", level="WARNING")
                    yield _make_agent_response(
                        content=None,
                        status="REQUEST_REPLAN",
                        error=error_msg,
                        replan_context=analysis.to_replan_context()
                    )
                    return
                   
            except Exception as analysis_error:
                logger.warning(f"[SELF_CORRECTION] Analysis failed: {analysis_error}")
           
            # B1: Track failure
            if self.metrics:
                self.metrics.increment(MetricNames.AGENT_FAILURE, {"agent": "pm_agent", "error_type": type(e).__name__})
                if invoke_start_time:
                    self.metrics.timer_stop("agent_invoke", invoke_start_time)
           
            # End Langfuse trace with error (fallback)
            _finalize_langfuse_trace(trace, {"error": str(e)}, str(e), level="ERROR")
           
            yield _make_agent_response(content=error_msg, status="FAILED", error=str(e))

    def _sanitize_response(self, response: str, context: str = "") -> str:
        """
        Sanitize response to ensure no raw JSON plans or metadata leak to users.
       
        Args:
            response: Response text to sanitize
            context: Context for logging (e.g., "final_response", "summary")
           
        Returns:
            Sanitized response safe for display
        """
        # Accept strings, dicts, or lists. Return a user-friendly string.
        if not response:
            return "No response available."

        # If caller passed structured data, handle it here
        if isinstance(response, (dict, list)):
            parsed = response
            # Planner/tool dict suppression
            if isinstance(parsed, dict) and ("action" in parsed or "tool" in parsed):
                logger.warning(f"[SANITIZE] Detected planner/tool dict in {context}, suppressing")
                message = parsed.get("message", "") if isinstance(parsed, dict) else ""
                if message:
                    return message
                return "⚠️ The system planned an internal tool call which is not shown. Please rephrase or try a simpler query."

            # Routing metadata
            if isinstance(parsed, dict) and "_routing" in parsed:
                logger.warning(f"[SANITIZE] Detected routing metadata in {context}, extracting content")
                return parsed.get("content", "Response processing completed.")

            # Legitimate MCP result shape - shallow summary
            if isinstance(parsed, dict) and ("count" in parsed or "items" in parsed or "results" in parsed or "workItems" in parsed):
                try:
                    count = parsed.get("count") or len(parsed.get("items") or parsed.get("results") or parsed.get("workItems") or [])
                    return f"Returned {count} items from MCP. Use the UI to inspect details."
                except Exception:
                    return json.dumps(parsed, indent=2)[:2000]

            # If list, summarize length
            if isinstance(parsed, list):
                try:
                    l = len(parsed)
                    preview = []
                    for i, it in enumerate(parsed[:5]):
                        if isinstance(it, dict):
                            iid = it.get("id") or it.get("workItemId") or it.get("workItem", {}).get("id") if isinstance(it, dict) else None
                            title = it.get("title") or (it.get("fields") or {}).get("System.Title") if isinstance(it, dict) else None
                            preview.append(f"{iid or '?'}: {title or str(it)[:60]}")
                        else:
                            preview.append(str(it)[:60])
                    preview_s = "; ".join(preview)
                    return f"Found {l} items. Preview: {preview_s}"
                except Exception:
                    return str(parsed)[:2000]

            # Default: stringify dict
            try:
                return json.dumps(parsed, indent=2)[:2000]
            except Exception:
                return str(parsed)[:2000]

        # For string inputs, use existing JSON-detection logic
        if isinstance(response, str):
            response_stripped = response.strip()
            if response_stripped.startswith(("{", "[")):
                try:
                    parsed = json.loads(response_stripped)
                    if isinstance(parsed, dict):
                        # Check for planner JSON patterns: {action, tool, args, confidence}
                        if "action" in parsed or ("tool" in parsed and "args" in parsed) or ("tool" in parsed and "confidence" in parsed):
                            action = parsed.get("action", "")
                            if action == "ask_clarification":
                                logger.warning(f"[SANITIZE] Detected ask_clarification JSON in {context}")
                                return parsed.get("message", "Could you provide more details about your request?")
                            elif action == "call_tool" or action == "no_tool" or "tool" in parsed:
                                logger.error(f"[SANITIZE] CRITICAL: Raw planner JSON detected in {context}!")
                                logger.error(f"[SANITIZE] Plan JSON: {json.dumps(parsed, indent=2)[:500]}")
                                message = parsed.get("message", "")
                                if message:
                                    return message
                                else:
                                    return "⚠️ I encountered an issue processing your request. The query was understood but couldn't be completed. Please try rephrasing your question."
                        if "_routing" in parsed:
                            logger.warning(f"[SANITIZE] Detected routing metadata in {context}, extracting content")
                            return parsed.get("content", "Response processing completed.")
                        if "count" in parsed or "items" in parsed or "results" in parsed:
                            logger.debug(f"[SANITIZE] Found structured MCP result in {context}, keeping as-is")
                            return response
                except json.JSONDecodeError:
                    pass
            return response
        # Fallback
        return str(response)
    
    async def _resolve_identity_value(
        self, 
        person_value: str, 
        arg_name: str, 
        tool_name: str, 
        trace
    ) -> str:
        """
        Resolve a person identifier (name, username, email) to ADO identity email.
        
        This method:
        1. Skips resolution if value is already an email (contains '@')
        2. Calls ADO identity API (core_get_identity_ids) to resolve names
        3. Handles single matches, multiple matches (clarification), and no matches
        4. Logs all steps for Langfuse tracing
        
        Args:
            person_value: The person identifier to resolve (name, username, email)
            arg_name: The argument name (for logging, e.g., 'assignedTo')
            tool_name: The tool being called (for logging context)
            trace: Langfuse trace object for observability
            
        Returns:
            Resolved email string, or original value if already resolved.
            Returns None if resolution requires clarification/error (caller should return early).
        """
        person_value = str(person_value).strip()
        
        # Skip if already an email (contains '@')
        if '@' in person_value:
            logger.debug(f"[IDENTITY_RESOLUTION] Skipping '{person_value}' - already an email")
            return person_value
        
        # Skip empty values
        if not person_value:
            return person_value
        
        logger.info(f"[IDENTITY_RESOLUTION] Resolving {arg_name}='{person_value}' for tool={tool_name}")
        
        try:
            from utilities.identity_resolution import resolve_person_name
            
            resolution = await resolve_person_name(
                person_value, 
                self.mcp_connector,
                allow_clarification=True
            )
            
            # Log resolution details for Langfuse
            resolution_data = {
                "input_name": person_value,
                "arg_name": arg_name,
                "tool": tool_name,
                "match_count": resolution.match_count,
                "resolution_method": resolution.resolution_method,
                "confidence": resolution.confidence
            }
            
            if resolution.needs_clarification:
                # Multiple ambiguous matches - ask user for clarification
                logger.info(
                    f"[IDENTITY_RESOLUTION] Clarification needed: {resolution.match_count} matches "
                    f"for '{person_value}' (method={resolution.resolution_method})"
                )
                resolution_data["status"] = "clarification_needed"
                resolution_data["matches"] = [
                    {"displayName": m.get("displayName"), "email": m.get("uniqueName")}
                    for m in resolution.all_matches[:5]
                ]
                _finalize_langfuse_trace(trace, resolution_data, "identity_clarification")
                
                # Yield clarification message to user
                # Note: This is a generator function pattern - we need to handle this differently
                # Since we can't yield from a helper, we return None to signal early exit
                self._pending_identity_clarification = resolution.clarification_message
                return None
                
            elif resolution.error_message:
                # No matches found
                logger.warning(
                    f"[IDENTITY_RESOLUTION] No match for '{person_value}': {resolution.error_message}"
                )
                resolution_data["status"] = "not_found"
                resolution_data["error"] = resolution.error_message
                _finalize_langfuse_trace(trace, resolution_data, "identity_not_found", level="WARNING")
                
                self._pending_identity_error = resolution.error_message
                return None
                
            elif resolution.resolved:
                # Successfully resolved to a single identity
                resolved_email = resolution.identity_email
                logger.info(
                    f"[IDENTITY_RESOLUTION] Resolved '{person_value}' → '{resolved_email}' "
                    f"(confidence={resolution.confidence:.2f}, method={resolution.resolution_method})"
                )
                resolution_data["status"] = "resolved"
                resolution_data["resolved_email"] = resolved_email
                resolution_data["resolved_name"] = resolution.identity_display_name
                
                # Don't finalize trace here - let the tool execution do that
                return resolved_email
            else:
                # Unexpected state - use original value but log warning
                logger.warning(
                    f"[IDENTITY_RESOLUTION] Unexpected resolution state for '{person_value}' - "
                    f"using original value. resolved={resolution.resolved}, "
                    f"needs_clarification={resolution.needs_clarification}, "
                    f"error={resolution.error_message}"
                )
                return person_value
                
        except ImportError as ie:
            logger.warning(f"[IDENTITY_RESOLUTION] Module not available: {ie}")
            return person_value
        except Exception as e:
            logger.error(f"[IDENTITY_RESOLUTION] Failed for '{person_value}': {e}", exc_info=True)
            # On error, use original value to allow tool execution to proceed
            return person_value
   
    async def _execute_fixed_skill(self, skill_match, context: dict) -> str:
        """
        Execute a fixed skill without LLM planning.
       
        Args:
            skill_match: SkillMatch object with skill_name and extracted_params
            context: Merged project/session context
           
        Returns:
            Result string, or None if skill couldn't execute
        """
        from .skills import SkillRouter
        skill_name = skill_match.skill_name
        params = skill_match.extracted_params
        project = context.get('project', 'FracPro-OPS')
       
        logger.debug(f"Executing fixed skill: {skill_name} with params: {params}")
       
        # Map skill names to execution logic
        if skill_name == 'list_area_paths':
            project = params.get('project') or project
            return await self.list_area_paths(project)
       
        elif skill_name == 'list_projects':
            # Use core_list_projects tool
            plan = {
                'action': 'call_tool',
                'tool': 'core_list_projects',
                'args': {}
            }
            exec_result = await self.tool_executor.execute(plan)
            if exec_result.get('success'):
                return json.dumps(exec_result, indent=2)
            # Fallback: call directly via MCP connector
            try:
                result = await self.mcp_connector.call_tool("core_list_projects", {})
                return result
            except Exception as e:
                return f"Failed to list projects: {e}"
       
        elif skill_name == 'list_teams':
            plan = {
                'action': 'call_tool',
                'tool': 'core_list_project_teams',
                'args': {'project': project}
            }
            exec_result = await self.tool_executor.execute(plan)
            if exec_result.get('success'):
                return json.dumps(exec_result, indent=2)
            try:
                result = await self.mcp_connector.call_tool("core_list_project_teams", {"project": project})
                return result
            except Exception as e:
                return f"Failed to list teams: {e}"
       
        elif skill_name == 'list_iterations':
            plan = {
                'action': 'call_tool',
                'tool': 'work_list_iterations',
                'args': {'project': project}
            }
            exec_result = await self.tool_executor.execute(plan)
            if exec_result.get('success'):
                return json.dumps(exec_result, indent=2)
            try:
                result = await self.mcp_connector.call_tool("work_list_iterations", {"project": project})
                return result
            except Exception as e:
                return f"Failed to list iterations: {e}"
       
        elif skill_name == 'current_iteration':
            # Use list_iterations tool and filter to current
            plan = {
                'action': 'call_tool',
                'tool': 'work_list_team_iterations',
                'args': {'project': project, 'timeframe': 'current'}
            }
            exec_result = await self.tool_executor.execute(plan)
            if exec_result.get('success'):
                return json.dumps(exec_result, indent=2)
            return None
       
        elif skill_name == 'list_repositories':
            plan = {
                'action': 'call_tool',
                'tool': 'repo_list_repos_by_project',
                'args': {'project': project}
            }
            exec_result = await self.tool_executor.execute(plan)
            if exec_result.get('success'):
                return json.dumps(exec_result, indent=2)
            try:
                result = await self.mcp_connector.call_tool("repo_list_repos_by_project", {"project": project})
                return result
            except Exception as e:
                return f"Failed to list repositories: {e}"
       
        elif skill_name == 'list_builds':
            plan = {
                'action': 'call_tool',
                'tool': 'pipelines_get_builds',
                'args': {'project': project}
            }
            exec_result = await self.tool_executor.execute(plan)
            if exec_result.get('success'):
                return json.dumps(exec_result, indent=2)
            try:
                result = await self.mcp_connector.call_tool("pipelines_get_builds", {"project": project})
                return result
            except Exception as e:
                return f"Failed to list builds: {e}"
       
        elif skill_name == 'list_test_plans':
            plan = {
                'action': 'call_tool',
                'tool': 'testplan_list_test_plans',
                'args': {'project': project}
            }
            exec_result = await self.tool_executor.execute(plan)
            if exec_result.get('success'):
                return json.dumps(exec_result, indent=2)
            try:
                result = await self.mcp_connector.call_tool("testplan_list_test_plans", {"project": project})
                return result
            except Exception as e:
                return f"Failed to list test plans: {e}"
       
        elif skill_name == 'my_work_items':
            plan = {
                'action': 'call_tool',
                'tool': 'wit_my_work_items',
                'args': {'project': project, 'top': 50}
            }
            exec_result = await self.tool_executor.execute(plan)
            if exec_result.get('success'):
                return json.dumps(exec_result, indent=2)
            try:
                result = await self.mcp_connector.call_tool("wit_my_work_items", {"project": project})
                return result
            except Exception as e:
                return f"Failed to get my work items: {e}"
       
        elif skill_name == 'list_bugs':
            # Use t:Bug prefix (ADO search API syntax)
            plan = {
                'action': 'call_tool',
                'tool': 'search_workitem',
                'args': {
                    'searchText': 't:Bug',
                    'project': [project],
                    'top': 100
                }
            }
            exec_result = await self.tool_executor.execute(plan)
            if exec_result.get('success'):
                return json.dumps(exec_result, indent=2)
            try:
                result = await self.mcp_connector.call_tool("search_workitem", {
                    "searchText": "t:Bug",
                    "project": [project],
                    "top": 100
                })
                return result
            except Exception as e:
                return f"Failed to list bugs: {e}"
       
        elif skill_name == 'list_stories':
            # Use t:"User Story" prefix (ADO search API syntax)
            plan = {
                'action': 'call_tool',
                'tool': 'search_workitem',
                'args': {
                    'searchText': 't:"User Story"',
                    'project': [project],
                    'top': 100
                }
            }
            exec_result = await self.tool_executor.execute(plan)
            if exec_result.get('success'):
                return json.dumps(exec_result, indent=2)
            try:
                result = await self.mcp_connector.call_tool("search_workitem", {
                    "searchText": 't:"User Story"',
                    "project": [project],
                    "top": 100
                })
                return result
            except Exception as e:
                return f"Failed to list user stories: {e}"
       
        elif skill_name == 'blocked_items':
            plan = {
                'action': 'call_tool',
                'tool': 'search_workitem',
                'args': {
                    'searchText': 'state:Blocked OR t:Bug',
                    'project': [project],
                    'top': 50
                }
            }
            exec_result = await self.tool_executor.execute(plan)
            if exec_result.get('success'):
                return json.dumps(exec_result, indent=2)
            return None
       
        elif skill_name == 'search_developers_by_skill':
            # Developer knowledge base search using Milvus vector DB
            from .developer_kb_handler import search_developers_by_skill, format_developer_search_results
           
            # Extract technology/skill from params
            technology = params.get('technology')
            skill_query = params.get('skill_query')
            top_k = params.get('top_k')
           
            # Get parent trace for Langfuse
            parent_trace = context.get('parent_trace')
           
            result = await search_developers_by_skill(
                skill_query=skill_query,
                technology=technology,
                top_k=top_k,
                include_evidence=True,
                parent_trace=parent_trace
            )
           
            # Format the results for display
            formatted = format_developer_search_results(result)
            return formatted
       
        # No fixed skill matched
        return None

    async def list_area_paths(self, project: str = None):
        """Return a human-readable list of area paths for a project using the ADO REST API.

        This is a small fixed skill the PM agent exposes to answer "area paths" queries
        without invoking the LLM/MCP fallback.
        """
        from config import config
        ado_org = config.ado_org_url
        pat = config.ado_pat
        if not pat:
            return "ADO_PAT not configured in config"

        if not project:
            project = config.pm_default_project

        url = f"{ado_org}/{project}/_apis/wit/classificationnodes/areas?$depth=10&api-version=7.0"
        try:
            resp = requests.get(url, auth=('', pat), timeout=30)
            resp.raise_for_status()
        except Exception as e:
            return f"Failed to fetch area paths: {e}"

        data = resp.json()
        paths = []

        def visit(node, prefix=''):
            name = node.get('name')
            full = f"{prefix}{name}" if prefix else name
            paths.append(full)
            for child in node.get('children', []):
                visit(child, full + '\\')

        if 'value' in data:
            for node in data['value']:
                visit(node, '')
        elif 'children' in data:
            for node in data['children']:
                visit(node, '')
        else:
            return "No area paths found"

        # Format human-readable output (short list)
        lines = [f"- {p}" for p in paths]
        header = f"Area paths for project '{project}' ({len(paths)}):\n"
        return header + "\n".join(lines)

    # ═══════════════════════════════════════════════════════════════════════
    # DATA ADAPTER METHODS - For PM Skills Agent to use instead of direct ADO
    # ═══════════════════════════════════════════════════════════════════════
   
    async def run_wiql(self, wiql: str, project: str = None) -> List[int]:
        """
        Execute a WIQL query and return list of work item IDs.
       
        This is a data adapter method for PM Skills Agent.
       
        Args:
            wiql: WIQL query string
            project: Project name (defaults to ADO_PROJECT)
           
        Returns:
            List of work item IDs matching the query
        """
        from utilities.mcp.pat import get_pat
        from config import config
       
        org_url = config.ado_org_url
        pat = get_pat()
        project = project or config.ado_project
       
        if not org_url or not pat:
            logger.error("run_wiql: ADO_ORG_URL or PAT not configured")
            return []
       
        url = f"{org_url}/{project}/_apis/wit/wiql?api-version=7.0"
        headers = {"Content-Type": "application/json"}
       
        try:
            resp = requests.post(
                url,
                auth=("", pat),
                headers=headers,
                json={"query": wiql},
                timeout=60
            )
            resp.raise_for_status()
            data = resp.json()
           
            work_items = data.get("workItems", [])
            ids = [wi.get("id") for wi in work_items if wi.get("id")]
            logger.info(f"run_wiql: Found {len(ids)} work items")
            return ids
           
        except Exception as e:
            logger.exception(f"run_wiql error: {e}")
            return []
   
    async def fetch_workitems(self, ids: List[int], fields: List[str] = None) -> List[Dict[str, Any]]:
        """
        Fetch work item details by IDs.
       
        This is a data adapter method for PM Skills Agent.
       
        Args:
            ids: List of work item IDs
            fields: Optional list of fields to retrieve
           
        Returns:
            List of work item dicts with requested fields
        """
        from utilities.mcp.pat import get_pat
        from config import config
       
        if not ids:
            return []
       
        org_url = config.ado_org_url
        pat = get_pat()
       
        if not org_url or not pat:
            logger.error("fetch_workitems: ADO_ORG_URL or PAT not configured")
            return []
       
        # Default fields if not specified
        if not fields:
            fields = [
                "System.Id", "System.Title", "System.State",
                "System.WorkItemType", "System.AreaPath",
                "System.CreatedDate", "System.AssignedTo",
                "System.ChangedDate", "System.Description"
            ]
       
        work_items = []
       
        # Batch in groups of 200 (ADO limit)
        batch_size = 200
        for i in range(0, len(ids), batch_size):
            batch = ids[i:i + batch_size]
            ids_str = ",".join(str(x) for x in batch)
            fields_str = ",".join(fields)
           
            url = f"{org_url}/_apis/wit/workitems?ids={ids_str}&fields={fields_str}&api-version=7.0"
           
            try:
                resp = requests.get(url, auth=("", pat), timeout=60)
                resp.raise_for_status()
                data = resp.json()
               
                for wi in data.get("value", []):
                    work_items.append(wi)
                   
            except Exception as e:
                logger.exception(f"fetch_workitems batch error: {e}")
       
        logger.info(f"fetch_workitems: Retrieved {len(work_items)} work items")
        return work_items
   
    async def get_area_paths_list(self, project: str = None) -> List[str]:
        """
        Get area paths as a list of strings.
       
        This is a data adapter method for PM Skills Agent.
       
        Args:
            project: Project name
           
        Returns:
            List of area path strings
        """
        from utilities.mcp.pat import get_pat
        from config import config
       
        org_url = config.ado_org_url
        pat = get_pat()
        project = project or config.ado_project
       
        if not org_url or not pat:
            logger.error("get_area_paths_list: ADO_ORG_URL or PAT not configured")
            return []
       
        url = f"{org_url}/{project}/_apis/wit/classificationnodes/areas?$depth=10&api-version=7.0"
       
        try:
            resp = requests.get(url, auth=("", pat), timeout=30)
            resp.raise_for_status()
            data = resp.json()
           
            paths = []
           
            def visit(node, prefix=''):
                name = node.get('name')
                full = f"{prefix}{name}" if prefix else name
                paths.append(full)
                for child in node.get('children', []):
                    visit(child, full + '\\')
           
            if 'value' in data:
                for node in data['value']:
                    visit(node, '')
            elif 'children' in data:
                for node in data['children']:
                    visit(node, '')
           
            logger.info(f"get_area_paths_list: Found {len(paths)} area paths")
            return paths
           
        except Exception as e:
            logger.exception(f"get_area_paths_list error: {e}")
            return []
   
    async def search_workitems(
        self,
        search_text: str,
        project: str = None,
        work_item_types: List[str] = None,
        top: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Search for work items using ADO search API.
       
        This is a data adapter method for PM Skills Agent.
       
        Args:
            search_text: Search text
            project: Project name
            work_item_types: List of work item types to filter
            top: Maximum results
           
        Returns:
            List of work item dicts
        """
        try:
            from config import config
            result = await self.mcp_connector.call_tool("search_workitem", {
                "searchText": search_text,
                "project": [project or config.ado_project],
                "top": top,
                **({"workItemType": work_item_types} if work_item_types else {})
            })
           
            if result:
                # Parse if JSON string
                if isinstance(result, str):
                    try:
                        data = json.loads(result)
                        return data.get("value", []) if isinstance(data, dict) else []
                    except json.JSONDecodeError:
                        return []
                return result if isinstance(result, list) else []
               
        except Exception as e:
            logger.exception(f"search_workitems error: {e}")
            return []
       
        return []