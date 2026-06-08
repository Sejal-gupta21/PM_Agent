import logging
import os
import traceback
import sys
import json
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.utils import new_task, new_agent_text_message
from agents.pm_agent.agent import PMAgent
from a2a.server.tasks import TaskUpdater
from a2a.types import TaskState
import asyncio

logger = logging.getLogger(__name__)

# Import PM Skills Agent for LangGraph-based skill execution
_pm_skill_agent = None

async def get_pm_skill_agent():
    """Lazy-load PM Skills Agent for skill execution via LangGraph."""
    global _pm_skill_agent
    if _pm_skill_agent is None:
        from agents.pm_skill_agent.agent import PMSkillAgent
        _pm_skill_agent = PMSkillAgent(config={"use_langgraph": True})
        logger.info("PM Skills Agent initialized for skill routing (LangGraph enabled)")
    return _pm_skill_agent


def _format_skill_result(skill_name: str, result: dict) -> str:
    """Format skill result dict into human-readable text."""
    if isinstance(result, str):
        return result
    
    # Handle bug_areas_highlight result
    if skill_name == "bug_areas_highlight":
        count = result.get("count", 0)
        recurring = result.get("recurring_count", 0)
        areas = result.get("areas", [])
        preview = result.get("preview_path", "")
        
        lines = [f"## Bug Areas Highlight Report", ""]
        lines.append(f"**Total Bugs Analyzed:** {count}")
        lines.append(f"**Recurring Bug Areas:** {recurring}")
        
        if areas:
            lines.append("\n### Top Bug Areas:")
            for i, area in enumerate(areas[:10], 1):
                lines.append(f"{i}. {area}")
        
        if preview:
            lines.append(f"\n📄 **Full Report:** {preview}")
        
        return "\n".join(lines)
    
    # Handle list_area_paths result
    if skill_name == "list_area_paths":
        if isinstance(result, list):
            lines = ["## Area Paths", ""]
            for path in result:
                lines.append(f"- {path}")
            return "\n".join(lines)
        return str(result)
    
    # Default: pretty-print as JSON for unknown skills
    try:
        return json.dumps(result, indent=2, default=str)
    except:
        return str(result)


PM_DEBUG = os.getenv("PM_DEBUG", "0") in ("1", "true", "True")


def safe_print(msg):
    """Print with fallback for unicode characters that can't be encoded.

    Printing is gated by the `PM_DEBUG` environment variable to avoid
    verbose logs in production.
    """
    if not PM_DEBUG:
        return
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        # Encode with replacement for characters that can't be displayed
        safe_msg = msg.encode(sys.stdout.encoding or 'utf-8', errors='replace').decode(sys.stdout.encoding or 'utf-8', errors='replace')
        print(safe_msg, flush=True)


def _extract_query_text(query) -> str:
    """Extract plain text from an A2A task payload or plain string query.
    
    Handles nested A2A format:
      {'id': ..., 'params': {'message': {'parts': [{'type': 'text', 'text': '...'}]}}}
    Also handles simpler dict formats with 'parts', 'message', or 'text' keys.
    """
    if isinstance(query, str):
        return query
    if isinstance(query, dict):
        # A2A nested format: params -> message -> parts -> text
        try:
            parts = query.get('params', {}).get('message', {}).get('parts', [])
            for part in parts:
                if isinstance(part, dict) and part.get('type') == 'text' and part.get('text'):
                    return part['text']
        except (AttributeError, TypeError):
            pass
        # Direct parts format (e.g. message dict with 'parts')
        if 'parts' in query and isinstance(query.get('parts'), list):
            for part in query['parts']:
                if isinstance(part, dict) and part.get('type') == 'text' and part.get('text'):
                    return part['text']
        # Simple key formats
        if isinstance(query.get('message'), str):
            return query['message']
        if isinstance(query.get('text'), str):
            return query['text']
        return str(query)
    return str(query)


class PMAgentExecutor(AgentExecutor):
    def __init__(self):
        self.agent = PMAgent()

    async def create(self):
        await self.agent.create()

    async def execute(self, request_context: RequestContext, event_queue: EventQueue) -> str:
        safe_print(f"[EXECUTOR] execute() called")
        query = request_context.get_user_input()
        safe_print(f"[EXECUTOR] Raw query type: {type(query)}, value: {query}")
        
        task = request_context.current_task
        if not task:
            safe_print("[EXECUTOR] Creating new task")
            task = new_task(request_context.message)
            await event_queue.enqueue_event(task)

        updater = TaskUpdater(event_queue, task.id, task.context_id)

        try:
            safe_print("[EXECUTOR] Starting agent.invoke()")
            logger.info(f"Starting execution for query: {query}")
            # Support short-circuited fixed skills via an explicit payload
            # Route through PM Skills Agent (LangGraph) for observability
            if isinstance(query, dict) and query.get('skill'):
                skill = query.get('skill')
                logger.info(f"Routing skill '{skill}' through PM Skills Agent (LangGraph)")
                
                # Create trace for this skill execution (PM Agent server doesn't have controller/orchestrator traces)
                from utilities.langfuse_client import create_trace, set_current_trace, finalize_trace
                
                trace = create_trace(
                    name="pm_skill_agent_request",
                    input_data={"skill": skill, "query": str(query)[:500]},
                    metadata={"source": "pm_agent_executor", "skill": skill},
                    session_id=task.context_id
                )
                
                if trace:
                    set_current_trace(trace)
                    logger.info(f"Created trace for skill execution: {skill} (session_id={task.context_id})")
                
                try:
                    # Use PM Skills Agent for LangGraph-based execution with Langfuse tracing
                    pm_skill_agent = await get_pm_skill_agent()
                    
                    # Build request for PM Skills Agent
                    skill_request = {
                        "skill": skill,
                        "query": f"execute {skill}",  # Provide query for LangGraph matching
                        "params": {k: v for k, v in query.items() if k not in ('skill', 'query')},
                        "session_id": task.context_id
                    }
                    
                    # Execute via PM Skills Agent (uses LangGraph with Langfuse tracing)
                    async for result in pm_skill_agent.invoke(skill_request):
                        if result.get("is_task_complete"):
                            content = result.get("content", {})
                            # Handle both dict and string results
                            if isinstance(content, dict):
                                # Check for formatted_result first (human-readable)
                                if content.get("formatted_result"):
                                    final_result = content["formatted_result"]
                                elif content.get("message"):
                                    final_result = content["message"]
                                elif content.get("result"):
                                    # Format the result dict into human-readable text
                                    skill_result = content["result"]
                                    final_result = _format_skill_result(skill, skill_result)
                                else:
                                    final_result = str(content)
                            else:
                                final_result = str(content)
                            logger.info(f"Skill result via LangGraph: {str(final_result)[:200]}")
                            await updater.update_status(TaskState.completed, new_agent_text_message(final_result, task.context_id, task.id))
                            
                            # Finalize trace
                            if trace:
                                finalize_trace(trace, output=final_result[:500], status="success")
                            
                            return final_result
                finally:
                    # Ensure trace is finalized even on error
                    if trace:
                        finalize_trace(trace, status="error")
            
            # Create trace for non-skill PM Agent execution (when called via HTTP without controller)
            safe_print("[EXECUTOR] Calling agent.invoke() now")
            from utilities.langfuse_client import create_trace, set_current_trace, finalize_trace
            
            trace = create_trace(
                name="pm_agent_request",
                input_data={"query": str(query)[:500]},
                metadata={"source": "pm_agent_executor_http"},
                session_id=task.context_id
            )
            
            if trace:
                set_current_trace(trace)
                logger.info(f"Created trace for PM Agent execution (session_id={task.context_id})")
            
            try:
                async for item in self.agent.invoke(query, task.context_id, parent_trace=trace):
                    safe_print(f"[EXECUTOR] Got item: {item}")
                    is_task_complete = item.get("is_task_complete", False)
                    if not is_task_complete:
                        message = item.get("updates", "PMAgent working...")
                        await updater.update_status(TaskState.working, new_agent_text_message(message, task.context_id, task.id))
                    else:
                        # FIX #9: Handle NEEDS_DEEP_ANALYSIS status
                        # When agent returns this status, we need to call Deep LLM and re-invoke
                        status = item.get("status", "SUCCESS")
                        if status == "NEEDS_DEEP_ANALYSIS":
                            logger.info("[EXECUTOR] Agent returned NEEDS_DEEP_ANALYSIS, calling Deep LLM planner")
                            safe_print("[EXECUTOR] NEEDS_DEEP_ANALYSIS detected, calling Deep LLM")
                            
                            # Get deep analysis context
                            deep_context = item.get("deep_analysis_context", {})
                            merged_context = deep_context.get("context", {})
                            
                            # Call Deep LLM to get a plan
                            from utilities.llm import plan_mcp_call
                            from utilities.mcp.tool_registry import MCP_TOOL_REGISTRY
                            
                            # Extract plain text from A2A query payload
                            query_text = _extract_query_text(query)
                            logger.info(f"[EXECUTOR] Extracted query text for Deep LLM: {query_text[:200]}")
                            
                            try:
                                plan_result = await plan_mcp_call(
                                    query=query_text,
                                    context=merged_context,
                                    tool_registry=MCP_TOOL_REGISTRY,
                                    session_id=task.context_id
                                )
                                
                                if not plan_result:
                                    logger.warning("[EXECUTOR] Deep LLM returned no plan, returning original response")
                                    final_result = item.get("content", "Unable to plan for this query.")
                                else:
                                    # ═══════════════════════════════════════════════════
                                    # SEMANTIC OVERRIDE: If LLM returned ask_clarification
                                    # but semantic routing matched a PM skill with high
                                    # confidence, use the skill match instead
                                    # ═══════════════════════════════════════════════════
                                    if plan_result.get('action') == 'ask_clarification':
                                        try:
                                            from utilities.skill_registry import match_skill_by_query
                                            semantic_match = match_skill_by_query(query_text, threshold=0.5)
                                            if semantic_match:
                                                skill_id = semantic_match.get("id")
                                                # match_skill_by_query already applies threshold filtering
                                                # If it returns a result, the match is confident enough
                                                if skill_id:
                                                    logger.info(f"[EXECUTOR] Semantic override: LLM chose ask_clarification but semantic match found skill '{skill_id}'")
                                                    plan_result = {
                                                        "action": "call_tool",
                                                        "tool": skill_id,
                                                        "args": {"query": query_text, "project": merged_context.get("project")},
                                                        "confidence": 0.75,
                                                        "reasoning_summary": f"Semantic routing matched skill '{skill_id}' - overriding LLM ask_clarification"
                                                    }
                                        except Exception as e:
                                            logger.warning(f"[EXECUTOR] Semantic override check failed: {e}")

                                    # ═══════════════════════════════════════════════════
                                    # SEMANTIC OVERRIDE #2: If LLM chose search_workitem
                                    # but semantic routing matches a dedicated PM skill
                                    # with HIGH confidence, prefer the PM skill.
                                    # PM skills have built-in logic for analysis/reporting
                                    # that search_workitem cannot replicate.
                                    # ═══════════════════════════════════════════════════
                                    if (plan_result.get('action') == 'call_tool' and
                                            plan_result.get('tool') == 'search_workitem'):
                                        try:
                                            from utilities.skill_registry import match_skill_by_query
                                            logger.debug(f"[EXECUTOR] Semantic override #2: checking for PM skill match for '{query_text[:80]}'")
                                            semantic_match = match_skill_by_query(query_text, threshold=0.45)
                                            logger.debug(f"[EXECUTOR] Semantic override #2: match_result={semantic_match}")
                                            if semantic_match:
                                                skill_id = semantic_match.get("id")
                                                if skill_id:
                                                    logger.info(
                                                        f"[EXECUTOR] Semantic override #2: LLM chose search_workitem "
                                                        f"but semantic match found skill '{skill_id}' - overriding"
                                                    )
                                                    plan_result = {
                                                        "action": "call_tool",
                                                        "tool": skill_id,
                                                        "args": {"query": query_text, "project": merged_context.get("project")},
                                                        "confidence": 0.80,
                                                        "reasoning_summary": f"Semantic routing matched skill '{skill_id}' - overriding search_workitem"
                                                    }
                                        except Exception as e:
                                            logger.warning(f"[EXECUTOR] Semantic override #2 check failed: {e}")
                                    
                                    logger.info(f"[EXECUTOR] Deep LLM plan: action={plan_result.get('action')}, tool={plan_result.get('tool')}")
                                    safe_print(f"[EXECUTOR] Deep LLM plan received, re-invoking agent")
                                    
                                    # Re-invoke agent with the plan
                                    async for replan_item in self.agent.invoke(
                                        query, 
                                        task.context_id, 
                                        parent_trace=trace,
                                        orchestrator_plan=plan_result
                                    ):
                                        safe_print(f"[EXECUTOR] Re-invoke got item: {replan_item}")
                                        if replan_item.get("is_task_complete"):
                                            final_result = replan_item.get("content", "no result is received.")
                                            break
                                        else:
                                            message = replan_item.get("updates", "PMAgent working with plan...")
                                            await updater.update_status(TaskState.working, new_agent_text_message(message, task.context_id, task.id))
                                    else:
                                        final_result = "Agent did not complete after Deep LLM planning."
                            except Exception as plan_error:
                                logger.exception(f"[EXECUTOR] Deep LLM planning failed: {plan_error}")
                                final_result = f"Deep LLM planning failed: {plan_error}"
                        else:
                            final_result = item.get("content", "no result is received.")
                        
                        logger.info(f"Final result: {final_result[:200] if final_result else 'None'}")
                        await updater.update_status(TaskState.completed, new_agent_text_message(final_result, task.context_id, task.id))
                        
                        # Finalize trace
                        if trace:
                            finalize_trace(trace, output=final_result[:500] if final_result else "", status="success")
                        
                        return final_result
                    await asyncio.sleep(0.1)
            finally:
                # Ensure trace is finalized even on error
                if trace:
                    finalize_trace(trace, status="error")
        except Exception as e:
            safe_print(f"[EXECUTOR] Exception caught: {type(e).__name__}: {e}")
            safe_print(f"[EXECUTOR] Traceback:\n{traceback.format_exc()}")
            logger.exception(f"Error during PMAgent execution: {e}")
            error_message = f"Error during PMAgent execution: {e}"
            await updater.update_status(TaskState.failed, new_agent_text_message(error_message, task.context_id, task.id))
            raise

    async def cancel(self):
        pass
