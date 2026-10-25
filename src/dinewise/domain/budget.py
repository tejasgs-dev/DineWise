"""Budget bands: mapping between cost for two (INR) and low / medium / high (arch §5.4)."""

from __future__ import annotations

from typing import Literal, Protocol

from dinewise.domain.formatting import format_indian, format_inr

BudgetBand = Literal["low", "medium", "high"]
BANDS: tuple[BudgetBand, ...] = ("low", "medium", "high")


class BudgetThresholds(Protocol):
    budget_low_max: int
    budget_medium_max: int


def cost_to_band(cost: int | None, thresholds: BudgetThresholds) -> BudgetBand | None:
    """Return the band for a cost for two, or None if the cost is unknown or invalid.

    Edges are inclusive on the upper side: cost == low_max is "low".
    """
    if cost is None or cost <= 0:
        return None
    if cost <= thresholds.budget_low_max:
        return "low"
    if cost <= thresholds.budget_medium_max:
        return "medium"
    return "high"


def band_range(band: BudgetBand, thresholds: BudgetThresholds) -> tuple[int, int | None]:
    """Inclusive (min, max) cost for a band; max is None for the open-ended "high" band."""
    low, medium = thresholds.budget_low_max, thresholds.budget_medium_max
    if band == "low":
        return (1, low)
    if band == "medium":
        return (low + 1, medium)
    if band == "high":
        return (medium + 1, None)
    raise ValueError(f"unknown budget band: {band!r}")


def band_label(band: BudgetBand, thresholds: BudgetThresholds) -> str:
    """Human-readable range, e.g. "₹401–1,000 for two" or "above ₹1,000 for two"."""
    lo, hi = band_range(band, thresholds)
    if band == "low":
        return f"up to {format_inr(hi)} for two"
    if hi is None:
        return f"above {format_inr(lo - 1)} for two"
    return f"{format_inr(lo)}–{format_indian(hi)} for two"
