# 0002 — Authentication model: one user per tenant, DB-backed session cookies, argon2id

## Context

Every endpoint since M1 authenticated via a dev-only `X-Dev-Tenant-Id`/`X-Dev-User-Id` header stub,
trusted verbatim with no verification (`docs/PLAN.md` open question 9, raised in M4b when this stub
first became reachable from a browser — `apps/web/lib/api.ts` generated a random tenant/user UUID
pair client-side into `localStorage` and sent it as those same two headers on every request). Anyone
who could reach the API could set those headers to any tenant ID they chose. There was no identity
provider, no session, no credential check anywhere in this codebase — RLS enforcement (every table
carries `tenant_id`, every query filters on it, per `CLAUDE.md`) was real, but the tenant a request
claimed to be acting as was not.

M8's approved design spec (`docs/superpowers/specs/2026-08-31-real-authentication-design.md`) scoped
closing this gap with real email+password signup/login, deliberately bounded to what a single-tenant
personal-use karaoke tool actually needs, not a general-purpose multi-tenant identity system.

## Decision

**One user per tenant.** A signup auto-provisions its own `tenant_id` — no teams, invites, or
shared tenants, no roles. This matches how the product is actually used (one person's own tracks)
and avoids building membership/permission machinery for a use case that doesn't exist yet. If shared
tenants are ever needed, that's new scope for a future milestone, not a gap in this one.

**Argon2id (via `argon2-cffi`'s `PasswordHasher`, `services/api/app/auth.py`) for password hashing,
not a general-purpose hash.** Argon2id is a memory-hard KDF purpose-built for password storage —
resistant to GPU/ASIC brute-force in a way a fast general-purpose hash (SHA-256, etc.) is not, even
when salted. `argon2-cffi`'s defaults are used as-is rather than hand-tuned, since this project has
no measured load profile that would justify deviating from them.

**DB-backed opaque session cookies, not JWTs.** `POST /auth/signup` and `POST /auth/login` generate
a random token (`secrets.token_urlsafe(32)`, matching the existing `secrets`-module pattern already
used for the admin-key comparison in this same file), set it as an httpOnly `songbox_session` cookie,
and persist only `sha256(raw_token)` in a new `sessions` table — a database read alone can never
reproduce a valid cookie. `get_identity()` (`app/auth.py`) hashes the incoming cookie, looks up
`sessions` joined to `users`, and checks `expires_at`. This was chosen over a signed JWT specifically
for **server-side revocability without a blocklist**: logout (`POST /auth/logout`) is a single `DELETE`
of the session row, and revoking a session takes effect on the very next request, no different from
how it worked before. A JWT is validated by its signature alone, so revoking one before its natural
expiry requires either short-lived tokens with a refresh dance or maintaining a separate blocklist of
revoked-but-unexpired tokens — real infrastructure this project doesn't need, since it isn't
distributing verification across multiple independently-trusted services (the single reason JWTs earn
their complexity). The cost of the DB-backed approach is a lookup on every authenticated request; that
cost is accepted as reasonable for this project's real scale.

Session cookies are httpOnly, `Secure` in production (`is_production()` checks `SONGBOX_ENV`), and
`SameSite=Lax`, with a **fixed 30-day expiry from creation and no sliding-window renewal** — a
session that goes 30 days unused simply requires re-login, rather than adding renewal-on-activity
logic this project has no requirement for yet.

**`users` and `sessions` are deliberately excluded from Postgres row-level security — and more than
that, the restricted `songbox_app` role has no grant on them at all**, per migration
`0009_add_users_and_sessions.py`'s comment. This is not an oversight in an otherwise-RLS-everywhere
schema; it's the same documented exception category `app/db.py`'s `get_admin_db()` already uses for
cross-tenant operations. RLS scopes access to tenant *content* once a request's tenant is already
known; `users`/`sessions` are the substrate that establishes which tenant a request belongs to in the
first place, so they can't themselves be scoped by the tenant they're used to discover — a session
lookup has to run as the unrestricted `songbox` role (`SessionLocal`, not `AppSessionLocal`) or every
request would fail before identity is even established.

## Explicit non-goals

These were decided during brainstorming, not silently dropped:

- **No OAuth / social login.** Email+password is the entire credential surface for this milestone;
  adding a second identity provider (Google, GitHub, etc.) is real integration work with its own
  callback/state/token-exchange surface that this project doesn't need yet for a personal-use tool.
- **No teams, multi-user tenants, invites, or roles.** Follows directly from the one-user-per-tenant
  decision above — there is no membership model to build permissions on top of.
- **No email verification.** There is no transactional email sender anywhere in this project, and
  building one only to send a single verification link is disproportionate to the actual risk here
  (a personal karaoke tool, not a service where an unverified email enables abuse of a shared
  resource against other users).
- **No self-serve password reset**, for the same reason — no transactional email sender exists. A
  user who forgets their password has no in-product recovery path in this milestone.
- **No migration path for existing dev-stub data.** Every `tenant_id`/`user_id` produced by the old
  `X-Dev-Tenant-Id`/`X-Dev-User-Id` header stub is synthetic dev/test data with no real user behind
  it. A clean slate — old dev-stub rows simply become unreachable through the new auth path — is an
  acceptable outcome, not a data-loss risk.
- **No admin role folded into user accounts.** `X-Admin-Key` (`require_admin_key()`, `app/auth.py`)
  remains a separate, operator-only mechanism for the takedown/purge routes, untouched by this
  milestone. Merging "admin" into the new user/session model would conflate two different trust
  boundaries (an operator running the service vs. a signed-up end user) that have no reason to share
  one mechanism.

## Consequences

- Every route handler and `get_db()`'s RLS wiring is unaffected — both already consumed `get_identity()`'s
  `Identity(tenant_id, user_id)` dataclass as an opaque input, and that interface did not change. Only
  `get_identity()`'s internals changed, from trusting a header to verifying a session cookie against the
  database.
- A real, measured cost: every authenticated request now does one additional database lookup
  (`sessions` joined to `users`) that the header-trust stub never needed. This has not been
  benchmarked against real production load — no claim is made about its latency impact beyond "it
  exists."
- Logout is now a real, effective control — deleting the `sessions` row genuinely revokes access on
  the next request, which the old header stub had no equivalent of (there was nothing to revoke; a
  client could always just resend the headers).
- Two known, real gaps ship with this milestone, deliberately documented rather than silently
  fixed or hidden (see `docs/STATUS.md`'s M8 entry and `docs/DECISIONS_LOG.md` for the incident that
  found each):
  - `login()`'s unknown-email branch and its wrong-password branch are not timing-equivalent.
    `if user is None or not verify_password(...)` short-circuits on `user is None`, so an unknown
    email never pays argon2's verification cost that a known email with a wrong password does. The
    response body is identical either way (`_GENERIC_LOGIN_FAILURE`, generic `401`), but the timing
    channel itself is not closed. Flagged during Task 3's review as plan-mandated for final
    whole-branch triage; not fixed in this milestone.
  - `tests/test_auth.py::test_expired_session_returns_401` inserts a `sessions` row with a
    hardcoded token (`"expired-token-fixture"`) directly via `SessionLocal` and never deletes it,
    so a second consecutive full-suite run against the same persistent test database fails on the
    row's `token_hash` uniqueness constraint. This is a test-isolation bug, not a bug in the auth
    code itself, and was flagged during Task 3's review as a pre-existing issue (introduced in
    Task 2) for final review triage. Not fixed in this milestone.
- No email verification or password reset means a typo'd signup email or a forgotten password both
  currently have no in-product recovery path — the user is stuck creating a new account. Acceptable
  for this milestone's scope, but a real limitation a future milestone would need to address before
  this became a service with users other than its own developer.
