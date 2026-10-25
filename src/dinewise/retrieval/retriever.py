"""Candidate retrieval: filter -> relax -> score -> shortlist (arch §7)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from dinewise.config import Settings
from dinewise.domain.budget import BudgetBand, band_label
from dinewise.domain.formatting import join_words
from dinewise.domain.models import NormalizedPreferences, RetrievalResult
from dinewise.retrieval import filters
from dinewise.retrieval.repository import RestaurantRepository
from dinewise.retrieval.scoring import heuristic_scores, rank_order, select_shortlist

RATING_RELAX_STEP = 0.5
RATING_RELAX_FLOOR = 3.0
NEARBY_NAMES_SHOWN = 4

# Which band to add when widening a budget (D-03): one step up, or down from "high".
_WIDEN_TO: dict[BudgetBand, BudgetBand] = {"low": "medium", "medium": "high", "high": "medium"}


@dataclass
class _Query:
    """The current, possibly relaxed, hard constraints."""

    localities: list[str] | None  # None = whole city
    bands: set[BudgetBand] | None
    min_rating: float
    cuisines_required: bool


class CandidateRetriever:
    def __init__(self, repo: RestaurantRepository, settings: Settings) -> None:
        self._repo = repo
        self._settings = settings

    def retrieve(self, prefs: NormalizedPreferences) -> RetrievalResult:
        query = _Query(
            localities=list(prefs.localities) if prefs.location_kind == "locality" else None,
            bands={prefs.budget} if prefs.budget else None,
            min_rating=prefs.min_rating,
            cuisines_required=bool(prefs.cuisines),
        )
        relaxations: list[str] = []
        mask = self._mask(query, prefs)

        # Relax one constraint at a time, in a fixed order, until there are enough (FLT-19).
        steps = (
            self._relax_budget,
            self._relax_rating,
            self._relax_cuisine,
            self._relax_to_nearby,
            self._relax_to_city,
        )
        for step in steps:
            if mask.sum() >= self._settings.min_candidates:
                break
            message = step(query, prefs)
            if message:  # a step that can't relax anything is skipped silently (FLT-06/07)
                relaxations.append(message)
                mask = self._mask(query, prefs)

        positions = np.flatnonzero(mask)
        scores = heuristic_scores(
            self._repo, positions, prefs.cuisines, prefs.extras_tags, self._settings.scoring_weights
        )
        order = rank_order(self._repo, positions, scores)
        shortlist = select_shortlist(
            self._repo.chain_keys,
            positions[order],
            self._settings.shortlist_size,
            self._settings.max_branches_per_chain,
        )
        score_by_pos = dict(zip(positions.tolist(), scores.tolist(), strict=True))
        candidates = self._repo.restaurants_at(shortlist)
        return RetrievalResult(
            preferences=prefs,
            candidates=candidates,
            scores={
                r.id: round(score_by_pos[p], 6) for r, p in zip(candidates, shortlist, strict=True)
            },
            relaxations=relaxations,
            total_matches=int(positions.size),
        )

    # --- Filtering --------------------------------------------------------------------

    def _mask(self, query: _Query, prefs: NormalizedPreferences) -> np.ndarray:
        repo = self._repo
        mask = filters.location_mask(repo, query.localities)
        if query.cuisines_required:
            mask &= filters.cuisine_mask(repo, prefs.cuisines)
        mask &= filters.rating_mask(repo, query.min_rating)
        mask &= filters.budget_mask(repo, query.bands)
        return mask

    # --- Relaxation steps (arch §7.2). Each returns a message, or None if it can't relax.

    def _relax_budget(self, query: _Query, prefs: NormalizedPreferences) -> str | None:
        if not query.bands or not prefs.budget:
            return None
        added = _WIDEN_TO[prefs.budget]
        if added in query.bands:
            return None
        query.bands.add(added)
        return f"Budget widened to include {added} ({band_label(added, self._settings)})"

    def _relax_rating(self, query: _Query, prefs: NormalizedPreferences) -> str | None:
        if query.min_rating <= RATING_RELAX_FLOOR:
            return None
        before = query.min_rating
        query.min_rating = max(RATING_RELAX_FLOOR, before - RATING_RELAX_STEP)  # FLT-08
        return f"Minimum rating lowered from {before:g} to {query.min_rating:g}"

    def _relax_cuisine(self, query: _Query, prefs: NormalizedPreferences) -> str | None:
        if not query.cuisines_required:
            return None
        query.cuisines_required = False  # still a soft signal in scoring (FLT-10)
        cuisines = join_words(prefs.cuisines, "or")
        return f"Included places that don't serve {cuisines} (those that do still rank first)"

    def _relax_to_nearby(self, query: _Query, prefs: NormalizedPreferences) -> str | None:
        if query.localities is None:
            return None
        nearby = self._repo.nearby_localities(query.localities)
        if not nearby:
            return None
        query.localities = sorted(set(query.localities) | set(nearby))
        shown = nearby[:NEARBY_NAMES_SHOWN]
        more = len(nearby) - len(shown)
        names = ", ".join(shown) + (f" and {more} more" if more else "")
        return f"Search widened from {prefs.location} to nearby areas: {names}"

    def _relax_to_city(self, query: _Query, prefs: NormalizedPreferences) -> str | None:
        if query.localities is None:
            return None
        query.localities = None
        return f"Search widened to all of {prefs.city}"
