"""Record download / export API: preview, then generate the premium PDF dossier (optionally with the original documents
appended) or a complete ZIP archive. Everything is generated in memory per request, streamed once and never written to
disk or to the repository. Only a guardian of the child can export; every export is audited."""
import hashlib
import io
import re
from datetime import date
from typing import Literal, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session as DbSession
from .database import get_db
from .security import require_user, own_patient
from .models import User, Audit
from . import export_pdf, export_model

router=APIRouter(prefix='/api',tags=['export'])
Preset=Literal['complete','ecoli','latest_hospitalization','antibiotics','labs','vaccination','daily_care_7d','custom']
Mode=Literal['summary','with_sources','archive']

class ExportSpec(BaseModel):
    preset:Preset='complete'
    mode:Mode='with_sources'
    sections:Optional[list[str]]=None          # custom only: subset of export_model.ALL_SECTIONS (+ 'prescriptions')
    date_from:Optional[date]=None
    date_to:Optional[date]=None
    include_sources:bool=True
    include_photos:bool=False                   # stool photographs only when explicitly chosen
    daily_care_days:Optional[int]=Field(default=None,ge=1,le=365)
    reason:Optional[str]=Field(default=None,max_length=200)
    def custom(self):
        if self.preset!='custom':return {'include_photos':self.include_photos,'include_sources':self.include_sources}
        bad=[s for s in (self.sections or []) if s not in export_model.ALL_SECTIONS+('prescriptions','stool_photos')]
        if bad:raise HTTPException(422,f'Unknown section(s): {", ".join(bad)}')
        if self.date_from and self.date_to and self.date_from>self.date_to:raise HTTPException(422,'date_from must not be after date_to')
        secs=[s for s in (self.sections or list(export_model.ALL_SECTIONS)) if s!='stool_photos']
        return {'sections':secs,'date_from':self.date_from,'date_to':self.date_to,'include_sources':self.include_sources,'include_photos':self.include_photos or ('stool_photos' in (self.sections or [])),'daily_care_days':self.daily_care_days}

def audit(db,user,pid,action,entity,obj_id):db.add(Audit(user_id=user.id,patient_id=pid,action=action,entity=entity,entity_id=obj_id))

@router.get('/export/presets')
def presets():
    return {'presets':[{'key':k,'title':t,'description':d} for k,(t,_,d) in export_model.EXPORT_TYPES.items()],'sections':list(export_model.ALL_SECTIONS)+['prescriptions','stool_photos'],'modes':[{'key':'summary','title':'PDF summary','description':'Designed medical summary only.'},{'key':'with_sources','title':'PDF + original reports','description':'Summary with every original document appended as an appendix (recommended for doctors).'},{'key':'archive','title':'Complete ZIP archive','description':'Medical_Record.pdf, Original_Reports/ as separate files and manifest.csv.'}]}

@router.post('/patients/{pid}/export/preview')
def export_preview(pid:int,spec:ExportSpec,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    p=own_patient(db,user,pid)
    pv=export_pdf.preview(db,p,spec.preset,spec.custom())
    pv['mode']=spec.mode;pv['filename']=pv['filename']+('.zip' if spec.mode=='archive' else '.pdf')
    if spec.mode=='summary':pv['estimated_pages']=pv['estimated_pages_summary_only']
    return pv

def _stream(data,filename,media):
    def gen(payload=data):
        buf=io.BytesIO(payload);payload=None
        try:
            while True:
                chunk=buf.read(1<<20)
                if not chunk:break
                yield chunk
        finally:buf.close()
    safe=re.sub(r'[^A-Za-z0-9._-]','_',filename)
    return StreamingResponse(gen(),media_type=media,headers={'Content-Disposition':f'attachment; filename="{safe}"','Cache-Control':'no-store, private','X-Content-Type-Options':'nosniff','Content-Length':str(len(data))})

@router.post('/patients/{pid}/export/download')
def export_download(pid:int,spec:ExportSpec,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    p=own_patient(db,user,pid)
    custom=spec.custom()
    data,filename,info=export_pdf.render(db,p,spec.preset,custom,spec.mode)
    audit(db,user,pid,f'export_{spec.mode}','export',0);db.add(Audit(user_id=user.id,patient_id=pid,action='export_detail',entity=f"{spec.preset}:{info['pages']}p:{info['sources_appended']}src:{hashlib.sha256(data).hexdigest()[:12]}",entity_id=0));db.commit()
    return _stream(data,filename,'application/zip' if spec.mode=='archive' else 'application/pdf')

@router.get('/patients/{pid}/export/download')
def export_download_get(pid:int,preset:Preset='complete',mode:Mode='with_sources',include_photos:bool=False,include_sources:bool=True,sections:Optional[str]=None,date_from:Optional[date]=None,date_to:Optional[date]=None,daily_care_days:Optional[int]=Query(default=None,ge=1,le=365),user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    """Same as POST for simple links (mobile share sheet downloads with a GET)."""
    spec=ExportSpec(preset=preset,mode=mode,include_photos=include_photos,include_sources=include_sources,sections=[x for x in (sections or '').split(',') if x] or None,date_from=date_from,date_to=date_to,daily_care_days=daily_care_days)
    return export_download(pid,spec,user,db)

@router.get('/patients/{pid}/export/debug.txt')
def export_text(pid:int,preset:Preset='complete',user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    """Plain-text rendering of the same MedicalExportModel (internal / support use)."""
    from fastapi.responses import PlainTextResponse
    p=own_patient(db,user,pid);model=export_model.build_model(db,p,preset)
    return PlainTextResponse(export_model.export_text(model),headers={'Cache-Control':'no-store, private'})
