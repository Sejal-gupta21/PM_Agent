"""Simple local skills registry used by the Streamlit chat orchestrator.

Each skill exports a `handle(prompt: str) -> (handled: bool, response: Optional[str], confidence: float)` function.
The orchestrator will call `run_skills()` and use the first skill that returns
`handled == True` with a confidence >= provided threshold.
"""
from typing import Optional, Tuple

from . import faq_skill

# Registry of skill modules (order = priority)
SKILLS = [faq_skill]


def run_skills(prompt: str, threshold: float = 0.7) -> Tuple[bool, Optional[str], float]:
    """Run registered skills against `prompt`.

    Returns (handled, response, confidence). If no skill handled the prompt,
    returns (False, None, 0.0).
    """
    for skill in SKILLS:
        try:
            handled, resp, conf = skill.handle(prompt)
        except Exception:
            # Skill failures should not stop orchestration
            handled, resp, conf = False, None, 0.0
        if handled and (conf is None or conf >= threshold):
            return True, resp, conf
    return False, None, 0.0
