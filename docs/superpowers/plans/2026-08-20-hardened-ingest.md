# M2 Hardened Ingest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden `POST /tracks/upload` so the malformed-file test suite `docs/PLAN.md` names as M2's
"done when" (truncated headers, wrong magic bytes, a playlist referencing a remote URL, a duration
bomb) is fully rejected — without changing the endpoint's request/response shape.

**Architecture:** A new pure-function magic-byte detector gates the upload before any subprocess is
spawned; `fingerprint.py`'s existing ffprobe/ffmpeg calls gain timeouts and a stream-count check on
top of the duration check they already have; the MinIO storage key drops the client-supplied filename
entirely. All of it lands inside the existing M1 code paths — no new endpoints, no new tables.

**Tech Stack:** Same as M1 — FastAPI, ffmpeg/ffprobe (already installed, already invoked as argument
arrays with `-protocol_whitelist file`), MinIO, pytest.

## Global Constraints

- Windows dev machine: commands via `services/api/.venv/Scripts/python.exe`.
- `ruff check .` and `mypy app` must both pass, in addition to pytest. `pyproject.toml` already has
  `ignore = ["B008"]` for FastAPI's `Depends()` pattern — no need to touch it.
- Magic bytes are checked, **never** filename extension or client-supplied `Content-Type` — both are
  attacker-controlled.
- ffmpeg/ffprobe are invoked as argument arrays only, `-protocol_whitelist file`, never `shell=True`
  — already true throughout `fingerprint.py`, keep it that way in every change.
- No new pip dependency — magic-byte detection is pure Python over `bytes`, no library needed.
- **Sandboxing in this plan is process-level only** (subprocess timeouts, resource-conscious limits)
  — container-level isolation (no network egress, seccomp, read-only root) is explicitly M3's job,
  once real GPU workers exist to sandbox. Don't build container infrastructure in this plan.
- **Presigned direct-to-storage upload is deliberately NOT built in this plan** — `POST /tracks/upload`
  keeps its existing single multipart-request shape from M1. This is a real, acknowledged deviation
  from the original spec's M2 description, decided with the user; don't reintroduce a presign/finalize
  split.
- Postgres (port 5433), Redis, and MinIO must be up (`docker compose up -d` from the repo root) before
  running any test in this plan — Tasks 3 and 4 hit real MinIO; Task 4 also hits real Postgres. ffmpeg
  must be on PATH (already true on this machine, including from Bash-tool subagents — see
  `C:\Users\aashw\bin`).
- Duration cap: **720 seconds (12 minutes)**. Stream-count cap: **2**. Subprocess timeout: **30
  seconds** on each of the ffprobe and ffmpeg calls in `fingerprint.py`.

---

### Task 1: Magic-byte validation

**Files:**
- Create: `services/api/app/validation.py`
- Test: `services/api/tests/test_validation.py`

**Interfaces:**
- Produces: `detect_audio_format(data: bytes) -> str | None` — returns one of `"wav"`, `"flac"`,
  `"mp3"`, `"m4a"`, `"ogg"`, `"aiff"`, or `None` if the bytes don't match any accepted signature
  (including if `data` is too short to contain one). Task 4 depends on this exact name and signature.

- [ ] **Step 1: Write the failing test**

```python
# services/api/tests/test_validation.py
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from app.validation import detect_audio_format


def _generate(tmp_path: Path, suffix: str, extra_args: list[str] | None = None) -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg, "ffmpeg must be on PATH to run this test"
    out_path = tmp_path / f"tone{suffix}"
    args = [ffmpeg, "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1"]
    if extra_args:
        args += extra_args
    args.append(str(out_path))
    result = subprocess.run(args, capture_output=True)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return out_path.read_bytes()


def test_detects_wav(tmp_path: Path) -> None:
    assert detect_audio_format(_generate(tmp_path, ".wav")) == "wav"


def test_detects_flac(tmp_path: Path) -> None:
    assert detect_audio_format(_generate(tmp_path, ".flac")) == "flac"


def test_detects_mp3(tmp_path: Path) -> None:
    assert detect_audio_format(_generate(tmp_path, ".mp3")) == "mp3"


def test_detects_m4a(tmp_path: Path) -> None:
    assert detect_audio_format(_generate(tmp_path, ".m4a", ["-c:a", "aac"])) == "m4a"


def test_detects_ogg(tmp_path: Path) -> None:
    assert detect_audio_format(_generate(tmp_path, ".ogg", ["-c:a", "libvorbis"])) == "ogg"


def test_detects_aiff(tmp_path: Path) -> None:
    assert detect_audio_format(_generate(tmp_path, ".aiff")) == "aiff"


def test_rejects_truncated_header() -> None:
    assert detect_audio_format(b"RIFF") is None


def test_rejects_empty_bytes() -> None:
    assert detect_audio_format(b"") is None


def test_rejects_wrong_magic_bytes() -> None:
    assert detect_audio_format(b"this is definitely not an audio file, just plain text") is None


def test_rejects_playlist_with_remote_url() -> None:
    playlist = b"#EXTM3U\n#EXTINF:-1,Remote\nhttp://evil.example.com/payload.wav\n"
    assert detect_audio_format(playlist) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/api && ./.venv/Scripts/python.exe -m pytest tests/test_validation.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.validation'`

- [ ] **Step 3: Write the implementation**

Create `services/api/app/validation.py`:

```python
from __future__ import annotations


def detect_audio_format(data: bytes) -> str | None:
    """Identify one of the six accepted audio formats by binary signature -- never by filename
    extension or client-supplied Content-Type, both of which are attacker-controlled. Returns
    the format name, or None if the bytes don't match any accepted signature (including if data
    is too short to contain one). Bytes slicing is used throughout instead of direct indexing
    for the multi-byte checks because slicing past the end of a short bytes object returns an
    empty/short result rather than raising -- only the two-byte MPEG frame-sync check needs an
    explicit length guard, since it indexes individual bytes.
    """
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data[:4] == b"fLaC":
        return "flac"
    if data[:3] == b"ID3":
        return "mp3"
    if len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0:
        return "mp3"
    if data[4:8] == b"ftyp":
        return "m4a"
    if data[:4] == b"OggS":
        return "ogg"
    if data[:4] == b"FORM" and data[8:12] in (b"AIFF", b"AIFC"):
        return "aiff"
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/api && ./.venv/Scripts/python.exe -m pytest tests/test_validation.py -v`
Expected: `10 passed`

Also run: `./.venv/Scripts/python.exe -m ruff check . && ./.venv/Scripts/python.exe -m mypy app`

- [ ] **Step 5: Commit**

```bash
git add services/api/app/validation.py services/api/tests/test_validation.py
git commit -m "M2: add magic-byte audio format detection"
```

---

### Task 2: ffprobe/ffmpeg hardening — timeouts and stream-count limit

**Files:**
- Modify: `services/api/app/fingerprint.py` (full rewrite of `fingerprint_audio`, shown below)
- Test: `services/api/tests/test_fingerprint.py` (append new tests; existing 3 tests stay as-is and
  must keep passing unchanged)

**Interfaces:**
- Consumes: nothing new.
- Produces: `fingerprint_audio(path: Path) -> Fingerprint` — same signature as M1, but now also raises
  `FingerprintError` when duration exceeds 720s, stream count exceeds 2, or either subprocess call
  doesn't return within 30s. `Fingerprint`/`FingerprintError` are unchanged from M1. Task 4 doesn't
  need any code change to benefit from this — `upload_track` already catches `FingerprintError` and
  returns 422.

- [ ] **Step 1: Write the failing tests**

The current file only imports `from __future__ import annotations`, `pathlib.Path`, `pytest`, and
`app.fingerprint`'s `FingerprintError`/`fingerprint_audio` — it does NOT import `shutil` or
`subprocess` (those live in `conftest.py`'s `synthetic_wav` fixture, not this file). The new tests
below generate their own files with non-default ffmpeg parameters, so they need both imports added
fresh. Add `import shutil` and `import subprocess` near the top of
`services/api/tests/test_fingerprint.py`, after the existing `from pathlib import Path` line and
before the existing `import pytest` line (matching the file's existing import-ordering style: stdlib
imports before third-party). Keep the existing 3 tests and their imports unchanged. Then append:

```python
def test_fingerprint_audio_rejects_duration_exceeding_the_cap(tmp_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg
    out_path = tmp_path / "too_long.wav"
    result = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=721",
            "-ar",
            "8000",
            "-ac",
            "1",
            str(out_path),
        ],
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")

    with pytest.raises(FingerprintError, match="duration"):
        fingerprint_audio(out_path)


def test_fingerprint_audio_rejects_stream_count_exceeding_the_cap(tmp_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg
    out_path = tmp_path / "multi_stream.m4a"
    result = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=550:duration=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=660:duration=1",
            "-map",
            "0:a",
            "-map",
            "1:a",
            "-map",
            "2:a",
            "-c:a",
            "aac",
            str(out_path),
        ],
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")

    with pytest.raises(FingerprintError, match="stream"):
        fingerprint_audio(out_path)


def test_fingerprint_audio_raises_on_probe_timeout(
    synthetic_wav: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd="ffprobe", timeout=30)

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(FingerprintError):
        fingerprint_audio(synthetic_wav)
```

`shutil` is already imported in this file from M1 (used by `fingerprint_audio` itself, and the module
already imports it at the top) — reuse that import, don't add a duplicate.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/api && ./.venv/Scripts/python.exe -m pytest tests/test_fingerprint.py -v`
Expected: the 3 existing tests still pass; the 3 new ones FAIL (duration/stream-count checks don't
exist yet, so no `FingerprintError` is raised for those inputs; the timeout test fails because
`fingerprint_audio` doesn't currently pass `timeout=` to `subprocess.run`, so `fake_run`'s signature
mismatch or the absence of timeout handling won't raise `FingerprintError` the way the test expects —
read the actual failure output to confirm it's failing for the expected reason before proceeding).

- [ ] **Step 3: Write the implementation**

Replace `services/api/app/fingerprint.py` in full:

```python
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

MAX_DURATION_SECONDS = 12 * 60
MAX_STREAM_COUNT = 2
SUBPROCESS_TIMEOUT_SECONDS = 30


class FingerprintError(Exception):
    """Raised when ffmpeg/ffprobe cannot produce a fingerprint for the given file, or the file
    fails a hardening check (duration cap, stream-count cap, subprocess timeout)."""


@dataclass(frozen=True)
class Fingerprint:
    value: str
    duration_seconds: float


def fingerprint_audio(path: Path) -> Fingerprint:
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg is None or ffprobe is None:
        raise FingerprintError("ffmpeg/ffprobe not found on PATH")

    try:
        probe_result = subprocess.run(
            [
                ffprobe,
                "-protocol_whitelist",
                "file",
                "-v",
                "error",
                "-show_entries",
                "format=duration,nb_streams",
                "-of",
                "default=noprint_wrappers=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise FingerprintError(f"ffprobe timed out after {exc.timeout}s") from exc

    if probe_result.returncode != 0 or not probe_result.stdout.strip():
        raise FingerprintError(f"ffprobe could not read file info: {probe_result.stderr.strip()}")

    probe_values: dict[str, str] = {}
    for line in probe_result.stdout.strip().splitlines():
        key, _, value = line.partition("=")
        probe_values[key] = value

    try:
        duration_seconds = float(probe_values["duration"])
        stream_count = int(probe_values["nb_streams"])
    except (KeyError, ValueError) as exc:
        raise FingerprintError(
            f"ffprobe returned unparseable output: {probe_result.stdout!r}"
        ) from exc

    if duration_seconds > MAX_DURATION_SECONDS:
        raise FingerprintError(
            f"duration {duration_seconds:.1f}s exceeds the {MAX_DURATION_SECONDS}s limit"
        )
    if stream_count > MAX_STREAM_COUNT:
        raise FingerprintError(f"stream count {stream_count} exceeds the {MAX_STREAM_COUNT} limit")

    try:
        fp_result = subprocess.run(
            [
                ffmpeg,
                "-protocol_whitelist",
                "file",
                "-i",
                str(path),
                "-f",
                "chromaprint",
                "-fp_format",
                "base64",
                "-",
            ],
            capture_output=True,
            timeout=SUBPROCESS_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise FingerprintError(f"ffmpeg timed out after {exc.timeout}s") from exc

    if fp_result.returncode != 0 or not fp_result.stdout.strip():
        stderr_msg = fp_result.stderr.decode(errors="replace").strip()
        raise FingerprintError(f"ffmpeg could not fingerprint {path}: {stderr_msg}")

    return Fingerprint(value=fp_result.stdout.decode().strip(), duration_seconds=duration_seconds)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/api && ./.venv/Scripts/python.exe -m pytest tests/test_fingerprint.py -v`
Expected: `6 passed` (the original 3 plus the 3 new ones)

Also run: `./.venv/Scripts/python.exe -m ruff check . && ./.venv/Scripts/python.exe -m mypy app`

- [ ] **Step 5: Commit**

```bash
git add services/api/app/fingerprint.py services/api/tests/test_fingerprint.py
git commit -m "M2: add duration/stream-count limits and subprocess timeouts to fingerprinting"
```

---

### Task 3: Storage key drops the client-supplied filename

**Files:**
- Modify: `services/api/app/storage.py`
- Test: `services/api/tests/test_storage.py`

**Interfaces:**
- Produces: `save_track_file(client: Minio, tenant_id: uuid.UUID, data: bytes) -> str` — signature
  change from M1 (drops the `filename: str` parameter). Task 4's `upload_track` call site must be
  updated to match; this is done in Task 4, not here.

- [ ] **Step 1: Update the test first**

Replace `services/api/tests/test_storage.py` in full:

```python
from __future__ import annotations

import uuid

from app.storage import get_minio_client, save_track_file


def test_save_track_file_round_trips_through_minio() -> None:
    client = get_minio_client()
    tenant_id = uuid.uuid4()
    data = b"not real audio, just test bytes"

    storage_key = save_track_file(client, tenant_id, data)

    assert storage_key.startswith(f"{tenant_id}/")
    response = client.get_object("songbox-tracks", storage_key)
    try:
        assert response.read() == data
    finally:
        response.close()
        response.release_conn()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/api && ./.venv/Scripts/python.exe -m pytest tests/test_storage.py -v`
Expected: FAIL with a `TypeError` — `save_track_file()` still requires the `filename` argument this
test no longer passes.

- [ ] **Step 3: Write the implementation**

Replace `services/api/app/storage.py` in full:

```python
from __future__ import annotations

import io
import os
import uuid

from minio import Minio

_BUCKET = "songbox-tracks"


def get_minio_client() -> Minio:
    endpoint = os.environ.get("MINIO_ENDPOINT", "localhost:9000")
    access_key = os.environ.get("MINIO_ACCESS_KEY", "songbox")
    secret_key = os.environ.get("MINIO_SECRET_KEY", "songbox-dev-only")
    return Minio(endpoint, access_key=access_key, secret_key=secret_key, secure=False)


def ensure_bucket(client: Minio) -> None:
    if not client.bucket_exists(_BUCKET):
        client.make_bucket(_BUCKET)


def save_track_file(client: Minio, tenant_id: uuid.UUID, data: bytes) -> str:
    """Storage key is bare tenant_id/uuid4 -- no client-supplied filename component at all, so
    nothing about the key is attacker-influenced (M1 originally appended the raw filename; that
    was flagged as an unnecessary risk and removed here)."""
    ensure_bucket(client)
    storage_key = f"{tenant_id}/{uuid.uuid4()}"
    client.put_object(_BUCKET, storage_key, io.BytesIO(data), length=len(data))
    return storage_key
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/api && ./.venv/Scripts/python.exe -m pytest tests/test_storage.py -v`
Expected: `1 passed`

Also run: `./.venv/Scripts/python.exe -m ruff check . && ./.venv/Scripts/python.exe -m mypy app`

- [ ] **Step 5: Commit**

```bash
git add services/api/app/storage.py services/api/tests/test_storage.py
git commit -m "M2: drop client-supplied filename from MinIO storage keys"
```

---

### Task 4: Wire the magic-byte check into `upload_track` + the malformed-file test suite

**Files:**
- Modify: `services/api/app/routes/tracks.py`
- Test: `services/api/tests/test_tracks_upload.py` (append new tests)

**Interfaces:**
- Consumes: `detect_audio_format` (Task 1), the hardened `fingerprint_audio` (Task 2, already wired
  in from M1 — no call-site change needed, just benefits automatically), `save_track_file`'s new
  2-positional-arg-plus-data signature (Task 3).

This task is where M2's actual "done when" gets proven: the four malformed-file cases, through the
real HTTP endpoint.

- [ ] **Step 1: Write the failing tests**

Append to `services/api/tests/test_tracks_upload.py` (the file already has `shutil`, `subprocess`,
`Path` imported from M1's `_make_tone` helper — reuse them, don't re-import):

```python
def test_upload_rejects_truncated_header() -> None:
    response = client.post(
        "/tracks/upload",
        headers=HEADERS,
        data={"lane": "A", "attestation_text": "I made this recording"},
        files={"file": ("tone.wav", b"RIFF", "audio/wav")},
    )
    assert response.status_code == 422


def test_upload_rejects_wrong_magic_bytes() -> None:
    response = client.post(
        "/tracks/upload",
        headers=HEADERS,
        data={"lane": "A", "attestation_text": "I made this recording"},
        files={"file": ("tone.wav", b"this is plain text, not audio at all", "audio/wav")},
    )
    assert response.status_code == 422


def test_upload_rejects_playlist_with_remote_url() -> None:
    playlist = b"#EXTM3U\n#EXTINF:-1,Remote\nhttp://evil.example.com/payload.wav\n"
    response = client.post(
        "/tracks/upload",
        headers=HEADERS,
        data={"lane": "A", "attestation_text": "I made this recording"},
        files={"file": ("playlist.wav", playlist, "audio/wav")},
    )
    assert response.status_code == 422


def test_upload_rejects_duration_bomb(tmp_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg
    out_path = tmp_path / "too_long.wav"
    result = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=721",
            "-ar",
            "8000",
            "-ac",
            "1",
            str(out_path),
        ],
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")

    with out_path.open("rb") as fh:
        response = client.post(
            "/tracks/upload",
            headers=HEADERS,
            data={"lane": "A", "attestation_text": "I made this recording"},
            files={"file": ("tone.wav", fh, "audio/wav")},
        )
    assert response.status_code == 422
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd services/api && ./.venv/Scripts/python.exe -m pytest tests/test_tracks_upload.py -v`
Expected: the pre-existing 2 tests still pass; the 4 new ones FAIL — the truncated-header,
wrong-magic-bytes, and playlist cases currently reach `fingerprint_audio` (no magic-byte gate exists
yet) and get whatever error path ffmpeg/ffprobe produce for garbage input, not necessarily a clean
422 at the right stage; the duration-bomb case currently succeeds (no duration cap existed before
Task 2 — but Task 2 is already merged by this point in the plan, so this one should actually already
pass by the time you reach this step; if it does, that's expected and fine, note it in your report
rather than treating it as a problem).

- [ ] **Step 3: Write the implementation**

In `services/api/app/routes/tracks.py`, add the import and insert the magic-byte check. Add to the
existing import block:

```python
from app.validation import detect_audio_format
```

In `upload_track`, right after `data = file.file.read()` and before the `tempfile.NamedTemporaryFile`
block, insert:

```python
    if detect_audio_format(data) is None:
        raise HTTPException(status_code=422, detail="file does not match any accepted audio format")
```

Then update the `save_track_file` call (dropping the now-removed `filename` argument — Task 3 changed
its signature):

```python
    storage_key = save_track_file(minio_client, identity.tenant_id, data)
```

(previously: `save_track_file(minio_client, identity.tenant_id, file.filename or "upload", data)`)

No other changes to `upload_track` are needed — the hardened `fingerprint_audio` from Task 2 is
already called exactly as before; its new duration/stream-count/timeout checks surface through the
existing `except FingerprintError as exc: raise HTTPException(422, ...)` block automatically.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd services/api && ./.venv/Scripts/python.exe -m pytest tests/test_tracks_upload.py -v`
Expected: `6 passed`

Run the full suite too: `cd services/api && ./.venv/Scripts/python.exe -m pytest -v`
Expected: all tests across M1 and this plan's Tasks 1-4 pass together (M1 left off at 32 tests; this
plan adds 10 in Task 1, 3 in Task 2, and 4 in Task 4 — Task 3 replaces rather than adds — so expect
32 + 10 + 3 + 4 = 49, but verify the actual count from the real run rather than assuming this exact
number is correct, since it's arithmetic done ahead of time, not observed).

Also run: `./.venv/Scripts/python.exe -m ruff check . && ./.venv/Scripts/python.exe -m mypy app`

- [ ] **Step 5: Commit**

```bash
git add services/api/app/routes/tracks.py services/api/tests/test_tracks_upload.py
git commit -m "M2: wire magic-byte validation into POST /tracks/upload"
```

---

## After Task 4

Update `docs/STATUS.md` to mark M2 done, mirroring how M0 and M1's completion were recorded:
- M2's own "done when" is proven by the four tests added in Task 4's Step 1
  (`test_upload_rejects_truncated_header`, `test_upload_rejects_wrong_magic_bytes`,
  `test_upload_rejects_playlist_with_remote_url`, `test_upload_rejects_duration_bomb`).
- State plainly, the same way M1's STATUS.md entry noted its own deferred items, that presigned
  upload and container-level sandboxing were deliberately not built in M2 — say why (see this plan's
  Global Constraints and `docs/superpowers/specs/2026-08-20-hardened-ingest-design.md`'s Context
  section), not just that they're missing.
