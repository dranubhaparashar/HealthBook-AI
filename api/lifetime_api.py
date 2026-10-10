"""Lifetime Health API: fever history, lifetime antibiotic metrics, illness analysis, parent entries and structured Ask answers."""
import hashlib
import json
import secrets
from datetime import date, datetime
from typing import Literal
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session as DbSession
from .database import get_db
from .security import require_user, own_patient, own_resource
from .models import User, Patient, Document, Episode, Admission, Medicine, Audit, utcnow
from .lifetime_models import FeverEpisode, AntibioticCourse, AntibioticAdministration, LifetimeEvidence, ParentHealthEntry, ORIGINS, VERIFICATION, ADMIN_STATUS, SETTINGS, ROUTES, CERTAINTY
from .history import remember
from .models import RecordRevision
from . import lifetime as L

router=APIRouter(prefix='/api')

def audit(db,user,pid,action,entity,obj_id):
    db.add(Audit(user_id=user.id,patient_id=pid,action=action,entity=entity,entity_id=obj_id))

def safe_doc(db,pid,doc_id,page=1):
    if doc_id is None:return
    item=db.get(Document,doc_id)
    if item is None or item.patient_id!=pid:raise HTTPException(400,'Source document does not belong to this child')
    if not 1<=(page or 1)<=item.page_count:raise HTTPException(422,'Source page is outside the original document')

def today_for(db,pid):
    return utcnow().astimezone(ZoneInfo(db.get(Patient,pid).tz)).date()

def check_ref(db,pid,model,ref_id,label):
    if ref_id is None:return
    obj=db.get(model,ref_id)
    if not obj or obj.patient_id!=pid:raise HTTPException(400,f'{label} does not belong to this child')

# ----------------------------------------------------------------------------------- schemas
class EvidenceIn(BaseModel):
    source_document_id:int|None=None
    source_page:int=Field(default=1,ge=1,le=35)
    source_ref:str=Field(default='',max_length=40)
    quoted_text:str=Field(default='',max_length=2000)
    evidence_kind:str=Field(default='mention',max_length=40)
    observed_on:date|None=None
    certainty:Literal['high','medium','low']='medium'

class FeverIn(BaseModel):
    title:str=Field(min_length=1,max_length=180)
    illness_episode_id:int|None=None
    admission_id:int|None=None
    first_documented_on:date|None=None
    last_documented_on:date|None=None
    documented_fever_days:int|None=Field(default=None,ge=0,le=120)
    duration_text:str=Field(default='',max_length=200)
    date_certainty:Literal['exact','approximate','unknown']='unknown'
    highest_temperature:str=Field(default='',max_length=12)
    temperature_unit:Literal['F','C','']=''
    symptoms:str=Field(default='',max_length=2000)
    documented_diagnosis:str=Field(default='',max_length=2000)
    hospitalized:bool=False
    antipyretics:str=Field(default='',max_length=500)
    resolution_documented_on:date|None=None
    origin:Literal['clinician_document','parent_report']='clinician_document'
    needs_parent_check:str=Field(default='',max_length=1000)
    notes:str=Field(default='',max_length=3000)
    source_document_id:int|None=None
    source_page:int=Field(default=1,ge=1,le=35)
    import_key:str|None=Field(default=None,max_length=120)
    evidence:list[EvidenceIn]=Field(default_factory=list,max_length=40)
    @model_validator(mode='after')
    def ordered(self):
        if self.first_documented_on and self.last_documented_on and self.last_documented_on<self.first_documented_on:raise ValueError('Last documented fever date precedes the first')
        if self.highest_temperature and not self.temperature_unit:raise ValueError('Give the unit for the recorded temperature')
        if self.origin=='clinician_document' and not self.source_document_id and not self.import_key:raise ValueError('A clinician-documented episode needs the original document')
        return self

class CourseIn(BaseModel):
    generic_name:str=Field(min_length=2,max_length=120)
    brand_name:str=Field(default='',max_length=120)
    illness_episode_id:int|None=None
    admission_id:int|None=None
    medicine_id:int|None=None
    fever_episode_id:int|None=None
    route:Literal['oral','IV','IM','other','unknown']='unknown'
    dose:str=Field(default='',max_length=120)
    frequency:str=Field(default='',max_length=120)
    prescribed_on:date|None=None
    planned_start:date|None=None
    planned_end:date|None=None
    prescribed_duration_days:int|None=Field(default=None,ge=1,le=120)
    prescribed_duration_text:str=Field(default='',max_length=120)
    actual_start:date|None=None
    actual_stop:date|None=None
    administered_duration_days:int|None=Field(default=None,ge=1,le=120)
    administration_status:Literal['prescribed_only','scheduled','confirmed_administered','unknown','not_received']='unknown'
    setting:Literal['inpatient','outpatient','discharge_prescription','unknown']='unknown'
    prescribing_doctor:str=Field(default='',max_length=180)
    facility:str=Field(default='',max_length=180)
    indication:str=Field(default='',max_length=1000)
    continuation_of_id:int|None=None
    origin:Literal['clinician_document','parent_report']='clinician_document'
    uncertain_fields:str=Field(default='',max_length=250)
    needs_parent_check:str=Field(default='',max_length=1000)
    notes:str=Field(default='',max_length=3000)
    source_document_id:int|None=None
    source_page:int=Field(default=1,ge=1,le=35)
    import_key:str|None=Field(default=None,max_length=120)
    allow_unrecognised_ingredient:bool=False
    evidence:list[EvidenceIn]=Field(default_factory=list,max_length=40)
    @model_validator(mode='after')
    def ordered(self):
        if self.planned_start and self.planned_end and self.planned_end<self.planned_start:raise ValueError('Planned end precedes planned start')
        if self.actual_start and self.actual_stop and self.actual_stop<self.actual_start:raise ValueError('Actual stop precedes actual start')
        if self.administration_status=='confirmed_administered' and self.origin=='clinician_document' and not (self.actual_start or self.administered_duration_days):
            # Allowed, but the engine will flag it; a discharge summary may state administration without dates.
            pass
        if self.origin=='clinician_document' and not self.source_document_id and not self.import_key:raise ValueError('A clinician-documented course needs the original document')
        return self

class AdministrationIn(BaseModel):
    administered_on:date
    administered_time:str=Field(default='',pattern=r'^$|^(?:[01]\d|2[0-3]):[0-5]\d$')
    dose:str=Field(default='',max_length=120)
    route:Literal['oral','IV','IM','other','unknown']='unknown'
    source:Literal['nursing_chart','discharge_summary','caregiver_report']='caregiver_report'
    source_document_id:int|None=None
    source_page:int=Field(default=1,ge=1,le=35)
    note:str=Field(default='',max_length=1000)
    idempotency_key:str=Field(default_factory=lambda:secrets.token_hex(12),min_length=8,max_length=80)

class VerifyIn(BaseModel):
    status:Literal['verified','rejected','needs_verification']
    checked_against_original:bool=False
    note:str=Field(default='',max_length=1000)

class ParentEntryIn(BaseModel):
    kind:Literal['fever','medicine','hospital_visit','note']
    started_on:date|None=None
    ended_on:date|None=None
    temperature:str=Field(default='',max_length=12)
    temperature_unit:Literal['F','C','']=''
    symptoms:str=Field(default='',max_length=2000)
    medicine_name:str=Field(default='',max_length=150)
    medicine_dose:str=Field(default='',max_length=120)
    administered_on:date|None=None
    administered_time:str=Field(default='',pattern=r'^$|^(?:[01]\d|2[0-3]):[0-5]\d$')
    hospital_visit:str=Field(default='',max_length=500)
    notes:str=Field(default='',max_length=3000)
    @model_validator(mode='after')
    def consistent(self):
        if self.kind=='fever' and not self.started_on:raise ValueError('Enter the fever onset date')
        if self.ended_on and self.started_on and self.ended_on<self.started_on:raise ValueError('End date precedes onset')
        if self.temperature and not self.temperature_unit:raise ValueError('Give the temperature unit')
        if self.kind=='medicine' and not (self.medicine_name and self.administered_on):raise ValueError('Enter the medicine and the actual date it was given')
        return self

class TranscriptionImport(BaseModel):
    """Guardian-reviewed transcription bundle keyed by bundle page ids (source_ref). Idempotent through import_key."""
    batch:str=Field(min_length=3,max_length=80)
    fever_episodes:list[dict]=Field(default_factory=list,max_length=200)
    antibiotic_courses:list[dict]=Field(default_factory=list,max_length=400)
    administrations:list[dict]=Field(default_factory=list,max_length=2000)
    checked_against_originals:Literal[True]

class AskIn(BaseModel):
    question:str=Field(min_length=2,max_length=1000)

# ----------------------------------------------------------------------------------- helpers
def add_evidence(db,pid,entity,entity_id,items):
    for e in items:
        safe_doc(db,pid,e.source_document_id,e.source_page)
        db.add(LifetimeEvidence(patient_id=pid,entity=entity,entity_id=entity_id,**e.model_dump()))

def resolve_ref(db,pid,ref):
    if not ref:return None
    d=db.query(Document).filter(Document.patient_id==pid,Document.source_ref==ref).first()
    if not d:raise HTTPException(422,f'Bundle page {ref} has not been imported as a document yet')
    return d.id

def fever_payload(db,user,pid,f):
    return {**L.row(f),'evidence':L.evidence_for(db,pid,'fever_episode',[f.id]).get(f.id,[])}

# ----------------------------------------------------------------------------------- dashboard & reports
@router.get('/patients/{pid}/lifetime/dashboard')
def lifetime_dashboard(pid:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid);return L.dashboard(db,pid)

@router.get('/patients/{pid}/lifetime/fever')
def fever(pid:int,scope:Literal['all','verified']='all',user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid);return L.fever_report(db,pid,scope)

@router.get('/patients/{pid}/lifetime/antibiotics')
def antibiotics(pid:int,scope:Literal['all','verified']='all',user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid);return L.antibiotic_report(db,pid,scope)

@router.get('/patients/{pid}/lifetime/illnesses')
def illnesses(pid:int,scope:Literal['all','verified']='all',user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid);return L.illness_report(db,pid,scope)

@router.get('/patients/{pid}/lifetime/ecoli')
def ecoli(pid:int,scope:Literal['all','verified']='all',user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid);return L.ecoli_report(db,pid,scope)

@router.get('/patients/{pid}/lifetime/correlation')
def correlation(pid:int,scope:Literal['all','verified']='all',user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid);return L.correlation_report(db,pid,scope)

@router.get('/patients/{pid}/lifetime/scan')
def scan(pid:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid)
    return {'pages':L.scan_documents(db,pid),'warning':'Candidates found in transcribed text. Nothing here is a fact until you confirm it against the original page.'}

@router.get('/patients/{pid}/lifetime/review-tables')
def review(pid:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid);return L.review_tables(db,pid)

@router.get('/patients/{pid}/lifetime/summary',response_class=PlainTextResponse)
def summary(pid:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid)
    return PlainTextResponse(L.summary_text(db,pid),headers={'Content-Disposition':'attachment; filename="lifetime-health-summary.txt"'})

@router.get('/lifetime/drug-check')
def drug_check(name:str=Query(min_length=1,max_length=120),user:User=Depends(require_user)):
    return L.classify_drug(name)

@router.get('/patients/{pid}/lifetime/evidence')
def evidence(pid:int,entity:str=Query(max_length=40),entity_id:int=Query(ge=1),user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid)
    docs=L.doc_index(db,pid)
    return {'items':[{**e,'document':docs.get(e['source_document_id'])} for e in L.evidence_for(db,pid,entity,[entity_id]).get(entity_id,[])]}

@router.post('/patients/{pid}/lifetime/evidence',status_code=201)
def evidence_add(pid:int,entity:str=Query(max_length=40),entity_id:int=Query(ge=1),body:EvidenceIn=None,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid)
    model={'fever_episode':FeverEpisode,'antibiotic_course':AntibioticCourse,'illness_episode':Episode}.get(entity)
    if not model:raise HTTPException(422,'Unknown entity')
    check_ref(db,pid,model,entity_id,'Record')
    add_evidence(db,pid,entity,entity_id,[body]);audit(db,user,pid,'attach_evidence',entity,entity_id);db.commit()
    return {'ok':True}

# ----------------------------------------------------------------------------------- fever CRUD
@router.post('/patients/{pid}/lifetime/fever',status_code=201)
def fever_add(pid:int,body:FeverIn,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid);safe_doc(db,pid,body.source_document_id,body.source_page)
    check_ref(db,pid,Episode,body.illness_episode_id,'Illness episode');check_ref(db,pid,Admission,body.admission_id,'Admission')
    if body.first_documented_on and body.first_documented_on<db.get(Patient,pid).dob:raise HTTPException(422,'Fever date precedes birth')
    if body.import_key and (dup:=db.query(FeverEpisode).filter_by(import_key=body.import_key).first()):return {**fever_payload(db,user,pid,dup),'existing':True}
    f=FeverEpisode(patient_id=pid,**body.model_dump(exclude={'evidence'}))
    db.add(f);db.flush();add_evidence(db,pid,'fever_episode',f.id,body.evidence)
    audit(db,user,pid,'create_fever_episode_'+body.origin,'fever_episode',f.id);db.commit()
    return fever_payload(db,user,pid,f)

@router.patch('/lifetime/fever/{fid}')
def fever_edit(fid:int,body:FeverIn,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    f=own_resource(db,user,FeverEpisode,fid);safe_doc(db,f.patient_id,body.source_document_id,body.source_page)
    check_ref(db,f.patient_id,Episode,body.illness_episode_id,'Illness episode');check_ref(db,f.patient_id,Admission,body.admission_id,'Admission')
    remember(db,user,f)
    for k,v in body.model_dump(exclude={'evidence','import_key'}).items():setattr(f,k,v)
    f.version+=1;f.verification_status='needs_verification';f.verified_at=None;f.verified_by=None
    add_evidence(db,f.patient_id,'fever_episode',f.id,body.evidence)
    audit(db,user,f.patient_id,'revise_fever_episode','fever_episode',fid);db.commit()
    return fever_payload(db,user,f.patient_id,f)

@router.post('/lifetime/fever/{fid}/verify')
def fever_verify(fid:int,body:VerifyIn,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    f=own_resource(db,user,FeverEpisode,fid)
    if body.status=='verified' and not body.checked_against_original:raise HTTPException(422,'Confirm that you checked the original page')
    remember(db,user,f)
    f.verification_status=body.status;f.verified_at=utcnow() if body.status=='verified' else None;f.verified_by=user.id if body.status=='verified' else None
    if body.note:f.notes=(f.notes+'\n'+body.note).strip()[:3000]
    audit(db,user,f.patient_id,'fever_'+body.status,'fever_episode',fid);db.commit()
    return fever_payload(db,user,f.patient_id,f)

# ----------------------------------------------------------------------------------- antibiotic CRUD
def validate_course(db,pid,body):
    safe_doc(db,pid,body.source_document_id,body.source_page)
    for model,ref,label in ((Episode,body.illness_episode_id,'Illness episode'),(Admission,body.admission_id,'Admission'),(Medicine,body.medicine_id,'Medicine course'),(FeverEpisode,body.fever_episode_id,'Fever episode'),(AntibioticCourse,body.continuation_of_id,'Continued course')):
        check_ref(db,pid,model,ref,label)
    verdict=L.classify_drug(body.generic_name)
    if verdict['is_antibiotic'] is False:raise HTTPException(422,f'"{body.generic_name}" is recognised as {verdict["generic"]}, which is not an antibiotic')
    if verdict['is_antibiotic'] is None and not body.allow_unrecognised_ingredient:raise HTTPException(422,{'message':'Active ingredient not in the verified table. Enter the generic name or explicitly allow an unrecognised ingredient.','classification':verdict})
    generic=verdict['generic'] or body.generic_name.strip().casefold()
    brand=body.brand_name or (body.generic_name if verdict.get('brand_recognized') else '')
    today=today_for(db,pid)
    if any(d and d>today for d in (body.actual_start,body.actual_stop)):raise HTTPException(422,'Actual administration dates cannot be in the future')
    return generic,brand,verdict

def course_payload(db,pid,c):
    return {**L.row(c),'evidence':L.evidence_for(db,pid,'antibiotic_course',[c.id]).get(c.id,[]),'administrations':[L.row(a) for a in db.query(AntibioticAdministration).filter_by(course_id=c.id).order_by(AntibioticAdministration.administered_on)]}

@router.post('/patients/{pid}/lifetime/antibiotics',status_code=201)
def course_add(pid:int,body:CourseIn,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid);generic,brand,verdict=validate_course(db,pid,body)
    if body.import_key and (dup:=db.query(AntibioticCourse).filter_by(import_key=body.import_key).first()):return {**course_payload(db,pid,dup),'existing':True}
    data=body.model_dump(exclude={'evidence','allow_unrecognised_ingredient'});data.update(generic_name=generic,brand_name=brand,drug_class=verdict.get('class','') if verdict['is_antibiotic'] else 'unclassified')
    c=AntibioticCourse(patient_id=pid,**data)
    db.add(c);db.flush();add_evidence(db,pid,'antibiotic_course',c.id,body.evidence)
    audit(db,user,pid,'create_antibiotic_course_'+body.origin,'antibiotic_course',c.id);db.commit()
    return course_payload(db,pid,c)

@router.patch('/lifetime/antibiotics/{cid}')
def course_edit(cid:int,body:CourseIn,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    c=own_resource(db,user,AntibioticCourse,cid);generic,brand,verdict=validate_course(db,c.patient_id,body)
    if body.continuation_of_id==cid:raise HTTPException(422,'A course cannot continue itself')
    remember(db,user,c)
    for k,v in body.model_dump(exclude={'evidence','import_key','allow_unrecognised_ingredient'}).items():setattr(c,k,v)
    c.generic_name=generic;c.brand_name=brand;c.drug_class=verdict.get('class','') if verdict['is_antibiotic'] else 'unclassified'
    c.version+=1;c.verification_status='needs_verification';c.verified_at=None;c.verified_by=None
    add_evidence(db,c.patient_id,'antibiotic_course',c.id,body.evidence)
    audit(db,user,c.patient_id,'revise_antibiotic_course','antibiotic_course',cid);db.commit()
    return course_payload(db,c.patient_id,c)

@router.post('/lifetime/antibiotics/{cid}/verify')
def course_verify(cid:int,body:VerifyIn,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    c=own_resource(db,user,AntibioticCourse,cid)
    if body.status=='verified' and not body.checked_against_original:raise HTTPException(422,'Confirm that you checked the original page')
    remember(db,user,c)
    c.verification_status=body.status;c.verified_at=utcnow() if body.status=='verified' else None;c.verified_by=user.id if body.status=='verified' else None
    if body.note:c.notes=(c.notes+'\n'+body.note).strip()[:3000]
    audit(db,user,c.patient_id,'antibiotic_'+body.status,'antibiotic_course',cid);db.commit()
    return course_payload(db,c.patient_id,c)

@router.post('/lifetime/antibiotics/{cid}/administrations',status_code=201)
def administration_add(cid:int,body:AdministrationIn,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    c=own_resource(db,user,AntibioticCourse,cid);safe_doc(db,c.patient_id,body.source_document_id,body.source_page)
    if c.administration_status=='not_received':raise HTTPException(409,'This record is a culture-only or not-received entry; it cannot have administrations')
    if body.source!='caregiver_report' and not body.source_document_id:raise HTTPException(422,'Attach the hospital chart page that documents this administration')
    p=db.get(Patient,c.patient_id)
    if body.administered_on>today_for(db,p.id) or body.administered_on<p.dob:raise HTTPException(422,'Administration must be a past date after birth')
    existing=db.query(AntibioticAdministration).filter_by(idempotency_key=body.idempotency_key).first()
    if existing:return L.row(existing)
    a=AntibioticAdministration(patient_id=p.id,course_id=cid,**body.model_dump())
    db.add(a);db.flush();audit(db,user,p.id,'record_antibiotic_administration_'+body.source,'antibiotic_administration',a.id);db.commit()
    return L.row(a)

# ----------------------------------------------------------------------------------- parent entries
def parent_payload(db,row):
    revisions=[{'id':r.id,'changed_at':r.changed_at.isoformat(),'previous_values':json.loads(r.snapshot_json)} for r in db.query(RecordRevision).filter_by(entity='parent_health_entries',entity_id=row.id).order_by(RecordRevision.changed_at.desc())]
    return {**L.row(row),'revisions':revisions,'origin_label':'Parent-reported'}

def derive_from_parent_entry(db,user,entry):
    """Create or refresh the derived fever episode / antibiotic record so parent reports appear in the analysis, clearly labelled."""
    pid=entry.patient_id
    if entry.kind=='fever':
        f=db.get(FeverEpisode,entry.fever_episode_id) if entry.fever_episode_id else None
        if not f:f=FeverEpisode(patient_id=pid,title='Parent-reported fever',origin='parent_report',verification_status='needs_verification');db.add(f);db.flush();entry.fever_episode_id=f.id
        else:remember(db,user,f);f.version+=1
        span=(entry.ended_on-entry.started_on).days+1 if entry.started_on and entry.ended_on else None
        f.title=f'Parent-reported fever from {entry.started_on}';f.first_documented_on=entry.started_on;f.last_documented_on=entry.ended_on;f.documented_fever_days=span;f.date_certainty='exact' if entry.started_on else 'unknown'
        f.highest_temperature=entry.temperature;f.temperature_unit=entry.temperature_unit;f.symptoms=entry.symptoms;f.hospitalized=bool(entry.hospital_visit);f.antipyretics=entry.medicine_name if entry.medicine_name and L.classify_drug(entry.medicine_name).get('is_antipyretic') else f.antipyretics
        f.notes=f"Parent entry #{entry.id}. {entry.notes}".strip();f.source_document_id=None;f.verification_status='needs_verification' if entry.status=='active' else 'rejected'
    if entry.kind=='medicine' and entry.medicine_name:
        verdict=L.classify_drug(entry.medicine_name)
        if verdict['is_antibiotic']:
            c=db.get(AntibioticCourse,entry.antibiotic_course_id) if entry.antibiotic_course_id else None
            if not c:
                c=AntibioticCourse(patient_id=pid,generic_name=verdict['generic'],brand_name=entry.medicine_name if verdict.get('brand_recognized') else '',drug_class=verdict['class'],origin='parent_report',setting='outpatient',administration_status='confirmed_administered')
                db.add(c);db.flush();entry.antibiotic_course_id=c.id
            else:remember(db,user,c);c.version+=1
            c.dose=entry.medicine_dose;c.actual_start=entry.administered_on;c.actual_stop=entry.ended_on or entry.administered_on;c.route='oral' if c.route=='unknown' else c.route
            c.notes=f"Parent entry #{entry.id}: {entry.notes}".strip();c.verification_status='needs_verification' if entry.status=='active' else 'rejected';c.administration_status='confirmed_administered' if entry.status=='active' else 'not_received'
            key=f'parent-entry-{entry.id}-v{entry.version}'
            if not db.query(AntibioticAdministration).filter_by(idempotency_key=key).first() and entry.status=='active':
                db.add(AntibioticAdministration(patient_id=pid,course_id=c.id,administered_on=entry.administered_on,administered_time=entry.administered_time,dose=entry.medicine_dose,route=c.route,source='caregiver_report',note=f'Parent entry #{entry.id} v{entry.version}',parent_entry_id=entry.id,idempotency_key=key))

@router.get('/patients/{pid}/lifetime/parent-entries')
def parent_entries(pid:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid)
    return [parent_payload(db,r) for r in db.query(ParentHealthEntry).filter_by(patient_id=pid).order_by(ParentHealthEntry.started_on.desc().nullslast(),ParentHealthEntry.id.desc())]

@router.post('/patients/{pid}/lifetime/parent-entries',status_code=201)
def parent_entry_add(pid:int,body:ParentEntryIn,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    p=own_patient(db,user,pid);today=today_for(db,pid)
    for d in (body.started_on,body.ended_on,body.administered_on):
        if d and (d<p.dob or d>today):raise HTTPException(422,'Dates must be between birth and today')
    e=ParentHealthEntry(patient_id=pid,user_id=user.id,**body.model_dump())
    db.add(e);db.flush();derive_from_parent_entry(db,user,e)
    audit(db,user,pid,'parent_entry_create','parent_health_entry',e.id);db.commit()
    return parent_payload(db,e)

@router.patch('/lifetime/parent-entries/{eid}')
def parent_entry_edit(eid:int,body:ParentEntryIn,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    e=own_resource(db,user,ParentHealthEntry,eid);p=db.get(Patient,e.patient_id);today=today_for(db,p.id)
    for d in (body.started_on,body.ended_on,body.administered_on):
        if d and (d<p.dob or d>today):raise HTTPException(422,'Dates must be between birth and today')
    if body.kind!=e.kind:raise HTTPException(422,'Withdraw this entry and add a new one to change its type')
    remember(db,user,e)  # previous values are retained; nothing is overwritten silently
    for k,v in body.model_dump().items():setattr(e,k,v)
    e.version+=1;e.updated_at=utcnow();derive_from_parent_entry(db,user,e)
    audit(db,user,e.patient_id,'parent_entry_correct','parent_health_entry',eid);db.commit()
    return parent_payload(db,e)

@router.post('/lifetime/parent-entries/{eid}/withdraw')
def parent_entry_withdraw(eid:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    e=own_resource(db,user,ParentHealthEntry,eid)
    remember(db,user,e);e.status='withdrawn';e.version+=1;e.updated_at=utcnow();derive_from_parent_entry(db,user,e)
    audit(db,user,e.patient_id,'parent_entry_withdraw','parent_health_entry',eid);db.commit()
    return parent_payload(db,e)

# ----------------------------------------------------------------------------------- transcription import (guardian-reviewed bundle)
@router.post('/patients/{pid}/lifetime/import-transcription')
def import_transcription(pid:int,body:TranscriptionImport,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    p=own_patient(db,user,pid)
    if p.user_id!=user.id:raise HTTPException(403,'Only the primary guardian can import a transcription bundle')
    created={'fever_episodes':[],'antibiotic_courses':[],'administrations':[],'skipped_existing':[]}
    course_ids={}
    def with_doc(item):
        item=dict(item);ref=item.pop('source_ref',None)
        if ref and not item.get('source_document_id'):item['source_document_id']=resolve_ref(db,pid,ref)
        ev=[]
        for e in item.get('evidence',[]):
            e=dict(e);r=e.get('source_ref')
            if r and not e.get('source_document_id'):e['source_document_id']=resolve_ref(db,pid,r)
            ev.append(e)
        item['evidence']=ev
        return item
    for raw in body.fever_episodes:
        item=with_doc(raw);item['import_key']=f"{body.batch}:fever:{item.get('key') or hashlib.sha1(json.dumps(raw,sort_keys=True,default=str).encode()).hexdigest()[:16]}";item.pop('key',None)
        try:model=FeverIn.model_validate(item)
        except ValueError as exc:raise HTTPException(422,{'item':raw.get('title'),'error':str(exc)})
        result=fever_add(pid,model,user,db)
        (created['skipped_existing'] if result.get('existing') else created['fever_episodes']).append(result['id'])
    fever_by_key={}
    for raw,fid in zip([r for r in body.fever_episodes],created['fever_episodes']+created['skipped_existing']):pass
    for raw in body.antibiotic_courses:
        item=with_doc(raw);key=item.pop('key',None);item['import_key']=f"{body.batch}:abx:{key or hashlib.sha1(json.dumps(raw,sort_keys=True,default=str).encode()).hexdigest()[:16]}"
        cont=item.pop('continuation_of_key',None)
        if cont:
            if cont not in course_ids:raise HTTPException(422,f'continuation_of_key {cont} must appear earlier in the bundle')
            item['continuation_of_id']=course_ids[cont]
        fever_key=item.pop('fever_key',None)
        if fever_key:
            f=db.query(FeverEpisode).filter_by(import_key=f'{body.batch}:fever:{fever_key}').first()
            if f:item['fever_episode_id']=f.id
        try:model=CourseIn.model_validate(item)
        except ValueError as exc:raise HTTPException(422,{'item':raw.get('generic_name'),'error':str(exc)})
        result=course_add(pid,model,user,db)
        if key:course_ids[key]=result['id']
        (created['skipped_existing'] if result.get('existing') else created['antibiotic_courses']).append(result['id'])
    for raw in body.administrations:
        item=dict(raw);key=item.pop('course_key');ref=item.pop('source_ref',None)
        if ref and not item.get('source_document_id'):item['source_document_id']=resolve_ref(db,pid,ref)
        cid=course_ids.get(key) or (db.query(AntibioticCourse).filter_by(import_key=f'{body.batch}:abx:{key}').first() or {}).id if key else None
        if not cid:raise HTTPException(422,f'Unknown course_key {key}')
        item['idempotency_key']=f"{body.batch}:adm:{key}:{item['administered_on']}:{item.get('administered_time','')}"[:80]
        result=administration_add(cid,AdministrationIn.model_validate(item),user,db)
        created['administrations'].append(result['id'])
    audit(db,user,pid,'import_lifetime_transcription','patient',pid);db.commit()
    return created

# ----------------------------------------------------------------------------------- Ask
@router.post('/patients/{pid}/lifetime/ask')
def ask(pid:int,body:AskIn,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid)
    hit=L.lifetime_answer(db,pid,body.question)
    if hit:return hit
    from .intelligence import answer
    return answer(db,pid,body.question)
