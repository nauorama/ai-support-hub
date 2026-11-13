"""`/health` and `/ready` (docs/api.md §6), at the root, outside `/api/v1`.

The paths do not end in `z`: Cloud Run reserves some such paths.
"""

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from hub.deps import Container
from hub.observability.readiness import ReadinessCheck

router = APIRouter(tags=["health"])

CHECK_TIMEOUT_SECONDS = 2.0


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


async def _run_check(name: str, check: ReadinessCheck) -> tuple[str, dict[str, str]]:
    try:
        async with asyncio.timeout(CHECK_TIMEOUT_SECONDS):
            await check()
    except TimeoutError:
        return name, {"status": "error", "error": "timeout"}
    except Exception as exc:
        # Only the class name: messages of connection errors can contain the connection string.
        return name, {"status": "error", "error": type(exc).__name__}
    return name, {"status": "ok"}


@router.get("/ready")
async def ready(request: Request) -> JSONResponse:
    container: Container = request.app.state.container
    results = await asyncio.gather(
        *(_run_check(name, check) for name, check in container.readiness_checks.items())
    )
    checks = dict(results)
    is_ready = all(result["status"] == "ok" for result in checks.values())
    return JSONResponse(
        {"status": "ready" if is_ready else "not_ready", "checks": checks},
        status_code=200 if is_ready else 503,
    )
