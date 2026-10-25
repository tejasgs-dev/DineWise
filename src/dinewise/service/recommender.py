"""Orchestration: normalize -> retrieve -> rank -> assemble (arch §2, §8.5).

The LLM ranker is pluggable (the Groq ranker in `dinewise.llm.ranker`). Whenever a ranker
is missing, returns nothing, or raises, the service answers with the rule-based ranking
and marks the response `degraded`. A failed LLM never turns into an error for the user.
"""

from __future__ import annotations

import logging
import time
from typing import Protocol

from pydantic import BaseModel

from dinewise.config import Settings
from dinewise.domain.models import (
    Recommendation,
    RecommendationResponse,
    ResponseMeta,
    RetrievalResult,
    UserPreferences,
)
from dinewise.retrieval.explain import heuristic_recommendations, template_summary
from dinewise.retrieval.normalize import PreferenceNormalizer
from dinewise.retrieval.repository import RestaurantRepository
from dinewise.retrieval.retriever import CandidateRetriever

log = logging.getLogger(__name__)


class RankingOutcome(BaseModel):
    """What a ranker returns: validated picks drawn only from the candidates."""

    recommendations: list[Recommendation]
    summary: str
    model: str | None = None
    usage: dict[str, int | float] | None = None  # tokens and timings, for logging only


class Ranker(Protocol):
    """Ranks a retrieval shortlist. Returns None when it can't (refusal, outage, ...)."""

    def rank(self, result: RetrievalResult) -> RankingOutcome | None: ...


class Recommender:
    def __init__(
        self, repo: RestaurantRepository, settings: Settings, ranker: Ranker | None = None
    ) -> None:
        self.repo = repo
        self.settings = settings
        self.ranker = ranker
        self._normalizer = PreferenceNormalizer(repo, settings.fuzzy_match_threshold)
        self._retriever = CandidateRetriever(repo, settings)

    def recommend(self, prefs: UserPreferences) -> RecommendationResponse:
        """Raises `UnknownValueError` for a location or cuisine that can't be matched."""
        started = time.perf_counter()
        normalized = self._normalizer.normalize(prefs)
        result = self._retriever.retrieve(normalized)

        outcome = None
        if result.candidates and self.ranker is not None:
            outcome = self._try_rank(result)  # no LLM call when nothing matched (FLT-12)

        if outcome is not None:
            picks, summary = outcome.recommendations, outcome.summary
            ranking, model = "llm", outcome.model
        else:
            picks = heuristic_recommendations(result, self.settings)
            summary = template_summary(result, picks)
            ranking, model = "heuristic", None

        latency_ms = round((time.perf_counter() - started) * 1000)
        response = RecommendationResponse(
            recommendations=picks,
            summary=summary,
            relaxations=result.relaxations,
            degraded=ranking == "heuristic" and bool(result.candidates),
            normalized_preferences=normalized,
            meta=ResponseMeta(
                candidates_considered=len(result.candidates),
                total_matches=result.total_matches,
                ranking=ranking,
                model=model,
                latency_ms=latency_ms,
            ),
        )
        self._log(response, outcome)
        return response

    def _try_rank(self, result: RetrievalResult) -> RankingOutcome | None:
        try:
            return self.ranker.rank(result)
        except Exception:  # a broken ranker must degrade, not fail the request
            log.exception("ranker failed; falling back to rule-based ranking")
            return None

    @staticmethod
    def _log(response: RecommendationResponse, outcome: RankingOutcome | None) -> None:
        prefs = response.normalized_preferences
        log.info(
            "recommendation",
            extra={
                "event": "recommendation",
                "location": prefs.location,
                "cuisines": prefs.cuisines,
                "budget": prefs.budget,
                "min_rating": prefs.min_rating,
                "extras": (prefs.extras or "")[:80] or None,  # truncated (arch §12.2)
                "candidates": response.meta.candidates_considered,
                "total_matches": response.meta.total_matches,
                "relaxations": response.relaxations,
                "ranking": response.meta.ranking,
                "degraded": response.degraded,
                "model": response.meta.model,
                "usage": outcome.usage if outcome else None,
                "latency_ms": response.meta.latency_ms,
            },
        )
