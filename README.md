# SongBox

A karaoke-generation platform: upload a track, and it gets rights-checked, split into stems
(vocals/drums/bass/other), transcribed and word-aligned, and turned into a synced karaoke player
with pitch guide, key/tempo control, and live mic scoring.

Built end to end — rights gate, GPU inference pipeline, real authentication, and a browser player —
as a from-scratch full-stack project.

## What it does

1. **Upload** an audio file under one of three rights lanes (creator-owned, licensed, or
   public-domain/Creative Commons) with an attestation.
2. **Rights gate**: Chromaprint fingerprinting + AcoustID lookup decide automatically whether the
   track passes or needs manual review — nothing reaches a GPU without a pass.
3. **Source separation** (Demucs) splits the track into vocals/drums/bass/other stems.
4. **Transcription + forced alignment** (Whisper on the isolated vocal stem, wav2vec2 alignment)
   produces word-level timestamps.
5. **Pitch + structure extraction** (CREPE, beat/section detection) builds the pitch guide.
6. **Player**: synced word-highlighted lyrics, a pitch-lane visualization, an independent
   per-stem mixer, key/tempo transposition, and live microphone pitch scoring against the
   original vocal's contour.

## Architecture

```
Next.js (App Router)  ──────────  FastAPI API
                                       │  rights gate: PASS only
                                Object storage (MinIO / S3-compatible)
                                       │
                                GPU pipeline, swappable backend
                                  local (dev) ──or── Modal (cloud, sandboxed, no egress)
                                  Demucs → Whisper+wav2vec2 → CREPE → package
                                       │
                                Postgres (RLS-enforced multi-tenancy) + Redis (rate limiting)
```

- **Multi-tenant by construction**: every table carries `tenant_id`, enforced by Postgres row-level
  security, not just application-layer filtering — verified by tests that a signed-up user
  genuinely cannot see another tenant's tracks, and that the RLS-restricted database role has no
  grant on the identity tables at all.
- **Real authentication**: email + password with Argon2id hashing, DB-backed opaque session
  cookies (not JWT — see [`docs/adr/0002-authentication-model.md`](docs/adr/0002-authentication-model.md)
  for why), per-request session revocation on logout.
- **Swappable GPU backend**: the same pipeline code runs against a local CUDA GPU in dev or a
  sandboxed Modal deployment in "production" mode, with the no-network-egress sandbox guarantee
  validated against the real cloud deployment, not just asserted (see
  [`docs/adr/0001-gpu-backend-abstraction.md`](docs/adr/0001-gpu-backend-abstraction.md)).
- **No `yt-dlp`, no "paste a link"**: every track enters the system through an explicit upload +
  attestation, never a remote-fetch-from-a-third-party-platform path. This is a hard project
  invariant, not a missing feature.

## Tech stack

**Frontend** — Next.js 16 (App Router, Turbopack) · TypeScript · Tailwind CSS 4 · Web Audio API ·
`@soundtouchjs/audio-worklet` for real-time pitch/tempo shifting

**Backend** — FastAPI · SQLAlchemy 2.0 · Alembic · Postgres (row-level security) · Redis
(rate limiting) · MinIO (S3-compatible object storage) · Argon2id password hashing

**ML pipeline** — Demucs (source separation) · faster-whisper + wav2vec2 (transcription/alignment)
· torchcrepe (pitch tracking) · Chromaprint/AcoustID (audio fingerprinting)

**Infra** — Modal (sandboxed serverless GPU) · Docker Compose (local dev stack) · GitHub Actions CI

## Status

All 8 planned milestones (M0–M8) are complete: rights gate, hardened ingest, source separation,
transcription + alignment, pitch + structure extraction, the full player (mixer, transposition,
live mic scoring), retention/takedown, and real authentication.

**192 backend tests** (190 run by default; 2 more require an explicit opt-in since they make real,
billable calls to the live Modal deployment to verify the no-egress sandbox against real
infrastructure, not just local reasoning). `ruff` and `mypy --strict` clean throughout.

Full build history — every milestone's design spec, implementation plan, and status — lives in
[`docs/`](docs), including two whole-branch reviews (dispatched on the most capable available
model) that each independently found and fixed real security-relevant issues invisible to any
single change's own review.

**Known, honestly-tracked gaps** (see [`docs/STATUS.md`](docs/STATUS.md) for the full list):
alignment accuracy is measured at 68.2ms median, missing the ±50ms target; live mic-bleed survival
has never been tested against a real microphone/speaker setup; no production hosting is configured
yet (this runs locally via Docker Compose).

## Running it locally

Requires Docker, Python 3.12+, and Node 18+.

```bash
# 1. Start Postgres, Redis, and MinIO
docker compose up -d

# 2. Apply database migrations
cd services/api
pip install -e ".[dev]"
python -m alembic upgrade head

# 3. Start the API
python -m uvicorn app.main:app --port 8000

# 4. In a second terminal, start the frontend
cd apps/web
npm install
npm run dev
```

Then visit `http://localhost:3000`, sign up, and upload a track.

## Project layout

```
apps/web/          Next.js frontend
services/api/       FastAPI backend (app/, alembic/, tests/)
docs/                design specs, implementation plans, ADRs, status log
config/              GPU cost tracking
scripts/             local dev/demo tooling
```
