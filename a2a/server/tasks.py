class InMemoryTaskStore:
    """Very small in-memory task store used for local testing."""

    def __init__(self):
        self._tasks = {}

    async def add_task(self, task_id, task):
        self._tasks[task_id] = task

    async def get_task(self, task_id):
        return self._tasks.get(task_id)

    async def list_tasks(self):
        return list(self._tasks.values())


class TaskUpdater:
    """Simple updater that posts status updates to an EventQueue."""

    def __init__(self, event_queue, task_id, context_id):
        self.event_queue = event_queue
        self.task_id = task_id
        self.context_id = context_id

    async def update_status(self, state, message):
        await self.event_queue.enqueue_event({
            "task_id": self.task_id,
            "context_id": self.context_id,
            "state": state,
            "message": message,
        })
