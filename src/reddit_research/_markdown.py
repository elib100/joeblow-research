"""Internal: dataclass → markdown formatter for LLM consumption.

Phase 0 measured signal-to-noise of ~10% in raw Reddit JSON (the rest is
operational metadata Reddit needs but the LLM doesn't). This module
strips that overhead and presents the surviving content as
depth-indented markdown — natively compact, easy to scan, and roughly
10× cheaper in tokens than the equivalent raw JSON for typical
research workloads.

What we keep:
  - Title, body / selftext (verbatim — Reddit already returns markdown)
  - Author (``[deleted]`` if Reddit returned ``None``)
  - Score
  - Subreddit (in metadata; useful when results span multiple subs)
  - Permalink (as a markdown link, so the LLM can cite the source)
  - num_comments (in thread metadata)
  - Fullname (``t1_xxx`` / ``t3_xxx``) — backticked so the LLM can
    reliably extract it for follow-up tool calls (expand_comment,
    get_thread). This is the key reason markdown is usable at all for
    research workflows: without IDs in-context, every drill-down
    requires a separate JSON round-trip.

What we strip:
  - created_utc — rarely useful for summarization
  - upvote_ratio — low signal
  - parent_id — implicit in nesting
  - is_self / depth / id — derivable or unused
  - empty selftext — rendered nothing instead of a blank section

Why this isn't in ``core/``: same reason as ``_serialize.py`` — core
is "no UI/adapter deps". This is presentation transform; CLI and MCP
both consume it.

The dispatch entry point is :func:`to_markdown`; specific shapes get
specialized helpers (``thread_to_markdown``, ``comment_tree_to_markdown``,
etc.) for callers that already know the type.
"""

from __future__ import annotations

from typing import Any, Iterable

from reddit_research.core.operations import (
    CommentSummary,
    CommentTree,
    Status,
    Thread,
    ThreadSummary,
)


# ---- Public entry point ---------------------------------------------------


def to_markdown(payload: Any) -> str:
    """Dispatch a dataclass payload to the right markdown formatter.

    Accepts the four shapes Operations returns: ``list[ThreadSummary]``
    (search/listing), ``Thread`` (post + comments), ``CommentTree``
    (focal + descendants), ``Status``. Anything else is rendered with
    a generic fallback so callers don't have to special-case.

    Returns the formatted markdown without a trailing newline (the
    caller adds one if writing to a stream).
    """
    if isinstance(payload, list):
        # Listing of ThreadSummary (search / get_subreddit_listing).
        if not payload:
            return "_(no results)_"
        return _listing_to_markdown(payload)
    if isinstance(payload, Thread):
        return thread_to_markdown(payload)
    if isinstance(payload, CommentTree):
        return comment_tree_to_markdown(payload)
    if isinstance(payload, ThreadSummary):
        return thread_summary_to_markdown(payload)
    if isinstance(payload, Status):
        return status_to_markdown(payload)
    # Fallback: stringify. Better than raising — preserves a debuggable
    # output if the operations layer adds a new return type and this
    # module hasn't caught up yet.
    return f"```\n{payload!r}\n```"


# ---- Specialized formatters -----------------------------------------------


def thread_summary_to_markdown(t: ThreadSummary) -> str:
    """One-line bullet for a search / listing result.

    Format::

        - **[score]** r/sub · [Title](permalink) — author · `t3_xxx` · N comments
    """
    return _thread_summary_line(t)


def thread_to_markdown(thread: Thread) -> str:
    """A post + its top-level comment forest.

    H1 title + italic metadata line + (selftext if present) + comments
    section. Comments get the same recursive bullet-tree as
    :func:`comment_tree_to_markdown`.
    """
    p = thread.post
    parts: list[str] = [
        f"# {_escape_title(p.title)}",
        "",
        "*"
        + " · ".join([
            f"r/{p.subreddit}",
            f"score={p.score}",
            f"`{p.fullname}`",
            f"{p.num_comments} comments",
            f"author={_author(p.author)}",
            f"[permalink](https://reddit.com{p.permalink})",
        ])
        + "*",
    ]
    if p.is_self and p.selftext:
        parts.extend(["", p.selftext.rstrip()])
    parts.extend(["", "---", "", "## Comments", ""])
    if not thread.comments:
        parts.append("_(no comments)_")
    else:
        parts.extend(_comments_block(thread.comments, depth=0))
    return "\n".join(parts)


def comment_tree_to_markdown(tree: CommentTree) -> str:
    """A focal comment + its reply subtree (the result of ``expand_comment``)."""
    return "\n".join(
        ["## Comment subtree", "", *_comments_block((tree.root,), depth=0)]
    )


def status_to_markdown(s: Status) -> str:
    """Status snapshot, rendered as a small fenced block.

    Status is rare in markdown context (the LLM usually wants this raw
    to make routing decisions), but supporting it keeps the dispatch
    total — easier than telling callers to special-case Status.
    """
    lines: list[str] = ["## Status", ""]
    lines.append(f"- cache: {s.cache_row_count} rows, {s.cache_db_size_bytes:,} bytes")
    if s.cache_hit_rate is not None:
        lines.append(
            f"- hit rate: {s.cache_hit_rate:.1%} "
            f"({s.cache_hits} hits / {s.cache_misses} misses)"
        )
    else:
        lines.append("- hit rate: n/a")
    lines.append(f"- writes this session: {s.cache_writes}")
    lines.append(f"- api calls this session: {s.total_api_calls}")
    lines.append(f"- last call status: {s.last_call_status}")
    if s.headroom:
        lines.append(
            f"- rate-limit remaining: {s.headroom.get('remaining')} "
            f"(reset in {s.headroom.get('reset_in_seconds')}s)"
        )
    if s.workflow_budget:
        b = s.workflow_budget
        lines.append(
            f"- budget: {b['api_calls']}/{b['max_api_calls']} api_calls, "
            f"{b['comments']}/{b['max_comments']} comments"
        )
    return "\n".join(lines)


# ---- Internals ------------------------------------------------------------


def _listing_to_markdown(threads: Iterable[ThreadSummary]) -> str:
    return "\n".join(_thread_summary_line(t) for t in threads)


def _thread_summary_line(t: ThreadSummary) -> str:
    return (
        f"- **[{t.score}]** r/{t.subreddit} · "
        f"[{_escape_title(t.title)}](https://reddit.com{t.permalink}) — "
        f"{_author(t.author)} · `{t.fullname}` · {t.num_comments} comments"
    )


def _comments_block(
    comments: Iterable[CommentSummary], depth: int
) -> list[str]:
    """Render an iterable of CommentSummary as a flat list of markdown lines.

    Recurses into ``replies``. Each comment occupies one or more lines:
    the header line (bullet + score + author + fullname + first line of
    body) followed by zero or more continuation lines for multi-line
    bodies, then any nested replies indented one level deeper.

    Indentation rule (CommonMark): each nesting level adds 2 spaces to
    the bullet's indent; body continuation lines align to the bullet's
    *content* column, which is bullet_indent + 2.
    """
    lines: list[str] = []
    for c in comments:
        lines.extend(_comment_lines(c, depth))
    return lines


def _comment_lines(c: CommentSummary, depth: int) -> list[str]:
    bullet_indent = "  " * depth
    content_indent = bullet_indent + "  "
    body = (c.body or "").strip()
    header_prefix = f"{bullet_indent}- **[{c.score}]** {_author(c.author)} · `{c.fullname}`:"

    if not body:
        # Removed/empty body — keep the metadata line so the LLM still
        # sees the comment exists.
        first_line = f"{header_prefix} _(empty)_"
        body_continuation: list[str] = []
    else:
        body_lines = body.split("\n")
        first_line = f"{header_prefix} {body_lines[0]}"
        # Continuation lines aligned with the bullet's content column.
        # Blank lines stay blank (a blank line inside a list item makes
        # subsequent lines a new paragraph in the same item, which is
        # what we want for multi-paragraph bodies).
        body_continuation = [
            f"{content_indent}{line}" if line.strip() else ""
            for line in body_lines[1:]
        ]

    out: list[str] = [first_line, *body_continuation]
    for r in c.replies:
        out.extend(_comment_lines(r, depth + 1))
    return out


def _author(raw: str | None) -> str:
    return raw if raw else "[deleted]"


def _escape_title(title: str) -> str:
    """Escape characters that would break inline markdown link text.

    Specifically: brackets ``[ ]`` and pipe ``|`` (the latter only
    matters inside tables, which we don't emit, but cheap to guard).
    Pipes also confuse some renderers when they appear next to bullet
    delimiters. Backslash-escape these.
    """
    return (
        title.replace("\\", "\\\\")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("|", "\\|")
    )
