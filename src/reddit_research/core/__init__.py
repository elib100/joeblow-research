"""Core library — pure transport + cache + operations. No UI/adapter deps."""

from reddit_research.core.client import RedditJSONClient
from reddit_research.core.config import Config, load_config
from reddit_research.core.errors import (
    BudgetExceededError,
    ForbiddenError,
    HTTPError,
    InvalidIdError,
    NotFoundError,
    RateLimitError,
    RedditError,
    TransportError,
    UpstreamError,
)

__all__ = [
    "BudgetExceededError",
    "Config",
    "ForbiddenError",
    "HTTPError",
    "InvalidIdError",
    "NotFoundError",
    "RateLimitError",
    "RedditError",
    "RedditJSONClient",
    "TransportError",
    "UpstreamError",
    "load_config",
]
