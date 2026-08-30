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

## 2026-08-30 — `run_separate` cannot get `block_network=True`; `run_package`'s wire format changed to keep its own

Real deployment against real Modal infrastructure (M7c Task 4) found `run_separate`'s return value
(four full audio stems) routinely exceeds Modal's real 2 MiB inline-payload threshold, and above
that threshold Modal's own blob-storage transport needs network access from inside the container —
which `block_network=True` blocks, confirmed by a real failed deploy. Tried
`outbound_domain_allowlist` (scoping network to exactly Modal's own blob domain) as a
less-drastic fix first; `mypy` caught that this parameter doesn't exist on `@app.function` (it's
`modal.Sandbox`-only) before another wasted deploy attempt. Resolved by giving `run_separate`
normal networking (no `block_network` argument at all) — its own code never makes a network call,
so the actual gap is narrower than "arbitrary attacker-directed egress," but it is a real,
disclosed exception to Decision 2's original "zero egress" claim for all four functions. Full
reasoning lives in `app/modal_app.py`'s comment on `run_separate`, not just here.

The same final-review pass found `run_package`'s pitch-contour payload crosses the identical 2 MiB
threshold at this project's own 12-minute `MAX_DURATION_SECONDS` cap (measured: 2.67 MiB) — the
same bug, just never triggered by the milestone's 3-second synthetic test track. Fixed by keeping
`block_network=True` on `run_package` and instead shrinking its wire format: the Modal Function
struct-packs the pitch contour as three parallel byte arrays instead of a `list[PitchFrame]` of
dataclass instances (measured: 0.83 MiB at 12 minutes), and `gpu_backend.py`'s
`_run_package_modal` unpacks it back into a real `PackageResult` — no other caller in the codebase
ever sees the compact wire format. Chosen over the alternative (drop `block_network=True` from
`run_package` too, matching `run_separate`) because the fix here is cheap and keeps the stronger
guarantee, rather than conceding a second function to the same weaker posture as the first.
