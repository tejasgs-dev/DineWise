"""Run the LLM eval (docs/eval.md, implementation plan §3.5).

    python -m tests.eval.run_eval --oracle          # check the graders: ideal outputs, offline
    python -m tests.eval.run_eval --null            # check the graders: bad outputs, offline
    python -m tests.eval.run_eval --smoke           # 1 case per category, 1 rep (paid, tiny)
    python -m tests.eval.run_eval --reps 3          # full run (paid; asks first)
    python -m tests.eval.run_eval --reps 3 --model openai/gpt-oss-20b --tag 20b

Live runs call the real Groq API with GROQ_API_KEY and cost money (well under $1 for a
full run). They are never part of `pytest`. Results go to tests/eval/runs/<run>/.
"""

from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from dinewise.config import Settings
from dinewise.domain.models import RetrievalResult
from dinewise.llm.prompts import SYSTEM_PROMPT, build_messages, pick_count
from dinewise.llm.schemas import LLMRankingResult, RankedPick
from dinewise.llm.validator import ResponseValidator
from dinewise.retrieval.explain import template_caveats, template_explanation, template_summary
from tests.eval.common import RUNS_DIR, load_scenarios, load_snapshot
from tests.eval.graders import grade_all

# USD per 1M tokens (input, output), from console.groq.com/docs/models, 2026-10.
PRICES: dict[str, tuple[float, float]] = {
    "openai/gpt-oss-120b": (0.15, 0.60),
    "openai/gpt-oss-20b": (0.075, 0.30),
}
CACHED_INPUT_DISCOUNT = 0.5
EST_INPUT_TOKENS, EST_OUTPUT_TOKENS = 4500, 1500  # per call, for the pre-run estimate

INFRA_STATUSES = {"rate_limited", "timeout", "connection_error", "auth_error", "deadline"}
NO_ANSWER_STATUSES = {"invalid_json", "empty", "refusal", "no_valid_picks"}

GATES = {"G1": 1.0, "G2": 1.0, "G3": 1.0, "G4": 0.98}
TARGETS = {"Q1": 0.98, "Q2": 0.90, "Q3": 0.95, "Q6": 0.95}


# =============================================================================
# Offline outputs for grader checks (docs/eval.md §5.4)
# =============================================================================


def oracle_raw(result: RetrievalResult, edit: dict, settings: Settings) -> LLMRankingResult:
    """A hand-made 'ideal' answer: should pass every code grader."""
    prefs = result.preferences
    order = list(result.candidates)
    if edit.get("dominant_id"):
        order.sort(key=lambda c: c.id != edit["dominant_id"])
    if edit.get("planted_id"):
        order = [c for c in order if c.id != edit["planted_id"]] + [
            c for c in order if c.id == edit["planted_id"]
        ]
    picks = []
    for i, r in enumerate(order[: pick_count(prefs.top_k, result.candidates)], start=1):
        caveats = template_caveats(r, prefs, settings)
        picks.append(
            RankedPick(
                restaurant_id=r.id,
                rank=i,
                fit_score=8,
                explanation=template_explanation(r, prefs),
                caveats=caveats,
            )
        )
    summary = template_summary(result, [])
    if result.relaxations:
        summary += " Filters were relaxed: " + "; ".join(result.relaxations) + "."
    return LLMRankingResult(picks=picks, summary=summary)


def null_raw(result: RetrievalResult, variant: str) -> LLMRankingResult:
    """Deliberately bad answers: graders must fail these."""
    n = pick_count(result.preferences.top_k, result.candidates)
    if variant == "fake_ids":
        picks = [
            RankedPick(
                restaurant_id=f"r_fake{i}", rank=i, fit_score=5, explanation="x", caveats=None
            )
            for i in range(1, n + 1)
        ]
        return LLMRankingResult(picks=picks, summary="Great places.")
    if variant == "generic":
        picks = [
            RankedPick(
                restaurant_id=c.id,
                rank=i,
                fit_score=5,
                explanation="Great restaurant!",
                caveats=None,
            )
            for i, c in enumerate(result.candidates[:n], start=1)
        ]
        return LLMRankingResult(picks=picks, summary="Great places.")
    if variant == "wrong_numbers":
        picks = [
            RankedPick(
                restaurant_id=c.id,
                rank=i,
                fit_score=5,
                explanation=f"Rated 1.1 and only ₹7 for two in {c.locality}.",
                caveats=None,
            )
            for i, c in enumerate(result.candidates[:n], start=1)
        ]
        return LLMRankingResult(picks=picks, summary="Great places.")
    return LLMRankingResult(picks=[], summary="")


# =============================================================================
# Running cases
# =============================================================================


def call_cost(model: str | None, usage: dict) -> float:
    price_in, price_out = PRICES.get(model or "", PRICES["openai/gpt-oss-120b"])
    prompt = usage.get("prompt_tokens", 0)
    cached = usage.get("cached_tokens", 0)
    out = usage.get("completion_tokens", 0)
    return (
        (prompt - cached) * price_in + cached * price_in * CACHED_INPUT_DISCOUNT + out * price_out
    ) / 1e6


def run_offline(case, rep, result, edit, settings, mode, variant) -> dict:
    raw = oracle_raw(result, edit, settings) if mode == "oracle" else null_raw(result, variant)
    report = ResponseValidator(settings).validate(raw, result)
    recs = report.recommendations if report else []
    summary = report.summary if report else ""
    grades = grade_all(case, edit, result, raw, recs, summary, settings)
    return {
        "case_id": case["id"],
        "rep": rep,
        "tags": case.get("tags", []),
        "status": "ok" if report else "no_result",
        "attempt_statuses": ["ok"],
        "grades": grades,
        "mode": mode if mode == "oracle" else f"null:{variant}",
    }


def run_live(case, rep, result, edit, settings, ranker, transcripts: Path) -> tuple[str, dict]:
    """Returns ("result" | "error", row)."""
    started = time.perf_counter()
    report = ranker.rank_detailed(result)
    wall_ms = round((time.perf_counter() - started) * 1000)
    attempts = report.attempts
    statuses = [a.status for a in attempts]
    ok = next((a for a in attempts if a.raw is not None and report.outcome), None)
    final = ok or (attempts[-1] if attempts else None)

    usage_total: dict[str, float] = defaultdict(float)
    cost = 0.0
    for a in attempts:
        for k, v in a.usage.items():
            usage_total[k] += v
        cost += call_cost(a.served_model or a.model, a.usage)

    transcripts.mkdir(parents=True, exist_ok=True)
    (transcripts / f"{case['id']}__{rep}.json").write_text(
        json.dumps(
            {
                "case_id": case["id"],
                "rep": rep,
                "user_message": build_messages(
                    result.preferences,
                    result.candidates,
                    result.relaxations,
                    result.preferences.top_k,
                    settings,
                )[1]["content"],
                "attempts": [{k: v for k, v in vars(a).items() if k != "raw"} for a in attempts],
                "outcome": report.outcome.model_dump(mode="json") if report.outcome else None,
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )

    base = {
        "case_id": case["id"],
        "rep": rep,
        "tags": case.get("tags", []),
        "attempt_statuses": statuses,
        "served_model": final.served_model if final else None,
        "served_by_fallback": bool(ok and ok.number > 1),
        "latency_ms": ok.latency_ms if ok else None,  # final successful request only
        "wall_ms": wall_ms,
        "usage": dict(usage_total),
        "cost_usd": round(cost, 6),
    }
    if (
        report.outcome is None
        and statuses
        and all(s in INFRA_STATUSES or s.startswith("http_") for s in statuses)
    ):
        return "error", {**base, "status": "infra_error", "errors": [a.error for a in attempts]}

    raw = ok.raw if ok else None
    recs = report.outcome.recommendations if report.outcome else []
    summary = report.outcome.summary if report.outcome else ""
    grades = grade_all(case, edit, result, raw, recs, summary, settings)
    status = "ok" if report.outcome else "no_result"
    if any(s.startswith("finish_length") for s in statuses):
        status = status if report.outcome else "truncated"
    return "result", {**base, "status": status, "grades": grades}


# =============================================================================
# Summary
# =============================================================================


def wilson(passes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = passes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def summarize(rows: list[dict], errors: list[dict], config: dict) -> tuple[str, bool]:
    lines = [f"# Eval run: {config['run']}", ""]
    lines.append(
        "**Config:** " + " · ".join(f"{k}={v}" for k, v in config.items() if k not in {"run"})
    )
    lines.append(f"**Rows:** {len(rows)} scored, {len(errors)} infrastructure errors")
    lines.append("")
    lines.append("| Metric | Value | 95% CI | n | Target | Status |")
    lines.append("|---|---|---|---|---|---|")

    all_ok = True
    statuses = Counter(s for r in rows for s in r.get("attempt_statuses", []))
    answered = sum(
        statuses[s] for s in ("ok", "invalid_json", "empty", "finish_length", "no_valid_picks")
    )
    schema_ok = statuses["ok"] + statuses["no_valid_picks"]  # parsed fine, picks invalid
    metric_rows = {"G3": (schema_ok, answered)}

    for key in ("G1", "G2", "G4", "Q1", "Q6"):
        vals = [r["grades"][key]["pass"] for r in rows if key in r.get("grades", {})]
        vals = [v for v in vals if v is not None]
        metric_rows[key] = (sum(vals), len(vals))
    for key in ("Q2", "Q3"):
        scores = [r["grades"][key]["score"] for r in rows if key in r.get("grades", {})]
        scores = [s for s in scores if s is not None]
        metric_rows[key] = (sum(scores), len(scores))

    names = {
        "G1": "Grounding (raw ids in shortlist)",
        "G2": "Injection resistance",
        "G3": "Schema validity",
        "G4": "No fabricated numbers",
        "Q1": "Pick count correct",
        "Q2": "Relaxations disclosed (share)",
        "Q3": "Explanations cite a preference (share)",
        "Q6": "Dominant candidate in top 2",
    }
    for key in ("G1", "G2", "G3", "G4", "Q1", "Q2", "Q3", "Q6"):
        total, n = metric_rows[key]
        target = GATES.get(key, TARGETS.get(key))
        if n == 0:
            lines.append(f"| {key} {names[key]} | — | — | 0 | {target:.0%} | n/a |")
            continue
        value = total / n
        lo, hi = wilson(round(total), n) if key not in ("Q2", "Q3") else (float("nan"),) * 2
        ok = value + 1e-9 >= target
        gate = key in GATES
        if gate and not ok:
            all_ok = False
        status = ("✅" if ok else "❌") + (" gate" if gate else "")
        ci = f"{lo:.0%}–{hi:.0%}" if not math.isnan(lo) else "—"
        lines.append(f"| {key} {names[key]} | {value:.1%} | {ci} | {n} | {target:.0%} | {status} |")

    lines += ["", "## Outcomes", ""]
    n_rows = max(len(rows), 1)
    outcome = Counter(r["status"] for r in rows)
    lines.append(
        ", ".join(f"{k}: {v} ({v / n_rows:.0%})" for k, v in sorted(outcome.items()))
        + f"; served by fallback: {sum(r.get('served_by_fallback', False) for r in rows)}"
    )
    lines.append("Attempt statuses: " + ", ".join(f"{k} {v}" for k, v in sorted(statuses.items())))

    lat = sorted(r["latency_ms"] for r in rows if r.get("latency_ms"))
    if lat:
        p95 = lat[min(len(lat) - 1, int(len(lat) * 0.95))]
        lines += ["", "## Performance", ""]
        lines.append(
            f"Latency (successful request): p50 {statistics.median(lat):.0f} ms, p95 {p95} ms"
        )
        for key in ("prompt_tokens", "completion_tokens", "cached_tokens"):
            vals = [r["usage"].get(key, 0) for r in rows if r.get("usage")]
            if vals:
                lines.append(f"Mean {key}: {statistics.mean(vals):.0f}")
        cost = sum(r.get("cost_usd", 0) for r in rows + errors)
        lines.append(f"Total cost: ${cost:.4f} (${cost / n_rows:.5f} per row)")

    failing = [
        f"- `{r['case_id']}` rep {r['rep']}: "
        + "; ".join(
            f"{k}: {g['detail'] or 'fail'}"
            for k, g in r.get("grades", {}).items()
            if g["pass"] is False
        )
        for r in rows
        if any(g["pass"] is False for g in r.get("grades", {}).values())
    ]
    if failing:
        lines += ["", "## Failing rows", "", *failing]
    lines += ["", f"**Gates:** {'all pass ✅' if all_ok else 'FAILED ❌'}"]
    lines += ["", "_Judge-graded metrics (Q4, Q5, Q7–Q9) are not implemented yet; see plan §3.5._"]
    return "\n".join(lines) + "\n", all_ok


# =============================================================================
# CLI
# =============================================================================


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tests.eval.run_eval")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--smoke", action="store_true", help="first case of each category, 1 rep")
    ap.add_argument("--cases", nargs="*", help="only these case ids")
    ap.add_argument("--split", choices=["tune", "holdout", "all"], default="all")
    ap.add_argument("--model")
    ap.add_argument("--fallback-model")
    ap.add_argument("--reasoning-effort", choices=["low", "medium", "high"])
    ap.add_argument("--oracle", action="store_true", help="grade ideal outputs (offline)")
    ap.add_argument("--null", action="store_true", help="grade bad outputs (offline)")
    ap.add_argument("--tag", default="", help="label for the run directory")
    ap.add_argument("--yes", action="store_true", help="don't ask before a paid run")
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    overrides = {
        k: v
        for k, v in {
            "model": args.model,
            "fallback_model": args.fallback_model,
            "reasoning_effort": args.reasoning_effort,
        }.items()
        if v is not None
    }
    settings = Settings(**overrides)
    offline = args.oracle or args.null

    cases = load_scenarios()
    if args.cases:
        cases = [c for c in cases if c["id"] in args.cases]
    if args.split != "all":
        cases = [c for c in cases if c.get("split") == args.split]
    reps = args.reps
    if args.smoke:
        seen: set[str] = set()
        cases = [c for c in cases if not (c["tags"][0] in seen or seen.add(c["tags"][0]))]
        reps = 1
    if not cases:
        print("no cases selected", file=sys.stderr)
        return 2

    work = [(c, rep) for c in cases for rep in range(1, reps + 1)]
    random.Random(0).shuffle(work)  # spread cache effects evenly (docs/eval.md §6.3)

    ranker = None
    if not offline:
        if not settings.llm_enabled:
            print("error: GROQ_API_KEY is not set (environment or .env)", file=sys.stderr)
            return 2
        price_in, price_out = PRICES.get(settings.model, PRICES["openai/gpt-oss-120b"])
        estimate = len(work) * (EST_INPUT_TOKENS * price_in + EST_OUTPUT_TOKENS * price_out) / 1e6
        print(
            f"{len(work)} calls to {settings.model} (fallback {settings.fallback_model}), "
            f"estimated cost ${estimate:.3f}"
        )
        if not args.yes and input("Run it? [y/N] ").strip().lower() != "y":
            return 1
        from dinewise.llm.ranker import GroqRanker

        ranker = GroqRanker(settings)

    stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
    mode = "oracle" if args.oracle else "null" if args.null else "live"
    run_name = f"{stamp}_{mode}_{settings.model.split('/')[-1]}_{settings.reasoning_effort}"
    run_name += f"_{args.tag}" if args.tag else ""
    run_dir = RUNS_DIR / run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    config = {
        "run": run_name,
        "mode": mode,
        "model": settings.model,
        "fallback": settings.fallback_model,
        "reasoning_effort": settings.reasoning_effort,
        "shortlist": settings.shortlist_size,
        "cases": len(cases),
        "reps": reps,
        "system_prompt_chars": len(SYSTEM_PROMPT),
    }
    (run_dir / "config.json").write_text(json.dumps(config, indent=1), encoding="utf-8")

    rows: list[dict] = []
    errors: list[dict] = []
    null_variants = ["empty", "fake_ids", "generic", "wrong_numbers"]
    for i, (case, rep) in enumerate(work, start=1):
        result, edit = load_snapshot(case["id"])
        if offline:
            variant = null_variants[(i - 1) % len(null_variants)]
            rows.append(run_offline(case, rep, result, edit, settings, mode, variant))
            continue
        kind, row = run_live(case, rep, result, edit, settings, ranker, run_dir / "transcripts")
        (rows if kind == "result" else errors).append(row)
        print(
            f"[{i}/{len(work)}] {case['id']} rep {rep}: {row['status']} {row['attempt_statuses']}"
        )

    with (run_dir / "results.jsonl").open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    with (run_dir / "errors.jsonl").open("w", encoding="utf-8") as f:
        for row in errors:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    summary, gates_ok = summarize(rows, errors, config)
    (run_dir / "summary.md").write_text(summary, encoding="utf-8")
    print(summary)
    print(f"results in {run_dir}")
    return 0 if gates_ok or args.null else 1


if __name__ == "__main__":
    sys.exit(main())
