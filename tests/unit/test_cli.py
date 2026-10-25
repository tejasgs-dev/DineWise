import json

import pytest

from dinewise import cli
from dinewise.config import get_settings

from .conftest import make_df


@pytest.fixture
def data_env(tmp_path, monkeypatch):
    path = tmp_path / "restaurants.parquet"
    make_df().to_parquet(path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DINEWISE_DATA_PATH", str(path))
    get_settings.cache_clear()
    yield path
    get_settings.cache_clear()


def test_recommend_prints_ranked_picks(data_env, capsys):
    code = cli.main(
        ["recommend", "--location", "indiranagr", "--cuisine", "Italian", "--min-rating", "4"]
    )
    out = capsys.readouterr().out
    assert code == 0
    assert "(matched 'indiranagr' to 'Indiranagar')" in out
    assert "1. Toit — Indiranagar" in out
    assert "Rated 4.7 (14,956 votes); serves Italian; ₹1,500 for two." in out
    assert "retrieval" in out and "ms" in out


def test_recommend_reports_relaxations(data_env, capsys):
    cli.main(["recommend", "--location", "Indiranagar", "--cuisine", "Japanese", "--top-k", "2"])
    out = capsys.readouterr().out
    assert "! Included places that don't serve Japanese" in out


def test_recommend_json(data_env, capsys):
    code = cli.main(["recommend", "--location", "Domlur", "--budget", "Medium", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["preferences"]["budget"] == "medium"
    assert len(payload["recommendations"]) == 5
    assert all(r["source"] == "heuristic" for r in payload["recommendations"])
    assert "review_snippets" not in payload["recommendations"][0]["restaurant"]


def test_unknown_location_exits_2_with_suggestions(data_env, capsys):
    assert cli.main(["recommend", "--location", "Mumbai"]) == 2
    assert "Bangalore only" in capsys.readouterr().err


def test_invalid_input_exits_2(data_env, capsys):
    assert cli.main(["recommend", "--location", "Domlur", "--min-rating", "9"]) == 2
    assert "min_rating" in capsys.readouterr().err


def test_missing_data_exits_1(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DINEWISE_DATA_PATH", str(tmp_path / "missing.parquet"))
    get_settings.cache_clear()
    try:
        assert cli.main(["recommend", "--location", "Domlur"]) == 1
        assert "dinewise.ingestion.run" in capsys.readouterr().err
    finally:
        get_settings.cache_clear()


def test_no_results_message(tmp_path, monkeypatch, capsys):
    from .conftest import RESTAURANTS

    path = tmp_path / "r.parquet"
    make_df(rows=[r for r in RESTAURANTS if r[0] == "Dosa Corner"]).to_parquet(path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DINEWISE_DATA_PATH", str(path))
    get_settings.cache_clear()
    try:
        assert cli.main(["recommend", "--location", "Indiranagar", "--budget", "high"]) == 0
        assert "No restaurants matched" in capsys.readouterr().out
    finally:
        get_settings.cache_clear()


def test_llm_flag_needs_a_key(data_env, monkeypatch, capsys):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    assert cli.main(["recommend", "--location", "Domlur", "--llm"]) == 2
    assert "GROQ_API_KEY" in capsys.readouterr().err


def test_llm_flag_shows_ai_explanations(data_env, monkeypatch, capsys):
    from dinewise.domain.models import Recommendation
    from dinewise.service.recommender import RankingOutcome

    class FakeRanker:
        def rank(self, result):
            top = result.candidates[0]
            return RankingOutcome(
                recommendations=[
                    Recommendation(rank=1, restaurant=top, explanation="AI pick.", source="llm")
                ],
                summary="AI summary.",
                model="openai/gpt-oss-120b",
            )

    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    get_settings.cache_clear()
    monkeypatch.setattr(cli, "make_ranker", lambda settings: FakeRanker())
    assert cli.main(["recommend", "--location", "Domlur", "--llm", "--top-k", "1"]) == 0
    out = capsys.readouterr().out
    assert "AI summary." in out and "(AI) AI pick." in out
    assert "llm ranking, model openai/gpt-oss-120b" in out
