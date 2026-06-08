"""CLI helper to fetch a work item report via the MCP connector and email it to the PM.

Usage: ./scripts/send_wi_report.py <workItemId> <pm_email>
"""
import sys
import asyncio
from pathlib import Path

# Add root to path for imports
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from utilities.mcp.mcp_ado_connector import get_mcp_connector
from utilities.emailer import send_wi_report
from utilities.langfuse_client import trace_task


@trace_task("work_item_report", metadata={"source": "pm_agent"})
async def run(work_item_id: str, pm_email: str):
    conn = await get_mcp_connector()
    resp = await conn.timelog_get_time_and_comments(str(work_item_id), includeTimeLogExtension=True)
    data = resp if isinstance(resp, dict) else {}
    send_wi_report(str(work_item_id), data, pm_email)


if __name__ == '__main__':
    if len(sys.argv) < 3:
        print("Usage: send_wi_report.py <workItemId> <pm_email>")
        sys.exit(1)
    wi = sys.argv[1]
    pm = sys.argv[2]
    asyncio.run(run(wi, pm))
