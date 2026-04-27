# joeblow-research

A personal research tool: an LLM (Claude, via the Model Context Protocol) drives Reddit search and summarizes discussions on demand.

**Status:** design phase. The design has been through three rounds of multi-model review (Codex / Gemini / Opus). No production code yet — implementation begins after a Phase 0 verification spike confirms API behavior assumptions.

## Use case

Single user. When researching a topic — for example, medical questions about a family member's condition, technical problems, or first-hand experiences on a subject — I want my AI assistant to look up the most relevant Reddit threads, read the top-voted comments, and summarize the findings.

The tool is a Python library exposed to the LLM via a local stdio MCP server. Output goes only to me; nothing is republished, redistributed, used commercially, or used to train models.

## Design principles

- **Read-only by default.** Write capability exists in the spec but is gated behind an explicit configuration flag (`REDDIT_WRITE_ENABLED`). PRAW is initialized with `read_only=True` as defense in depth.
- **Strict rate-limit discipline.** PRAW's internal backoff plus a mandatory cap on `MoreComments` expansion (`replace_more(limit=10)`). Per-call and per-workflow budgets prevent any single research session from exhausting the API budget.
- **Local cache for token + speed efficiency.** LLM workflows re-read the same threads repeatedly; all reads cache by default with a per-kind TTL. Bypassable per call with `fresh=True`.
- **Layered architecture.** Core library has no UI/transport dependencies; CLI, MCP server, and any future agent integrations are thin adapters.
- **Single-user scope.** No multi-tenancy, no public network exposure, no multi-account.

## Repository layout

| File | Purpose |
|---|---|
| [`CLAUDE.md`](CLAUDE.md) | Full design spec — read this first |
| [`HANDOFF.md`](HANDOFF.md) | Session handoff: status, what's next, what's blocked |
| [`docs/manifest.json`](docs/manifest.json) | Machine-readable project state (operations, schema, decisions, caps, panel reviews, session log) |
| [`docs/reddit-api-inventory.json`](docs/reddit-api-inventory.json) | Scraped Reddit API endpoint catalog (202 endpoints across 19 sections, with OAuth scopes) |
| [`phase0_spike.py`](phase0_spike.py) | One-shot verification spike: auth check, error mapping, `MoreComments` cap, benchmark queries |
| [`requirements-phase0.txt`](requirements-phase0.txt) | Phase 0 dependencies (`praw`, `python-dotenv`, `tiktoken`) |
| [`.env.example`](.env.example) | Configuration schema (no secret values) |

## Tech stack

- Python 3.11+
- PRAW (Python Reddit API Wrapper)
- SQLite (single-file local cache, WAL mode)
- python-dotenv (configuration loading)
- MCP via stdio transport (planned, post-MVP)

## Not accepting contributions

This is a personal project. Issues are disabled and pull requests will not be reviewed. The README and `CLAUDE.md` together contain all the project context I'm comfortable making public.

## License

MIT — see [LICENSE](LICENSE).
