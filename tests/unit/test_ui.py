"""Streamlit frontend: API client, display helpers, and the page itself (via AppTest).

No network: the client is tested with httpx.MockTransport, and the page with a
patched ApiClient. IDs in comments refer to docs/edge-case.md.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import httpx
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from dinewise.ui import client as client_module
from dinewise.ui.client import ApiClient, ApiError
from dinewise.ui.view import (
    card_html,
    chips,
    corrections_text,
    cost_text,
    empty_state_tips,
    md_escape,
    rating_text,
)

APP = Path(__file__).resolve().parents[2] / "src" / "dinewise" / "ui" / "streamlit_app.py"


def restaurant(**overrides: Any) -> dict[str, Any]:
    base = {
        "id": "r_1",
        "name": "Chianti",
        "city": "Bangalore",
        "locality": "Koramangala 5th Block",
        "address": "12, 17th Main, Bangalore",
        "cuisines": ["Italian"],
        "rest_types": ["Casual Dining"],
        "cost_for_two": 1500,
        "budget_band": "high",
        "rating": 4.7,
        "votes": 4872,
        "is_new": False,
        "online_order": False,
        "book_table": True,
        "tags": ["table_booking"],
    }
    return base | overrides


def rec(rank: int = 1, source: str = "llm", **overrides: Any) -> dict[str, Any]:
    return {
        "rank": rank,
        "restaurant": restaurant(**overrides),
        "explanation": "Great pasta, well within your budget.",
        "caveats": None,
        "source": source,
    }


RESPONSE: dict[str, Any] = {
    "recommendations": [rec(1), rec(2, name="Onesta", locality="Koramangala 4th Block")],
    "summary": "Two strong Italian picks in Koramangala.",
    "relaxations": [],
    "degraded": False,
    "normalized_preferences": {
        "location": "Koramangala",
        "location_kind": "locality",
        "city": "Bangalore",
        "localities": ["Koramangala"],
        "budget": None,
        "cuisines": ["Italian"],
        "min_rating": 0.0,
        "extras": None,
        "extras_tags": [],
        "top_k": 5,
        "corrections": [],
    },
    "meta": {
        "candidates_considered": 25,
        "total_matches": 70,
        "ranking": "llm",
        "model": "openai/gpt-oss-120b",
        "latency_ms": 2400,
    },
}


# =============================================================================
# API client
# =============================================================================


def make_client(handler) -> ApiClient:
    return ApiClient("http://api.test", timeout=5, transport=httpx.MockTransport(handler))


def test_client_posts_preferences_and_returns_body():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = request.content
        return httpx.Response(200, json=RESPONSE)

    body = make_client(handler).recommend({"location": "Koramangala"})
    assert body["summary"] == RESPONSE["summary"]
    assert seen["path"] == "/api/v1/recommendations"
    assert b"Koramangala" in seen["body"]


def test_client_maps_unknown_value_error_with_suggestions():
    payload = {
        "error": {
            "code": "unknown_value",
            "message": "Unknown location 'xyzzy'",
            "field": "location",
            "value": "xyzzy",
            "suggestions": ["Yelahanka", "Electronic City"],
        },
        "request_id": "abc",
    }
    client = make_client(lambda request: httpx.Response(422, json=payload))
    with pytest.raises(ApiError) as info:
        client.recommend({"location": "xyzzy"})
    err = info.value
    assert (err.status, err.code) == (422, "unknown_value")
    assert (err.field, err.value) == ("location", "xyzzy")
    assert err.suggestions == ["Yelahanka", "Electronic City"]
    assert err.request_id == "abc"


def test_client_maps_fastapi_validation_error():
    payload = {"detail": [{"loc": ["body", "min_rating"], "msg": "Input should be <= 5"}]}
    client = make_client(lambda request: httpx.Response(422, json=payload))
    with pytest.raises(ApiError) as info:
        client.recommend({"location": "x", "min_rating": 9})
    assert info.value.field == "min_rating"
    assert "min_rating" in info.value.message


@pytest.mark.parametrize(
    ("status", "fragment"),
    [(429, "Too many requests"), (500, "hit an error"), (503, "unavailable")],
)
def test_client_maps_status_without_body(status, fragment):
    client = make_client(lambda request: httpx.Response(status, text="oops"))
    with pytest.raises(ApiError) as info:
        client.locations()
    assert info.value.status == status
    assert fragment in info.value.message


def test_client_unreachable_and_timeout():
    def refuse(request):
        raise httpx.ConnectError("refused")

    def slow(request):
        raise httpx.ReadTimeout("slow")

    with pytest.raises(ApiError) as info:
        make_client(refuse).cuisines()
    assert info.value.unreachable
    assert "api.test" in info.value.message

    with pytest.raises(ApiError) as info:
        make_client(slow).recommend({"location": "x"})
    assert info.value.unreachable
    assert "too long" in info.value.message


def test_client_health_returns_503_body():
    body = {"status": "unavailable", "detail": "data missing"}
    client = make_client(lambda request: httpx.Response(503, json=body))
    assert client.health()["status"] == "unavailable"


def test_api_url_and_timeout_from_env(monkeypatch):
    monkeypatch.setenv("DINEWISE_API_URL", "http://api:8000/ ")
    monkeypatch.setenv("DINEWISE_UI_TIMEOUT_S", "nonsense")
    assert client_module.api_url_from_env() == "http://api:8000"
    assert client_module.timeout_from_env() == client_module.DEFAULT_TIMEOUT_S
    monkeypatch.delenv("DINEWISE_API_URL")
    assert client_module.api_url_from_env() == client_module.DEFAULT_API_URL


# =============================================================================
# Display helpers
# =============================================================================


def test_rating_and_cost_text():
    assert rating_text(restaurant()) == "★ 4.7 (4,872 votes)"
    assert rating_text(restaurant(votes=1)) == "★ 4.7 (1 vote)"
    assert rating_text(restaurant(rating=None, is_new=True)) == "New / not rated"  # UI-09
    assert cost_text(restaurant(cost_for_two=1200)) == "₹1,200 for two"
    assert cost_text(restaurant(cost_for_two=120000)) == "₹1,20,000 for two"  # UI-11
    assert cost_text(restaurant(cost_for_two=None)) == "Cost not available"  # UI-10


def test_chips_cap_with_more_count():
    values = [f"C{i}" for i in range(10)]
    shown, more = chips(values)
    assert len(shown) == 6 and more == 4
    assert chips(["A"]) == (["A"], 0)


def test_card_shows_required_fields_and_labels():
    html = card_html(rec(3, source="llm", cuisines=[f"C{i}" for i in range(9)]))
    for fragment in ("#3", "Chianti", "★ 4.7", "₹1,500 for two", "Koramangala 5th Block"):
        assert fragment in html
    assert "Great pasta" in html
    assert "AI-generated" in html
    assert "+3 more" in html  # UI-12
    heuristic = card_html(rec(source="heuristic"))
    assert "Basic ranking" in heuristic and "AI-generated" not in heuristic  # UI-05, UI-06


def test_card_escapes_api_text():
    evil = rec(name="<script>alert(1)</script>")
    evil["explanation"] = "<img src=x onerror=alert(1)> **bold**"
    evil["caveats"] = "</div><b>break</b>"
    html = card_html(evil)
    assert "<script>" not in html and "<img" not in html and "<b>break" not in html  # UI-14
    assert "&lt;script&gt;" in html
    assert "dw-caveat" in html


def test_md_escape_neutralises_markdown_and_latex():
    assert md_escape("**x** $5 [a](b)") == r"\*\*x\*\* \$5 \[a\]\(b\)"


def test_corrections_text():
    assert corrections_text([]) is None
    text = corrections_text([{"field": "location", "input": "koramangla", "value": "Koramangala"}])
    assert text == 'Showing results for **Koramangala** (you typed "koramangla").'


def test_empty_state_tips_follow_the_query():
    tips = empty_state_tips({"min_rating": 4.5, "budget": "low", "cuisines": ["Thai"]})
    assert any("rating" in t for t in tips)
    assert any("budget" in t for t in tips)
    assert any("cuisine" in t for t in tips)
    assert len(empty_state_tips({})) == 1
    assert empty_state_tips({}, city_wide=True) == []  # already searched the whole city


# =============================================================================
# The page (Streamlit AppTest with a fake API client)
# =============================================================================

META = {
    "locations": {
        "city": "Bangalore",
        "locations": [{"name": "Indiranagar", "count": 500}, {"name": "Koramangala", "count": 900}],
    },
    "cuisines": {"cuisines": [{"name": "Italian", "count": 800}, {"name": "Thai", "count": 90}]},
    "budgets": {
        "budgets": [
            {"band": "low", "label": "up to ₹400 for two", "min_cost": 1, "max_cost": 400},
            {"band": "medium", "label": "₹401–1,000 for two", "min_cost": 401, "max_cost": 1000},
            {"band": "high", "label": "above ₹1,000 for two", "min_cost": 1001, "max_cost": None},
        ]
    },
}


class FakeApi:
    """Stands in for the HTTP API; records the requests the page sends."""

    def __init__(self) -> None:
        self.meta_error: ApiError | None = None
        self.responses: list[dict[str, Any] | ApiError] = [RESPONSE]
        self.requests: list[dict[str, Any]] = []

    def install(self, monkeypatch) -> None:
        fake = self

        def meta(key):
            def call(self):
                if fake.meta_error:
                    raise fake.meta_error
                return META[key]

            return call

        def recommend(self, prefs):
            fake.requests.append(prefs)
            outcome = fake.responses[min(len(fake.requests), len(fake.responses)) - 1]
            if isinstance(outcome, ApiError):
                raise outcome
            return copy.deepcopy(outcome)

        monkeypatch.setattr(ApiClient, "locations", meta("locations"))
        monkeypatch.setattr(ApiClient, "cuisines", meta("cuisines"))
        monkeypatch.setattr(ApiClient, "budgets", meta("budgets"))
        monkeypatch.setattr(ApiClient, "health", lambda self: {"llm_ranking": True})
        monkeypatch.setattr(ApiClient, "recommend", recommend)


@pytest.fixture
def api(monkeypatch) -> FakeApi:
    st.cache_data.clear()
    st.cache_resource.clear()
    fake = FakeApi()
    fake.install(monkeypatch)
    return fake


def run_app() -> AppTest:
    return AppTest.from_file(str(APP), default_timeout=30).run()


def search(at: AppTest, location: str | None = "Koramangala", **fields: Any) -> AppTest:
    if location is not None:
        at.selectbox(key="location").set_value(location)
    if "cuisines" in fields:
        at.multiselect(key="cuisines").set_value(fields["cuisines"])
    if "budget" in fields:
        at.radio(key="budget").set_value(fields["budget"])
    if "min_rating" in fields:
        at.slider(key="min_rating").set_value(fields["min_rating"])
    if "extras" in fields:
        at.text_area(key="extras").set_value(fields["extras"])
    return at.button[0].click().run()


def markdown_text(at: AppTest) -> str:
    return "\n".join(m.value for m in at.markdown)


def test_page_loads_form_from_meta(api):
    at = run_app()
    assert not at.exception
    location = at.selectbox(key="location")
    assert location.options == ["Bangalore", "Indiranagar", "Koramangala"]
    assert at.multiselect(key="cuisines").options == ["Italian", "Thai"]
    assert at.radio(key="budget").value == "any"
    assert at.slider(key="min_rating").value == 0.0
    assert "Find restaurants" in at.button[0].label


def test_search_sends_preferences_and_renders_cards(api):
    at = search(
        run_app(),
        cuisines=["Italian"],
        budget="medium",
        min_rating=4.0,
        extras="  family-friendly  ",
    )
    assert not at.exception
    assert api.requests == [
        {
            "location": "Koramangala",
            "cuisines": ["Italian"],
            "budget": "medium",
            "min_rating": 4.0,
            "extras": "family-friendly",
        }
    ]
    text = markdown_text(at)
    assert text.count('class="dw-card"') == 2
    assert "Two strong Italian picks" in text
    assert any("AI-generated summary" in c.value for c in at.caption)
    assert not at.warning
    assert at.session_state.pending is False  # button enabled again (UI-15)


def test_search_without_location_warns_and_sends_nothing(api):
    at = search(run_app(), location=None)
    assert api.requests == []
    assert any("Choose a location" in w.value for w in at.warning)


def test_degraded_relaxed_and_corrected_result(api):
    response = copy.deepcopy(RESPONSE)
    response["degraded"] = True
    response["relaxations"] = ["Budget widened to include medium (₹401–1,000 for two)"]
    response["normalized_preferences"]["corrections"] = [
        {"field": "location", "input": "koramangla", "value": "Koramangala"}
    ]
    for r in response["recommendations"]:
        r["source"] = "heuristic"
    api.responses = [response]

    # AppTest can't hold a typed (unlisted) selectbox value, so the correction comes
    # from the fake API; the real typo path is covered by the manual end-to-end demo.
    at = search(run_app())
    warnings = " ".join(w.value for w in at.warning)
    assert "Basic ranking (AI unavailable)" in warnings  # UI-05
    assert "Budget widened" in warnings
    assert any("Koramangala" in i.value and "koramangla" in i.value for i in at.info)  # UI-18
    assert not any("AI-generated" in c.value for c in at.caption)
    assert "AI-generated" not in markdown_text(at)


def test_empty_result_shows_tips(api):
    empty = copy.deepcopy(RESPONSE)
    empty["recommendations"] = []
    empty["summary"] = "No restaurants matched, even after relaxing filters."
    api.responses = [empty]
    at = search(run_app(), min_rating=4.5)
    assert any("No restaurants matched" in i.value for i in at.info)
    assert "Lower the minimum rating" in markdown_text(at)  # UI-07
    assert 'class="dw-card"' not in markdown_text(at)


def test_unknown_location_offers_clickable_suggestions(api):
    api.responses = [
        ApiError(
            422,
            "Unknown location 'xyzzy'",
            code="unknown_value",
            field="location",
            value="xyzzy",
            suggestions=["Indiranagar", "Koramangala"],
        ),
        RESPONSE,
    ]
    at = search(run_app(), location="Koramangala")  # the fake answers 422 regardless
    assert any("Unknown location" in e.value for e in at.error)
    suggestion = next(b for b in at.button if b.label == "Indiranagar")

    at = suggestion.click().run()  # UI-03: picking a suggestion searches again
    assert api.requests[-1]["location"] == "Indiranagar"
    assert at.session_state.location == "Indiranagar"
    assert not at.error
    assert 'class="dw-card"' in markdown_text(at)


def test_unknown_cuisine_suggestion_replaces_only_that_cuisine(api):
    api.responses = [
        ApiError(422, "Unknown cuisine", field="cuisines", value="itlian", suggestions=["Italian"]),
        RESPONSE,
    ]
    at = search(run_app(), cuisines=["Thai", "itlian"])
    at = next(b for b in at.button if b.label == "Italian").click().run()
    assert api.requests[-1]["cuisines"] == ["Thai", "Italian"]


@pytest.mark.parametrize(
    ("error", "kind", "fragment"),
    [
        (ApiError(None, "Can't reach the DineWise service."), "error", "Make sure the API"),
        (ApiError(429, "slow down"), "warning", "Too many requests"),  # UI-04
        (ApiError(500, "boom", request_id="rid-1"), "error", "Something went wrong"),
    ],
)
def test_search_errors_keep_the_form(api, error, kind, fragment):
    api.responses = [error]
    at = search(run_app(), cuisines=["Thai"])
    assert any(fragment in e.value for e in getattr(at, kind))
    assert at.multiselect(key="cuisines").value == ["Thai"]  # UI-02
    assert at.selectbox(key="location").value == "Koramangala"


def test_previous_results_stay_when_a_later_search_fails(api):
    api.responses = [RESPONSE, ApiError(500, "boom")]
    at = search(run_app())
    at = at.button[0].click().run()
    assert at.error  # the failure is shown...
    assert 'class="dw-card"' in markdown_text(at)  # ...above the earlier results (UI-17)


def test_api_down_on_load_shows_retry(api):
    api.meta_error = ApiError(None, "Can't reach the DineWise service at http://x.")
    at = run_app()
    assert any("Service unavailable" in e.value for e in at.error)  # UI-01
    assert [b.label for b in at.button] == ["Retry"]

    api.meta_error = None
    at = at.button[0].click().run()
    assert not at.error
    assert at.selectbox(key="location").options
