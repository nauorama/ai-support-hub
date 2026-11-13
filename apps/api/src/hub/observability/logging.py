"""structlog JSON logs to stdout; every record is masked before it is written (G9).

Logs from the standard library (uvicorn, libraries) go through the same chain, so the masking
processor sees every line the process writes, not only the lines of our own code.
"""

import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog
import structlog.tracebacks
from structlog.typing import EventDict, Processor, WrappedLogger

# TEMPORARY (P2-01): a stricter stand-in until `guardrails/masking.py` exists (P2-03).
# Any run of 13 or more digits becomes [REDACTED] as a whole: no Luhn check and no upper length,
# so a card number with the expiry typed after it is caught too. Between two digits it allows up
# to three separators (whitespace including NBSP, - + . / _) or a URL-encoded space, plus or dash.
# P2-03 removes this and wires in masking.py (docs/build-plan.md, task P2-03).
_TEMPORARY_SEPARATOR = r"(?:[\s\-+./_]{1,3}|%20|%2[bBdD])"
_TEMPORARY_CARD_LIKE = re.compile(rf"\d(?:{_TEMPORARY_SEPARATOR}?\d){{12,}}")
_TEMPORARY_REPLACEMENT = "[REDACTED]"


def mask_text(text: str) -> str:
    """The log mask, for code that must know whether a value would change in the logs."""
    return _TEMPORARY_CARD_LIKE.sub(_TEMPORARY_REPLACEMENT, text)


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
