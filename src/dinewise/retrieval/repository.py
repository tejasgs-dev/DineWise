"""In-memory restaurant store with precomputed indexes (arch §5.5).

The cleaned Parquet file is loaded once. Filtering and scoring work on NumPy arrays and
boolean masks built here, so a query over all ~12,500 restaurants takes milliseconds.
Rows are only converted to `Restaurant` models for the final shortlist.
"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Iterable, Sequence
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq

from dinewise.domain.budget import BANDS, BudgetThresholds, cost_to_band
from dinewise.domain.models import Restaurant
from dinewise.ingestion.clean import OUTPUT_COLUMNS, normalize_key
from dinewise.ingestion.run import BUILD_ID_KEY

log = logging.getLogger(__name__)

# A neighbourhood's zone profile: zones that list at least this share of its restaurants.
ZONE_PROFILE_MIN_SHARE = 0.5
# Neighbourhoods with fewer restaurants than this get no profile (too noisy for "nearby").
ZONE_PROFILE_MIN_RESTAURANTS = 5


class DataNotLoadedError(RuntimeError):
    """The restaurant data file is missing or unreadable (edge cases API-01, API-02)."""


class RestaurantRepository:
    def __init__(
        self, df: pd.DataFrame, thresholds: BudgetThresholds, build_id: str | None = None
    ) -> None:
        missing = [col for col in OUTPUT_COLUMNS if col not in df.columns]
        if missing:
            raise DataNotLoadedError(f"restaurant data is missing columns: {', '.join(missing)}")

        df = df.reset_index(drop=True)
        self._df = df
        self.build_id = build_id
        n = len(df)

        self.ids: np.ndarray = df["id"].to_numpy(dtype=object)
        self._position = {rid: i for i, rid in enumerate(self.ids)}
        self.ratings: np.ndarray = df["rating"].to_numpy(dtype=float, na_value=np.nan)
        self.votes: np.ndarray = df["votes"].to_numpy(dtype=np.int64)
        self.costs: np.ndarray = df["cost_for_two"].to_numpy(dtype=float, na_value=np.nan)
        self.localities: np.ndarray = df["locality"].to_numpy(dtype=object)
        self.chain_keys: np.ndarray = np.array([normalize_key(x) for x in df["name"]], dtype=object)

        # Bands are recomputed from cost with the current thresholds, so changing them in
        # config takes effect without re-running ingestion (edge case D-10).
        bands = [cost_to_band(None if np.isnan(c) else int(c), thresholds) for c in self.costs]
        self.band_codes: np.ndarray = np.array(
            [BANDS.index(b) if b else -1 for b in bands], dtype=np.int8
        )
        self._bands = bands

        self._locality_masks = _index(df["locality"].map(lambda x: [x]), n)
        self._cuisine_masks = _index(df["cuisines"], n, key=str.lower)
        self._tag_masks = _index(df["tags"], n)
        self._cuisine_names = _canonical_names(df["cuisines"])
        self._locality_counts = Counter(df["locality"])
        self._cuisine_counts = Counter(c for cs in df["cuisines"] for c in cs)
        self._cities = sorted(set(df["city"]))
        self._zone_profiles = _zone_profiles(df)

    # --- Construction ---------------------------------------------------------------

    @classmethod
    def from_parquet(cls, path: Path, thresholds: BudgetThresholds) -> RestaurantRepository:
        if not path.exists():
            raise DataNotLoadedError(
                f"{path} not found; build it with `python -m dinewise.ingestion.run`"
            )
        try:
            table = pq.read_table(path)
        except Exception as exc:  # pyarrow raises several error types for bad files
            raise DataNotLoadedError(f"could not read {path}: {exc}") from exc
        metadata = table.schema.metadata or {}
        build_id = metadata.get(BUILD_ID_KEY, b"").decode() or None
        repo = cls(table.to_pandas(), thresholds, build_id=build_id)
        log.info("loaded %d restaurants from %s (build %s)", len(repo), path, build_id)
        return repo

    # --- Lookups ----------------------------------------------------------------------

    def __len__(self) -> int:
        return len(self.ids)

    def cities(self) -> list[str]:
        return list(self._cities)

    def known_localities(self) -> list[str]:
        return sorted(self._locality_counts)

    def known_cuisines(self) -> list[str]:
        return sorted(self._cuisine_counts)

    def locality_counts(self) -> dict[str, int]:
        return dict(self._locality_counts)

    def cuisine_counts(self) -> dict[str, int]:
        return dict(self._cuisine_counts)

    def canonical_cuisine(self, name: str) -> str | None:
        return self._cuisine_names.get(name.lower())

    def locality_group(self, locality: str) -> list[str]:
        """A neighbourhood plus its sub-areas.

        "Koramangala" also covers "Koramangala 1st Block" ... "8th Block", and
        "Whitefield" covers "ITPL Main Road, Whitefield".
        """
        group = {
            name
            for name in self._locality_counts
            if name == locality or name.startswith(locality + " ") or name.endswith(", " + locality)
        }
        return sorted(group)

    def nearby_localities(self, localities: Iterable[str]) -> list[str]:
        """Neighbourhoods whose main Zomato zones overlap the given ones (edge case D-04).

        The dataset has no coordinates. Instead, each neighbourhood with enough
        restaurants gets a zone profile (zones that list at least half of them), and two
        neighbourhoods count as nearby when their profiles share a zone.
        """
        given = set(localities)
        profile: set[str] = set()
        for loc in given:
            profile |= self._zone_profiles.get(loc, set())
        if not profile:
            return []
        return sorted(
            loc
            for loc, zones in self._zone_profiles.items()
            if loc not in given and zones & profile
        )

    def get_by_ids(self, ids: Iterable[str]) -> list[Restaurant]:
        """Restaurants for the given IDs, in the same order; unknown IDs are skipped."""
        positions = [self._position[rid] for rid in ids if rid in self._position]
        return self.restaurants_at(positions)

    def restaurants_at(self, positions: Sequence[int] | np.ndarray) -> list[Restaurant]:
        rows = self._df.iloc[list(positions)].to_dict(orient="records")
        out = []
        for pos, row in zip(positions, rows, strict=True):
            row["budget_band"] = self._bands[pos]
            out.append(Restaurant.model_validate(row))
        return out

    # --- Masks used by filters and scoring ----------------------------------------------

    def locality_mask(self, localities: Iterable[str]) -> np.ndarray:
        return _union(self._locality_masks, localities, len(self))

    def cuisine_mask(self, cuisines: Iterable[str]) -> np.ndarray:
        return _union(self._cuisine_masks, (c.lower() for c in cuisines), len(self))

    def cuisine_match_counts(self, cuisines: Iterable[str]) -> np.ndarray:
        return _count(self._cuisine_masks, (c.lower() for c in cuisines), len(self))

    def tag_match_counts(self, tags: Iterable[str]) -> np.ndarray:
        return _count(self._tag_masks, tags, len(self))


def _index(column: pd.Series, n: int, key=None) -> dict[str, np.ndarray]:
    """Map each list item in `column` to a boolean mask of the rows that contain it."""
    positions: dict[str, list[int]] = {}
    for i, items in enumerate(column):
        for item in items:
            positions.setdefault(key(item) if key else item, []).append(i)
    masks = {}
    for item, rows in positions.items():
        mask = np.zeros(n, dtype=bool)
        mask[rows] = True
        masks[item] = mask
    return masks


def _canonical_names(column: pd.Series) -> dict[str, str]:
    names: dict[str, str] = {}
    for items in column:
        for item in items:
            names.setdefault(item.lower(), item)
    return names


def _union(masks: dict[str, np.ndarray], keys: Iterable[str], n: int) -> np.ndarray:
    out = np.zeros(n, dtype=bool)
    for key in keys:
        if key in masks:
            out |= masks[key]
    return out


def _count(masks: dict[str, np.ndarray], keys: Iterable[str], n: int) -> np.ndarray:
    out = np.zeros(n, dtype=np.int16)
    for key in set(keys):
        if key in masks:
            out += masks[key]
    return out


def _zone_profiles(df: pd.DataFrame) -> dict[str, set[str]]:
    profiles: dict[str, set[str]] = {}
    for loc, zones in df.groupby("locality", sort=False)["zones"]:
        if len(zones) < ZONE_PROFILE_MIN_RESTAURANTS:
            continue
        counts = Counter(z for zs in zones for z in zs)
        if not counts:
            continue
        main = {z for z, k in counts.items() if k / len(zones) >= ZONE_PROFILE_MIN_SHARE}
        profiles[loc] = main or {counts.most_common(1)[0][0]}
    return profiles
