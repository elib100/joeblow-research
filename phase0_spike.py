#!/usr/bin/env python3
"""Phase 0 spike — verify Reddit API behavior + measure baseline costs.

Probes the Reddit API for the four things we need to know before writing the
library:

    1. Auth check          — does PRAW authenticate and pull one thread?
    2. Error mapping       — which prawcore exceptions for 404 / 403 / etc?
    3. MoreComments cap    — does replace_more(limit=10) behave as expected?
    4. Benchmarks          — for 5 realistic research queries, measure
                              per-path API calls / wall time / signal-to-noise
                              ratio / approximate token count.

Outputs:
    phase0_notes.md        — free-form observations + exception mapping
    phase0_benchmarks.md   — human-readable benchmark summary
    phase0_benchmarks.json — machine-readable, drives v0.2 comparison

Setup:
    cd /home/elib/code/reddit_api
    python3 -m venv venv && source venv/bin/activate
    pip install -r requirements-phase0.txt
    cp .env.example .env && chmod 600 .env
    # ... fill REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_PASSWORD into .env

Run:
    python phase0_spike.py            # all four probes
    python phase0_spike.py --probe auth         # one at a time
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import praw
    import prawcore
except ImportError:
    sys.exit("Missing praw — run: pip install -r requirements-phase0.txt")

try:
    from dotenv import load_dotenv
except ImportError:
    sys.exit("Missing python-dotenv — run: pip install -r requirements-phase0.txt")

try:
    import tiktoken
    _ENC = tiktoken.get_encoding("cl100k_base")
    TOKENIZER = "tiktoken cl100k_base"
except Exception:
    _ENC = None
    TOKENIZER = "chars/4 fallback"


ROOT = Path(__file__).parent
NOTES_PATH = ROOT / "phase0_notes.md"
BENCH_MD_PATH = ROOT / "phase0_benchmarks.md"
BENCH_JSON_PATH = ROOT / "phase0_benchmarks.json"

REQUIRED_ENV = [
    "REDDIT_CLIENT_ID",
    "REDDIT_CLIENT_SECRET",
    "REDDIT_USERNAME",
    "REDDIT_PASSWORD",
    "REDDIT_USER_AGENT",
]


def load_reddit() -> praw.Reddit:
    load_dotenv(ROOT / ".env")
    missing = [k for k in REQUIRED_ENV if not os.environ.get(k)]
    if missing:
        sys.exit(
            f"Missing in .env ({ROOT / '.env'}): {', '.join(missing)}\n"
            f"Copy .env.example to .env and fill in the secret values."
        )
    # CLAUDE.md decision 3: read_only=True (defense in depth — also switches PRAW
    # to application-only auth, so user.me() returns None in this mode; that's
    # expected and the auth probe handles it). timeout=30 prevents hung runs on
    # network blips. check_for_updates=False skips a per-invocation PyPI call.
    return praw.Reddit(
        client_id=os.environ["REDDIT_CLIENT_ID"],
        client_secret=os.environ["REDDIT_CLIENT_SECRET"],
        username=os.environ["REDDIT_USERNAME"],
        password=os.environ["REDDIT_PASSWORD"],
        user_agent=os.environ["REDDIT_USER_AGENT"],
        check_for_updates=False,
        read_only=True,
        timeout=30,
    )


def headroom(reddit: praw.Reddit) -> dict:
    """Snapshot PRAW's exposed rate-limit headroom."""
    try:
        limits = reddit.auth.limits
        return {
            "remaining": limits.get("remaining"),
            "used": limits.get("used"),
            "reset_timestamp": limits.get("reset_timestamp"),
        }
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def count_tokens(text: str) -> int:
    """Approximate token count. cl100k_base is a tight-enough proxy for ratios."""
    if _ENC is not None:
        return len(_ENC.encode(text))
    return max(1, len(text) // 4)


def submission_signal(s) -> dict:
    return {
        "title": s.title,
        "body": s.selftext or "",
        "author": str(s.author) if s.author else "[deleted]",
        "score": s.score,
        "created_utc": s.created_utc,
        "num_comments": s.num_comments,
        "permalink": s.permalink,
    }


def comment_signal(c) -> dict:
    return {
        "body": getattr(c, "body", ""),
        "author": str(c.author) if getattr(c, "author", None) else "[deleted]",
        "score": getattr(c, "score", 0),
        "created_utc": getattr(c, "created_utc", 0),
    }


def full_payload_size(praw_obj) -> int:
    """Approximation of the full Reddit API payload size for one PRAW object.

    Filters out PRAW-internal attrs (anything starting with `_`) — most
    importantly `_reddit`, the back-reference to the Reddit instance, which
    would serialize via `default=str` to a huge repr and inflate the "full"
    size 5–50x. After filtering, what's left is approximately the API payload
    fields PRAW populated by lazy-loading.
    """
    try:
        public = {k: v for k, v in praw_obj.__dict__.items() if not k.startswith("_")}
        return len(json.dumps(public, default=str))
    except Exception:
        return -1


# ----- Probes ---------------------------------------------------------------


def probe_auth(reddit: praw.Reddit) -> dict:
    """Verify auth via a minimal authenticated read.

    With `read_only=True`, PRAW uses application-only auth (client_credentials),
    so `reddit.user.me()` returns None. We instead use any successful API call
    as proof of auth, and check that `auth.limits` populates afterward (it's
    lazy — pre-call snapshot would always be `None`, so we don't bother taking
    one).
    """
    out = {"name": "auth", "ok": False, "details": {}, "errors": []}
    out["details"]["read_only_mode"] = bool(getattr(reddit, "read_only", False))
    try:
        sub = next(iter(reddit.subreddit("python").hot(limit=1)))
        _ = (sub.title, sub.score, sub.num_comments, sub.author)
        out["details"]["sample_thread"] = {
            "fullname": sub.fullname,
            "title": sub.title[:80],
            "num_comments": sub.num_comments,
        }
        post_headroom = headroom(reddit)
        out["details"]["headroom_post"] = post_headroom
        # The whole point of this probe: confirm PRAW exposes headroom after a real call.
        out["details"]["headroom_populated"] = (
            isinstance(post_headroom, dict)
            and post_headroom.get("remaining") is not None
        )
        out["ok"] = True
    except prawcore.exceptions.OAuthException as e:
        out["errors"].append(f"OAuthException: {e}")
    except Exception as e:
        out["errors"].append(f"{type(e).__module__}.{type(e).__name__}: {e}")
    return out


def probe_errors(reddit: praw.Reddit) -> dict:
    """Map prawcore exception types for 404 / 403 / quarantine / deleted."""
    out = {"name": "errors", "exceptions_observed": {}, "details": {}}

    cases = [
        (
            "nonexistent_subreddit",
            lambda: list(
                reddit.subreddit("this_does_not_exist_zzzz_99999_qwertyu").hot(limit=1)
            ),
        ),
        (
            "bogus_thread_id",
            lambda: reddit.submission(id="zzzzzz").title,
        ),
        (
            "bogus_username",
            lambda: list(reddit.redditor("user_does_not_exist_zzzzzz_99999").submissions.new(limit=1)),
        ),
    ]

    for label, action in cases:
        try:
            action()
            out["exceptions_observed"][label] = "no exception (unexpected — note this)"
        except Exception as e:
            out["exceptions_observed"][label] = f"{type(e).__module__}.{type(e).__name__}: {e}"

    out["details"]["headroom_post"] = headroom(reddit)
    return out


def probe_more_comments(reddit: praw.Reddit) -> dict:
    """Verify replace_more cap on a moderately-sized thread.

    Picks a thread with 200–3000 comments (avoids AskReddit-top-all-time, which
    is 50k+ and would burn 10%+ of the daily rate budget on this single probe).
    Uses limit=2 so worst case is 2 extra /api/morechildren calls, not 10.
    """
    out = {"name": "more_comments", "details": {}, "errors": []}
    try:
        # Find a manageably-sized thread by scanning a few hot listings
        candidate = None
        for s in reddit.subreddit("python").top(time_filter="month", limit=15):
            if 200 <= s.num_comments <= 3000:
                candidate = s
                break
        if candidate is None:
            out["errors"].append(
                "No thread with 200-3000 comments found in r/python top/month — "
                "fall back: re-run after picking a different sub."
            )
            return out

        pre = headroom(reddit)
        t0 = time.time()
        candidate.comments.replace_more(limit=2)  # cap at 2 extra calls
        elapsed = time.time() - t0
        # Count top-level only AND total flattened — both are useful signals.
        top_level_after = len(list(candidate.comments))
        total_flattened = len(candidate.comments.list())
        out["details"] = {
            "thread": {
                "fullname": candidate.fullname,
                "subreddit": str(candidate.subreddit),
                "title": candidate.title[:80],
                "num_comments_official": candidate.num_comments,
            },
            "top_level_comments_after_replace_more_limit_2": top_level_after,
            "total_comments_flattened_after_replace_more_limit_2": total_flattened,
            "wall_time_seconds": round(elapsed, 2),
            "headroom_pre": pre,
            "headroom_post": headroom(reddit),
            "interpretation": (
                "If `total_flattened` is much less than `num_comments_official`, "
                "the cap is working — the gap represents comments that would have "
                "required additional /api/morechildren calls."
            ),
        }
    except Exception as e:
        out["errors"].append(f"{type(e).__module__}.{type(e).__name__}: {e}")
    return out


BENCHMARK_QUERIES = [
    {
        "name": "search_r_ml_transformer",
        "kind": "search",
        "subreddit": "MachineLearning",
        "query": "transformer optimization",
        "limit": 5,
    },
    {
        "name": "search_r_python_asyncio",
        "kind": "search",
        "subreddit": "python",
        "query": "asyncio performance",
        "limit": 5,
    },
    {
        "name": "listing_hot_r_technology",
        "kind": "listing",
        "subreddit": "technology",
        "sort": "hot",
        "limit": 10,
    },
    {
        "name": "search_all_claude_caching",
        "kind": "search_all",
        "query": "claude api caching",
        "limit": 5,
    },
    {
        "name": "listing_top_month_askhistorians",
        "kind": "listing",
        "subreddit": "AskHistorians",
        "sort": "top",
        "time_filter": "month",
        "limit": 5,
    },
]


def run_listing(reddit: praw.Reddit, q: dict) -> list:
    if q["kind"] == "search":
        return list(
            reddit.subreddit(q["subreddit"]).search(q["query"], limit=q["limit"])
        )
    if q["kind"] == "search_all":
        return list(reddit.subreddit("all").search(q["query"], limit=q["limit"]))
    if q["kind"] == "listing":
        sub = reddit.subreddit(q["subreddit"])
        if q["sort"] == "hot":
            return list(sub.hot(limit=q["limit"]))
        if q["sort"] == "new":
            return list(sub.new(limit=q["limit"]))
        if q["sort"] == "top":
            return list(sub.top(time_filter=q.get("time_filter", "all"), limit=q["limit"]))
    raise ValueError(f"unknown query kind: {q['kind']}")


def probe_benchmarks(reddit: praw.Reddit) -> dict:
    out = {"name": "benchmarks", "queries": []}

    for q in BENCHMARK_QUERIES:
        result = {"query": q, "paths": {}}

        # --- Path 1: list only (search or listing, just the result page) ---
        path1 = {"name": "list_only"}
        path1["headroom_pre"] = headroom(reddit)
        t0 = time.time()
        listing = []
        try:
            listing = run_listing(reddit, q)
            # Touch fields so __dict__ populates for full_payload_size
            for s in listing:
                _ = (s.title, s.score, s.num_comments, s.author, s.selftext)
            sig_total = sum(len(json.dumps(submission_signal(s))) for s in listing)
            full_total = sum(full_payload_size(s) for s in listing)
            path1.update(
                {
                    "wall_time_seconds": round(time.time() - t0, 2),
                    "thread_count": len(listing),
                    "signal_bytes": sig_total,
                    "full_bytes": full_total,
                    "signal_to_noise_ratio": round(sig_total / full_total, 4)
                    if full_total > 0
                    else None,
                    "tokens_signal": count_tokens(
                        json.dumps([submission_signal(s) for s in listing])
                    ),
                }
            )
        except Exception as e:
            path1["error"] = f"{type(e).__module__}.{type(e).__name__}: {e}"
        path1["headroom_post"] = headroom(reddit)
        result["paths"]["list_only"] = path1

        # --- Path 2: list + get_thread for first 3 (top 20 *top-level* comments each) ---
        # Bias note: sampling first-3 of N favors highest-ranked items. For the SNR
        # ratio (per-thread JSON shape) this is fine; for absolute volume estimates
        # it's biased. Documented in phase0_notes.md.
        path2 = {"name": "list_plus_threads_top20", "_sampling_note": "first 3 of listing"}
        path2["headroom_pre"] = headroom(reddit)
        threads = []  # used by Path 3 too
        if not listing:
            path2["error"] = "no listing — see list_only error"
        else:
            try:
                t0 = time.time()
                for s in listing[:3]:
                    s.comments.replace_more(limit=0)  # remove MoreComments, no expansion
                    # IMPORTANT: iterate s.comments (top-level only), NOT
                    # s.comments.list() which flattens to all depths and would
                    # include nested replies — wrong shape vs what
                    # get_thread(top_n_comments=20) will return in v0.1.
                    top_level = [c for c in s.comments if hasattr(c, "score")]
                    top_comments = sorted(top_level, key=lambda c: c.score, reverse=True)[:20]
                    for c in top_comments:
                        _ = (c.body, c.score, c.author, c.created_utc)
                    threads.append({"submission": s, "comments": top_comments})

                sig = sum(
                    len(json.dumps(submission_signal(t["submission"])))
                    + sum(len(json.dumps(comment_signal(c))) for c in t["comments"])
                    for t in threads
                )
                full = sum(
                    full_payload_size(t["submission"])
                    + sum(full_payload_size(c) for c in t["comments"])
                    for t in threads
                )
                sig_text = json.dumps(
                    [
                        {
                            **submission_signal(t["submission"]),
                            "comments": [comment_signal(c) for c in t["comments"]],
                        }
                        for t in threads
                    ]
                )
                path2.update(
                    {
                        "wall_time_seconds": round(time.time() - t0, 2),
                        "threads_pulled": len(threads),
                        "comments_total": sum(len(t["comments"]) for t in threads),
                        "signal_bytes": sig,
                        "full_bytes": full,
                        "signal_to_noise_ratio": round(sig / full, 4)
                        if full > 0
                        else None,
                        "tokens_signal": count_tokens(sig_text),
                    }
                )
            except Exception as e:
                path2["error"] = f"{type(e).__module__}.{type(e).__name__}: {e}"
        path2["headroom_post"] = headroom(reddit)
        result["paths"]["list_plus_threads_top20"] = path2

        # --- Path 3: Path 2 + expand_comment on highest-scored top-level comment with replies ---
        # Models the LLM's adaptive workflow: see top comments, follow one promising
        # sub-tree. Per CLAUDE.md Phase 0 step 4 — measures the marginal cost of the
        # adaptive step.
        path3 = {"name": "list_plus_threads_plus_expand_one", "_sampling_note": "expand 1 top-level comment per thread, depth=1"}
        path3["headroom_pre"] = headroom(reddit)
        if not threads:
            path3["error"] = "no threads — see list_plus_threads_top20 error"
        else:
            try:
                t0 = time.time()
                expansions = []  # list of (parent_comment, [reply_comments])
                for t in threads:
                    candidate = next(
                        (c for c in t["comments"] if getattr(c, "replies", None)
                         and len(list(c.replies)) > 0),
                        None,
                    )
                    if candidate is None:
                        continue
                    candidate.replies.replace_more(limit=1)  # one extra call max
                    reply_top_level = [r for r in candidate.replies if hasattr(r, "score")]
                    for r in reply_top_level:
                        _ = (r.body, r.score, r.author, r.created_utc)
                    expansions.append((candidate, reply_top_level))

                # Marginal cost: bytes/tokens of just the new replies on top of Path 2
                marginal_sig = sum(
                    sum(len(json.dumps(comment_signal(r))) for r in replies)
                    for _, replies in expansions
                )
                marginal_full = sum(
                    sum(full_payload_size(r) for r in replies)
                    for _, replies in expansions
                )
                marginal_text = json.dumps(
                    [[comment_signal(r) for r in replies] for _, replies in expansions]
                )
                path3.update(
                    {
                        "wall_time_seconds": round(time.time() - t0, 2),
                        "threads_expanded": len(expansions),
                        "replies_pulled": sum(len(r) for _, r in expansions),
                        "marginal_signal_bytes": marginal_sig,
                        "marginal_full_bytes": marginal_full,
                        "marginal_signal_to_noise_ratio": round(marginal_sig / marginal_full, 4)
                        if marginal_full > 0 else None,
                        "marginal_tokens_signal": count_tokens(marginal_text),
                    }
                )
            except Exception as e:
                path3["error"] = f"{type(e).__module__}.{type(e).__name__}: {e}"
        path3["headroom_post"] = headroom(reddit)
        result["paths"]["list_plus_threads_plus_expand_one"] = path3

        out["queries"].append(result)

    return out


# ----- Output ---------------------------------------------------------------


def write_outputs(probes: list[dict]) -> None:
    summary = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "tokenizer": TOKENIZER,
        "tokenizer_note": (
            "tiktoken cl100k_base is an approximation of Anthropic's tokenizer. "
            "Difference is typically 5-15% (worse on code-heavy content, up to ~20%). "
            "Ratios across queries are stable; absolute counts are advisory. "
            "Use Anthropic's messages.count_tokens API later to verify if absolute "
            "numbers drive a v0.2 budget decision."
        ),
        "probes": probes,
    }
    BENCH_JSON_PATH.write_text(json.dumps(summary, indent=2, default=str))

    # Markdown bench
    md = [
        "# Phase 0 Benchmarks",
        "",
        f"- Fetched: {summary['fetched_at']}",
        f"- Tokenizer: {TOKENIZER}",
        "",
    ]
    for p in probes:
        md.append(f"## Probe: {p['name']}")
        md.append("")
        if p["name"] == "benchmarks":
            for q in p.get("queries", []):
                md.append(f"### {q['query']['name']}")
                md.append("")
                md.append(f"Query: `{json.dumps(q['query'])}`")
                md.append("")
                for path_name, path in q.get("paths", {}).items():
                    if "error" in path:
                        md.append(f"- **{path_name}**: ERROR — {path['error']}")
                    elif "marginal_signal_bytes" in path:
                        # Path 3: marginal-cost framing
                        ratio = path.get("marginal_signal_to_noise_ratio")
                        ratio_str = f"{ratio:.1%}" if ratio is not None else "?"
                        md.append(
                            f"- **{path_name}** (marginal): "
                            f"{path.get('threads_expanded', '?')} expanded"
                            + f" · {path.get('replies_pulled', 0)} replies"
                            + f" · {path.get('wall_time_seconds')}s"
                            + f" · signal {path.get('marginal_signal_bytes', 0):,}B"
                            + f" / full {path.get('marginal_full_bytes', 0):,}B"
                            + f" · ratio {ratio_str}"
                            + f" · ~{path.get('marginal_tokens_signal', 0):,} signal tokens"
                        )
                        continue
                    else:
                        ratio = path.get("signal_to_noise_ratio")
                        ratio_str = f"{ratio:.1%}" if ratio is not None else "?"
                        md.append(
                            f"- **{path_name}**: "
                            f"{path.get('thread_count', path.get('threads_pulled', '?'))} threads"
                            + (
                                f" · {path['comments_total']} comments"
                                if "comments_total" in path
                                else ""
                            )
                            + f" · {path.get('wall_time_seconds')}s"
                            + f" · signal {path.get('signal_bytes', 0):,}B"
                            + f" / full {path.get('full_bytes', 0):,}B"
                            + f" · ratio {ratio_str}"
                            + f" · ~{path.get('tokens_signal', 0):,} signal tokens"
                        )
                md.append("")
        else:
            md.append("```json")
            md.append(json.dumps(p, indent=2, default=str))
            md.append("```")
            md.append("")
    BENCH_MD_PATH.write_text("\n".join(md))

    # Notes — anomalies + the key things to write down
    notes = [
        "# Phase 0 Notes",
        "",
        f"Spike: {summary['fetched_at']}",
        "",
        "## What to verify by hand against this run",
        "",
        "1. **Auth probe** should show `ok: True` and `headroom_populated: true`. "
        "If `headroom_populated: false`, PRAW isn't exposing rate-limit headroom "
        "on this auth flow — the `status` command's headroom display won't work, "
        "and we'd need to add a request-layer hook. (Note: with `read_only=True`, "
        "`reddit.user.me()` returns None — that's expected, not a failure.)",
        "2. **Errors probe** should map each label (`nonexistent_subreddit`, "
        "`bogus_thread_id`, `bogus_username`) to a distinct `prawcore` exception "
        "type (NotFound, Redirect, Forbidden, etc.). Lock those into a future "
        "`core/errors.py` mapping.",
        "3. **MoreComments probe** — `total_comments_flattened_after_replace_more_limit_2` "
        "should be much less than `num_comments_official`. If they're equal, the cap "
        "isn't doing what we think.",
        "4. **Benchmarks** — note the **signal-to-noise ratio** per path. That's the "
        "ceiling for v0.2 markdown-preprocessor token savings. If already >50%, "
        "preprocessing won't help much; if <25%, preprocessing is the highest-leverage "
        "v0.2 change. **Path 3's `marginal_signal_to_noise_ratio`** captures the same "
        "for the adaptive expand-comment step in isolation.",
        "",
        "## Known measurement caveats",
        "",
        "- **Path 2 + 3 sample only the first 3 of N listing items** to limit rate "
        "budget. This biases absolute volumes toward highest-ranked items but the "
        "per-thread JSON shape is consistent across rank, so the SNR ratio is "
        "stable. If absolute volume matters, expand sample size in a follow-up run.",
        "- **Tokenizer is tiktoken cl100k_base** (OpenAI). Anthropic's tokenizer is "
        "5–15% different (up to ~20% on code-heavy content). Ratios across queries "
        "are stable; absolute token counts are advisory.",
        "- **`full_payload_size` filters PRAW internals** (anything starting with `_`) "
        "to avoid the `_reddit` back-reference inflating sizes 5–50x. What's left is "
        "approximately the API payload PRAW lazy-loaded — directionally correct for "
        "ratios, not byte-exact.",
        "",
        "## Probe results (errors only — see phase0_benchmarks.{md,json} for the rest)",
        "",
    ]
    for p in probes:
        if p.get("errors"):
            notes.append(f"### {p['name']} — errors")
            for e in p["errors"]:
                notes.append(f"- {e}")
            notes.append("")
        if p["name"] == "errors":
            notes.append("### Exception map (lock into `core/errors.py`)")
            for label, exc in p.get("exceptions_observed", {}).items():
                notes.append(f"- **{label}**: `{exc}`")
            notes.append("")
    NOTES_PATH.write_text("\n".join(notes))


# ----- Main -----------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--probe",
        default="all",
        choices=["all", "auth", "errors", "morechildren", "benchmarks"],
    )
    args = parser.parse_args()

    print(f"reddit_research Phase 0 spike  ·  tokenizer: {TOKENIZER}")
    reddit = load_reddit()
    print(f"  authenticated user: {os.environ['REDDIT_USERNAME']}")
    print(f"  user-agent: {os.environ['REDDIT_USER_AGENT']}")

    probes = []
    runners = [
        ("auth", probe_auth),
        ("errors", probe_errors),
        ("morechildren", probe_more_comments),
        ("benchmarks", probe_benchmarks),
    ]
    for i, (name, fn) in enumerate(runners, start=1):
        if args.probe in ("all", name):
            print(f"\n[{i}/{len(runners)}] {name}...")
            probes.append(fn(reddit))

    write_outputs(probes)
    print(
        f"\nWrote: {NOTES_PATH.name}, {BENCH_MD_PATH.name}, {BENCH_JSON_PATH.name}"
    )

    print("\n=== Summary ===")
    for p in probes:
        if p.get("errors"):
            print(f"  {p['name']}: ERRORS — {p['errors'][0]}")
        elif "ok" in p:
            print(f"  {p['name']}: ok={p['ok']}")
        else:
            print(f"  {p['name']}: see outputs")


if __name__ == "__main__":
    main()
