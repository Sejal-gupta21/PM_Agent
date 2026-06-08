class EventQueue:
    """Minimal event queue shim used by the Host Agent executor."""

    def __init__(self):
        self._events = []

    async def enqueue_event(self, event):
        self._events.append(event)

    async def get_events(self):
        return list(self._events)
