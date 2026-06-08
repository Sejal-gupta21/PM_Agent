"""
Feedback to Dev Skill Handler

Skill implementation for PM Skill Agent integration.
"""

import os
import logging
import asyncio
from typing import Dict, Any
from dataclasses import dataclass, field
from config import config

logger = logging.getLogger("pm_agent.features.feedback_to_dev.skill")


@dataclass
class SkillResult:
    """Result from executing a skill."""
    success: bool
    result: Any = None
    message: str = None
    error: str = None
    metadata: Dict[str, Any] = field(default_factory=dict)


async def run_feedback_to_dev(params: Dict[str, Any]) -> SkillResult:
    """
    Execute Feedback to Dev skill.
    
    Detects new bugs, finds similar historical bugs, extracts RCA content,
    and sends feedback notifications.
    
    Params:
        - project: ADO project name
        - lookback_minutes: How far back to look for new bugs (default 1440 = 24h)
        - historical_days: How far back to look for historical bugs (default 30)
        - embedding_threshold: Similarity threshold for embeddings (default 0.82)
        - recipients: Email recipients list
        - is_test: Whether this is a test run (default False)
    """
    try:
        from .service import FeedbackToDevService
        
        # Extract parameters
        project = params.get("project") or config.ado_project
        lookback_minutes = int(params.get("lookback_minutes", 1440))
        historical_days = int(params.get("historical_days", 30))
        embedding_threshold = float(params.get("embedding_threshold", 0.82))
        is_test = bool(params.get("is_test", False))
        
        logger.info(
            f"Running feedback_to_dev: project={project}, "
            f"lookback={lookback_minutes}min, hist_days={historical_days}"
        )
        
        # Initialize service
        service = FeedbackToDevService()
        
        if not service.org_url:
            return SkillResult(
                success=False,
                error="ADO_ORG_URL not configured"
            )
        
        # Run workflow
        result = await service.run_workflow(
            lookback_minutes=lookback_minutes,
            historical_days=historical_days,
            embedding_threshold=embedding_threshold,
            is_test=is_test,
        )
        
        if "error" in result:
            return SkillResult(
                success=False,
                error=result["error"],
                metadata={"project": project}
            )
        
        return SkillResult(
            success=True,
            result=result,
            metadata={
                "project": project,
                "lookback_minutes": lookback_minutes,
                "historical_days": historical_days,
                "embedding_threshold": embedding_threshold,
            }
        )
        
    except Exception as e:
        logger.exception(f"Feedback to dev error: {e}")
        return SkillResult(success=False, error=str(e))


# Skill definition for registration
feedback_to_dev_skill = {
    "name": "feedback_to_dev",
    "description": "Detect new bugs, find similar historical bugs, extract RCA content, and send feedback notifications to developers",
    "handler": run_feedback_to_dev,
    "required_params": [],
    "optional_params": {
        "project": "ADO project name",
        "lookback_minutes": "How far back to look for new bugs (default 1440 = 24h)",
        "historical_days": "How far back to look for historical bugs (default 30)",
        "embedding_threshold": "Similarity threshold for embeddings (default 0.82)",
        "recipients": "Email recipients list",
        "is_test": "Whether this is a test run (default False)"
    },
    "use_cases": [
        "feedback to dev", "bug feedback", "rca feedback",
        "new bug notification", "developer feedback", "bug analysis feedback"
    ]
}
