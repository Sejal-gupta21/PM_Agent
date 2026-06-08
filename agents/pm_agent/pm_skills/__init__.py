"""
PM Agent Skills Package.

Contains specialized skills for PM Agent:
- wiql_skill: Direct WIQL query execution via execute_wiql
"""

from .wiql_skill import (
    execute_wiql,
    validate_wiql,
    run_wiql_query,  # DEPRECATED alias — kept for backward compat only
)

__all__ = [
    "execute_wiql",
    "validate_wiql",
    "run_wiql_query",
]
