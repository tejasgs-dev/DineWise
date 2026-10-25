"""Phase 2 exit criterion: retrieval under 50 ms per query on the full dataset.

Runs only when data/restaurants.parquet has been built (it isn't in CI).
"""

import statistics
import time
from pathlib import Path

import pytest

from dinewise.config import Settings
from dinewise.domain.models import UserPreferences
from dinewise.retrieval.normalize import PreferenceNormalizer
from dinewise.retrieval.repository import RestaurantRepository
from dinewise.retrieval.retriever import CandidateRetriever

DATA = Path(__file__).resolve().parents[2] / "data" / "restaurants.parquet"

QUERIES = [
    {"location": "Indiranagar", "cuisines": ["Italian"], "budget": "medium", "min_rating": 4},
    {"location": "Hebbal", "cuisines": ["Japanese"], "budget": "high", "min_rating": 4.5},
    {"location": "Bangalore", "min_rating": 4, "extras": "family buffet"},
    {"location": "koramangla", "cuisines": ["chinese"], "budget": "low", "extras": "quick"},
    {"location": "Kengeri", "cuisines": ["Thai"], "min_rating": 4.8},
    {"location": "Whitefield", "cuisines": ["Burger", "Pizza"]},
]


@pytest.mark.skipif(not DATA.exists(), reason="data/restaurants.parquet not built")
def test_retrieval_under_50ms_on_full_dataset():
    settings = Settings(_env_file=None)
    repo = RestaurantRepository.from_parquet(DATA, settings)
    normalizer = PreferenceNormalizer(repo, settings.fuzzy_match_threshold)
    retriever = CandidateRetriever(repo, settings)

    timings = []
    for _ in range(5):
        for query in QUERIES:
            started = time.perf_counter()
            retriever.retrieve(normalizer.normalize(UserPreferences(**query)))
            timings.append((time.perf_counter() - started) * 1000)

    assert statistics.median(timings) < 50, f"median {statistics.median(timings):.1f} ms"
    assert max(timings) < 50, f"slowest {max(timings):.1f} ms"
