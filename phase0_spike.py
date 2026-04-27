#!/usr/bin/env python3
"""Phase 0 spike — verify Reddit .json transport behavior + measure baseline costs.

Targets Reddit's public .json endpoints (no auth needed). Probes for the four
things we need to know before writing the library:

    1. Connectivity        — UA accepted, headroom headers present.
    2. Error mapping       — 404 / 403 (with structured `reason`) distinguishable.
    3. MoreComments cap    — /api/morechildren.json with capped child list.
    4. Benchmarks          — for 5 realistic queries, measure three paths
                              (list_only, list_plus_threads_top20,
                              list_plus_threads_plus_expand_one) with API call
                              count, wall time, signal-to-noise ratio, token count.

Outputs:
    phase0_notes.md
    phase0_benchmarks.md
    phase0_benchmarks.json

Setup:
    cd /home/elib/code/reddit_api
    python3 -m venv venv && source venv/bin/activate
    pip install -r requirements-phase0.txt
    cp .env.example .env  # only REDDIT_USER_AGENT matters; defaults are fine

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
from typing import Any

try:
    import httpx
except ImportError:
    sys.exit("Missing httpx — run: pip install -r requirements-phase0.txt")

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

DEFAULT_UA = "reddit-research:0.1 (by /u/joeblowfromidaho)"


# ----- HTTP client ----------------------------------------------------------


class RedditJSONClient:
    """Thin httpx wrapper that observes Reddit's rate-limit headers.

    No auth. No PRAW. No write capability — `.json` doesn't expose it. Returns
    parsed JSON + status code; transport errors raise.
    """

    BASE = "https://www.reddit.com"

    def __init__(self, user_agent: str, timeout: float = 30.0) -> None:
        self.client = httpx.Client(
            base_url=self.BASE,
            headers={"User-Agent": user_agent},
            timeout=timeout,
            follow_redirects=True,
        )
        self.headroom: dict | None = None
        self.api_call_count = 0
        self.user_agent = user_agent

    def get(self, path: str, params: dict | None = None) -> tuple[Any, int]:
        """Issue a GET, observe rate-limit headers, return (parsed_json, status)."""
        self.api_call_count += 1
        r = self.client.get(path, params=params)

        used = r.headers.get("x-ratelimit-used")
        rem = r.headers.get("x-ratelimit-remaining")
        rst = r.headers.get("x-ratelimit-reset")
        if used is not None or rem is not None or rst is not None:
            self.headroom = {
                "used": float(used) if used is not None else None,
                "remaining": float(rem) if rem is not None else None,
                "reset_in_seconds": float(rst) if rst is not None else None,
            }

        try:
            body: Any = r.json()
        except Exception:
            body = {"_error": "non-json response", "_text": r.text[:500]}
        return body, r.status_code

    def close(self) -> None:
        self.client.close()


# ----- Helpers --------------------------------------------------------------


def count_tokens(text: str) -> int:
    """Approximate token count; cl100k_base for ratios."""
    if _ENC is not None:
        return len(_ENC.encode(text))
    return max(1, len(text) // 4)


def post_signal(d: dict) -> dict:
    """The fields an LLM actually needs from a post. Drives signal-to-noise."""
    return {
        "title": d.get("title", ""),
        "body": d.get("selftext", ""),
        "author": d.get("author", "[deleted]"),
        "score": d.get("score", 0),
        "created_utc": d.get("created_utc", 0),
        "num_comments": d.get("num_comments", 0),
        "permalink": d.get("permalink", ""),
        "subreddit": d.get("subreddit", ""),
    }


def comment_signal(d: dict) -> dict:
    return {
        "body": d.get("body", ""),
        "author": d.get("author", "[deleted]"),
        "score": d.get("score", 0),
        "created_utc": d.get("created_utc", 0),
    }


def full_size(body: Any) -> int:
    """Honest full payload size — what Reddit actually returned."""
    try:
        return len(json.dumps(body, default=str))
    except Exception:
        return -1


def walk_comment_tree(items: list, into: list) -> None:
    """Recursively collect every Comment dict from a comment-tree listing."""
    for item in items:
        kind = item.get("kind")
        if kind != "t1":
            continue
        d = item["data"]
        into.append(d)
        replies = d.get("replies")
        if isinstance(replies, dict):
            walk_comment_tree(replies.get("data", {}).get("children", []), into)


# ----- Probes ---------------------------------------------------------------


def probe_auth(client: RedditJSONClient) -> dict:
    """Confirm UA accepted, headroom headers populated, sample fetch succeeds."""
    out: dict = {"name": "auth", "ok": False, "details": {}, "errors": []}
    try:
        body, status = client.get("/r/python/hot.json", {"limit": 1})
        out["details"]["status"] = status
        out["details"]["headroom"] = client.headroom
        out["details"]["headroom_populated"] = (
            client.headroom is not None
            and client.headroom.get("remaining") is not None
        )
        if status == 200 and isinstance(body, dict) and body.get("kind") == "Listing":
            out["ok"] = True
            children = body.get("data", {}).get("children", [])
            if children:
                first = children[0]["data"]
                out["details"]["sample_thread"] = {
                    "fullname": "t3_" + first.get("id", ""),
                    "title": first.get("title", "")[:80],
                    "num_comments": first.get("num_comments"),
                }
        else:
            out["errors"].append(
                f"unexpected response: status={status}, body type={type(body).__name__}"
            )
    except Exception as e:
        out["errors"].append(f"{type(e).__module__}.{type(e).__name__}: {e}")
    return out


def probe_errors(client: RedditJSONClient) -> dict:
    """Map status codes + structured reason fields for known error cases."""
    out: dict = {"name": "errors", "responses": {}}
    cases = [
        (
            "nonexistent_subreddit",
            "/r/this_does_not_exist_zzz_qwerty99/hot.json",
            None,
        ),
        ("bogus_thread_id", "/comments/zzzzzz.json", None),
        ("premium_only_sub", "/r/lounge.json", None),
    ]
    for label, path, params in cases:
        try:
            body, status = client.get(path, params)
            entry: dict = {"status": status}
            if isinstance(body, dict):
                entry["body_keys"] = list(body.keys())
                entry["reason"] = body.get("reason")
                entry["message"] = body.get("message")
                entry["error"] = body.get("error")
            else:
                entry["body_type"] = type(body).__name__
            out["responses"][label] = entry
        except Exception as e:
            out["responses"][label] = {
                "transport_error": f"{type(e).__name__}: {e}"
            }
    out["headroom_post"] = client.headroom
    return out


def probe_more_comments(client: RedditJSONClient) -> dict:
    """Verify /api/morechildren.json behavior with a capped child list.

    Picks a thread with 200–3000 comments (avoids the 50k+ AskReddit-top-all-time
    pitfall). Sends a 10-id child list to constrain rate-budget cost to one call.
    """
    out: dict = {"name": "more_comments", "details": {}, "errors": []}
    try:
        listing, status = client.get("/r/python/top.json", {"limit": 15, "t": "month"})
        if status != 200:
            out["errors"].append(f"listing fetch failed: status={status}")
            return out

        candidate = None
        for c in listing["data"]["children"]:
            d = c["data"]
            if 200 <= d.get("num_comments", 0) <= 3000:
                candidate = d
                break
        if candidate is None:
            out["errors"].append(
                "no thread with 200-3000 comments in r/python top/month — try a different sub"
            )
            return out

        thread_body, t_status = client.get(
            f"/comments/{candidate['id']}.json", {"limit": 100}
        )
        if t_status != 200:
            out["errors"].append(f"thread fetch failed: status={t_status}")
            return out

        more_marker = None
        for child in thread_body[1]["data"]["children"]:
            if child["kind"] == "more":
                more_marker = child["data"]
                break

        if not more_marker:
            out["details"]["no_more_marker"] = (
                "thread had no MoreComments — single fetch returned everything. "
                "Cap behavior cannot be exercised; pick a busier thread next time."
            )
            return out

        children_to_request = more_marker["children"][:10]
        link_id = "t3_" + candidate["id"]

        t0 = time.time()
        more_body, m_status = client.get(
            "/api/morechildren.json",
            {
                "api_type": "json",
                "link_id": link_id,
                "children": ",".join(children_to_request),
            },
        )
        elapsed = time.time() - t0

        things = []
        if isinstance(more_body, dict):
            things = more_body.get("json", {}).get("data", {}).get("things", [])

        out["details"] = {
            "thread": {
                "id": candidate["id"],
                "title": candidate["title"][:80],
                "num_comments_official": candidate["num_comments"],
            },
            "more_marker_total_children": len(more_marker["children"]),
            "more_marker_count_field": more_marker.get("count"),
            "children_requested": len(children_to_request),
            "things_returned": len(things),
            "morechildren_status": m_status,
            "wall_time_seconds": round(elapsed, 2),
            "headroom_post": client.headroom,
            "interpretation": (
                "/api/morechildren.json takes a capped child-id list and returns at "
                "most that many things — one HTTP call regardless of list length. "
                "If `things_returned` < `children_requested`, some children were "
                "deleted/removed (expected, not a failure)."
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


def run_listing(client: RedditJSONClient, q: dict) -> tuple[Any, int]:
    if q["kind"] == "search":
        return client.get(
            f"/r/{q['subreddit']}/search.json",
            {"q": q["query"], "restrict_sr": 1, "sort": "relevance", "limit": q["limit"]},
        )
    if q["kind"] == "search_all":
        return client.get(
            "/search.json",
            {"q": q["query"], "sort": "relevance", "limit": q["limit"]},
        )
    if q["kind"] == "listing":
        sort = q["sort"]
        params: dict = {"limit": q["limit"]}
        if sort == "top":
            params["t"] = q.get("time_filter", "all")
        return client.get(f"/r/{q['subreddit']}/{sort}.json", params)
    raise ValueError(f"unknown query kind: {q['kind']}")


def probe_benchmarks(client: RedditJSONClient) -> dict:
    out: dict = {"name": "benchmarks", "queries": []}

    for q in BENCHMARK_QUERIES:
        result: dict = {"query": q, "paths": {}}

        # ---------- Path 1: list_only ----------
        path1: dict = {"name": "list_only"}
        path1["headroom_pre"] = client.headroom
        listing_body: Any = None
        listing_status = 0
        listing_children: list = []
        try:
            t0 = time.time()
            listing_body, listing_status = run_listing(client, q)
            path1["wall_time_seconds"] = round(time.time() - t0, 2)
            path1["status"] = listing_status
            if listing_status == 200 and isinstance(listing_body, dict):
                listing_children = listing_body.get("data", {}).get("children", [])
                path1["thread_count"] = len(listing_children)
                path1["full_bytes"] = full_size(listing_body)
                path1["signal_bytes"] = sum(
                    len(json.dumps(post_signal(c["data"]))) for c in listing_children
                )
                path1["signal_to_noise_ratio"] = (
                    round(path1["signal_bytes"] / path1["full_bytes"], 4)
                    if path1["full_bytes"] > 0
                    else None
                )
                sig_text = json.dumps(
                    [post_signal(c["data"]) for c in listing_children]
                )
                path1["tokens_signal"] = count_tokens(sig_text)
            else:
                path1["error"] = f"non-200 status: {listing_status}"
        except Exception as e:
            path1["error"] = f"{type(e).__module__}.{type(e).__name__}: {e}"
        path1["headroom_post"] = client.headroom
        result["paths"]["list_only"] = path1

        # ---------- Path 2: list_plus_threads_top20 ----------
        # Sample first 3 of N to limit rate-budget cost. Per-thread JSON shape is
        # consistent across rank, so SNR ratio is stable; absolute volume is biased.
        path2: dict = {
            "name": "list_plus_threads_top20",
            "_sampling_note": "first 3 of listing",
        }
        path2["headroom_pre"] = client.headroom
        threads: list = []  # passed to Path 3
        if not listing_children:
            path2["error"] = "no listing — see list_only"
        else:
            try:
                t0 = time.time()
                full_total = 0
                for c in listing_children[:3]:
                    pid = c["data"]["id"]
                    t_body, t_status = client.get(
                        f"/comments/{pid}.json", {"limit": 20}
                    )
                    if t_status != 200 or not isinstance(t_body, list) or len(t_body) < 2:
                        continue
                    full_total += full_size(t_body)
                    post_data = t_body[0]["data"]["children"][0]["data"]
                    top_level = [
                        cc["data"]
                        for cc in t_body[1]["data"]["children"]
                        if cc.get("kind") == "t1"
                    ]
                    top_20 = sorted(
                        top_level, key=lambda d: d.get("score", 0), reverse=True
                    )[:20]
                    threads.append({"post": post_data, "comments": top_20})

                path2["wall_time_seconds"] = round(time.time() - t0, 2)
                path2["threads_pulled"] = len(threads)
                path2["comments_total"] = sum(len(t["comments"]) for t in threads)
                path2["full_bytes"] = full_total
                path2["signal_bytes"] = sum(
                    len(json.dumps(post_signal(t["post"])))
                    + sum(len(json.dumps(comment_signal(c))) for c in t["comments"])
                    for t in threads
                )
                path2["signal_to_noise_ratio"] = (
                    round(path2["signal_bytes"] / path2["full_bytes"], 4)
                    if path2["full_bytes"] > 0
                    else None
                )
                sig_text = json.dumps(
                    [
                        {
                            **post_signal(t["post"]),
                            "comments": [comment_signal(c) for c in t["comments"]],
                        }
                        for t in threads
                    ]
                )
                path2["tokens_signal"] = count_tokens(sig_text)
            except Exception as e:
                path2["error"] = f"{type(e).__module__}.{type(e).__name__}: {e}"
        path2["headroom_post"] = client.headroom
        result["paths"]["list_plus_threads_top20"] = path2

        # ---------- Path 3: list_plus_threads_plus_expand_one ----------
        # For each Path 2 thread, pick the highest-scored top-level comment with
        # a 'replies' subtree (more potential value to expand) and fetch it via
        # /r/<sub>/comments/<thread>/_/<comment>.json — single dedicated call,
        # returns the comment with its full reply tree in context.
        # Marginal bytes/tokens = the additional payload from this expand step.
        path3: dict = {
            "name": "list_plus_threads_plus_expand_one",
            "_sampling_note": "highest-scored top-level comment per thread, single expand call",
        }
        path3["headroom_pre"] = client.headroom
        if not threads:
            path3["error"] = "no threads — see list_plus_threads_top20"
        else:
            try:
                t0 = time.time()
                expansions: list = []
                marginal_full = 0
                marginal_signal = 0
                marginal_text_parts: list = []
                for t in threads:
                    if not t["comments"]:
                        continue
                    candidate = t["comments"][0]
                    comment_id = candidate["id"]
                    thread_id = t["post"]["id"]
                    sub = t["post"]["subreddit"]
                    exp_body, exp_status = client.get(
                        f"/r/{sub}/comments/{thread_id}/_/{comment_id}.json",
                        {"limit": 20, "depth": 5},
                    )
                    if exp_status != 200 or not isinstance(exp_body, list) or len(exp_body) < 2:
                        continue
                    subtree: list = []
                    walk_comment_tree(
                        exp_body[1]["data"]["children"], subtree
                    )
                    exp_full = full_size(exp_body)
                    exp_signal = sum(
                        len(json.dumps(comment_signal(c))) for c in subtree
                    )
                    marginal_full += exp_full
                    marginal_signal += exp_signal
                    marginal_text_parts.append(
                        json.dumps([comment_signal(c) for c in subtree])
                    )
                    expansions.append(
                        {
                            "thread_id": thread_id,
                            "comment_id": comment_id,
                            "subtree_comments_total": len(subtree),
                            "subtree_full_bytes": exp_full,
                            "subtree_signal_bytes": exp_signal,
                        }
                    )

                path3["wall_time_seconds"] = round(time.time() - t0, 2)
                path3["threads_expanded"] = len(expansions)
                path3["total_subtree_comments"] = sum(
                    e["subtree_comments_total"] for e in expansions
                )
                path3["marginal_full_bytes"] = marginal_full
                path3["marginal_signal_bytes"] = marginal_signal
                path3["marginal_signal_to_noise_ratio"] = (
                    round(marginal_signal / marginal_full, 4)
                    if marginal_full > 0
                    else None
                )
                path3["marginal_tokens_signal"] = count_tokens(
                    "[" + ",".join(marginal_text_parts) + "]"
                )
                path3["expansions"] = expansions
            except Exception as e:
                path3["error"] = f"{type(e).__module__}.{type(e).__name__}: {e}"
        path3["headroom_post"] = client.headroom
        result["paths"]["list_plus_threads_plus_expand_one"] = path3

        out["queries"].append(result)

    return out


# ----- Output ---------------------------------------------------------------


def write_outputs(probes: list[dict], client: RedditJSONClient) -> None:
    summary = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "tokenizer": TOKENIZER,
        "tokenizer_note": (
            "tiktoken cl100k_base is an approximation of Anthropic's tokenizer. "
            "Difference is typically 5-15% (worse on code-heavy content, up to ~20%). "
            "Ratios across queries are stable; absolute counts are advisory."
        ),
        "transport": "Reddit .json (unauthenticated)",
        "user_agent": client.user_agent,
        "total_api_calls": client.api_call_count,
        "final_headroom": client.headroom,
        "probes": probes,
    }
    BENCH_JSON_PATH.write_text(json.dumps(summary, indent=2, default=str))

    # Markdown bench
    md = [
        "# Phase 0 Benchmarks",
        "",
        f"- Fetched: {summary['fetched_at']}",
        f"- Transport: {summary['transport']}",
        f"- Tokenizer: {TOKENIZER}",
        f"- Total HTTP calls this run: {summary['total_api_calls']}",
        f"- Final rate-limit headroom: {summary['final_headroom']}",
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
                        continue
                    if "marginal_signal_bytes" in path:
                        ratio = path.get("marginal_signal_to_noise_ratio")
                        ratio_str = f"{ratio:.1%}" if ratio is not None else "?"
                        md.append(
                            f"- **{path_name}** (marginal): "
                            f"{path.get('threads_expanded', '?')} expansions"
                            + f" · {path.get('total_subtree_comments', 0)} subtree comments"
                            + f" · {path.get('wall_time_seconds')}s"
                            + f" · signal {path.get('marginal_signal_bytes', 0):,}B"
                            + f" / full {path.get('marginal_full_bytes', 0):,}B"
                            + f" · ratio {ratio_str}"
                            + f" · ~{path.get('marginal_tokens_signal', 0):,} signal tokens"
                        )
                        continue
                    ratio = path.get("signal_to_noise_ratio")
                    ratio_str = f"{ratio:.1%}" if ratio is not None else "?"
                    extras = ""
                    if "comments_total" in path:
                        extras = f" · {path['comments_total']} comments"
                    md.append(
                        f"- **{path_name}**: "
                        f"{path.get('thread_count', path.get('threads_pulled', '?'))} threads"
                        + extras
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

    # Notes — anomalies + things to verify by hand
    notes = [
        "# Phase 0 Notes",
        "",
        f"Spike: {summary['fetched_at']}  ·  {summary['total_api_calls']} HTTP calls total",
        "",
        "## What to verify by hand against this run",
        "",
        "1. **Auth probe** should show `ok: True` and `headroom_populated: true`. "
        "If `headroom_populated: false`, Reddit isn't returning the X-Ratelimit-* "
        "headers via this transport — the rate-limit observability assumption falls "
        "apart and we'd need to fall back to fixed-rate pacing.",
        "2. **Errors probe** should map each label to a distinguishable status + "
        "structured `reason` field (especially for 403). Lock those into a future "
        "`core/errors.py` exception map.",
        "3. **MoreComments probe** — if `things_returned` < `children_requested`, "
        "that's expected (deleted/removed comments). What we're verifying is that "
        "the call costs ONE HTTP request regardless of child-list length.",
        "4. **Benchmarks** — note **signal-to-noise ratio** per path. That's the "
        "ceiling for v0.2 markdown-preprocessor token savings. If already >50%, "
        "preprocessing won't help much; if <25%, preprocessing is the highest-leverage "
        "v0.2 change. Path 3's `marginal_signal_to_noise_ratio` measures the same for "
        "the adaptive expand-comment step in isolation.",
        "",
        "## Known measurement caveats",
        "",
        "- **Path 2 + 3 sample only the first 3 of N listing items** to limit rate "
        "budget. Per-thread JSON shape is consistent across rank, so the SNR ratio "
        "is stable; absolute volume estimates are biased toward highest-ranked items.",
        "- **Tokenizer is tiktoken cl100k_base** (OpenAI). Anthropic's tokenizer is "
        "5-15% different (up to ~20% on code-heavy content). Ratios across queries "
        "are stable; absolute token counts are advisory.",
        "- **`full_bytes` is the actual JSON response size** Reddit returned — no "
        "object-graph noise from a wrapper library, so SNR ratios are trustworthy.",
        "",
        "## Probe results (errors only — see phase0_benchmarks.{md,json} for benchmarks)",
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
            for label, info in p.get("responses", {}).items():
                notes.append(f"- **{label}**: {json.dumps(info)}")
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

    load_dotenv(ROOT / ".env")
    user_agent = os.environ.get("REDDIT_USER_AGENT", DEFAULT_UA)
    client = RedditJSONClient(user_agent=user_agent)

    print(f"reddit_research Phase 0 spike  ·  transport: .json  ·  tokenizer: {TOKENIZER}")
    print(f"  user-agent: {user_agent}")

    probes: list[dict] = []
    runners = [
        ("auth", probe_auth),
        ("errors", probe_errors),
        ("morechildren", probe_more_comments),
        ("benchmarks", probe_benchmarks),
    ]
    try:
        for i, (name, fn) in enumerate(runners, start=1):
            if args.probe in ("all", name):
                print(f"\n[{i}/{len(runners)}] {name}...")
                probes.append(fn(client))
    finally:
        client.close()

    write_outputs(probes, client)
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
    print(f"  total HTTP calls: {client.api_call_count}")
    print(f"  final headroom: {client.headroom}")


if __name__ == "__main__":
    main()
