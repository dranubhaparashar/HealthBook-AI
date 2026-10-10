"""Care workflows shared by the web and native clients."""
import csv
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import zipfile
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Response, Query
from pydantic import BaseModel, Field, model_validator, field_validator, ValidationError
from sqlalchemy import update, or_, select
from sqlalchemy.orm import Session as DbSession
from .database import get_db
from .config import DATA_DIR
from .crypto import encrypt, decrypt, crypto
from .storage import read_document, delete_document
from .security import require_user, own_patient, own_resource, hash_password
from .models import (User, Patient, Document, Medicine, MedicationEvent, MedicineRevision, HospitalVisit, Vaccine,
    Observation, Admission, TreatmentAdministration, Episode, Growth, CaregiverAccess, Invitation,
    ExtractionReview, Reminder, Notification, PushDevice, WorkerHeartbeat, Audit, Session, utcnow)
from .main import as_json, safe_doc, audit, MedicineIn, VaccineIn, ObservationIn, PatientIn, VisitIn
from .reminders import aware, local_time
from .history import remember
from .models import RecordRevision

router=APIRouter(prefix='/api')

def guardian(db,user,pid):
    p=own_patient(db,user,pid)
    if p.user_id!=user.id:raise HTTPException(403,'Only the primary guardian can manage sharing or account settings')
    return p

def save(db,user,pid,row,action='create'):
    db.add(row);db.flush();audit(db,user,pid,action,row.__tablename__,row.id);db.commit()
    return as_json(row)

def check_vaccine_duplicate(db,pid,body,exclude=None):
    candidates=[v for v in db.query(Vaccine).filter_by(patient_id=pid) if v.id!=exclude and
        re.sub(r'\W','',v.name.casefold())==re.sub(r'\W','',body.name.casefold()) and v.dose_label.casefold()==body.dose_label.casefold()]
    if candidates and not body.duplicate_confirmed:
        raise HTTPException(409,{'message':'Possible duplicate vaccine/dose. Review these existing entries and explicitly confirm if this is a separate record.','duplicates':[as_json(v) for v in candidates]})

@router.patch('/patients/{pid}')
def profile(pid:int,body:PatientIn,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    p=guardian(db,user,pid)
    remember(db,user,p)
    if p.tz!=body.tz:
        # Existing times cannot silently acquire a new meaning.
        for m in db.query(Medicine).filter_by(patient_id=pid,status='verified'):m.status='draft';m.verified_at=None;m.version+=1
        for v in db.query(Vaccine).filter_by(patient_id=pid,confirmed=True).filter(Vaccine.given_date.is_(None)):v.confirmed=False;v.version+=1
        for v in db.query(HospitalVisit).filter_by(patient_id=pid,confirmed=True,status='planned'):v.confirmed=False;v.version+=1
    for k,v in body.model_dump().items():setattr(p,k,v)
    out=save(db,user,pid,p,'update_profile_reconcile_timezone')
    from .profiles import profile_json
    return profile_json(p) if isinstance(out,dict) else out

@router.get('/documents/{did}/pages/{page}')
def document_page(did:int,page:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    d=own_resource(db,user,Document,did);safe_doc(db,d.patient_id,did,page)
    raw=decrypt(read_document(d.storage_name))
    if d.mime=='application/pdf':
        import fitz
        with fitz.open(stream=raw,filetype='pdf') as pdf:
            p=pdf[page-1]
            scale=min(1.5,1800/max(p.rect.width,p.rect.height))
            content=p.get_pixmap(matrix=fitz.Matrix(scale,scale)).tobytes('png')
    else:
        from PIL import Image
        from .extract import heif_support
        heif_support()
        with Image.open(io.BytesIO(raw)) as image:
            image.thumbnail((1800,1800));buf=io.BytesIO();image.convert('RGB').save(buf,format='PNG');content=buf.getvalue()
    return Response(content,media_type='image/png')

@router.post('/documents/{did}/signed-access')
def signed_access(did:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_resource(db,user,Document,did)
    ticket=encrypt(json.dumps({'document_id':did,'user_id':user.id,'session_hash':user._session_hash,'expires':int(utcnow().timestamp())+120}).encode()).decode()
    return {'url':f'/api/files/{ticket}','expires_in':120}

@router.get('/files/{ticket}')
def signed_file(ticket:str,db:DbSession=Depends(get_db)):
    try:
        payload=json.loads(crypto().decrypt(ticket.encode(),ttl=120))
        if payload['expires']<utcnow().timestamp():raise ValueError()
        session=db.query(Session).filter_by(token_hash=payload['session_hash'],user_id=payload['user_id']).first()
        if not session or aware(session.expires_at)<utcnow():raise ValueError()
        user=db.get(User,payload['user_id'])
        d=own_resource(db,user,Document,payload['document_id'])
    except Exception:raise HTTPException(404,'File link expired or unavailable')
    raw=decrypt(read_document(d.storage_name))
    return Response(raw,media_type=d.mime,headers={'Content-Disposition':'attachment; filename="medical-document'+('.pdf' if d.mime=='application/pdf' else '.jpg' if d.mime=='image/jpeg' else '.heic' if d.mime=='image/heic' else '.png')+'"'})

class ReviewItem(BaseModel):
    kind:Literal['medicine','vaccine','observation','episode']
    page:int=Field(default=1,ge=1,le=35)
    fields:dict
    included:bool=True       # only included rows become history; OCR suggestions arrive excluded from the web review
    verified:bool=False
    activate_reminders:bool=False
    duplicate_confirmed:bool=False

class ReviewBody(BaseModel):
    text:str=Field(default='',max_length=200000)
    items:list[ReviewItem]=Field(default_factory=list,max_length=300)
    version:int=Field(default=1,ge=1)
    checked_against_original:bool=False

@router.get('/documents/{did}/review')
def review_get(did:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    d=own_resource(db,user,Document,did)
    row=db.query(ExtractionReview).filter_by(document_id=did).first()
    if row:return {'document':as_json(d),**json.loads(row.payload),'status':row.status,'version':row.version}
    from .extraction import structured_candidates
    text=decrypt(d.extracted_cipher.encode()).decode() if d.extracted_cipher else ''
    return {'document':as_json(d),'text':text,'items':structured_candidates(text,d.category),'version':1,'status':'needs_review'}

@router.put('/documents/{did}/review')
def review_save(did:int,body:ReviewBody,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    d=own_resource(db,user,Document,did)
    row=db.query(ExtractionReview).filter_by(document_id=did).first()
    if row and (row.status=='confirmed' or row.version!=body.version):raise HTTPException(409,'Review has changed or already been confirmed. Reload it.')
    if not row:
        row=ExtractionReview(document_id=did,payload='{}',version=1);db.add(row);db.flush()
    result=db.execute(update(ExtractionReview).where(ExtractionReview.id==row.id,ExtractionReview.version==body.version,ExtractionReview.status=='needs_review').values(payload=json.dumps(body.model_dump(mode='json')),version=body.version+1))
    if result.rowcount!=1:raise HTTPException(409,'Another caregiver changed this review. Reload it.')
    d.extraction_status='needs_review';audit(db,user,d.patient_id,'save_extraction_review','document',did);db.commit()
    return {'version':body.version+1,'status':'needs_review'}

@router.post('/documents/{did}/review/confirm')
def review_confirm(did:int,body:ReviewBody,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    """Archive the reviewed document and create history ONLY from rows the caregiver included and checked.

    Validation runs completely before anything is written and returns field-level errors (row, page, field, reason),
    so the review can highlight the exact problem. Excluded, removed and empty rows and the raw transcription text never
    block. A document without any included row is saved as a reviewed document (transcription only)."""
    from .review_validation import validate_review, summary
    d=own_resource(db,user,Document,did)
    row=db.query(ExtractionReview).filter_by(document_id=did).first()
    if row and row.status=='confirmed':raise HTTPException(409,'This review has already been saved to the medical history')
    patient=db.get(Patient,d.patient_id)
    errors,rows=validate_review(body,d.page_count,{'medicine':MedicineIn,'vaccine':VaccineIn,'observation':ObservationIn,'episode':EpisodeIn})
    from .review_validation import _err,row_numbers
    numbers=row_numbers(body.items)
    if not errors:
        today=utcnow().astimezone(ZoneInfo(patient.tz)).date()
        for i,item in rows:
            if item.kind!='vaccine':continue
            f=item.fields;n=numbers[i]
            given=date.fromisoformat(f['given_date']) if f.get('given_date') else None
            nxt=date.fromisoformat(f['next_due_date']) if f.get('next_due_date') else None
            if given and not patient.dob<=given<=today:errors.append(_err(i,item,n,'given_date','out_of_range','Date given must be between birth and today.'))
            if nxt and given and nxt<=given:errors.append(_err(i,item,n,'next_due_date','order','Next dose date must follow the date given.'))
            if item.activate_reminders and not (nxt or (f.get('due_date') and not given)):errors.append(_err(i,item,n,'next_due_date','reminder_without_date','Enter a next-dose date before activating reminders for a dose that was already given.'))
            try:check_vaccine_duplicate(db,d.patient_id,VaccineIn.model_validate({**f,'source_type':'vaccination_card','duplicate_confirmed':item.duplicate_confirmed}))
            except HTTPException:errors.append(_err(i,item,n,'name','possible_duplicate','A matching vaccine dose already exists. Tick "I verified this is a separate record" or exclude this row.'))
    if errors:
        raise HTTPException(422,{'message':summary(errors),'errors':errors})
    if not row:
        row=ExtractionReview(document_id=did,payload='{}',version=1);db.add(row);db.flush()
    result=db.execute(update(ExtractionReview).where(ExtractionReview.id==row.id,ExtractionReview.version==body.version,ExtractionReview.status=='needs_review').values(status='confirmed',version=body.version+1))
    if result.rowcount!=1:raise HTTPException(409,'Another caregiver changed this review, or it was already confirmed. Reload it.')
    created=[]
    for i,item in rows:
        fields={**item.fields,'source_document_id':did,'source_page':item.page}
        if item.kind=='medicine':
            record=Medicine(patient_id=d.patient_id,**MedicineIn.model_validate(fields).model_dump(),status='draft')
        elif item.kind=='vaccine':
            data=VaccineIn.model_validate({**fields,'source_type':'vaccination_card','duplicate_confirmed':item.duplicate_confirmed})
            next_date=date.fromisoformat(fields['next_due_date']) if fields.get('next_due_date') else None
            record=Vaccine(patient_id=d.patient_id,**data.model_dump(exclude={'duplicate_confirmed'}),confirmed=bool(data.given_date or (item.activate_reminders and not next_date)))
            if next_date:
                upcoming=VaccineIn.model_validate({**fields,'source_type':'vaccination_card','given_date':None,'due_date':next_date,'dose_label':fields.get('next_dose_label') or '','batch_number':'','duplicate_confirmed':True})
                appointment=Vaccine(patient_id=d.patient_id,**upcoming.model_dump(exclude={'duplicate_confirmed'}),confirmed=item.activate_reminders)
                db.add(appointment);db.flush();created.append({'kind':'vaccine','id':appointment.id,'index':i});audit(db,user,d.patient_id,'confirmed_next_dose' if item.activate_reminders else 'unconfirmed_next_dose','vaccines',appointment.id)
        elif item.kind=='observation':
            record=Observation(patient_id=d.patient_id,**ObservationIn.model_validate({**fields,'verified':True}).model_dump())
        else:
            data=EpisodeIn.model_validate(fields)
            record=Episode(patient_id=d.patient_id,**data.model_dump(exclude={'links'}),links_json=json.dumps(validate_links(db,d.patient_id,data.links)))
        db.add(record);db.flush();created.append({'kind':item.kind,'id':record.id,'index':i});audit(db,user,d.patient_id,'confirmed_transcription',record.__tablename__,record.id)
    if body.text.strip():
        d.extracted_cipher=encrypt(body.text.encode()).decode()   # the corrected transcription is kept with the document
    row.payload=json.dumps(body.model_dump(mode='json'));row.confirmed_at=utcnow()
    d.extraction_status='reviewed'
    audit(db,user,d.patient_id,'confirm_extraction_review','document',did);db.commit()
    return {'created':created,'status':'confirmed','document_status':'reviewed','transcription_only':not created,
            'skipped':{'excluded':sum(1 for x in body.items if not x.included),'empty':sum(1 for x in body.items if x.included)-len(rows)},
            'medication_reminders':'Require separate prescription confirmation'}

class StopCourse(BaseModel):
    actual_start_date:date|None=None
    actual_stop_date:date
    reason:str=Field(min_length=2,max_length=1000)

@router.post('/medicines/{mid}/stop')
def stop_course(mid:int,body:StopCourse,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    m=own_resource(db,user,Medicine,mid)
    today=utcnow().astimezone(ZoneInfo(db.get(Patient,m.patient_id).tz)).date()
    start=body.actual_start_date or m.actual_start_date
    if body.actual_stop_date>today or (start and start>body.actual_stop_date):raise HTTPException(422,'Actual course dates must be ordered and not in the future')
    db.add(MedicineRevision(medicine_id=mid,version=m.version,snapshot_json=json.dumps(as_json(m))))
    db.add(MedicationEvent(medicine_id=mid,medicine_version=m.version,event_type='stopped',occurred_at=datetime.combine(body.actual_stop_date,datetime.min.time()),source='caregiver_report',note=body.reason,idempotency_key=secrets.token_hex(16)))
    m.actual_start_date=body.actual_start_date or m.actual_start_date;m.actual_stop_date=body.actual_stop_date;m.status='discontinued';m.version+=1
    audit(db,user,m.patient_id,'caregiver_recorded_stop','medicine',mid)
    return save(db,user,m.patient_id,m,'stop_course')

@router.get('/medicines/{mid}')
def medicine_detail(mid:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    m=own_resource(db,user,Medicine,mid)
    return {**as_json(m),'events':[as_json(e) for e in db.query(MedicationEvent).filter_by(medicine_id=mid).order_by(MedicationEvent.occurred_at.desc())],
        'revisions':[{'version':r.version,'snapshot':json.loads(r.snapshot_json)} for r in db.query(MedicineRevision).filter_by(medicine_id=mid).order_by(MedicineRevision.version.desc())]}

class SourceIn(BaseModel):
    source_document_id:int|None=None
    source_page:int=Field(default=1,ge=1,le=35)

class AdmissionIn(SourceIn):
    facility:str=Field(min_length=1,max_length=180)
    doctor:str=Field(default='',max_length=180)
    diagnosis:str=Field(default='',max_length=2000)
    treatment_plan:str=Field(default='',max_length=4000)
    admitted_on:date
    discharged_on:date|None=None
    discharge_document_id:int|None=None
    @model_validator(mode='after')
    def ordered(self):
        if self.discharged_on and self.discharged_on<self.admitted_on:raise ValueError('Discharge cannot precede admission')
        return self

@router.post('/patients/{pid}/admissions',status_code=201)
def add_admission(pid:int,body:AdmissionIn,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid);safe_doc(db,pid,body.source_document_id,body.source_page);safe_doc(db,pid,body.discharge_document_id)
    return save(db,user,pid,Admission(patient_id=pid,**body.model_dump()))

@router.get('/patients/{pid}/admissions')
def admissions(pid:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid)
    return [as_json(x) for x in db.query(Admission).filter_by(patient_id=pid).order_by(Admission.admitted_on.desc())]

@router.patch('/admissions/{aid}')
def edit_admission(aid:int,body:AdmissionIn,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    row=own_resource(db,user,Admission,aid);safe_doc(db,row.patient_id,body.source_document_id,body.source_page);safe_doc(db,row.patient_id,body.discharge_document_id)
    remember(db,user,row)
    for k,v in body.model_dump().items():setattr(row,k,v)
    return save(db,user,row.patient_id,row,'update')

class VisitSeries(BaseModel):
    visits:list[VisitIn]=Field(min_length=1,max_length=60)
    checked_against_hospital_plan:Literal[True]

@router.post('/patients/{pid}/hospital-visit-series',status_code=201)
def visit_series(pid:int,body:VisitSeries,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    p=own_patient(db,user,pid);out=[]
    for item in body.visits:
        safe_doc(db,pid,item.source_document_id,item.source_page)
        if not item.source_document_id or not item.confirmed:raise HTTPException(422,'Every appointment must have a source and an explicitly confirmed date')
        if item.admission_id:
            a=own_resource(db,user,Admission,item.admission_id)
            if a.patient_id!=pid:raise HTTPException(400,'Admission does not belong to this child')
        try:local_time(item.planned_at,p.tz)
        except ValueError as e:raise HTTPException(422,str(e))
        row=HospitalVisit(patient_id=pid,**item.model_dump());db.add(row);db.flush();out.append(as_json(row));audit(db,user,pid,'confirm_visit_series','hospital_visit',row.id)
    db.commit();return out

class Reschedule(BaseModel):
    planned_at:datetime
    checked_against_source:Literal[True]
    source_document_id:int
    source_page:int=Field(default=1,ge=1,le=35)

@router.post('/hospital-visits/{vid}/reschedule')
def reschedule(vid:int,body:Reschedule,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    row=own_resource(db,user,HospitalVisit,vid);safe_doc(db,row.patient_id,body.source_document_id,body.source_page)
    remember(db,user,row)
    if body.planned_at.tzinfo:raise HTTPException(422,'Enter the child timezone local clock time')
    try:local_time(body.planned_at,db.get(Patient,row.patient_id).tz)
    except ValueError as e:raise HTTPException(422,str(e))
    row.planned_at=body.planned_at;row.source_document_id=body.source_document_id;row.source_page=body.source_page;row.version+=1;row.status='planned';row.confirmed=True
    return save(db,user,row.patient_id,row,'reschedule_confirmed')

class AdministrationIn(SourceIn):
    source_document_id:int
    occurred_at:datetime
    medication:str=Field(min_length=1,max_length=180)
    recorded_dose:str=Field(default='',max_length=180)
    recorded_route:str=Field(default='',max_length=80)
    note:str=Field(default='',max_length=2000)
    checked_administration_record:Literal[True]
    idempotency_key:str=Field(default_factory=lambda:secrets.token_hex(16),min_length=8,max_length=64)

@router.post('/hospital-visits/{vid}/administrations',status_code=201)
def administration(vid:int,body:AdministrationIn,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    visit=own_resource(db,user,HospitalVisit,vid);safe_doc(db,visit.patient_id,body.source_document_id,body.source_page)
    p=db.get(Patient,visit.patient_id)
    try:when=body.occurred_at if body.occurred_at.tzinfo else local_time(body.occurred_at,p.tz)
    except ValueError as e:raise HTTPException(422,str(e))
    if when>utcnow()+timedelta(minutes=5) or when.date()<p.dob:raise HTTPException(422,'Administration must be an actual past event after birth')
    existing=db.query(TreatmentAdministration).filter_by(idempotency_key=body.idempotency_key).first()
    if existing:
        if existing.visit_id!=vid:raise HTTPException(409,'Idempotency key already used')
        return as_json(existing)
    row=TreatmentAdministration(patient_id=p.id,visit_id=vid,**{**body.model_dump(exclude={'checked_administration_record'}),'occurred_at':when.astimezone(timezone.utc)})
    return save(db,user,p.id,row,'record_verified_administration')

@router.get('/patients/{pid}/administrations')
def administrations(pid:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid)
    return [as_json(x) for x in db.query(TreatmentAdministration).filter_by(patient_id=pid).order_by(TreatmentAdministration.occurred_at.desc())]

class EpisodeLink(BaseModel):
    kind:Literal['document','medicine','observation','admission','visit']
    id:int=Field(ge=1)

class EpisodeIn(SourceIn):
    title:str=Field(min_length=1,max_length=180)
    started_on:date
    ended_on:date|None=None
    symptoms:str=Field(default='',max_length=3000)
    documented_diagnosis:str=Field(default='',max_length=3000)
    notes:str=Field(default='',max_length=3000)
    links:list[EpisodeLink]=Field(default_factory=list,max_length=400)
    @model_validator(mode='after')
    def ordered(self):
        if self.ended_on and self.ended_on<self.started_on:raise ValueError('Episode end precedes start')
        return self

def validate_links(db,pid,links):
    models={'document':Document,'medicine':Medicine,'observation':Observation,'admission':Admission,'visit':HospitalVisit}
    for item in links:
        obj=db.get(models[item.kind],item.id)
        if not obj or obj.patient_id!=pid:raise HTTPException(400,'Linked record does not belong to this child')
    return [x.model_dump() for x in links]

@router.post('/patients/{pid}/episodes',status_code=201)
def episode_add(pid:int,body:EpisodeIn,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid);safe_doc(db,pid,body.source_document_id,body.source_page)
    return save(db,user,pid,Episode(patient_id=pid,**body.model_dump(exclude={'links'}),links_json=json.dumps(validate_links(db,pid,body.links))))

@router.get('/patients/{pid}/episodes')
def episodes(pid:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid)
    return [{**as_json(x),'links':json.loads(x.links_json)} for x in db.query(Episode).filter_by(patient_id=pid).order_by(Episode.started_on.desc())]

class GrowthIn(SourceIn):
    record_date:date
    weight_kg:str=Field(default='',max_length=20)
    length_cm:str=Field(default='',max_length=20)
    head_cm:str=Field(default='',max_length=20)
    milestone:str=Field(default='',max_length=1000)
    note:str=Field(default='',max_length=2000)
    @field_validator('weight_kg','length_cm','head_cm')
    @classmethod
    def numeric(cls,v):
        if v and (not re.fullmatch(r'\d{1,3}(\.\d{1,3})?',v) or float(v)<=0):raise ValueError('Enter a positive measurement or leave unknown')
        return v

@router.post('/patients/{pid}/growth',status_code=201)
def growth_add(pid:int,body:GrowthIn,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    p=own_patient(db,user,pid);safe_doc(db,pid,body.source_document_id,body.source_page)
    if not p.dob<=body.record_date<=utcnow().astimezone(ZoneInfo(p.tz)).date():raise HTTPException(422,'Measurement date must be between birth and today')
    if not any([body.weight_kg,body.length_cm,body.head_cm,body.milestone]):raise HTTPException(422,'Enter a measurement or milestone')
    return save(db,user,pid,Growth(patient_id=pid,**body.model_dump()))

@router.get('/patients/{pid}/growth')
def growth_list(pid:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid)
    return [as_json(x) for x in db.query(Growth).filter_by(patient_id=pid).order_by(Growth.record_date)]

@router.get('/patients/{pid}/lab-trends')
def lab_trends(pid:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid);groups={}
    for row in db.query(Observation).filter_by(patient_id=pid).order_by(Observation.record_date):
        key=(row.test_name.strip().casefold(),row.unit.strip(),row.specimen.strip().casefold())
        group=groups.setdefault(key,{'test_name':row.test_name,'unit':row.unit,'specimen':row.specimen,'points':[]})
        try: value=float(row.value);value=value if __import__('math').isfinite(value) else None
        except ValueError:value=None
        group['points'].append({**as_json(row),'numeric_value':value,'plot_eligible':value is not None and row.verified and bool(row.unit and row.specimen)})
    return {'groups':list(groups.values()),'warning':'Units and specimens are kept separate. Laboratory ranges are transcribed, not interpreted as pediatric reference standards.'}

class AskIn(BaseModel):
    question:str=Field(min_length=2,max_length=1000)

@router.post('/patients/{pid}/ask')
def ask(pid:int,body:AskIn,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid)
    from .intelligence import answer
    return answer(db,pid,body.question)

@router.get('/patients/{pid}/episode-comparison')
def compare_episodes(pid:int,q:str=Query(default='E. coli',min_length=2,max_length=150),user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid)
    from .intelligence import answer
    return answer(db,pid,'Compare '+q+' reports and episodes')

class InviteIn(BaseModel):
    email:str=Field(min_length=4,max_length=254)
    role:Literal['viewer','editor']='viewer'
    guardian_permission:Literal[True]

@router.post('/patients/{pid}/invitations',status_code=201)
def invite(pid:int,body:InviteIn,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    guardian(db,user,pid);token=secrets.token_urlsafe(32)
    row=Invitation(patient_id=pid,email=body.email.strip().lower(),role=body.role,token_hash=hashlib.sha256(token.encode()).hexdigest(),expires_at=utcnow()+timedelta(days=3))
    save(db,user,pid,row,'guardian_invited_caregiver')
    return {'invitation_code':token,'expires_at':row.expires_at.isoformat(),'role':row.role,'delivery':'Share this single-use code with the intended caregiver yourself'}

class AcceptInvite(BaseModel):
    invitation_code:str=Field(min_length=20,max_length=100)

@router.post('/invitations/accept')
def accept_invite(body:AcceptInvite,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    row=db.query(Invitation).filter_by(token_hash=hashlib.sha256(body.invitation_code.encode()).hexdigest(),accepted=False,email=user.email).first()
    if not row or aware(row.expires_at)<utcnow():raise HTTPException(404,'Invitation unavailable, expired, or intended for another email')
    if db.get(Patient,row.patient_id).user_id==user.id:raise HTTPException(409,'You already own this profile')
    access=db.query(CaregiverAccess).filter_by(patient_id=row.patient_id,user_id=user.id).first()
    if access:access.role=row.role
    else:db.add(CaregiverAccess(patient_id=row.patient_id,user_id=user.id,role=row.role))
    row.accepted=True;audit(db,user,row.patient_id,'accept_guardian_invitation','invitation',row.id);db.commit()
    return {'patient_id':row.patient_id,'role':row.role}

@router.get('/patients/{pid}/family')
def family(pid:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    p=own_patient(db,user,pid)
    return {'owner':p.user_id==user.id,'members':[{'id':g.id,'email':db.get(User,g.user_id).email,'role':g.role} for g in db.query(CaregiverAccess).filter_by(patient_id=pid)]}

@router.delete('/patients/{pid}/family/{access_id}')
def revoke(pid:int,access_id:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    guardian(db,user,pid);g=db.query(CaregiverAccess).filter_by(id=access_id,patient_id=pid).first()
    if not g:raise HTTPException(404,'Not found')
    db.query(Notification).filter(Notification.user_id==g.user_id,Notification.reminder_id.in_(select(Reminder.id).where(Reminder.patient_id==pid)),Notification.status.in_(['pending','retry'])).update({'status':'cancelled'},synchronize_session=False)
    db.delete(g);audit(db,user,pid,'revoke_caregiver','access',access_id);db.commit();return {'ok':True}

@router.get('/patients/{pid}/notifications')
def notifications(pid:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid)
    rows=db.query(Notification,Reminder).join(Reminder,Reminder.id==Notification.reminder_id).filter(Notification.user_id==user.id,Reminder.patient_id==pid).order_by(Notification.created_at.desc()).limit(200).all()
    beat=db.get(WorkerHeartbeat,1)
    return {'worker_running':bool(beat and aware(beat.at)>utcnow()-timedelta(minutes=2)),'push_configured':os.getenv('BABYCARE_PUSH_ENABLED','false').lower()=='true',
        'items':[{**as_json(n),'title':r.title,'kind':r.kind,'due_at':aware(r.due_at).isoformat(),'reminder_status':r.status} for n,r in rows]}

class NotificationAction(BaseModel):
    action:Literal['acknowledge','snooze']
    minutes:int=Field(default=15,ge=1,le=1440)

@router.post('/notifications/{nid}/action')
def notification_action(nid:int,body:NotificationAction,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    n=db.query(Notification).filter_by(id=nid,user_id=user.id).first()
    if not n:raise HTTPException(404,'Not found')
    r=own_resource(db,user,Reminder,n.reminder_id)
    if body.action=='acknowledge':
        n.acknowledged_at=utcnow();n.status='acknowledged'
        db.query(Notification).filter(Notification.reminder_id==r.id,Notification.user_id==user.id,Notification.status.in_(['pending','retry'])).update({'status':'cancelled'})
    else:
        if r.status=='cancelled':raise HTTPException(409,'The underlying schedule changed; review the current plan')
        from .reminders import source_active
        if not source_active(db,r):raise HTTPException(409,'The underlying schedule changed; review the current plan')
        db.query(Notification).filter(Notification.reminder_id==r.id,Notification.status.in_(['pending','retry'])).update({'status':'cancelled'})
        r.status='snoozed';r.snoozed_until=utcnow()+timedelta(minutes=body.minutes)
    audit(db,user,r.patient_id,body.action+'_notification_only','notification',nid);db.commit();return {'ok':True}

class DeviceIn(BaseModel):
    token:str=Field(pattern=r'^(ExponentPushToken|ExpoPushToken)\[[A-Za-z0-9_-]+\]$',max_length=250)
    consent_generic_push:Literal[True]

@router.post('/devices')
def device_add(body:DeviceIn,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    digest=hashlib.sha256(body.token.encode()).hexdigest();row=db.query(PushDevice).filter_by(token_hash=digest).first()
    if row and row.user_id!=user.id:raise HTTPException(409,'This device is registered to another account; revoke it from that account first')
    if not row:row=PushDevice(user_id=user.id,token=body.token,token_hash=digest)
    row.enabled=True;db.add(row);db.commit();return {'id':row.id,'enabled':True}

@router.delete('/devices')
def devices_remove(user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    db.query(PushDevice).filter_by(user_id=user.id).update({'enabled':False});db.commit();return {'ok':True}

@router.delete('/devices/{did}')
def device_remove(did:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    device=db.query(PushDevice).filter_by(id=did,user_id=user.id).first()
    if not device:raise HTTPException(404,'Device not found')
    device.enabled=False;db.commit();return {'ok':True}

@router.get('/patients/{pid}/audit')
def audits(pid:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    guardian(db,user,pid)
    return [as_json(a) for a in db.query(Audit).filter_by(patient_id=pid).order_by(Audit.at.desc()).limit(250)]

@router.get('/patients/{pid}/record-revisions')
def record_revisions(pid:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid)
    return [{'id':r.id,'entity':r.entity,'entity_id':r.entity_id,'user_id':r.user_id,'changed_at':r.changed_at.isoformat(),'previous_values':json.loads(r.snapshot_json)} for r in db.query(RecordRevision).filter_by(patient_id=pid).order_by(RecordRevision.changed_at.desc())]

def csv_response(rows,name):
    buf=io.StringIO();keys=list(rows[0]) if rows else ['No records']
    writer=csv.DictWriter(buf,fieldnames=keys);writer.writeheader()
    # Prevent spreadsheet formula execution when a clinician opens an export.
    for row in rows:writer.writerow({k:("'"+v if isinstance(v,str) and v.startswith(('=','+','-','@','\t','\r')) else v) for k,v in row.items()})
    return Response('\ufeff'+buf.getvalue(),media_type='text/csv',headers={'Content-Disposition':f'attachment; filename="{name}.csv"'})

@router.get('/patients/{pid}/vaccination-chart.csv')
def vaccination_export(pid:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid)
    return csv_response([as_json(v) for v in db.query(Vaccine).filter_by(patient_id=pid)],'vaccination-chart')

@router.get('/patients/{pid}/labs.csv')
def labs_export(pid:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid)
    return csv_response([as_json(v) for v in db.query(Observation).filter_by(patient_id=pid)],'laboratory-history')

@router.get('/patients/{pid}/export')
def export_patient(pid:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    p=guardian(db,user,pid)
    data={'schema':'babycare-export-1','patient':as_json(p),'exported_at':utcnow().isoformat()}
    data['record_revisions']=record_revisions(pid,user,db)
    for model in [Document,Medicine,Vaccine,Observation,HospitalVisit,Admission,TreatmentAdministration,Episode,Growth,Audit]:
        data[model.__tablename__]=[as_json(x) for x in db.query(model).filter_by(patient_id=pid)]
    ids=[x['id'] for x in data['medicines']]
    data['medication_events']=[as_json(e) for e in db.query(MedicationEvent).filter(MedicationEvent.medicine_id.in_(ids))]
    data['medicine_revisions']=[{**as_json(r),'snapshot':json.loads(r.snapshot_json)} for r in db.query(MedicineRevision).filter(MedicineRevision.medicine_id.in_(ids))]
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('records.json',json.dumps(data,ensure_ascii=False,indent=2))
        for d in db.query(Document).filter_by(patient_id=pid):
            ext={'application/pdf':'.pdf','image/png':'.png','image/jpeg':'.jpg','image/heic':'.heic'}[d.mime]
            archive.writestr(f'originals/document-{d.id}{ext}',decrypt(read_document(d.storage_name)))
            if d.extracted_cipher:archive.writestr(f'transcriptions/document-{d.id}.txt',decrypt(d.extracted_cipher.encode()))
        for r in db.query(ExtractionReview).filter(ExtractionReview.document_id.in_([d['id'] for d in data['documents']])):
            archive.writestr(f'reviews/document-{r.document_id}.json',r.payload)
    audit(db,user,pid,'export_family_records','patient',pid);db.commit()
    return Response(buf.getvalue(),media_type='application/zip',headers={'Content-Disposition':'attachment; filename="healthbook-private-export.zip"'})

class DeleteAccount(BaseModel):
    password:str=Field(min_length=12,max_length=200)
    confirmation:Literal['DELETE MY ACCOUNT AND OWNED CHILD RECORDS']

@router.post('/account/delete')
def account_delete(body:DeleteAccount,response:Response,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    if not hmac.compare_digest(hash_password(body.password,user.password_salt),user.password_hash):raise HTTPException(403,'Password check failed')
    pids=[p.id for p in db.query(Patient).filter_by(user_id=user.id)]
    docs=db.query(Document).filter(Document.patient_id.in_(pids)).all()
    names=[d.storage_name for d in docs]
    # Remove restrictive administration links before owned documents; all other child records cascade.
    db.query(TreatmentAdministration).filter(TreatmentAdministration.patient_id.in_(pids)).delete(synchronize_session=False)
    db.delete(user);db.commit()
    for name in names:
        try:delete_document(name)
        except Exception:pass  # the row is already gone; a stray encrypted blob is unreadable without the key
    response.delete_cookie('babycare_session',path='/')
    return {'deleted':True,'backup_note':'Offline encrypted backups must be expired according to the operator retention policy.'}
