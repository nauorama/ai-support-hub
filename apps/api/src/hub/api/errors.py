"""RFC 9457 problem details for every error the API returns (docs/api.md §1).

`type` is `about:blank`, so `title` is the standard HTTP status phrase and `code` carries the
meaning. Error bodies never repeat what the client sent: a request can hold card data, and a
validation error that echoes the input would send it back and into any log of the response.
"""

from http import HTTPStatus
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

PROBLEM_MEDIA_TYPE = "application/problem+json"


class ProblemError(Exception):
    """Raised by endpoints for an expected error with a code from the API contract."""

    def __init__(self, status: int, code: str, detail: str) -> None:
        super().__init__(code)
        self.status = status
        self.code = code
        self.detail = detail


def problem_response(
    status: int,
    code: str,
    detail: str,
    *,
    headers: dict[str, str] | None = None,
    extensions: dict[str, Any] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": "about:blank",
        "title": HTTPStatus(status).phrase,
        "status": status,
        "detail": detail,
        "code": code,
    }
    body.update(extensions or {})
    return JSONResponse(body, status_code=status, headers=headers, media_type=PROBLEM_MEDIA_TYPE)


def internal_error_response() -> JSONResponse:
    # Never the exception message: it may hold data from the request.
    return problem_response(
        500,
        "internal_error",
        "An unexpected error occurred. Quote the X-Request-ID header when you report it.",
    )


def _code_for_status(status: int) -> str:
    return HTTPStatus(status).phrase.lower().replace(" ", "_").replace("-", "_").replace("'", "")


async def _problem_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ProblemError)  # noqa: S101 - registered for this class only
    return problem_response(exc.status, exc.code, exc.detail)


async def _http_exception_handler(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, HTTPException)  # noqa: S101 - registered for this class only
    title = HTTPStatus(exc.status_code).phrase
    detail = exc.detail if isinstance(exc.detail, str) and exc.detail else title
    return problem_response(
        exc.status_code,
        _code_for_status(exc.status_code),
        detail,
        headers=dict(exc.headers) if exc.headers else None,
    )


async def _validation_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)  # noqa: S101 - registered for this class only
    # Only where and what kind: pydantic's `input` and `ctx` can contain the submitted values.
    errors = [
        {"loc": list(error.get("loc", ())), "type": error.get("type"), "msg": error.get("msg")}
        for error in exc.errors()
    ]
    return problem_response(
        422,
        "validation_error",
        "The request is not valid.",
        extensions={"errors": errors},
    )


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ProblemError, _problem_error_handler)
    app.add_exception_handler(HTTPException, _http_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
