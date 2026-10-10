"""Vitals journal engine: validation, day series, observational summaries and the Ask hook. Values are reported exactly
as recorded; nothing here interprets a reading or suggests a diagnosis."""
import re
from collections import defaultdict
from datetime import date, timedelta
from fastapi import HTTPException
from .models import Patient, utcnow
from .vitals_models import VitalsEntry, FIELDS
from .care import parse_when, local_date_of, local_today, active, tz_of

RANGES={'systolic':(50,260,'mmHg'),'diastolic':(30,160,'mmHg'),'heart_rate':(20,250,'bpm'),'spo2':(50,100,'%'),'glucose_mg_dl':(20,700,'mg/dL'),'weight_kg':(0.5,400,'kg'),'temperature_c':(30.0,45.0,'°C')}
GLUCOSE_CONTEXTS=('','fasting','post_meal','random','bedtime')
LABELS={'systolic':'Systolic','diastolic':'Diastolic','heart_rate':'Heart rate','spo2':'SpO2','glucose_mg_dl':'Glucose','weight_kg':'Weight','temperature_c':'Temperature'}

def validate(payload):
    out={}
    for k,(lo,hi,unit) in RANGES.items():
        v=payload.get(k)
        if v is None or v=='':continue
        try:v=float(v)
        except (TypeError,ValueError):raise HTTPException(422,f'{LABELS[k]} must be a number')
        if not lo<=v<=hi:raise HTTPException(422,f'{LABELS[k]} must be between {lo:g} and {hi:g} {unit}')
        out[k]=int(v) if k in ('systolic','diastolic','heart_rate','spo2') else round(v,2)
    if ('systolic' in out)!=('diastolic' in out):raise HTTPException(422,'Blood pressure needs both systolic and diastolic values')
    if 'systolic' in out and out['systolic']<=out['diastolic']:raise HTTPException(422,'Systolic must be higher than diastolic')
    ctx=(payload.get('glucose_context') or '').strip()
    if ctx not in GLUCOSE_CONTEXTS:raise HTTPException(422,'Unknown glucose context')
    out['glucose_context']=ctx if 'glucose_mg_dl' in out else ''
    out['meal']=(payload.get('meal') or '').strip()[:300];out['note']=(payload.get('note') or '').strip()[:500]
    if not any(k in out for k in RANGES) and not out['meal'] and not out['note']:raise HTTPException(422,'Enter at least one value, a meal or a note')
    return out

def add_entry(db,p,user,payload):
    vals=validate(payload)
    key=(payload.get('client_key') or '').strip()[:80] or None
    if key:
        dup=db.query(VitalsEntry).filter_by(patient_id=p.id,client_key=key).first()
        if dup:return dup,False
    when=parse_when(payload.get('at'),p)
    if when>utcnow()+timedelta(minutes=5):raise HTTPException(422,'Entries cannot be in the future')
    if when.date()<p.dob-timedelta(days=1):raise HTTPException(422,'Entries cannot be before birth')
    row=VitalsEntry(patient_id=p.id,recorded_by=user.id,at=when,local_date=local_date_of(when,p),client_key=key,source='parent',**vals)
    db.add(row);db.flush();return row,True

def entry_json(x,p):
    return {'id':x.id,'at':x.at.astimezone(tz_of(p)).isoformat(),'local_date':x.local_date.isoformat(),'systolic':x.systolic,'diastolic':x.diastolic,'heart_rate':x.heart_rate,'spo2':x.spo2,'glucose_mg_dl':x.glucose_mg_dl,'glucose_context':x.glucose_context or '','weight_kg':x.weight_kg,'temperature_c':x.temperature_c,'meal':x.meal or '','note':x.note or '','source':x.source,'status':x.status,'version':x.version,'summary':summary_line(x)}
def summary_line(x):
    parts=[]
    if x.systolic:parts.append(f'BP {x.systolic}/{x.diastolic} mmHg')
    if x.heart_rate:parts.append(f'HR {x.heart_rate} bpm')
    if x.spo2:parts.append(f'SpO2 {x.spo2}%')
    if x.glucose_mg_dl is not None:parts.append(f"glucose {x.glucose_mg_dl:g} mg/dL"+(f" ({x.glucose_context.replace('_',' ')})" if x.glucose_context else ''))
    if x.weight_kg is not None:parts.append(f'weight {x.weight_kg:g} kg')
    if x.temperature_c is not None:parts.append(f'temperature {x.temperature_c:g} °C')
    if x.meal:parts.append(f'meal: {x.meal}')
    return ' · '.join(parts) or (x.note[:60] if x.note else 'note')

def entries(db,pid,start,end):
    return active(db.query(VitalsEntry).filter(VitalsEntry.patient_id==pid,VitalsEntry.local_date>=start,VitalsEntry.local_date<=end),VitalsEntry).order_by(VitalsEntry.at).all()

def series(rows,start,end):
    """Per-day aggregation: every reading kept (min/max/last), never averaged away."""
    by=defaultdict(list)
    for r in rows:by[r.local_date].append(r)
    out=[];d=start
    while d<=end:
        rs=by.get(d,[]);day={'date':d.isoformat(),'count':len(rs)}
        for k in RANGES:
            vals=[getattr(r,k) for r in rs if getattr(r,k) is not None]
            day[k]={'min':min(vals),'max':max(vals),'last':vals[-1],'n':len(vals)} if vals else None
        day['meals']=sum(1 for r in rs if r.meal)
        out.append(day);d+=timedelta(days=1)
    return out

def statements(rows,start,end):
    days=(end-start).days+1;s=[]
    def span(k,fmt):
        vals=[getattr(r,k) for r in rows if getattr(r,k) is not None]
        if not vals:return None
        ds=len({r.local_date for r in rows if getattr(r,k) is not None})
        return f"{LABELS[k]} recorded on {ds} of {days} days ({len(vals)} reading(s)); lowest {fmt(min(vals))}, highest {fmt(max(vals))}, latest {fmt(vals[-1])}."
    bp=[r for r in rows if r.systolic]
    if bp:
        ds=len({r.local_date for r in bp});s.append(f"Blood pressure recorded on {ds} of {days} days ({len(bp)} reading(s)); systolic {min(r.systolic for r in bp)}–{max(r.systolic for r in bp)} mmHg, diastolic {min(r.diastolic for r in bp)}–{max(r.diastolic for r in bp)} mmHg; latest {bp[-1].systolic}/{bp[-1].diastolic} mmHg.")
    for k,fmt in (('heart_rate',lambda v:f'{v} bpm'),('spo2',lambda v:f'{v}%'),('glucose_mg_dl',lambda v:f'{v:g} mg/dL'),('weight_kg',lambda v:f'{v:g} kg'),('temperature_c',lambda v:f'{v:g} °C')):
        t=span(k,fmt)
        if t:s.append(t)
    meals=[r for r in rows if r.meal]
    if meals:s.append(f"{len(meals)} meal note(s) recorded.")
    return s

def summary(db,p,start,end):
    rows=entries(db,p.id,start,end)
    return {'start':start.isoformat(),'end':end.isoformat(),'entries':[entry_json(r,p) for r in rows],'series':series(rows,start,end),'statements':statements(rows,start,end),'latest':entry_json(rows[-1],p) if rows else None,'warnings':['Values are shown exactly as recorded. HealthBook AI does not interpret vitals; discuss readings with the treating clinician.']}

VITALS_TERMS=re.compile(r'\b(blood pressure|bp|systolic|diastolic|heart rate|pulse|spo2|oxygen saturation|oxygen|glucose|blood sugar|sugar level|sugar|weigh|weight|vitals?|meals?|what did i eat|ate)\b',re.I)
def vitals_answer(db,pid,question):
    q=question.casefold()
    if not VITALS_TERMS.search(q):return None
    p=db.get(Patient,pid);today=local_today(p)
    days=1 if re.search(r'\btoday\b',q) else 7 if re.search(r'\b(week|7 days|last seven)\b',q) else 30 if re.search(r'\b(month|30 days)\b',q) else 90 if re.search(r'\b(3 months|90 days|quarter)\b',q) else 30
    if re.search(r'\byesterday\b',q):start=end=today-timedelta(days=1)
    else:end=today;start=end-timedelta(days=days-1)
    rows=entries(db,pid,start,end)
    if not rows:return None  # let the record-grounded answerers try (growth weight, lab glucose, documents)
    want=[k for k,pat in (('systolic',r'blood pressure|\bbp\b|systolic|diastolic'),('heart_rate',r'heart rate|pulse'),('spo2',r'spo2|oxygen'),('glucose_mg_dl',r'glucose|sugar'),('weight_kg',r'weigh'),('temperature_c',r'temperature'),('meal',r'meal|eat|ate')) if re.search(pat,q)] or list(RANGES)+['meal']
    picked=[r for r in rows if any(getattr(r,k) not in (None,'') for k in want)]
    if not picked:return None
    label=f"{start.isoformat()}" if start==end else f"{start.isoformat()} to {end.isoformat()}"
    parts=[f"Vitals recorded for {p.name} ({label}): {len(picked)} entr{'y' if len(picked)==1 else 'ies'}."]
    st=[s for s in statements(picked,start,end) if any(LABELS[k].split()[0].casefold() in s.casefold() or (k=='meal' and 'meal' in s.casefold()) or (k=='systolic' and 'blood pressure' in s.casefold()) for k in want)]
    parts+=st or statements(picked,start,end)
    evidence=[{'citation':f'vitals:{r.id}','record_type':'vitals','record_id':r.id,'statement':f"{r.at.astimezone(tz_of(p)).strftime('%d %b %Y %H:%M')} · {summary_line(r)}",'date':r.local_date.isoformat(),'verified':True,'source_document_id':None,'source_page':None} for r in picked[-40:]]
    return {'question':question,'mode':'daily_care_structured','answer':'\n\n'.join(parts),'evidence':evidence,'warnings':['Answers come only from vitals entries recorded in Daily Health; no reading is interpreted.']}

def export_rows(db,p,start,end):
    rows=entries(db,p.id,start,end)
    return {'start':start.isoformat(),'end':end.isoformat(),'rows':[entry_json(r,p) for r in rows],'series':series(rows,start,end),'statements':statements(rows,start,end)} if rows else None
