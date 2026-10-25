"""Preference validation and normalization. IDs refer to docs/edge-case.md."""

import pytest
from pydantic import ValidationError

from dinewise.domain.models import UserPreferences
from dinewise.retrieval.normalize import PreferenceNormalizer, UnknownValueError, extract_tags


@pytest.fixture
def normalizer(repo):
    return PreferenceNormalizer(repo, fuzzy_threshold=85)


def norm(normalizer, **kwargs):
    return normalizer.normalize(UserPreferences(**kwargs))


# --- UserPreferences validation --------------------------------------------------------


def test_budget_is_case_insensitive():  # INP-17
    assert UserPreferences(location="x", budget=" Medium ").budget == "medium"
    assert UserPreferences(location="x", budget="any").budget is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"location": "   "},  # INP-07
        {"location": "x", "budget": "cheap"},  # INP-18
        {"location": "x", "min_rating": 5.5},  # INP-21
        {"location": "x", "min_rating": -1},
        {"location": "x", "min_rating": float("nan")},
        {"location": "x", "extras": "a" * 301},  # INP-24
        {"location": "x", "top_k": 0},  # INP-30
        {"location": "x", "top_k": 11},
        {"location": "x", "cuisines": ["a", "b", "c", "d", "e", "f"]},  # INP-13
        {"location": "x", "cuisines": "Italian"},  # INP-32: not silently coerced
        {"location": "x", "surprise": True},  # INP-31 / D-02
    ],
)
def test_invalid_preferences_rejected(kwargs):
    with pytest.raises(ValidationError):
        UserPreferences(**kwargs)


def test_blank_extras_become_none():  # INP-23
    assert UserPreferences(location="x", extras="   ").extras is None


def test_duplicate_cuisines_removed():  # INP-12
    assert UserPreferences(location="x", cuisines=["italian", "Italian ", ""]).cuisines == [
        "italian"
    ]


# --- Location ---------------------------------------------------------------------------


def test_exact_location_any_case(normalizer):  # INP-01
    p = norm(normalizer, location="indiranagar")
    assert p.location == "Indiranagar" and p.location_kind == "locality"
    assert p.localities == ["Indiranagar"]
    assert p.corrections == []  # only casing changed


def test_location_typo_is_corrected_and_reported(normalizer):  # INP-02
    p = norm(normalizer, location="Indiranagr")
    assert p.location == "Indiranagar"
    assert [(c.field, c.input, c.value) for c in p.corrections] == [
        ("location", "Indiranagr", "Indiranagar")
    ]


def test_koramangla_typo(normalizer):
    assert norm(normalizer, location="koramangla").location == "Koramangala"


def test_ambiguous_typo_is_not_guessed(normalizer):  # INP-03
    with pytest.raises(UnknownValueError) as exc:
        norm(normalizer, location="Koramangala th Block")
    assert set(exc.value.suggestions) == {"Koramangala 5th Block", "Koramangala 6th Block"}


def test_unknown_location_gives_suggestions(normalizer):  # INP-04
    with pytest.raises(UnknownValueError) as exc:
        norm(normalizer, location="Atlantis")
    assert exc.value.field == "location"
    assert 1 <= len(exc.value.suggestions) <= 3


def test_city_name_means_city_wide(normalizer):  # INP-05
    for raw in ("Bangalore", "bengaluru"):
        p = norm(normalizer, location=raw)
        assert p.location == "Bangalore" and p.location_kind == "city" and p.localities == []


def test_unsupported_city_explains_coverage(normalizer):  # INP-06
    with pytest.raises(UnknownValueError, match="Bangalore only"):
        norm(normalizer, location="Delhi")


def test_whitespace_and_city_suffix(normalizer):  # INP-08, INP-09
    assert norm(normalizer, location="  Indiranagar ,  Bangalore ").location == "Indiranagar"
    assert norm(normalizer, location="indira   nagar").location == "Indiranagar"


def test_area_covers_its_sub_areas(normalizer):
    assert norm(normalizer, location="Koramangala").localities == [
        "Koramangala",
        "Koramangala 5th Block",
        "Koramangala 6th Block",
    ]
    assert norm(normalizer, location="Whitefield").localities == [
        "ITPL Main Road, Whitefield",
        "Whitefield",
    ]


# --- Cuisine ----------------------------------------------------------------------------


def test_cuisines_matched_and_deduplicated(normalizer):  # INP-10, INP-12
    assert norm(normalizer, location="Domlur", cuisines=["THAI", "Italian"]).cuisines == [
        "Thai",
        "Italian",
    ]
    assert norm(normalizer, location="Domlur").cuisines == []


def test_cuisine_synonyms_and_typos(normalizer):  # INP-14
    p = norm(normalizer, location="Domlur", cuisines=["chinese food", "itallian", "momo"])
    assert p.cuisines == ["Chinese", "Italian", "Momos"]
    assert {c.value for c in p.corrections} == {"Chinese", "Italian", "Momos"}


def test_unknown_cuisine_names_the_value(normalizer):  # INP-11
    with pytest.raises(UnknownValueError) as exc:
        norm(normalizer, location="Domlur", cuisines=["Italian", "Klingon"])
    assert exc.value.field == "cuisines" and exc.value.value == "Klingon"
    assert "Klingon" in str(exc.value)


# --- Extras -> tags ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,tags",
    [
        ("family-friendly", ["family_friendly"]),
        ("good for kids, quick service", ["family_friendly", "quick_service"]),
        ("fast food and table booking", ["quick_service", "table_booking"]),
        ("not a bar", []),  # INP-28
        ("family place, no buffet", ["family_friendly"]),
        ("I want a buffet but not a pub", ["buffet"]),
        ("romantic dinner", ["fine_dining"]),
        ("coffee and cake", ["cafe", "desserts"]),
        ("", []),
        (None, []),
        ("बच्चों के साथ", []),  # INP-27: non-English passes through untagged
        ("barbecue", []),  # "bar" only on word boundaries
    ],
)
def test_extract_tags(text, tags):
    assert extract_tags(text) == tags


def test_normalized_preferences_carry_extras_tags(normalizer):
    p = norm(normalizer, location="Domlur", extras="family dinner, avoid bars")
    assert p.extras_tags == ["family_friendly"]
    assert p.extras == "family dinner, avoid bars"
