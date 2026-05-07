"""MCP server adapter — exposes the six core operations as MCP tools.

Imports :mod:`reddit_research.core` only. Stdio transport. Run via the
``reddit-mcp`` console script (see :mod:`reddit_research.mcp.__main__`).
"""

from reddit_research.mcp.server import build_server

__all__ = ["build_server"]
