import uuid
from dataclasses import dataclass


@dataclass
class Task:
    id: str
    context_id: str
    message: any


def new_task(message):
    tid = str(uuid.uuid4())
    ctx = str(uuid.uuid4())
    return Task(id=tid, context_id=ctx, message=message)


def new_agent_text_message(text, context_id=None, task_id=None):
    return {"text": text, "context_id": context_id, "task_id": task_id}
