"""HTTP endpoints (arch §9.1)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from dinewise.api.schemas import (
    BudgetBandOut,
    BudgetsOut,
    CuisinesOut,
    ErrorResponse,
    HealthOut,
    LocationsOut,
    NamedCount,
    RecommendationResponseOut,
)
from dinewise.domain.budget import BANDS, band_label, band_range
from dinewise.domain.models import RecommendationResponse, UserPreferences
from dinewise.service.recommender import Recommender


class ServiceUnavailableError(RuntimeError):
    """The restaurant data isn't loaded, so nothing can be served (edge case API-01)."""


def get_recommender(request: Request) -> Recommender:
    recommender = getattr(request.app.state, "recommender", None)
    if recommender is None:
        reason = getattr(request.app.state, "load_error", None) or "restaurant data is loading"
        raise ServiceUnavailableError(reason)
    return recommender


RecommenderDep = Annotated[Recommender, Depends(get_recommender)]

api = APIRouter(prefix="/api/v1")
health = APIRouter()

_ERRORS = {
    422: {"model": ErrorResponse, "description": "Unknown location or cuisine, or invalid input"},
    503: {"model": ErrorResponse, "description": "Restaurant data not loaded"},
}


@api.post(
    "/recommendations",
    response_model=RecommendationResponseOut,
    responses=_ERRORS,
    summary="Recommend restaurants for a set of preferences",
)
def recommend(prefs: UserPreferences, recommender: RecommenderDep) -> RecommendationResponse:
    # A sync endpoint: FastAPI runs it in a worker thread, so a slow LLM call in one
    # request doesn't block the event loop for the others (edge case API-07).
    return recommender.recommend(prefs)


@api.get(
    "/meta/locations",
    response_model=LocationsOut,
    responses={503: _ERRORS[503]},
    summary="Neighbourhoods that can be searched, with restaurant counts",
)
def locations(recommender: RecommenderDep) -> LocationsOut:
    repo = recommender.repo
    counts = repo.locality_counts()
    cities = repo.cities()
    return LocationsOut(
        city=cities[0] if len(cities) == 1 else ", ".join(cities),
        locations=[NamedCount(name=n, count=counts[n]) for n in repo.known_localities()],
    )


@api.get(
    "/meta/cuisines",
    response_model=CuisinesOut,
    responses={503: _ERRORS[503]},
    summary="Cuisines that can be searched, with restaurant counts",
)
def cuisines(recommender: RecommenderDep) -> CuisinesOut:
    repo = recommender.repo
    counts = repo.cuisine_counts()
    return CuisinesOut(
        cuisines=[NamedCount(name=n, count=counts[n]) for n in repo.known_cuisines()]
    )


@api.get(
    "/meta/budgets",
    response_model=BudgetsOut,
    summary="Budget bands and their cost-for-two ranges",
)
def budgets(request: Request) -> BudgetsOut:
    settings = request.app.state.settings
    out = []
    for band in BANDS:
        lo, hi = band_range(band, settings)
        out.append(
            BudgetBandOut(band=band, label=band_label(band, settings), min_cost=lo, max_cost=hi)
        )
    return BudgetsOut(budgets=out)


@health.get("/healthz", response_model=HealthOut, responses={503: {"model": HealthOut}})
def healthz(request: Request):
    state = request.app.state
    recommender: Recommender | None = getattr(state, "recommender", None)
    if recommender is None:
        body = HealthOut(
            status="unavailable",
            detail=getattr(state, "load_error", None) or "restaurant data is loading",
        )
        return JSONResponse(status_code=503, content=body.model_dump())  # API-15
    return HealthOut(
        status="ok",
        restaurants=len(recommender.repo),
        build_id=recommender.repo.build_id,
        llm_ranking=recommender.ranker is not None,
    )
