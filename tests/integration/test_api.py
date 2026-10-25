"""API integration tests: real app, fixture data, fake rankers, no network.

IDs in comments refer to docs/edge-case.md.
"""

from __future__ import annotations

import json
import logging

import pytest
from fastapi.testclient import TestClient

from dinewise.api.main import create_app
from dinewise.api.schemas import RestaurantOut
from dinewise.config import Settings
from dinewise.domain.models import Recommendation, RetrievalResult
from dinewise.logging_setup import JsonFormatter
from dinewise.retrieval.repository import RestaurantRepository
from dinewise.service.recommender import RankingOutcome
from tests.unit.conftest import RESTAURANTS, make_df

URL = "/api/v1/recommendations"
ITALIAN = {"location": "Indiranagar", "cuisines": ["Italian"], "top_k": 3}


# --- Fake rankers ---------------------------------------------------------------------


class FakeRanker:
    """Picks the shortlist in reverse order, so its ranking differs from the heuristic."""

    def __init__(self) -> None:
        self.calls = 0

    def rank(self, result: RetrievalResult) -> RankingOutcome:
        self.calls += 1
        picks = list(reversed(result.candidates))[: result.preferences.top_k]
        return RankingOutcome(
            recommendations=[
                Recommendation(
                    rank=i, restaurant=r, explanation=f"AI says {r.name} fits.", source="llm"
                )
                for i, r in enumerate(picks, start=1)
            ],
            summary="AI summary.",
            model="fake-model",
            usage={"input_tokens": 4000, "output_tokens": 700, "cache_read_input_tokens": 0},
        )


class FailingRanker:
    def __init__(self) -> None:
        self.calls = 0

    def rank(self, result: RetrievalResult) -> RankingOutcome:
        self.calls += 1
        raise RuntimeError("upstream exploded")


class NoneRanker:
    def rank(self, result: RetrievalResult) -> None:
        return None  # e.g. a refusal or an API outage


# --- Fixtures ---------------------------------------------------------------------------


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(_env_file=None, data_path=tmp_path / "missing.parquet", log_format="json")


def client_for(settings, ranker=None, rows=RESTAURANTS) -> TestClient:
    repo = RestaurantRepository(make_df(rows=rows), settings)
    return TestClient(create_app(settings, repository=repo, ranker=ranker))


@pytest.fixture
def fake_ranker():
    return FakeRanker()


@pytest.fixture
def client(settings, fake_ranker):
    with client_for(settings, fake_ranker) as c:
        yield c


# =============================================================================
# POST /api/v1/recommendations
# =============================================================================


def test_happy_path_contract(client):
    r = client.post(URL, json=ITALIAN)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {
        "recommendations",
        "summary",
        "relaxations",
        "degraded",
        "normalized_preferences",
        "meta",
    }
    assert body["degraded"] is False and body["summary"] == "AI summary."
    assert body["meta"] == {
        "candidates_considered": 5,
        "total_matches": 5,
        "ranking": "llm",
        "model": "fake-model",
        "latency_ms": body["meta"]["latency_ms"],
    }
    assert [rec["rank"] for rec in body["recommendations"]] == [1, 2, 3]
    first = body["recommendations"][0]
    assert set(first) == {"rank", "restaurant", "explanation", "caveats", "source"}
    assert first["source"] == "llm" and first["explanation"].startswith("AI says")
    # Only public restaurant fields; internal ones (zones, review snippets) are dropped.
    assert set(first["restaurant"]) == set(RestaurantOut.model_fields)
    assert body["normalized_preferences"]["location"] == "Indiranagar"


def test_corrections_are_returned(client):  # D-08, API-13
    body = client.post(URL, json={**ITALIAN, "location": "indiranagr"}).json()
    assert body["normalized_preferences"]["corrections"] == [
        {"field": "location", "input": "indiranagr", "value": "Indiranagar"}
    ]


def test_relaxations_are_returned(client):
    body = client.post(URL, json={"location": "Indiranagar", "cuisines": ["Japanese"]}).json()
    assert body["relaxations"][0].startswith("Included places that don't serve Japanese")


# --- Errors from matching (arch §9.3) -------------------------------------------------


def test_unknown_location_returns_422_with_suggestions(client):  # INP-04
    r = client.post(URL, json={"location": "Atlantis"})
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "unknown_value" and err["field"] == "location"
    assert err["value"] == "Atlantis" and 1 <= len(err["suggestions"]) <= 3
    assert r.json()["request_id"] == r.headers["x-request-id"]


def test_unsupported_city_returns_422(client):  # INP-06
    r = client.post(URL, json={"location": "Mumbai"})
    assert r.status_code == 422 and "Bangalore only" in r.json()["error"]["message"]


def test_unknown_cuisine_returns_422(client):  # INP-11
    r = client.post(URL, json={"location": "Domlur", "cuisines": ["Klingon"]})
    assert r.status_code == 422 and r.json()["error"]["field"] == "cuisines"


# --- Ranker failures degrade, never error (LLM-22..32) ----------------------------------


@pytest.mark.parametrize(
    "ranker", [FailingRanker(), NoneRanker(), None], ids=["raises", "none", "absent"]
)
def test_ranker_failure_returns_heuristic_picks(settings, ranker):
    with client_for(settings, ranker) as c:
        r = c.post(URL, json=ITALIAN)
    assert r.status_code == 200
    body = r.json()
    assert body["degraded"] is True
    assert body["meta"]["ranking"] == "heuristic" and body["meta"]["model"] is None
    assert len(body["recommendations"]) == 3
    assert all(rec["source"] == "heuristic" for rec in body["recommendations"])
    assert body["recommendations"][0]["explanation"].startswith("Rated ")
    assert body["summary"].startswith("Top picks in Indiranagar for Italian")


def test_no_candidates_returns_empty_list_and_skips_ranker(settings):  # FLT-12, API-12
    ranker = FailingRanker()
    rows = [r for r in RESTAURANTS if r[0] == "Dosa Corner"]
    with client_for(settings, ranker, rows=rows) as c:
        r = c.post(URL, json={"location": "Indiranagar", "budget": "high"})
    body = r.json()
    assert r.status_code == 200
    assert body["recommendations"] == [] and body["degraded"] is False
    assert body["summary"].startswith("No restaurants matched in Indiranagar")
    assert ranker.calls == 0


# --- Input validation (arch §6.1, Phase 4 input limits) ------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {"location": "Domlur", "min_rating": 6},  # INP-21
        {"location": "Domlur", "min_rating": -0.5},
        {"location": "Domlur", "top_k": 0},  # INP-30
        {"location": "Domlur", "top_k": 11},
        {"location": "Domlur", "extras": "x" * 301},  # INP-24
        {"location": "Domlur", "cuisines": ["A", "B", "C", "D", "E", "F"]},  # INP-13
        {"location": "Domlur", "budget": "cheap"},  # INP-18
        {"location": "Domlur", "colour": "blue"},  # INP-31: unknown fields rejected (D-02)
        {"budget": "low"},  # INP-07: location required
        {"location": "   "},
    ],
)
def test_invalid_input_returns_422(client, payload):
    r = client.post(URL, json=payload)
    assert r.status_code == 422
    assert "detail" in r.json()


def test_malformed_json_returns_422(client):  # API-04
    r = client.post(URL, content=b"{not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 422


def test_oversized_body_returns_413(client):  # SEC-08
    r = client.post(
        URL, content=b"{" + b" " * 20_000 + b"}", headers={"Content-Type": "application/json"}
    )
    assert r.status_code == 413 and r.json()["error"]["code"] == "request_too_large"


def test_wrong_method_and_unknown_route(client):  # API-05, API-06
    assert client.get(URL).status_code == 405
    assert client.get("/api/v1/nope").status_code == 404


def test_web_frontend_is_served_at_root(client):
    page = client.get("/")
    assert page.status_code == 200 and "text/html" in page.headers["content-type"]
    assert '<script src="/static/config.js">' in page.text
    assert '<script src="/static/app.js">' in page.text
    config = client.get("/static/config.js")
    assert config.status_code == 200 and 'window.DINEWISE_API_BASE = "";' in config.text
    script = client.get("/static/app.js")
    assert script.status_code == 200 and "/api/v1/recommendations" in script.text
    # The static mount must not shadow the API or its docs.
    assert client.get("/healthz").status_code == 200
    assert client.get("/docs").status_code == 200
    assert client.get("/static/missing.js").status_code == 404


def test_unexpected_error_returns_safe_500(settings, monkeypatch):  # SEC-11
    c = client_for(settings, FakeRanker())
    with c:
        recommender = c.app.state.recommender

        def boom(prefs):
            raise KeyError("secret internal detail")

        monkeypatch.setattr(recommender, "recommend", boom)
        r = c.post(URL, json=ITALIAN)
    assert r.status_code == 500
    body = r.json()
    assert body["error"]["code"] == "internal_error"
    assert "secret" not in r.text and "Traceback" not in r.text
    assert body["request_id"] == r.headers["x-request-id"]


# =============================================================================
# Meta endpoints and health
# =============================================================================


def test_meta_locations_sorted(client):
    body = client.get("/api/v1/meta/locations").json()
    names = [loc["name"] for loc in body["locations"]]
    assert body["city"] == "Bangalore"
    assert names and names == sorted(names)
    assert {"name": "Domlur", "count": 5} in body["locations"]


def test_meta_cuisines_sorted(client):
    names = [c["name"] for c in client.get("/api/v1/meta/cuisines").json()["cuisines"]]
    assert names and names == sorted(names) and "Italian" in names


def test_meta_budgets(client):
    budgets = client.get("/api/v1/meta/budgets").json()["budgets"]
    assert [b["band"] for b in budgets] == ["low", "medium", "high"]
    assert budgets[1] == {
        "band": "medium",
        "label": "₹401–1,000 for two",
        "min_cost": 401,
        "max_cost": 1000,
    }


def test_healthz_ok(client):
    body = client.get("/healthz").json()
    assert body["status"] == "ok" and body["restaurants"] == len(RESTAURANTS)
    assert body["llm_ranking"] is True


def test_data_loaded_from_parquet_at_startup(tmp_path):
    path = tmp_path / "restaurants.parquet"
    make_df().to_parquet(path)
    settings = Settings(_env_file=None, data_path=path, log_format="text")
    with TestClient(create_app(settings)) as c:
        assert c.get("/healthz").json()["restaurants"] == len(RESTAURANTS)
        assert c.post(URL, json=ITALIAN).status_code == 200


def test_missing_data_returns_503_everywhere(settings):  # API-01, API-15
    with TestClient(create_app(settings)) as c:  # data_path doesn't exist
        health = c.get("/healthz")
        assert health.status_code == 503 and health.json()["status"] == "unavailable"
        assert "missing.parquet" in health.json()["detail"]
        for method, path in [("post", URL), ("get", "/api/v1/meta/locations")]:
            r = getattr(c, method)(path, json=ITALIAN) if method == "post" else c.get(path)
            assert r.status_code == 503 and r.json()["error"]["code"] == "data_unavailable"


# =============================================================================
# Cross-cutting: request IDs, logging, CORS, OpenAPI
# =============================================================================


def test_request_id_generated_or_echoed(client):
    generated = client.get("/healthz").headers["x-request-id"]
    assert len(generated) == 32
    assert (
        client.get("/healthz", headers={"X-Request-ID": "abc-123"}).headers["x-request-id"]
        == "abc-123"
    )
    unsafe = client.get("/healthz", headers={"X-Request-ID": "bad id\n"}).headers["x-request-id"]
    assert unsafe != "bad id\n" and len(unsafe) == 32


class _ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def log_records():
    handler = _ListHandler()
    logger = logging.getLogger("dinewise")
    logger.addHandler(handler)
    yield handler.records
    logger.removeHandler(handler)


def test_structured_logs_include_request_id_and_usage(client, log_records):  # arch §12.2
    r = client.post(URL, json={**ITALIAN, "extras": "family dinner " * 20})
    rid = r.headers["x-request-id"]
    rec = next(x for x in log_records if getattr(x, "event", None) == "recommendation")
    assert rec.request_id == rid
    assert rec.ranking == "llm" and rec.degraded is False
    assert rec.usage == {"input_tokens": 4000, "output_tokens": 700, "cache_read_input_tokens": 0}
    assert len(rec.extras) <= 80  # truncated
    access = next(x for x in log_records if getattr(x, "event", None) == "http_request")
    assert access.request_id == rid and access.status == 200 and access.latency_ms >= 0

    line = json.loads(JsonFormatter().format(rec))  # what actually goes to stderr
    assert line["request_id"] == rid and line["event"] == "recommendation"
    assert line["usage"]["input_tokens"] == 4000 and line["level"] == "INFO"


def test_cors_allows_only_configured_origin(client):  # SEC-14
    preflight = {
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type",
    }
    ok = client.options(URL, headers={"Origin": "http://localhost:8501", **preflight})
    assert ok.headers.get("access-control-allow-origin") == "http://localhost:8501"
    bad = client.options(URL, headers={"Origin": "https://evil.example", **preflight})
    assert "access-control-allow-origin" not in bad.headers


def test_cors_allows_origins_matching_regex(tmp_path):
    settings = Settings(
        _env_file=None,
        data_path=tmp_path / "missing.parquet",
        cors_origin_regex=r"^https://dinewise(-[a-z0-9-]+)?\.vercel\.app$",
    )
    client = client_for(settings)
    preflight = {
        "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "content-type",
    }
    for origin in ("https://dinewise.vercel.app", "https://dinewise-git-main-me.vercel.app"):
        ok = client.options(URL, headers={"Origin": origin, **preflight})
        assert ok.headers.get("access-control-allow-origin") == origin
    bad = client.options(URL, headers={"Origin": "https://evil.vercel.app", **preflight})
    assert "access-control-allow-origin" not in bad.headers


def test_recommendations_are_rate_limited_per_client(tmp_path):  # SEC-09, SEC-10
    settings = Settings(
        _env_file=None,
        data_path=tmp_path / "missing.parquet",
        rate_limit_per_minute=2,
        forwarded_hops=1,
    )
    client = client_for(settings)
    alice = {"X-Forwarded-For": "203.0.113.7"}
    assert [client.post(URL, json=ITALIAN, headers=alice).status_code for _ in range(2)] == [
        200,
        200,
    ]
    limited = client.post(URL, json=ITALIAN, headers=alice)
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "rate_limited"
    assert 1 <= int(limited.headers["Retry-After"]) <= 60
    assert limited.headers["X-Request-ID"]
    # A forged left-hand entry doesn't make Alice a new client.
    forged = {"X-Forwarded-For": "6.6.6.6, 203.0.113.7"}
    assert client.post(URL, json=ITALIAN, headers=forged).status_code == 429
    # Another client, and the other endpoints, are unaffected.
    bob = {"X-Forwarded-For": "198.51.100.9"}
    assert client.post(URL, json=ITALIAN, headers=bob).status_code == 200
    assert client.get("/api/v1/meta/locations", headers=alice).status_code == 200


def test_rate_limit_can_be_disabled(tmp_path):
    settings = Settings(
        _env_file=None, data_path=tmp_path / "missing.parquet", rate_limit_per_minute=0
    )
    client = client_for(settings)
    assert all(client.post(URL, json=ITALIAN).status_code == 200 for _ in range(15))


def test_openapi_documents_the_contract(client):
    spec = client.get("/openapi.json").json()
    assert {
        "/api/v1/recommendations",
        "/api/v1/meta/locations",
        "/api/v1/meta/cuisines",
        "/healthz",
    } <= set(spec["paths"])
    prefs = spec["components"]["schemas"]["UserPreferences"]["properties"]
    assert prefs["extras"]["anyOf"][0]["maxLength"] == 300
    assert prefs["top_k"]["maximum"] == 10 and prefs["cuisines"]["maxItems"] == 5
    responses = spec["paths"]["/api/v1/recommendations"]["post"]["responses"]
    assert {"200", "422", "503"} <= set(responses)
    assert client.get("/docs").status_code == 200
