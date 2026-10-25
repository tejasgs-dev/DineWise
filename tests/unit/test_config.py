from pathlib import Path

import pytest
from pydantic import ValidationError

from dinewise.config import Settings, get_settings

DINEWISE_VARS = [
    "GROQ_API_KEY",
    "DINEWISE_FALLBACK_MODEL",
    "DINEWISE_LLM_DEADLINE_S",
    "DINEWISE_LLM_MAX_COMPLETION_TOKENS",
    "DINEWISE_MODEL",
    "DINEWISE_REASONING_EFFORT",
    "DINEWISE_LLM_TIMEOUT_S",
    "DINEWISE_SHORTLIST_SIZE",
    "DINEWISE_MIN_CANDIDATES",
    "DINEWISE_BUDGET_LOW_MAX",
    "DINEWISE_BUDGET_MEDIUM_MAX",
    "DINEWISE_WEIGHT_RATING",
    "DINEWISE_WEIGHT_VOTES",
    "DINEWISE_WEIGHT_CUISINE",
    "DINEWISE_WEIGHT_TAGS",
    "DINEWISE_DATA_PATH",
    "DINEWISE_META_PATH",
    "DINEWISE_RAW_DIR",
    "DINEWISE_HF_FILENAME",
    "DINEWISE_HF_DATASET",
    "DINEWISE_HF_REVISION",
]


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch, tmp_path):
    """Run each test with no DineWise env vars and no .env file in the working dir."""
    for var in DINEWISE_VARS:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_defaults():
    s = Settings()
    assert s.model == "openai/gpt-oss-120b"
    assert s.fallback_model == "openai/gpt-oss-20b"
    assert s.reasoning_effort == "low"
    assert s.llm_max_completion_tokens == 1200
    assert s.llm_deadline_s == 20
    assert s.llm_timeout_s == 30
    assert s.shortlist_size == 10
    assert s.min_candidates == 5
    assert (s.budget_low_max, s.budget_medium_max) == (400, 1000)
    assert s.data_path == Path("data/restaurants.parquet")
    assert s.raw_dir == Path("data/raw")
    assert s.hf_revision == "5738e9eda2fad49ad51c6e0ed26e761d9b947133"
    assert s.groq_api_key is None
    assert s.llm_enabled is False


def test_scoring_weights_default():
    w = Settings().scoring_weights
    assert (w.rating, w.votes, w.cuisine, w.tags) == (0.45, 0.25, 0.15, 0.15)


def test_env_vars_override_defaults(monkeypatch):
    monkeypatch.setenv("DINEWISE_MODEL", "openai/gpt-oss-20b")
    monkeypatch.setenv("DINEWISE_REASONING_EFFORT", "high")
    monkeypatch.setenv("DINEWISE_SHORTLIST_SIZE", "15")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    s = Settings()
    assert s.model == "openai/gpt-oss-20b"
    assert s.reasoning_effort == "high"
    assert s.fallback_model is None  # same as the primary model, so no fallback
    assert s.shortlist_size == 15
    assert s.llm_enabled is True
    assert s.groq_api_key.get_secret_value() == "gsk-test"


def test_dotenv_file_is_loaded(tmp_path):
    (tmp_path / ".env").write_text(
        "DINEWISE_MODEL=from-dotenv\nDINEWISE_BUDGET_LOW_MAX=400\nGROQ_API_KEY=gsk-dotenv\n",
        encoding="utf-8",
    )
    s = Settings()
    assert s.model == "from-dotenv"
    assert s.budget_low_max == 400
    assert s.llm_enabled is True


def test_env_var_wins_over_dotenv(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("DINEWISE_MODEL=from-dotenv\n", encoding="utf-8")
    monkeypatch.setenv("DINEWISE_MODEL", "from-env")
    assert Settings().model == "from-env"


def test_blank_api_key_means_llm_disabled(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "  ")
    assert Settings().llm_enabled is False


def test_blank_revision_becomes_none(monkeypatch):
    monkeypatch.setenv("DINEWISE_HF_REVISION", "")
    assert Settings().hf_revision is None


def test_api_key_is_not_exposed_in_repr(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "gsk-secret-value")
    s = Settings()
    assert "gsk-secret-value" not in repr(s)
    assert "gsk-secret-value" not in str(s.model_dump())


# --- Validation (edge cases OPS-01, SCR-09) ----------------------------------


def test_invalid_reasoning_effort_rejected(monkeypatch):
    monkeypatch.setenv("DINEWISE_REASONING_EFFORT", "max")
    with pytest.raises(ValidationError):
        Settings()


@pytest.mark.parametrize("low,medium", [(1000, 1000), (2000, 1000)])
def test_budget_bands_must_be_ordered(monkeypatch, low, medium):
    monkeypatch.setenv("DINEWISE_BUDGET_LOW_MAX", str(low))
    monkeypatch.setenv("DINEWISE_BUDGET_MEDIUM_MAX", str(medium))
    with pytest.raises(ValidationError, match="budget_low_max"):
        Settings()


def test_weights_must_sum_to_one(monkeypatch):
    monkeypatch.setenv("DINEWISE_WEIGHT_RATING", "0.9")
    with pytest.raises(ValidationError, match="sum to 1.0"):
        Settings()


def test_negative_shortlist_rejected(monkeypatch):
    monkeypatch.setenv("DINEWISE_SHORTLIST_SIZE", "-1")
    with pytest.raises(ValidationError):
        Settings()


def test_min_candidates_cannot_exceed_shortlist(monkeypatch):
    monkeypatch.setenv("DINEWISE_SHORTLIST_SIZE", "5")
    monkeypatch.setenv("DINEWISE_MIN_CANDIDATES", "10")
    with pytest.raises(ValidationError, match="min_candidates"):
        Settings()


def test_get_settings_is_cached():
    assert get_settings() is get_settings()


def test_blank_fallback_model_disables_fallback(monkeypatch):
    monkeypatch.setenv("DINEWISE_FALLBACK_MODEL", "")
    assert Settings().fallback_model is None
