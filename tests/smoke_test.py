#!/usr/bin/env python3
"""Live smoke test. Hits Reddit. ~3 API calls per run.

Use this after install + after deploy to confirm the .json transport, the
cache layer, and the operations API are all functioning end-to-end against
the real Reddit API. Run from project root: ``python tests/smoke_test.py``.

Uses a tmpdir cache so it doesn't pollute the production cache. Exits 0 on
pass, non-zero on any failure.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from reddit_research.core import (
    Cache,
    Operations,
    RedditJSONClient,
    WorkflowBudget,
    load_config,
)


def main() -> int:
    cfg = load_config()
    print(f"smoke_test: user_agent={cfg.user_agent!r}")

    with tempfile.TemporaryDirectory() as td:
        budget = WorkflowBudget(max_api_calls=10, max_comments=100)
        client = RedditJSONClient(user_agent=cfg.user_agent, budget=budget)
        cache = Cache(Path(td) / "smoke.db")
        with Operations(client=client, cache=cache) as ops:
            # 1. status before any call
            s0 = ops.status()
            assert s0.total_api_calls == 0, f"expected 0 api_calls initially, got {s0.total_api_calls}"
            print(f"  initial: api_calls={s0.total_api_calls}")

            # 2. one listing
            print("  fetching r/python hot[2]...")
            results = ops.get_subreddit_listing("python", sort="hot", limit=2)
            if not results:
                print("FAIL: no listing results", file=sys.stderr)
                return 1
            print(f"    {len(results)} threads")
            print(f"    first: {results[0].fullname} {results[0].title[:60]!r}")

            # 3. one thread fetch (uses bare id from the listing)
            print(f"  fetching thread {results[0].id}...")
            thread = ops.get_thread(results[0].id, top_n_comments=3)
            print(f"    post: {thread.post.title[:60]!r}")
            print(f"    {len(thread.comments)} top-level comments")

            # 4. cache hit on a re-fetch
            print("  re-fetching same listing (cache hit expected)...")
            ops.get_subreddit_listing("python", sort="hot", limit=2)

            s = ops.status()
            print()
            print(f"final: api_calls={s.total_api_calls}, "
                  f"cache_hits={s.cache_hits}, cache_misses={s.cache_misses}, "
                  f"cache_rows={s.cache_row_count}")
            print(f"       headroom_remaining={s.headroom and s.headroom.get('remaining')}")

            if s.cache_hits == 0:
                print("FAIL: expected at least one cache hit", file=sys.stderr)
                return 1
            if s.total_api_calls < 2:
                print(f"FAIL: expected at least 2 api_calls, got {s.total_api_calls}",
                      file=sys.stderr)
                return 1

    print()
    print("smoke_test PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
