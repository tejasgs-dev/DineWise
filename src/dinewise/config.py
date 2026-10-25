"""Application settings, loaded from environment variables and an optional `.env` file.

Every DineWise variable uses the ``DINEWISE_`` prefix (for example ``DINEWISE_MODEL``).
The Groq credential is the standard ``GROQ_API_KEY``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ReasoningEffort = Literal["low", "medium", "high"]


@dataclass(frozen=True)
class ScoringWeights:
    """Heuristic pre-score weights (arch §7.3)."""

    rating: float
    votes: float
    cuisine: float
    tags: float


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DINEWISE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- LLM (Groq) -------------------------------------------------------
    groq_api_key: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices("GROQ_API_KEY", "groq_api_key"),
    )
    # Must support strict JSON-schema output on Groq (implementation plan, Phase 3).
    model: str = "openai/gpt-oss-120b"
    # Tried once when the primary model gives no usable answer. Set empty to disable.
    fallback_model: str | None = "openai/gpt-oss-20b"
    # Sized for Groq's free tier (8K tokens/minute, 200K/day for gpt-oss-120b), which
    # counts the prompt plus this completion limit: about 3K tokens per request in all.
    reasoning_effort: ReasoningEffort = "low"
    llm_max_completion_tokens: int = Field(default=1200, ge=256)  # reasoning + JSON answer
    llm_timeout_s: float = Field(default=30.0, gt=0)  # per HTTP request
    # Upper bound for the whole LLM step, including SDK retries and the fallback model
    # (edge cases API-14, OPS-08).
    llm_deadline_s: float = Field(default=20.0, gt=0)

    # --- Retrieval --------------------------------------------------------
    # Candidates sent to the LLM; each costs about 115 prompt tokens.
    shortlist_size: int = Field(default=10, ge=1, le=100)
    min_candidates: int = Field(default=5, ge=1)
    # At most this many branches of one chain in a shortlist (edge case D-07).
    max_branches_per_chain: int = Field(default=2, ge=1)
    # rapidfuzz score (0-100) needed to accept a misspelled location or cuisine.
    fuzzy_match_threshold: int = Field(default=85, ge=50, le=100)

    # Cost for two (INR). Defaults chosen from the dataset's distribution so each
    # band holds a usable share of restaurants (~59% / 34% / 6%; docs/data-notes.md).
    budget_low_max: int = Field(default=400, gt=0)
    budget_medium_max: int = Field(default=1000, gt=0)

    weight_rating: float = Field(default=0.45, ge=0)
    weight_votes: float = Field(default=0.25, ge=0)
    weight_cuisine: float = Field(default=0.15, ge=0)
    weight_tags: float = Field(default=0.15, ge=0)

    # --- API --------------------------------------------------------------
    # Browser origins allowed to call the API (edge case SEC-14). In env, a JSON list:
    # DINEWISE_CORS_ORIGINS='["http://localhost:8501"]'
    cors_origins: list[str] = ["http://localhost:8501", "http://127.0.0.1:8501"]
    # Also allow origins matching this regex, e.g. Vercel preview deployments, whose URLs
    # change per deploy: '^https://dinewise(-[a-z0-9-]+)?\.vercel\.app$'
    cors_origin_regex: str | None = None
    max_request_bytes: int = Field(default=16_384, gt=0)  # edge case SEC-08
    # Recommendation requests per client IP per minute; 0 disables the limit (SEC-09).
    rate_limit_per_minute: int = Field(default=10, ge=0)
    # Trusted proxies in front of the API that append to X-Forwarded-For: 0 when it is
    # reached directly, 1 on Render. Used to find the client IP for the limit (SEC-10).
    forwarded_hops: int = Field(default=0, ge=0)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_format: Literal["json", "text"] = "json"

    # --- Data -------------------------------------------------------------
    data_path: Path = Path("data/restaurants.parquet")
    meta_path: Path = Path("data/meta.json")
    raw_dir: Path = Path("data/raw")
    hf_dataset: str = "ManikaSaini/zomato-restaurant-recommendation"
    hf_filename: str = "zomato.csv"
    # Pinned for reproducible builds (edge case DATA-37). Set empty to use the latest revision.
    hf_revision: str | None = "5738e9eda2fad49ad51c6e0ed26e761d9b947133"

    @model_validator(mode="after")
    def _check_consistency(self) -> Settings:
        if self.budget_low_max >= self.budget_medium_max:
            raise ValueError(
                f"budget_low_max ({self.budget_low_max}) must be less than "
                f"budget_medium_max ({self.budget_medium_max})"
            )
        total = self.weight_rating + self.weight_votes + self.weight_cuisine + self.weight_tags
        if not math.isclose(total, 1.0, abs_tol=1e-6):
            raise ValueError(f"scoring weights must sum to 1.0 (got {total:.4f})")
        if self.min_candidates > self.shortlist_size:
            raise ValueError(
                f"min_candidates ({self.min_candidates}) must not exceed "
                f"shortlist_size ({self.shortlist_size})"
            )
        if self.cors_origin_regex == "":
            self.cors_origin_regex = None
        if self.hf_revision == "":
            self.hf_revision = None
        if not self.fallback_model or self.fallback_model == self.model:
            self.fallback_model = None
        return self

    @property
    def llm_enabled(self) -> bool:
        """True when an API key is configured; otherwise the app serves heuristic results."""
        return self.groq_api_key is not None and bool(self.groq_api_key.get_secret_value().strip())

    @property
    def scoring_weights(self) -> ScoringWeights:
        return ScoringWeights(
            rating=self.weight_rating,
            votes=self.weight_votes,
            cuisine=self.weight_cuisine,
            tags=self.weight_tags,
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings instance (cached after the first call)."""
    return Settings()
