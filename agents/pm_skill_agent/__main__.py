"""PM Skills Agent entry point for standalone execution."""

import asyncio
import logging
import os
import sys

# Ensure project root is on path
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents.pm_skill_agent.agent import PMSkillAgent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("pm_skill_agent.__main__")


async def main():
    """Run PM Skills Agent with test command."""
    agent = PMSkillAgent()
    
    # Health check
    health = await agent.health_check()
    logger.info(f"Agent health: {health}")
    
    # List available skills
    skills = agent.get_available_skills()
    logger.info(f"Available skills: {list(skills.keys())}")
    
    # Example: run bug areas highlight in preview mode
    if len(sys.argv) > 1:
        skill_name = sys.argv[1]
        params = {"preview_only": True}
        
        logger.info(f"Executing skill: {skill_name}")
        async for response in agent.invoke({"skill": skill_name, "params": params}):
            logger.info(f"Response: {response}")
    else:
        logger.info("Usage: python -m agents.pm_skill_agent <skill_name>")
        logger.info(f"Skills: {list(skills.keys())}")


if __name__ == "__main__":
    asyncio.run(main())
