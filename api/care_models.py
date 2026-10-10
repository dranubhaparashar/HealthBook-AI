"""Daily Care journal and Home Activity Camera persistence.

Typed, patient-scoped, auditable entities. Every entry stores the moment it happened as a timezone-aware instant
plus the child's local calendar date (computed in the patient's timezone at write time) so day/week/month views and
"today" counts are stable across timezone boundaries. Corrections go through api.history.remember (previous values
are kept in record_revisions); rows are withdrawn, never deleted. client_key makes offline/duplicate submissions
idempotent per patient.
"""
from datetime import datetime, date
from sqlalchemy import String, Integer, Float, Date, DateTime, Boolean, ForeignKey, UniqueConstraint, Index
from sqlalchemy.orm import Mapped, mapped_column
from .database import Base
from .crypto import EncryptedText
from .models import utcnow

FLUID_TYPES=('water','ors','breastmilk','formula','milk','juice','soup','other')
DIAPER_KINDS=('wet','stool','mixed','dry')
URINE_AMOUNTS=('light','medium','heavy','unknown','')
STOOL_COLOURS=('brown','yellow','green','dark_green','black','red','pale','white','other','')
STOOL_CONSISTENCIES=('watery','loose','soft','formed','hard','')
STOOL_AMOUNTS=('small','medium','large','')
CLINICAL_REVIEW_COLOURS=('black','red','pale','white')
ADMIN_STATUSES=('given','skipped','missed','later','unknown')
SYMPTOM_KINDS=('fever','vomiting','loose_stool','constipation','cough','cold','rash','reduced_appetite','irritability','lethargy','pain_crying','other')
ACTIVITY_KINDS=('awake','playing','walking','crawling','sitting','feeding','outdoor','exercise','bath','tummy_time','other')
SLEEP_KINDS=('night','nap')
ENTRY_SOURCES=('parent','camera_confirmed','import')
CAMERA_PROTOCOLS=('rtsp','onvif','http')
ANALYSIS_MODES=('off','motion','activity')
OBSERVATION_KINDS=('movement','active','low_activity','presence','no_presence','rest','standing','sitting','walking_like','crawling_like')
REVIEW_STATES=('ai_observed','parent_confirmed','parent_corrected','dismissed')


class _CareEntry:
    """Columns shared by every journal entry type."""
    id: Mapped[int]=mapped_column(primary_key=True)
    patient_id: Mapped[int]=mapped_column(ForeignKey('patients.id',ondelete='CASCADE'),index=True)
    recorded_by: Mapped[int|None]=mapped_column(ForeignKey('users.id',ondelete='SET NULL'),nullable=True)
    at: Mapped[datetime]=mapped_column(DateTime(timezone=True),index=True)
    local_date: Mapped[date]=mapped_column(Date,index=True)
    note: Mapped[str]=mapped_column(EncryptedText,default='')
    source: Mapped[str]=mapped_column(String(20),default='parent')
    status: Mapped[str]=mapped_column(String(12),default='active')  # active | withdrawn
    version: Mapped[int]=mapped_column(Integer,default=1)
    client_key: Mapped[str|None]=mapped_column(String(80),nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)


class CareDay(Base):
    __tablename__='care_days'
    id: Mapped[int]=mapped_column(primary_key=True)
    patient_id: Mapped[int]=mapped_column(ForeignKey('patients.id',ondelete='CASCADE'),index=True)
    local_date: Mapped[date]=mapped_column(Date)
    day_note: Mapped[str]=mapped_column(EncryptedText,default='')
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)
    __table_args__=(UniqueConstraint('patient_id','local_date',name='uq_care_day'),)


class CareSettings(Base):
    """Parent-entered configuration. Thresholds are only ever the clinician-provided values the parent typed in."""
    __tablename__='care_settings'
    id: Mapped[int]=mapped_column(primary_key=True)
    patient_id: Mapped[int]=mapped_column(ForeignKey('patients.id',ondelete='CASCADE'),unique=True)
    wet_diaper_threshold_per_day: Mapped[int|None]=mapped_column(Integer,nullable=True)
    threshold_source: Mapped[str]=mapped_column(EncryptedText,default='')  # e.g. "advised by Dr X on 2 Oct"
    fluid_quick_amounts_ml: Mapped[str]=mapped_column(String(80),default='30,60,100,150')
    default_fluid_type: Mapped[str]=mapped_column(String(20),default='water')
    clinical_review_colours: Mapped[str]=mapped_column(String(80),default='black,red,pale,white')
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)


class FluidEntry(_CareEntry,Base):
    __tablename__='fluid_entries'
    amount_ml: Mapped[float|None]=mapped_column(Float,nullable=True)
    amount_unknown: Mapped[bool]=mapped_column(Boolean,default=False)
    custom_amount: Mapped[str]=mapped_column(EncryptedText,default='')  # free text such as "half a cup"
    unit: Mapped[str]=mapped_column(String(10),default='ml')
    fluid_type: Mapped[str]=mapped_column(String(20),default='water')
    duration_minutes: Mapped[int|None]=mapped_column(Integer,nullable=True)  # breastfeeding sessions
    __table_args__=(UniqueConstraint('patient_id','client_key',name='uq_fluid_client_key'),)


class DiaperEntry(_CareEntry,Base):
    __tablename__='diaper_entries'
    kind: Mapped[str]=mapped_column(String(10))
    urine_amount: Mapped[str]=mapped_column(String(10),default='')
    stool_entry_id: Mapped[int|None]=mapped_column(ForeignKey('stool_entries.id',ondelete='SET NULL'),nullable=True)
    __table_args__=(UniqueConstraint('patient_id','client_key',name='uq_diaper_client_key'),)


class StoolEntry(_CareEntry,Base):
    __tablename__='stool_entries'
    colour: Mapped[str]=mapped_column(String(12),default='')
    consistency: Mapped[str]=mapped_column(String(10),default='')
    amount: Mapped[str]=mapped_column(String(10),default='')
    mucus_observed: Mapped[bool|None]=mapped_column(Boolean,nullable=True)
    blood_observed: Mapped[bool|None]=mapped_column(Boolean,nullable=True)
    watery: Mapped[bool]=mapped_column(Boolean,default=False)
    parent_concern: Mapped[str]=mapped_column(String(10),default='none')  # none | mild | moderate | high
    symptoms: Mapped[str]=mapped_column(EncryptedText,default='')
    illness_episode_id: Mapped[int|None]=mapped_column(ForeignKey('episodes.id',ondelete='SET NULL'),nullable=True)
    clinical_review_suggested: Mapped[bool]=mapped_column(Boolean,default=False)
    ai_observation_json: Mapped[str]=mapped_column(EncryptedText,default='')
    ai_review_status: Mapped[str]=mapped_column(String(20),default='none')  # none | ai_observed | parent_confirmed | parent_corrected | dismissed
    time_source: Mapped[str]=mapped_column(String(20),default='manual')  # manual | exif | exif_created | filename | metadata | filesystem | parent_corrected
    ai_suggestion_json: Mapped[str]=mapped_column(EncryptedText,default='')   # what the app pre-selected (audit trail)
    parent_values_json: Mapped[str]=mapped_column(EncryptedText,default='')   # what the parent confirmed or corrected at save time
    __table_args__=(UniqueConstraint('patient_id','client_key',name='uq_stool_client_key'),)


class StoolPhoto(Base):
    """Original photograph, Fernet-encrypted through api.storage; never modified, never public."""
    __tablename__='stool_photos'
    id: Mapped[int]=mapped_column(primary_key=True)
    patient_id: Mapped[int]=mapped_column(ForeignKey('patients.id',ondelete='CASCADE'),index=True)
    stool_entry_id: Mapped[int]=mapped_column(ForeignKey('stool_entries.id',ondelete='CASCADE'),index=True)
    storage_name: Mapped[str]=mapped_column(String(70),unique=True)
    sha256: Mapped[str]=mapped_column(String(64))
    mime: Mapped[str]=mapped_column(String(40))
    width: Mapped[int]=mapped_column(Integer,default=0)
    height: Mapped[int]=mapped_column(Integer,default=0)
    taken_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)
    detected_taken_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)  # before any parent correction
    taken_at_source: Mapped[str]=mapped_column(String(20),default='')   # exif | exif_created | filename | metadata | filename_date | filesystem | manual | none
    taken_at_confidence: Mapped[str]=mapped_column(String(10),default='')
    taken_at_tz: Mapped[str]=mapped_column(String(40),default='')
    original_filename: Mapped[str]=mapped_column(EncryptedText,default='')
    metadata_json: Mapped[str]=mapped_column(EncryptedText,default='')  # timestamp candidates, camera make/model; never pixel data
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)
    deleted_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)


class StoolIntake(Base):
    """A photo that was uploaded but not yet confirmed by the parent. Holds the detected capture time and the
    AI suggestions until 'Confirm & save' turns it into a StoolEntry + StoolPhoto (and optionally a DiaperEntry)."""
    __tablename__='stool_intake'
    id: Mapped[int]=mapped_column(primary_key=True)
    patient_id: Mapped[int]=mapped_column(ForeignKey('patients.id',ondelete='CASCADE'),index=True)
    user_id: Mapped[int|None]=mapped_column(ForeignKey('users.id',ondelete='SET NULL'),nullable=True)
    storage_name: Mapped[str]=mapped_column(String(70),unique=True)
    sha256: Mapped[str]=mapped_column(String(64),index=True)
    mime: Mapped[str]=mapped_column(String(40))
    width: Mapped[int]=mapped_column(Integer,default=0)
    height: Mapped[int]=mapped_column(Integer,default=0)
    original_filename: Mapped[str]=mapped_column(EncryptedText,default='')
    detected_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)
    detected_source: Mapped[str]=mapped_column(String(20),default='none')
    detected_confidence: Mapped[str]=mapped_column(String(10),default='none')
    detected_tz: Mapped[str]=mapped_column(String(40),default='')
    detection_json: Mapped[str]=mapped_column(EncryptedText,default='')
    suggestion_json: Mapped[str]=mapped_column(EncryptedText,default='')
    status: Mapped[str]=mapped_column(String(12),default='pending')  # pending | confirmed | discarded | duplicate
    duplicate_of_entry_id: Mapped[int|None]=mapped_column(ForeignKey('stool_entries.id',ondelete='SET NULL'),nullable=True)
    stool_entry_id: Mapped[int|None]=mapped_column(ForeignKey('stool_entries.id',ondelete='SET NULL'),nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)
    expires_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)


class MedicationAdministration(_CareEntry,Base):
    """What the parent actually recorded: given, skipped, missed, deferred or unknown. Never created from a schedule."""
    __tablename__='medication_administrations'
    medicine_id: Mapped[int|None]=mapped_column(ForeignKey('medicines.id',ondelete='SET NULL'),nullable=True)
    medicine_name: Mapped[str]=mapped_column(EncryptedText)
    dose: Mapped[str]=mapped_column(EncryptedText,default='')
    dose_unit: Mapped[str]=mapped_column(String(20),default='')
    route: Mapped[str]=mapped_column(String(20),default='')
    scheduled_for: Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)
    administration_status: Mapped[str]=mapped_column(String(10),default='given')
    antibiotic_course_id: Mapped[int|None]=mapped_column(ForeignKey('antibiotic_courses.id',ondelete='SET NULL'),nullable=True)
    __table_args__=(UniqueConstraint('patient_id','client_key',name='uq_medadmin_client_key'),Index('ix_medadmin_patient_date','patient_id','local_date'))


class SymptomEntry(_CareEntry,Base):
    __tablename__='symptom_entries'
    kind: Mapped[str]=mapped_column(String(20))
    ended_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)
    severity: Mapped[str]=mapped_column(String(10),default='')  # mild | moderate | severe, chosen by parent
    temperature: Mapped[str]=mapped_column(String(10),default='')
    temperature_unit: Mapped[str]=mapped_column(String(2),default='')
    illness_episode_id: Mapped[int|None]=mapped_column(ForeignKey('episodes.id',ondelete='SET NULL'),nullable=True)
    fever_episode_id: Mapped[int|None]=mapped_column(ForeignKey('fever_episodes.id',ondelete='SET NULL'),nullable=True)
    __table_args__=(UniqueConstraint('patient_id','client_key',name='uq_symptom_client_key'),)


class ActivityEntry(_CareEntry,Base):
    __tablename__='activity_entries'
    kind: Mapped[str]=mapped_column(String(20))
    ended_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)
    duration_minutes: Mapped[int|None]=mapped_column(Integer,nullable=True)
    camera_observation_id: Mapped[int|None]=mapped_column(ForeignKey('camera_observations.id',ondelete='SET NULL'),nullable=True)
    __table_args__=(UniqueConstraint('patient_id','client_key',name='uq_activity_client_key'),)


class SleepEntry(_CareEntry,Base):
    __tablename__='sleep_entries'
    kind: Mapped[str]=mapped_column(String(10),default='nap')
    ended_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)
    duration_minutes: Mapped[int|None]=mapped_column(Integer,nullable=True)
    location: Mapped[str]=mapped_column(EncryptedText,default='')
    camera_observation_id: Mapped[int|None]=mapped_column(ForeignKey('camera_observations.id',ondelete='SET NULL'),nullable=True)
    __table_args__=(UniqueConstraint('patient_id','client_key',name='uq_sleep_client_key'),)


class Camera(Base):
    """Home camera configuration. Stream URL and credentials are application-encrypted; API responses never return them."""
    __tablename__='cameras'
    id: Mapped[int]=mapped_column(primary_key=True)
    patient_id: Mapped[int]=mapped_column(ForeignKey('patients.id',ondelete='CASCADE'),index=True)
    name: Mapped[str]=mapped_column(EncryptedText)
    room: Mapped[str]=mapped_column(EncryptedText,default='')
    protocol: Mapped[str]=mapped_column(String(10),default='rtsp')
    stream_url: Mapped[str]=mapped_column(EncryptedText)  # without embedded credentials
    username: Mapped[str]=mapped_column(EncryptedText,default='')
    password: Mapped[str]=mapped_column(EncryptedText,default='')
    enabled: Mapped[bool]=mapped_column(Boolean,default=False)
    analysis_mode: Mapped[str]=mapped_column(String(12),default='motion')
    audio_enabled: Mapped[bool]=mapped_column(Boolean,default=False)
    record_clips: Mapped[bool]=mapped_column(Boolean,default=False)
    retention_days: Mapped[int]=mapped_column(Integer,default=7)
    local_network_only: Mapped[bool]=mapped_column(Boolean,default=True)
    cloud_upload_allowed: Mapped[bool]=mapped_column(Boolean,default=False)
    worker_key_hash: Mapped[str|None]=mapped_column(String(64),nullable=True)
    worker_state: Mapped[str]=mapped_column(String(20),default='never_connected')  # never_connected | connected | reconnecting | disconnected | disabled
    worker_seen_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)
    worker_message: Mapped[str]=mapped_column(String(200),default='')
    last_test_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)
    last_test_ok: Mapped[bool|None]=mapped_column(Boolean,nullable=True)
    last_test_detail: Mapped[str]=mapped_column(String(200),default='')
    disabled_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)
    updated_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)


class CameraObservation(Base):
    """Derived, non-medical activity metadata produced by the local worker. Raw video never passes through here."""
    __tablename__='camera_observations'
    id: Mapped[int]=mapped_column(primary_key=True)
    patient_id: Mapped[int]=mapped_column(ForeignKey('patients.id',ondelete='CASCADE'),index=True)
    camera_id: Mapped[int]=mapped_column(ForeignKey('cameras.id',ondelete='CASCADE'),index=True)
    started_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),index=True)
    ended_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)
    local_date: Mapped[date]=mapped_column(Date,index=True)
    kind: Mapped[str]=mapped_column(String(20))
    confidence: Mapped[float]=mapped_column(Float,default=0.0)
    review_status: Mapped[str]=mapped_column(String(20),default='ai_observed')
    corrected_kind: Mapped[str]=mapped_column(String(20),default='')
    review_note: Mapped[str]=mapped_column(EncryptedText,default='')
    reviewed_by: Mapped[int|None]=mapped_column(ForeignKey('users.id',ondelete='SET NULL'),nullable=True)
    reviewed_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)
    clip_storage_name: Mapped[str|None]=mapped_column(String(70),nullable=True)
    clip_deleted_at: Mapped[datetime|None]=mapped_column(DateTime(timezone=True),nullable=True)
    worker_id: Mapped[str]=mapped_column(String(60),default='')
    metrics_json: Mapped[str]=mapped_column(String(400),default='')  # e.g. {"motion_ratio":0.12,"frames":300}
    dedupe_key: Mapped[str]=mapped_column(String(80))
    journal_entry_kind: Mapped[str]=mapped_column(String(10),default='')  # activity | sleep when added to the journal
    journal_entry_id: Mapped[int|None]=mapped_column(Integer,nullable=True)
    created_at: Mapped[datetime]=mapped_column(DateTime(timezone=True),default=utcnow)
    __table_args__=(UniqueConstraint('camera_id','dedupe_key',name='uq_camera_observation_dedupe'),)


ENTRY_MODELS={'fluid':FluidEntry,'diaper':DiaperEntry,'stool':StoolEntry,'medicine':MedicationAdministration,'symptom':SymptomEntry,'activity':ActivityEntry,'sleep':SleepEntry}
