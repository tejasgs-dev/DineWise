"""Rank a shortlist with an LLM on Groq (implementation plan, Phase 3).

One request per attempt: the primary model first, then the fallback model once if the
first gives no usable answer. Everything runs inside an overall deadline. Any failure
ends in `None`, which makes the recommender serve the rule-based ranking (`degraded`).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import groq
from pydantic import ValidationError

from dinewise.config import Settings
from dinewise.domain.models import RetrievalResult
from dinewise.llm.client import MAX_RETRIES, make_client
from dinewise.llm.prompts import build_messages
from dinewise.llm.schemas import LLMRankingResult, ranking_schema
from dinewise.llm.validator import ResponseValidator, ValidationReport
from dinewise.service.recommender import RankingOutcome

log = logging.getLogger(__name__)

# Don't start an attempt with less time than this left before the deadline.
MIN_ATTEMPT_S = 1.0


@dataclass
class Attempt:
    """What happened on one call to one model. Kept for logs and the eval."""

    model: str
    number: int  # 1 = primary, 2 = fallback
    status: str  # "ok", or why there was no usable result
    latency_ms: int = 0
    served_model: str | None = None
    finish_reason: str | None = None
    usage: dict[str, int | float] = field(default_factory=dict)
    raw_content: str | None = None
    raw: LLMRankingResult | None = None  # parsed, before validation
    error: str | None = None


@dataclass
class RankingReport:
    """The full story of one `rank` call: every attempt, and the validated result."""

    attempts: list[Attempt] = field(default_factory=list)
    validation: ValidationReport | None = None
    outcome: RankingOutcome | None = None


class _Fatal(Exception):
    """Errors that another model won't fix (bad or revoked key)."""


class GroqRanker:
    def __init__(
        self,
        settings: Settings,
        client: groq.Groq | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._settings = settings
        self._client = client if client is not None else make_client(settings)
        self._clock = clock
        self._validator = ResponseValidator(settings)

    def rank(self, result: RetrievalResult) -> RankingOutcome | None:
        return self.rank_detailed(result).outcome

    def rank_detailed(self, result: RetrievalResult) -> RankingReport:
        report = RankingReport()
        if not result.candidates:
            return report

        s = self._settings
        prefs = result.preferences
        messages = build_messages(prefs, result.candidates, result.relaxations, prefs.top_k, s)
        schema = ranking_schema([c.id for c in result.candidates])
        deadline = self._clock() + s.llm_deadline_s
        models = [s.model] + ([s.fallback_model] if s.fallback_model else [])

        for number, model in enumerate(models, start=1):
            remaining = deadline - self._clock()
            if remaining < MIN_ATTEMPT_S:
                log.warning("llm deadline reached before attempt %d (%s)", number, model)
                report.attempts.append(Attempt(model=model, number=number, status="deadline"))
                break
            try:
                attempt = self._call(model, number, messages, schema, remaining)
            except _Fatal as exc:
                report.attempts.append(
                    Attempt(model=model, number=number, status="auth_error", error=str(exc))
                )
                break
            report.attempts.append(attempt)
            if attempt.raw is None:
                continue
            validation = self._validator.validate(attempt.raw, result)
            if validation is None:
                attempt.status = "no_valid_picks"
                log.warning("llm returned no valid picks", extra={"model": model})
                continue
            report.validation = validation
            report.outcome = RankingOutcome(
                recommendations=validation.recommendations,
                summary=validation.summary,
                model=attempt.served_model or model,  # the model that answered (LLM-23)
                usage={**attempt.usage, "attempt": number},
            )
            if validation.unknown_ids or validation.duplicate_ids or validation.backfilled:
                log.warning(
                    "llm ranking repaired",
                    extra={
                        "unknown_ids": validation.unknown_ids,
                        "duplicate_ids": validation.duplicate_ids,
                        "backfilled": validation.backfilled,
                    },
                )
            return report
        return report

    # --- One call -------------------------------------------------------------------------

    def _call(
        self,
        model: str,
        number: int,
        messages: list[dict[str, str]],
        schema: dict[str, Any],
        remaining_s: float,
    ) -> Attempt:
        s = self._settings
        # Keep SDK retries inside the deadline: split the remaining time across tries.
        retries = MAX_RETRIES if number == 1 else 0
        per_try = min(s.llm_timeout_s, remaining_s / (retries + 1))
        attempt = Attempt(model=model, number=number, status="error")
        started = self._clock()
        try:
            response = self._client.with_options(
                timeout=per_try, max_retries=retries
            ).chat.completions.create(
                model=model,
                messages=messages,
                response_format={
                    "type": "json_schema",
                    "json_schema": {"name": "ranking", "strict": True, "schema": schema},
                },
                reasoning_effort=s.reasoning_effort,
                include_reasoning=False,
                max_completion_tokens=s.llm_max_completion_tokens,
            )
        except (groq.AuthenticationError, groq.PermissionDeniedError) as exc:
            # A config problem: another model won't help (LLM-28).
            log.error("groq rejected the API key: %s", _short(exc))
            raise _Fatal(_short(exc)) from exc
        except groq.RateLimitError as exc:
            return self._failed(attempt, started, "rate_limited", exc, logging.WARNING)
        except groq.APITimeoutError as exc:  # subclass of APIConnectionError: catch first
            return self._failed(attempt, started, "timeout", exc, logging.WARNING)
        except groq.APIConnectionError as exc:
            return self._failed(attempt, started, "connection_error", exc, logging.WARNING)
        except groq.BadRequestError as exc:
            # A schema or parameter problem: a bug or config error, not bad luck (LLM-29).
            return self._failed(attempt, started, "bad_request", exc, logging.ERROR)
        except groq.APIStatusError as exc:
            return self._failed(attempt, started, f"http_{exc.status_code}", exc, logging.WARNING)

        attempt.latency_ms = round((self._clock() - started) * 1000)
        attempt.served_model = getattr(response, "model", None)
        attempt.usage = _usage(response)
        choice = response.choices[0] if response.choices else None
        attempt.finish_reason = getattr(choice, "finish_reason", None)
        message = getattr(choice, "message", None)
        content = getattr(message, "content", None)
        attempt.raw_content = content

        if getattr(message, "refusal", None):
            attempt.status = "refusal"
        elif attempt.finish_reason != "stop":
            attempt.status = f"finish_{attempt.finish_reason}"  # e.g. cut off (LLM-24)
        elif not content or not content.strip():
            attempt.status = "empty"
        else:
            try:
                attempt.raw = LLMRankingResult.model_validate_json(content)
                attempt.status = "ok"
            except ValidationError as exc:  # LLM-25
                attempt.status = "invalid_json"
                attempt.error = _short(exc)

        log.info(
            "llm call",
            extra={
                "event": "llm_call",
                "model": model,
                "served_model": attempt.served_model,
                "attempt": number,
                "status": attempt.status,
                "finish_reason": attempt.finish_reason,
                "latency_ms": attempt.latency_ms,
                **attempt.usage,
            },
        )
        return attempt

    def _failed(
        self, attempt: Attempt, started: float, status: str, exc: Exception, level: int
    ) -> Attempt:
        attempt.status = status
        attempt.error = _short(exc)
        attempt.latency_ms = round((self._clock() - started) * 1000)
        log.log(
            level,
            "llm call failed",
            extra={
                "event": "llm_call",
                "model": attempt.model,
                "attempt": attempt.number,
                "status": status,
                "error": attempt.error,
                "latency_ms": attempt.latency_ms,
            },
        )
        return attempt


def _usage(response: Any) -> dict[str, int | float]:
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    out: dict[str, int | float] = {}
    for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
        value = getattr(usage, key, None)
        if isinstance(value, int):
            out[key] = value
    details = getattr(usage, "prompt_tokens_details", None)
    cached = getattr(details, "cached_tokens", None)
    if isinstance(cached, int):
        out["cached_tokens"] = cached
    for key in ("queue_time", "total_time"):  # seconds, reported by Groq
        value = getattr(usage, key, None)
        if isinstance(value, int | float):
            out[key] = round(float(value), 4)
    return out


def _short(exc: Exception, limit: int = 300) -> str:
    text = str(exc).replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"
