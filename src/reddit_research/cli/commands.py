"""Per-subcommand handlers + output formatting.

Each ``cmd_*`` function takes the parsed argparse namespace and returns an
exit code. They share a common ``_run()`` wrapper that handles cache /
client lifecycle and maps typed exceptions to exit codes:

    0   success
    1   generic RedditError or unexpected
    2   BudgetExceededError (per-workflow cap tripped)
    3   RateLimitError (after retries exhausted)
    4   TransportError or UpstreamError (transport / 5xx)
    5   NotFoundError or ForbiddenError (Reddit said no)
    6   RedirectError (Reddit issued a 3xx — unexpected per Phase 0)
    7   InvalidIdError (caller-supplied id failed validation)
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, is_dataclass
from typing import Any, Callable

from reddit_research.core import (
    BudgetExceededError,
    Cache,
    CommentSummary,
    CommentTree,
    ForbiddenError,
    InvalidIdError,
    NotFoundError,
    Operations,
    RateLimitError,
    RedditError,
    RedditJSONClient,
    RedirectError,
    Status,
    Thread,
    ThreadSummary,
    TransportError,
    UpstreamError,
    WorkflowBudget,
    load_config,
)


# ---- Lifecycle + dispatch -------------------------------------------------


def _build_ops(args: argparse.Namespace) -> Operations:
    """env + flags -> Operations with a fresh budget tracker."""
    cfg = load_config(args.env_file)
    budget = WorkflowBudget(
        max_api_calls=int(args.budget_api),
        max_comments=int(args.budget_comments),
    )
    client = RedditJSONClient(user_agent=cfg.user_agent, budget=budget)
    cache = Cache(cfg.cache_db_path)
    return Operations(client=client, cache=cache)


def _run(args: argparse.Namespace, fn: Callable[[Operations, argparse.Namespace], Any]) -> int:
    """Wrap a command call with cleanup + typed-exception → exit-code mapping."""
    try:
        with _build_ops(args) as ops:
            payload = fn(ops, args)
            _emit(args, payload)
        return 0
    except BudgetExceededError as e:
        print(f"reddit-cli: budget exceeded — {e}", file=sys.stderr)
        return 2
    except RateLimitError as e:
        retry = f"; retry after {e.retry_after:.1f}s" if e.retry_after else ""
        print(f"reddit-cli: rate limited{retry} ({e})", file=sys.stderr)
        return 3
    except (TransportError, UpstreamError) as e:
        print(f"reddit-cli: transport/upstream error — {e}", file=sys.stderr)
        return 4
    except (NotFoundError, ForbiddenError) as e:
        print(f"reddit-cli: not available — {e}", file=sys.stderr)
        return 5
    except RedirectError as e:
        loc = f" (location={e.location!r})" if e.location else ""
        print(f"reddit-cli: unexpected redirect — {e}{loc}", file=sys.stderr)
        return 6
    except InvalidIdError as e:
        print(f"reddit-cli: invalid id — {e}", file=sys.stderr)
        return 7
    except RedditError as e:
        print(f"reddit-cli: error — {e}", file=sys.stderr)
        return 1


# ---- Commands ------------------------------------------------------------


def cmd_search(args: argparse.Namespace) -> int:
    return _run(args, lambda ops, a: ops.search(
        query=a.query,
        subreddit=a.subreddit,
        sort=a.sort,
        time_filter=a.time,
        limit=a.limit,
        fresh=a.fresh,
    ))


def cmd_listing(args: argparse.Namespace) -> int:
    return _run(args, lambda ops, a: ops.get_subreddit_listing(
        name=a.name,
        sort=a.sort,
        limit=a.limit,
        time_filter=a.time,
        fresh=a.fresh,
    ))


def cmd_thread(args: argparse.Namespace) -> int:
    return _run(args, lambda ops, a: ops.get_thread(
        thread_id=a.id,
        top_n_comments=a.top_n,
        fresh=a.fresh,
    ))


def cmd_expand(args: argparse.Namespace) -> int:
    return _run(args, lambda ops, a: ops.expand_comment(
        subreddit=a.subreddit,
        thread_id=a.thread_id,
        comment_id=a.comment_id,
        depth=a.depth,
        limit=a.limit,
        fresh=a.fresh,
    ))


def cmd_purge(args: argparse.Namespace) -> int:
    return _run(args, lambda ops, a: {
        "deleted_rows": ops.purge(older_than_days=a.older_than_days),
        "older_than_days": a.older_than_days,
    })


def cmd_status(args: argparse.Namespace) -> int:
    return _run(args, lambda ops, a: ops.status())


# ---- Output formatting ---------------------------------------------------


def _emit(args: argparse.Namespace, payload: Any) -> None:
    if args.format == "json":
        print(json.dumps(_to_jsonable(payload), indent=2, default=str))
    else:
        _emit_text(payload)


def _to_jsonable(payload: Any) -> Any:
    if is_dataclass(payload) and not isinstance(payload, type):
        return asdict(payload)
    if isinstance(payload, list):
        return [_to_jsonable(x) for x in payload]
    if isinstance(payload, dict):
        return {k: _to_jsonable(v) for k, v in payload.items()}
    return payload


def _emit_text(payload: Any) -> None:
    if isinstance(payload, list):
        # Listings of ThreadSummary
        for i, item in enumerate(payload):
            if isinstance(item, ThreadSummary):
                _print_thread_summary(item)
            else:
                print(item)
        if not payload:
            print("(no results)")
        return
    if isinstance(payload, Thread):
        _print_thread(payload)
        return
    if isinstance(payload, CommentTree):
        _print_comment_tree(payload)
        return
    if isinstance(payload, Status):
        _print_status(payload)
        return
    if isinstance(payload, dict):
        for k, v in payload.items():
            print(f"{k}: {v}")
        return
    print(payload)


def _print_thread_summary(t: ThreadSummary) -> None:
    print(f"[{t.score:>5}] r/{t.subreddit} · {t.title}")
    print(
        f"        comments={t.num_comments}  author={t.author or '[deleted]'}  "
        f"fullname={t.fullname}"
    )
    print(f"        https://reddit.com{t.permalink}")
    print()


def _print_thread(thread: Thread) -> None:
    p = thread.post
    print(f"# {p.title}")
    print(
        f"r/{p.subreddit}  ·  score={p.score}  ·  comments={p.num_comments}  "
        f"·  author={p.author or '[deleted]'}"
    )
    print(f"https://reddit.com{p.permalink}")
    if p.is_self and p.selftext:
        print()
        print(p.selftext)
    print()
    print(f"--- top {len(thread.comments)} comments (Reddit's order) ---")
    print()
    for c in thread.comments:
        _print_comment(c, depth=0)


def _print_comment(c: CommentSummary, depth: int) -> None:
    indent = "  " * depth
    print(f"{indent}[{c.score:>4}] {c.author or '[deleted]'}  ({c.fullname})")
    body = (c.body or "").strip().replace("\n", "\n" + indent + "    ")
    print(f"{indent}    {body}")
    print()
    for r in c.replies:
        _print_comment(r, depth + 1)


def _print_comment_tree(tree: CommentTree) -> None:
    print(f"--- comment subtree rooted at {tree.root.fullname} ---")
    print()
    _print_comment(tree.root, depth=0)


def _print_status(s: Status) -> None:
    print("=== status ===")
    print(f"cache: {s.cache_row_count} rows, {s.cache_db_size_bytes:,} bytes on disk")
    if s.cache_hit_rate is not None:
        print(
            f"  hit rate: {s.cache_hit_rate:.1%} "
            f"({s.cache_hits} hits / {s.cache_misses} misses)"
        )
    else:
        print("  hit rate: n/a (no reads this session)")
    print(f"  writes this session: {s.cache_writes}")
    print()
    print("transport:")
    print(f"  api calls this session: {s.total_api_calls}")
    print(f"  last call status: {s.last_call_status}")
    if s.headroom:
        rem = s.headroom.get("remaining")
        rst = s.headroom.get("reset_in_seconds")
        print(f"  rate-limit remaining: {rem}")
        print(f"  reset in: {rst}s")
    if s.workflow_budget:
        b = s.workflow_budget
        print(
            f"  budget: {b['api_calls']}/{b['max_api_calls']} api_calls, "
            f"{b['comments']}/{b['max_comments']} comments"
        )
