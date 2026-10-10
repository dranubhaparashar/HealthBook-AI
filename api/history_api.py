"""Read-only longitudinal views over authorized records: dashboard, timeline, medication, vaccination and lab series.

Every value returned here is read from stored records; nothing is inferred, averaged or diagnosed.
"""
import io
import json
import math
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session as DbSession
from .database import get_db
from .config import DATA_DIR
from .crypto import decrypt
from .storage import read_document
from .security import require_user, own_patient, own_resource
from .models import (User, Patient, Document, Medicine, MedicationEvent, HospitalVisit, Vaccine, Observation, Admission,
    TreatmentAdministration, Episode, Growth, ExtractionReview, utcnow)
from .main import as_json
from .reminders import reminder_items

router=APIRouter(prefix='/api')

# ---------------------------------------------------------------- unit and test-name canonicalisation (deterministic)
UNIT_FAMILIES={
    'g/dL':{'g/dl':1,'gm%':1,'gm/dl':1,'g%':1,'gm/dL':1,'g/dL':1},
    '/µL':{'/cumm':1,'/cu.mm':1,'/µl':1,'/ul':1,'cells/cumm':1,'cells/µl':1,'/mm3':1,'/mm³':1,
           'x10^3/ul':1000,'x10^3/µl':1000,'x10^3 /ul':1000,'x10^3 /µl':1000,'x10³/µl':1000,'10^3/µl':1000,'thou/cu.mm':1000,'thou/cumm':1000,'thousand/µl':1000,'k/µl':1000,
           'lakh/cumm':100000,'lakh/cu.mm':100000,'lakhs/cumm':100000},
    'mg/L':{'mg/l':1},
    'µg/g':{'µg/g':1,'ug/g':1,'mg/kg':1,'ug/g or mg/kg':1},
    'ng/mL':{'ng/ml':1},
    'pg/mL':{'pg/ml':1},
    'µg/dL':{'µg/dl':1,'ug/dl':1},
    'mg/dL':{'mg/dl':1},
    'mmol/L':{'mmol/l':1},
    'IU/L':{'iu/l':1,'u/l':1},
    '%':{'%':1},
    'U/mL':{'u/ml':1},
    'uIU/mL':{'uiu/ml':1,'µiu/ml':1},
    'g/dL (protein)':{},
}
TEST_ALIASES=[
    ('Haemoglobin',('haemoglobin','hemoglobin','hb','haemoglobin (hb)','hb (haemoglobin) whole blood')),
    ('WBC (total leukocyte count)',('wbc','total wbc count','tlc','total leukocyte count','total leukocyte count (tlc)','total leucocyte count','tlc (total leukocyte count)','wbc count')),
    ('Platelet count',('platelet count','platelets','plt')),
    ('CRP',('crp','c-reactive protein')),
    ('Fecal calprotectin',('fecal calprotectin','faecal calprotectin','calprotectin')),
    ('RBC count',('rbc count','rbc','red blood cell count')),
    ('Haematocrit',('hct','hct(pcv)','hct/pcv','hematocrit','haematocrit','pcv')),
    ('MCV',('mcv',)),('MCH',('mch',)),('MCHC',('mchc',)),('RDW',('rdw','rdw-cv','rdw-cv')),
    ('Neutrophils %',('neutrophils','neutrophil')),('Lymphocytes %',('lymphocytes','lymphocyte')),
    ('Eosinophils %',('eosinophils','eosinophil')),('Monocytes %',('monocytes','monocyte')),('Basophils %',('basophils','basophil')),
    ('Serum iron',('serum iron','iron serum','iron')),('Ferritin',('ferritin',)),('Vitamin B12',('vitamin b12','b12','vitamin b12 (cyanocobalamin)')),
    ('25-OH Vitamin D',('25-oh vitamin d','vitamin d (25 hydroxy)','vitamin d','25 oh vitamin d','25-hydroxy vitamin d')),
    ('Folate',('folate','folic acid','folate/folic acid serum')),
    ('SGOT (AST)',('sgot','ast','sgot (ast)')),('SGPT (ALT)',('sgpt','alt','sgpt (alt)')),('Alkaline phosphatase',('serum alkaline po4','alkaline phosphatase','alp')),
    ('Total bilirubin',('total bilirubin','bilirubin total','bilirubin total, serum')),('Direct bilirubin',('direct bilirubin','bilirubin direct, serum')),('Indirect bilirubin',('indirect bilirubin','bilirubin indirect, serum')),
    ('Blood urea',('blood urea','urea')),('Creatinine',('sr.creatinine','creatinine','serum creatinine')),('Sodium',('sr.sodium','sodium')),('Potassium',('sr.potassium','potassium')),
    ('Calcium',('sr.calcium','calcium','serum calcium')),('Phosphorus',('sr.phosphorus','phosphorus')),('Ionic calcium',('ionic calcium','ionised calcium','ionized calcium')),
    ('Total protein',('sr.proteins','total protein','proteins')),('Albumin',('sr.albumin','albumin')),('Globulin',('sr.globulin','globulin')),
    ('ESR',('esr',)),('Reticulocyte count',('reticulocyte count','retic count')),('TSH',('tsh','cord tsh')),
    ('Immunoglobulin IgA',('immunoglobulin iga','iga')),('tTG antibody IgA',('ttg antibody iga','ttg iga','anti-ttg iga')),('Gliadin antibody IgA',('gliadin antibody iga','gliadin antibody iga (de-amidated dgp)','dgp iga')),
    ('UIBC',('uibc','unsaturated iron binding capacity (uibc)','unsaturated iron binding capacity')),('TIBC',('tibc','tibc (total iron binding capacity)','total iron binding capacity')),
    ('Transferrin',('transferrin',)),('Transferrin saturation',('transferrin saturation','transferrin saturation %')),
    ('Stool culture',('stool culture','stool culture & sensitivity','stool culture and sensitivity')),('Blood culture',('blood culture','culture blood (bactec alert) aerobic','blood culture aerobic')),
]
_ALIAS_INDEX={alias:canon for canon,aliases in TEST_ALIASES for alias in aliases}

def canonical_test(name):
    key=re.sub(r'\s+',' ',name.strip().casefold())
    return _ALIAS_INDEX.get(key,name.strip())

_UNIT_INDEX={re.sub(r'\s+','',alias):(family,factor) for family,members in UNIT_FAMILIES.items() for alias,factor in members.items()}

def canonical_unit(unit):
    key=re.sub(r'\s+','',(unit or '').casefold()).replace('μ','µ')
    if key in _UNIT_INDEX:return _UNIT_INDEX[key]
    return (unit or '').strip(),1

def numeric(value):
    try:
        v=float(str(value).replace(',','').strip().lstrip('<>'))
        return v if math.isfinite(v) else None
    except ValueError:return None

def doc_index(db,pid):
    return {d.id:d for d in db.query(Document).filter_by(patient_id=pid)}

def src(doc,page=1):
    return {'source_document_id':doc.id if doc else None,'source_page':page or 1,'source_ref':doc.source_ref if doc else None,'source_title':doc.title if doc else None,'source_category':doc.category if doc else None}

def today_for(p):
    return utcnow().astimezone(ZoneInfo(p.tz)).date()

def vaccine_status(v,today):
    if v.source_type=='India_UIP_reference':return 'reference_only'
    if v.given_date:return 'documented_given_verified' if v.confirmed else 'documented_given_pending_verification'
    if v.due_date:
        if v.confirmed:return 'upcoming_confirmed' if v.due_date>=today else 'confirmed_date_passed'
        return 'due_pending_verification' if v.due_date>=today else 'due_date_passed_unverified'
    return 'incomplete_record'

STATUS_LABEL={'documented_given_verified':'Documented given (caregiver verified)','documented_given_pending_verification':'Documented given on card – pending caregiver verification',
    'upcoming_confirmed':'Upcoming – confirmed appointment','confirmed_date_passed':'Confirmed date has passed – ask provider','due_pending_verification':'Due date on card – pending verification',
    'due_date_passed_unverified':'Card due date has passed – unverified; ask provider','reference_only':'Reference schedule only','incomplete_record':'Incomplete record'}

# ---------------------------------------------------------------- dashboard
@router.get('/patients/{pid}/dashboard')
def dashboard(pid:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    p=own_patient(db,user,pid);today=today_for(p);docs=doc_index(db,pid)
    by_cat=defaultdict(int);undated=0;pending_docs=0
    for d in docs.values():
        by_cat[d.category]+=1
        if not d.record_date:undated+=1
        if d.extraction_status!='reviewed':pending_docs+=1
    meds=db.query(Medicine).filter_by(patient_id=pid).all()
    current=[m for m in meds if m.status=='verified' and (not m.end_date or m.end_date>=today) and not m.actual_stop_date]
    vaccines=db.query(Vaccine).filter_by(patient_id=pid).all()
    confirmed_upcoming=sorted([v for v in vaccines if v.confirmed and v.due_date and not v.given_date and v.due_date>=today],key=lambda v:v.due_date)
    pending=[v for v in vaccines if not v.confirmed and v.source_type!='India_UIP_reference']
    pending_due=defaultdict(list)
    for v in pending:
        if v.due_date and not v.given_date:pending_due[v.due_date.isoformat()].append(f'{v.name} {v.dose_label}'.strip())
    visits=db.query(HospitalVisit).filter_by(patient_id=pid).all()
    upcoming_visits=sorted([v for v in visits if v.confirmed and v.status=='planned' and v.planned_at.date()>=today],key=lambda v:v.planned_at)
    obs=db.query(Observation).filter_by(patient_id=pid).order_by(Observation.record_date.desc(),Observation.id.desc()).all()
    recent=[];seen=set();headline=['Haemoglobin','WBC (total leukocyte count)','Platelet count','CRP','Fecal calprotectin']
    def rank(o):
        canon=canonical_test(o.test_name)
        return (not o.verified,o.record_date.toordinal()*-1,headline.index(canon) if canon in headline else len(headline))
    for o in sorted(obs,key=rank):
        key=(o.test_name.casefold(),o.record_date)
        if key in seen or (o.organism=='' and o.culture_findings=='' and numeric(o.value) is None):continue
        seen.add(key);recent.append({**as_json(o),**src(docs.get(o.source_document_id),o.source_page)})
        if len(recent)>=8:break
    episodes=db.query(Episode).filter_by(patient_id=pid).order_by(Episode.started_on.desc()).all()
    admissions=db.query(Admission).filter_by(patient_id=pid).order_by(Admission.admitted_on.desc()).all()
    reviews_pending=pending_docs+sum(1 for m in meds if m.status=='draft')+len(pending)+sum(1 for o in obs if not o.verified)
    def vax(v):return {**as_json(v),**src(docs.get(v.source_document_id),v.source_page),'status':vaccine_status(v,today),'status_label':STATUS_LABEL[vaccine_status(v,today)]}
    return {'date':today.isoformat(),'patient':as_json(p),
        'documents':{'total':len(docs),'by_category':dict(by_cat),'undated':undated,'pending_review':pending_docs},
        'episodes':{'total':len(episodes),'latest':({**as_json(episodes[0]),'links':json.loads(episodes[0].links_json)} if episodes else None)},
        'admissions':{'total':len(admissions),'latest':(as_json(admissions[0]) if admissions else None)},
        'medications':{'courses':len(meds),'verified_current':len(current),'drafts':sum(1 for m in meds if m.status=='draft'),'current':[as_json(m) for m in current],
            'by_type':{k:sum(1 for m in meds if m.course_type==k) for k in ('prescription','discharge_prescription','inpatient_treatment','historical_prescription')}},
        'vaccination':{'next_confirmed':(vax(confirmed_upcoming[0]) if confirmed_upcoming else None),'confirmed_upcoming_count':len(confirmed_upcoming),
            'documented_given':sum(1 for v in vaccines if v.given_date),'verified_given':sum(1 for v in vaccines if v.given_date and v.confirmed),
            'pending_verification':len(pending),'pending_due_dates':[{'due_date':k,'vaccines':sorted(names)} for k,names in sorted(pending_due.items())]},
        'appointments':{'upcoming_confirmed':[as_json(v) for v in upcoming_visits[:5]],'unconfirmed_planned':sum(1 for v in visits if not v.confirmed and v.status=='planned')},
        'labs':{'total':len(obs),'verified':sum(1 for o in obs if o.verified),'unverified':sum(1 for o in obs if not o.verified),'recent':recent},
        'reviews_pending':reviews_pending,
        'reminders_next_30_days':len(reminder_items(db,p,30)),
        'safety':'Counts come from stored records. Pending items are unverified transcriptions and never generate reminders.'}

# ---------------------------------------------------------------- medical timeline grouped into episodes
@router.get('/patients/{pid}/history')
def history(pid:int,include_documents:bool=Query(default=True),user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    p=own_patient(db,user,pid);today=today_for(p);docs=doc_index(db,pid);events=[]
    def add(when,kind,title,detail,record_type,record_id,doc=None,page=1,certainty='documented',end=None,extra=None):
        events.append({'date':when.isoformat() if when else None,'end_date':end.isoformat() if end else None,'kind':kind,'title':title,'detail':detail,'record_type':record_type,'record_id':record_id,
            'certainty':certainty,**src(doc,page),**(extra or {})})
    admissions=db.query(Admission).filter_by(patient_id=pid).all()
    for a in admissions:
        add(a.admitted_on,'admission',f'Hospital admission: {a.facility}',f'Documented diagnosis: {a.diagnosis or "not documented"}. Doctor: {a.doctor or "not documented"}.','admission',a.id,docs.get(a.source_document_id),a.source_page,end=a.discharged_on,extra={'treatment_plan':a.treatment_plan,'discharge_document_id':a.discharge_document_id})
        if a.discharged_on:add(a.discharged_on,'discharge',f'Discharged from {a.facility}',a.treatment_plan[:400],'admission',a.id,docs.get(a.discharge_document_id or a.source_document_id),1)
    meds=db.query(Medicine).filter_by(patient_id=pid).all()
    for m in meds:
        label=m.course_type.replace('_',' ')
        when=m.prescription_date or m.start_date
        detail=f'{m.instructions}'.strip()
        add(when,'medicine_prescribed' if m.course_type!='inpatient_treatment' else 'inpatient_treatment',f'{m.name} ({label})',detail,'medicine',m.id,docs.get(m.source_document_id),m.source_page,
            certainty='unverified_draft' if m.status=='draft' else 'verified',extra={'status':m.status,'planned_start':m.start_date.isoformat() if m.start_date else None,'planned_end':m.end_date.isoformat() if m.end_date else None,
            'actual_start':m.actual_start_date.isoformat() if m.actual_start_date else None,'actual_stop':m.actual_stop_date.isoformat() if m.actual_stop_date else None,'course_type':m.course_type})
        if m.actual_stop_date:add(m.actual_stop_date,'medicine_stopped',f'{m.name} stopped','Caregiver recorded stop date.','medicine',m.id,docs.get(m.source_document_id),m.source_page,certainty='caregiver_report')
        for e in db.query(MedicationEvent).filter_by(medicine_id=m.id):
            add(e.occurred_at.date(),'medication_'+e.event_type,f'{m.name}: {e.event_type}',f'{e.source}; {e.note}','medication_event',e.id,docs.get(e.source_document_id or m.source_document_id),e.source_page,certainty='caregiver_report' if e.source=='caregiver_report' else 'documented')
    for v in db.query(Vaccine).filter_by(patient_id=pid):
        st=vaccine_status(v,today)
        if v.given_date:add(v.given_date,'vaccine_given',f'Vaccine: {v.name} {v.dose_label}'.strip(),STATUS_LABEL[st],'vaccine',v.id,docs.get(v.source_document_id),v.source_page,certainty='verified' if v.confirmed else 'unverified_transcription',extra={'status':st})
        elif v.due_date and v.source_type!='India_UIP_reference':add(v.due_date,'vaccine_due',f'Vaccine due on card: {v.name} {v.dose_label}'.strip(),STATUS_LABEL[st],'vaccine',v.id,docs.get(v.source_document_id),v.source_page,certainty='confirmed' if v.confirmed else 'unverified_transcription',extra={'status':st})
    # Laboratory results grouped per document and date so one report is one timeline entry.
    groups=defaultdict(list)
    for o in db.query(Observation).filter_by(patient_id=pid).order_by(Observation.record_date,Observation.id):groups[(o.record_date,o.source_document_id)].append(o)
    for (when,did),rows in groups.items():
        d=docs.get(did);cultures=[o for o in rows if o.organism or o.culture_findings]
        title=d.title if d else 'Laboratory results'
        if cultures:
            title=f'Culture: {cultures[0].test_name}'
        summary='; '.join(f'{o.test_name} {o.value} {o.unit}'.strip() for o in rows[:8])+(' …' if len(rows)>8 else '')
        add(when,'culture' if cultures else 'laboratory',title,summary,'observation_group',rows[0].id,d,rows[0].source_page,certainty='verified' if all(o.verified for o in rows) else 'unverified_transcription',
            extra={'results':[{**as_json(o)} for o in rows],'organism':cultures[0].organism if cultures else '','culture_findings':cultures[0].culture_findings if cultures else '','susceptibility':cultures[0].susceptibility if cultures else ''})
    for v in db.query(HospitalVisit).filter_by(patient_id=pid):
        add(v.planned_at.date(),'visit_'+v.kind,f'{v.kind.replace("_"," ").title()}: {v.purpose}',f'{v.facility}; {v.status}; {"confirmed" if v.confirmed else "unconfirmed"}','visit',v.id,docs.get(v.source_document_id),v.source_page,certainty='confirmed' if v.confirmed else 'unconfirmed')
    for t in db.query(TreatmentAdministration).filter_by(patient_id=pid):
        add(t.occurred_at.date(),'documented_administration',f'Administered: {t.medication}',f'Dose {t.recorded_dose or "not recorded"}; route {t.recorded_route or "not recorded"}','treatment_administration',t.id,docs.get(t.source_document_id),t.source_page)
    for g in db.query(Growth).filter_by(patient_id=pid):
        add(g.record_date,'growth',g.milestone or 'Growth measurement',' · '.join(x for x in [f'Weight {g.weight_kg} kg' if g.weight_kg else '',f'Length {g.length_cm} cm' if g.length_cm else '',f'Head {g.head_cm} cm' if g.head_cm else ''] if x)+(f' · {g.note}' if g.note else ''),'growth',g.id,docs.get(g.source_document_id),g.source_page,certainty='documented' if g.source_document_id else 'caregiver_report')
    linked_doc_ids={e['source_document_id'] for e in events if e['source_document_id']}
    undated=[]
    if include_documents:
        for d in docs.values():
            if d.record_date:
                if d.id in linked_doc_ids and d.category in ('lab','culture'):continue  # already represented by its results
                add(d.record_date,'document',d.title,f'{d.category.replace("_"," ")} · {d.notes}'.strip(' ·'),'document',d.id,d,1,certainty='document_date' if not d.notes.lower().startswith('date inferred') else 'date_inferred',extra={'category':d.category})
            else:undated.append({**as_json(d),'source_ref':d.source_ref})
    episodes=[]
    for e in db.query(Episode).filter_by(patient_id=pid).order_by(Episode.started_on):
        links=json.loads(e.links_json);link_keys={(l['kind'],l['id']) for l in links}
        linked_docs={l['id'] for l in links if l['kind']=='document'}
        episodes.append({**as_json(e),'links':links,'_keys':link_keys,'_docs':linked_docs,'events':[],**src(docs.get(e.source_document_id),e.source_page)})
    kind_map={'medicine':'medicine','observation_group':'observation','admission':'admission','visit':'visit','document':'document'}
    unassigned=[]
    for ev in sorted(events,key=lambda x:(x['date'] or '9999',x['kind'])):
        placed=False
        for ep in episodes:
            key=(kind_map.get(ev['record_type']),ev['record_id'])
            by_link=key in ep['_keys'] or (ev['source_document_id'] in ep['_docs']) or (ev['record_type']=='observation_group' and any(('observation',r['id']) in ep['_keys'] for r in ev.get('results',[])))
            if by_link:
                ep['events'].append({**ev,'association':'explicit_link'});placed=True;break
        if not placed:
            for ep in episodes:
                start=date.fromisoformat(ep['started_on']);end=date.fromisoformat(ep['ended_on']) if ep['ended_on'] else start
                if ev['date'] and start<=date.fromisoformat(ev['date'])<=end and ev['kind'] not in ('vaccine_given','vaccine_due','growth','document'):
                    ep['events'].append({**ev,'association':'within_episode_dates'});placed=True;break
        if not placed:unassigned.append(ev)
    for ep in episodes:
        ep.pop('_keys');ep.pop('_docs');ep['events'].sort(key=lambda x:(x['date'] or '9999'))
    dated=sorted({e['date'] for e in events if e['date']})
    gaps=[]
    for a,b in zip(dated,dated[1:]):
        delta=(date.fromisoformat(b)-date.fromisoformat(a)).days
        if delta>=60:gaps.append({'from':a,'to':b,'days':delta,'note':'No dated record supplied for this interval; absence of records is not evidence of health or illness.'})
    return {'patient':as_json(p),'birth':p.dob.isoformat(),'episodes':episodes,'events':unassigned,'all_events_count':len(events),'undated_documents':undated,'gaps':gaps,
        'warnings':['Episodes group records by explicit caregiver links or by date range only; a shared organism name across episodes does not establish the same infection or reinfection.',
                    'Prescription entries are what was written; administration is recorded separately and may be absent.']}

# ---------------------------------------------------------------- medication history
ANTIBIOTIC_TERMS=('meropenem','amikacin','piperacillin','pipzo','tazobactam','cefixime','cefpodoxime','cefpodox','ceftriaxone','cefotaxime','azithromycin','amoxicillin','amoxyclav','augmentin','ciprofloxacin','ofloxacin','levofloxacin','gentamicin','co-trimoxazole','cotrimoxazole','nitrofurantoin','metronidazole','omnix','ciprodac','taxim','zifi','cefoperazone','colistin','tobramycin','moxifloxacin')

def missing_info(m,events):
    gaps=[]
    if not m.prescription_date and not m.start_date:gaps.append('prescription date not documented')
    if not m.actual_start_date:gaps.append('actual start date unknown')
    if not m.actual_stop_date and m.status!='discontinued':gaps.append('actual end date unknown')
    if not any(e.event_type=='given' for e in events) and m.course_type!='inpatient_treatment':gaps.append('no administration events recorded')
    if m.course_type=='inpatient_treatment':gaps.append('inpatient dates and times not in supplied records; hospital administration chart needed')
    if m.status=='draft':gaps.append('prescription not yet verified by caregiver against the original')
    if not m.source_document_id:gaps.append('no source document attached')
    return gaps

@router.get('/patients/{pid}/medication-history')
def medication_history(pid:int,q:str=Query(default='',max_length=120),user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid);docs=doc_index(db,pid);key=q.strip().casefold();courses=[]
    for m in db.query(Medicine).filter_by(patient_id=pid).all():
        hay=' '.join([m.name,m.generic_name,m.brand_name,m.instructions,m.indication,m.course_type]).casefold()
        if key and key not in hay:continue
        events=db.query(MedicationEvent).filter_by(medicine_id=m.id).order_by(MedicationEvent.occurred_at).all()
        d=docs.get(m.source_document_id)
        courses.append({**as_json(m),**src(d,m.source_page),'events':[as_json(e) for e in events],'missing':missing_info(m,events),
            'antibiotic_list_match':any(t in hay for t in ANTIBIOTIC_TERMS),
            'display_period':{'prescribed':(m.prescription_date or None) and m.prescription_date.isoformat(),'planned_start':m.start_date.isoformat() if m.start_date else None,'planned_end':m.end_date.isoformat() if m.end_date else None,
                'actual_start':m.actual_start_date.isoformat() if m.actual_start_date else None,'actual_stop':m.actual_stop_date.isoformat() if m.actual_stop_date else None}})
    courses.sort(key=lambda c:(c['prescription_date'] or c['start_date'] or c['created_at'] or ''))
    groups=defaultdict(list)
    for c in courses:groups[re.sub(r'[^a-z0-9]+',' ',(c['generic_name'] or c['name']).casefold()).strip()].append(c['id'])
    return {'query':q,'courses':courses,'groups':[{'name':k,'course_ids':v} for k,v in groups.items()],
        'summary':{'total':len(courses),'verified':sum(1 for c in courses if c['status']=='verified'),'drafts':sum(1 for c in courses if c['status']=='draft'),
            'with_administration_events':sum(1 for c in courses if any(e['event_type']=='given' for e in c['events']))},
        'warnings':['A prescription proves what was written, not that doses were taken. "Antibiotic list match" means the name matched a fixed word list; it is not a clinical classification.',
                    'No dose, frequency or schedule is derived from handwriting; unknown dates stay unknown.']}

# ---------------------------------------------------------------- vaccination chart
@router.get('/patients/{pid}/vaccination-chart')
def vaccination_chart(pid:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    p=own_patient(db,user,pid);today=today_for(p);docs=doc_index(db,pid)
    reminders=defaultdict(list)
    for item in reminder_items(db,p,365):
        if item['kind']=='vaccine':reminders[item['item_id']].append(item['at'])
    rows=[]
    for v in db.query(Vaccine).filter_by(patient_id=pid):
        st=vaccine_status(v,today);anchor=v.given_date or v.due_date
        age_days=(anchor-p.dob).days if anchor else None
        rows.append({**as_json(v),**src(docs.get(v.source_document_id),v.source_page),'status':st,'status_label':STATUS_LABEL[st],'reminders_scheduled':reminders.get(v.id,[]),
            'age_at_date':(f'{age_days//30} months' if age_days is not None and age_days>=60 else f'{age_days} days' if age_days is not None else None),'anchor_date':anchor.isoformat() if anchor else None})
    rows.sort(key=lambda r:(r['anchor_date'] or '9999',r['name']))
    by_status=defaultdict(int)
    for r in rows:by_status[r['status']]+=1
    upcoming=sorted([r for r in rows if r['status']=='upcoming_confirmed'],key=lambda r:r['due_date'])
    pending=defaultdict(list)
    for r in rows:
        if r['status'] in ('due_pending_verification','due_date_passed_unverified'):pending[r['due_date']].append(r)
    return {'rows':rows,'by_status':dict(by_status),'next_confirmed':upcoming[0] if upcoming else None,
        'pending_due_dates':[{'due_date':k,'rows':v} for k,v in sorted(pending.items())],
        'reminder_policy':'Confirmed future appointments schedule reminders at the configured lead days (default 7, 1 and 0 days before, 09:00 child time). Unverified card entries never schedule reminders.',
        'warnings':['Given dates transcribed from the card are shown as documented but remain unverified until the caregiver confirms each against the original.',
                    'A due date is an appointment written on the card, never an administration record.']}

# ---------------------------------------------------------------- laboratory series with deterministic unit canonicalisation
@router.get('/patients/{pid}/lab-series')
def lab_series(pid:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    own_patient(db,user,pid);docs=doc_index(db,pid);series={}
    rows=db.query(Observation).filter_by(patient_id=pid).order_by(Observation.record_date,Observation.id).all()
    for o in rows:
        canon=canonical_test(o.test_name);unit,factor=canonical_unit(o.unit);specimen=(o.specimen or '').strip().casefold()
        value=numeric(o.value);converted=value*factor if value is not None else None
        key=(canon,unit,specimen)
        group=series.setdefault(key,{'test_name':canon,'unit':unit,'specimen':o.specimen,'points':[],'original_units':set(),'original_names':set()})
        group['original_units'].add(o.unit);group['original_names'].add(o.test_name)
        d=docs.get(o.source_document_id)
        group['points'].append({**as_json(o),**src(d,o.source_page),'numeric_value':converted,'original_value':o.value,'original_unit':o.unit,'conversion_factor':factor,
            'plot_eligible':converted is not None and o.verified and bool(unit),'is_culture':bool(o.organism or o.culture_findings)})
    out=[]
    for g in series.values():
        dates=defaultdict(list)
        for pt in g['points']:dates[pt['record_date']].append(pt['id'])
        for pt in g['points']:
            others=[i for i in dates[pt['record_date']] if i!=pt['id']]
            pt['same_day_separate_reports']=others
        g['original_units']=sorted(g['original_units']);g['original_names']=sorted(g['original_names'])
        g['unit_conversions_applied']=any(pt['conversion_factor']!=1 for pt in g['points'])
        g['plot_points']=sum(1 for pt in g['points'] if pt['plot_eligible'])
        out.append(g)
    priority=['Haemoglobin','WBC (total leukocyte count)','Platelet count','CRP','Fecal calprotectin']
    out.sort(key=lambda g:(priority.index(g['test_name']) if g['test_name'] in priority else 99,-g['plot_points'],g['test_name']))
    return {'series':out,'warnings':['Only exact unit-family conversions are applied (for example x10^3/µL × 1000 = /µL). Different laboratories on the same date stay separate points; nothing is averaged.',
        'Printed reference intervals are shown as transcribed from each report and may be adult or laboratory-specific; no pediatric interval is supplied by the app.']}

# ---------------------------------------------------------------- document thumbnails and back-links
@router.get('/documents/{did}/thumbnail')
def document_thumbnail(did:int,w:int=Query(default=360,ge=80,le=800),user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    d=own_resource(db,user,Document,did)
    raw=decrypt(read_document(d.storage_name))
    if d.mime=='application/pdf':
        import fitz
        with fitz.open(stream=raw,filetype='pdf') as pdf:
            page=pdf[0];scale=w/max(page.rect.width,1)
            content=page.get_pixmap(matrix=fitz.Matrix(scale,scale)).tobytes('png')
        return Response(content,media_type='image/png')
    from PIL import Image
    from .extract import heif_support
    heif_support()
    with Image.open(io.BytesIO(raw)) as image:
        image.draft('RGB',(w*2,w*2))
        image.thumbnail((w,int(w*1.6)))
        buf=io.BytesIO();image.convert('RGB').save(buf,format='JPEG',quality=72)
    return Response(buf.getvalue(),media_type='image/jpeg')

@router.get('/documents/{did}/links')
def document_links(did:int,user:User=Depends(require_user),db:DbSession=Depends(get_db)):
    d=own_resource(db,user,Document,did);pid=d.patient_id
    out={'document':as_json(d),'medicines':[as_json(m) for m in db.query(Medicine).filter_by(patient_id=pid,source_document_id=did)],
        'vaccines':[as_json(v) for v in db.query(Vaccine).filter_by(patient_id=pid,source_document_id=did)],
        'observations':[as_json(o) for o in db.query(Observation).filter_by(patient_id=pid,source_document_id=did).order_by(Observation.record_date)],
        'admissions':[as_json(a) for a in db.query(Admission).filter_by(patient_id=pid).filter((Admission.source_document_id==did)|(Admission.discharge_document_id==did))],
        'visits':[as_json(v) for v in db.query(HospitalVisit).filter_by(patient_id=pid,source_document_id=did)],
        'administrations':[as_json(t) for t in db.query(TreatmentAdministration).filter_by(patient_id=pid,source_document_id=did)],
        'growth':[as_json(g) for g in db.query(Growth).filter_by(patient_id=pid,source_document_id=did)],
        'episodes':[{**as_json(e),'links':json.loads(e.links_json)} for e in db.query(Episode).filter_by(patient_id=pid) if e.source_document_id==did or any(l['kind']=='document' and l['id']==did for l in json.loads(e.links_json))]}
    review=db.query(ExtractionReview).filter_by(document_id=did).first()
    out['review']={'status':review.status,'items':len(json.loads(review.payload).get('items',[]))} if review else None
    out['total_links']=sum(len(v) for k,v in out.items() if isinstance(v,list))
    return out
