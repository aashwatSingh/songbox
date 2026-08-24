# M6a: Core Synced Player Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `GET /tracks/{id}/package` read endpoint (assembling the versioned, schema-validated
`karaoke.json` v1 document from M5's flat DB columns), a `GET /tracks/{id}/stems/{stem_type}` audio
proxy, and a `/tracks/{id}/play` frontend page that plays the track's instrumental with synced
word-highlight lyrics and a pitch-lane visualization.

**Architecture:** One new backend module (`karaoke_schema.py`) plus two new endpoints on the existing
`tracks.py` router. One new frontend route (`/tracks/[id]/play`) plus a small player library
(`lib/player.ts`) encapsulating Web Audio sync math independent of React rendering.

**Tech Stack:** `jsonschema` (MIT, verified from installed 4.26.0 package metadata) for schema
validation. Browser-native Web Audio API (`AudioContext`, `AudioBufferSourceNode`, `GainNode`) for
playback — no new frontend dependency.

## Global Constraints

- Pitch runs on vocals, structure on accompaniment — **already built in M5**; this milestone only
  reads what M5 wrote. Never re-derive or re-run extraction.
- The player plays **drums + bass + other only** — never vocals. `GET /tracks/{id}/stems/{stem_type}`
  must 404 for `vocals` and any value outside `("drums", "bass", "other")`.
- `karaoke.json` is a versioned schema (`CLAUDE.md`) — `KARAOKE_SCHEMA_V1` lives in
  `services/api/app/karaoke_schema.py`; a future shape change adds a new versioned schema constant,
  never mutates this one in place.
- Every table/query already carries `tenant_id` (existing `Track`/`KaraokePackage`/`Stem` models) —
  both new endpoints use the same `track.tenant_id != identity.tenant_id` 404 gate every other
  endpoint in `tracks.py` uses. No new tables in this milestone.
- No presigned MinIO URLs — stem audio is proxied through FastAPI (already-configured CORS for
  `localhost:3000`), not through new, unverified MinIO CORS configuration. See the design spec's
  Decision 4 correction.
- `CLAUDE.md`: never log raw audio or lyrics. No exception message in this milestone's new code may
  echo document/word content.
- Per the working agreement (`docs/PLAN.md`): backend is test-first (matching every prior milestone's
  practice for `tracks.py` endpoints); the frontend player page and `lib/player.ts` are UI/glue code,
  exempt from test-first, verified instead via a real live browser session — this exemption does not
  mean "untested," it means "verified differently" (per M4b's established precedent).

---

### Task 1: `GET /tracks/{track_id}/package` and `GET /tracks/{track_id}/stems/{stem_type}`

**Files:**
- Create: `services/api/app/karaoke_schema.py`
- Modify: `services/api/app/routes/tracks.py`
- Modify: `services/api/pyproject.toml` (add `jsonschema>=4.26` to `dependencies`; add
  `"jsonschema.*"` to the mypy untyped-stub overrides)
- Test: `services/api/tests/test_tracks_package_get.py`

**Interfaces:**
- Consumes: `app.models.KaraokePackage` (fields: `id, tenant_id, track_id, schema_version, words,
  pitch_model, pitch, tempo_bpm, beats_ms, sections_ms, created_at` — all from M5, unchanged),
  `app.models.Stem` (fields: `track_id, stem_type, storage_key`), `app.packaging.CREPE_HOP_MS` (= 10,
  from M5), `app.storage.get_minio_client`/`fetch_track_file` (both existing, unchanged).
- Produces: `app.karaoke_schema.KARAOKE_SCHEMA_V1: dict[str, object]` — importable by Task 2's tests
  if any are added later, and by this task's own tests. `GET /tracks/{track_id}/package` response
  shape: `{"karaoke": {schema_version, track_id, words, pitch: {model, hop_ms, frames}, tempo_bpm,
  beats_ms, sections_ms}, "stem_urls": {"drums": "/tracks/{id}/stems/drums", "bass": "...", "other":
  "..."}}`. `GET /tracks/{track_id}/stems/{stem_type}` returns raw `audio/wav` bytes.

- [ ] **Step 1: Write the failing tests**

Create `services/api/tests/test_tracks_package_get.py`:

```python
from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jsonschema
from fastapi.testclient import TestClient

from app.acoustid.client import FixtureAcoustIDClient
from app.db import db_session_for_tenant
from app.karaoke_schema import KARAOKE_SCHEMA_V1
from app.main import app
from app.models import KaraokePackage, Transcription
from app.routes.tracks import get_acoustid_client

client = TestClient(app)

HEADERS = {
    "X-Dev-Tenant-Id": str(uuid.uuid4()),
    "X-Dev-User-Id": str(uuid.uuid4()),
}


def _upload_pass_and_separate_track(synthetic_wav: Path) -> str:
    app.dependency_overrides[get_acoustid_client] = lambda: FixtureAcoustIDClient({})
    try:
        with synthetic_wav.open("rb") as fh:
            response = client.post(
                "/tracks/upload",
                headers=HEADERS,
                data={"lane": "A", "attestation_text": "I made this recording"},
                files={"file": ("tone.wav", fh, "audio/wav")},
            )
    finally:
        app.dependency_overrides.pop(get_acoustid_client, None)
    assert response.status_code == 200
    assert response.json()["status"] == "passed"
    track_id = response.json()["track_id"]

    separate_response = client.post(f"/tracks/{track_id}/separate", headers=HEADERS)
    assert separate_response.status_code == 200
    return track_id


def _insert_transcription(track_id: str, *, lyrics_display_allowed: bool = True) -> None:
    session = db_session_for_tenant(uuid.UUID(HEADERS["X-Dev-Tenant-Id"]))
    try:
        session.add(
            Transcription(
                id=uuid.uuid4(),
                tenant_id=uuid.UUID(HEADERS["X-Dev-Tenant-Id"]),
                track_id=uuid.UUID(track_id),
                whisper_model="base",
                aligner="wav2vec2",
                language="en",
                lyrics_display_allowed=lyrics_display_allowed,
                words=[
                    {"idx": 0, "text": "hello", "start_ms": 0, "end_ms": 400, "confidence": 0.9},
                    {"idx": 1, "text": "world", "start_ms": 400, "end_ms": 800, "confidence": 0.9},
                ],
                created_at=datetime.now(UTC),
            )
        )
        session.commit()
    finally:
        session.close()


def _build_package(track_id: str) -> None:
    response = client.post(f"/tracks/{track_id}/package", headers=HEADERS)
    assert response.status_code == 200


def _insert_package(track_id: str, *, pitch_model: str, created_at: datetime) -> None:
    session = db_session_for_tenant(uuid.UUID(HEADERS["X-Dev-Tenant-Id"]))
    try:
        session.add(
            KaraokePackage(
                id=uuid.uuid4(),
                tenant_id=uuid.UUID(HEADERS["X-Dev-Tenant-Id"]),
                track_id=uuid.UUID(track_id),
                schema_version=1,
                words=[
                    {"idx": 0, "text": "hello", "start_ms": 0, "end_ms": 400, "confidence": 0.9},
                ],
                pitch_model=pitch_model,
                pitch=[{"time_ms": 0, "hz": 220.0, "confidence": 0.9}],
                tempo_bpm=120.0,
                beats_ms=[0, 500],
                sections_ms=[0],
                created_at=created_at,
            )
        )
        session.commit()
    finally:
        session.close()


def test_get_package_returns_a_schema_valid_document_and_working_stem_urls(
    synthetic_wav: Path,
) -> None:
    track_id = _upload_pass_and_separate_track(synthetic_wav)
    _insert_transcription(track_id)
    _build_package(track_id)

    response = client.get(f"/tracks/{track_id}/package", headers=HEADERS)

    assert response.status_code == 200
    body = response.json()
    jsonschema.validate(instance=body["karaoke"], schema=KARAOKE_SCHEMA_V1)
    assert body["karaoke"]["track_id"] == track_id
    assert body["karaoke"]["pitch"]["model"] == "tiny"
    assert body["karaoke"]["pitch"]["hop_ms"] == 10
    assert len(body["karaoke"]["pitch"]["frames"]) > 0
    assert set(body["stem_urls"].keys()) == {"drums", "bass", "other"}

    for stem_type, path in body["stem_urls"].items():
        stem_response = client.get(path, headers=HEADERS)
        assert stem_response.status_code == 200, stem_type
        assert stem_response.headers["content-type"] == "audio/wav"
        assert len(stem_response.content) > 0


def test_get_package_rejects_unknown_track() -> None:
    response = client.get(f"/tracks/{uuid.uuid4()}/package", headers=HEADERS)
    assert response.status_code == 404


def test_get_package_returns_404_before_a_package_exists(synthetic_wav: Path) -> None:
    track_id = _upload_pass_and_separate_track(synthetic_wav)
    # Separated, but /package was never called.

    response = client.get(f"/tracks/{track_id}/package", headers=HEADERS)

    assert response.status_code == 404


def test_get_package_nulls_word_text_when_lyrics_display_is_not_allowed(
    synthetic_wav: Path,
) -> None:
    track_id = _upload_pass_and_separate_track(synthetic_wav)
    _insert_transcription(track_id, lyrics_display_allowed=False)
    _build_package(track_id)

    response = client.get(f"/tracks/{track_id}/package", headers=HEADERS)

    assert response.status_code == 200
    body = response.json()
    jsonschema.validate(instance=body["karaoke"], schema=KARAOKE_SCHEMA_V1)
    for word in body["karaoke"]["words"]:
        assert word["text"] is None


def test_get_package_returns_the_latest_package_when_multiple_exist(
    synthetic_wav: Path,
) -> None:
    track_id = _upload_pass_and_separate_track(synthetic_wav)
    now = datetime.now(UTC)
    _insert_package(track_id, pitch_model="tiny", created_at=now - timedelta(minutes=5))
    _insert_package(track_id, pitch_model="full", created_at=now)

    response = client.get(f"/tracks/{track_id}/package", headers=HEADERS)

    assert response.status_code == 200
    assert response.json()["karaoke"]["pitch"]["model"] == "full"


def test_get_stem_rejects_vocals_and_unknown_stem_types(synthetic_wav: Path) -> None:
    track_id = _upload_pass_and_separate_track(synthetic_wav)
    _insert_transcription(track_id)
    _build_package(track_id)

    for stem_type in ("vocals", "master"):
        response = client.get(f"/tracks/{track_id}/stems/{stem_type}", headers=HEADERS)
        assert response.status_code == 404, stem_type


def test_get_stem_rejects_unknown_track() -> None:
    response = client.get(f"/tracks/{uuid.uuid4()}/stems/drums", headers=HEADERS)
    assert response.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd services/api && python -m pytest tests/test_tracks_package_get.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.karaoke_schema'` (collection error, since
the test file imports it at module load time).

- [ ] **Step 3: Add the `jsonschema` dependency**

In `services/api/pyproject.toml`, add to `dependencies` (after `"librosa>=0.10",`):

```toml
    "jsonschema>=4.26",
```

Change the `[[tool.mypy.overrides]]` block's `module` line from:

```python
module = ["torchaudio.*", "faster_whisper.*", "soundfile.*", "torchcrepe.*", "librosa.*"]
```

to:

```python
module = ["torchaudio.*", "faster_whisper.*", "soundfile.*", "torchcrepe.*", "librosa.*", "jsonschema.*"]
```

Run: `cd services/api && python -m pip install -e ".[dev]"`

- [ ] **Step 4: Write `karaoke_schema.py`**

Create `services/api/app/karaoke_schema.py`:

```python
from __future__ import annotations

# JSON Schema (Draft 2020-12) for karaoke.json v1 -- the versioned document GET
# /tracks/{id}/package assembles from the flat karaoke_packages DB row (M5) and validates before
# returning. CLAUDE.md: "karaoke.json is a versioned schema. Any shape change needs a migration
# path, not a silent bump" -- bumping the app.routes.tracks.KARAOKE_SCHEMA_VERSION constant in
# lockstep with a NEW, separately-named schema dict here (never mutating this one in place) is
# that migration path.
KARAOKE_SCHEMA_V1: dict[str, object] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "karaoke.json v1",
    "type": "object",
    "required": [
        "schema_version",
        "track_id",
        "words",
        "pitch",
        "tempo_bpm",
        "beats_ms",
        "sections_ms",
    ],
    "additionalProperties": False,
    "properties": {
        "schema_version": {"const": 1},
        "track_id": {"type": "string"},
        "words": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["idx", "start_ms", "end_ms", "confidence", "text"],
                "additionalProperties": False,
                "properties": {
                    "idx": {"type": "integer", "minimum": 0},
                    "start_ms": {"type": "integer", "minimum": 0},
                    "end_ms": {"type": "integer", "minimum": 0},
                    "confidence": {"type": "number"},
                    "text": {"type": ["string", "null"]},
                },
            },
        },
        "pitch": {
            "type": "object",
            "required": ["model", "hop_ms", "frames"],
            "additionalProperties": False,
            "properties": {
                "model": {"type": "string"},
                "hop_ms": {"type": "integer", "minimum": 1},
                "frames": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["time_ms", "hz", "confidence"],
                        "additionalProperties": False,
                        "properties": {
                            "time_ms": {"type": "integer", "minimum": 0},
                            "hz": {"type": ["number", "null"]},
                            "confidence": {"type": "number"},
                        },
                    },
                },
            },
        },
        "tempo_bpm": {"type": "number", "minimum": 0},
        "beats_ms": {"type": "array", "items": {"type": "integer", "minimum": 0}},
        "sections_ms": {"type": "array", "items": {"type": "integer", "minimum": 0}},
    },
}
```

- [ ] **Step 5: Run tests to verify the module import error is gone**

Run: `cd services/api && python -m pytest tests/test_tracks_package_get.py -v`
Expected: FAIL — now collects and runs, but every test fails with 404 (routes don't exist yet) or a
`NameError`/`AttributeError` for `KaraokePackage` import in the test itself succeeding (that model
already exists from M5) while the actual endpoints 404 since they aren't registered.

- [ ] **Step 6: Implement the two endpoints in `tracks.py`**

In `services/api/app/routes/tracks.py`, add to the imports:

```python
import jsonschema
from fastapi import Response
```

(Add `Response` to the existing `from fastapi import APIRouter, Depends, File, Form, HTTPException,
Request, UploadFile` line rather than a separate import.)

Add to the existing `from app.packaging import (...)` block:

```python
from app.packaging import (
    CREPE_HOP_MS,
    AccompanimentError,
    PitchExtractionError,
    StructureExtractionError,
    build_package,
)
```

Add a new import line:

```python
from app.karaoke_schema import KARAOKE_SCHEMA_V1
```

Add near `ALLOWED_PITCH_MODELS`:

```python
# The player (M6a) only ever plays the instrumental -- vocals are never served for playback.
ALLOWED_PLAYBACK_STEM_TYPES = ("drums", "bass", "other")
```

Add at the end of the file (after `package_track`'s closing lines):

```python
class PitchFrameOut(BaseModel):
    time_ms: int
    hz: float | None
    confidence: float


class PitchInfo(BaseModel):
    model: str
    hop_ms: int
    frames: list[PitchFrameOut]


class KaraokeDocument(BaseModel):
    schema_version: int
    track_id: uuid.UUID
    words: list[WordInfo]
    pitch: PitchInfo
    tempo_bpm: float
    beats_ms: list[int]
    sections_ms: list[int]


class StemUrls(BaseModel):
    drums: str
    bass: str
    other: str


class PackageGetResponse(BaseModel):
    karaoke: KaraokeDocument
    stem_urls: StemUrls


def _assemble_karaoke_document(package: KaraokePackage) -> dict[str, object]:
    # hop_ms is packaging.py's CREPE_HOP_MS constant, not stored per-row -- if hop length ever
    # becomes configurable per package, that's a real schema/migration change (CLAUDE.md), not
    # something to smuggle in here.
    return {
        "schema_version": package.schema_version,
        "track_id": str(package.track_id),
        "words": package.words,
        "pitch": {
            "model": package.pitch_model,
            "hop_ms": CREPE_HOP_MS,
            "frames": package.pitch,
        },
        "tempo_bpm": package.tempo_bpm,
        "beats_ms": package.beats_ms,
        "sections_ms": package.sections_ms,
    }


@router.get("/tracks/{track_id}/package", response_model=PackageGetResponse)
def get_package(
    track_id: uuid.UUID,
    identity: Identity = Depends(get_identity),
    db: Session = Depends(get_db),
) -> PackageGetResponse:
    track = db.get(Track, track_id)
    if track is None or track.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="track not found")

    package = db.execute(
        select(KaraokePackage)
        .where(KaraokePackage.track_id == track.id)
        .order_by(KaraokePackage.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if package is None:
        raise HTTPException(status_code=404, detail="no karaoke package found for this track")

    document = _assemble_karaoke_document(package)
    try:
        jsonschema.validate(instance=document, schema=KARAOKE_SCHEMA_V1)
    except jsonschema.exceptions.ValidationError as exc:
        # A validation failure here means the assembled document doesn't match its own declared
        # schema -- a genuine internal bug, never a client-input problem. The generic detail
        # string is deliberate: the ValidationError's own message can echo a fragment of the
        # instance data (which may include word/lyric content), and CLAUDE.md forbids logging or
        # returning raw lyrics.
        raise HTTPException(
            status_code=500, detail="assembled karaoke document failed validation"
        ) from exc

    stem_rows = (
        db.execute(
            select(Stem).where(
                Stem.track_id == track.id, Stem.stem_type.in_(ALLOWED_PLAYBACK_STEM_TYPES)
            )
        )
        .scalars()
        .all()
    )
    stems_by_type: dict[str, Stem] = {}
    for stem in stem_rows:
        stems_by_type.setdefault(stem.stem_type, stem)
    missing = set(ALLOWED_PLAYBACK_STEM_TYPES) - set(stems_by_type)
    if missing:
        # Shouldn't happen if a KaraokePackage row exists (packaging itself required all four
        # stems) -- but not impossible if a stem was deleted out-of-band. Fail closed rather than
        # silently return a response with fewer than three stem URLs.
        raise HTTPException(status_code=500, detail="package exists but stems are missing")

    stem_urls = {
        stem_type: f"/tracks/{track_id}/stems/{stem_type}"
        for stem_type in ALLOWED_PLAYBACK_STEM_TYPES
    }

    return PackageGetResponse(
        karaoke=KaraokeDocument.model_validate(document),
        stem_urls=StemUrls(**stem_urls),
    )


@router.get("/tracks/{track_id}/stems/{stem_type}")
def get_stem(
    track_id: uuid.UUID,
    stem_type: str,
    identity: Identity = Depends(get_identity),
    db: Session = Depends(get_db),
) -> Response:
    if stem_type not in ALLOWED_PLAYBACK_STEM_TYPES:
        raise HTTPException(status_code=404, detail="stem not found")

    track = db.get(Track, track_id)
    if track is None or track.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="track not found")

    stem = db.execute(
        select(Stem)
        .where(Stem.track_id == track.id, Stem.stem_type == stem_type)
        .limit(1)
    ).scalar_one_or_none()
    if stem is None:
        raise HTTPException(status_code=404, detail="stem not found")

    minio_client = get_minio_client()
    data = fetch_track_file(minio_client, stem.storage_key)
    return Response(content=data, media_type="audio/wav")
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `cd services/api && python -m pytest tests/test_tracks_package_get.py -v`
Expected: PASS (7/7).

- [ ] **Step 8: Run ruff, mypy, and the full suite**

Run: `cd services/api && python -m ruff check . && python -m mypy app && python -m pytest -q`
Expected: all clean, no regressions (101 pre-existing + 7 new = 108 passing; the known pre-existing
`test_get_transcription_returns_the_stored_result` flake from M5's fix round may still occasionally
appear — if it does, re-run that single test in isolation to confirm it's the known flake, not a
regression, per that flake's own documented comment in `test_tracks_transcribe.py`).

- [ ] **Step 9: Commit**

```bash
git add services/api/app/karaoke_schema.py services/api/app/routes/tracks.py \
    services/api/pyproject.toml services/api/tests/test_tracks_package_get.py
git commit -m "M6a: add GET /tracks/{id}/package and GET /tracks/{id}/stems/{stem_type}"
```

---

### Task 2: Frontend player — `apps/web/lib/player.ts`, API client additions, `/tracks/[id]/play`

**Files:**
- Create: `apps/web/lib/player.ts`
- Modify: `apps/web/lib/api.ts`
- Create: `apps/web/app/tracks/[id]/play/page.tsx`
- Modify: `apps/web/app/tracks/page.tsx` (add a link to the player)
- Modify: `apps/web/app/tracks/[id]/page.tsx` (add a link to the player)

**Interfaces:**
- Consumes: `GET /tracks/{id}/package` (Task 1) → `{karaoke: {schema_version, track_id, words:
  [{idx, start_ms, end_ms, confidence, text}], pitch: {model, hop_ms, frames: [{time_ms, hz,
  confidence}]}, tempo_bpm, beats_ms, sections_ms}, stem_urls: {drums, bass, other}}`.
  `POST /tracks/{id}/package` (already exists from M5, no request body needed for the default
  `pitch_model`). `GET /tracks/{id}/stems/{stem_type}` (Task 1) → raw `audio/wav` bytes, fetched
  directly via the browser's `fetch()` (same-origin relative to `NEXT_PUBLIC_API_BASE_URL`, already
  covered by existing `CORSMiddleware`).
- Produces: `lib/player.ts` exports `StemPlayer` class, `findActiveWordIndex()`,
  `findActivePitchFrameIndex()`, `PlayerWord`/`PlayerPitchFrame`/`StemBuffers` types — self-contained,
  no dependency on `lib/api.ts`'s types (kept structurally compatible, not import-coupled, so the
  sync math has zero React/Next.js/fetch dependencies).

- [ ] **Step 1: Add types and fetch wrappers to `apps/web/lib/api.ts`**

Append to `apps/web/lib/api.ts`:

```typescript
export interface PitchFrame {
  time_ms: number;
  hz: number | null;
  confidence: number;
}

export interface KaraokeDocument {
  schema_version: number;
  track_id: string;
  words: WordInfo[];
  pitch: {
    model: string;
    hop_ms: number;
    frames: PitchFrame[];
  };
  tempo_bpm: number;
  beats_ms: number[];
  sections_ms: number[];
}

export interface StemUrls {
  drums: string;
  bass: string;
  other: string;
}

export interface PackageResponse {
  karaoke: KaraokeDocument;
  stem_urls: StemUrls;
}

export function getPackage(trackId: string): Promise<PackageResponse> {
  return apiFetch<PackageResponse>(`/tracks/${trackId}/package`);
}

export function generatePackage(trackId: string): Promise<unknown> {
  return apiFetch(`/tracks/${trackId}/package`, { method: "POST" });
}

export function stemUrl(path: string): string {
  return `${API_BASE_URL}${path}`;
}
```

`stemUrl()` resolves a relative `stem_urls` path (e.g. `/tracks/{id}/stems/drums`) against the same
`API_BASE_URL` every other client call uses — `apiFetch` does this internally already, but stem audio
is fetched directly with the browser's `fetch()` (not through `apiFetch`, since the response is
binary audio, not JSON), so this small helper avoids duplicating the base-URL string elsewhere.

- [ ] **Step 2: Write `apps/web/lib/player.ts`**

Create `apps/web/lib/player.ts`:

```typescript
export interface PlayerWord {
  idx: number;
  start_ms: number;
  end_ms: number;
  text: string | null;
}

export interface PlayerPitchFrame {
  time_ms: number;
  hz: number | null;
  confidence: number;
}

// Binary search for the last word whose start_ms is <= currentTimeMs. Returns -1 before the
// first word starts. Words are assumed sorted by start_ms (guaranteed by the alignment engine's
// output order -- M4a).
export function findActiveWordIndex(words: PlayerWord[], currentTimeMs: number): number {
  let lo = 0;
  let hi = words.length - 1;
  let result = -1;
  while (lo <= hi) {
    const mid = Math.floor((lo + hi) / 2);
    if (words[mid].start_ms <= currentTimeMs) {
      result = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  return result;
}

// Same binary search shape for pitch frames (frames are emitted at a fixed hop_ms, but the
// search doesn't assume even spacing, matching findActiveWordIndex's approach for consistency).
export function findActivePitchFrameIndex(
  frames: PlayerPitchFrame[],
  currentTimeMs: number
): number {
  let lo = 0;
  let hi = frames.length - 1;
  let result = -1;
  while (lo <= hi) {
    const mid = Math.floor((lo + hi) / 2);
    if (frames[mid].time_ms <= currentTimeMs) {
      result = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  return result;
}

export interface StemBuffers {
  drums: AudioBuffer;
  bass: AudioBuffer;
  other: AudioBuffer;
}

// Plays three stems sample-aligned via the Web Audio API, summed through independent GainNodes
// (left at gain=1 in M6a -- M6b's mixer milestone will expose these as user controls without
// needing to touch this class's playback logic).
export class StemPlayer {
  private context: AudioContext;
  private buffers: StemBuffers;
  private sources: AudioBufferSourceNode[] = [];
  private gains: GainNode[] = [];
  private startedAtContextTime = 0;
  private startedAtOffsetSeconds = 0;
  private playing = false;

  constructor(context: AudioContext, buffers: StemBuffers) {
    this.context = context;
    this.buffers = buffers;
  }

  play(offsetSeconds = 0): void {
    this.stopSources();
    const stems: (keyof StemBuffers)[] = ["drums", "bass", "other"];
    this.sources = [];
    this.gains = [];
    for (const stem of stems) {
      const source = this.context.createBufferSource();
      source.buffer = this.buffers[stem];
      const gain = this.context.createGain();
      gain.gain.value = 1;
      source.connect(gain).connect(this.context.destination);
      source.start(0, offsetSeconds);
      this.sources.push(source);
      this.gains.push(gain);
    }
    this.startedAtContextTime = this.context.currentTime;
    this.startedAtOffsetSeconds = offsetSeconds;
    this.playing = true;
  }

  pause(): void {
    this.stopSources();
    this.startedAtOffsetSeconds = this.currentTimeSeconds;
    this.playing = false;
  }

  seek(offsetSeconds: number): void {
    if (this.playing) {
      this.play(offsetSeconds);
    } else {
      this.startedAtOffsetSeconds = offsetSeconds;
    }
  }

  get currentTimeSeconds(): number {
    if (!this.playing) {
      return this.startedAtOffsetSeconds;
    }
    return this.startedAtOffsetSeconds + (this.context.currentTime - this.startedAtContextTime);
  }

  get isPlaying(): boolean {
    return this.playing;
  }

  private stopSources(): void {
    for (const source of this.sources) {
      try {
        source.stop();
      } catch {
        // Already stopped or never started -- fine to ignore.
      }
    }
    this.sources = [];
  }
}
```

Note the `pause()` fix versus a naive implementation: it captures `currentTimeSeconds` (computed
while `playing` is still `true`) into `startedAtOffsetSeconds` *before* setting `playing = false`,
so a subsequent `play()` with no explicit offset resumes from the paused position rather than
silently restarting from the beginning.

- [ ] **Step 3: Write the player page, `apps/web/app/tracks/[id]/play/page.tsx`**

```tsx
"use client";

import Link from "next/link";
import { use, useEffect, useMemo, useRef, useState } from "react";
import {
  generatePackage,
  getPackage,
  stemUrl,
  type PackageResponse,
} from "@/lib/api";
import { StemPlayer, findActiveWordIndex, findActivePitchFrameIndex } from "@/lib/player";

function BackToTracksLink() {
  return (
    <Link
      href="/tracks"
      className="mb-4 inline-block text-sm font-medium text-blue-600 hover:underline"
    >
      &larr; Back to tracks
    </Link>
  );
}

async function decodeStem(context: AudioContext, path: string): Promise<AudioBuffer> {
  const response = await fetch(stemUrl(path));
  if (!response.ok) {
    throw new Error(`could not fetch stem audio (${response.status})`);
  }
  const arrayBuffer = await response.arrayBuffer();
  return context.decodeAudioData(arrayBuffer);
}

export default function PlayerPage(props: PageProps<"/tracks/[id]/play">) {
  const { id } = use(props.params);
  const [pkg, setPkg] = useState<PackageResponse | null>(null);
  const [notReady, setNotReady] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loadingAudio, setLoadingAudio] = useState(false);
  const [isPlaying, setIsPlaying] = useState(false);
  const [currentTimeMs, setCurrentTimeMs] = useState(0);

  const playerRef = useRef<StemPlayer | null>(null);
  const audioContextRef = useRef<AudioContext | null>(null);
  const rafRef = useRef<number | null>(null);
  const durationSecondsRef = useRef(0);

  useEffect(() => {
    setNotReady(false);
    setError(null);
    getPackage(id)
      .then(setPkg)
      .catch((err: Error) => {
        if (err.message.toLowerCase().includes("no karaoke package")) {
          setNotReady(true);
        } else {
          setError(err.message);
        }
      });
  }, [id]);

  useEffect(() => {
    return () => {
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
      audioContextRef.current?.close();
    };
  }, []);

  async function handleGenerate() {
    setGenerating(true);
    setError(null);
    try {
      await generatePackage(id);
      const result = await getPackage(id);
      setPkg(result);
      setNotReady(false);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setGenerating(false);
    }
  }

  async function ensurePlayerLoaded(current: PackageResponse): Promise<StemPlayer> {
    if (playerRef.current) return playerRef.current;
    setLoadingAudio(true);
    try {
      const context = new AudioContext();
      audioContextRef.current = context;
      const [drums, bass, other] = await Promise.all([
        decodeStem(context, current.stem_urls.drums),
        decodeStem(context, current.stem_urls.bass),
        decodeStem(context, current.stem_urls.other),
      ]);
      durationSecondsRef.current = Math.max(drums.duration, bass.duration, other.duration);
      const player = new StemPlayer(context, { drums, bass, other });
      playerRef.current = player;
      return player;
    } finally {
      setLoadingAudio(false);
    }
  }

  function tick() {
    const player = playerRef.current;
    if (player && player.isPlaying) {
      setCurrentTimeMs(player.currentTimeSeconds * 1000);
      rafRef.current = requestAnimationFrame(tick);
    }
  }

  async function handlePlayPause() {
    if (!pkg) return;
    const player = await ensurePlayerLoaded(pkg);
    if (player.isPlaying) {
      player.pause();
      setIsPlaying(false);
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
    } else {
      player.play(player.currentTimeSeconds);
      setIsPlaying(true);
      rafRef.current = requestAnimationFrame(tick);
    }
  }

  function handleSeek(e: React.ChangeEvent<HTMLInputElement>) {
    const seconds = Number(e.target.value);
    playerRef.current?.seek(seconds);
    setCurrentTimeMs(seconds * 1000);
  }

  const activeWordIndex = useMemo(
    () => (pkg ? findActiveWordIndex(pkg.karaoke.words, currentTimeMs) : -1),
    [pkg, currentTimeMs]
  );
  const activeFrameIndex = useMemo(
    () => (pkg ? findActivePitchFrameIndex(pkg.karaoke.pitch.frames, currentTimeMs) : -1),
    [pkg, currentTimeMs]
  );

  if (error) {
    return (
      <main className="max-w-2xl mx-auto py-12 px-6">
        <BackToTracksLink />
        <p className="text-red-600">{error}</p>
      </main>
    );
  }

  if (notReady) {
    return (
      <main className="max-w-2xl mx-auto py-12 px-6">
        <BackToTracksLink />
        <h1 className="text-2xl font-semibold mb-4">Track {id}</h1>
        <p className="mb-4 text-zinc-600">No karaoke package exists for this track yet.</p>
        <button
          onClick={handleGenerate}
          disabled={generating}
          className="rounded bg-blue-600 px-4 py-2 text-white text-sm font-medium disabled:opacity-50"
        >
          {generating ? "Generating..." : "Generate karaoke package"}
        </button>
      </main>
    );
  }

  if (pkg === null) {
    return (
      <main className="max-w-2xl mx-auto py-12 px-6">
        <p>Loading...</p>
      </main>
    );
  }

  const lyricsWithheld = pkg.karaoke.words.every((w) => w.text === null);
  const maxPitchHz = Math.max(1, ...pkg.karaoke.pitch.frames.map((f) => f.hz ?? 0));
  const durationMs = Math.max(1, durationSecondsRef.current * 1000);
  const playheadX = (currentTimeMs / durationMs) * 400;

  return (
    <main className="max-w-2xl mx-auto py-12 px-6">
      <BackToTracksLink />
      <h1 className="text-2xl font-semibold mb-4">Track {id}</h1>

      {lyricsWithheld && (
        <p className="mb-4 rounded bg-zinc-100 p-3 text-sm text-zinc-600">
          Lyric display isn&apos;t permitted for this track &mdash; playing without lyrics.
        </p>
      )}

      <div className="rounded bg-zinc-950 p-4 mb-4">
        <div className="text-center text-lg mb-3 min-h-[1.75rem]">
          {pkg.karaoke.words.map((word, idx) => (
            <span
              key={word.idx}
              className={idx === activeWordIndex ? "text-blue-300 font-bold" : "text-zinc-500"}
            >
              {(word.text ?? "\u2022") + " "}
            </span>
          ))}
        </div>
        <svg viewBox="0 0 400 60" className="w-full h-[60px] block">
          <polyline
            points={pkg.karaoke.pitch.frames
              .map((f) => {
                const x = (f.time_ms / durationMs) * 400;
                const y = f.hz === null ? 60 : 60 - (f.hz / maxPitchHz) * 55;
                return `${x},${y}`;
              })
              .join(" ")}
            fill="none"
            stroke="#8fd6ff"
            strokeWidth={2}
          />
          {activeFrameIndex >= 0 && (
            <line
              x1={playheadX}
              y1={0}
              x2={playheadX}
              y2={60}
              stroke="#fff"
              strokeWidth={1.5}
              opacity={0.6}
            />
          )}
        </svg>
      </div>

      <div className="flex items-center gap-3">
        <button
          onClick={handlePlayPause}
          disabled={loadingAudio}
          className="rounded bg-blue-600 px-4 py-2 text-white text-sm font-medium disabled:opacity-50"
        >
          {loadingAudio ? "Loading audio..." : isPlaying ? "Pause" : "Play"}
        </button>
        <input
          type="range"
          min={0}
          max={durationSecondsRef.current || 1}
          step={0.1}
          value={currentTimeMs / 1000}
          onChange={handleSeek}
          className="flex-1"
        />
      </div>
    </main>
  );
}
```

- [ ] **Step 4: Add a link to the player from `apps/web/app/tracks/page.tsx`**

In the `<li>` block that currently renders either "Edit lyrics" or "not transcribed yet"
(`apps/web/app/tracks/page.tsx`), add a second link next to the existing one:

```tsx
              {track.has_transcription ? (
                <div className="shrink-0 flex items-center gap-3">
                  <Link
                    href={`/tracks/${track.track_id}`}
                    className="text-sm font-medium text-blue-600 hover:underline"
                  >
                    Edit lyrics
                  </Link>
                  <Link
                    href={`/tracks/${track.track_id}/play`}
                    className="text-sm font-medium text-blue-600 hover:underline"
                  >
                    Play
                  </Link>
                </div>
              ) : (
                <span className="shrink-0 text-sm text-zinc-400">not transcribed yet</span>
              )}
```

This replaces the existing single-`Link` block in that file (same conditional, same `track_id`
usage) with the two-link version above.

- [ ] **Step 5: Add a link to the player from `apps/web/app/tracks/[id]/page.tsx`**

In `BackToTracksLink`'s render sites, add a sibling "Play" link. Change every occurrence of:

```tsx
        <BackToTracksLink />
```

to:

```tsx
        <div className="mb-4 flex items-center gap-4">
          <BackToTracksLink />
          <Link href={`/tracks/${id}/play`} className="text-sm font-medium text-blue-600 hover:underline">
            Play &rarr;
          </Link>
        </div>
```

(There are 3 occurrences of `<BackToTracksLink />` in this file — the not-lyrics-allowed branch, the
non-English branch, and the main editable branch. Replace all 3. `BackToTracksLink`'s own `mb-4`
class should be removed from its definition since the wrapping `div` now owns that margin — or,
simpler: leave `BackToTracksLink` unchanged and just wrap it, accepting the slightly larger combined
margin. Prefer the simpler option — don't restructure `BackToTracksLink` itself, this task's scope is
adding a link, not refactoring existing spacing.)

- [ ] **Step 6: Start both dev servers and verify live in a real browser**

This step replaces automated tests for this task (UI/glue code, per the working agreement).

Ensure `C:\Users\aashw\Downloads\.claude\launch.json` has current entries for this checkout's actual
path (the M4b-era `songbox-api`/`songbox-web` entries point at a worktree that no longer exists —
update their `--app-dir`/`--prefix` paths to `songbox/services/api` and `songbox/apps/web`
respectively, since M6a runs directly on `master`, not a worktree).

Using the Browser pane tools:
1. `preview_start` with `{name: "songbox-api"}`, then `{name: "songbox-web"}`.
2. Navigate to `http://localhost:3000/tracks`.
3. Pick (or create, via `curl`/an existing test track) a track that has been uploaded, separated,
   and transcribed but not yet packaged. Click its "Play" link.
4. Confirm the "Generate karaoke package" button appears. Click it; confirm it disables while
   generating and the player UI appears afterward.
5. Click "Play". Confirm: audio is audible (check `read_console_messages` for no errors; visually
   confirm via a screenshot that the play button now reads "Pause"), the word-highlight visibly
   advances through the lyrics strip over a few seconds, the pitch-lane polyline renders, and the
   white playhead line moves left-to-right.
6. Drag the seek bar partway through; confirm playback jumps to that position (word highlight and
   playhead both jump correspondingly) rather than restarting from zero.
7. Click "Pause", then "Play" again; confirm playback resumes from the paused position (this is
   what Step 3's `pause()`/`currentTimeSeconds` capture is specifically for — if this fails, that
   logic is the first place to check).
8. Check `read_network_requests` for the three `GET /tracks/{id}/stems/{type}` calls — confirm all
   three return 200 with `content-type: audio/wav` (this validates Task 1's endpoint working
   end-to-end from a real browser, not just from `TestClient`).
9. Navigate back to `/tracks` and to `/tracks/{id}` (the correction editor); confirm both pages'
   new "Play" links navigate correctly to `/tracks/{id}/play`.
10. Check `read_console_messages` with `onlyErrors: true` across the whole flow — confirm empty.

If audio fetch fails with a CORS error at Step 5/8, that means Task 1's assumption (reusing
`CORSMiddleware`) didn't hold for a binary `Response` the way it does for JSON — check
`services/api/app/main.py`'s `allow_methods` includes `GET` (it should already, from M4b) and that no
extra headers are needed for a non-JSON response. This would be a genuine finding to report, not
something to silently work around.

- [ ] **Step 7: Commit**

```bash
git add apps/web/lib/api.ts apps/web/lib/player.ts apps/web/app/tracks/[id]/play/page.tsx \
    apps/web/app/tracks/page.tsx "apps/web/app/tracks/[id]/page.tsx"
git commit -m "M6a: add core synced player page and Web Audio playback library"
```

---

## Self-Review Notes

**Spec coverage:** Decision 1 (client-side stem mix, no persisted accompaniment) — covered in Task
2's `StemPlayer` (three independent sources/gains, nothing written server-side). Decision 2 (GET
endpoint gate order, latest-row, schema validation) — covered in Task 1's `get_package`. Decision 3
(nested `karaoke.json` v1 shape assembled at read time, `stem_urls` kept outside the validated
document) — covered in `_assemble_karaoke_document` and `PackageGetResponse`'s two top-level keys.
Decision 4 as corrected (stem proxy through FastAPI, not presigned URLs) — covered by `get_stem` and
Task 2's `stemUrl()`/`decodeStem()` using relative paths. Decision 5 as corrected (two states, no
non-English banner, lyrics-withheld read directly from `words[*].text`) — covered in the player
page's `notReady`/ready-with-inline-banner structure. All "What M6a builds" items (1-6 in the spec)
map onto Task 1 items 1-2 and Task 2 items 3-6.

**Placeholder scan:** No TBD/TODO in this plan's own instructions.

**Type consistency:** Backend `PackageGetResponse.karaoke.pitch.{model, hop_ms, frames}` matches
frontend `KaraokeDocument.pitch.{model, hop_ms, frames}` field-for-field (checked by hand against
Task 1's Pydantic models and Task 2's TypeScript interfaces). `StemUrls`/`stem_urls` field names
(`drums`, `bass`, `other`) match on both sides. `findActiveWordIndex(words: PlayerWord[], ...)` and
`findActivePitchFrameIndex(frames: PlayerPitchFrame[], ...)`'s call sites in the page component pass
`pkg.karaoke.words`/`pkg.karaoke.pitch.frames` directly — the `KaraokeDocument`/`WordInfo`/`PitchFrame`
API types are structurally compatible with `PlayerWord`/`PlayerPitchFrame` (same field names/types),
so no adapter is needed, matching `player.ts`'s stated design goal of staying import-decoupled from
`lib/api.ts` while remaining structurally usable with its types.
