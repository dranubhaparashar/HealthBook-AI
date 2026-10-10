"""Daily Care API: typed journal entries, stool photos with image-based observations, medicine administration
calendar, unified day/week/month views, analytics, illness comparison, export and settings."""
import hashlib
import io
import json
import secrets
from datetime import date, datetime, timedelta, timezone
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Form, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session as DbSession
from .database import get_db
from .security import require_user, own_patient, own_resource
from .models import User, Patient, Medicine, Episode, Audit, utcnow
from .lifetime_models import FeverEpisode, AntibioticCourse
from .care_models import (CareSettings, CareDay, FluidEntry, DiaperEntry, StoolEntry, StoolPhoto, StoolIntake, MedicationAdministration, SymptomEntry, ActivityEntry, SleepEntry,
    ENTRY_MODELS, FLUID_TYPES, DIAPER_KINDS, URINE_AMOUNTS, STOOL_COLOURS, STOOL_CONSISTENCIES, STOOL_AMOUNTS, ADMIN_STATUSES, SYMPTOM_KINDS, ACTIVITY_KINDS, SLEEP_KINDS)
from .history import remember
from .models import RecordRevision
from .crypto import encrypt, decrypt
from . import storage
from . import care as C
from . import care_vision
from . import stool_intake as SI

router=APIRouter(prefix='/api')

def audit(db,user,pid,action,entity,obj_id):db.add(Audit(user_id=user.id,patient_id=pid,action=action,entity=entity,entity_id=obj_id))

# ----------------------------------------------------------------------------------- schemas
class EntryBase(BaseModel):
    at:datetime|str
    note:str=Field(default='',max_length=2000)
    client_key:str|None=Field(default=None,min_length=6,max_length=80)

class FluidIn(EntryBase):
    amount_ml:float|None=Field(default=None,ge=0,le=3000)
    amount_unknown:bool=False
    custom_amount:str=Field(default='',max_length=80)
    unit:Literal['ml','oz','cup','other']='ml'
    fluid_type:Literal[FLUID_TYPES]='water'
    duration_minutes:int|None=Field(default=None,ge=0,le=600)
    @model_validator(mode='after')
    def amount_rule(self):
        if self.unit=='oz' and self.amount_ml is not None:self.amount_ml=round(self.amount_ml*29.5735,1);self.unit='ml'
        if self.amount_ml is None and not self.amount_unknown and not self.custom_amount and self.fluid_type!='breastmilk':raise ValueError('Enter an amount, a custom description, or mark the amount unknown')
        if self.amount_ml is None and not self.custom_amount:self.amount_unknown=True
        return self

class DiaperIn(EntryBase):
    kind:Literal[DIAPER_KINDS]
    urine_amount:Literal[URINE_AMOUNTS]=''
    stool_entry_id:int|None=None

class StoolIn(EntryBase):
    colour:Literal[STOOL_COLOURS]=''
    consistency:Literal[STOOL_CONSISTENCIES]=''
    amount:Literal[STOOL_AMOUNTS]=''
    mucus_observed:bool|None=None
    blood_observed:bool|None=None
    watery:bool=False
    parent_concern:Literal['none','mild','moderate','high']='none'
    symptoms:str=Field(default='',max_length=1000)
    illness_episode_id:int|None=None
    create_diaper:bool=False

class MedicationIn(EntryBase):
    medicine_id:int|None=None
    medicine_name:str=Field(default='',max_length=150)
    dose:str=Field(default='',max_length=60)
    dose_unit:str=Field(default='',max_length=20)
    route:str=Field(default='',max_length=20)
    scheduled_for:datetime|str|None=None
    administration_status:Literal[ADMIN_STATUSES]='given'
    antibiotic_course_id:int|None=None
    @model_validator(mode='after')
    def named(self):
        if not self.medicine_id and not self.medicine_name.strip():raise ValueError('Choose a medicine or type its name')
        return self

class SymptomIn(EntryBase):
    kind:Literal[SYMPTOM_KINDS]
    ended_at:datetime|str|None=None
    severity:Literal['','mild','moderate','severe']=''
    temperature:str=Field(default='',max_length=10)
    temperature_unit:Literal['','F','C']=''
    illness_episode_id:int|None=None
    fever_episode_id:int|None=None
    @model_validator(mode='after')
    def unit_rule(self):
        if self.temperature and not self.temperature_unit:raise ValueError('Give the temperature unit')
        return self

class ActivityIn(EntryBase):
    kind:Literal[ACTIVITY_KINDS]
    ended_at:datetime|str|None=None
    duration_minutes:int|None=Field(default=None,ge=1,le=1440)

class SleepIn(EntryBase):
    kind:Literal[SLEEP_KINDS]='nap'
    ended_at:datetime|str|None=None
    duration_minutes:int|None=Field(default=None,ge=1,le=1440)
    location:str=Field(default='',max_length=120)

class SettingsIn(BaseModel):
    wet_diaper_threshold_per_day:int|None=Field(default=None,ge=0,le=40)
    threshold_source:str=Field(default='',max_length=300)
    fluid_quick_amounts_ml:str=Field(default='30,60,100,150',pattern=r'^\d{1,4}(,\d{1,4}){0,7}$')
    default_fluid_type:Literal[FLUID_TYPES]='water'
    @model_validator(mode='after')
    def source_required(self):
        if self.wet_diaper_threshold_per_day is not None and len(self.threshold_source.strip())<3:raise ValueError('Record who gave you this threshold (for example the clinician and date); the app does not provide thresholds')
        return self

class DayNoteIn(BaseModel):
    day_note:str=Field(default='',max_length=3000)

class ReviewIn(BaseModel):
    action:Literal['confirm','correct','dismiss']
    corrections:dict=Field(default_factory=dict)

SCHEMAS={'fluid':FluidIn,'diaper':DiaperIn,'stool':StoolIn,'medicine':MedicationIn,'symptom':SymptomIn,'activity':ActivityIn,'sleep':SleepIn}

# ----------------------------------------------------------------------------------- helpers
def check_ref(db,pid,model,ref_id,label):
    if ref_id is None:return
    obj=db.get(model,ref_id)
    if not obj or obj.patient_id!=pid:raise HTTPException(400,f'{label} does not belong to this child')

def prepare(db,p,kind,body):
    data=body.model_dump();when=C.parse_when(data.pop('at'),p)
    if when>utcnow()+timedelta(minutes=5):raise HTTPException(422,'Entries cannot be in the future')
    if when.date()<p.dob-timedelta(days=1):raise HTTPException(422,'Entries cannot be before birth')
    for key in ('ended_at','scheduled_for'):
        if key in data and data[key]:data[key]=C.parse_when(data[key],p)
    if data.get('ended_at') and data['ended_at']<when:raise HTTPException(422,'End time precedes start time')
    if kind in ('activity','sleep') and data.get('ended_at') and not data.get('duration_minutes'):data['duration_minutes']=C.minutes_between(when,data['ended_at'])
    if kind=='stool':
        check_ref(db,p.id,Episode,data.get('illness_episode_id'),'Illness episode')
        data['clinical_review_suggested']=data['colour'] in ('black','red','pale','white') or bool(data.get('blood_observed'))
        data.pop('create_diaper',None)
    if kind=='diaper':check_ref(db,p.id,StoolEntry,data.get('stool_entry_id'),'Stool entry')
    if kind=='symptom':check_ref(db,p.id,Episode,data.get('illness_episode_id'),'Illness episode');check_ref(db,p.id,FeverEpisode,data.get('fever_episode_id'),'Fever episode')
    if kind=='medicine':
        check_ref(db,p.id,Medicine,data.get('medicine_id'),'Medicine');check_ref(db,p.id,AntibioticCourse,data.get('antibiotic_course_id'),'Antibiotic record')
        if data.get('medicine_id'):
            m=db.get(Medicine,data['medicine_id']);data['medicine_name']=data['medicine_name'].strip() or m.name
            if not data.get('dose') and m.dosage:data['dose']=m.dosage;data['dose_unit']=data.get('dose_unit') or m.dose_unit
            if not data.get('route'):data['route']=(m.route or '')[:20]
    data.update(at=when,local_date=C.local_date_of(when,p))
    return data

def existing_by_key(db,pid,model,key):
    return db.query(model).filter(model.patient_id==pid,model.client_key==key).first() if key else None

def ensure_day(db,pid,d):
    if not db.query(CareDay).filter_by(patient_id=pid,local_date=d).first():db.add(CareDay(patient_id=pid,local_date=d))

def out(db,x,p):
    return C.stool_row(db,x,p) if isinstance(x,StoolEntry) else C.row(x,p)

# ----------------------------------------------------------------------------------- quick medicine actions
class QuickDose(BaseModel):
    medicine_id:int|None=None
    medicine_name:str=Field(default='',max_length=150)
    scheduled_for:datetime|str|None=None
    action:Literal['given','skipped','later','missed']
    at:datetime|str|None=None
    dose:str=Field(default='',max_length=60)
    dose_unit:str=Field(default='',max_length=20)
    note:str=Field(default='',max_length=500)
    client_key:str|None=Field(default=None,min_length=6,max_length=80)

@router.post('/patients/{pid}/care/medicine/quick',status_code=201)
def quick_dose(pid:int,body:QuickDose,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    """GIVEN / SKIP / LATER / MISSED from the schedule card. Never called automatically."""
    p=own_patient(db,user,pid)
    payload=MedicationIn(at=body.at or utcnow(),medicine_id=body.medicine_id,medicine_name=body.medicine_name,scheduled_for=body.scheduled_for,administration_status=body.action,dose=body.dose,dose_unit=body.dose_unit,note=body.note,client_key=body.client_key)
    return entry_add(pid,'medicine',payload.model_dump(mode='json'),user,db)

@router.get('/patients/{pid}/care/medicines/today')
def medicines_today(pid:int,day:date|None=None,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    p=own_patient(db,user,pid);d=day or C.local_today(p)
    recent=[r[0] for r in db.query(MedicationAdministration.medicine_name).filter(MedicationAdministration.patient_id==pid,MedicationAdministration.local_date>=d-timedelta(days=14),MedicationAdministration.status=='active').distinct()]
    verified=[{'id':m.id,'name':m.name,'dose':m.dosage,'dose_unit':m.dose_unit,'route':m.route,'schedule_times':m.schedule_times,'status':m.status} for m in db.query(Medicine).filter(Medicine.patient_id==pid,Medicine.status.in_(('verified','draft'))).order_by(Medicine.status.desc(),Medicine.name)]
    return {**C.medicine_day(db,p,d),'quick_names':sorted(set(recent)),'medicines':verified,'warning':'Scheduled slots are reminders; only your GIVEN record counts as administered.'}

# ----------------------------------------------------------------------------------- stool photos
def _sniff(raw):
    if raw.startswith(b'\xff\xd8\xff'):return 'image/jpeg'
    if raw.startswith(b'\x89PNG\r\n\x1a\n'):return 'image/png'
    if len(raw)>12 and raw[4:8]==b'ftyp':return 'image/heic'
    if raw[:4]==b'RIFF' and raw[8:12]==b'WEBP':return 'image/webp'
    raise HTTPException(415,'JPEG, PNG, HEIC or WebP photo required')

def _dimensions(raw):
    try:
        from PIL import Image
        import pillow_heif;pillow_heif.register_heif_opener()
        with Image.open(io.BytesIO(raw)) as im:return im.size
    except Exception:raise HTTPException(400,'Damaged or unreadable photo')

def _detection(raw,filename,p,client_modified=None,client_exif=None):
    return SI.detect_timestamp(raw,filename or '',p.tz,client_modified=client_modified or None,client_exif=client_exif or None,now=utcnow(),dob=p.dob)

def _detected_view(det,p):
    dt=det.get('detected_at')
    return {'at':C.iso_local(dt,p) if dt else None,'date':C.local(dt,p).date().isoformat() if dt else None,'label':C.local(dt,p).strftime('%d %b %Y · %I:%M %p').replace(' 0',' ').replace('·  ','· ') if dt else 'Time not detected',
        'source':det['source'],'source_label':det['source_label'],'confidence':det['confidence'],'needs_confirmation':det['needs_confirmation'],'time_unknown':det.get('time_unknown',False),
        'tz':det['tz'],'camera':det.get('camera',''),'candidates':det.get('candidates',[]),'rejected':det.get('rejected',[])}

@router.post('/care/stool/{eid}/photos',status_code=201)
async def stool_photo_add(eid:int,file:UploadFile=File(...),taken_at:str|None=Form(None),analyse:bool=Form(True),file_modified:str|None=Form(None),client_exif:str|None=Form(None),user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    """Attach a photo to an existing stool entry. The capture time is detected (EXIF > file name > metadata) and
    stored with its source; taken_at from the client overrides it as a manual value."""
    x=own_resource(db,user,StoolEntry,eid);p=db.get(Patient,x.patient_id)
    raw=await file.read(12*1024*1024+1)
    if len(raw)>12*1024*1024:raise HTTPException(413,'Photo limit: 12 MB')
    mime=_sniff(raw);digest=hashlib.sha256(raw).hexdigest()
    dup=db.query(StoolPhoto).filter_by(stool_entry_id=eid,sha256=digest,deleted_at=None).first()
    if dup:return {'id':dup.id,'existing':True,'ai_observation':json.loads(x.ai_observation_json) if x.ai_observation_json else None}
    width,height=_dimensions(raw)
    det=_detection(raw,file.filename,p,file_modified,client_exif)
    name=secrets.token_hex(24)+'.enc'
    storage.write_document(name,encrypt(raw))
    taken=C.parse_when(taken_at,p) if taken_at else (det['detected_at'].astimezone(timezone.utc) if det['detected_at'] else None)
    photo=StoolPhoto(patient_id=x.patient_id,stool_entry_id=eid,storage_name=name,sha256=digest,mime=mime,width=width,height=height,taken_at=taken,
        detected_taken_at=det['detected_at'].astimezone(timezone.utc) if det['detected_at'] else None,taken_at_source='manual' if taken_at else det['source'],
        taken_at_confidence='confirmed' if taken_at else det['confidence'],taken_at_tz=p.tz,original_filename=det['original_filename'],
        metadata_json=json.dumps({'camera':det['camera'],'candidates':det['candidates'],'rejected':det['rejected']}))
    db.add(photo);db.flush()
    observation=None
    if analyse:
        try:
            observation=care_vision.observe(raw)
            x.ai_observation_json=json.dumps(observation);x.ai_review_status='ai_observed'
            if observation['consider_clinical_review']:x.clinical_review_suggested=True
        except Exception:
            observation={'error':'Image-based observation unavailable for this photo','disclaimer':care_vision.DISCLAIMER}
    audit(db,user,x.patient_id,'care_stool_photo_add','stool_photo',photo.id);db.commit()
    return {'id':photo.id,'mime':mime,'width':width,'height':height,'ai_observation':observation,'detected':_detected_view(det,p),'disclaimer':care_vision.DISCLAIMER}

# ----------------------------------------------------------------------------------- intelligent batch intake
INTAKE_TTL=timedelta(hours=48)

def _intake_view(db,it,p):
    det=json.loads(it.detection_json) if it.detection_json else {}
    sug=json.loads(it.suggestion_json) if it.suggestion_json else None
    dt=C.aware(it.detected_at) if it.detected_at else None
    dup=None
    if it.duplicate_of_entry_id:
        e=db.get(StoolEntry,it.duplicate_of_entry_id)
        if e:dup={'entry_id':e.id,'at':C.iso_local(e.at,p),'label':C.local(e.at,p).strftime('%d %b · %I:%M %p').replace(' 0',' ')}
    return {'id':it.id,'status':it.status,'mime':it.mime,'width':it.width,'height':it.height,'original_filename':it.original_filename,'sha256':it.sha256[:12],
        'detected':{'at':C.iso_local(dt,p) if dt else None,'date':C.local(dt,p).date().isoformat() if dt else None,
            'label':C.local(dt,p).strftime('%d %b %Y · %I:%M %p').replace(' 0',' ') if dt else 'Time not detected',
            'source':it.detected_source,'source_label':SI.SOURCE_LABEL.get(it.detected_source,it.detected_source),'confidence':it.detected_confidence,
            'needs_confirmation':it.detected_source in SI.NEEDS_CONFIRMATION,'time_unknown':it.detected_source=='filename_date','tz':it.detected_tz,
            'camera':det.get('camera',''),'candidates':det.get('candidates',[]),'rejected':det.get('rejected',[])},
        'suggestions':sug,'prefill':SI.form_prefill(sug),'duplicate_of':dup,'stool_entry_id':it.stool_entry_id,'image_url':f'/api/care/stool-intake/{it.id}/image','disclaimer':care_vision.DISCLAIMER}

def _expire_intakes(db,pid):
    now=utcnow()
    for it in db.query(StoolIntake).filter(StoolIntake.patient_id==pid,StoolIntake.status=='pending',StoolIntake.expires_at!=None,StoolIntake.expires_at<now).all():
        it.status='discarded'
        try:storage.delete_document(it.storage_name)
        except Exception:pass

def _intake_payload(db,p,items):
    views=[_intake_view(db,it,p) for it in items]
    views.sort(key=lambda v:(v['detected']['at'] is None,v['detected']['at'] or '',v['id']))
    by_date={}
    for v in views:
        if v['status']!='pending':continue
        d=v['detected']['date'] or 'unknown';by_date[d]=by_date.get(d,0)+1
    return {'count':len(views),'pending':sum(1 for v in views if v['status']=='pending'),'duplicates':sum(1 for v in views if v['status']=='duplicate'),
        'items':views,'by_date':[{'date':d,'count':c} for d,c in sorted(by_date.items())],'disclaimer':care_vision.DISCLAIMER}

@router.post('/patients/{pid}/care/stool/intake',status_code=201)
async def stool_intake_add(pid:int,files:list[UploadFile]=File(...),file_modified:list[str]=Form([]),client_exif:list[str]=Form([]),client_names:list[str]=Form([]),user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    """Upload one or many stool photos. Each becomes a pending intake card with its detected capture time, the
    photo's AI suggestions and a duplicate check (sha256 against this child's stored photos). Nothing is written to
    the journal until the parent confirms a card."""
    p=own_patient(db,user,pid)
    if len(files)>24:raise HTTPException(413,'Up to 24 photos per upload')
    _expire_intakes(db,pid)
    created=[]
    for i,f in enumerate(files):
        raw=await f.read(12*1024*1024+1)
        if len(raw)>12*1024*1024:raise HTTPException(413,f'Photo limit: 12 MB ({f.filename})')
        mime=_sniff(raw);digest=hashlib.sha256(raw).hexdigest();width,height=_dimensions(raw)
        filename=(client_names[i] if i<len(client_names) and client_names[i] else f.filename) or ''
        det=_detection(raw,filename,p,file_modified[i] if i<len(file_modified) else None,client_exif[i] if i<len(client_exif) else None)
        existing=db.query(StoolPhoto).join(StoolEntry,StoolEntry.id==StoolPhoto.stool_entry_id).filter(StoolPhoto.patient_id==pid,StoolPhoto.sha256==digest,StoolPhoto.deleted_at==None,StoolEntry.status=='active').first()
        pending_dup=db.query(StoolIntake).filter(StoolIntake.patient_id==pid,StoolIntake.sha256==digest,StoolIntake.status.in_(('pending','confirmed'))).first()
        if pending_dup and pending_dup.status=='pending':
            if pending_dup not in created:created.append(pending_dup)
            continue  # same file selected twice in one session: one card
        try:sug=SI.suggest(raw)
        except Exception:sug={'error':'Photo saved, but automatic observations could not be generated.','disclaimer':care_vision.DISCLAIMER}
        name=secrets.token_hex(24)+'.enc';storage.write_document(name,encrypt(raw))
        it=StoolIntake(patient_id=pid,user_id=user.id,storage_name=name,sha256=digest,mime=mime,width=width,height=height,original_filename=det['original_filename'],
            detected_at=det['detected_at'].astimezone(timezone.utc) if det['detected_at'] else None,detected_source=det['source'],detected_confidence=det['confidence'],detected_tz=p.tz,
            detection_json=json.dumps({'camera':det['camera'],'candidates':det['candidates'],'rejected':det['rejected'],'tz':det['tz']}),suggestion_json=json.dumps(sug),
            status='duplicate' if (existing or (pending_dup and pending_dup.stool_entry_id)) else 'pending',
            duplicate_of_entry_id=existing.stool_entry_id if existing else (pending_dup.stool_entry_id if pending_dup else None),expires_at=utcnow()+INTAKE_TTL)
        db.add(it);db.flush();created.append(it)
        audit(db,user,pid,'care_stool_intake_add','stool_intake',it.id)
    db.commit()
    return _intake_payload(db,p,created)

@router.get('/patients/{pid}/care/stool/photos')
def stool_photo_history(pid:int,start:date|None=None,end:date|None=None,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    p=own_patient(db,user,pid);end=end or C.local_today(p);start=start or end-timedelta(days=29)
    if (end-start).days>366:raise HTTPException(422,'Range limit: one year')
    return C.photo_history(db,pid,start,end)

@router.get('/patients/{pid}/care/stool/intake')
def stool_intake_list(pid:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    p=own_patient(db,user,pid);_expire_intakes(db,pid);db.commit()
    items=db.query(StoolIntake).filter(StoolIntake.patient_id==pid,StoolIntake.status.in_(('pending','duplicate'))).order_by(StoolIntake.id).all()
    return _intake_payload(db,p,items)

@router.get('/care/stool-intake/{iid}/image')
def stool_intake_image(iid:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    it=own_resource(db,user,StoolIntake,iid)
    if it.status in ('discarded',):raise HTTPException(404,'Photo discarded')
    if it.status=='confirmed' and it.stool_entry_id:
        ph=db.query(StoolPhoto).filter_by(storage_name=it.storage_name).first()
        if ph and ph.deleted_at:raise HTTPException(404,'Photo deleted')
    return Response(decrypt(storage.read_document(it.storage_name)),media_type=it.mime,headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})

class IntakeConfirmIn(BaseModel):
    at:datetime|str|None=None            # parent-corrected time; defaults to the detected time
    colour:Literal[STOOL_COLOURS]=''
    consistency:Literal[STOOL_CONSISTENCIES]=''
    amount:Literal[STOOL_AMOUNTS]=''
    mucus_observed:bool|None=None
    blood_observed:bool|None=None
    watery:bool=False
    parent_concern:Literal['none','mild','moderate','high']='none'
    symptoms:str=Field(default='',max_length=1000)
    note:str=Field(default='',max_length=2000)
    illness_episode_id:int|None=None
    create_diaper:bool=True
    client_key:str|None=Field(default=None,min_length=6,max_length=80)

@router.post('/care/stool-intake/{iid}/confirm',status_code=201)
def stool_intake_confirm(iid:int,body:IntakeConfirmIn,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    """Parent confirms one review card: creates the stool entry at the detected or corrected time, attaches the
    photo, optionally creates the stool diaper, and stores both the AI suggestion and the parent's values."""
    it=own_resource(db,user,StoolIntake,iid);p=db.get(Patient,it.patient_id)
    if it.status=='confirmed' and it.stool_entry_id:
        e=db.get(StoolEntry,it.stool_entry_id);return {**C.stool_row(db,e,p),'existing':True}
    if it.status=='discarded':raise HTTPException(410,'This photo was discarded')
    if it.status=='duplicate':
        e=db.get(StoolEntry,it.duplicate_of_entry_id) if it.duplicate_of_entry_id else None
        raise HTTPException(409,f'This photo is already recorded as stool entry on {C.local(e.at,p).strftime("%d %b %Y at %I:%M %p") if e else "an existing entry"}. Open that entry instead of adding it twice.')
    if body.at is None and it.detected_at is None:raise HTTPException(422,'No capture time could be detected for this photo: enter the date and time')
    if body.at is None and it.detected_source in SI.NEEDS_CONFIRMATION:raise HTTPException(422,'The detected time is weak (%s): confirm or correct the date and time'%SI.SOURCE_LABEL.get(it.detected_source,it.detected_source))
    when_in=body.at if body.at is not None else C.local(C.aware(it.detected_at),p).isoformat()
    stool_in=StoolIn(at=when_in,colour=body.colour,consistency=body.consistency,amount=body.amount,mucus_observed=body.mucus_observed,blood_observed=body.blood_observed,watery=body.watery,
        parent_concern=body.parent_concern,symptoms=body.symptoms,note=body.note,illness_episode_id=body.illness_episode_id,create_diaper=body.create_diaper,client_key=body.client_key)
    dup=existing_by_key(db,it.patient_id,StoolEntry,body.client_key)
    if dup:return {**C.stool_row(db,dup,p),'existing':True}
    data=prepare(db,p,'stool',stool_in)
    sug=json.loads(it.suggestion_json) if it.suggestion_json else {}
    prefill=SI.form_prefill(sug) if sug and 'error' not in sug else {}
    parent_values={k:data.get(k) for k in ('colour','consistency','amount','mucus_observed','blood_observed','watery','parent_concern')}
    changed=any(prefill.get(k)!=parent_values.get(k) for k in prefill) if prefill else True
    detected_utc=C.aware(it.detected_at) if it.detected_at else None
    time_changed=body.at is not None and (detected_utc is None or abs((data['at']-detected_utc).total_seconds())>60)
    entry=StoolEntry(patient_id=it.patient_id,recorded_by=user.id,**data,
        ai_observation_json=json.dumps(sug.get('observation')) if sug.get('observation') else '',ai_suggestion_json=json.dumps(sug) if sug else '',
        parent_values_json=json.dumps({**parent_values,'at':C.iso_local(data['at'],p),'time_source':'parent_corrected' if time_changed else it.detected_source,'confirmed_at':utcnow().isoformat()}),
        ai_review_status=('parent_corrected' if changed else 'parent_confirmed') if sug and 'error' not in sug else 'none',
        time_source='parent_corrected' if time_changed else (it.detected_source if it.detected_source!='none' else 'manual'))
    if sug.get('consider_clinical_review'):entry.clinical_review_suggested=True
    db.add(entry);db.flush();ensure_day(db,it.patient_id,entry.local_date)
    photo=StoolPhoto(patient_id=it.patient_id,stool_entry_id=entry.id,storage_name=it.storage_name,sha256=it.sha256,mime=it.mime,width=it.width,height=it.height,taken_at=entry.at,
        detected_taken_at=it.detected_at,taken_at_source='manual' if time_changed else it.detected_source,taken_at_confidence='confirmed' if time_changed else it.detected_confidence,
        taken_at_tz=it.detected_tz,original_filename=it.original_filename,metadata_json=it.detection_json)
    db.add(photo);db.flush()
    if body.create_diaper:
        db.add(DiaperEntry(patient_id=it.patient_id,recorded_by=user.id,at=entry.at,local_date=entry.local_date,kind='stool',stool_entry_id=entry.id,client_key=(body.client_key+':diaper') if body.client_key else None))
    it.status='confirmed';it.stool_entry_id=entry.id
    audit(db,user,it.patient_id,'care_stool_add','stool_entries',entry.id);audit(db,user,it.patient_id,'care_stool_photo_add','stool_photo',photo.id);audit(db,user,it.patient_id,'care_stool_intake_confirm','stool_intake',it.id)
    db.commit()
    return C.stool_row(db,entry,p)

@router.delete('/care/stool-intake/{iid}')
def stool_intake_discard(iid:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    it=own_resource(db,user,StoolIntake,iid)
    if it.status=='confirmed':raise HTTPException(409,'Already saved as a stool entry; withdraw the entry instead')
    if it.status!='discarded':
        it.status='discarded'
        try:storage.delete_document(it.storage_name)
        except Exception:pass
        audit(db,user,it.patient_id,'care_stool_intake_discard','stool_intake',it.id);db.commit()
    return {'ok':True,'id':iid,'status':'discarded'}

@router.get('/care/stool-photos/{photo_id}')
def stool_photo_get(photo_id:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    ph=own_resource(db,user,StoolPhoto,photo_id)
    if ph.deleted_at:raise HTTPException(404,'Photo deleted')
    return Response(decrypt(storage.read_document(ph.storage_name)),media_type=ph.mime,headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff'})

@router.delete('/care/stool-photos/{photo_id}')
def stool_photo_delete(photo_id:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    ph=own_resource(db,user,StoolPhoto,photo_id)
    ph.deleted_at=utcnow();storage.delete_document(ph.storage_name);audit(db,user,ph.patient_id,'care_stool_photo_delete','stool_photo',photo_id);db.commit();return {'ok':True}

@router.post('/care/stool/{eid}/observation-review')
def stool_observation_review(eid:int,body:ReviewIn,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    """Parent confirms, corrects or dismisses the image-based observation. Corrections write the parent's values."""
    x=own_resource(db,user,StoolEntry,eid);p=db.get(Patient,x.patient_id);remember(db,user,x)
    if body.action=='dismiss':x.ai_review_status='dismissed'
    elif body.action=='confirm':
        ai=json.loads(x.ai_observation_json) if x.ai_observation_json else {}
        if ai.get('apparent_colour') in STOOL_COLOURS and not x.colour:x.colour=ai['apparent_colour']
        x.ai_review_status='parent_confirmed'
    else:
        allowed={'colour':STOOL_COLOURS,'consistency':STOOL_CONSISTENCIES,'amount':STOOL_AMOUNTS}
        for k,v in body.corrections.items():
            if k in allowed and v in allowed[k]:setattr(x,k,v)
            elif k in ('mucus_observed','blood_observed'):setattr(x,k,bool(v))
            elif k=='note':x.note=str(v)[:2000]
        x.ai_review_status='parent_corrected'
    x.clinical_review_suggested=x.colour in ('black','red','pale','white') or bool(x.blood_observed)
    x.version+=1;audit(db,user,x.patient_id,'care_stool_observation_'+body.action,'stool_entries',eid);db.commit()
    return C.stool_row(db,x,p)

# ----------------------------------------------------------------------------------- views
@router.get('/patients/{pid}/care/day')
def care_day(pid:int,day:date|None=None,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    p=own_patient(db,user,pid);d=day or C.local_today(p)
    note=db.query(CareDay).filter_by(patient_id=pid,local_date=d).first()
    return {**C.day_summary(db,pid,d),'day_note':note.day_note if note else '','timeline':C.timeline(db,pid,d,['all','clinical','camera'])['items']}

@router.put('/patients/{pid}/care/day/{day}/note')
def care_day_note(pid:int,day:date,body:DayNoteIn,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid);note=db.query(CareDay).filter_by(patient_id=pid,local_date=day).first()
    if not note:note=CareDay(patient_id=pid,local_date=day);db.add(note);db.flush()
    note.day_note=body.day_note;note.updated_at=utcnow();audit(db,user,pid,'care_day_note','care_days',note.id);db.commit();return {'date':day.isoformat(),'day_note':note.day_note}

@router.get('/patients/{pid}/care/timeline')
def care_timeline(pid:int,day:date|None=None,filters:str='all',user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    p=own_patient(db,user,pid);d=day or C.local_today(p)
    return C.timeline(db,pid,d,[f.strip() for f in filters.split(',') if f.strip()])

@router.get('/patients/{pid}/care/calendar/month')
def care_month(pid:int,year:int=Query(ge=2000,le=2100),month:int=Query(ge=1,le=12),user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid);return C.month_markers(db,pid,year,month)

@router.get('/patients/{pid}/care/calendar/week')
def care_week(pid:int,start:date|None=None,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    p=own_patient(db,user,pid);today=C.local_today(p);s=start or today-timedelta(days=today.weekday())
    return C.week_grid(db,pid,s)

@router.get('/patients/{pid}/care/analytics')
def care_analytics(pid:int,start:date|None=None,end:date|None=None,range:Literal['today','7d','30d','custom']|None=None,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    p=own_patient(db,user,pid);today=C.local_today(p)
    if range=='today':start=end=today
    elif range=='7d':start,end=today-timedelta(days=6),today
    elif range=='30d':start,end=today-timedelta(days=29),today
    end=end or today;start=start or end-timedelta(days=6)
    if (end-start).days>366:raise HTTPException(422,'Range limited to one year')
    return C.analytics(db,pid,start,end)

@router.get('/patients/{pid}/care/illnesses')
def care_illnesses(pid:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid);return C.illness_choices(db,pid)

@router.get('/patients/{pid}/care/compare')
def care_compare(pid:int,kind:Literal['episode','admission'],ref_id:int,window_days:int=Query(default=7,ge=1,le=30),user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid);c=C.compare_illness(db,pid,kind,ref_id,window_days)
    if not c:raise HTTPException(404,'Illness not found')
    return c

@router.get('/patients/{pid}/care/export')
def care_export(pid:int,preset:Literal['24h','3d','7d','illness','custom']='7d',start:date|None=None,end:date|None=None,kind:Literal['episode','admission']|None=None,ref_id:int|None=None,include_photos:bool=False,format:Literal['json','txt']='json',user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    p=own_patient(db,user,pid);today=C.local_today(p)
    if preset=='24h':start,end=today-timedelta(days=1),today
    elif preset=='3d':start,end=today-timedelta(days=2),today
    elif preset=='7d':start,end=today-timedelta(days=6),today
    elif preset=='illness':
        w=C.illness_window(db,pid,kind or 'episode',ref_id or 0)
        if not w:raise HTTPException(404,'Illness not found')
        start,end=w['start'],w['end']
    if not start or not end:raise HTTPException(422,'start and end are required for a custom range')
    result=C.care_export(db,pid,start,end,include_photos)
    audit(db,user,pid,'care_export','patient',pid);db.commit()
    if format=='txt':return PlainTextResponse(result['summary_text'],headers={'Content-Disposition':f'attachment; filename="daily-care-{start}-{end}.txt"'})
    return {**result,'start':start.isoformat(),'end':end.isoformat(),'photos_included':include_photos}

@router.get('/patients/{pid}/care/settings')
def settings_get(pid:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid);s=C.settings_for(db,pid);return {k:getattr(s,k) for k in ('wet_diaper_threshold_per_day','threshold_source','fluid_quick_amounts_ml','default_fluid_type','clinical_review_colours')}

@router.put('/patients/{pid}/care/settings')
def settings_put(pid:int,body:SettingsIn,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid);s=db.query(CareSettings).filter_by(patient_id=pid).first()
    if not s:s=CareSettings(patient_id=pid);db.add(s);db.flush()
    else:remember(db,user,s)
    for k,v in body.model_dump().items():setattr(s,k,v)
    s.updated_at=utcnow();audit(db,user,pid,'care_settings','care_settings',s.id);db.commit();return settings_get(pid,user,db)

class AskIn(BaseModel):
    question:str=Field(min_length=2,max_length=1000)

@router.post('/patients/{pid}/care/ask')
def care_ask(pid:int,body:AskIn,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid)
    hit=C.care_answer(db,pid,body.question)
    if hit:return hit
    from .intelligence import answer
    return answer(db,pid,body.question)

# Generic entry routes are registered LAST so that specific paths such as /care/day and /care/settings win.
# ----------------------------------------------------------------------------------- generic entry CRUD
@router.post('/patients/{pid}/care/{kind}',status_code=201)
def entry_add(pid:int,kind:str,body:dict,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    p=own_patient(db,user,pid)
    if kind not in SCHEMAS:raise HTTPException(404,'Unknown entry type')
    try:model_in=SCHEMAS[kind].model_validate(body)
    except ValueError as exc:raise HTTPException(422,str(exc))
    model=ENTRY_MODELS[kind]
    dup=existing_by_key(db,pid,model,model_in.client_key)
    if dup:return {**out(db,dup,p),'existing':True}
    data=prepare(db,p,kind,model_in)
    rowobj=model(patient_id=pid,recorded_by=user.id,**data)
    db.add(rowobj);db.flush();ensure_day(db,pid,rowobj.local_date)
    if kind=='stool' and body.get('create_diaper'):
        db.add(DiaperEntry(patient_id=pid,recorded_by=user.id,at=rowobj.at,local_date=rowobj.local_date,kind='stool',stool_entry_id=rowobj.id,client_key=(model_in.client_key+':diaper') if model_in.client_key else None))
    audit(db,user,pid,f'care_{kind}_add',model.__tablename__,rowobj.id);db.commit()
    return out(db,rowobj,p)

@router.get('/patients/{pid}/care/{kind}')
def entry_list(pid:int,kind:str,start:date|None=None,end:date|None=None,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    p=own_patient(db,user,pid)
    if kind not in ENTRY_MODELS:raise HTTPException(404,'Unknown entry type')
    model=ENTRY_MODELS[kind];end=end or C.local_today(p);start=start or end-timedelta(days=6)
    rows=C.active(db.query(model).filter(model.patient_id==pid,model.local_date>=start,model.local_date<=end),model).order_by(model.at.desc()).limit(500).all()
    return [out(db,x,p) for x in rows]

@router.patch('/care/{kind}/{eid}')
def entry_edit(kind:str,eid:int,body:dict,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    if kind not in SCHEMAS:raise HTTPException(404,'Unknown entry type')
    model=ENTRY_MODELS[kind];x=own_resource(db,user,model,eid);p=db.get(Patient,x.patient_id)
    current={c.name:getattr(x,c.name) for c in model.__table__.columns}
    merged={**current,**body};merged['at']=body.get('at',C.local(x.at,p).isoformat())
    for k in ('ended_at','scheduled_for'):
        if k in current and k not in body and current.get(k):merged[k]=C.local(current[k],p).isoformat()
    try:model_in=SCHEMAS[kind].model_validate({k:v for k,v in merged.items() if k in SCHEMAS[kind].model_fields})
    except ValueError as exc:raise HTTPException(422,str(exc))
    remember(db,user,x)
    for k,v in prepare(db,p,kind,model_in).items():setattr(x,k,v)
    x.version+=1;x.updated_at=utcnow()
    if kind=='stool' and x.ai_review_status=='ai_observed':x.ai_review_status='parent_corrected'
    audit(db,user,x.patient_id,f'care_{kind}_correct',model.__tablename__,eid);db.commit()
    return out(db,x,p)

@router.post('/care/{kind}/{eid}/withdraw')
def entry_withdraw(kind:str,eid:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    if kind not in ENTRY_MODELS:raise HTTPException(404,'Unknown entry type')
    model=ENTRY_MODELS[kind];x=own_resource(db,user,model,eid)
    remember(db,user,x);x.status='withdrawn';x.version+=1;x.updated_at=utcnow()
    audit(db,user,x.patient_id,f'care_{kind}_withdraw',model.__tablename__,eid);db.commit()
    return {'ok':True,'id':eid,'status':'withdrawn'}

@router.get('/care/{kind}/{eid}/history')
def entry_history(kind:str,eid:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    if kind not in ENTRY_MODELS:raise HTTPException(404,'Unknown entry type')
    model=ENTRY_MODELS[kind];x=own_resource(db,user,model,eid)
    return {'current':out(db,x,db.get(Patient,x.patient_id)),'revisions':[{'id':r.id,'changed_at':r.changed_at.isoformat(),'previous_values':json.loads(r.snapshot_json)} for r in db.query(RecordRevision).filter_by(entity=model.__tablename__,entity_id=eid).order_by(RecordRevision.changed_at.desc())]}

