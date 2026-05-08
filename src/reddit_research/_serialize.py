"""Internal: shared dataclass → JSON-able conversion.

The CLI emits JSON for ``--format json``; the MCP server returns dicts
that the host serializes for the LLM. Both need the same logic for
handling the dataclasses that :mod:`reddit_research.core.operations`
returns (ThreadSummary / Thread / CommentSummary / CommentTree / Status).

Why this isn't in ``core/``: a serializer is presentation, not transport
or business logic, and ``core/`` is documented as "no UI/adapter deps."
But it's not an adapter either — it's a generic helper both adapters
call. Top-level private (``_serialize``) keeps the layering honest.

Round-10 panel (Gemini P2-4): previously duplicated between
``cli/commands.py`` and ``mcp/server.py``. Drift between the two would
silently change one consumer's output shape.
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any


def to_jsonable(payload: Any) -> Any:
    """Recursively convert dataclasses / tuples to plain JSON-able values.

    - Frozen dataclasses → dict (via ``dataclasses.asdict``), then recurse.
    - Tuples → lists (so JSON consumers don't see ``(...,)`` strings).
    - Lists / dicts → recurse into elements.
    - Everything else → returned as-is. ``json.dumps(default=str)``
      catches anything else (datetime etc.) at the call site.
    """
    if is_dataclass(payload) and not isinstance(payload, type):
        return to_jsonable(asdict(payload))
    if isinstance(payload, (list, tuple)):
        return [to_jsonable(x) for x in payload]
    if isinstance(payload, dict):
        return {k: to_jsonable(v) for k, v in payload.items()}
    return payload
