import json
import logging

from dinewise.domain.models import NormalizedPreferences, RetrievalResult
from dinewise.logging_setup import JsonFormatter, configure_logging, request_id_var
from dinewise.retrieval.explain import (
    heuristic_recommendations,
    search_description,
    template_summary,
)
from dinewise.retrieval.retriever import CandidateRetriever
from dinewise.service.recommender import Recommender


def prefs(**kw) -> NormalizedPreferences:
    base = dict(
        location="Domlur", location_kind="locality", city="Bangalore", localities=["Domlur"]
    )
    return NormalizedPreferences(**{**base, **kw})


def test_search_description():
    assert search_description(prefs()) == "in Domlur"
    assert (
        search_description(prefs(cuisines=["Thai", "Italian"], budget="low", min_rating=4))
        == "in Domlur for Thai or Italian, low budget, rated 4+"
    )


def test_template_summary_mentions_relaxation(repo, settings):
    result = CandidateRetriever(repo, settings).retrieve(prefs(cuisines=["Japanese"]))
    assert result.relaxations  # only one Japanese place in Domlur
    text = template_summary(result, heuristic_recommendations(result, settings))
    assert text.startswith("Top picks in Domlur for Japanese: ")
    assert text.endswith("Some filters were relaxed to find enough matches.")


def test_recommender_without_ranker_is_degraded(repo, settings):
    from dinewise.domain.models import UserPreferences

    response = Recommender(repo, settings).recommend(UserPreferences(location="Domlur"))
    assert response.degraded and response.meta.ranking == "heuristic"
    assert len(response.recommendations) == 5


def test_template_summary_when_nothing_matched():
    empty = RetrievalResult(preferences=prefs(budget="high"), candidates=[], scores={})
    assert template_summary(empty, []).startswith("No restaurants matched in Domlur, high budget")


def test_json_formatter_outside_a_request():
    record = logging.makeLogRecord(
        {"msg": "hello %s", "args": ("world",), "levelname": "INFO", "name": "x"}
    )
    line = json.loads(JsonFormatter().format(record))
    assert line["msg"] == "hello world" and "request_id" not in line


def test_log_records_carry_request_id():
    configure_logging("INFO", "json")
    factory = logging.getLogRecordFactory()
    token = request_id_var.set("req-42")
    try:
        assert factory("n", logging.INFO, "f", 1, "m", (), None).request_id == "req-42"
    finally:
        request_id_var.reset(token)
    assert factory("n", logging.INFO, "f", 1, "m", (), None).request_id is None


def test_configure_logging_is_idempotent():
    configure_logging("INFO", "json")
    configure_logging("DEBUG", "json")
    logger = logging.getLogger("dinewise")
    assert [h.get_name() for h in logger.handlers].count("dinewise-json") == 1
    assert logger.level == logging.DEBUG and logger.propagate is False
    configure_logging("INFO", "text")
    assert [h.get_name() for h in logger.handlers].count("dinewise-json") == 0
    assert logger.propagate is True
