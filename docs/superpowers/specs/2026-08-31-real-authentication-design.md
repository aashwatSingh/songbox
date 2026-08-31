# Real Authentication — Design Spec

Closes `docs/PLAN.md` open question 9: every endpoint since M1 has authenticated via a dev-only
`X-Dev-Tenant-Id`/`X-Dev-User-Id` header stub, trusted verbatim with no verification, generated
client-side into `localStorage` by `apps/web/lib/api.ts`. This spec replaces that stub with real
signup/login/session infrastructure.

## Scope

Milestone M8. One user per tenant (a signup auto-provisions its own `tenant_id` — no teams,
invites, or roles). Email + password credentials, verified server-side with argon2id. Sessions are
httpOnly, DB-backed opaque cookies — not JWTs, not `localStorage` tokens. No email verification,
no password reset, no data migration for existing dev-stub records, no admin role folded into user
accounts (`X-Admin-Key` stays a separate operator-only mechanism, untouched).

These non-goals were decided during brainstorming, not silently dropped — see "Explicit non-goals"
below and `docs/adr/0002-authentication-model.md` for the reasoning behind each.

## Architecture

```
Browser                    FastAPI                         Postgres
  |  POST /auth/signup        |                                |
  |  {email, password} ----->|  argon2 hash password          |
  |                           |  INSERT users (new tenant_id)  |
  |                           |  INSERT sessions (token hash) -|--> users, sessions
  |  <-- Set-Cookie: session--|  (opaque token, httpOnly)      |    (NOT RLS-scoped -- this
  |                           |                                |     is the identity substrate
  |  GET /tracks              |                                |     RLS relies on, not
  |  Cookie: session -------->|  get_identity(): hash cookie,  |     tenant data)
  |                           |    look up sessions->users      |
  |                           |  get_db(): RLS session scoped  |
  |                           |    to that tenant_id ----------|--> tracks, stems, etc.
  |  <---------- 200 tracks --|  (RLS enforcement unchanged)   |    (still RLS-scoped)
```

`get_identity()` (`app/auth.py`) already returns an `Identity(tenant_id, user_id)` dataclass that
every route and `get_db()`'s RLS wiring consumes as an opaque input — that interface does not
change. Only `get_identity()`'s internals change (header-trust to session-cookie verification), so
none of the existing track/admin route handlers need to change.

`users` and `sessions` are deliberately excluded from Postgres row-level security. They are the
identity substrate RLS depends on (how a request's tenant is discovered in the first place), not
tenant content — the same category of documented exception `get_admin_db()` already uses for
cross-tenant operations. A session lookup uses the unrestricted `songbox` role directly via a
short-lived session opened and closed inline inside `get_identity()`.

## Data model

New migration `0009_add_users_and_sessions.py`:

```sql
CREATE EXTENSION IF NOT EXISTS citext;

CREATE TABLE users (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id     uuid UNIQUE NOT NULL DEFAULT gen_random_uuid(),
    email         citext UNIQUE NOT NULL,
    password_hash text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE sessions (
    id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id     uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    tenant_id   uuid NOT NULL,
    token_hash  text UNIQUE NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    expires_at  timestamptz NOT NULL
);
```

`tenant_id` on `sessions` is denormalized (avoids a join to `users` on every request).
`token_hash` is `sha256(raw cookie token)` — only the hash is stored, so a database read alone
can't produce a valid session cookie. Corresponding `SQLAlchemy` models added to `app/models.py`
as `User` and `UserSession` (not `Session` — collides with SQLAlchemy's own `Session` class already
imported throughout this codebase).

## Endpoints (`app/routes/auth.py`, new)

- **`POST /auth/signup`** — `{email, password}`. Validates email format (`pydantic.EmailStr`, new
  dependency `email-validator`) and `len(password) >= 8`. Hashes the password with `argon2-cffi`
  (new dependency), inserts `users` + `sessions` rows, sets the session cookie. Duplicate email:
  `409`. Rate-limited `5/hour` per IP via the existing `limiter` (M7b) — prevents automated account
  spam.
- **`POST /auth/login`** — `{email, password}`. Looks up by email, `argon2.verify()`s the password,
  creates a new session row, sets the cookie. Wrong password or unknown email both return the same
  generic `401 "invalid email or password"` — never reveal which one was wrong. Rate-limited
  `10/minute` per IP — brute-force resistance without punishing a legitimate typo-retry.
- **`POST /auth/logout`** — deletes the `sessions` row for the current cookie, clears it.
- **`GET /auth/me`** — returns `{tenant_id, user_id, email}` for the current session, or `401`.
  The frontend calls this on load to determine signed-in state. Depends on `get_identity()` like
  every other route, then does one small additional lookup of `email` by `user_id` — `Identity`
  itself stays exactly `{tenant_id, user_id}` and does not grow an `email` field, since every other
  route consumes `Identity` and has no use for it; only this one rarely-called endpoint needs it.

`get_identity()` rewritten: reads the `songbox_session` cookie (`fastapi.Cookie`), SHA-256s it,
looks up `sessions` joined to `users`, checks `expires_at > now()`, returns `Identity`. Missing,
invalid, or expired cookie raises the same `401` shape the dev-header stub already raised, so no
caller-facing error contract changes.

Session cookie: httpOnly, `Secure` in production (allowed over plain HTTP in dev), `SameSite=Lax`
(sufficient here — `localhost:3000` and `localhost:8000` are same-site, different-origin, and Lax
cookies are sent on same-site cross-origin fetches, not just top-level navigations), fixed 30-day
expiry from creation. No sliding-window renewal — re-login after expiry. `secrets.token_urlsafe(32)`
generates the raw token, matching the existing `secrets`-module usage pattern in `app/auth.py`'s
admin-key comparison.

`require_admin_key()` and `X-Admin-Key` are unchanged.

## CORS

`allow_credentials=True` added to the existing `CORSMiddleware` block in `app/main.py`. Currently
`allow_origins=["http://localhost:3000"]` — already not `"*"`, so this is a compatible addition,
not a loosening of the existing policy.

## Frontend (`apps/web`)

- `apps/web/lib/api.ts`: delete `getDevIdentity()` and `getDevIdentityHeaders()`. `apiFetch()` adds
  `credentials: "include"` instead of manually attaching dev headers — the session cookie now
  travels automatically on every request. New `signup()`, `login()`, `logout()`, `me()` wrappers
  around the new endpoints.
- New `apps/web/app/login/page.tsx` and `apps/web/app/signup/page.tsx` — plain forms (signup
  includes a client-side password-confirmation field for UX; not a security control), POST to the
  new endpoints, redirect to `/tracks` on success.
- New `AuthContext`/`useAuth()` React context (calls `GET /auth/me` on mount), wrapping the app in
  `apps/web/app/layout.tsx`. Pages under `/tracks` redirect to `/login` when unauthenticated. A
  logout control is added to the app's header/nav.

## Error handling

- Wrong password / unknown email at login: generic `401`, identical message for both cases.
- Duplicate email at signup: `409`.
- Missing/invalid/expired session cookie: `401`, same shape as today's dev-header-stub error.
- Malformed email or too-short password: `422` (FastAPI/pydantic's default validation behavior — no
  custom handling needed).
- Argon2 hashing/verification failures are never caught by a broad `except Exception` — a real
  hashing error must surface as a `500`, not be silently folded into a fake auth failure.

## Testing

18 existing test files currently set `X-Dev-Tenant-Id`/`X-Dev-User-Id` headers directly against a
`TestClient`. A new `conftest.py` fixture (`authed_client(email=..., password=...)`) signs up and
logs in a real user, returning a `TestClient` whose cookie jar already carries the session; existing
tests swap their manual header dict for this fixture. This is real, non-trivial migration work
across the suite, sized as its own task in the implementation plan, not hand-waved.

New tests specific to this milestone: signup/login/logout/me happy paths; wrong password; duplicate
email signup; expired session cookie; tampered/garbage session cookie; and an end-to-end RLS check
that two independently signed-up users cannot see each other's tracks through the real auth path
(not just through the old dev-header mechanism the existing RLS tests already cover).

## Docs

- `docs/adr/0002-authentication-model.md` (new): the one-user-per-tenant decision, DB-backed opaque
  session cookies over JWT, argon2id, and each explicit non-goal below, with reasoning tied back to
  the brainstorming discussion.
- `docs/PLAN.md` open question 9: marked resolved, pointing at the ADR.
- `docs/STATUS.md`: new M8 milestone entry once implemented.
- `docs/DECISIONS_LOG.md`: entries for the session-cookie-vs-JWT choice and the
  users/sessions-outside-RLS decision specifically — the two choices most likely to be
  second-guessed later.

## Explicit non-goals

Recorded here so they read as decided, not overlooked:

- No OAuth / social login.
- No teams, multi-user tenants, invites, or roles.
- No email verification.
- No self-serve password reset (no transactional email sender exists in this project).
- No migration path for existing dev-stub data — it's synthetic dev/test data with no real users
  behind it; a clean slate is acceptable.
- No admin role folded into user accounts — `X-Admin-Key` remains a separate, operator-only
  mechanism for the takedown/purge routes.
