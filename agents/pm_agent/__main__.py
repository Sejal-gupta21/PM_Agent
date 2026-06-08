from a2a.types import AgentSkill, AgentCard, AgentCapabilities

from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore
from agents.pm_agent.agent_executor import PMAgentExecutor
from a2a.server.apps import A2AStarletteApplication
import uvicorn
import asyncio
import asyncclick as click
from config import config
import sys
import os

# Setup centralized logging before anything else
from utilities.logging_config import setup_logging, get_logger
setup_logging(level=config.log_level)
logger = get_logger(__name__)


@click.command()
@click.option('--host', default=config.DEFAULT_HOST, help='Host for the agent server.')
@click.option('--port', default=int(config.PM_AGENT_PORT), help='Port for the PM agent server.')
async def main(host: str, port: int):
    logger.info("=" * 60)
    logger.info("PM Agent starting up...")
    logger.info("=" * 60)
    
    skill = AgentSkill(
        name="PMAgent",
        id="pm_agent",
        description="PM-focused agent for Azure DevOps and MCP integrations",
        tags=["pm agent", "ADO", "MCP"],
        examples=["Use MCP tools to answer ADO queries"],
    )

    card = AgentCard(
        name="PMAgent",
        description="PM-focused agent for Azure DevOps and MCP integrations",
        skills=[skill],
        capabilities=AgentCapabilities(streaming=True, multi_turn=True),
        url=f"http://{host}:{port}/",
        version="1.0",
        default_input_modes=["text"],
        default_output_modes=["text"],
    )

    logger.info("Creating PM Agent executor...")
    executor = PMAgentExecutor()
    await executor.create()
    logger.info("PM Agent executor created successfully")

    request_handler = DefaultRequestHandler(agent_executor=executor, task_store=InMemoryTaskStore())

    server = A2AStarletteApplication(agent_card=card, http_handler=request_handler)
    # Set timeout to 180 seconds (3 minutes) to allow for slow ADO API calls
    config_uv = uvicorn.Config(
        server.build(), 
        host=host, 
        port=port, 
        log_level="info",
        timeout_keep_alive=180,
        timeout_graceful_shutdown=10
    )
    server_instance = uvicorn.Server(config_uv)
    logger.info(f"Starting Uvicorn on http://{host}:{port} (timeout: 180s)")
    await server_instance.serve()


if __name__ == "__main__":
    import sys
    if sys.platform == 'win32':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
