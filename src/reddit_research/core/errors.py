"""Exception hierarchy for reddit_research.

The mapping from HTTP status to exception type was derived empirically by the
Phase 0 spike (see docs/json_endpoint_findings.md). Notable surprises:

- Bogus thread IDs return 403, not 404 — Reddit treats invalid thread IDs as
  Forbidden rather than Not Found.
- Banned subreddits return 404 (no `reason` field) — indistinguishable from a
  never-existed subreddit on this transport.
- Premium-only subreddits return 403 with ``reason="gold_only"`` — the
  ``reason`` field is the only structured discriminator within 403s.
"""

from __future__ import annotations


class RedditError(Exception):
    """Base exception for all reddit_research-raised errors."""


# ---- Transport / network ---------------------------------------------------


class TransportError(RedditError):
    """Network failure, timeout, DNS issue, or otherwise non-HTTP error."""


# ---- HTTP-status errors ---------------------------------------------------


class HTTPError(RedditError):
    """Base for errors derived from a Reddit HTTP response.

    Attributes:
        status: the HTTP status code returned by Reddit.
        path: the request path that produced the error.
        body: the parsed response body (dict for typical Reddit errors).
        reason: the Reddit ``reason`` field on the response body, if present.
            For 403s this is the only way to distinguish gold-only / private /
            banned / quarantined etc.
    """

    def __init__(
        self,
        status: int,
        path: str,
        body: object,
        reason: str | None = None,
        message: str | None = None,
    ) -> None:
        self.status = status
        self.path = path
        self.body = body
        self.reason = reason
        self.message = message
        detail = f"reason={reason!r}" if reason else f"message={message!r}"
        super().__init__(f"HTTP {status} on {path} ({detail})")


class NotFoundError(HTTPError):
    """HTTP 404. Sub or thread doesn't exist, or sub is banned (indistinguishable)."""


class ForbiddenError(HTTPError):
    """HTTP 403. Includes invalid thread IDs (no ``reason``) and gold-only /
    private / quarantined subs (with ``reason``).

    Use the :attr:`reason` attribute to discriminate cases. Empirically observed
    values include ``"gold_only"``; ``private``, ``banned``, and ``quarantined``
    are documented but not yet observed in spike output.
    """


class RateLimitError(HTTPError):
    """HTTP 429. Always include ``retry_after`` (seconds) when available.

    The client is responsible for honoring ``retry_after`` and a single retry
    pass; this exception is raised only when retry exhausts.
    """

    def __init__(
        self,
        status: int,
        path: str,
        body: object,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(status, path, body)
        self.retry_after = retry_after


class UpstreamError(HTTPError):
    """HTTP 5xx. Reddit-side problem; not cached, briefly retryable."""


class RedirectError(HTTPError):
    """HTTP 3xx with ``follow_redirects=False``.

    The client never follows redirects automatically. If Reddit issues a 3xx,
    we raise this and let the caller decide. Not observed in Phase 0 across
    the test cases tried — added for safety.
    """

    def __init__(
        self,
        status: int,
        path: str,
        body: object,
        location: str | None = None,
    ) -> None:
        super().__init__(status, path, body)
        self.location = location


# ---- Validation / budget --------------------------------------------------


class InvalidIdError(RedditError):
    """Caller-supplied ``id`` or ``fullname`` doesn't match the expected pattern."""


class BudgetExceededError(RedditError):
    """Per-workflow aggregate budget cap tripped (max API calls or comments).

    Attributes:
        kind: ``"api_calls"`` or ``"comments"``.
        used: how many of that resource were consumed before the cap tripped.
        cap: the cap value.
    """

    def __init__(self, kind: str, used: int, cap: int) -> None:
        self.kind = kind
        self.used = used
        self.cap = cap
        super().__init__(
            f"Per-workflow budget exceeded: {kind} used={used} cap={cap}"
        )


# ---- Mapping helper -------------------------------------------------------


def from_http_response(
    status: int,
    path: str,
    body: object,
    headers: dict | None = None,
) -> HTTPError:
    """Build the appropriate :class:`HTTPError` subclass from a Reddit response.

    Centralizes the mapping so :mod:`reddit_research.core.client` doesn't have
    to repeat it. Caller is responsible for ensuring this is invoked on
    non-2xx responses only.
    """
    headers = headers or {}
    reason = None
    message = None
    if isinstance(body, dict):
        reason = body.get("reason")
        message = body.get("message")
    if 300 <= status < 400:
        return RedirectError(status, path, body, location=headers.get("location"))
    if status == 404:
        return NotFoundError(status, path, body, reason=reason, message=message)
    if status == 403:
        return ForbiddenError(status, path, body, reason=reason, message=message)
    if status == 429:
        retry_after_raw = headers.get("retry-after")
        retry_after: float | None = None
        if retry_after_raw is not None:
            try:
                retry_after = float(retry_after_raw)
            except (TypeError, ValueError):
                retry_after = None
        return RateLimitError(status, path, body, retry_after=retry_after)
    if 500 <= status < 600:
        return UpstreamError(status, path, body, reason=reason, message=message)
    return HTTPError(status, path, body, reason=reason, message=message)
