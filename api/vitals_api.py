"""Vitals journal REST API (adult-friendly Daily Health fields). Guardian-only, audited, per profile."""
from datetime import date, timedelta
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session as DbSession
from .database import get_db
from .security import require_user, own_patient, own_resource
from .models import User, Audit, utcnow
from .history import remember
from .vitals_models import VitalsEntry
from . import vitals as V
from .care import local_today

router=APIRouter(prefix='/api',tags=['vitals'])
def audit(db,user,pid,action,entity,obj_id):db.add(Audit(user_id=user.id,patient_id=pid,action=action,entity=entity,entity_id=obj_id))

class VitalsIn(BaseModel):
    at:Optional[str]=None
    systolic:Optional[float]=None;diastolic:Optional[float]=None;heart_rate:Optional[float]=None;spo2:Optional[float]=None
    glucose_mg_dl:Optional[float]=None;glucose_context:Optional[str]=Field(default='',max_length=20)
    weight_kg:Optional[float]=None;temperature_c:Optional[float]=None
    meal:Optional[str]=Field(default='',max_length=300);note:Optional[str]=Field(default='',max_length=500)
    client_key:Optional[str]=Field(default=None,max_length=80)

def _range(p,days,start,end):
    today=local_today(p)
    if start or end:
        s=start or (end or today)-timedelta(days=6);e=end or today
    else:e=today;s=e-timedelta(days=max(1,min(days,365))-1)
    if s>e:raise HTTPException(422,'start must not be after end')
    return s,e

@router.post('/patients/{pid}/vitals',status_code=201)
def add(pid:int,body:VitalsIn,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    p=own_patient(db,user,pid);row,created=V.add_entry(db,p,user,body.model_dump())
    if created:audit(db,user,pid,'vitals_add','vitals_entries',row.id);db.commit()
    out=V.entry_json(row,p);out['created']=created;return out

@router.get('/patients/{pid}/vitals')
def list_entries(pid:int,days:int=7,start:Optional[date]=None,end:Optional[date]=None,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    p=own_patient(db,user,pid);s,e=_range(p,days,start,end)
    return {'start':s.isoformat(),'end':e.isoformat(),'entries':[V.entry_json(r,p) for r in V.entries(db,pid,s,e)]}

@router.get('/patients/{pid}/vitals/summary')
def summary(pid:int,days:int=14,start:Optional[date]=None,end:Optional[date]=None,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    p=own_patient(db,user,pid);s,e=_range(p,days,start,end);return V.summary(db,p,s,e)

@router.patch('/vitals/{eid}')
def edit(eid:int,body:VitalsIn,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    x=own_resource(db,user,VitalsEntry,eid);p=own_patient(db,user,x.patient_id)
    if x.status!='active':raise HTTPException(409,'Entry was withdrawn')
    vals=V.validate({**{k:getattr(x,k) for k in V.RANGES},'glucose_context':x.glucose_context,'meal':x.meal,'note':x.note,**{k:v for k,v in body.model_dump(exclude_unset=True).items() if k not in ('at','client_key')}})
    remember(db,user,x)
    for k,v in vals.items():setattr(x,k,v)
    for k in V.RANGES:
        if k in body.model_fields_set and getattr(body,k) is None:setattr(x,k,None)
    if body.at:
        when=V.parse_when(body.at,p)
        if when>utcnow()+timedelta(minutes=5):raise HTTPException(422,'Entries cannot be in the future')
        x.at=when;x.local_date=V.local_date_of(when,p)
    x.version+=1;x.updated_at=utcnow();audit(db,user,x.patient_id,'vitals_edit','vitals_entries',x.id);db.commit();return V.entry_json(x,p)

@router.post('/vitals/{eid}/withdraw')
def withdraw(eid:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    x=own_resource(db,user,VitalsEntry,eid)
    if x.status=='active':
        remember(db,user,x);x.status='withdrawn';x.version+=1;x.updated_at=utcnow();audit(db,user,x.patient_id,'vitals_withdraw','vitals_entries',x.id);db.commit()
    return {'ok':True,'id':x.id,'status':x.status}
