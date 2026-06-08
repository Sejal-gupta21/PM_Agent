"""Minimal LLM helpers for PM Skills Agent (placeholder).

This module is intentionally lightweight; PM Skills Agent primarily runs deterministic
handlers and does not perform planning. The orchestrator and PM Agent provide
LLM planner capabilities when routing is ambiguous.
"""


def select_tools_hint(query: str) -> list:
    """Return a short list of skill names that may be relevant for the given query.
    This is a best-effort helper used for discovery or UI hints.
    """
    q = query.lower()
    hints = []
    if "bug" in q or "recurring" in q or "repeat" in q:
        hints.append("bug_areas_highlight")
    if "overlook" in q or "stale" in q:
        hints.append("overlooked_stories")
    if "iteration" in q or "sprint" in q or "report" in q:
        hints.append("iteration_report")
    return hints
