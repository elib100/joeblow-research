"""Entry point for the ``reddit-mcp`` console script.

Run as ``reddit-mcp`` on the deploy host (warehouse-vm) under the
``redditmcp`` service user; access from a laptop via SSH
(``ssh warehouse reddit-mcp``). Stdio transport — no network listener.

Configuration: same ``.env`` and environment variables as ``reddit-cli``
(see :mod:`reddit_research.core.config`). Budget caps and a custom
``.env`` path can be overridden via flags.
"""

from __future__ import annotations

import argparse
import logging
import sys

from reddit_research.core import (
    Cache,
    Operations,
    RedditJSONClient,
    load_config,
)
from reddit_research.mcp.server import make_default_budget, serve


def _positive_int(s: str) -> int:
    n = int(s)
    if n < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer; got {n}")
    return n


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="reddit-mcp",
        description=(
            "MCP stdio server for personal Reddit research. Exposes the six "
            "core operations as MCP tools."
        ),
    )
    p.add_argument(
        "--budget-api", type=_positive_int, default=50,
        help="Max API calls per session (default 50). Persists across all "
             "tool calls until the server exits or reset_budget() is called.",
    )
    p.add_argument(
        "--budget-comments", type=_positive_int, default=500,
        help="Max comments fetched per session (default 500).",
    )
    p.add_argument(
        "--env-file", default=None,
        help="Path to a .env file. Default: ./.env in the cwd if it exists.",
    )
    p.add_argument(
        "-v", "--verbose", action="store_true",
        help="Enable INFO-level logging from reddit_research.* on stderr.",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.verbose:
        # IMPORTANT: stderr only. Stdout is the MCP transport channel — any
        # stray write there corrupts JSON-RPC framing.
        logging.basicConfig(
            level=logging.INFO,
            stream=sys.stderr,
            format="%(levelname)s %(name)s: %(message)s",
        )

    cfg = load_config(args.env_file)
    budget = make_default_budget(
        max_api_calls=args.budget_api,
        max_comments=args.budget_comments,
    )
    client = RedditJSONClient(user_agent=cfg.user_agent, budget=budget)
    cache = Cache(cfg.cache_db_path)
    try:
        with Operations(client=client, cache=cache) as ops:
            try:
                serve(ops, budget=budget, transport="stdio")
            except KeyboardInterrupt:
                # Host disconnected via SIGINT — clean exit, not a crash.
                return 130
    except Exception as e:
        # Last-resort safety net. Goes to stderr only — stdout is sacred.
        print(f"reddit-mcp: fatal — {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
