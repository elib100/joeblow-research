"""Reddit JSON transport.

A thin :class:`httpx.Client` wrapper that:

- Targets Reddit's public ``.json`` endpoints (no auth).
- Reads ``X-Ratelimit-*`` headers on every response and exposes them.
- Backs off proactively when the published headroom drops below a threshold.
- Honors ``Retry-After`` on 429 once, then raises.
- Refuses to follow redirects automatically — the panel reasoning is that a
  silent follow can hand back JSON for a different subreddit when Reddit
  renames or the URL is wrong. Empirically observed redirects: zero across
  the Phase 0 spike's test cases, but the safety remains worth having.
- Maps non-2xx responses to typed exceptions via :func:`from_http_response`.

The client is **transport-agnostic at the call signature level**: ``get(path,
params)`` exposes Reddit's URL paths but doesn't bake in ``.json`` URL
construction at the call site. If/when an OAuth-backed client lands later, it
can satisfy the same shape.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from reddit_research.core.errors import (
    BudgetExceededError,
    RateLimitError,
    TransportError,
    from_http_response,
)


logger = logging.getLogger(__name__)


# ---- Tunables --------------------------------------------------------------

DEFAULT_BASE_URL = "https://www.reddit.com"

# httpx.Timeout splits connect + read so a slow network handshake doesn't get
# treated the same as a slow download.
DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)

# When X-Ratelimit-Remaining drops below this, sleep until reset rather than
# pressing on. The published budget is ~100 per ~10-min window (verified Phase
# 0); 10 leaves substantial margin for any concurrent activity.
HEADROOM_BACKOFF_THRESHOLD = 10.0

# Retry-After ceiling — if Reddit asks us to wait longer than this, give up
# rather than block the calling process.
MAX_RETRY_AFTER_SECONDS = 120.0

# How many times to retry a 429 with Retry-After before raising.
MAX_429_RETRIES = 1


# ---- Workflow budget ------------------------------------------------------


class WorkflowBudget:
    """Per-workflow aggregate caps to prevent runaway research sessions.

    Counts HTTP calls and comments fetched. When either cap is exceeded, the
    next operation raises :class:`BudgetExceededError` instead of silently
    fetching more. Per-process for the CLI; per-session for the MCP server.

    Pass an instance of this into the client; it's incremented on every call
    that successfully reaches the wire (including failed-with-status ones,
    which still cost rate budget).
    """

    def __init__(self, max_api_calls: int = 50, max_comments: int = 500) -> None:
        self.max_api_calls = max_api_calls
        self.max_comments = max_comments
        self.api_calls = 0
        self.comments = 0

    def spend_api_call(self) -> None:
        if self.api_calls >= self.max_api_calls:
            raise BudgetExceededError(
                "api_calls", used=self.api_calls, cap=self.max_api_calls
            )
        self.api_calls += 1

    def spend_comments(self, n: int) -> None:
        if self.comments + n > self.max_comments:
            raise BudgetExceededError(
                "comments", used=self.comments + n, cap=self.max_comments
            )
        self.comments += n

    def snapshot(self) -> dict:
        return {
            "api_calls": self.api_calls,
            "max_api_calls": self.max_api_calls,
            "comments": self.comments,
            "max_comments": self.max_comments,
        }


# ---- Client ---------------------------------------------------------------


class RedditJSONClient:
    """Reddit ``.json`` transport with rate-limit awareness."""

    def __init__(
        self,
        user_agent: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: httpx.Timeout | None = None,
        budget: WorkflowBudget | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            headers={"User-Agent": user_agent},
            timeout=timeout or DEFAULT_TIMEOUT,
            follow_redirects=False,
        )
        self.user_agent = user_agent
        self.headroom: dict | None = None
        self.last_call_status: int | None = None
        self.budget = budget

    # ---- public API ----

    def get(self, path: str, params: dict | None = None) -> Any:
        """Issue a GET, observe rate-limit headers, return parsed JSON.

        Raises subclasses of :class:`HTTPError` on non-2xx, or
        :class:`TransportError` on network failure. 429 is honored with up to
        :data:`MAX_429_RETRIES` retry pass(es) using ``Retry-After``.

        Note: this isn't backwards-compatible with the spike's
        ``(body, status)`` return tuple — at the library level, raising on
        non-2xx is more idiomatic. The spike returned status because it was
        deliberately probing error paths; production callers should catch the
        typed exceptions instead.
        """
        if self.budget is not None:
            self.budget.spend_api_call()

        # Proactive backoff: if last response showed remaining < threshold,
        # sleep until reset. (First call has no prior headroom; we sail in.)
        self._proactive_backoff()

        attempts_left = 1 + MAX_429_RETRIES
        while True:
            attempts_left -= 1
            try:
                r = self._client.get(path, params=params)
            except httpx.HTTPError as e:
                raise TransportError(f"{type(e).__name__}: {e}") from e

            self.last_call_status = r.status_code
            self._record_headroom(r.headers)

            if 200 <= r.status_code < 300:
                return self._parse_body(r)

            # Non-2xx — let the typed-exception mapper decide what to raise.
            body = self._parse_body(r, allow_non_json=True)
            exc = from_http_response(
                r.status_code, path, body, headers=dict(r.headers)
            )

            # 429 retry: honor Retry-After once if we have budget for it.
            if isinstance(exc, RateLimitError) and attempts_left > 0:
                wait = exc.retry_after or 1.0
                if wait > MAX_RETRY_AFTER_SECONDS:
                    logger.warning(
                        "429 with Retry-After=%.1fs exceeds cap %.1fs; raising instead of sleeping",
                        wait,
                        MAX_RETRY_AFTER_SECONDS,
                    )
                    raise exc
                logger.info(
                    "429 from %s; sleeping %.1fs per Retry-After then retrying",
                    path,
                    wait,
                )
                time.sleep(wait + 0.5)  # safety margin
                # Don't re-spend a budget API-call unit for the retry — it's
                # the same logical operation.
                continue

            raise exc

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> RedditJSONClient:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ---- internals ----

    def _parse_body(self, r: httpx.Response, allow_non_json: bool = False) -> Any:
        try:
            return r.json()
        except Exception:
            if allow_non_json:
                return {"_text": r.text[:500]}
            raise TransportError(
                f"non-JSON response from {r.request.url}: {r.text[:200]!r}"
            )

    def _record_headroom(self, headers: httpx.Headers) -> None:
        used = headers.get("x-ratelimit-used")
        rem = headers.get("x-ratelimit-remaining")
        rst = headers.get("x-ratelimit-reset")
        if used is None and rem is None and rst is None:
            return
        self.headroom = {
            "used": _try_float(used),
            "remaining": _try_float(rem),
            "reset_in_seconds": _try_float(rst),
            "reset_raw": rst,
        }

    def _proactive_backoff(self) -> None:
        if self.headroom is None:
            return
        rem = self.headroom.get("remaining")
        rst = self.headroom.get("reset_in_seconds")
        if rem is None or rst is None:
            return
        if rem < HEADROOM_BACKOFF_THRESHOLD:
            wait = max(rst, 1.0)
            if wait > MAX_RETRY_AFTER_SECONDS:
                logger.warning(
                    "Headroom low (remaining=%.1f) but reset=%ss exceeds cap; not backing off",
                    rem,
                    rst,
                )
                return
            logger.info(
                "Headroom remaining=%.1f below threshold; sleeping %.1fs until reset",
                rem,
                wait,
            )
            time.sleep(wait + 0.5)


# ---- Helpers --------------------------------------------------------------


def _try_float(s: object) -> float | None:
    if s is None:
        return None
    try:
        return float(s)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
