# Reddit `.json` public-endpoint findings

**Probed:** 2026-04-27 (warehouse-vm, curl + identifying User-Agent).

**Purpose:** verify that Reddit's append-`.json`-to-URL pattern still works as a fallback if the OAuth API access request is denied.

## Bottom line

The `.json` endpoint isn't just a viable fallback — it's a **strictly better fit** for our use case than OAuth + PRAW on three dimensions, and equivalent on the rest. We should consider promoting it from "fallback" to "primary path."

## What works

| Probe | Endpoint | Result |
|---|---|---|
| 1 | `/r/python/hot.json?limit=5` | HTTP 200, full Listing, 27 KB |
| 2 | `/r/python/search.json?q=asyncio+performance&restrict_sr=1` | HTTP 200, full Listing, 37 KB |
| 3 | `/search.json?q=claude+api+caching` (search-all) | HTTP 200, full Listing, 65 KB |
| 4 | `/r/Python/comments/<id>.json?limit=20` (thread + comments) | HTTP 200, `[post, comments]` shape, 61 KB |
| 5 | `/api/morechildren.json?link_id=t3_X&children=A,B,...` | HTTP 200, expanded children returned |

Returns the **exact same JSON structure as the OAuth API** — `Listing` → `t3` items with the full set of fields (`subreddit`, `selftext`, `score`, `num_comments`, `author`, `permalink`, etc.). Drop-in compatible.

## What rate-limit looks like

- Header `X-Ratelimit-Used`, `X-Ratelimit-Remaining`, `X-Ratelimit-Reset` **all exposed cleanly** on every response (this is the headroom observability that panel reviews flagged we couldn't get from PRAW).
- Sustained 10 calls in succession: every call returned 200, `Remaining` decremented by 1 each time, `Reset` ticked down with wall-clock seconds.
- **Reset window is ~600 seconds** (10 min), not 60. So the actual budget is ~100 calls per 10-minute sliding window — meaningfully more generous than the published 60-100 QPM figure.
- For a typical research session (50–100 calls, then idle for 30+ min), this is plenty.

## Edge cases mapped

| Scenario | Status | Body | Distinguishable? |
|---|---|---|---|
| Nonexistent subreddit | `404` | `{"message":"404 Not Found","error":404}` | ✓ |
| Bogus thread id | `404` | same shape | ✓ |
| Premium-only sub (r/lounge) | `403` | `{"reason":"gold_only","message":"Forbidden","error":403}` | ✓ — `reason` field discriminates |

This is **better error data** than PRAW typically surfaces — the `reason` field gives a structured reason for forbidden access (`gold_only`, `private`, `banned`, `quarantined`, etc.), which the OAuth flow loses behind a generic `Forbidden` exception.

## Trade-offs vs OAuth + PRAW

| Dimension | `.json` endpoints | OAuth + PRAW |
|---|---|---|
| Auth complexity | None | Form approval, OAuth handshake, refresh tokens, password storage |
| Approval gate | None | Reddit Responsible Builder Policy form (Nov 2025) |
| Dependency | `httpx` or stdlib `urllib` | `praw` + `prawcore` |
| Rate-limit headers visible | ✓ Yes | ✗ Hidden by PRAW (panel's prior P1 finding) |
| Listing pagination | Manual `after`/`before` cursor | PRAW auto-paginates |
| Comment-tree expansion | Manual via `/api/morechildren.json` | PRAW's `replace_more` |
| Backoff on 429 | We implement | PRAW does it |
| Read user identity (`me`) | Not available | Available (with full auth) |
| Write capability | Not available (structurally read-only) | Available behind flag |
| Risk of vendor lockout | Reddit could block unauth at any time | Already gated behind RBP form |
| Already approved? | ✓ Works today | ✗ Awaiting Reddit response |

## Recommendation

**Treat `.json` as the primary path for v0.1.** Reasons:

1. **Eliminates the approval blocker.** We can start building today without waiting on Reddit's review.
2. **Solves the headroom-observability concern** the panel flagged in earlier rounds.
3. **Smaller dependency footprint** — `httpx` is well-known, well-maintained, no PRAW-specific quirks to debug.
4. **Structurally read-only** — there's no `.json` endpoint for writes, so the write-flag/read-only-mode plumbing becomes unnecessary in MVP.
5. **Better error data** via the `reason` field on 403s.

If Reddit ever approves OAuth access, we can add it as an *upgrade path* (for higher rate limits, write capability, or user-context reads) rather than a primary requirement.

The architectural change is small: replace "PRAW client" in the core with a thin `RedditJSONClient` that wraps `httpx` + URL templating + rate-limit observation + retry/backoff. The cache layer, caps, key normalization, and write-gate (still useful for paranoia, even if no write path exists) all stay the same.

## Risks worth naming

- **Reddit could block unauthenticated `.json` access at any time without warning.** They've done partial restrictions before. Mitigation: keep the OAuth path designed-in even if not implemented; if `.json` breaks, swap the transport.
- **No user-context operations.** If we ever wanted `me()`, subscribed-subs, voting history etc., we'd need OAuth. None of those are in our use case (research, not personal-feed).
- **Less idiomatic than PRAW** — we'd be writing our own listing iterator, comment-tree walker, etc. PRAW's conveniences are real. Our scope is narrow enough that this isn't crippling.

## Open questions to revisit

- Does the `.json` rate budget interact with any IP-based throttling that's separate from the per-UA budget? The header-reported budget is one thing; Cloudflare-level throttling is another. Probably fine, but worth watching for 429s under sustained use.
- Are there any endpoints we need that are *only* available via OAuth (not via `.json`)? Initial walk-through of `docs/reddit-api-inventory.json` suggests no for the read-side endpoints we care about.
- Is there a way to query the comment-context endpoint (`/r/X/comments/Y/_/Z.json` for a single comment with parents)? Worth verifying.
