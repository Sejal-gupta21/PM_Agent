"""Lightweight local shim for the `a2a` package used in development.

This module provides a minimal API surface (types + client) so the
repository can be executed without installing the upstream `a2a` package.
It intentionally implements only the small subset required by the
Streamlit UI and local agents in this repo.
"""

from . import client, types

__all__ = ["client", "types"]
