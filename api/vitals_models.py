"""Vitals journal rows (adult-friendly Daily Health fields). Same lifecycle as other journal entries: patient-scoped,
timezone-aware `at` plus `local_date`, idempotent `client_key`, withdrawn-never-deleted, revisions kept."""
from sqlalchemy import Integer, Float, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from .database import Base
from .models import EncryptedText
from .care_models import _CareEntry

class VitalsEntry(_CareEntry,Base):
    __tablename__='vitals_entries'
    systolic: Mapped[int|None]=mapped_column(Integer,nullable=True)
    diastolic: Mapped[int|None]=mapped_column(Integer,nullable=True)
    heart_rate: Mapped[int|None]=mapped_column(Integer,nullable=True)
    spo2: Mapped[int|None]=mapped_column(Integer,nullable=True)
    glucose_mg_dl: Mapped[float|None]=mapped_column(Float,nullable=True)
    glucose_context: Mapped[str]=mapped_column(String(20),default='')   # fasting | post_meal | random | bedtime | ''
    weight_kg: Mapped[float|None]=mapped_column(Float,nullable=True)
    temperature_c: Mapped[float|None]=mapped_column(Float,nullable=True)
    meal: Mapped[str]=mapped_column(EncryptedText,default='')
    __table_args__=(UniqueConstraint('patient_id','client_key',name='uq_vitals_client_key'),)

FIELDS=('systolic','diastolic','heart_rate','spo2','glucose_mg_dl','glucose_context','weight_kg','temperature_c','meal','note')
