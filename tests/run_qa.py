#!/usr/bin/env python3
"""Custom QA suite for reddit_research. Not pytest — runs imperatively per
the portfolio convention. Run from project root: ``python tests/run_qa.py``.

Tests are pure-Python: mocked HTTP via ``httpx.MockTransport`` + tmpdir
SQLite. No live Reddit calls. Includes regression tests for the bugs each
panel round caught (round 5 / 6 / 7).
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
import time
import traceback
from pathlib import Path

import httpx

from reddit_research.core import (
    BudgetExceededError,
    Cache,
    ForbiddenError,
    InvalidIdError,
    NotFoundError,
    Operations,
    RateLimitError,
    RedditJSONClient,
    RedirectError,
    UpstreamError,
    WorkflowBudget,
)
from reddit_research.core.errors import from_http_response
from reddit_research.core.keys import (
    comment_subtree_key,
    listing_key,
    search_key,
    thread_key,
    to_fullname,
    validate_id_or_fullname,
)


# ---- Tiny test framework -------------------------------------------------


_TESTS: list = []
_RESULTS = {"passed": 0, "failed": 0, "errors": []}


def test(fn):
    _TESTS.append(fn)
    return fn


def assert_(cond, msg=""):
    if not cond:
        raise AssertionError(msg or "assert failed")


def assert_eq(a, b, msg=""):
    if a != b:
        raise AssertionError(msg or f"{a!r} != {b!r}")


def assert_raises(exc_type, fn, *args, **kw):
    try:
        fn(*args, **kw)
    except exc_type:
        return
    except Exception as e:
        raise AssertionError(
            f"expected {exc_type.__name__}, got {type(e).__name__}: {e}"
        )
    raise AssertionError(f"expected {exc_type.__name__} not raised")


def run() -> int:
    section = ""
    for t in _TESTS:
        new_section = t.__name__.split("_")[1] if "_" in t.__name__ else "misc"
        if new_section != section:
            print(f"\n[{new_section}]")
            section = new_section
        try:
            t()
            _RESULTS["passed"] += 1
            print(f"  ok    {t.__name__}")
        except Exception as e:
            _RESULTS["failed"] += 1
            _RESULTS["errors"].append((t.__name__, e, traceback.format_exc()))
            print(f"  FAIL  {t.__name__}: {e}")

    print()
    print(f"=== {_RESULTS['passed']} passed, {_RESULTS['failed']} failed ===")
    if _RESULTS["failed"]:
        print()
        for name, _, tb in _RESULTS["errors"]:
            print(f"\n--- {name} ---")
            print(tb)
    return 0 if _RESULTS["failed"] == 0 else 1


# ---- keys / validation ---------------------------------------------------


@test
def test_keys_validate_bare_id():
    assert_eq(validate_id_or_fullname("abc123"), "abc123")
    # Round-7 fix: bare id with expect_kind passes (silently — no kind to verify)
    assert_eq(validate_id_or_fullname("abc123", expect_kind="t3"), "abc123")
    assert_raises(InvalidIdError, validate_id_or_fullname, "ABC")
    assert_raises(InvalidIdError, validate_id_or_fullname, "")
    assert_raises(InvalidIdError, validate_id_or_fullname, 123)


@test
def test_keys_validate_fullname():
    assert_eq(validate_id_or_fullname("t3_abc"), "abc")
    assert_eq(validate_id_or_fullname("t1_xyz", expect_kind="t1"), "xyz")
    assert_raises(InvalidIdError, validate_id_or_fullname, "t1_abc", expect_kind="t3")
    assert_raises(InvalidIdError, validate_id_or_fullname, "t9_abc")


@test
def test_keys_to_fullname():
    assert_eq(to_fullname("abc", "t3"), "t3_abc")
    assert_raises(InvalidIdError, to_fullname, "abc", "t9")
    assert_raises(InvalidIdError, to_fullname, "ABC", "t3")


@test
def test_keys_search_canonical():
    a = search_key("foo", "python", "relevance", "all", 25)
    b = search_key("foo", "PYTHON", "relevance", "all", 25)
    assert_eq(a, b, "subreddit case must normalize")
    assert_(":v1" in a)


@test
def test_keys_listing_omits_time_for_hot():
    # Round-7 fix: hot listings do NOT include time segment in cache key
    k_hot = listing_key("python", "hot", 25)
    k_hot_explicit_none = listing_key("python", "hot", 25, None)
    assert_eq(k_hot, k_hot_explicit_none, "None time should produce same key as omitted")
    assert_(":t" not in k_hot.replace(":top", "").replace(":t3_", ""),
            f"hot key should not have :t<filter> segment; got {k_hot}")
    k_top = listing_key("python", "top", 25, "all")
    assert_(":tall:" in k_top, f"top key should include time; got {k_top}")


@test
def test_keys_thread_with_bare_or_fullname():
    k_bare = thread_key("abc123", 20)
    k_full = thread_key("t3_abc123", 20)
    assert_eq(k_bare, k_full)
    assert_eq(k_bare, "thread:t3_abc123:top20:v1")


@test
def test_keys_comment_subtree():
    assert_eq(
        comment_subtree_key("abc", "xyz", 2, 20),
        "comment_subtree:t3_abc:t1_xyz:d2:l20:v1",
    )


# ---- errors / from_http_response -----------------------------------------


@test
def test_errors_404_no_reason():
    e = from_http_response(404, "/r/x.json", {"message": "Not Found", "error": 404})
    assert_(isinstance(e, NotFoundError))
    assert_eq(e.path, "/r/x.json")
    assert_eq(e.message, "Not Found")
    assert_(e.reason is None)


@test
def test_errors_403_with_reason():
    e = from_http_response(403, "/r/lounge.json", {"reason": "gold_only", "message": "Forbidden"})
    assert_(isinstance(e, ForbiddenError))
    assert_eq(e.reason, "gold_only")


@test
def test_errors_429_retry_after():
    h = httpx.Headers({"Retry-After": "30"})
    e = from_http_response(429, "/x", {}, headers=h)
    assert_(isinstance(e, RateLimitError))
    assert_eq(e.retry_after, 30.0)


@test
def test_errors_429_keeps_reason_message():
    # Round-6 fix: RateLimitError forwards reason/message
    e = from_http_response(429, "/x", {"reason": "test", "message": "rate"}, headers={})
    assert_eq(e.reason, "test")
    assert_eq(e.message, "rate")


@test
def test_errors_429_case_insensitive_retry_after():
    # Round-6 fix: Retry-After lookup must work whether httpx returns canonical case or lowercase
    e_lower = from_http_response(429, "/x", {}, headers=httpx.Headers({"retry-after": "5"}))
    e_canon = from_http_response(429, "/x", {}, headers=httpx.Headers({"Retry-After": "5"}))
    assert_eq(e_lower.retry_after, 5.0)
    assert_eq(e_canon.retry_after, 5.0)


@test
def test_errors_redirect():
    h = httpx.Headers({"Location": "/elsewhere"})
    e = from_http_response(302, "/x", {}, headers=h)
    assert_(isinstance(e, RedirectError))
    assert_eq(e.location, "/elsewhere")


@test
def test_errors_5xx_upstream():
    e = from_http_response(503, "/x", {})
    assert_(isinstance(e, UpstreamError))


# ---- cache --------------------------------------------------------------


@test
def test_cache_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        with Cache(Path(td) / "c.db") as cache:
            cache.put("thread", "k", {"x": 1})
            hit = cache.get("thread", "k")
            assert_(hit is not None)
            assert_eq(hit.body, {"x": 1})
            assert_eq(hit.status_code, 200)


@test
def test_cache_ttl_expiry():
    with tempfile.TemporaryDirectory() as td:
        with Cache(Path(td) / "c.db") as cache:
            now = int(time.time())
            cache.put("search", "k", {"x": 1}, now=now)
            # 14 min: within search TTL (15 min)
            assert_(cache.get("search", "k", now=now + 14 * 60) is not None)
            # 16 min: stale
            assert_(cache.get("search", "k", now=now + 16 * 60) is None)


@test
def test_cache_error_caching():
    with tempfile.TemporaryDirectory() as td:
        with Cache(Path(td) / "c.db") as cache:
            cache.put_error("thread", "k", 404, {"message": "Not Found"})
            hit = cache.get("thread", "k")
            assert_(hit is not None)
            assert_eq(hit.status_code, 404)
            assert_raises(ValueError, cache.put_error, "thread", "k", 503, {})
            assert_raises(ValueError, cache.put_error, "thread", "k", 200, {})
            assert_raises(ValueError, cache.put, "thread", "k", {}, status_code=404)


@test
def test_cache_unknown_kind():
    with tempfile.TemporaryDirectory() as td:
        with Cache(Path(td) / "c.db") as cache:
            assert_raises(ValueError, cache.get, "bogus", "k")
            assert_raises(ValueError, cache.put, "bogus", "k", {})


@test
def test_cache_purge():
    with tempfile.TemporaryDirectory() as td:
        with Cache(Path(td) / "c.db") as cache:
            now = int(time.time())
            cache.put("thread", "k1", {"x": 1}, now=now - 7200)
            cache.put("thread", "k2", {"x": 2}, now=now)
            assert_eq(cache.purge(older_than_seconds=3600, now=now), 1)


@test
def test_cache_schema_mismatch_raises():
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "c.db"
        conn = sqlite3.connect(str(path))
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute("INSERT INTO meta VALUES ('schema_version', '999')")
        conn.commit()
        conn.close()
        with Cache(path) as cache:
            assert_raises(RuntimeError, cache.get, "thread", "k")


# ---- workflow budget ----------------------------------------------------


@test
def test_budget_api_calls_cap():
    b = WorkflowBudget(max_api_calls=2, max_comments=100)
    b.spend_api_call()
    b.spend_api_call()
    assert_raises(BudgetExceededError, b.spend_api_call)


@test
def test_budget_comments_cap():
    b = WorkflowBudget(max_api_calls=100, max_comments=10)
    b.spend_comments(5)
    b.spend_comments(5)
    assert_raises(BudgetExceededError, b.spend_comments, 1)


# ---- client (mocked transport) ------------------------------------------


def _mock_client(handler, budget=None) -> RedditJSONClient:
    """Build a RedditJSONClient that uses httpx.MockTransport.

    The RedditJSONClient constructor doesn't accept a transport arg, so we
    swap the underlying httpx.Client after construction. This is a test-only
    workaround; production code never does this.
    """
    client = RedditJSONClient(user_agent="test:v0", budget=budget)
    client._client.close()
    client._client = httpx.Client(
        base_url="https://www.reddit.com",
        headers={"User-Agent": "test:v0"},
        timeout=5.0,
        follow_redirects=False,
        transport=httpx.MockTransport(handler),
    )
    return client


@test
def test_client_get_returns_parsed_body():
    def handler(req):
        return httpx.Response(
            200,
            json={"kind": "Listing", "data": {"children": []}},
            headers={
                "X-Ratelimit-Used": "1",
                "X-Ratelimit-Remaining": "99",
                "X-Ratelimit-Reset": "300",
            },
        )
    client = _mock_client(handler)
    try:
        body = client.get("/r/x/hot.json")
        assert_eq(body["kind"], "Listing")
        assert_(client.headroom is not None)
        assert_eq(client.headroom["remaining"], 99.0)
        # Round-6 fix: reset_at_monotonic stored, not raw seconds-from-now
        assert_("reset_at_monotonic" in client.headroom)
    finally:
        client.close()


@test
def test_client_404_raises_typed():
    def handler(req):
        return httpx.Response(404, json={"message": "Not Found", "error": 404})
    client = _mock_client(handler)
    try:
        try:
            client.get("/r/x.json")
        except NotFoundError as e:
            assert_eq(e.status, 404)
            assert_eq(e.path, "/r/x.json")
        else:
            raise AssertionError("expected NotFoundError")
    finally:
        client.close()


@test
def test_client_403_with_reason():
    def handler(req):
        return httpx.Response(403, json={"reason": "gold_only", "message": "Forbidden"})
    client = _mock_client(handler)
    try:
        try:
            client.get("/r/lounge.json")
        except ForbiddenError as e:
            assert_eq(e.reason, "gold_only")
        else:
            raise AssertionError("expected ForbiddenError")
    finally:
        client.close()


@test
def test_client_429_retries_then_raises():
    """MAX_429_RETRIES=1 means 2 attempts total. Both 429 → raise."""
    calls = [0]

    def handler(req):
        calls[0] += 1
        return httpx.Response(429, json={"message": "rate limited"},
                              headers={"Retry-After": "0"})

    client = _mock_client(handler)
    try:
        assert_raises(RateLimitError, client.get, "/x")
        assert_eq(calls[0], 2, "should have made 2 attempts (1 initial + 1 retry)")
    finally:
        client.close()


@test
def test_client_redirect_raises_redirecterror():
    """Round-5 fix: follow_redirects=False, raise on 3xx with location."""
    def handler(req):
        return httpx.Response(302, headers={"Location": "/elsewhere"})
    client = _mock_client(handler)
    try:
        try:
            client.get("/x")
        except RedirectError as e:
            assert_eq(e.location, "/elsewhere")
        else:
            raise AssertionError("expected RedirectError")
    finally:
        client.close()


@test
def test_client_5xx_raises_upstream():
    def handler(req):
        return httpx.Response(503, text="error")
    client = _mock_client(handler)
    try:
        assert_raises(UpstreamError, client.get, "/x")
    finally:
        client.close()


@test
def test_client_budget_enforced_at_call():
    def handler(req):
        return httpx.Response(200, json={"ok": True})
    b = WorkflowBudget(max_api_calls=2, max_comments=100)
    client = _mock_client(handler, budget=b)
    try:
        client.get("/x")
        client.get("/x")
        assert_raises(BudgetExceededError, client.get, "/x")
    finally:
        client.close()


# ---- operations (mocked transport) --------------------------------------


def _mock_ops(handler, db_dir, budget=None) -> Operations:
    client = _mock_client(handler, budget=budget)
    cache = Cache(Path(db_dir) / "c.db")
    return Operations(client=client, cache=cache)


def _listing_response(children_data):
    return httpx.Response(200, json={
        "kind": "Listing",
        "data": {"children": [{"kind": "t3", "data": d} for d in children_data]},
    })


def _thread_response(post_data, comments_data):
    return httpx.Response(200, json=[
        {"kind": "Listing", "data": {"children": [{"kind": "t3", "data": post_data}]}},
        {"kind": "Listing", "data": {"children": [{"kind": "t1", "data": c} for c in comments_data]}},
    ])


_MIN_POST = {
    "id": "abc", "subreddit": "python", "title": "T", "score": 5,
    "num_comments": 1, "permalink": "/p", "created_utc": 0, "author": "u",
}


@test
def test_ops_search_caches_second_call():
    calls = [0]
    def handler(req):
        calls[0] += 1
        return _listing_response([_MIN_POST])
    with tempfile.TemporaryDirectory() as td:
        with _mock_ops(handler, td) as ops:
            r1 = ops.search("foo", subreddit="python")
            r2 = ops.search("foo", subreddit="python")
            assert_eq(calls[0], 1, "second call should hit cache")
            assert_eq(len(r1), 1)
            assert_eq(r1[0].fullname, "t3_abc")
            assert_eq(len(r2), 1)


@test
def test_ops_get_thread_returns_parsed():
    def handler(req):
        return _thread_response(
            _MIN_POST,
            [{"id": "c1", "body": "hi", "author": "a", "score": 3,
              "created_utc": 0, "parent_id": "t3_abc"}],
        )
    with tempfile.TemporaryDirectory() as td:
        with _mock_ops(handler, td) as ops:
            t = ops.get_thread("abc", top_n_comments=5)
            assert_eq(t.post.title, "T")
            assert_eq(len(t.comments), 1)
            assert_eq(t.comments[0].fullname, "t1_c1")


@test
def test_ops_get_thread_preserves_reddit_order():
    """Round-7 fix: comments returned in Reddit's order, NOT score-sorted."""
    def handler(req):
        return _thread_response(
            _MIN_POST,
            [
                {"id": "c1", "body": "low", "author": "a", "score": 1,
                 "created_utc": 0, "parent_id": "t3_abc"},
                {"id": "c2", "body": "high", "author": "a", "score": 9,
                 "created_utc": 0, "parent_id": "t3_abc"},
                {"id": "c3", "body": "mid", "author": "a", "score": 5,
                 "created_utc": 0, "parent_id": "t3_abc"},
            ],
        )
    with tempfile.TemporaryDirectory() as td:
        with _mock_ops(handler, td) as ops:
            t = ops.get_thread("abc")
            scores = [c.score for c in t.comments]
            assert_eq(scores, [1, 9, 5], "must preserve Reddit's order")


@test
def test_ops_error_caching_replays_with_url_path():
    """Round-7 fix: cached error replay preserves the original URL path."""
    calls = [0]
    def handler(req):
        calls[0] += 1
        return httpx.Response(403, json={"message": "Forbidden"})
    with tempfile.TemporaryDirectory() as td:
        with _mock_ops(handler, td) as ops:
            try:
                ops.get_thread("zzzzzz")
            except ForbiddenError as e:
                first = e.path
            try:
                ops.get_thread("zzzzzz")
            except ForbiddenError as e:
                second = e.path
            assert_eq(first, second, "cached replay must preserve URL path")
            assert_(first.startswith("/comments/"),
                    f"path should be a URL, got {first!r}")
            assert_eq(calls[0], 1, "second call should hit error cache")


@test
def test_ops_listing_strict_parser():
    """Round-7 fix: bad shape raises ValueError, not silent []."""
    def handler(req):
        return httpx.Response(200, json="not a dict")
    with tempfile.TemporaryDirectory() as td:
        with _mock_ops(handler, td) as ops:
            assert_raises(ValueError, ops.get_subreddit_listing, "python")


@test
def test_ops_expand_comment_caps():
    """Per-call hard caps validate before any HTTP call."""
    def handler(req):
        return httpx.Response(200, json=[])
    with tempfile.TemporaryDirectory() as td:
        with _mock_ops(handler, td) as ops:
            assert_raises(ValueError, ops.expand_comment, "x", "abc", "xyz", depth=10)
            assert_raises(ValueError, ops.expand_comment, "x", "abc", "xyz", limit=100)
            assert_raises(ValueError, ops.expand_comment, "x", "abc", "xyz", depth=-1)
            assert_raises(ValueError, ops.expand_comment, "x", "abc", "xyz", limit=0)


@test
def test_ops_listing_uses_no_time_for_hot():
    """Round-7 fix: hot listing must not send `t` param to Reddit AND must
    cache under the same key whether time_filter is the default 'all' or
    explicitly None.
    """
    seen_paths = []
    def handler(req):
        seen_paths.append((req.url.path, dict(req.url.params)))
        return _listing_response([])
    with tempfile.TemporaryDirectory() as td:
        with _mock_ops(handler, td) as ops:
            ops.get_subreddit_listing("python", sort="hot")
            ops.get_subreddit_listing("python", sort="hot", time_filter="week")
        # Both should hit the same cache key (no network for second call)
        assert_eq(len(seen_paths), 1, "hot listing time_filter must be ignored for cache key")
        # And the request must NOT include a `t` param
        assert_("t" not in seen_paths[0][1], f"hot listing must not send t={seen_paths[0][1]!r}")


@test
def test_ops_expand_comment_happy_path():
    """Round-8 fix: exercise _parse_comment_tree end-to-end.

    Reddit's expand response is the same ``[post_listing, comment_listing]``
    shape as a thread fetch; with ``context=0`` the comment_listing should
    contain the focal comment (with descendants nested in `replies`) as its
    sole top-level entry.
    """
    def handler(req):
        return httpx.Response(200, json=[
            {"kind": "Listing", "data": {"children": [
                {"kind": "t3", "data": _MIN_POST}
            ]}},
            {"kind": "Listing", "data": {"children": [
                {"kind": "t1", "data": {
                    "id": "focal", "body": "root comment", "author": "a",
                    "score": 5, "created_utc": 0, "parent_id": "t3_abc",
                    "replies": {"kind": "Listing", "data": {"children": [
                        {"kind": "t1", "data": {
                            "id": "reply1", "body": "reply", "author": "b",
                            "score": 2, "created_utc": 0, "parent_id": "t1_focal",
                        }},
                    ]}},
                }},
            ]}},
        ])
    with tempfile.TemporaryDirectory() as td:
        with _mock_ops(handler, td) as ops:
            tree = ops.expand_comment("python", "abc", "focal", depth=2, limit=10)
            assert_eq(tree.root.fullname, "t1_focal")
            assert_eq(tree.root.body, "root comment")
            assert_eq(len(tree.root.replies), 1)
            assert_eq(tree.root.replies[0].fullname, "t1_reply1")
            assert_eq(tree.root.replies[0].depth, 1)


@test
def test_ops_get_thread_budget_counts_nested():
    """Round-8 fix: comment-budget charge must include nested replies, not
    just top-level count. Otherwise threads with deep replies under-enforce
    the workflow safety cap.

    Round-9 strengthening: include a depth-2 grandchild to verify the
    recursion goes all the way down, not just one level.
    """
    def handler(req):
        return _thread_response(
            _MIN_POST,
            [{
                "id": "c1", "body": "top", "author": "a", "score": 3,
                "created_utc": 0, "parent_id": "t3_abc",
                "replies": {"kind": "Listing", "data": {"children": [
                    {"kind": "t1", "data": {
                        "id": "r1", "body": "nested1", "author": "b",
                        "score": 1, "created_utc": 0, "parent_id": "t1_c1",
                        # Grandchild — depth-2 recursion check
                        "replies": {"kind": "Listing", "data": {"children": [
                            {"kind": "t1", "data": {
                                "id": "g1", "body": "grandchild", "author": "c",
                                "score": 0, "created_utc": 0, "parent_id": "t1_r1",
                            }},
                        ]}},
                    }},
                    {"kind": "t1", "data": {
                        "id": "r2", "body": "nested2", "author": "b",
                        "score": 1, "created_utc": 0, "parent_id": "t1_c1",
                    }},
                ]}},
            }],
        )
    with tempfile.TemporaryDirectory() as td:
        budget = WorkflowBudget(max_api_calls=10, max_comments=100)
        client = _mock_client(handler, budget=budget)
        cache = Cache(Path(td) / "c.db")
        with Operations(client=client, cache=cache) as ops:
            ops.get_thread("abc")
            # 1 top-level + 2 children + 1 grandchild = 4
            assert_eq(budget.comments, 4,
                      "must count grandchild too: 1 top + 2 nested + 1 grand = 4")


@test
def test_ops_negative_limit_rejected():
    """Round-8 fix: defense-in-depth — Python API users bypass argparse."""
    def handler(req):
        return _listing_response([])
    with tempfile.TemporaryDirectory() as td:
        with _mock_ops(handler, td) as ops:
            assert_raises(ValueError, ops.search, "x", subreddit="python", limit=0)
            assert_raises(ValueError, ops.search, "x", subreddit="python", limit=-1)
            assert_raises(ValueError, ops.get_subreddit_listing, "python", limit=0)
            assert_raises(ValueError, ops.get_thread, "abc", top_n_comments=0)
            assert_raises(ValueError, ops.get_thread, "abc", top_n_comments=-5)


@test
def test_ops_cache_write_failure_does_not_mask_transport():
    """Round-7 fix: cache write failures must be best-effort; the original
    transport exception (or success) must reach the caller unaltered.
    """
    class BrokenCache:
        def get(self, *a, **kw): return None
        def put(self, *a, **kw): raise RuntimeError("cache write boom")
        def put_error(self, *a, **kw): raise RuntimeError("cache put_error boom")
        def purge(self, **kw): return 0
        def stats(self):
            from reddit_research.core import CacheStats
            return CacheStats()
        def close(self): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass

    # 200 path: cache.put fails — operation must still return the body
    def handler200(req):
        return _listing_response([_MIN_POST])
    client = _mock_client(handler200)
    try:
        ops = Operations(client=client, cache=BrokenCache())
        results = ops.search("foo", subreddit="python")
        assert_eq(len(results), 1, "cache failure must not lose the fetched body")
    finally:
        client.close()

    # 403 path: cache.put_error fails — original ForbiddenError must still propagate
    def handler403(req):
        return httpx.Response(403, json={"reason": "test", "message": "Forbidden"})
    client = _mock_client(handler403)
    try:
        ops = Operations(client=client, cache=BrokenCache())
        try:
            ops.get_thread("zzzzzz")
        except ForbiddenError as e:
            assert_eq(e.reason, "test", "original error must reach caller")
        else:
            raise AssertionError("expected ForbiddenError")
    finally:
        client.close()


@test
def test_ops_parse_thread_strict_missing_post():
    """Round-7 design: _parse_thread raises if the post element is missing."""
    def handler(req):
        # Simulates a malformed response — comment listing present but no post
        return httpx.Response(200, json=[
            {"kind": "Listing", "data": {"children": []}},  # no t3 post
            {"kind": "Listing", "data": {"children": []}},
        ])
    with tempfile.TemporaryDirectory() as td:
        with _mock_ops(handler, td) as ops:
            assert_raises(ValueError, ops.get_thread, "abc")


@test
def test_ops_fresh_bypasses_cache():
    """fresh=True forces a network call even when a valid cache entry exists."""
    calls = [0]
    def handler(req):
        calls[0] += 1
        return _listing_response([_MIN_POST])
    with tempfile.TemporaryDirectory() as td:
        with _mock_ops(handler, td) as ops:
            ops.search("foo", subreddit="python")  # populates cache
            ops.search("foo", subreddit="python")  # cache hit
            ops.search("foo", subreddit="python", fresh=True)  # bypass cache
            assert_eq(calls[0], 2, "fresh=True must trigger a 2nd network call")


@test
def test_ops_search_all_path_no_subreddit():
    """search(subreddit=None) hits /search.json (search-all), not /r/<x>/search.json."""
    seen_paths = []
    def handler(req):
        seen_paths.append(req.url.path)
        return _listing_response([])
    with tempfile.TemporaryDirectory() as td:
        with _mock_ops(handler, td) as ops:
            ops.search("foo")
            assert_eq(seen_paths, ["/search.json"])


# ---- CLI parser + dispatch ----------------------------------------------


@test
def test_cli_parser_rejects_negative_limit():
    """Round-8 fix: --limit must reject 0/negative via argparse converter."""
    from reddit_research.cli.__main__ import build_parser
    parser = build_parser()
    # argparse exits via SystemExit on bad args
    assert_raises(SystemExit, parser.parse_args, ["search", "foo", "--limit", "0"])
    assert_raises(SystemExit, parser.parse_args, ["search", "foo", "--limit", "-1"])


@test
def test_cli_parser_accepts_depth_zero():
    """Round-8 fix: --depth allows 0 (focal comment only); help text says (0-5)."""
    from reddit_research.cli.__main__ import build_parser
    parser = build_parser()
    args = parser.parse_args(["expand", "python", "abc", "xyz", "--depth", "0"])
    assert_eq(args.depth, 0)


@test
def test_cli_run_catches_value_error():
    """Round-8 fix: _run() must catch ValueError (parser strict / cap violation)
    and return a clean exit code instead of raising a traceback."""
    from reddit_research.cli.commands import _run
    from argparse import Namespace

    class FakeArgs(Namespace):
        format = "text"
        env_file = None
        budget_api = 50
        budget_comments = 500

    def boom(ops, args):
        raise ValueError("schema drift!")

    code = _run(FakeArgs(), boom)
    assert_eq(code, 1, "ValueError must map to exit code 1")


@test
def test_cli_run_catches_unexpected():
    """Round-8 fix: bare `Exception` last-resort catch."""
    from reddit_research.cli.commands import _run
    from argparse import Namespace

    class FakeArgs(Namespace):
        format = "text"
        env_file = None
        budget_api = 50
        budget_comments = 500

    def boom(ops, args):
        raise KeyError("totally unexpected")

    code = _run(FakeArgs(), boom)
    assert_eq(code, 1, "unexpected exception must still map to exit 1, not crash")


@test
def test_ops_malformed_200_not_cached():
    """Round-9 fix: a strict-parser failure must NOT cache the bad body.

    Previously cache.put ran before the operations layer parsed the body, so
    a malformed 200 (Reddit schema drift) would be persisted for the full
    TTL — only fresh=True could escape. Now parse runs first; if it raises,
    the cache stays clean and the next call retries the network.
    """
    calls = [0]

    def handler(req):
        calls[0] += 1
        # Return a malformed 200 — missing data.children, _parse_listing_children raises
        return httpx.Response(200, json={"kind": "Listing", "data": {}})

    with tempfile.TemporaryDirectory() as td:
        with _mock_ops(handler, td) as ops:
            assert_raises(ValueError, ops.search, "foo", subreddit="python")
            # Critical: second call must NOT hit the cache. The bad body must
            # not have been persisted.
            assert_raises(ValueError, ops.search, "foo", subreddit="python")
            assert_eq(calls[0], 2,
                      "malformed 200 must not poison cache; second call must retry network")


@test
def test_client_budget_not_charged_on_proactive_backoff_raise():
    """Round-9 fix: when proactive backoff raises (reset > cap), the budget
    must NOT be charged — no upstream request went out.
    """
    # Build a client whose headroom indicates remaining=0 and reset way beyond cap
    def handler(req):
        # Should never be called — proactive backoff raises first
        return httpx.Response(200, json={"ok": True})

    b = WorkflowBudget(max_api_calls=10, max_comments=100)
    client = _mock_client(handler, budget=b)
    try:
        # Seed headroom: remaining=0, reset way beyond MAX_RETRY_AFTER_SECONDS
        # (120s). Use 1000s. Set reset_at_monotonic directly to bypass the
        # initial "first call has no headroom" check.
        client.headroom = {
            "used": 100.0,
            "remaining": 0.0,
            "reset_in_seconds": 1000.0,
            "reset_at_monotonic": time.monotonic() + 1000.0,
            "reset_raw": "1000",
        }
        try:
            client.get("/r/python/hot.json")
        except RateLimitError:
            pass
        else:
            raise AssertionError("expected RateLimitError from proactive backoff")
        # The actual request never went out; budget must show 0 charges
        assert_eq(b.api_calls, 0,
                  "proactive-backoff raise must NOT charge api_calls budget")
    finally:
        client.close()


@test
def test_client_budget_charges_per_attempt_on_429_retry():
    """Round-9 fix: each network attempt is a separate budget charge.
    A 429 → retry → 429 sequence should charge twice (Reddit saw 2 requests).
    """
    def handler(req):
        return httpx.Response(429, json={"message": "rate limited"},
                              headers={"Retry-After": "0"})

    b = WorkflowBudget(max_api_calls=10, max_comments=100)
    client = _mock_client(handler, budget=b)
    try:
        try:
            client.get("/x")
        except RateLimitError:
            pass
        # Initial attempt + 1 retry = 2 charges
        assert_eq(b.api_calls, 2,
                  "429 retry must charge twice (2 actual network attempts)")
    finally:
        client.close()


@test
def test_ops_status_snapshot():
    def handler(req):
        return _listing_response([_MIN_POST])
    with tempfile.TemporaryDirectory() as td:
        budget = WorkflowBudget(max_api_calls=10, max_comments=100)
        client = _mock_client(handler, budget=budget)
        cache = Cache(Path(td) / "c.db")
        with Operations(client=client, cache=cache) as ops:
            ops.search("foo", subreddit="python")
            ops.search("foo", subreddit="python")  # cache hit
            s = ops.status()
            assert_eq(s.total_api_calls, 1, "1 network call (second was cache hit)")
            assert_eq(s.cache_hits, 1)
            assert_(s.cache_writes >= 1)


# ---- Run ----------------------------------------------------------------


if __name__ == "__main__":
    sys.exit(run())
