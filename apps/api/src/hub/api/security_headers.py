"""Security headers on every response (security §4, T13; P8-04).

Plain ASGI rather than Starlette's BaseHTTPMiddleware, so streamed (SSE) responses pass through
untouched: the headers are added to the `http.response.start` message only.

A header a route set itself is kept (`setdefault`): e.g. the SSE stream's own `Cache-Control`.

**Content-Security-Policy.** The web app (P10, served from the same origin) loads only its own
scripts and styles; there is no inline script and no third-party origin. Messages are rendered as
text with a small Markdown subset and links built by the server (docs/ui.md §2), so the CSP is a
second line behind that rule, not the only one.

**API docs.** FastAPI's Swagger UI and ReDoc pages load their bundles from cdn.jsdelivr.net and
start them with an inline script, which the strict policy blocks. Those two pages (and Swagger's
OAuth redirect page) get their own policy that allows exactly that CDN and inline scripts; they
render no customer data. The public demo (`APP_ENV=demo`) does not serve them at all
(`hub.main.create_app`); the OpenAPI JSON stays, it holds no secrets.
"""

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

CONTENT_SECURITY_POLICY = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; font-src 'self'; object-src 'none'; base-uri 'none'; "
    "frame-ancestors 'none'; form-action 'self'"
)
_DOCS_CDN = "https://cdn.jsdelivr.net"
DOCS_CONTENT_SECURITY_POLICY = (
    f"default-src 'self'; script-src 'self' 'unsafe-inline' {_DOCS_CDN}; "
    f"style-src 'self' 'unsafe-inline' {_DOCS_CDN}; "
    "img-src 'self' data: https://fastapi.tiangolo.com; "
    "connect-src 'self'; worker-src 'self' blob:; object-src 'none'; base-uri 'none'; "
    "frame-ancestors 'none'; form-action 'self'"
)
DOCS_PATHS = frozenset({"/docs", "/docs/oauth2-redirect", "/redoc"})

# Same for every response. HSTS is ignored by browsers on plain http (local development), so it
# is safe to send everywhere; Cloud Run serves https only.
_COMMON_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    # The app links only to its own pages; no other site needs to know where a visitor came from.
    ("Referrer-Policy", "no-referrer"),
    ("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Strict-Transport-Security", "max-age=31536000; includeSubDomains"),
)
# API answers hold personal data (tickets, messages): no browser or proxy cache may keep them.
API_PREFIX = "/api/"


def headers_for(path: str) -> list[tuple[str, str]]:
    """The security headers of a response to `path`."""
    policy = DOCS_CONTENT_SECURITY_POLICY if path in DOCS_PATHS else CONTENT_SECURITY_POLICY
    headers = [("Content-Security-Policy", policy), *_COMMON_HEADERS]
    if path.startswith(API_PREFIX):
        headers.append(("Cache-Control", "no-store"))
    return headers


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        added = headers_for(scope["path"])

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in added:
                    headers.setdefault(name, value)
            await send(message)

        await self.app(scope, receive, send_with_headers)
