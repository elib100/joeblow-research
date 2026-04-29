"""Cache key normalization + ``id`` / ``fullname`` validation.

Cache keys MUST encode every retrieval parameter that affects the response
shape, plus a ``normalization_version`` (the ``:v1`` suffix). Round-2 panel
review caught a P1 bug where partial views (e.g. ``get_thread(top=20)``)
silently cache-poisoned wider requests because the key was just the thread
fullname. Don't regress.
"""

from __future__ import annotations

import json
import re
from typing import Any


# Empirically verified in Phase 0: these are the prefixes Reddit uses.
#   t1 = comment, t2 = user, t3 = post, t4 = message, t5 = subreddit, t6 = award
_FULLNAME_RE = re.compile(r"^t[1-6]_[a-z0-9]+$")
_BARE_ID_RE = re.compile(r"^[a-z0-9]+$")

NORMALIZATION_VERSION = "v1"


def validate_fullname(fullname: str, expect_kind: str | None = None) -> str:
    """Validate a Reddit fullname like ``t3_abc123``. Returns it unchanged.

    Args:
        fullname: the fullname to validate.
        expect_kind: optional kind prefix (``"t1"``, ``"t3"``, etc.) to enforce.

    Raises:
        InvalidIdError: if the fullname doesn't match the pattern, or if
            ``expect_kind`` is given and doesn't match.
    """
    from reddit_research.core.errors import InvalidIdError

    if not isinstance(fullname, str) or not _FULLNAME_RE.match(fullname):
        raise InvalidIdError(f"not a valid Reddit fullname: {fullname!r}")
    if expect_kind is not None and not fullname.startswith(expect_kind + "_"):
        raise InvalidIdError(
            f"expected fullname kind {expect_kind!r} but got {fullname!r}"
        )
    return fullname


def validate_id_or_fullname(value: str, expect_kind: str | None = None) -> str:
    """Validate a Reddit id (bare ``abc123``) or fullname (``t3_abc123``).

    Returns the *bare id*, stripping any prefix. Used by operations that
    accept either form for ergonomics.
    """
    from reddit_research.core.errors import InvalidIdError

    if not isinstance(value, str):
        raise InvalidIdError(f"not a string: {value!r}")
    if _FULLNAME_RE.match(value):
        validate_fullname(value, expect_kind=expect_kind)
        return value.split("_", 1)[1]
    if _BARE_ID_RE.match(value):
        return value
    raise InvalidIdError(f"not a valid Reddit id or fullname: {value!r}")


def to_fullname(bare_id: str, kind: str) -> str:
    """Convert a bare id like ``abc123`` to a fullname like ``t3_abc123``.

    The ``kind`` argument is one of ``t1``..``t6``. Validates both inputs.
    """
    from reddit_research.core.errors import InvalidIdError

    if not _BARE_ID_RE.match(bare_id or ""):
        raise InvalidIdError(f"not a valid Reddit bare id: {bare_id!r}")
    if not re.match(r"^t[1-6]$", kind):
        raise InvalidIdError(f"not a valid Reddit kind prefix: {kind!r}")
    return f"{kind}_{bare_id}"


# ---- Cache key builders ---------------------------------------------------


def search_key(
    query: str,
    subreddit: str | None,
    sort: str,
    time_filter: str,
    limit: int,
) -> str:
    """Canonical search cache key.

    Includes every parameter that affects the result. Defaults are NOT stripped
    here — keep all params present so two callers writing the same query in
    different ways collide on the same cache entry. Subreddit is lowercased
    for canonicalization.
    """
    payload = {
        "query": query,
        "subreddit": (subreddit or "").lower() or None,
        "sort": sort,
        "time": time_filter,
        "limit": int(limit),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return f"search:{canonical}:{NORMALIZATION_VERSION}"


def listing_key(name: str, sort: str, limit: int, time_filter: str | None = None) -> str:
    """Subreddit listing cache key."""
    sub = (name or "").lower()
    parts = [f"listing:r/{sub}", str(sort), f"l{int(limit)}"]
    if time_filter:
        parts.append(f"t{time_filter}")
    parts.append(NORMALIZATION_VERSION)
    return ":".join(parts)


def thread_key(thread_id: str, top_n_comments: int) -> str:
    """``get_thread`` cache key.

    The ``top_n_comments`` parameter affects what's in the response (Reddit's
    ``limit`` param truncates top-level comments), so it's part of the key.
    """
    bare = validate_id_or_fullname(thread_id, expect_kind="t3")
    return f"thread:t3_{bare}:top{int(top_n_comments)}:{NORMALIZATION_VERSION}"


def comment_subtree_key(
    thread_id: str,
    comment_id: str,
    depth: int,
    limit: int,
) -> str:
    """``expand_comment`` cache key. Both ids needed because the URL needs
    both, and the same comment under two different threads is a different
    request."""
    t = validate_id_or_fullname(thread_id, expect_kind="t3")
    c = validate_id_or_fullname(comment_id, expect_kind="t1")
    return (
        f"comment_subtree:t3_{t}:t1_{c}"
        f":d{int(depth)}:l{int(limit)}:{NORMALIZATION_VERSION}"
    )


# ---- Misc helpers ---------------------------------------------------------


def stable_canonical_json(obj: Any) -> str:
    """Sorted-key, no-whitespace JSON. Useful for any place we need a stable
    string representation of a dict for hashing or comparison."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))
