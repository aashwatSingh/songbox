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
