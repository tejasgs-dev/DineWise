"""Groq client factory."""

from __future__ import annotations

import groq

from dinewise.config import Settings

# SDK-level retries for transient errors (408/409/429/5xx, connection errors), with
# exponential backoff. The ranker lowers this per call to stay within its deadline.
MAX_RETRIES = 2


def make_client(settings: Settings) -> groq.Groq:
    if not settings.llm_enabled:
        raise ValueError("GROQ_API_KEY is not set")
    return groq.Groq(
        api_key=settings.groq_api_key.get_secret_value(),
        timeout=settings.llm_timeout_s,
        max_retries=MAX_RETRIES,
    )
