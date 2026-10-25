from dataclasses import dataclass

import pytest

from dinewise.domain.budget import band_label, band_range, cost_to_band


@dataclass
class Thresholds:
    budget_low_max: int = 500
    budget_medium_max: int = 1500


T = Thresholds()


@pytest.mark.parametrize(
    "cost,band",
    [
        (1, "low"),
        (500, "low"),
        (501, "medium"),
        (1500, "medium"),
        (1501, "high"),
        (60000, "high"),
    ],
)
def test_band_edges(cost, band):
    assert cost_to_band(cost, T) == band


@pytest.mark.parametrize("cost", [None, 0, -100])
def test_unknown_or_invalid_cost_has_no_band(cost):
    assert cost_to_band(cost, T) is None


def test_band_ranges_are_contiguous():
    assert band_range("low", T) == (1, 500)
    assert band_range("medium", T) == (501, 1500)
    assert band_range("high", T) == (1501, None)


def test_band_range_rejects_unknown_band():
    with pytest.raises(ValueError):
        band_range("luxury", T)  # type: ignore[arg-type]


def test_band_labels():
    assert band_label("low", T) == "up to ₹500 for two"
    assert band_label("medium", T) == "₹501–1,500 for two"
    assert band_label("high", T) == "above ₹1,500 for two"


def test_follows_settings_thresholds():
    t = Thresholds(budget_low_max=400, budget_medium_max=1000)
    assert cost_to_band(400, t) == "low"
    assert cost_to_band(401, t) == "medium"
    assert cost_to_band(1001, t) == "high"
