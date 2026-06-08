import asyncio
from typing import Optional
from .types import AgentCard, DummyModel


class A2ACardResolver:
    """Resolve an agent card from a base URL.

    For local development this will attempt to fetch /.well-known/agent.json
    if an httpx client is provided, otherwise return a simple `AgentCard`.
    """

    def __init__(self, base_url: str, httpx_client: Optional[object] = None):
        self.base_url = base_url
        self.httpx_client = httpx_client

    async def get_agent_card(self) -> Optional[AgentCard]:
        # Try to fetch the well-known agent card if an async httpx client is available.
        try:
            if self.httpx_client is not None:
                url = self.base_url.rstrip("/") + "/.well-known/agent.json"
                r = await self.httpx_client.get(url)
                if r.status_code == 200:
                    data = r.json()
                    return AgentCard(name=data.get("name", "agent"), description=data.get("description", ""), base_url=self.base_url)
        except Exception:
            pass
        return AgentCard(name=self.base_url, description=f"Agent at {self.base_url}", base_url=self.base_url)


class A2AClient:
    """Minimal client shim for sending messages to remote agents.

    The implementation here intentionally does not attempt to implement
    the full upstream behaviour. For local development it either returns
    a simulated echo response or (in the future) could be extended to
    proxy to a real agent endpoint.
    """

    def __init__(self, httpx_client: Optional[object] = None, agent_card: Optional[AgentCard] = None):
        self.httpx_client = httpx_client
        self.agent_card = agent_card

    async def send_message(self, request):
        # Extract message text if possible
        try:
            msg = request.params.message
            parts = msg.parts
            first_part = parts[0].root.text if parts and parts[0] and parts[0].root else ""
            reply_text = f"(local) Echo: {first_part}"
        except Exception:
            reply_text = "(local) No reply"

        payload = {
            "result": {
                "status": {
                    "message": {
                        "parts": [{"text": reply_text}]
                    }
                }
            }
        }
        await asyncio.sleep(0)  # keep it async-friendly
        return DummyModel(payload)
