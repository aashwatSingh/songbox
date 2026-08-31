from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import CITEXT, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class License(Base):
    __tablename__ = "licenses"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    reference: Mapped[str] = mapped_column(Text, nullable=False)
    covers_recording: Mapped[bool] = mapped_column(Boolean, nullable=False)
    covers_lyrics: Mapped[bool] = mapped_column(Boolean, nullable=False)
    expiry: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RightsDeclaration(Base):
    __tablename__ = "rights_declarations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # Lane: "A" | "B" | "C"
    lane: Mapped[str] = mapped_column(String(1), nullable=False)
    attestation_text: Mapped[str] = mapped_column(Text, nullable=False)
    ip_address: Mapped[str] = mapped_column(String(45), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    release_name: Mapped[str | None] = mapped_column(Text, nullable=True)
    license_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("licenses.id"), nullable=True
    )
    pd_cc_source: Mapped[str | None] = mapped_column(Text, nullable=True)
    pd_cc_license: Mapped[str | None] = mapped_column(Text, nullable=True)
    attribution_string: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Set only on supplementary declarations created after the original (e.g. the "stronger
    # attestation" row confirm-attestation creates) -- links them back to their track so the
    # retention purge script can find and delete them too. The original declaration a track is
    # created with is reachable the other way, via Track.rights_declaration_id, and leaves this
    # column null.
    track_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tracks.id"), nullable=True
    )


class Track(Base):
    __tablename__ = "tracks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    artist: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Numeric, nullable=True)
    fingerprint: Mapped[str | None] = mapped_column(Text, nullable=True)
    rights_declaration_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("rights_declarations.id"), nullable=False
    )
    # Status: pending_review|passed|rejected
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    # New in M7a: "taken_down" is a new value for status, alongside the existing
    # pending_review|passed|rejected. takedown_reason/takedown_at are only ever set together, by
    # the takedown endpoint -- both null for every other status.
    takedown_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    takedown_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class FingerprintMatch(Base):
    __tablename__ = "fingerprint_matches"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    track_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tracks.id"), nullable=False
    )
    acoustid_response: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    matched_release: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Resolution: no_match|held|confirmed|mismatch
    resolution: Mapped[str] = mapped_column(String(20), nullable=False)
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)


class Stem(Base):
    __tablename__ = "stems"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    track_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tracks.id"), nullable=False
    )
    # stem_type: "vocals" | "drums" | "bass" | "other"
    stem_type: Mapped[str] = mapped_column(String(10), nullable=False)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False)
    # model_name: "htdemucs" | "htdemucs_ft" -- which model variant actually produced this row
    model_name: Mapped[str] = mapped_column(String(20), nullable=False)


class Transcription(Base):
    __tablename__ = "transcriptions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    track_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tracks.id"), nullable=False
    )
    # whisper_model: which faster-whisper size produced this row, e.g. "base"
    whisper_model: Mapped[str] = mapped_column(String(20), nullable=False)
    # aligner: "wav2vec2" | "whisper_native"
    aligner: Mapped[str] = mapped_column(String(20), nullable=False)
    # language: Whisper's detected ISO language code, e.g. "en"
    language: Mapped[str] = mapped_column(String(10), nullable=False)
    lyrics_display_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # words: [{"idx": int, "text": str, "start_ms": int, "end_ms": int, "confidence": float}, ...]
    words: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class KaraokePackage(Base):
    __tablename__ = "karaoke_packages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    track_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tracks.id"), nullable=False
    )
    # schema_version: karaoke.json's own version -- 1 for every row this milestone produces.
    # CLAUDE.md: any shape change needs a migration path, not a silent bump.
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    # words: [{"idx": int, "text": str | None, "start_ms": int, "end_ms": int, "confidence": float}]
    # -- copied from the track's latest Transcription row at packaging time, text nulled when
    # lyrics_display_allowed is False (checked at write time, not just read time).
    words: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False)
    # pitch_model: which torchcrepe variant produced this row, e.g. "tiny"
    pitch_model: Mapped[str] = mapped_column(String(20), nullable=False)
    # pitch: [{"time_ms": int, "hz": float | None, "confidence": float}, ...]
    pitch: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False)
    tempo_bpm: Mapped[float] = mapped_column(Float, nullable=False)
    beats_ms: Mapped[list[int]] = mapped_column(JSONB, nullable=False)
    sections_ms: Mapped[list[int]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, unique=True, default=uuid.uuid4
    )
    email: Mapped[str] = mapped_column(CITEXT, nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class UserSession(Base):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    token_hash: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
