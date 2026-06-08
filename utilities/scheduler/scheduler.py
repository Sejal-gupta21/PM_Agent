"""
Generic, reusable scheduler that can run registered tasks on cron-like schedules.

Design goals:
- Small API to register tasks: scheduler.register_task(...)
- Uses `croniter` to compute next execution times and asyncio to run tasks.
- Tasks can be async functions or sync callables (sync run in executor).
"""

import asyncio
import logging
from datetime import datetime
from datetime import timedelta
from zoneinfo import ZoneInfo
from croniter import croniter
from typing import Callable, Any, Dict

logger = logging.getLogger("pm_agent.scheduler")


class ScheduledTask:
    def __init__(
        self,
        name: str,
        cron: str,
        timezone: str,
        coro: Callable[..., Any],
        kwargs: Dict = None,
        enabled: bool = True,
        skip_missed_on_startup: bool = True,
    ):
        self.name = name
        self.cron = cron
        self.timezone = timezone or "UTC"
        self.coro = coro
        self.kwargs = kwargs or {}
        self.enabled = enabled
        self.skip_missed_on_startup = skip_missed_on_startup
        self._task = None
        self._first_run = True

    async def _run_loop(self):
        tz = ZoneInfo(self.timezone)
        logger.info(
            "Task '%s' scheduler loop started (cron=%s tz=%s)",
            self.name,
            self.cron,
            self.timezone,
        )
        while self.enabled:
            # get current time in the task timezone
            now = datetime.now(tz)

            # On first run (startup), use current time as base to avoid running
            # tasks that were scheduled in the recent past. This prevents tasks
            # from running immediately when the scheduler starts after their
            # scheduled time (e.g., scheduler starts at 10:32, tasks scheduled
            # for 10:00 should wait until tomorrow at 10:00, not run immediately).
            if self._first_run and self.skip_missed_on_startup:
                # Use current time (rounded to current minute) as base
                base = now.replace(second=0, microsecond=0)
                self._first_run = False
                logger.info(
                    "Task '%s' first run - skipping any missed schedules, next run computed from %s",
                    self.name,
                    base.isoformat()
                )
            else:
                # Use current minute as base to avoid tiny negative sleeps
                # (previous logic could produce 0-second sleeps and tight loops).
                base = now.replace(second=0, microsecond=0)

            try:
                itr = croniter(self.cron, base)
                next_dt = itr.get_next(datetime)
            except Exception as e:
                logger.exception("Invalid cron for task '%s': %s", self.name, e)
                return

            # Ensure next_dt is timezone-aware with the same tz
            if next_dt.tzinfo is None:
                try:
                    next_dt = next_dt.replace(tzinfo=tz)
                except Exception:
                    # fallback: attach UTC then convert
                    from datetime import timezone

                    next_dt = next_dt.replace(tzinfo=timezone.utc).astimezone(tz)

            # ensure we never spin with a 0s sleep; use at least 1s
            sleep_seconds = max(1.0, (next_dt - now).total_seconds())

            logger.info("Task '%s' next run scheduled at %s (in %.1f seconds)", self.name, next_dt.isoformat(), sleep_seconds)
            # sleep until next run time
            await asyncio.sleep(sleep_seconds)

            logger.info(
                "Scheduler triggering task '%s' at %s", self.name, datetime.now(tz).isoformat()
            )
            try:
                if asyncio.iscoroutinefunction(self.coro):
                    await self.coro(**self.kwargs)
                else:
                    loop = asyncio.get_event_loop()
                    await loop.run_in_executor(None, lambda: self.coro(**self.kwargs))
                logger.info("Task '%s' completed successfully", self.name)
            except Exception:
                logger.exception("Task '%s' raised an exception", self.name)

    def start(self):
        if not self.enabled:
            logger.info("Task '%s' is disabled; skipping start", self.name)
            return
        if self._task and not self._task.done():
            logger.warning("Task '%s' already running", self.name)
            return
        self._task = asyncio.create_task(self._run_loop())

    def stop(self):
        self.enabled = False
        if self._task:
            self._task.cancel()


class Scheduler:
    def __init__(self):
        self.tasks = {}
        logging.getLogger("pm_agent.scheduler").setLevel(logging.INFO)

    def register_task(
        self,
        name: str,
        cron: str,
        timezone: str,
        coro: Callable[..., Any],
        kwargs: Dict = None,
        enabled: bool = True,
        skip_missed_on_startup: bool = True,
    ):
        st = ScheduledTask(
            name=name, 
            cron=cron, 
            timezone=timezone, 
            coro=coro, 
            kwargs=kwargs or {}, 
            enabled=enabled,
            skip_missed_on_startup=skip_missed_on_startup
        )
        self.tasks[name] = st
        logger.info(
            "Registered task '%s' (cron=%s tz=%s enabled=%s skip_missed=%s)", 
            name, cron, timezone, enabled, skip_missed_on_startup
        )

    def start_all(self):
        logger.info("Starting %d scheduled task(s)", len(self.tasks))
        for name, task in self.tasks.items():
            task.start()

    def stop_all(self):
        for task in self.tasks.values():
            task.stop()
