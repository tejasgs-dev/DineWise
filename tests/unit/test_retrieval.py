"""Repository, filters, relaxation, scoring, shortlist, and template explanations."""

import numpy as np
import pytest

from dinewise.config import ScoringWeights, Settings
from dinewise.domain.models import NormalizedPreferences
from dinewise.retrieval import filters
from dinewise.retrieval.explain import (
    heuristic_recommendations,
    template_caveats,
    template_explanation,
)
from dinewise.retrieval.repository import DataNotLoadedError, RestaurantRepository
from dinewise.retrieval.retriever import CandidateRetriever
from dinewise.retrieval.scoring import (
    fill_unrated,
    heuristic_scores,
    minmax,
    rank_order,
    select_shortlist,
)

from .conftest import RESTAURANTS, make_df


def prefs(**kwargs) -> NormalizedPreferences:
    defaults = {
        "location": "Indiranagar",
        "location_kind": "locality",
        "city": "Bangalore",
        "localities": ["Indiranagar"],
    }
    if kwargs.get("location_kind") == "city":
        defaults.update(location="Bangalore", localities=[])
    return NormalizedPreferences(**{**defaults, **kwargs})


def names(result) -> list[str]:
    return [r.name for r in result.candidates]


@pytest.fixture
def retriever(repo, settings):
    return CandidateRetriever(repo, settings)


# =============================================================================
# Repository
# =============================================================================


def test_known_values_are_sorted(repo):
    assert repo.known_localities()[:3] == ["Domlur", "ITPL Main Road, Whitefield", "Indiranagar"]
    assert "Italian" in repo.known_cuisines()
    assert repo.known_cuisines() == sorted(repo.known_cuisines())
    assert repo.cities() == ["Bangalore"]


def test_get_by_ids_keeps_order_and_skips_unknown(repo):
    ids = list(repo.ids[[3, 0]])
    assert [r.name for r in repo.get_by_ids([ids[0], "r_missing", ids[1]])] == ["Toit", "Bologna"]


def test_nearby_uses_shared_main_zones(repo):  # D-04
    assert repo.nearby_localities(["Indiranagar"]) == ["Domlur"]
    assert repo.nearby_localities(["Kengeri"]) == []  # too few restaurants for a profile


def test_bands_follow_current_thresholds():  # D-10
    df = make_df()  # bands stored with the 400/1000 defaults
    stored = dict(zip(df["name"], df["budget_band"], strict=True))
    assert stored["Kor Dhaba"] == "medium" and stored["Chinita"] == "high"

    repo = RestaurantRepository(
        df, Settings(_env_file=None, budget_low_max=500, budget_medium_max=1500)
    )
    by_name = {r.name: r for r in repo.get_by_ids(repo.ids)}
    assert by_name["Kor Dhaba"].budget_band == "low"  # ₹450
    assert by_name["Chinita"].budget_band == "medium"  # ₹1,100
    assert filters.budget_mask(repo, {"low"})[list(repo.ids).index(by_name["Kor Dhaba"].id)]


def test_from_parquet_round_trip(tmp_path, settings):
    path = tmp_path / "r.parquet"
    make_df().to_parquet(path)
    repo = RestaurantRepository.from_parquet(path, settings)
    assert len(repo) == len(RESTAURANTS)


def test_from_parquet_missing_file(tmp_path, settings):  # API-01
    with pytest.raises(DataNotLoadedError, match="ingestion"):
        RestaurantRepository.from_parquet(tmp_path / "nope.parquet", settings)


def test_from_parquet_corrupt_file(tmp_path, settings):  # API-02
    path = tmp_path / "bad.parquet"
    path.write_bytes(b"not parquet")
    with pytest.raises(DataNotLoadedError, match="could not read"):
        RestaurantRepository.from_parquet(path, settings)


def test_missing_columns_rejected(settings):
    with pytest.raises(DataNotLoadedError, match="missing columns"):
        RestaurantRepository(make_df().drop(columns=["tags"]), settings)


# =============================================================================
# Filters
# =============================================================================


def selected(repo, mask) -> set[str]:
    return {repo.ids[i] for i in np.flatnonzero(mask)}


def test_location_filter(repo):
    mask = filters.location_mask(repo, ["Domlur"])
    assert mask.sum() == 5
    assert filters.location_mask(repo, None).all()


def test_cuisine_filter_is_or(repo):  # INP-15
    mask = filters.cuisine_mask(repo, ["Thai", "Japanese"])
    assert {repo.get_by_ids([i])[0].name for i in selected(repo, mask)} == {
        "Domlur Thai",
        "Domlur Sushi",
        "Mystery Cost",
        "Cheap Thai",
    }
    assert filters.cuisine_mask(repo, []).all()


def test_rating_filter(repo):
    assert filters.rating_mask(repo, 0).all()  # INP-19: unrated included at 0
    mask = filters.rating_mask(repo, 4.0)
    rated = repo.ratings[mask]
    assert (rated >= 4.0).all() and not np.isnan(rated).any()  # FLT-14
    assert filters.rating_mask(repo, 4.7).sum() == 1  # inclusive: only Toit (4.7)


def test_budget_filter_excludes_unknown_cost(repo):  # D-05, FLT-15
    mask = filters.budget_mask(repo, {"low", "medium", "high"})
    assert mask.sum() == len(repo) - 1
    assert filters.budget_mask(repo, None).all()


def test_filters_combined(repo):
    # Indiranagar + Italian + medium (₹401–1,000) + 4.0+: Pasta Street (3.9), Toit (high),
    # and New Place (unrated) are filtered out.
    s = Settings(_env_file=None, min_candidates=2)
    result = CandidateRetriever(repo, s).retrieve(
        prefs(cuisines=["Italian"], budget="medium", min_rating=4.0)
    )
    assert result.relaxations == []
    assert names(result) == ["Bologna", "Onesta"]  # higher rating outweighs Onesta's votes


# =============================================================================
# Relaxation (arch §7.2)
# =============================================================================


def test_no_relaxation_when_enough(retriever):  # FLT-01
    result = retriever.retrieve(prefs())
    assert result.relaxations == [] and result.total_matches == 10


def test_exactly_min_candidates_is_enough(repo, settings):  # FLT-02
    result = CandidateRetriever(repo, settings).retrieve(
        prefs(localities=["Domlur"], location="Domlur")
    )
    assert result.total_matches == 5 and result.relaxations == []


def test_budget_relaxes_first_and_stops_early(repo):  # FLT-03
    s = Settings(_env_file=None, min_candidates=3)
    result = CandidateRetriever(repo, s).retrieve(prefs(budget="high"))
    # high in Indiranagar: Toit, Chinita -> widened to include medium.
    assert result.relaxations == ["Budget widened to include medium (₹401–1,000 for two)"]
    assert result.total_matches >= 3


def test_relaxation_order(retriever):  # FLT-19
    result = retriever.retrieve(prefs(cuisines=["Japanese"], budget="low", min_rating=4.5))
    steps = [m.split()[0] for m in result.relaxations]
    assert steps == ["Budget", "Minimum", "Included", "Search", "Search"]
    assert result.relaxations[1] == "Minimum rating lowered from 4.5 to 4"
    assert result.relaxations[3] == "Search widened from Indiranagar to nearby areas: Domlur"
    assert result.relaxations[4] == "Search widened to all of Bangalore"


def test_medium_budget_widens_up(retriever):  # D-03, FLT-05
    result = retriever.retrieve(prefs(budget="medium", cuisines=["Mexican"], min_rating=3))
    assert result.relaxations[0].startswith("Budget widened to include high")


def test_rating_relaxation_clamps_at_floor(repo):  # FLT-08
    s = Settings(_env_file=None, min_candidates=30, shortlist_size=100)
    result = CandidateRetriever(repo, s).retrieve(prefs(min_rating=3.2))
    assert "Minimum rating lowered from 3.2 to 3" in result.relaxations


def test_rating_at_or_below_floor_is_not_relaxed(repo):  # FLT-07
    s = Settings(_env_file=None, min_candidates=30, shortlist_size=100)
    result = CandidateRetriever(repo, s).retrieve(prefs(min_rating=3.0))
    assert not any(m.startswith("Minimum rating") for m in result.relaxations)


def test_steps_with_nothing_to_relax_are_skipped(repo):  # FLT-06
    s = Settings(_env_file=None, min_candidates=30, shortlist_size=100)
    result = CandidateRetriever(repo, s).retrieve(prefs())
    assert [m.split()[0] for m in result.relaxations] == ["Search", "Search"]
    assert result.relaxations[-1] == "Search widened to all of Bangalore"


def test_cuisine_stays_a_soft_signal_after_relaxing(repo):  # FLT-10
    s = Settings(_env_file=None, min_candidates=6)
    result = CandidateRetriever(repo, s).retrieve(
        prefs(location="Domlur", localities=["Domlur"], cuisines=["Thai"])
    )
    assert any(m.startswith("Included places") for m in result.relaxations)
    order = names(result)
    # Chinita has a higher rating and more votes, but Domlur Thai serves the cuisine asked for.
    assert order.index("Domlur Thai") < order.index("Chinita")


def test_city_wide_search_skips_location_steps(repo):
    s = Settings(_env_file=None, min_candidates=100, shortlist_size=100)
    result = CandidateRetriever(repo, s).retrieve(prefs(location_kind="city"))
    assert result.relaxations == []
    assert result.total_matches == len(RESTAURANTS)


def test_nothing_matches_even_after_full_relaxation(settings):  # FLT-12
    # Only a ₹200 place exists; a "high" budget widens to medium at most, never to low.
    df = make_df(rows=[r for r in RESTAURANTS if r[0] == "Dosa Corner"])
    result = CandidateRetriever(RestaurantRepository(df, settings), settings).retrieve(
        prefs(budget="high")
    )
    assert result.candidates == [] and result.total_matches == 0
    assert result.relaxations == [
        "Budget widened to include medium (₹401–1,000 for two)",
        "Search widened to all of Bangalore",
    ]


def test_empty_result_when_no_rows(settings):  # FLT-12
    df = make_df(rows=[])
    result = CandidateRetriever(RestaurantRepository(df, settings), settings).retrieve(prefs())
    assert result.candidates == [] and result.total_matches == 0
    assert result.relaxations == ["Search widened to all of Bangalore"]


def test_fewer_candidates_than_top_k_are_returned_as_is(settings):  # FLT-13
    df = make_df(rows=[r for r in RESTAURANTS if r[0] in {"Bologna", "Onesta", "Pasta Street"}])
    result = CandidateRetriever(RestaurantRepository(df, settings), settings).retrieve(
        prefs(cuisines=["Italian"], top_k=5)
    )
    assert len(result.candidates) == 3
    assert len(heuristic_recommendations(result, settings)) == 3  # no padding


def test_small_neighbourhood_skips_nearby_and_goes_city_wide(repo, settings):
    result = CandidateRetriever(repo, settings).retrieve(
        prefs(location="Kengeri", localities=["Kengeri"])
    )
    # Kengeri has 1 restaurant, too few for a zone profile, so there is no "nearby" step.
    assert result.relaxations == ["Search widened to all of Bangalore"]


# =============================================================================
# Scoring and shortlist
# =============================================================================


WEIGHTS = ScoringWeights(rating=0.45, votes=0.25, cuisine=0.15, tags=0.15)


def test_minmax_handles_equal_and_empty():  # SCR-01, SCR-02
    assert minmax(np.array([3.0, 3.0])).tolist() == [1.0, 1.0]
    assert minmax(np.array([5.0])).tolist() == [1.0]
    assert minmax(np.array([])).size == 0
    assert minmax(np.array([1.0, 2.0, 3.0])).tolist() == [0.0, 0.5, 1.0]


def test_unrated_get_median():  # D-06, SCR-03
    assert fill_unrated(np.array([3.0, np.nan, 5.0, 4.0])).tolist() == [3.0, 4.0, 5.0, 4.0]
    assert not np.isnan(fill_unrated(np.array([np.nan, np.nan]))).any()


def _two_row_repo(settings, a, b):
    base = [r for r in RESTAURANTS if r[0] == "Bologna"][0]
    rows = [
        ("A", *base[1:4], a[0], a[1], *base[6:]),
        ("B", *base[1:4], b[0], b[1], *base[6:]),
    ]
    return RestaurantRepository(make_df(rows=rows), settings)


def test_score_rises_with_rating(settings):
    repo = _two_row_repo(settings, (4.5, 100), (3.5, 100))
    s = heuristic_scores(repo, np.array([0, 1]), [], [], WEIGHTS)
    assert s[0] > s[1]


def test_score_rises_with_votes(settings):  # SCR-04
    repo = _two_row_repo(settings, (4.0, 5000), (4.0, 0))
    s = heuristic_scores(repo, np.array([0, 1]), [], [], WEIGHTS)
    assert s[0] > s[1]


def test_score_rewards_cuisine_and_tag_matches(repo):
    positions = np.arange(len(repo))
    plain = heuristic_scores(repo, positions, [], [], WEIGHTS)
    boosted = heuristic_scores(repo, positions, ["Italian"], ["family_friendly"], WEIGHTS)
    bologna = 0  # Italian and family_friendly: gets the full cuisine and tag weights
    assert boosted[bologna] - plain[bologna] == pytest.approx(0.30)
    toit = 3  # Italian but a bar: only the cuisine weight
    assert boosted[toit] - plain[toit] == pytest.approx(0.15)


def test_ties_break_by_votes_then_id(settings):  # SCR-06
    repo = _two_row_repo(settings, (4.0, 10), (4.0, 10))
    positions = np.array([0, 1])
    order = rank_order(repo, positions, np.array([0.5, 0.5]))
    assert list(repo.ids[positions[order]]) == sorted(repo.ids)


def test_chain_cap_with_backfill(repo):  # D-07, SCR-12
    whitefield = np.flatnonzero(repo.locality_mask(["Whitefield"]))
    scores = heuristic_scores(repo, whitefield, ["Burger"], [], WEIGHTS)
    ordered = whitefield[rank_order(repo, whitefield, scores)]
    capped = select_shortlist(repo.chain_keys, ordered, size=4, max_per_chain=2)
    assert [repo.chain_keys[p] for p in capped].count("kfc") == 2
    # Only 5 Whitefield places exist, so the third KFC is used to fill the shortlist,
    # in its original rank order.
    full = select_shortlist(repo.chain_keys, ordered, size=5, max_per_chain=2)
    assert len(full) == 5 and [repo.chain_keys[p] for p in full].count("kfc") == 3
    assert full == sorted(full, key=list(ordered).index)


def test_shortlist_size_respected(repo):  # SCR-10
    s = Settings(_env_file=None, shortlist_size=7, min_candidates=5)
    result = CandidateRetriever(repo, s).retrieve(prefs(location_kind="city"))
    assert len(result.candidates) == 7 and set(result.scores) == {r.id for r in result.candidates}
    ordered = [result.scores[r.id] for r in result.candidates]
    assert ordered == sorted(ordered, reverse=True)


# =============================================================================
# Template explanations
# =============================================================================


def test_template_explanation(retriever):
    result = retriever.retrieve(prefs(cuisines=["Italian"], extras_tags=["family_friendly"]))
    bologna = next(r for r in result.candidates if r.name == "Bologna")
    text = template_explanation(bologna, result.preferences)
    assert (
        text
        == "Rated 4.5 (769 votes); serves Italian; ₹1,000 for two; family-friendly sit-down dining."
    )


def test_template_explanation_unrated_and_unknown_cost(repo):
    p = prefs()
    new_place = next(r for r in repo.get_by_ids(repo.ids) if r.name == "New Place")
    assert template_explanation(new_place, p).startswith("New, not yet rated")
    mystery = next(r for r in repo.get_by_ids(repo.ids) if r.name == "Mystery Cost")
    assert "cost not listed" in template_explanation(mystery, p)


def test_template_caveats_after_relaxation(repo, settings):
    toit = next(r for r in repo.get_by_ids(repo.ids) if r.name == "Toit")
    p = prefs(
        budget="low", min_rating=4.8, cuisines=["Thai"], location="Domlur", localities=["Domlur"]
    )
    caveats = template_caveats(toit, p, settings)
    assert "Above your low budget" in caveats
    assert "below your minimum of 4.8" in caveats
    assert "Doesn't serve Thai" in caveats
    assert "outside Domlur" in caveats
    assert template_caveats(toit, prefs(), settings) is None


def test_heuristic_recommendations(retriever, settings):
    result = retriever.retrieve(prefs(cuisines=["Italian"], top_k=3))
    recs = heuristic_recommendations(result, settings)
    assert [r.rank for r in recs] == [1, 2, 3]
    assert all(r.source == "heuristic" and r.explanation for r in recs)
    assert [r.restaurant.id for r in recs] == [c.id for c in result.candidates[:3]]
