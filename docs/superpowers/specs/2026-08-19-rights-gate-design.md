# M1: Rights gate — design

Status: approved. Date: 2026-08-19.

## Context

M0 (skeleton) is done and committed. M1 is the next milestone per `docs/PLAN.md`: the rights gate —
the component the whole project's architecture is built around, since nothing may reach a GPU without
a PASS from it (`CLAUDE.md`). This spec covers the three ingress lanes, attestation records, Chromaprint
fingerprinting, AcoustID lookup, and the hold-and-review flow, per spec §3 and §7 of the original build
prompt and `docs/PLAN.md`'s M1 entry.

Three scope decisions were made with the user before designing the rest, because each one meaningfully
changes what M1 builds:

1. **Auth is stubbed, not real.** `X-Dev-User-Id` / `X-Dev-Tenant-Id` request headers stand in for a
   verified session. Real auth (Supabase auth, sessions) is a separate future milestone; swapping it in
   later only changes how those two identifiers get populated, not the gate logic, schema, or RLS.
2. **AcoustID is mocked, not real, for now.** No API key exists yet. The AcoustID client is built behind
   an interface with a fixture-driven test double; a real key can be dropped into an env var later with
   zero code changes.
3. **M1 includes a minimal, unhardened upload endpoint.** Full upload hardening (magic-byte validation,
   presigned URLs, sandboxing, size limits) is explicitly M2's job per `docs/PLAN.md`. M1's own
   "done when" criterion (a known commercial recording gets held, an original one passes) needs a real
   file to fingerprint, so a bare-bones `POST /tracks/upload` is built now and hardened later — M2 comes
   back and hardens this exact endpoint rather than M1 building a throwaway one.

## Data model

New SQLAlchemy models, Postgres via Alembic migrations. Every table carries `tenant_id`; a test asserts
this and that every query filters on it.

- **`rights_declarations`** — immutable (no updates, ever — review resolution lives elsewhere, see
  `fingerprint_matches`). `id`, `tenant_id`, `user_id`, `lane` (`A`/`B`/`C`), `attestation_text` (exact
  text shown to the user), `ip_address`, `created_at`, plus lane-specific fields: `release_name`
  (Lane A's second, stronger attestation after a fingerprint match), `license_id` (FK → `licenses`,
  Lane B), `pd_cc_source` / `pd_cc_license` / `attribution_string` (Lane C).
- **`licenses`** — Lane B references. `id`, `tenant_id`, `reference`, `covers_recording` (bool),
  `covers_lyrics` (bool — tracked separately from `covers_recording` per spec §3's lyric-rights rule),
  `expiry`.
- **`tracks`** — `id`, `tenant_id`, `title`, `artist`, `duration_seconds`, `fingerprint` (Chromaprint
  string), `rights_declaration_id` (FK), `status` (`pending_review` / `passed` / `rejected`),
  `storage_key` (MinIO object key — not in the original spec's field list, added because M1's upload
  path needs to know where the file landed).
  - `key`/`tempo` (musical key, tempo) are in the spec's full data-model list but are M5 (structure
    analysis) outputs — deliberately NOT added as always-null columns now. They land via an M5
    migration when something actually populates them.
- **`fingerprint_matches`** — `id`, `tenant_id`, `track_id` (FK), `acoustid_response` (raw JSON),
  `matched_release`, `resolution` (`no_match` / `held` / `confirmed` / `mismatch`), `reviewer_id`
  (nullable — set when a human resolves a hold).

**Deliberately excluded from M1** (real per spec §7, but belong to later milestones): `users`/`tenants`
as real tables (no FK constraint on `tenant_id`/`user_id` yet — plain UUID columns, since RLS policies
compare against a column value, not a table reference, so no real table is needed to make RLS work now).
`jobs`, `stems`, `lyric_versions`, `word_timings`, `pitch_contours` — all downstream pipeline-stage
tables (M3+). `takedowns` — M7.

**Deferred, not forgotten:** the spec's abuse-prevention asks (rate-limiting users who repeatedly trip
the Lane-A hold; flagging the same fingerprint appearing under multiple "I own this" claims as a signal)
are both queries the schema already supports (`fingerprint_matches` history) — no new table needed — but
the actual rate-limit/alerting logic is not built in M1. Not in M1's "done when" criteria.

## Gate flow

Synchronous, inline in the API request — matches the spec's architecture diagram (the gate sits between
the API and object storage, not inside the async worker pool). A short clip's fingerprint plus an
AcoustID call (mocked for now) are fast enough that a client wait is simpler than a job/poll flow for M1.

**`POST /tracks/upload`** (multipart: file + `lane` + lane-specific attestation fields;
`X-Dev-User-Id` / `X-Dev-Tenant-Id` headers)
1. Save the file to MinIO under a UUID-derived key. No hardening yet (M2).
2. Compute the Chromaprint fingerprint (ffmpeg's built-in muxer — see below).
3. Look up the fingerprint via the `AcoustIDClient` interface.
4. Resolve by lane:
   - **No match** → `tracks.status = passed`.
   - **Match, Lane A** → hold (`pending_review`); response asks the client to confirm via
     `confirm-attestation` with the stronger, release-naming attestation.
   - **Match, Lane B** → cross-check against `licenses.covers_recording`; mismatch → hold; match → pass.
   - **Match, Lane C** → always hold for manual PD/CC verification. Never auto-passes on a match — the
     "1810 symphony vs. 2019 recording" distinction genuinely isn't automatable from a fingerprint alone.
5. Write `rights_declarations` + `fingerprint_matches` regardless of outcome; return the track's status.

**`POST /tracks/{id}/confirm-attestation`** — Lane A's second attestation after a hold. Records the
reviewer as the submitting user themselves; resolves the hold to `confirmed`, or leaves it open if it
still doesn't reconcile.

**`GET /review-queue`** / **`POST /review-queue/{id}/resolve`** — human hold-and-review. Any
authenticated dev user can act as reviewer in M1 (no separate admin role yet — that's a future
refinement, not blocking M1).

## Fingerprinting and AcoustID

**Chromaprint via ffmpeg, not a separate `fpcalc` binary.** The installed ffmpeg build has
`--enable-chromaprint` compiled in, so `ffmpeg -i <file> -f chromaprint -` produces a fingerprint
directly — invoked as an argument array with the `file`-only protocol whitelist, same discipline as
every other ffmpeg call per `CLAUDE.md`. One fewer external binary to install and track CVEs on
(`fpcalc` is not installed on this machine and won't be needed).

**`AcoustIDClient` interface** (`lookup(fingerprint, duration) -> AcoustIDResult`):
- `HTTPAcoustIDClient` — real implementation, calls `api.acoustid.org`, reads the API key from an env
  var that is currently unset.
- `FixtureAcoustIDClient` — test double driven by canned JSON fixtures (known match, known non-match,
  malformed/timeout response). This is what M1's test suite runs against; nothing in M1 depends on a
  real key existing yet.

**Error handling:** an AcoustID timeout or 5xx is treated as "no match" for the fingerprint-lookup
result, but the track is flagged for manual review rather than silently passing — a flaky external
dependency must never silently grant a PASS on rights-sensitive content. Audio ffmpeg can't fingerprint
at all (corrupt/malformed) is rejected outright at upload — this overlaps with M2's hardening, but an
unfingerprintable file can't pass the gate either way, so a minimal check belongs here too.

## Testing strategy (test-first, per the working agreement)

1. Models + Alembic migration + the RLS-policy test (every table, every query, tenant-filtered) —
   written first, run against the already-running Postgres container.
2. Gate resolution logic (lane × match-result → outcome) as pure functions, tested against
   `FixtureAcoustIDClient` for every branch above — no HTTP, no ffmpeg, fast unit tests.
3. The ffmpeg-based fingerprinting step, tested against 2-3 tiny synthetic WAV fixtures checked into
   `services/api/tests/fixtures/` (generated with `ffmpeg -f lavfi -i sine=...`, not copyrighted audio).
4. `/tracks/upload` wired end-to-end: `FixtureAcoustIDClient` + real ffmpeg fingerprinting + a real
   MinIO write. This is what proves M1's actual "done when" — a known-commercial fixture gets held, an
   original one passes.
5. `confirm-attestation` and `review-queue` endpoints last.

## Out of scope for M1

Real auth, real AcoustID key, upload hardening (M2), rate-limiting/abuse-alerting logic, `key`/`tempo`
track fields, admin roles, `jobs`/`stems`/`lyric_versions`/`word_timings`/`pitch_contours`/`takedowns`
tables.
