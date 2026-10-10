from datetime import datetime, date, timezone
from sqlalchemy import String, Integer, Date, DateTime, Text, ForeignKey, Boolean, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.orm import DeclarativeBase
class Base(DeclarativeBase):
    pass

def utcnow():
    return datetime.now(timezone.utc)

class User(Base):
    __tablename__ = 'users'
    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    password_salt: Mapped[str] = mapped_column(String(100))
    password_hash: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

class Session(Base):
    __tablename__ = 'sessions'
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

class Patient(Base):
    __tablename__ = 'patients'
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    name: Mapped[str] = mapped_column(String(100))
    dob: Mapped[date] = mapped_column(Date)
    tz: Mapped[str] = mapped_column(String(64), default='Asia/Kolkata')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

class Document(Base):
    __tablename__ = 'documents'
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey('patients.id', ondelete='CASCADE'), index=True)
    title: Mapped[str] = mapped_column(String(180))
    category: Mapped[str] = mapped_column(String(40), default='other')
    record_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    mime: Mapped[str] = mapped_column(String(60))
    original_name: Mapped[str] = mapped_column(String(180))
    storage_name: Mapped[str] = mapped_column(String(70), unique=True)
    sha256: Mapped[str] = mapped_column(String(64))
    extracted_cipher: Mapped[str | None] = mapped_column(Text, nullable=True)
    extraction_status: Mapped[str] = mapped_column(String(40), default='not_extracted')

class Medicine(Base):
    __tablename__ = 'medicines'
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey('patients.id', ondelete='CASCADE'), index=True)
    name: Mapped[str] = mapped_column(String(150))
    instructions: Mapped[str] = mapped_column(Text, default='')
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    schedule_times: Mapped[str] = mapped_column(String(100), default='')
    source_document_id: Mapped[int | None] = mapped_column(ForeignKey('documents.id', ondelete='SET NULL'), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default='draft')
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revised_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

class MedicineRevision(Base):
    __tablename__ = 'medicine_revisions'
    id: Mapped[int] = mapped_column(primary_key=True)
    medicine_id: Mapped[int] = mapped_column(ForeignKey('medicines.id', ondelete='CASCADE'), index=True)
    snapshot_json: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

class MedicationEvent(Base):
    __tablename__ = 'medication_events'
    id: Mapped[int] = mapped_column(primary_key=True)
    medicine_id: Mapped[int] = mapped_column(ForeignKey('medicines.id', ondelete='CASCADE'), index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    event_type: Mapped[str] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(35), default='caregiver_report')
    note: Mapped[str] = mapped_column(Text, default='')
    idempotency_key: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint('medicine_id','idempotency_key',name='uq_event_idempotency'),)

class HospitalVisit(Base):
    __tablename__ = 'hospital_visits'
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey('patients.id', ondelete='CASCADE'), index=True)
    facility: Mapped[str] = mapped_column(String(160), default='')
    planned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    purpose: Mapped[str] = mapped_column(Text)
    source_document_id: Mapped[int | None] = mapped_column(ForeignKey('documents.id', ondelete='SET NULL'), nullable=True)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(25), default='planned')
    notes: Mapped[str] = mapped_column(Text, default='')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

class Vaccine(Base):
    __tablename__ = 'vaccines'
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey('patients.id', ondelete='CASCADE'), index=True)
    name: Mapped[str] = mapped_column(String(120))
    dose_label: Mapped[str] = mapped_column(String(70), default='')
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    given_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    source_document_id: Mapped[int | None] = mapped_column(ForeignKey('documents.id', ondelete='SET NULL'), nullable=True)
    source_type: Mapped[str] = mapped_column(String(30), default='manual')
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str] = mapped_column(Text, default='')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (Index('ix_vax_patient_due', 'patient_id', 'due_date'),)

class Observation(Base):
    __tablename__ = 'observations'
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey('patients.id', ondelete='CASCADE'), index=True)
    test_name: Mapped[str] = mapped_column(String(120))
    value: Mapped[str] = mapped_column(String(75))
    unit: Mapped[str] = mapped_column(String(50), default='')
    specimen: Mapped[str] = mapped_column(String(80), default='')
    record_date: Mapped[date] = mapped_column(Date)
    source_document_id: Mapped[int | None] = mapped_column(ForeignKey('documents.id', ondelete='SET NULL'), nullable=True)
    reference_range: Mapped[str] = mapped_column(String(120), default='')

class Audit(Base):
    __tablename__ = 'audit'
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    patient_id: Mapped[int | None] = mapped_column(ForeignKey('patients.id', ondelete='CASCADE'), nullable=True)
    action: Mapped[str] = mapped_column(String(70))
    entity: Mapped[str] = mapped_column(String(55))
    entity_id: Mapped[int] = mapped_column(Integer)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
