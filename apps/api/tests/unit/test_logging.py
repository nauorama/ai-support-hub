"""Logs are JSON lines on stdout, and card data never reaches them (the G1 mask, G9)."""

import json
import logging
from collections.abc import Callable
from typing import Any

import pytest
import structlog

from hub.observability.logging import configure_logging, mask_processor

LogLines = Callable[[], list[dict[str, Any]]]
TEST_CARD = "4111 1111 1111 1111"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("4111111111111111", "[CARD ****1111]"),
        ("4111 1111 1111 1111", "[CARD ****1111]"),
        ("4111-1111-1111-1111", "[CARD ****1111]"),
        ("card4111111111111111end", "card[CARD ****1111]end"),
        ("378282246310005", "[CARD ****0005]"),  # 15 digits (public Amex test number)
        ("4222222222222", "[CARD ****2222]"),  # 13 digits
        ("41111111111111111228", "[CARD ****1111]"),  # card and expiry typed together
        ("4111\u00a01111\u00a01111\u00a01111", "[CARD ****1111]"),  # no-break spaces
        ("4111+1111+1111+1111", "[CARD ****1111]"),  # URL form encoding
        ("4111%201111%201111%201111", "[CARD ****1111]"),
        ("cvv 737", "cvv [REDACTED]"),
    ],
)
def test_card_data_is_masked_with_the_g1_mask(text: str, expected: str) -> None:
    masked = mask_processor(None, "info", {"event": f"paid with {text} today"})["event"]
    assert masked == f"paid with {expected} today"


@pytest.mark.parametrize(
    "text",
    [
        "123456789012",
        "2026-11-13T10:00:00.123456Z",
        "41 11 11",
        "+49 30 1234 5678",
        "4111111111111112",  # fails Luhn: logs follow G1 (owner decision in P2-03)
    ],
)
def test_other_numbers_and_timestamps_are_kept(text: str) -> None:
    assert mask_processor(None, "info", {"event": text})["event"] == text


def test_nested_values_and_ints_are_masked() -> None:
    event = {
        "event": "x",
        "details": {"note": [f"card {TEST_CARD}", ("4111111111111111",)]},
        "card_as_int": 4111111111111111,
        "count": 3,
        "flag": True,
        "ratio": 0.5,
        "nothing": None,
    }
    masked = mask_processor(None, "info", event)
    assert masked["details"] == {"note": ["card [CARD ****1111]", ["[CARD ****1111]"]]}
    assert masked["card_as_int"] == "[CARD ****1111]"
    assert (masked["count"], masked["flag"], masked["ratio"], masked["nothing"]) == (
        3,
        True,
        0.5,
        None,
    )


def test_structlog_lines_are_masked_json(log_lines: LogLines) -> None:
    configure_logging("INFO")
    structlog.get_logger("test").info("payment.note", note=f"card {TEST_CARD}")
    (line,) = log_lines()
    assert line["event"] == "payment.note"
    assert line["note"] == "card [CARD ****1111]"
    assert line["level"] == "info"
    assert line["logger"] == "test"
    assert line["timestamp"].endswith("Z")


def test_standard_library_lines_are_masked_json(log_lines: LogLines) -> None:
    configure_logging("INFO")
    logging.getLogger("some.library").info("charged card %s", "4111111111111111")
    (line,) = log_lines()
    assert line["event"] == "charged card [CARD ****1111]"
    assert line["logger"] == "some.library"


def test_exception_tracebacks_are_masked(log_lines: LogLines) -> None:
    configure_logging("INFO")
    try:
        raise ValueError(f"bad card {TEST_CARD}")
    except ValueError:
        structlog.get_logger("test").exception("failed")
    (line,) = log_lines()
    assert "4111" not in str(line)
    assert line["exception"][0]["exc_value"] == "bad card [CARD ****1111]"


def test_level_filter_applies(log_lines: LogLines) -> None:
    configure_logging("WARNING")
    structlog.get_logger("test").info("hidden")
    logging.getLogger("lib").info("hidden too")
    structlog.get_logger("test").warning("shown")
    assert [line["event"] for line in log_lines()] == ["shown"]


def test_uvicorn_logs_after_uvicorn_configured_logging(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # uvicorn applies its own LOGGING_CONFIG before it calls the app factory.
    import logging.config

    from uvicorn.config import LOGGING_CONFIG

    logging.config.dictConfig(LOGGING_CONFIG)
    configure_logging("INFO")
    logging.getLogger("uvicorn.error").info("server card %s", "4111111111111111")
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d', "127.0.0.1", "GET", "/x?card=4111+1111+1111+1111", "1.1", 200
    )
    captured = capsys.readouterr()
    assert captured.err == ""  # nothing through uvicorn's own (unmasked) handlers
    lines = [json.loads(line) for line in captured.out.splitlines()]
    assert lines == [
        {
            "event": "server card [CARD ****1111]",
            "level": "info",
            "logger": "uvicorn.error",
            "timestamp": lines[0]["timestamp"],
        }
    ]


def test_uvicorn_follows_the_configured_level(capsys: pytest.CaptureFixture[str]) -> None:
    import logging.config

    from uvicorn.config import LOGGING_CONFIG

    logging.config.dictConfig(LOGGING_CONFIG)
    configure_logging("WARNING")
    logging.getLogger("uvicorn.error").info("hidden")
    logging.getLogger("httpx2").info("HTTP Request: GET http://x/?q=hidden")
    logging.getLogger("uvicorn.error").warning("shown")
    captured = capsys.readouterr()
    assert captured.err == ""
    assert [json.loads(line)["event"] for line in captured.out.splitlines()] == ["shown"]


def test_http_client_info_lines_are_dropped(log_lines: LogLines) -> None:
    configure_logging("INFO")
    logging.getLogger("httpx2").info("HTTP Request: GET http://x/?q=secret")
    logging.getLogger("httpx2").warning("retrying")
    assert [line["event"] for line in log_lines()] == ["retrying"]


def test_uvicorn_access_logger_is_switched_off() -> None:
    # Without `disabled`, its WARNING records would reach logging's last-resort stderr handler,
    # unmasked; without `propagate = False`, its request lines would reach our handler.
    import logging.config

    from uvicorn.config import LOGGING_CONFIG

    logging.config.dictConfig(LOGGING_CONFIG)
    access = logging.getLogger("uvicorn.access")
    # As if uvicorn ran with a log config that lets access records propagate.
    access.propagate = True
    configure_logging("INFO")
    assert access.disabled
    assert access.propagate is False
    assert access.handlers == []


def test_uvicorn_debug_lines_show_at_debug_level(capsys: pytest.CaptureFixture[str]) -> None:
    import logging.config

    from uvicorn.config import LOGGING_CONFIG

    logging.config.dictConfig(LOGGING_CONFIG)  # sets uvicorn's loggers to INFO
    configure_logging("DEBUG")
    logging.getLogger("uvicorn.error").debug("shown at debug")
    events = [json.loads(line)["event"] for line in capsys.readouterr().out.splitlines()]
    assert events == ["shown at debug"]
