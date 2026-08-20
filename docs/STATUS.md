# Status

Last updated: 2026-08-20.

## Done — M1 complete

All of M1's own "done when" criterion (`docs/PLAN.md`: "a known commercial recording uploaded under
Lane A is held, and an original recording passes") is met and actually verified, not just written —
proven by `test_lane_a_upload_of_known_commercial_fingerprint_is_held` and
`test_lane_a_upload_of_original_recording_passes` in `services/api/tests/test_tracks_upload.py`
(Task 9).

Built across 11 tasks, each test-first and reviewed:
- `services/api/app/models.py` + initial Alembic migration — `licenses`, `rights_declarations`,
  `tracks`, `fingerprint_matches` tables, every one carrying `tenant_id`.
- Row-level security enforced on all four rights-gate tables, with the app connecting as a
  non-superuser role (`songbox_app`) so RLS actually applies — `services/api/app/db.py`'s
  `db_session_for_tenant` sets `app.tenant_id` via `set_config` per-session.
- `services/api/app/auth.py` — dev auth stub (`X-Dev-Tenant-Id`/`X-Dev-User-Id` headers), wired into
  `get_db` so every request's queries are automatically tenant-scoped by RLS, not by manual
  `WHERE tenant_id = ...` filtering in route code.
- `services/api/app/acoustid/` — `AcoustIDClient` interface, real HTTP implementation, and a
  `FixtureAcoustIDClient` test double driven by `services/api/app/acoustid/fixtures.py`.
- `services/api/app/fingerprint.py` — Chromaprint fingerprinting via ffmpeg's built-in `chromaprint`
  muxer (`ffmpeg -f chromaprint`), not the separate `fpcalc` binary — one fewer external tool to
  install/pin/track CVEs on, per the design spec.
- `services/api/app/gate.py` — `resolve_lane_outcome`, the lane x match-result table: Lane A always
  holds on a match, Lane B holds unless the license on file covers the recording, Lane C always holds
  on a match (PD/CC claims need manual verification even though they might be legitimately public
  domain), no match always passes, and an AcoustID lookup error holds rather than passing silently.
- `services/api/app/storage.py` — MinIO wrapper for uploaded track files.
- `services/api/app/routes/tracks.py` — `POST /tracks/upload` (end-to-end: fingerprint, gate,
  store file, write declaration/track/match rows) and `POST /tracks/{id}/confirm-attestation`
  (Lane A's path to override a hold with a stronger, named-release attestation — written as a new
  superseding `rights_declarations` row, never a mutation of the original).
- `services/api/app/routes/review_queue.py` (this task) — `GET /review-queue` (lists tracks stuck in
  `pending_review`, tenant-scoped via RLS) and `POST /review-queue/{id}/resolve` (a human reviewer
  approves -> `passed`, or rejects -> `rejected`). `"rejected"` is a human-review-only status; the
  automated gate in `gate.py` never produces it itself.
- 30 tests across `services/api/tests/`, all passing; `ruff check .` and `mypy app` (strict) both clean.

Deliberately deferred (all listed under "Out of scope for M1" in
`docs/superpowers/specs/2026-08-19-rights-gate-design.md`, so none of this should come as a surprise
later):
- Real auth (the `X-Dev-Tenant-Id`/`X-Dev-User-Id` header stub stays until a real milestone replaces
  it).
- A real AcoustID API key (`HTTPAcoustIDClient` is implemented but untested against the live service;
  tests run against `FixtureAcoustIDClient`).
- Upload hardening — presigned uploads, magic-byte validation, ffprobe gating, sandboxed transcode —
  all explicitly M2's job.
- Rate-limiting / abuse-alerting logic.
- `key` and `tempo` track fields.
- Admin roles (the review-queue endpoints are gated only by the dev auth stub's identity, not by any
  reviewer/admin role check).
- The `jobs`, `stems`, `lyric_versions`, `word_timings`, `pitch_contours`, `takedowns` tables (later
  milestones).

## Reviewed — M1

Each of M1's 11 tasks went through a task-scoped implement-then-review gate (per
superpowers:subagent-driven-development), not just a single pass. Four real, reproducible bugs
surfaced and were fixed along the way — this wasn't a smooth, uninterrupted build, and that's the
point of the gate:

- **Task 1** — `db_session_for_tenant` originally used `SET LOCAL app.tenant_id = :tenant_id`.
  Postgres's `SET`/`SET LOCAL` grammar rejects bound parameters outright (`psycopg.errors.SyntaxError`
  on every call, reproduced live) — switched to `SELECT set_config('app.tenant_id', :tenant_id, true)`,
  which does accept one and has identical transaction-scoped semantics.
- **Task 2** — the implementer added unrequested `alembic/__init__.py` and
  `alembic/versions/__init__.py` files, and the first one shadowed the real third-party `alembic`
  package, breaking `python -m alembic upgrade head` — the exact command the plan documents. Both
  files removed; standard `alembic init` never generates them for this reason.
- **Task 3 (the significant one)** — discovered that RLS policies alone don't work: `songbox`
  (`POSTGRES_USER` in the official postgres image) is a genuine Postgres superuser, and superusers
  unconditionally bypass every RLS policy regardless of `FORCE ROW LEVEL SECURITY` — confirmed live
  (`rolsuper = t`). A restricted, non-superuser role (`songbox_app`) was added, created and granted
  table privileges by the RLS migration itself, with `db_session_for_tenant` connecting through it
  instead. The reviewer independently live-verified `current_user` via that function actually resolves
  to `songbox_app` with `rolsuper = False`, audited the exact grants (nothing broader than needed), and
  ran a full downgrade/upgrade cycle.
- **Task 9** — two bugs surfaced together in the endpoint that proves M1's own "done when" criterion:
  (1) `tempfile.NamedTemporaryFile`'s `with`-block form keeps its own handle open, which Windows won't
  let a second process (ffmpeg) also open — fixed with `delete=False` + explicit close + manual
  cleanup; (2) none of the SQLAlchemy models declare `relationship()`s, so nothing gives the ORM
  ordering guidance across FK-dependent inserts in one flush — a 3-model insert chain
  (declaration→track→match) tripped a real `ForeignKeyViolation` that a 2-model chain didn't, proving
  genuine order-dependence rather than a phantom problem. Fixed with explicit `db.flush()` calls,
  applied proactively to Task 10's near-identical insert-then-update pattern too, so it didn't recur.

Two environment issues (not code bugs) were also hit and fixed permanently rather than worked around
per-task: a native Windows PostgreSQL 18 service was silently shadowing Docker's Postgres container on
port 5432 for any `localhost` client (remapped the container to 5433); and this session's Bash tool had
snapshotted its `PATH` before ffmpeg was installed mid-session, so Bash-driven subagents couldn't find
it even though PowerShell could (fixed by placing the binaries in `C:\Users\aashw\bin`, already first on
Bash's `PATH`).

Every fix above was independently verified — either by the task reviewer re-running the failing
command live, or by the reviewer reproducing the bug from scratch in an isolated script before
confirming the fix — not accepted on the implementer's word alone.

## Done — M0 complete

All of M0's own "done when" criteria are now met and actually verified, not just written:

- Repo scaffolded (`apps/web`, `services/api`, `workers`, `config`, `docs`, `docs/adr`).
- `CLAUDE.md`, `docs/PLAN.md`, `docs/DECISIONS_LOG.md`, `docs/adr/0001-gpu-backend-abstraction.md`.
- `services/api` — FastAPI health-check skeleton. `pytest`, `ruff check`, `mypy --strict` all pass.
- `apps/web` — Next.js 16 App Router + TypeScript + Tailwind shell (via `create-next-app`).
  `npm run lint` and `npm run build` both pass clean.
- `docker-compose.yml` — Postgres, Redis, MinIO. **Verified running**: `docker compose up -d` brought
  up all three containers, all report `(healthy)` after their healthchecks settled.
- `.github/workflows/ci.yml` — forbidden-deps check, API job (ruff/mypy/pytest, pip-cached), web job
  (lint/build, npm-cached). Not yet run on a remote — no GitHub remote configured yet (still open).
- `.pre-commit-config.yaml` — ruff + ruff-format on `services/api`, a local `mypy-api` hook that runs
  mypy through the project's own venv, and the forbidden-dependency guard. Not yet installed
  (`pre-commit install`) as a real git hook — still open, low priority.
- `scripts/check_forbidden_deps.py` — structurally parses manifests (pyproject.toml/
  requirements*.txt/package.json) and scans lockfiles for `yt-dlp`/`youtube-dl`/`pytube`. Regression
  tests at `scripts/tests/test_check_forbidden_deps.py`.
- `config/gpu_costs.yaml` — stub, `TODO: unmeasured`, no real pricing yet.
- **ffmpeg installed** (v9.0-full_build via winget, `Gyan.FFmpeg`) — confirmed `ffmpeg -version` and
  `ffprobe -version` both work. Compiled with `--enable-chromaprint` and `--enable-whisper`, useful
  for M1/M4 later.
- **Docker Desktop installed and running** (v4.87.0 via winget, `docker version` reports both client
  and server). All three Compose services confirmed healthy.

## Reviewed

Ran a full multi-angle code review (8 finder angles + verify pass) against the M0 skeleton. 6 confirmed
findings, all fixed:
- `scripts/check_forbidden_deps.py` — was a raw-text substring search (false-positived on comments
  mentioning banned names) that only checked manifests, not lockfiles (false-negative on transitive
  deps), and walked `node_modules`/`.venv` fully before filtering. Rewritten: structural parsing
  (tomllib/json/line-based) for manifests, substring scan for lockfiles (package-lock.json, yarn.lock,
  pnpm-lock.yaml, uv.lock, poetry.lock — safe there since they're machine-generated), and a pruning
  walk that never descends into ignored directories. Regression-tested (6 tests, stdlib unittest).
- `CLAUDE.md` — the tenant_id line falsely claimed a test currently enforces it; reworded to a
  forward-looking requirement.
- `.pre-commit-config.yaml` — the mypy hook used to pin its own independent FastAPI version, divergent
  from `services/api/pyproject.toml`. Replaced with a local hook (`scripts/run_mypy_api.py`) that runs
  mypy through the actual project venv, so pre-commit and CI type-check against identical installed
  dependencies.
- `.github/workflows/ci.yml` — added `cache: "pip"` to the api job's setup-python step, matching the
  web job's npm caching.
- All fixes verified together: forbidden-deps script clean on the real repo, its unit tests pass,
  `run_mypy_api.py` passes, API pytest/ruff/mypy all pass.

## In flight

- Nothing mid-work right now. M0 and M1 are both done; M2 (hardened ingest) hasn't been started.

## Blocked

- **No GitHub remote configured yet**, so `.github/workflows/ci.yml` has only been reasoned about, not
  actually run by GitHub Actions. Not blocking M2 work, only CI-on-push.

## Next three actions

1. Push to a GitHub remote (once one exists) to get CI actually running.
2. Decide whether to start M2 (hardened ingest: presigned upload, magic-byte validation, ffprobe
   gating, sandboxed transcode, all security-section limits enforced) — 1 session per `docs/PLAN.md`,
   *done when* a malformed-file test suite (truncated headers, wrong magic bytes,
   playlist-with-remote-URL, duration bomb) is fully rejected.
3. Get a real AcoustID API key before M2 wraps, so `HTTPAcoustIDClient` can be exercised against the
   live service at least once instead of only the fixture double.
