"""Every response carries a request id, and every log line of the request names it."""

import re
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from hub.api.middleware import request_id_for

MakeClient = Callable[..., TestClient]
LogLines = Callable[[], list[dict[str, Any]]]


def test_request_id_is_generated(make_client: MakeClient) -> None:
    response = make_client().get("/health")
    assert re.fullmatch(r"[0-9a-f]{32}", response.headers["x-request-id"])


def test_each_request_gets_its_own_id(make_client: MakeClient) -> None:
    client = make_client()
    first = client.get("/health").headers["x-request-id"]
    second = client.get("/health").headers["x-request-id"]
    assert first != second


def test_valid_incoming_id_is_kept(make_client: MakeClient) -> None:
    response = make_client().get("/health", headers={"X-Request-ID": "lb-trace.42_a"})
    assert response.headers["x-request-id"] == "lb-trace.42_a"


@pytest.mark.parametrize("incoming", ["", "a" * 65, "has space", "semi;colon", "über"])
def test_unsafe_incoming_id_is_replaced(incoming: str) -> None:
    assert re.fullmatch(r"[0-9a-f]{32}", request_id_for(incoming))


def test_log_line_names_the_request(make_client: MakeClient, log_lines: LogLines) -> None:
    response = make_client().get("/nope", headers={"X-Request-ID": "req-1"})
    line = next(line for line in log_lines() if line["event"] == "request.completed")
    assert line["request_id"] == response.headers["x-request-id"] == "req-1"
    assert line["method"] == "GET"
    assert line["path"] == "/nope"
    assert line["status"] == 404
    assert line["level"] == "info"
    assert isinstance(line["duration_ms"], float)


def test_request_id_does_not_leak_into_later_logs(
    make_client: MakeClient, log_lines: LogLines
) -> None:
    import structlog

    make_client().get("/health", headers={"X-Request-ID": "req-2"})
    structlog.get_logger("after").info("after.request")
    line = next(line for line in log_lines() if line["event"] == "after.request")
    assert "request_id" not in line


def test_lifespan_passes_through(make_client: MakeClient) -> None:
    with make_client() as client:  # entering runs the lifespan scope through the middleware
        assert client.get("/health").status_code == 200


def test_error_after_the_response_started_is_logged_not_raised(
    make_client: MakeClient, log_lines: LogLines
) -> None:
    from collections.abc import AsyncIterator

    from fastapi.responses import StreamingResponse

    client = make_client()

    async def events() -> AsyncIterator[str]:
        yield "event: ticket.created\n\n"
        raise RuntimeError("stream broke")

    @client.app.get("/stream")  # type: ignore[attr-defined, untyped-decorator]
    async def stream() -> StreamingResponse:
        return StreamingResponse(events(), media_type="text/event-stream")

    response = client.get("/stream")
    assert response.status_code == 200
    assert response.text.startswith("event: ticket.created")
    events_logged = [line["event"] for line in log_lines()]
    assert "request.unhandled_error" in events_logged
    assert "request.completed" in events_logged
