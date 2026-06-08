"""
Features Package - Modular feature implementations for PM Agent.

Each feature is organized in its own folder with:
- service.py: Core business logic
- skill.py: Skill handler for PM Skill Agent
- scheduler.py: Scheduler task integration
- __init__.py: Module exports
"""

from .bug_area_highlight import (
    BugAreaHighlightService,
    bug_areas_highlight_skill,
    bug_areas_highlight_scheduled_task,
)

from .feedback_to_dev import (
    FeedbackToDevService,
    feedback_to_dev_skill,
    feedback_to_dev_scheduled_task,
)

__all__ = [
    # Bug Area Highlight
    "BugAreaHighlightService",
    "bug_areas_highlight_skill",
    "bug_areas_highlight_scheduled_task",
    # Feedback to Dev
    "FeedbackToDevService",
    "feedback_to_dev_skill",
    "feedback_to_dev_scheduled_task",
]
