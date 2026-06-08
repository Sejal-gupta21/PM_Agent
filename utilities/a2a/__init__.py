"""Utilities A2A package shim for minimal local development.

This package exposes `agent_connector` and `agent_discovery` modules used by
the Streamlit UI. In production, these modules should be replaced by the
full A2A client implementations.
"""
"""Utilities A2A package shim for minimal local development.

This package exposes `agent_connector` and `agent_discovery` modules used by
the Streamlit UI. In production, these modules should be replaced by the
full A2A client implementations.

This file also ensures the project root is on `sys.path` so that the
top-level `a2a` shim (created for local development) can be imported
reliably even when Streamlit runs the script from a different cwd.
"""

import sys
import os

# Insert project root into sys.path (parent of `utilities` directory)
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
	sys.path.insert(0, ROOT)

from . import agent_connector, agent_discovery

__all__ = ["agent_connector", "agent_discovery"]
