class DefaultRequestHandler:
    """Minimal request handler used by the Host Agent during local testing.

    This handler adapts incoming HTTP task payloads into the internal
    executor API by creating a `RequestContext`, running the configured
    `agent_executor`, and returning the final buffered result. It is a
    small compatibility shim and not a full production implementation.
    """

    def __init__(self, agent_executor=None, task_store=None):
        self.agent_executor = agent_executor
        self.task_store = task_store

    async def handle_request(self, payload=None):
        if not self.agent_executor:
            return {"status": "error", "message": "no agent executor configured"}

        from a2a.server.agent_execution import RequestContext
        from a2a.server.events import EventQueue

        ctx = RequestContext(message=payload)
        event_queue = EventQueue()

        try:
            result = await self.agent_executor.execute(ctx, event_queue)
            return {"status": "completed", "result": result}
        except Exception as exc:  # pragma: no cover - surface runtime errors
            return {"status": "failed", "error": str(exc)}
