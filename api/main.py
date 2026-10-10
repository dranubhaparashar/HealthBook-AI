"""HealthBook AI API (formerly BabyCare AI). Private, local-first prototype — not clinical decision software."""
import hashlib
import json
import re
import secrets
from datetime import date, datetime, timedelta, timezone, time
from pathlib import Path
from typing import Optional,  Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import FastAPI, Depends, HTTPException, UploadFile, File, Form, Response, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator
from sqlalchemy import select, or_
from sqlalchemy.orm import Session as DbSession

from .database import DATA_DIR, Base, engine, get_db
from .models import User, Session as AuthSession, Patient, Document, Medicine, MedicineRevision, MedicationEvent, HospitalVisit, Vaccine, Observation, Audit, utcnow, CaregiverAccess, Admission
from .security import create_password, hash_password, issue_session, require_user, own_patient, own_resource
from .extract import encrypt, decrypt, extract_text, medication_candidates
from .vaccination import card_candidates, reference
from .history import remember

app=FastAPI(title='HealthBook AI — local prototype',version='0.1.0',description='Personal medical record & reminders; NEVER a prescriber.')
from .migrate import upgrade
upgrade()
from .cloud import harden_database, prepare_storage, install_handlers
harden_database(engine)   # PostgreSQL: RLS on every table, Supabase client roles revoked (no-op on SQLite)
prepare_storage()         # local directory, or the PRIVATE Supabase bucket on Render
install_handlers(app)
from .storage import write_document, read_document, delete_document, MissingObject

from .guardrails import secure_headers
app.middleware('http')(secure_headers)

# Explicit origins only; web frontend is normally same-origin on port 8000.
import os
origins=[x.strip() for x in os.getenv('BABYCARE_CORS_ORIGINS','').split(',') if x.strip()]
if origins:
    app.add_middleware(CORSMiddleware,allow_origins=origins,allow_credentials=True,allow_methods=['GET','POST','PUT','PATCH','DELETE'],allow_headers=['Authorization','Content-Type','X-Requested-With','X-Share-Pin'],expose_headers=['Content-Disposition','X-Document-Count'])

class Credentials(BaseModel):
    client:Literal['web','mobile']='mobile'
    email:str=Field(min_length=4,max_length=254)
    password:str=Field(min_length=12,max_length=200)
    @field_validator('email')
    @classmethod
    def email_clean(cls,v):
        v=v.strip().lower()
        if '@' not in v or '.' not in v.split('@')[-1]: raise ValueError('Enter a valid email address')
        return v

class PatientIn(BaseModel):
    name:str=Field(min_length=1,max_length=100)
    dob:date
    tz:str='Asia/Kolkata'
    sex:Optional[str]=None
    relationship:Optional[str]=None
    @field_validator('sex')
    @classmethod
    def sex_valid(cls,value):
        from .profiles import SEXES
        if value in (None,''):return None
        if value not in SEXES:raise ValueError('Unknown sex value')
        return value
    @field_validator('relationship')
    @classmethod
    def relationship_valid(cls,value):
        from .profiles import RELATIONSHIPS
        if value in (None,''):return None
        if value not in RELATIONSHIPS:raise ValueError('Unknown relationship')
        return value
    @field_validator('tz')
    @classmethod
    def timezone_valid(cls,value):
        try: ZoneInfo(value)
        except ZoneInfoNotFoundError:raise ValueError('Unknown timezone')
        return value
    @field_validator('dob')
    @classmethod
    def dob_valid(cls,value):
        if value>date.today():raise ValueError('Date of birth cannot be in the future')
        return value

class MedicineIn(BaseModel):
    source_page:int=Field(default=1,ge=1,le=35)
    generic_name:str=Field(default='',max_length=150)
    brand_name:str=Field(default='',max_length=150)
    dosage:str=Field(default='',max_length=100)
    dose_unit:str=Field(default='',max_length=60)
    route:str=Field(default='',max_length=80)
    frequency:str=Field(default='',max_length=180)
    prescribing_doctor:str=Field(default='',max_length=180)
    indication:str=Field(default='',max_length=1000)
    prescription_date:date|None=None
    actual_start_date:date|None=None
    actual_stop_date:date|None=None
    name:str=Field(min_length=1,max_length=150)
    instructions:str=Field(default='',max_length=3000)
    start_date:date|None=None
    end_date:date|None=None
    schedule_times:str=Field(default='',max_length=100)
    source_document_id:int|None=None
    course_type:Literal['prescription','discharge_prescription','inpatient_treatment','historical_prescription']='prescription'
    @field_validator('schedule_times')
    @classmethod
    def valid_schedule(cls,v):
        v=v.strip()
        if v:
            times=[x.strip() for x in v.split(',')]
            if len(times)>8 or len(times)!=len(set(times)) or not all(re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',x) for x in times):
                raise ValueError('Use unique 24-hour HH:MM times separated by commas')
            return ','.join(sorted(times))
        return ''
    @model_validator(mode='after')
    def ordered(self):
        if self.start_date and self.end_date and self.start_date>self.end_date:raise ValueError('End date cannot precede start date')
        if self.actual_start_date and self.actual_stop_date and self.actual_start_date>self.actual_stop_date:raise ValueError('Actual stop date cannot precede actual start')
        if any(d and d>date.today() for d in (self.actual_start_date,self.actual_stop_date)):raise ValueError('Actual treatment dates cannot be in the future')
        return self

class VerifyIn(BaseModel):
    checked_against_prescription:Literal[True]

class MedicineEventIn(BaseModel):
    source_document_id:int|None=None
    source_page:int=Field(default=1,ge=1,le=35)
    event_type:Literal['given','skipped']
    occurred_at:datetime
    source:Literal['caregiver_report','copied_from_hospital_document']='caregiver_report'
    note:str=Field(default='',max_length=1000)
    idempotency_key:str=Field(default_factory=lambda:secrets.token_hex(12),min_length=8,max_length=64)

class VisitIn(BaseModel):
    source_page:int=Field(default=1,ge=1,le=35)
    admission_id:int|None=None
    kind:Literal['hospital','consultation','laboratory','treatment_review']='hospital'
    facility:str=Field(default='',max_length=160)
    planned_at:datetime
    purpose:str=Field(min_length=2,max_length=1000)
    source_document_id:int|None=None
    confirmed:bool=False
    notes:str=Field(default='',max_length=1000)
    @field_validator('planned_at')
    @classmethod
    def naive_local_clock(cls,v):
        # Every patient has an explicit timezone; use clock times entered in that timezone.
        if v.tzinfo is not None:raise ValueError('Enter local clock time without UTC suffix; patient timezone applies')
        return v

class VisitUpdate(BaseModel):
    status:Literal['planned','attended','postponed','cancelled']
    note:str=Field(default='',max_length=1000)

class VaccineIn(BaseModel):
    duplicate_confirmed:bool=False
    source_page:int=Field(default=1,ge=1,le=35)
    batch_number:str=Field(default='',max_length=150)
    facility:str=Field(default='',max_length=180)
    doctor:str=Field(default='',max_length=180)
    schedule:Literal['card','India_UIP','IAP_private']='card'
    reminder_days:str=Field(default='7,1,0',pattern=r'^\d{1,2}(,\d{1,2}){0,9}$')
    name:str=Field(min_length=1,max_length=120)
    dose_label:str=Field(default='',max_length=70)
    due_date:date|None=None
    given_date:date|None=None
    source_document_id:int|None=None
    source_type:Literal['manual','vaccination_card','doctor_schedule','India_UIP_reference']='manual'
    note:str=Field(default='',max_length=1000)

class VaccineUpdate(BaseModel):
    source_page:int|None=Field(default=None,ge=1,le=35)
    batch_number:str|None=Field(default=None,max_length=150)
    facility:str|None=Field(default=None,max_length=180)
    doctor:str|None=Field(default=None,max_length=180)
    schedule:Literal['card','India_UIP','IAP_private']|None=None
    reminder_days:str|None=Field(default=None,pattern=r'^\d{1,2}(,\d{1,2}){0,9}$')
    source_document_id:int|None=None
    source_type:Literal['manual','vaccination_card','doctor_schedule','India_UIP_reference']|None=None
    name:str|None=Field(default=None,min_length=1,max_length=120)
    dose_label:str|None=Field(default=None,max_length=70)
    due_date:date|None=None
    given_date:date|None=None
    confirmed:bool|None=None
    note:str|None=Field(default=None,max_length=1000)

class ObservationIn(BaseModel):
    source_page:int=Field(default=1,ge=1,le=35)
    verified:bool=False
    organism:str=Field(default='',max_length=180)
    culture_findings:str=Field(default='',max_length=2000)
    susceptibility:str=Field(default='',max_length=2000)
    test_name:str=Field(min_length=1,max_length=120)
    value:str=Field(min_length=1,max_length=75)
    unit:str=Field(default='',max_length=50)
    specimen:str=Field(default='',max_length=80)
    record_date:date
    source_document_id:int|None=None
    reference_range:str=Field(default='',max_length=120)

def audit(db,user,patient_id,action,entity,obj_id):
    db.add(Audit(user_id=user.id,patient_id=patient_id,action=action,entity=entity,entity_id=obj_id))

def safe_doc(db,patient_id,doc_id,page=1):
    if doc_id is None:return
    item=db.get(Document,doc_id)
    if item is None or item.patient_id!=patient_id:raise HTTPException(400,'Source document does not belong to this profile')
    if not 1<=page<=item.page_count:raise HTTPException(422,'Source page is outside the original document')

def as_json(obj):
    def val(v):
        if isinstance(v,(date,datetime)):return v.isoformat()
        return v
    return {col.name:val(getattr(obj,col.name)) for col in obj.__table__.columns if col.name not in ['password_salt','password_hash','token_hash','extracted_cipher','storage_name','sha256']}

@app.post('/api/auth/register',status_code=201)
def register(body:Credentials,response:Response,db:DbSession=Depends(get_db)):
    if db.query(User).filter(User.email==body.email).first():raise HTTPException(409,'Email already registered')
    salt,digest=create_password(body.password)
    user=User(email=body.email,password_salt=salt,password_hash=digest)
    db.add(user);db.commit();db.refresh(user)
    token=issue_session(db,user)
    if body.client == 'web':
        from .config import PRODUCTION
        response.set_cookie('babycare_session',token,httponly=True,secure=PRODUCTION,samesite='strict',max_age=14*86400,path='/')
        return {'session':True,'user':{'id':user.id,'email':user.email}}
    return {'token':token,'user':{'id':user.id,'email':user.email}}

@app.post('/api/auth/login')
def login(body:Credentials,response:Response,db:DbSession=Depends(get_db)):
    user=db.query(User).filter(User.email==body.email).first()
    import hmac
    if user is None or not hmac.compare_digest(hash_password(body.password,user.password_salt),user.password_hash):
        raise HTTPException(401,'Incorrect email or password')
    token=issue_session(db,user)
    if body.client == 'web':
        from .config import PRODUCTION
        response.set_cookie('babycare_session',token,httponly=True,secure=PRODUCTION,samesite='strict',max_age=14*86400,path='/')
        return {'session':True,'user':{'id':user.id,'email':user.email}}
    return {'token':token,'user':{'id':user.id,'email':user.email}}

@app.post('/api/auth/logout')
def logout(response:Response,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    db.query(AuthSession).filter(AuthSession.token_hash==user._session_hash).delete()
    response.delete_cookie('babycare_session',path='/')
    db.commit()
    return {'ok':True}

@app.get('/api/me')
def me(user:User=Depends(require_user)):
    return {'id':user.id,'email':user.email}

@app.post('/api/patients',status_code=201)
def create_patient(body:PatientIn,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    patient=Patient(user_id=user.id,**body.model_dump())
    db.add(patient);db.flush();audit(db,user,patient.id,'create','patient',patient.id);db.commit()
    from .profiles import profile_json
    return profile_json(patient)

@app.get('/api/patients')
def patients(user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    from .profiles import profile_json
    return [profile_json(p) for p in db.query(Patient).filter(or_(Patient.user_id==user.id,Patient.id.in_(select(CaregiverAccess.patient_id).where(CaregiverAccess.user_id==user.id)))).order_by(Patient.id).all()]

DOCUMENT_CATEGORIES=('other','vaccination_card','prescription','lab','culture','discharge','hospital','birth','imaging','bill','admission','consultation')

@app.post('/api/patients/{pid}/documents',status_code=201)
async def upload(pid:int,file:UploadFile=File(...),category:str=Form('other'),title:str=Form(''),record_date:date|None=Form(None),source_ref:str=Form(''),import_batch:str=Form(''),notes:str=Form(''),user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid)
    if category not in DOCUMENT_CATEGORIES:
        raise HTTPException(422,'Unsupported document category')
    if len(title)>180:raise HTTPException(422,'Title too long')
    source_ref=source_ref.strip()[:40] or None;import_batch=import_batch.strip()[:80] or None;notes=notes.strip()[:2000]
    if source_ref and not re.fullmatch(r'[A-Za-z0-9_.\-]{1,40}',source_ref):raise HTTPException(422,'Source reference may contain letters, digits, dot, underscore and dash only')
    if source_ref and db.query(Document).filter(Document.patient_id==pid,Document.source_ref==source_ref).first():
        raise HTTPException(409,f'Source reference {source_ref} was already imported as document #{db.query(Document).filter(Document.patient_id==pid,Document.source_ref==source_ref).first().id}')
    page_count=1
    raw=await file.read(12*1024*1024+1)
    if len(raw)>12*1024*1024:raise HTTPException(413,'File limit: 12 MB')
    if raw.startswith(b'%PDF-'):mime='application/pdf'
    elif raw.startswith(b'\x89PNG\r\n\x1a\n'):mime='image/png'
    elif raw.startswith(b'\xff\xd8\xff'):mime='image/jpeg'
    elif len(raw)>12 and raw[4:8]==b'ftyp' and raw[8:12] in (b'heic',b'heix',b'mif1',b'hevc'):mime='image/heic'
    else:raise HTTPException(415,'Only PDF, PNG, JPEG and supported HEIC images accepted')
    try:
        if mime=='application/pdf':
            import fitz
            with fitz.open(stream=raw,filetype='pdf') as doc:
                if doc.is_encrypted:raise HTTPException(400,'Password-protected PDFs cannot be read')
                page_count=doc.page_count
                if doc.page_count>35:raise HTTPException(400,'Split PDFs over 35 pages')
        else:
            from PIL import Image
            from .extract import heif_support
            if mime=='image/heic' and not heif_support():raise HTTPException(415,'HEIC decoding is unavailable on this server; convert the photo to JPEG or PNG')
            import io
            with Image.open(io.BytesIO(raw)) as image:
                if image.width*image.height>25_000_000: raise HTTPException(413,'Image exceeds 25 megapixels')
                image.verify()
    except HTTPException:raise
    except Exception:raise HTTPException(400,'Damaged or invalid file')
    digest=hashlib.sha256(raw).hexdigest()
    existing=db.query(Document).filter(Document.patient_id==pid,Document.sha256==digest).first()
    if existing:raise HTTPException(409,f'This file was already uploaded as document #{existing.id}')
    storage_name=secrets.token_hex(24)+'.enc'
    try:write_document(storage_name,encrypt(raw))
    except Exception:raise HTTPException(503,'Encrypted document store unavailable; nothing was saved')
    name=Path(file.filename or 'upload').name[:180]
    doc=Document(patient_id=pid,title=title.strip() or name,category=category,record_date=record_date,mime=mime,original_name=name,storage_name=storage_name,sha256=digest,page_count=page_count,source_ref=source_ref,import_batch=import_batch,notes=notes)
    try:
        db.add(doc);db.flush();audit(db,user,pid,'upload','document',doc.id);db.commit()
    except Exception:
        try:delete_document(storage_name)
        except Exception:pass
        raise
    return as_json(doc)

@app.get('/api/patients/{pid}/documents')
def documents(pid:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid)
    return [as_json(d) for d in db.query(Document).filter(Document.patient_id==pid).order_by(Document.record_date.desc().nullslast(),Document.uploaded_at.desc()).all()]

class DocumentMeta(BaseModel):
    title:str|None=Field(default=None,min_length=1,max_length=180)
    category:Literal['other','vaccination_card','prescription','lab','culture','discharge','hospital','birth','imaging','bill','admission','consultation']|None=None
    record_date:date|None=None
    notes:str|None=Field(default=None,max_length=2000)

@app.patch('/api/documents/{doc_id}')
def edit_document(doc_id:int,body:DocumentMeta,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    d=own_resource(db,user,Document,doc_id)
    remember(db,user,d)
    for k in body.model_fields_set:
        value=getattr(body,k)
        if value is None and k!='record_date':raise HTTPException(422,'This field cannot be null')
        setattr(d,k,value)
    audit(db,user,d.patient_id,'update_document_metadata','document',doc_id);db.commit()
    return as_json(d)

@app.get('/api/documents/{doc_id}/download')
def download(doc_id:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    d=own_resource(db,user,Document,doc_id)
    try:raw=read_document(d.storage_name)
    except MissingObject:raise HTTPException(404,'Encrypted file is missing')
    name=re.sub(r'[^a-zA-Z0-9 ._\-]','_',d.original_name)
    return Response(content=decrypt(raw),media_type=d.mime,headers={'Content-Disposition':f'attachment; filename="{name}"','X-Content-Type-Options':'nosniff'})

@app.post('/api/documents/{doc_id}/extract')
def document_extract(doc_id:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    d=own_resource(db,user,Document,doc_id)
    if d.extraction_status=='reviewed':raise HTTPException(409,'The confirmed review is preserved; this document has already been reviewed')
    try:text=extract_text(decrypt(read_document(d.storage_name)),d.mime)
    except MissingObject:raise HTTPException(404,'Encrypted file is missing')
    except ValueError as exc:raise HTTPException(400,str(exc))
    except Exception:raise HTTPException(503,'Local OCR unavailable or failed; use the original card and manual entry')
    d.extracted_cipher=encrypt(text.encode()).decode()
    if d.category=='other':
        from .extraction import classify
        d.category=classify(text)
    d.extraction_status='needs_review' # OCR NEVER confirms any medical fact or activates reminders.
    audit(db,user,d.patient_id,'extract_unverified','document',doc_id);db.commit()
    return {'id':d.id,'status':'needs_review','text':text,'vaccine_candidates':card_candidates(text) if d.category=='vaccination_card' else [],'medication_candidates':medication_candidates(text) if d.category=='prescription' else [],'warning':'OCR is only a candidate transcription. Confirm every date and vaccine against the original.'}

@app.get('/api/documents/{doc_id}/extraction')
def get_extraction(doc_id:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    d=own_resource(db,user,Document,doc_id)
    text=decrypt(d.extracted_cipher.encode()).decode() if d.extracted_cipher else ''
    return {'id':d.id,'status':d.extraction_status,'text':text,'vaccine_candidates':card_candidates(text) if d.category=='vaccination_card' else [],'medication_candidates':medication_candidates(text) if d.category=='prescription' else [],'warning':'Review against original document; no automatic clinical scheduling.'}

@app.post('/api/patients/{pid}/medicines',status_code=201)
def add_medicine(pid:int,body:MedicineIn,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid);safe_doc(db,pid,body.source_document_id,body.source_page)
    m=Medicine(patient_id=pid,**body.model_dump(),status='draft')
    db.add(m);db.flush();audit(db,user,pid,'create_draft','medicine',m.id);db.commit()
    return as_json(m)

@app.get('/api/patients/{pid}/medicines')
def list_medicines(pid:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid)
    ms=db.query(Medicine).filter(Medicine.patient_id==pid).order_by(Medicine.created_at.desc()).all()
    records=[]
    for m in ms:
        events=db.query(MedicationEvent).filter(MedicationEvent.medicine_id==m.id).order_by(MedicationEvent.occurred_at).all()
        given=[e.occurred_at for e in events if e.event_type=='given']
        records.append({**as_json(m),'events':[as_json(e) for e in events[-120:][::-1]],
                        'first_reported_given':given[0].isoformat() if given else None,
                        'last_reported_given':given[-1].isoformat() if given else None,
                        'reported_given_count':len(given)})
    return records

@app.post('/api/medicines/{mid}/confirm')
def confirm_medicine(mid:int,body:VerifyIn,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    m=own_resource(db,user,Medicine,mid)
    if not m.instructions.strip():raise HTTPException(422,'Doctor-written instructions are required')
    if m.schedule_times and (m.start_date is None or m.end_date is None):
        raise HTTPException(422,'Exact start/end dates required for timed reminders')
    if not m.source_document_id:raise HTTPException(422,'Link the actual prescription or treatment order before confirmation')
    m.status='verified';m.verified_at=utcnow()
    audit(db,user,m.patient_id,'guardian_verified_against_document','medicine',mid);db.commit()
    return as_json(m)

@app.patch('/api/medicines/{mid}')
def revise_medicine(mid:int,body:MedicineIn,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    m=own_resource(db,user,Medicine,mid)
    snap=as_json(m)
    changes=body.model_dump(exclude_unset=True)
    try:merged=MedicineIn.model_validate({**snap,**changes})
    except ValueError:raise HTTPException(422,'The revised course has inconsistent dates or instructions')
    safe_doc(db,m.patient_id,merged.source_document_id,merged.source_page)
    db.add(MedicineRevision(medicine_id=m.id,version=m.version,snapshot_json=json.dumps(snap)))
    for k,v in changes.items():setattr(m,k,v)
    m.version+=1;m.status='draft';m.verified_at=None;m.revised_at=utcnow()
    audit(db,user,m.patient_id,'revised_requires_reconfirmation','medicine',mid);db.commit()
    return as_json(m)

@app.get('/api/medicines/{mid}/revisions')
def medicine_revisions(mid:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_resource(db,user,Medicine,mid)
    return [dict(version=r.version,snapshot=json.loads(r.snapshot_json),changed_at=r.changed_at.isoformat()) for r in db.query(MedicineRevision).filter(MedicineRevision.medicine_id==mid).order_by(MedicineRevision.version.desc())]

@app.post('/api/medicines/{mid}/events',status_code=201)
def log_med_event(mid:int,body:MedicineEventIn,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    m=own_resource(db,user,Medicine,mid)
    if m.status!='verified':raise HTTPException(409,'Cannot record administration against an unverified or revised draft')
    found=db.query(MedicationEvent).filter(MedicationEvent.medicine_id==mid,MedicationEvent.idempotency_key==body.idempotency_key).first()
    if found:return as_json(found)
    when=body.occurred_at
    if when.tzinfo is not None:raise HTTPException(422,'Use patient local clock time without timezone suffix')
    if when>datetime.now(ZoneInfo(db.get(Patient,m.patient_id).tz)).replace(tzinfo=None)+timedelta(minutes=5):raise HTTPException(422,'Actual administration cannot be in the future')
    safe_doc(db,m.patient_id,body.source_document_id,body.source_page)
    if body.source=='copied_from_hospital_document' and not body.source_document_id:raise HTTPException(422,'Attach the actual hospital administration record')
    from .reminders import local_time
    try:local_time(when,db.get(Patient,m.patient_id).tz)
    except ValueError as exc:raise HTTPException(422,str(exc))
    event=MedicationEvent(medicine_id=mid,medicine_version=m.version,**body.model_dump())
    db.add(event);db.flush();audit(db,user,m.patient_id,'record_caregiver_event','medicine_event',event.id);db.commit()
    return as_json(event)

@app.post('/api/patients/{pid}/hospital-visits',status_code=201)
def add_visit(pid:int,body:VisitIn,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    p=own_patient(db,user,pid);safe_doc(db,pid,body.source_document_id,body.source_page)
    from .reminders import local_time
    try:local_time(body.planned_at,p.tz)
    except ValueError as exc:raise HTTPException(422,str(exc))
    if body.confirmed and not body.source_document_id:
        raise HTTPException(422,'Attach a hospital-issued appointment/order before confirming')
    if body.admission_id:
        admission=db.get(Admission,body.admission_id)
        if not admission or admission.patient_id!=pid:raise HTTPException(400,'Admission belongs to a different profile')
    v=HospitalVisit(patient_id=pid,**body.model_dump())
    db.add(v);db.flush();audit(db,user,pid,'create','hospital_visit',v.id);db.commit()
    return as_json(v)

@app.get('/api/patients/{pid}/hospital-visits')
def visits(pid:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid)
    return [as_json(x) for x in db.query(HospitalVisit).filter(HospitalVisit.patient_id==pid).order_by(HospitalVisit.planned_at.desc()).all()]

@app.patch('/api/hospital-visits/{vid}')
def change_visit(vid:int,body:VisitUpdate,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    v=own_resource(db,user,HospitalVisit,vid)
    remember(db,user,v)
    if v.status!=body.status:v.version+=1
    v.status=body.status;v.notes=(v.notes+'\n'+body.note).strip()[:1000]
    # Attended means visited the facility; NOT that any medication was infused.
    audit(db,user,v.patient_id,'update_appointment_only','hospital_visit',vid);db.commit()
    return as_json(v)

@app.get('/api/patients/{pid}/vaccines')
def vaccines(pid:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid)
    return [as_json(x) for x in db.query(Vaccine).filter(Vaccine.patient_id==pid).order_by(Vaccine.due_date.asc().nullslast(),Vaccine.id).all()]

@app.post('/api/patients/{pid}/vaccines',status_code=201)
def add_vaccine(pid:int,body:VaccineIn,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid);safe_doc(db,pid,body.source_document_id,body.source_page)
    if body.given_date and not db.get(Patient,pid).dob<=body.given_date<=datetime.now(ZoneInfo(db.get(Patient,pid).tz)).date():
        raise HTTPException(422,'Recorded administered vaccine date must be between birth and today')
    from .workflows import check_vaccine_duplicate
    check_vaccine_duplicate(db,pid,body)
    v=Vaccine(patient_id=pid,**body.model_dump(exclude={'duplicate_confirmed'}),confirmed=False)
    db.add(v);db.flush();audit(db,user,pid,'add_unverified','vaccine',v.id);db.commit()
    return as_json(v)

@app.patch('/api/vaccines/{vid}')
def edit_vaccine(vid:int,body:VaccineUpdate,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    v=own_resource(db,user,Vaccine,vid)
    remember(db,user,v)
    fields=body.model_fields_set
    if 'source_document_id' in fields:safe_doc(db,v.patient_id,body.source_document_id)
    doc_id=body.source_document_id if 'source_document_id' in fields else v.source_document_id
    type_name=body.source_type if 'source_type' in fields else v.source_type
    given_date=body.given_date if 'given_date' in fields else v.given_date
    if given_date and not db.get(Patient,v.patient_id).dob<=given_date<=datetime.now(ZoneInfo(db.get(Patient,v.patient_id).tz)).date():
        raise HTTPException(422,'Recorded administered vaccine date must be between birth and today')
    if body.confirmed and (not doc_id or type_name=='India_UIP_reference'):
        raise HTTPException(422,'Reference-only dates require a real vaccination card or doctor schedule before confirmation')
    if body.confirmed and not ((body.due_date if 'due_date' in fields else v.due_date) or (body.given_date if 'given_date' in fields else v.given_date)):
        raise HTTPException(422,'Cannot confirm a vaccine entry with no recorded due/given date')
    for k in fields:
        if getattr(body,k) is None and k not in ('due_date','given_date','source_document_id'):raise HTTPException(422,'This field cannot be null')
        setattr(v,k,getattr(body,k))
    safe_doc(db,v.patient_id,v.source_document_id,v.source_page)
    v.version+=1
    if ('due_date' in fields or 'given_date' in fields or 'name' in fields or 'dose_label' in fields or 'source_document_id' in fields or 'source_type' in fields or 'source_page' in fields or 'reminder_days' in fields) and 'confirmed' not in fields:
        v.confirmed=False # material changes require reconfirmation
    audit(db,user,v.patient_id,'update_vaccination_record','vaccine',vid);db.commit()
    return as_json(v)

@app.get('/api/patients/{pid}/vaccine-reference')
def vaccine_reference(pid:int,schedule:Literal['India_UIP','IAP_private']='India_UIP',user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    p=own_patient(db,user,pid)
    return {'source':'IAP ACVIP: private-clinic reference' if schedule=='IAP_private' else 'India UIP/NIS: Government of India', 'warnings':['Reference dates are NOT individualized eligibility or catch-up recommendations','Your vaccination card and treating clinician take precedence','JE is applicable only in specific areas. Schedules can change.'], 'schedule':schedule,'entries':reference(p.dob,schedule)}

@app.post('/api/patients/{pid}/vaccine-reference/import')
def import_vaccine_reference(pid:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    p=own_patient(db,user,pid)
    inserted=0
    for candidate in reference(p.dob):
        present=next((v for v in db.query(Vaccine).filter_by(patient_id=pid) if v.name==candidate['name'] and v.dose_label==candidate['dose_label']),None)
        if present:continue
        db.add(Vaccine(patient_id=pid,name=candidate['name'],dose_label=candidate['dose_label'],due_date=date.fromisoformat(candidate['reference_date']),source_type='India_UIP_reference',note='Reference only: confirm actual due date with the vaccination card/doctor',confirmed=False))
        inserted+=1
    db.flush();audit(db,user,pid,'import_unverified_reference','vaccine',pid);db.commit()
    return {'created':inserted,'warning':'Reference entries are unconfirmed drafts. NO vaccine reminders will be generated until confirmed.'}

@app.post('/api/patients/{pid}/observations',status_code=201)
def add_observation(pid:int,body:ObservationIn,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid);safe_doc(db,pid,body.source_document_id,body.source_page)
    if body.verified and not body.source_document_id:raise HTTPException(422,'Attach the laboratory report before marking this result verified')
    item=Observation(patient_id=pid,**body.model_dump())
    db.add(item);db.flush();audit(db,user,pid,'add_manual_result','observation',item.id);db.commit()
    return as_json(item)

@app.get('/api/patients/{pid}/observations')
def observations(pid:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid)
    return [as_json(x) for x in db.query(Observation).filter(Observation.patient_id==pid).order_by(Observation.record_date.desc()).all()]

from .reminders import reminder_items

@app.get('/api/patients/{pid}/reminders')
def reminders(pid:int,days:int=Query(default=30,ge=0,le=120),user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    p=own_patient(db,user,pid)
    return {'timezone':p.tz,'delivery':'Persisted reminders; independent worker delivers in-app and configured remote push', 'items':reminder_items(db,p,days)}

@app.get('/api/patients/{pid}/today')
def today(pid:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    p=own_patient(db,user,pid)
    local_today=datetime.now(ZoneInfo(p.tz)).date().isoformat()
    rr=reminder_items(db,p,1)
    return {'date':local_today,'patient':as_json(p),'reminders':[x for x in rr if x['at'].startswith(local_today)],'draft_medicines':db.query(Medicine).filter(Medicine.patient_id==pid,Medicine.status=='draft').count(),'unconfirmed_vaccines':db.query(Vaccine).filter(Vaccine.patient_id==pid,Vaccine.confirmed==False).count()}

@app.get('/api/patients/{pid}/timeline')
def timeline(pid:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid)
    items=[]
    for doc in db.query(Document).filter(Document.patient_id==pid).all():
        items.append({'date':doc.record_date.isoformat() if doc.record_date else None,'kind':'document','title':doc.title,'source_document_id':doc.id,'note':doc.category,'source_ref':doc.source_ref,'record_id':doc.id})
    for med in db.query(Medicine).filter(Medicine.patient_id==pid).all():
        items.append({'date':(med.prescription_date or med.start_date).isoformat() if (med.prescription_date or med.start_date) else None,'kind':'medicine','title':med.name,'source_document_id':med.source_document_id,'source_page':med.source_page,'note':f'{med.course_type.replace("_"," ")}; {med.status}','record_id':med.id})
    for visit in db.query(HospitalVisit).filter(HospitalVisit.patient_id==pid).all():
        items.append({'date':visit.planned_at.date().isoformat(),'kind':'hospital','title':visit.purpose,'source_document_id':visit.source_document_id,'note':visit.status})
    for vaccine in db.query(Vaccine).filter(Vaccine.patient_id==pid).all():
        if vaccine.given_date and vaccine.confirmed:
            items.append({'date':vaccine.given_date.isoformat(),'kind':'vaccine_given','title':f'{vaccine.name} {vaccine.dose_label}','source_document_id':vaccine.source_document_id,'note':'Recorded administration; confirm provenance'})
    from .reporting import additional_timeline
    items.extend(additional_timeline(db,pid))
    return sorted(items,key=lambda x:x['date'] or '0000-00-00',reverse=True)

@app.get('/api/patients/{pid}/search')
def search(pid:int,q:str=Query(min_length=2,max_length=150),user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid)
    key=q.casefold();results=[]
    for m in db.query(Medicine).filter(Medicine.patient_id==pid).all():
        if key in (m.name+' '+m.instructions).casefold():
            results.append({'type':'medicine','title':m.name,'date':m.start_date.isoformat() if m.start_date else None,'details':f'{m.instructions} | Status: {m.status}; planned end: {m.end_date or "not documented"}','source_document_id':m.source_document_id})
    for d in db.query(Document).filter(Document.patient_id==pid).all():
        body=decrypt(d.extracted_cipher.encode()).decode() if d.extracted_cipher else ''
        offset=body.casefold().find(key)
        if key in (d.title+' '+d.category).casefold() or offset>=0:
            snippet=body[max(0,offset-65):offset+175] if offset>=0 else 'Document metadata match; text has not been verified'
            results.append({'type':'document','title':d.title,'date':d.record_date.isoformat() if d.record_date else None,'details':snippet,'source_document_id':d.id,'certainty':'OCR unverified' if offset>=0 else 'metadata'})
    for v in db.query(Vaccine).filter(Vaccine.patient_id==pid).all():
        if key in (v.name+' '+v.dose_label).casefold():
            results.append({'type':'vaccine','title':v.name+' '+v.dose_label,'date':v.due_date.isoformat() if v.due_date else None,'details':f'Due {v.due_date or "unknown"}; given {v.given_date or "not recorded"}; confirmed {v.confirmed}','source_document_id':v.source_document_id})
    return {'query':q,'results':results[:100],'warning':'Search is keyword-based, not a diagnosis. OCR matches are unverified.'}

@app.get('/api/patients/{pid}/doctor-summary',response_class=PlainTextResponse)
def doctor_summary(pid:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    p=own_patient(db,user,pid)
    from .reporting import medical_summary
    return PlainTextResponse(medical_summary(db,p),headers={'Content-Disposition':'attachment; filename="medical-summary.txt"'})

@app.get('/api/health')
def health():return {'ok':True,'mode':'personal health record','medical_device':False}

from .workflows import router
app.include_router(router)
from .providers import router as provider_router
app.include_router(provider_router)
from .history_api import router as history_router
app.include_router(history_router)
from .lifetime_api import router as lifetime_router
app.include_router(lifetime_router)
from .cloud import router as cloud_router
app.include_router(cloud_router)
from .share import router as share_router
app.include_router(share_router)

from .care_api import router as care_router
app.include_router(care_router)
from .camera_api import router as camera_router
app.include_router(camera_router)
from .export_api import router as export_router
app.include_router(export_router)
from .vitals_api import router as vitals_router
app.include_router(vitals_router)
from .profiles_api import router as profiles_router
app.include_router(profiles_router)
from .password_reset import router as password_reset_router
app.include_router(password_reset_router)
WEB_DIR=Path(__file__).resolve().parent.parent/'web'
if WEB_DIR.exists():
    app.mount('/',StaticFiles(directory=str(WEB_DIR),html=True),name='web')
