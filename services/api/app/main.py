from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from app.logging_config import configure_logging
from app.routes.admin import router as admin_router
from app.routes.review_queue import router as review_queue_router
from app.routes.tracks import router as tracks_router

configure_logging()
_access_logger = logging.getLogger("songbox.access")

app = FastAPI(title="Songbox API")

# Dev-only permissive CORS so the Next.js dev server (localhost:3000) can call this API
# (localhost:8000) cross-origin. Not a production CORS policy -- tighten before any real deploy.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["GET", "POST"],
    allow_headers=["X-Dev-Tenant-Id", "X-Dev-User-Id", "Content-Type"],
)


@app.middleware("http")
async def log_requests(
    request: Request, call_next: Callable[[Request], Awaitable[Response]]
) -> Response:
    start = time.monotonic()
    response = await call_next(request)
    duration_ms = (time.monotonic() - start) * 1000
    _access_logger.info(
        "request",
        extra={
            "method": request.method,
            "path": request.url.path,
            "status_code": response.status_code,
            "duration_ms": round(duration_ms, 2),
            "tenant_id": request.headers.get("X-Dev-Tenant-Id"),
            "client_ip": request.client.host if request.client else None,
        },
    )
    return response


app.include_router(tracks_router)
app.include_router(review_queue_router)
app.include_router(admin_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
