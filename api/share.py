"""Share with Doctor: selective export of a profile's records as a ZIP package, a clinician PDF summary, or an
expiring, revocable doctor link that exposes only the explicitly selected records.

Design rules (see the "Share With Doctor Architecture" wiki page):
- The parent sees the exact document list (preview) before anything is generated.
- Condition-specific scopes are resolved from documented links only (episode links, culture observations, lifetime
  fever/antibiotic reports); unrelated records are never pulled in.
- Every statement in the summary carries a provenance label (DOCUMENTED / PARENT REPORTED / OCR UNVERIFIED /
  PRESCRIBED / CONFIRMED ADMINISTERED / UNKNOWN) and a reference to the file inside the package.
- Exports contain no auth tokens, database ids, storage names or keys. Files are named by date, title and bundle ref.
- Doctor links: random 256-bit token (only its hash is stored), optional PBKDF2 PIN, fixed expiry, revocation,
  access counter, view-only / download toggles, noindex headers, package-scoped document access by index.
"""
import csv
import hashlib
import hmac
import io
import json
import re
import secrets
import zipfile
from datetime import datetime, timedelta, timezone, date
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Header, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session as DbSession

from .database import get_db
from .models import (User, Patient, Document, Medicine, MedicationEvent, HospitalVisit, Vaccine, Observation, Admission,
                     TreatmentAdministration, Episode, Growth, Audit, utcnow)
from .lifetime_models import FeverEpisode, AntibioticCourse, AntibioticAdministration
from .share_models import SharePackage, ShareAccessLog, EXPIRY_CHOICES
from .security import require_user, own_patient, hash_password
from .crypto import decrypt
from .storage import read_document
from . import lifetime

router = APIRouter(prefix='/api')

FOLDER = {'lab': 'Labs', 'culture': 'Labs', 'imaging': 'Imaging', 'prescription': 'Prescriptions', 'consultation': 'Prescriptions',
          'hospital': 'Hospital', 'discharge': 'Hospital', 'admission': 'Hospital', 'birth': 'Hospital', 'vaccination_card': 'Vaccinations',
          'bill': 'Other', 'other': 'Other'}
CATEGORY_GROUPS = {'labs': ('lab', 'culture', 'imaging'), 'prescriptions': ('prescription', 'consultation'), 'hospital': ('hospital', 'discharge', 'admission', 'birth'),
                   'vaccinations': ('vaccination_card',), 'other': ('other', 'bill')}
CATEGORY_CHOICES = ('labs', 'prescriptions', 'hospital', 'vaccinations', 'antibiotics', 'fever', 'growth', 'illness', 'medications', 'other', 'daily_care')
PRESETS = {
    'ecoli': ('E. coli-related history', 'Culture reports naming E. coli, the laboratory reports, prescriptions, antibiotic courses, fever events, hospitalization and consultations linked to those illnesses.'),
    'latest_hospitalization': ('Latest hospitalization', 'The most recent admission with its discharge summary and every record dated inside the stay window.'),
    'antibiotics': ('All antibiotic history', 'Every documented antibiotic course and prescription with its source pages and administration records.'),
    'latest_labs': ('Latest laboratory reports', 'Laboratory and culture reports from the most recent two weeks of testing, with every transcribed value.'),
    'vaccination': ('Vaccination record', 'All vaccination card entries with their status and the card pages.'),
    'complete': ('Complete medical record', 'Every stored document and every record, with the full summary.'),
}
ECOLI_RE = re.compile(r'e\.?\s*coli|escherichia', re.I)
MIME_EXT = {'application/pdf': '.pdf', 'image/jpeg': '.jpg', 'image/png': '.png', 'image/heic': '.heic'}


# ------------------------------------------------------------------ request schema
class ShareSpec(BaseModel):
    mode: Literal['complete', 'condition', 'categories', 'dates', 'manual', 'preset'] = 'preset'
    preset: Literal['ecoli', 'latest_hospitalization', 'antibiotics', 'latest_labs', 'vaccination', 'complete'] | None = None
    keyword: str = Field(default='', max_length=80)
    episode_ids: list[int] = Field(default_factory=list, max_length=50)
    categories: list[str] = Field(default_factory=list, max_length=12)
    date_from: date | None = None
    date_to: date | None = None
    document_ids: list[int] = Field(default_factory=list, max_length=500)
    exclude_document_ids: list[int] = Field(default_factory=list, max_length=500)
    include_summary: bool = True
    include_vaccinations: bool | None = None
    include_growth: bool | None = None
    include_pending: bool = True   # pending/unverified rows are included but always labelled; False keeps only verified rows
    daily_care_days: int | None = Field(default=None, ge=1, le=90)
    reason: str = Field(default='', max_length=200)

    @field_validator('categories')
    @classmethod
    def known_categories(cls, value):
        bad = [c for c in value if c not in CATEGORY_CHOICES]
        if bad:
            raise ValueError(f'Unknown categories: {bad}')
        return value


class LinkIn(BaseModel):
    spec: ShareSpec
    label: str = Field(default='Doctor share', max_length=120)
    expiry: Literal['1h', '24h', '3d', '7d'] = '24h'
    pin: str = Field(default='', max_length=12, pattern=r'^(\d{4,12})?$')
    view_only: bool = False
    allow_download: bool = True


# ------------------------------------------------------------------ provenance labels
def obs_label(o):
    return 'DOCUMENTED' if o.verified else 'OCR UNVERIFIED'


def vax_label(v):
    if v.source_type == 'India_UIP_reference':
        return 'REFERENCE ONLY'
    if v.confirmed and v.given_date:
        return 'DOCUMENTED'
    if v.confirmed:
        return 'DOCUMENTED (appointment date)'
    return 'OCR UNVERIFIED'


def med_label(m):
    base = 'PRESCRIBED'
    return base if m.status == 'verified' else base + ' / OCR UNVERIFIED (draft)'


def event_label(e):
    return 'CONFIRMED ADMINISTERED' if e.source == 'copied_from_hospital_document' else 'PARENT REPORTED'


def course_label(c):
    status = {'confirmed_administered': 'CONFIRMED ADMINISTERED', 'prescribed_only': 'PRESCRIBED', 'scheduled': 'PRESCRIBED (scheduled)',
              'unknown': 'UNKNOWN', 'not_received': 'NOT RECEIVED (culture panel entry)'}.get(c.administration_status, 'UNKNOWN')
    if c.origin == 'parent_report':
        status = 'PARENT REPORTED / ' + status
    if c.verification_status != 'verified':
        status += ' / PENDING VERIFICATION'
    if getattr(c, 'administration_evidence', '') == 'statement_only':
        status += ' (hospital statement only; no dated chart)'
    return status


def fever_label(f):
    status = 'PARENT REPORTED' if f.origin == 'parent_report' else 'DOCUMENTED'
    if f.verification_status != 'verified':
        status += ' / PENDING VERIFICATION'
    if f.date_certainty != 'exact':
        status += f' (dates {f.date_certainty})'
    return status


def age_text(dob, on):
    days = (on - dob).days
    if days < 56:
        return f'{days} days'
    if days < 120:
        return f'{days // 7} weeks'
    months = (on.year - dob.year) * 12 + on.month - dob.month - (on.day < dob.day)
    return f'{months} months' if months < 24 else f'{months // 12} years {months % 12} months'


def slug(text, n=40):
    s = re.sub(r'[^A-Za-z0-9]+', '-', str(text or '')).strip('-')
    return (s[:n].rstrip('-') or 'document')


# ------------------------------------------------------------------ scope resolution
def _in_range(d, spec):
    if d is None:
        return not (spec.date_from or spec.date_to)
    return (not spec.date_from or d >= spec.date_from) and (not spec.date_to or d <= spec.date_to)


def resolve(db, p, spec):
    """Return the resolved selection: ordered document ids and record ids, derived only from documented links."""
    pid = p.id
    docs = {d.id: d for d in db.query(Document).filter_by(patient_id=pid)}
    sel = {k: set() for k in ('documents', 'episodes', 'admissions', 'medicines', 'observations', 'vaccines', 'growth', 'fevers', 'courses', 'visits', 'administrations')}
    sel['mode'] = spec.mode
    preset = spec.preset if spec.mode == 'preset' else None
    today = utcnow().astimezone(ZoneInfo(p.tz)).date()
    reason = spec.reason or (PRESETS[preset][0] if preset else {'complete': 'Complete medical record', 'condition': spec.keyword or 'Selected illness episodes', 'categories': 'Selected categories',
                                                                 'dates': 'Selected date range', 'manual': 'Hand-picked documents'}[spec.mode])
    include_vax = spec.include_vaccinations
    include_growth = spec.include_growth

    def add_doc(doc_id):
        if doc_id and doc_id in docs:
            sel['documents'].add(doc_id)

    def add_records_for_docs(doc_ids):
        for o in db.query(Observation).filter(Observation.patient_id == pid, Observation.source_document_id.in_(doc_ids)):
            sel['observations'].add(o.id)
        for m in db.query(Medicine).filter(Medicine.patient_id == pid, Medicine.source_document_id.in_(doc_ids)):
            sel['medicines'].add(m.id)
        for v in db.query(Vaccine).filter(Vaccine.patient_id == pid, Vaccine.source_document_id.in_(doc_ids)):
            sel['vaccines'].add(v.id)
        for a in db.query(Admission).filter(Admission.patient_id == pid).filter((Admission.source_document_id.in_(doc_ids)) | (Admission.discharge_document_id.in_(doc_ids))):
            sel['admissions'].add(a.id)
        for c in db.query(AntibioticCourse).filter(AntibioticCourse.patient_id == pid, AntibioticCourse.source_document_id.in_(doc_ids)):
            sel['courses'].add(c.id)
        for f in db.query(FeverEpisode).filter(FeverEpisode.patient_id == pid, FeverEpisode.source_document_id.in_(doc_ids)):
            sel['fevers'].add(f.id)
        for g in db.query(Growth).filter(Growth.patient_id == pid, Growth.source_document_id.in_(doc_ids)):
            sel['growth'].add(g.id)

    def add_episode(e):
        sel['episodes'].add(e.id)
        add_doc(e.source_document_id)
        for link in json.loads(e.links_json or '[]'):
            kind, rid = link.get('kind'), link.get('id')
            if kind == 'document':
                add_doc(rid)
            elif kind == 'medicine':
                m = db.get(Medicine, rid)
                if m and m.patient_id == pid:
                    sel['medicines'].add(m.id); add_doc(m.source_document_id)
            elif kind == 'observation':
                o = db.get(Observation, rid)
                if o and o.patient_id == pid:
                    sel['observations'].add(o.id); add_doc(o.source_document_id)
            elif kind == 'admission':
                a = db.get(Admission, rid)
                if a and a.patient_id == pid:
                    sel['admissions'].add(a.id); add_doc(a.source_document_id); add_doc(a.discharge_document_id)
            elif kind == 'visit':
                v = db.get(HospitalVisit, rid)
                if v and v.patient_id == pid:
                    sel['visits'].add(v.id); add_doc(v.source_document_id)
        for f in db.query(FeverEpisode).filter_by(patient_id=pid, illness_episode_id=e.id):
            sel['fevers'].add(f.id); add_doc(f.source_document_id)
        for c in db.query(AntibioticCourse).filter_by(patient_id=pid, illness_episode_id=e.id):
            sel['courses'].add(c.id); add_doc(c.source_document_id)
        for a in db.query(Admission).filter_by(patient_id=pid).filter(Admission.admitted_on >= e.started_on, Admission.admitted_on <= (e.ended_on or e.started_on)):
            sel['admissions'].add(a.id); add_doc(a.source_document_id); add_doc(a.discharge_document_id)

    def add_window(start, end):
        for d in docs.values():
            if d.record_date and start <= d.record_date <= end:
                add_doc(d.id)
        for o in db.query(Observation).filter(Observation.patient_id == pid, Observation.record_date >= start, Observation.record_date <= end):
            sel['observations'].add(o.id); add_doc(o.source_document_id)
        for m in db.query(Medicine).filter_by(patient_id=pid):
            when = m.prescription_date or m.start_date
            if when and start <= when <= end:
                sel['medicines'].add(m.id); add_doc(m.source_document_id)
        for c in db.query(AntibioticCourse).filter_by(patient_id=pid):
            when = c.actual_start or c.prescribed_on or c.planned_start
            if when and start <= when <= end:
                sel['courses'].add(c.id); add_doc(c.source_document_id)
        for f in db.query(FeverEpisode).filter_by(patient_id=pid):
            if f.first_documented_on and start <= f.first_documented_on <= end:
                sel['fevers'].add(f.id); add_doc(f.source_document_id)
        for v in db.query(HospitalVisit).filter_by(patient_id=pid):
            if start <= v.planned_at.date() <= end:
                sel['visits'].add(v.id); add_doc(v.source_document_id)
        for t in db.query(TreatmentAdministration).filter_by(patient_id=pid):
            if start <= t.occurred_at.date() <= end:
                sel['administrations'].add(t.id); add_doc(t.source_document_id)
        for e in db.query(Episode).filter_by(patient_id=pid):
            if e.started_on <= end and (e.ended_on or e.started_on) >= start:
                sel['episodes'].add(e.id); add_doc(e.source_document_id)

    if spec.mode == 'complete' or preset == 'complete':
        for d in docs.values():
            if _in_range(d.record_date, spec) or d.record_date is None:
                add_doc(d.id)
        for model, key, dcol in ((Episode, 'episodes', 'started_on'), (Admission, 'admissions', 'admitted_on'), (Medicine, 'medicines', 'prescription_date'), (Observation, 'observations', 'record_date'),
                                 (Vaccine, 'vaccines', None), (Growth, 'growth', 'record_date'), (FeverEpisode, 'fevers', 'first_documented_on'), (AntibioticCourse, 'courses', 'prescribed_on'),
                                 (HospitalVisit, 'visits', None), (TreatmentAdministration, 'administrations', None)):
            for row in db.query(model).filter_by(patient_id=pid):
                when = getattr(row, dcol, None) if dcol else None
                if not (spec.date_from or spec.date_to) or _in_range(when, spec) or when is None:
                    sel[key].add(row.id)
        include_vax = True if include_vax is None else include_vax
        include_growth = True if include_growth is None else include_growth
    elif spec.mode == 'manual':
        for did in spec.document_ids:
            add_doc(did)
        add_records_for_docs(list(sel['documents']) or [0])
    elif spec.mode == 'dates':
        if not (spec.date_from or spec.date_to):
            raise HTTPException(422, 'Choose at least one date')
        add_window(spec.date_from or date(1900, 1, 1), spec.date_to or today)
        include_vax = True if include_vax is None else include_vax
    elif spec.mode == 'categories':
        cats = set(spec.categories)
        if not cats:
            raise HTTPException(422, 'Choose at least one category')
        for key, groups in CATEGORY_GROUPS.items():
            if key in cats:
                for d in docs.values():
                    if d.category in groups and _in_range(d.record_date, spec):
                        add_doc(d.id)
        if 'labs' in cats:
            for o in db.query(Observation).filter_by(patient_id=pid):
                if _in_range(o.record_date, spec):
                    sel['observations'].add(o.id); add_doc(o.source_document_id)
        if 'prescriptions' in cats or 'medications' in cats:
            for m in db.query(Medicine).filter_by(patient_id=pid):
                if _in_range(m.prescription_date or m.start_date, spec):
                    sel['medicines'].add(m.id); add_doc(m.source_document_id)
        if 'hospital' in cats:
            for a in db.query(Admission).filter_by(patient_id=pid):
                if _in_range(a.admitted_on, spec):
                    sel['admissions'].add(a.id); add_doc(a.source_document_id); add_doc(a.discharge_document_id)
            for v in db.query(HospitalVisit).filter_by(patient_id=pid):
                if _in_range(v.planned_at.date(), spec):
                    sel['visits'].add(v.id); add_doc(v.source_document_id)
            for t in db.query(TreatmentAdministration).filter_by(patient_id=pid):
                if _in_range(t.occurred_at.date(), spec):
                    sel['administrations'].add(t.id); add_doc(t.source_document_id)
        if 'vaccinations' in cats:
            include_vax = True
        if 'antibiotics' in cats:
            for c in db.query(AntibioticCourse).filter_by(patient_id=pid):
                if _in_range(c.actual_start or c.prescribed_on or c.planned_start, spec):
                    sel['courses'].add(c.id); add_doc(c.source_document_id)
            for m in db.query(Medicine).filter_by(patient_id=pid):
                if lifetime.classify_drug(m.generic_name or m.name).get('is_antibiotic') and _in_range(m.prescription_date or m.start_date, spec):
                    sel['medicines'].add(m.id); add_doc(m.source_document_id)
        if 'fever' in cats:
            for f in db.query(FeverEpisode).filter_by(patient_id=pid):
                if _in_range(f.first_documented_on, spec):
                    sel['fevers'].add(f.id); add_doc(f.source_document_id)
        if 'growth' in cats:
            include_growth = True
        if 'illness' in cats:
            for e in db.query(Episode).filter_by(patient_id=pid):
                if _in_range(e.started_on, spec):
                    add_episode(e)
    elif spec.mode == 'condition' or preset == 'ecoli':
        episodes = {e.id: e for e in db.query(Episode).filter_by(patient_id=pid)}
        chosen = set(i for i in spec.episode_ids if i in episodes)
        keyword = spec.keyword.strip()
        if preset == 'ecoli' or ECOLI_RE.search(keyword):
            report = lifetime.ecoli_report(db, pid, 'all')
            culture_obs = set()
            for item in report.get('laboratory_findings', []):
                oid = item['observation']['id']
                culture_obs.add(oid)
                sel['observations'].add(oid)
                add_doc(item['observation'].get('source_document_id'))
                for c in item.get('antibiotics_prescribed_or_administered_within_window', []):
                    cid = c.get('course_id') or c.get('id')
                    if cid:
                        sel['courses'].add(cid)
                for f in item.get('fever_episodes_within_14_days', []):
                    sel['fevers'].add(f['id'])
            for ill in lifetime.illness_report(db, pid, 'all').get('illnesses', []):
                e = ill['episode']
                text = ' '.join([e.get('title', ''), e.get('documented_diagnosis', ''), e.get('symptoms', ''), e.get('notes', '')])
                lab_ids = {o['id'] for o in ill.get('laboratory', [])}
                if lab_ids & culture_obs or ECOLI_RE.search(text):
                    chosen.add(e['id'])
        elif keyword:
            kw = keyword.casefold()
            for e in episodes.values():
                if kw in ' '.join([e.title, e.documented_diagnosis, e.symptoms, e.notes]).casefold():
                    chosen.add(e.id)
            for o in db.query(Observation).filter_by(patient_id=pid):
                if kw in ' '.join([o.test_name, o.organism or '', o.culture_findings or '']).casefold():
                    sel['observations'].add(o.id); add_doc(o.source_document_id)
        if not chosen and not sel['observations']:
            raise HTTPException(422, 'No documented records match this condition yet. Choose episodes or documents manually.')
        for eid in chosen:
            add_episode(episodes[eid])
        for c in list(sel['courses']):
            course = db.get(AntibioticCourse, c)
            if course and course.patient_id == pid:
                add_doc(course.source_document_id)
                for adm in db.query(AntibioticAdministration).filter_by(course_id=course.id):
                    add_doc(adm.source_document_id)
            else:
                sel['courses'].discard(c)
        for f in list(sel['fevers']):
            fever = db.get(FeverEpisode, f)
            if fever and fever.patient_id == pid:
                add_doc(fever.source_document_id)
            else:
                sel['fevers'].discard(f)
    elif preset == 'latest_hospitalization':
        adm = db.query(Admission).filter_by(patient_id=pid).order_by(Admission.admitted_on.desc()).first()
        if not adm:
            raise HTTPException(422, 'No hospital admission is recorded yet.')
        sel['admissions'].add(adm.id); add_doc(adm.source_document_id); add_doc(adm.discharge_document_id)
        add_window(adm.admitted_on - timedelta(days=2), (adm.discharged_on or today) + timedelta(days=7))
    elif preset == 'antibiotics':
        for c in db.query(AntibioticCourse).filter_by(patient_id=pid):
            sel['courses'].add(c.id); add_doc(c.source_document_id)
            for adm in db.query(AntibioticAdministration).filter_by(course_id=c.id):
                add_doc(adm.source_document_id)
        for m in db.query(Medicine).filter_by(patient_id=pid):
            if lifetime.classify_drug(m.generic_name or m.name).get('is_antibiotic'):
                sel['medicines'].add(m.id); add_doc(m.source_document_id)
        if not sel['courses'] and not sel['medicines']:
            raise HTTPException(422, 'No antibiotic course or prescription is recorded yet.')
    elif preset == 'latest_labs':
        lab_docs = [d for d in docs.values() if d.category in ('lab', 'culture') and d.record_date]
        if not lab_docs:
            raise HTTPException(422, 'No dated laboratory report is stored yet.')
        latest = max(d.record_date for d in lab_docs)
        for d in lab_docs:
            if d.record_date >= latest - timedelta(days=14):
                add_doc(d.id)
        for o in db.query(Observation).filter(Observation.patient_id == pid, Observation.record_date >= latest - timedelta(days=14)):
            sel['observations'].add(o.id); add_doc(o.source_document_id)
    elif preset == 'vaccination':
        include_vax = True
        for d in docs.values():
            if d.category == 'vaccination_card':
                add_doc(d.id)
    if include_vax:
        for v in db.query(Vaccine).filter_by(patient_id=pid):
            if v.source_type != 'India_UIP_reference':
                sel['vaccines'].add(v.id); add_doc(v.source_document_id)
    if include_growth:
        for g in db.query(Growth).filter_by(patient_id=pid):
            if _in_range(g.record_date, spec):
                sel['growth'].add(g.id); add_doc(g.source_document_id)
    if not spec.include_pending:
        sel['observations'] = {i for i in sel['observations'] if db.get(Observation, i).verified}
        sel['medicines'] = {i for i in sel['medicines'] if db.get(Medicine, i).status == 'verified'}
        sel['vaccines'] = {i for i in sel['vaccines'] if db.get(Vaccine, i).confirmed}
        sel['fevers'] = {i for i in sel['fevers'] if db.get(FeverEpisode, i).verification_status == 'verified'}
        sel['courses'] = {i for i in sel['courses'] if db.get(AntibioticCourse, i).verification_status == 'verified'}
    for did in spec.exclude_document_ids:
        sel['documents'].discard(did)
    # Daily care (optional, provided by the care module when present)
    sel['daily_care_days'] = spec.daily_care_days if ('daily_care' in spec.categories or spec.daily_care_days) else None
    ordered = sorted(sel['documents'], key=lambda i: ((docs[i].record_date or date(1900, 1, 1)).isoformat(), i))
    out = {k: sorted(v) for k, v in sel.items() if isinstance(v, set)}
    out.update({'documents': ordered, 'mode': spec.mode, 'preset': preset, 'reason': reason, 'include_pending': spec.include_pending, 'include_summary': spec.include_summary,
                'date_from': spec.date_from.isoformat() if spec.date_from else None, 'date_to': spec.date_to.isoformat() if spec.date_to else None,
                'include_vaccinations': bool(include_vax), 'include_growth': bool(include_growth), 'daily_care_days': sel['daily_care_days']})
    return out


def entries_for(db, sel):
    """Package file names for the selected documents (no database ids; date, title and bundle ref only)."""
    used, entries = set(), []
    for n, did in enumerate(sel['documents'], 1):
        d = db.get(Document, did)
        if not d:
            continue
        ext = MIME_EXT.get(d.mime, '.bin')
        base = f"{FOLDER.get(d.category, 'Other')}/{(d.record_date.isoformat() if d.record_date else 'undated')}_{slug(d.title)}{('_' + d.source_ref) if d.source_ref else ''}"
        path, k = base + ext, 2
        while path in used:
            path, k = f'{base}_{k}{ext}', k + 1
        used.add(path)
        entries.append({'n': n, 'doc': d, 'path': path})
    return entries


def preview_payload(db, p, sel):
    entries = entries_for(db, sel)
    docs = [{'id': e['doc'].id, 'n': e['n'], 'title': e['doc'].title, 'category': e['doc'].category, 'folder': e['path'].split('/')[0], 'file': e['path'], 'record_date': e['doc'].record_date.isoformat() if e['doc'].record_date else None,
             'source_ref': e['doc'].source_ref, 'pages': e['doc'].page_count, 'mime': e['doc'].mime, 'reviewed': e['doc'].extraction_status == 'reviewed'} for e in entries]
    counts = {k: len(sel[k]) for k in ('episodes', 'admissions', 'medicines', 'observations', 'vaccines', 'growth', 'fevers', 'courses', 'visits', 'administrations')}
    unverified = sum(1 for i in sel['observations'] if not db.get(Observation, i).verified) + sum(1 for i in sel['medicines'] if db.get(Medicine, i).status != 'verified') + \
        sum(1 for i in sel['vaccines'] if not db.get(Vaccine, i).confirmed) + sum(1 for i in sel['fevers'] if db.get(FeverEpisode, i).verification_status != 'verified') + \
        sum(1 for i in sel['courses'] if db.get(AntibioticCourse, i).verification_status != 'verified')
    return {'reason': sel['reason'], 'mode': sel['mode'], 'preset': sel['preset'], 'documents': docs, 'counts': counts, 'unverified_rows': unverified,
            'folders': sorted({d['folder'] for d in docs}), 'include_summary': sel['include_summary'],
            'warnings': ['Only the documents listed here will be included. Records are copied as documented; nothing is interpreted.',
                         'Rows marked OCR UNVERIFIED or PENDING VERIFICATION are transcriptions the caregiver has not yet checked against the original page.']}


# ------------------------------------------------------------------ PDF summary
def _latin(text):
    return str(text).replace('—', '-').replace('–', '-').replace('→', '->').replace('✓', '[x]').replace('×', 'x').replace('’', "'").replace('‘', "'") \
        .replace('“', '"').replace('”', '"').replace('…', '...').replace('•', '-').encode('latin-1', 'replace').decode('latin-1')


class Pdf:
    def __init__(self, footer):
        import fitz
        self.fitz = fitz
        self.doc = fitz.open()
        self.footer = footer
        self.W, self.H, self.M = 595, 842, 48
        self.y = None
        self.page = None
        self.links = []
        self.new_page()

    def new_page(self):
        self.page = self.doc.new_page(width=self.W, height=self.H)
        self.y = self.M
        self.page.insert_text((self.M, self.H - 24), _latin(f'{self.footer}  -  page {self.doc.page_count}'), fontsize=7.5, fontname='helv', color=(0.45, 0.5, 0.5))

    def ensure(self, h):
        if self.y + h > self.H - 44:
            self.new_page()

    def wrap(self, text, size, width):
        words, lines, cur = _latin(text).split(' '), [], ''
        for w in words:
            trial = (cur + ' ' + w).strip()
            if self.fitz.get_text_length(trial, fontname='helv', fontsize=size) <= width:
                cur = trial
            else:
                if cur:
                    lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
        return lines or ['']

    def text(self, text, size=9.5, bold=False, color=(0.13, 0.19, 0.22), indent=0, gap=3, link=None):
        width = self.W - 2 * self.M - indent
        lines = self.wrap(text, size, width)
        lh = size * 1.38
        self.ensure(lh * len(lines) + gap)
        font = 'hebo' if bold else 'helv'
        top = self.y
        for line in lines:
            self.page.insert_text((self.M + indent, self.y + size), line, fontsize=size, fontname=font, color=color)
            self.y += lh
        if link:
            rect = self.fitz.Rect(self.M + indent, top, self.W - self.M, self.y)
            self.page.insert_link({'kind': self.fitz.LINK_URI, 'from': rect, 'uri': link})
        self.y += gap

    def h1(self, text):
        self.y += 4; self.text(text, 17, True, (0.04, 0.42, 0.38), gap=6)

    def h2(self, text):
        self.y += 8; self.text(text.upper(), 9, True, (0.45, 0.5, 0.5), gap=2)
        self.ensure(4); self.page.draw_line((self.M, self.y), (self.W - self.M, self.y), color=(0.86, 0.88, 0.85), width=0.6); self.y += 6

    def bullet(self, label, text, ref=None, link=None):
        tag = f'[{label}] ' if label else ''
        self.text(f'- {tag}{text}' + (f'  ->  {ref}' if ref else ''), 9.2, indent=6, gap=2, link=link)

    def note(self, text):
        self.text(text, 8.4, False, (0.4, 0.45, 0.45), gap=3)

    def bytes(self):
        return self.doc.tobytes(garbage=3, deflate=True)


def build_summary_pdf(db, p, sel, entries, generated, care_section=None):
    ref_of = {e['doc'].id: e for e in entries}

    def ref(doc_id, page=None):
        e = ref_of.get(doc_id)
        if not e:
            return ('not in package', None)
        return (f"{e['path']}" + (f' p.{page}' if page and page > 1 else ''), e['path'])

    tz = ZoneInfo(p.tz)
    pdf = Pdf('HealthBook AI - records as documented by the family and their clinicians; not a diagnosis or treatment recommendation')
    pdf.h1('Medical Summary for Clinician')
    pdf.text(f'Profile: {p.name}', 11, True)
    pdf.text(f'Date of birth: {p.dob.isoformat()}    Age at export: {age_text(p.dob, generated.date())}', 10)
    pdf.text(f'Export generated: {generated.strftime("%d %b %Y, %H:%M")} ({p.tz})', 10)
    pdf.text(f'Reason / selected history: {sel["reason"]}', 10, True)
    if sel.get('date_from') or sel.get('date_to'):
        pdf.text(f'Date range: {sel.get("date_from") or "start"} to {sel.get("date_to") or "today"}', 10)
    pdf.text(f'Package contents: {len(entries)} original documents; {len(sel["observations"])} laboratory values; {len(sel["medicines"])} prescriptions; {len(sel["courses"])} antibiotic courses; '
             f'{len(sel["fevers"])} fever episodes; {len(sel["admissions"])} admissions; {len(sel["episodes"])} illness episodes.', 9.5)
    pdf.h2('How to read the labels')
    for k, v in [('DOCUMENTED', 'written on a clinician document or verified by the caregiver against the original page'), ('PARENT REPORTED', 'entered by the caregiver from memory or a home record; not clinician-documented'),
                 ('OCR UNVERIFIED', 'transcribed from a scan and not yet checked against the original; treat as a pointer to the page, not as fact'), ('PRESCRIBED', 'a prescription or discharge plan; it does not prove the medicine was given'),
                 ('CONFIRMED ADMINISTERED', 'a dated administration record (chart or hospital document) exists'), ('UNKNOWN', 'administration not documented either way'), ('PENDING VERIFICATION', 'a transcribed lifetime-health row awaiting the caregiver check')]:
        pdf.bullet(k, v)
    pdf.note('Every statement below ends with the package file it comes from (click to open when the package is extracted alongside this PDF). Culture susceptibility entries are never counted as administered medicines.')

    episodes = [db.get(Episode, i) for i in sel['episodes']]
    if episodes:
        pdf.h2(f'Illness episodes ({len(episodes)})')
        for e in sorted(episodes, key=lambda x: x.started_on):
            r, link = ref(e.source_document_id, e.source_page)
            pdf.bullet('DOCUMENTED', f'{e.started_on} to {e.ended_on or "end not documented"}: {e.title}. Documented diagnosis: {e.documented_diagnosis or "not documented"}. Symptoms as written: {e.symptoms or "not documented"}.', r, link)
    fevers = [db.get(FeverEpisode, i) for i in sel['fevers']]
    if fevers:
        pdf.h2(f'Fever history ({len(fevers)})')
        for f in sorted(fevers, key=lambda x: (x.first_documented_on or date(1900, 1, 1))):
            r, link = ref(f.source_document_id, f.source_page)
            temp = f'{f.highest_temperature} deg{f.temperature_unit}' if f.highest_temperature else 'temperature not recorded'
            pdf.bullet(fever_label(f), f'{f.first_documented_on or "date not documented"}{(" to " + f.last_documented_on.isoformat()) if f.last_documented_on and f.last_documented_on != f.first_documented_on else ""}: {f.title}; {temp}; '
                       f'duration as written: {f.duration_text or (str(f.documented_fever_days) + " day(s)" if f.documented_fever_days is not None else "not stated")}; symptoms: {f.symptoms or "not documented"}; '
                       f'documented diagnosis: {f.documented_diagnosis or "none"}; hospitalized: {"yes" if f.hospitalized else "no"}.', r, link)
    admissions = [db.get(Admission, i) for i in sel['admissions']]
    if admissions:
        pdf.h2(f'Hospitalizations ({len(admissions)})')
        for a in sorted(admissions, key=lambda x: x.admitted_on):
            r, link = ref(a.source_document_id, a.source_page)
            r2, _ = ref(a.discharge_document_id)
            pdf.bullet('DOCUMENTED', f'{a.admitted_on} to {a.discharged_on or "discharge not documented"} at {a.facility}; doctor {a.doctor or "not documented"}; documented diagnosis: {a.diagnosis or "not documented"}. '
                       f'Recorded plan: {(a.treatment_plan or "")[:400]}' + (f' Discharge summary: {r2}.' if a.discharge_document_id else ''), r, link)
    obs = [db.get(Observation, i) for i in sel['observations']]
    if obs:
        pdf.h2(f'Laboratory findings and cultures ({len(obs)} values)')
        groups = {}
        for o in obs:
            groups.setdefault((o.record_date, o.source_document_id), []).append(o)
        for (when, did), rows in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1] or 0)):
            r, link = ref(did, rows[0].source_page)
            pdf.text(f'{when}  -  {r}', 9, True, indent=6, gap=1, link=link)
            for o in sorted(rows, key=lambda x: x.id):
                line = f'{o.test_name}: {o.value} {o.unit}'.strip() + (f' (specimen {o.specimen})' if o.specimen else '') + (f'; printed reference {o.reference_range}' if o.reference_range else '')
                if o.organism or o.culture_findings or o.susceptibility:
                    line += f'. Organism as reported: {o.organism or "see report"}; findings: {o.culture_findings or "see report"}; susceptibility as printed: {o.susceptibility or "not transcribed"}'
                pdf.bullet(obs_label(o), line)
    meds = [db.get(Medicine, i) for i in sel['medicines']]
    if meds:
        pdf.h2(f'Prescribed medication ({len(meds)})')
        for m in sorted(meds, key=lambda x: (x.prescription_date or x.start_date or date(1900, 1, 1))):
            r, link = ref(m.source_document_id, m.source_page)
            pdf.bullet(med_label(m), f'{m.prescription_date or m.start_date or "date not documented"}: {m.name}' + (f' ({m.generic_name})' if m.generic_name else '') + f'; {m.course_type.replace("_", " ")}; '
                       f'written directions: {m.instructions or "not transcribed"}; dose {m.dosage or "-"} {m.dose_unit or ""}; route {m.route or "not documented"}; frequency {m.frequency or "not documented"}; '
                       f'planned {m.start_date or "?"} to {m.end_date or "?"}; actual {m.actual_start_date or "unknown"} to {m.actual_stop_date or "unknown"}.', r, link)
    given = []
    for i in sel['administrations']:
        t = db.get(TreatmentAdministration, i)
        r, link = ref(t.source_document_id, t.source_page)
        given.append((t.occurred_at.isoformat(), 'CONFIRMED ADMINISTERED', f'{t.occurred_at:%Y-%m-%d %H:%M} UTC: {t.medication}; recorded dose {t.recorded_dose or "-"}; route {t.recorded_route or "-"}.', r, link))
    for mid in sel['medicines']:
        for e in db.query(MedicationEvent).filter_by(medicine_id=mid):
            if e.event_type == 'given':
                m = db.get(Medicine, mid)
                r, link = ref(e.source_document_id or m.source_document_id, e.source_page)
                given.append((e.occurred_at.isoformat(), event_label(e), f'{e.occurred_at:%Y-%m-%d %H:%M}: {m.name} given; {e.note or ""}'.strip(), r, link))
    for cid in sel['courses']:
        for adm in db.query(AntibioticAdministration).filter_by(course_id=cid):
            c = db.get(AntibioticCourse, cid)
            r, link = ref(adm.source_document_id, adm.source_page)
            given.append((adm.administered_on.isoformat(), 'CONFIRMED ADMINISTERED' if adm.source != 'caregiver_report' else 'PARENT REPORTED', f'{adm.administered_on} {adm.administered_time}: {c.generic_name} {adm.dose} {adm.route}; source {adm.source.replace("_", " ")}.', r, link))
    pdf.h2(f'Documented administered medication ({len(given)})')
    if given:
        for _, label, text, r, link in sorted(given):
            pdf.bullet(label, text, r, link)
    else:
        pdf.note('No dated administration record is included. Prescriptions above do not establish that doses were given.')
    courses = [db.get(AntibioticCourse, i) for i in sel['courses']]
    if courses:
        pdf.h2(f'Antibiotic exposure ({len(courses)} courses)')
        for c in sorted(courses, key=lambda x: (x.actual_start or x.prescribed_on or x.planned_start or date(1900, 1, 1))):
            r, link = ref(c.source_document_id, c.source_page)
            days = len(lifetime.course_exposure(c, db.query(AntibioticAdministration).filter_by(course_id=c.id).all())) if hasattr(lifetime, 'course_exposure') else 0
            pdf.bullet(course_label(c), f'{c.actual_start or c.prescribed_on or c.planned_start or "date not documented"}: {c.generic_name}' + (f' ({c.brand_name})' if c.brand_name else '') +
                       f'; {c.route}; {c.setting.replace("_", " ")}; dose {c.dose or "-"} {c.frequency or ""}; prescribed duration {c.prescribed_duration_text or (str(c.prescribed_duration_days) + " days" if c.prescribed_duration_days else "not documented")}; '
                       f'actual {c.actual_start or "unknown"} to {c.actual_stop or "unknown"}; confirmed exposure days: {days}; indication: {c.indication or "not documented"}.' + (f' Caregiver note: {c.needs_parent_check}' if c.needs_parent_check else ''), r, link)
        try:
            for w in lifetime.antibiotic_report(db, p.id, 'all').get('warnings', []):
                pdf.note(w)
        except Exception:
            pass
    vaccines = [db.get(Vaccine, i) for i in sel['vaccines']]
    if vaccines:
        pdf.h2(f'Vaccinations ({len(vaccines)})')
        for v in sorted(vaccines, key=lambda x: (x.given_date or x.due_date or date(2100, 1, 1))):
            r, link = ref(v.source_document_id, v.source_page)
            pdf.bullet(vax_label(v), f'{v.name} {v.dose_label}: given {v.given_date or "not recorded"}; due/appointment {v.due_date or "-"}; source {v.source_type.replace("_", " ")}.', r, link)
    growth = [db.get(Growth, i) for i in sel['growth']]
    if growth:
        pdf.h2(f'Growth measurements ({len(growth)})')
        for g in sorted(growth, key=lambda x: x.record_date):
            r, link = ref(g.source_document_id, g.source_page)
            pdf.bullet('DOCUMENTED' if g.source_document_id else 'PARENT REPORTED', f'{g.record_date} (age {age_text(p.dob, g.record_date)}): weight {g.weight_kg or "-"} kg; length {g.length_cm or "-"} cm; head {g.head_cm or "-"} cm; {g.milestone or ""}.', r, link)
        pdf.note('No percentile interpretation is included; values are as recorded.')
    if care_section:
        pdf.h2(care_section.get('title', 'Daily care'))
        for line in care_section.get('lines', []):
            pdf.bullet(line.get('label', 'PARENT REPORTED'), line.get('text', ''))
    pending = [f'{o.record_date}: {o.test_name} {o.value} {o.unit}' for o in obs if not o.verified] + [f'{m.name} ({m.course_type.replace("_", " ")})' for m in meds if m.status != 'verified'] + \
        [f'{v.name} {v.dose_label}' for v in vaccines if not v.confirmed] + [f'Fever: {f.title}' for f in fevers if f.verification_status != 'verified'] + [f'Antibiotic: {c.generic_name}' for c in courses if c.verification_status != 'verified']
    pdf.h2(f'Pending or unverified information ({len(pending)})')
    if pending:
        pdf.note('These rows were transcribed from scans or entered by the caregiver and have not been verified against the original page. The original pages are in the package.')
        for item in pending[:80]:
            pdf.bullet('OCR UNVERIFIED', item)
    else:
        pdf.note('Every included row has been verified against its source page.')
    try:
        warnings = list(dict.fromkeys(lifetime.fever_report(db, p.id, 'all').get('warnings', []) + lifetime.illness_report(db, p.id, 'all').get('warnings', [])))
    except Exception:
        warnings = []
    if warnings:
        pdf.h2('Statements that apply to this summary')
        for w in warnings:
            pdf.note(w)
    pdf.h2(f'Document index ({len(entries)})')
    for e in entries:
        d = e['doc']
        pdf.bullet(f'{e["n"]:03d}', f'{d.record_date or "undated"}  {d.title}  ({d.category.replace("_", " ")}{", " + d.source_ref if d.source_ref else ""}; {d.page_count} page(s))', e['path'], e['path'])
    return pdf.bytes()


def build_index_html(p, sel, entries, generated, pdf_included):
    rows = ''.join(f'<tr><td>{e["n"]:03d}</td><td>{(e["doc"].record_date or "undated")}</td><td>{_esc(e["doc"].title)}</td><td>{_esc(e["doc"].category.replace("_", " "))}</td><td>{_esc(e["doc"].source_ref or "")}</td><td><a href="{_esc(e["path"])}">{_esc(e["path"])}</a></td></tr>' for e in entries)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="robots" content="noindex,nofollow"><title>HealthBook AI share package</title>
<style>body{{font:15px/1.5 system-ui,sans-serif;color:#223139;background:#f7f5f0;margin:0;padding:28px}}main{{max-width:980px;margin:auto;background:#fff;border:1px solid #e3e5dd;border-radius:16px;padding:28px}}h1{{margin:0 0 4px;color:#0a8c7f}}table{{border-collapse:collapse;width:100%;font-size:13px}}th,td{{text-align:left;padding:8px 10px;border-bottom:1px solid #eceee7;vertical-align:top}}th{{font-size:11px;text-transform:uppercase;letter-spacing:.6px;color:#75848a}}.note{{background:#fbefd9;border-radius:10px;padding:10px 14px;font-size:13px;margin:14px 0}}</style></head>
<body><main><h1>Medical records shared by the family</h1><p><b>Profile:</b> {_esc(p.name)} &middot; <b>DOB:</b> {p.dob} &middot; <b>Age at export:</b> {age_text(p.dob, generated.date())}<br><b>Generated:</b> {generated:%d %b %Y %H:%M} ({_esc(p.tz)}) &middot; <b>Reason:</b> {_esc(sel['reason'])}</p>
{'<p><a href="Doctor_Summary.pdf"><b>Open the clinician summary (PDF)</b></a></p>' if pdf_included else ''}
<div class="note">Records are shared as documented by the family and their clinicians. Labels inside the summary distinguish documented, parent-reported, unverified-transcription, prescribed and confirmed-administered information. This package is not a diagnosis.</div>
<table><thead><tr><th>#</th><th>Date</th><th>Title</th><th>Type</th><th>Ref</th><th>File</th></tr></thead><tbody>{rows}</tbody></table></main></body></html>"""


def _esc(v):
    return str(v or '').replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


def care_section_for(db, p, sel):
    days = sel.get('daily_care_days')
    if not days:
        return None
    try:
        from .care import care_export
    except Exception:
        return None
    try:
        end = utcnow().astimezone(ZoneInfo(p.tz)).date()
        data = care_export(db, p.id, end - timedelta(days=days), end, include_photos=False)
        lines = [{'label': 'PARENT REPORTED', 'text': s} for s in str(data.get('summary_text', '')).splitlines() if s.strip()][:120]
        return {'title': f'Daily care (last {days} days, parent journal)', 'lines': lines}
    except Exception:
        return None


def build_zip(db, p, sel):
    generated = utcnow().astimezone(ZoneInfo(p.tz))
    entries = entries_for(db, sel)
    stamp = generated.strftime('%Y-%m-%d')
    root = f'HealthBook_Share_{stamp}/'
    buf = io.BytesIO()
    hashes = {}
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as archive:
        for e in entries:
            raw = decrypt(read_document(e['doc'].storage_name))
            hashes[e['path']] = hashlib.sha256(raw).hexdigest()
            archive.writestr(root + e['path'], raw)
        pdf = build_summary_pdf(db, p, sel, entries, generated, care_section_for(db, p, sel)) if sel.get('include_summary', True) else None
        if pdf:
            archive.writestr(root + 'Doctor_Summary.pdf', pdf)
        archive.writestr(root + 'INDEX.html', build_index_html(p, sel, entries, generated, bool(pdf)))
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(['file', 'folder', 'record_date', 'title', 'type', 'bundle_ref', 'pages', 'sha256'])
        for e in entries:
            d = e['doc']
            writer.writerow([e['path'], e['path'].split('/')[0], d.record_date.isoformat() if d.record_date else '', d.title, d.category, d.source_ref or '', d.page_count, hashes[e['path']]])
        archive.writestr(root + 'manifest.csv', '﻿' + out.getvalue())
    return buf.getvalue(), f'HealthBook_Share_{stamp}.zip', entries


# ------------------------------------------------------------------ helpers
def _audit(db, user, pid, action, entity_id, detail=''):
    db.add(Audit(user_id=user.id, patient_id=pid, action=action, entity='share_package', entity_id=entity_id))


def _log(db, package, event, request=None, detail=''):
    ip = (request.client.host if request and request.client else '') or ''
    db.add(ShareAccessLog(package_id=package.id, event=event, ip_hash=hashlib.sha256(ip.encode()).hexdigest() if ip else '', detail=detail[:120]))


def _no_index(response):
    response.headers['X-Robots-Tag'] = 'noindex, nofollow, noarchive'
    response.headers['Cache-Control'] = 'no-store'
    response.headers['Referrer-Policy'] = 'no-referrer'


def _aware(v):
    return v.replace(tzinfo=timezone.utc) if v.tzinfo is None else v


def package_json(pkg):
    now = utcnow()
    return {'id': pkg.id, 'label': pkg.label, 'reason': pkg.reason, 'created_at': _aware(pkg.created_at).isoformat(), 'expires_at': _aware(pkg.expires_at).isoformat(),
            'revoked_at': _aware(pkg.revoked_at).isoformat() if pkg.revoked_at else None, 'last_accessed_at': _aware(pkg.last_accessed_at).isoformat() if pkg.last_accessed_at else None,
            'access_count': pkg.access_count, 'document_count': pkg.document_count, 'view_only': pkg.view_only, 'allow_download': pkg.allow_download, 'pin_protected': bool(pkg.pin_hash),
            'status': 'revoked' if pkg.revoked_at else 'expired' if _aware(pkg.expires_at) < now else 'active'}


# ------------------------------------------------------------------ parent endpoints
@router.get('/share/presets')
def presets(user: User = Depends(require_user)):
    return {'presets': [{'key': k, 'title': v[0], 'description': v[1]} for k, v in PRESETS.items()], 'categories': list(CATEGORY_CHOICES), 'expiry': list(EXPIRY_CHOICES)}


@router.post('/patients/{pid}/share/preview')
def share_preview(pid: int, spec: ShareSpec, user: User = Depends(require_user), db: DbSession = Depends(get_db)):
    p = own_patient(db, user, pid)
    return preview_payload(db, p, resolve(db, p, spec))


@router.post('/patients/{pid}/share/package.zip')
def share_zip(pid: int, spec: ShareSpec, user: User = Depends(require_user), db: DbSession = Depends(get_db)):
    p = own_patient(db, user, pid)
    sel = resolve(db, p, spec)
    data, name, entries = build_zip(db, p, sel)
    _audit(db, user, pid, 'share_export_zip', 0); db.commit()
    return Response(data, media_type='application/zip', headers={'Content-Disposition': f'attachment; filename="{name}"', 'X-Document-Count': str(len(entries))})


@router.post('/patients/{pid}/share/summary.pdf')
def share_pdf(pid: int, spec: ShareSpec, user: User = Depends(require_user), db: DbSession = Depends(get_db)):
    p = own_patient(db, user, pid)
    sel = resolve(db, p, spec)
    generated = utcnow().astimezone(ZoneInfo(p.tz))
    pdf = build_summary_pdf(db, p, sel, entries_for(db, sel), generated, care_section_for(db, p, sel))
    _audit(db, user, pid, 'share_export_pdf', 0); db.commit()
    return Response(pdf, media_type='application/pdf', headers={'Content-Disposition': f'attachment; filename="HealthBook_Doctor_Summary_{generated:%Y-%m-%d}.pdf"'})


def _spec_from_query(spec_b64: str) -> ShareSpec:
    """Mobile clients download via GET so the OS download manager can hand the file to the native share sheet."""
    import base64
    try:
        pad = '=' * (-len(spec_b64) % 4)
        return ShareSpec(**json.loads(base64.urlsafe_b64decode(spec_b64 + pad).decode()))
    except Exception:
        raise HTTPException(422, 'Invalid share specification')


@router.get('/patients/{pid}/share/package.zip')
def share_zip_get(pid: int, spec: str, user: User = Depends(require_user), db: DbSession = Depends(get_db)):
    return share_zip(pid, _spec_from_query(spec), user, db)


@router.get('/patients/{pid}/share/summary.pdf')
def share_pdf_get(pid: int, spec: str, user: User = Depends(require_user), db: DbSession = Depends(get_db)):
    return share_pdf(pid, _spec_from_query(spec), user, db)


@router.post('/patients/{pid}/share/links', status_code=201)
def share_link_create(pid: int, body: LinkIn, request: Request, user: User = Depends(require_user), db: DbSession = Depends(get_db)):
    p = own_patient(db, user, pid)
    if p.user_id != user.id:
        raise HTTPException(403, 'Only the primary guardian can create doctor links')
    sel = resolve(db, p, body.spec)
    if not sel['documents'] and not any(sel[k] for k in ('observations', 'medicines', 'vaccines', 'courses', 'fevers', 'episodes', 'admissions', 'growth', 'visits', 'administrations')):
        raise HTTPException(422, 'Nothing selected to share')
    token = secrets.token_urlsafe(32)
    salt = hash_ = None
    if body.pin:
        salt = secrets.token_hex(16)
        hash_ = hash_password(body.pin, salt)
    pkg = SharePackage(patient_id=pid, created_by=user.id, label=body.label.strip() or 'Doctor share', reason=sel['reason'], scope_json=json.dumps(sel), token_hash=hashlib.sha256(token.encode()).hexdigest(),
                       pin_salt=salt, pin_hash=hash_, view_only=body.view_only, allow_download=body.allow_download and not body.view_only, include_summary=body.spec.include_summary,
                       document_count=len(sel['documents']), expires_at=utcnow() + timedelta(hours=EXPIRY_CHOICES[body.expiry]))
    db.add(pkg); db.flush()
    _log(db, pkg, 'created', request, f'{len(sel["documents"])} documents; expiry {body.expiry}; pin {"yes" if body.pin else "no"}')
    _audit(db, user, pid, 'share_link_created', pkg.id)
    db.commit()
    return {**package_json(pkg), 'token': token, 'path': f'/doctor.html#t={token}', 'api_path': f'/api/share/public/{token}'}


@router.get('/patients/{pid}/share/links')
def share_links(pid: int, user: User = Depends(require_user), db: DbSession = Depends(get_db)):
    own_patient(db, user, pid)
    rows = db.query(SharePackage).filter_by(patient_id=pid).order_by(SharePackage.created_at.desc()).limit(100).all()
    return {'items': [package_json(r) for r in rows]}


@router.get('/share/links/{sid}/log')
def share_link_log(sid: int, user: User = Depends(require_user), db: DbSession = Depends(get_db)):
    pkg = db.get(SharePackage, sid)
    if not pkg:
        raise HTTPException(404, 'Not found')
    own_patient(db, user, pkg.patient_id)
    return {'package': package_json(pkg), 'events': [{'at': _aware(e.at).isoformat(), 'event': e.event, 'detail': e.detail} for e in db.query(ShareAccessLog).filter_by(package_id=sid).order_by(ShareAccessLog.at.desc()).limit(200)]}


@router.delete('/share/links/{sid}')
def share_link_revoke(sid: int, request: Request, user: User = Depends(require_user), db: DbSession = Depends(get_db)):
    pkg = db.get(SharePackage, sid)
    if not pkg:
        raise HTTPException(404, 'Not found')
    p = own_patient(db, user, pkg.patient_id)
    if p.user_id != user.id:
        raise HTTPException(403, 'Only the primary guardian can revoke doctor links')
    if not pkg.revoked_at:
        pkg.revoked_at = utcnow()
        _log(db, pkg, 'revoked', request)
        _audit(db, user, pkg.patient_id, 'share_link_revoked', pkg.id)
        db.commit()
    return package_json(pkg)


# ------------------------------------------------------------------ public doctor endpoints (token + optional PIN; package-scoped)
def _open_package(db, token, pin, request, response, event='opened'):
    _no_index(response)
    if not token or len(token) < 20 or len(token) > 120:
        raise HTTPException(404, 'This link is not valid')
    pkg = db.query(SharePackage).filter_by(token_hash=hashlib.sha256(token.encode()).hexdigest()).first()
    if not pkg:
        raise HTTPException(404, 'This link is not valid')
    now = utcnow()
    if pkg.revoked_at:
        _log(db, pkg, 'denied', request, 'revoked'); db.commit()
        raise HTTPException(410, 'This link was revoked by the family')
    if _aware(pkg.expires_at) < now:
        _log(db, pkg, 'expired_hit', request); db.commit()
        raise HTTPException(410, 'This link has expired')
    if pkg.pin_hash:
        recent_fail = db.query(ShareAccessLog).filter(ShareAccessLog.package_id == pkg.id, ShareAccessLog.event == 'pin_failed', ShareAccessLog.at >= now - timedelta(minutes=15)).count()
        if recent_fail >= 8:
            raise HTTPException(429, 'Too many PIN attempts; try again later')
        if not pin or not hmac.compare_digest(hash_password(pin, pkg.pin_salt), pkg.pin_hash):
            _log(db, pkg, 'pin_failed', request); db.commit()
            raise HTTPException(401, 'PIN required' if not pin else 'Incorrect PIN')
    pkg.access_count += 1
    pkg.last_accessed_at = now
    _log(db, pkg, event, request)
    db.commit()
    return pkg


@router.get('/share/public/{token}')
def public_package(token: str, request: Request, response: Response, x_share_pin: str | None = Header(default=None), db: DbSession = Depends(get_db)):
    pkg = _open_package(db, token, x_share_pin, request, response)
    sel = json.loads(pkg.scope_json)
    p = db.get(Patient, pkg.patient_id)
    entries = entries_for(db, sel)
    generated = utcnow().astimezone(ZoneInfo(p.tz))
    return {'label': pkg.label, 'reason': sel['reason'], 'child': {'name': p.name, 'dob': p.dob.isoformat(), 'age': age_text(p.dob, generated.date())}, 'expires_at': _aware(pkg.expires_at).isoformat(),
            'view_only': pkg.view_only, 'allow_download': pkg.allow_download, 'include_summary': pkg.include_summary,
            'documents': [{'n': e['n'], 'title': e['doc'].title, 'category': e['doc'].category, 'record_date': e['doc'].record_date.isoformat() if e['doc'].record_date else None, 'pages': e['doc'].page_count, 'mime': e['doc'].mime, 'ref': e['doc'].source_ref} for e in entries],
            'counts': {k: len(sel[k]) for k in ('episodes', 'admissions', 'medicines', 'observations', 'vaccines', 'fevers', 'courses')},
            'notice': 'Shared by the family for clinical review. Labels distinguish documented, parent-reported, unverified-transcription, prescribed and confirmed-administered information. Not a diagnosis.'}


@router.get('/share/public/{token}/document/{n}')
def public_document(token: str, n: int, request: Request, response: Response, x_share_pin: str | None = Header(default=None), db: DbSession = Depends(get_db)):
    pkg = _open_package(db, token, x_share_pin, request, response, event='document')
    sel = json.loads(pkg.scope_json)
    entry = next((e for e in entries_for(db, sel) if e['n'] == n), None)
    if not entry:
        raise HTTPException(404, 'Not in this package')
    d = entry['doc']
    raw = decrypt(read_document(d.storage_name))
    disposition = 'inline' if pkg.view_only or not pkg.allow_download else 'attachment'
    return Response(raw, media_type=d.mime, headers={'Content-Disposition': f'{disposition}; filename="{entry["path"].split("/")[-1]}"', 'X-Robots-Tag': 'noindex, nofollow', 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})


@router.get('/share/public/{token}/summary.pdf')
def public_summary(token: str, request: Request, response: Response, x_share_pin: str | None = Header(default=None), db: DbSession = Depends(get_db)):
    pkg = _open_package(db, token, x_share_pin, request, response, event='summary')
    if not pkg.include_summary:
        raise HTTPException(404, 'This share has no summary')
    sel = json.loads(pkg.scope_json)
    p = db.get(Patient, pkg.patient_id)
    generated = utcnow().astimezone(ZoneInfo(p.tz))
    pdf = build_summary_pdf(db, p, sel, entries_for(db, sel), generated, care_section_for(db, p, sel))
    disposition = 'inline' if pkg.view_only else 'attachment'
    return Response(pdf, media_type='application/pdf', headers={'Content-Disposition': f'{disposition}; filename="Doctor_Summary.pdf"', 'X-Robots-Tag': 'noindex, nofollow', 'Cache-Control': 'no-store'})


@router.get('/share/public/{token}/package.zip')
def public_zip(token: str, request: Request, response: Response, x_share_pin: str | None = Header(default=None), db: DbSession = Depends(get_db)):
    pkg = _open_package(db, token, x_share_pin, request, response, event='package')
    if pkg.view_only or not pkg.allow_download:
        raise HTTPException(403, 'Downloads are disabled for this share')
    sel = json.loads(pkg.scope_json)
    p = db.get(Patient, pkg.patient_id)
    data, name, _ = build_zip(db, p, sel)
    return Response(data, media_type='application/zip', headers={'Content-Disposition': f'attachment; filename="{name}"', 'X-Robots-Tag': 'noindex, nofollow', 'Cache-Control': 'no-store'})
