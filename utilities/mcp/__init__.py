"""
Package initializer for utilities.mcp

Avoid importing submodules at package import time to prevent circular
imports. Import specific symbols where needed, for example:

	from utilities.mcp import mcp_discovery
	from utilities.mcp.mcp_discovery import discover_mcp

This file intentionally does not import submodules so other modules
can import `utilities.mcp` without triggering their initialization.
"""

__all__ = []
