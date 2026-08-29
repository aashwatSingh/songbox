# M7a: Data Lifecycle & Compliance (Retention Purge + Takedown) — Design Spec

## Context

`docs/PLAN.md` names M7 as "retention purge, takedown endpoint, rate limits, observability, load
test, [GPU backend swap]" with no policy specified for any of it. Per the user's approved
decomposition, M7 splits into three sub-milestones the same way M1/M4/M6 each split once their real
scope became clear: M7a (this milestone — retention purge + takedown, both "find and delete a
track's data" mechanics), M7b (rate limits + observability), M7c (the cloud GPU backend swap + real
no-egress sandbox validation + load test, the one requiring a real external Modal/RunPod account).

Two things were verified before this design was written, not assumed:

- **`Track` has no `created_at` column** (checked the real model in `services/api/app/models.py`) —
  retention purge needs a track's age to know if it's past the retention window. Rather than add a
  migration for a new column, `RightsDeclaration.created_at` (joined via the existing
  `Track.rights_declaration_id` foreign key) is a valid, accurate proxy: a `RightsDeclaration` row
  is created in the exact same upload request that creates its `Track` row (M1's rights-gate flow),
  so its timestamp *is* the upload time. No schema change needed for this milestone.
- **`app/db.py` already has exactly the cross-tenant session this milestone needs**: `SessionLocal`
  (bound to the unrestricted `songbox` superuser role via `DATABASE_URL`), distinct from
  `AppSessionLocal`/`db_session_for_tenant` (the RLS-scoped `songbox_app` role every other endpoint
  uses). No new session infrastructure needed — this milestone is simply the first to have a
  legitimate reason to use the session type that was already there.

## Decision 1: a shared deletion core, called by both features

One new module, `services/api/app/deletion.py`, exposing `delete_track_content(session: Session,
track: Track) -> None` — deletes, in FK-safe order, every row and object-storage blob a track owns:
`FingerprintMatch`, `Stem` rows (plus each stem's MinIO object), `Transcription`, `KaraokePackage`,
then the original upload's MinIO object (`Track.storage_key`). It does **not** delete the `Track`
row itself or its `RightsDeclaration` — callers decide that part, since retention purge and takedown
want different endings (Decision 3). Retention-purged tracks never had a `Stem`/`Transcription`/
`KaraokePackage` row in the first place (those pipeline stages only run after the rights gate
passes), so for them this function's extra queries are cheap no-ops, not dead code — reusing one
function for both cases is simpler than maintaining two purpose-built deletion paths that would
drift apart over time.

A new `storage.py` function, `delete_track_file(client: Minio, storage_key: str) -> None`, wraps
`client.remove_object(_BUCKET, storage_key)` — the one MinIO operation this project has never needed
before (`save_track_file`/`fetch_track_file` exist; delete doesn't).

## Decision 2: retention purge — a standalone script, not a new always-on service

This project has no scheduler/cron infrastructure anywhere (RQ handles per-job queuing, not
periodic tasks; there's no long-running worker daemon). Building one just for this milestone would
be new infrastructure this project doesn't otherwise need yet — inventing it now would be scope
creep beyond what M7a actually requires. Instead: `services/api/scripts/purge_expired_tracks.py`,
matching the established convention of `scripts/benchmark_pitch.py`/`benchmark_separation.py` —
runnable manually or via an external OS-level scheduled task (the same pattern several sibling
projects in this environment already use for their own periodic jobs), not a feature this codebase
runs itself.

**Scope, per the approved decision:** only tracks whose `status` is still `pending_review` or
`rejected` — i.e., uploads that never passed the rights gate — older than a retention window.
`RETENTION_WINDOW_DAYS = 30` is a **policy choice, explicitly stated as such**, not a measured or
industry-validated number (matching this project's established pattern for tunable constants, e.g.
M5's section-count heuristic, M6c's ±50-cent tolerance) — easy to change, not backed by a real
compliance review. Matched tracks are **hard-deleted**: `delete_track_content()`, then the `Track`
row itself, then its `RightsDeclaration` (in that order — `Track.rights_declaration_id` must be
gone before its target can be). No compliance reason exists to keep a record of an upload that
never established a rights basis in the first place.

The script prints a summary (track count, a truncated non-PII identifier list) to stdout — never
the attestation text, never any audio/lyric content, per `CLAUDE.md`'s "never log raw audio or
lyrics" rule, which applies here for the first time to a *deletion* path rather than a processing
one.

## Decision 3: takedown endpoint — tombstone, not hard delete

`POST /admin/tracks/{track_id}/takedown` — gated by a new `X-Admin-Key` header (Decision 4), checked
before any other work, matching every existing endpoint's "cheapest check first" gate-ordering
convention. Request body: `{"reason": str}` (required, min length 1 — the same bounded-text pattern
`RealignRequest.text` already established in M4b, to avoid an unbounded free-text field).

Unlike retention purge, takedown **keeps a tombstone**: `delete_track_content()` removes all
content-bearing data and storage, but the `Track` row survives with a new `status = "taken_down"`
value (extending the existing `status` column's known values: `pending_review` | `passed` |
`rejected` | now also `taken_down`) plus two new columns recording the compliance trail —
`takedown_reason: Text` and `takedown_at: DateTime(timezone=True)`. This is the standard real-world
shape of a takedown: the platform can show *that* something was removed, *when*, and *why*, without
retaining the content itself. Requires a migration (`0007_add_track_takedown_columns.py`) — additive
only, matching every prior migration in this project.

`Stem.storage_key`/`Track.storage_key` are not nulled out after deletion (the columns keep their old
values) — the row itself is gone for stems, and the track's own `storage_key` becomes a harmless
dangling reference to an object that no longer exists, consistent with how this project has never
needed to distinguish "row exists but object doesn't" from "both exist" anywhere else. Fine to leave
as-is; a future milestone can null it if this ever needs to be queried directly.

## Decision 4: `X-Admin-Key`, checked before the dev-tenant identity

A new dependency, `require_admin_key`, in `app/auth.py` alongside `get_identity` — checks a required
`X-Admin-Key` header against an `ADMIN_API_KEY` environment variable using `secrets.compare_digest`
(constant-time comparison, avoiding a timing side-channel on a real secret — cheap to do correctly,
worth doing correctly). This is deliberately **separate** from `get_identity`'s dev-tenant-header
scheme: takedown is the first endpoint in this codebase where the caller isn't necessarily the
track's own tenant, so gating it behind the same header scheme every other endpoint trusts
uncritically would be a real, avoidable step backward. This doesn't solve the project's tracked
real-auth gap (`docs/PLAN.md` open question 9) — it's a minimal, honest safeguard for one
specifically higher-risk operation, stated as such, not represented as "auth is now solved."

`ADMIN_API_KEY` has no default value in code — if unset, `require_admin_key` fails closed (500,
"admin API key not configured"), never silently accepting any/no key.

## What M7a builds

1. `services/api/app/deletion.py` — `delete_track_content(session, track)`.
2. `services/api/app/storage.py` addition — `delete_track_file(client, storage_key)`.
3. `services/api/scripts/purge_expired_tracks.py` — the retention purge script.
4. `services/api/alembic/versions/0007_add_track_takedown_columns.py` — adds `takedown_reason`,
   `takedown_at` to `tracks`; `Track` model updated to match.
5. `services/api/app/auth.py` addition — `require_admin_key`.
6. `POST /admin/tracks/{track_id}/takedown` on a new `services/api/app/routes/admin.py` router (kept
   separate from `tracks.py`, since every other route in that file is tenant-scoped by design, and
   mixing an intentionally-cross-tenant admin route into it would blur that file's existing
   invariant rather than extend it cleanly).

No frontend changes — this milestone is entirely backend, matching the "harden and launch" framing
(none of this is user-facing product surface).

## Testing strategy

Backend, real-pipeline-touching code — test-first, per the working agreement.

- `delete_track_content()`: real test building a track with real stems/transcription/package rows
  (mirroring the existing `_upload_pass_and_separate_track`-style helpers already used throughout
  `services/api/tests/`), confirming every row and every MinIO object is actually gone afterward —
  not just that the function returns without error.
- Retention purge script: a real-pipeline test with a `pending_review` track backdated (via a direct
  `RightsDeclaration.created_at` write, matching this codebase's established direct-DB-insert
  test-setup pattern) past the window, confirming it's deleted; a fresh one just inside the window,
  confirming it's untouched; a `passed` track backdated well past the window, confirming retention
  purge never touches passed tracks regardless of age (the core scope boundary from Decision 2's
  approved choice).
- Takedown endpoint: missing/wrong `X-Admin-Key` → 401, with a non-invocation guard proving
  `delete_track_content` is never reached; unset `ADMIN_API_KEY` env var → 500; a real takedown
  request confirming the tombstone (`status`, `takedown_reason`, `takedown_at` all correct) and that
  content rows/storage are genuinely gone, mirroring `delete_track_content()`'s own test approach
  rather than re-deriving it.
- Cross-tenant reach: a real test confirming `SessionLocal` (not `AppSessionLocal`) genuinely
  operates across tenants — e.g. a takedown request succeeding against a track belonging to a
  *different* tenant than any header on the request, which would be blocked if this accidentally
  used the RLS-scoped session instead.

## Out of scope for M7a

Rate limits and observability (M7b). The GPU backend swap and real no-egress sandbox validation
(M7c). Any frontend/UI for triggering a takedown (this is a backend, presumably-internal-tooling
endpoint for this milestone — a real admin UI, if ever built, is separate scope). Real production
scheduling for the purge script (a hosted cron, a scheduled serverless function) — deferred until
this project has real deployment infrastructure at all, which doesn't exist yet. Notifying a track's
owner that their content was taken down (a real product feature, not built here). Retention policy
for `passed` tracks or inactive accounts — explicitly out of scope per the approved decision, since
no real account-lifecycle infrastructure exists to hang that policy on yet.
