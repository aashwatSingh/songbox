# Decisions log

Lightweight log for decisions made without a full ADR — per working agreement rule 5, ambiguity costing
under two hours to unwind gets logged here rather than escalated.

## 2026-08-19 — Repo location and init

Created at `Downloads\songbox`, fresh `git init`. Matches the convention already used for sibling
projects in this Downloads folder (nle-engine, distrokid, fortnite-s1, deepwater-nights, etc.) rather
than nesting under an existing project or using a different root.

## 2026-08-19 — Local infra: Docker Desktop on WSL2

Machine had no `docker` on PATH. Asked the user directly (this was a >2hr-to-unwind decision — infra
choice affects every session going forward): chose Docker Desktop with the WSL2 backend over (a)
Docker-in-WSL-without-Desktop or (b) native installs with no containers at all. Native-install-only was
explicitly flagged as diverging from the spec's `docker compose up` definition-of-done, which is why it
wasn't picked. Docker Desktop itself is not yet installed — that's a blocking next action, tracked in
`docs/STATUS.md`.

## 2026-08-19 — GPU backend: local for dev, Modal/RunPod for prod

Machine has a local NVIDIA GPU (CUDA 12.6 driver present). Asked the user: use it directly for
Demucs/Whisper/wav2vec2/CREPE during M0–M6 (free, fast iteration), and only wire up the serverless
Modal/RunPod backend in M7 when hardening for production. This is architecturally significant enough to
get its own ADR — see `docs/adr/0001-gpu-backend-abstraction.md` — because it means the "no network
egress" sandbox guarantee (spec §5) isn't actually validated until M7; local runs before that don't
prove it.
