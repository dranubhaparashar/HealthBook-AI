"""MedicalExportModel: one structured source of truth for every record export (JSON, TXT, premium PDF, appendix).

Selection comes from api.share.resolve() (documented links only). This module arranges the selected records into
presentation-ready sections with provenance labels, source references ("Source R57 · p.1" = document id 57) and the
ordered list of original documents to append. Nothing is interpreted: statuses are copied from records, prescriptions
never become administrations, culture panels never become medicines.
"""
import json
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from .models import Patient, Document, Medicine, MedicationEvent, HospitalVisit, Vaccine, Observation, Admission, TreatmentAdministration, Episode, Growth, utcnow
from .lifetime_models import FeverEpisode, AntibioticCourse, AntibioticAdministration
from . import share, lifetime
from .history_api import canonical_test, canonical_unit, numeric, vaccine_status, STATUS_LABEL

EXPORT_TYPES={
 'complete':('Complete Medical Record','Medical_Record','Every stored document and record.'),
 'ecoli':('E. coli History','Ecoli_History','Culture reports naming E. coli, the linked illness episodes, hospitalization, prescriptions, antibiotic courses and laboratory reports.'),
 'latest_hospitalization':('Latest Hospitalization','Latest_Hospitalization','The most recent admission with its discharge summary and every record dated inside the stay.'),
 'antibiotics':('Antibiotic History','Antibiotic_History','Every documented antibiotic course and prescription with sources and administration records.'),
 'labs':('Laboratory History','Laboratory_History','All laboratory and culture reports with transcribed values and trends.'),
 'vaccination':('Vaccination Record','Vaccination_Record','All vaccination card entries with status and card pages.'),
 'daily_care_7d':('Daily Health — last 7 days','Daily_Health_7_days','Journal of the last seven days: fluids, stool, medicines actually given, symptoms, sleep, activity and vitals.'),
 'custom':('Custom Selection','Custom_Selection','Sections, categories and date range chosen by the parent.'),
}
ALL_SECTIONS=('summary','timeline','illnesses','hospitalizations','medicines','antibiotics','labs','cultures','vaccinations','growth','daily_care','sources')
LAB_CATEGORIES=[
 ('Haematology',('Haemoglobin','WBC (total leukocyte count)','Platelet count','RBC count','HCT (PCV)','MCV','MCH','MCHC','RDW','Neutrophils','Lymphocytes','Eosinophils','Monocytes','Basophils','Absolute neutrophil count','Absolute lymphocyte count','MPV','PDW','PCT','Reticulocyte count','ESR','Peripheral smear')),
 ('Inflammation',('CRP','Procalcitonin','Fecal calprotectin')),
 ('Stool',('Stool routine','Stool colour','Stool consistency','Stool reaction','Stool mucus','Stool pus cells','Stool RBCs','Stool parasites','Stool pH','Stool reducing sugar','Stool bacteria','Stool epithelial cells','Stool starch cells','Stool others','Stool occult blood')),
 ('Liver',('Total bilirubin','Direct bilirubin','Indirect bilirubin','SGOT','SGPT','Alkaline phosphatase','Total protein','Albumin','Globulin','A/G ratio')),
 ('Kidney / electrolytes',('Blood urea','Creatinine','Sodium','Potassium','Calcium','Phosphorus','Ionic calcium')),
 ('Nutrition',('Vitamin D (25-OH)','Vitamin B12','Folate','Ferritin','Serum iron','UIBC','TIBC','Transferrin','Transferrin saturation')),
 ('Thyroid',('TSH',)),
 ('Immunology / serology',('Immunoglobulin IgA','tTG antibody IgA','Gliadin antibody IgA','Typhoid IgG','Typhoid IgM','Blood group','DCT')),
 ('Microbiology',('Stool culture','Blood culture','Urine culture')),
]
def lab_category(canon,o):
    if o.organism or o.culture_findings or o.susceptibility or 'culture' in canon.casefold():return 'Microbiology'
    low=canon.casefold()
    for cat,names in LAB_CATEGORIES:
        if canon in names:return cat
    if low.startswith('stool'):return 'Stool'
    if any(k in low for k in ('haemo','hemo','wbc','leuko','platelet','rbc','mcv','mch','rdw','neutro','lympho','eosino','mono','baso','hct','pcv','smear','retic','esr','mpv','pdw')):return 'Haematology'
    if any(k in low for k in ('crp','calprotectin','procalcitonin')):return 'Inflammation'
    if any(k in low for k in ('bilirubin','sgot','sgpt','alkaline','protein','albumin','globulin')):return 'Liver'
    if any(k in low for k in ('urea','creatinine','sodium','potassium','calcium','phosph')):return 'Kidney / electrolytes'
    if any(k in low for k in ('vitamin','b12','folate','ferritin','iron','tibc','uibc','transferrin')):return 'Nutrition'
    if 'tsh' in low or 'thyro' in low:return 'Thyroid'
    if any(k in low for k in ('iga','igg','igm','typhoid','antibod','blood group','dct')):return 'Immunology / serology'
    return 'Other'

MED_STATUS={'verified':'PRESCRIBED','draft':'PRESCRIBED (unverified transcription)','discontinued':'STOPPED'}

def fmt_d(d):return d.strftime('%d %b %Y') if d else '—'
def slug(text):return re.sub(r'[^A-Za-z0-9]+','_',str(text or '')).strip('_')

def age_text(dob,on):return share.age_text(dob,on)

# ------------------------------------------------------------------ spec translation
def spec_for(preset,custom=None):
    """Translate an export request into the share module's selection spec plus the presentation options."""
    custom=custom or {}
    sections=set(ALL_SECTIONS)
    include_sources=custom.get('include_sources',True)
    photos=bool(custom.get('include_photos',False))
    care_days=None
    if preset=='complete':spec=share.ShareSpec(mode='preset',preset='complete')
    elif preset=='ecoli':spec=share.ShareSpec(mode='preset',preset='ecoli');sections-={'growth'}
    elif preset=='latest_hospitalization':spec=share.ShareSpec(mode='preset',preset='latest_hospitalization');sections-={'growth','vaccinations'}
    elif preset=='antibiotics':spec=share.ShareSpec(mode='preset',preset='antibiotics');sections-={'growth','vaccinations','labs'}
    elif preset=='labs':spec=share.ShareSpec(mode='categories',categories=['labs']);sections={'summary','timeline','labs','cultures','sources'}
    elif preset=='vaccination':spec=share.ShareSpec(mode='preset',preset='vaccination');sections={'summary','vaccinations','sources'}
    elif preset=='daily_care_7d':spec=share.ShareSpec(mode='categories',categories=['daily_care'],daily_care_days=7,include_vaccinations=False);sections={'summary','daily_care','sources'};care_days=7
    else:
        chosen=set(custom.get('sections') or ALL_SECTIONS)
        cats=[]
        if 'hospitalizations' in chosen:cats.append('hospital')
        if 'illnesses' in chosen:cats.append('illness')
        if 'medicines' in chosen or 'prescriptions' in chosen:cats+=['prescriptions','medications']
        if 'antibiotics' in chosen:cats.append('antibiotics')
        if 'labs' in chosen or 'cultures' in chosen:cats.append('labs')
        if 'vaccinations' in chosen:cats.append('vaccinations')
        if 'growth' in chosen:cats.append('growth')
        if 'daily_care' in chosen:cats.append('daily_care');care_days=int(custom.get('daily_care_days') or 7)
        df=custom.get('date_from');dt=custom.get('date_to')
        df=date.fromisoformat(df) if isinstance(df,str) and df else df;dt=date.fromisoformat(dt) if isinstance(dt,str) and dt else dt
        if not cats:spec=share.ShareSpec(mode='complete',date_from=df,date_to=dt)
        else:spec=share.ShareSpec(mode='categories',categories=cats,date_from=df,date_to=dt,include_vaccinations='vaccinations' in chosen,include_growth='growth' in chosen,daily_care_days=care_days)
        sections=chosen|{'summary','sources'} if include_sources else chosen|{'summary'}
        if 'prescriptions' in chosen:sections.add('medicines')
        sections={x for x in sections if x in ALL_SECTIONS}
    if not include_sources:sections.discard('sources')
    return spec,{'sections':sections,'include_photos':photos,'include_sources':include_sources,'daily_care_days':care_days or (custom.get('daily_care_days') if preset=='custom' else None)}

# ------------------------------------------------------------------ model
def build_model(db,p,preset,custom=None,generated=None):
    spec,opt=spec_for(preset,custom)
    sel=share.resolve(db,p,spec)
    generated=generated or utcnow().astimezone(ZoneInfo(p.tz))
    title,file_slug,desc=EXPORT_TYPES.get(preset,EXPORT_TYPES['custom'])
    docs={d.id:d for d in db.query(Document).filter_by(patient_id=p.id)}
    entries=share.entries_for(db,sel)
    sources=[];letter=lambda i:chr(65+i) if i<26 else f'{chr(65+i//26-1)}{chr(65+i%26)}'
    for i,e in enumerate(entries):
        d=e['doc'];sources.append({'id':d.id,'ref':f'R{d.id}','appendix':letter(i),'title':d.title,'category':d.category,'record_date':d.record_date.isoformat() if d.record_date else None,'pages':d.page_count,'mime':d.mime,'source_ref':d.source_ref,'file':e['path'],'reviewed':d.extraction_status=='reviewed'})
    ref_index={s['id']:s for s in sources}
    def ref(doc_id,page=1):
        s=ref_index.get(doc_id)
        if not s:return None
        return {'doc_id':doc_id,'ref':s['ref'],'appendix':s['appendix'],'page':page or 1,'label':f"Source {s['ref']}"+(f" · p.{page}" if page and page>1 else '')}
    secs=opt['sections']
    get=lambda model,ids:[db.get(model,i) for i in ids]
    episodes=sorted(get(Episode,sel['episodes']),key=lambda x:x.started_on)
    admissions=sorted(get(Admission,sel['admissions']),key=lambda x:x.admitted_on)
    meds=sorted(get(Medicine,sel['medicines']),key=lambda x:(x.prescription_date or x.start_date or date(1900,1,1),x.id))
    obs=sorted(get(Observation,sel['observations']),key=lambda x:(x.record_date,x.id))
    vaccines=sorted(get(Vaccine,sel['vaccines']),key=lambda x:(x.given_date or x.due_date or date(2100,1,1)))
    growth=sorted(get(Growth,sel['growth']),key=lambda x:x.record_date)
    fevers=sorted(get(FeverEpisode,sel['fevers']),key=lambda x:x.first_documented_on or date(1900,1,1))
    courses=sorted(get(AntibioticCourse,sel['courses']),key=lambda x:(x.actual_start or x.prescribed_on or x.planned_start or date(1900,1,1),x.id))
    visits=sorted(get(HospitalVisit,sel['visits']),key=lambda x:x.planned_at)
    tadmins=sorted(get(TreatmentAdministration,sel['administrations']),key=lambda x:x.occurred_at)
    today=generated.date()
    # ---- medicines table
    med_rows=[]
    for m in meds:
        events=db.query(MedicationEvent).filter_by(medicine_id=m.id).order_by(MedicationEvent.occurred_at).all()
        given=[e for e in events if e.event_type=='given']
        status='CONFIRMED GIVEN' if given else ('SCHEDULED' if m.status=='verified' and m.schedule_times else MED_STATUS.get(m.status,'PRESCRIBED'))
        context={'prescription':'Outpatient prescription','discharge_prescription':'Discharge prescription','inpatient_treatment':'Listed as given in hospital','historical_prescription':'Historical prescription'}.get(m.course_type,m.course_type.replace('_',' '))
        med_rows.append({'id':m.id,'name':m.name,'generic':m.generic_name,'dose':' '.join(x for x in (m.dosage,m.dose_unit) if x).strip() or '—','route':m.route or '—','frequency':m.frequency or '—','context':context,'status':status,'prescribed_on':m.prescription_date.isoformat() if m.prescription_date else None,'planned':(m.start_date,m.end_date),'actual':(m.actual_start_date,m.actual_stop_date),'dates':fmt_d(m.prescription_date or m.start_date)+((' → '+fmt_d(m.end_date)) if m.end_date else ''),'instructions':m.instructions,'given_events':[{'at':e.occurred_at.isoformat(),'source':e.source,'ref':ref(e.source_document_id or m.source_document_id,e.source_page)} for e in given],'verified':m.status=='verified','ref':ref(m.source_document_id,m.source_page)})
    # ---- antibiotics
    abx_report=lifetime.antibiotic_report(db,p.id,'all') if courses else None
    abx_rows=[];course_ids={c.id for c in courses}
    for c in courses:
        adms=db.query(AntibioticAdministration).filter_by(course_id=c.id).order_by(AntibioticAdministration.administered_on).all()
        days=sorted(x.isoformat() for x in lifetime.course_exposure(c,adms))
        status={'prescribed_only':'PRESCRIBED','scheduled':'SCHEDULED','confirmed_administered':'CONFIRMED GIVEN','unknown':'UNKNOWN','not_received':'NOT RECEIVED'}[c.administration_status]
        if c.administration_status=='confirmed_administered' and not days:status='CONFIRMED GIVEN (hospital statement, no dated chart)'
        ep=db.get(Episode,c.illness_episode_id) if c.illness_episode_id else None
        start=c.actual_start or c.prescribed_on or c.planned_start;end=c.actual_stop or c.planned_end or (start+timedelta(days=(c.prescribed_duration_days or 1)-1) if start and c.prescribed_duration_days else start)
        if not start and c.admission_id:
            a=db.get(Admission,c.admission_id);start,end=(a.admitted_on,a.discharged_on) if a else (None,None)
        abx_rows.append({'id':c.id,'drug':c.generic_name,'brand':c.brand_name,'class':c.drug_class,'route':c.route,'setting':c.setting.replace('_',' '),'reason':(ep.title if ep else c.indication) or '—','start':start.isoformat() if start else None,'end':end.isoformat() if end else None,'dates_known':bool(c.actual_start or c.prescribed_on or c.planned_start),'dose':' '.join(x for x in (c.dose,c.frequency) if x) or '—','status':status,'status_key':c.administration_status,'exposure_days':days,'prescribed_duration':c.prescribed_duration_text or (f'{c.prescribed_duration_days} days' if c.prescribed_duration_days else '—'),'continuation_of':c.continuation_of_id,'verification':c.verification_status,'origin':c.origin,'needs_check':c.needs_parent_check,'ref':ref(c.source_document_id,c.source_page),'administrations':[{'on':a.administered_on.isoformat(),'time':a.administered_time,'source':a.source,'ref':ref(a.source_document_id,a.source_page)} for a in adms]})
    if abx_report:
        m=abx_report['metrics'];inc=lambda ids:[i for i in ids if i in course_ids]
        abx_metrics={'distinct':sorted({c['drug'] for c in abx_rows if c['status_key']!='not_received' and c['class']!='unclassified'}),'courses':len([c for c in abx_rows if not c['continuation_of'] and c['status_key']!='not_received' and c['class']!='unclassified']),'confirmed_days':len({d for c in abx_rows for d in c['exposure_days']}),'prescribed_only':len([c for c in abx_rows if c['status_key']=='prescribed_only']),'hospital_courses':len([c for c in abx_rows if c['setting']=='inpatient']),'statement_only':len([c for c in abx_rows if c['status_key']=='confirmed_administered' and not c['exposure_days']]),'unknown':len([c for c in abx_rows if c['status_key']=='unknown'])}
    else:abx_metrics=None
    # ---- labs
    lab_groups=defaultdict(list);trend_groups=defaultdict(list);cultures=[]
    for o in obs:
        canon=canonical_test(o.test_name);cat=lab_category(canon,o)
        row={'id':o.id,'date':o.record_date.isoformat(),'test':canon,'original_name':o.test_name,'value':o.value,'unit':o.unit,'specimen':o.specimen,'reference':o.reference_range,'verified':o.verified,'ref':ref(o.source_document_id,o.source_page),'organism':o.organism,'findings':o.culture_findings,'susceptibility':o.susceptibility}
        if cat=='Microbiology':cultures.append(row)
        else:
            lab_groups[cat].append(row)
            if numeric(o.value) is not None:trend_groups[(canon,re.sub(r'[^a-z0-9/%]','',(canonical_unit(o.unit)[0] or o.unit or '').casefold()))].append({**row,'num':numeric(o.value)*canonical_unit(o.unit)[1],'unit_display':canonical_unit(o.unit)[0] or o.unit})
    order=[c for c,_ in LAB_CATEGORIES if c!='Microbiology']+['Other']
    labs=[{'category':c,'rows':lab_groups[c]} for c in order if lab_groups.get(c)]
    trends=[{'test':k[0],'unit':v[-1]['unit_display'],'points':sorted(v,key=lambda r:(r['date'],r['id']))} for k,v in trend_groups.items() if len(v)>=2]
    trends.sort(key=lambda t:(['Haemoglobin','CRP','WBC (total leukocyte count)','Platelet count','Fecal calprotectin'].index(t['test']) if t['test'] in ('Haemoglobin','CRP','WBC (total leukocyte count)','Platelet count','Fecal calprotectin') else 99,t['test']))
    same_day=defaultdict(list)
    for t in trends:
        for r in t['points']:same_day[(t['test'],r['date'])].append(r['id'])
    conflicts=[{'test':k[0],'date':k[1],'ids':v} for k,v in same_day.items() if len(v)>1]
    # susceptibility matrix across cultures with an organism
    sus_cols=[c for c in cultures if c['susceptibility']];matrix=None
    if sus_cols:
        parsed=[(c,lifetime._parse_susceptibility(c['susceptibility'])) for c in sus_cols]
        drugs=[]
        for _,s in parsed:
            for k in ('susceptible','resistant','intermediate'):
                for d in s[k]:
                    if d not in drugs:drugs.append(d)
        def mark(s,d):return 'S' if d in s['susceptible'] else 'R' if d in s['resistant'] else 'I' if d in s['intermediate'] else '—'
        matrix={'columns':[{'id':c['id'],'date':c['date'],'specimen':c['specimen'],'organism':c['organism'],'ref':c['ref']} for c,_ in parsed],'rows':[{'drug':d,'marks':[mark(s,d) for _,s in parsed],'prescribed_or_given':any(r['drug']==d or d in r['drug'] for r in abx_rows if r['status_key']!='not_received')} for d in drugs]}
    # ---- vaccinations
    vax_rows=[{'id':v.id,'name':v.name,'dose':v.dose_label,'due':v.due_date.isoformat() if v.due_date else None,'given':v.given_date.isoformat() if v.given_date else None,'status':STATUS_LABEL.get(vaccine_status(v,today),vaccine_status(v,today)),'status_key':vaccine_status(v,today),'source_type':v.source_type.replace('_',' '),'confirmed':v.confirmed,'batch':v.batch_number,'ref':ref(v.source_document_id,v.source_page)} for v in vaccines]
    # ---- hospitalizations
    hosp=[]
    for a in admissions:
        plan=a.treatment_plan or ''
        parts={'treatment':'','discharge':'','follow_up':''}
        m1=re.search(r'(?is)treatment given during hospitalization[^:]*:\s*(.*?)(?:discharge prescription|$)',plan);m2=re.search(r'(?is)discharge prescription:\s*(.*?)(?:follow[- ]?up|$)',plan);m3=re.search(r'(?is)follow[- ]?up[^:]*:?\s*(.*)$',plan)
        parts['treatment']=m1[1].strip(' .') if m1 else plan;parts['discharge']=m2[1].strip(' .') if m2 else '';parts['follow_up']=m3[1].strip(' .') if m3 else ''
        ep=next((e for e in episodes if e.started_on<=a.admitted_on<=(e.ended_on or a.admitted_on)),None)
        hosp.append({'id':a.id,'facility':a.facility,'admitted':a.admitted_on.isoformat(),'discharged':a.discharged_on.isoformat() if a.discharged_on else None,'length_days':((a.discharged_on-a.admitted_on).days+1) if a.discharged_on else None,'doctor':a.doctor,'diagnosis':a.diagnosis,'complaints':ep.symptoms if ep else '','course':(ep.notes if ep else ''),'treatment':parts['treatment'],'discharge_medicines':[r for r in med_rows if r['context']=='Discharge prescription' and r['prescribed_on'] and a.discharged_on and r['prescribed_on']==a.discharged_on.isoformat()],'inpatient_listed':[r for r in med_rows if r['context']=='Listed as given in hospital' and any(x and a.admitted_on-timedelta(days=1)<=x<=(a.discharged_on or a.admitted_on)+timedelta(days=1) for x in (r['planned'][0],r['actual'][0],date.fromisoformat(r['prescribed_on']) if r['prescribed_on'] else None))],'follow_up':parts['follow_up'],'labs_in_stay':[o.id for o in obs if a.admitted_on<=o.record_date<=(a.discharged_on or a.admitted_on)],'ref':ref(a.source_document_id,a.source_page),'discharge_ref':ref(a.discharge_document_id)})
    # ---- illness episodes
    ill=[]
    for e in episodes:
        links=json.loads(e.links_json or '[]')
        ill.append({'id':e.id,'title':e.title,'start':e.started_on.isoformat(),'end':e.ended_on.isoformat() if e.ended_on else None,'symptoms':e.symptoms,'diagnosis':e.documented_diagnosis,'notes':e.notes,'fevers':[{'id':f.id,'title':f.title,'first':f.first_documented_on.isoformat() if f.first_documented_on else None,'temp':(f.highest_temperature+' °'+f.temperature_unit) if f.highest_temperature else '','verification':f.verification_status,'origin':f.origin,'ref':ref(f.source_document_id,f.source_page)} for f in fevers if f.illness_episode_id==e.id],'courses':[r['id'] for r in abx_rows if db.get(AntibioticCourse,r['id']).illness_episode_id==e.id],'labs':[o.id for o in obs if e.started_on-timedelta(days=3)<=o.record_date<=(e.ended_on or e.started_on)+timedelta(days=3)],'hospitalized':any(a.admitted_on>=e.started_on and a.admitted_on<=(e.ended_on or e.started_on) for a in admissions),'ref':ref(e.source_document_id,e.source_page),'doc_refs':[ref(l['id']) for l in links if l.get('kind')=='document' and ref(l['id'])]})
    # ---- timeline (documented events only)
    tl=[]
    for a in admissions:
        tl.append({'date':a.admitted_on.isoformat(),'kind':'admission','title':'Hospital admission','detail':f"{a.facility}"+(f" · {a.diagnosis[:120]}" if a.diagnosis else ''),'ref':ref(a.source_document_id,a.source_page)})
        if a.discharged_on:tl.append({'date':a.discharged_on.isoformat(),'kind':'discharge','title':'Hospital discharge','detail':a.facility,'ref':ref(a.discharge_document_id) or ref(a.source_document_id)})
    for e in episodes:tl.append({'date':e.started_on.isoformat(),'kind':'episode','title':e.title,'detail':e.documented_diagnosis or e.symptoms,'ref':ref(e.source_document_id,e.source_page)})
    for f in fevers:
        if f.first_documented_on:tl.append({'date':f.first_documented_on.isoformat(),'kind':'fever','title':'Fever documented','detail':f.title+(' · parent-reported' if f.origin=='parent_report' else ''),'ref':ref(f.source_document_id,f.source_page)})
    for c in cultures:tl.append({'date':c['date'],'kind':'culture','title':f"{c['test']}",'detail':(c['organism'] or c['value'])+(f" ({c['specimen']})" if c['specimen'] else ''),'ref':c['ref']})
    seen_lab_days=set()
    for o in obs:
        key=(o.record_date,o.source_document_id)
        if (o.organism or o.culture_findings) or key in seen_lab_days:continue
        seen_lab_days.add(key);n=sum(1 for x in obs if (x.record_date,x.source_document_id)==key)
        tl.append({'date':o.record_date.isoformat(),'kind':'lab','title':'Laboratory report','detail':f"{n} value(s): "+', '.join(dict.fromkeys(canonical_test(x.test_name) for x in obs if (x.record_date,x.source_document_id)==key))[:140],'ref':ref(o.source_document_id,o.source_page)})
    for r in med_rows:
        if r['prescribed_on']:tl.append({'date':r['prescribed_on'],'kind':'medicine','title':f"{r['name']} — {r['status']}",'detail':r['context'],'ref':r['ref']})
    for r in abx_rows:
        if r['start'] and r['dates_known']:tl.append({'date':r['start'],'kind':'antibiotic','title':f"Antibiotic: {r['drug']} ({r['route']}) — {r['status'].split(' (')[0]}",'detail':r['reason'],'ref':r['ref']})
    for v in vaccines:
        if v.given_date:tl.append({'date':v.given_date.isoformat(),'kind':'vaccine','title':f"Vaccination recorded: {v.name} {v.dose_label}".strip(),'detail':'verified' if v.confirmed else 'card entry, pending verification','ref':ref(v.source_document_id,v.source_page)})
    for v in visits:tl.append({'date':v.planned_at.date().isoformat(),'kind':'visit','title':f"Hospital visit · {v.status}",'detail':v.purpose,'ref':ref(v.source_document_id,v.source_page)})
    for g in growth:tl.append({'date':g.record_date.isoformat(),'kind':'growth','title':'Growth measurement','detail':' · '.join(x for x in (f"{g.weight_kg} kg" if g.weight_kg else '',f"{g.length_cm} cm" if g.length_cm else '',g.milestone) if x),'ref':ref(g.source_document_id,g.source_page)})
    if p.dob and (preset=='complete' or any(a.admitted_on==p.dob for a in admissions)):tl.append({'date':p.dob.isoformat(),'kind':'birth','title':'Birth','detail':'Date of birth as recorded in the profile','ref':None})
    tl.sort(key=lambda x:(x['date'],['birth','admission','episode','fever','culture','lab','antibiotic','medicine','vaccine','visit','growth','discharge'].index(x['kind']) if x['kind'] in ('birth','admission','episode','fever','culture','lab','antibiotic','medicine','vaccine','visit','growth','discharge') else 50))
    # ---- daily care
    care=None
    days=opt.get('daily_care_days')
    if days and 'daily_care' in secs:
        try:
            from . import care as C
            end=today;start=end-timedelta(days=days-1)
            if spec.date_from and spec.date_to:start,end=spec.date_from,spec.date_to
            an=C.analytics(db,p.id,start,end);exp=C.care_export(db,p.id,start,end,opt['include_photos'])
            care={'start':start.isoformat(),'end':end.isoformat(),'series':an['series'],'totals':an['totals'],'stool_patterns':an['stool_patterns'],'medicine_adherence':an['medicine_adherence'],'symptom_occurrences':an['symptom_occurrences'],'statements':an['statements'],'warnings':an['warnings'],'sections':exp['sections'],'photo_ids':exp['photo_document_ids'] if opt['include_photos'] else [],'symptom_temps':[{'date':s.local_date.isoformat(),'at':s.at.isoformat(),'kind':s.kind,'temperature':s.temperature,'unit':s.temperature_unit} for s in C.entries(db,p.id,start,end)['symptom'] if s.temperature]}
        except Exception:care=None
    vit=None
    try:
        from .profiles import modules_for
        from . import vitals as VT
        if 'vitals' in modules_for(p,today)[0] and (days or preset in ('complete','custom')):
            vend=today;vstart=vend-timedelta(days=(days or 30)-1)
            if spec.date_from and spec.date_to:vstart,vend=spec.date_from,spec.date_to
            vit=VT.export_rows(db,p,vstart,vend)
            if vit:secs.add('daily_care')
    except Exception:vit=None
    # ---- pending items
    pending=[f"Lab {r['date']}: {r['test']} {r['value']} {r['unit']}".strip() for g in labs for r in g['rows'] if not r['verified']]+[f"Culture {c['date']}: {c['test']}" for c in cultures if not c['verified']]+[f"Medicine: {r['name']} ({r['context']})" for r in med_rows if not r['verified']]+[f"Vaccine: {r['name']} {r['dose']}".strip() for r in vax_rows if not r['confirmed']]+[f"Fever: {f.title}" for f in fevers if f.verification_status!='verified']+[f"Antibiotic: {r['drug']}" for r in abx_rows if r['verification']!='verified']
    # ---- executive summary cards
    recent_cut=today-timedelta(days=90)
    summary={
     'recent':[t for t in tl if t['date']>=recent_cut.isoformat() and t['kind'] in ('admission','episode','fever','culture','antibiotic')][-8:],
     'hospitalizations':[{'facility':h['facility'],'dates':f"{fmt_d(date.fromisoformat(h['admitted']))} – {fmt_d(date.fromisoformat(h['discharged'])) if h['discharged'] else 'discharge not documented'}",'diagnosis':h['diagnosis'],'ref':h['ref']} for h in hosp],
     'episodes':[{'title':i['title'],'dates':f"{fmt_d(date.fromisoformat(i['start']))}"+(f" – {fmt_d(date.fromisoformat(i['end']))}" if i['end'] else ''),'diagnosis':i['diagnosis'],'ref':i['ref']} for i in ill][-6:],
     'medicines':[r for r in med_rows if (r['prescribed_on'] or '')>=recent_cut.isoformat()][-8:] or med_rows[-6:],
     'antibiotics':abx_metrics,
     'lab_trends':[{'test':t['test'],'unit':t['unit'],'latest':t['points'][-1],'previous':t['points'][-2]} for t in trends[:6]],
     'cultures':[{'date':c['date'],'test':c['test'],'organism':c['organism'] or c['value'],'specimen':c['specimen'],'ref':c['ref']} for c in cultures],
     'vaccination':{'given_verified':sum(1 for r in vax_rows if r['status_key']=='documented_given_verified'),'given_pending':sum(1 for r in vax_rows if r['status_key']=='documented_given_pending_verification'),'upcoming':[r for r in vax_rows if r['status_key'] in ('upcoming_confirmed','due_pending_verification')][:4],'overdue':[r for r in vax_rows if r['status_key'] in ('confirmed_date_passed','due_date_passed_unverified')][:4]},
     'allergies':'No allergy is recorded in the app profile; hospital discharge summaries may state allergy status (see appendix).',
     'growth':{'latest':{'date':growth[-1].record_date.isoformat(),'weight':growth[-1].weight_kg,'length':growth[-1].length_cm,'head':growth[-1].head_cm} if growth else None,'count':len(growth)},
     'pending':pending[:12],'pending_count':len(pending)}
    date_range={'from':sel.get('date_from'),'to':sel.get('date_to')}
    if not date_range['from'] and not date_range['to']:
        dates=[t['date'] for t in tl]
        if dates:date_range={'from':min(dates),'to':max(dates),'derived':True}
    filename=f"{slug(p.name)}_{file_slug}_{generated.strftime('%Y-%m-%d')}"
    return {'schema':'babycare-export-1','patient':{'id':p.id,'name':p.name,'dob':p.dob.isoformat(),'age':age_text(p.dob,today),'tz':p.tz},
        'meta':{'preset':preset,'title':title,'description':desc,'generated':generated.isoformat(),'generated_display':generated.strftime('%d %b %Y, %H:%M'),'date_range':date_range,'sections':sorted(secs,key=ALL_SECTIONS.index),'filename':filename,'reason':sel['reason'],'include_photos':opt['include_photos'],'include_sources':opt['include_sources'],'note':'Family-maintained medical record kept in HealthBook AI. Clinical decisions should be verified against the appended original source documents.'},
        'sources':sources,'selection':{k:v for k,v in sel.items() if k not in ('documents',)},'summary':summary,'timeline':tl,'illnesses':ill,'hospitalizations':hosp,'medicines':med_rows,'antibiotics':{'metrics':abx_metrics,'rows':abx_rows},'labs':labs,'trends':trends,'same_day_conflicts':conflicts,'cultures':cultures,'susceptibility_matrix':matrix,'vaccinations':vax_rows,'growth':[{'date':g.record_date.isoformat(),'age':age_text(p.dob,g.record_date),'weight':g.weight_kg,'length':g.length_cm,'head':g.head_cm,'milestone':g.milestone,'ref':ref(g.source_document_id,g.source_page)} for g in growth],'daily_care':care,'vitals':vit,'pending':pending,
        'counts':{'sources':len(sources),'source_pages':sum(s['pages'] for s in sources),'episodes':len(ill),'hospitalizations':len(hosp),'prescriptions':len(med_rows),'antibiotic_courses':len(abx_rows),'lab_values':sum(len(g['rows']) for g in labs),'lab_documents':len({r['ref']['doc_id'] for g in labs for r in g['rows'] if r['ref']}|{c['ref']['doc_id'] for c in cultures if c['ref']}),'cultures':len(cultures),'vaccinations':len(vax_rows),'growth':len(growth),'pending':len(pending)}}

def estimate_pages(model):
    n=3  # cover, summary, toc
    n+=max(1,len(model['timeline'])//22+1) if 'timeline' in model['meta']['sections'] else 0
    n+=len(model['hospitalizations'])*1+(1 if model['illnesses'] else 0)+max(0,len(model['illnesses'])//4)
    n+=1 if model['medicines'] else 0;n+=len(model['medicines'])//18
    n+=(1+len(model['antibiotics']['rows'])//12) if model['antibiotics']['rows'] else 0
    n+=sum(1+len(g['rows'])//28 for g in model['labs'])+len(model['trends'])//3
    n+=1 if model['cultures'] else 0;n+=1 if model['vaccinations'] else 0;n+=1 if model['growth'] else 0
    n+=2 if model['daily_care'] else 0;n+=1 if model.get('vitals') else 0
    if 'sources' in model['meta']['sections']:n+=sum(1+s['pages'] for s in model['sources'])
    return n

# ------------------------------------------------------------------ TXT (debug / audit) rendering from the same model
def export_text(model):
    p=model['patient'];m=model['meta'];L=[f"HEALTHBOOK AI — {m['title'].upper()}",f"{p['name']} · DOB {p['dob']} · age {p['age']} · generated {m['generated_display']}",m['note'],'']
    def r(ref):return f" [{ref['label']}]" if ref else ''
    L.append('TIMELINE');L+=[f"- {t['date']} {t['title']}: {t['detail'] or ''}{r(t['ref'])}" for t in model['timeline']]
    L.append('');L.append('HOSPITALIZATIONS');L+=[f"- {h['admitted']} to {h['discharged'] or 'not documented'} {h['facility']}; {h['diagnosis']}{r(h['ref'])}" for h in model['hospitalizations']]
    L.append('');L.append('MEDICINES (status is copied, never inferred)');L+=[f"- {x['name']} | {x['dose']} | {x['route']} | {x['frequency']} | {x['context']} | {x['status']} | {x['dates']}{r(x['ref'])}" for x in model['medicines']]
    L.append('');L.append('ANTIBIOTICS');L+=[f"- {x['drug']} | {x['route']} | {x['setting']} | {x['start'] or '?'} to {x['end'] or '?'} | {x['status']} | confirmed days {len(x['exposure_days'])}{r(x['ref'])}" for x in model['antibiotics']['rows']]
    for g in model['labs']:
        L.append('');L.append(f"LABS — {g['category'].upper()}");L+=[f"- {x['date']} {x['test']}: {x['value']} {x['unit']} (ref {x['reference'] or 'not printed'}){'' if x['verified'] else ' [unverified]'}{r(x['ref'])}" for x in g['rows']]
    L.append('');L.append('MICROBIOLOGY');L+=[f"- {c['date']} {c['test']} ({c['specimen']}): {c['organism'] or c['value']}; susceptibility: {c['susceptibility'] or 'not transcribed'}{r(c['ref'])}" for c in model['cultures']]
    L.append('');L.append('VACCINATIONS');L+=[f"- {v['name']} {v['dose']}: due {v['due'] or '—'}; given {v['given'] or 'not recorded'}; {v['status']}{r(v['ref'])}" for v in model['vaccinations']]
    L.append('');L.append('SOURCE DOCUMENTS');L+=[f"- Appendix {s['appendix']} · {s['ref']} · {s['record_date'] or 'undated'} · {s['title']} ({s['pages']} page(s))" for s in model['sources']]
    return '\n'.join(L)
