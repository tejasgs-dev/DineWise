"""Heuristic pre-score and ordering (arch §7.3).

    score = w_rating  * norm(rating)
          + w_votes   * norm(log(1 + votes))
          + w_cuisine * share of requested cuisines served
          + w_tags    * share of extras tags matched

Normalization is min-max over the candidate set, so scores compare candidates within
one query, not across queries. This order is also the fallback ranking when the LLM
step fails.
"""

from __future__ import annotations

from collections.abc import Sequence

import numpy as np

from dinewise.config import ScoringWeights
from dinewise.retrieval.repository import RestaurantRepository


def minmax(values: np.ndarray) -> np.ndarray:
    """Scale to 0-1. If every value is equal, all get 1.0 rather than NaN (SCR-01, SCR-02)."""
    if values.size == 0:
        return values.astype(float)
    lo, hi = float(values.min()), float(values.max())
    if hi - lo < 1e-12:
        return np.ones_like(values, dtype=float)
    return (values - lo) / (hi - lo)


def fill_unrated(ratings: np.ndarray) -> np.ndarray:
    """Give unrated candidates the median of the rated ones (D-06, SCR-03).

    New places are then neither buried at the bottom nor boosted to the top. With no
    rated candidates at all, every candidate gets the same neutral value.
    """
    rated = ratings[~np.isnan(ratings)]
    fill = float(np.median(rated)) if rated.size else 0.0
    return np.where(np.isnan(ratings), fill, ratings)


def heuristic_scores(
    repo: RestaurantRepository,
    positions: np.ndarray,
    cuisines: Sequence[str],
    extras_tags: Sequence[str],
    weights: ScoringWeights,
) -> np.ndarray:
    """Score the rows at `positions`; returns one float per position."""
    if positions.size == 0:
        return np.zeros(0)

    rating_part = minmax(fill_unrated(repo.ratings[positions]))
    votes_part = minmax(np.log1p(repo.votes[positions].astype(float)))  # SCR-04, SCR-05

    if cuisines:
        cuisine_part = repo.cuisine_match_counts(cuisines)[positions] / len(set(cuisines))
    else:
        cuisine_part = np.zeros(positions.size)  # SCR-07

    if extras_tags:
        tag_part = repo.tag_match_counts(extras_tags)[positions] / len(set(extras_tags))
    else:
        tag_part = np.zeros(positions.size)  # SCR-08

    return (
        weights.rating * rating_part
        + weights.votes * votes_part
        + weights.cuisine * cuisine_part
        + weights.tags * tag_part
    )


def rank_order(repo: RestaurantRepository, positions: np.ndarray, scores: np.ndarray) -> np.ndarray:
    """Indices into `positions`, best first: score, then votes, then ID (SCR-06)."""
    # np.lexsort sorts by the last key first, ascending; negate to get descending.
    ids = repo.ids[positions].astype(str)
    return np.lexsort((ids, -repo.votes[positions], -np.round(scores, 9)))


def select_shortlist(
    chain_keys: np.ndarray, ordered_positions: np.ndarray, size: int, max_per_chain: int
) -> list[int]:
    """Take the best `size` positions, with at most `max_per_chain` branches per chain.

    Branches beyond the cap are only used to fill the shortlist if there aren't enough
    other candidates (D-07, SCR-12).
    """
    picked: list[int] = []
    overflow: list[int] = []
    per_chain: dict[str, int] = {}
    for pos in ordered_positions:
        key = chain_keys[pos]
        if per_chain.get(key, 0) < max_per_chain:
            per_chain[key] = per_chain.get(key, 0) + 1
            picked.append(int(pos))
            if len(picked) == size:
                return picked
        else:
            overflow.append(int(pos))
    if len(picked) < size:
        # Keep the overall order: merge back the overflow by original rank.
        rank = {int(p): i for i, p in enumerate(ordered_positions)}
        picked = sorted(picked + overflow[: size - len(picked)], key=rank.__getitem__)
    return picked
