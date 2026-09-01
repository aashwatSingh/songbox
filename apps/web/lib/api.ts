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
  title: string | null;
  artist: string | null;
  duration_seconds: number | null;
  has_transcription: boolean;
  bookmarked: boolean;
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

export interface UploadResponse {
  track_id: string;
  status: string;
  reason: string;
}

export async function uploadTrack(
  file: File,
  attestationText: string,
  title?: string,
  artist?: string,
): Promise<UploadResponse> {
  const formData = new FormData();
  formData.append("file", file);
  formData.append("lane", "A");
  formData.append("attestation_text", attestationText);
  if (title) {
    formData.append("title", title);
  }
  if (artist) {
    formData.append("artist", artist);
  }
  // Raw fetch, not apiFetch() -- apiFetch() unconditionally sets Content-Type: application/json
  // whenever a body is present, which would break a multipart upload (the browser must set its
  // own Content-Type with the multipart boundary itself). credentials: "include" still applies,
  // same as every other authenticated call.
  const response = await fetch(`${API_BASE_URL}/tracks/upload`, {
    method: "POST",
    credentials: "include",
    body: formData,
  });
  if (!response.ok) {
    const body: unknown = await response.json().catch(() => null);
    const detail =
      body && typeof body === "object" && "detail" in body && typeof body.detail === "string"
        ? body.detail
        : response.statusText;
    throw new Error(detail);
  }
  return response.json() as Promise<UploadResponse>;
}

export function toggleBookmark(trackId: string): Promise<{ track_id: string; bookmarked: boolean }> {
  return apiFetch(`/tracks/${trackId}/bookmark`, { method: "POST" });
}

export async function deleteTrack(trackId: string): Promise<void> {
  const response = await fetch(`${API_BASE_URL}/tracks/${trackId}`, {
    method: "DELETE",
    credentials: "include",
  });
  if (!response.ok) {
    const body: unknown = await response.json().catch(() => null);
    const detail =
      body && typeof body === "object" && "detail" in body && typeof body.detail === "string"
        ? body.detail
        : response.statusText;
    throw new Error(detail);
  }
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
