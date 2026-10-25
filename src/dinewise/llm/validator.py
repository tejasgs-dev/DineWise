"""Turn the LLM's raw ranking into safe, complete recommendations (arch §8.5).

The LLM's answer is never trusted as-is: only real candidates survive, each at most once,
the list is cut to the requested size, and gaps are filled from the heuristic ranking.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from dinewise.domain.budget import BudgetThresholds
from dinewise.domain.models import Recommendation, RetrievalResult
from dinewise.llm.prompts import pick_count
from dinewise.llm.schemas import LLMRankingResult
from dinewise.retrieval.explain import template_caveats, template_explanation, template_summary


@dataclass
class ValidationReport:
    recommendations: list[Recommendation]
    summary: str
    unknown_ids: list[str] = field(default_factory=list)
    duplicate_ids: list[str] = field(default_factory=list)
    backfilled: int = 0
    templated_explanations: int = 0

    @property
    def llm_picks(self) -> int:
        return sum(r.source == "llm" for r in self.recommendations)


class ResponseValidator:
    def __init__(self, thresholds: BudgetThresholds) -> None:
        self._thresholds = thresholds

    def validate(self, raw: LLMRankingResult, result: RetrievalResult) -> ValidationReport | None:
        """Returns None when no valid pick remains, so the caller falls back (LLM-12)."""
        prefs = result.preferences
        by_id = {c.id: c for c in result.candidates}
        wanted = pick_count(prefs.top_k, result.candidates)

        report = ValidationReport(recommendations=[], summary="")
        seen: set[str] = set()
        # List order is the ranking; the model's own `rank` numbers are ignored (LLM-13).
        for pick in raw.picks:
            if len(report.recommendations) == wanted:  # too many picks (LLM-10)
                break
            restaurant = by_id.get(pick.restaurant_id)  # exact match only (LLM-07, LLM-08)
            if restaurant is None:
                report.unknown_ids.append(pick.restaurant_id)
                continue
            if restaurant.id in seen:
                report.duplicate_ids.append(restaurant.id)
                continue
            seen.add(restaurant.id)

            explanation = (pick.explanation or "").strip()
            if not explanation or explanation.lower() == restaurant.name.lower():  # LLM-15
                explanation = template_explanation(restaurant, prefs)
                report.templated_explanations += 1
            caveats = (pick.caveats or "").strip() or None
            report.recommendations.append(
                Recommendation(
                    rank=len(report.recommendations) + 1,
                    restaurant=restaurant,
                    explanation=explanation,
                    caveats=caveats,
                    source="llm",
                )
            )

        if not report.recommendations:
            return None

        # Fill any gap from the heuristic order, marked as such (LLM-11).
        for restaurant in result.candidates:
            if len(report.recommendations) == wanted:
                break
            if restaurant.id in seen:
                continue
            seen.add(restaurant.id)
            report.backfilled += 1
            report.recommendations.append(
                Recommendation(
                    rank=len(report.recommendations) + 1,
                    restaurant=restaurant,
                    explanation=template_explanation(restaurant, prefs),
                    caveats=template_caveats(restaurant, prefs, self._thresholds),
                    source="heuristic",
                )
            )

        report.summary = (raw.summary or "").strip() or template_summary(
            result, report.recommendations
        )  # LLM-19
        return report
