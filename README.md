# joeblow-research

A personal research tool: an LLM (Claude, via the Model Context Protocol) drives Reddit search and summarizes discussions on demand.

**Status:** design v4. The design has been through multiple rounds of multi-model review (Codex / Gemini / Opus). No production code yet — implementation begins after a Phase 0 verification spike confirms transport behavior.

## Use case

Single user. When researching a topic — for example, medical questions about a family member's condition, technical problems, or first-hand experiences on a subject — I want my AI assistant to look up the most relevant Reddit threads, read the top-voted comments, and summarize the findings.

The tool is a Python library exposed to the LLM via a local stdio MCP server. Output goes only to me; nothing is republished, redistributed, used commercially, or used to train models.

## Transport: Reddit's public `.json` endpoints

Reddit exposes every page as JSON when you append `.json` to the URL: `https://www.reddit.com/r/python/hot.json` etc. This works without authentication, returns the same JSON shape the OAuth API does, and exposes rate-limit headers cleanly. For a single-user read-only research tool, it's a strictly better fit than OAuth + PRAW:

- No support-form approval gate (Reddit's Responsible Builder Policy, Nov 2025).
- No OAuth handshake or password storage — the configuration has no secrets.
- Rate-limit headers (`X-Ratelimit-Used` / `Remaining` / `Reset`) come back on every response, so the client can back off proactively.
- Structurally read-only — there are no write endpoints exposed via `.json`.

**Findings:** [`docs/json_endpoint_findings.md`](docs/json_endpoint_findings.md).

OAuth + PRAW is documented as the upgrade path for if/when Reddit grants elevated access — useful for higher rate limits or write capability — but is not on the MVP roadmap.

## Design principles

- **Read-only by transport.** The `.json` endpoints have no write capability. Write-flag plumbing returns only if/when OAuth is added later as an upgrade.
- **Strict rate-limit discipline.** Read every `X-Ratelimit-*` header; back off proactively when remaining drops below 10; honor 429 with `Retry-After` plus safety margin. Per-call (`depth ≤ 5`, `limit ≤ 50` on `expand_comment`) and per-workflow budgets prevent any single research session from exhausting the rate budget.
- **Local cache for token + speed efficiency.** LLM workflows re-read the same threads repeatedly; all reads cache by default with a per-kind TTL. Bypassable per call with `fresh=True`.
- **Layered architecture.** Core library has no UI/transport-adapter dependencies; CLI, MCP server, and any future agent integrations are thin adapters.
- **Single-user scope.** No multi-tenancy, no public network exposure, no multi-account.

## Repository layout

| File | Purpose |
|---|---|
| [`CLAUDE.md`](CLAUDE.md) | Full design spec — read this first |
| [`HANDOFF.md`](HANDOFF.md) | Session handoff: status, what's next, what's blocked |
| [`docs/manifest.json`](docs/manifest.json) | Machine-readable project state (operations, schema, decisions, caps, panel reviews, session log) |
| [`docs/json_endpoint_findings.md`](docs/json_endpoint_findings.md) | What the `.json` transport supports, rate-limit behavior, edge cases |
| [`docs/reddit-api-inventory.json`](docs/reddit-api-inventory.json) | Scraped OAuth-API endpoint catalog (202 endpoints, 19 sections, 28 OAuth scopes) — reference for the deferred OAuth upgrade path |
| [`phase0_spike.py`](phase0_spike.py) | One-shot verification spike: connectivity, error mapping, `MoreComments` cap, benchmark queries |
| [`requirements-phase0.txt`](requirements-phase0.txt) | Phase 0 dependencies (`httpx`, `python-dotenv`, `tiktoken`) |
| [`.env.example`](.env.example) | Configuration schema (no secret values; the `.json` transport requires no auth) |

## Tech stack

- Python 3.11+
- `httpx` (HTTP client)
- SQLite (single-file local cache, WAL mode)
- `python-dotenv` (configuration loading)
- `tiktoken` (token-cost estimation in benchmarks)
- MCP via stdio transport (planned, post-MVP)

## Not accepting contributions

This is a personal project. Issues are disabled and pull requests will not be reviewed. The README and `CLAUDE.md` together contain all the project context I'm comfortable making public.

## License

MIT — see [LICENSE](LICENSE).
