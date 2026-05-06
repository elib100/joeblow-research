# Handoff — reddit_research

**Updated:** 2026-05-05
**Status:** **v0.1.0 tagged + deployed.** Released on GitHub at <https://github.com/elib100/joeblow-research/releases/tag/v0.1.0>; production install live on warehouse-vm under `redditmcp` user. Panel-cleared through 9 review rounds (Codex / Gemini / Opus). 51/51 unit tests + live smoke test passing.

## TL;DR

Personal Reddit research tool — Python library + CLI (v0.1.0 done). MCP server adapter is the next major addition. Single user, deployed on warehouse-vm. Hard isolation from Motroco data.

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

## What's next (v0.2 candidates, in recommended priority order)

1. **MCP server adapter** (~250 lines) — exposes the six operations as MCP tools so Claude can drive the research workflow natively. Biggest single-step value increase. Built on the existing core; nothing new to design.
2. **Markdown preprocessor** (~200 lines) — strips Reddit's ~90% operational metadata, formats comment trees as depth-indented markdown for LLM consumption. Phase 0 measured ~10× token leverage on the most expensive paths. Particularly valuable once MCP usage starts burning tokens on raw JSON.
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
    ~/reddit-research/venv/bin/pip install -e .
  '
  ```
- Smoke test post-deploy: `sudo -u redditmcp -H ~/reddit-research/venv/bin/python ~/reddit-research/source/tests/smoke_test.py`
- `reddit-cli` console script is at `/home/redditmcp/reddit-research/venv/bin/reddit-cli`. Not in `redditmcp`'s PATH unless venv is activated; for ad-hoc calls use that absolute path or `sudo -u redditmcp -H bash -c 'source ~/reddit-research/venv/bin/activate && reddit-cli ...'`.

## Operating constraints to respect

- **Don't trip Reddit's edge rate limiter.** Read every `X-Ratelimit-*` header; back off proactively when `Remaining < 10`. Honor 429 with `Retry-After`.
- **Hard data isolation from Motroco.** If a Motroco project ever needs Reddit data, it deploys a separate install under a different service user with its own cache.db. Same code, different data. See historian.
- **No network listener.** MCP transport is stdio only; remote access via SSH.
- **Transport is swappable.** If Reddit kills unauthenticated `.json`, swap the client out — OAuth (when granted) is the slot-in replacement.

## How to update this handoff

Anything that changes design (operations, schema, caps, decisions) → update `manifest.json` in the same commit. Then update CLAUDE.md (human-readable) and this HANDOFF.md (status + what's next) to match. Three files, one source of truth.
