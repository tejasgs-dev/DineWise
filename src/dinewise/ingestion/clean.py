"""Cleaning pipeline for the raw Zomato CSV (arch §5.2).

The scalar helpers (`parse_rating_value`, `clean_text`, ...) do the per-value work and are
unit-tested directly. The DataFrame steps (`parse_rating`, `deduplicate`, ...) are pure
functions: each takes a DataFrame and returns a new one. `clean()` runs them in order.

Text handling uses Python's `re`, not pandas `.str` regex methods: with pandas 3's Arrow
strings those use RE2, which has different syntax.
"""

from __future__ import annotations

import ast
import hashlib
import html
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import pandas as pd

from dinewise.domain.budget import BudgetThresholds, cost_to_band

DEFAULT_CITY = "Bangalore"

RAW_TO_INTERNAL: dict[str, str] = {
    "url": "url",
    "address": "address",
    "name": "name",
    "online_order": "online_order",
    "book_table": "book_table",
    "rate": "rate",
    "votes": "votes",
    "location": "location",
    "rest_type": "rest_type",
    "dish_liked": "dish_liked",
    "cuisines": "cuisines",
    "approx_cost(for two people)": "cost",
    "reviews_list": "reviews_list",
    "listed_in(type)": "listing_type",
    "listed_in(city)": "zone",
}

OUTPUT_COLUMNS: tuple[str, ...] = (
    "id",
    "name",
    "city",
    "locality",
    "address",
    "zones",
    "cuisines",
    "rest_types",
    "listing_types",
    "cost_for_two",
    "budget_band",
    "rating",
    "votes",
    "is_new",
    "online_order",
    "book_table",
    "dishes_liked",
    "tags",
    "review_snippets",
)

# Canonical spellings, keyed by lowercase (edge case DATA-17).
CUISINE_ALIASES: dict[str, str] = {
    "afghani": "Afghan",
    "bbq": "BBQ",
    "hot dogs": "Hot Dogs",
}
REST_TYPE_ALIASES: dict[str, str] = {
    "irani cafee": "Irani Cafe",
}

# --- Tag rules (arch §5.2 step 4) ---------------------------------------------
BAR_REST_TYPES = frozenset({"Bar", "Pub", "Microbrewery", "Lounge", "Club"})
QUICK_REST_TYPES = frozenset(
    {"Quick Bites", "Food Court", "Kiosk", "Takeaway", "Food Truck", "Delivery"}
)
CAFE_REST_TYPES = frozenset({"Cafe", "Irani Cafe"})
DESSERT_REST_TYPES = frozenset({"Dessert Parlor", "Bakery", "Sweet Shop", "Confectionery"})
SIT_DOWN_REST_TYPES = frozenset({"Casual Dining", "Fine Dining"})

_MISSING_TOKENS = frozenset({"", "nan", "none", "null", "n/a", "na", "-"})
_RATING_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(?:/\s*5)?$")
_COST_RE = re.compile(r"^\d+(?:\.\d+)?$")
_WS_RE = re.compile(r"\s+")
_C1_RE = re.compile(r"[\x80-\x9f]")
_MOJIBAKE_RESIDUE_RE = re.compile(r"[ÃÂ][\x80-\xbf]*")
# Runs of 3+ Latin-1 / Windows-1252 symbols left where a character was mangled beyond
# repair, e.g. "didnƒƒ‚ƒƒ‚‚t". Real text almost never has three in a row.
_MOJIBAKE_RUN_RE = re.compile(
    "[\u0080-¿ÂÃŒœŠšŸŽžƒˆ"
    "˜–-„†-•…‰‹›€™]{3,}"
    r"(?=(s|t|ll|re|ve|m|d)\b)?"
)
_KEY_RE = re.compile(r"[^\w]+")
_URL_CITY_RE = re.compile(r"zomato\.com/([^/?#]+)/")
_RATED_PREFIX_RE = re.compile(r"^\s*RATED\s*", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
_PHONE_RE = re.compile(r"(?<!\w)\+?\d[\d\s-]{8,}\d(?!\w)")


# =============================================================================
# Scalar helpers
# =============================================================================


def is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    if type(value).__name__ == "NAType":
        return True
    return str(value).strip().lower() in _MISSING_TOKENS


def fix_mojibake(text: str, max_rounds: int = 8) -> str:
    """Undo repeated UTF-8 -> Latin-1 double encoding ("CafÃƒÂ©" -> "Café"), edge case DATA-19.

    Text that is already correct fails the round-trip and is returned unchanged.
    """
    for _ in range(max_rounds):
        try:
            repaired = text.encode("latin-1").decode("utf-8")
        except (UnicodeEncodeError, UnicodeDecodeError):
            break
        if repaired == text:
            break
        text = repaired
    if _C1_RE.search(text):
        # Leftover Windows-1252 bytes such as \x92 (a curly apostrophe).
        try:
            text = text.encode("latin-1").decode("cp1252")
        except (UnicodeEncodeError, UnicodeDecodeError):
            text = _C1_RE.sub("", text)
    return text


def clean_text(value: Any) -> str | None:
    """Repair encoding, unescape HTML entities, and collapse whitespace; blank -> None."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if type(value).__name__ == "NAType":
        return None
    text = fix_mojibake(str(value))
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text).strip()
    return text or None


def parse_rating_value(value: Any) -> tuple[float | None, bool, bool]:
    """Parse a raw `rate` value. Returns (rating, is_new, is_invalid).

    "4.1/5", "4.1 /5", "4.1" -> 4.1. "NEW" -> new and unrated. "-" or blank -> unknown.
    Anything else, or a value outside 0-5, is invalid and unrated (DATA-01..06).
    """
    if is_missing(value):
        return None, False, False
    text = str(value).strip()
    if text.upper() == "NEW":
        return None, True, False
    match = _RATING_RE.match(text)
    if not match:
        return None, False, True
    rating = float(match.group(1))
    if not 0 <= rating <= 5:
        return None, False, True
    return round(rating, 1), False, False


def parse_cost_value(value: Any) -> int | None:
    """Parse a raw cost for two: "1,200" -> 1200; blank, non-numeric, or <= 0 -> None."""
    if is_missing(value):
        return None
    text = str(value).strip().replace(",", "")
    if not _COST_RE.match(text):
        return None
    cost = round(float(text))
    return cost if cost > 0 else None


def parse_votes_value(value: Any) -> int:
    """Parse votes; missing, negative, or non-numeric -> 0 (DATA-12)."""
    if is_missing(value):
        return 0
    try:
        votes = int(float(str(value).strip().replace(",", "")))
    except ValueError:
        return 0
    return max(votes, 0)


def parse_bool_value(value: Any) -> bool:
    """Case-insensitive "yes" -> True; everything else -> False (DATA-13)."""
    return not is_missing(value) and str(value).strip().lower() == "yes"


def split_list_value(value: Any, aliases: dict[str, str] | None = None) -> list[str]:
    """Split a comma-separated field into clean, de-duplicated items (DATA-14..17).

    All-lowercase items are title-cased; items that already have capitals keep them, so
    "BBQ" doesn't become "Bbq". `aliases` maps lowercase spellings to a canonical form.
    """
    if is_missing(value):
        return []
    aliases = aliases or {}
    items: list[str] = []
    seen: set[str] = set()
    for part in str(value).split(","):
        item = clean_text(part)
        if not item or item.lower() in _MISSING_TOKENS:
            continue
        canonical = aliases.get(item.lower())
        if canonical is None:
            canonical = item.title() if item.islower() else item
        key = canonical.lower()
        if key not in seen:
            seen.add(key)
            items.append(canonical)
    return items


def normalize_key(text: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace: the basis for dedup keys and IDs."""
    return _WS_RE.sub(" ", _KEY_RE.sub(" ", text.lower())).strip()


def make_id(name: str, address: str) -> str:
    """Stable restaurant ID from the normalized name and address (DATA-28, DATA-29)."""
    digest = hashlib.sha1(f"{normalize_key(name)}|{normalize_key(address)}".encode()).hexdigest()
    return f"r_{digest[:12]}"


def city_from_url(url: Any, default: str = DEFAULT_CITY) -> str:
    """Zomato URLs look like https://www.zomato.com/bangalore/<slug>; take the city segment."""
    if is_missing(url):
        return default
    match = _URL_CITY_RE.search(str(url))
    if not match:
        return default
    return match.group(1).replace("-", " ").title()


def truncate(text: str, max_len: int) -> str:
    """Cut text to at most `max_len` characters, at a word boundary where possible."""
    if len(text) <= max_len:
        return text
    cut = text[: max_len - 1]
    space = cut.rfind(" ")
    if space >= max_len * 0.6:
        cut = cut[:space]
    return cut.rstrip(" ,.;:-") + "…"


def scrub_pii(text: str) -> str:
    """Replace email addresses and phone numbers in review text (edge case SEC-07)."""
    text = _EMAIL_RE.sub("[email removed]", text)
    return _PHONE_RE.sub("[phone removed]", text)


def extract_snippets_value(value: Any, max_snippets: int = 3, max_len: int = 200) -> list[str]:
    """Pick the first few usable review texts from a raw `reviews_list` string (DATA-20, 21).

    The raw value is a Python-literal list of ("Rated 4.0", "RATED\\n  text") tuples.
    `ast.literal_eval` parses literals only; it never executes code.
    """
    if is_missing(value) or str(value).strip() == "[]":
        return []
    try:
        items = ast.literal_eval(str(value))
    except (ValueError, SyntaxError, MemoryError, RecursionError, TypeError):
        return []
    if not isinstance(items, list | tuple):
        return []

    snippets: list[str] = []
    for item in items:
        if isinstance(item, list | tuple) and len(item) >= 2:
            raw_text = item[1]
        elif isinstance(item, str):
            raw_text = item
        else:
            continue
        if not isinstance(raw_text, str):
            continue
        text = clean_text(_RATED_PREFIX_RE.sub("", raw_text))
        if not text:
            continue
        text = _strip_mojibake_runs(_MOJIBAKE_RESIDUE_RE.sub("", text))
        text = _WS_RE.sub(" ", text).strip()
        text = scrub_pii(text)
        if len(text) < 20:  # too short to say anything useful
            continue
        snippets.append(truncate(text, max_len))
        if len(snippets) == max_snippets:
            break
    return snippets


def tags_for(
    rest_types: Iterable[str],
    listing_types: Iterable[str],
    book_table: bool,
    online_order: bool,
) -> list[str]:
    """Derive normalized feature tags used to match free-text preferences (arch §5.2).

    `family_friendly` is a heuristic: a sit-down restaurant that isn't also a bar or pub.
    `bar` comes from the restaurant's own type only: the "Pubs and bars" and
    "Drinks & nightlife" listings also include juice and dessert shops.
    """
    rest = set(rest_types)
    listing = set(listing_types)
    is_bar = bool(rest & BAR_REST_TYPES)

    tags: set[str] = set()
    if rest & SIT_DOWN_REST_TYPES and not is_bar:
        tags.add("family_friendly")
    if rest & QUICK_REST_TYPES:
        tags.add("quick_service")
    if book_table:
        tags.add("table_booking")
    if online_order or "Delivery" in listing:
        tags.add("delivery")
    if is_bar:
        tags.add("bar")
    if "Buffet" in listing:
        tags.add("buffet")
    if rest & CAFE_REST_TYPES or "Cafes" in listing:
        tags.add("cafe")
    if rest & DESSERT_REST_TYPES or "Desserts" in listing:
        tags.add("desserts")
    if "Fine Dining" in rest:
        tags.add("fine_dining")
    return sorted(tags)


# =============================================================================
# DataFrame steps
# =============================================================================


def standardize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Rename raw columns to internal names and drop everything else."""
    return df[list(RAW_TO_INTERNAL)].rename(columns=RAW_TO_INTERNAL)


def clean_text_fields(df: pd.DataFrame) -> pd.DataFrame:
    return df.assign(
        **{
            col: df[col].map(clean_text)
            for col in ("name", "address", "location", "zone", "listing_type")
        }
    )


def drop_invalid(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Drop rows with no name or address: they can't be identified or shown (DATA-27)."""
    keep = df["name"].notna() & df["address"].notna()
    return df[keep].copy(), int((~keep).sum())


def parse_rating(df: pd.DataFrame) -> pd.DataFrame:
    parsed = df["rate"].map(parse_rating_value)
    return df.assign(
        rating=pd.array([p[0] for p in parsed], dtype="Float64"),
        is_new=[p[1] for p in parsed],
        rating_invalid=[p[2] for p in parsed],
    ).drop(columns=["rate"])


def parse_cost(df: pd.DataFrame) -> pd.DataFrame:
    return df.assign(
        cost_for_two=pd.array(df["cost"].map(parse_cost_value).tolist(), dtype="Int64"),
        votes=df["votes"].map(parse_votes_value).astype("int64"),
    ).drop(columns=["cost"])


def parse_bools(df: pd.DataFrame) -> pd.DataFrame:
    return df.assign(
        online_order=df["online_order"].map(parse_bool_value).astype(bool),
        book_table=df["book_table"].map(parse_bool_value).astype(bool),
    )


def split_list_fields(df: pd.DataFrame) -> pd.DataFrame:
    return df.assign(
        cuisines=df["cuisines"].map(lambda v: split_list_value(v, CUISINE_ALIASES)),
        rest_types=df["rest_type"].map(lambda v: split_list_value(v, REST_TYPE_ALIASES)),
        dishes_liked=df["dish_liked"].map(split_list_value),
    ).drop(columns=["rest_type", "dish_liked"])


def deduplicate(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Collapse repeated listings of one restaurant into a single row (DATA-23..26).

    The raw data lists a restaurant once per `listed_in(type)` and zone, and those copies
    were scraped at different times. Rows are grouped by normalized (name, address); the
    row with the most votes (the freshest) supplies the values, ties keep the first row,
    and listing types and zones are merged across the group.
    """
    keyed = df.assign(
        _key=[
            f"{normalize_key(n)}|{normalize_key(a)}"
            for n, a in zip(df["name"], df["address"], strict=True)
        ]
    )
    merged_listing = keyed.groupby("_key", sort=False)["listing_type"].agg(_sorted_unique)
    merged_zones = keyed.groupby("_key", sort=False)["zone"].agg(_sorted_unique)

    best = (
        keyed.sort_values("votes", ascending=False, kind="mergesort")
        .drop_duplicates("_key", keep="first")
        .sort_index()
    )
    best = best.assign(
        listing_types=best["_key"].map(merged_listing),
        zones=best["_key"].map(merged_zones),
    ).drop(columns=["_key", "listing_type", "zone"])
    return best, len(df) - len(best)


def extract_review_snippets(df: pd.DataFrame) -> pd.DataFrame:
    return df.assign(review_snippets=df["reviews_list"].map(extract_snippets_value)).drop(
        columns=["reviews_list"]
    )


def derive_fields(df: pd.DataFrame, thresholds: BudgetThresholds) -> pd.DataFrame:
    """Add `id`, `city`, `locality`, and `budget_band`.

    `locality` is the raw `location` (a neighbourhood). When it is blank, the first zone is
    used instead (DATA-31).
    """
    locality = [
        loc if isinstance(loc, str) and loc else (zones[0] if zones else None)
        for loc, zones in zip(df["location"], df["zones"], strict=True)
    ]
    return df.assign(
        id=[make_id(n, a) for n, a in zip(df["name"], df["address"], strict=True)],
        city=df["url"].map(city_from_url),
        locality=locality,
        budget_band=[
            cost_to_band(None if pd.isna(c) else int(c), thresholds) for c in df["cost_for_two"]
        ],
    ).drop(columns=["location", "url"])


def derive_tags(df: pd.DataFrame) -> pd.DataFrame:
    return df.assign(
        tags=[
            tags_for(r, lt, bt, oo)
            for r, lt, bt, oo in zip(
                df["rest_types"],
                df["listing_types"],
                df["book_table"],
                df["online_order"],
                strict=True,
            )
        ]
    )


def finalize(df: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Drop rows with no locality, select output columns, and check IDs are unique."""
    has_locality = df["locality"].notna()
    out = df[has_locality][list(OUTPUT_COLUMNS)].reset_index(drop=True)
    if out["id"].duplicated().any():
        dupes = out.loc[out["id"].duplicated(keep=False), ["id", "name", "address"]]
        raise ValueError(f"duplicate restaurant IDs after cleaning:\n{dupes.head(10)}")
    return out, int((~has_locality).sum())


# =============================================================================
# Pipeline
# =============================================================================


@dataclass
class CleanStats:
    rows_in: int = 0
    dropped_invalid: int = 0
    dropped_no_locality: int = 0
    duplicates_removed: int = 0
    rows_out: int = 0
    no_rating: int = 0
    new: int = 0
    invalid_rating: int = 0
    no_cost: int = 0
    band_counts: dict[str, int] | None = None

    @property
    def dropped_share(self) -> float:
        """Share of input rows dropped as unusable (duplicates don't count)."""
        dropped = self.dropped_invalid + self.dropped_no_locality
        return dropped / self.rows_in if self.rows_in else 0.0

    def lines(self) -> list[str]:
        bands = self.band_counts or {}
        return [
            f"rows in:              {self.rows_in:>8,}",
            f"dropped (no name/addr): {self.dropped_invalid:>6,}",
            f"dropped (no locality):  {self.dropped_no_locality:>6,}",
            f"duplicates removed:   {self.duplicates_removed:>8,}",
            f"rows out:             {self.rows_out:>8,}",
            f"  no rating:          {self.no_rating:>8,}"
            f"  (new: {self.new:,}, invalid: {self.invalid_rating:,})",
            f"  no cost:            {self.no_cost:>8,}",
            "  budget bands:       "
            + ", ".join(f"{band} {bands.get(band, 0):,}" for band in ("low", "medium", "high")),
        ]


def clean(raw: pd.DataFrame, thresholds: BudgetThresholds) -> tuple[pd.DataFrame, CleanStats]:
    """Run the full cleaning pipeline on a raw DataFrame."""
    stats = CleanStats(rows_in=len(raw))

    df = standardize_columns(raw)
    df = clean_text_fields(df)
    df, stats.dropped_invalid = drop_invalid(df)
    df = parse_rating(df)
    df = parse_cost(df)
    df = parse_bools(df)
    df = split_list_fields(df)
    df, stats.duplicates_removed = deduplicate(df)
    # Reviews are the most expensive field to parse, so do it after deduplication.
    df = extract_review_snippets(df)
    df = derive_fields(df, thresholds)
    df = derive_tags(df)

    stats.invalid_rating = int(df["rating_invalid"].sum())
    df, stats.dropped_no_locality = finalize(df)

    stats.rows_out = len(df)
    stats.no_rating = int(df["rating"].isna().sum())
    stats.new = int(df["is_new"].sum())
    stats.no_cost = int(df["cost_for_two"].isna().sum())
    stats.band_counts = {k: int(v) for k, v in df["budget_band"].value_counts().items()}
    return df, stats


def _sorted_unique(values: Iterable[Any]) -> list[str]:
    return sorted({v for v in values if isinstance(v, str) and v})


def _strip_mojibake_runs(text: str) -> str:
    """Remove unrepairable mojibake from review text.

    Most runs stand in for an apostrophe ("it<junk>s", "you<junk>re"), so one is put back
    where a contraction follows; anything else is dropped.
    """
    return _MOJIBAKE_RUN_RE.sub(lambda m: "'" if m.group(1) else "", text)
