"""Template explanations for heuristic (non-LLM) results.

These are used when the LLM is unavailable (arch §8.5) and by the dev CLI. They state
only facts from the record, e.g. "Rated 4.5 (3,200 votes); serves Italian; ₹1,200 for two."
"""

from __future__ import annotations

from dinewise.domain.budget import BANDS, BudgetThresholds, band_label
from dinewise.domain.formatting import format_indian, format_inr, join_words
from dinewise.domain.models import (
    NormalizedPreferences,
    Recommendation,
    Restaurant,
    RetrievalResult,
)

TAG_LABELS: dict[str, str] = {
    "family_friendly": "family-friendly sit-down dining",
    "quick_service": "quick service",
    "table_booking": "takes table bookings",
    "delivery": "delivers",
    "bar": "has a bar",
    "buffet": "offers a buffet",
    "cafe": "café",
    "desserts": "desserts",
    "fine_dining": "fine dining",
}


def template_explanation(restaurant: Restaurant, prefs: NormalizedPreferences) -> str:
    r = restaurant
    parts: list[str] = []

    if r.rating is not None:
        parts.append(f"Rated {r.rating:.1f} ({format_indian(r.votes)} votes)")
    elif r.is_new:
        parts.append("New, not yet rated")
    else:
        parts.append("Not yet rated")

    wanted = {c.lower() for c in prefs.cuisines}
    matched = [c for c in r.cuisines if c.lower() in wanted]
    shown = matched or r.cuisines[:2]
    if shown:
        parts.append(f"serves {join_words(shown)}")

    parts.append(f"{format_inr(r.cost_for_two)} for two" if r.cost_for_two else "cost not listed")

    if prefs.localities and r.locality not in prefs.localities:
        parts.append(f"in {r.locality}")

    tag_hits = [TAG_LABELS[t] for t in prefs.extras_tags if t in r.tags and t in TAG_LABELS]
    if tag_hits:
        parts.append(join_words(tag_hits))

    return "; ".join(parts) + "."


def template_caveats(
    restaurant: Restaurant, prefs: NormalizedPreferences, thresholds: BudgetThresholds
) -> str | None:
    """Where a pick misses a stated preference (only possible after relaxation)."""
    r = restaurant
    notes: list[str] = []
    if prefs.budget and r.budget_band and r.budget_band != prefs.budget:
        direction = "Above" if BANDS.index(r.budget_band) > BANDS.index(prefs.budget) else "Below"
        notes.append(
            f"{direction} your {prefs.budget} budget ({band_label(prefs.budget, thresholds)})"
        )
    if prefs.min_rating > 0 and (r.rating is None or r.rating < prefs.min_rating):
        rating = "not yet rated" if r.rating is None else f"rated {r.rating:.1f}"
        notes.append(f"{rating[0].upper()}{rating[1:]}, below your minimum of {prefs.min_rating:g}")
    wanted = {c.lower() for c in prefs.cuisines}
    if wanted and not wanted & {c.lower() for c in r.cuisines}:
        notes.append(f"Doesn't serve {join_words(prefs.cuisines, 'or')}")
    if prefs.localities and r.locality not in prefs.localities:
        notes.append(f"In {r.locality}, outside {prefs.location}")
    return "; ".join(notes) + "." if notes else None


def heuristic_recommendations(
    result: RetrievalResult, thresholds: BudgetThresholds, top_k: int | None = None
) -> list[Recommendation]:
    """Top-k picks in pre-score order, with template explanations (source="heuristic")."""
    prefs = result.preferences
    k = top_k or prefs.top_k
    return [
        Recommendation(
            rank=i,
            restaurant=r,
            explanation=template_explanation(r, prefs),
            caveats=template_caveats(r, prefs, thresholds),
            source="heuristic",
        )
        for i, r in enumerate(result.candidates[:k], start=1)
    ]


def search_description(prefs: NormalizedPreferences) -> str:
    """ "in Indiranagar for Italian, medium budget, rated 4+" """
    text = f"in {prefs.location}"
    if prefs.cuisines:
        text += f" for {join_words(prefs.cuisines, 'or')}"
    details = []
    if prefs.budget:
        details.append(f"{prefs.budget} budget")
    if prefs.min_rating:
        details.append(f"rated {prefs.min_rating:g}+")
    if details:
        text += ", " + ", ".join(details)
    return text


def template_summary(result: RetrievalResult, picks: list[Recommendation]) -> str:
    """A short, factual summary for rule-based results (the LLM writes its own)."""
    where = search_description(result.preferences)
    if not picks:
        return (
            f"No restaurants matched {where}, even after relaxing the filters. "
            "Try a lower minimum rating, a different budget, or a nearby area."
        )
    names = join_words([p.restaurant.name for p in picks[:3]])
    text = f"Top picks {where}: {names}, ranked by rating and popularity."
    if result.relaxations:
        text += " Some filters were relaxed to find enough matches."
    return text
