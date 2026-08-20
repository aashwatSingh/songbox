# Status

Last updated: 2026-08-19.

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

- Nothing mid-work right now. M0 is done; M1 (rights gate) hasn't been started.

## Blocked

- **No GitHub remote configured yet**, so `.github/workflows/ci.yml` has only been reasoned about, not
  actually run by GitHub Actions. Not blocking M1 work, only CI-on-push.

## Next three actions

1. Decide whether to commit the M0 skeleton now (nothing is committed yet — nothing in this repo has
   git history).
2. Decide whether to start M1 (rights gate: three lanes, attestation records, Chromaprint
   fingerprinting, AcoustID lookup, hold-and-review flow) — this is a 2-session milestone per
   `docs/PLAN.md` and the working agreement calls for test-first development on it specifically, so it
   likely wants its own scoping pass rather than starting cold.
3. Push to a GitHub remote (once one exists) to get CI actually running.
