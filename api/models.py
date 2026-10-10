from datetime import datetime, date, timezone
from sqlalchemy import String, Integer, Date, DateTime, Text, ForeignKey, Boolean, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column
from .database import Base
from .crypto import EncryptedText

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
    name: Mapped[str] = mapped_column(EncryptedText)
    dob: Mapped[date] = mapped_column(Date)
    tz: Mapped[str] = mapped_column(String(64), default='Asia/Kolkata')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sex: Mapped[str | None] = mapped_column(String(16), nullable=True)
    relationship: Mapped[str | None] = mapped_column(String(32), nullable=True)
    modules_json: Mapped[str | None] = mapped_column(Text, nullable=True)

class Document(Base):
    __tablename__ = 'documents'
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey('patients.id', ondelete='CASCADE'), index=True)
    title: Mapped[str] = mapped_column(EncryptedText)
    category: Mapped[str] = mapped_column(String(40), default='other')
    record_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    mime: Mapped[str] = mapped_column(String(60))
    original_name: Mapped[str] = mapped_column(EncryptedText)
    storage_name: Mapped[str] = mapped_column(String(70), unique=True)
    sha256: Mapped[str] = mapped_column(String(64))
    extracted_cipher: Mapped[str | None] = mapped_column(Text, nullable=True)
    extraction_status: Mapped[str] = mapped_column(String(40), default='not_extracted')
    page_count: Mapped[int] = mapped_column(Integer, default=1, server_default='1')
    # Import provenance: the bundle's own page identifier (for example SRC_09) and batch label.
    source_ref: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    import_batch: Mapped[str | None] = mapped_column(String(80), nullable=True)
    notes: Mapped[str] = mapped_column(EncryptedText, default='', server_default='')

class Medicine(Base):
    __tablename__ = 'medicines'
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey('patients.id', ondelete='CASCADE'), index=True)
    name: Mapped[str] = mapped_column(EncryptedText)
    instructions: Mapped[str] = mapped_column(EncryptedText, default='')
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    schedule_times: Mapped[str] = mapped_column(String(100), default='')
    source_document_id: Mapped[int | None] = mapped_column(ForeignKey('documents.id', ondelete='SET NULL'), nullable=True)
    status: Mapped[str] = mapped_column(String(24), default='draft')
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revised_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    source_page: Mapped[int] = mapped_column(Integer, default=1, server_default='1')
    generic_name: Mapped[str] = mapped_column(EncryptedText, default='', server_default='')
    brand_name: Mapped[str] = mapped_column(EncryptedText, default='', server_default='')
    dosage: Mapped[str] = mapped_column(EncryptedText, default='', server_default='')
    dose_unit: Mapped[str] = mapped_column(EncryptedText, default='', server_default='')
    route: Mapped[str] = mapped_column(EncryptedText, default='', server_default='')
    frequency: Mapped[str] = mapped_column(EncryptedText, default='', server_default='')
    prescribing_doctor: Mapped[str] = mapped_column(EncryptedText, default='', server_default='')
    indication: Mapped[str] = mapped_column(EncryptedText, default='', server_default='')
    prescription_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    actual_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    actual_stop_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # prescription | discharge_prescription | inpatient_treatment | historical_prescription
    course_type: Mapped[str] = mapped_column(String(30), default='prescription', server_default='prescription')

class MedicineRevision(Base):
    __tablename__ = 'medicine_revisions'
    id: Mapped[int] = mapped_column(primary_key=True)
    medicine_id: Mapped[int] = mapped_column(ForeignKey('medicines.id', ondelete='CASCADE'), index=True)
    snapshot_json: Mapped[str] = mapped_column(EncryptedText)
    version: Mapped[int] = mapped_column(Integer)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

class MedicationEvent(Base):
    __tablename__ = 'medication_events'
    id: Mapped[int] = mapped_column(primary_key=True)
    medicine_id: Mapped[int] = mapped_column(ForeignKey('medicines.id', ondelete='CASCADE'), index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    event_type: Mapped[str] = mapped_column(String(20))
    source: Mapped[str] = mapped_column(String(35), default='caregiver_report')
    note: Mapped[str] = mapped_column(EncryptedText, default='')
    idempotency_key: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    source_document_id: Mapped[int | None] = mapped_column(ForeignKey('documents.id', ondelete='SET NULL'), nullable=True)
    source_page: Mapped[int] = mapped_column(Integer, default=1, server_default='1')
    medicine_version: Mapped[int] = mapped_column(Integer, default=1, server_default='1')
    __table_args__ = (UniqueConstraint('medicine_id','idempotency_key',name='uq_event_idempotency'),)

class HospitalVisit(Base):
    __tablename__ = 'hospital_visits'
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey('patients.id', ondelete='CASCADE'), index=True)
    facility: Mapped[str] = mapped_column(EncryptedText, default='')
    planned_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    purpose: Mapped[str] = mapped_column(EncryptedText)
    source_document_id: Mapped[int | None] = mapped_column(ForeignKey('documents.id', ondelete='SET NULL'), nullable=True)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(25), default='planned')
    notes: Mapped[str] = mapped_column(EncryptedText, default='')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    source_page: Mapped[int] = mapped_column(Integer, default=1, server_default='1')
    admission_id: Mapped[int | None] = mapped_column(ForeignKey('admissions.id', ondelete='SET NULL'), nullable=True)
    kind: Mapped[str] = mapped_column(String(30), default='hospital', server_default='hospital')
    version: Mapped[int] = mapped_column(Integer, default=1, server_default='1')

class Vaccine(Base):
    __tablename__ = 'vaccines'
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey('patients.id', ondelete='CASCADE'), index=True)
    name: Mapped[str] = mapped_column(EncryptedText)
    dose_label: Mapped[str] = mapped_column(String(70), default='')
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    given_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    source_document_id: Mapped[int | None] = mapped_column(ForeignKey('documents.id', ondelete='SET NULL'), nullable=True)
    source_type: Mapped[str] = mapped_column(String(30), default='manual')
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str] = mapped_column(EncryptedText, default='')
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    source_page: Mapped[int] = mapped_column(Integer, default=1, server_default='1')
    batch_number: Mapped[str] = mapped_column(EncryptedText, default='', server_default='')
    facility: Mapped[str] = mapped_column(EncryptedText, default='', server_default='')
    doctor: Mapped[str] = mapped_column(EncryptedText, default='', server_default='')
    schedule: Mapped[str] = mapped_column(String(30), default='card', server_default='card')
    reminder_days: Mapped[str] = mapped_column(String(80), default='7,1,0', server_default='7,1,0')
    version: Mapped[int] = mapped_column(Integer, default=1, server_default='1')
    __table_args__ = (Index('ix_vax_patient_due', 'patient_id', 'due_date'),)

class Observation(Base):
    __tablename__ = 'observations'
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey('patients.id', ondelete='CASCADE'), index=True)
    test_name: Mapped[str] = mapped_column(EncryptedText)
    value: Mapped[str] = mapped_column(EncryptedText)
    unit: Mapped[str] = mapped_column(EncryptedText, default='')
    specimen: Mapped[str] = mapped_column(EncryptedText, default='')
    record_date: Mapped[date] = mapped_column(Date)
    source_document_id: Mapped[int | None] = mapped_column(ForeignKey('documents.id', ondelete='SET NULL'), nullable=True)
    reference_range: Mapped[str] = mapped_column(EncryptedText, default='')
    source_page: Mapped[int] = mapped_column(Integer, default=1, server_default='1')
    verified: Mapped[bool] = mapped_column(Boolean, default=False, server_default='0')
    organism: Mapped[str] = mapped_column(EncryptedText, default='', server_default='')
    culture_findings: Mapped[str] = mapped_column(EncryptedText, default='', server_default='')
    susceptibility: Mapped[str] = mapped_column(EncryptedText, default='', server_default='')

class CaregiverAccess(Base):
    __tablename__ = 'caregiver_access'
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey('patients.id', ondelete='CASCADE'), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    role: Mapped[str] = mapped_column(String(20))
    __table_args__ = (UniqueConstraint('patient_id', 'user_id'),)

class Invitation(Base):
    __tablename__ = 'invitations'
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey('patients.id', ondelete='CASCADE'), index=True)
    email: Mapped[str] = mapped_column(String(254))
    role: Mapped[str] = mapped_column(String(20))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    accepted: Mapped[bool] = mapped_column(Boolean, default=False)

class ExtractionReview(Base):
    __tablename__ = 'extraction_reviews'
    id: Mapped[int] = mapped_column(primary_key=True)
    document_id: Mapped[int] = mapped_column(ForeignKey('documents.id', ondelete='CASCADE'), unique=True)
    payload: Mapped[str] = mapped_column(EncryptedText)
    status: Mapped[str] = mapped_column(String(30), default='needs_review')
    version: Mapped[int] = mapped_column(Integer, default=1)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

class Admission(Base):
    __tablename__ = 'admissions'
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey('patients.id', ondelete='CASCADE'), index=True)
    facility: Mapped[str] = mapped_column(EncryptedText)
    doctor: Mapped[str] = mapped_column(EncryptedText, default='')
    diagnosis: Mapped[str] = mapped_column(EncryptedText, default='')
    treatment_plan: Mapped[str] = mapped_column(EncryptedText, default='')
    admitted_on: Mapped[date] = mapped_column(Date)
    discharged_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    source_document_id: Mapped[int | None] = mapped_column(ForeignKey('documents.id', ondelete='SET NULL'), nullable=True)
    source_page: Mapped[int] = mapped_column(Integer, default=1)
    discharge_document_id: Mapped[int | None] = mapped_column(ForeignKey('documents.id', ondelete='SET NULL'), nullable=True)

class TreatmentAdministration(Base):
    __tablename__ = 'treatment_administrations'
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey('patients.id', ondelete='CASCADE'), index=True)
    visit_id: Mapped[int] = mapped_column(ForeignKey('hospital_visits.id', ondelete='CASCADE'), index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    medication: Mapped[str] = mapped_column(EncryptedText)
    recorded_dose: Mapped[str] = mapped_column(EncryptedText, default='')
    recorded_route: Mapped[str] = mapped_column(EncryptedText, default='')
    source_document_id: Mapped[int] = mapped_column(ForeignKey('documents.id'))
    source_page: Mapped[int] = mapped_column(Integer, default=1)
    note: Mapped[str] = mapped_column(EncryptedText, default='')
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)

class Episode(Base):
    __tablename__ = 'episodes'
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey('patients.id', ondelete='CASCADE'), index=True)
    title: Mapped[str] = mapped_column(EncryptedText)
    started_on: Mapped[date] = mapped_column(Date)
    ended_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    symptoms: Mapped[str] = mapped_column(EncryptedText, default='')
    documented_diagnosis: Mapped[str] = mapped_column(EncryptedText, default='')
    notes: Mapped[str] = mapped_column(EncryptedText, default='')
    links_json: Mapped[str] = mapped_column(EncryptedText, default='[]')
    source_document_id: Mapped[int | None] = mapped_column(ForeignKey('documents.id', ondelete='SET NULL'), nullable=True)
    source_page: Mapped[int] = mapped_column(Integer, default=1)

class Growth(Base):
    __tablename__ = 'growth'
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey('patients.id', ondelete='CASCADE'), index=True)
    record_date: Mapped[date] = mapped_column(Date)
    weight_kg: Mapped[str] = mapped_column(EncryptedText, default='')
    length_cm: Mapped[str] = mapped_column(EncryptedText, default='')
    head_cm: Mapped[str] = mapped_column(EncryptedText, default='')
    milestone: Mapped[str] = mapped_column(EncryptedText, default='')
    note: Mapped[str] = mapped_column(EncryptedText, default='')
    source_document_id: Mapped[int | None] = mapped_column(ForeignKey('documents.id', ondelete='SET NULL'), nullable=True)
    source_page: Mapped[int] = mapped_column(Integer, default=1)

class Reminder(Base):
    __tablename__ = 'reminders'
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey('patients.id', ondelete='CASCADE'), index=True)
    key: Mapped[str] = mapped_column(String(180), unique=True)
    kind: Mapped[str] = mapped_column(String(30))
    item_id: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(EncryptedText)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(30), default='pending', index=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

class PushDevice(Base):
    __tablename__ = 'push_devices'
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    token: Mapped[str] = mapped_column(EncryptedText)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

class Notification(Base):
    __tablename__ = 'notifications'
    id: Mapped[int] = mapped_column(primary_key=True)
    reminder_id: Mapped[int] = mapped_column(ForeignKey('reminders.id', ondelete='CASCADE'), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    device_id: Mapped[int | None] = mapped_column(ForeignKey('push_devices.id', ondelete='CASCADE'), nullable=True)
    channel: Mapped[str] = mapped_column(String(20), default='in_app')
    dedupe_key: Mapped[str] = mapped_column(String(180), unique=True)
    status: Mapped[str] = mapped_column(String(30), default='pending', index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    receipt_id: Mapped[str | None] = mapped_column(String(180), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

class WorkerHeartbeat(Base):
    __tablename__ = 'worker_heartbeat'
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

class RateWindow(Base):
    __tablename__ = 'rate_windows'
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    count: Mapped[int] = mapped_column(Integer, default=1)

class Audit(Base):
    __tablename__ = 'audit'
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    patient_id: Mapped[int | None] = mapped_column(ForeignKey('patients.id', ondelete='CASCADE'), nullable=True)
    action: Mapped[str] = mapped_column(String(70))
    entity: Mapped[str] = mapped_column(String(55))
    entity_id: Mapped[int] = mapped_column(Integer)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RecordRevision(Base):
    __tablename__ = 'record_revisions'
    id: Mapped[int] = mapped_column(primary_key=True)
    patient_id: Mapped[int] = mapped_column(ForeignKey('patients.id', ondelete='CASCADE'), index=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    entity: Mapped[str] = mapped_column(String(55))
    entity_id: Mapped[int] = mapped_column(Integer)
    snapshot_json: Mapped[str] = mapped_column(EncryptedText)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
