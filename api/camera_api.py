"""Home Activity Camera API: configuration with encrypted credentials, test connection, worker ingest, event review,
privacy controls (disable instantly, retention, delete recordings) and journal suggestions."""
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Literal
from fastapi import APIRouter, Depends, HTTPException, Header, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session as DbSession
from .database import get_db
from .security import require_user, own_patient, own_resource
from .models import User, Patient, Audit, utcnow
from .care_models import Camera, CameraObservation, ActivityEntry, SleepEntry, OBSERVATION_KINDS, REVIEW_STATES
from .history import remember
from . import camera as CAM
from . import care as C
from . import storage

router=APIRouter(prefix='/api')

def audit(db,user,pid,action,entity,obj_id):db.add(Audit(user_id=user.id,patient_id=pid,action=action,entity=entity,entity_id=obj_id))

class CameraIn(BaseModel):
    name:str=Field(min_length=1,max_length=80)
    room:str=Field(default='',max_length=80)
    protocol:Literal['rtsp','onvif','http']='rtsp'
    stream_url:str=Field(min_length=7,max_length=500)
    username:str=Field(default='',max_length=100)
    password:str=Field(default='',max_length=200)
    enabled:bool=False
    analysis_mode:Literal['off','motion','activity']='motion'
    audio_enabled:bool=False
    record_clips:bool=False
    retention_days:int=Field(default=7,ge=0,le=90)
    local_network_only:bool=True
    cloud_upload_allowed:bool=False
    keep_existing_password:bool=False

class CameraPatch(BaseModel):
    enabled:bool|None=None
    analysis_mode:Literal['off','motion','activity']|None=None
    audio_enabled:bool|None=None
    record_clips:bool|None=None
    retention_days:int|None=Field(default=None,ge=0,le=90)
    cloud_upload_allowed:bool|None=None

def public(c,p=None):
    return {'id':c.id,'patient_id':c.patient_id,'name':c.name,'room':c.room,'protocol':c.protocol,'stream_display':CAM.redact(c.stream_url),'has_credentials':bool(c.username or c.password),'enabled':c.enabled,'analysis_mode':c.analysis_mode,'audio_enabled':c.audio_enabled,'record_clips':c.record_clips,'retention_days':c.retention_days,'local_network_only':c.local_network_only,'cloud_upload_allowed':c.cloud_upload_allowed,'worker_state':c.worker_state,'worker_seen_at':C.aware(c.worker_seen_at).isoformat() if c.worker_seen_at else None,'worker_message':c.worker_message,'worker_key_set':bool(c.worker_key_hash),'last_test_at':C.aware(c.last_test_at).isoformat() if c.last_test_at else None,'last_test_ok':c.last_test_ok,'last_test_detail':c.last_test_detail,'disabled_at':C.aware(c.disabled_at).isoformat() if c.disabled_at else None,'created_at':C.aware(c.created_at).isoformat()}

def obs_row(o,p):
    return {'id':o.id,'camera_id':o.camera_id,'started_at':C.iso_local(o.started_at,p),'ended_at':C.iso_local(o.ended_at,p) if o.ended_at else None,'local_date':o.local_date.isoformat(),'kind':o.kind,'effective_kind':o.corrected_kind or o.kind,'confidence':o.confidence,'confidence_pct':round(o.confidence*100),'review_status':o.review_status,'corrected_kind':o.corrected_kind,'review_note':o.review_note,'reviewed_at':C.aware(o.reviewed_at).isoformat() if o.reviewed_at else None,'has_clip':bool(o.clip_storage_name and not o.clip_deleted_at),'metrics':json.loads(o.metrics_json) if o.metrics_json else {},'journal_entry_kind':o.journal_entry_kind,'journal_entry_id':o.journal_entry_id,'label':'AI OBSERVED' if o.review_status=='ai_observed' else 'PARENT CONFIRMED' if o.review_status=='parent_confirmed' else 'PARENT CORRECTED' if o.review_status=='parent_corrected' else 'DISMISSED','suggested_journal_kind':'sleep' if (o.corrected_kind or o.kind) in ('rest','low_activity') else 'activity'}

# ----------------------------------------------------------------------------------- configuration
@router.get('/patients/{pid}/cameras')
def cameras(pid:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid)
    return {'cameras':[public(c) for c in db.query(Camera).filter_by(patient_id=pid).order_by(Camera.id)],'privacy':['Raw video stays on your home network; the local worker sends only derived events.','Audio is off by default. Clips are recorded only if you enable them, and are deleted after the retention period.','Credentials are encrypted at rest and never returned by the API or stored in the code repository.']}

@router.post('/patients/{pid}/cameras',status_code=201)
def camera_add(pid:int,body:CameraIn,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    p=own_patient(db,user,pid)
    if p.user_id!=user.id:raise HTTPException(403,'Only the primary guardian can configure cameras')
    try:clean,u,pw=CAM.validate_stream(body.stream_url,body.local_network_only)
    except ValueError as exc:raise HTTPException(422,str(exc))
    c=Camera(patient_id=pid,name=body.name,room=body.room,protocol=body.protocol,stream_url=clean,username=body.username or u,password=body.password or pw,enabled=body.enabled,analysis_mode=body.analysis_mode,audio_enabled=body.audio_enabled,record_clips=body.record_clips,retention_days=body.retention_days,local_network_only=body.local_network_only,cloud_upload_allowed=body.cloud_upload_allowed)
    db.add(c);db.flush();audit(db,user,pid,'camera_add','cameras',c.id);db.commit()
    return public(c)

@router.put('/cameras/{cid}')
def camera_edit(cid:int,body:CameraIn,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    c=own_resource(db,user,Camera,cid);p=db.get(Patient,c.patient_id)
    if p.user_id!=user.id:raise HTTPException(403,'Only the primary guardian can configure cameras')
    try:clean,u,pw=CAM.validate_stream(body.stream_url,body.local_network_only)
    except ValueError as exc:raise HTTPException(422,str(exc))
    snapshot={k:v for k,v in {col.name:getattr(c,col.name) for col in Camera.__table__.columns}.items() if k not in ('password','username','stream_url')}
    from .models import RecordRevision
    db.add(RecordRevision(patient_id=c.patient_id,user_id=user.id,entity='cameras',entity_id=c.id,snapshot_json=json.dumps(snapshot,default=str)))
    for k in ('name','room','protocol','enabled','analysis_mode','audio_enabled','record_clips','retention_days','local_network_only','cloud_upload_allowed'):setattr(c,k,getattr(body,k))
    c.stream_url=clean
    if body.username or u:c.username=body.username or u
    if body.password or pw:c.password=body.password or pw
    elif not body.keep_existing_password:c.password=''
    c.updated_at=utcnow();audit(db,user,c.patient_id,'camera_edit','cameras',cid);db.commit();return public(c)

@router.patch('/cameras/{cid}')
def camera_patch(cid:int,body:CameraPatch,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    c=own_resource(db,user,Camera,cid)
    for k,v in body.model_dump(exclude_none=True).items():setattr(c,k,v)
    if body.enabled is False:c.disabled_at=utcnow();c.worker_state='disabled'
    c.updated_at=utcnow();audit(db,user,c.patient_id,'camera_patch','cameras',cid);db.commit();return public(c)

@router.post('/cameras/{cid}/disable')
def camera_disable(cid:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    """Instant off switch: the worker stops analysing on its next heartbeat and no further events are accepted."""
    c=own_resource(db,user,Camera,cid);c.enabled=False;c.disabled_at=utcnow();c.worker_state='disabled';c.updated_at=utcnow()
    audit(db,user,c.patient_id,'camera_disable','cameras',cid);db.commit();return public(c)

@router.delete('/cameras/{cid}')
def camera_delete(cid:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    c=own_resource(db,user,Camera,cid);pid=c.patient_id
    if db.get(Patient,pid).user_id!=user.id:raise HTTPException(403,'Only the primary guardian can remove a camera')
    for o in db.query(CameraObservation).filter_by(camera_id=cid):
        if o.clip_storage_name and not o.clip_deleted_at:
            try:storage.delete_document(o.clip_storage_name)
            except Exception:pass
    db.delete(c);audit(db,user,pid,'camera_delete','cameras',cid);db.commit();return {'ok':True}

@router.post('/cameras/{cid}/test')
def camera_test(cid:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    c=own_resource(db,user,Camera,cid)
    result=CAM.probe(c.protocol,c.stream_url,c.username,c.password)
    c.last_test_at=utcnow();c.last_test_ok=result['ok'];c.last_test_detail=result['detail'][:200];audit(db,user,c.patient_id,'camera_test','cameras',cid);db.commit()
    return {**result,'camera':public(c)}

@router.post('/cameras/{cid}/worker-key')
def camera_worker_key(cid:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    """Issue a key for the local worker. Shown once; only its hash is stored."""
    c=own_resource(db,user,Camera,cid)
    if db.get(Patient,c.patient_id).user_id!=user.id:raise HTTPException(403,'Only the primary guardian can issue worker keys')
    key,h=CAM.new_worker_key();c.worker_key_hash=h;c.updated_at=utcnow();audit(db,user,c.patient_id,'camera_worker_key','cameras',cid);db.commit()
    return {'camera_id':cid,'worker_key':key,'note':'Store this in the worker environment (BABYCARE_CAMERA_KEY). It is not retrievable later; issue a new one to rotate.'}

@router.get('/cameras/{cid}/preview')
def camera_preview(cid:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    """One JPEG frame, fetched only after authentication and only when OpenCV is available on this host."""
    c=own_resource(db,user,Camera,cid)
    try:import cv2
    except ImportError:raise HTTPException(503,'Preview needs opencv-python-headless on the machine that runs the API; the local worker preview is available in the worker log instead.')
    url=c.stream_url
    if c.username:
        from urllib.parse import urlsplit,urlunsplit,quote
        p=urlsplit(url);url=urlunsplit((p.scheme,f'{quote(c.username)}:{quote(c.password)}@{p.hostname}'+(f':{p.port}' if p.port else ''),p.path,p.query,''))
    cap=cv2.VideoCapture(url)
    ok,frame=cap.read();cap.release()
    if not ok:raise HTTPException(502,'Camera did not return a frame')
    ok,buf=cv2.imencode('.jpg',frame,[int(cv2.IMWRITE_JPEG_QUALITY),80])
    return Response(buf.tobytes(),media_type='image/jpeg',headers={'Cache-Control':'no-store'})

# ----------------------------------------------------------------------------------- worker ingest (key-authenticated, no user session)
class WorkerEvent(BaseModel):
    kind:Literal[OBSERVATION_KINDS]
    started_at:datetime
    ended_at:datetime|None=None
    confidence:float=Field(ge=0,le=1)
    metrics:dict=Field(default_factory=dict)
    dedupe_key:str=Field(min_length=4,max_length=80)

class WorkerBatch(BaseModel):
    worker_id:str=Field(min_length=1,max_length=60)
    state:Literal['connected','reconnecting','disconnected']='connected'
    message:str=Field(default='',max_length=200)
    events:list[WorkerEvent]=Field(default_factory=list,max_length=500)

def worker_camera(db,cid,key):
    c=db.get(Camera,cid)
    if not c or not c.worker_key_hash or not key or CAM.key_hash(key)!=c.worker_key_hash:raise HTTPException(401,'Invalid camera worker key')
    return c

@router.get('/cameras/{cid}/worker-config')
def worker_config(cid:int,x_camera_worker_key:str|None=Header(default=None),db:DbSession=Depends(get_db)):
    """The worker fetches its instructions here; credentials are returned ONLY to a holder of the worker key."""
    c=worker_camera(db,cid,x_camera_worker_key)
    return {'camera_id':c.id,'enabled':c.enabled,'protocol':c.protocol,'stream_url':c.stream_url,'username':c.username,'password':c.password,'analysis_mode':c.analysis_mode,'audio_enabled':c.audio_enabled,'record_clips':c.record_clips,'retention_days':c.retention_days,'cloud_upload_allowed':c.cloud_upload_allowed}

@router.post('/cameras/{cid}/worker/ingest')
def worker_ingest(cid:int,body:WorkerBatch,x_camera_worker_key:str|None=Header(default=None),db:DbSession=Depends(get_db)):
    c=worker_camera(db,cid,x_camera_worker_key);p=db.get(Patient,c.patient_id)
    c.worker_seen_at=utcnow();c.worker_state=body.state if c.enabled else 'disabled';c.worker_message=body.message[:200]
    accepted=0;skipped=0;seen=set()
    if c.enabled and c.analysis_mode!='off':
        for ev in body.events:
            if ev.kind in ('walking_like','crawling_like','standing','sitting') and ev.confidence<0.6:skipped+=1;continue  # posture classes require a reliable model
            if ev.dedupe_key in seen or db.query(CameraObservation).filter_by(camera_id=cid,dedupe_key=ev.dedupe_key).first():skipped+=1;continue
            seen.add(ev.dedupe_key)
            started=ev.started_at if ev.started_at.tzinfo else ev.started_at.replace(tzinfo=timezone.utc)
            db.add(CameraObservation(patient_id=c.patient_id,camera_id=cid,started_at=started,ended_at=ev.ended_at,local_date=C.local_date_of(started,p),kind=ev.kind,confidence=ev.confidence,worker_id=body.worker_id,metrics_json=json.dumps(ev.metrics)[:400],dedupe_key=ev.dedupe_key))
            accepted+=1
    else:skipped=len(body.events)
    db.commit()
    return {'accepted':accepted,'skipped':skipped,'enabled':c.enabled,'analysis_mode':c.analysis_mode,'record_clips':c.record_clips,'audio_enabled':c.audio_enabled}

# ----------------------------------------------------------------------------------- review and analytics
@router.get('/patients/{pid}/camera-observations')
def observations(pid:int,day:str|None=None,status:str|None=None,limit:int=Query(default=200,ge=1,le=1000),user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    p=own_patient(db,user,pid);q=db.query(CameraObservation).filter_by(patient_id=pid)
    if day:q=q.filter(CameraObservation.local_date==datetime.fromisoformat(day).date())
    if status:q=q.filter(CameraObservation.review_status==status)
    rows=q.order_by(CameraObservation.started_at.desc()).limit(limit).all()
    return {'items':[obs_row(o,p) for o in rows],'warnings':['Camera events describe movement and presence only. They are never used to infer fever, illness, pain or development.','Only parent-confirmed events are used in long-term activity summaries.']}

class ObservationReview(BaseModel):
    action:Literal['confirm','correct','dismiss']
    corrected_kind:Literal[OBSERVATION_KINDS]|None=None
    note:str=Field(default='',max_length=500)

@router.post('/camera-observations/{oid}/review')
def observation_review(oid:int,body:ObservationReview,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    o=own_resource(db,user,CameraObservation,oid);p=db.get(Patient,o.patient_id)
    if body.action=='correct' and not body.corrected_kind:raise HTTPException(422,'Choose the corrected activity')
    o.review_status={'confirm':'parent_confirmed','correct':'parent_corrected','dismiss':'dismissed'}[body.action]
    o.corrected_kind=body.corrected_kind or '' if body.action=='correct' else o.corrected_kind
    o.review_note=body.note;o.reviewed_by=user.id;o.reviewed_at=utcnow()
    audit(db,user,o.patient_id,'camera_observation_'+body.action,'camera_observations',oid);db.commit();return obs_row(o,p)

class AddToJournal(BaseModel):
    as_kind:Literal['activity','sleep']='activity'
    activity_kind:str=Field(default='playing',max_length=20)
    note:str=Field(default='',max_length=500)

@router.post('/camera-observations/{oid}/add-to-journal',status_code=201)
def observation_to_journal(oid:int,body:AddToJournal,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    """Parent confirmation first: creates a journal entry marked source=camera_confirmed and confirms the observation."""
    o=own_resource(db,user,CameraObservation,oid);p=db.get(Patient,o.patient_id)
    if o.journal_entry_id:raise HTTPException(409,'This observation is already in the journal')
    started=C.aware(o.started_at);ended=C.aware(o.ended_at) if o.ended_at else None;dur=C.minutes_between(started,ended) if ended else None
    if body.as_kind=='sleep':entry=SleepEntry(patient_id=o.patient_id,recorded_by=user.id,at=started,local_date=o.local_date,kind='nap',ended_at=ended,duration_minutes=dur,note=body.note or f'From camera observation #{o.id}',source='camera_confirmed',camera_observation_id=o.id,client_key=f'camera-{o.id}')
    else:entry=ActivityEntry(patient_id=o.patient_id,recorded_by=user.id,at=started,local_date=o.local_date,kind=body.activity_kind if body.activity_kind in ('awake','playing','walking','crawling','sitting','feeding','outdoor','exercise','bath','tummy_time','other') else 'playing',ended_at=ended,duration_minutes=dur,note=body.note or f'From camera observation #{o.id}',source='camera_confirmed',camera_observation_id=o.id,client_key=f'camera-{o.id}')
    db.add(entry);db.flush();o.journal_entry_kind=body.as_kind;o.journal_entry_id=entry.id
    if o.review_status=='ai_observed':o.review_status='parent_confirmed';o.reviewed_by=user.id;o.reviewed_at=utcnow()
    audit(db,user,o.patient_id,'camera_observation_to_journal',entry.__tablename__,entry.id);db.commit()
    return {'observation':obs_row(o,p),'entry':C.row(entry,p)}

@router.get('/patients/{pid}/camera-analytics')
def camera_analytics(pid:int,start:str|None=None,end:str|None=None,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    """Conservative non-medical activity analytics from PARENT-CONFIRMED events; AI-only events are reported separately as pending."""
    p=own_patient(db,user,pid);today=C.local_today(p)
    e=datetime.fromisoformat(end).date() if end else today;s=datetime.fromisoformat(start).date() if start else e-timedelta(days=6)
    rows=db.query(CameraObservation).filter(CameraObservation.patient_id==pid,CameraObservation.local_date>=s,CameraObservation.local_date<=e).all()
    def mins(o):return C.minutes_between(o.started_at,o.ended_at) or 0
    confirmed=[o for o in rows if o.review_status in ('parent_confirmed','parent_corrected')]
    per_day={}
    for o in confirmed:
        d=per_day.setdefault(o.local_date.isoformat(),{'movement':0,'low_activity':0,'presence':0,'rest':0,'other':0})
        k=o.corrected_kind or o.kind;d[k if k in d else 'other']+=mins(o)
    return {'start':s.isoformat(),'end':e.isoformat(),'confirmed_minutes_by_day':per_day,'confirmed_events':len(confirmed),'pending_ai_events':sum(1 for o in rows if o.review_status=='ai_observed'),'dismissed':sum(1 for o in rows if o.review_status=='dismissed'),'kinds':{k:sum(1 for o in confirmed if (o.corrected_kind or o.kind)==k) for k in OBSERVATION_KINDS},
        'warnings':['Minutes are summed from parent-confirmed camera events only.','Movement and presence are not developmental or medical measures; walking-like / crawling-like classes are only emitted when a reliable model is installed on the worker.']}

@router.post('/cameras/{cid}/purge-recordings')
def purge(cid:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    c=own_resource(db,user,Camera,cid);n=0
    for o in db.query(CameraObservation).filter_by(camera_id=cid):
        if o.clip_storage_name and not o.clip_deleted_at:
            try:storage.delete_document(o.clip_storage_name)
            except Exception:pass
            o.clip_deleted_at=utcnow();n+=1
    audit(db,user,c.patient_id,'camera_purge_recordings','cameras',cid);db.commit();return {'deleted_clips':n}
