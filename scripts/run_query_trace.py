import asyncio
import logging
import json
import sys
import os

# Ensure workspace root is on sys.path for local imports
sys.path.insert(0, os.getcwd())

from agents.pm_agent.agent import PMAgent
import agents.pm_agent.agent as agent_mod

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Monkeypatch summarize_mcp_result to avoid external LLM calls
async def _fake_summarize_mcp_result(query, raw_result, system_prompt):
    return f"Mock summary for query '{query}': extracted data -> {raw_result[:200]}"

agent_mod.summarize_mcp_result = _fake_summarize_mcp_result

async def fake_call_tool(tool_name, args):
    # Return a JSON-like string similar to MCP response
    return json.dumps({
        "id": args.get("id"),
        "fields": {"System.Title": "Mock Work Item", "System.State": "Active"}
    })

async def run():
    pm = PMAgent()
    # Patch the MCP connector to avoid external calls
    pm.mcp_connector.call_tool = fake_call_tool

    query = "get work item 58495"
    logger.info(f"Invoking PMAgent with: {query}")
    async for item in pm.invoke(query, session_id="test-session-1"):
        logger.info(f"Received: {item}")

if __name__ == "__main__":
    asyncio.run(run())
