"""Core library — pure transport + cache + operations. No UI/adapter deps."""

from reddit_research.core.cache import Cache, CacheHit, CacheStats
from reddit_research.core.client import RedditJSONClient, WorkflowBudget
from reddit_research.core.config import Config, load_config
from reddit_research.core.errors import (
    BudgetExceededError,
    ForbiddenError,
    HTTPError,
    InvalidIdError,
    NotFoundError,
    RateLimitError,
    RedditError,
    RedirectError,
    TransportError,
    UpstreamError,
)
from reddit_research.core.operations import (
    CommentSummary,
    CommentTree,
    Operations,
    Status,
    Thread,
    ThreadSummary,
)

__all__ = [
    "BudgetExceededError",
    "Cache",
    "CacheHit",
    "CacheStats",
    "CommentSummary",
    "CommentTree",
    "Config",
    "ForbiddenError",
    "HTTPError",
    "InvalidIdError",
    "NotFoundError",
    "Operations",
    "RateLimitError",
    "RedditError",
    "RedditJSONClient",
    "RedirectError",
    "Status",
    "Thread",
    "ThreadSummary",
    "TransportError",
    "UpstreamError",
    "WorkflowBudget",
    "load_config",
]
