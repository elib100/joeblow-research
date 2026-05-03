"""User-facing operations.

Wraps :class:`reddit_research.core.client.RedditJSONClient` +
:class:`reddit_research.core.cache.Cache` into the six operations the CLI and
(future) MCP server expose:

- :meth:`Operations.search`
- :meth:`Operations.get_subreddit_listing`
- :meth:`Operations.get_thread`
- :meth:`Operations.expand_comment`
- :meth:`Operations.purge`
- :meth:`Operations.status`

Each read operation follows the same shape::

    1. Validate inputs (via core.keys validators).
    2. Build cache key (canonical form including all retrieval params).
    3. Unless `fresh=True`, look up the cache. On a 200 hit, parse and return.
       On a 403/404 hit, re-raise the original error.
    4. On miss / fresh: call client.get(). Parse the response. Cache the body
       (200 → put, 403/404 → put_error). Charge comment-budget if applicable.
       Return.
    5. Transient (5xx / network) errors propagate without caching.

Returned types are typed dataclasses (ThreadSummary / CommentSummary /
Thread / CommentTree). The CLI / MCP layer serialize via dataclasses.asdict()
when they need JSON.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

from reddit_research.core.cache import Cache, CacheStats
from reddit_research.core.client import RedditJSONClient
from reddit_research.core.config import Config
from reddit_research.core.errors import (
    ForbiddenError,
    HTTPError,
    NotFoundError,
    from_http_response,
)
from reddit_research.core.keys import (
    comment_subtree_key,
    listing_key,
    search_key,
    thread_key,
    validate_id_or_fullname,
)


# Round-5 panel: per-call hard caps on expand_comment so a single LLM call
# can't ask for arbitrarily deep / wide expansion.
EXPAND_DEPTH_MAX = 5
EXPAND_LIMIT_MAX = 50


# ---- Result types ---------------------------------------------------------


@dataclass(frozen=True)
class ThreadSummary:
    """A single post from a Listing — search results, hot/top/etc."""

    fullname: str         # 't3_<id>'
    id: str               # bare id
    subreddit: str
    title: str
    author: str | None    # None when '[deleted]' or absent
    score: int
    upvote_ratio: float | None
    num_comments: int
    permalink: str
    created_utc: float
    is_self: bool
    selftext: str         # empty for link posts


@dataclass(frozen=True)
class CommentSummary:
    """A single comment, with optional nested replies."""

    fullname: str         # 't1_<id>'
    id: str
    body: str
    author: str | None
    score: int
    created_utc: float
    parent_id: str        # 't1_X' or 't3_X'
    depth: int            # 0 for top-level
    # Nested replies in tree form. Empty tuple if no replies returned. Note:
    # this only includes comments Reddit returned in the response — `more`
    # markers are silently dropped at parse time. Operations that care about
    # full coverage call expand_comment() to fetch deeper trees.
    replies: tuple[CommentSummary, ...] = ()


@dataclass(frozen=True)
class Thread:
    post: ThreadSummary
    comments: tuple[CommentSummary, ...]   # top-level CommentForest


@dataclass(frozen=True)
class CommentTree:
    root: CommentSummary                   # the focal comment
    # `root` already has its replies populated (depth-N), so this is a thin
    # wrapper around CommentSummary. Kept as a distinct return type so the
    # CLI / MCP can label outputs differently.


@dataclass(frozen=True)
class Status:
    cache_db_size_bytes: int
    cache_row_count: int
    cache_hit_rate: float | None
    cache_hits: int
    cache_misses: int
    cache_writes: int
    headroom: dict | None
    total_api_calls: int
    last_call_status: int | None
    workflow_budget: dict | None = None


# ---- Operations ----------------------------------------------------------


class Operations:
    """User-facing API. Owns refs to a client + a cache; plumbs them through
    the six MVP operations.
    """

    def __init__(self, client: RedditJSONClient, cache: Cache) -> None:
        self._client = client
        self._cache = cache

    # ---- factory ----

    @classmethod
    def from_config(cls, config: Config, *, budget: object | None = None) -> Operations:
        """Convenience: build a default-configured Operations from a Config."""
        client = RedditJSONClient(
            user_agent=config.user_agent,
            budget=budget,  # type: ignore[arg-type]
        )
        cache = Cache(config.cache_db_path)
        return cls(client=client, cache=cache)

    def close(self) -> None:
        self._client.close()
        self._cache.close()

    def __enter__(self) -> Operations:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ---- read operations ----

    def search(
        self,
        query: str,
        subreddit: str | None = None,
        sort: str = "relevance",
        time_filter: str = "all",
        limit: int = 25,
        *,
        fresh: bool = False,
    ) -> list[ThreadSummary]:
        """Search Reddit. ``subreddit=None`` means search-all."""
        key = search_key(query, subreddit, sort, time_filter, limit)
        if not fresh:
            hit = self._cache.get("search", key)
            if hit is not None:
                self._raise_if_cached_error(hit, key)
                return _parse_listing_children(hit.body)

        if subreddit:
            path = f"/r/{subreddit}/search.json"
            params: dict[str, Any] = {
                "q": query,
                "restrict_sr": 1,
                "sort": sort,
                "t": time_filter,
                "limit": int(limit),
            }
        else:
            path = "/search.json"
            params = {
                "q": query,
                "sort": sort,
                "t": time_filter,
                "limit": int(limit),
            }

        body = self._call_and_cache("search", key, path, params)
        return _parse_listing_children(body)

    def get_subreddit_listing(
        self,
        name: str,
        sort: str = "hot",
        limit: int = 25,
        time_filter: str = "all",
        *,
        fresh: bool = False,
    ) -> list[ThreadSummary]:
        """Fetch a subreddit listing — hot/new/top/rising/controversial.

        ``time_filter`` only affects ``top`` and ``controversial`` sorts;
        Reddit ignores it on others. We omit it from both the API request
        AND the cache key for irrelevant sorts so two callers spelling the
        same query differently collide on one entry (round-7 panel).
        """
        sort_uses_time = sort in ("top", "controversial")
        effective_time_filter = time_filter if sort_uses_time else None
        key = listing_key(name, sort, limit, effective_time_filter)
        if not fresh:
            hit = self._cache.get("listing", key)
            if hit is not None:
                self._raise_if_cached_error(hit, key)
                return _parse_listing_children(hit.body)

        params: dict[str, Any] = {"limit": int(limit)}
        if sort_uses_time:
            params["t"] = time_filter
        path = f"/r/{name.lower()}/{sort}.json"

        body = self._call_and_cache("listing", key, path, params)
        return _parse_listing_children(body)

    def get_thread(
        self,
        thread_id: str,
        top_n_comments: int = 20,
        *,
        fresh: bool = False,
    ) -> Thread:
        """Fetch a thread + the first ``top_n_comments`` top-level comments
        Reddit returns (Reddit's own ranking — typically ``best``/``confidence``).

        ``thread_id`` may be a bare id (``abc123``) or fullname (``t3_abc123``).
        Round-7 panel: the previous "top by score" framing was misleading
        because Reddit's ``limit`` truncates server-side using its own ranking,
        so re-sorting the truncated subset doesn't reveal higher-scored
        comments outside Reddit's first-N window. Caller can sort the
        returned ``comments`` tuple if a specific order is needed.
        """
        key = thread_key(thread_id, top_n_comments)
        bare = validate_id_or_fullname(thread_id)

        if not fresh:
            hit = self._cache.get("thread", key)
            if hit is not None:
                self._raise_if_cached_error(hit, key)
                return _parse_thread(hit.body, top_n_comments)

        path = f"/comments/{bare}.json"
        params: dict[str, Any] = {"limit": int(top_n_comments)}

        body = self._call_and_cache("thread", key, path, params)
        thread = _parse_thread(body, top_n_comments)
        # Round-5 panel: charge comment budget after parsing — the operations
        # layer is the only place that knows the comment count.
        if self._client.budget is not None:
            self._client.budget.spend_comments(len(thread.comments))
        return thread

    def expand_comment(
        self,
        subreddit: str,
        thread_id: str,
        comment_id: str,
        depth: int = 2,
        limit: int = 20,
        *,
        fresh: bool = False,
    ) -> CommentTree:
        """Fetch a single comment + its reply subtree (up to ``depth`` levels).

        Per-call hard caps: ``depth ≤ 5``, ``limit ≤ 50`` (round-5 panel).
        ``subreddit`` is required because Reddit's endpoint URL needs it; it's
        in the response of any prior :meth:`get_thread` call.
        """
        if depth > EXPAND_DEPTH_MAX or depth < 0:
            raise ValueError(
                f"depth must be in [0, {EXPAND_DEPTH_MAX}]; got {depth}"
            )
        if limit > EXPAND_LIMIT_MAX or limit < 1:
            raise ValueError(
                f"limit must be in [1, {EXPAND_LIMIT_MAX}]; got {limit}"
            )

        key = comment_subtree_key(thread_id, comment_id, depth, limit)
        # Accept bare or fullname (CLAUDE.md API contract). Wrong-kind IDs
        # surface as Reddit 404/403 rather than a local validation error —
        # acceptable trade-off for ergonomics.
        bare_thread = validate_id_or_fullname(thread_id)
        bare_comment = validate_id_or_fullname(comment_id)
        sub = subreddit.lower()

        if not fresh:
            hit = self._cache.get("comment_subtree", key)
            if hit is not None:
                self._raise_if_cached_error(hit, key)
                return _parse_comment_tree(hit.body, focal_comment_id=bare_comment)

        path = f"/r/{sub}/comments/{bare_thread}/_/{bare_comment}.json"
        # context=0 returns the focal subtree only — no parent/sibling leak.
        # Round-5 panel verified empirically.
        params: dict[str, Any] = {"limit": int(limit), "depth": int(depth), "context": 0}

        body = self._call_and_cache("comment_subtree", key, path, params)
        tree = _parse_comment_tree(body, focal_comment_id=bare_comment)
        if self._client.budget is not None:
            # Count root + recursive replies
            self._client.budget.spend_comments(_count_comments(tree.root))
        return tree

    # ---- maintenance ----

    def purge(self, older_than_days: int = 30) -> int:
        """Delete cache rows older than N days. Returns rows deleted."""
        return self._cache.purge(older_than_seconds=int(older_than_days) * 86400)

    def status(self) -> Status:
        """Snapshot of cache + transport state."""
        cs: CacheStats = self._cache.stats()
        return Status(
            cache_db_size_bytes=cs.db_size_bytes,
            cache_row_count=cs.row_count,
            cache_hit_rate=cs.hit_rate,
            cache_hits=cs.hits,
            cache_misses=cs.misses,
            cache_writes=cs.writes,
            headroom=self._client.headroom,
            total_api_calls=(
                self._client.budget.api_calls
                if self._client.budget is not None
                else 0
            ),
            last_call_status=self._client.last_call_status,
            workflow_budget=(
                self._client.budget.snapshot()
                if self._client.budget is not None
                else None
            ),
        )

    # ---- internals ----

    def _call_and_cache(
        self,
        kind: str,
        key: str,
        path: str,
        params: dict[str, Any],
    ) -> Any:
        """Issue a GET, cache result (200 → put, 403/404 → put_error), return body.

        Cache writes are best-effort: a cache failure (disk full, schema
        mismatch, etc.) is logged but never blocks the original transport
        outcome from propagating to the caller. This is the round-7 panel
        finding: caching is observability/optimization; it must not change
        operation semantics.

        Transient errors (5xx, transport, redirect, budget-exceeded) are
        re-raised without being cached.
        """
        try:
            body = self._client.get(path, params=params)
        except (NotFoundError, ForbiddenError) as e:
            try:
                self._cache.put_error(
                    kind, key, e.status, _error_body_for_cache(e, path)
                )
            except Exception as cache_exc:
                logger.warning(
                    "cache.put_error failed for %s %s: %s",
                    kind, key, cache_exc,
                )
            raise
        try:
            self._cache.put(kind, key, body, status_code=200)
        except Exception as cache_exc:
            logger.warning(
                "cache.put failed for %s %s: %s",
                kind, key, cache_exc,
            )
        return body

    def _raise_if_cached_error(self, hit, key: str) -> None:
        """If a cache hit holds a 403/404, replay the original error.

        The cached error body includes the original request path (stored by
        ``_error_body_for_cache``) so the replayed exception's ``.path`` is
        the URL the user requested, not the cache key.
        """
        if hit.status_code == 200:
            return
        body = hit.body if isinstance(hit.body, dict) else {}
        original_path = body.get("_request_path") or key
        # Pass only the discriminator fields to from_http_response — the
        # ``_request_path`` key is internal cache metadata and shouldn't
        # show up in the exception body.
        replay_body = {k: v for k, v in body.items() if not k.startswith("_")}
        raise from_http_response(hit.status_code, original_path, replay_body)


# ---- Parsers --------------------------------------------------------------


def _parse_listing_children(body: Any) -> list[ThreadSummary]:
    """Parse a Listing response into ThreadSummary list.

    Round-7 panel: raise ``ValueError`` on shape mismatch rather than
    silently returning ``[]``. Silent degradation is harder to detect than
    a loud failure, and a bad 200 cached for the full TTL is worse than a
    crash that surfaces a Reddit schema change. Individual non-t3 children
    are skipped (mixed listings can include sub/user kinds), but a missing
    ``data.children`` array is a hard error.
    """
    if not isinstance(body, dict):
        raise ValueError(
            f"listing response not a dict: type={type(body).__name__}"
        )
    data = body.get("data")
    if not isinstance(data, dict) or "children" not in data:
        raise ValueError(
            "listing response missing data.children — possible Reddit schema change"
        )
    children = data.get("children", [])
    if not isinstance(children, list):
        raise ValueError(
            f"listing data.children not a list: type={type(children).__name__}"
        )
    return [
        _thread_summary(c["data"])
        for c in children
        if isinstance(c, dict) and c.get("kind") == "t3" and isinstance(c.get("data"), dict)
    ]


def _parse_thread(body: Any, top_n_comments: int) -> Thread:
    """Parse a /comments/<id>.json response: ``[post_listing, comment_listing]``.

    Returns top-level comments in **Reddit's order** (typically
    ``best``/``confidence`` ranking). Truncated to at most ``top_n_comments``.
    Round-7 panel: do NOT re-sort by score — Reddit already truncated by its
    own ranking, so a local re-sort can't reveal higher-scored comments
    outside Reddit's first-N window. Caller sorts if needed.
    """
    if not (isinstance(body, list) and len(body) >= 2):
        raise ValueError(
            f"unexpected thread response shape: type={type(body).__name__} "
            f"len={len(body) if hasattr(body, '__len__') else '?'}"
        )
    post_children = body[0].get("data", {}).get("children", [])
    if not post_children or post_children[0].get("kind") != "t3":
        raise ValueError("thread response missing post element")
    post = _thread_summary(post_children[0]["data"])

    comment_children = body[1].get("data", {}).get("children", [])
    top_level = tuple(
        _comment_summary(c["data"], depth=0)
        for c in comment_children
        if isinstance(c, dict) and c.get("kind") == "t1" and isinstance(c.get("data"), dict)
    )[:top_n_comments]
    return Thread(post=post, comments=top_level)


def _parse_comment_tree(body: Any, focal_comment_id: str) -> CommentTree:
    """Parse a /r/<sub>/comments/<thread>/_/<comment>.json response.

    The response is the same ``[post_listing, comment_listing]`` shape as a
    thread fetch. With ``context=0`` the comment_listing should contain the
    focal comment as its single top-level entry, with replies nested.
    """
    if not (isinstance(body, list) and len(body) >= 2):
        raise ValueError(
            f"unexpected expand response shape: type={type(body).__name__}"
        )
    children = body[1].get("data", {}).get("children", [])
    focal: CommentSummary | None = None
    for c in children:
        if not isinstance(c, dict) or c.get("kind") != "t1":
            continue
        d = c.get("data", {})
        if d.get("id") == focal_comment_id:
            focal = _comment_summary(d, depth=0)
            break
    if focal is None:
        # Shouldn't happen with context=0 — log via raise so callers see it.
        raise ValueError(
            f"focal comment {focal_comment_id!r} not found in expand response; "
            f"possible Reddit shape change or wrong ids"
        )
    return CommentTree(root=focal)


def _thread_summary(d: dict) -> ThreadSummary:
    return ThreadSummary(
        fullname=f"t3_{d.get('id', '')}",
        id=str(d.get("id", "")),
        subreddit=str(d.get("subreddit", "")),
        title=str(d.get("title", "")),
        author=_author(d.get("author")),
        score=int(d.get("score", 0) or 0),
        upvote_ratio=_optional_float(d.get("upvote_ratio")),
        num_comments=int(d.get("num_comments", 0) or 0),
        permalink=str(d.get("permalink", "")),
        created_utc=float(d.get("created_utc", 0) or 0),
        is_self=bool(d.get("is_self", False)),
        selftext=str(d.get("selftext", "") or ""),
    )


def _comment_summary(d: dict, depth: int) -> CommentSummary:
    replies_raw = d.get("replies")
    nested: tuple[CommentSummary, ...] = ()
    if isinstance(replies_raw, dict):
        nested = tuple(
            _comment_summary(r["data"], depth + 1)
            for r in replies_raw.get("data", {}).get("children", [])
            if isinstance(r, dict) and r.get("kind") == "t1" and isinstance(r.get("data"), dict)
        )
    return CommentSummary(
        fullname=f"t1_{d.get('id', '')}",
        id=str(d.get("id", "")),
        body=str(d.get("body", "") or ""),
        author=_author(d.get("author")),
        score=int(d.get("score", 0) or 0),
        created_utc=float(d.get("created_utc", 0) or 0),
        parent_id=str(d.get("parent_id", "")),
        depth=depth,
        replies=nested,
    )


def _author(raw: Any) -> str | None:
    """Reddit returns '[deleted]' for deleted-author posts/comments."""
    if raw is None or raw == "[deleted]":
        return None
    return str(raw)


def _optional_float(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _count_comments(c: CommentSummary) -> int:
    """Recursively count a comment + all its nested replies."""
    return 1 + sum(_count_comments(r) for r in c.replies)


def _error_body_for_cache(e: HTTPError, request_path: str) -> dict:
    """Distill a typed HTTPError into the dict shape we cache.

    We don't cache the raw HTTP body — just the discriminator fields (reason,
    message) plus the status. The original request path is stored under the
    underscore-prefixed ``_request_path`` key so the replayed exception's
    ``.path`` attribute is the URL the user requested, not the cache key
    (round-7 panel).
    """
    return {
        "reason": e.reason,
        "message": e.message,
        "_request_path": request_path,
    }
