"""Every error is an RFC 9457 problem; no error repeats the request or the exception text."""

import json
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from httpx2 import Response

from hub.api.errors import PROBLEM_MEDIA_TYPE, ProblemError, problem_response

MakeClient = Callable[..., TestClient]
LogLines = Callable[[], list[dict[str, Any]]]

PROBLEM_FIELDS = {"type", "title", "status", "detail", "code"}


def _assert_problem(response: Response, status: int, title: str, code: str) -> dict[str, Any]:
    assert response.status_code == status
    assert response.headers["content-type"] == PROBLEM_MEDIA_TYPE
    body: dict[str, Any] = response.json()
    assert body.keys() >= PROBLEM_FIELDS
    assert body["type"] == "about:blank"
    assert body["title"] == title
    assert body["status"] == status
    assert body["code"] == code
    assert isinstance(body["detail"], str)
    assert body["detail"]
    return body


def test_unknown_path_is_404_problem(make_client: MakeClient) -> None:
    response = make_client().get("/nope")
    _assert_problem(response, 404, "Not Found", "not_found")


def test_wrong_method_is_405_problem_with_allow(make_client: MakeClient) -> None:
    response = make_client().post("/health")
    _assert_problem(response, 405, "Method Not Allowed", "method_not_allowed")
    assert response.headers["allow"] == "GET"


def test_problem_error_keeps_its_code(make_client: MakeClient) -> None:
    body = _assert_problem(make_client().get("/conflict"), 409, "Conflict", "stale_draft")
    assert body["detail"] == "The draft is out of date."


def test_validation_error_is_422_problem_without_the_input(
    make_client: MakeClient, test_card: str
) -> None:
    response = make_client().post("/payload", json={"amount": test_card})
    body = _assert_problem(response, 422, "Unprocessable Content", "validation_error")
    assert body["errors"] == [
        {
            "loc": ["body", "amount"],
            "type": "int_parsing",
            "msg": "Input should be a valid integer, unable to parse string as an integer",
        }
    ]
    assert "4111" not in response.text


def test_unhandled_error_is_500_problem_without_the_message(
    make_client: MakeClient, log_lines: LogLines
) -> None:
    response = make_client().get("/boom")
    _assert_problem(response, 500, "Internal Server Error", "internal_error")
    assert "card" not in response.text
    assert response.headers["x-request-id"]

    error_line = next(line for line in log_lines() if line["event"] == "request.unhandled_error")
    assert error_line["request_id"] == response.headers["x-request-id"]
    assert "4111" not in str(error_line)
    assert "[REDACTED]" in str(error_line["exception"])  # the traceback is logged, masked


@pytest.mark.parametrize(
    ("status", "code"),
    [(400, "bad_request"), (413, "content_too_large"), (429, "too_many_requests")],
)
def test_http_errors_get_a_snake_case_code(make_client: MakeClient, status: int, code: str) -> None:
    app = make_client().app

    @app.get("/raise")  # type: ignore[attr-defined, untyped-decorator]
    async def raise_status() -> None:
        from starlette.exceptions import HTTPException

        raise HTTPException(status)

    response = TestClient(app, raise_server_exceptions=False).get("/raise")
    assert response.json()["code"] == code


def test_problem_extensions_are_added_to_the_body() -> None:
    response = problem_response(429, "rate_limited", "Slow down.", extensions={"retry": 3})
    body = json.loads(bytes(response.body))
    assert body["type"] == "about:blank"
    assert body["retry"] == 3


def test_problem_error_carries_its_fields() -> None:
    error = ProblemError(409, "stale_draft", "The draft is out of date.")
    assert (error.status, error.code, error.detail) == (
        409,
        "stale_draft",
        "The draft is out of date.",
    )


def test_unhandled_error_log_has_no_locals_request_content_or_secrets(
    make_client: MakeClient, log_lines: LogLines, test_db_password: str
) -> None:
    message = "My email is jane.doe@example.com and IBAN DE89370400440532013000"
    response = make_client().post("/boom-with-body", json={"message": message})
    assert response.status_code == 500

    lines = log_lines()
    error_line = next(line for line in lines if line["event"] == "request.unhandled_error")
    frames = [frame for exc in error_line["exception"] for frame in exc["frames"]]
    assert frames, "the traceback is logged"
    assert all("locals" not in frame for frame in frames)
    everything = json.dumps(lines)
    assert "jane.doe" not in everything
    assert test_db_password not in everything


def test_extensions_cannot_replace_standard_members() -> None:
    response = problem_response(
        409, "stale_draft", "Out of date.", extensions={"type": "x", "code": "y", "status": 200}
    )
    body = json.loads(bytes(response.body))
    assert (body["type"], body["code"], body["status"]) == ("about:blank", "stale_draft", 409)


def test_non_standard_status_still_gets_a_problem(make_client: MakeClient) -> None:
    from starlette.exceptions import HTTPException

    client = make_client()

    @client.app.get("/client-closed")  # type: ignore[attr-defined, untyped-decorator]
    async def client_closed() -> None:
        raise HTTPException(499)

    response = client.get("/client-closed")
    _assert_problem(response, 499, "HTTP 499", "http_499")
