# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A personal Reddit research tool. Single user (Eli's developer account). The use case is: **search subreddits for important / useful information for an LLM to summarize.** Read-first; write capability exists but is disabled by default and gated behind an explicit flag.

Source on warehouse-vm at `/home/elib/code/reddit_api/` (this directory). Runtime under a dedicated service user on the same VM.

**For new sessions:** read [`HANDOFF.md`](HANDOFF.md) first; it points at [`docs/manifest.json`](docs/manifest.json) which has the full machine-readable project state (operations, schema, caps, decisions, session log).

## Decisions (already made — don't relitigate)

1. **Language:** Python 3.11+.
2. **Reddit client:** `praw`. Handles auth, rate-limit backoff, listing pagination, `MoreComments` expansion. Drop to raw `requests` against `oauth.reddit.com` only if PRAW lacks an endpoint we need.
3. **PRAW init flags:** `check_for_updates=False` (no per-invocation PyPI call), `read_only=True` whenever the write flag is off (defense in depth — PRAW will refuse write attempts at the client level).
4. **Cache backend:** SQLite, single file, WAL mode, `busy_timeout=5000`, `PRAGMA synchronous=NORMAL`. See `## Cache design`.
5. **Auth:** script app + password grant for MVP. Reddit username for the User-Agent: `joeblowfromidaho`. Credentials live in `.env` files (never committed) — see `## Credentials` for paths and permissions. The existing dev app already works this way; no value in moving to refresh-token flow until the threat model changes. Reddit changing its script-app/password-grant policy is a known hardening trigger — if that happens, re-register the app as `web`/`installed` and switch to refresh-token flow.
6. **Packaging:** PEP 621 `pyproject.toml`, `src/` layout, `pip install -e .` for dev.
7. **MCP transport:** stdio only. Run on warehouse-vm; access from a laptop via SSH (`ssh warehouse reddit-mcp`). No network listener.
8. **Roadmap:** core library → CLI (debug + Eli's personal use) → MCP server. Preprocessing optimizations and write capability ship after the MVP.

## MVP scope (v0.1)

Smallest thing that works:

**Operations exposed by `reddit_research.core`:**
- `search(query, subreddit=None, sort='relevance', time='all', limit=25, fresh=False)` → list of thread summaries (title, score, author, sub, num_comments, permalink, fullname, created_utc).
- `get_subreddit_listing(name, sort='hot', limit=25, fresh=False)` → same shape.
- `get_thread(fullname, top_n_comments=20, fresh=False)` → post + top N top-level comments by score, no sub-tree expansion. Cheap.
- `expand_comment(comment_fullname, depth=2, limit=20, fresh=False)` → one comment + N levels of replies. Per-call hard caps: `depth ≤ 5`, `limit ≤ 50`. The "follow promising sub-trees" operation.
- `purge(older_than_days=30)` → delete cache rows older than N days.
- `status()` → cache size, cache hit rate, PRAW's `auth.limits` (rate-limit headroom), and **auth state** (one of: `never_exercised`, `last_call_ok`, `last_call_failed:<reason>` — distinguishes bad credentials from "we just haven't called yet").

**Input validation lives in core, not the MCP boundary.** All `fullname` parameters are validated against `^t[1-6]_[a-z0-9]+$` before being passed to PRAW. CLI gets the same protection as MCP.

**Adapter (CLI):** `reddit-cli` with subcommands matching the operations. JSON or text output.

**Other:**
- `.env` with credentials, mode 0600.
- `smoke_test.py` — verifies auth + one read call + one cache round-trip.
- `run_qa.py` — unit tests with mocked PRAW + in-memory SQLite. Matches portfolio testing pattern.

**Deferred to v0.2+:** MCP server adapter, preprocessed views (token-saving markdown transforms), write capability behind flag, async, snapshots, multi-account.

## Architecture

```
src/reddit_research/
  core/      # PRAW wrapper, cache, write-gate, fullname validation. No UI/transport deps.
  cli/       # argparse adapter. Imports core.
  mcp/       # MCP server adapter (later). Imports core.
```

If `core/` ever imports from `cli/` or `mcp/`, the layering broke. Adapters know about core; core never knows about adapters.

## Security

| Risk | Mitigation |
|---|---|
| Secrets at rest | `.env` mode 0600, cache dir mode 0700, owned by deploy user only. |
| Cache leaks research interests | Same dir/file permissions as above. |
| TLS bypass footgun | No knob to disable certificate verification. |
| MCP network exposure | Stdio transport only. Remote access via SSH. |
| LLM-supplied input | `fullname` validated in core (`^t[1-6]_[a-z0-9]+$`). All adapters get the protection. |
| Accidental writes | Write flag default off, checked at *call time*. PRAW also gets `read_only=True` (defense in depth). Audit log records every write attempt + response (post-MVP). |
| Rate-limit failure | Trust PRAW's internal backoff. Surface PRAW's `auth.limits` via `status` command. Per-call + per-workflow caps on `expand_comment` (see `## Rate-limit discipline`). |
| Reddit ToS | Personal/non-commercial research only. If that changes, re-read API terms. |
| Cross-context data leakage (personal vs Motroco) | Hard isolation by deployment: separate service user, separate cache.db, separate credentials per use context. See historian's `personal/CLAUDE.md` for the rule. |

## Credentials

OAuth script-app credentials + Reddit account password live in two `.env` files (one per environment), never in committed files, never in shell history (no `export REDDIT_PASSWORD=...`).

| Use | Path | Owner | Mode |
|---|---|---|---|
| Production runtime | `/home/redditmcp/reddit-cache/.env` | `redditmcp:redditmcp` | 0600 |
| Dev / Phase 0 spike | `/home/elib/code/reddit_api/.env` | `elib:elib` | 0600 (gitignored) |
| Schema template | `/home/elib/code/reddit_api/.env.example` | `elib:elib` | 0644 (committed, no values) |

Required keys (see `.env.example`):
- `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET` — from <https://www.reddit.com/prefs/apps>
- `REDDIT_USERNAME` = `joeblowfromidaho`
- `REDDIT_PASSWORD` — Reddit account password
- `REDDIT_USER_AGENT` = `reddit-research:0.1 (by /u/joeblowfromidaho)`
- `REDDIT_WRITE_ENABLED` — defaults to `false`; only set `true` when intentionally enabling write operations
- `REDDIT_CACHE_DIR` — production sets to `/home/redditmcp/reddit-cache/`; dev defaults to `~/.cache/reddit-research/`

**Loading:** `python-dotenv` auto-loads at process start. PRAW could be configured via `praw.ini` instead but `.env` is more standard and shared by both CLI and (future) MCP adapter.

**Deploy from dev to prod** (after Phase 0 + MVP build): `sudo install -o redditmcp -g redditmcp -m 0600 /home/elib/code/reddit_api/.env /home/redditmcp/reddit-cache/.env`. Two files, deliberately duplicated; if creds rotate, update both.

**No vault / 1Password / etc.** Single-user single-machine personal tool — file-system permissions on a non-shared host are sufficient. Reddit creds are also low-stakes (single account, revocable + reissuable from the dev portal at any time).

## Cache design

Why cache at all when most Reddit clients don't? Because LLM research re-reads the same threads within a session, and revisits similar topics across sessions. Cached reads are tens of ms vs. hundreds of ms for live API + don't burn rate budget + don't burn tokens on re-fetched bloated JSON.

**Cache by default** (opt-out, not opt-in). Every read operation takes optional `fresh=True` to bypass the cache. The LLM shouldn't have to think about caching ergonomics; fast-by-default is the point.

**`fresh=True` propagates to nested fetches** triggered by the same call (e.g., `get_thread(fresh=True)` also forces fresh on any internal listing/comment-expansion sub-fetches it makes).

**Storage:** SQLite single file at `/home/<deploy_user>/reddit-cache/cache.db`. WAL mode, `busy_timeout=5000`, `PRAGMA synchronous=NORMAL`.

**Cached objects:** normalized PRAW objects serialized to JSON dict (NOT byte-for-byte raw API responses — PRAW returns hydrated objects, not raw payloads).

**Error caching split:**
- `403`, `404` → cached for 24h (permanent — banned/private/deleted, won't change soon).
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

Age-aware TTL was considered and cut as overengineered.

**Cache key — must include all retrieval params, not just object id.** Different views of the same object are different cache entries.

| Operation | Key shape |
|---|---|
| `search(...)` | `search:<canonical-json-of-{query,subreddit,sort,time,limit}>` |
| `get_subreddit_listing(name, sort, limit)` | `listing:<r/name>:<sort>:<limit>` |
| `get_thread(fullname, top_n_comments)` | `thread:<fullname>:top<N>:v1` |
| `expand_comment(fullname, depth, limit)` | `comment_subtree:<fullname>:d<depth>:l<limit>:v1` |

The `:v1` suffix is a `normalization_version` — bump when the on-disk shape changes so old entries are invalidated cleanly. Canonical JSON for search keys = sorted keys, defaults stripped, subreddit lowercased.

**Without param-in-key, the cache silently serves wrong-shape data:** `get_thread('t3_abc', top_n_comments=20)` would cache 20 comments at key `t3_abc`, then `get_thread('t3_abc', top_n_comments=100)` would cache-hit and return 20 instead of 100.

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
    body_json TEXT NOT NULL,            -- normalized PRAW object dict, or error metadata for non-200
    PRIMARY KEY (kind, cache_key)
);
CREATE INDEX idx_cached_fetched_at ON cached_objects(fetched_at);
```

`schema_version` exists from day one so future migrations have a hook. No special migration framework — `ALTER TABLE` + bump version when needed.

**Purge:** `reddit-cli purge --older-than=30d`. Run via cron daily on warehouse-vm.

## Rate-limit discipline (hard requirement)

Failure mode to prevent: **losing API access**.

- Trust PRAW's internal leaky-bucket backoff. It honors `X-Ratelimit-*` and `429` correctly.
- Set a correct, identifying `User-Agent`: `reddit-research:0.1 (by /u/joeblowfromidaho)` (loaded from `REDDIT_USER_AGENT`).
- `check_for_updates=False` on PRAW init.
- **Mandatory cap on `MoreComments` expansion** — never call `replace_more(limit=None)`. Default `limit=10`, configurable up to a sane max.
- **Per-call caps on `expand_comment`:** `depth ≤ 5`, `limit ≤ 50`.
- **Per-workflow aggregate budget** for the adaptive path: a single research session (search → get_thread → expand_comment ×N) caps total API calls (default 50) and total comments expanded (default 500). Returns a `BudgetExceeded` error rather than silently fetching more. The cap is per-process for the CLI; the MCP server enforces per-session.
- `status` command surfaces PRAW's `reddit.auth.limits` (`{remaining, used, reset_timestamp}`) for operator visibility.

## Reddit API surface

- **Canonical spec:** <https://www.reddit.com/dev/api/>. Not fetchable by `WebFetch` (Reddit blocks the WebFetch UA at the edge). Fetchable by `curl` with a proper UA.
- **Cached structured inventory:** [`docs/reddit-api-inventory.json`](docs/reddit-api-inventory.json) — all 202 endpoints across 19 sections, each with its OAuth scope. Refresh by re-running the curl + parser.
- **Scope minimum for read-only research:** `read identity`. Of 28 OAuth scopes, `read` alone covers 44 endpoints — including everything in the research path.
- OAuth2 token endpoint: `https://www.reddit.com/api/v1/access_token`. Authenticated API base: `https://oauth.reddit.com`. Listing pagination: `after`/`before`. Comment-tree expansion: `/api/morechildren`.

## Deployment (warehouse-vm)

Service user: **`redditmcp`** (personal install). Same pattern as existing `collector` and `warehouse` services on this VM. Create with `sudo useradd -m -s /bin/bash redditmcp` if it doesn't exist yet.

| Slot | Path | Owner | Mode |
|---|---|---|---|
| Source | `/home/elib/code/reddit_api/` | elib | 0755 |
| Venv | `/home/redditmcp/reddit-research/venv/` | redditmcp | 0755 |
| Data dir | `/home/redditmcp/reddit-cache/` | redditmcp | 0700 |
| `.env` (prod) | `/home/redditmcp/reddit-cache/.env` | redditmcp | 0600 |
| `cache.db` | `/home/redditmcp/reddit-cache/cache.db` | redditmcp | 0600 |
| Audit log (post-MVP) | `/home/redditmcp/reddit-cache/audit.log` | redditmcp | 0600 |

**Cross-context isolation rule:** if a Motroco project ever needs Reddit access, it deploys a *separate* install under a different service user (e.g. `motroco-redditmcp`) with its own credentials and its own `cache.db`. The personal install never sees Motroco data and vice versa. Same code, two installs, zero data crossover. Enforced by Unix file permissions.

Update flow: `git pull` in source, `pip install -e .` in venv. Linux-portable (Unix modes, SSH, cron); same code runs on any Linux host by changing the deploy paths.

## Phase 0 (one-shot spike, ~60–90 min, do once before writing the library)

1. **Auth check:** confirm the existing dev app authenticates and pulls one thread successfully via PRAW.
2. **Map error behavior:** deliberately request a banned/private/deleted thread; observe how PRAW raises (`prawcore.Forbidden`, `prawcore.NotFound`, etc.). Pick stable exception types to map to in core. Confirm 5xx is distinguishable from permanent errors.
3. **Verify `MoreComments` cap:** call `replace_more(limit=10)` on a real big thread; confirm behavior matches expectations.
4. **Benchmark queries** — pick 3–5 realistic research queries (e.g. "search r/MachineLearning for 'transformer optimization', pull top 5 threads with top-20 comments each"). For each:
   - **Per-path measurement:** measure `search` alone, `search + get_thread`, and `search + get_thread + expand_comment` separately so we can see the marginal cost of each adaptive step.
   - **Real tokenizer counts** — use the Anthropic tokenizer (or whatever model will summarize) for token counts. Don't use chars/4; it's a poor predictor of v0.2 preprocessing gains.
   - **Signal-to-noise ratio** — for each thread, compute size of `{post.body, post.title, comments[].{body,author,score,created_utc}}` vs full PRAW JSON payload size. The delta is the *ceiling* for v0.2 markdown-preprocessor token savings.
   - **API call count and wall time** per query, plus PRAW's `auth.limits` headroom before/after.
   - Save as `phase0_benchmarks.md` + `phase0_benchmarks.json` (machine-readable for v0.2 comparison).
5. **Cache-hit/miss instrumentation in the smoke test** so v0.2 work has a baseline.

If anything unexpected, capture in `phase0_notes.md` and revisit decisions before writing code.

## Non-goals

- Multi-user / multi-account.
- Web UI.
- Real-time stream consumption (live threads, websocket).
- HTML scraping (always use the API).
- Reddit moderation tooling.
- Public exposure of the MCP server.
- Long-term archival storage / delta-over-time research.
- Sharing data between personal and Motroco contexts (see `## Deployment` cross-context isolation).

## Portfolio context

Personal/utility project, **not** part of Motroco business systems. The historian at `/home/elib/code/motroco-document/` covers it under `personal/reddit_research/` (separate from the `projects/` tree which is Motroco-only). See `motroco-document/personal/CLAUDE.md` for the data-isolation rule between personal tools and Motroco projects.
