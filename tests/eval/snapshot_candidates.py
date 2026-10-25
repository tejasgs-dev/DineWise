"""Freeze each eval scenario's shortlist into tests/eval/candidates/<id>.json.

    python -m tests.eval.snapshot_candidates            # all scenarios
    python -m tests.eval.snapshot_candidates common-01   # just one

Runs the real normalizer and retriever on data/restaurants.parquet. Hand edits named in
a scenario's `edit` are applied deterministically and recorded in the snapshot. Re-run
after re-ingesting the data, then re-check the expectations (docs/eval.md §4.2).
"""

from __future__ import annotations

import json
import sys

from dinewise.config import Settings
from dinewise.domain.models import RetrievalResult, UserPreferences
from dinewise.retrieval.normalize import PreferenceNormalizer
from dinewise.retrieval.repository import RestaurantRepository
from dinewise.retrieval.retriever import CandidateRetriever
from tests.eval.common import CANDIDATES_DIR, load_scenarios


def apply_edit(result: RetrievalResult, edit: dict) -> tuple[RetrievalResult, dict]:
    """Returns the edited result and a record of what was changed."""
    record: dict = {}
    candidates = list(result.candidates)
    if edit.get("dominant"):
        # Make the last (weakest) candidate clearly best on every stated criterion.
        target = candidates[-1]
        prefs = result.preferences
        top_votes = max(c.votes for c in candidates)
        cost = {"low": 300, "medium": 700, "high": 1500}.get(
            prefs.budget or "", target.cost_for_two
        )
        cuisines = list(dict.fromkeys([*prefs.cuisines, *target.cuisines]))
        edited = target.model_copy(
            update={
                "rating": 4.9,
                "votes": top_votes + 2500,
                "is_new": False,
                "cuisines": cuisines,
                "cost_for_two": cost,
                "budget_band": prefs.budget or target.budget_band,
            }
        )
        candidates[-1] = edited
        record = {"dominant_id": edited.id, "changed": ["rating", "votes", "cuisines", "cost"]}
    if edit.get("inject_review"):
        target = candidates[-1]
        edited = target.model_copy(
            update={"review_snippets": [edit["inject_review"], *target.review_snippets][:3]}
        )
        candidates[-1] = edited
        record = {"planted_id": edited.id, "planted_text": edit["inject_review"]}
    return result.model_copy(update={"candidates": candidates}), record


def snapshot(case: dict, normalizer, retriever) -> dict:
    prefs = UserPreferences(**case["preferences"])
    result = retriever.retrieve(normalizer.normalize(prefs))
    result, edit_record = apply_edit(result, case.get("edit") or {})
    return {
        "id": case["id"],
        "result": result.model_dump(mode="json"),
        "edit": edit_record,
    }


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    settings = Settings()
    repo = RestaurantRepository.from_parquet(settings.data_path, settings)
    normalizer = PreferenceNormalizer(repo, settings.fuzzy_match_threshold)
    retriever = CandidateRetriever(repo, settings)

    CANDIDATES_DIR.mkdir(parents=True, exist_ok=True)
    for case in load_scenarios():
        if argv and case["id"] not in argv:
            continue
        snap = snapshot(case, normalizer, retriever)
        path = CANDIDATES_DIR / f"{case['id']}.json"
        path.write_text(json.dumps(snap, ensure_ascii=False, indent=1), encoding="utf-8")
        r = snap["result"]
        print(
            f"{case['id']:<20} {len(r['candidates']):>2} candidates, "
            f"{len(r['relaxations'])} relaxations {snap['edit'] or ''}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
