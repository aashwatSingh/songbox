# M7b: Rate Limits + Observability — Design Spec

## Context

`docs/PLAN.md` names M7 as "retention purge, takedown endpoint, rate limits, observability, load
test, [GPU backend swap]" with no policy specified for any of it. Per the user's approved
decomposition, M7 splits into three sub-milestones: M7a (retention purge + takedown, done — see
`docs/STATUS.md`), M7b (this milestone — rate limits + observability), M7c (the cloud GPU backend
swap + real no-egress sandbox validation + load test, the one requiring a real external
Modal/RunPod account).

Two things were verified before this design was written, not assumed:

- **This codebase has zero rate-limiting or observability infrastructure today.** No
  `slowapi`/`limits`, no structured logging, no metrics library, no middleware beyond dev-only
  CORS in `services/api/app/main.py`.
- **Every GPU-invoking pipeline stage (Demucs separation, Whisper+wav2vec2 transcription/
  alignment, CREPE pitch/structure extraction) runs synchronously in-process inside its route
  handler** — `POST /tracks/{id}/separate`, `/transcribe`, `/realign`, and `/package` all block
  until the job finishes. There is no RQ enqueue/worker-pool indirection in this codebase despite
  the original spec's "worker pool" framing; the GPU-backend abstraction (`local` vs. `modal`/
  `runpod`) is a swappable function call, not an async queue. This matters for both milestones
  here: a rate limit on these routes directly throttles GPU spend, and a job-cost timer can wrap
  the call in place with no queue-polling machinery needed.
- **Redis is already running** (`docker-compose.yml`), but nothing in this codebase actually uses
  it yet — `workers/` is an empty scaffold directory from M0, and no code anywhere imports `rq` or
  connects to Redis; every GPU-invoking route calls its model directly in-process (see above).
  This was checked directly, not assumed from the original spec's "Redis + RQ" framing, which
  named an intended architecture this codebase never actually built. Redis being both running and
  entirely unused makes it a clean, dependency-free backend for this milestone's rate-limit
  counters — no conflict with existing usage because there is none.
- **`config/gpu_costs.yaml` already exists** as an empty, explicitly `TODO: unmeasured` stub —
  this milestone's job-cost logging is designed to read from it, not duplicate it.
- **Open question 9** (`docs/PLAN.md`) is still unresolved: there is no real authentication
  anywhere in this codebase. Every request identifies itself via the dev-only, trivially spoofable
  `X-Dev-Tenant-Id` header. This directly shapes the rate-limiting design below — a limit keyed on
  that header protects nothing against a deliberate abuser, only against accidental client bugs.

## Decision 1: rate limiting is per-IP, via `slowapi`, on cost/abuse-relevant endpoints only

**Key:** client IP address (`slowapi`'s `get_remote_address`), not `X-Dev-Tenant-Id`. Given open
question 9, a tenant-keyed limit is bypassed by anyone willing to change a header; per-IP is the
only backstop that's actually enforceable today. This doesn't solve real auth (still open) — it's
a minimal, honest defense against unauthenticated abuse, stated as such.

**Mechanism:** `slowapi` (built on the `limits` package), backed by the already-running Redis
instance. `limits`' Redis storage uses atomic Lua scripts for its counters, so concurrent requests
can't race past a limit the way a hand-rolled `INCR`/`EXPIRE` pair could under contention — this is
exactly the kind of correctness-sensitive counting not worth re-deriving in-house. New dependency:
`slowapi` in `services/api/pyproject.toml`.

**Scope — limited routes and their limits (per IP):**

| Route | Limit | Why |
|---|---|---|
| `POST /tracks/upload` | 30/hour | The abuse entry point — storage and DB writes, plus a synchronous Chromaprint/AcoustID lookup, even though it's not itself a GPU job. |
| `POST /tracks/{id}/separate` | 20/hour | Demucs — GPU-costing. |
| `POST /tracks/{id}/transcribe` | 20/hour | Whisper + wav2vec2 — GPU-costing. |
| `POST /tracks/{id}/realign` | 20/hour | wav2vec2 realignment — GPU-costing. |
| `POST /tracks/{id}/package` | 20/hour | CREPE pitch/beat/section extraction — GPU-costing. |
| `POST /admin/tracks/{id}/takedown` | 10/minute | Not GPU-costing, but M7a's final whole-branch review explicitly flagged this endpoint as brute-forceable (`secrets.compare_digest` prevents timing attacks, not guessing against a single static shared secret with no lockout). This limit is defense-in-depth, not the primary defense — the admin key's own entropy is. |

Every other route (`GET /tracks`, `GET /tracks/{id}/package`, `GET /health`,
`confirm-attestation`, `review-queue`, etc.) stays unlimited — they're cheap reads or low-cost
writes, and rate-limiting them would add friction without addressing real cost or abuse risk.

**These numbers are explicit policy, not measurement.** This project has no real traffic yet (M7c,
the cloud backend swap, hasn't happened) — there's nothing to tune against. They're a defensible
starting point (per `CLAUDE.md`'s "no fabricated... figure" rule, they're labeled as a policy
choice in code comments, exactly like `RETENTION_WINDOW_DAYS` was in M7a), not a result derived
from load data. Revisiting them with real numbers is explicitly future work (M7c's load test is
the first point real traffic-shaped data will exist).

**Exceeding a limit** returns HTTP `429` with a `Retry-After` header (`Limiter(..., headers_enabled=True)` — this is opt-in, not slowapi's default). Every 429 is
also captured by Decision 2's request logging, so real abuse patterns are visible in logs even
before any specific limit is retuned.

## Decision 2: structured JSON request logging via stdlib `logging`, no new dependency

A FastAPI middleware wraps every request, measuring wall-clock duration and emitting one JSON line
to stdout via Python's stdlib `logging` module with a small hand-written `JSONFormatter` (no
`structlog` or similar — this is straightforward formatting, not the kind of concurrency-sensitive
logic Decision 1 avoided hand-rolling). Fields per line: `timestamp` (ISO 8601 UTC),
`method`, `path`, `status_code`, `duration_ms`, `tenant_id` (read from `X-Dev-Tenant-Id` if
present, else `null` — logged for correlation, not trusted as real identity, consistent with open
question 9), `client_ip`.

**Never logged:** track `title`/`artist`, `attestation_text`, audio bytes, lyric text, or signed
URLs — `CLAUDE.md`'s "never log raw audio, lyrics, or signed URLs" rule applies directly here for
the first time to a whole-API logging layer rather than one code path. Track identity in every log
line is limited to the opaque `track_id` UUID (already part of `path` on most routes) — nothing
else about a track is ever logged.

## Decision 3: GPU job cost logging, reading real prices from the existing `gpu_costs.yaml` stub

A small shared context manager (`app/job_cost.py`, new) wraps the actual model-inference call
inside each of the four GPU-invoking route handlers (`separate`, `transcribe`, `realign`,
`package`). On exit, it logs one JSON line (via the same `logging`/`JSONFormatter` machinery as
Decision 2, distinguishable by a `job_type` field) with: `track_id`, `job_type` (`"separate"` |
`"transcribe"` | `"realign"` | `"package"`), `duration_seconds` (real, measured via
`time.monotonic()`), and `estimated_cost_usd`.

**Checked directly, not assumed:** there is no GPU-backend-selection concept anywhere in this
codebase today. `app/gpu_backend.py`'s `run_inference()` — the single call site every one of these
four routes goes through — is ADR-0001's `local` backend outright, hardcoded (a process-wide lock
plus a wall-clock timeout on the caller's own machine); the `modal`/`runpod` implementation doesn't
exist yet (that's M7c's job). Since `local` execution runs on the developer's own machine, it has
no real per-second billing to attach a number to, regardless of what `config/gpu_costs.yaml`
contains. So `estimated_cost_usd` is computed by a standalone lookup — `job_cost.py`'s
`estimate_cost_usd(duration_seconds: float) -> float | None` — that reads
`config/gpu_costs.yaml`'s `providers` list, picks the entry with the most recent
`effective_date` not in the future (the yaml's own header comment already establishes dated,
non-overwritten entries as its convention), multiplies `price_per_second_usd * duration_seconds`,
and returns `None` if `providers` is empty. It has no backend parameter and doesn't need one: this
project only ever has one meaningfully "active" price at a time (there's no concurrent multi-backend
operation), so the lookup is "whatever the most recent populated entry says," independent of which
code path actually ran the job. Today, with `providers: []` still empty (a `TODO: unmeasured` stub,
per its own header comment), every call returns `None`, logged as `estimated_cost_usd: null` —
never a fabricated number, regardless of which route or backend produced the duration. This
directly feeds open question 4 ("cost per track end-to-end") the moment real pricing data lands —
no code change needed then, just populating the YAML file (and, later, M7c wiring an actual
`modal`/`runpod` backend that this same lookup would apply to unchanged).

This is entirely additive around the existing synchronous call sites — no change to what
`separate`/`transcribe`/`realign`/`package` actually do, just a timer wrapped around the existing
inference call in each.

## What M7b builds

1. `services/api/pyproject.toml` — add `slowapi` dependency.
2. `services/api/app/rate_limit.py` (new) — the shared `Limiter` instance (Redis-backed,
   `get_remote_address` key func) and the 429 exception handler, imported by `main.py` and the
   route modules that apply per-route limits.
3. `services/api/app/routes/tracks.py` — add `@limiter.limit(...)` decorators to `upload`,
   `separate`, `transcribe`, `realign`, `package` with the values from Decision 1's table.
4. `services/api/app/routes/admin.py` — add `@limiter.limit("10/minute")` to the takedown route.
5. `services/api/app/logging_config.py` (new) — `JSONFormatter` and a `configure_logging()`
   call wired into `app/main.py` at startup.
6. `services/api/app/main.py` — a request-timing middleware emitting Decision 2's structured log
   line per request; wires in `rate_limit.py`'s limiter and exception handler; calls
   `configure_logging()`.
7. `services/api/app/job_cost.py` (new) — the shared context manager from Decision 3, plus a
   small helper reading `config/gpu_costs.yaml`.
8. `services/api/app/routes/tracks.py` — wrap the four GPU-invoking call sites with
   `job_cost.py`'s context manager.

No frontend changes — this milestone is entirely backend, matching the "harden and launch" framing
(none of this is user-facing product surface).

## Testing strategy

Backend, infrastructure-touching code — test-first, per the working agreement, against real Redis
and real captured log output, not mocks.

- Rate limiting: a real test hitting one of the limited routes past its limit against the real
  running Redis, asserting a genuine `429` with a `Retry-After` header on the request that exceeds
  it; a second test from a distinct simulated client IP confirming its own limit is untouched by
  the first client's usage (proving the Redis-backed counter is genuinely per-IP, not global); a
  test confirming an unlimited route (e.g. `GET /tracks`) never 429s regardless of call count.
- Request logging: capture real stdout/the real logging handler (e.g. via `caplog` or a redirected
  stream) during a real request, parse the emitted line as JSON, and assert on its fields —
  including a real regression test asserting a track's `title`/`attestation_text` never appears in
  any captured log line for a request that touches that data.
- Job cost logging: run a real (or fixture-backed, matching this codebase's existing
  `FixtureAcoustIDClient`-style test conventions) `separate`/`transcribe`/`realign`/`package` call,
  parse the emitted job-cost log line, assert `duration_seconds` is a real positive measured
  value and `estimated_cost_usd` is `null` while `config/gpu_costs.yaml` stays empty (proving no
  fabricated cost figure) — plus one test that temporarily populates the YAML with a fake price
  entry and confirms the multiplication is arithmetically correct, so the day real prices land the
  math has already been proven right.

## Out of scope for M7b

Load testing (M7c — needs the real cloud backend to be meaningful). A `/metrics` Prometheus
endpoint (nothing in this project's infrastructure consumes one yet — building it now would be for
infrastructure that doesn't exist). Populating real GPU provider pricing into
`config/gpu_costs.yaml` (a data-entry task requiring real provider quotes, not an engineering task
this milestone can do). Retuning the rate-limit numbers with real traffic data (there isn't any
yet — M7c's load test is the first point that data will exist). Any change to the GPU-backend
abstraction itself (`local` vs. `modal`/`runpod` — M7c's scope). Solving open question 9 (real
auth) — this milestone's per-IP keying is an explicit, honest workaround, not a resolution.
