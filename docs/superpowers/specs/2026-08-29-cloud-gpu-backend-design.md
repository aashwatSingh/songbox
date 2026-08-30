# M7c: Cloud GPU Backend Swap + Sandbox Validation — Design Spec

## Context

`docs/PLAN.md` names M7's third piece as "swap the GPU backend from local to Modal/RunPod and
validate the no-egress sandbox for real." Per the approved M7 decomposition, this is M7c — the
last piece of M7, after M7a (retention purge + takedown, done) and M7b (rate limits +
observability, done).

`docs/adr/0001-gpu-backend-abstraction.md` set this up from M0: a swappable backend interface
(`services/api/app/gpu_backend.py`'s `run_inference()`), `local` used through M0–M6 for free/fast
dev iteration on this machine's own GPU, with the real `modal`/`runpod` implementation and the
real no-egress sandbox validation deliberately deferred to M7. That ADR is explicit that any claim
the pipeline is network-sandboxed before this milestone is false — this is the first point that
claim is actually tested, not assumed.

**This milestone has a real, external, non-engineering dependency that shapes everything below:**
implementing and validating a cloud GPU backend requires a real account with a cloud GPU provider,
real billing, and a real API token — none of which exist in this codebase or can be created by an
engineering session. This was flagged before the design conversation started. The design and a
meaningful amount of implementation (the backend module, the Modal Function definitions, the image
build) can proceed without live credentials; nothing can actually *run* — no sandbox validation, no
load test, no real cost number — until the account exists.

**Provider choice, verified against real current documentation, not memory:** Modal is used, not
RunPod. Modal's `@app.function(..., block_network=True)` is a real, documented parameter directly
on the exact execution primitive this project would use (verified via
[Modal's Networking and security guide](https://modal.com/docs/guide/sandbox-networking) and the
Modal changelog/docs referencing `block_network` on Functions, not just Sandboxes) — a direct fit
for the "no network egress" requirement. A less strict `outbound_domain_allowlist`/
`outbound_cidr_allowlist` pair is also available if full blocking ever proves too strict. RunPod's
public docs, searched the same way, surface no equivalent lightweight per-endpoint egress-control
primitive for Serverless — the closest analog is a separate "Secure Cloud" compliance tier, a
bigger commitment than a decorator argument. Modal's real, current GPU pricing (per
[modal.com/pricing](https://modal.com/pricing)) also matters directly for Decision 4 below.

## Decision 1: the interface changes from closure-based to bytes-based dispatch

`run_inference()`'s current signature, `run_inference(fn: Callable[[], T], *, timeout_seconds:
float) -> T`, takes an arbitrary zero-argument closure — every one of the four call sites in
`services/api/app/routes/tracks.py` closes over a **local temp file path**
(e.g. `lambda: separate_audio(Path(tmp.name), model_name=model_name)`). That's fine for the
`local` backend (same process, same filesystem) but cannot work for a real remote backend: a Modal
Function runs in a separate container with no access to the API server's local disk, and Modal
ships function *code* via its own decorators, not ad-hoc closures capturing local state.

**Checked directly, not assumed:** none of the four underlying pipeline functions
(`separate_audio`, `run_transcription_and_alignment`, `align_words`, `build_package`) need to
change — they're already file-path-based and backend-agnostic. Only the *dispatch* layer changes.
The new interface centers on bytes crossing the local-API-server ↔ backend boundary, not a Path:

- **`local` backend** (`GPU_BACKEND=local`, the default): writes the caller-supplied bytes to a
  local temp file, calls the existing pipeline function with that path, returns the typed result —
  functionally identical to what each route already does today, just moved behind the backend
  interface instead of being inlined at each of the four call sites.
- **`modal` backend** (`GPU_BACKEND=modal`): calls a remote Modal Function
  (`block_network=True`), passing the audio bytes directly as the function argument. The Modal
  Function writes to its *own* container-local temp file, calls the same pipeline function
  (imported into the Modal image's environment), and returns the typed result, which Modal
  marshals back over its own control plane automatically — no network call from inside the
  function body at all.

## Decision 2: a spec-improving discovery — zero egress, not restricted egress

The original spec's "no network egress except to object storage and the queue" phrasing assumed a
data flow where the worker itself fetches from object storage. This codebase's actual
architecture already fetches bytes from MinIO in the **API layer**, before `run_inference()` is
ever called — the ML call itself never touches storage directly, today or after this milestone.
That means the Modal Function can use `block_network=True` (zero egress, no allowlist needed at
all) rather than the originally-assumed restricted-egress allowlist — stronger than what the
original spec assumed was necessary, and simpler to implement. This is a genuine correction to the
original spec's assumption, not a narrowing of the security requirement.

## Decision 3: backend selection stays swappable, `local` stays the default

A `GPU_BACKEND` environment variable (`"local"` | `"modal"`, defaulting to `"local"` if unset) on
`app/gpu_backend.py`'s public entry point. This matches ADR-0001's original intent exactly: `local`
remains available for free, fast dev iteration going forward (no cost, no network dependency for
routine development), while `modal` is the real, validated path this milestone proves works.
Nothing about the four existing routes' request/response shapes changes — this is purely an
internal dispatch change.

## Decision 4: real GPU type and cost data, closing the loop from M7b

Modal's `A10` GPU (their real name — not AWS's `A10G`) is the target: $0.000306/second per
[modal.com/pricing](https://modal.com/pricing), comfortably sized for this pipeline's model sizes
(Demucs, faster-whisper, wav2vec2, torchcrepe — none of which need a top-tier H100/B200-class
card). `config/gpu_costs.yaml` (an empty `TODO: unmeasured` stub since M0, and the exact file M7b's
`job_cost.py` was built to read from) gets its first real entry: Modal's A10 price, dated the day
this milestone's real testing runs. The moment that entry lands, M7b's existing cost-logging code
starts emitting real `estimated_cost_usd` values instead of `null` — zero code changes needed in
`job_cost.py` itself, since it was already built to read whatever the file says.

## Decision 5: sandbox validation is the actual point of this milestone

Per ADR-0001, this milestone's real job is proving the no-egress claim, not just wiring up a
provider. Two things, both against the real deployed Modal Function, not a local approximation:

- **Re-run the security-relevant subset** of the existing malformed-file/abuse test suite (the
  tests M2 built for hardened ingest) against the real `modal` backend, confirming nothing about
  running on a genuinely different, remote execution environment changes those results.
- **A deliberate "try to phone home" probe**: one new test track whose processing path
  deliberately attempts a real outbound HTTP call (e.g. to a public, harmless endpoint) from
  *inside* the Modal Function. This must genuinely fail/be blocked — confirming `block_network=True`
  actually blocks traffic in this specific deployment, not merely that it was left unconfigured
  (which would look identical from the outside if egress happened to succeed by accident, or if
  the parameter were silently ignored). This is the one test in the whole milestone that must be
  run against the real live sandbox to mean anything — no local approximation is possible.

## Decision 6: load test — light, per the approved scope

3–5 real synthetic tracks processed **concurrently** through the full Modal-backed pipeline
(separate → transcribe → package), confirming the pipeline behaves correctly under genuine
concurrency (not just "one job at a time," which is all the `local` backend's process-wide lock
ever exercised) and producing the milestone's one real, measured cost-per-track figure — closing
`docs/PLAN.md` open question 4 for the first time with real data instead of `TODO: unmeasured`.
This is explicitly **not** a production-scale load test (dozens+ of concurrent jobs, sustained
duration, cold-start/autoscaling-behavior characterization) — that's disproportionate to a project
with no real traffic yet, and is called out as future work if/when this project has real users.

## What M7c builds

1. `services/api/app/gpu_backend.py` — add `GPU_BACKEND` env-var-based dispatch, restructure
   `run_inference()`'s public interface around bytes rather than an arbitrary closure (existing
   `local`-backend behavior preserved exactly, just relocated behind the new interface).
2. `services/api/app/modal_app.py` (new) — the Modal `App`/`Image`/Function definitions: one
   `block_network=True` Function per pipeline stage (or a single dispatching Function taking a
   stage-name argument — the plan will decide which, weighing Modal image-build time against code
   duplication), covering `separate`/`transcribe`/`realign`/`package`.
3. `services/api/app/routes/tracks.py` — update the four call sites to use the new bytes-based
   dispatch interface (no change to request/response shapes, HTTP behavior, or error handling).
4. `config/gpu_costs.yaml` — one real entry: Modal's A10 pricing, dated the day real testing runs.
5. A new, real-Modal-only test file re-running the security-relevant malformed-file/abuse cases
   plus the "try to phone home" probe against the live `modal` backend.
6. A short, real load-test script/test exercising 3–5 concurrent real jobs and recording the
   resulting real cost-per-track figure into `docs/BENCHMARKS.md`, closing open question 4.
7. `docs/STATUS.md` and `docs/PLAN.md` open question 4 updated with the real measured result.

No frontend changes — this milestone, like M7a and M7b, is entirely backend infrastructure.

## Testing strategy

Everything that can be tested without live Modal credentials is tested first and normally: the
`local` backend's behavior after the interface restructuring (Decision 1) must be provably
unchanged — the existing test suite covering `/separate`/`/transcribe`/`/realign`/`/package`
already exercises this and must keep passing exactly as-is against `GPU_BACKEND=local` (the
default), with no new mocks introduced for that path.

Everything that inherently requires the real cloud sandbox (Decision 5's two checks, Decision 6's
load test, Decision 4's real cost number) cannot be tested any other way — mocking Modal's network
policy would prove nothing about whether the real policy is actually configured and enforced. These
are real-money-costing, real-account-requiring tests, explicitly bounded by the approved ~$10
budget (Modal's Starter-plan $30/month free credit comfortably covers this, so no real charge is
expected during this milestone's validation).

## Out of scope for M7c

Production-scale load testing (dozens+ concurrent jobs, sustained duration, autoscaling
characterization) — deferred until this project has real traffic to justify it. A RunPod
implementation — Modal is the chosen provider; RunPod isn't built as a fallback/alternative unless
a real problem with Modal surfaces later. Retuning M7b's rate-limit numbers against real traffic —
still no real traffic exists after this milestone either, just a real *cost* number. Any UI/frontend
surface for selecting or monitoring the GPU backend — this is operational configuration
(`GPU_BACKEND` env var), not a product feature. A full migration off `local` — `local` stays
available and remains the default for routine dev work, per Decision 3.
