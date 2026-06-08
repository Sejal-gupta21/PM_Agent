from a2a.types import AgentCard, SendMessageRequest, MessageSendParams
from a2a.client import A2AClient
import httpx
from uuid import uuid4
from a2a.types import TextPart, Message, Part
import traceback
import logging

# Import trace context helpers for unified observability
from utilities.langfuse_client import get_current_trace_id

logger = logging.getLogger("a2a.agent_connector")


class AgentConnector:
    """
    Connect to remote A2A agents using their AgentCard information and create a unified interface to interact with them.
    
    IMPORTANT: This connector propagates trace IDs to remote agents for unified single-trace observability.
    """
    
    def __init__(self, agent_card: AgentCard):
        self.agent_card = agent_card
        
    async def send_task(self, message: str = "", session_id: str = "", trace_id: str = None) -> str:
        """
        Send a request to the remote agent and return the response.
        
        Args:
            message (str): The input message to send to the agent.
            session_id (str): Session identifier for conversation tracking.
            trace_id (str): Optional trace ID for unified observability. If not provided,
                           will attempt to get from current trace context.
            
        Returns:
            str: The response from the agent.
        """
        # Get trace ID from parameter or current context for unified observability
        effective_trace_id = trace_id or get_current_trace_id()
        if effective_trace_id:
            logger.debug(f"Propagating trace_id={effective_trace_id} to remote agent")
        
        async with httpx.AsyncClient(timeout=300) as httpx_client:
            a2a_client = A2AClient(
                httpx_client=httpx_client,
                agent_card = self.agent_card,
            )
            
            # Build message with trace metadata for unified observability
            send_message_payload = {
                    "role": "user",
                    "message_id": str(uuid4()),
                    "parts": [Part(root=TextPart(text=message))],
                    # Include trace metadata for remote agent to join the trace
                    "metadata": {
                        "trace_id": effective_trace_id,
                        "session_id": session_id,
                    } if effective_trace_id else {}
                }

            request = SendMessageRequest(
                id = str(uuid4()),
                params = MessageSendParams(
                    message=Message(**send_message_payload))
            )
            
            # Add trace ID header for HTTP-level propagation
            if effective_trace_id:
                httpx_client.headers["X-Trace-Id"] = effective_trace_id
            
            response = await a2a_client.send_message(request)
            response_data = response.model_dump(mode="json", exclude_none=True)   

            try:
                agent_response = response_data["result"]['status']['message']['parts'][0]['text']
            except (KeyError, IndexError):
                logger.error(f"A2A response parsing error: {response}")
                agent_response = "No response received from the agent." 
                logger.exception("A2A Error")
            
            return agent_response
            
