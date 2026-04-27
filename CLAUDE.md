# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A personal Reddit research tool. Single user. The use case is: **search subreddits for important / useful information for an LLM to summarize.** Read-only by design.

Source on warehouse-vm at `/home/elib/code/reddit_api/` (this directory). Runtime under a dedicated service user on the same VM.

**For new sessions:** read [`HANDOFF.md`](HANDOFF.md) first; it points at [`docs/manifest.json`](docs/manifest.json) which has the full machine-readable project state (operations, schema, decisions, caps, session log).

## Decisions (already made — don't relitigate)

1. **Language:** Python 3.11+.
2. **Reddit transport:** `httpx` against Reddit's public `.json` endpoints (e.g. `https://www.reddit.com/r/python/hot.json`, `/comments/<id>.json`, `/api/morechildren.json`, `/search.json`). No OAuth, no PRAW.
   - Verified working in 2026 with same JSON shape PRAW returns ([`docs/json_endpoint_findings.md`](docs/json_endpoint_findings.md)).
   - Returns `X-Ratelimit-*` headers cleanly (PRAW hides these — observability was a panel concern in earlier rounds).
   - ~100 calls per ~10-min sliding window, no auth handshake, no support-form approval needed.
   - Structurally read-only — there's no write path through `.json`, which makes the entire write-flag plumbing a no-op for MVP.
3. **OAuth + PRAW is the documented upgrade path**, not the MVP. Reasons we'd switch later: (a) higher rate limits, (b) write capability, (c) user-context operations like reading subscribed subs. None are MVP requirements. Reddit's API access form was submitted 2026-04-27 — if approved, we add OAuth as an *upgrade* alongside the existing `.json` transport, not as a replacement.
4. **Cache backend:** SQLite, single file, WAL mode, `busy_timeout=5000`, `PRAGMA synchronous=NORMAL`. See `## Cache design`.
5. **Packaging:** PEP 621 `pyproject.toml`, `src/` layout, `pip install -e .` for dev.
6. **MCP transport:** stdio only. Run on warehouse-vm; access from a laptop via SSH (`ssh warehouse reddit-mcp`). No network listener.
7. **Roadmap:** core library → CLI (debug + Eli's personal use) → MCP server. Preprocessing optimizations ship after the MVP.

## MVP scope (v0.1)

Smallest thing that works:

**Operations exposed by `reddit_research.core`:**

- `search(query, subreddit=None, sort='relevance', time='all', limit=25, fresh=False)` → list of thread summaries (title, score, author, sub, num_comments, permalink, fullname, created_utc).
- `get_subreddit_listing(name, sort='hot', limit=25, fresh=False)` → same shape.
- `get_thread(thread_id_or_fullname, top_n_comments=20, fresh=False)` → post + top N top-level comments by score, no sub-tree expansion. Cheap.
- `expand_comment(thread_id, comment_id, depth=2, limit=20, fresh=False)` → one comment + N levels of replies. Per-call hard caps: `depth ≤ 5`, `limit ≤ 50`. Implemented via `/r/<sub>/comments/<thread>/_/<comment>.json` (single dedicated call).
- `purge(older_than_days=30)` → delete cache rows older than N days.
- `status()` → cache size, cache hit rate, last-seen rate-limit headroom, request count this session.

**Input validation lives in core, not the MCP boundary.** All `id`/`fullname` parameters are validated against `^t[1-6]_[a-z0-9]+$` (or bare `[a-z0-9]+` for unprefixed) before being passed to the HTTP layer. CLI gets the same protection as MCP.

**Adapter (CLI):** `reddit-cli` with subcommands matching the operations. JSON or text output.

**Other:**

- `.env` (optional) with User-Agent customization. No secrets — there are no auth credentials to store with the `.json` transport.
- `smoke_test.py` — verifies one read call + one cache round-trip.
- `run_qa.py` — unit tests with mocked HTTP + in-memory SQLite. Matches portfolio testing pattern.

**Deferred to v0.2+:** MCP server adapter, preprocessed views (token-saving markdown transforms), OAuth + write capability (only if Reddit grants access), async, snapshots, multi-account.

## Architecture

```
src/reddit_research/
  core/
    client.py         # RedditJSONClient — httpx wrapper, rate-limit aware, retry/backoff
    operations.py     # search, get_thread, get_subreddit_listing, expand_comment
    cache.py          # SQLite with variable TTL by kind
    keys.py           # cache key normalization
    errors.py         # exception types mapped from HTTP status + reason field
    config.py         # env loading (User-Agent, cache dir)
  cli/                # argparse adapter. Imports core only.
  mcp/                # MCP server adapter (later). Imports core only.
```

If `core/` ever imports from `cli/` or `mcp/`, the layering broke. Adapters know about core; core never knows about adapters.

## Security

| Risk | Mitigation |
|---|---|
| Secrets at rest | None to manage — `.json` transport is unauthenticated. The optional `.env` only contains a User-Agent string. |
| Cache leaks research interests | Cache dir mode 0700, owned by deploy user only. |
| TLS bypass footgun | No knob to disable certificate verification. |
| MCP network exposure | Stdio transport only. Remote access via SSH. |
| LLM-supplied input | `id`/`fullname` validated in core (`^t[1-6]_[a-z0-9]+$` or `[a-z0-9]+`). All adapters get the protection. |
| Accidental writes | **Structurally impossible** through the `.json` transport — no write endpoints exposed. (When OAuth is added later, write-flag plumbing returns.) |
| Rate-limit failure | Read `X-Ratelimit-Remaining` on every response; back off proactively at <10% headroom. Honor 429 with `Retry-After`. Per-call + per-workflow caps on `expand_comment`. |
| Reddit ToS | Personal/non-commercial research only. If that changes, re-read API terms. |
| Reddit blocks `.json` | Possible at any time without warning. Mitigation: design `core/client.py` so transport is swappable; if Reddit kills unauthenticated `.json`, OAuth (when granted) is the slot-in replacement. |
| Cross-context data leakage (personal vs Motroco) | Hard isolation by deployment: separate service user, separate cache.db per use context. See historian's `personal/CLAUDE.md` for the rule. |

## Configuration

Optional `.env` file at `/home/elib/code/reddit_api/.env` (dev) or `/home/redditmcp/reddit-cache/.env` (prod). All keys optional with sensible defaults; the only one worth setting in MVP is `REDDIT_USER_AGENT`.

| Key | Required? | Default | Purpose |
|---|---|---|---|
| `REDDIT_USER_AGENT` | recommended | `reddit-research:0.1 (by /u/joeblowfromidaho)` | Reddit blocks generic UAs at the edge. Use a real, identifying string. |
| `REDDIT_CACHE_DIR` | no | `~/.cache/reddit-research/` | Where `cache.db` lives. |

Future OAuth-related keys (added if/when access is granted): `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_PASSWORD`, `REDDIT_WRITE_ENABLED`. Not used in MVP.

## Cache design

Why cache at all when most Reddit clients don't? Because LLM research re-reads the same threads within a session and revisits similar topics across sessions. Cached reads are tens of ms vs. hundreds of ms for live API + don't burn rate budget + don't burn tokens on re-fetched bloated JSON.

**Cache by default** (opt-out, not opt-in). Every read operation takes optional `fresh=True` to bypass. The LLM shouldn't have to think about caching ergonomics; fast-by-default is the point.

**`fresh=True` propagates to nested fetches** triggered by the same call.

**Storage:** SQLite single file at `<REDDIT_CACHE_DIR>/cache.db`. WAL mode, `busy_timeout=5000`, `PRAGMA synchronous=NORMAL`.

**Cached objects:** parsed JSON dicts as Reddit returned them. No PRAW translation layer means no shape ambiguity — what's cached is what's served.

**Error caching split:**

- `403`, `404` → cached for 24h (permanent — banned/private/deleted, won't change soon). The structured `reason` field on 403 (`gold_only`, `private`, `banned`, `quarantined`) is preserved so adaptive callers can distinguish.
- `5xx` → never cached (transient — caching would poison after one Reddit blip).
- Network errors / timeouts → never cached.

**TTL — three flat values:**

| Kind | TTL |
|---|---|
| `search` | 15 min |
| `listing` | 1 hr |
| `thread` | 24 hr |
| `comment_subtree` | 24 hr |
| `error_permanent` (403/404) | 24 hr |

**Cache key — must include all retrieval params, not just object id.** Different views of the same object are different cache entries.

| Operation | Key shape |
|---|---|
| `search(...)` | `search:<canonical-json-of-{query,subreddit,sort,time,limit}>` |
| `get_subreddit_listing(name, sort, limit)` | `listing:<r/name>:<sort>:<limit>` |
| `get_thread(id, top_n_comments)` | `thread:t3_<id>:top<N>:v1` |
| `expand_comment(thread_id, comment_id, depth, limit)` | `comment_subtree:t3_<thread>:t1_<comment>:d<depth>:l<limit>:v1` |

The `:v1` suffix is a `normalization_version` — bump when the on-disk shape changes so old entries are invalidated cleanly. Canonical JSON for search keys = sorted keys, defaults stripped, subreddit lowercased.

**Schema:**

```sql
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT INTO meta (key, value) VALUES ('schema_version', '1');

CREATE TABLE cached_objects (
    kind TEXT NOT NULL,                 -- 'search' | 'listing' | 'thread' | 'comment_subtree' | 'error_permanent'
    cache_key TEXT NOT NULL,            -- includes all retrieval params + normalization_version
    fetched_at INTEGER NOT NULL,        -- unix seconds
    status_code INTEGER NOT NULL,       -- 200 / 403 / 404
    body_json TEXT NOT NULL,            -- raw parsed JSON dict, or {"reason": ..., "message": ...} for non-200
    PRIMARY KEY (kind, cache_key)
);
CREATE INDEX idx_cached_fetched_at ON cached_objects(fetched_at);
```

**Purge:** `reddit-cli purge --older-than=30d`. Run via cron daily on warehouse-vm.

## Rate-limit discipline (hard requirement)

Failure mode to prevent: **getting blocked by Reddit's edge rate limiter** (the `.json` endpoints aren't tied to an account, so this isn't "losing API access" so much as "getting our IP/UA blacklisted for a window," but the discipline is the same).

- Read `X-Ratelimit-Used` / `X-Ratelimit-Remaining` / `X-Ratelimit-Reset` on every response.
- Back off proactively when `Remaining` drops below 10 — sleep until `Reset` elapses.
- Honor 429 with `Retry-After` header value + safety margin. Never retry faster than the server says.
- Set a correct, identifying `User-Agent`: `reddit-research:0.1 (by /u/joeblowfromidaho)`. Reddit blocks generic UAs (e.g. WebFetch's default) at the edge — confirmed empirically.
- Conservative pacing by default. No "burst" mode.
- **Per-call caps on `expand_comment`:** `depth ≤ 5`, `limit ≤ 50`. Refuse outside that.
- **Per-workflow aggregate budget** for the adaptive path: a single research session caps total HTTP calls (default 50) and total comments fetched (default 500). Returns a `BudgetExceeded` error rather than silently fetching more. Per-process for the CLI; per-session for the MCP server.

## Reddit API surface

- **Canonical OAuth API spec:** <https://www.reddit.com/dev/api/>. Not fetchable by `WebFetch` (Reddit blocks the WebFetch UA at the edge). Fetchable by `curl` with a proper UA.
- **Cached structured inventory:** [`docs/reddit-api-inventory.json`](docs/reddit-api-inventory.json) — all 202 OAuth endpoints across 19 sections, each with its OAuth scope. Useful as a reference even though MVP doesn't use OAuth.
- **`.json` endpoint findings:** [`docs/json_endpoint_findings.md`](docs/json_endpoint_findings.md) — what works, what's distinguishable, rate-limit behavior.
- **Endpoints used in MVP** (all unauthenticated `.json`):
  - `GET /r/<sub>/<sort>.json` — listings (hot/new/top/rising/controversial)
  - `GET /r/<sub>/search.json?q=…&restrict_sr=1` — subreddit-scoped search
  - `GET /search.json?q=…` — search-all
  - `GET /comments/<thread_id>.json` — thread + initial comment forest
  - `GET /r/<sub>/comments/<thread_id>/_/<comment_id>.json` — single comment with full reply subtree
  - `GET /api/morechildren.json?api_type=json&link_id=t3_…&children=…` — expand `more` markers

## Deployment (warehouse-vm)

Service user: **`redditmcp`** (personal install). Same pattern as existing `collector` and `warehouse` services on this VM. Create with `sudo useradd -m -s /bin/bash redditmcp` if it doesn't exist yet.

| Slot | Path | Owner | Mode |
|---|---|---|---|
| Source | `/home/elib/code/reddit_api/` | elib | 0755 |
| Venv | `/home/redditmcp/reddit-research/venv/` | redditmcp | 0755 |
| Data dir | `/home/redditmcp/reddit-cache/` | redditmcp | 0700 |
| `.env` (optional) | `/home/redditmcp/reddit-cache/.env` | redditmcp | 0600 |
| `cache.db` | `/home/redditmcp/reddit-cache/cache.db` | redditmcp | 0600 |

**Cross-context isolation rule:** if a Motroco project ever needs Reddit access, it deploys a *separate* install under a different service user (e.g. `motroco-redditmcp`) with its own `cache.db`. The personal install never sees Motroco data and vice versa. Same code, two installs, zero data crossover. Enforced by Unix file permissions.

Update flow: `git pull` in source, `pip install -e .` in venv. Linux-portable (Unix modes, SSH, cron); same code runs on any Linux host by changing the deploy paths.

## Phase 0 (one-shot spike, ~30 min, do once before writing the library)

Now runnable immediately — no Reddit account setup needed for the `.json` path.

1. **Connectivity:** confirm `/r/python/hot.json` returns 200 with the expected User-Agent and that rate-limit headers come back populated.
2. **Map error behavior:** call nonexistent subreddit, bogus thread id, premium-only sub. Confirm 404 / 403-with-reason / etc are distinguishable.
3. **Verify `MoreComments` cap:** call `/api/morechildren.json` with a 10-child cap on a real thread; observe response size and headroom delta.
4. **Benchmark queries** — pick 3–5 realistic research queries. For each, measure three paths separately:
   - `list_only` — single `.json` listing/search call.
   - `list_plus_threads_top20` — listing + `/comments/<id>.json` for first 3 threads.
   - `list_plus_threads_plus_expand_one` — above plus `/r/<sub>/comments/<thread>/_/<comment>.json` for the highest-scored top-level comment of each thread.
   - **Real tokenizer counts** via `tiktoken` cl100k_base (5–15% off Anthropic's tokenizer; ratios stable).
   - **Signal-to-noise ratio** — size of `{title, body, author, score, created_utc}` vs full response payload bytes. The delta is the *ceiling* for v0.2 markdown-preprocessor token savings.
   - **Headroom delta** before/after each path.
5. Save outputs as `phase0_notes.md`, `phase0_benchmarks.md`, `phase0_benchmarks.json`.

If anything unexpected, capture in `phase0_notes.md` and revisit decisions before writing code.

## Non-goals

- Multi-user / multi-account.
- Web UI.
- Real-time stream consumption (live threads, websocket).
- HTML scraping (always use the `.json` API path, never the rendered HTML).
- Reddit moderation tooling.
- Public exposure of the MCP server.
- Long-term archival storage / delta-over-time research.
- Sharing data between personal and Motroco contexts (see `## Deployment` cross-context isolation).
- Write capability in MVP (structurally unavailable through `.json` transport).

## Portfolio context

Personal/utility project, **not** part of Motroco business systems. The historian at `/home/elib/code/motroco-document/` covers it under `personal/reddit_research/` (separate from the `projects/` tree which is Motroco-only). See `motroco-document/personal/CLAUDE.md` for the data-isolation rule.
