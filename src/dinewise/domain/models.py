"""Core domain models (arch §5.3)."""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from dinewise.domain.budget import BudgetBand


def _to_python(value: Any) -> Any:
    """Convert pandas / numpy / pyarrow values from a DataFrame row into plain Python."""
    if hasattr(value, "tolist") and not isinstance(value, str | bytes):
        value = value.tolist()  # numpy arrays and scalars
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    if type(value).__name__ in {"NAType", "NaTType"}:
        return None
    return value


class Restaurant(BaseModel):
    id: str
    name: str
    city: str
    locality: str
    address: str
    zones: list[str] = []  # Zomato listing areas (raw `listed_in(city)`), broader than locality
    cuisines: list[str] = []
    rest_types: list[str] = []
    listing_types: list[str] = []  # raw `listed_in(type)`: Delivery, Dine-out, Buffet, ...
    cost_for_two: int | None = Field(default=None, gt=0)  # INR
    budget_band: BudgetBand | None = None
    rating: float | None = Field(default=None, ge=0, le=5)
    votes: int = Field(default=0, ge=0)
    is_new: bool = False
    online_order: bool = False
    book_table: bool = False
    dishes_liked: list[str] = []
    tags: list[str] = []
    review_snippets: list[str] = []

    @model_validator(mode="before")
    @classmethod
    def _normalize_record(cls, data: Any) -> Any:
        if isinstance(data, dict):
            return {key: _to_python(value) for key, value in data.items()}
        return data


# =============================================================================
# Requests and retrieval results (arch §6, §7)
# =============================================================================


class UserPreferences(BaseModel):
    """What the user asked for, as submitted (arch §6.1)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)  # D-02

    location: str = Field(min_length=1, max_length=100)
    budget: BudgetBand | None = None
    cuisines: list[str] = Field(default_factory=list, max_length=5)
    min_rating: float = Field(default=0, ge=0, le=5, allow_inf_nan=False)
    extras: str | None = Field(default=None, max_length=300)
    top_k: int = Field(default=5, ge=1, le=10)

    @field_validator("budget", mode="before")
    @classmethod
    def _normalize_budget(cls, value: Any) -> Any:
        # Accept "Medium", " LOW " (INP-17); "" and "any" mean no budget.
        if isinstance(value, str):
            value = value.strip().lower()
            if value in {"", "any"}:
                return None
        return value

    @field_validator("cuisines")
    @classmethod
    def _dedupe_cuisines(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for item in value:
            key = item.lower()
            if item and key not in seen:
                seen.add(key)
                out.append(item)
        return out

    @field_validator("extras")
    @classmethod
    def _blank_extras_to_none(cls, value: str | None) -> str | None:
        return value or None


class Correction(BaseModel):
    """A user value that was matched to a different canonical value (INP-02)."""

    field: Literal["location", "cuisines"]
    input: str
    value: str


class NormalizedPreferences(BaseModel):
    """Preferences after matching against the dataset; what retrieval actually uses."""

    location: str  # canonical display value, e.g. "Koramangala" or "Bangalore"
    location_kind: Literal["locality", "city"]
    city: str
    localities: list[str]  # neighbourhoods covered; empty for a city-wide search
    budget: BudgetBand | None = None
    cuisines: list[str] = []
    min_rating: float = 0
    extras: str | None = None
    extras_tags: list[str] = []
    top_k: int = 5
    corrections: list[Correction] = []


class RetrievalResult(BaseModel):
    """The shortlist handed to the ranker, best heuristic score first (arch §7)."""

    preferences: NormalizedPreferences
    candidates: list[Restaurant]
    scores: dict[str, float]  # restaurant id -> heuristic pre-score
    relaxations: list[str] = []
    total_matches: int = 0  # matches before shortlisting


class Recommendation(BaseModel):
    """One ranked pick, as returned to the user (arch §9.2)."""

    rank: int = Field(ge=1)
    restaurant: Restaurant
    explanation: str
    caveats: str | None = None
    source: Literal["llm", "heuristic"]


class ResponseMeta(BaseModel):
    candidates_considered: int  # size of the shortlist given to the ranker
    total_matches: int  # matches before shortlisting
    ranking: Literal["llm", "heuristic"]
    model: str | None = None  # the model that produced the ranking, if an LLM did
    latency_ms: int


class RecommendationResponse(BaseModel):
    """The full answer to one request (arch §9.2)."""

    recommendations: list[Recommendation]
    summary: str
    relaxations: list[str] = []
    degraded: bool  # True when rule-based ranking was used instead of the LLM
    normalized_preferences: NormalizedPreferences  # what was actually searched (D-08)
    meta: ResponseMeta
