"""Shared paths and loaders for the eval suite."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from dinewise.domain.models import RetrievalResult

EVAL_DIR = Path(__file__).resolve().parent
SCENARIOS_PATH = EVAL_DIR / "scenarios.yaml"
CANDIDATES_DIR = EVAL_DIR / "candidates"
RUNS_DIR = EVAL_DIR / "runs"


def load_scenarios(path: Path = SCENARIOS_PATH) -> list[dict]:
    cases = yaml.safe_load(path.read_text(encoding="utf-8"))
    ids = [c["id"] for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate scenario ids in scenarios.yaml")
    return cases


def load_snapshot(case_id: str) -> tuple[RetrievalResult, dict]:
    path = CANDIDATES_DIR / f"{case_id}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"{path} missing; run `python -m tests.eval.snapshot_candidates` first"
        )
    snap = json.loads(path.read_text(encoding="utf-8"))
    return RetrievalResult.model_validate(snap["result"]), snap.get("edit") or {}
