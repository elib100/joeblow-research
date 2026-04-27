# Handoff — reddit_research

**Updated:** 2026-04-27
**Status:** design v4. **Pivoted to Reddit's public `.json` endpoints as primary transport.** Spike is runnable now — no Reddit account, no OAuth, no API approval needed. Reddit API access request still pending (submitted earlier today) but no longer on the critical path.

## TL;DR

Personal Reddit research tool — a Python library + CLI + (later) MCP server that lets an LLM search Reddit and pull threads/comments efficiently for summarization. Single user, deployed on warehouse-vm. Hard isolation from Motroco data.

**Major architectural change today:** moved from OAuth + PRAW (which is gated behind Reddit's Responsible Builder Policy approval, with low approval rates for personal projects) to the unauthenticated `.json` endpoints (which work today, expose rate-limit headers cleanly, and are structurally read-only). OAuth becomes a future upgrade path for higher rate limits / write capability if Reddit ever grants access.

## Where to look

| What you want | Where it is |
|---|---|
| Full design spec, human-readable | [`CLAUDE.md`](CLAUDE.md) |
| Machine-readable project state | [`docs/manifest.json`](docs/manifest.json) |
| Why we picked the `.json` transport | [`docs/json_endpoint_findings.md`](docs/json_endpoint_findings.md) |
| Reddit OAuth API endpoint catalog (reference for the deferred upgrade path) | [`docs/reddit-api-inventory.json`](docs/reddit-api-inventory.json) |
| Historian entry | `/home/elib/code/motroco-document/personal/reddit_research/AUDIT.md` |
| Data-isolation rule (personal vs Motroco) | `/home/elib/code/motroco-document/personal/CLAUDE.md` |

`manifest.json` is the structured source of truth. CLAUDE.md is the human-readable view of the same content. If they disagree, fix one to match.

## What's been done

1. Scoped the project (single-user research tool, read-only).
2. Scraped the Reddit OAuth API spec into `docs/reddit-api-inventory.json` — kept as reference for the deferred OAuth upgrade.
3. Designed cache + rate-limit + deployment + security model.
4. Sent design through three rounds of multi-model panel review (Codex / Gemini / Opus).
5. Discovered Reddit's Responsible Builder Policy (Nov 2025) blocks self-service script-app creation. Submitted API access form anyway.
6. Published source repo at <https://github.com/elib100/joeblow-research>.
7. **Probed Reddit's `.json` public endpoints — verified working in 2026 across every shape we need.**
8. **Pivoted primary transport to `.json`** (httpx, no OAuth, no PRAW). Spike rewritten accordingly.

## What's next (Phase 0 spike, ~30 min, runnable now)

The spike script (`phase0_spike.py`) targets the `.json` transport and runs all four probes:

1. Connectivity — UA accepted, headroom headers populated.
2. Error mapping — 404 / 403 (with structured `reason`) distinguishable.
3. `MoreComments` cap — `/api/morechildren.json` with capped child list.
4. Benchmark queries — five realistic queries, three paths each (`list_only`, `list_plus_threads_top20`, `list_plus_threads_plus_expand_one`), with real tokenizer counts and signal-to-noise ratio.

Setup + run:

```bash
cd /home/elib/code/reddit_api
python3 -m venv venv && source venv/bin/activate
pip install -r requirements-phase0.txt
cp .env.example .env
python phase0_spike.py
```

The `.env` only has `REDDIT_USER_AGENT` worth setting (defaults to `reddit-research:0.1 (by /u/joeblowfromidaho)` if unset). No secrets needed.

Outputs: `phase0_notes.md`, `phase0_benchmarks.md`, `phase0_benchmarks.json`. All gitignored.

## What's deferred

MCP server adapter, preprocessed views, OAuth + PRAW transport (only relevant if Reddit grants the API access request), write capability, audit log, async, snapshots, multi-account, age-aware TTL.

## Operating constraints to respect

- **Don't trip Reddit's edge rate limiter.** Read every `X-Ratelimit-*` header; back off proactively when `Remaining < 10`. Honor 429 with `Retry-After`.
- **Hard data isolation from Motroco.** If a Motroco project ever needs Reddit data, it deploys a separate install under a different service user with its own cache.db. Same code, different data. See historian.
- **No network listener.** MCP transport is stdio only; remote access via SSH.
- **Transport is swappable.** If Reddit kills unauthenticated `.json`, swap the client out — OAuth (when granted) is the slot-in replacement.

## How to update this handoff

Anything that changes design (operations, schema, caps, decisions) → update `manifest.json` in the same commit. Then update CLAUDE.md (human-readable) and this HANDOFF.md (status + what's next) to match. Three files, one source of truth.
