"""Ingestion CLI: download -> clean -> validate -> write Parquet + meta.json.

python -m dinewise.ingestion.run            # download if needed, then build
python -m dinewise.ingestion.run --offline  # use the existing data/raw copy only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import ValidationError

from dinewise.config import Settings, get_settings
from dinewise.domain.models import Restaurant
from dinewise.ingestion import clean, load

BUILD_ID_KEY = b"dinewise_build_id"
DEFAULT_MAX_DROP_SHARE = 0.20  # edge case DATA-40
VALIDATION_SAMPLE_SIZE = 1000


class IngestionError(RuntimeError):
    pass


def validate_sample(df: pd.DataFrame, sample_size: int = VALIDATION_SAMPLE_SIZE) -> int:
    """Validate a random sample of rows against the `Restaurant` model. Returns rows checked."""
    sample = df.sample(n=min(sample_size, len(df)), random_state=0) if len(df) else df
    errors: list[str] = []
    for record in sample.to_dict(orient="records"):
        try:
            Restaurant.model_validate(record)
        except ValidationError as exc:
            errors.append(f"{record.get('id')}: {exc.errors()[0]['msg']}")
    if errors:
        raise IngestionError(
            f"{len(errors)} of {len(sample)} sampled rows failed validation, e.g.\n  "
            + "\n  ".join(errors[:5])
        )
    return len(sample)


def compute_build_id(df: pd.DataFrame, settings: Settings) -> str:
    """Deterministic fingerprint of the output: same input and config -> same build ID."""
    h = hashlib.sha256()
    h.update(f"{settings.hf_dataset}@{settings.hf_revision}".encode())
    h.update(f"{settings.budget_low_max}/{settings.budget_medium_max}".encode())
    h.update(
        pd.util.hash_pandas_object(
            df[["id", "rating", "votes", "cost_for_two"]], index=False
        ).values.tobytes()
    )
    return h.hexdigest()[:16]


def build_meta(df: pd.DataFrame, settings: Settings, build_id: str) -> dict[str, Any]:
    def counted(values: list[str]) -> list[dict[str, Any]]:
        return [{"name": k, "count": v} for k, v in sorted(Counter(values).items())]

    return {
        "build_id": build_id,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "source": {"dataset": settings.hf_dataset, "revision": settings.hf_revision},
        "row_count": len(df),
        "budget_thresholds": {
            "low_max": settings.budget_low_max,
            "medium_max": settings.budget_medium_max,
        },
        "cities": counted(df["city"].tolist()),
        "localities": counted(df["locality"].tolist()),
        "zones": counted([z for zones in df["zones"] for z in zones]),
        "cuisines": counted([c for cuisines in df["cuisines"] for c in cuisines]),
    }


def write_outputs(df: pd.DataFrame, meta: dict[str, Any], data_path: Path, meta_path: Path) -> None:
    """Write both files to temporaries, then swap them into place (DATA-38, DATA-39).

    A failure part-way leaves the previous Parquet and meta.json untouched.
    """
    data_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    data_tmp = data_path.with_name(data_path.name + ".tmp")
    meta_tmp = meta_path.with_name(meta_path.name + ".tmp")

    table = pa.Table.from_pandas(df, preserve_index=False)
    table = table.replace_schema_metadata(
        {**(table.schema.metadata or {}), BUILD_ID_KEY: meta["build_id"].encode()}
    )
    try:
        pq.write_table(table, data_tmp)
        meta_tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(data_tmp, data_path)
        os.replace(meta_tmp, meta_path)
    finally:
        for tmp in (data_tmp, meta_tmp):
            tmp.unlink(missing_ok=True)


def run(
    settings: Settings,
    *,
    raw_file: Path | None = None,
    offline: bool = False,
    max_drop_share: float = DEFAULT_MAX_DROP_SHARE,
    out=sys.stdout,
) -> clean.CleanStats:
    started = time.perf_counter()

    raw_path = raw_file or load.ensure_raw_file(settings, offline=offline)
    print(f"reading {raw_path}", file=out)
    raw = load.read_raw(raw_path)

    df, stats = clean.clean(raw, settings)
    if stats.dropped_share > max_drop_share:
        raise IngestionError(
            f"cleaning dropped {stats.dropped_share:.1%} of rows (limit {max_drop_share:.0%}); "
            "the raw data may have changed shape"
        )

    checked = validate_sample(df)
    build_id = compute_build_id(df, settings)
    meta = build_meta(df, settings, build_id)
    write_outputs(df, meta, settings.data_path, settings.meta_path)

    elapsed = time.perf_counter() - started
    print("\n".join(stats.lines()), file=out)
    print(f"validated:            {checked:>8,} sampled rows", file=out)
    print(
        f"localities: {len(meta['localities'])}, zones: {len(meta['zones'])}, "
        f"cuisines: {len(meta['cuisines'])}",
        file=out,
    )
    print(f"build id: {build_id}", file=out)
    print(f"wrote {settings.data_path} and {settings.meta_path} in {elapsed:.1f}s", file=out)
    return stats


def read_build_id(data_path: Path) -> str | None:
    """Read the build ID stored in a Parquet file's metadata."""
    metadata = pq.read_schema(data_path).metadata or {}
    value = metadata.get(BUILD_ID_KEY)
    return value.decode() if value else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build data/restaurants.parquet from the raw Zomato dataset."
    )
    parser.add_argument(
        "--offline", action="store_true", help="don't download; use the local raw file"
    )
    parser.add_argument(
        "--raw-file", type=Path, help="use this CSV instead of the Hugging Face copy"
    )
    parser.add_argument(
        "--data-path", type=Path, help="output Parquet path (default from settings)"
    )
    parser.add_argument(
        "--meta-path", type=Path, help="output meta.json path (default from settings)"
    )
    parser.add_argument(
        "--max-drop-share",
        type=float,
        default=DEFAULT_MAX_DROP_SHARE,
        help="fail if more than this share of rows is dropped as unusable (default 0.2)",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    overrides = {
        k: v for k, v in {"data_path": args.data_path, "meta_path": args.meta_path}.items() if v
    }
    if overrides:
        settings = settings.model_copy(update=overrides)

    try:
        run(
            settings,
            raw_file=args.raw_file,
            offline=args.offline,
            max_drop_share=args.max_drop_share,
        )
    except (IngestionError, load.MissingColumnsError, FileNotFoundError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
