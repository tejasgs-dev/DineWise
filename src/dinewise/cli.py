"""Developer CLI.

    python -m dinewise.cli recommend --location Indiranagar --cuisine Italian \\
        --budget medium --min-rating 4 --extras "family friendly"
    python -m dinewise.cli recommend --location Indiranagar --cuisine Italian --llm

Without `--llm` the ranking is rule-based. With `--llm` the shortlist is ranked and
explained by the LLM on Groq (needs GROQ_API_KEY), using the same `Recommender` as the
API, so a failed LLM call falls back to the rule-based ranking the same way.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from pydantic import ValidationError

from dinewise.config import Settings, get_settings
from dinewise.domain.formatting import format_indian, format_inr, join_words
from dinewise.domain.models import RecommendationResponse, UserPreferences
from dinewise.retrieval.normalize import UnknownValueError
from dinewise.retrieval.repository import DataNotLoadedError, RestaurantRepository
from dinewise.service.recommender import Ranker, Recommender

EXIT_DATA = 1
EXIT_INPUT = 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m dinewise.cli")
    sub = parser.add_subparsers(dest="command", required=True)

    rec = sub.add_parser("recommend", help="recommend restaurants")
    rec.add_argument("--location", required=True, help='neighbourhood, e.g. "Indiranagar"')
    rec.add_argument(
        "--cuisine", action="append", default=[], help="repeat for several, e.g. --cuisine Thai"
    )
    rec.add_argument("--budget", help="low, medium, or high")
    rec.add_argument("--min-rating", type=float, default=0.0)
    rec.add_argument("--extras", help='free text, e.g. "family friendly, quick service"')
    rec.add_argument("--top-k", type=int, default=5)
    rec.add_argument("--llm", action="store_true", help="rank with the LLM on Groq")
    rec.add_argument("--json", action="store_true", help="print machine-readable JSON")
    return parser


def make_ranker(settings: Settings) -> Ranker:
    from dinewise.llm.ranker import GroqRanker  # only needed with --llm

    return GroqRanker(settings)


def run_recommend(args: argparse.Namespace, settings: Settings, out=None) -> int:
    out = out or sys.stdout  # resolved at call time, so redirection and capture work
    try:
        prefs = UserPreferences(
            location=args.location,
            budget=args.budget,
            cuisines=args.cuisine,
            min_rating=args.min_rating,
            extras=args.extras,
            top_k=args.top_k,
        )
    except ValidationError as exc:
        for err in exc.errors():
            field = ".".join(str(p) for p in err["loc"])
            print(f"error: {field}: {err['msg']}", file=sys.stderr)
        return EXIT_INPUT

    if args.llm and not settings.llm_enabled:
        print("error: --llm needs GROQ_API_KEY (in the environment or .env)", file=sys.stderr)
        return EXIT_INPUT

    try:
        started = time.perf_counter()
        repo = RestaurantRepository.from_parquet(settings.data_path, settings)
        load_ms = (time.perf_counter() - started) * 1000
    except DataNotLoadedError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_DATA

    recommender = Recommender(repo, settings, make_ranker(settings) if args.llm else None)
    try:
        response = recommender.recommend(prefs)
    except UnknownValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_INPUT

    if args.json:
        payload = {
            "preferences": response.normalized_preferences.model_dump(),
            "summary": response.summary,
            "relaxations": response.relaxations,
            "degraded": response.degraded,
            "total_matches": response.meta.total_matches,
            "meta": response.meta.model_dump(),
            "recommendations": [
                r.model_dump(exclude={"restaurant": {"review_snippets"}})
                for r in response.recommendations
            ],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2), file=out)
    else:
        print_report(response, out, llm_requested=args.llm)
        meta = response.meta
        model = f", model {meta.model}" if meta.model else ""
        print(
            f"\n{meta.ranking} ranking{model}; retrieval and ranking {meta.latency_ms} ms "
            f"(data load {load_ms:.0f} ms, {len(repo):,} restaurants)",
            file=out,
        )
    return 0


def print_report(response: RecommendationResponse, out, llm_requested: bool = False) -> None:
    p = response.normalized_preferences
    wants = [p.location]
    if p.cuisines:
        wants.append(join_words(p.cuisines, "or"))
    if p.budget:
        wants.append(f"{p.budget} budget")
    if p.min_rating:
        wants.append(f"rated {p.min_rating:g}+")
    if p.extras_tags:
        wants.append("tags: " + ", ".join(p.extras_tags))
    print(f"Looking for: {' · '.join(wants)}", file=out)
    for c in p.corrections:
        print(f"  (matched '{c.input}' to '{c.value}')", file=out)
    for message in response.relaxations:
        print(f"  ! {message}", file=out)
    if llm_requested and response.degraded:
        print("  ! The LLM gave no usable answer; showing the rule-based ranking", file=out)
    print(
        f"{format_indian(response.meta.total_matches)} matches, "
        f"{response.meta.candidates_considered} shortlisted\n",
        file=out,
    )
    if not response.recommendations:
        print("No restaurants matched, even after relaxing the filters.", file=out)
        return
    print(f"{response.summary}\n", file=out)
    for rec in response.recommendations:
        r = rec.restaurant
        rating = f"★ {r.rating:.1f}" if r.rating is not None else "★ new"
        cost = format_inr(r.cost_for_two) if r.cost_for_two else "₹?"
        label = "AI" if rec.source == "llm" else "rule-based"
        print(f"{rec.rank}. {r.name} — {r.locality}  [{rating} · {cost} for two]", file=out)
        print(f"   {', '.join(r.cuisines)}", file=out)
        print(f"   ({label}) {rec.explanation}", file=out)
        if rec.caveats:
            print(f"   Note: {rec.caveats}", file=out)


def main(argv: list[str] | None = None) -> int:
    # Windows consoles default to a legacy code page that can't print "₹" or "★".
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    args = build_parser().parse_args(argv)
    if args.command == "recommend":
        return run_recommend(args, get_settings())
    return EXIT_INPUT  # pragma: no cover - argparse enforces a known command


if __name__ == "__main__":
    sys.exit(main())
