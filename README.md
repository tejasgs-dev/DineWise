# DineWise

AI-powered restaurant recommendations, inspired by Zomato. DineWise filters a real restaurant dataset by your preferences (location, budget, cuisine, minimum rating, and free-text extras), then uses an LLM served by [Groq](https://groq.com) (`openai/gpt-oss-120b`) to rank the best matches and explain why each one fits.

> **Status:** Phases 0–5 are built: data ingestion, retrieval, LLM ranking on Groq, the REST API, and the Streamlit UI. Phase 3's paid eval run and tuning are still open. Without a `GROQ_API_KEY`, responses use rule-based ranking and are marked `degraded`. See the [implementation plan](docs/implementation-plan.md) for what comes next.

## Documentation

| Document | Contents |
|---|---|
| [Problem statement](docs/problemStatement.md) | What the project must do |
| [Architecture](docs/architecture.md) | Components, data flow, LLM integration |
| [Data notes](docs/data-notes.md) | What the dataset contains and how it's cleaned |
| [Implementation plan](docs/implementation-plan.md) | Phase-by-phase build plan |
| [Edge cases](docs/edge-case.md) | Corner scenarios and expected behaviour |
| [Eval plan](docs/eval.md) | How the LLM ranking quality is measured |

## Setup

Requires **Python 3.11+**.

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate          # macOS / Linux
.venv\Scripts\activate             # Windows (PowerShell or cmd)

# 2. Install the package with dev tools
pip install -e ".[dev]"

# 3. Configure
cp .env.example .env               # then set GROQ_API_KEY in .env
```

Get a key at [console.groq.com](https://console.groq.com/keys). Without `GROQ_API_KEY`, the app still works but returns rule-based rankings instead of AI explanations.

## Build the restaurant data

```bash
python -m dinewise.ingestion.run            # downloads the dataset (574 MB) on first run
python -m dinewise.ingestion.run --offline  # rebuild from the copy already in data/raw/
```

This writes `data/restaurants.parquet` (about 12,450 cleaned, deduplicated Bangalore restaurants) and `data/meta.json` (neighbourhoods, cuisines, and a build ID). It takes about 15 seconds after the download. The dataset revision is pinned, so rebuilding gives the same result. See [docs/data-notes.md](docs/data-notes.md) for what the cleaning does and why.

## Try it

```bash
python -m dinewise.cli recommend --location Indiranagar --cuisine Italian --budget medium --min-rating 4
python -m dinewise.cli recommend --location koramangla --cuisine chinese --extras "quick, not a bar"
python -m dinewise.cli recommend --location Bangalore --budget high --extras "family buffet" --json
```

Locations are Bangalore neighbourhoods (or "Bangalore" for the whole city). Typos are corrected, and filters are relaxed step by step when too few places match. Each relaxation is reported. Add `--llm` to rank and explain with the LLM (needs `GROQ_API_KEY`); without it, explanations come from templates:

```bash
python -m dinewise.cli recommend --location Indiranagar --cuisine Italian --budget medium --llm
```

## Run the API

```bash
uvicorn dinewise.api.main:app --reload        # http://127.0.0.1:8000, docs at /docs
```

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/recommendations   -H "Content-Type: application/json"   -d '{"location": "Indiranagar", "cuisines": ["Italian"], "budget": "medium", "min_rating": 4, "top_k": 5}'
```

| Endpoint | Purpose |
|---|---|
| `POST /api/v1/recommendations` | Preferences in, ranked picks with explanations out |
| `GET /api/v1/meta/locations` | Searchable neighbourhoods, with restaurant counts |
| `GET /api/v1/meta/cuisines` | Searchable cuisines, with restaurant counts |
| `GET /api/v1/meta/budgets` | Budget bands and their ₹ ranges |
| `GET /healthz` | `200` when the data is loaded, `503` otherwise |

Unknown locations or cuisines return `422` with suggestions. Every response has an `X-Request-ID` header (send your own to trace a request), and logs are one JSON object per line.

## Run the UI

The web frontend is served by the API itself. Start the API (see above) and open **http://127.0.0.1:8000/**. It is a dark-theme single page built from the Google Stitch design in [stitch_dinewise_restaurant_recommender/](stitch_dinewise_restaurant_recommender/) (design tokens in `dinewise_bangalore_nocturne/DESIGN.md`). The source is plain HTML, Tailwind (CDN) and vanilla JavaScript in [src/dinewise/web/](src/dinewise/web/), with no build step; edit the files and reload the page.

It covers the same edge cases as the Streamlit app: typo corrections, "Did you mean" suggestions, relaxed filters, basic ranking when the AI is unavailable, empty results with one-click fixes, rate limiting, and a retry screen when the API has no data.

### Streamlit (legacy)

The original Streamlit app is still available. It is an optional extra (the `dev` extra includes it too). It is a thin client over the API, so start the API first, then in a second terminal:

```bash
pip install -e ".[ui]"
streamlit run src/dinewise/ui/streamlit_app.py   # http://localhost:8501
```

Pick a neighbourhood (or type one; typos are corrected), optionally cuisines, a budget, a minimum rating, and extras such as "family-friendly, quick service", then press **Find restaurants**. Each card shows the name, cuisines, rating, cost for two, locality, and the explanation, labelled "AI-generated" or "Basic ranking". When the LLM is unavailable, a "Basic ranking (AI unavailable)" notice is shown.

The app reads two variables from its own environment (not from `.env`):

| Variable | Default | Purpose |
|---|---|---|
| `DINEWISE_API_URL` | `http://127.0.0.1:8000` | Where the API is running |
| `DINEWISE_UI_TIMEOUT_S` | `60` | How long to wait for a recommendation; keep it above `DINEWISE_LLM_DEADLINE_S` |

Streamlit reloads the app script when it changes, but not the `dinewise` modules it imports. Restart `streamlit run` after editing those.

## Configuration

All settings come from environment variables or `.env` (see [.env.example](.env.example)). They are loaded by `dinewise.config.Settings`.

| Variable | Default | Purpose |
|---|---|---|
| `GROQ_API_KEY` | — | Groq API key (enables LLM ranking) |
| `DINEWISE_MODEL` | `openai/gpt-oss-120b` | Groq model; must support strict JSON-schema output |
| `DINEWISE_FALLBACK_MODEL` | `openai/gpt-oss-20b` | Tried once if the primary model gives no usable answer; empty disables it |
| `DINEWISE_REASONING_EFFORT` | `low` | `low`, `medium`, or `high` |
| `DINEWISE_LLM_MAX_COMPLETION_TOKENS` | `1200` | Output limit (reasoning + JSON answer) |
| `DINEWISE_LLM_DEADLINE_S` | `20` | Time limit for the whole LLM step, including retries and the fallback |
| `DINEWISE_LLM_TIMEOUT_S` | `30` | LLM request timeout, in seconds |
| `DINEWISE_SHORTLIST_SIZE` | `10` | Candidates sent to the LLM (about 115 prompt tokens each) |
| `DINEWISE_MIN_CANDIDATES` | `5` | Fewer matches than this triggers relaxation |
| `DINEWISE_MAX_BRANCHES_PER_CHAIN` | `2` | Max branches of one chain in a shortlist |
| `DINEWISE_FUZZY_MATCH_THRESHOLD` | `85` | Fuzzy-match score (0–100) to accept a misspelled location or cuisine |
| `DINEWISE_BUDGET_LOW_MAX` / `_MEDIUM_MAX` | `400` / `1000` | Budget band thresholds (₹ for two); applied when the data is loaded |
| `DINEWISE_WEIGHT_*` | `0.45 / 0.25 / 0.15 / 0.15` | Pre-score weights: rating, votes, cuisine, tags (must sum to 1) |
| `DINEWISE_CORS_ORIGINS` | `["http://localhost:8501", …]` | Browser origins allowed to call the API (JSON list) |
| `DINEWISE_CORS_ORIGIN_REGEX` | — | Also allow origins matching this regex (e.g. Vercel previews) |
| `DINEWISE_MAX_REQUEST_BYTES` | `16384` | Larger request bodies get `413` |
| `DINEWISE_RATE_LIMIT_PER_MINUTE` | `10` | Recommendation requests per client IP per minute (`429` beyond it); `0` disables |
| `DINEWISE_FORWARDED_HOPS` | `0` | Trusted proxies appending to `X-Forwarded-For` (`1` on Render); finds the client IP |
| `DINEWISE_LOG_LEVEL` / `_LOG_FORMAT` | `INFO` / `json` | Logging level; `json` or `text` lines |
| `DINEWISE_DATA_PATH` | `data/restaurants.parquet` | Cleaned dataset |
| `DINEWISE_META_PATH` | `data/meta.json` | Localities and cuisines for the UI |
| `DINEWISE_RAW_DIR` | `data/raw` | Where the raw CSV is downloaded |
| `DINEWISE_HF_DATASET` / `_HF_REVISION` | Zomato dataset / `5738e9e…` | Source dataset and pinned revision (empty = latest) |

Invalid values (for example an unknown reasoning effort, or weights that don't sum to 1) stop the app at startup with a clear error.

The LLM defaults are sized for Groq's free tier for `openai/gpt-oss-120b` (30 requests and 8K tokens per minute, 1K requests and 200K tokens per day). A typical search uses about 2K prompt and 0.9K completion tokens, so roughly 2 searches a minute and 70 a day fit. Raising the shortlist size, reasoning effort, or completion limit costs more of that budget.

## Development

```bash
pytest                 # unit + integration tests
pytest --cov           # with coverage
ruff check .           # lint
ruff format .          # format
```

The LLM eval suite in `tests/eval/` calls the real Groq API and costs money (well under $1 for a full run), so `pytest` does not run it:

```bash
python -m tests.eval.run_eval --oracle         # check the graders, offline
python -m tests.eval.run_eval --smoke          # 1 case per category (paid, tiny)
python -m tests.eval.run_eval --reps 3         # full run; asks before spending
python -m tests.eval.snapshot_candidates       # refresh frozen shortlists after re-ingesting
```

See [docs/eval.md](docs/eval.md).

## Project layout

```
src/dinewise/
├── config.py      # Settings
├── cli.py         # dev CLI: python -m dinewise.cli recommend ...
├── ingestion/     # dataset -> cleaned Parquet        (Phase 1)
├── domain/        # Restaurant, UserPreferences       (Phases 1–2)
├── retrieval/     # filters, relaxation, scoring      (Phase 2)
├── llm/           # prompts, schema, validator, Groq ranker
├── service/       # Recommender: orchestration + fallback
├── api/           # FastAPI app, routes, schemas      (Phase 4)
├── ui/            # Streamlit app + API client         (Phase 5)
└── web/           # browser frontend served at "/"
tests/
├── unit/
├── integration/
└── eval/          # paid LLM evals, run manually
```
