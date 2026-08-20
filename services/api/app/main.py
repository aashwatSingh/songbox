from fastapi import FastAPI

from app.routes.review_queue import router as review_queue_router
from app.routes.tracks import router as tracks_router

app = FastAPI(title="Songbox API")

app.include_router(tracks_router)
app.include_router(review_queue_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
