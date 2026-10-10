"""Extractive history answers: every statement is rendered from an authorized record."""
import json
import re
from datetime import timedelta, date
from zoneinfo import ZoneInfo
from .models import Medicine,MedicationEvent,Vaccine,HospitalVisit,Document,Observation,Episode,Admission,TreatmentAdministration,Patient,ExtractionReview,utcnow
from .crypto import decrypt
from .extraction import pages

STOP={'when','was','were','has','have','had','did','does','do','the','my','son','daughter','child','his','her','for','how','many','days','all','please','compare','previous','latest','last','first','which','what','during','take','taken','given','prescribed','is','are','a','an','of','to','and','in','on','from','reports','report','history','medical','related','treatment','records','record','with','this','week','next','confirmed','documented','show','me','since','birth','actually','administered','happened','change','changed','over','time','at','been','he','she','it','there','any','ever','his','get','got','receive','received'}
MONTHS={m:i+1 for i,m in enumerate(['january','february','march','april','may','june','july','august','september','october','november','december'])}
MONTHS.update({m[:3]:i for m,i in list(MONTHS.items())})
ANTIBIOTIC_TERMS=('meropenem','amikacin','piperacillin','pipzo','tazobactam','cefixime','cefpodoxime','cefpodox','ceftriaxone','cefotaxime','azithromycin','amoxicillin','amoxyclav','augmentin','ciprofloxacin','ofloxacin','levofloxacin','gentamicin','co-trimoxazole','cotrimoxazole','nitrofurantoin','metronidazole','omnix','ciprodac','taxim','zifi','cefoperazone','colistin','tobramycin','moxifloxacin','antibiotic')
CBC_TERMS=('cbc','hemoglobin','haemoglobin','hgb','hb','wbc','leukocyte','leucocyte','rbc','erythrocyte','platelet','hematocrit','haematocrit','hct','mcv','mch','rdw','neutrophil','lymphocyte','monocyte','eosinophil','basophil','tlc','pcv','mchc','esr','reticulocyte')

def terms(question):
    return [x for x in re.findall(r'[a-z0-9]+',question.casefold()) if x not in STOP]

def _months(q):
    return {MONTHS[w] for w in re.findall(r'[a-z]+',q) if w in MONTHS}

def _in_months(d,months):
    return not months or (d is not None and d.month in months)

LIFETIME_GUARD=re.compile(r'fever|febrile|pyrexia|temperature|antibiotic|antimicrobial|lifetime|how many|how often|how much|longest|per year|exposure',re.I)
LIFETIME_DOMAIN=re.compile(r'fever|febrile|pyrexia|temperature|antibiotic|antimicrobial',re.I)

def answer(db,pid,question):
    # The lifetime module (fever history, antibiotic exposure metrics) answers only questions in its own domain;
    # record-grounded questions about prescriptions, administration, hospitalizations, cultures and episodes stay here.
    # Daily Care (fluids, diapers, stool entries, medicines actually given, activity, sleep) answers only from journal rows; it self-guards and returns None otherwise.
    from .vitals import vitals_answer
    vitals_hit=vitals_answer(db,pid,question)
    if vitals_hit:return vitals_hit
    from .care import care_answer
    care_hit=care_answer(db,pid,question)
    if care_hit:return care_hit
    if LIFETIME_GUARD.search(question) and ('compare' not in question.casefold() or LIFETIME_DOMAIN.search(question)):
        from .lifetime import lifetime_answer
        hit=lifetime_answer(db,pid,question)
        if hit:return hit
    q=question.casefold();tokens=terms(question);evidence=[]
    p=db.get(Patient,pid);today=utcnow().astimezone(ZoneInfo(p.tz)).date();months=_months(q)
    illness_intent=any(w in q for w in ('illness','illnesses','sick','diagnos','episode','disease','infection','what happened'))
    hospital_intent=any(w in q for w in ('hospital','admission','admitted','hospitalis','hospitaliz','discharge','iv '))
    discharge_intent='discharge' in q
    administered_intent=any(w in q for w in ('actually administered','actually given','were administered','was administered','administered','actually taken'))
    antibiotic_intent='antibiotic' in q
    compare_intent='compare' in q or 'difference' in q or 'differ' in q
    culture_intent=any(w in q for w in ('culture','coli','organism','sensitivity','susceptib'))
    trend_intent=any(w in q for w in ('change','trend','over time','rise','fall','increase','decrease'))
    scope='vaccines' if any(w in q for w in ('vaccin','immuniz','immunis')) else 'labs' if any(w in q for w in ('cbc','blood test','lab','culture','coli','crp','calprotectin','hemoglobin','haemoglobin','platelet','wbc')) else 'episodes' if illness_intent and not hospital_intent else 'hospital' if hospital_intent else 'medicine'
    generic={'medication','medications','medicine','medicines','vitamin','vaccination','vaccinations','vaccine','vaccines','hospital','hospitalization','hospitalisation','scheduled','schedules','appointment','appointments','cbc','blood','test','tests','labs','lab','culture','cultures','episodes','episode','illness','illnesses','antibiotic','antibiotics','treatments','treatment','discharge','admission','september','august','july','june','october','november','december','january','february','march','april','may','coli','e','latest','compare'}
    specific=[x for x in tokens if x not in generic]
    def matches(text):
        normalized=re.sub(r'[^a-z0-9]+',' ',text.casefold())
        return bool(specific) and all(re.search(r'\b'+re.escape(t)+r'\b',normalized) for t in specific)
    def add(kind,row,statement,when=None,verified=True,doc=None,page=None,extra=None):
        evidence.append({'citation':f'{kind}:{row.id}','record_type':kind,'record_id':row.id,'statement':statement,'date':str(when)[:19] if when else None,
            'verified':verified,'source_document_id':doc if doc is not None else getattr(row,'source_document_id',None),'source_page':page or getattr(row,'source_page',None),**(extra or {})})
    admissions=db.query(Admission).filter_by(patient_id=pid).order_by(Admission.admitted_on.desc()).all()
    summary=[]

    # ---- medicines
    for m in db.query(Medicine).filter_by(patient_id=pid):
        hay=m.name+' '+m.generic_name+' '+m.brand_name+' '+m.instructions+' '+m.indication+' '+m.course_type
        include=matches(hay) or (scope=='medicine' and not specific)
        if antibiotic_intent:include=any(t in hay.casefold() for t in ANTIBIOTIC_TERMS)
        if discharge_intent and scope!='labs':include=m.course_type=='discharge_prescription' or (matches(hay) and 'discharge' in hay.casefold())
        if hospital_intent and not discharge_intent and scope=='hospital':
            admission_docs={d for a in admissions if _in_months(a.admitted_on,months) or _in_months(a.discharged_on,months) for d in (a.source_document_id,a.discharge_document_id) if d}
            include=m.course_type in ('inpatient_treatment','discharge_prescription') and (m.source_document_id in admission_docs or (not admission_docs and _in_months(m.prescription_date or m.start_date,months)))
        if not include:continue
        events=db.query(MedicationEvent).filter_by(medicine_id=m.id,event_type='given').order_by(MedicationEvent.occurred_at).all()
        if administered_intent and not events and m.course_type!='inpatient_treatment':continue
        duration=(m.end_date-m.start_date).days+1 if m.start_date and m.end_date else None
        label={'prescription':'Prescription','discharge_prescription':'Prescribed at hospital discharge','inpatient_treatment':'Listed as treatment given during hospitalization (no dates or times supplied)','historical_prescription':'Historical prescription'}[m.course_type]
        statement=f'{m.name} [{label}]: prescription date {m.prescription_date or "not documented"}; planned course {m.start_date or "unknown"} to {m.end_date or "unknown"}; prescribed duration {str(duration)+" calendar days (inclusive)" if duration else "not established"}. Written instructions: {m.instructions or "none recorded"}. Status: {m.status}. Actual start {m.actual_start_date or "not documented"}; actual stop {m.actual_stop_date or "not documented"}.'
        if events:statement+=f' {len(events)} recorded given events, first {events[0].occurred_at}, last {events[-1].occurred_at}; continuous daily administration is not established.'
        else:statement+=' No actual given events are documented.'
        add('medicine',m,statement,m.prescription_date or m.start_date,m.status=='verified',extra={'course_type':m.course_type,'antibiotic_list_match':any(t in hay.casefold() for t in ANTIBIOTIC_TERMS)})
        for e in events:
            add('medication_event',e,f'{m.name}: {e.event_type} at {e.occurred_at} ({e.source}), prescription version {e.medicine_version}.',e.occurred_at,True,e.source_document_id or m.source_document_id,e.source_page if e.source_document_id else m.source_page)
    if scope=='medicine' and specific and not any(x['record_type']=='medicine' for x in evidence):
        summary.append(f'No medication course matching "{" ".join(tokens)}" is recorded as a prescription record. Any matches below come from unverified transcriptions of handwritten pages (dated OPD sheets) and no administration events exist for them.')
    if administered_intent and scope=='medicine' and not any(x['record_type'] in ('medication_event','treatment_administration') for x in evidence):
        summary.append('No actual administration events are documented in the stored records. Prescriptions and discharge lists show what was written, not what was given; the hospital administration chart or caregiver dose logs would be needed.')

    # ---- vaccines
    for v in db.query(Vaccine).filter_by(patient_id=pid):
        if not (scope=='vaccines' or matches(v.name+' '+v.note)):continue
        if ('next' in q or 'upcoming' in q) and (not v.confirmed or not v.due_date or v.given_date or v.due_date<today):continue
        if ('administered' in q or 'given' in q) and (not v.confirmed or not v.given_date):continue
        add('vaccine',v,f'{v.name} {v.dose_label}: '+(f'caregiver-verified administration date {v.given_date}.' if v.given_date and v.confirmed else f'unverified candidate administration date {v.given_date}.' if v.given_date else 'No administration documented.')+f' Appointment date {v.due_date or "not recorded"}; {"confirmed" if v.confirmed else "unconfirmed"}. Reference source: {v.source_type}.',v.given_date or v.due_date,v.confirmed)
    if scope=='vaccines' and ('next' in q or 'upcoming' in q) and not evidence:
        pending=[v for v in db.query(Vaccine).filter_by(patient_id=pid,confirmed=False) if v.due_date and not v.given_date and v.source_type!='India_UIP_reference']
        summary.append('No caregiver-confirmed upcoming vaccination appointment is recorded, so no reminder is scheduled.'+(f' {len(pending)} card entries with due dates are pending verification: '+'; '.join(f'{v.name} {v.dose_label} due {v.due_date}' for v in sorted(pending,key=lambda x:x.due_date)) if pending else ''))
        for v in pending:add('vaccine',v,f'{v.name} {v.dose_label}: due date {v.due_date} written on card; given column blank; NOT confirmed; no reminder active.',v.due_date,False)

    # ---- observations
    obs_rows=db.query(Observation).filter_by(patient_id=pid).order_by(Observation.record_date,Observation.id).all()
    culture_rows=[];grouped={}
    wants_results=any(w in q for w in ('result','results','test','tests','lab','labs','report','reports','happened','investigation','finding'))
    for o in obs_rows:
        is_culture=bool(o.organism or o.culture_findings)
        include=matches(' '.join([o.test_name,o.organism,o.culture_findings,o.value,o.specimen])) or (scope=='labs' and not specific)
        if culture_intent and scope=='labs':include=is_culture or matches(o.test_name+' '+o.organism)
        group=False
        if hospital_intent and scope=='hospital' and admissions:
            include=(wants_results and any(a.admitted_on<=o.record_date<=(a.discharged_on or a.admitted_on) and _in_months(a.admitted_on,months) for a in admissions)) or (is_culture and culture_intent)
            group=not is_culture
        if not include:continue
        if 'cbc' in q and not any(term in o.test_name.casefold() for term in CBC_TERMS):continue
        if months and scope=='labs' and not _in_months(o.record_date,months):continue
        if is_culture:culture_rows.append(o)
        if group:
            grouped.setdefault((o.record_date,o.source_document_id),[]).append(o);continue
        add('observation',o,f'{o.test_name}: {o.value} {o.unit or "unit unknown"}; specimen: {o.specimen or "unknown"}; printed reference interval: {o.reference_range or "not provided"}; organism: {o.organism or "not recorded"}; culture: {o.culture_findings or "not recorded"}; susceptibility: {o.susceptibility or "not reported"}.',o.record_date,o.verified)
    for (when,did),rows in grouped.items():
        # One statement per report keeps hospitalization answers readable; every value stays as printed.
        add('observation_group',rows[0],f'Laboratory report dated {when}: '+'; '.join(f'{r.test_name} {r.value} {r.unit}'.strip() for r in rows)+'.',when,all(r.verified for r in rows),did,rows[0].source_page,extra={'observation_ids':[r.id for r in rows]})

    # ---- hospital visits, administrations
    for v in db.query(HospitalVisit).filter_by(patient_id=pid).order_by(HospitalVisit.planned_at):
        if not (scope=='hospital' or matches(v.purpose+' '+v.notes)):continue
        if 'last' in q and 'admission' in q and (not admissions or v.admission_id!=admissions[0].id):continue
        if ('scheduled' in q or 'next' in q) and (not v.confirmed or v.status!='planned'):continue
        if 'week' in q and not today<=v.planned_at.date()<=today+timedelta(days=7):continue
        if months and not _in_months(v.planned_at.date(),months):continue
        add('visit',v,f'{v.facility}: {v.purpose}; scheduled {v.planned_at}; status {v.status}; {"confirmed" if v.confirmed else "unconfirmed"}. Attendance is not evidence of medication administration.',v.planned_at,v.confirmed)
    for a in db.query(TreatmentAdministration).filter_by(patient_id=pid).order_by(TreatmentAdministration.occurred_at):
        if scope not in ('hospital','medicine') and not matches(a.medication):continue
        if scope=='medicine' and not (administered_intent or matches(a.medication)):continue
        visit=db.get(HospitalVisit,a.visit_id)
        if 'last' in q and 'admission' in q and (not admissions or visit.admission_id!=admissions[0].id):continue
        add('treatment_administration',a,f'{a.medication}: administration recorded {a.occurred_at}; copied dose {a.recorded_dose or "not recorded"}; route {a.recorded_route or "not recorded"}; administration source checked by caregiver.',a.occurred_at)

    # ---- admissions
    meds_only=discharge_intent and not wants_results
    if (scope=='hospital' or illness_intent) and not meds_only:
        for a in admissions:
            if months and not _in_months(a.admitted_on,months) and not _in_months(a.discharged_on,months):continue
            add('admission',a,f'Admission at {a.facility} from {a.admitted_on} to {a.discharged_on or "discharge date unknown"}; doctor {a.doctor or "not documented"}; documented diagnosis: {a.diagnosis or "not documented"}; recorded course/treatment: {a.treatment_plan or "not documented"}.',a.admitted_on,bool(a.source_document_id),extra={'discharge_document_id':a.discharge_document_id})

    # ---- episodes
    for e in ([] if meds_only else db.query(Episode).filter_by(patient_id=pid).order_by(Episode.started_on)):
        include=matches(e.title+' '+e.documented_diagnosis+' '+e.symptoms+' '+e.notes) or scope=='episodes' or (culture_intent and 'coli' in (e.title+e.documented_diagnosis+e.notes).casefold() and 'coli' in q)
        if hospital_intent and scope=='hospital':
            include=any(l['kind']=='admission' for l in json.loads(e.links_json)) and _in_months(e.started_on,months)
        if months and scope=='episodes' and not (_in_months(e.started_on,months) or _in_months(e.ended_on,months)):continue
        if not include:continue
        add('episode',e,f'{e.title}: {e.started_on} to {e.ended_on or "end unknown"}; symptoms {e.symptoms or "not recorded"}; documented diagnosis {e.documented_diagnosis or "not recorded"}; notes {e.notes}.',e.started_on,bool(e.source_document_id))
        linked=json.loads(e.links_json)
        models={'medicine':Medicine,'observation':Observation,'admission':Admission,'visit':HospitalVisit,'document':Document}
        for link in linked:
            obj=db.get(models[link['kind']],link['id'])
            if not obj or obj.patient_id!=pid:continue
            if isinstance(obj,Medicine) and specific and scope not in ('hospital','episodes') and not any(x['citation']==f'medicine:{obj.id}' for x in evidence):add('medicine',obj,f'Explicitly linked to episode {e.id}: {obj.name}; prescribed instructions {obj.instructions}; planned {obj.start_date or "unknown"} to {obj.end_date or "unknown"}; {obj.status}. Prescription does not prove administration.',obj.prescription_date or obj.start_date,obj.status=='verified')
            if isinstance(obj,Observation) and scope in ('episodes','hospital') and (obj.organism or obj.culture_findings) and not any(x['citation']==f'observation:{obj.id}' for x in evidence):add('observation',obj,f'Linked culture result: {obj.test_name} {obj.value}; organism {obj.organism or "n/a"}; susceptibility {obj.susceptibility or "n/a"}.',obj.record_date,obj.verified)

    # ---- document text (OCR or reviewer transcription; always unverified)
    reviews={r.document_id:r for r in db.query(ExtractionReview).join(Document,Document.id==ExtractionReview.document_id).filter(Document.patient_id==pid)}
    for d in db.query(Document).filter_by(patient_id=pid):
        text=decrypt(d.extracted_cipher.encode()).decode() if d.extracted_cipher else ''
        sources=[('Unverified OCR transcription',text)]
        review=reviews.get(d.id)
        if review:
            try:review_text=json.loads(review.payload).get('text','')
            except ValueError:review_text=''
            if review_text and review_text!=text:sources.append(('Unverified reviewer transcription of handwriting' if review.status!='confirmed' else 'Caregiver-reviewed transcription',review_text))
        per_doc=0
        for label,body_text in sources:
            for page,body in pages(body_text):
                for line in body.splitlines():
                    if per_doc>=3:break
                    if matches(line) and not line.startswith('[REVIEWER TRANSCRIPTION'):
                        add('document',d,f'{label} ({d.title}): {line[:500]}',d.record_date,label.startswith('Caregiver'),d.id,page);per_doc+=1

    seen=set();unique=[]
    for item in evidence:
        key=(item['citation'],item['statement'])
        if key not in seen:unique.append(item);seen.add(key)
    unique.sort(key=lambda x:(not x['verified'],x['date'] or ''))
    if scope=='vaccines' and ('next' in q or 'upcoming' in q) and any(x['verified'] for x in unique):unique=[x for x in unique if x['record_type']=='vaccine' and x['verified']][:1]
    if ('latest' in q or 'previous' in q or 'last' in q) and scope=='labs' and not specific and not culture_intent:
        dates=sorted({x['date'] for x in unique if x['record_type']=='observation' and x['date']},reverse=True)
        selected=dates[:2] if 'compare' in q else dates[1:2] if 'previous' in q else dates[:1]
        unique=[x for x in unique if x['record_type']=='observation' and x['date'] in selected]
        if selected:summary.append(f'Most recent matching laboratory date: {selected[0]}.')

    # ---- structured comparison of culture reports (side by side, no inference)
    comparison=None
    if culture_intent and (compare_intent or 'coli' in q) and culture_rows:
        cols=[]
        for o in culture_rows:
            sus={}
            for part in re.split(r'[;\n]',o.susceptibility or ''):
                m=re.match(r'\s*([^:]+?)\s*[:=]\s*([SIR])\b',part.strip(),re.I)
                if m:sus[m[1].strip()]=m[2].upper()
            cols.append({'observation_id':o.id,'date':o.record_date.isoformat(),'test':o.test_name,'specimen':o.specimen,'organism':o.organism,'findings':o.culture_findings,'susceptibility':sus,'source_document_id':o.source_document_id,'verified':o.verified})
        drugs=sorted({d for c in cols for d in c['susceptibility']})
        table=[{'antibiotic':d,'results':[c['susceptibility'].get(d) for c in cols],'differs':len({c['susceptibility'].get(d) for c in cols if c['susceptibility'].get(d)})>1} for d in drugs]
        comparison={'reports':cols,'table':table,'differences':[r['antibiotic'] for r in table if r['differs']],
            'note':'Side-by-side transcription of laboratory reports. Different specimens/dates with the same organism name do not establish the same infection, reinfection or persistence; antibiotic choice belongs to the treating clinician.'}
        if comparison['differences']:summary.append('Susceptibility results that differ between the compared reports: '+', '.join(comparison['differences'])+'.')
        if len(cols)>=2:summary.append(f'{len(cols)} culture reports compared ('+'; '.join(f'{c["date"]} {c["specimen"] or "specimen unknown"}: {c["organism"] or c["findings"] or "no organism recorded"}' for c in cols)+').')
    if trend_intent and scope=='labs':
        series=[x for x in unique if x['record_type']=='observation']
        if series:summary.append('Values in date order: '+' → '.join(f'{x["date"]}: {x["statement"].split(":")[1].split(";")[0].strip()}' for x in series)+'. Values are shown as printed; interpretation needs the treating clinician.')
    if scope=='episodes' and not specific:
        eps=[x for x in unique if x['record_type']=='episode']
        summary.append(f'{len(eps)} documented illness episode(s) are recorded since birth ({p.dob}); each is listed with its documented diagnosis or marked as not documented.' if eps else 'No illness episodes have been recorded yet.')
    if hospital_intent and scope=='hospital':
        ads=[x for x in unique if x['record_type']=='admission']
        if not ads and not meds_only:summary.append('No hospital admission matching the question is recorded.')
        for x in ads:
            a=db.get(Admission,x['record_id'])
            inpatient=sum(1 for y in unique if y['record_type']=='medicine' and y.get('course_type')=='inpatient_treatment')
            discharge=sum(1 for y in unique if y['record_type']=='medicine' and y.get('course_type')=='discharge_prescription')
            labs=sum(1 for y in unique if y['record_type']=='observation_group')
            summary.append(f'Admission at {a.facility}: {a.admitted_on} to {a.discharged_on or "discharge date not documented"}. Documented diagnosis: {a.diagnosis or "not documented"}. Records linked: {labs} laboratory report(s), {inpatient} inpatient treatment item(s) listed without dates or times, {discharge} discharge prescription(s). No administration events after discharge are documented.')

    warnings=['Records are caregiver-entered or caregiver-verified. Unknown dates and gaps remain unknown. No diagnosis or treatment change is inferred.']
    if 'coli' in q:warnings.append('E. coli findings in different specimens or episodes do not establish the same disease or reinfection. Susceptibility is shown only when transcribed from laboratory evidence.')
    if scope=='labs':warnings.append('Compare only matching units and specimens; printed laboratory intervals are not assumed to be age-appropriate pediatric ranges.')
    if antibiotic_intent:warnings.append('"Antibiotic" matches come from a fixed word list of medicine names; the app does not classify drugs or recommend changes, doses or IV treatment.')
    if len(unique)>100:warnings.append('More than 100 matching records; narrow the question to see remaining evidence.')
    text='\n\n'.join(f'{x["statement"]} [{x["citation"]}]' for x in unique[:100]) or 'No matching evidence is documented. Missing records cannot establish whether a treatment or vaccination happened.'
    return {'question':question,'mode':'local_source_grounded_retrieval','summary':summary,'answer':text,'evidence':unique[:100],'comparison':comparison,'warnings':warnings}
