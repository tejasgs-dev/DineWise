"""Public API contract (arch §9.2). These models shape responses and the OpenAPI docs.

Internal models carry more fields (zones, review snippets, ...) than clients need;
FastAPI filters responses down to these.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from dinewise.domain.budget import BudgetBand
from dinewise.domain.models import NormalizedPreferences, ResponseMeta


class RestaurantOut(BaseModel):
    id: str
    name: str
    city: str
    locality: str
    address: str
    cuisines: list[str]
    rest_types: list[str]
    cost_for_two: int | None
    budget_band: BudgetBand | None
    rating: float | None
    votes: int
    is_new: bool
    online_order: bool
    book_table: bool
    tags: list[str]


class RecommendationOut(BaseModel):
    rank: int
    restaurant: RestaurantOut
    explanation: str
    caveats: str | None
    source: Literal["llm", "heuristic"]


class RecommendationResponseOut(BaseModel):
    recommendations: list[RecommendationOut]
    summary: str
    relaxations: list[str]
    degraded: bool
    normalized_preferences: NormalizedPreferences
    meta: ResponseMeta


class NamedCount(BaseModel):
    name: str
    count: int


class LocationsOut(BaseModel):
    city: str
    locations: list[NamedCount]


class CuisinesOut(BaseModel):
    cuisines: list[NamedCount]


class BudgetBandOut(BaseModel):
    band: BudgetBand
    label: str  # e.g. "₹401–1,000 for two"
    min_cost: int
    max_cost: int | None


class BudgetsOut(BaseModel):
    budgets: list[BudgetBandOut]


class HealthOut(BaseModel):
    status: Literal["ok", "unavailable"]
    restaurants: int = 0
    build_id: str | None = None
    llm_ranking: bool = False
    detail: str | None = None


class ErrorDetail(BaseModel):
    code: str
    message: str
    field: str | None = None
    value: str | None = None
    suggestions: list[str] = []


class ErrorResponse(BaseModel):
    error: ErrorDetail
    request_id: str | None = None
