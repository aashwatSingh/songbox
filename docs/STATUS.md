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
- `services/api/app/fingerprint.py` — ffmpeg/Chromaprint-based fingerprinting (`fpcalc` via
  subprocess).
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
