"""
Feedback to Dev Feature

Detects newly created bugs, finds related historical bugs using similarity matching,
extracts RCA content, and sends feedback notifications to developers.
"""

from .service import FeedbackToDevService
from .skill import feedback_to_dev_skill, run_feedback_to_dev
from .scheduler import feedback_to_dev_scheduled_task, run_from_config

__all__ = [
    "FeedbackToDevService",
    "feedback_to_dev_skill",
    "run_feedback_to_dev",
    "feedback_to_dev_scheduled_task",
    "run_from_config",
]
