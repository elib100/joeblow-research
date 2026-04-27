# Handoff — reddit_research

**Updated:** 2026-04-27
**Status:** design v3 (panel-reviewed twice). No code yet. **Phase 0 unblocked** — only remaining setup is Eli filling credential values into `/home/elib/code/reddit_api/.env` (use `.env.example` as the schema).

## TL;DR

Personal Reddit research tool — a Python library + CLI + (later) MCP server that lets an LLM search subreddits and pull threads/comments efficiently for summarization. Single user (Eli's dev account), deployed on warehouse-vm. Hard isolation from Motroco data.

## Where to look

| What you want | Where it is |
|---|---|
| Full design spec, human-readable | [`CLAUDE.md`](CLAUDE.md) |
| Machine-readable project state (operations, schema, decisions, caps, session log, open questions) | [`docs/manifest.json`](docs/manifest.json) |
| Reddit API endpoint catalog (all 202 endpoints, scoped) | [`docs/reddit-api-inventory.json`](docs/reddit-api-inventory.json) |
| Historian entry (cross-portfolio audit) | `/home/elib/code/motroco-document/personal/reddit_research/AUDIT.md` |
| Data-isolation rule (personal vs Motroco) | `/home/elib/code/motroco-document/personal/CLAUDE.md` |

**`manifest.json` is the structured source of truth.** Read it first if you're picking up cold — it has every operation signature, the schema, every decision with rationale, every cap, and the full session decision log. CLAUDE.md is the same content rendered for human reading.

If `CLAUDE.md` and `manifest.json` ever disagree, fix one to match the other. Don't let drift accumulate.

## What's been done

1. Scoped the project (single-user research tool, full Reddit API surface eventually, read-first).
2. Scraped the Reddit API spec into `docs/reddit-api-inventory.json` — confirmed `read` scope alone covers 44 endpoints including everything in the research path.
3. Designed cache + rate-limit + deployment + security model.
4. Sent design to a 3-model panel (OpenAI Codex + Gemini + Claude Opus 4.6). Round 1 found 3 blockers — resolved in v2. Round 2 found 1 real bug (cache-key/adaptive-retrieval interaction) plus a should-fix list — all applied in v3.
5. Added project to historian under new `personal/` section (parallel to `projects/` Motroco tree, with hard data isolation).

## What's next (Phase 0 spike, ~60–90 min)

Run `phase0_spike.py` — the script implements all four probes:

1. Auth check (PRAW pulls one thread, headroom snapshot before/after).
2. Error mapping (deliberate 404 / 403 / bogus-id calls → captures `prawcore` exception types).
3. `MoreComments` cap (`replace_more(limit=10)` on a real big thread).
4. Benchmark queries — 5 realistic queries, two paths each (`list_only`, `list_plus_threads_top20`), with real tokenizer counts (tiktoken cl100k_base) and signal-to-noise ratio per path.

Outputs:
- `phase0_notes.md` — exception map + things to verify by hand
- `phase0_benchmarks.md` — human summary
- `phase0_benchmarks.json` — machine-readable, becomes v0.2 yardstick

Setup + run:

```bash
cd /home/elib/code/reddit_api
python3 -m venv venv && source venv/bin/activate
pip install -r requirements-phase0.txt
cp .env.example .env && chmod 600 .env
# Fill REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_PASSWORD into .env
python phase0_spike.py
```

Run individual probes with `--probe auth|errors|morechildren|benchmarks` if you want to iterate. After running, read `phase0_notes.md` first — it lists what to verify by hand.

## Setup before Phase 0 can run

Three pre-spike blockers were resolved 2026-04-27:

- **Username** = `joeblowfromidaho` (in `.env.example` already)
- **Deploy service user** = `redditmcp` (must be created with `sudo useradd -m -s /bin/bash redditmcp` before first deploy — not needed for Phase 0 dev work)
- **Credentials location** = two `.env` files; for Phase 0 (dev), populate `/home/elib/code/reddit_api/.env` from `.env.example` (mode 0600, gitignored)

Remaining one-time setup: Eli fills `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_PASSWORD` into the dev `.env` from his Reddit dev app at <https://www.reddit.com/prefs/apps>.

Full resolution log in `manifest.json` → `resolved_questions`. The `open_questions` array is now empty.

## What's deferred (don't build yet)

MCP server adapter, preprocessed views (token-saving markdown transforms), write capability, audit log, async, snapshots, multi-account, age-aware TTL. See `manifest.json` → `deferred_to_v0_2_or_later`.

## Operating constraints to respect

- **Don't lose API access.** This is the single hard requirement. Trust PRAW's backoff; respect every cap; never call `replace_more(limit=None)`.
- **Hard data isolation from Motroco.** If a Motroco project ever needs Reddit data, it deploys a separate install under a different service user with its own cache.db and credentials. Same code, different data. See historian.
- **No network listener.** MCP transport is stdio only; remote access via SSH.
- **Write flag default off**, checked at call time, with PRAW `read_only=True` as defense in depth.

## How to update this handoff

Anything that changes design (operations, schema, caps, decisions) → update `manifest.json` in the same commit. Then update CLAUDE.md (human-readable view) and this HANDOFF.md (status + what's next) to match. Three files, one source of truth.
