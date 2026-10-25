"""Shared fixtures: a small hand-written restaurant table in the cleaned Parquet schema."""

from __future__ import annotations

import pandas as pd
import pytest

from dinewise.config import Settings
from dinewise.domain.budget import cost_to_band
from dinewise.ingestion.clean import OUTPUT_COLUMNS, make_id
from dinewise.retrieval.repository import RestaurantRepository

IND = ["Indiranagar", "Old Airport Road"]
DOM = ["Old Airport Road"]
KOR = ["Koramangala 5th Block"]
WHF = ["Whitefield"]

# (name, locality, cuisines, cost, rating, votes, tags, zones)
RESTAURANTS = [
    # Indiranagar: 8 restaurants
    ("Bologna", "Indiranagar", ["Italian"], 1000, 4.5, 769, ["family_friendly"], IND),
    (
        "Onesta",
        "Indiranagar",
        ["Pizza", "Italian", "Cafe"],
        600,
        4.3,
        4709,
        ["cafe", "delivery"],
        IND,
    ),
    ("Pasta Street", "Indiranagar", ["Italian"], 900, 3.9, 410, ["family_friendly"], IND),
    ("Toit", "Indiranagar", ["Italian", "American"], 1500, 4.7, 14956, ["bar"], IND),
    ("Truffles", "Indiranagar", ["American", "Burger"], 900, 4.6, 3900, ["quick_service"], IND),
    ("Chinita", "Indiranagar", ["Mexican"], 1100, 4.2, 1200, ["family_friendly"], IND),
    ("Dosa Corner", "Indiranagar", ["South Indian"], 200, 3.8, 90, ["quick_service"], IND),
    ("New Place", "Indiranagar", ["Italian"], 700, None, 0, [], IND),
    # Domlur: 5 restaurants, shares the Old Airport Road zone with Indiranagar
    ("Domlur Italian", "Domlur", ["Italian"], 800, 4.4, 300, ["family_friendly"], DOM),
    ("Domlur Thai", "Domlur", ["Thai"], 1200, 4.1, 250, ["table_booking"], DOM),
    ("Domlur Biryani", "Domlur", ["Biryani", "North Indian"], 350, 3.9, 800, ["delivery"], DOM),
    ("Domlur Cafe", "Domlur", ["Cafe"], 400, 3.6, 120, ["cafe"], DOM),
    ("Domlur Sushi", "Domlur", ["Japanese", "Sushi"], 1800, 4.0, 60, ["fine_dining"], DOM),
    # Koramangala blocks
    (
        "Khawa Karpo",
        "Koramangala 5th Block",
        ["Chinese", "Tibetan"],
        350,
        4.3,
        996,
        ["quick_service"],
        KOR,
    ),
    (
        "Momo Hut",
        "Koramangala 5th Block",
        ["Chinese", "Momos"],
        250,
        4.0,
        685,
        ["quick_service"],
        KOR,
    ),
    ("Big Pitcher", "Koramangala 5th Block", ["North Indian"], 1600, 4.4, 5000, ["bar"], KOR),
    (
        "Meghana Foods",
        "Koramangala 5th Block",
        ["Biryani", "Andhra"],
        600,
        4.4,
        9000,
        ["delivery"],
        KOR,
    ),
    ("Kor Cafe", "Koramangala 5th Block", ["Cafe"], 300, 3.4, 40, ["cafe"], KOR),
    ("Kor Dhaba", "Koramangala 5th Block", ["North Indian"], 450, 3.2, 30, ["quick_service"], KOR),
    ("Kor Six", "Koramangala 6th Block", ["Chinese"], 500, 3.7, 50, [], KOR),
    ("Kor Generic", "Koramangala", ["Italian"], 900, 4.1, 100, ["family_friendly"], KOR),
    # Whitefield, plus a sub-area with a comma name
    ("KFC", "Whitefield", ["Burger", "Fast Food"], 450, 3.9, 536, ["quick_service"], WHF),
    ("KFC", "Whitefield", ["Burger", "Fast Food"], 450, 3.8, 500, ["quick_service"], WHF),
    ("KFC", "Whitefield", ["Burger", "Fast Food"], 450, 3.7, 400, ["quick_service"], WHF),
    ("Windmills", "Whitefield", ["American"], 1600, 4.6, 5000, ["bar"], WHF),
    ("Whitefield Udupi", "Whitefield", ["South Indian"], 200, 4.0, 300, ["quick_service"], WHF),
    ("ITPL Biryani", "ITPL Main Road, Whitefield", ["Biryani"], 300, 3.5, 20, [], WHF),
    # A far-off neighbourhood with too few restaurants for a zone profile
    ("Kengeri Mess", "Kengeri", ["South Indian"], 150, 3.6, 10, ["quick_service"], ["Kengeri"]),
    # Unknown cost
    ("Mystery Cost", "Indiranagar", ["Thai"], None, 4.0, 50, [], IND),
    ("Cheap Thai", "Indiranagar", ["Thai"], 350, 3.3, 15, [], IND),
]


def make_df(rows=RESTAURANTS, thresholds=None) -> pd.DataFrame:
    thresholds = thresholds or Settings(_env_file=None)
    records = []
    for i, (name, locality, cuisines, cost, rating, votes, tags, zones) in enumerate(rows):
        address = f"{i} Main Road, {locality}, Bangalore"
        records.append(
            {
                "id": make_id(name, address),
                "name": name,
                "city": "Bangalore",
                "locality": locality,
                "address": address,
                "zones": zones,
                "cuisines": cuisines,
                "rest_types": [],
                "listing_types": [],
                "cost_for_two": cost,
                "budget_band": cost_to_band(cost, thresholds),
                "rating": rating,
                "votes": votes,
                "is_new": rating is None and votes == 0,
                "online_order": "delivery" in tags,
                "book_table": "table_booking" in tags,
                "dishes_liked": [],
                "tags": tags,
                "review_snippets": [],
            }
        )
    df = pd.DataFrame.from_records(records, columns=list(OUTPUT_COLUMNS))
    df["cost_for_two"] = df["cost_for_two"].astype("Int64")
    df["rating"] = df["rating"].astype("Float64")
    return df


@pytest.fixture
def settings(monkeypatch) -> Settings:
    for var in (
        "DINEWISE_MIN_CANDIDATES",
        "DINEWISE_SHORTLIST_SIZE",
        "DINEWISE_BUDGET_LOW_MAX",
        "DINEWISE_BUDGET_MEDIUM_MAX",
        "DINEWISE_MAX_BRANCHES_PER_CHAIN",
    ):
        monkeypatch.delenv(var, raising=False)
    return Settings(_env_file=None)


@pytest.fixture
def repo(settings) -> RestaurantRepository:
    return RestaurantRepository(make_df(), settings)
