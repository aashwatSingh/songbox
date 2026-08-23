from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routes.review_queue import router as review_queue_router
from app.routes.tracks import router as tracks_router

app = FastAPI(title="Songbox API")

# Dev-only permissive CORS so the Next.js dev server (localhost:3000) can call this API
# (localhost:8000) cross-origin. Not a production CORS policy -- tighten before any real deploy.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["GET", "POST"],
    allow_headers=["X-Dev-Tenant-Id", "X-Dev-User-Id", "Content-Type"],
)

app.include_router(tracks_router)
app.include_router(review_queue_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
