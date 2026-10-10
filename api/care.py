"""Daily Care engine: day summaries, unified timeline, calendar views, observational analytics, illness comparison,
doctor export and structured Ask answers. Everything is computed from recorded rows; no clinical inference.

Language rules enforced here: counts and totals describe what the parent recorded; comparisons are co-occurrence
statements ("loose-stool entries were recorded on 4 of 6 days"), never causal; thresholds are only the ones the
parent typed in as clinician-provided.
"""
import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone, time
from zoneinfo import ZoneInfo
from .models import Patient, Medicine, MedicationEvent, Vaccine, HospitalVisit, Admission, Observation, Episode, utcnow
from .lifetime_models import FeverEpisode, AntibioticCourse
from .care_models import (CareSettings, FluidEntry, DiaperEntry, StoolEntry, StoolPhoto, MedicationAdministration, SymptomEntry,
    ActivityEntry, SleepEntry, Camera, CameraObservation, CLINICAL_REVIEW_COLOURS)

KIND_LABEL={'fluid':'Fluids','diaper':'Diapers','stool':'Stool','medicine':'Medicines','symptom':'Symptoms','activity':'Activity','sleep':'Sleep','vaccine':'Vaccination','visit':'Hospital visit','admission':'Hospital stay','lab':'Lab result','fever':'Fever (documented)','camera':'Camera suggestion','medication_event':'Medicines'}

# ------------------------------------------------------------------ time helpers
def tz_of(p):return ZoneInfo(p.tz)
def aware(dt):return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
def local(dt,p):return aware(dt).astimezone(tz_of(p))
def local_today(p):return utcnow().astimezone(tz_of(p)).date()
def parse_when(value,p):
    """Client clock values are the child's local time unless they carry an offset."""
    if isinstance(value,datetime):dt=value
    else:dt=datetime.fromisoformat(str(value).replace('Z','+00:00'))
    if dt.tzinfo is None:dt=dt.replace(tzinfo=tz_of(p))
    return dt.astimezone(timezone.utc)
def local_date_of(dt,p):return local(dt,p).date()
def day_bounds(d,p):
    start=datetime.combine(d,time.min,tzinfo=tz_of(p));return start.astimezone(timezone.utc),(start+timedelta(days=1)).astimezone(timezone.utc)
def hhmm(dt,p):return local(dt,p).strftime('%H:%M')
def iso_local(dt,p):return local(dt,p).isoformat() if dt else None
def minutes_between(a,b):return int(round((aware(b)-aware(a)).total_seconds()/60)) if a and b else None

def row(obj,p=None):
    out={}
    for c in obj.__table__.columns:
        v=getattr(obj,c.name)
        if isinstance(v,datetime):out[c.name]=iso_local(v,p) if p else aware(v).isoformat()
        elif isinstance(v,date):out[c.name]=v.isoformat()
        else:out[c.name]=v
    return out

def active(q,model):return q.filter(model.status=='active')

def entries(db,pid,start,end):
    """All active journal rows whose local_date lies in [start,end]."""
    out={}
    for key,model in (('fluid',FluidEntry),('diaper',DiaperEntry),('stool',StoolEntry),('medicine',MedicationAdministration),('symptom',SymptomEntry),('activity',ActivityEntry),('sleep',SleepEntry)):
        out[key]=active(db.query(model).filter(model.patient_id==pid,model.local_date>=start,model.local_date<=end),model).order_by(model.at).all()
    return out

def settings_for(db,pid):
    s=db.query(CareSettings).filter_by(patient_id=pid).first()
    return s or CareSettings(patient_id=pid)

# ------------------------------------------------------------------ medicine schedule (due slots come only from verified prescriptions)
def due_slots(db,p,d):
    slots=[]
    for m in db.query(Medicine).filter(Medicine.patient_id==p.id,Medicine.status=='verified'):
        if not m.schedule_times:continue
        if m.start_date and d<m.start_date:continue
        if m.end_date and d>m.end_date:continue
        if m.actual_stop_date and d>m.actual_stop_date:continue
        for hm in m.schedule_times.split(','):
            h,mi=map(int,hm.split(':'))
            slots.append({'medicine_id':m.id,'medicine_name':m.name,'dose':m.dosage,'dose_unit':m.dose_unit,'route':m.route,'scheduled_for':datetime.combine(d,time(h,mi),tzinfo=tz_of(p)).astimezone(timezone.utc)})
    return sorted(slots,key=lambda s:s['scheduled_for'])

def medicine_day(db,p,d,admins=None):
    """Due slots versus what the parent actually recorded. A slot without a record is 'no record', never 'given'."""
    admins=admins if admins is not None else active(db.query(MedicationAdministration).filter(MedicationAdministration.patient_id==p.id,MedicationAdministration.local_date==d),MedicationAdministration).all()
    legacy=[(e,m) for e,m in db.query(MedicationEvent,Medicine).join(Medicine,Medicine.id==MedicationEvent.medicine_id).filter(Medicine.patient_id==p.id) if local_date_of(e.occurred_at,p)==d]
    slots=due_slots(db,p,d);used=set();out=[]
    for s in slots:
        match=None
        for a in admins:
            if a.id in used or a.medicine_id!=s['medicine_id']:continue
            if a.scheduled_for and abs((aware(a.scheduled_for)-s['scheduled_for']).total_seconds())<60:match=a;break
            if not a.scheduled_for and abs((aware(a.at)-s['scheduled_for']).total_seconds())<=3*3600:match=a;break
        if match:used.add(match.id)
        out.append({**{k:(v.isoformat() if isinstance(v,datetime) else v) for k,v in s.items()},'scheduled_local':hhmm(s['scheduled_for'],p),'record':row(match,p) if match else None,'status':match.administration_status if match else 'no_record'})
    extra=[row(a,p) for a in admins if a.id not in used]
    legacy_rows=[{'id':e.id,'medicine_id':m.id,'medicine_name':m.name,'at':iso_local(e.occurred_at,p),'administration_status':e.event_type,'source':'medication_event'} for e,m in legacy]
    given=[a for a in admins if a.administration_status=='given']
    return {'date':d.isoformat(),'due':out,'unscheduled_records':extra,'legacy_events':legacy_rows,'counts':{'due':len(slots),'given':len(given)+sum(1 for e,_ in legacy if e.event_type=='given'),'skipped':sum(1 for a in admins if a.administration_status=='skipped')+sum(1 for e,_ in legacy if e.event_type=='skipped'),'missed':sum(1 for a in admins if a.administration_status=='missed'),'later':sum(1 for a in admins if a.administration_status=='later'),'unknown':sum(1 for a in admins if a.administration_status=='unknown'),'due_without_record':sum(1 for x in out if x['status']=='no_record')}}

# ------------------------------------------------------------------ day summary
def day_summary(db,pid,d):
    p=db.get(Patient,pid);e=entries(db,pid,d,d);s=settings_for(db,pid);today=local_today(p);now=utcnow()
    fluids=e['fluid'];known=[f for f in fluids if f.amount_ml is not None and not f.amount_unknown]
    by_type=defaultdict(float)
    for f in known:by_type[f.fluid_type]+=f.amount_ml
    diapers=e['diaper'];wet=[x for x in diapers if x.kind in ('wet','mixed')];stool_d=[x for x in diapers if x.kind in ('stool','mixed')]
    stools=e['stool']
    last_wet=max((aware(x.at) for x in wet),default=None)
    last_stool_candidates=[aware(x.at) for x in stool_d]+[aware(x.at) for x in stools]
    last_stool=max(last_stool_candidates,default=None)
    if d==today:  # look back across earlier days for "time since"
        prev_wet=active(db.query(DiaperEntry).filter(DiaperEntry.patient_id==pid,DiaperEntry.kind.in_(('wet','mixed')),DiaperEntry.local_date<d),DiaperEntry).order_by(DiaperEntry.at.desc()).first()
        prev_stool=active(db.query(StoolEntry).filter(StoolEntry.patient_id==pid,StoolEntry.local_date<d),StoolEntry).order_by(StoolEntry.at.desc()).first()
        prev_stool_d=active(db.query(DiaperEntry).filter(DiaperEntry.patient_id==pid,DiaperEntry.kind.in_(('stool','mixed')),DiaperEntry.local_date<d),DiaperEntry).order_by(DiaperEntry.at.desc()).first()
        if not last_wet and prev_wet:last_wet=aware(prev_wet.at)
        if not last_stool:last_stool=max([aware(x.at) for x in (prev_stool,prev_stool_d) if x],default=None)
    meds=medicine_day(db,p,d,e['medicine'])
    sleep_min=sum((x.duration_minutes or minutes_between(x.at,x.ended_at) or 0) for x in e['sleep'])
    active_min=sum((x.duration_minutes or minutes_between(x.at,x.ended_at) or 0) for x in e['activity'] if x.kind!='awake')
    threshold=None
    if s.wet_diaper_threshold_per_day is not None:
        threshold={'value':s.wet_diaper_threshold_per_day,'source':s.threshold_source,'wet_today':len(wet),'review_requested':len(wet)<s.wet_diaper_threshold_per_day and d==today and local(now,p).hour>=18,'message':'Review requested: fewer wet diapers than the threshold you entered.' if len(wet)<s.wet_diaper_threshold_per_day else ''}
    camera_suggestions=db.query(CameraObservation).filter(CameraObservation.patient_id==pid,CameraObservation.local_date==d,CameraObservation.review_status=='ai_observed',CameraObservation.kind.in_(('active','walking_like','crawling_like','rest','standing','sitting'))).count()
    return {'date':d.isoformat(),'is_today':d==today,
        'fluids':{'total_ml':round(sum(f.amount_ml for f in known),1),'events':len(fluids),'unknown_amount_events':len(fluids)-len(known),'by_type':{k:round(v,1) for k,v in by_type.items()},'timeline':[{'id':f.id,'at':hhmm(f.at,p),'amount_ml':f.amount_ml,'amount_unknown':f.amount_unknown,'custom_amount':f.custom_amount,'fluid_type':f.fluid_type,'duration_minutes':f.duration_minutes,'note':f.note} for f in fluids]},
        'diapers':{'wet':len(wet),'stool':len(stool_d),'mixed':sum(1 for x in diapers if x.kind=='mixed'),'dry_checks':sum(1 for x in diapers if x.kind=='dry'),'total_changes':sum(1 for x in diapers if x.kind!='dry'),'minutes_since_last_wet':minutes_between(last_wet,now) if last_wet else None,'minutes_since_last_stool':minutes_between(last_stool,now) if last_stool else None,'last_wet_at':aware(last_wet).astimezone(tz_of(p)).isoformat() if last_wet else None,'last_stool_at':aware(last_stool).astimezone(tz_of(p)).isoformat() if last_stool else None,'threshold':threshold},
        'stool':{'entries':len(stools),'watery_or_loose':sum(1 for x in stools if x.watery or x.consistency in ('watery','loose')),'with_photo':sum(1 for x in stools if db.query(StoolPhoto).filter_by(stool_entry_id=x.id,deleted_at=None).count()),'review_suggested':sum(1 for x in stools if x.clinical_review_suggested),'items':[stool_row(db,x,p) for x in stools],'photos':stool_photos(db,p,stools)},
        'medicines':meds,
        'symptoms':[row(x,p) for x in e['symptom']],
        'activity':{'active_minutes':active_min,'items':[row(x,p) for x in e['activity']]},
        'sleep':{'sleep_minutes':sleep_min,'periods':[row(x,p) for x in e['sleep']]},
        'camera_suggestions_pending':camera_suggestions,
        'warnings':['Fluid volumes and diaper counts are what you recorded; they are not a hydration assessment.','Medicines are counted as given only when you recorded them as given.']}

def stool_row(db,x,p):
    photos=db.query(StoolPhoto).filter_by(stool_entry_id=x.id,deleted_at=None).all()
    ai=json.loads(x.ai_observation_json) if x.ai_observation_json else None
    sug=json.loads(x.ai_suggestion_json) if getattr(x,'ai_suggestion_json','') else None
    pv=json.loads(x.parent_values_json) if getattr(x,'parent_values_json','') else None
    return {**row(x,p),'photos':[{'id':ph.id,'stool_entry_id':x.id,'mime':ph.mime,'width':ph.width,'height':ph.height,'taken_at':iso_local(ph.taken_at,p) if ph.taken_at else None,
            'captured_at':iso_local(ph.taken_at,p) if ph.taken_at else None,'captured_at_source':ph.taken_at_source or '','captured_at_confidence':ph.taken_at_confidence or '',
            'detected_taken_at':iso_local(ph.detected_taken_at,p) if ph.detected_taken_at else None,'uploaded_at':iso_local(ph.created_at,p) if ph.created_at else None,
            'taken_at_source':ph.taken_at_source or '','taken_at_confidence':ph.taken_at_confidence or '','original_filename':ph.original_filename or '',
            'thumbnail_url':f'/api/care/stool-photos/{ph.id}','original_url':f'/api/care/stool-photos/{ph.id}','ai_observation':ai,'review_status':x.ai_review_status} for ph in photos],
        'ai_observation':ai,'ai_suggestion':sug,'parent_values':pv,'time_source':getattr(x,'time_source','manual') or 'manual','review_message':'Consider clinical review' if x.clinical_review_suggested else ''}

def photo_meta(ph,x,p):
    return {'id':ph.id,'entry_id':x.id,'at':iso_local(x.at,p),'time':hhmm(x.at,p),'date':x.local_date.isoformat(),'taken_at':iso_local(ph.taken_at,p) if ph.taken_at else None,
        'taken_at_source':ph.taken_at_source or '','taken_at_confidence':ph.taken_at_confidence or '','original_filename':ph.original_filename or '',
        'colour':x.colour,'consistency':x.consistency,'review_message':'Consider clinical review' if x.clinical_review_suggested else '','ai_review_status':x.ai_review_status,'time_source':getattr(x,'time_source','manual') or 'manual','url':f'/api/care/stool-photos/{ph.id}'}

def stool_photos(db,p,stools):
    out=[]
    for x in stools:
        for ph in db.query(StoolPhoto).filter_by(stool_entry_id=x.id,deleted_at=None).order_by(StoolPhoto.id).all():out.append(photo_meta(ph,x,p))
    out.sort(key=lambda m:m['at']);return out

def photo_history(db,pid,start,end):
    """Every stool photograph between two dates, grouped by the entry's local day, newest day first."""
    p=db.get(Patient,pid)
    rows=db.query(StoolPhoto,StoolEntry).join(StoolEntry,StoolEntry.id==StoolPhoto.stool_entry_id).filter(StoolPhoto.patient_id==pid,StoolPhoto.deleted_at==None,StoolEntry.status=='active',StoolEntry.local_date>=start,StoolEntry.local_date<=end).order_by(StoolEntry.at.desc(),StoolPhoto.id).all()
    days={}
    for ph,x in rows:days.setdefault(x.local_date.isoformat(),[]).append(photo_meta(ph,x,p))
    return {'start':start.isoformat(),'end':end.isoformat(),'total':len(rows),'days':[{'date':d,'count':len(v),'photos':v} for d,v in sorted(days.items(),reverse=True)],'disclaimer':'Image-based observation only — not a diagnosis.'}

# ------------------------------------------------------------------ unified timeline
def timeline(db,pid,d,filters=None):
    p=db.get(Patient,pid);e=entries(db,pid,d,d);items=[]
    f=set(filters or [])
    def want(kind):return not f or 'all' in f or kind in f
    linked={x.stool_entry_id:x for x in e['diaper'] if x.stool_entry_id and x.kind in ('stool','mixed')}  # diapers logged together with a stool entry are shown inside that entry
    if want('diaper'):
        for x in e['diaper']:
            if x.stool_entry_id in linked:continue
            items.append({'kind':'diaper','at':iso_local(x.at,p),'time':hhmm(x.at,p),'title':{'wet':'Wet diaper','stool':'Stool diaper','mixed':'Wet + stool diaper','dry':'Dry check'}[x.kind]+(f' · {x.urine_amount}' if x.urine_amount and x.urine_amount!='unknown' else ''),'detail':x.note,'id':x.id,'source':x.source})
    if want('fluid'):
        for x in e['fluid']:items.append({'kind':'fluid','at':iso_local(x.at,p),'time':hhmm(x.at,p),'title':(f'{x.amount_ml:g} ml ' if x.amount_ml is not None and not x.amount_unknown else (x.custom_amount+' ' if x.custom_amount else 'Unknown amount '))+x.fluid_type.replace('_',' ')+(f' · {x.duration_minutes} min' if x.duration_minutes else ''),'detail':x.note,'id':x.id,'source':x.source})
    if want('stool') or (want('diaper') and linked):
        for x in e['stool']:
            if not want('stool') and x.id not in linked:continue
            photos=db.query(StoolPhoto).filter_by(stool_entry_id=x.id,deleted_at=None).order_by(StoolPhoto.id).all()
            dp=linked.get(x.id)
            title='Stool'+(' · '+x.colour.replace('_',' ') if x.colour else '')+(' · '+x.consistency if x.consistency else '')+(' · diaper changed' if dp else '')
            items.append({'kind':'stool','at':iso_local(x.at,p),'time':hhmm(x.at,p),'title':title,'detail':('Consider clinical review · ' if x.clinical_review_suggested else '')+(x.note or ''),'id':x.id,
                'photo':bool(photos),'photo_id':photos[0].id if photos else None,'photo_count':len(photos),'photo_ids':[ph.id for ph in photos],
                'time_source':getattr(x,'time_source','manual') or 'manual','colour':x.colour,'consistency':x.consistency,'amount':x.amount,
                'review_message':'Consider clinical review' if x.clinical_review_suggested else '','ai_review_status':x.ai_review_status,
                'linked_diaper_id':dp.id if dp else None,'diaper_logged':bool(dp),'source':x.source})
    if want('medicine'):
        for x in e['medicine']:items.append({'kind':'medicine','at':iso_local(x.at,p),'time':hhmm(x.at,p),'title':f"{x.medicine_name} — {x.administration_status}"+(f' · {x.dose} {x.dose_unit}'.rstrip() if x.dose else ''),'detail':x.note,'id':x.id,'status':x.administration_status,'source':x.source})
        for ev,m in db.query(MedicationEvent,Medicine).join(Medicine,Medicine.id==MedicationEvent.medicine_id).filter(Medicine.patient_id==pid):
            if local_date_of(ev.occurred_at,p)==d:items.append({'kind':'medication_event','at':iso_local(ev.occurred_at,p),'time':hhmm(ev.occurred_at,p),'title':f'{m.name} — {ev.event_type} (medication history)','detail':ev.note,'id':ev.id,'status':ev.event_type,'source':ev.source})
    if want('symptom'):
        for x in e['symptom']:items.append({'kind':'symptom','at':iso_local(x.at,p),'time':hhmm(x.at,p),'title':x.kind.replace('_',' ').capitalize()+(f' · {x.severity}' if x.severity else '')+(f' · {x.temperature} °{x.temperature_unit}' if x.temperature else ''),'detail':x.note,'id':x.id,'ended_at':iso_local(x.ended_at,p) if x.ended_at else None,'source':x.source})
    if want('activity'):
        for x in e['activity']:items.append({'kind':'activity','at':iso_local(x.at,p),'time':hhmm(x.at,p),'title':x.kind.replace('_',' ').capitalize()+(f' · {x.duration_minutes or minutes_between(x.at,x.ended_at)} min' if (x.duration_minutes or x.ended_at) else ''),'detail':x.note,'id':x.id,'ended_at':iso_local(x.ended_at,p) if x.ended_at else None,'source':x.source})
    if want('sleep'):
        for x in e['sleep']:items.append({'kind':'sleep','at':iso_local(x.at,p),'time':hhmm(x.at,p),'title':('Night sleep' if x.kind=='night' else 'Nap')+(f' · {x.duration_minutes or minutes_between(x.at,x.ended_at)} min' if (x.duration_minutes or x.ended_at) else ''),'detail':x.note,'id':x.id,'ended_at':iso_local(x.ended_at,p) if x.ended_at else None,'source':x.source})
    if want('clinical'):
        for v in db.query(Vaccine).filter(Vaccine.patient_id==pid,Vaccine.given_date==d,Vaccine.confirmed==True):items.append({'kind':'vaccine','at':None,'time':'—','title':f'{v.name} {v.dose_label} vaccination recorded','detail':'','id':v.id})
        for v in db.query(HospitalVisit).filter(HospitalVisit.patient_id==pid):
            if local_date_of(v.planned_at,p)==d:items.append({'kind':'visit','at':iso_local(v.planned_at,p),'time':hhmm(v.planned_at,p),'title':f'{v.purpose} · {v.status}','detail':v.facility,'id':v.id})
        for a in db.query(Admission).filter(Admission.patient_id==pid):
            if a.admitted_on<=d<=(a.discharged_on or d):items.append({'kind':'admission','at':None,'time':'—','title':f'Hospital stay · {a.facility}'+(' (admitted today)' if a.admitted_on==d else ' (discharged today)' if a.discharged_on==d else ''),'detail':a.diagnosis,'id':a.id})
        for o in db.query(Observation).filter(Observation.patient_id==pid,Observation.record_date==d):items.append({'kind':'lab','at':None,'time':'—','title':f'{o.test_name}: {o.value} {o.unit}','detail':o.specimen,'id':o.id,'source_document_id':o.source_document_id,'source_page':o.source_page})
        for fe in db.query(FeverEpisode).filter(FeverEpisode.patient_id==pid,FeverEpisode.verification_status!='rejected'):
            if fe.first_documented_on and fe.first_documented_on<=d<=(fe.last_documented_on or fe.first_documented_on):items.append({'kind':'fever','at':None,'time':'—','title':fe.title,'detail':fe.origin.replace('_',' '),'id':fe.id})
    if want('camera'):
        for c in db.query(CameraObservation).filter(CameraObservation.patient_id==pid,CameraObservation.local_date==d,CameraObservation.review_status!='dismissed'):
            items.append({'kind':'camera','at':iso_local(c.started_at,p),'time':hhmm(c.started_at,p),'title':('Camera · ' if c.review_status=='parent_confirmed' else 'Camera suggestion · ')+(c.corrected_kind or c.kind).replace('_',' ')+f' ({round(c.confidence*100)}%)','detail':c.review_status.replace('_',' '),'id':c.id,'review_status':c.review_status,'ended_at':iso_local(c.ended_at,p) if c.ended_at else None,'in_journal':bool(c.journal_entry_id)})
    items.sort(key=lambda x:(x['at'] or '0000',x['kind']))
    return {'date':d.isoformat(),'items':items,'filters':sorted(f) if f else ['all']}

# ------------------------------------------------------------------ calendar
def month_markers(db,pid,year,month):
    p=db.get(Patient,pid);start=date(year,month,1);end=(date(year+(month==12),month%12+1,1)-timedelta(days=1))
    e=entries(db,pid,start,end);days=defaultdict(lambda:defaultdict(int))
    for key,rows in e.items():
        for x in rows:days[x.local_date.isoformat()][key]+=1
    for f in e['fluid']:
        if f.amount_ml and not f.amount_unknown:days[f.local_date.isoformat()]['fluid_ml']+=f.amount_ml
    for x in e['diaper']:
        if x.kind in ('wet','mixed'):days[x.local_date.isoformat()]['wet']+=1
    for v in db.query(Vaccine).filter(Vaccine.patient_id==pid,Vaccine.given_date>=start,Vaccine.given_date<=end,Vaccine.confirmed==True):days[v.given_date.isoformat()]['vaccine']+=1
    for v in db.query(HospitalVisit).filter(HospitalVisit.patient_id==pid):
        ld=local_date_of(v.planned_at,p)
        if start<=ld<=end:days[ld.isoformat()]['visit']+=1
    for a in db.query(Admission).filter(Admission.patient_id==pid):
        d=max(a.admitted_on,start)
        while d<=min(a.discharged_on or a.admitted_on,end):days[d.isoformat()]['admission']+=1;d+=timedelta(days=1)
    for o in db.query(Observation).filter(Observation.patient_id==pid,Observation.record_date>=start,Observation.record_date<=end):days[o.record_date.isoformat()]['lab']+=1
    for fe in db.query(FeverEpisode).filter(FeverEpisode.patient_id==pid,FeverEpisode.verification_status!='rejected'):
        if not fe.first_documented_on:continue
        d=max(fe.first_documented_on,start)
        while d<=min(fe.last_documented_on or fe.first_documented_on,end):days[d.isoformat()]['fever']+=1;d+=timedelta(days=1)
    for ep in db.query(Episode).filter(Episode.patient_id==pid):
        d=max(ep.started_on,start)
        while d<=min(ep.ended_on or ep.started_on,end):days[d.isoformat()]['illness']+=1;d+=timedelta(days=1)
    for c in db.query(CameraObservation).filter(CameraObservation.patient_id==pid,CameraObservation.local_date>=start,CameraObservation.local_date<=end,CameraObservation.review_status=='parent_confirmed'):days[c.local_date.isoformat()]['camera']+=1
    return {'year':year,'month':month,'days':{k:dict(v) for k,v in days.items()},'categories':['medicine','fever','admission','visit','stool','diaper','vaccine','lab','illness','fluid','symptom','activity','sleep','camera']}

def week_grid(db,pid,start):
    p=db.get(Patient,pid);days=[start+timedelta(days=i) for i in range(7)]
    return {'start':start.isoformat(),'days':[{'date':d.isoformat(),'weekday':d.strftime('%a'),'items':timeline(db,pid,d,['all'])['items'],'summary':{k:v for k,v in day_summary(db,pid,d).items() if k in ('fluids','diapers','stool','medicines','sleep','activity')}} for d in days]}

# ------------------------------------------------------------------ analytics
def daily_series(db,pid,start,end):
    p=db.get(Patient,pid);e=entries(db,pid,start,end);days=[start+timedelta(days=i) for i in range((end-start).days+1)]
    by=lambda rows:defaultdict(list,{d:[x for x in rows if x.local_date==d] for d in days})
    fl,di,st,me,sy,ac,sl=(by(e[k]) for k in ('fluid','diaper','stool','medicine','symptom','activity','sleep'))
    series=[]
    for d in days:
        known=[f for f in fl[d] if f.amount_ml is not None and not f.amount_unknown]
        meds=medicine_day(db,p,d,me[d])['counts']
        series.append({'date':d.isoformat(),'fluid_ml':round(sum(f.amount_ml for f in known),1),'fluid_events':len(fl[d]),'fluid_unknown':len(fl[d])-len(known),
            'wet':sum(1 for x in di[d] if x.kind in ('wet','mixed')),'stool_diapers':sum(1 for x in di[d] if x.kind in ('stool','mixed')),'diaper_changes':sum(1 for x in di[d] if x.kind!='dry'),
            'stool_entries':len(st[d]),'loose_or_watery':sum(1 for x in st[d] if x.watery or x.consistency in ('watery','loose')),'stool_review_flags':sum(1 for x in st[d] if x.clinical_review_suggested),
            'medicines':meds,'symptoms':{k:sum(1 for x in sy[d] if x.kind==k) for k in {x.kind for x in sy[d]}},'fever_symptom':sum(1 for x in sy[d] if x.kind=='fever'),
            'sleep_minutes':sum((x.duration_minutes or minutes_between(x.at,x.ended_at) or 0) for x in sl[d]),'active_minutes':sum((x.duration_minutes or minutes_between(x.at,x.ended_at) or 0) for x in ac[d] if x.kind!='awake'),'recorded_anything':bool(fl[d] or di[d] or st[d] or me[d] or sy[d] or ac[d] or sl[d])})
    return series,e

def analytics(db,pid,start,end):
    series,e=daily_series(db,pid,start,end);n=len(series);recorded=[s for s in series if s['recorded_anything']]
    stools=e['stool'];colour=defaultdict(int);cons=defaultdict(int)
    for x in stools:
        if x.colour:colour[x.colour]+=1
        if x.consistency:cons[x.consistency]+=1
    adherence=defaultdict(lambda:{'due':0,'given':0,'skipped':0,'missed':0,'later':0,'unknown':0,'days_with_given':set()})
    for x in e['medicine']:
        a=adherence[x.medicine_name.strip().casefold()];a['name']=x.medicine_name;a[x.administration_status]=a.get(x.administration_status,0)+1
        if x.administration_status=='given':a['days_with_given'].add(x.local_date)
    for s in series:
        for slot in medicine_day(db,db.get(Patient,pid),date.fromisoformat(s['date']))['due']:adherence[slot['medicine_name'].strip().casefold()]['due']+=1;adherence[slot['medicine_name'].strip().casefold()].setdefault('name',slot['medicine_name'])
    med_rows=[{'medicine':v.get('name',k),'due_slots':v['due'],'given':v['given'],'skipped':v['skipped'],'missed':v['missed'],'later':v['later'],'unknown':v['unknown'],'days_with_a_given_record':len(v['days_with_given']),'days_in_range':n,'recorded_given_per_due_slot':round(v['given']/v['due'],2) if v['due'] else None} for k,v in adherence.items()]
    symptoms=defaultdict(int)
    for x in e['symptom']:symptoms[x.kind]+=1
    statements=[]
    loose_days=sum(1 for s in series if s['loose_or_watery']);
    if recorded:statements.append(f"Entries were recorded on {len(recorded)} of {n} days in this range.")
    if loose_days:statements.append(f"Loose or watery stool entries were recorded on {loose_days} of {len(recorded) or n} recorded days.")
    wet_days=[s['wet'] for s in recorded if s['diaper_changes']]
    if wet_days:statements.append(f"Wet diapers per recorded day ranged from {min(wet_days)} to {max(wet_days)} (median {sorted(wet_days)[len(wet_days)//2]}).")
    fluid_days=[s['fluid_ml'] for s in recorded if s['fluid_events']]
    if fluid_days:statements.append(f"Recorded fluid volume per day ranged from {min(fluid_days):g} to {max(fluid_days):g} ml, excluding entries with unknown amounts.")
    for r in med_rows:
        if r['due_slots']:statements.append(f"{r['medicine']}: a given record exists for {r['given']} of {r['due_slots']} scheduled slots.")
        elif r['given']:statements.append(f"{r['medicine']}: recorded as given on {r['days_with_a_given_record']} of {n} days (no schedule registered).")
    return {'start':start.isoformat(),'end':end.isoformat(),'days':n,'series':series,'totals':{'fluid_ml':round(sum(s['fluid_ml'] for s in series),1),'fluid_events':sum(s['fluid_events'] for s in series),'wet':sum(s['wet'] for s in series),'stool_diapers':sum(s['stool_diapers'] for s in series),'stool_entries':sum(s['stool_entries'] for s in series),'loose_or_watery_days':loose_days,'sleep_minutes':sum(s['sleep_minutes'] for s in series),'active_minutes':sum(s['active_minutes'] for s in series),'recorded_days':len(recorded)},
        'stool_patterns':{'colour':dict(colour),'consistency':dict(cons),'review_flags':sum(1 for x in stools if x.clinical_review_suggested),'mucus_reported':sum(1 for x in stools if x.mucus_observed),'blood_reported':sum(1 for x in stools if x.blood_observed)},
        'medicine_adherence':med_rows,'symptom_occurrences':dict(symptoms),'statements':statements,
        'warnings':['Observational summaries of what was recorded. They do not show that any medicine, food or event caused a change, and they are not a hydration or illness assessment.']}

# ------------------------------------------------------------------ illness comparison
def illness_window(db,pid,kind,ref_id):
    if kind=='episode':
        ep=db.get(Episode,ref_id)
        if not ep or ep.patient_id!=pid:return None
        return {'title':ep.title,'start':ep.started_on,'end':ep.ended_on or ep.started_on,'kind':'episode','id':ep.id}
    a=db.get(Admission,ref_id)
    if not a or a.patient_id!=pid:return None
    return {'title':f'Hospital stay · {a.facility}','start':a.admitted_on,'end':a.discharged_on or a.admitted_on,'kind':'admission','id':a.id}

def compare_illness(db,pid,kind,ref_id,window_days=7):
    w=illness_window(db,pid,kind,ref_id)
    if not w:return None
    p=db.get(Patient,pid)
    def bucket(label,s,e):
        a=analytics(db,pid,s,e);t=a['totals'];
        meds=[{'medicine':r['medicine'],'given':r['given'],'due_slots':r['due_slots']} for r in a['medicine_adherence']]
        return {'label':label,'start':s.isoformat(),'end':e.isoformat(),'days':a['days'],'recorded_days':t['recorded_days'],'fluid_ml':t['fluid_ml'],'fluid_events':t['fluid_events'],'wet':t['wet'],'stool_entries':t['stool_entries'],'stool_diapers':t['stool_diapers'],'loose_or_watery_days':t['loose_or_watery_days'],'symptoms':a['symptom_occurrences'],'medicines_given':meds,'sleep_minutes':t['sleep_minutes'],'active_minutes':t['active_minutes'],'stool_patterns':a['stool_patterns']}
    before=bucket('before',w['start']-timedelta(days=window_days),w['start']-timedelta(days=1));during=bucket('during',w['start'],w['end']);after=bucket('after',w['end']+timedelta(days=1),w['end']+timedelta(days=window_days))
    fevers=[{'id':f.id,'title':f.title,'first':f.first_documented_on.isoformat() if f.first_documented_on else None} for f in db.query(FeverEpisode).filter(FeverEpisode.patient_id==pid,FeverEpisode.verification_status!='rejected') if f.first_documented_on and w['start']-timedelta(days=window_days)<=f.first_documented_on<=w['end']+timedelta(days=window_days)]
    abx=[{'id':c.id,'generic_name':c.generic_name,'route':c.route,'administration_status':c.administration_status} for c in db.query(AntibioticCourse).filter(AntibioticCourse.patient_id==pid,AntibioticCourse.verification_status!='rejected') if (c.prescribed_on or c.actual_start) and w['start']-timedelta(days=window_days)<=(c.prescribed_on or c.actual_start)<=w['end']+timedelta(days=window_days)]
    labs=[{'id':o.id,'date':o.record_date.isoformat(),'test':o.test_name,'value':o.value,'unit':o.unit,'specimen':o.specimen} for o in db.query(Observation).filter(Observation.patient_id==pid,Observation.record_date>=w['start']-timedelta(days=window_days),Observation.record_date<=w['end']+timedelta(days=window_days)).order_by(Observation.record_date)]
    visits=[{'id':v.id,'at':iso_local(v.planned_at,p),'purpose':v.purpose,'status':v.status} for v in db.query(HospitalVisit).filter(HospitalVisit.patient_id==pid) if w['start']-timedelta(days=window_days)<=local_date_of(v.planned_at,p)<=w['end']+timedelta(days=window_days)]
    statements=[]
    for b in (before,during,after):
        if b['recorded_days']:statements.append(f"{b['label'].capitalize()} ({b['start']} to {b['end']}): journal entries on {b['recorded_days']} of {b['days']} days; loose/watery stool entries on {b['loose_or_watery_days']} day(s); {b['wet']} wet diapers and {b['fluid_ml']:g} ml of recorded fluids.")
        else:statements.append(f"{b['label'].capitalize()} ({b['start']} to {b['end']}): no journal entries recorded.")
    return {'illness':{**w,'start':w['start'].isoformat(),'end':w['end'].isoformat()},'window_days':window_days,'before':before,'during':during,'after':after,'fever_episodes':fevers,'antibiotic_records':abx,'laboratory':labs,'hospital_visits':visits,'statements':statements,
        'warnings':['Before/during/after figures describe what was recorded in each window. Differences are observations, not evidence that any treatment or event caused them.']}

def illness_choices(db,pid):
    out=[{'kind':'episode','id':e.id,'title':e.title,'start':e.started_on.isoformat(),'end':(e.ended_on or e.started_on).isoformat(),'ecoli':'coli' in (e.title+' '+(e.documented_diagnosis or '')).casefold()} for e in db.query(Episode).filter_by(patient_id=pid).order_by(Episode.started_on.desc())]
    out+=[{'kind':'admission','id':a.id,'title':f'Hospital stay · {a.facility}','start':a.admitted_on.isoformat(),'end':(a.discharged_on or a.admitted_on).isoformat(),'ecoli':'coli' in (a.diagnosis or '').casefold()} for a in db.query(Admission).filter_by(patient_id=pid).order_by(Admission.admitted_on.desc())]
    return out

# ------------------------------------------------------------------ doctor export
def care_export(db,pid,start,end,include_photos=False):
    """Plain-language care summary for a date range. Returns summary_text, structured sections and the stool photo ids the parent may attach."""
    p=db.get(Patient,pid);a=analytics(db,pid,start,end);t=a['totals']
    lines=[f'DAILY CARE SUMMARY — {start} to {end} ({a["days"]} days; entries on {t["recorded_days"]} days)','Parent-recorded journal. Counts describe what was recorded, not a clinical assessment.','']
    lines.append(f"FLUIDS: {t['fluid_events']} entries; {t['fluid_ml']:g} ml with known amounts.")
    lines.append(f"DIAPERS: {t['wet']} wet, {t['stool_diapers']} stool diapers.")
    lines.append(f"STOOL ENTRIES: {t['stool_entries']}; loose/watery on {t['loose_or_watery_days']} day(s); colours {a['stool_patterns']['colour'] or 'not recorded'}; consistency {a['stool_patterns']['consistency'] or 'not recorded'}; mucus reported {a['stool_patterns']['mucus_reported']}; blood reported {a['stool_patterns']['blood_reported']}; 'consider clinical review' flags {a['stool_patterns']['review_flags']}.")
    lines.append('MEDICINES RECORDED AS GIVEN: '+('; '.join(f"{r['medicine']} given {r['given']}×"+(f" of {r['due_slots']} scheduled" if r['due_slots'] else '')+(f", skipped {r['skipped']}" if r['skipped'] else '')+(f", missed {r['missed']}" if r['missed'] else '') for r in a['medicine_adherence']) or 'none recorded'))
    lines.append('SYMPTOMS: '+(', '.join(f'{k.replace("_"," ")} ×{v}' for k,v in a['symptom_occurrences'].items()) or 'none recorded'))
    lines.append(f"SLEEP: {t['sleep_minutes']} min recorded; ACTIVITY: {t['active_minutes']} min recorded.")
    sections=[]
    e=entries(db,pid,start,end)
    days=sorted({x.local_date for rows in e.values() for x in rows})
    for d in days:
        tl=timeline(db,pid,d,['all'])['items']
        sections.append({'date':d.isoformat(),'items':[f"{i['time']} {i['title']}"+(f" — {i['detail']}" if i.get('detail') else '') for i in tl]})
    lines.append('');lines.append('DAY BY DAY')
    for s in sections:lines.append(f"- {s['date']}: "+' | '.join(s['items']))
    fevers=[f for f in db.query(FeverEpisode).filter(FeverEpisode.patient_id==pid,FeverEpisode.verification_status!='rejected') if f.first_documented_on and start<=f.first_documented_on<=end]
    if fevers:lines.append('DOCUMENTED FEVER EPISODES IN RANGE: '+'; '.join(f"{f.title} ({f.first_documented_on})" for f in fevers))
    visits=[v for v in db.query(HospitalVisit).filter(HospitalVisit.patient_id==pid) if start<=local_date_of(v.planned_at,p)<=end]
    if visits:lines.append('HOSPITAL EVENTS: '+'; '.join(f"{local_date_of(v.planned_at,p)} {v.purpose} ({v.status})" for v in visits))
    photos=[ph.id for ph in db.query(StoolPhoto).join(StoolEntry,StoolEntry.id==StoolPhoto.stool_entry_id).filter(StoolPhoto.patient_id==pid,StoolPhoto.deleted_at==None,StoolEntry.local_date>=start,StoolEntry.local_date<=end)] if include_photos else []
    lines.append('');lines.append(f"Stool photographs {'attached: '+str(len(photos)) if include_photos else 'not included'}.")
    lines.append(a['warnings'][0])
    return {'summary_text':'\n'.join(lines),'sections':sections,'photo_document_ids':photos,'totals':t,'medicine_adherence':a['medicine_adherence'],'statements':a['statements']}

# ------------------------------------------------------------------ Ask HealthBook AI
CARE_TERMS=re.compile(r'\b(water|fluid|fluids|drink|drank|drinking|ors|wet diaper|wet diapers|diaper|diapers|nappy|nappies|urinat\w*|last stool|stool entries|stool entry|potty|loose stool|loose stools|activity|nap|naps|sleep|slept|did i give|medicines did i|medicine did i|actually give|actually gave|i gave|daily care|care journal|journal)\b',re.I)
def _range_from(q,p):
    today=local_today(p)
    if 'yesterday' in q:d=today-timedelta(days=1);return d,d,'yesterday'
    if 'today' in q:return today,today,'today'
    if 'this week' in q:return today-timedelta(days=today.weekday()),today,'this week'
    if 'last 7 days' in q or 'past week' in q or 'last week' in q:return today-timedelta(days=6),today,'the last 7 days'
    if 'this month' in q:return today.replace(day=1),today,'this month'
    if '30 days' in q or 'this month' in q or 'last month' in q:return today-timedelta(days=29),today,'the last 30 days'
    m=re.search(r'\b(20\d\d-\d\d-\d\d)\b',q)
    if m:d=date.fromisoformat(m[1]);return d,d,m[1]
    return None

def care_answer(db,pid,question):
    q=question.casefold()
    if not CARE_TERMS.search(q):return None
    p=db.get(Patient,pid);rng=_range_from(q,p);parts=[];evidence=[]
    def cite(kind,rid,statement,when):evidence.append({'citation':f'{kind}:{rid}','record_type':kind,'record_id':rid,'statement':statement,'date':when,'verified':True,'source_document_id':None,'source_page':None})
    months={'january':1,'february':2,'march':3,'april':4,'may':5,'june':6,'july':7,'august':8,'september':9,'october':10,'november':11,'december':12,'jan':1,'feb':2,'mar':3,'apr':4,'jun':6,'jul':7,'aug':8,'sep':9,'sept':9,'oct':10,'nov':11,'dec':12}
    month=next((v for k,v in months.items() if re.search(r'\b'+k+r'\b',q)),None)
    illness=None
    if 'illness' in q or 'episode' in q or 'coli' in q or 'hospital' in q:
        choices=illness_choices(db,pid)
        cands=[c for c in choices if (month and date.fromisoformat(c['start']).month==month) or ('coli' in q and c['ecoli'])] or ([choices[0]] if choices and ('illness' in q or 'episode' in q) else [])
        illness=cands[0] if cands else None
    if 'compare' in q and illness:
        c=compare_illness(db,pid,illness['kind'],illness['id'])
        parts.append(f"Daily care around {illness['title']} ({c['illness']['start']} to {c['illness']['end']}):");parts+=c['statements']
        for b in (c['before'],c['during'],c['after']):cite('care_window',0,f"{b['label']}: fluids {b['fluid_ml']:g} ml / {b['fluid_events']} entries; wet diapers {b['wet']}; stool entries {b['stool_entries']} (loose/watery days {b['loose_or_watery_days']}); medicines given {', '.join(f'{m['medicine']} ×{m['given']}' for m in b['medicines_given']) or 'none recorded'}; symptoms {b['symptoms'] or 'none recorded'}",b['start'])
        return _wrap(question,parts,evidence,c['warnings'])
    if ('stool' in q or 'potty' in q) and illness and ('show' in q or 'during' in q or 'list' in q):
        s,e=date.fromisoformat(illness['start']),date.fromisoformat(illness['end']);rows=entries(db,pid,s,e)['stool']
        parts.append(f"Stool entries during {illness['title']} ({s} to {e}): {len(rows)}.")
        for x in rows:cite('stool_entry',x.id,f"{iso_local(x.at,p)}: colour {x.colour or 'not recorded'}, consistency {x.consistency or 'not recorded'}, watery {'yes' if x.watery else 'no'}, mucus {x.mucus_observed}, blood {x.blood_observed}; {'consider clinical review; ' if x.clinical_review_suggested else ''}{x.note}",x.local_date.isoformat())
        return _wrap(question,parts,evidence,['Stool entries are parent observations and image descriptions, not a diagnosis.'])
    if 'last stool' in q or ('when' in q and 'stool' in q):
        last=active(db.query(StoolEntry).filter(StoolEntry.patient_id==pid),StoolEntry).order_by(StoolEntry.at.desc()).first()
        lastd=active(db.query(DiaperEntry).filter(DiaperEntry.patient_id==pid,DiaperEntry.kind.in_(('stool','mixed'))),DiaperEntry).order_by(DiaperEntry.at.desc()).first()
        cands=[(aware(x.at),'stool_entry',x) for x in [last] if x]+[(aware(x.at),'diaper_entry',x) for x in [lastd] if x]
        if cands:
            when,kind,x=max(cands);parts.append(f"Last recorded stool: {iso_local(when,p)} ({'stool entry' if kind=='stool_entry' else 'stool diaper'}), {minutes_between(when,utcnow())//60} hours ago.");cite(kind,x.id,f"{kind.replace('_',' ')} at {iso_local(when,p)}",x.local_date.isoformat())
        else:parts.append('No stool entry or stool diaper has been recorded yet.')
        return _wrap(question,parts,evidence,[])
    if re.search(r'how many days.*(loose|watery)',q) or ('loose' in q and 'days' in q):
        s,e=(rng[0],rng[1]) if rng else (p.dob,local_today(p));series,_=daily_series(db,pid,s,e);days=[x['date'] for x in series if x['loose_or_watery']]
        sym=active(db.query(SymptomEntry).filter(SymptomEntry.patient_id==pid,SymptomEntry.kind=='loose_stool',SymptomEntry.local_date>=s,SymptomEntry.local_date<=e),SymptomEntry).all()
        days=sorted(set(days)|{x.local_date.isoformat() for x in sym})
        parts.append(f"Loose or watery stools were recorded on {len(days)} day(s) between {s} and {e}"+(': '+', '.join(days) if days else '.'))
        return _wrap(question,parts,evidence,['A count of recorded days, not an assessment of illness.'])
    s,e,label=rng if rng else (local_today(p),local_today(p),'today')
    if re.search(r'zinc|vitamin d|calcium|iron|every day|did i give|which medicines|medicines did i',q):
        series,ent=daily_series(db,pid,s,e);name=next((w for w in ('zinc','vitamin d','calcium','iron','meropenem','amikacin','ors') if w in q),None)
        rows=[x for x in ent['medicine'] if not name or name in x.medicine_name.casefold()]
        if 'every day' in q and name:
            days_given=sorted({x.local_date.isoformat() for x in rows if x.administration_status=='given'});alld=[x['date'] for x in series]
            missing=[d for d in alld if d not in days_given]
            parts.append(f"{name.capitalize()} was recorded as given on {len(days_given)} of {len(alld)} days {label}"+(f"; no given record on {', '.join(missing)}." if missing else '; every day has a given record.'))
        else:
            given=[x for x in rows if x.administration_status=='given']
            parts.append(f"Medicines recorded as given {label}: "+(', '.join(f"{x.medicine_name} at {hhmm(x.at,p)}"+(f" ({x.dose} {x.dose_unit})".replace(' )',')') if x.dose else '') for x in given) or 'none')+'. '+(f"Also recorded: {', '.join(f'{x.medicine_name} {x.administration_status} {hhmm(x.at,p)}' for x in rows if x.administration_status!='given')}." if any(x.administration_status!='given' for x in rows) else ''))
        for x in rows:cite('medication_administration',x.id,f"{x.medicine_name}: {x.administration_status} at {iso_local(x.at,p)}{' · '+x.dose+' '+x.dose_unit if x.dose else ''}{' · '+x.note if x.note else ''}",x.local_date.isoformat())
        return _wrap(question,parts,evidence,['Only entries you recorded as given count as given; scheduled doses without a record are shown as no record.'])
    if 'wet' in q or 'diaper' in q or 'nappy' in q or 'urinat' in q:
        series,ent=daily_series(db,pid,s,e);wet=sum(x['wet'] for x in series);changes=sum(x['diaper_changes'] for x in series)
        parts.append(f"Wet diapers recorded {label}: {wet} (total diaper changes {changes}, stool diapers {sum(x['stool_diapers'] for x in series)}).")
        for x in ent['diaper']:cite('diaper_entry',x.id,f"{x.kind} diaper at {iso_local(x.at,p)}{' · '+x.urine_amount if x.urine_amount else ''}",x.local_date.isoformat())
        return _wrap(question,parts,evidence,['Diaper counts are what you recorded; they are not a hydration assessment.'])
    if re.search(r'water|fluid|drink|drank|ors',q):
        series,ent=daily_series(db,pid,s,e);total=sum(x['fluid_ml'] for x in series);unknown=sum(x['fluid_unknown'] for x in series)
        parts.append(f"Fluids recorded {label}: {total:g} ml across {sum(x['fluid_events'] for x in series)} entries"+(f" ({unknown} entries had an unknown amount and are not in the total)" if unknown else '')+'.')
        for x in ent['fluid']:cite('fluid_entry',x.id,f"{hhmm(x.at,p)}: {x.amount_ml if x.amount_ml is not None and not x.amount_unknown else (x.custom_amount or 'unknown amount')}{' ml' if x.amount_ml is not None and not x.amount_unknown else ''} {x.fluid_type}",x.local_date.isoformat())
        return _wrap(question,parts,evidence,['Recorded volumes only; this is not a hydration assessment.'])
    if re.search(r'activity|sleep|slept|nap',q):
        series,ent=daily_series(db,pid,s,e)
        parts.append(f"Activity {label}: {sum(x['active_minutes'] for x in series)} min recorded across {len(ent['activity'])} entries; sleep {sum(x['sleep_minutes'] for x in series)} min across {len(ent['sleep'])} periods.")
        for x in ent['activity']:cite('activity_entry',x.id,f"{hhmm(x.at,p)} {x.kind.replace('_',' ')}{' · '+str(x.duration_minutes or minutes_between(x.at,x.ended_at))+' min' if (x.duration_minutes or x.ended_at) else ''}{' · '+x.source if x.source!='parent' else ''}",x.local_date.isoformat())
        for x in ent['sleep']:cite('sleep_entry',x.id,f"{hhmm(x.at,p)} {x.kind}{' · '+str(x.duration_minutes or minutes_between(x.at,x.ended_at))+' min' if (x.duration_minutes or x.ended_at) else ''}",x.local_date.isoformat())
        return _wrap(question,parts,evidence,['Recorded activity and sleep periods only; not a developmental assessment.'])
    return None

def _wrap(question,parts,evidence,warnings):
    return {'question':question,'mode':'daily_care_structured','answer':'\n\n'.join(parts),'evidence':evidence[:150],'warnings':['Answers come only from entries recorded in Daily Health.']+warnings}
