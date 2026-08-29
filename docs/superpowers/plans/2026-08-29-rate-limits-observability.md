# M7b: Rate Limits + Observability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add per-IP rate limiting on GPU-costing and admin endpoints, structured JSON request
logging, and real GPU job cost logging against the existing `config/gpu_costs.yaml` stub.

**Architecture:** `slowapi` (Redis-backed, atomic) gates five routes by client IP. A stdlib
`logging`-based JSON formatter, wired into a request-timing middleware, gives every request one
structured log line. A small context manager wraps each of the four GPU-invoking route handlers'
existing `run_inference(...)` call sites to log real duration and (once real pricing data exists)
real cost — never a fabricated number.

**Tech Stack:** FastAPI, `slowapi`/`limits` (new), `redis` python client (new, NOT via slowapi's
`[redis]` extra — see Global Constraints), stdlib `logging`, `PyYAML` (new).

## Global Constraints

- Rate limiting is keyed on client IP (`slowapi.util.get_remote_address`), never on
  `X-Dev-Tenant-Id` — that header is spoofable (`docs/PLAN.md` open question 9), so a tenant-keyed
  limit would protect nothing against a deliberate abuser.
- **Do not install `slowapi[redis]`.** That extra pins `redis>=3.4.1,<4.0.0`, which imports
  `distutils` — removed from the stdlib in Python 3.12+, and this project requires
  `python>=3.12` (verified: `import redis` with that old version raises
  `ModuleNotFoundError: No module named 'distutils'` on this project's real Python 3.13
  interpreter). Add `redis>=5.0` as a **separate**, plain dependency instead — pip does not apply
  slowapi's extra-scoped constraint unless the `[redis]` extra itself is requested.
- Every `@limiter.limit(...)`-decorated route (or dependency function) **must** accept
  `request: Request` as an explicit parameter — slowapi cannot hook in otherwise (verified against
  the real installed package; this is a documented, hard requirement, not an inference).
- `Limiter(..., headers_enabled=True)` is required for a `Retry-After` header to appear on a 429 —
  this is **not** slowapi's default (`headers_enabled: bool = False` in the real `Limiter.__init__`
  signature). Any route relying on `headers_enabled` must also accept `response: Response` as an
  explicit parameter, or slowapi raises `Exception: parameter 'response' must be an instance of
  starlette.responses.Response` (verified directly).
- **The admin takedown endpoint cannot use the plain `@limiter.limit(...)` decorator pattern.**
  Verified directly: when a route is gated by a FastAPI dependency that can fail
  (`require_admin_key`), a `@limiter.limit(...)` decorator on the route function itself only runs
  **after** all of that route's `Depends(...)` have already resolved — meaning a flood of *wrong*
  admin-key attempts is rejected by `require_admin_key` every time with a 401, and the decorated
  function (and therefore the rate limiter) is **never reached at all** (confirmed: 5 real requests
  against a route shaped this way produced 5× 401 and a handler-call-count of 0 — the limiter never
  engaged). The fix, also verified directly: wrap a small no-op function with
  `@limiter.limit(...)` and place it as a `Depends(...)` **before** `Depends(require_admin_key)` in
  the router's `dependencies=[...]` list. FastAPI resolves a dependency list in order and
  short-circuits on the first exception, so once the rate limit is hit, subsequent attempts get 429
  **before** `require_admin_key` ever runs — throttling the brute-force attempt regardless of
  whether any given guess is right or wrong (confirmed: with this ordering, attempts past the limit
  return 429, not 401, and never reach the guessed-key check).
- Never log raw audio, lyrics, track title/artist, attestation text, or signed URLs anywhere in
  this milestone's new logging (`CLAUDE.md`). Every log line's field set is fixed by this plan's
  code — nothing implicit is ever captured.
- No fabricated cost or measurement figures (`CLAUDE.md`). `estimated_cost_usd` is `null` until
  `config/gpu_costs.yaml`'s `providers` list is genuinely populated with real pricing.
- Rate-limit values (30/hour upload, 20/hour each GPU route, 10/minute admin takedown) are stated
  policy, not measured/tuned numbers — there is no real traffic yet to tune against.
- This project's CI (`.github/workflows/ci.yml`) currently starts **no Redis service at all** —
  only `postgres` (as a `services:` block) and a manually-started MinIO container. Task 2 adds a
  `redis` service block, or every rate-limiting test in this plan will fail in CI even though it
  passes locally.

---

### Task 1: Structured JSON request logging

**Files:**
- Create: `services/api/app/logging_config.py`
- Modify: `services/api/app/main.py`
- Test: `services/api/tests/test_request_logging.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `app.logging_config.JSONFormatter` (a `logging.Formatter` subclass) and
  `app.logging_config.configure_logging() -> None`, both consumed only by `main.py` in this task.
  Also establishes the pattern (`logging.getLogger(<name>).info(msg, extra={...})`, one JSON line
  per record) that Task 3's job-cost logging reuses under a different logger name
  (`"songbox.job_cost"` vs. this task's `"songbox.access"`).

- [ ] **Step 1: Write the failing tests**

Create `services/api/tests/test_request_logging.py`:

```python
from __future__ import annotations

import json
import logging
import uuid

import pytest
from fastapi.testclient import TestClient

from app.logging_config import JSONFormatter
from app.main import app

client = TestClient(app)


def test_json_formatter_emits_one_parseable_line_with_extra_fields() -> None:
    record = logging.LogRecord(
        name="songbox.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="request",
        args=(),
        exc_info=None,
    )
    record.method = "GET"
    record.path = "/tracks"
    record.status_code = 200
    record.duration_ms = 12.3
    record.tenant_id = "abc-123"
    record.client_ip = "127.0.0.1"

    line = JSONFormatter().format(record)
    payload = json.loads(line)

    assert payload["message"] == "request"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "songbox.access"
    assert payload["method"] == "GET"
    assert payload["path"] == "/tracks"
    assert payload["status_code"] == 200
    assert payload["duration_ms"] == 12.3
    assert payload["tenant_id"] == "abc-123"
    assert payload["client_ip"] == "127.0.0.1"


def test_json_formatter_includes_no_fields_beyond_what_was_explicitly_set() -> None:
    baseline = logging.LogRecord(
        name="x", level=logging.INFO, pathname="x", lineno=1, msg="x", args=(), exc_info=None
    )
    standard_keys = set(vars(baseline).keys())

    record = logging.LogRecord(
        name="songbox.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="request",
        args=(),
        exc_info=None,
    )
    record.method = "GET"

    payload = json.loads(JSONFormatter().format(record))
    fixed_fields = {"timestamp", "level", "logger", "message"}
    extra_keys = set(payload.keys()) - fixed_fields
    assert extra_keys == {"method"}
    assert not (extra_keys & standard_keys)


def test_real_get_request_logs_method_path_status_and_duration(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="songbox.access"):
        response = client.get("/health")

    assert response.status_code == 200
    access_records = [r for r in caplog.records if r.name == "songbox.access"]
    assert len(access_records) == 1
    record = access_records[0]
    assert record.method == "GET"  # type: ignore[attr-defined]
    assert record.path == "/health"  # type: ignore[attr-defined]
    assert record.status_code == 200  # type: ignore[attr-defined]
    assert isinstance(record.duration_ms, float)  # type: ignore[attr-defined]
    assert record.duration_ms >= 0  # type: ignore[attr-defined]
    assert record.tenant_id is None  # type: ignore[attr-defined]  # no X-Dev-Tenant-Id was sent


def test_request_with_tenant_header_logs_that_tenant_id(
    caplog: pytest.LogCaptureFixture,
) -> None:
    tenant_id = str(uuid.uuid4())
    with caplog.at_level(logging.INFO, logger="songbox.access"):
        client.get("/tracks", headers={"X-Dev-Tenant-Id": tenant_id, "X-Dev-User-Id": str(uuid.uuid4())})

    access_records = [r for r in caplog.records if r.name == "songbox.access"]
    assert len(access_records) == 1
    assert access_records[0].tenant_id == tenant_id  # type: ignore[attr-defined]


def test_4xx_response_still_logs_the_real_status_code(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="songbox.access"):
        response = client.get(f"/tracks/{uuid.uuid4()}/transcription")

    access_records = [r for r in caplog.records if r.name == "songbox.access"]
    assert len(access_records) == 1
    assert access_records[0].status_code == response.status_code  # type: ignore[attr-defined]
    assert response.status_code >= 400


def test_upload_request_log_never_leaks_track_content(
    caplog: pytest.LogCaptureFixture, synthetic_wav
) -> None:
    headers = {"X-Dev-Tenant-Id": str(uuid.uuid4()), "X-Dev-User-Id": str(uuid.uuid4())}
    with caplog.at_level(logging.INFO, logger="songbox.access"):
        with synthetic_wav.open("rb") as fh:
            client.post(
                "/tracks/upload",
                headers=headers,
                data={"lane": "C", "pd_cc_source": "a very specific public domain source string"},
                files={"file": ("tone.wav", fh, "audio/wav")},
            )

    access_records = [r for r in caplog.records if r.name == "songbox.access"]
    assert len(access_records) == 1
    logged_extra_keys = {
        k
        for k in vars(access_records[0])
        if k not in set(vars(logging.LogRecord("x", logging.INFO, "x", 1, "x", (), None)))
    }
    assert logged_extra_keys == {"method", "path", "status_code", "duration_ms", "tenant_id", "client_ip"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd services/api && python -m pytest tests/test_request_logging.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.logging_config'`

- [ ] **Step 3: Write `app/logging_config.py`**

Create `services/api/app/logging_config.py`:

```python
from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

_STANDARD_LOGRECORD_KEYS = frozenset(
    vars(logging.LogRecord("x", logging.INFO, "x", 1, "x", (), None)).keys()
)


class JSONFormatter(logging.Formatter):
    """Formats each LogRecord as one JSON line. Only fields explicitly set on the record via
    `extra={...}` at the call site are included beyond timestamp/level/logger/message -- nothing
    is captured implicitly, so a field nobody intended to log (raw audio, lyrics, a signed URL)
    can never leak in just because it happened to be a local variable somewhere in the call
    stack. CLAUDE.md's "never log raw audio, lyrics, or signed URLs" rule is enforced by this
    formatter never reading anything beyond what a caller explicitly passed via `extra`.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD_LOGRECORD_KEYS:
                payload[key] = value
        return json.dumps(payload, default=str)


def configure_logging() -> None:
    """Replaces the root logger's handlers with a single stdout handler emitting one JSON line
    per record. Idempotent -- clears existing handlers before adding its own rather than
    appending, so calling this more than once (app startup, then again in a test) never produces
    duplicate log lines per record.
    """
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JSONFormatter())
    root.addHandler(handler)
    root.setLevel(logging.INFO)
```

- [ ] **Step 4: Run tests to verify the formatter tests pass (the request-logging tests will still fail — the middleware doesn't exist yet)**

Run: `cd services/api && python -m pytest tests/test_request_logging.py -v`
Expected: `test_json_formatter_emits_one_parseable_line_with_extra_fields` and
`test_json_formatter_includes_no_fields_beyond_what_was_explicitly_set` PASS. The other four FAIL
(no `songbox.access` records exist yet — the middleware isn't wired in).

- [ ] **Step 5: Wire the request-logging middleware into `app/main.py`**

Modify `services/api/app/main.py` — replace the file's contents entirely with:

```python
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
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd services/api && python -m pytest tests/test_request_logging.py -v`
Expected: PASS (6/6).

- [ ] **Step 7: Run ruff, mypy, and the full suite**

Run: `cd services/api && python -m ruff check . && python -m mypy app && python -m pytest -q`
Expected: all clean, no regressions.

- [ ] **Step 8: Commit**

```bash
git add services/api/app/logging_config.py services/api/app/main.py services/api/tests/test_request_logging.py
git commit -m "M7b: add structured JSON request logging"
```

---

### Task 2: Rate limiting

**Files:**
- Modify: `services/api/pyproject.toml`
- Create: `services/api/app/rate_limit.py`
- Modify: `services/api/app/main.py`
- Modify: `services/api/app/routes/tracks.py`
- Modify: `services/api/app/routes/admin.py`
- Modify: `.github/workflows/ci.yml`
- Test: `services/api/tests/test_rate_limiting.py`

**Interfaces:**
- Consumes: `app.logging_config.configure_logging` (Task 1, already wired into `main.py` —
  untouched by this task).
- Produces: `app.rate_limit.limiter` (a `slowapi.Limiter` instance), imported by `main.py`,
  `tracks.py`, and `admin.py`. No later task depends on this — Task 3 is independent.

- [ ] **Step 1: Add dependencies**

In `services/api/pyproject.toml`, add to the `dependencies` list (after `"jsonschema>=4.26",`):

```toml
    "slowapi>=0.1.10",
    "redis>=5.0",
```

**Do not add `slowapi[redis]`** — see Global Constraints for why (a broken transitive `redis<4.0`
pin that fails to import on Python 3.12+). Plain `redis>=5.0` as its own line avoids that pin
entirely, since pip only applies an extra's constraints when that extra is actually requested.

Run: `cd services/api && pip install -e ".[dev]"`
Expected: installs cleanly, `pip show redis` reports a version `>=5.0`.

- [ ] **Step 2: Write the failing tests**

Create `services/api/tests/test_rate_limiting.py`:

```python
from __future__ import annotations

import random
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.acoustid.client import FixtureAcoustIDClient
from app.main import app
from app.routes.tracks import get_acoustid_client

HEADERS = {
    "X-Dev-Tenant-Id": str(uuid.uuid4()),
    "X-Dev-User-Id": str(uuid.uuid4()),
}


def _random_test_ip() -> str:
    # A fresh, effectively-unique IP per test -- Redis-backed rate-limit state persists across
    # test runs (there is no flush fixture), so each test needs its own bucket to stay isolated.
    # Same reasoning this codebase's other tests use fresh random UUIDs for tenant isolation.
    return f"10.{random.randint(0, 255)}.{random.randint(0, 255)}.{random.randint(0, 255)}"


def test_separate_is_rate_limited_to_20_per_hour_and_429_carries_retry_after() -> None:
    # A nonexistent track_id 404s immediately, before any real Demucs work -- the rate-limit
    # decorator still counts every attempt regardless of what the route body does, so this
    # exercises the real /separate route's real limit without running real GPU inference.
    client = TestClient(app, client=(_random_test_ip(), 1))
    fake_track_id = uuid.uuid4()

    responses = [
        client.post(f"/tracks/{fake_track_id}/separate", headers=HEADERS) for _ in range(21)
    ]

    assert [r.status_code for r in responses[:20]] == [404] * 20
    assert responses[20].status_code == 429
    assert "retry-after" in responses[20].headers


def test_transcribe_is_rate_limited_to_20_per_hour() -> None:
    client = TestClient(app, client=(_random_test_ip(), 1))
    fake_track_id = uuid.uuid4()

    responses = [
        client.post(f"/tracks/{fake_track_id}/transcribe", headers=HEADERS) for _ in range(21)
    ]

    assert [r.status_code for r in responses[:20]] == [404] * 20
    assert responses[20].status_code == 429


def test_realign_is_rate_limited_to_20_per_hour() -> None:
    client = TestClient(app, client=(_random_test_ip(), 1))
    fake_track_id = uuid.uuid4()

    responses = [
        client.post(
            f"/tracks/{fake_track_id}/realign", headers=HEADERS, json={"text": "whatever"}
        )
        for _ in range(21)
    ]

    assert [r.status_code for r in responses[:20]] == [404] * 20
    assert responses[20].status_code == 429


def test_package_is_rate_limited_to_20_per_hour() -> None:
    client = TestClient(app, client=(_random_test_ip(), 1))
    fake_track_id = uuid.uuid4()

    responses = [
        client.post(f"/tracks/{fake_track_id}/package", headers=HEADERS) for _ in range(21)
    ]

    assert [r.status_code for r in responses[:20]] == [404] * 20
    assert responses[20].status_code == 429


def test_takedown_rate_limits_even_wrong_admin_key_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The real point of this endpoint's limit: it must throttle repeated WRONG-key guesses, not
    # just successful calls -- require_admin_key alone allows unlimited guesses since it never
    # increments any counter. Verified during planning that a plain @limiter.limit() decorator on
    # this route would NOT catch this case (the failing dependency runs first and the decorated
    # function is never reached) -- app/routes/admin.py wires the limiter as a dependency ordered
    # BEFORE require_admin_key specifically so this test passes.
    monkeypatch.setenv("ADMIN_API_KEY", "the-real-key-for-this-test")
    client = TestClient(app, client=(_random_test_ip(), 1))
    fake_track_id = uuid.uuid4()

    responses = [
        client.post(
            f"/admin/tracks/{fake_track_id}/takedown",
            json={"reason": "test"},
            headers={"X-Admin-Key": "definitely-the-wrong-key"},
        )
        for _ in range(11)
    ]

    assert [r.status_code for r in responses[:10]] == [401] * 10
    assert responses[10].status_code == 429


def test_upload_rate_limit_boundary_and_per_ip_isolation(synthetic_wav: Path) -> None:
    ip_a = _random_test_ip()
    ip_b = _random_test_ip()
    client_a = TestClient(app, client=(ip_a, 1))
    client_b = TestClient(app, client=(ip_b, 1))

    app.dependency_overrides[get_acoustid_client] = lambda: FixtureAcoustIDClient({})
    try:
        for _ in range(30):
            with synthetic_wav.open("rb") as fh:
                response = client_a.post(
                    "/tracks/upload",
                    headers=HEADERS,
                    data={"lane": "A", "attestation_text": "I made this recording"},
                    files={"file": ("tone.wav", fh, "audio/wav")},
                )
            assert response.status_code == 200

        with synthetic_wav.open("rb") as fh:
            exhausted_response = client_a.post(
                "/tracks/upload",
                headers=HEADERS,
                data={"lane": "A", "attestation_text": "I made this recording"},
                files={"file": ("tone.wav", fh, "audio/wav")},
            )
        assert exhausted_response.status_code == 429

        # A distinct IP gets its own fresh bucket -- proves the counter is genuinely per-IP,
        # not a global/shared limit.
        with synthetic_wav.open("rb") as fh:
            b_response = client_b.post(
                "/tracks/upload",
                headers=HEADERS,
                data={"lane": "A", "attestation_text": "I made this recording"},
                files={"file": ("tone.wav", fh, "audio/wav")},
            )
        assert b_response.status_code == 200
    finally:
        app.dependency_overrides.pop(get_acoustid_client, None)


def test_unlimited_route_never_rate_limits() -> None:
    client = TestClient(app, client=(_random_test_ip(), 1))
    for _ in range(50):
        response = client.get("/tracks", headers=HEADERS)
        assert response.status_code == 200
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd services/api && python -m pytest tests/test_rate_limiting.py -v`
Expected: FAIL — every request currently returns its un-rate-limited status (404s never turn into
429s), since no rate limiting exists yet.

- [ ] **Step 4: Write `app/rate_limit.py`**

Create `services/api/app/rate_limit.py`:

```python
from __future__ import annotations

import os

from slowapi import Limiter
from slowapi.util import get_remote_address

# Per-IP, not per-tenant: X-Dev-Tenant-Id is a spoofable dev-only header (docs/PLAN.md open
# question 9), so keying on it would protect nothing against a deliberate abuser -- IP is the
# real, if coarse, backstop today. REDIS_URL defaults to the docker-compose Redis instance, which
# nothing else in this codebase uses yet (see the M7b design spec -- RQ was named in the original
# architecture doc but never actually wired up). headers_enabled=True so a 429 carries a real
# Retry-After header -- this is NOT slowapi's default.
limiter = Limiter(
    key_func=get_remote_address,
    storage_uri=os.environ.get("REDIS_URL", "redis://localhost:6379"),
    headers_enabled=True,
)
```

- [ ] **Step 5: Wire the limiter into `app/main.py`**

Modify `services/api/app/main.py` — add these imports near the top (after the existing `fastapi`
imports):

```python
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from app.rate_limit import limiter
```

Then, immediately after the `app = FastAPI(title="Songbox API")` line, add:

```python
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
```

- [ ] **Step 6: Add rate limits to the four GPU-invoking routes in `app/routes/tracks.py`**

Add this import near the other `app.*` imports:

```python
from app.rate_limit import limiter
```

Change `upload_track`'s decorator and add a `response: Response` parameter (`Request`/`Response`
are already imported in this file; `request: Request` is already a parameter):

```python
@router.post("/tracks/upload", response_model=UploadResponse)
@limiter.limit("30/hour")
def upload_track(
    request: Request,
    response: Response,
    file: UploadFile = File(...),
    lane: str = Form(...),
    attestation_text: str = Form(...),
    license_id: uuid.UUID | None = Form(default=None),
    pd_cc_source: str | None = Form(default=None),
    pd_cc_license: str | None = Form(default=None),
    attribution_string: str | None = Form(default=None),
    identity: Identity = Depends(get_identity),
    db: Session = Depends(get_db),
    acoustid_client: AcoustIDClient = Depends(get_acoustid_client),
) -> UploadResponse:
```

Change `separate_track`'s decorator and signature:

```python
@router.post("/tracks/{track_id}/separate", response_model=SeparateResponse)
@limiter.limit("20/hour")
def separate_track(
    track_id: uuid.UUID,
    request: Request,
    response: Response,
    body: SeparateRequest | None = None,
    identity: Identity = Depends(get_identity),
    db: Session = Depends(get_db),
) -> SeparateResponse:
```

Change `transcribe_track`'s decorator and signature:

```python
@router.post("/tracks/{track_id}/transcribe", response_model=TranscribeResponse)
@limiter.limit("20/hour")
def transcribe_track(
    track_id: uuid.UUID,
    request: Request,
    response: Response,
    body: TranscribeRequest | None = None,
    identity: Identity = Depends(get_identity),
    db: Session = Depends(get_db),
) -> TranscribeResponse:
```

Change `realign_track`'s decorator and signature:

```python
@router.post("/tracks/{track_id}/realign", response_model=TranscribeResponse)
@limiter.limit("20/hour")
def realign_track(
    track_id: uuid.UUID,
    request: Request,
    response: Response,
    body: RealignRequest,
    identity: Identity = Depends(get_identity),
    db: Session = Depends(get_db),
) -> TranscribeResponse:
```

Change `package_track`'s decorator and signature:

```python
@router.post("/tracks/{track_id}/package", response_model=PackageResponse)
@limiter.limit("20/hour")
def package_track(
    track_id: uuid.UUID,
    request: Request,
    response: Response,
    body: PackageRequest | None = None,
    identity: Identity = Depends(get_identity),
    db: Session = Depends(get_db),
) -> PackageResponse:
```

None of these five functions' bodies change — only their signatures (adding `request`/`response`
where not already present) and the new decorator line.

- [ ] **Step 7: Add the rate limit to `app/routes/admin.py`, ordered before the admin-key check**

Modify `services/api/app/routes/admin.py`. Change the imports line:

```python
from fastapi import APIRouter, Depends, HTTPException, Request, Response
```

Add this import:

```python
from app.rate_limit import limiter
```

Replace the router construction (currently `router = APIRouter(prefix="/admin",
dependencies=[Depends(require_admin_key)])`) with a rate-limit dependency defined first, then the
router listing both dependencies **in this exact order** — the rate limit MUST come before the
admin-key check, per the Global Constraints explanation of why a plain decorator doesn't work here:

```python
@limiter.limit("10/minute")
def _check_takedown_rate_limit(request: Request, response: Response) -> None:
    return None


router = APIRouter(
    prefix="/admin",
    dependencies=[Depends(_check_takedown_rate_limit), Depends(require_admin_key)],
)
```

Nothing else in this file changes — `takedown_track`'s own signature and body are untouched; the
router-level `dependencies=[...]` already applies to every route in this file.

- [ ] **Step 8: Add a Redis service to CI**

Modify `.github/workflows/ci.yml`. In the `api` job's `services:` block, add a `redis` service
alongside the existing `postgres` one:

```yaml
    services:
      postgres:
        image: postgres:16-alpine
        env:
          POSTGRES_USER: songbox
          POSTGRES_PASSWORD: songbox
          POSTGRES_DB: songbox
        ports:
          - 5432:5432
        options: >-
          --health-cmd "pg_isready -U songbox"
          --health-interval 5s
          --health-timeout 5s
          --health-retries 10
      redis:
        image: redis:7-alpine
        ports:
          - 6379:6379
        options: >-
          --health-cmd "redis-cli ping"
          --health-interval 5s
          --health-timeout 5s
          --health-retries 10
```

And add `REDIS_URL` to the job's `env:` block, alongside the existing `DATABASE_URL` etc.:

```yaml
    env:
      DATABASE_URL: postgresql+psycopg://songbox:songbox@localhost:5432/songbox
      APP_DATABASE_URL: postgresql+psycopg://songbox_app:songbox_app@localhost:5432/songbox
      MINIO_ENDPOINT: localhost:9000
      MINIO_ACCESS_KEY: songbox
      MINIO_SECRET_KEY: songbox-dev-only
      REDIS_URL: redis://localhost:6379
```

(This documents the dependency explicitly, even though `app/rate_limit.py`'s default already
matches `localhost:6379`.)

- [ ] **Step 9: Run tests to verify they pass**

Ensure the local docker-compose Redis is running (`docker compose up -d redis` from the repo
root, if not already up), then run:

Run: `cd services/api && python -m pytest tests/test_rate_limiting.py -v`
Expected: PASS (7/7).

- [ ] **Step 10: Run ruff, mypy, and the full suite**

Run: `cd services/api && python -m ruff check . && python -m mypy app && python -m pytest -q`
Expected: all clean, no regressions. (The full suite will now take noticeably longer due to
Step 2's 30-real-upload test — this is expected, not a hang.)

- [ ] **Step 11: Commit**

```bash
git add services/api/pyproject.toml services/api/app/rate_limit.py services/api/app/main.py \
    services/api/app/routes/tracks.py services/api/app/routes/admin.py \
    .github/workflows/ci.yml services/api/tests/test_rate_limiting.py
git commit -m "M7b: add per-IP rate limiting on GPU-costing and admin endpoints"
```

---

### Task 3: GPU job cost logging

**Files:**
- Modify: `services/api/pyproject.toml`
- Create: `services/api/app/job_cost.py`
- Modify: `services/api/app/routes/tracks.py`
- Test: `services/api/tests/test_job_cost.py`

**Interfaces:**
- Consumes: `app.logging_config`'s established logging pattern (Task 1) — reused under a
  different logger name, not imported directly.
- Produces: nothing consumed by another task — this is the last task in this milestone.

- [ ] **Step 1: Add the PyYAML dependency**

In `services/api/pyproject.toml`, add to the `dependencies` list:

```toml
    "pyyaml>=6.0",
```

Run: `cd services/api && pip install -e ".[dev]"`

- [ ] **Step 2: Write the failing tests**

Create `services/api/tests/test_job_cost.py`:

```python
from __future__ import annotations

import logging
import textwrap
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.acoustid.client import FixtureAcoustIDClient
from app.job_cost import estimate_cost_usd, track_job_cost
from app.main import app
from app.routes.tracks import get_acoustid_client

client = TestClient(app)


def test_estimate_cost_usd_returns_none_when_providers_empty(tmp_path: Path) -> None:
    gpu_costs_path = tmp_path / "gpu_costs.yaml"
    gpu_costs_path.write_text("providers: []\n")

    assert estimate_cost_usd(10.0, gpu_costs_path=gpu_costs_path) is None


def test_estimate_cost_usd_uses_the_most_recent_dated_entry(tmp_path: Path) -> None:
    gpu_costs_path = tmp_path / "gpu_costs.yaml"
    gpu_costs_path.write_text(
        textwrap.dedent(
            """
            providers:
              - name: fake-provider
                gpu_type: FAKE-GPU
                price_per_second_usd: 0.001
                effective_date: "2020-01-01"
              - name: fake-provider
                gpu_type: FAKE-GPU
                price_per_second_usd: 0.002
                effective_date: "2024-06-01"
            """
        )
    )

    result = estimate_cost_usd(100.0, gpu_costs_path=gpu_costs_path)

    # Must pick the MORE RECENT entry (0.002/s -> 0.2), not the first one in the file (0.001/s).
    assert result == pytest.approx(0.2)


def test_estimate_cost_usd_ignores_future_dated_entries(tmp_path: Path) -> None:
    gpu_costs_path = tmp_path / "gpu_costs.yaml"
    gpu_costs_path.write_text(
        textwrap.dedent(
            """
            providers:
              - name: fake-provider
                gpu_type: FAKE-GPU
                price_per_second_usd: 0.001
                effective_date: "2020-01-01"
              - name: fake-provider
                gpu_type: FAKE-GPU
                price_per_second_usd: 999.0
                effective_date: "2099-01-01"
            """
        )
    )

    result = estimate_cost_usd(10.0, gpu_costs_path=gpu_costs_path)

    assert result == pytest.approx(0.01)


def test_track_job_cost_logs_real_duration_and_null_cost_against_real_empty_yaml(
    caplog: pytest.LogCaptureFixture,
) -> None:
    track_id = uuid.uuid4()
    with caplog.at_level(logging.INFO, logger="songbox.job_cost"):
        with track_job_cost(track_id, "separate"):
            pass

    job_records = [r for r in caplog.records if r.name == "songbox.job_cost"]
    assert len(job_records) == 1
    record = job_records[0]
    assert record.track_id == str(track_id)  # type: ignore[attr-defined]
    assert record.job_type == "separate"  # type: ignore[attr-defined]
    assert isinstance(record.duration_seconds, float)  # type: ignore[attr-defined]
    assert record.duration_seconds >= 0  # type: ignore[attr-defined]
    # config/gpu_costs.yaml is still an empty TODO: unmeasured stub at this point in the project.
    assert record.estimated_cost_usd is None  # type: ignore[attr-defined]


def test_track_job_cost_still_logs_when_the_wrapped_block_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    track_id = uuid.uuid4()
    with caplog.at_level(logging.INFO, logger="songbox.job_cost"):
        with pytest.raises(ValueError):
            with track_job_cost(track_id, "transcribe"):
                raise ValueError("simulated inference failure")

    job_records = [r for r in caplog.records if r.name == "songbox.job_cost"]
    assert len(job_records) == 1
    assert job_records[0].job_type == "transcribe"  # type: ignore[attr-defined]


def test_separate_endpoint_logs_a_real_gpu_job_cost_line(
    caplog: pytest.LogCaptureFixture, synthetic_wav: Path
) -> None:
    headers = {"X-Dev-Tenant-Id": str(uuid.uuid4()), "X-Dev-User-Id": str(uuid.uuid4())}
    app.dependency_overrides[get_acoustid_client] = lambda: FixtureAcoustIDClient({})
    try:
        with synthetic_wav.open("rb") as fh:
            upload_response = client.post(
                "/tracks/upload",
                headers=headers,
                data={"lane": "A", "attestation_text": "I made this recording"},
                files={"file": ("tone.wav", fh, "audio/wav")},
            )
        assert upload_response.status_code == 200
        track_id = upload_response.json()["track_id"]

        with caplog.at_level(logging.INFO, logger="songbox.job_cost"):
            separate_response = client.post(f"/tracks/{track_id}/separate", headers=headers)
        assert separate_response.status_code == 200
    finally:
        app.dependency_overrides.pop(get_acoustid_client, None)

    job_records = [r for r in caplog.records if r.name == "songbox.job_cost"]
    assert len(job_records) == 1
    assert job_records[0].track_id == track_id  # type: ignore[attr-defined]
    assert job_records[0].job_type == "separate"  # type: ignore[attr-defined]
    assert job_records[0].duration_seconds > 0  # type: ignore[attr-defined]
    assert job_records[0].estimated_cost_usd is None  # type: ignore[attr-defined]
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd services/api && python -m pytest tests/test_job_cost.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.job_cost'`

- [ ] **Step 4: Write `app/job_cost.py`**

Create `services/api/app/job_cost.py`:

```python
from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path

import yaml

_GPU_COSTS_PATH = Path(__file__).resolve().parents[3] / "config" / "gpu_costs.yaml"
_job_cost_logger = logging.getLogger("songbox.job_cost")


def estimate_cost_usd(
    duration_seconds: float, *, gpu_costs_path: Path = _GPU_COSTS_PATH
) -> float | None:
    """Looks up the most recent (as of today, never a future-dated entry) price-per-second entry
    in gpu_costs.yaml and multiplies by duration_seconds. Returns None if `providers` is empty or
    no entry's effective_date has arrived yet -- never a fabricated number.

    There is no per-request GPU-backend-selection concept anywhere in this codebase yet: every
    job runs through app.gpu_backend.run_inference(), which is ADR-0001's `local` backend,
    hardcoded (the `modal`/`runpod` implementation doesn't exist until M7c). `local` execution
    runs on the developer's own machine and has no real per-second billing to attach a number to,
    regardless of what this file contains -- so this function has no backend parameter and
    doesn't need one. It always returns the single most-recently-dated applicable entry, since
    this project only ever has one meaningfully "current" price at a time.
    """
    data = yaml.safe_load(gpu_costs_path.read_text()) or {}
    providers = data.get("providers") or []
    if not providers:
        return None

    today = date.today()
    applicable = [p for p in providers if date.fromisoformat(str(p["effective_date"])) <= today]
    if not applicable:
        return None

    latest = max(applicable, key=lambda p: date.fromisoformat(str(p["effective_date"])))
    return float(latest["price_per_second_usd"]) * duration_seconds


@contextmanager
def track_job_cost(track_id: object, job_type: str) -> Iterator[None]:
    """Times the wrapped block and logs one job-cost line on exit -- even if the block raised,
    since the duration up to a failure is still real and worth recording (retries/failures are
    exactly the kind of waste this exists to give visibility into). Never logs anything about the
    track's content -- only its opaque id, the job type, real measured duration, and an estimated
    cost that is `null` until real GPU pricing data exists.
    """
    start = time.monotonic()
    try:
        yield
    finally:
        duration_seconds = time.monotonic() - start
        _job_cost_logger.info(
            "gpu_job",
            extra={
                "track_id": str(track_id),
                "job_type": job_type,
                "duration_seconds": round(duration_seconds, 3),
                "estimated_cost_usd": estimate_cost_usd(duration_seconds),
            },
        )
```

`Path(__file__).resolve().parents[3]` from `services/api/app/job_cost.py` resolves to the repo
root (`app/` -> `api/` -> `services/` -> repo root), matching where `config/gpu_costs.yaml` lives.

- [ ] **Step 5: Run tests to verify the standalone tests pass (the route-integration test still fails)**

Run: `cd services/api && python -m pytest tests/test_job_cost.py -v`
Expected: the five tests not touching `/tracks/{id}/separate` PASS.
`test_separate_endpoint_logs_a_real_gpu_job_cost_line` FAILS (no `songbox.job_cost` records —
`separate_track` doesn't call `track_job_cost` yet).

- [ ] **Step 6: Wrap the four GPU call sites in `app/routes/tracks.py`**

Add this import near the other `app.*` imports:

```python
from app.job_cost import track_job_cost
```

In `separate_track`, wrap the existing `run_inference(...)` call (and its three `except` clauses)
in a `with track_job_cost(track.id, "separate"):` block:

```python
        try:
            with track_job_cost(track.id, "separate"):
                try:
                    stem_paths = run_inference(
                        lambda: separate_audio(Path(tmp.name), model_name=model_name),
                        timeout_seconds=SEPARATION_TIMEOUT_SECONDS,
                    )
                except BackendBusyError as exc:
                    raise HTTPException(status_code=503, detail=str(exc)) from exc
                except BackendTimeoutError as exc:
                    raise HTTPException(status_code=504, detail=str(exc)) from exc
                except SeparationError as exc:
                    raise HTTPException(
                        status_code=422, detail=f"could not separate audio: {exc}"
                    ) from exc
        finally:
            Path(tmp.name).unlink(missing_ok=True)
```

(This nests the new `with track_job_cost(...)` block one level inside the existing outer `try`/
`finally` that cleans up the temp file — the outer `try:`/`finally:` lines and their indentation
are unchanged; only the inner `try:`/`except` block gains one level of indentation from the new
`with` wrapping it.)

In `transcribe_track`, apply the same wrapping to its `run_inference(...)` call:

```python
        try:
            with track_job_cost(track.id, "transcribe"):
                try:
                    result = run_inference(
                        lambda: run_transcription_and_alignment(
                            Path(tmp.name), model_size=model_size
                        ),
                        timeout_seconds=TRANSCRIPTION_TIMEOUT_SECONDS,
                    )
                except BackendBusyError as exc:
                    raise HTTPException(status_code=503, detail=str(exc)) from exc
                except BackendTimeoutError as exc:
                    raise HTTPException(status_code=504, detail=str(exc)) from exc
                except TranscriptionError as exc:
                    raise HTTPException(
                        status_code=422, detail=f"could not transcribe audio: {exc}"
                    ) from exc
                except AlignmentError as exc:
                    raise HTTPException(
                        status_code=422, detail="could not align transcript to audio"
                    ) from exc
        finally:
            Path(tmp.name).unlink(missing_ok=True)
```

In `realign_track`, apply the same wrapping:

```python
        try:
            with track_job_cost(track.id, "realign"):
                try:
                    words = run_inference(
                        lambda: align_words(Path(tmp.name), body.text),
                        timeout_seconds=TRANSCRIPTION_TIMEOUT_SECONDS,
                    )
                except BackendBusyError as exc:
                    raise HTTPException(status_code=503, detail=str(exc)) from exc
                except BackendTimeoutError as exc:
                    raise HTTPException(status_code=504, detail=str(exc)) from exc
```

(Keep whatever `except` clauses and the rest of `realign_track`'s existing body follow this call
site exactly as they are today — only the `with track_job_cost(track.id, "realign"):` wrapper and
its one added indentation level are new.)

In `package_track`, apply the same wrapping:

```python
        try:
            with track_job_cost(track.id, "package"):
                try:
                    result = run_inference(
                        lambda: build_package(
                            vocals_path=tmp_paths["vocals"],
                            drums_path=tmp_paths["drums"],
                            bass_path=tmp_paths["bass"],
                            other_path=tmp_paths["other"],
                            pitch_model=pitch_model,
                        ),
                        timeout_seconds=PACKAGE_TIMEOUT_SECONDS,
                    )
                except BackendBusyError as exc:
                    raise HTTPException(status_code=503, detail=str(exc)) from exc
                except BackendTimeoutError as exc:
                    raise HTTPException(status_code=504, detail=str(exc)) from exc
                except (AccompanimentError, PitchExtractionError, StructureExtractionError) as exc:
                    raise HTTPException(
                        status_code=422, detail="could not package track"
                    ) from exc
```

(Keep whatever follows this call site in `package_track` today exactly as it is — only the `with
track_job_cost(track.id, "package"):` wrapper and its one added indentation level are new.)

- [ ] **Step 7: Run tests to verify they pass**

Run: `cd services/api && python -m pytest tests/test_job_cost.py -v`
Expected: PASS (6/6).

- [ ] **Step 8: Run ruff, mypy, and the full suite**

Run: `cd services/api && python -m ruff check . && python -m mypy app && python -m pytest -q`
Expected: all clean, no regressions.

- [ ] **Step 9: Commit**

```bash
git add services/api/pyproject.toml services/api/app/job_cost.py \
    services/api/app/routes/tracks.py services/api/tests/test_job_cost.py
git commit -m "M7b: add GPU job cost logging against the gpu_costs.yaml stub"
```

---

## Self-Review Notes

**Spec coverage:** Decision 1 (per-IP `slowapi` rate limiting, exact routes and limits, 429 +
`Retry-After`) — covered in Task 2, including the verified-necessary dependency-ordering fix for
the admin endpoint that a naive reading of the spec's decorator-based design would have missed.
Decision 2 (structured JSON request logging, fixed field set, nothing implicit) — covered in
Task 1, with an explicit regression test proving the field set never grows to include track
content. Decision 3 (GPU job cost logging, real duration, `null` cost until `gpu_costs.yaml` is
populated, no backend-selection concept) — covered in Task 3. The "What M7b builds" list's 8 items
all map onto these three tasks one-to-one. The testing strategy's four bullets (real Redis, real
captured logs, real job timing, arithmetic-correctness-against-a-temporary-populated-yaml) are all
present as concrete tests, not placeholders.

**Placeholder scan:** No TBD/TODO in this plan's own instructions (the `TODO: unmeasured` strings
that appear are real, quoted values from the actual `gpu_costs.yaml` file's own header comment,
not placeholders in this plan).

**Type consistency:** `track_job_cost(track_id: object, job_type: str)` (Task 3) is called
identically at all four Task 3 call sites (`track.id`, `"separate"`/`"transcribe"`/`"realign"`/
`"package"`) — matches the enum values fixed in the spec's Decision 3 and in `job_cost.py`'s own
docstring. `estimate_cost_usd(duration_seconds: float, *, gpu_costs_path: Path = _GPU_COSTS_PATH)
-> float | None`'s signature matches every call site, including the tests that override
`gpu_costs_path` for isolation. `limiter` (Task 2, `app/rate_limit.py`) is imported and used
identically (`@limiter.limit("N/period")`) across `tracks.py`'s five decorated routes, and via the
verified different (dependency-ordered) mechanism in `admin.py` — this difference is intentional
and documented in Global Constraints, not an inconsistency.
