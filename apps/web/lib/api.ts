const API_BASE_URL = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://localhost:8000";

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...init,
    credentials: "include",
    headers: {
      ...(init?.headers ?? {}),
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

export interface Identity {
  tenant_id: string;
  user_id: string;
}

export interface CurrentUser extends Identity {
  email: string;
}

export function signup(email: string, password: string): Promise<Identity> {
  return apiFetch<Identity>("/auth/signup", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
}

export function login(email: string, password: string): Promise<Identity> {
  return apiFetch<Identity>("/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
  });
}

export async function logout(): Promise<void> {
  await apiFetch<{ status: string }>("/auth/logout", { method: "POST" });
}

export async function me(): Promise<CurrentUser | null> {
  try {
    return await apiFetch<CurrentUser>("/auth/me");
  } catch {
    return null;
  }
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
