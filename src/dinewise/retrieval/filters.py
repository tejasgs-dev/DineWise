"""Hard filters (arch §7.1). Each returns a boolean mask over the repository's rows."""

from __future__ import annotations

from collections.abc import Collection

import numpy as np

from dinewise.domain.budget import BANDS, BudgetBand
from dinewise.retrieval.repository import RestaurantRepository

# Ratings are stored to one decimal; this keeps 4.0 >= 4.0 safe from float noise.
_RATING_EPSILON = 1e-9


def location_mask(repo: RestaurantRepository, localities: Collection[str] | None) -> np.ndarray:
    """Rows in any of `localities`; None means the whole city."""
    if localities is None:
        return np.ones(len(repo), dtype=bool)
    return repo.locality_mask(localities)


def cuisine_mask(repo: RestaurantRepository, cuisines: Collection[str]) -> np.ndarray:
    """Rows serving any of `cuisines` (OR semantics, INP-15); empty means any cuisine."""
    if not cuisines:
        return np.ones(len(repo), dtype=bool)
    return repo.cuisine_mask(cuisines)


def rating_mask(repo: RestaurantRepository, min_rating: float) -> np.ndarray:
    """Rows rated at least `min_rating`.

    With `min_rating` 0 there is no filter and unrated places are included (INP-19);
    otherwise unrated places are excluded (FLT-14).
    """
    if min_rating <= 0:
        return np.ones(len(repo), dtype=bool)
    with np.errstate(invalid="ignore"):
        return repo.ratings >= min_rating - _RATING_EPSILON  # NaN compares False


def budget_mask(repo: RestaurantRepository, bands: Collection[BudgetBand] | None) -> np.ndarray:
    """Rows in any of `bands`; None means any budget.

    Restaurants with no known cost are excluded whenever a budget is set (D-05).
    """
    if not bands:
        return np.ones(len(repo), dtype=bool)
    codes = [BANDS.index(b) for b in bands]
    return np.isin(repo.band_codes, codes)
