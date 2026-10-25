"""FastAPI application factory (arch §9).

uvicorn dinewise.api.main:app --reload
"""

from __future__ import annotations

import logging
import math
import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from dinewise import __version__
from dinewise.api.ratelimit import RateLimiter, client_ip
from dinewise.api.routes import ServiceUnavailableError, api, health
from dinewise.api.schemas import ErrorDetail, ErrorResponse
from dinewise.config import Settings, get_settings
from dinewise.llm.ranker import GroqRanker
from dinewise.logging_setup import configure_logging, request_id_var
from dinewise.retrieval.normalize import UnknownValueError
from dinewise.retrieval.repository import DataNotLoadedError, RestaurantRepository
from dinewise.service.recommender import Ranker, Recommender

log = logging.getLogger("dinewise.api")

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
RATE_LIMITED_PATH = "/api/v1/recommendations"  # the endpoint that calls the LLM (SEC-09)
WEB_DIR = Path(__file__).resolve().parent.parent / "web"  # the browser frontend (arch §10)


def build_ranker(settings: Settings) -> Ranker | None:
    """The LLM ranker used in production: Groq, when `GROQ_API_KEY` is set.

    Without a key there is none, and every response uses rule-based ranking (`degraded`).
    """
    if not settings.llm_enabled:
        log.warning("GROQ_API_KEY not set; serving rule-based rankings only")
        return None
    return GroqRanker(settings)


def create_app(
    settings: Settings | None = None,
    *,
    repository: RestaurantRepository | None = None,
    ranker: Ranker | None = None,
) -> FastAPI:
    """Build the app. Tests pass `repository` and `ranker` to skip loading real data."""
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_format)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if app.state.recommender is None:
            try:
                repo = RestaurantRepository.from_parquet(settings.data_path, settings)
            except DataNotLoadedError as exc:
                # Keep running so /healthz can report the problem (edge cases API-01, API-02).
                app.state.load_error = str(exc)
                log.error("restaurant data not loaded: %s", exc)
            else:
                app.state.recommender = Recommender(
                    repo, settings, ranker if ranker is not None else build_ranker(settings)
                )
        yield

    app = FastAPI(
        title="DineWise API",
        version=__version__,
        description=(
            "AI-powered restaurant recommendations for Bengaluru, from the Zomato dataset. "
            "Filters real restaurants by your preferences, then ranks and explains the "
            "best matches."
        ),
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.load_error = None
    app.state.recommender = (
        Recommender(repository, settings, ranker) if repository is not None else None
    )

    _add_middleware(app, settings)
    _add_exception_handlers(app)
    app.include_router(api)
    app.include_router(health)
    _add_web_frontend(app)
    return app


def _add_web_frontend(app: FastAPI) -> None:
    """Serve the browser UI: the page at "/", its assets under "/static".

    Not a catch-all mount at "/", which would turn the API's 405s into 404s (API-05).
    """

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=WEB_DIR), name="web")


def _add_middleware(app: FastAPI, settings: Settings) -> None:
    limit = settings.rate_limit_per_minute
    limiter = RateLimiter(limit) if limit > 0 else None

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        incoming = request.headers.get("x-request-id", "")
        request_id = incoming if _REQUEST_ID_RE.match(incoming) else uuid.uuid4().hex
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        try:
            length = request.headers.get("content-length")
            retry_after = (
                limiter.hit(client_ip(request, settings.forwarded_hops))
                if limiter and request.method == "POST" and request.url.path == RATE_LIMITED_PATH
                else None
            )
            if length and length.isdigit() and int(length) > settings.max_request_bytes:
                response = _error(
                    413,
                    "request_too_large",
                    f"Request body is larger than {settings.max_request_bytes} bytes",
                )
            elif retry_after is not None:
                response = _error(
                    429,
                    "rate_limited",
                    f"Too many requests. Limit: {limit} per minute. Please try again shortly.",
                )
                response.headers["Retry-After"] = str(max(1, math.ceil(retry_after)))
            else:
                try:
                    response = await call_next(request)
                except Exception:
                    # Caught here, not by an app-level handler, so the 500 still carries
                    # the request ID. Details go to the log, never to the client (SEC-11).
                    log.exception("unhandled error")
                    response = _error(
                        500, "internal_error", "Something went wrong. Please try again."
                    )
            response.headers["X-Request-ID"] = request_id
            log.info(
                "request",
                extra={
                    "event": "http_request",
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                },
            )
            return response
        finally:
            request_id_var.reset(token)

    # Added last so it wraps everything, including error responses (SEC-14).
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_origin_regex=settings.cors_origin_regex,
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type", "X-Request-ID"],
        expose_headers=["X-Request-ID", "Retry-After"],
    )


def _add_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(UnknownValueError)
    async def unknown_value(request: Request, exc: UnknownValueError) -> JSONResponse:
        return _error(
            422,
            "unknown_value",
            exc.message,
            field=exc.field,
            value=exc.value,
            suggestions=exc.suggestions,
        )

    @app.exception_handler(ServiceUnavailableError)
    async def unavailable(request: Request, exc: ServiceUnavailableError) -> JSONResponse:
        return _error(503, "data_unavailable", f"Service unavailable: {exc}")


def _error(status: int, code: str, message: str, **detail) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorDetail(code=code, message=message, **detail),
        request_id=request_id_var.get(),
    )
    return JSONResponse(status_code=status, content=body.model_dump())


app = create_app()
