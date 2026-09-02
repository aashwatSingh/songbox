from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.logging_config import configure_logging
from app.rate_limit import limiter
from app.routes.admin import router as admin_router
from app.routes.auth import router as auth_router
from app.routes.review_queue import router as review_queue_router
from app.routes.tracks import router as tracks_router

configure_logging()
_access_logger = logging.getLogger("songbox.access")
# Deliberately NOT songbox.access. That logger has a strict contract -- exactly one record per
# request, carrying only the six access fields -- which tests assert on directly. Logging a
# traceback there would emit a second record and break it.
_error_logger = logging.getLogger("songbox.error")

app = FastAPI(title="Songbox API")
app.state.limiter = limiter
# slowapi's handler is typed to accept only RateLimitExceeded, narrower than Starlette's
# Callable[[Request, Exception], ...] contract -- a known upstream slowapi/Starlette typing
# mismatch (the handler is registered specifically for RateLimitExceeded via the first
# argument, so this is safe at runtime; mypy just can't see that from the signature alone).
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]

@app.middleware("http")
async def log_requests(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    start = time.monotonic()
    status_code = 500
    try:
        try:
            response = await call_next(request)
        except Exception:
            # Turn an unhandled error into a real 500 response HERE, inside the middleware stack,
            # instead of letting it propagate to Starlette's ServerErrorMiddleware. That
            # middleware sits OUTSIDE every user middleware including CORS, so the 500 it builds
            # carries no Access-Control-Allow-Origin header -- and a browser discards a
            # cross-origin response without one, reporting the generic TypeError "Failed to
            # fetch". The real status and error were invisible in the UI for exactly this reason
            # while /tracks/upload was 500ing on every full-length song.
            _error_logger.exception("unhandled error", extra={"path": request.url.path})
            response = JSONResponse(status_code=500, content={"detail": "internal server error"})
        status_code = response.status_code
        return response
    finally:
        duration_ms = (time.monotonic() - start) * 1000
        _access_logger.info(
            "request",
            extra={
                "method": request.method,
                "path": request.url.path,
                "status_code": status_code,
                "duration_ms": round(duration_ms, 2),
                # Set by app/auth.py's get_identity() on request.state after a real session
                # lookup succeeds -- None for unauthenticated requests (e.g. /health, or a 401
                # before identity ever resolves), same as before. Unlike the old
                # X-Dev-Tenant-Id header this replaces, this value is now verified, not merely
                # whatever the caller claimed.
                "tenant_id": getattr(request.state, "tenant_id", None),
                "client_ip": request.client.host if request.client else None,
            },
        )


# Added AFTER log_requests on purpose. Starlette's add_middleware() inserts at position 0, so the
# LAST-added middleware is the outermost -- CORS must wrap log_requests for the 500 response that
# middleware now builds to come back out through CORS and pick up its headers.
#
# Dev-only permissive CORS so the Next.js dev server (localhost:3000) can call this API
# (localhost:8000) cross-origin. Not a production CORS policy -- tighten before any real deploy.
# allow_credentials=True is required for the browser to send/receive the httpOnly session cookie
# cross-origin (localhost:3000 -> localhost:8000) -- safe here specifically because allow_origins
# is a concrete origin, not "*" (the CORS spec forbids combining allow_credentials with a wildcard
# origin, and browsers enforce this).
#
# DELETE is in allow_methods because the frontend really issues it (lib/api.ts's deleteTrack).
# It was missing, so the browser's preflight rejected every delete and the button failed with the
# same opaque "Failed to fetch" -- while curl, which does not preflight, worked fine.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type"],
    allow_credentials=True,
)

app.include_router(auth_router)
app.include_router(tracks_router)
app.include_router(review_queue_router)
app.include_router(admin_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
