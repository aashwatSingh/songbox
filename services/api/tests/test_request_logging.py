from __future__ import annotations

import json
import logging
import sys
import uuid

import pytest
from fastapi.testclient import TestClient

from app.logging_config import JSONFormatter
from app.main import app
from tests.conftest import AuthedClient


def test_json_formatter_emits_one_parseable_line_with_extra_fields() -> None:
    record = logging.LogRecord(
        name="songbox.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="request",
        args=(),
        exc_info=None,
    )
    record.method = "GET"
    record.path = "/tracks"
    record.status_code = 200
    record.duration_ms = 12.3
    record.tenant_id = "abc-123"
    record.client_ip = "127.0.0.1"

    line = JSONFormatter().format(record)
    payload = json.loads(line)

    assert payload["message"] == "request"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "songbox.access"
    assert payload["method"] == "GET"
    assert payload["path"] == "/tracks"
    assert payload["status_code"] == 200
    assert payload["duration_ms"] == 12.3
    assert payload["tenant_id"] == "abc-123"
    assert payload["client_ip"] == "127.0.0.1"


def test_json_formatter_includes_no_fields_beyond_what_was_explicitly_set() -> None:
    baseline = logging.LogRecord(
        name="x", level=logging.INFO, pathname="x", lineno=1, msg="x", args=(), exc_info=None
    )
    standard_keys = set(vars(baseline).keys())

    record = logging.LogRecord(
        name="songbox.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="request",
        args=(),
        exc_info=None,
    )
    record.method = "GET"

    payload = json.loads(JSONFormatter().format(record))
    fixed_fields = {"timestamp", "level", "logger", "message"}
    extra_keys = set(payload.keys()) - fixed_fields
    assert extra_keys == {"method"}
    assert not (extra_keys & standard_keys)


def test_real_get_request_logs_method_path_status_and_duration(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = TestClient(app)
    with caplog.at_level(logging.INFO, logger="songbox.access"):
        response = client.get("/health")

    assert response.status_code == 200
    access_records = [r for r in caplog.records if r.name == "songbox.access"]
    assert len(access_records) == 1
    record = access_records[0]
    assert record.method == "GET"  # type: ignore[attr-defined]
    assert record.path == "/health"  # type: ignore[attr-defined]
    assert record.status_code == 200  # type: ignore[attr-defined]
    assert isinstance(record.duration_ms, float)  # type: ignore[attr-defined]
    assert record.duration_ms >= 0  # type: ignore[attr-defined]
    assert record.tenant_id is None  # type: ignore[attr-defined]  # no X-Dev-Tenant-Id was sent


def test_authenticated_request_logs_the_real_verified_tenant_id(
    caplog: pytest.LogCaptureFixture, authed_client: AuthedClient
) -> None:
    with caplog.at_level(logging.INFO, logger="songbox.access"):
        authed_client.client.get("/tracks")

    access_records = [r for r in caplog.records if r.name == "songbox.access"]
    assert len(access_records) == 1
    assert access_records[0].tenant_id == str(authed_client.tenant_id)  # type: ignore[attr-defined]


def test_4xx_response_still_logs_the_real_status_code(
    caplog: pytest.LogCaptureFixture,
) -> None:
    client = TestClient(app)
    with caplog.at_level(logging.INFO, logger="songbox.access"):
        response = client.get(f"/tracks/{uuid.uuid4()}/transcription")

    access_records = [r for r in caplog.records if r.name == "songbox.access"]
    assert len(access_records) == 1
    assert access_records[0].status_code == response.status_code  # type: ignore[attr-defined]
    assert response.status_code >= 400


def test_upload_request_log_never_leaks_track_content(
    caplog: pytest.LogCaptureFixture, synthetic_wav, authed_client: AuthedClient
) -> None:
    client = authed_client.client
    with caplog.at_level(logging.INFO, logger="songbox.access"):
        with synthetic_wav.open("rb") as fh:
            client.post(
                "/tracks/upload",
                data={"lane": "C", "pd_cc_source": "a very specific public domain source string"},
                files={"file": ("tone.wav", fh, "audio/wav")},
            )

    access_records = [r for r in caplog.records if r.name == "songbox.access"]
    assert len(access_records) == 1
    # Create baseline that accounts for pytest's caplog adding "message" attribute
    baseline = logging.LogRecord(
        name="x", level=logging.INFO, pathname="x", lineno=1, msg="x", args=(), exc_info=None
    )
    baseline.message = baseline.getMessage()
    logged_extra_keys = {
        k
        for k in vars(access_records[0])
        if k not in set(vars(baseline))
    }
    expected_keys = {
        "method",
        "path",
        "status_code",
        "duration_ms",
        "tenant_id",
        "client_ip",
    }
    assert logged_extra_keys == expected_keys


def test_unhandled_exception_still_logs_a_request_line(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.auth import get_identity

    def _broken_identity() -> None:
        raise ValueError("simulated crash")

    app.dependency_overrides[get_identity] = _broken_identity
    broken_client = TestClient(app, raise_server_exceptions=False)
    try:
        with caplog.at_level(logging.INFO, logger="songbox.access"):
            response = broken_client.get("/tracks")
    finally:
        app.dependency_overrides.pop(get_identity, None)

    assert response.status_code == 500
    access_records = [r for r in caplog.records if r.name == "songbox.access"]
    assert len(access_records) == 1
    assert access_records[0].status_code == 500  # type: ignore[attr-defined]


def test_json_formatter_includes_traceback_when_exc_info_present() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            name="songbox.access",
            level=logging.ERROR,
            pathname=__file__,
            lineno=1,
            msg="failed",
            args=(),
            exc_info=sys.exc_info(),
        )

    payload = json.loads(JSONFormatter().format(record))
    assert "ValueError: boom" in payload["exception"]
    assert "Traceback" in payload["exception"]
