from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import _parse_extra_origins, app

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
    assert response.headers["access-control-allow-credentials"] == "true"


def test_unlisted_origin_gets_no_cors_headers() -> None:
    # The actual security property CORS provides, which nothing asserted before: an origin that is
    # NOT on the allow-list must come back with no access-control-allow-origin at all, so the
    # browser refuses to hand the response to that page. Checked on both the preflight and the
    # simple response, because Starlette builds those headers along two separate code paths.
    preflight = client.options(
        "/tracks",
        headers={
            "Origin": "http://evil.example.com",
            "Access-Control-Request-Method": "GET",
        },
    )
    assert "access-control-allow-origin" not in preflight.headers

    simple = client.get("/tracks", headers={"Origin": "http://evil.example.com"})
    assert "access-control-allow-origin" not in simple.headers


def test_parse_extra_origins_handles_blanks_whitespace_and_trailing_comma() -> None:
    raw = "http://a.example:3000, ,  http://b.example:3000 ,"
    assert _parse_extra_origins(raw) == ["http://a.example:3000", "http://b.example:3000"]


def test_parse_extra_origins_empty_input_adds_nothing() -> None:
    # The default for anyone who never sets the var -- it must not widen the allow-list at all.
    assert _parse_extra_origins("") == []
    assert _parse_extra_origins(",,") == []


def test_parse_extra_origins_lowercases() -> None:
    # CORSMiddleware compares against the browser's Origin header by exact string equality, and
    # browsers lowercase scheme and host -- so a hand-typed capitalised MagicDNS name would
    # otherwise silently never match.
    assert _parse_extra_origins("HTTP://MyPC.Tail1234.TS.NET:3000") == [
        "http://mypc.tail1234.ts.net:3000"
    ]


def test_parse_extra_origins_rejects_wildcard() -> None:
    # A "*" here would not degrade to a safe wildcard: Starlette combines it with
    # allow_credentials=True by echoing every requesting origin back as allowed. Must fail loudly.
    with pytest.raises(RuntimeError, match="never '\\*'"):
        _parse_extra_origins("http://a.example:3000,*")
