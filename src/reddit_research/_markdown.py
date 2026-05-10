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

**User content vs. renderer structure** (round-11 panel, Codex P1):
A naive "embed body inline" approach lets a hostile or merely-mistaken
Reddit user spoof renderer markup. A comment body containing
``- **[999]** attacker · \`t1_fake\`: payload`` would render at the
same indentation as a real reply, and because backticked fullnames are
load-bearing for follow-up tool calls (``expand_comment``,
``get_thread``), an LLM consumer could be tricked into calling tools
on attacker-chosen IDs. The fix is structural: all user-authored body
content (comment bodies and post selftext) is emitted inside CommonMark
blockquotes (``> `` prefix). User markup stays inside the blockquote
namespace; renderer-emitted bullets, headings, and separators stay
outside. The LLM-visible rule: "anything inside ``>`` came from a
Reddit user; anything outside came from this renderer."

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
    if p.is_self and p.selftext.strip():
        # Selftext is user-authored — blockquote it so it can't spoof
        # the `---` separator or the `## Comments` header that follow
        # (round-11 Codex P1).
        parts.append("")
        parts.extend(_blockquote_body(p.selftext, content_indent=""))
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

    Recurses into ``replies``. Each comment occupies a header line
    (bullet + score + author + fullname) plus one blockquoted line per
    body line, then any nested replies indented one level deeper.

    Indentation rules (CommonMark):
      - Bullet at depth N is indented ``N*2`` spaces.
      - The bullet's "content column" — where blockquoted body lines
        and nested bullets must start — is ``(N*2) + 2`` spaces.
      - Round-11 panel (Codex P1): body lines are emitted as
        blockquotes (``> `` prefix) at the content column so user-
        authored Reddit markdown can't spoof renderer structure.
    """
    lines: list[str] = []
    for c in comments:
        lines.extend(_comment_lines(c, depth))
    return lines


def _comment_lines(c: CommentSummary, depth: int) -> list[str]:
    bullet_indent = "  " * depth
    content_indent = bullet_indent + "  "
    header = f"{bullet_indent}- **[{c.score}]** {_author(c.author)} · `{c.fullname}`"

    body_lines = _blockquote_body(c.body or "", content_indent=content_indent)
    out: list[str] = [header, *body_lines]
    for r in c.replies:
        out.extend(_comment_lines(r, depth + 1))
    return out


def _blockquote_body(body: str, *, content_indent: str) -> list[str]:
    """Render ``body`` as one or more CommonMark blockquote lines.

    Round-11 panel (Codex P1): user-authored body content lives inside
    ``> `` blockquotes so it can't spoof renderer structure (fake
    ``- **[N]** ... `t1_xxx`:`` bullets, fake ``## Comments`` headers,
    fake ``---`` separators). Renderer markup stays outside the
    blockquote namespace.

    Round-11 panel (Codex P2): the original implementation called
    ``body.strip()`` for the rendered content, which corrupted code
    blocks and other whitespace-sensitive markup. Now ``.strip()`` is
    only used to decide whether the body is empty; the rendered text
    is the raw value with at most a trailing-newline trim.

    Returns a list of full lines (each prefixed with
    ``content_indent + "> "``), or a single ``_(empty)_`` placeholder
    line for removed/empty bodies. Empty input is rendered as the
    placeholder so deletions stay visible to summarization.
    """
    if not body or not body.strip():
        return [f"{content_indent}> _(empty)_"]
    raw_lines = body.rstrip("\n").splitlines() or [""]
    return [
        # CommonMark: ``> `` produces a blockquote; ``>`` alone (no
        # trailing space) keeps blank lines inside the same blockquote
        # without introducing a paragraph break artifact.
        f"{content_indent}> {line}" if line else f"{content_indent}>"
        for line in raw_lines
    ]


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
