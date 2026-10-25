"""Match user preferences against the dataset (arch §6.2).

Locations and cuisines are matched case-insensitively, then through a small alias list,
then by fuzzy match (rapidfuzz). Values that still don't match raise `UnknownValueError`
with suggestions. Free-text extras are scanned for keywords that map to restaurant tags.
"""

from __future__ import annotations

import re

from rapidfuzz import fuzz, process

from dinewise.domain.models import Correction, NormalizedPreferences, UserPreferences
from dinewise.retrieval.repository import RestaurantRepository

CITY_ALIASES: dict[str, str] = {
    "bangalore": "Bangalore",
    "bengaluru": "Bangalore",
    "banglore": "Bangalore",
    "blr": "Bangalore",
}

# Cities people are likely to ask for that the dataset doesn't cover (edge case INP-06).
UNSUPPORTED_CITIES = frozenset(
    {
        "delhi",
        "new delhi",
        "mumbai",
        "bombay",
        "pune",
        "hyderabad",
        "chennai",
        "kolkata",
        "gurgaon",
        "gurugram",
        "noida",
        "ahmedabad",
        "jaipur",
        "goa",
        "kochi",
        "mysore",
        "mysuru",
    }
)

LOCALITY_ALIASES: dict[str, str] = {
    "btm layout": "BTM",
    "hsr layout": "HSR",
    "indira nagar": "Indiranagar",
    "j p nagar": "JP Nagar",
    "jayanagara": "Jayanagar",
    "mg road": "MG Road",
    "m g road": "MG Road",
    "ecity": "Electronic City",
    "e city": "Electronic City",
    "cv raman nagar": "CV Raman Nagar",
    "kr puram": "KR Puram",
    "rt nagar": "RT Nagar",
}

CUISINE_ALIASES: dict[str, str] = {
    "sea food": "Seafood",
    "ice-cream": "Ice Cream",
    "icecream": "Ice Cream",
    "barbecue": "BBQ",
    "bbq": "BBQ",
    "afghani": "Afghan",
    "dessert": "Desserts",
    "burgers": "Burger",
    "sandwiches": "Sandwich",
    "momo": "Momos",
    "wrap": "Wraps",
    "roll": "Rolls",
    "juice": "Juices",
}

_CUISINE_SUFFIX_RE = re.compile(r"\s+(food|cuisine|dishes)$")
_CITY_SUFFIX_RE = re.compile(r",?\s*\b(bangalore|bengaluru)$", re.IGNORECASE)
_WS_RE = re.compile(r"\s+")

# Keyword patterns for each tag. Matched on word boundaries, case-insensitively.
TAG_KEYWORDS: dict[str, tuple[str, ...]] = {
    "family_friendly": (r"family", r"families", r"kids?", r"child(ren)?", r"parents"),
    "quick_service": (r"quick", r"fast", r"hurry", r"take ?away", r"grab", r"on the go"),
    "table_booking": (r"book(ing)?", r"reserv(e|ation|ations)", r"table booking"),
    "delivery": (r"deliver(y|s)?", r"order in", r"home delivery"),
    "bar": (r"bars?", r"pubs?", r"beers?", r"drinks", r"cocktails?", r"brew(ery|eries)?"),
    "buffet": (r"buffets?",),
    "cafe": (r"caf[eé]s?", r"coffee"),
    "desserts": (r"desserts?", r"sweets?", r"cakes?", r"ice ?cream", r"bakery"),
    "fine_dining": (r"fine dining", r"upscale", r"fancy", r"luxur(y|ious)", r"romantic"),
}
_TAG_PATTERNS = {
    tag: re.compile(r"\b(" + "|".join(words) + r")\b", re.IGNORECASE)
    for tag, words in TAG_KEYWORDS.items()
}
_NEGATIONS = frozenset(
    {"no", "not", "without", "avoid", "don't", "dont", "never", "non", "nothing", "except"}
)
_CLAUSE_SPLIT_RE = re.compile(r"[,.;!?]|\bbut\b|\band\b|\bor\b", re.IGNORECASE)
_WORD_RE = re.compile(r"[a-z']+")

SUGGESTION_LIMIT = 3
AMBIGUITY_MARGIN = 3  # two fuzzy matches this close are treated as ambiguous (INP-03)


class UnknownValueError(ValueError):
    """A location or cuisine that couldn't be matched; carries up to 3 suggestions."""

    def __init__(
        self, field: str, value: str, suggestions: list[str], message: str | None = None
    ) -> None:
        self.field = field
        self.value = value
        self.suggestions = suggestions
        self.message = message or f"Unknown {field.rstrip('s')} '{value}'"
        hint = f" Did you mean: {', '.join(suggestions)}?" if suggestions else ""
        super().__init__(self.message + "." + hint)


def extract_tags(text: str | None) -> list[str]:
    """Map free-text extras to tags, ignoring negated phrases (edge case INP-28).

    "family friendly, not a bar" -> ["family_friendly"]; "no buffet" -> [].
    """
    if not text:
        return []
    tags: set[str] = set()
    for clause in _CLAUSE_SPLIT_RE.split(text):
        lowered = clause.lower()
        for tag, pattern in _TAG_PATTERNS.items():
            for match in pattern.finditer(lowered):
                before = set(_WORD_RE.findall(lowered[: match.start()]))
                if not before & _NEGATIONS:
                    tags.add(tag)
                    break
    return sorted(tags)


class PreferenceNormalizer:
    def __init__(self, repo: RestaurantRepository, fuzzy_threshold: int = 85) -> None:
        self._repo = repo
        self._threshold = fuzzy_threshold
        self._localities = {name.lower(): name for name in repo.known_localities()}
        self._cuisines = {name.lower(): name for name in repo.known_cuisines()}
        cities = repo.cities()
        self._city = cities[0] if len(cities) == 1 else None
        self._busiest = [
            name
            for name, _ in sorted(repo.locality_counts().items(), key=lambda kv: -kv[1])[
                :SUGGESTION_LIMIT
            ]
        ]

    def normalize(self, prefs: UserPreferences) -> NormalizedPreferences:
        corrections: list[Correction] = []
        location, kind = self._match_location(prefs.location, corrections)
        localities = [] if kind == "city" else self._repo.locality_group(location)
        cuisines = [self._match_cuisine(c, corrections) for c in prefs.cuisines]
        cuisines = list(dict.fromkeys(cuisines))  # "italian" + "Italian " -> one (INP-12)

        return NormalizedPreferences(
            location=location,
            location_kind=kind,
            city=self._city or location,
            localities=localities,
            budget=prefs.budget,
            cuisines=cuisines,
            min_rating=prefs.min_rating,
            extras=prefs.extras,
            extras_tags=extract_tags(prefs.extras),
            top_k=prefs.top_k,
            corrections=corrections,
        )

    # --- Location ---------------------------------------------------------------------

    def _match_location(self, raw: str, corrections: list[Correction]) -> tuple[str, str]:
        text = _WS_RE.sub(" ", raw).strip()
        if not text:
            raise UnknownValueError("location", raw, self._busiest, "Location is required")
        key = text.lower()

        if key in CITY_ALIASES and CITY_ALIASES[key] in self._repo.cities():
            return self._accept("location", raw, CITY_ALIASES[key], corrections), "city"  # INP-05

        stripped = _CITY_SUFFIX_RE.sub("", text).strip(" ,")  # "Indiranagar, Bangalore"
        if stripped and stripped.lower() != key:
            text, key = stripped, stripped.lower()

        if key in UNSUPPORTED_CITIES:
            raise UnknownValueError(
                "location",
                raw,
                self._busiest,
                f"DineWise currently covers Bangalore only, not '{text}'",
            )

        if key in LOCALITY_ALIASES and LOCALITY_ALIASES[key] in self._repo.locality_counts():
            return self._accept("location", raw, LOCALITY_ALIASES[key], corrections), "locality"
        if key in self._localities:
            return self._accept("location", raw, self._localities[key], corrections), "locality"

        match = self._fuzzy("location", text, self._localities)
        return self._accept("location", raw, match, corrections), "locality"

    # --- Cuisine ----------------------------------------------------------------------

    def _match_cuisine(self, raw: str, corrections: list[Correction]) -> str:
        text = _WS_RE.sub(" ", raw).strip()
        key = text.lower()
        for candidate in (key, _CUISINE_SUFFIX_RE.sub("", key)):
            canonical = CUISINE_ALIASES.get(candidate) or self._cuisines.get(candidate)
            if canonical and canonical.lower() in self._cuisines:
                return self._accept("cuisines", raw, self._cuisines[canonical.lower()], corrections)
        match = self._fuzzy("cuisines", text, self._cuisines)
        return self._accept("cuisines", raw, match, corrections)

    # --- Helpers ----------------------------------------------------------------------

    def _fuzzy(self, field: str, text: str, choices: dict[str, str]) -> str:
        """Return the single close match, or raise with suggestions (INP-02..04, INP-11)."""
        key = text.lower()
        scored = process.extract(key, list(choices), scorer=fuzz.ratio, limit=2)
        if scored and scored[0][1] >= self._threshold:
            ambiguous = (
                len(scored) > 1
                and scored[1][1] >= self._threshold
                and scored[0][1] - scored[1][1] < AMBIGUITY_MARGIN
            )
            if not ambiguous:
                return choices[scored[0][0]]
            suggestions = [choices[k] for k, _, _ in scored]
            raise UnknownValueError(
                field, text, suggestions, f"'{text}' matches more than one {field.rstrip('s')}"
            )
        suggestions = [
            choices[k]
            for k, _, _ in process.extract(key, list(choices), scorer=fuzz.WRatio, limit=3)
        ]
        raise UnknownValueError(field, text, suggestions)

    @staticmethod
    def _accept(field: str, raw: str, value: str, corrections: list[Correction]) -> str:
        if _WS_RE.sub(" ", raw).strip().lower() != value.lower():
            corrections.append(Correction(field=field, input=raw, value=value))
        return value
