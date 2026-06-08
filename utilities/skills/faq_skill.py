"""A tiny FAQ skill example.

This skill matches a few simple question patterns and returns canned answers
with a confidence score. It's intentionally minimal so it can be replaced or
extended by richer skills later.
"""
from typing import Optional, Tuple


FAQ_MAP = {
    "what is the current iteration": (
        "The current iteration for the XOPS Bugs Enhancement team is 25.24 (2025-12-08 to 2025-12-19).",
        0.95,
    ),
    "how many active work items": (
        "There are 36 active work items in the current iteration for the specified area.",
        0.9,
    ),
}


def _normalize(text: str) -> str:
    return text.strip().lower()


def handle(prompt: str) -> Tuple[bool, Optional[str], float]:
    q = _normalize(prompt)
    # simple exact/contains matching
    for key, (answer, conf) in FAQ_MAP.items():
        if key in q:
            return True, answer, conf
    return False, None, 0.0
