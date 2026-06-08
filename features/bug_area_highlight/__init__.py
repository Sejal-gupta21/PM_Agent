"""
Bug Area Highlight Feature

Detects recurring bugs by area path and generates email reports.
"""

from .service import BugAreaHighlightService
from .skill import bug_areas_highlight_skill, run_bug_areas_highlight
from .scheduler import bug_areas_highlight_scheduled_task, run_from_config

__all__ = [
    "BugAreaHighlightService",
    "bug_areas_highlight_skill",
    "run_bug_areas_highlight",
    "bug_areas_highlight_scheduled_task",
    "run_from_config",
]
