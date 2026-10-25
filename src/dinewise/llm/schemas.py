"""The LLM's structured output (arch §8.3), and its conversion to Groq's strict JSON schema."""

from __future__ import annotations

import copy
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class RankedPick(BaseModel):
    model_config = ConfigDict(extra="forbid")

    restaurant_id: str = Field(description="Candidate id")
    rank: int = Field(description="1 = best")
    fit_score: int = Field(description="Fit, 1-10")
    explanation: str = Field(description="One sentence")
    caveats: str | None = Field(description="Missed preference, or null")

    @field_validator("fit_score", mode="before")
    @classmethod
    def _clamp_fit_score(cls, value: Any) -> Any:
        # Out-of-range scores are clamped, not rejected: one bad number shouldn't throw
        # away an otherwise good ranking (edge case LLM-14).
        if isinstance(value, int | float):
            return int(min(10, max(1, round(value))))
        return value


class LLMRankingResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    picks: list[RankedPick] = Field(description="Best fit first")
    summary: str = Field(description="1-2 sentences")


# Keywords that strict mode doesn't need, or may not support. Range and length limits are
# enforced after parsing instead (see `RankedPick._clamp_fit_score`).
_DROP_KEYS = frozenset(
    {
        "title",
        "default",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "pattern",
        "format",
    }
)


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Convert a Pydantic model's JSON schema into Groq's strict-mode form.

    Strict mode requires every property to be listed in `required` and every object to
    set `additionalProperties: false`. Optional fields stay as `anyOf [type, null]`.
    """
    return _strictify(copy.deepcopy(model.model_json_schema()))


def ranking_schema(candidate_ids: Sequence[str]) -> dict[str, Any]:
    """The response schema for one request, with `restaurant_id` limited to the candidates.

    Strict mode decodes against the schema, so the model can't produce an ID outside the
    shortlist. The validator still checks, as a second line of defence.
    """
    schema = strict_json_schema(LLMRankingResult)
    if candidate_ids:
        pick = schema["$defs"]["RankedPick"]["properties"]["restaurant_id"]
        pick["enum"] = list(dict.fromkeys(candidate_ids))
    return schema


def _strictify(node: Any) -> Any:
    if isinstance(node, dict):
        for key in list(node):
            if key in _DROP_KEYS:
                del node[key]
        for value in node.values():
            _strictify(value)
        if node.get("type") == "object" or "properties" in node:
            props = node.get("properties", {})
            node["required"] = list(props)
            node["additionalProperties"] = False
    elif isinstance(node, list):
        for item in node:
            _strictify(item)
    return node
