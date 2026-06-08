import logging
import traceback
import sys
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.utils import new_task, new_agent_text_message
from agents.pm_skill_agent.agent import PMSkillAgent
from a2a.server.tasks import TaskUpdater
from a2a.types import TaskState
import asyncio

logger = logging.getLogger(__name__)


def safe_print(msg):
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        safe_msg = msg.encode(sys.stdout.encoding or 'utf-8', errors='replace').decode(sys.stdout.encoding or 'utf-8', errors='replace')
        print(safe_msg, flush=True)


class PMSkillAgentExecutor(AgentExecutor):
    def __init__(self):
        self.agent = PMSkillAgent()

    async def create(self):
        # PMSkillAgent has no heavy async init, but keep parity
        return

    async def execute(self, request_context: RequestContext, event_queue: EventQueue) -> str:
        safe_print(f"[SKILL EXECUTOR] execute() called")
        query = request_context.get_user_input()
        safe_print(f"[SKILL EXECUTOR] Raw query type: {type(query)}, value: {query}")

        task = request_context.current_task
        if not task:
            safe_print("[SKILL EXECUTOR] Creating new task")
            task = new_task(request_context.message)
            await event_queue.enqueue_event(task)

        updater = TaskUpdater(event_queue, task.id, task.context_id)

        try:
            # If a specific skill is requested, run it directly
            if isinstance(query, dict) and query.get('skill'):
                skill = query.get('skill')
                params = query.get('params', {})
                safe_print(f"[SKILL EXECUTOR] Executing skill {skill} with params {params}")
                result = await self.agent.execute_skill(skill, params)
                final_result = result.result if hasattr(result, 'result') else result
                await updater.update_status(TaskState.completed, new_agent_text_message(final_result, task.context_id, task.id))
                return final_result

            # Otherwise, reject non-skill requests (PM Skills Agent expects structured calls)
            error_message = "PMSkillAgent only accepts structured skill invocations (dict with 'skill')."
            await updater.update_status(TaskState.failed, new_agent_text_message(error_message, task.context_id, task.id))
            return error_message

        except Exception as e:
            safe_print(f"[SKILL EXECUTOR] Exception: {e}")
            safe_print(f"[SKILL EXECUTOR] Traceback:\n{traceback.format_exc()}")
            logger.exception(f"Error during PMSkillAgent execution: {e}")
            error_message = f"Error during PMSkillAgent execution: {e}"
            await updater.update_status(TaskState.failed, new_agent_text_message(error_message, task.context_id, task.id))
            raise

    async def cancel(self):
        pass
