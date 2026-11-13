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
from structlog.typing import EventDict, Processor, WrappedLogger

# TEMPORARY (P2-01): a stricter stand-in until `guardrails/masking.py` exists (P2-03).
# Any run of 13-19 digits, optionally split by single spaces or dashes, becomes [REDACTED].
# No Luhn check, so it masks too much rather than too little. P2-03 removes this and wires in
# masking.py (docs/build-plan.md, task P2-03).
_TEMPORARY_CARD_LIKE = re.compile(r"(?<!\d)\d(?:[ -]?\d){12,18}(?!\d)")
_TEMPORARY_REPLACEMENT = "[REDACTED]"


def _temporary_mask(text: str) -> str:
    return _TEMPORARY_CARD_LIKE.sub(_TEMPORARY_REPLACEMENT, text)


def _mask_value(value: object) -> object:
    if isinstance(value, str):
        return _temporary_mask(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        # A card number logged as an int must not slip through.
        text = str(value)
        masked = _temporary_mask(text)
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
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_shared_processors(),
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            # Tracebacks become data first, so the masking below also covers exception text.
            structlog.processors.dict_tracebacks,
            mask_processor,
            structlog.processors.JSONRenderer(),
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # uvicorn installs its own unmasked handlers; send its records through the root instead.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True

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
