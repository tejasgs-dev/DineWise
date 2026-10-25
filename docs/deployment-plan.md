# DineWise — Deployment Plan

**Target:** the FastAPI backend on **Render** (Python web service) and the static web frontend on **Vercel**.

```
Browser ──► Vercel  (static: index.html, config.js, app.js, images)
   │
   └─fetch─► Render  (FastAPI/Uvicorn: /api/v1/*, /healthz) ──► Groq API
                └── data/restaurants.parquet (committed; loaded into memory at startup)
```

The frontend in `src/dinewise/web/` is plain HTML and JS (Tailwind from a CDN, no bundler). On Vercel it calls the API across origins, so the API's CORS list must include the Vercel domain. The API still serves the same page itself at `/`, which is useful for local development and as a fallback.

---

## Status

**Done in the code:**

- [x] The frontend can call an API on another origin (`config.js` + `DINEWISE_API_BASE`).
- [x] `vercel.json`: builds `config.js` from an environment variable and maps the `/static/*` paths.
- [x] `render.yaml` blueprint with a health check.
- [x] CORS: a fixed origin list, plus an optional regex for Vercel preview URLs.
- [x] Per-IP rate limiting on `/api/v1/recommendations`. Without it, anyone with the URL could use up the Groq quota (SEC-09). The client IP is read safely behind Render's proxy (SEC-10).
- [x] Streamlit is an optional extra, so the Render build skips it.
- [x] `.gitignore` lets `data/restaurants.parquet` be committed.

**Still to do (you):**

1. §2 Give DineWise its own Git repo, commit (including the Parquet file), and push to GitHub.
2. §3 Create the Render service.
3. §4 Create the Vercel project.
4. §5 Connect the two (CORS), then run the end-to-end checks.

---

## 1. Deployment files in the repo

| File | Purpose |
|---|---|
| [`render.yaml`](../render.yaml) | Render Blueprint: build and start commands, health check, environment variables |
| [`src/dinewise/web/vercel.json`](../src/dinewise/web/vercel.json) | Vercel config: build command, output directory, `/static` rewrite, image caching |
| [`src/dinewise/web/config.js`](../src/dinewise/web/config.js) | `window.DINEWISE_API_BASE`. Empty (same origin) in the repo; rewritten by the Vercel build |
| [`src/dinewise/api/ratelimit.py`](../src/dinewise/api/ratelimit.py) | Sliding-window limiter (in process) and the trusted-proxy client IP lookup |
| [`data/restaurants.parquet`](../data/) | The built dataset (3.5 MB, ~12.5K restaurants). The API reads it at startup |
| [`.env.example`](../.env.example) | Every setting with its default |

How the frontend finds the API: `index.html` loads `/static/config.js` and then `/static/app.js`. `app.js` uses `API_BASE = window.DINEWISE_API_BASE` (with any trailing `/` removed). On Vercel the build command writes `config.js` from the `DINEWISE_API_BASE` environment variable, and the rewrite maps `/static/:path*` to `/:path*`, because the web folder is the site root there.

---

## 2. Own repository and first push

Right now the Git root is `C:/Projects` (the unrelated `git-learning-test` repo), and DineWise is untracked inside it. Render and Vercel deploy from a GitHub repo, so give DineWise its own:

```bash
cd C:/Projects/DineWise
git init -b main
git check-ignore .env data/raw/zomato.csv   # must print both paths
git add .
git status                                  # data/restaurants.parquet listed; .env, .venv/, data/raw/ not listed
git commit -m "DineWise: initial commit"
gh repo create dinewise --private --source . --push   # or create the repo on GitHub, then add the remote and push
```

Checks before the push:

- [ ] `.env` is **not** staged. It holds the real `GROQ_API_KEY`.
- [ ] `data/raw/zomato.csv` (574 MB) is **not** staged.
- [ ] `data/restaurants.parquet` **is** staged. Without it, `/healthz` returns 503 and Render won't make the deploy live.
- [ ] Optional: leave out `stitch_dinewise_restaurant_recommender/` (design mockups) if you don't want it in the repo.

**Why ship the Parquet file instead of building it on Render:** building it downloads a 574 MB CSV from Hugging Face and loads it into pandas. That is slow, probably too much memory for the free tier (512 MB), and fails whenever Hugging Face is down (OPS-04). `meta.json` isn't read by the API, so it stays ignored.

> Alternative: keep a monorepo and set **Root Directory** to `DineWise` on Render and `DineWise/src/dinewise/web` on Vercel. Everything else in this plan stays the same.

---

## 3. Backend on Render

### 3.1 Create the service from the blueprint

1. Render dashboard → **New → Blueprint**, connect GitHub, and select the repo. Render reads `render.yaml`.
2. Enter **`GROQ_API_KEY`** when prompted. It is `sync: false`, so it is never stored in the repo.
3. Leave `DINEWISE_CORS_ORIGINS` as the placeholder for now. You fix it in §5 once you know the Vercel domain.
4. Apply. The first build installs the package (`pip install .`), starts Uvicorn, and waits for `/healthz` to return 200.

What `render.yaml` sets:

| Setting | Value | Why |
|---|---|---|
| Plan / region | `free` / `singapore` | Closest Render region to Bengaluru. See §7 for the free-tier trade-offs |
| Build | `pip install --upgrade pip && pip install .` | Installs the API and its web assets; Streamlit isn't installed |
| Start | `uvicorn dinewise.api.main:app --host 0.0.0.0 --port $PORT` | One worker, which suits 512 MB |
| Health check | `/healthz` | Returns 503 if the data didn't load, so a broken deploy never replaces a working one |
| `PYTHON_VERSION` | `3.12.10` | Matches local development (the project needs ≥ 3.11) |
| `GROQ_API_KEY` | *(secret, set in the dashboard)* | Without it the API still works, but every result is rule-based and marked `degraded` |
| `DINEWISE_CORS_ORIGINS` | `["https://<your-app>.vercel.app"]` | JSON list of exact origins (§5) |
| `DINEWISE_FORWARDED_HOPS` | `1` | Render's proxy appends the real client IP to `X-Forwarded-For`. The rate limiter uses that entry and ignores client-supplied ones |
| `DINEWISE_RATE_LIMIT_PER_MINUTE` | `10` | Recommendation requests per client IP per minute. Extra requests get `429` with a `Retry-After` header, and the page shows "Too many requests" |
| `DINEWISE_LOG_FORMAT` / `_LOG_LEVEL` | `json` / `INFO` | Structured logs in Render → Logs; every line carries a request ID |

Every other setting has a working default (see `.env.example`). Add an environment variable only to change one.

> No Blueprint? **New → Web Service** with the same build command, start command, health check path and environment variables works too.

### 3.2 Verify the API

```bash
API=https://dinewise-api.onrender.com   # your service URL

curl -s $API/healthz
# {"status":"ok","restaurants":12449,"build_id":"d2f323aca9acf327","llm_ranking":true,"detail":null}

curl -s -X POST $API/api/v1/recommendations -H "Content-Type: application/json" \
  -d '{"location":"Indiranagar","cuisines":["Italian"],"budget":"high","min_rating":4}' | head -c 400
```

- `restaurants: 12449` means the Parquet file shipped.
- `llm_ranking: true` means the Groq key is set.
- Results with `"source": "llm"` mean the key works (a bad key or quota problem shows up as `degraded`).

---

## 4. Frontend on Vercel

1. Vercel dashboard → **Add New → Project**, then import the repo.
2. Configure:

   | Setting | Value |
   |---|---|
   | Framework preset | **Other** |
   | Root directory | `src/dinewise/web` |
   | Build command / output directory | Leave empty; `vercel.json` sets them |
   | Environment variable | `DINEWISE_API_BASE` = `https://dinewise-api.onrender.com` (no trailing slash). Set it for **Production**, and for **Preview** if you use §5.2 |

3. Deploy, and note the production domain (for example `https://dinewise.vercel.app`, or a custom domain).

Check that `https://<your-app>.vercel.app/config.js` contains your Render URL. If it shows `""`, the environment variable wasn't set for that environment. Set it and redeploy, because the variable is read at build time.

---

## 5. Connect them (CORS) and test end to end

### 5.1 Production origin

In Render → the service → **Environment**, set:

```
DINEWISE_CORS_ORIGINS=["https://dinewise.vercel.app"]
```

Use the exact origin: the scheme, no trailing slash, and no path. If you add a custom domain, list it as well (`["https://dinewise.vercel.app","https://dinewise.example.com"]`). Saving redeploys the API. To keep the blueprint in sync, update the value in `render.yaml` too.

### 5.2 Optional: Vercel preview deployments

Preview URLs change on every deploy, so add a regex as well:

```
DINEWISE_CORS_ORIGIN_REGEX=^https://dinewise(-[a-z0-9-]+)?\.vercel\.app$
```

Adjust the prefix to your Vercel project name.

### 5.3 End-to-end checks

```bash
# CORS preflight: expect access-control-allow-origin: https://dinewise.vercel.app
curl -si -X OPTIONS $API/api/v1/recommendations \
  -H "Origin: https://dinewise.vercel.app" \
  -H "Access-Control-Request-Method: POST" \
  -H "Access-Control-Request-Headers: content-type" | grep -i access-control

# Rate limit: expect ten 200s, then 429s
for i in $(seq 1 12); do
  curl -s -o /dev/null -w "%{http_code} " -X POST $API/api/v1/recommendations \
    -H "Content-Type: application/json" -d '{"location":"HSR","top_k":1}'
done; echo

# A forged X-Forwarded-For must NOT get a fresh allowance: still 429
curl -s -o /dev/null -w "%{http_code}\n" -X POST $API/api/v1/recommendations \
  -H "Content-Type: application/json" -H "X-Forwarded-For: 6.6.6.6" -d '{"location":"HSR","top_k":1}'
```

> The rate-limit check uses about 10 Groq calls. If the forged request gets `200`, or two different networks (for example a laptop and a phone on mobile data) share one limit, `DINEWISE_FORWARDED_HOPS` doesn't match Render's proxy chain. Look at the `X-Forwarded-For` header the app receives and adjust the value.

In the browser, on the Vercel URL:

- [ ] The page loads, and the location, cuisine and budget dropdowns fill in (from `/api/v1/meta/*`).
- [ ] A Quick Start search returns cards with "Namma AI" explanations, not the "Basic ranking" banner.
- [ ] DevTools → Network: API calls go to `onrender.com` with no CORS errors, and `/static/app.js`, `/static/config.js` and the images return 200.
- [ ] A misspelled location (for example "Indranagar") shows the "Did you mean" suggestion.
- [ ] Searching quickly more than 10 times in a minute shows "Too many requests. Please try again in a minute."

---

## 6. Day-to-day

- **Deploys:** every push to `main` redeploys both services (Render `autoDeploy: true`, plus Vercel's Git integration). Vercel builds a preview for each branch and pull request.
- **Data updates:** run `python -m dinewise.ingestion.run --offline` locally, check the result, and commit `data/restaurants.parquet`. `/healthz` → `build_id` shows which build is live.
- **Settings:** change environment variables in the Render dashboard (this redeploys) and mirror the change in `render.yaml`. Changing `DINEWISE_API_BASE` on Vercel needs a redeploy.
- **Local development is unchanged:** `uvicorn dinewise.api.main:app --reload`, then open http://127.0.0.1:8000. `config.js` is empty, so the page uses the local API.

---

## 7. Risks and production considerations

| Risk | Impact | Mitigation |
|---|---|---|
| **Free tier sleeps after ~15 min idle** | The first request after a sleep takes ~30–60 s, and the page gives up at 60 s (`REQUEST_TIMEOUT_MS`). A cold start plus a slow LLM call can fail. | The page calls `/healthz` on load, which starts waking the API while the user fills in the form. For a demo you share, use the **Starter** plan (always on) or an external uptime ping on `/healthz` every ~10 min. |
| **Groq quota or cost** | The free tier allows ~8K tokens per minute for `gpt-oss-120b` (~3K per request). Several users at once can hit it, and those results fall back to `degraded` (rule-based) ranking. | The per-IP limit (10/min) stops one client from using it all. Set a spend limit in the Groq console, and lower `DINEWISE_RATE_LIMIT_PER_MINUTE` if needed. |
| **Rate limit is per instance** | With more than one instance, each keeps its own count (OPS-05). | Fine on one instance. If you scale out, move the counts to a shared store (Redis). |
| **Memory (512 MB free tier)** | pandas, pyarrow and NumPy plus ~12.5K rows. | Check Render → Metrics after the first deploy (OPS-06). Keep one Uvicorn worker. |
| **Secrets** | A leaked `GROQ_API_KEY`. | It is set only in the Render dashboard. It never goes in `render.yaml`, `vercel.json`, `config.js` or the repo. The frontend holds no secrets. |
| **CORS mismatch** | The page loads, but every API call fails with a CORS error. | The origin must match exactly. Use the preflight check in §5.3. |
| **Tailwind from a CDN** | A console warning, and a heavier first load. | Acceptable for v1. Later, precompile with the Tailwind CLI in the Vercel build command. |

---

## 8. Rollback

- **Render:** Dashboard → the service → **Events** → choose an earlier deploy → **Rollback**. The `/healthz` health check already stops a build whose data didn't load from going live.
- **Vercel:** **Deployments** → choose an earlier deployment → **Promote to Production** (instant).
- The two deploy independently. If an API change breaks the page, roll back Render first. The frontend only depends on the `/api/v1` contract.
