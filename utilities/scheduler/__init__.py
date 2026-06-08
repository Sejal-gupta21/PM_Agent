"""Utilities scheduler package.

This package exposes the scheduler implementation in the module
`utilities.scheduler.scheduler`. For convenience we re-export the
primary classes at package level so existing imports like

    from utilities.scheduler import Scheduler

continue to work.
"""

from .scheduler import Scheduler, ScheduledTask
from .task_lock import (
    acquire_task_lock,
    release_task_lock,
    task_execution_lock,
    get_task_status,
    clear_task_lock,
)

__all__ = [
    "Scheduler",
    "ScheduledTask",
    "acquire_task_lock",
    "release_task_lock",
    "task_execution_lock",
    "get_task_status",
    "clear_task_lock",
]
