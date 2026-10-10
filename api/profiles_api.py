"""Family workspace: profile metadata and per-profile module configuration."""
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session as DbSession
from .database import get_db
from .security import require_user, own_patient
from .models import User, Audit
from .history import remember
from . import profiles as PR

router=APIRouter(prefix='/api',tags=['profiles'])
def audit(db,user,pid,action,entity,obj_id):db.add(Audit(user_id=user.id,patient_id=pid,action=action,entity=entity,entity_id=obj_id))

class ModulesIn(BaseModel):
    enabled:Optional[list[str]]=None     # None resets to the age-based defaults
class ProfileMetaIn(BaseModel):
    sex:Optional[str]=Field(default=None,max_length=16)
    relationship:Optional[str]=Field(default=None,max_length=32)

@router.get('/profiles/options')
def options():
    return {'brand':PR.BRAND,'tagline':PR.TAGLINE,'modules':[{'key':k,'title':t,'description':d} for k,t,d in PR.ALL_MODULES],'relationships':PR.RELATIONSHIPS,'sexes':PR.SEXES,'age_groups':PR.AGE_GROUPS,'defaults':{g:sorted(v,key=PR.MODULE_KEYS.index) for g,v in PR.DEFAULT_MODULES.items()}}

@router.get('/patients/{pid}/profile')
def profile(pid:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    return PR.profile_json(own_patient(db,user,pid))

@router.patch('/patients/{pid}/profile')
def profile_meta(pid:int,body:ProfileMetaIn,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    p=own_patient(db,user,pid)
    if body.sex not in (None,'') and body.sex not in PR.SEXES:raise HTTPException(422,'Unknown sex value')
    if body.relationship not in (None,'') and body.relationship not in PR.RELATIONSHIPS:raise HTTPException(422,'Unknown relationship')
    remember(db,user,p)
    if 'sex' in body.model_fields_set:p.sex=body.sex or None
    if 'relationship' in body.model_fields_set:p.relationship=body.relationship or None
    audit(db,user,pid,'profile_meta','patient',pid);db.commit();return PR.profile_json(p)

@router.put('/patients/{pid}/modules')
def modules(pid:int,body:ModulesIn,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    p=own_patient(db,user,pid)
    if body.enabled is not None:
        bad=[k for k in body.enabled if k not in PR.MODULE_KEYS]
        if bad:raise HTTPException(422,f'Unknown module(s): {", ".join(bad)}')
        PR.set_modules(p,body.enabled)
    else:PR.reset_modules(p)
    audit(db,user,pid,'profile_modules','patient',pid);db.commit();return PR.profile_json(p)
