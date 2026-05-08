"""FastMCP server exposing the six core operations as MCP tools.

Why a thin adapter rather than re-implementing protocol details: the MCP
protocol surface (initialize / tools/list / tools/call / shutdown) is a
moving target with discovery semantics, capability negotiation, and JSON
schema conventions; FastMCP owns that, and we own the Reddit logic.

Lifecycle: one :class:`reddit_research.core.Operations` instance backs the
whole server lifetime — ``cache.db`` and the ``WorkflowBudget`` are shared
across every tool call in the stdio session. The budget is "per-session for
the MCP server" (CLAUDE.md ## Rate-limit discipline) so it deliberately
does NOT reset between calls; the host process exit is the session
boundary. A ``reset_budget`` tool gives the LLM a manual escape hatch when
it knows it's starting a new logical research run within one session.

Error mapping: Reddit-domain errors that the LLM should reason about
(``NotFound``/``Forbidden``/``RateLimit``/``BudgetExceeded``/``InvalidId``/
parser-strict ``ValueError``) come back as structured dicts under an
``error`` key, NOT as raised exceptions. Raising would surface as
``isError: True`` on the MCP CallToolResult, which loses the
discriminator fields the LLM needs to decide what to do (retry vs. give
up vs. ask for clarification). Pure programmer / infrastructure errors
(cache disk full, schema mismatch, unexpected exception) DO raise — those
are not the LLM's problem.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from pydantic import Field

from reddit_research import __version__ as PKG_VERSION
from reddit_research._serialize import to_jsonable
from reddit_research.core import (
    BudgetExceededError,
    ForbiddenError,
    HTTPError,
    InvalidIdError,
    NotFoundError,
    Operations,
    RateLimitError,
    RedirectError,
    TransportError,
    UpstreamError,
    WorkflowBudget,
)

logger = logging.getLogger(__name__)


SERVER_NAME = "reddit-research"
SERVER_INSTRUCTIONS = (
    "Read-only Reddit research tools. Workflow: search() or "
    "get_subreddit_listing() to find threads → get_thread() for a cheap "
    "post + top-N top-level comment summary → expand_comment() to follow "
    "promising sub-trees deeper. All reads are cached automatically; pass "
    "fresh=True to bypass for a specific call. Per-session aggregate "
    "budget caps total API calls and total comments fetched (raises "
    "structured budget_exceeded errors); call reset_budget() when starting "
    "a new logical research run within the same session. Use status() to "
    "check cache state and remaining budget."
)


# ---- Result-shape helpers -------------------------------------------------


def _ok(payload: Any) -> dict:
    return {"ok": True, "result": to_jsonable(payload)}


def _err(error_type: str, **fields: Any) -> dict:
    """Return-style error envelope for Reddit-domain failures.

    Tools always return this rather than raising for outcomes the LLM is
    expected to handle (404/403/rate-limit/budget/invalid-id/schema drift).
    The ``ok: false`` marker is the LLM's branch signal; the typed
    ``error.type`` discriminator tells it which branch.
    """
    return {"ok": False, "error": {"type": error_type, **fields}}


def _wrap_reddit_call(fn, *args, **kwargs) -> dict:
    """Run a core operation and translate typed errors into structured dicts.

    Pure programmer / infrastructure failures (RuntimeError from cache
    schema mismatch, unexpected Exception) propagate so FastMCP surfaces
    them as MCP-level isError responses — those aren't the LLM's problem
    and a structured ``ok: false`` would make them invisible to the host
    operator.

    Round-10 panel (Opus P2-2): catches generic ``HTTPError`` last so
    statuses outside the 3xx/403/404/429/5xx mapping (400 / 401 / 402 /
    405 / 418 etc.) still surface structured to the LLM with the status
    code preserved, instead of escaping as MCP isError. Reddit's ``.json``
    transport doesn't return these today, but if its edge changes the
    failure stays informative.
    """
    try:
        result = fn(*args, **kwargs)
    except BudgetExceededError as e:
        return _err(
            "budget_exceeded",
            kind=e.kind,
            used=e.used,
            cap=e.cap,
            message=str(e),
        )
    except RateLimitError as e:
        return _err(
            "rate_limited",
            retry_after_seconds=e.retry_after,
            path=e.path,
            reason=e.reason,
            message=e.message or str(e),
        )
    except NotFoundError as e:
        return _err(
            "not_found",
            path=e.path,
            reason=e.reason,
            message=e.message or str(e),
        )
    except ForbiddenError as e:
        return _err(
            "forbidden",
            path=e.path,
            reason=e.reason,
            message=e.message or str(e),
        )
    except RedirectError as e:
        return _err(
            "redirect",
            path=e.path,
            location=e.location,
            message=str(e),
        )
    except (TransportError, UpstreamError) as e:
        return _err(
            "transport_error" if isinstance(e, TransportError) else "upstream_error",
            message=str(e),
        )
    except InvalidIdError as e:
        return _err("invalid_id", message=str(e))
    except HTTPError as e:
        # Catch-all for unmapped statuses. Must come AFTER the specific
        # subclasses above (HTTPError is their base class). Preserves the
        # status code so the LLM can branch on it.
        return _err(
            "http_error",
            status=e.status,
            path=e.path,
            reason=e.reason,
            message=e.message or str(e),
        )
    except ValueError as e:
        # Strict parsers (Reddit schema drift) and operation-layer cap
        # validators (depth/limit/top_n out of range) both raise ValueError.
        # Both are LLM-facing — schema drift means "ask the user / try
        # later", cap violations mean "fix your arguments". Same envelope,
        # the message disambiguates.
        return _err("invalid_input", message=str(e))
    return _ok(result)


# ---- Server factory -------------------------------------------------------


def build_server(
    ops: Operations,
    budget: WorkflowBudget | None = None,
) -> FastMCP:
    """Build a FastMCP server with the six tools wired to ``ops``.

    ``budget`` is the same :class:`WorkflowBudget` that was passed into
    ``ops``'s underlying client — the server holds a direct reference to
    expose ``reset_budget`` without reaching into private state. Pass
    ``None`` if the server was started without a budget; ``reset_budget``
    will return a structured ``no_budget_configured`` error.

    Caller owns ``ops`` lifetime — the server does not close it on
    shutdown. This split keeps the factory testable (drop in a mock
    Operations) and lets the entry point handle teardown via try/finally.
    """
    mcp = FastMCP(name=SERVER_NAME, instructions=SERVER_INSTRUCTIONS)
    # Override the SDK-version default so serverInfo.version reports OUR
    # package version (what hosts display to users), not the mcp SDK's.
    # FastMCP doesn't expose a `version=` constructor arg yet (1.27.0); the
    # underlying lowlevel.Server reads `.version` directly during initialize.
    mcp._mcp_server.version = PKG_VERSION

    @mcp.tool(
        description=(
            "Search Reddit. subreddit=None searches all of Reddit; pass a "
            "name (no 'r/' prefix) to scope. sort: relevance|hot|top|new|"
            "comments. time: all|year|month|week|day|hour. limit: 1-100, "
            "default 25. Returns a list of thread summaries; pick a "
            "fullname (e.g. t3_abc123) to feed into get_thread()."
        )
    )
    def search(
        query: str,
        subreddit: str | None = None,
        sort: str = "relevance",
        time_filter: str = "all",
        limit: Annotated[int, Field(ge=1, le=100)] = 25,
        fresh: bool = False,
    ) -> dict:
        return _wrap_reddit_call(
            ops.search,
            query=query,
            subreddit=subreddit,
            sort=sort,
            time_filter=time_filter,
            limit=limit,
            fresh=fresh,
        )

    @mcp.tool(
        description=(
            "Fetch a subreddit listing — hot|new|top|rising|controversial. "
            "limit: 1-100, default 25. time_filter only applies to top "
            "and controversial; ignored otherwise. Returns thread summaries "
            "in Reddit's order."
        )
    )
    def get_subreddit_listing(
        name: str,
        sort: str = "hot",
        limit: Annotated[int, Field(ge=1, le=100)] = 25,
        time_filter: str = "all",
        fresh: bool = False,
    ) -> dict:
        return _wrap_reddit_call(
            ops.get_subreddit_listing,
            name=name,
            sort=sort,
            limit=limit,
            time_filter=time_filter,
            fresh=fresh,
        )

    @mcp.tool(
        description=(
            "Fetch a thread's post + the first N top-level comments in "
            "Reddit's own ranking. thread_id accepts bare id (abc123) or "
            "fullname (t3_abc123). top_n_comments: 1-100, default 20. "
            "Cheap by design — use expand_comment() to dive into a "
            "specific subtree once you've picked one. Returned comments "
            "include nested replies that came back in Reddit's response "
            "(may be partial; expand_comment() on the focal comment for "
            "full coverage)."
        )
    )
    def get_thread(
        thread_id: str,
        top_n_comments: Annotated[int, Field(ge=1, le=100)] = 20,
        fresh: bool = False,
    ) -> dict:
        return _wrap_reddit_call(
            ops.get_thread,
            thread_id=thread_id,
            top_n_comments=top_n_comments,
            fresh=fresh,
        )

    @mcp.tool(
        description=(
            "Fetch one comment + its reply subtree. subreddit is the "
            "thread's subreddit (no 'r/' prefix). thread_id and comment_id "
            "accept bare ids or fullnames. depth: 0-5 (0 = focal comment "
            "only). limit: 1-50. Per-call hard caps; per-session aggregate "
            "budget also applies."
        )
    )
    def expand_comment(
        subreddit: str,
        thread_id: str,
        comment_id: str,
        depth: Annotated[int, Field(ge=0, le=5)] = 2,
        limit: Annotated[int, Field(ge=1, le=50)] = 20,
        fresh: bool = False,
    ) -> dict:
        return _wrap_reddit_call(
            ops.expand_comment,
            subreddit=subreddit,
            thread_id=thread_id,
            comment_id=comment_id,
            depth=depth,
            limit=limit,
            fresh=fresh,
        )

    @mcp.tool(
        description=(
            "Delete cache rows older than older_than_days. Returns the "
            "number of rows removed. Doesn't touch live Reddit; safe to "
            "call any time."
        )
    )
    def purge(older_than_days: Annotated[int, Field(ge=0)] = 30) -> dict:
        # Round-10 panel (Gemini P1): ge=0 in the schema gives hosts the
        # fail-fast signal; the underlying Cache.purge enforces the same
        # bound at runtime so direct callers (CLI, library) get parity.
        try:
            deleted = ops.purge(older_than_days=older_than_days)
        except ValueError as e:
            return _err("invalid_input", message=str(e))
        return _ok({"deleted_rows": deleted, "older_than_days": older_than_days})

    @mcp.tool(
        description=(
            "Snapshot of cache state, transport rate-limit headroom, and "
            "session budget consumption. Call this before launching a "
            "wide search to confirm headroom; or after, to see what got "
            "spent. Doesn't touch the network."
        )
    )
    def status() -> dict:
        return _wrap_reddit_call(ops.status)

    @mcp.tool(
        description=(
            "Zero the per-session aggregate budget's consumed counters "
            "(api_calls / comments). Configured caps are unchanged. Use "
            "this when starting a new logical research run within the "
            "same MCP session; the budget is otherwise bounded by the "
            "host process lifetime."
        )
    )
    def reset_budget() -> dict:
        if budget is None:
            return _err(
                "no_budget_configured",
                message="Server was started without a WorkflowBudget; nothing to reset.",
            )
        budget.reset()
        return _ok(budget.snapshot())

    return mcp


# ---- Entry-point helper used by __main__ ----------------------------------


def serve(
    ops: Operations,
    budget: WorkflowBudget | None = None,
    *,
    transport: str = "stdio",
) -> None:
    """Build and run an MCP server until the host disconnects.

    Sync. Blocks the calling process. The caller owns ``ops`` and is
    responsible for closing it after this returns (typically in a
    try/finally in :mod:`reddit_research.mcp.__main__`).
    """
    server = build_server(ops, budget=budget)
    server.run(transport=transport)


def make_default_budget(
    *, max_api_calls: int = 50, max_comments: int = 500
) -> WorkflowBudget:
    """Convenience for the entry point — keeps the FastMCP-server file
    self-contained and the __main__ stub small."""
    return WorkflowBudget(
        max_api_calls=int(max_api_calls),
        max_comments=int(max_comments),
    )
