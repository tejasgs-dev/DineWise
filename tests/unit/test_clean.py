"""Tests for the cleaning pipeline. IDs in comments refer to docs/edge-case.md."""

import math

import pandas as pd
import pytest

from dinewise.domain.models import Restaurant
from dinewise.ingestion import clean
from dinewise.ingestion.clean import (
    city_from_url,
    clean_text,
    extract_snippets_value,
    fix_mojibake,
    make_id,
    parse_bool_value,
    parse_cost_value,
    parse_rating_value,
    parse_votes_value,
    split_list_value,
    tags_for,
    truncate,
)


class Thresholds:
    budget_low_max = 500
    budget_medium_max = 1500


def raw_row(**overrides):
    row = {
        "url": "https://www.zomato.com/bangalore/test-place?context=abc",
        "address": "12, 100 Feet Road, Indiranagar, Bangalore",
        "name": "Test Place",
        "online_order": "Yes",
        "book_table": "No",
        "rate": "4.1/5",
        "votes": "120",
        "location": "Indiranagar",
        "rest_type": "Casual Dining",
        "dish_liked": "Pasta, Pizza",
        "cuisines": "Italian, Continental",
        "approx_cost(for two people)": "800",
        "reviews_list": repr(
            [("Rated 4.0", "RATED\n  Lovely pasta and a calm place for a weekend lunch.")]
        ),
        "listed_in(type)": "Dine-out",
        "listed_in(city)": "Indiranagar",
    }
    row.update(overrides)
    return row


def run_clean(rows):
    return clean.clean(pd.DataFrame(rows, dtype=str), Thresholds())


# --- Ratings (DATA-01..06) -----------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("4.1/5", (4.1, False, False)),
        ("4.1 /5", (4.1, False, False)),
        (" 4.1/5 ", (4.1, False, False)),
        ("4.1", (4.1, False, False)),
        ("5/5", (5.0, False, False)),
        ("NEW", (None, True, False)),
        ("new", (None, True, False)),
        ("-", (None, False, False)),
        ("", (None, False, False)),
        (None, (None, False, False)),
        (float("nan"), (None, False, False)),
        ("6/5", (None, False, True)),
        ("-1/5", (None, False, True)),
        ("good", (None, False, True)),
    ],
)
def test_parse_rating_value(raw, expected):
    assert parse_rating_value(raw) == expected


# --- Cost and votes (DATA-07..12) ---------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1,200", 1200),
        ("800", 800),
        ("1200.0", 1200),
        ("6,000", 6000),
        ("", None),
        (None, None),
        (float("nan"), None),
        ("N/A", None),
        ("abc", None),
        ("0", None),
        ("-300", None),
    ],
)
def test_parse_cost_value(raw, expected):
    assert parse_cost_value(raw) == expected


@pytest.mark.parametrize(
    "raw,expected", [("775", 775), ("1,024", 1024), ("", 0), (None, 0), ("-5", 0), ("x", 0)]
)
def test_parse_votes_value(raw, expected):
    assert parse_votes_value(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Yes", True),
        ("yes", True),
        (" YES ", True),
        ("No", False),
        ("", False),
        (None, False),
        ("maybe", False),
    ],
)
def test_parse_bool_value(raw, expected):
    assert parse_bool_value(raw) is expected


# --- List fields (DATA-14..17, DATA-22) ----------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("North Indian, Mughlai, Chinese", ["North Indian", "Mughlai", "Chinese"]),
        ("Italian,,Chinese", ["Italian", "Chinese"]),
        ("Italian , chinese ,", ["Italian", "Chinese"]),
        ("Chinese, chinese", ["Chinese"]),
        ("  south   indian ", ["South Indian"]),
        ("", []),
        (None, []),
        (float("nan"), []),
    ],
)
def test_split_list_value(raw, expected):
    assert split_list_value(raw) == expected


def test_split_list_value_keeps_existing_capitals_and_applies_aliases():
    assert split_list_value("BBQ, bbq, Afghani, Afghan", clean.CUISINE_ALIASES) == ["BBQ", "Afghan"]


# --- Text repair (DATA-18, DATA-19) --------------------------------------------


def _double_encode(text: str, rounds: int) -> str:
    for _ in range(rounds):
        text = text.encode("utf-8").decode("latin-1")
    return text


@pytest.mark.parametrize("rounds", [1, 2, 4])
def test_fix_mojibake_undoes_repeated_double_encoding(rounds):
    assert fix_mojibake(_double_encode("Café Crème", rounds)) == "Café Crème"


@pytest.mark.parametrize("text", ["Café Noir", "ಬೆಂಗಳೂರು Kitchen", "Plain ASCII", "100°C"])
def test_fix_mojibake_leaves_correct_text_alone(text):
    assert fix_mojibake(text) == text


def test_fix_mojibake_maps_stray_cp1252_bytes():
    assert fix_mojibake("Brother\x92s Bar") == "Brother’s Bar"


def test_clean_text():
    assert clean_text("  Caf&eacute;   Noir \n") == "Café Noir"
    assert clean_text("   ") is None
    assert clean_text(None) is None
    assert clean_text(float("nan")) is None


# --- Reviews (DATA-20, DATA-21, SEC-07) ----------------------------------------


def test_extract_snippets_parses_and_strips_prefix():
    raw = (
        "[('Rated 4.0', 'RATED\\n  A beautiful place to dine in. Great food.'), "
        "('Rated 3.0', 'RATED\\n  Decent value for money, slow service.')]"
    )
    assert extract_snippets_value(raw) == [
        "A beautiful place to dine in. Great food.",
        "Decent value for money, slow service.",
    ]


def test_extract_snippets_limits_count_and_length():
    long_text = "word " * 100
    raw = repr([("Rated 5.0", f"RATED\n  {long_text}")] * 5)
    snippets = extract_snippets_value(raw, max_snippets=3, max_len=200)
    assert len(snippets) == 3
    assert all(len(s) <= 200 and s.endswith("…") for s in snippets)


@pytest.mark.parametrize("raw", ["[]", "", None, "not a list", "[('Rated 4.0',", "{'a': 1}"])
def test_extract_snippets_handles_empty_and_malformed(raw):
    assert extract_snippets_value(raw) == []


def test_extract_snippets_skips_very_short_reviews():
    raw = repr(
        [
            ("Rated 5.0", "RATED\n  Good"),
            ("Rated 4.0", "RATED\n  The biryani here is consistently excellent."),
        ]
    )
    assert extract_snippets_value(raw) == ["The biryani here is consistently excellent."]


def test_extract_snippets_scrubs_contact_details():
    raw = repr(
        [("Rated 4.0", "RATED\n  Call 9876543210 or mail owner@example.com to book a table here.")]
    )
    (snippet,) = extract_snippets_value(raw)
    assert "9876543210" not in snippet and "owner@example.com" not in snippet
    assert "[phone removed]" in snippet and "[email removed]" in snippet


def test_extract_snippets_does_not_execute_code():
    assert extract_snippets_value("__import__('os').system('echo hacked')") == []


def test_truncate_at_word_boundary():
    assert truncate("short", 200) == "short"
    out = truncate("alpha beta gamma delta epsilon", 20)
    assert len(out) <= 20 and out.endswith("…") and " " not in out[-2:]


# --- Identity (DATA-25, DATA-28, DATA-29) --------------------------------------


def test_make_id_is_stable_and_normalized():
    a = make_id("Jalsa", "942, 21st Main Road, Banashankari")
    assert a == make_id("Jalsa", "942, 21st Main Road, Banashankari")
    assert a == make_id("  JALSA ", "942 21st Main Road,  Banashankari.")
    assert a.startswith("r_") and len(a) == 14


def test_make_id_distinguishes_branches():
    assert make_id("KFC", "MG Road") != make_id("KFC", "Koramangala")


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.zomato.com/bangalore/jalsa-banashankari?context=x", "Bangalore"),
        ("https://www.zomato.com/new-delhi/some-place", "New Delhi"),
        (None, "Bangalore"),
        ("not a url", "Bangalore"),
    ],
)
def test_city_from_url(url, expected):
    assert city_from_url(url) == expected


# --- Tags ----------------------------------------------------------------------


def test_tags_for_sit_down_restaurant():
    assert tags_for(
        ["Casual Dining"], ["Dine-out", "Buffet"], book_table=True, online_order=False
    ) == [
        "buffet",
        "family_friendly",
        "table_booking",
    ]


def test_tags_for_bar_is_not_family_friendly():
    tags = tags_for(
        ["Casual Dining", "Bar"], ["Drinks & nightlife"], book_table=False, online_order=False
    )
    assert "bar" in tags and "family_friendly" not in tags


def test_tags_for_juice_shop_in_pubs_listing_is_not_a_bar():
    tags = tags_for(["Beverage Shop"], ["Pubs and bars"], book_table=False, online_order=True)
    assert "bar" not in tags


def test_tags_for_quick_bites_with_delivery():
    assert tags_for(["Quick Bites"], ["Delivery"], book_table=False, online_order=True) == [
        "delivery",
        "quick_service",
    ]


def test_tags_for_cafe_and_desserts():
    tags = tags_for(["Cafe", "Dessert Parlor"], ["Cafes"], book_table=False, online_order=False)
    assert {"cafe", "desserts"} <= set(tags)


# --- Pipeline ------------------------------------------------------------------


def test_clean_end_to_end_single_row():
    df, stats = run_clean([raw_row()])
    assert stats.rows_in == 1 and stats.rows_out == 1
    r = df.iloc[0]
    assert r["name"] == "Test Place"
    assert r["city"] == "Bangalore"
    assert r["locality"] == "Indiranagar"
    assert r["rating"] == pytest.approx(4.1)
    assert r["cost_for_two"] == 800
    assert r["budget_band"] == "medium"
    assert list(r["cuisines"]) == ["Italian", "Continental"]
    assert list(r["zones"]) == ["Indiranagar"]
    assert r["online_order"] and not r["book_table"]
    assert "family_friendly" in r["tags"]
    assert list(r["review_snippets"]) == ["Lovely pasta and a calm place for a weekend lunch."]
    assert list(df.columns) == list(clean.OUTPUT_COLUMNS)


def test_dedup_merges_listings_and_keeps_most_voted_row():  # DATA-23, DATA-26
    rows = [
        raw_row(
            **{
                "listed_in(type)": "Delivery",
                "listed_in(city)": "Indiranagar",
                "votes": "100",
                "rate": "3.9/5",
            }
        ),
        raw_row(
            **{
                "listed_in(type)": "Dine-out",
                "listed_in(city)": "Old Airport Road",
                "votes": "150",
                "rate": "4.2/5",
            }
        ),
        raw_row(
            **{
                "listed_in(type)": "Delivery",
                "listed_in(city)": "Indiranagar",
                "votes": "90",
                "rate": "3.8/5",
            }
        ),
    ]
    df, stats = run_clean(rows)
    assert stats.rows_out == 1 and stats.duplicates_removed == 2
    r = df.iloc[0]
    assert r["votes"] == 150 and r["rating"] == pytest.approx(4.2)
    assert list(r["listing_types"]) == ["Delivery", "Dine-out"]
    assert list(r["zones"]) == ["Indiranagar", "Old Airport Road"]


def test_dedup_treats_address_formatting_differences_as_same_place():  # DATA-25
    rows = [raw_row(), raw_row(address="12 100 feet road,  Indiranagar, Bangalore.")]
    _, stats = run_clean(rows)
    assert stats.rows_out == 1


def test_dedup_keeps_chain_branches_separate():  # DATA-24
    rows = [
        raw_row(name="KFC"),
        raw_row(name="KFC", address="5, MG Road, Bangalore", location="MG Road"),
    ]
    df, _ = run_clean(rows)
    assert len(df) == 2 and df["id"].is_unique


def test_rows_without_name_or_address_are_dropped():  # DATA-27
    _, stats = run_clean([raw_row(), raw_row(name=""), raw_row(address=None)])
    assert stats.rows_out == 1 and stats.dropped_invalid == 2


def test_blank_location_falls_back_to_zone():  # DATA-31
    df, stats = run_clean([raw_row(location=None, **{"listed_in(city)": "Old Airport Road"})])
    assert stats.dropped_no_locality == 0
    assert df.iloc[0]["locality"] == "Old Airport Road"


def test_missing_values_become_nulls_not_errors():
    row = raw_row(rate="NEW", cuisines=None, rest_type=None, dish_liked=None, reviews_list="[]")
    row["approx_cost(for two people)"] = None
    df, stats = run_clean([row])
    r = df.iloc[0]
    assert pd.isna(r["rating"]) and r["is_new"]
    assert pd.isna(r["cost_for_two"])
    assert pd.isna(r["budget_band"])
    assert list(r["cuisines"]) == [] and list(r["review_snippets"]) == []
    assert stats.no_rating == 1 and stats.new == 1 and stats.no_cost == 1


def test_ids_are_stable_across_runs_and_row_order():  # DATA-29
    rows = [raw_row(name=f"Place {i}", address=f"{i} Main Road") for i in range(5)]
    first, _ = run_clean(rows)
    second, _ = run_clean(list(reversed(rows)))
    assert sorted(first["id"]) == sorted(second["id"])


def test_every_output_row_validates_as_restaurant():
    rows = [
        raw_row(),
        raw_row(
            name="New Spot", address="1 Lane", rate="NEW", **{"approx_cost(for two people)": ""}
        ),
        raw_row(
            name="Bar One", address="2 Lane", rest_type="Bar, Pub", rate="-", reviews_list="[]"
        ),
    ]
    df, _ = run_clean(rows)
    for record in df.to_dict(orient="records"):
        restaurant = Restaurant.model_validate(record)
        assert restaurant.rating is None or 0 <= restaurant.rating <= 5
        assert restaurant.cost_for_two is None or restaurant.cost_for_two > 0
        assert not (isinstance(restaurant.rating, float) and math.isnan(restaurant.rating))


def test_dropped_share_ignores_duplicates():
    stats = clean.CleanStats(rows_in=100, dropped_invalid=5, duplicates_removed=60)
    assert stats.dropped_share == pytest.approx(0.05)


@pytest.mark.parametrize(
    "garbled,expected",
    [
        (
            "but usually itƒƒ‚ƒƒ‚‚s crowded on weekends here",
            "but usually it's crowded on weekends here",
        ),
        ("didnƒƒ‚ƒƒ‚‚ƒƒƒ‚‚ƒ‚‚€™t feel the vibe at all", "didn't feel the vibe at all"),
        ("the best cafƒƒ‚ƒƒ‚‚© in the neighbourhood", "the best caf in the neighbourhood"),
        (
            "Rated 4/5 – good food • nice staff",
            "Rated 4/5 – good food • nice staff",
        ),  # real symbols kept
    ],
)
def test_extract_snippets_strips_unrepairable_mojibake(garbled, expected):
    raw = repr([("Rated 4.0", f"RATED\n  {garbled}")])
    assert extract_snippets_value(raw) == [expected]
