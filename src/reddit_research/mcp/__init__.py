"""MCP server adapter — exposes the six core operations as MCP tools.

Imports :mod:`reddit_research.core` only. Stdio transport. Run via the
``reddit-mcp`` console script (see :mod:`reddit_research.mcp.__main__`).

The ``mcp`` Python SDK is installed as the ``[mcp]`` extra rather than a
base dependency. This package therefore does NOT eagerly import
:mod:`reddit_research.mcp.server` — that would crash on plain installs
without the extra (round-10 panel finding from Codex). Importers that
want the server factory should either ``from reddit_research.mcp.server
import build_server`` directly, or rely on the ``reddit-mcp`` console
script's :func:`reddit_research.mcp.__main__.main` which surfaces a
friendly install hint when the SDK is missing.
"""

# Intentionally no eager imports of `.server`. See module docstring.
