from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.auth import SESSION_COOKIE_NAME
from app.main import app

client = TestClient(app)


def _unique_email() -> str:
    # NOTE: deviates from the task-3 brief's literal `@example.test` -- email-validator (which
    # pydantic's EmailStr delegates to, via a fixed internal wrapper with no config hook) rejects
    # the ".test" TLD as a special-use/reserved domain regardless of the check_deliverability
    # setting (see email_validator/syntax.py's SPECIAL_USE_DOMAIN_NAMES check), so every /auth/
    # signup and /auth/login call in this file 422'd before this change. "example.com" is on
    # email-validator's own explicitly-allowed list (RFC 6761: applications SHOULD NOT treat
    # "example" domains as special) and isn't deliverability-checked here (check_deliverability
    # is always False), so it is syntactically valid without being a real mailbox.
    return f"{uuid.uuid4()}@example.com"


def test_signup_creates_a_real_account_and_sets_a_session_cookie() -> None:
    email = _unique_email()
    response = client.post("/auth/signup", json={"email": email, "password": "hunter22ab"})

    assert response.status_code == 200
    body = response.json()
    assert uuid.UUID(body["tenant_id"])
    assert uuid.UUID(body["user_id"])
    assert SESSION_COOKIE_NAME in response.cookies


def test_signup_rejects_a_too_short_password() -> None:
    response = client.post("/auth/signup", json={"email": _unique_email(), "password": "short"})
    assert response.status_code == 422


def test_signup_rejects_a_malformed_email() -> None:
    response = client.post(
        "/auth/signup", json={"email": "not-an-email", "password": "hunter22ab"}
    )
    assert response.status_code == 422


def test_signup_with_a_duplicate_email_returns_409() -> None:
    email = _unique_email()
    first = client.post("/auth/signup", json={"email": email, "password": "hunter22ab"})
    assert first.status_code == 200

    second = client.post("/auth/signup", json={"email": email, "password": "different-password"})
    assert second.status_code == 409


def test_login_with_correct_credentials_succeeds() -> None:
    email = _unique_email()
    client.post("/auth/signup", json={"email": email, "password": "hunter22ab"})

    response = client.post("/auth/login", json={"email": email, "password": "hunter22ab"})

    assert response.status_code == 200
    assert SESSION_COOKIE_NAME in response.cookies


def test_login_with_wrong_password_returns_401() -> None:
    email = _unique_email()
    client.post("/auth/signup", json={"email": email, "password": "hunter22ab"})

    response = client.post("/auth/login", json={"email": email, "password": "wrong-password"})

    assert response.status_code == 401


def test_login_with_unknown_email_returns_401_with_the_same_message_as_wrong_password() -> None:
    real_email = _unique_email()
    client.post("/auth/signup", json={"email": real_email, "password": "hunter22ab"})

    wrong_password_response = client.post(
        "/auth/login", json={"email": real_email, "password": "wrong-password"}
    )
    unknown_email_response = client.post(
        "/auth/login", json={"email": _unique_email(), "password": "hunter22ab"}
    )

    assert wrong_password_response.status_code == 401
    assert unknown_email_response.status_code == 401
    assert wrong_password_response.json()["detail"] == unknown_email_response.json()["detail"]


def test_me_returns_the_signed_in_users_identity_and_email() -> None:
    email = _unique_email()
    signup_response = client.post("/auth/signup", json={"email": email, "password": "hunter22ab"})
    session_client = TestClient(app)
    session_client.cookies.set(SESSION_COOKIE_NAME, signup_response.cookies[SESSION_COOKIE_NAME])

    response = session_client.get("/auth/me")

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == email
    assert uuid.UUID(body["tenant_id"]) == uuid.UUID(signup_response.json()["tenant_id"])


def test_me_without_a_session_returns_401() -> None:
    response = TestClient(app).get("/auth/me")
    assert response.status_code == 401


def test_logout_clears_the_session_so_me_then_401s() -> None:
    email = _unique_email()
    session_client = TestClient(app)
    session_client.post("/auth/signup", json={"email": email, "password": "hunter22ab"})

    logout_response = session_client.post("/auth/logout")
    me_response = session_client.get("/auth/me")

    assert logout_response.status_code == 200
    assert me_response.status_code == 401


def test_logout_without_a_session_is_a_no_op_200() -> None:
    response = TestClient(app).post("/auth/logout")
    assert response.status_code == 200
