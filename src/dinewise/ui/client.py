"""HTTP client for the DineWise REST API, used by the Streamlit app (arch §10).

The UI talks to the API only through this module, never to the Python service
directly, so it can run in a separate process or container.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

import httpx

DEFAULT_API_URL = "http://127.0.0.1:8000"
# Longer than the API's own LLM deadline (DINEWISE_LLM_DEADLINE_S, 20 s by default), so
# the UI never gives up on a request the API is still allowed to finish (edge case UI-16).
DEFAULT_TIMEOUT_S = 60.0


def api_url_from_env() -> str:
    return os.environ.get("DINEWISE_API_URL", "").strip().rstrip("/") or DEFAULT_API_URL


def timeout_from_env() -> float:
    try:
        return float(os.environ.get("DINEWISE_UI_TIMEOUT_S", "") or DEFAULT_TIMEOUT_S)
    except ValueError:
        return DEFAULT_TIMEOUT_S


@dataclass
class ApiError(Exception):
    """The API answered with an error, or could not be reached (`status` is None)."""

    status: int | None
    message: str
    code: str | None = None
    field: str | None = None
    value: str | None = None
    suggestions: list[str] = dc_field(default_factory=list)
    request_id: str | None = None

    def __str__(self) -> str:
        return self.message

    @property
    def unreachable(self) -> bool:
        return self.status is None


class ApiClient:
    def __init__(
        self,
        base_url: str | None = None,
        *,
        timeout: float | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url or api_url_from_env()).rstrip("/")
        self._timeout = timeout if timeout is not None else timeout_from_env()
        self._transport = transport  # tests pass an httpx.MockTransport

    # --- Endpoints ------------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """`/healthz`. Returns the body for 503 too, since it says why data isn't loaded."""
        response = self._request("GET", "/healthz")
        if response.status_code in (200, 503):
            return response.json()
        raise _error_from_response(response)

    def locations(self) -> dict[str, Any]:
        return self._get_json("/api/v1/meta/locations")

    def cuisines(self) -> dict[str, Any]:
        return self._get_json("/api/v1/meta/cuisines")

    def budgets(self) -> dict[str, Any]:
        return self._get_json("/api/v1/meta/budgets")

    def recommend(self, preferences: dict[str, Any]) -> dict[str, Any]:
        response = self._request("POST", "/api/v1/recommendations", json=preferences)
        if response.status_code != 200:
            raise _error_from_response(response)
        return response.json()

    # --- Plumbing -------------------------------------------------------------------

    def _get_json(self, path: str) -> dict[str, Any]:
        response = self._request("GET", path)
        if response.status_code != 200:
            raise _error_from_response(response)
        return response.json()

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            with httpx.Client(
                base_url=self.base_url, timeout=self._timeout, transport=self._transport
            ) as http:
                return http.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise ApiError(None, "The DineWise service took too long to answer.") from exc
        except httpx.HTTPError as exc:
            raise ApiError(None, f"Can't reach the DineWise service at {self.base_url}.") from exc


def _error_from_response(response: httpx.Response) -> ApiError:
    status = response.status_code
    request_id = response.headers.get("x-request-id")
    try:
        body = response.json()
    except ValueError:
        body = None

    # Our own errors: {"error": {"code", "message", "field", "value", "suggestions"}}
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        err = body["error"]
        return ApiError(
            status,
            str(err.get("message") or "Request failed"),
            code=err.get("code"),
            field=err.get("field"),
            value=err.get("value"),
            suggestions=list(err.get("suggestions") or []),
            request_id=body.get("request_id") or request_id,
        )

    # FastAPI request validation: {"detail": [{"loc": [...], "msg": ...}, ...]}
    if isinstance(body, dict) and isinstance(body.get("detail"), list) and body["detail"]:
        first = body["detail"][0]
        loc = [str(part) for part in first.get("loc", []) if part != "body"]
        name = loc[0] if loc else None
        message = str(first.get("msg") or "Invalid input")
        return ApiError(
            status,
            f"{name}: {message}" if name else message,
            code="validation_error",
            field=name,
            request_id=request_id,
        )

    defaults = {
        429: "Too many requests. Please try again in a minute.",
        503: "The DineWise service is temporarily unavailable.",
    }
    message = defaults.get(status) or (
        "The DineWise service hit an error." if status >= 500 else f"Request failed ({status})."
    )
    return ApiError(status, message, request_id=request_id)
