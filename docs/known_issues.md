# reddit_research — Known Issues, Findings & Open Items

**Compiled:** 2026-05-08 (after round-10 panel review)
**State:** v0.2 chunk 1 (MCP stdio adapter) merged on `main` (commit 3153244). v0.1.0 deployed to warehouse-vm. 71/71 unit tests passing.

This is a single-file roll-up of everything currently open or worth remembering. Source-of-truth for design decisions remains `docs/manifest.json`; this file is the operations-and-bugs view.

---

## 1. Open / not-yet-applied items

### 1.1 `core/config.py:load_config()` — `cwd_env.is_file()` raises `PermissionError` in restricted cwds

- **Severity:** Latent (low).
- **Where:** `src/reddit_research/core/config.py:69-71` — `cwd_env = Path.cwd() / ".env"; if cwd_env.is_file(): ...`
- **Failure mode:** If `load_config()` runs from a directory the executing user can't traverse (unusual sudo-from-other-dir setups), `Path.cwd()` works but `.is_file()` may raise `PermissionError` instead of returning `False`.
- **Fix:** wrap the `is_file()` check in `try / except (OSError, PermissionError)` and treat it as "no .env in cwd". One-line.
- **Impact:** Production runs from `redditmcp`'s home directory, which the user owns; this never trips in production. Documented in the prior-session handoff as "captured but not blocking anything".

### 1.2 Reddit API access form (submitted 2026-04-27) still pending

- **Severity:** Not blocking.
- **Status:** No response from Reddit's support form. The `.json` transport works without it.
- **If approved:** OAuth becomes a slot-in upgrade path (decision #3 in manifest) for higher rate limits + write capability. Adds new `.env` keys: `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_PASSWORD`, `REDDIT_WRITE_ENABLED`. Not implemented.

### 1.3 v0.2 chunk 1 not yet pulled to production

- **Severity:** Coordination.
- **Where:** warehouse-vm `redditmcp` user. Production is at v0.1.0; `main` has v0.2 chunk 1.
- **Action:** standard update flow (HANDOFF.md), but **must use the `[mcp]` extra**: `pip install -e '.[mcp]'`. Without it, `reddit-mcp` exits 1 with a friendly install hint (verified 2026-05-08 in clean venv).
- **Verify after:** live JSON-RPC handshake against `reddit-mcp` — should return `serverInfo: {"name":"reddit-research","version":"0.1.0"}` plus the 7-tool list.

---

## 2. Round-10 panel findings (2026-05-08, on v0.2 chunk 1)

Verdicts: Codex BLOCK → APPROVE after fixes / Gemini APPROVE-WITH-FIXES → APPROVE / Opus APPROVE.

Blockers fixed in commit `2cef169`. P2s applied in the same commit unless noted.

### 2.1 P1s — fixed

#### Codex P1: optional-dep gating mismatch (FIXED `2cef169`)

- **Where:** `pyproject.toml`, `src/reddit_research/mcp/__init__.py`, `src/reddit_research/mcp/__main__.py`.
- **Failure mode:** `mcp` SDK was declared in `[project.optional-dependencies] mcp` extra, but `[project.scripts]` always installs `reddit-mcp` AND `mcp/__init__.py` did `from .server import build_server` eagerly. Plain installs (without the extra) crashed on `import reddit_research.mcp` and the `reddit-mcp` script printed a stack trace instead of a useful message.
- **Fix:** dropped the eager re-export from `__init__.py`; `__main__.py` wraps the server import in `try/except ImportError` and prints `pip install 'reddit-research[mcp]'` with exit 1 when SDK is missing.
- **Verified:** clean venv install (no extras) → `reddit-mcp` exits 1 with the install hint. With `[mcp]` extra → handshake succeeds.

#### Gemini P1.1: `Cache.purge(older_than_seconds=-1)` silently wiped the entire cache (FIXED `2cef169`)

- **Where:** `src/reddit_research/core/cache.py:175` (was `cutoff = now_ts - int(older_than_seconds)`).
- **Failure mode:** A negative `older_than_seconds` made `cutoff = now + |seconds|`, so `WHERE fetched_at < cutoff` matched every row. CLI argparse already guarded this (`type=_positive_int`), but library callers and the MCP layer were exposed.
- **Fix:** `Cache.purge` now raises `ValueError` on negative input. Universal — protects CLI, MCP, and direct Python callers. MCP `purge` tool also encodes `Field(ge=0)` in its pydantic schema for fail-fast behavior.
- **Test:** `test_mcp_purge_negative_via_core_validation` (run_qa.py).

### 2.2 P1 — declined

#### Gemini P1.2: `WorkflowBudget` not thread-safe (DECLINED)

- **Why declined:** verified at `mcp/server/fastmcp/utilities/func_metadata.py:95` — FastMCP runs sync `@tool` functions inline in the async event loop (`fn(**arguments_parsed_dict)`), no `to_thread.run_sync`. Stdio is a single-stream serial dispatch model; no two tool calls execute simultaneously within one stdio process.
- **Re-examine if:** we add async tools, switch to `streamable-http` transport, or FastMCP changes its dispatch model in a future version.
- **Memory:** `~/.claude/projects/-home-elib-code-reddit-api/memory/project_fastmcp_sync_dispatch.md`.

### 2.3 P2s — applied

All applied in commit `2cef169`.

| # | Source | Issue | Fix |
|---|---|---|---|
| 2.3.1 | Opus P2-2 | Generic `HTTPError` (status codes outside the 3xx/403/404/429/5xx mapping — e.g. 400, 401, 418) escaped `_wrap_reddit_call` as MCP `isError: true`, losing status/path/reason fields | Added `except HTTPError as e:` catch-all at the end of the chain returning `{"ok": false, "error": {"type": "http_error", "status": ..., "path": ..., ...}}` |
| 2.3.2 | Codex + Gemini P2 | Tool schemas didn't encode the documented numeric bounds (depth 0-5, limit 1-50, etc.); hosts only learned them from prose | Encoded bounds via `Annotated[int, Field(ge=..., le=...)]` on tool param annotations. FastMCP/pydantic now reject out-of-range args before the wrapper sees them |
| 2.3.3 | Gemini P2-1 | `search` / `get_subreddit_listing` / `get_thread` advertised 1-100 limits but core only enforced the lower bound | Added upper bounds in core/operations.py: `LISTING_LIMIT_MAX = 100`, `THREAD_TOP_N_MAX = 100`. Reddit silently truncates above 100 anyway, so this closes the doc-vs-code gap rather than changing wire behavior |
| 2.3.4 | Gemini P2-2 | `_wrap_reddit_call(label, fn, ...)` — `label` parameter was never used | Removed the parameter; updated all 6 call sites |
| 2.3.5 | Gemini P2-4 | `_to_jsonable` was duplicated between `cli/commands.py` and `mcp/server.py` — drift would silently change one consumer's output shape | Extracted to `src/reddit_research/_serialize.py` as `to_jsonable()`. Both adapters now import from the shared util |
| 2.3.6 | Opus P2-1 + Codex P2 | Four `_wrap_reddit_call` error branches had no MCP-layer test coverage: `RateLimitError` (with the `retry_after_seconds` rename), `RedirectError`, `TransportError`, `UpstreamError`, parser-strict `ValueError` | Added 5 new tests in `tests/run_qa.py` for each branch + 1 new test for the `http_error` catch-all (2.3.1). Test count 64 → 71 |

### 2.4 P2s — declined

#### Codex P2: `reset_budget` is an LLM-bypassable safety guardrail (DECLINED)

- **The concern:** the LLM can erase its own consumption counters mid-session; theoretically a looping host could repeatedly reset and burn unlimited budget.
- **Why declined:** the budget is documented as a per-research-run guardrail (CLAUDE.md "per-session for the MCP server"), not a hard per-process cap. Restarting the server for legitimate multi-run sessions is operationally costly. Single-user pragma — not a multi-tenant service.
- **Mitigations in place:** the user monitors via `status()`; the tool description tells the LLM to use it only for new logical research runs.
- **Re-examine if:** we ever expose this server beyond Eli's own use.

#### Gemini P2-3: Split parser-strict `ValueError` vs cap-violation `ValueError` into distinct envelope types (DECLINED)

- **The concern:** `_wrap_reddit_call` maps both Reddit-schema-drift `ValueError` (from strict parsers) and cap-violation `ValueError` (from `expand_comment` depth/limit checks) to `{"error": {"type": "invalid_input"}}`. The LLM can't tell which one happened from the type alone.
- **Why declined:** the message disambiguates (schema drift mentions `data.children`, cap violations mention `depth must be in [0, 5]`). Adding a `SchemaError` class is more churn than payoff.
- **Note:** with bounds-encoded schemas (2.3.2) now in place, cap violations rarely reach `_wrap_reddit_call` anyway — pydantic rejects them upstream and FastMCP raises `ToolError`. So this concern is mostly moot in practice.

---

## 3. Earlier panel findings (rounds 1-9) — all fixed pre-v0.1.0

Listed for completeness. Full detail in `docs/manifest.json` → `panel_reviews`. All applied in their respective commits before v0.1.0 was tagged.

| Round | Date | Highlights |
|---|---|---|
| 1 | 2026-04-27 | 3 P1: Reddit script-app refresh-token impossibility / PRAW hides X-Ratelimit-* / byte-for-byte raw cache incompatible with PRAW. Resolved by simplification in plan v2. |
| 2 | 2026-04-27 | 1 P1: cache key must include retrieval params (silent partial-view cache poisoning). 10 should-fixes (per-call caps on expand_comment, error caching split, fresh propagation, etc.). All applied in plan v3. |
| 3 + 4 | 2026-04-27 | Spike script reviews. Round 4 found Path 3 was measuring pre-loaded data. Made moot by the `.json` transport pivot. |
| 5 | 2026-05-02 | core skeleton. 4 P1 + 4 P2: stale `reset_in_seconds` in proactive backoff, case-sensitive header lookup, `expect_kind` ergonomic regression, etc. |
| 6 | 2026-05-02 | core skeleton round 2. Same shape as round 5; all applied. |
| 7 | 2026-05-02 | cache + operations. 3 P1 + 3 P2: misleading "top by score" framing, lost path on cached error replay, listing time pollution, parser strictness asymmetry. All applied. |
| 8 | 2026-05-04 | CLI + tests. 1 P1: get_thread comment-budget undercount on nested replies. 4 P2: CLI `_run` not catching `ValueError` / `RuntimeError`, --depth help mismatch, missing positive-int validation, missing happy-path expand_comment test. All applied. |
| 9 | 2026-05-04 | Final pre-tag. 2 P1 (Codex): bad 200s cached before parsing (could poison cache for full TTL on Reddit schema drift), and budget charged before `_proactive_backoff` (could charge with zero upstream call). Both applied. Opus + Gemini approved. |

---

## 4. Environmental issues encountered this session (not code bugs)

### 4.1 Reddit varnish edge 403 HTML soft-block (2026-05-08)

- **What happened:** during round-10 testing, the live smoke test failed with `ForbiddenError: HTTP 403 on /r/python/hot.json (message=None)`. Same UA had worked earlier the same day.
- **Diagnosis:** direct `httpx.get` confirmed the response was a ~180KB HTML page from Reddit's varnish-fronted edge (headers included `via: 1.1 varnish`, `set-cookie: csv=...`, `set-cookie: edgebucket=...`), NOT a JSON `{"reason": ...}` body. Likely an IP/UA throttle from cumulative volume during the panel review CLIs + repeated smoke tests.
- **Not a regression:** the diff under review didn't touch the transport layer.
- **Mitigation:** wait an hour; or test from a different IP. Don't try to "fix" the transport mid-session.
- **Memory:** `~/.claude/projects/-home-elib-code-reddit-api/memory/project_reddit_edge_soft_block.md`.

---

## 5. Documented transport / operational risks

From `CLAUDE.md` ## Security and ## Rate-limit-discipline; included here for one-stop awareness:

| Risk | Mitigation |
|---|---|
| Reddit kills unauthenticated `.json` access | Transport is swappable (`core/client.py`); OAuth (when granted) is the slot-in replacement |
| Reddit edge soft-block (cumulative volume) | Conservative pacing; proactive backoff at <10% headroom; honor 429 Retry-After; UA discipline |
| Cache leaks research interests | Cache dir mode 0700, owned by deploy user only |
| TLS bypass | No knob to disable certificate verification |
| MCP network exposure | Stdio transport only; remote access via SSH |
| Cross-context leakage (personal vs Motroco) | Hard isolation by deployment: separate service users, separate cache.db per use context |
| Accidental writes | Structurally impossible through `.json` transport |

---

## 6. What's deferred (not bugs, just future work)

From `docs/manifest.json` → `deferred_to_v0_2_or_later`:

- **Markdown preprocessor (v0.2 chunk 2)** — strips Reddit's ~90% operational metadata, formats comment trees as depth-indented markdown. Phase 0 measured ~10× token leverage on the most expensive paths.
- OAuth + PRAW transport (only if Reddit grants access)
- Write capability (depends on OAuth)
- Audit log for writes
- Async (httpx async client)
- Snapshots / delta-over-time research
- Multi-account
- Age-aware TTL

---

## 7. Where to go from here

Recommended next chunk: **markdown preprocessor (v0.2 chunk 2)** — see HANDOFF.md ## What's next. After that lands and is panel-cleared, pull v0.2 to production on warehouse-vm with the `[mcp]` extra and verify with the live JSON-RPC handshake recipe in HANDOFF.md ## Operating notes for production.
