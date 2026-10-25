"""DineWise Streamlit frontend (arch §10).

    streamlit run src/dinewise/ui/streamlit_app.py

A thin client over the REST API: start the API first (uvicorn dinewise.api.main:app).
The API base URL comes from DINEWISE_API_URL (default http://127.0.0.1:8000).
IDs in comments refer to docs/edge-case.md.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from dinewise.domain.formatting import format_indian
from dinewise.ui.client import ApiClient, ApiError
from dinewise.ui.view import CSS, card_html, corrections_text, empty_state_tips, md_escape

BUDGET_OPTIONS = ["any", "low", "medium", "high"]
DEFAULTS: dict[str, Any] = {
    # Form inputs
    "location": None,
    "cuisines": [],
    "budget": "any",
    "min_rating": 0.0,
    "extras": "",
    # Request state
    "pending": False,  # a search is queued or running; the button is disabled (UI-15)
    "result": None,  # last successful response; stays visible until the next one (UI-17)
    "query": None,  # the preferences that produced `result`
    "error": None,  # ApiError from the last search, if it failed
    "form_warning": None,
}


@st.cache_resource
def get_client() -> ApiClient:
    return ApiClient()


@st.cache_data(ttl=600, show_spinner=False)
def load_meta(base_url: str) -> dict[str, Any]:
    """Locations, cuisines and budget bands. Failures raise and are not cached."""
    client = get_client()
    return {
        "locations": client.locations(),
        "cuisines": client.cuisines(),
        "budgets": client.budgets(),
    }


@st.cache_data(ttl=30, show_spinner=False)
def llm_ranking_enabled(base_url: str) -> bool | None:
    """Whether the API has an LLM ranker configured; None if it can't be told."""
    try:
        return bool(get_client().health().get("llm_ranking"))
    except ApiError:
        return None


# --- Callbacks (run before the next script run, so they may set widget state) -------------


def start_search() -> None:
    state = st.session_state
    if not state.location:
        state.form_warning = "Choose a location first."
        return
    state.form_warning = None
    state.pending = True


def apply_suggestion(field: str | None, wrong: str | None, suggestion: str) -> None:
    """Swap the value the API didn't recognise for the one the user picked, and search (UI-03)."""
    state = st.session_state
    if field == "location":
        state.location = suggestion
    elif field == "cuisines":
        current = list(state.cuisines)
        wrong_key = (wrong or "").lower()
        replaced = [suggestion if c.lower() == wrong_key else c for c in current]
        if suggestion not in replaced:
            replaced.append(suggestion)
        state.cuisines = list(dict.fromkeys(replaced))[:5]
    start_search()


# --- Page sections ----------------------------------------------------------------------


def build_preferences() -> dict[str, Any]:
    state = st.session_state
    extras = (state.extras or "").strip()
    return {
        "location": state.location,
        "cuisines": list(state.cuisines),
        "budget": None if state.budget == "any" else state.budget,
        "min_rating": float(state.min_rating),
        "extras": extras or None,
    }


def render_form(meta: dict[str, Any]) -> None:
    city = meta["locations"]["city"]
    # Plain names as labels: Streamlit filters on the label, so typing an exact name
    # must match it rather than offer "Add: <name>" first.
    location_options = [city, *(loc["name"] for loc in meta["locations"]["locations"])]
    cuisine_options = [c["name"] for c in meta["cuisines"]["cuisines"]]
    budget_labels = {b["band"]: b["label"] for b in meta["budgets"]["budgets"]}

    with st.sidebar:
        st.header("Your preferences")
        with st.form("preferences", border=False):
            st.selectbox(
                "Location",
                location_options,
                key="location",
                placeholder="Choose or type a neighbourhood",
                help=f"A {city} neighbourhood, or {city} to search the whole city. "
                "Typos are corrected.",
                accept_new_options=True,
            )
            st.multiselect(
                "Cuisines",
                cuisine_options,
                key="cuisines",
                max_selections=5,
                placeholder="Any cuisine (up to 5)",
                accept_new_options=True,
            )
            st.radio(
                "Budget (cost for two)",
                BUDGET_OPTIONS,
                key="budget",
                format_func=str.title,
                captions=["No limit", *(budget_labels.get(b, "") for b in BUDGET_OPTIONS[1:])],
            )
            st.slider(
                "Minimum rating",
                min_value=0.0,
                max_value=5.0,
                step=0.5,
                key="min_rating",
                format="%.1f ★",
                help="0 means any rating, including new places without one.",
            )
            st.text_area(
                "Anything else?",
                key="extras",
                max_chars=300,
                placeholder="family-friendly, quick service",
            )
            st.form_submit_button(
                "Find restaurants",
                type="primary",
                width="stretch",
                on_click=start_search,
                disabled=st.session_state.pending,
            )
        if st.session_state.form_warning:
            st.warning(st.session_state.form_warning)

        llm = llm_ranking_enabled(get_client().base_url)
        if llm is not None:
            st.caption("AI ranking: on" if llm else "AI ranking: off (basic ranking only)")


def run_pending_search() -> None:
    """Send the queued search, store the outcome, then rerun to re-enable the button."""
    state = st.session_state
    prefs = build_preferences()
    llm = llm_ranking_enabled(get_client().base_url)
    message = "Asking the AI to rank the best matches…" if llm else "Finding restaurants…"
    try:
        with st.spinner(message):  # UI-16
            result = get_client().recommend(prefs)
    except ApiError as exc:
        state.error = exc  # the form keeps its values (UI-02)
    else:
        state.result, state.query, state.error = result, prefs, None
    finally:
        state.pending = False
    st.rerun()


def render_error(err: ApiError) -> None:
    if err.unreachable:
        st.error(f"{md_escape(err.message)} Make sure the API is running, then try again.")
    elif err.status == 429:
        st.warning("Too many requests. Please try again in a minute.")  # UI-04
    elif err.status == 422:
        st.error(md_escape(err.message))
        if err.suggestions:
            st.write("Did you mean:")
            cols = st.columns(len(err.suggestions))
            for i, (col, suggestion) in enumerate(zip(cols, err.suggestions, strict=True)):
                col.button(
                    suggestion,
                    key=f"suggestion-{i}",
                    on_click=apply_suggestion,
                    args=(err.field, err.value, suggestion),
                    width="stretch",
                )
    else:
        st.error("Something went wrong on our side. Please try again in a moment.")
    if err.request_id:
        st.caption(f"Request ID: {err.request_id}")


def render_result(result: dict[str, Any], query: dict[str, Any]) -> None:
    recs = result["recommendations"]
    normalized = result.get("normalized_preferences") or {}

    corrected = corrections_text(normalized.get("corrections") or [])
    if corrected:
        st.info(corrected, icon="🔎")  # UI-18

    if result.get("degraded"):
        st.warning(
            "**Basic ranking (AI unavailable).** These picks are ranked by rating and "
            "popularity, and the explanations are generated from the data.",
            icon="⚙️",
        )  # UI-05

    if not recs:
        st.info(md_escape(result.get("summary") or "No restaurants matched."), icon="🍽️")
        city_wide = normalized.get("location_kind") == "city"
        tips = empty_state_tips(query, city_wide=city_wide)
        if tips:
            items = "\n".join(f"- {md_escape(t)}" for t in tips)
            st.markdown(f"**Try this:**\n{items}")  # UI-07
        return

    with st.container(border=True):
        if not result.get("degraded"):
            st.caption("✨ AI-generated summary")
        st.markdown(md_escape(result.get("summary") or ""))

    if result.get("relaxations"):
        items = "\n".join(f"- {md_escape(r)}" for r in result["relaxations"])
        st.warning(f"We relaxed some filters to find enough matches:\n{items}", icon="↔️")

    for rec in recs:  # only what was found; no placeholder cards (UI-08)
        st.markdown(card_html(rec), unsafe_allow_html=True)

    meta = result.get("meta") or {}
    details = [f"{meta.get('candidates_considered', len(recs))} candidates considered"]
    if meta.get("model"):
        details.append(f"ranked by {meta['model']}")
    if meta.get("latency_ms") is not None:
        details.append(f"{format_indian(meta['latency_ms'])} ms")
    st.caption(" · ".join(details))


def main() -> None:
    st.set_page_config(page_title="DineWise", page_icon="🍽️", layout="centered")
    st.markdown(CSS, unsafe_allow_html=True)
    for key, value in DEFAULTS.items():
        st.session_state.setdefault(key, value)

    st.title("🍽️ DineWise")
    st.caption("Restaurant picks for Bengaluru, ranked and explained by AI.")

    client = get_client()
    try:
        meta = load_meta(client.base_url)
    except ApiError as exc:  # UI-01
        st.error(f"Service unavailable: {md_escape(exc.message)}")
        if st.button("Retry"):
            st.rerun()
        st.stop()

    render_form(meta)

    state = st.session_state
    if state.pending:
        run_pending_search()
    if state.error is not None:
        render_error(state.error)
    if state.result is not None:
        render_result(state.result, state.query or {})
    elif state.error is None:
        st.markdown(
            "Choose a location in the sidebar, add any preferences, and press **Find restaurants**."
        )


main()
