from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_configured_localhost_origin_gets_cors_headers_on_preflight() -> None:
    # Dev-only CORS (app/main.py) allows the Next.js dev server (localhost:3000) to call this
    # API cross-origin -- a lightweight check that the configured origin actually gets the
    # expected headers back, per the design spec's testing strategy. Not a broad CORS security
    # suite; just confirming the dev-only config works.
    response = client.options(
        "/tracks",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "GET",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"
