# Handoff — reddit_research

**Updated:** 2026-05-08
**Status:** **v0.2 chunk 1 (MCP server adapter) merged on `main`, panel-cleared (round 10).** v0.1.0 still the current release tag. 71/71 unit tests passing. Production on warehouse-vm still on v0.1.0 — not yet pulled to v0.2. Markdown preprocessor (chunk 2) is next.

## TL;DR

Personal Reddit research tool — Python library + CLI (v0.1.0 released) + MCP stdio server (v0.2 chunk 1, on `main`). Single user, deployed on warehouse-vm. Hard isolation from Motroco data.

Transport is Reddit's public `.json` endpoints (no OAuth, no PRAW). Reddit's Responsible Builder Policy gates traditional API access; the `.json` path sidesteps that, exposes rate-limit headers cleanly, and is structurally read-only. OAuth remains a documented future upgrade path if Reddit grants access (form was submitted 2026-04-27, still pending).

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

1. Design through 9 multi-model panel review rounds.
2. Pivoted from OAuth + PRAW to httpx + Reddit's public `.json` transport (sidesteps Responsible Builder Policy gate, structurally read-only, exposes rate-limit headers cleanly).
3. Phase 0 spike against the live API: confirmed transport works, mapped error responses (bogus thread IDs return 403 not 404; r/lounge returns 403 with `reason="gold_only"`), pagination cursor works, `/api/morechildren.json` works, signal-to-noise ratio in comment responses is ~10% (so v0.2 markdown preprocessor has ~10× token leverage).
4. Built v0.1: pyproject + core (errors / config / keys / client / cache / operations) + cli (argparse adapter) + tests (51 unit tests + live smoke). 51/51 passing.
5. Tagged v0.1.0. Pushed to GitHub.
6. **Deployed to warehouse-vm under `redditmcp` user (2026-05-05).** `/home/redditmcp/reddit-research/source/` (cloned at v0.1.0), `/home/redditmcp/reddit-research/venv/` (with the package installed editable), `/home/redditmcp/reddit-cache/` (mode 0700) holding `.env` (mode 0600) + `cache.db` (mode 0600). `reddit-cli` console script verified working.
7. Reddit API access request still pending from the 2026-04-27 form submission; no longer blocking anything.
8. **v0.2 chunk 1: MCP server adapter shipped (2026-05-07/08, commits `419810e` + `2cef169`).** FastMCP-based stdio server exposing the 6 core ops + `reset_budget` as MCP tools. Round-10 panel review (Codex BLOCK → APPROVE after fixes / Gemini APPROVE-WITH-FIXES → APPROVE / Opus APPROVE). 13 new MCP tests; suite is 71/71. Live JSON-RPC handshake verified. `mcp` SDK installed via `[mcp]` extra; lazy-imported with friendly install hint when missing.

## What's next (v0.2 candidates, in recommended priority order)

1. **Markdown preprocessor** (~200 lines) — strips Reddit's ~90% operational metadata, formats comment trees as depth-indented markdown for LLM consumption. Phase 0 measured ~10× token leverage on the most expensive paths. Particularly valuable now that MCP is shipped and starts burning tokens on raw JSON.
2. **Pull v0.2 chunk 1 to production on warehouse-vm.** Run the standard update flow under `redditmcp`, then verify with the live JSON-RPC handshake. Production is still on v0.1.0; the MCP server isn't accessible via `ssh warehouse reddit-mcp` yet. Will need `pip install -e '.[mcp]'` (note the extra) to install the SDK on the production venv.
3. **Snapshots / delta-over-time research** — if a research workflow ever wants to track how a thread evolves. Not currently needed.
4. **Write capability behind flag** — depends on Reddit OAuth access (form pending). Until granted, defer.
5. **Async (httpx async)** — single-user has no parallelism need. YAGNI.

## Operating notes for production

- Update flow on warehouse-vm:
  ```bash
  sudo -u redditmcp -H bash -c '
    cd ~/reddit-research/source
    git fetch --tags
    git checkout vX.Y.Z   # or main for unreleased
    ~/reddit-research/venv/bin/pip install -e ".[mcp]"   # include MCP extra
  '
  ```
  The `[mcp]` extra was added in v0.2 chunk 1 — without it, `reddit-mcp` exits 1 with a clear "install with `pip install 'reddit-research[mcp]'`" hint, but works fine. The CLI `reddit-cli` does not need it.
- Smoke test post-deploy: `sudo -u redditmcp -H ~/reddit-research/venv/bin/python ~/reddit-research/source/tests/smoke_test.py`
- MCP handshake post-deploy:
  ```bash
  sudo -u redditmcp -H bash -c '
    echo "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"initialize\",\"params\":{\"protocolVersion\":\"2025-06-18\",\"capabilities\":{},\"clientInfo\":{\"name\":\"smoke\",\"version\":\"0\"}}}" | ~/reddit-research/venv/bin/reddit-mcp
  '
  ```
  Should return `serverInfo: {"name":"reddit-research","version":"<pkg>"}` plus the tool list.
- `reddit-cli` and `reddit-mcp` console scripts are at `/home/redditmcp/reddit-research/venv/bin/`. Not in `redditmcp`'s PATH unless venv is activated; for ad-hoc calls use the absolute path or `sudo -u redditmcp -H bash -c 'source ~/reddit-research/venv/bin/activate && reddit-cli ...'`.

## Operating constraints to respect

- **Don't trip Reddit's edge rate limiter.** Read every `X-Ratelimit-*` header; back off proactively when `Remaining < 10`. Honor 429 with `Retry-After`.
- **Hard data isolation from Motroco.** If a Motroco project ever needs Reddit data, it deploys a separate install under a different service user with its own cache.db. Same code, different data. See historian.
- **No network listener.** MCP transport is stdio only; remote access via SSH.
- **Transport is swappable.** If Reddit kills unauthenticated `.json`, swap the client out — OAuth (when granted) is the slot-in replacement.

## How to update this handoff

Anything that changes design (operations, schema, caps, decisions) → update `manifest.json` in the same commit. Then update CLAUDE.md (human-readable) and this HANDOFF.md (status + what's next) to match. Three files, one source of truth.
