"""
Per-task execution lock to prevent duplicate task executions.

This module provides a file-based locking mechanism to ensure that
scheduled tasks run exactly once per scheduled interval, even if
multiple scheduler processes are accidentally started.
"""

import os
import json
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, Dict, Any
from contextlib import contextmanager

logger = logging.getLogger("pm_agent.scheduler.task_lock")

# Default directory for lock files
LOCKS_DIR = Path(__file__).resolve().parents[2] / "outputs" / "scheduler_locks"


def _ensure_locks_dir() -> Path:
    """Ensure the locks directory exists."""
    LOCKS_DIR.mkdir(parents=True, exist_ok=True)
    return LOCKS_DIR


def _get_lock_file_path(task_name: str) -> Path:
    """Get the path to the lock file for a task."""
    return _ensure_locks_dir() / f"{task_name}.lock.json"


def _compute_schedule_key(task_name: str, schedule_time: datetime) -> str:
    """
    Compute a unique key for a scheduled execution.
    
    Uses the task name and the scheduled time (rounded to minute) to create
    a unique identifier for this specific scheduled run.
    """
    # Round to the minute to handle small timing variations
    rounded = schedule_time.replace(second=0, microsecond=0)
    return f"{task_name}:{rounded.isoformat()}"


def acquire_task_lock(task_name: str, schedule_time: Optional[datetime] = None) -> bool:
    """
    Attempt to acquire a lock for a scheduled task execution.
    
    This is a non-blocking check that returns True if this is the first
    execution attempt for this task at this scheduled time.
    
    Args:
        task_name: Name of the scheduled task
        schedule_time: The scheduled execution time (defaults to now)
        
    Returns:
        True if lock acquired (task should run), False if already executed
    """
    if schedule_time is None:
        schedule_time = datetime.now(timezone.utc)
    
    lock_file = _get_lock_file_path(task_name)
    schedule_key = _compute_schedule_key(task_name, schedule_time)
    
    try:
        # Read existing lock data
        lock_data: Dict[str, Any] = {}
        if lock_file.exists():
            try:
                with open(lock_file, "r", encoding="utf-8") as f:
                    lock_data = json.load(f)
            except (json.JSONDecodeError, IOError):
                lock_data = {}
        
        # Check if this schedule was already executed
        last_key = lock_data.get("last_schedule_key")
        if last_key == schedule_key:
            logger.info(
                "Task '%s' already executed for schedule %s - skipping",
                task_name, schedule_time.isoformat()
            )
            return False
        
        # Write new lock with atomic operation
        new_data = {
            "task_name": task_name,
            "last_schedule_key": schedule_key,
            "last_execution_start": datetime.now(timezone.utc).isoformat(),
            "pid": os.getpid(),
        }
        
        tmp_file = str(lock_file) + ".tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(new_data, f, indent=2)
        
        # Atomic replace
        try:
            os.replace(tmp_file, lock_file)
        except OSError:
            os.rename(tmp_file, lock_file)
        
        logger.info(
            "Task '%s' acquired lock for schedule %s",
            task_name, schedule_time.isoformat()
        )
        return True
        
    except Exception as e:
        logger.exception("Error acquiring task lock for '%s': %s", task_name, e)
        # On error, allow execution to prevent missed runs
        return True


def release_task_lock(task_name: str, success: bool = True) -> None:
    """
    Update the lock file to record task completion.
    
    Args:
        task_name: Name of the scheduled task
        success: Whether the task completed successfully
    """
    lock_file = _get_lock_file_path(task_name)
    
    try:
        lock_data: Dict[str, Any] = {}
        if lock_file.exists():
            try:
                with open(lock_file, "r", encoding="utf-8") as f:
                    lock_data = json.load(f)
            except (json.JSONDecodeError, IOError):
                lock_data = {}
        
        lock_data["last_execution_end"] = datetime.now(timezone.utc).isoformat()
        lock_data["last_execution_success"] = success
        
        tmp_file = str(lock_file) + ".tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(lock_data, f, indent=2)
        
        try:
            os.replace(tmp_file, lock_file)
        except OSError:
            os.rename(tmp_file, lock_file)
            
    except Exception as e:
        logger.exception("Error releasing task lock for '%s': %s", task_name, e)


@contextmanager
def task_execution_lock(task_name: str, schedule_time: Optional[datetime] = None):
    """
    Context manager for task execution with automatic locking.
    
    Usage:
        with task_execution_lock("my_task") as should_run:
            if should_run:
                # Execute task
                pass
    
    Yields:
        True if lock acquired and task should run, False otherwise
    """
    acquired = acquire_task_lock(task_name, schedule_time)
    success = False
    try:
        yield acquired
        success = True
    finally:
        if acquired:
            release_task_lock(task_name, success)


def get_task_status(task_name: str) -> Optional[Dict[str, Any]]:
    """
    Get the current lock/execution status for a task.
    
    Returns:
        Dict with lock info or None if no lock file exists
    """
    lock_file = _get_lock_file_path(task_name)
    
    if not lock_file.exists():
        return None
    
    try:
        with open(lock_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return None


def clear_task_lock(task_name: str) -> bool:
    """
    Clear the lock for a task (for testing or manual reset).
    
    Returns:
        True if lock was cleared, False if no lock existed
    """
    lock_file = _get_lock_file_path(task_name)
    
    if lock_file.exists():
        try:
            lock_file.unlink()
            logger.info("Cleared lock for task '%s'", task_name)
            return True
        except Exception as e:
            logger.exception("Error clearing lock for '%s': %s", task_name, e)
    
    return False
