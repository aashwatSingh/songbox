"""Guards the two bugs that made every real-length song fail to upload.

Both were invisible to the rest of the suite because every other test uses a 3-second synthetic
tone, whose chromaprint fingerprint is a few hundred characters. A ~4 minute song fingerprints to
~6.2k characters, and that difference is the entire bug.
"""

from __future__ import annotations

import base64
import secrets
import uuid

from sqlalchemy import text

from app.db import db_session_for_tenant
from tests.conftest import AuthedClient


def test_a_full_length_fingerprint_can_be_stored(authed_client: AuthedClient) -> None:
    """A realistic fingerprint must be insertable.

    `ix_tracks_fingerprint` used to be a plain btree over the whole fingerprint column. Postgres
    caps a btree entry near 1/3 of an 8KB page (2704 bytes), so inserting a real song's
    fingerprint raised ProgramLimitExceeded and /tracks/upload returned 500 for every genuine
    track -- while passing for every short test clip.
    """
    session = db_session_for_tenant(authed_client.tenant_id)
    try:
        # Must be HIGH-ENTROPY, not a repeated pattern. Postgres compresses a btree entry before
        # applying the size limit, so a repetitive string like "AQAB" * 1600 shrinks to well under
        # the ceiling and passes even against the broken index -- a green test proving nothing.
        # A real chromaprint fingerprint is dense base64 that does not compress, so mimic that.
        fingerprint = base64.b64encode(secrets.token_bytes(4800)).decode()  # ~6400 chars
        declaration_id = uuid.uuid4()
        session.execute(
            text(
                "INSERT INTO rights_declarations (id, tenant_id, user_id, lane, attestation_text,"
                " ip_address, created_at) VALUES (:id, :tenant, :user, 'A', 'test', '127.0.0.1',"
                " now())"
            ),
            {
                "id": declaration_id,
                "tenant": authed_client.tenant_id,
                "user": authed_client.user_id,
            },
        )
        session.execute(
            text(
                "INSERT INTO tracks (id, tenant_id, duration_seconds, fingerprint,"
                " rights_declaration_id, status, storage_key, bookmarked)"
                " VALUES (:id, :tenant, 231.9, :fp, :decl, 'pending_review', 'k', false)"
            ),
            {
                "id": uuid.uuid4(),
                "tenant": authed_client.tenant_id,
                "fp": fingerprint,
                "decl": declaration_id,
            },
        )
        session.commit()
    finally:
        session.close()


def test_cors_headers_survive_an_unhandled_error(authed_client: AuthedClient) -> None:
    """A 500 must still carry CORS headers, or the browser hides the real error.

    Starlette's ServerErrorMiddleware sits outside every user middleware, CORS included, so a 500
    it renders has no Access-Control-Allow-Origin. The browser then discards the response and the
    frontend only ever sees the generic TypeError "Failed to fetch" -- which is precisely what
    masked the fingerprint bug above.
    """
    client = authed_client.client

    @client.app.get("/_boom_for_test")  # type: ignore[attr-defined]
    def _boom() -> None:
        raise RuntimeError("deliberate failure")

    response = client.get("/_boom_for_test", headers={"Origin": "http://localhost:3000"})

    assert response.status_code == 500
    assert response.headers.get("access-control-allow-origin") == "http://localhost:3000"


def test_cors_allows_the_delete_the_frontend_actually_sends(authed_client: AuthedClient) -> None:
    """lib/api.ts's deleteTrack issues DELETE; allow_methods listed only GET and POST, so the
    browser preflight rejected it and the delete button failed with the same opaque error."""
    response = authed_client.client.options(
        f"/tracks/{uuid.uuid4()}",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "DELETE",
        },
    )

    assert response.status_code == 200
    allowed = response.headers.get("access-control-allow-methods", "")
    assert "DELETE" in allowed
