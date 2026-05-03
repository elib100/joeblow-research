"""Entry point for the ``reddit-cli`` console script.

Sets up the argparse tree, dispatches to the per-subcommand handlers in
:mod:`reddit_research.cli.commands`, and handles cross-cutting concerns
(verbose logging, KeyboardInterrupt). All actual command logic lives in
``commands.py`` so it can be tested without going through argparse.
"""

from __future__ import annotations

import argparse
import logging
import sys

from reddit_research.cli.commands import (
    cmd_expand,
    cmd_listing,
    cmd_purge,
    cmd_search,
    cmd_status,
    cmd_thread,
)


_TIME_FILTERS = ["all", "year", "month", "week", "day", "hour"]
_LISTING_SORTS = ["hot", "new", "top", "rising", "controversial"]
_SEARCH_SORTS = ["relevance", "hot", "top", "new", "comments"]


def _positive_int(s: str) -> int:
    """Argparse type converter: int >= 1. Round-8 panel."""
    n = int(s)
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer; got {n}")
    return n


def _nonneg_int(s: str) -> int:
    """Argparse type converter: int >= 0."""
    n = int(s)
    if n < 0:
        raise argparse.ArgumentTypeError(f"must be a non-negative integer; got {n}")
    return n


def build_parser() -> argparse.ArgumentParser:
    """Construct the argparse tree. Exposed for tests."""
    p = argparse.ArgumentParser(
        prog="reddit-cli",
        description="Personal Reddit research tool — search, listings, threads, comments.",
    )
    p.add_argument(
        "--format", choices=["text", "json"], default="text",
        help="Output format. JSON uses dataclasses.asdict serialization.",
    )
    p.add_argument(
        "--budget-api", type=_positive_int, default=50,
        help="Max API calls per command invocation (default 50).",
    )
    p.add_argument(
        "--budget-comments", type=_positive_int, default=500,
        help="Max comments fetched per command invocation (default 500).",
    )
    p.add_argument(
        "--env-file", default=None,
        help="Path to a .env file. Default: ./.env in the cwd if it exists.",
    )
    p.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable INFO-level logging from reddit_research.*",
    )

    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    # search
    s = sub.add_parser("search", help="Search Reddit (sub-scoped or all).")
    s.add_argument("query")
    s.add_argument("--subreddit", default=None,
                   help="Restrict to one subreddit. Default: search all of Reddit.")
    s.add_argument("--sort", default="relevance", choices=_SEARCH_SORTS)
    s.add_argument("--time", default="all", choices=_TIME_FILTERS)
    s.add_argument("--limit", type=_positive_int, default=25)
    s.add_argument("--fresh", action="store_true",
                   help="Bypass cache for this call.")
    s.set_defaults(func=cmd_search)

    # listing
    s = sub.add_parser("listing", help="Fetch a subreddit listing.")
    s.add_argument("name", help="Subreddit name (without r/).")
    s.add_argument("--sort", default="hot", choices=_LISTING_SORTS)
    s.add_argument("--limit", type=_positive_int, default=25)
    s.add_argument("--time", default="all", choices=_TIME_FILTERS,
                   help="Only used when --sort is top or controversial.")
    s.add_argument("--fresh", action="store_true")
    s.set_defaults(func=cmd_listing)

    # thread
    s = sub.add_parser("thread", help="Fetch a thread + top-N top-level comments.")
    s.add_argument("id", help="Thread id (bare like abc123 or fullname like t3_abc123).")
    s.add_argument("--top-n", type=_positive_int, default=20)
    s.add_argument("--fresh", action="store_true")
    s.set_defaults(func=cmd_thread)

    # expand
    s = sub.add_parser("expand", help="Expand a comment subtree.")
    s.add_argument("subreddit", help="The thread's subreddit (without r/).")
    s.add_argument("thread_id", help="Thread id (bare or t3_-prefixed).")
    s.add_argument("comment_id", help="Comment id (bare or t1_-prefixed).")
    s.add_argument("--depth", type=_nonneg_int, default=2,
                   help="Max reply depth to fetch (0-5; 0 = focal comment only).")
    s.add_argument("--limit", type=_positive_int, default=20,
                   help="Max comments to fetch (1-50).")
    s.add_argument("--fresh", action="store_true")
    s.set_defaults(func=cmd_expand)

    # purge
    s = sub.add_parser("purge", help="Delete cache rows older than N days.")
    s.add_argument("--older-than-days", type=_positive_int, default=30)
    s.set_defaults(func=cmd_purge)

    # status
    s = sub.add_parser("status", help="Cache + transport state snapshot.")
    s.set_defaults(func=cmd_status)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.verbose:
        logging.basicConfig(
            level=logging.INFO,
            format="%(levelname)s %(name)s: %(message)s",
        )
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\nAborted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
