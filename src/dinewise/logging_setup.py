"""Structured JSON logging with a per-request ID (arch §12.2).

Every log record emitted while a request is being handled carries its `request_id`,
taken from a context variable that the API middleware sets.
"""

from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from datetime import UTC, datetime

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

# Attributes every LogRecord has; anything else was passed via `extra=` and is logged.
_STANDARD_ATTRS = frozenset(vars(logging.makeLogRecord({}))) | {"message", "asctime"}
_HANDLER_NAME = "dinewise-json"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        request_id = getattr(record, "request_id", None)
        if request_id:
            entry["request_id"] = request_id
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRS and key != "request_id":
                entry[key] = value
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False, default=str)


def _install_request_id_factory() -> None:
    """Stamp every new LogRecord with the current request ID.

    Done at record creation, not in a formatter, so the ID survives on the record for any
    handler, including ones that format later or in another thread.
    """
    current = logging.getLogRecordFactory()
    if getattr(current, "_dinewise", False):
        return

    def factory(*args, **kwargs) -> logging.LogRecord:
        record = current(*args, **kwargs)
        record.request_id = request_id_var.get()
        return record

    factory._dinewise = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(factory)


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    """Install one stderr handler on the `dinewise` logger. Safe to call repeatedly."""
    _install_request_id_factory()
    logger = logging.getLogger("dinewise")
    logger.setLevel(level.upper())
    logger.propagate = fmt != "json"  # JSON lines shouldn't be duplicated by root handlers
    for handler in list(logger.handlers):
        if handler.get_name() == _HANDLER_NAME:
            logger.removeHandler(handler)
    if fmt == "json":
        handler = logging.StreamHandler()
        handler.set_name(_HANDLER_NAME)
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
