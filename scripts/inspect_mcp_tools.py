import asyncio
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from utilities.mcp.mcp_ado_connector import get_mcp_connector

async def main():
    conn = await get_mcp_connector()
    print(f"Loaded tools: {len(conn.tools_cache)}")
    names = sorted(conn.tools_cache.keys())
    for n in names:
        print(n)

if __name__ == '__main__':
    asyncio.run(main())
