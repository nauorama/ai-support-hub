"""structlog JSON logs to stdout; every record is masked before it is written (G9).

The mask is the card-data mask of G1 (`hub.guardrails.masking`), so logs follow the same rules
as stored text.

Logs from the standard library (uvicorn, libraries) go through the same chain, so the masking
processor sees every line the process writes, not only the lines of our own code.
"""

import logging
import sys
from collections.abc import MutableMapping
from typing import Any

import psycopg
import sqlalchemy.exc
import structlog
import structlog.tracebacks
from structlog.typing import EventDict, Processor, WrappedLogger

from hub.guardrails.masking import mask_text


def _mask_value(value: object) -> object:
    if isinstance(value, str):
        return mask_text(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        # A card number logged as an int must not slip through.
        text = str(value)
        masked = mask_text(text)
        return value if masked == text else masked
    if isinstance(value, dict):
        return {key: _mask_value(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_mask_value(item) for item in value]
    return value


def mask_processor(_logger: WrappedLogger, _method: str, event_dict: EventDict) -> EventDict:
    """Mask every value of the record, nested ones included."""
    masked: MutableMapping[str, Any] = {key: _mask_value(v) for key, v in event_dict.items()}
    return masked


class DatabaseErrorSummary(Exception):
    """Stands in for a database error in the logs: its class and constraint name only."""


def _find_database_error(exc: BaseException | None, seen: set[int] | None = None) -> Any:  # noqa: ANN401
    """The first database error in `exc`, its cause/context chain or exception groups."""
    seen = set() if seen is None else seen
    if exc is None or id(exc) in seen:
        return None
    seen.add(id(exc))
    if isinstance(exc, sqlalchemy.exc.StatementError | psycopg.Error):
        return exc
    if isinstance(exc, BaseExceptionGroup):
        for inner in exc.exceptions:
            found = _find_database_error(inner, seen)
            if found is not None:
                return found
    return _find_database_error(exc.__cause__, seen) or _find_database_error(exc.__context__, seen)


def _constraint_name(error: BaseException) -> str | None:
    orig = error.orig if isinstance(error, sqlalchemy.exc.StatementError) else error
    diag = getattr(orig, "diag", None)
    name = getattr(diag, "constraint_name", None)
    return name if isinstance(name, str) else None


def database_error_processor(
    _logger: WrappedLogger, _method: str, event_dict: EventDict
) -> EventDict:
    """Replace a database error with its class and constraint name before it is rendered.

    The text of a database error can hold customer text: Postgres adds `DETAIL: Failing row
    contains (...)` for CHECK and NOT NULL violations, with the values of the row, and
    `hide_parameters` (hub/db/session.py) does not cover the server's detail. The frames stay
    (file, line, function; locals are off), the messages of the whole chain go."""
    exc_info = event_dict.get("exc_info")
    if exc_info is True:
        exc_info = sys.exc_info()
    exc = exc_info if isinstance(exc_info, BaseException) else None
    if isinstance(exc_info, tuple) and len(exc_info) == 3:
        exc = exc_info[1]
    error = _find_database_error(exc)
    if error is None or exc is None:
        return event_dict
    error_class = f"{type(error).__module__}.{type(error).__qualname__}"
    constraint = _constraint_name(error)
    summary = DatabaseErrorSummary(f"{error_class} (constraint: {constraint or 'none'})")
    summary.__suppress_context__ = True
    event_dict["exc_info"] = (DatabaseErrorSummary, summary, exc.__traceback__)
    event_dict["db_error_class"] = error_class
    event_dict["db_constraint"] = constraint
    return event_dict


def _shared_processors() -> list[Processor]:
    return [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]


def configure_logging(level: str) -> None:
    """Send structlog and standard-library logs as masked JSON lines to stdout."""
    # Tracebacks become data first, so the masking below also covers exception text. Frame
    # locals are off: they hold request bodies, settings and connection strings, which no mask
    # can be trusted to clean (structlog's default is show_locals=True).
    tracebacks = structlog.processors.ExceptionRenderer(
        structlog.tracebacks.ExceptionDictTransformer(show_locals=False)
    )
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_shared_processors(),
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            database_error_processor,
            tracebacks,
            mask_processor,
            structlog.processors.JSONRenderer(),
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    handler.setLevel(level)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # uvicorn installs its own unmasked handlers before it calls the app factory; send its
    # records through the root handler instead.
    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True
        # Inherit the root level, so LOG_LEVEL=DEBUG also shows uvicorn's DEBUG records.
        uvicorn_logger.setLevel(logging.NOTSET)
    # The access log is off: its request line carries the raw query string, which may hold
    # URL-encoded card data. RequestContextMiddleware logs method, path and status instead.
    access_logger = logging.getLogger("uvicorn.access")
    access_logger.handlers = []
    access_logger.propagate = False
    access_logger.disabled = True
    # HTTP clients log every URL at INFO, query string included; only their warnings are kept.
    for name in ("httpx2", "httpcore2"):
        logging.getLogger(name).setLevel(logging.WARNING)

    structlog.configure(
        processors=[
            *_shared_processors(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(level),
        # Off so that reconfiguring (tests, a second app) takes effect for existing loggers.
        cache_logger_on_first_use=False,
    )
