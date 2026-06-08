from typing import Any


class AgentExecutor:
    """Base executor class.

    Real implementations run tasks and interact with agents. This stub
    provides a minimal interface so other modules can subclass it.
    """

    async def create(self):
        pass

    async def execute(self, request_context: Any, event_queue: Any) -> str:
        raise NotImplementedError()

    async def cancel(self):
        pass


class RequestContext:
    """Simple request context holding a message and optional task state."""

    def __init__(self, message: Any = None):
        self.message = message
        self.current_task = None

    def get_user_input(self):
        # Support both message objects and raw strings
        if hasattr(self.message, 'text'):
            return self.message.text
        # Support dict with 'message' key (from Streamlit)
        if isinstance(self.message, dict):
            if 'message' in self.message:
                return self.message['message']
            if 'text' in self.message:
                return self.message['text']
        return self.message
