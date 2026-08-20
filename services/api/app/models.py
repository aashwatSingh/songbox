from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
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
