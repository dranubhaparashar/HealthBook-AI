"""Lifetime Health persistence: fever episodes, antibiotic courses, administrations, evidence and parent entries.

Every row is either transcribed from a clinician document (origin='clinician_document') or entered by a parent
(origin='parent_report'). Nothing here is inferred automatically; verification_status tracks guardian review.
"""
from datetime import datetime, date
from sqlalchemy import String, Integer, Date, DateTime, Text, ForeignKey, Boolean, Index
from sqlalchemy.orm import Mapped, mapped_column
from .database import Base
from .crypto import EncryptedText
from .models import utcnow

ORIGINS=('clinician_document','parent_report')
VERIFICATION=('needs_verification','verified','rejected')
ADMIN_STATUS=('prescribed_only','scheduled','confirmed_administered','unknown','not_received')
SETTINGS=('inpatient','outpatient','discharge_prescription','unknown')
ROUTES=('oral','IV','IM','other','unknown')
CERTAINTY=('exact','approximate','unknown')


class ParentHealthEntry(Base):
    """Raw parent-reported historical information. Corrections create RecordRevision snapshots; rows are never deleted."""
    __tablename__='parent_health_entries'
    id: Mapped[int]=mapped_column(primary_key=True)
    patient_id: Mapped[int]=mapped_column(ForeignKey('patients.id',ondelete='CASCADE'),index=True)
    user_id: Mapped[int|None]=mapped_column(ForeignKey('users.id',ondelete='SET NULL'),nullable=True)
    kind: Mapped[str]=mapped_column(String(20))  # fever | medicine | hospital_visit | note
    started_on: Mapped[date|None]=mapped_column(Date,nullable=True)
    ended_on: Mapped[date|None]=mapped_column(Date,nullable=True)
    temperature: Mapped[str]=mapped_column(EncryptedText,default='')
    temperature_unit: Mapped[str]=mapped_column(String(2),default='')
    symptoms: Mapped[str]=mapped_column(EncryptedText,default='')
    medicine_name: Mapped[str]=mapped_column(EncryptedText,default='')
    medicine_dose: Mapped[str]=mapped_column(EncryptedText,default='')
    administered_on: Mapped[date|None]=mapped_column(Date,nullable=True)
    administered_time: Mapped[str]=mapped_column(String(5),default='')
    hospital_visit: Mapped[str]=mapped_column(EncryptedText,default='')
    notes: Mapped[str]=mapped_column(EncryptedText,default='')
    status: Mapped[str]=mapped_column(String(20),default='active')  # active | withdrawn
    version: Mapped[int]=mapped_column(Integer,default=1)
    fever_episode_id: Mapped[int|None]=mapped_column(ForeignKey('fever_episodes.id',ondelete='SET NULL'),nullable=True)
    antibiotic_course_id: Mapped[int|None]=mapped_column(ForeignKey('antibiotic_courses.id',ondelete='SET NULL'),nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)


class FeverEpisode(Base):
    __tablename__='fever_episodes'
    id: Mapped[int]=mapped_column(primary_key=True)
    patient_id: Mapped[int]=mapped_column(ForeignKey('patients.id',ondelete='CASCADE'),index=True)
    illness_episode_id: Mapped[int|None]=mapped_column(ForeignKey('episodes.id',ondelete='SET NULL'),nullable=True)
    admission_id: Mapped[int|None]=mapped_column(ForeignKey('admissions.id',ondelete='SET NULL'),nullable=True)
    title: Mapped[str]=mapped_column(EncryptedText)
    first_documented_on: Mapped[date|None]=mapped_column(Date,nullable=True)
    last_documented_on: Mapped[date|None]=mapped_column(Date,nullable=True)
    documented_fever_days: Mapped[int|None]=mapped_column(Integer,nullable=True)
    duration_text: Mapped[str]=mapped_column(EncryptedText,default='')
    date_certainty: Mapped[str]=mapped_column(String(20),default='unknown')
    highest_temperature: Mapped[str]=mapped_column(EncryptedText,default='')
    temperature_unit: Mapped[str]=mapped_column(String(2),default='')
    symptoms: Mapped[str]=mapped_column(EncryptedText,default='')
    documented_diagnosis: Mapped[str]=mapped_column(EncryptedText,default='')
    hospitalized: Mapped[bool]=mapped_column(Boolean,default=False)
    antipyretics: Mapped[str]=mapped_column(EncryptedText,default='')
    resolution_documented_on: Mapped[date|None]=mapped_column(Date,nullable=True)
    origin: Mapped[str]=mapped_column(String(30),default='clinician_document')
    verification_status: Mapped[str]=mapped_column(String(30),default='needs_verification',index=True)
    verified_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)
    verified_by: Mapped[int|None]=mapped_column(ForeignKey('users.id',ondelete='SET NULL'),nullable=True)
    needs_parent_check: Mapped[str]=mapped_column(EncryptedText,default='')  # what exactly is unclear
    notes: Mapped[str]=mapped_column(EncryptedText,default='')
    source_document_id: Mapped[int|None]=mapped_column(ForeignKey('documents.id',ondelete='SET NULL'),nullable=True)
    source_page: Mapped[int]=mapped_column(Integer,default=1)
    import_key: Mapped[str|None]=mapped_column(String(120),nullable=True,unique=True)
    version: Mapped[int]=mapped_column(Integer,default=1)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)


class AntibioticCourse(Base):
    __tablename__='antibiotic_courses'
    id: Mapped[int]=mapped_column(primary_key=True)
    patient_id: Mapped[int]=mapped_column(ForeignKey('patients.id',ondelete='CASCADE'),index=True)
    illness_episode_id: Mapped[int|None]=mapped_column(ForeignKey('episodes.id',ondelete='SET NULL'),nullable=True)
    admission_id: Mapped[int|None]=mapped_column(ForeignKey('admissions.id',ondelete='SET NULL'),nullable=True)
    medicine_id: Mapped[int|None]=mapped_column(ForeignKey('medicines.id',ondelete='SET NULL'),nullable=True)
    fever_episode_id: Mapped[int|None]=mapped_column(ForeignKey('fever_episodes.id',ondelete='SET NULL'),nullable=True)
    generic_name: Mapped[str]=mapped_column(EncryptedText)
    brand_name: Mapped[str]=mapped_column(EncryptedText,default='')
    drug_class: Mapped[str]=mapped_column(String(60),default='')
    route: Mapped[str]=mapped_column(String(10),default='unknown')
    dose: Mapped[str]=mapped_column(EncryptedText,default='')
    frequency: Mapped[str]=mapped_column(EncryptedText,default='')
    prescribed_on: Mapped[date|None]=mapped_column(Date,nullable=True)
    planned_start: Mapped[date|None]=mapped_column(Date,nullable=True)
    planned_end: Mapped[date|None]=mapped_column(Date,nullable=True)
    prescribed_duration_days: Mapped[int|None]=mapped_column(Integer,nullable=True)
    prescribed_duration_text: Mapped[str]=mapped_column(EncryptedText,default='')
    actual_start: Mapped[date|None]=mapped_column(Date,nullable=True)
    actual_stop: Mapped[date|None]=mapped_column(Date,nullable=True)
    administered_duration_days: Mapped[int|None]=mapped_column(Integer,nullable=True)
    administration_status: Mapped[str]=mapped_column(String(30),default='unknown',index=True)
    setting: Mapped[str]=mapped_column(String(30),default='unknown')
    prescribing_doctor: Mapped[str]=mapped_column(EncryptedText,default='')
    facility: Mapped[str]=mapped_column(EncryptedText,default='')
    indication: Mapped[str]=mapped_column(EncryptedText,default='')
    continuation_of_id: Mapped[int|None]=mapped_column(ForeignKey('antibiotic_courses.id',ondelete='SET NULL'),nullable=True)
    origin: Mapped[str]=mapped_column(String(30),default='clinician_document')
    verification_status: Mapped[str]=mapped_column(String(30),default='needs_verification',index=True)
    verified_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)
    verified_by: Mapped[int|None]=mapped_column(ForeignKey('users.id',ondelete='SET NULL'),nullable=True)
    uncertain_fields: Mapped[str]=mapped_column(String(250),default='')
    needs_parent_check: Mapped[str]=mapped_column(EncryptedText,default='')
    notes: Mapped[str]=mapped_column(EncryptedText,default='')
    source_document_id: Mapped[int|None]=mapped_column(ForeignKey('documents.id',ondelete='SET NULL'),nullable=True)
    source_page: Mapped[int]=mapped_column(Integer,default=1)
    import_key: Mapped[str|None]=mapped_column(String(120),nullable=True,unique=True)
    version: Mapped[int]=mapped_column(Integer,default=1)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)


class AntibioticAdministration(Base):
    """One documented individual administration (nursing chart, hospital statement, or caregiver report)."""
    __tablename__='antibiotic_administrations'
    id: Mapped[int]=mapped_column(primary_key=True)
    patient_id: Mapped[int]=mapped_column(ForeignKey('patients.id',ondelete='CASCADE'),index=True)
    course_id: Mapped[int]=mapped_column(ForeignKey('antibiotic_courses.id',ondelete='CASCADE'),index=True)
    administered_on: Mapped[date]=mapped_column(Date)
    administered_time: Mapped[str]=mapped_column(String(5),default='')
    dose: Mapped[str]=mapped_column(EncryptedText,default='')
    route: Mapped[str]=mapped_column(String(10),default='unknown')
    source: Mapped[str]=mapped_column(String(40),default='caregiver_report')  # nursing_chart | discharge_summary | caregiver_report
    source_document_id: Mapped[int|None]=mapped_column(ForeignKey('documents.id',ondelete='SET NULL'),nullable=True)
    source_page: Mapped[int]=mapped_column(Integer,default=1)
    note: Mapped[str]=mapped_column(EncryptedText,default='')
    parent_entry_id: Mapped[int|None]=mapped_column(ForeignKey('parent_health_entries.id',ondelete='SET NULL'),nullable=True)
    idempotency_key: Mapped[str]=mapped_column(String(80),unique=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)


class LifetimeEvidence(Base):
    """A quoted passage from an original page that supports a fever episode, antibiotic course or illness."""
    __tablename__='lifetime_evidence'
    id: Mapped[int]=mapped_column(primary_key=True)
    patient_id: Mapped[int]=mapped_column(ForeignKey('patients.id',ondelete='CASCADE'),index=True)
    entity: Mapped[str]=mapped_column(String(40))  # fever_episode | antibiotic_course | illness_episode | administration
    entity_id: Mapped[int]=mapped_column(Integer)
    evidence_kind: Mapped[str]=mapped_column(String(40),default='mention')
    source_document_id: Mapped[int|None]=mapped_column(ForeignKey('documents.id',ondelete='SET NULL'),nullable=True)
    source_page: Mapped[int]=mapped_column(Integer,default=1)
    source_ref: Mapped[str]=mapped_column(String(40),default='')
    quoted_text: Mapped[str]=mapped_column(EncryptedText,default='')
    observed_on: Mapped[date|None]=mapped_column(Date,nullable=True)
    certainty: Mapped[str]=mapped_column(String(10),default='medium')
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)
    __table_args__=(Index('ix_lifetime_evidence_entity','entity','entity_id'),)
