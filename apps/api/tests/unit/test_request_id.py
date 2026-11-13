"""Every response carries a request id, and every log line of the request names it."""

import re
import uuid
from collections.abc import Callable
from typing import Any

import pytest
import structlog
from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send

from hub.api.middleware import RequestContextMiddleware, request_id_for
from hub.observability.logging import configure_logging

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


@pytest.mark.parametrize(
    "incoming",
    [
        "",
        "a" * 65,
        "has space",
        "semi;colon",
        "über",
        "1731500000000",  # epoch ms: 13+ digits in a row (api.md)
        "4111111111111112",  # fails Luhn, so not masked, but still 13+ digits in a row
        "4111111111111111",  # a card number sent as the id: never logged, never kept
        "req-4111-1111-1111-1111",  # the card mask would change it
        "pin-4821",  # the PIN mask would change it
    ],
)
def test_unsafe_incoming_id_is_replaced(incoming: str) -> None:
    assert re.fullmatch(r"[0-9a-f]{32}", request_id_for(incoming))


def test_log_line_names_the_request(make_client: MakeClient, log_lines: LogLines) -> None:
    response = make_client().get("/nope", headers={"X-Request-ID": "req-1"})
    line = next(line for line in log_lines() if line["event"] == "request.completed")
    assert line["request_id"] == response.headers["x-request-id"] == "req-1"
    assert line["method"] == "GET"
    assert line["path"] == "/nope"
    assert line["status"] == 404
    assert line["outcome"] == "ok"
    assert line["level"] == "info"
    assert isinstance(line["duration_ms"], float)


async def test_request_id_is_bound_during_the_request_and_reset_after(
    log_lines: LogLines,
) -> None:
    # The middleware is called directly in this task, so a binding without reset would stay
    # visible here afterwards (TestClient runs the app in another thread and cannot show it).
    configure_logging("INFO")
    seen_inside: dict[str, Any] = {}

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen_inside.update(structlog.contextvars.get_contextvars())
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "method": "GET",
        "path": "/x",
        "headers": [(b"x-request-id", b"req-3")],
    }
    await RequestContextMiddleware(app)(scope, receive, send)

    assert seen_inside["request_id"] == "req-3"
    assert "request_id" not in structlog.contextvars.get_contextvars()
    assert (b"x-request-id", b"req-3") in sent[0]["headers"]
    (line,) = log_lines()
    assert (line["event"], line["request_id"]) == ("request.completed", "req-3")


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
    lines = log_lines()
    assert any(line["event"] == "request.unhandled_error" for line in lines)
    completed = next(line for line in lines if line["event"] == "request.completed")
    assert (completed["status"], completed["outcome"]) == (200, "error")


def test_generated_id_is_drawn_again_when_the_mask_would_change_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    digit_heavy = uuid.UUID("12345678-9012-3456-7890-abcdefabcdef")  # 20 digits in a row
    clean = uuid.UUID("abcdefab-cdef-4bcd-8fab-cdefabcdefab")
    draws = iter([digit_heavy, clean])
    monkeypatch.setattr(uuid, "uuid4", lambda: next(draws))
    assert request_id_for(None) == clean.hex


def test_ids_read_the_same_in_the_header_and_the_log(
    make_client: MakeClient, log_lines: LogLines
) -> None:
    client = make_client()
    for incoming in ["1731500000000", "4111111111111111", None]:
        headers = {"X-Request-ID": incoming} if incoming else {}
        response = client.get("/health", headers=headers)
        logged = [line["request_id"] for line in log_lines() if "request_id" in line]
        assert logged == [response.headers["x-request-id"]]
        assert "[REDACTED]" not in logged[0]
        assert "[CARD" not in logged[0]
