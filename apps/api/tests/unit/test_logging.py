"""Logs are JSON lines on stdout, and card-like numbers never reach them (temporary P2-01 mask)."""

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
    "text",
    [
        "4111111111111111",
        "4111 1111 1111 1111",
        "4111-1111-1111-1111",
        "card4111111111111111end",
        "378282246310005",  # 15 digits (public Amex test number)
        "4222222222222",  # 13 digits
        "6011 0009 9013 9424 123",  # 19 digits
        "4111111111111112",  # fails Luhn: the temporary mask still masks it
        "41111111111111111228",  # card and expiry typed together
        "4111  1111  1111  1111",  # double spaces
        "4111 - 1111 - 1111 - 1111",
        "4111\u00a01111\u00a01111\u00a01111",  # no-break spaces
        "4111+1111+1111+1111",  # URL form encoding
        "4111%201111%201111%201111",
        "4111%2D1111%2d1111%2D1111",
        "4111.1111.1111.1111",
        "4111 1111 1111 1111 1111",  # 20 digits: masked as a whole
    ],
)
def test_card_like_numbers_are_masked(text: str) -> None:
    masked = mask_processor(None, "info", {"event": f"paid with {text} today"})["event"]
    expected = "card[REDACTED]end" if text.startswith("card") else "[REDACTED]"
    assert masked == f"paid with {expected} today"
    assert not any(char.isdigit() for char in masked)


@pytest.mark.parametrize(
    "text",
    ["123456789012", "2026-11-13T10:00:00.123456Z", "41 11 11", "+49 30 1234 5678"],
)
def test_short_numbers_and_timestamps_are_kept(text: str) -> None:
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
    assert masked["details"] == {"note": ["card [REDACTED]", ["[REDACTED]"]]}
    assert masked["card_as_int"] == "[REDACTED]"
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
    assert line["note"] == "card [REDACTED]"
    assert line["level"] == "info"
    assert line["logger"] == "test"
    assert line["timestamp"].endswith("Z")


def test_standard_library_lines_are_masked_json(log_lines: LogLines) -> None:
    configure_logging("INFO")
    logging.getLogger("some.library").info("charged card %s", "4111111111111111")
    (line,) = log_lines()
    assert line["event"] == "charged card [REDACTED]"
    assert line["logger"] == "some.library"


def test_exception_tracebacks_are_masked(log_lines: LogLines) -> None:
    configure_logging("INFO")
    try:
        raise ValueError(f"bad card {TEST_CARD}")
    except ValueError:
        structlog.get_logger("test").exception("failed")
    (line,) = log_lines()
    assert "4111" not in str(line)
    assert line["exception"][0]["exc_value"] == "bad card [REDACTED]"


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
            "event": "server card [REDACTED]",
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
