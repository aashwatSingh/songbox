from __future__ import annotations

import logging
import textwrap
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.acoustid.client import FixtureAcoustIDClient
from app.job_cost import estimate_cost_usd, track_job_cost
from app.main import app
from app.routes.tracks import get_acoustid_client

client = TestClient(app)


def test_estimate_cost_usd_returns_none_when_providers_empty(tmp_path: Path) -> None:
    gpu_costs_path = tmp_path / "gpu_costs.yaml"
    gpu_costs_path.write_text("providers: []\n")

    assert estimate_cost_usd(10.0, gpu_costs_path=gpu_costs_path) is None


def test_estimate_cost_usd_uses_the_most_recent_dated_entry(tmp_path: Path) -> None:
    gpu_costs_path = tmp_path / "gpu_costs.yaml"
    gpu_costs_path.write_text(
        textwrap.dedent(
            """
            providers:
              - name: fake-provider
                gpu_type: FAKE-GPU
                price_per_second_usd: 0.001
                effective_date: "2020-01-01"
              - name: fake-provider
                gpu_type: FAKE-GPU
                price_per_second_usd: 0.002
                effective_date: "2024-06-01"
            """
        )
    )

    result = estimate_cost_usd(100.0, gpu_costs_path=gpu_costs_path)

    # Must pick the MORE RECENT entry (0.002/s -> 0.2), not the first one in the file (0.001/s).
    assert result == pytest.approx(0.2)


def test_estimate_cost_usd_ignores_future_dated_entries(tmp_path: Path) -> None:
    gpu_costs_path = tmp_path / "gpu_costs.yaml"
    gpu_costs_path.write_text(
        textwrap.dedent(
            """
            providers:
              - name: fake-provider
                gpu_type: FAKE-GPU
                price_per_second_usd: 0.001
                effective_date: "2020-01-01"
              - name: fake-provider
                gpu_type: FAKE-GPU
                price_per_second_usd: 999.0
                effective_date: "2099-01-01"
            """
        )
    )

    result = estimate_cost_usd(10.0, gpu_costs_path=gpu_costs_path)

    assert result == pytest.approx(0.01)


def test_track_job_cost_logs_real_duration_and_null_cost_against_real_empty_yaml(
    caplog: pytest.LogCaptureFixture,
) -> None:
    track_id = uuid.uuid4()
    with caplog.at_level(logging.INFO, logger="songbox.job_cost"):
        with track_job_cost(track_id, "separate"):
            pass

    job_records = [r for r in caplog.records if r.name == "songbox.job_cost"]
    assert len(job_records) == 1
    record = job_records[0]
    assert record.track_id == str(track_id)  # type: ignore[attr-defined]
    assert record.job_type == "separate"  # type: ignore[attr-defined]
    assert isinstance(record.duration_seconds, float)  # type: ignore[attr-defined]
    assert record.duration_seconds >= 0  # type: ignore[attr-defined]
    # config/gpu_costs.yaml is still an empty TODO: unmeasured stub at this point in the project.
    assert record.estimated_cost_usd is None  # type: ignore[attr-defined]


def test_track_job_cost_still_logs_when_the_wrapped_block_raises(
    caplog: pytest.LogCaptureFixture,
) -> None:
    track_id = uuid.uuid4()
    with caplog.at_level(logging.INFO, logger="songbox.job_cost"):
        with pytest.raises(ValueError):
            with track_job_cost(track_id, "transcribe"):
                raise ValueError("simulated inference failure")

    job_records = [r for r in caplog.records if r.name == "songbox.job_cost"]
    assert len(job_records) == 1
    assert job_records[0].job_type == "transcribe"  # type: ignore[attr-defined]


def test_separate_endpoint_logs_a_real_gpu_job_cost_line(
    caplog: pytest.LogCaptureFixture, synthetic_wav: Path
) -> None:
    headers = {"X-Dev-Tenant-Id": str(uuid.uuid4()), "X-Dev-User-Id": str(uuid.uuid4())}
    app.dependency_overrides[get_acoustid_client] = lambda: FixtureAcoustIDClient({})
    try:
        with synthetic_wav.open("rb") as fh:
            upload_response = client.post(
                "/tracks/upload",
                headers=headers,
                data={"lane": "A", "attestation_text": "I made this recording"},
                files={"file": ("tone.wav", fh, "audio/wav")},
            )
        assert upload_response.status_code == 200
        track_id = upload_response.json()["track_id"]

        with caplog.at_level(logging.INFO, logger="songbox.job_cost"):
            separate_response = client.post(f"/tracks/{track_id}/separate", headers=headers)
        assert separate_response.status_code == 200
    finally:
        app.dependency_overrides.pop(get_acoustid_client, None)

    job_records = [r for r in caplog.records if r.name == "songbox.job_cost"]
    assert len(job_records) == 1
    assert job_records[0].track_id == track_id  # type: ignore[attr-defined]
    assert job_records[0].job_type == "separate"  # type: ignore[attr-defined]
    assert job_records[0].duration_seconds > 0  # type: ignore[attr-defined]
    assert job_records[0].estimated_cost_usd is None  # type: ignore[attr-defined]


def test_track_job_cost_degrades_to_null_cost_if_pricing_lookup_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _broken_estimate(duration_seconds: float) -> float | None:
        raise KeyError("price_per_second_usd")

    monkeypatch.setattr("app.job_cost.estimate_cost_usd", _broken_estimate)

    track_id = uuid.uuid4()
    # NOTE: intentionally logging.INFO here, not logging.WARNING -- caplog.at_level(level, logger=X)
    # raises X's own effective level to `level`, so passing WARNING would suppress the INFO
    # "gpu_job" record before it's even created, making the info_records assertion below fail
    # unconditionally regardless of what track_job_cost actually does. INFO is low enough to let
    # both the INFO and WARNING records through to caplog.
    with caplog.at_level(logging.INFO, logger="songbox.job_cost"):
        with track_job_cost(track_id, "separate"):
            pass

    info_records = [
        r for r in caplog.records if r.name == "songbox.job_cost" and r.levelno == logging.INFO
    ]
    assert len(info_records) == 1
    assert info_records[0].estimated_cost_usd is None  # type: ignore[attr-defined]
    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warning_records) == 1


def test_track_job_cost_pricing_failure_does_not_mask_the_original_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _broken_estimate(duration_seconds: float) -> float | None:
        raise KeyError("price_per_second_usd")

    monkeypatch.setattr("app.job_cost.estimate_cost_usd", _broken_estimate)

    with pytest.raises(ValueError, match="original failure"):
        with track_job_cost(uuid.uuid4(), "transcribe"):
            raise ValueError("original failure")
