"""
Bug Area Highlight Skill Handler

Skill implementation for PM Skill Agent integration.
"""

import os
import logging
from typing import Dict, Any
from dataclasses import dataclass, field

logger = logging.getLogger("pm_agent.features.bug_area_highlight.skill")


@dataclass
class SkillResult:
    """Result from executing a skill."""
    success: bool
    result: Any = None
    message: str = None
    error: str = None
    metadata: Dict[str, Any] = field(default_factory=dict)


async def run_bug_areas_highlight(params: Dict[str, Any]) -> SkillResult:
    """
    Execute Bug Areas Highlight skill.
    
    Params:
        - project: ADO project name
        - lookback_days: Days to look back (default 60)
        - recurrence_threshold: Min bugs to consider recurring (default 3)
        - similarity_threshold: Title similarity threshold (default 0.75)
        - recipients: Email recipients list
        - send_email: Whether to send email (default True)
        - preview_only: Just preview, don't email (default False)
    """
    try:
        from .service import BugAreaHighlightService
        from config import config as app_config
        
        # Extract parameters
        project = params.get("project") or app_config.ado_project
        lookback_days = int(params.get("lookback_days", 60))
        recurrence_threshold = int(params.get("recurrence_threshold", 3))
        similarity_threshold = float(params.get("similarity_threshold", 0.75))
        recipients = params.get("recipients") or getattr(app_config, "reportEmailRecipients", [])
        send_email = params.get("send_email", True)
        preview_only = params.get("preview_only", False)
        no_area_label = params.get("no_area_label", "No Area")
        
        logger.info(f"Running bug areas highlight: project={project}, lookback={lookback_days}d")
        
        # Initialize service
        service = BugAreaHighlightService()
        
        if not service.org_url:
            return SkillResult(
                success=False,
                error="ADO_ORG_URL not configured"
            )
        
        # Run analysis
        recurring, html_content, bugs = service.run_analysis(
            lookback_days=lookback_days,
            recurrence_threshold=recurrence_threshold,
            similarity_threshold=similarity_threshold,
            no_area_label=no_area_label,
        )
        
        recurring_count = sum(
            len(area_data.get("clusters", []))
            for area_data in recurring.values()
            if isinstance(area_data, dict)
        )
        
        # Save preview file
        preview_path = None
        try:
            os.makedirs("logs", exist_ok=True)
            preview_path = "logs/bug_areas_preview.html"
            with open(preview_path, "w", encoding="utf-8") as f:
                f.write(html_content)
            logger.info(f"Preview saved to {preview_path}")
        except Exception as e:
            logger.warning(f"Could not save preview: {e}")
        
        # Send email if requested
        email_sent = False
        email_error = None
        if send_email and not preview_only and recipients:
            try:
                ok, msg = service.send_report(recurring, html_content, recipients)
                email_sent = ok
                if not ok:
                    email_error = msg
                logger.info(f"Email {'sent' if ok else 'failed'}: {msg}")
            except Exception as e:
                email_error = str(e)
                logger.exception(f"Email send error: {e}")
        
        return SkillResult(
            success=True,
            result={
                "count": len(bugs),
                "recurring_count": recurring_count,
                "areas": list(recurring.keys()),
                "preview_path": preview_path,
                "email_sent": email_sent,
                "email_error": email_error,
                "recipients": recipients if email_sent else None
            },
            metadata={
                "project": project,
                "lookback_days": lookback_days,
                "recurrence_threshold": recurrence_threshold,
                "similarity_threshold": similarity_threshold,
            }
        )
        
    except Exception as e:
        logger.exception(f"Bug areas highlight error: {e}")
        return SkillResult(success=False, error=str(e))


# Skill definition for registration
bug_areas_highlight_skill = {
    "name": "bug_areas_highlight",
    "description": "Detect recurring bugs by area path and send email report with highlighted patterns",
    "handler": run_bug_areas_highlight,
    "required_params": [],
    "optional_params": {
        "project": "ADO project name",
        "lookback_days": "Days to look back (default 60)",
        "recurrence_threshold": "Min bugs to consider recurring (default 3)",
        "similarity_threshold": "Title similarity threshold (default 0.75)",
        "recipients": "Email recipients list",
        "send_email": "Whether to send email (default True)",
        "preview_only": "Just preview without emailing"
    },
    "use_cases": [
        "recurring bugs", "bug analysis", "bug patterns", 
        "bug areas", "highlight bugs", "bug report"
    ]
}
