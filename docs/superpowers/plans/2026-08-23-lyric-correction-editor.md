# M4b: Lyric Correction Editor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a browser-usable lyric correction editor — a track list page and a per-track editor that
lets a user fix word text and re-run forced alignment on the correction.

**Architecture:** Two new FastAPI endpoints (`GET /tracks`, `POST /tracks/{id}/realign`) reuse M4a's
existing `align_words()` and gating conventions exactly. The frontend (`apps/web`, untouched since
`create-next-app` until now) gets a small API client, a dev-only client-side identity, and two Client
Component pages — no Server Component data-fetching, since identity lives in `localStorage`.

**Tech Stack:** FastAPI + SQLAlchemy (backend, matching every prior milestone's conventions exactly).
Next.js 16 App Router + React 19 + TypeScript + Tailwind v4 (frontend — already configured in
`apps/web`, this is the first code that uses any of it).

## Global Constraints

- **Dev-only identity, not real auth** (design spec Decision 1): the frontend generates/reads a
  `tenant_id`/`user_id` pair in `localStorage` and sends the existing `X-Dev-Tenant-Id`/
  `X-Dev-User-Id` headers. This must never be described or built as real authentication.
- **`Track.title`/`Track.artist` are never populated** by anything in this pipeline — the track list
  shows IDs and status, not song names. Don't invent placeholder titles.
- **Text-only correction** (Decision 3) — no manual timing-adjustment UI. Correcting text and
  re-running `align_words()` is the entire re-alignment mechanism.
- **Corrections are new, immutable `Transcription` rows** (Decision 4), never an update to an existing
  row. A corrected row's `whisper_model` is the literal sentinel `"user-corrected"` (Whisper was never
  re-run) and `aligner` is `"wav2vec2"`.
- **Non-English tracks are read-only in the editor** (Decision 5) — `align_words()` only covers
  English. Enforced server-side in `/realign` (409), not only hidden in the UI.
- **Lyrics-withheld tracks are read-only too** (Decision 6, CLAUDE.md) — a `Transcription` row with
  `lyrics_display_allowed == false` cannot be corrected. Enforced server-side (409).
- **`CLAUDE.md`: never log raw lyrics** — `/realign`'s `AlignmentError` mapping must not interpolate
  the exception's message into the HTTP response, exactly matching `/transcribe`'s existing pattern.
- **UI and glue code are exempt from test-first** (the project's working agreement, `docs/PLAN.md`) —
  the two frontend tasks in this plan have no automated test steps; verification is `npm run build`
  (type-checks against the real API-response shapes) plus a live browser check, not unit tests.
- **No fabricated accuracy, latency, or cost figure** (`CLAUDE.md`) — not directly applicable to new
  code in this plan, but do not add UI copy that implies the editor improves on M4a's measured 68.2ms
  accuracy; it doesn't change alignment quality, only lets a human fix wrong words.

---

### Task 1: `GET /tracks` + CORS middleware

**Files:**
- Modify: `services/api/app/main.py` (add CORS middleware)
- Modify: `services/api/app/routes/tracks.py` (add `TrackSummary` model + `list_tracks` route)
- Test: `services/api/tests/test_tracks_list.py` (new)

**Interfaces:**
- Produces: `GET /tracks` → `list[TrackSummary]`, where `TrackSummary` is `{track_id: UUID, status:
  str, duration_seconds: float | None, has_transcription: bool}`. Task 3 (frontend) consumes this
  shape directly.

CORS is bundled into this task because it's needed before any frontend task can successfully call the
API from a browser, and `GET /tracks` is the first call the frontend makes (loading the list page).

- [ ] **Step 1: Write the failing tests**

Create `services/api/tests/test_tracks_list.py`:

```python
from __future__ import annotations

import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from app.acoustid.client import FixtureAcoustIDClient
from app.main import app
from app.routes.tracks import get_acoustid_client

client = TestClient(app)

HEADERS = {
    "X-Dev-Tenant-Id": str(uuid.uuid4()),
    "X-Dev-User-Id": str(uuid.uuid4()),
}
OTHER_TENANT_HEADERS = {
    "X-Dev-Tenant-Id": str(uuid.uuid4()),
    "X-Dev-User-Id": str(uuid.uuid4()),
}


def _upload_and_pass_track(synthetic_wav: Path) -> str:
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
    return response.json()["track_id"]


def test_list_tracks_returns_only_the_calling_tenants_tracks(synthetic_wav: Path) -> None:
    track_id = _upload_and_pass_track(synthetic_wav)

    response = client.get("/tracks", headers=HEADERS)
    other_response = client.get("/tracks", headers=OTHER_TENANT_HEADERS)

    assert response.status_code == 200
    track_ids = {t["track_id"] for t in response.json()}
    assert track_id in track_ids
    assert other_response.json() == []


def test_list_tracks_reports_has_transcription_accurately(synthetic_wav: Path) -> None:
    track_id = _upload_and_pass_track(synthetic_wav)
    separate_response = client.post(f"/tracks/{track_id}/separate", headers=HEADERS)
    assert separate_response.status_code == 200

    before = client.get("/tracks", headers=HEADERS)
    before_entry = next(t for t in before.json() if t["track_id"] == track_id)
    assert before_entry["has_transcription"] is False

    transcribe_response = client.post(f"/tracks/{track_id}/transcribe", headers=HEADERS)
    assert transcribe_response.status_code == 200

    after = client.get("/tracks", headers=HEADERS)
    after_entry = next(t for t in after.json() if t["track_id"] == track_id)
    assert after_entry["has_transcription"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd services/api && pytest tests/test_tracks_list.py -v`
Expected: FAIL with 404 (route doesn't exist yet) on both tests.

- [ ] **Step 3: Implement `GET /tracks`**

In `services/api/app/routes/tracks.py`, add near the top of the file (after the existing
`ALLOWED_WHISPER_MODEL_SIZES`/`TRANSCRIPTION_TIMEOUT_SECONDS` constants — no new imports needed
beyond what the file already has, since `select`, `Track`, `Transcription` are already imported):

```python
class TrackSummary(BaseModel):
    track_id: uuid.UUID
    status: str
    duration_seconds: float | None
    has_transcription: bool


@router.get("/tracks", response_model=list[TrackSummary])
def list_tracks(
    identity: Identity = Depends(get_identity),
    db: Session = Depends(get_db),
) -> list[TrackSummary]:
    tracks = db.execute(
        select(Track).where(Track.tenant_id == identity.tenant_id)
    ).scalars().all()

    transcribed_track_ids = set(
        db.execute(
            select(Transcription.track_id)
            .where(Transcription.tenant_id == identity.tenant_id)
            .distinct()
        ).scalars().all()
    )

    return [
        TrackSummary(
            track_id=track.id,
            status=track.status,
            duration_seconds=(
                float(track.duration_seconds) if track.duration_seconds is not None else None
            ),
            has_transcription=track.id in transcribed_track_ids,
        )
        for track in tracks
    ]
```

(Place this near the top of the file, before `router = APIRouter()`'s other route functions — exact
position doesn't matter since routes don't depend on declaration order, but grouping it near the top
keeps "list" logically before "act on one track by ID.")

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd services/api && pytest tests/test_tracks_list.py -v`
Expected: PASS (2/2).

- [ ] **Step 5: Add CORS middleware**

In `services/api/app/main.py`:

```python
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.routes.review_queue import router as review_queue_router
from app.routes.tracks import router as tracks_router

app = FastAPI(title="Songbox API")

# Dev-only permissive CORS so the Next.js dev server (localhost:3000) can call this API
# (localhost:8000) cross-origin. Not a production CORS policy -- tighten before any real deploy.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_methods=["GET", "POST"],
    allow_headers=["X-Dev-Tenant-Id", "X-Dev-User-Id", "Content-Type"],
)

app.include_router(tracks_router)
app.include_router(review_queue_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
```

- [ ] **Step 6: Run ruff, mypy, and the full suite**

Run: `cd services/api && ruff check . && mypy app && pytest -q`
Expected: all clean, no regressions.

- [ ] **Step 7: Commit**

```bash
git add services/api/app/main.py services/api/app/routes/tracks.py services/api/tests/test_tracks_list.py
git commit -m "M4b: add GET /tracks and dev-only CORS middleware"
```

---

### Task 2: `POST /tracks/{track_id}/realign`

**Files:**
- Modify: `services/api/app/routes/tracks.py`
- Test: `services/api/tests/test_tracks_realign.py` (new)

**Interfaces:**
- Consumes: `app.transcription.align_words(path: Path, text: str) -> list[Word]` (M4a, not yet
  imported into `tracks.py` — add it to the existing `from app.transcription import (...)` block).
- Produces: `POST /tracks/{track_id}/realign` — request body `{"text": str}`, response is the same
  `TranscribeResponse` shape `/transcribe` already returns. Task 4 (frontend editor) calls this
  directly.

- [ ] **Step 1: Write the failing tests**

Create `services/api/tests/test_tracks_realign.py`:

```python
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.acoustid.client import FixtureAcoustIDClient
from app.db import db_session_for_tenant
from app.main import app
from app.models import Transcription
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


def _insert_transcription(
    track_id: str,
    *,
    language: str = "en",
    lyrics_display_allowed: bool = True,
) -> None:
    session = db_session_for_tenant(uuid.UUID(HEADERS["X-Dev-Tenant-Id"]))
    try:
        session.add(
            Transcription(
                id=uuid.uuid4(),
                tenant_id=uuid.UUID(HEADERS["X-Dev-Tenant-Id"]),
                track_id=uuid.UUID(track_id),
                whisper_model="base",
                aligner="wav2vec2",
                language=language,
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


def test_realign_stores_a_new_transcription_with_corrected_text(synthetic_wav: Path) -> None:
    track_id = _upload_pass_and_separate_track(synthetic_wav)
    _insert_transcription(track_id)

    response = client.post(
        f"/tracks/{track_id}/realign", headers=HEADERS, json={"text": "hello world"}
    )

    assert response.status_code == 200
    body = response.json()
    assert [w["text"] for w in body["words"]] == ["hello", "world"]

    session = db_session_for_tenant(uuid.UUID(HEADERS["X-Dev-Tenant-Id"]))
    try:
        rows = session.execute(
            text(
                "SELECT whisper_model, aligner FROM transcriptions "
                "WHERE track_id = :track_id ORDER BY created_at"
            ),
            {"track_id": track_id},
        ).all()
    finally:
        session.close()
    assert len(rows) == 2
    assert rows[1].whisper_model == "user-corrected"
    assert rows[1].aligner == "wav2vec2"


def test_realign_rejects_track_with_no_transcription(
    monkeypatch: pytest.MonkeyPatch, synthetic_wav: Path
) -> None:
    def _fail_if_called(*args: object, **kwargs: object) -> list[object]:
        raise AssertionError("align_words must not be called with no transcription to correct")

    monkeypatch.setattr("app.routes.tracks.align_words", _fail_if_called)
    track_id = _upload_pass_and_separate_track(synthetic_wav)

    response = client.post(
        f"/tracks/{track_id}/realign", headers=HEADERS, json={"text": "hello world"}
    )

    assert response.status_code == 409


def test_realign_rejects_when_lyrics_display_not_allowed(
    monkeypatch: pytest.MonkeyPatch, synthetic_wav: Path
) -> None:
    def _fail_if_called(*args: object, **kwargs: object) -> list[object]:
        raise AssertionError("align_words must not be called when lyrics display isn't allowed")

    monkeypatch.setattr("app.routes.tracks.align_words", _fail_if_called)
    track_id = _upload_pass_and_separate_track(synthetic_wav)
    _insert_transcription(track_id, lyrics_display_allowed=False)

    response = client.post(
        f"/tracks/{track_id}/realign", headers=HEADERS, json={"text": "hello world"}
    )

    assert response.status_code == 409


def test_realign_rejects_non_english_tracks(
    monkeypatch: pytest.MonkeyPatch, synthetic_wav: Path
) -> None:
    def _fail_if_called(*args: object, **kwargs: object) -> list[object]:
        raise AssertionError("align_words must not be called for a non-English track")

    monkeypatch.setattr("app.routes.tracks.align_words", _fail_if_called)
    track_id = _upload_pass_and_separate_track(synthetic_wav)
    _insert_transcription(track_id, language="es")

    response = client.post(
        f"/tracks/{track_id}/realign", headers=HEADERS, json={"text": "hola mundo"}
    )

    assert response.status_code == 409
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd services/api && pytest tests/test_tracks_realign.py -v`
Expected: FAIL with 404 (route doesn't exist yet) on all four tests.

- [ ] **Step 3: Implement the route**

In `services/api/app/routes/tracks.py`, add `align_words` to the existing transcription import:

```python
from app.transcription import (
    AlignmentError,
    DEFAULT_WHISPER_MODEL_SIZE,
    TranscriptionError,
    align_words,
    run_transcription_and_alignment,
)
```

Add at the end of the file:

```python
class RealignRequest(BaseModel):
    text: str


@router.post("/tracks/{track_id}/realign", response_model=TranscribeResponse)
def realign_track(
    track_id: uuid.UUID,
    body: RealignRequest,
    identity: Identity = Depends(get_identity),
    db: Session = Depends(get_db),
) -> TranscribeResponse:
    track = db.get(Track, track_id)
    if track is None or track.tenant_id != identity.tenant_id:
        raise HTTPException(status_code=404, detail="track not found")
    if track.status != "passed":
        raise HTTPException(
            status_code=409,
            detail=f"track has not passed the rights gate (status={track.status})",
        )

    latest = db.execute(
        select(Transcription)
        .where(Transcription.track_id == track.id)
        .order_by(Transcription.created_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if latest is None:
        raise HTTPException(
            status_code=409, detail="no transcription found -- run /transcribe first"
        )
    if not latest.lyrics_display_allowed:
        raise HTTPException(
            status_code=409, detail="lyric correction is not available for this track"
        )
    if latest.language != "en":
        raise HTTPException(
            status_code=409,
            detail="correction/re-alignment is only available for English tracks",
        )

    vocals_stem = db.execute(
        select(Stem).where(Stem.track_id == track.id, Stem.stem_type == "vocals").limit(1)
    ).scalar_one_or_none()
    if vocals_stem is None:
        raise HTTPException(
            status_code=409, detail="track has no vocals stem -- run /separate first"
        )

    minio_client = get_minio_client()
    vocal_bytes = fetch_track_file(minio_client, vocals_stem.storage_key)

    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    try:
        tmp.write(vocal_bytes)
        tmp.flush()
        tmp.close()
        try:
            words = run_inference(
                lambda: align_words(Path(tmp.name), body.text),
                timeout_seconds=TRANSCRIPTION_TIMEOUT_SECONDS,
            )
        except BackendBusyError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except BackendTimeoutError as exc:
            raise HTTPException(status_code=504, detail=str(exc)) from exc
        except AlignmentError as exc:
            # Deliberately NOT interpolating `exc` -- same reasoning as /transcribe's mapping.
            raise HTTPException(
                status_code=422, detail="could not align transcript to audio"
            ) from exc
    finally:
        Path(tmp.name).unlink(missing_ok=True)

    declaration = db.get(RightsDeclaration, track.rights_declaration_id)
    if declaration is None:
        raise RuntimeError(f"track {track.id} has no rights declaration -- data integrity error")
    license_covers_lyrics: bool | None = None
    if declaration.license_id is not None:
        license_row = db.get(License, declaration.license_id)
        license_covers_lyrics = license_row.covers_lyrics if license_row else None
    lyrics_display_allowed = resolve_lyrics_display_allowed(declaration.lane, license_covers_lyrics)

    words_json = [
        {
            "idx": w.idx,
            "text": w.text,
            "start_ms": w.start_ms,
            "end_ms": w.end_ms,
            "confidence": w.confidence,
        }
        for w in words
    ]
    transcription = Transcription(
        id=uuid.uuid4(),
        tenant_id=identity.tenant_id,
        track_id=track.id,
        whisper_model="user-corrected",
        aligner="wav2vec2",
        language="en",
        lyrics_display_allowed=lyrics_display_allowed,
        words=words_json,
        created_at=datetime.now(UTC),
    )
    db.add(transcription)
    db.flush()

    return _transcription_to_response(transcription)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd services/api && pytest tests/test_tracks_realign.py -v`
Expected: PASS (4/4).

- [ ] **Step 5: Run ruff, mypy, and the full suite**

Run: `cd services/api && ruff check . && mypy app && pytest -q`
Expected: all clean, no regressions.

- [ ] **Step 6: Commit**

```bash
git add services/api/app/routes/tracks.py services/api/tests/test_tracks_realign.py
git commit -m "M4b: add POST /tracks/{track_id}/realign"
```

---

### Task 3: Frontend API client + `/tracks` list page

**Files:**
- Create: `apps/web/lib/api.ts`
- Create: `apps/web/app/tracks/page.tsx`

**Interfaces:**
- Produces: `apps/web/lib/api.ts` exports `getDevIdentity()`, `apiFetch<T>()`,
  `listTracks(): Promise<TrackSummary[]>`, `getTranscription(trackId: string):
  Promise<TranscriptionResponse>`, `realignTrack(trackId: string, text: string):
  Promise<TranscriptionResponse>`, and the TypeScript interfaces `TrackSummary`, `WordInfo`,
  `TranscriptionResponse`. Task 4 imports `getTranscription`, `realignTrack`,
  `TranscriptionResponse`, `WordInfo` from this same file.

No automated tests for this task per the working agreement's UI/glue-code exemption — verification is
`npm run build` (TypeScript strict mode + ESLint via Next.js's build pipeline) plus a live check once
Task 1's backend is running.

- [ ] **Step 1: Write the API client**

Create `apps/web/lib/api.ts`:

```typescript
const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

const TENANT_ID_KEY = "songbox-dev-tenant-id";
const USER_ID_KEY = "songbox-dev-user-id";

// Dev-only identity: no real authentication exists in this project yet (see docs/PLAN.md's open
// questions and docs/superpowers/specs/2026-08-23-lyric-correction-editor-design.md Decision 1).
// This generates a random tenant/user pair on first load and reuses it from localStorage after
// that, sent as the same X-Dev-Tenant-Id/X-Dev-User-Id headers every other client of this API
// (curl, pytest) has always used. Never mistake this for real auth.
function getDevIdentity(): { tenantId: string; userId: string } {
  if (typeof window === "undefined") {
    throw new Error("getDevIdentity() can only be called in the browser");
  }
  let tenantId = window.localStorage.getItem(TENANT_ID_KEY);
  let userId = window.localStorage.getItem(USER_ID_KEY);
  if (!tenantId || !userId) {
    tenantId = crypto.randomUUID();
    userId = crypto.randomUUID();
    window.localStorage.setItem(TENANT_ID_KEY, tenantId);
    window.localStorage.setItem(USER_ID_KEY, userId);
  }
  return { tenantId, userId };
}

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const { tenantId, userId } = getDevIdentity();
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    headers: {
      ...(init?.headers ?? {}),
      "X-Dev-Tenant-Id": tenantId,
      "X-Dev-User-Id": userId,
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
    },
  });
  if (!response.ok) {
    const body: unknown = await response.json().catch(() => null);
    const detail =
      body && typeof body === "object" && "detail" in body && typeof body.detail === "string"
        ? body.detail
        : response.statusText;
    throw new Error(detail);
  }
  return response.json() as Promise<T>;
}

export interface TrackSummary {
  track_id: string;
  status: string;
  duration_seconds: number | null;
  has_transcription: boolean;
}

export interface WordInfo {
  idx: number;
  start_ms: number;
  end_ms: number;
  confidence: number;
  text: string | null;
}

export interface TranscriptionResponse {
  track_id: string;
  language: string;
  aligner: string;
  lyrics_display_allowed: boolean;
  words: WordInfo[];
}

export function listTracks(): Promise<TrackSummary[]> {
  return apiFetch<TrackSummary[]>("/tracks");
}

export function getTranscription(trackId: string): Promise<TranscriptionResponse> {
  return apiFetch<TranscriptionResponse>(`/tracks/${trackId}/transcription`);
}

export function realignTrack(trackId: string, text: string): Promise<TranscriptionResponse> {
  return apiFetch<TranscriptionResponse>(`/tracks/${trackId}/realign`, {
    method: "POST",
    body: JSON.stringify({ text }),
  });
}
```

- [ ] **Step 2: Write the track list page**

Create `apps/web/app/tracks/page.tsx`:

```tsx
"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { listTracks, type TrackSummary } from "@/lib/api";

export default function TracksPage() {
  const [tracks, setTracks] = useState<TrackSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listTracks()
      .then(setTracks)
      .catch((err: Error) => setError(err.message));
  }, []);

  if (error) {
    return (
      <main className="max-w-2xl mx-auto py-12 px-6">
        <p className="text-red-600">Could not load tracks: {error}</p>
      </main>
    );
  }
  if (tracks === null) {
    return (
      <main className="max-w-2xl mx-auto py-12 px-6">
        <p>Loading tracks...</p>
      </main>
    );
  }

  return (
    <main className="max-w-2xl mx-auto py-12 px-6">
      <h1 className="text-2xl font-semibold mb-6">Tracks</h1>
      {tracks.length === 0 ? (
        <p className="text-zinc-500">No tracks yet.</p>
      ) : (
        <ul className="divide-y divide-zinc-200">
          {tracks.map((track) => (
            <li key={track.track_id} className="py-3 flex items-center justify-between gap-4">
              <div className="min-w-0">
                <p className="font-mono text-sm truncate">{track.track_id}</p>
                <p className="text-sm text-zinc-500">
                  status: {track.status}
                  {track.duration_seconds !== null &&
                    ` · ${track.duration_seconds.toFixed(1)}s`}
                </p>
              </div>
              {track.has_transcription ? (
                <Link
                  href={`/tracks/${track.track_id}`}
                  className="shrink-0 text-sm font-medium text-blue-600 hover:underline"
                >
                  Edit lyrics
                </Link>
              ) : (
                <span className="shrink-0 text-sm text-zinc-400">not transcribed yet</span>
              )}
            </li>
          ))}
        </ul>
      )}
    </main>
  );
}
```

- [ ] **Step 3: Verify it builds**

Run: `cd apps/web && npm run build`
Expected: builds successfully, no TypeScript or ESLint errors. If it fails on `@/lib/api` not being
found, confirm `tsconfig.json`'s `paths` maps `@/*` to `./*` (it already does — check `apps/web/
tsconfig.json` if this happens rather than changing the import).

- [ ] **Step 4: Commit**

```bash
git add apps/web/lib/api.ts apps/web/app/tracks/page.tsx
git commit -m "M4b: add API client and track list page"
```

---

### Task 4: `/tracks/[id]` editor page

**Files:**
- Create: `apps/web/app/tracks/[id]/page.tsx`

**Interfaces:**
- Consumes: `getTranscription`, `realignTrack`, `TranscriptionResponse`, `WordInfo` from
  `apps/web/lib/api.ts` (Task 3).

No automated tests for this task, same UI/glue-code exemption as Task 3.

- [ ] **Step 1: Write the editor page**

Create `apps/web/app/tracks/[id]/page.tsx`. This uses the `PageProps<'/tracks/[id]'>` global type
helper (the same pattern `apps/web/app/layout.tsx` already uses via `LayoutProps<"/">`) — it isn't
available until Next.js generates route types, which Step 2 (`npm run build`) does; this is expected
and not an error if `npm run dev`/`npm run build` hasn't run yet since this file was created:

```tsx
"use client";

import { use, useEffect, useState } from "react";
import { getTranscription, realignTrack, type TranscriptionResponse } from "@/lib/api";

export default function TrackEditorPage(props: PageProps<"/tracks/[id]">) {
  const { id } = use(props.params);
  const [transcription, setTranscription] = useState<TranscriptionResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [wordTexts, setWordTexts] = useState<string[]>([]);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    getTranscription(id)
      .then((result) => {
        setTranscription(result);
        setWordTexts(result.words.map((w) => w.text ?? ""));
      })
      .catch((err: Error) => setError(err.message));
  }, [id]);

  async function handleSave() {
    setSaving(true);
    setError(null);
    try {
      const result = await realignTrack(id, wordTexts.join(" "));
      setTranscription(result);
      setWordTexts(result.words.map((w) => w.text ?? ""));
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setSaving(false);
    }
  }

  if (error && transcription === null) {
    return (
      <main className="max-w-2xl mx-auto py-12 px-6">
        <p className="text-red-600">Could not load transcription: {error}</p>
      </main>
    );
  }
  if (transcription === null) {
    return (
      <main className="max-w-2xl mx-auto py-12 px-6">
        <p>Loading...</p>
      </main>
    );
  }

  if (!transcription.lyrics_display_allowed) {
    return (
      <main className="max-w-2xl mx-auto py-12 px-6">
        <h1 className="text-2xl font-semibold mb-4">Track {id}</h1>
        <p className="rounded bg-zinc-100 p-4 text-zinc-700">
          Lyric display isn&apos;t permitted for this track, so there&apos;s nothing to correct.
        </p>
      </main>
    );
  }

  if (transcription.language !== "en") {
    return (
      <main className="max-w-2xl mx-auto py-12 px-6">
        <h1 className="text-2xl font-semibold mb-4">Track {id}</h1>
        <p className="rounded bg-zinc-100 p-4 text-zinc-700">
          Correction editing is English-only right now (detected language:{" "}
          {transcription.language}).
        </p>
        <ul className="mt-4 space-y-1">
          {transcription.words.map((word) => (
            <li key={word.idx} className="text-sm">
              {word.text ?? "(no text)"}{" "}
              <span className="text-zinc-400">
                {word.start_ms}ms - {word.end_ms}ms
              </span>
            </li>
          ))}
        </ul>
      </main>
    );
  }

  return (
    <main className="max-w-2xl mx-auto py-12 px-6">
      <h1 className="text-2xl font-semibold mb-4">Track {id}</h1>
      <div className="flex flex-wrap gap-2 mb-6">
        {wordTexts.map((text, idx) => (
          <input
            key={idx}
            value={text}
            onChange={(e) => {
              const next = [...wordTexts];
              next[idx] = e.target.value;
              setWordTexts(next);
            }}
            className="border border-zinc-300 rounded px-2 py-1 text-sm w-24"
          />
        ))}
      </div>
      <button
        onClick={handleSave}
        disabled={saving}
        className="rounded bg-blue-600 px-4 py-2 text-white text-sm font-medium disabled:opacity-50"
      >
        {saving ? "Saving..." : "Save & re-align"}
      </button>
      {error && <p className="mt-4 text-red-600 text-sm">{error}</p>}
    </main>
  );
}
```

- [ ] **Step 2: Verify it builds**

Run: `cd apps/web && npm run build`
Expected: builds successfully. This is also the step that generates the `PageProps<'/tracks/[id]'>`
type — if it fails specifically on `PageProps` being unrecognized, run `npx next typegen` first, then
`npm run build` again.

- [ ] **Step 3: Commit**

```bash
git add "apps/web/app/tracks/[id]/page.tsx"
git commit -m "M4b: add lyric correction editor page"
```

---

## Verification note (not a task — for whoever runs the final review)

Because this plan's frontend tasks have no automated tests (per the working agreement's UI exemption),
the final whole-branch review for this milestone must include an actual live check, not just reading
the diff: start the API (`cd services/api && uvicorn app.main:app --reload`, default port 8000) and
the frontend (`cd apps/web && npm run dev`, default port 3000) together, and in a real browser: upload
a track through the existing endpoints (or use an already-passed/separated/transcribed track from
prior testing), visit `/tracks`, follow it into `/tracks/[id]`, confirm the three states render
correctly (editable, lyrics-not-allowed banner, non-English banner), and confirm a real save-and-realign
round-trip updates the displayed words. This is the one place in this plan's process where "the tests
pass" is not sufficient evidence the feature works.

## Self-Review Notes

**Spec coverage:** Decision 1 (dev-only client-side identity) — covered in Task 3's API client.
Decision 2 (minimal track list) — covered in Tasks 1 and 3. Decision 3 (text-only correction) —
covered in Task 4's editor form (per-word text inputs, no timing UI). Decision 4 (new immutable
`Transcription` rows, `"user-corrected"` sentinel) — covered in Task 2. Decision 5 (non-English
read-only, server-enforced) — covered in Task 2's gate order and Task 4's language check. Decision 6
(lyrics-withheld read-only) — covered in Task 2's gate order and Task 4's `lyrics_display_allowed`
check. CORS — covered in Task 1. Out-of-scope items (manual timing UI, real auth, upload/pipeline
trigger UI) — none of the four tasks build any of these; confirmed by re-reading each task's file list.

**Placeholder scan:** No TBD/TODO in this plan's own instructions or code.

**Type consistency:** `TranscribeResponse`'s shape (`track_id`, `language`, `aligner`,
`lyrics_display_allowed`, `words: list[WordInfo]`) is reused unchanged by `/realign` in Task 2 — the
frontend's `TranscriptionResponse` interface in Task 3 matches it field-for-field, and Task 4 consumes
that same interface with no divergence. `align_words(path: Path, text: str) -> list[Word]`'s signature
in Task 2 matches its actual definition in `services/api/app/transcription.py` (verified by reading
the current file before writing this plan, not assumed from the design spec alone).
