"""Share with Doctor: selective, expiring packages of explicitly selected records.

A SharePackage stores only the resolved selection (document and record ids) plus access policy. The link token is
never stored; only its SHA-256. The optional PIN is stored as a salted PBKDF2 hash. Access is logged without any
clinical content (event, time, hashed IP, file label).
"""
from datetime import datetime
from sqlalchemy import String, Integer, DateTime, Boolean, ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column
from .database import Base
from .crypto import EncryptedText
from .models import utcnow

EXPIRY_CHOICES = {'1h': 1, '24h': 24, '3d': 72, '7d': 168}


class SharePackage(Base):
    __tablename__ = 'share_packages'
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey('patients.id', ondelete='CASCADE'), index=True)
    created_by: Mapped[int | None] = mapped_column(ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    label: Mapped[str] = mapped_column(EncryptedText, default='')
    reason: Mapped[str] = mapped_column(EncryptedText, default='')
    scope_json: Mapped[str] = mapped_column(EncryptedText)  # resolved selection: document ids + record ids + mode
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    pin_salt: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pin_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    view_only: Mapped[bool] = mapped_column(Boolean, default=False)
    allow_download: Mapped[bool] = mapped_column(Boolean, default=True)
    include_summary: Mapped[bool] = mapped_column(Boolean, default=True)
    document_count: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    access_count: Mapped[int] = mapped_column(Integer, default=0)
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ShareAccessLog(Base):
    __tablename__ = 'share_access_log'
    id: Mapped[int] = mapped_column(primary_key=True)
    package_id: Mapped[int] = mapped_column(ForeignKey('share_packages.id', ondelete='CASCADE'), index=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    event: Mapped[str] = mapped_column(String(30))  # created | opened | document | package | summary | pin_failed | revoked | expired_hit | denied
    ip_hash: Mapped[str] = mapped_column(String(64), default='')
    detail: Mapped[str] = mapped_column(Text, default='')  # file label or policy word only; never clinical content
