"""Request id, one log line per request, and the last line of defence for unhandled errors.

Plain ASGI rather than Starlette's BaseHTTPMiddleware, so streamed (SSE) responses pass through
untouched. Unhandled errors are caught here, not left to Starlette's ServerErrorMiddleware: that
one re-raises, and the server would then log the raw traceback outside our masked logging.
"""

import re
import time
import uuid

import structlog
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from hub.api.errors import internal_error_response
from hub.guardrails.masking import mask_text

REQUEST_ID_HEADER = "X-Request-ID"
# Accept a caller's id only when it is short and plain, so it is safe in logs and headers.
_VALID_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
# A long digit run in an id looks like a card number or a timestamp; keep ids plainly readable.
_LONG_DIGIT_RUN = re.compile(r"\d{13,}")

log = structlog.get_logger(__name__)


def _reads_the_same_in_logs(request_id: str) -> bool:
    # The id must read the same in the header and in the logs, or nobody can find the log line.
    # It is never exempt from the mask instead: a caller could send a card number as the id.
    return not _LONG_DIGIT_RUN.search(request_id) and mask_text(request_id) == request_id


def _new_request_id() -> str:
    # About 2% of uuid4 hex strings hold 13+ digits in a row, which api.md does not allow;
    # draw again. Each draw fails with p ≈ 0.02, so this ends after one or two draws.
    while True:
        request_id = uuid.uuid4().hex
        if _reads_the_same_in_logs(request_id):
            return request_id


def request_id_for(incoming: str | None) -> str:
    """The caller's id when it is plain and survives the log mask; otherwise a new one."""
    if (
        incoming is not None
        and _VALID_REQUEST_ID.fullmatch(incoming)
        and _reads_the_same_in_logs(incoming)
    ):
        return incoming
    return _new_request_id()


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = request_id_for(Headers(scope=scope).get(REQUEST_ID_HEADER))
        scope.setdefault("state", {})["request_id"] = request_id
        status_code = 500
        response_started = False
        started = time.perf_counter()

        async def send_with_request_id(message: Message) -> None:
            nonlocal response_started, status_code
            if message["type"] == "http.response.start":
                response_started = True
                status_code = message["status"]
                MutableHeaders(scope=message).append(REQUEST_ID_HEADER, request_id)
            await send(message)

        outcome = "ok"
        with structlog.contextvars.bound_contextvars(request_id=request_id):
            try:
                await self.app(scope, receive, send_with_request_id)
            except Exception:
                # When the response had started (a stream), its status says nothing about the
                # failure; `outcome` does.
                outcome = "error"
                log.exception("request.unhandled_error")
                # Not re-raised: the error is logged (masked) above, and the server would
                # otherwise log the traceback again without masking.
                if not response_started:
                    await internal_error_response()(scope, receive, send_with_request_id)
            log.info(
                "request.completed",
                method=scope["method"],
                path=scope["path"],
                status=status_code,
                outcome=outcome,
                duration_ms=round((time.perf_counter() - started) * 1000, 1),
            )
