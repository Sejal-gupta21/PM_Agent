import asyncio
import json
import sys
from pathlib import Path

# Ensure repo root is on sys.path so imports work when run as a script
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from orchestrator.router import get_orchestrator


async def run_smoke():
    orchestrator = get_orchestrator()
    queries = [
        "Show recurring bugs for the last 30 days for client AcmeCorp",
        "List my open bugs",
        "Analyze bugs and send report to the team",
    ]

    for q in queries:
        print("=== QUERY ===\n", q)
        try:
            res = await orchestrator.process(q, session_id="smoke")
            print(json.dumps(res, indent=2)[:4000])
        except Exception as e:
            print(f"Error executing query '{q}': {e}")


if __name__ == '__main__':
    asyncio.run(run_smoke())
