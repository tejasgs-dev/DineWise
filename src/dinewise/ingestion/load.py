"""Fetch the raw Zomato CSV from Hugging Face and read it into a DataFrame."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from dinewise.config import Settings

# Raw columns the cleaning pipeline depends on (edge case DATA-35).
REQUIRED_COLUMNS: tuple[str, ...] = (
    "url",
    "address",
    "name",
    "online_order",
    "book_table",
    "rate",
    "votes",
    "location",
    "rest_type",
    "dish_liked",
    "cuisines",
    "approx_cost(for two people)",
    "reviews_list",
    "listed_in(type)",
    "listed_in(city)",
)

# Present in the raw data but not used: `phone` is personal data, `menu_item` is almost always "[]".
IGNORED_COLUMNS: frozenset[str] = frozenset({"phone", "menu_item"})


class MissingColumnsError(ValueError):
    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        super().__init__(f"raw dataset is missing required columns: {', '.join(missing)}")


def ensure_raw_file(settings: Settings, offline: bool = False) -> Path:
    """Return the path to the raw CSV, downloading it into `settings.raw_dir` if needed.

    With `offline=True`, only an existing local copy is used (edge case DATA-34).
    """
    local = settings.raw_dir / settings.hf_filename
    if offline:
        if not local.exists():
            raise FileNotFoundError(
                f"{local} not found; run without --offline to download it from Hugging Face"
            )
        return local

    from huggingface_hub import hf_hub_download

    path = hf_hub_download(
        repo_id=settings.hf_dataset,
        filename=settings.hf_filename,
        repo_type="dataset",
        revision=settings.hf_revision,
        local_dir=settings.raw_dir,
    )
    return Path(path)


def read_raw(path: Path) -> pd.DataFrame:
    """Read the raw CSV with every column as text; parsing happens in `clean`."""
    df = pd.read_csv(
        path,
        dtype=str,
        usecols=lambda col: col not in IGNORED_COLUMNS,
        encoding="utf-8",
    )
    check_columns(df)
    return df


def check_columns(df: pd.DataFrame) -> None:
    missing = [col for col in REQUIRED_COLUMNS if col not in df.columns]
    if missing:
        raise MissingColumnsError(missing)
