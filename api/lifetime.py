"""Lifetime Health Intelligence: deterministic, evidence-linked analysis of fever and antibiotic history.

Design rules (mirrors the parent's requirements):
* nothing is counted unless a record exists; every number returns the record ids that produced it;
* prescribed, scheduled, confirmed-administered and unknown-status courses are never mixed;
* an antibiotic appearing only in a culture susceptibility table is not an exposure;
* fever is never inferred from an antipyretic or antibiotic prescription;
* all totals are "documented in available records", never a claimed lifetime total.
"""
import json
import re
from collections import defaultdict
from datetime import date, timedelta
from .models import Document, Medicine, MedicationEvent, Observation, Admission, Episode, HospitalVisit, TreatmentAdministration, Patient, utcnow
from .lifetime_models import FeverEpisode, AntibioticCourse, AntibioticAdministration, LifetimeEvidence, ParentHealthEntry
from .crypto import decrypt
from .extraction import pages

# --------------------------------------------------------------------------------------
# Drug knowledge: active ingredient <- Indian brand names / spellings. Verified equivalents only.
# --------------------------------------------------------------------------------------
ANTIBIOTICS={
 'amoxicillin':{'class':'penicillin','brands':['mox','novamox','amoxil','wymox','amoxycillin','amoxicilin']},
 'amoxicillin-clavulanate':{'class':'penicillin + beta-lactamase inhibitor','brands':['augmentin','clavam','moxclav','moxikind-cv','moxikind cv','amoxyclav','amoxiclav','augpen','advent','amoxycillin+clavulanic','amoxycillin + clavulanic','amoxicillin+clavulanic','amoxicillin + clavulanic','amoxycillin-clavulanic','amoxicillin-clavulanic','amoxycillin clavulanic','clavulanic','co-amoxiclav','mega-cv','mega cv','clavix','fightox']},
 'ampicillin':{'class':'penicillin','brands':['ampicillin','ampilin']},
 'cloxacillin':{'class':'penicillin','brands':['cloxacillin','klox']},
 'ticarcillin-clavulanate':{'class':'penicillin + beta-lactamase inhibitor','brands':['ticarcillin','timentin']},
 'piperacillin-tazobactam':{'class':'penicillin + beta-lactamase inhibitor','brands':['pipzo','piptaz','pip-taz','pip taz','zosyn','tazact','piperacillin','tazobactam','pipracil']},
 'cefixime':{'class':'third-generation cephalosporin','brands':['taxim-o','taxim o','zifi','cefi','cefix','mahacef','ziprax','suprax','omnix','omnicef-o','cefolac','fixx','cefspan','milixim']},
 'cefpodoxime':{'class':'third-generation cephalosporin','brands':['cepodem','cefoprox','monocef-o','monocef o','podocef','gudcef','cefpodoxim','doxcef','kefpod']},
 'cefuroxime':{'class':'second-generation cephalosporin','brands':['ceftum','zinnat','cefakind','zocef','altacef','furoxime']},
 'cefadroxil':{'class':'first-generation cephalosporin','brands':['droxyl','cefadrox','odoxil','kefloxin']},
 'cephalexin':{'class':'first-generation cephalosporin','brands':['sporidex','phexin','cephalexin','cefalexin']},
 'cefotaxime':{'class':'third-generation cephalosporin','brands':['taxim','claforan','cefotaxim','omnatax']},
 'ceftriaxone':{'class':'third-generation cephalosporin','brands':['monocef','rocephin','oframax','ceftriaxon','xone','monotax','intacef']},
 'ceftazidime':{'class':'third-generation cephalosporin','brands':['fortum','ceftazidim','tazicef']},
 'cefoperazone-sulbactam':{'class':'third-generation cephalosporin + inhibitor','brands':['magnex','cefoperazone','sulbactam','zostum','kefbactum']},
 'cefepime':{'class':'fourth-generation cephalosporin','brands':['cefepime','maxipime','kefage']},
 'azithromycin':{'class':'macrolide','brands':['azee','azithral','zithromax','azax','aziwok','azithro','azithromycine','atm']},
 'clarithromycin':{'class':'macrolide','brands':['clarithromycin','claribid','crixan']},
 'erythromycin':{'class':'macrolide','brands':['erythromycin','althrocin','erythrocin']},
 'amikacin':{'class':'aminoglycoside','brands':['mikacin','amicin','amikacin','amikin','amika','amikacine','omnikacin']},
 'gentamicin':{'class':'aminoglycoside','brands':['gentamicin','genticyn','garamycin','gentamycin']},
 'meropenem':{'class':'carbapenem','brands':['meronem','merocrit','meropenem','mero','merotrol','meromer','penmer']},
 'imipenem':{'class':'carbapenem','brands':['imipenem','cilastatin','imicrit']},
 'ertapenem':{'class':'carbapenem','brands':['ertapenem','invanz']},
 'faropenem':{'class':'penem','brands':['farobact','faronem','faropenem']},
 'metronidazole':{'class':'nitroimidazole','brands':['metrogyl','flagyl','metronidazole','metron','aristogyl']},
 'ornidazole':{'class':'nitroimidazole','brands':['ornidazole','orni','dazolic']},
 'ofloxacin':{'class':'fluoroquinolone','brands':['oflox','zanocin','ofloxacin','zenflox','oflomac']},
 'ofloxacin-ornidazole':{'class':'fluoroquinolone + nitroimidazole','brands':['o2','oflox-oz','oflox oz','zanocin-oz','ornof','ofo','oflomac-oz']},
 'ciprofloxacin':{'class':'fluoroquinolone','brands':['ciplox','cifran','ciprofloxacin','cipro']},
 'levofloxacin':{'class':'fluoroquinolone','brands':['levoflox','levofloxacin','levoday','glevo']},
 'norfloxacin':{'class':'fluoroquinolone','brands':['norflox','norfloxacin']},
 'nalidixic acid':{'class':'quinolone','brands':['nalidixic','gramoneg']},
 'cotrimoxazole':{'class':'sulfonamide + trimethoprim','brands':['septran','bactrim','cotrimoxazole','co-trimoxazole','sulfamethoxazole','trimethoprim','oriprim']},
 'nitrofurantoin':{'class':'nitrofuran','brands':['nitrofurantoin','niftran','furadantin','martifur']},
 'doxycycline':{'class':'tetracycline','brands':['doxycycline','doxy','doxt']},
 'linezolid':{'class':'oxazolidinone','brands':['linezolid','linospan','lizolid']},
 'vancomycin':{'class':'glycopeptide','brands':['vancomycin','vancocin']},
 'teicoplanin':{'class':'glycopeptide','brands':['teicoplanin','targocid']},
 'clindamycin':{'class':'lincosamide','brands':['clindamycin','dalacin','clincin']},
 'colistin':{'class':'polymyxin','brands':['colistin','colistimethate','xylistin']},
 'polymyxin b':{'class':'polymyxin','brands':['polymyxin']},
 'rifaximin':{'class':'rifamycin (gut-selective)','brands':['rifaximin','rifagut','rcifax']},
 'chloramphenicol':{'class':'amphenicol','brands':['chloramphenicol','chloromycetin']},
 'tigecycline':{'class':'glycylcycline','brands':['tigecycline','tygacil']},
 'aztreonam':{'class':'monobactam','brands':['aztreonam','azteronam','azactam']},
 'fosfomycin':{'class':'phosphonic acid','brands':['fosfomycin','fosirol']},
 'tobramycin':{'class':'aminoglycoside','brands':['tobramycin']},
 'netilmicin':{'class':'aminoglycoside','brands':['netilmicin','netimicin','netromycin']},
}
NON_ANTIBIOTICS={
 'paracetamol':['pcm','crocin','calpol','dolo','p-250','p 250','febrex','fevago','flucold','t98','pacimol','fepanil','macfast','paracetamol','acetaminophen','tylenol','metacin','babygesic','nise'],
 'ibuprofen':['ibugesic','brufen','ibuprofen','advil'],
 'mefenamic acid':['meftal','meftal-p','meftal p','ponstan','mefenamic','mefkind','meftagesic'],
 'ondansetron':['emeset','ondem','ondansetron','vomikind','zofran','vomiz'],
 'domperidone':['domstal','domperidone','vomistop'],
 'ranitidine':['rantac','ranitidine','zinetac','aciloc'],
 'pantoprazole':['pantop','pantoprazole','pan-d','pan d','pan 40'],
 'zinc':['zn20','zn 20','zinc','zincovit','zinconia','z&d','zevit','zinkoral'],
 'probiotic':['tuffpro','tufpro','enterogermina','enteromax','econorm','vizylac','bifilac','darolac','sporlac','probiotic','lactobacillus','saccharomyces','velgut','gutpro'],
 'oral rehydration salts':['ors','who-ors','electral','enerzal','prolyte','pedialyte'],
 'racecadotril':['redotil','racecadotril','zedott','enuff'],
 'intravenous fluids':['ivf','iv fluid','dns','rl','ringer','normal saline','isolyte','nsal'],
 'enema':['neotomic','enema','glycerine'],
 'vitamin d':['calcshine','calshine','d3','vitamin d','uprise','arachitol','calcirol','cholecalciferol','d-rise'],
 'calcium':['calcium','shelcal','ostocalcium','calcimax'],
 'iron':['iron','ferium','orofer','tonoferon','dexorange','ferrous'],
 'multivitamin':['multivitamin','zincovit','polybion','becosules','a to z','supradyn','visyneral','vitcofol'],
 'antihistamine':['cetirizine','levocetirizine','levolet','alerid','okacet','levocet','allegra','fexofenadine','chlorpheniramine','t-minic','sinarest','ambrodil','phenergan','promethazine','avil'],
 'cough/cold':['ascoril','ambroxol','grilinctus','tusq','bro-zedex','salbutamol','asthalin','levolin','budecort','budesonide','nasivion','otrivin','saline drops','nasoclear'],
 'antispasmodic':['cyclopam','colicaid','colimex','dicyclomine','simethicone','bonnisan','gripe'],
 'lactulose':['duphalac','lactulose','looz','cremaffin'],
 'antacid':['digene','mucaine','gelusil','sucralfate','sucrafil'],
 'vitamin k':['vitamin k','vit k','phytonadione','kenadion'],
 'montelukast':['montelukast','montair','montek'],
 'deworming':['albendazole','zentel','mebendazole','bandy'],
 'nasal/eye drops':['moxiflox','tobra','ciplox eye','gentamicin eye','ofloxacin eye'],  # topical only when stated
}
ANTIPYRETICS={'paracetamol','ibuprofen','mefenamic acid'}
FEVER_RE=re.compile(r'\b(fever|febrile|pyrexia|pyrexial|high[- ]grade|low[- ]grade|hyperthermia|bukhar|temp(?:erature)?\.?\s*(?:of|was|is|:)?\s*\d{2,3}(?:\.\d)?\s*°?\s*[fc]?)\b',re.I)
TEMP_RE=re.compile(r'(\d{2,3}(?:\.\d)?)\s*°?\s*([fc])\b',re.I)
DURATION_RE=re.compile(r'(?:fever|febrile|pyrexia)\s*(?:x|×|for|since|of)?\s*(\d{1,2})\s*(day|days|d|wk|week|weeks|hr|hrs|hours)',re.I)
WORD_NUM={'one':1,'two':2,'three':3,'four':4,'five':5,'six':6,'seven':7,'eight':8,'nine':9,'ten':10}
DURATION_WORD_RE=re.compile(r'(?:fever|febrile|pyrexia)\s*(?:x|×|for|since|of)?\s*(one|two|three|four|five|six|seven|eight|nine|ten)\s*(day|days|week|weeks)',re.I)
_ALIAS=[]
for generic,info in ANTIBIOTICS.items():
    for alias in [generic]+info['brands']:_ALIAS.append((alias.casefold(),generic))
_ALIAS.sort(key=lambda x:-len(x[0]))
_NONAB=[]
for generic,aliases in NON_ANTIBIOTICS.items():
    for alias in aliases:_NONAB.append((alias.casefold(),generic))
_NONAB.sort(key=lambda x:-len(x[0]))

def _word_hit(alias,text):
    # Short aliases (o2, mox, atm, rl, ors) must be whole tokens; longer aliases may be prefixes (e.g. "augmentin-duo").
    if len(alias)<=4:return re.search(r'(?<![a-z0-9])'+re.escape(alias)+r'(?![a-z0-9])',text) is not None
    return re.search(r'(?<![a-z])'+re.escape(alias),text) is not None

def classify_drug(name):
    """Return the verified active ingredient for a written medicine name, or an explicit non-antibiotic / unknown verdict."""
    t=(name or '').casefold().strip()
    if not t:return {'input':name,'is_antibiotic':None,'generic':'','class':'','note':'empty name'}
    for alias,generic in _NONAB:
        if _word_hit(alias,t):
            return {'input':name,'is_antibiotic':False,'generic':generic,'class':'non-antibiotic','is_antipyretic':generic in ANTIPYRETICS,'matched':alias}
    for alias,generic in _ALIAS:
        if _word_hit(alias,t):
            return {'input':name,'is_antibiotic':True,'generic':generic,'class':ANTIBIOTICS[generic]['class'],'is_antipyretic':False,'matched':alias,'brand_recognized':alias!=generic}
    return {'input':name,'is_antibiotic':None,'generic':'','class':'','is_antipyretic':False,'note':'not in the verified ingredient table; classify manually'}

def normalize_generic(name):
    verdict=classify_drug(name)
    return verdict['generic'] if verdict['is_antibiotic'] else ''

def fever_mentions(text):
    """Verbatim fever / temperature phrases found in transcribed document text (candidates only)."""
    out=[]
    for line in text.splitlines():
        if FEVER_RE.search(line) or TEMP_RE.search(line) and re.search(r'temp|fever|°',line,re.I):
            temps=[f'{m[1]} {m[2].upper()}' for m in TEMP_RE.finditer(line) if 93<=float(m[1])<=108 or 35<=float(m[1])<=42.5]
            duration=None
            m=DURATION_RE.search(line) or DURATION_WORD_RE.search(line)
            if m:
                n=m[1].casefold();days=WORD_NUM.get(n) or int(n);unit=m[2].casefold()
                duration={'text':m[0].strip(),'days':days*7 if unit.startswith('w') else None if unit.startswith('h') else days}
            out.append({'line':line.strip()[:300],'temperatures':temps,'duration':duration})
    return out

def antibiotic_mentions(text):
    out=[]
    for line in text.splitlines():
        t=line.casefold()
        hits=[]
        for alias,generic in _ALIAS:
            if _word_hit(alias,t) and generic not in [h['generic'] for h in hits]:hits.append({'alias':alias,'generic':generic})
        if hits:
            bare=re.fullmatch(r'\s*[a-z][a-z0-9 \-/+().]*?\s*[:\-]?\s*\(?(s|r|i)\)?\s*',t)
            words=re.search(r'\b(sensitive|resistant|intermediate|susceptib|mic\b|zone|esbl)\b',t)
            dose=re.search(r'\b(tab|cap|syp|syrup|inj|iv|im|od|bd|tds|qid|sos|hs|mg|ml|days?|x\s*\d)\b',t)
            culture_only=bool(bare or words) and not dose
            out.append({'line':line.strip()[:300],'antibiotics':hits,'context':'culture_susceptibility_list' if culture_only else 'prescription_or_note'})
    return out

def scan_documents(db,pid):
    """Candidate fever and antibiotic mentions from every transcribed page. Candidates are unverified."""
    rows=[]
    for d in db.query(Document).filter_by(patient_id=pid).order_by(Document.id):
        text=decrypt(d.extracted_cipher.encode()).decode() if d.extracted_cipher else ''
        for page,body in pages(text):
            fev=fever_mentions(body);abx=antibiotic_mentions(body)
            if fev or abx:rows.append({'document_id':d.id,'title':d.title,'source_ref':getattr(d,'source_ref',None),'category':d.category,'record_date':d.record_date.isoformat() if d.record_date else None,'page':page,'fever_mentions':fev,'antibiotic_mentions':abx})
    return rows

# --------------------------------------------------------------------------------------
# Serialization helpers
# --------------------------------------------------------------------------------------
def row(obj):
    out={}
    for c in obj.__table__.columns:
        v=getattr(obj,c.name)
        out[c.name]=v.isoformat() if hasattr(v,'isoformat') else v
    return out

def evidence_for(db,pid,entity,ids):
    if not ids:return {}
    grouped=defaultdict(list)
    for e in db.query(LifetimeEvidence).filter(LifetimeEvidence.patient_id==pid,LifetimeEvidence.entity==entity,LifetimeEvidence.entity_id.in_(list(ids))).order_by(LifetimeEvidence.observed_on,LifetimeEvidence.id):
        grouped[e.entity_id].append(row(e))
    return grouped

def doc_index(db,pid):
    return {d.id:{'id':d.id,'title':d.title,'category':d.category,'record_date':d.record_date.isoformat() if d.record_date else None,'source_ref':getattr(d,'source_ref',None),'page_count':d.page_count} for d in db.query(Document).filter_by(patient_id=pid)}

def _dates(a,b):
    return [a+timedelta(days=i) for i in range((b-a).days+1)] if a and b and b>=a else []

def included(status,scope):
    return status!='rejected' and (scope=='all' or status=='verified')

# --------------------------------------------------------------------------------------
# Fever
# --------------------------------------------------------------------------------------
def fever_report(db,pid,scope='all'):
    p=db.get(Patient,pid)
    episodes=[f for f in db.query(FeverEpisode).filter_by(patient_id=pid).order_by(FeverEpisode.first_documented_on.nullslast(),FeverEpisode.id) if included(f.verification_status,scope)]
    ev=evidence_for(db,pid,'fever_episode',[f.id for f in episodes])
    courses=[c for c in db.query(AntibioticCourse).filter_by(patient_id=pid) if included(c.verification_status,scope)]
    items=[];days=set();uncertain=[];hospitalized=[]
    for f in episodes:
        linked=[c for c in courses if not c.continuation_of_id and (c.fever_episode_id==f.id or (f.illness_episode_id and c.illness_episode_id==f.illness_episode_id))]
        if not linked and f.first_documented_on:
            window_start=f.first_documented_on-timedelta(days=2);window_end=(f.last_documented_on or f.first_documented_on)+timedelta(days=7)
            linked=[c for c in courses if (c.prescribed_on or c.planned_start or c.actual_start) and window_start<=(c.prescribed_on or c.planned_start or c.actual_start)<=window_end]
        span=_dates(f.first_documented_on,f.last_documented_on or f.first_documented_on)
        for d in span:days.add(d.isoformat())
        documented_days=f.documented_fever_days if f.documented_fever_days is not None else (len(span) if span and f.date_certainty=='exact' else None)
        if f.date_certainty!='exact' or not f.first_documented_on:uncertain.append(f.id)
        if f.hospitalized:hospitalized.append(f.id)
        items.append({**row(f),'documented_duration_days':documented_days,'calendar_days':[d.isoformat() for d in span],'antibiotics_during_episode':[{'id':c.id,'generic_name':c.generic_name,'route':c.route,'administration_status':c.administration_status,'verification_status':c.verification_status,'link':'explicit' if (c.fever_episode_id==f.id or (f.illness_episode_id and c.illness_episode_id==f.illness_episode_id)) else 'date_window_only'} for c in linked],'evidence':ev.get(f.id,[]),'origin_label':'Parent-reported' if f.origin=='parent_report' else 'Clinician-documented'})
    temps=[(float(re.sub(r'[^0-9.]','',i['highest_temperature'] or '0') or 0),i['temperature_unit'],i['id']) for i in items if i['highest_temperature']]
    highest=None
    if temps:
        f_temps=[t for t in temps if t[1]=='F'];c_temps=[t for t in temps if t[1]=='C']
        best=max(f_temps) if f_temps else None;bestc=max(c_temps) if c_temps else None
        if best and bestc:
            best=best if best[0]>=bestc[0]*9/5+32 else bestc
        else:best=best or bestc
        highest={'value':best[0],'unit':best[1],'episode_id':best[2]}
    longest=max(([i['documented_duration_days'] or 0,i['id']] for i in items),default=None)
    return {'patient':{'dob':p.dob.isoformat(),'today':utcnow().date().isoformat()},'scope':scope,'episodes':items,'metrics':{
        'documented_episodes':{'value':len(items),'contributors':[i['id'] for i in items]},
        'episodes_with_uncertain_dates':{'value':len(uncertain),'contributors':uncertain},
        'hospitalized_episodes':{'value':len(hospitalized),'contributors':hospitalized},
        'documented_fever_calendar_days':{'value':len(days),'contributors':sorted(days)},
        'highest_recorded_temperature':highest,
        'longest_documented_episode':{'days':longest[0],'episode_id':longest[1]} if longest and longest[0] else None,
        'parent_reported_episodes':{'value':sum(1 for i in items if i['origin']=='parent_report'),'contributors':[i['id'] for i in items if i['origin']=='parent_report']},
        'pending_verification':{'value':sum(1 for i in items if i['verification_status']=='needs_verification'),'contributors':[i['id'] for i in items if i['verification_status']=='needs_verification']}},
        'calendar_days':sorted(days),
        'warnings':['Episodes are documented in the available records only; undocumented fevers at home are not represented.','Fever was never inferred from an antibiotic or antipyretic prescription.','Episodes marked approximate or unknown keep the duration exactly as written (for example "fever for three days").']}

# --------------------------------------------------------------------------------------
# Antibiotics
# --------------------------------------------------------------------------------------
def course_exposure(course,administrations):
    """Dates on which administration is actually documented for one course. Unknown stays unknown."""
    dates=set()
    if course.actual_start and course.actual_stop and course.administration_status=='confirmed_administered':
        dates.update(_dates(course.actual_start,course.actual_stop))
    elif course.actual_start and course.administered_duration_days and course.administration_status=='confirmed_administered':
        dates.update(_dates(course.actual_start,course.actual_start+timedelta(days=course.administered_duration_days-1)))
    for a in administrations:dates.add(a.administered_on)
    return dates

def antibiotic_report(db,pid,scope='all'):
    courses=[c for c in db.query(AntibioticCourse).filter_by(patient_id=pid).order_by(AntibioticCourse.prescribed_on.nullslast(),AntibioticCourse.planned_start.nullslast(),AntibioticCourse.id) if included(c.verification_status,scope)]
    admins=defaultdict(list)
    for a in db.query(AntibioticAdministration).filter_by(patient_id=pid).order_by(AntibioticAdministration.administered_on):admins[a.course_id].append(a)
    ev=evidence_for(db,pid,'antibiotic_course',[c.id for c in courses])
    episodes={e.id:e for e in db.query(Episode).filter_by(patient_id=pid)}
    fevers={f.id:f for f in db.query(FeverEpisode).filter_by(patient_id=pid)}
    unclassified=[c for c in courses if c.drug_class=='unclassified' or classify_drug(c.generic_name)['is_antibiotic'] is not True]
    received=[c for c in courses if c.administration_status!='not_received' and c not in unclassified]
    primary=[c for c in received if not c.continuation_of_id]  # renewals / IV->oral switches hang off a primary course
    admissions={a.id:a for a in db.query(Admission).filter_by(patient_id=pid)}
    by_generic=defaultdict(list)
    for c in received:by_generic[c.generic_name].append(c)
    # Metric C: exposure days, per antibiotic and overall calendar union
    per_drug_days={};overall=set();per_course_days={};bounded=[]
    for c in received:
        d=course_exposure(c,admins.get(c.id,[]))
        per_course_days[c.id]=sorted(x.isoformat() for x in d)
        if d:
            per_drug_days.setdefault(c.generic_name,set()).update(d);overall.update(d)
        elif c.administration_status=='confirmed_administered' and c.admission_id in admissions and admissions[c.admission_id].discharged_on:
            a=admissions[c.admission_id];span=_dates(a.admitted_on,a.discharged_on)
            bounded.append({'course_id':c.id,'generic_name':c.generic_name,'admission_id':a.id,'window_start':a.admitted_on.isoformat(),'window_end':a.discharged_on.isoformat(),'at_most_days':len(span),'note':'Administration is stated for the stay but no dated chart is attached; the stay length is an upper bound, not a confirmed count.'})
    status_groups={'prescribed_only':[],'scheduled':[],'confirmed_administered':[],'unknown':[],'not_received':[]}
    for c in courses:status_groups.setdefault(c.administration_status,[]).append(c.id)
    # Metric D
    admin_count=sum(len(v) for k,v in admins.items() if k in {c.id for c in received})
    # Charts
    monthly=defaultdict(lambda:defaultdict(int))
    for c in primary:
        when=c.prescribed_on or c.planned_start or c.actual_start
        if when:monthly[when.strftime('%Y-%m')][c.generic_name]+=1
    route_split=defaultdict(lambda:{'courses':0,'exposure_days':0})
    for c in primary:
        route_split[c.route]['courses']+=1
    for c in received:
        route_split[c.route]['exposure_days']+=len(per_course_days[c.id])
    # Duplicate / review flags
    flags=[]
    seen={}
    for c in courses:
        key=(c.generic_name,(c.prescribed_on or c.planned_start or c.actual_start),c.route)
        if key in seen and not c.continuation_of_id:flags.append({'type':'possible_duplicate_prescription','course_ids':[seen[key],c.id],'detail':f'{c.generic_name} with the same date and route appears twice; keep one or mark one as a continuation.'})
        seen.setdefault(key,c.id)
        if c.administration_status=='confirmed_administered' and not (c.actual_start or admins.get(c.id)):flags.append({'type':'confirmed_without_dates','course_ids':[c.id],'detail':'Marked administered but no actual start date or administration record is attached.'})
        if c.administration_status in ('prescribed_only','scheduled','unknown') and not c.prescribed_on and not c.planned_start:flags.append({'type':'undated_prescription','course_ids':[c.id],'detail':'No prescription or start date is legible; the course cannot be placed on the timeline.'})
        if c.uncertain_fields:flags.append({'type':'uncertain_transcription','course_ids':[c.id],'detail':'Fields needing the original page: '+c.uncertain_fields})
        verdict=classify_drug(c.generic_name)
        if verdict['is_antibiotic'] is not True or verdict['generic']!=c.generic_name:flags.append({'type':'classification_check','course_ids':[c.id],'detail':f'"{c.generic_name}" is not a recognised active ingredient in the verified table.'})
    items=[]
    for c in courses:
        items.append({**row(c),'exposure_days':per_course_days.get(c.id,[]),'administrations':[row(a) for a in admins.get(c.id,[])],'evidence':ev.get(c.id,[]),'illness':episodes[c.illness_episode_id].title if c.illness_episode_id in episodes else None,'fever_episode_title':fevers[c.fever_episode_id].title if c.fever_episode_id in fevers else None,'counted_as_separate_course':(not c.continuation_of_id) and c.administration_status!='not_received' and c not in unclassified,'unclassified':c in unclassified,'administration_evidence':'dated' if per_course_days.get(c.id) else ('statement_only' if c.administration_status=='confirmed_administered' else 'none'),'origin_label':'Parent-reported' if c.origin=='parent_report' else 'Clinician-documented'})
    distinct=sorted(by_generic)
    return {'scope':scope,'courses':items,'metrics':{
        'A_distinct_antibiotics':{'value':len(distinct),'contributors':[{'generic_name':g,'course_ids':[c.id for c in by_generic[g]],'drug_class':ANTIBIOTICS.get(g,{}).get('class','')} for g in distinct]},
        'B_antibiotic_courses':{'value':len(primary),'contributors':[c.id for c in primary],'continuations_merged':[c.id for c in received if c.continuation_of_id]},
        'C_exposure_days':{'overall_calendar_days':len(overall),'overall_dates':sorted(x.isoformat() for x in overall),'per_antibiotic':{g:{'days':len(d),'dates':sorted(x.isoformat() for x in d)} for g,d in per_drug_days.items()},'courses_without_documented_days':[c.id for c in received if not per_course_days[c.id]],'undated_inpatient_windows':bounded},
        'unclassified_candidates':{'value':len(unclassified),'contributors':[c.id for c in unclassified],'note':'Illegible or unrecognised drug names are listed for verification and excluded from every antibiotic count.'},
        'D_documented_administrations':{'value':admin_count,'contributors':[a.id for k,v in admins.items() for a in v if k in {c.id for c in received}]},
        'E_status_breakdown':{k:{'value':len(v),'contributors':v} for k,v in status_groups.items()},
        'courses_per_antibiotic':{g:len([c for c in by_generic[g] if not c.continuation_of_id]) for g in distinct},
        'prescribed_more_than_once':[g for g in distinct if len([c for c in by_generic[g] if not c.continuation_of_id])>1],
        'by_month':{m:dict(v) for m,v in sorted(monthly.items())},
        'route_split':dict(route_split),
        'pending_verification':{'value':sum(1 for c in courses if c.verification_status=='needs_verification'),'contributors':[c.id for c in courses if c.verification_status=='needs_verification']}},
        'review_flags':flags,
        'warnings':['Counts are documented in available records; the lifetime total cannot be established while prescriptions or administration charts are missing.','A prescription does not prove administration. Exposure days use only confirmed administration dates.','Antibiotics listed only in culture susceptibility reports are excluded from every count.','No judgement about appropriateness or overuse is made.']}

# --------------------------------------------------------------------------------------
# Illness-by-illness, E. coli, correlation
# --------------------------------------------------------------------------------------
def _in_window(d,start,end,pad=3):
    return d is not None and start is not None and (start-timedelta(days=pad))<=d<=((end or start)+timedelta(days=pad+14 if end is None else pad))

def illness_report(db,pid,scope='all'):
    eps=db.query(Episode).filter_by(patient_id=pid).order_by(Episode.started_on).all()
    fevers=[f for f in db.query(FeverEpisode).filter_by(patient_id=pid) if included(f.verification_status,scope)]
    courses=[c for c in db.query(AntibioticCourse).filter_by(patient_id=pid) if included(c.verification_status,scope)]
    admins=defaultdict(list)
    for a in db.query(AntibioticAdministration).filter_by(patient_id=pid):admins[a.course_id].append(a)
    labs=db.query(Observation).filter_by(patient_id=pid).order_by(Observation.record_date).all()
    admissions=db.query(Admission).filter_by(patient_id=pid).all()
    visits=db.query(HospitalVisit).filter_by(patient_id=pid).all()
    docs=db.query(Document).filter_by(patient_id=pid).all()
    out=[];used_f=set();used_c=set()
    for e in eps:
        links=json.loads(e.links_json or '[]')
        linked_obs={l['id'] for l in links if l['kind']=='observation'};linked_med={l['id'] for l in links if l['kind']=='medicine'};linked_adm={l['id'] for l in links if l['kind']=='admission'};linked_doc={l['id'] for l in links if l['kind']=='document'}
        f_list=[f for f in fevers if f.illness_episode_id==e.id or (f.illness_episode_id is None and _in_window(f.first_documented_on,e.started_on,e.ended_on))]
        c_list=[c for c in courses if c.illness_episode_id==e.id or c.medicine_id in linked_med or (c.illness_episode_id is None and _in_window(c.prescribed_on or c.planned_start or c.actual_start,e.started_on,e.ended_on))]
        used_f.update(f.id for f in f_list);used_c.update(c.id for c in c_list)
        o_list=[o for o in labs if o.id in linked_obs or _in_window(o.record_date,e.started_on,e.ended_on)]
        a_list=[a for a in admissions if a.id in linked_adm or _in_window(a.admitted_on,e.started_on,e.ended_on,0)]
        end=e.ended_on or e.started_on
        follow=[v for v in visits if end<=v.planned_at.date()<=end+timedelta(days=21)]+[d for d in docs if d.record_date and end<d.record_date<=end+timedelta(days=21) and d.category in ('prescription','consultation','lab','culture')]
        out.append({'episode':{**row(e),'links':links},'fever':[{'id':f.id,'title':f.title,'first_documented_on':f.first_documented_on.isoformat() if f.first_documented_on else None,'last_documented_on':f.last_documented_on.isoformat() if f.last_documented_on else None,'duration_text':f.duration_text,'highest_temperature':f.highest_temperature,'temperature_unit':f.temperature_unit,'verification_status':f.verification_status,'origin':f.origin} for f in f_list],
            'laboratory':[{'id':o.id,'record_date':o.record_date.isoformat(),'test_name':o.test_name,'value':o.value,'unit':o.unit,'specimen':o.specimen,'organism':o.organism,'susceptibility':o.susceptibility,'verified':o.verified,'source_document_id':o.source_document_id,'source_page':o.source_page} for o in o_list],
            'documented_diagnosis':e.documented_diagnosis,'symptoms':e.symptoms,
            'prescribed_antibiotics':[{'id':c.id,'generic_name':c.generic_name,'brand_name':c.brand_name,'route':c.route,'administration_status':c.administration_status,'prescribed_on':c.prescribed_on.isoformat() if c.prescribed_on else None,'setting':c.setting,'verification_status':c.verification_status} for c in c_list],
            'confirmed_administered':[{'id':c.id,'generic_name':c.generic_name,'route':c.route,'actual_start':c.actual_start.isoformat() if c.actual_start else None,'actual_stop':c.actual_stop.isoformat() if c.actual_stop else None,'administrations':len(admins.get(c.id,[]))} for c in c_list if c.administration_status=='confirmed_administered'],
            'hospitalization':[{'id':a.id,'facility':a.facility,'admitted_on':a.admitted_on.isoformat(),'discharged_on':a.discharged_on.isoformat() if a.discharged_on else None,'diagnosis':a.diagnosis,'source_document_id':a.source_document_id} for a in a_list],
            'follow_up':[{'kind':'visit','id':v.id,'date':v.planned_at.date().isoformat(),'title':v.purpose,'status':v.status} if isinstance(v,HospitalVisit) else {'kind':'document','id':v.id,'date':v.record_date.isoformat(),'title':v.title,'category':v.category} for v in follow],
            'linked_documents':sorted(linked_doc)})
    unassigned={'fever_episodes':[{'id':f.id,'title':f.title,'first_documented_on':f.first_documented_on.isoformat() if f.first_documented_on else None} for f in fevers if f.id not in used_f],'antibiotic_courses':[{'id':c.id,'generic_name':c.generic_name,'prescribed_on':c.prescribed_on.isoformat() if c.prescribed_on else None} for c in courses if c.id not in used_c]}
    return {'scope':scope,'illnesses':out,'unassigned':unassigned,'warnings':['Records are grouped by explicit links first and by overlapping dates second; a date overlap does not prove the same illness.','Laboratory findings (for example a positive stool culture) are listed separately from clinician-documented diagnoses.']}

def _parse_susceptibility(text):
    s,r,i=[],[],[]
    for part in re.split(r'[;,\n|]+',text or ''):
        m=re.match(r'\s*([A-Za-z][A-Za-z0-9 \-/+()]+?)\s*[:=\-]?\s*\(?\b(S|R|I|sensitive|resistant|intermediate|susceptible)\b\)?\s*$',part.strip(),re.I)
        if not m:continue
        name=m[1].strip();verdict=m[2][0].upper()
        generic=normalize_generic(name) or name.casefold()
        (s if verdict=='S' else r if verdict=='R' else i).append(generic)
    return {'susceptible':s,'resistant':r,'intermediate':i}

def ecoli_report(db,pid,scope='all'):
    obs=[o for o in db.query(Observation).filter_by(patient_id=pid).order_by(Observation.record_date) if 'coli' in (o.organism+' '+o.culture_findings+' '+o.test_name+' '+o.value).casefold()]
    courses=[c for c in db.query(AntibioticCourse).filter_by(patient_id=pid) if included(c.verification_status,scope) and c.administration_status!='not_received']
    fevers=[f for f in db.query(FeverEpisode).filter_by(patient_id=pid) if included(f.verification_status,scope)]
    items=[]
    for o in obs:
        specimen=(o.specimen or 'unknown').casefold()
        positive=bool(re.search(r'\b(growth|isolated|positive|grown|significant)\b',(o.culture_findings+' '+o.value).casefold())) and not re.search(r'\bno (aerobic )?(growth|organism)\b',(o.culture_findings+' '+o.value).casefold())
        sus=_parse_susceptibility(o.susceptibility)
        near=[c for c in courses if (c.prescribed_on or c.planned_start or c.actual_start) and o.record_date-timedelta(days=5)<=(c.prescribed_on or c.planned_start or c.actual_start)<=o.record_date+timedelta(days=21)]
        comparison=[]
        for c in near:
            g=c.generic_name;parts=set(g.split('-'))
            verdict='not tested'
            if g in sus['susceptible'] or parts&set(sus['susceptible']):verdict='reported susceptible'
            if g in sus['resistant'] or parts&set(sus['resistant']):verdict='reported resistant'
            if g in sus['intermediate'] or parts&set(sus['intermediate']):verdict='reported intermediate'
            comparison.append({'course_id':c.id,'generic_name':g,'route':c.route,'administration_status':c.administration_status,'prescribed_on':(c.prescribed_on or c.planned_start or c.actual_start).isoformat(),'laboratory_report_says':verdict})
        fever_near=[{'id':f.id,'title':f.title,'first_documented_on':f.first_documented_on.isoformat() if f.first_documented_on else None} for f in fevers if f.first_documented_on and abs((f.first_documented_on-o.record_date).days)<=14]
        items.append({'observation':{'id':o.id,'record_date':o.record_date.isoformat(),'test_name':o.test_name,'specimen':specimen,'organism':o.organism,'culture_findings':o.culture_findings,'susceptibility_text':o.susceptibility,'verified':o.verified,'source_document_id':o.source_document_id,'source_page':o.source_page},'specimen_type':specimen,'laboratory_finding':'positive culture' if positive else 'reported finding (see text)','clinical_diagnosis_documented':False,'parsed_susceptibility':sus,'antibiotics_prescribed_or_administered_within_window':comparison,'fever_episodes_within_14_days':fever_near})
    # Clinician-documented diagnoses mentioning E. coli are a different category from a laboratory finding.
    diag=[{'kind':'episode','id':e.id,'title':e.title,'documented_diagnosis':e.documented_diagnosis,'started_on':e.started_on.isoformat()} for e in db.query(Episode).filter_by(patient_id=pid) if 'coli' in (e.documented_diagnosis or '').casefold()]
    diag+=[{'kind':'admission','id':a.id,'facility':a.facility,'documented_diagnosis':a.diagnosis,'admitted_on':a.admitted_on.isoformat()} for a in db.query(Admission).filter_by(patient_id=pid) if 'coli' in (a.diagnosis or '').casefold()]
    return {'scope':scope,'laboratory_findings':items,'clinician_documented_diagnoses_mentioning_ecoli':diag,'specimen_summary':{k:len([i for i in items if i['specimen_type']==k]) for k in {i['specimen_type'] for i in items}},
        'warnings':['Positive cultures from different specimens or dates do not by themselves establish separate infections, reinfection or colonisation.','The susceptibility comparison is a side-by-side reading of the laboratory report and the prescription. It is not a treatment recommendation.']}

def correlation_report(db,pid,scope='all'):
    fevers=[f for f in db.query(FeverEpisode).filter_by(patient_id=pid) if included(f.verification_status,scope)]
    courses=[c for c in db.query(AntibioticCourse).filter_by(patient_id=pid) if included(c.verification_status,scope) and c.administration_status!='not_received']
    admins=db.query(AntibioticAdministration).filter_by(patient_id=pid).all()
    labs=db.query(Observation).filter_by(patient_id=pid).all()
    docs=db.query(Document).filter_by(patient_id=pid).all()
    visits=db.query(HospitalVisit).filter_by(patient_id=pid).all()
    events=[]
    for f in fevers:
        if f.first_documented_on:events.append({'date':f.first_documented_on.isoformat(),'step':'fever_onset','title':f.title,'entity':'fever_episode','id':f.id,'certainty':f.date_certainty,'source_document_id':f.source_document_id,'source_page':f.source_page})
        if f.resolution_documented_on:events.append({'date':f.resolution_documented_on.isoformat(),'step':'fever_resolution_documented','title':f.title,'entity':'fever_episode','id':f.id,'certainty':f.date_certainty,'source_document_id':f.source_document_id,'source_page':f.source_page})
    for d in docs:
        if d.record_date and d.category in ('prescription','consultation'):events.append({'date':d.record_date.isoformat(),'step':'doctor_consultation','title':d.title,'entity':'document','id':d.id,'source_document_id':d.id,'source_page':1})
    for o in labs:events.append({'date':o.record_date.isoformat(),'step':'laboratory_test','title':f'{o.test_name}: {o.value} {o.unit}','entity':'observation','id':o.id,'source_document_id':o.source_document_id,'source_page':o.source_page})
    for c in courses:
        when=c.prescribed_on or c.planned_start
        if when:events.append({'date':when.isoformat(),'step':'medication_prescribed','title':f'{c.generic_name} ({c.route})','entity':'antibiotic_course','id':c.id,'status':c.administration_status,'source_document_id':c.source_document_id,'source_page':c.source_page})
        if c.actual_start and c.administration_status=='confirmed_administered':events.append({'date':c.actual_start.isoformat(),'step':'documented_administration_start','title':c.generic_name,'entity':'antibiotic_course','id':c.id,'source_document_id':c.source_document_id,'source_page':c.source_page})
    for a in admins:events.append({'date':a.administered_on.isoformat(),'step':'documented_administration','title':a.dose or 'administration','entity':'administration','id':a.id,'course_id':a.course_id,'source_document_id':a.source_document_id,'source_page':a.source_page})
    for v in visits:events.append({'date':v.planned_at.date().isoformat(),'step':'follow_up_visit','title':v.purpose,'entity':'visit','id':v.id,'status':v.status,'source_document_id':v.source_document_id,'source_page':v.source_page})
    events.sort(key=lambda x:(x['date'],x['step']))
    def near(f,c):
        start=f.first_documented_on;end=f.last_documented_on or start
        when=c.prescribed_on or c.planned_start or c.actual_start
        return start and when and start-timedelta(days=2)<=when<=end+timedelta(days=7)
    fever_without_abx=[{'id':f.id,'title':f.title,'first_documented_on':f.first_documented_on.isoformat() if f.first_documented_on else None} for f in fevers if not any(c.fever_episode_id==f.id or near(f,c) for c in courses)]
    abx_without_fever=[{'id':c.id,'generic_name':c.generic_name,'prescribed_on':(c.prescribed_on or c.planned_start or c.actual_start).isoformat() if (c.prescribed_on or c.planned_start or c.actual_start) else None} for c in courses if not any(c.fever_episode_id==f.id or near(f,c) for f in fevers)]
    return {'scope':scope,'events':events,'fever_episodes_without_documented_antibiotic':fever_without_abx,'antibiotic_courses_without_documented_fever':abx_without_fever,
        'warnings':['Only dates with actual evidence are shown; missing steps mean missing records, not absence of care.','Timing does not show that an antibiotic resolved a fever or that an infection required antibiotics.']}

# --------------------------------------------------------------------------------------
# Dashboard, review tables, summary
# --------------------------------------------------------------------------------------
def dashboard(db,pid):
    fever=fever_report(db,pid,'all');abx=antibiotic_report(db,pid,'all');ill=illness_report(db,pid,'all')
    fever_v=fever_report(db,pid,'verified');abx_v=antibiotic_report(db,pid,'verified')
    admissions=db.query(Admission).filter_by(patient_id=pid).all()
    docs=db.query(Document).filter_by(patient_id=pid).all()
    pending_parent=db.query(ParentHealthEntry).filter_by(patient_id=pid,status='active').count()
    pending=fever['metrics']['pending_verification']['value']+abx['metrics']['pending_verification']['value']
    def tile(key,label,value,verified,contributors,entity,note=''):
        return {'key':key,'label':label,'value':value,'verified_value':verified,'contributors':contributors,'entity':entity,'note':note}
    tiles=[
        tile('fever_episodes','Documented fever episodes',fever['metrics']['documented_episodes']['value'],fever_v['metrics']['documented_episodes']['value'],fever['metrics']['documented_episodes']['contributors'],'fever_episode','Clinician-documented and parent-reported episodes, labelled separately'),
        tile('distinct_antibiotics','Distinct antibiotics',abx['metrics']['A_distinct_antibiotics']['value'],abx_v['metrics']['A_distinct_antibiotics']['value'],abx['metrics']['A_distinct_antibiotics']['contributors'],'antibiotic_generic','Active ingredients; brands normalised'),
        tile('antibiotic_courses','Documented antibiotic courses',abx['metrics']['B_antibiotic_courses']['value'],abx_v['metrics']['B_antibiotic_courses']['value'],abx['metrics']['B_antibiotic_courses']['contributors'],'antibiotic_course','Renewals and IV-to-oral switches of the same agent are merged'),
        tile('exposure_days','Confirmed antibiotic-exposure days',abx['metrics']['C_exposure_days']['overall_calendar_days'],abx_v['metrics']['C_exposure_days']['overall_calendar_days'],abx['metrics']['C_exposure_days']['overall_dates'],'exposure_date','Calendar days with a documented administration; overlapping agents count once'),
        tile('hospitalizations','Hospitalizations',len(admissions),len(admissions),[a.id for a in admissions],'admission',''),
        tile('illness_episodes','Illness episodes',len(ill['illnesses']),len(ill['illnesses']),[i['episode']['id'] for i in ill['illnesses']],'episode',''),
        tile('documents','Total medical reports',len(docs),len(docs),[d.id for d in docs],'document',f'{sum(d.page_count for d in docs)} pages stored'),
        tile('pending','Pending records requiring verification',pending+pending_parent,0,{'fever_episodes':fever['metrics']['pending_verification']['contributors'],'antibiotic_courses':abx['metrics']['pending_verification']['contributors'],'parent_entries':pending_parent},'pending','Transcriptions awaiting your check against the original page')]
    return {'generated_at':utcnow().isoformat(),'tiles':tiles,'fever':fever,'antibiotics':abx,'illnesses':ill,'coverage':{'documents':len(docs),'documents_with_text':sum(1 for d in docs if d.extracted_cipher),'documents_without_record_date':sum(1 for d in docs if not d.record_date)},
        'warnings':['All numbers are documented in available records. Processing every uploaded page does not mean every lifetime episode is known.']}

def review_tables(db,pid):
    fever=fever_report(db,pid,'all');abx=antibiotic_report(db,pid,'all');docs=doc_index(db,pid)
    def link_ok(doc_id,page):
        d=docs.get(doc_id);return bool(d) and 1<=(page or 1)<=d['page_count']
    fever_rows=[{'id':f['id'],'title':f['title'],'first_documented_on':f['first_documented_on'],'last_documented_on':f['last_documented_on'],'documented_duration_days':f['documented_duration_days'],'duration_text':f['duration_text'],'date_certainty':f['date_certainty'],'highest_temperature':(f['highest_temperature']+' '+f['temperature_unit']).strip(),'hospitalized':f['hospitalized'],'origin':f['origin'],'verification_status':f['verification_status'],'source_document_id':f['source_document_id'],'source_page':f['source_page'],'source_link_valid':link_ok(f['source_document_id'],f['source_page']) if f['source_document_id'] else f['origin']=='parent_report','evidence_count':len(f['evidence']),'evidence_links_valid':all(link_ok(e['source_document_id'],e['source_page']) for e in f['evidence'] if e['source_document_id']),'needs_parent_check':f['needs_parent_check']} for f in fever['episodes']]
    abx_rows=[{'id':c['id'],'generic_name':c['generic_name'],'brand_name':c['brand_name'],'route':c['route'],'prescribed_on':c['prescribed_on'],'actual_start':c['actual_start'],'actual_stop':c['actual_stop'],'prescribed_duration':c['prescribed_duration_text'] or c['prescribed_duration_days'],'administration_status':c['administration_status'],'setting':c['setting'],'continuation_of_id':c['continuation_of_id'],'counted_as_separate_course':c['counted_as_separate_course'],'exposure_days':len(c['exposure_days']),'administrations':len(c['administrations']),'origin':c['origin'],'verification_status':c['verification_status'],'uncertain_fields':c['uncertain_fields'],'source_document_id':c['source_document_id'],'source_page':c['source_page'],'source_link_valid':link_ok(c['source_document_id'],c['source_page']) if c['source_document_id'] else c['origin']=='parent_report','evidence_count':len(c['evidence']),'evidence_links_valid':all(link_ok(e['source_document_id'],e['source_page']) for e in c['evidence'] if e['source_document_id']),'classification':classify_drug(c['generic_name'])} for c in abx['courses']]
    # fever duplicates: overlapping date spans from the same origin
    dup=[]
    eps=fever['episodes']
    for i,a in enumerate(eps):
        for b in eps[i+1:]:
            if a['first_documented_on'] and b['first_documented_on']:
                a_end=a['last_documented_on'] or a['first_documented_on'];b_end=b['last_documented_on'] or b['first_documented_on']
                if a['first_documented_on']<=b_end and b['first_documented_on']<=a_end:dup.append({'type':'overlapping_fever_episodes','ids':[a['id'],b['id']],'detail':'Date ranges overlap; merge if they describe one continuous illness, keep separate if the records show distinct illnesses.'})
    return {'fever_review_table':fever_rows,'antibiotic_review_table':abx_rows,'duplicates_and_uncertain':dup+abx['review_flags'],'source_links':{'invalid':[r for r in fever_rows+abx_rows if not r['source_link_valid'] or not r['evidence_links_valid']]}}

def summary_text(db,pid):
    p=db.get(Patient,pid);d=dashboard(db,pid);fever=d['fever'];abx=d['antibiotics'];corr=correlation_report(db,pid);ecoli=ecoli_report(db,pid)
    docs=doc_index(db,pid)
    def src(doc_id,page):
        dd=docs.get(doc_id);return f"{dd['source_ref'] or '#'+str(doc_id)} p{page}" if dd else 'no source attached'
    lines=[f'LIFETIME HEALTH SUMMARY — documented in available records (generated {utcnow().date()})',f'Child born {p.dob}; {len(docs)} medical reports stored. This is an evidence index, not a complete lifetime history or a clinical assessment.','',
        f"FEVER EPISODES DOCUMENTED: {fever['metrics']['documented_episodes']['value']} (verified {d['tiles'][0]['verified_value']}; uncertain dates {fever['metrics']['episodes_with_uncertain_dates']['value']}; parent-reported {fever['metrics']['parent_reported_episodes']['value']})"]
    for f in fever['episodes']:
        lines.append(f"- #{f['id']} {f['title']}: {f['first_documented_on'] or 'date unknown'} to {f['last_documented_on'] or 'end not documented'}; duration {f['duration_text'] or (str(f['documented_duration_days'])+' days' if f['documented_duration_days'] else 'not documented')}; highest temperature {f['highest_temperature'] or 'not recorded'} {f['temperature_unit']}; diagnosis {f['documented_diagnosis'] or 'not documented'}; hospitalised {'yes' if f['hospitalized'] else 'no'}; antipyretics {f['antipyretics'] or 'none documented'}; antibiotics in episode {', '.join(a['generic_name'] for a in f['antibiotics_during_episode']) or 'none documented'}; {f['origin_label']}; {f['verification_status']}; source {src(f['source_document_id'],f['source_page'])}; evidence pages {', '.join(sorted({src(e['source_document_id'],e['source_page']) for e in f['evidence']})) or 'none'}")
    m=abx['metrics']
    lines+=['',f"DISTINCT ANTIBIOTICS DOCUMENTED: {m['A_distinct_antibiotics']['value']} — "+', '.join(c['generic_name'] for c in m['A_distinct_antibiotics']['contributors']),
        f"ANTIBIOTIC COURSES DOCUMENTED: {m['B_antibiotic_courses']['value']} (continuations merged: {len(m['B_antibiotic_courses']['continuations_merged'])})",
        f"PRESCRIBED-ONLY {m['E_status_breakdown']['prescribed_only']['value']} · SCHEDULED {m['E_status_breakdown']['scheduled']['value']} · CONFIRMED ADMINISTERED {m['E_status_breakdown']['confirmed_administered']['value']} · UNKNOWN STATUS {m['E_status_breakdown']['unknown']['value']} · CULTURE-ONLY / NOT RECEIVED {m['E_status_breakdown']['not_received']['value']}",
        f"CONFIRMED ANTIBIOTIC-EXPOSURE DAYS: {m['C_exposure_days']['overall_calendar_days']} calendar days ({', '.join(m['C_exposure_days']['overall_dates']) or 'none documented'}); documented individual administrations: {m['D_documented_administrations']['value']}",
        f"COURSES WITHOUT DOCUMENTED ADMINISTRATION DAYS: {len(m['C_exposure_days']['courses_without_documented_days'])}"]
    for c in abx['courses']:
        lines.append(f"- #{c['id']} {c['generic_name']}{' ('+c['brand_name']+')' if c['brand_name'] else ''}: {c['route']}; {c['setting']}; prescribed {c['prescribed_on'] or 'date not documented'}; prescribed duration {c['prescribed_duration_text'] or c['prescribed_duration_days'] or 'not documented'}; actual {c['actual_start'] or 'start unknown'} to {c['actual_stop'] or 'stop unknown'}; status {c['administration_status']}; exposure days documented {len(c['exposure_days'])}; {c['origin_label']}; {c['verification_status']}; source {src(c['source_document_id'],c['source_page'])}{'; uncertain: '+c['uncertain_fields'] if c['uncertain_fields'] else ''}")
    lines+=['',f"FEVER EPISODES WITHOUT A DOCUMENTED ANTIBIOTIC: {', '.join('#'+str(x['id']) for x in corr['fever_episodes_without_documented_antibiotic']) or 'none'}",f"ANTIBIOTIC COURSES WITHOUT A DOCUMENTED FEVER: {', '.join('#'+str(x['id']) for x in corr['antibiotic_courses_without_documented_fever']) or 'none'}",'',
        f"E. COLI-RELATED LABORATORY FINDINGS: {len(ecoli['laboratory_findings'])} ({', '.join(i['observation']['record_date']+' '+i['specimen_type'] for i in ecoli['laboratory_findings']) or 'none'}); clinician-documented diagnoses mentioning E. coli: {len(ecoli['clinician_documented_diagnoses_mentioning_ecoli'])}",'',
        f"RECORDS REQUIRING PARENT VERIFICATION: {d['tiles'][7]['value']}",'','FACTS THAT CANNOT BE ESTABLISHED FROM THE AVAILABLE DOCUMENTS:',
        '- the complete lifetime number of fevers or antibiotic courses (home fevers and prescriptions that were never scanned are absent);',
        '- actual administration dates and doses for prescriptions without a nursing chart or caregiver log;',
        '- exact onset dates for episodes recorded only as a duration (for example "fever for three days");',
        '- whether a positive culture represented infection, reinfection or colonisation;',
        '- whether any treatment caused a fever to resolve.']
    return '\n'.join(lines)

# --------------------------------------------------------------------------------------
# Ask HealthBook AI
# --------------------------------------------------------------------------------------
LIFETIME_TERMS=re.compile(r'\b(fever|febrile|pyrexia|temperature|antibiotic|antibiotics|exposure|hospitali[sz]ation|hospitali[sz]ed|administered|prescribed|course|courses|e\.? ?coli|illness|episode|episodes|longest|since birth|lifetime)\b',re.I)

def lifetime_answer(db,pid,question):
    """Structured, evidence-cited answers for lifetime fever / antibiotic questions. Returns None when not applicable."""
    q=question.casefold()
    # Scope: only questions about fever or antibiotics. General hospitalization, illness, culture or medicine questions stay with the record-grounded answerer.
    about_fever=bool(re.search(r'fever|febrile|pyrexia|temperature',q));about_abx=bool(re.search(r'antibiotic|antimicrobial|exposure[- ]days?',q));about_ecoli='coli' in q
    if not (about_fever or about_abx):return None
    docs=doc_index(db,pid);evidence=[];parts=[];warnings=['Answers are limited to documented records; a complete lifetime count cannot be established from scanned pages alone.']
    def cite(kind,rid,statement,when,verified,doc_id,page):
        evidence.append({'citation':f'{kind}:{rid}','record_type':kind,'record_id':rid,'statement':statement,'date':when,'verified':verified,'source_document_id':doc_id,'source_page':page,'source_ref':docs.get(doc_id,{}).get('source_ref') if doc_id else None})
    fever=fever_report(db,pid);abx=antibiotic_report(db,pid);m=abx['metrics']
    month_words={'january':1,'february':2,'march':3,'april':4,'may':5,'june':6,'july':7,'august':8,'september':9,'october':10,'november':11,'december':12,'jan':1,'feb':2,'mar':3,'apr':4,'jun':6,'jul':7,'aug':8,'sep':9,'sept':9,'oct':10,'nov':11,'dec':12}
    month=next((v for k,v in month_words.items() if re.search(r'\b'+k+r'\b',q)),None)
    if about_fever and not about_abx:
        eps=fever['episodes']
        if 'longest' in q:
            top=max((e['documented_duration_days'] or 0) for e in eps) if eps else 0
            best=[e for e in eps if (e['documented_duration_days'] or 0)==top and top]
            if best:parts.append(('Longest documented fever episode: ' if len(best)==1 else f'{len(best)} episodes share the longest documented duration: ')+'; '.join(f"{b['title']} with {b['documented_duration_days']} documented day(s) ({b['first_documented_on']} to {b['last_documented_on'] or 'end not documented'}{'; dates '+b['date_certainty'] if b['date_certainty']!='exact' else ''})" for b in best)+'. Durations are days on which fever is documented, not measured illness length.')
            else:parts.append('No episode has a documented duration in days; durations written only as text (for example "fever for three days") are listed below without inventing dates.')
            eps=best
        else:
            parts.append(f"Documented fever episodes since birth: {fever['metrics']['documented_episodes']['value']} ({fever['metrics']['parent_reported_episodes']['value']} parent-reported, {fever['metrics']['episodes_with_uncertain_dates']['value']} with uncertain dates). Undocumented home fevers are not represented.")
            parts.append(chr(10).join(f"• {e['title']} — {e['first_documented_on'] or 'date unknown'}{' to '+e['last_documented_on'] if e['last_documented_on'] and e['last_documented_on']!=e['first_documented_on'] else ''}; {e['duration_text'] or (str(e['documented_duration_days'])+' documented day(s)' if e['documented_duration_days'] else 'duration not documented')}; {e['origin_label'].lower()}; {e['verification_status'].replace('_',' ')}" for e in eps))
        for e in eps:
            cite('fever_episode',e['id'],f"{e['title']}: {e['first_documented_on'] or 'date unknown'} → {e['last_documented_on'] or 'end not documented'}; duration {e['duration_text'] or (str(e['documented_duration_days'])+' days' if e['documented_duration_days'] else 'not documented')}; highest temperature {e['highest_temperature'] or 'not recorded'} {e['temperature_unit']}; diagnosis {e['documented_diagnosis'] or 'not documented'}; hospitalised {'yes' if e['hospitalized'] else 'no'}; {e['origin_label']}.",e['first_documented_on'],e['verification_status']=='verified',e['source_document_id'],e['source_page'])
            for ev in e['evidence']:cite('evidence',ev['id'],f"Original wording ({ev['source_ref'] or 'page'}): “{ev['quoted_text']}”",ev['observed_on'],False,ev['source_document_id'],ev['source_page'])
    if about_abx or about_ecoli:
        courses=abx['courses']
        if 'different' in q or 'distinct' in q or 'how many antibiotics' in q:
            parts.append(f"Distinct antibiotic active ingredients documented: {m['A_distinct_antibiotics']['value']} — "+', '.join(f"{c['generic_name']} ({len(c['course_ids'])} course record(s))" for c in m['A_distinct_antibiotics']['contributors'])+f". Separate courses: {m['B_antibiotic_courses']['value']}.")
        if 'more than once' in q or 'repeated' in q or 'twice' in q:
            rep=m['prescribed_more_than_once'];parts.append('Antibiotics prescribed more than once: '+(', '.join(f"{g} ({m['courses_per_antibiotic'][g]} courses)" for g in rep) if rep else 'none documented more than once.'))
            courses=[c for c in courses if c['generic_name'] in rep]
        if 'exposure' in q or 'days' in q:
            parts.append(f"Confirmed antibiotic-exposure days: {m['C_exposure_days']['overall_calendar_days']} calendar day(s) with a documented administration"+(': '+', '.join(m['C_exposure_days']['overall_dates']) if m['C_exposure_days']['overall_dates'] else '')+f". Per antibiotic: "+(', '.join(f"{g} {v['days']} day(s)" for g,v in m['C_exposure_days']['per_antibiotic'].items()) or 'no confirmed administration dates')+f". {len(m['C_exposure_days']['courses_without_documented_days'])} course(s) have no documented administration dates.")
        if 'difference' in q or 'actually' in q or 'versus' in q or ' vs' in q:
            e=m['E_status_breakdown'];stated=[c for c in abx['courses'] if c['administration_status']=='confirmed_administered' and c['administration_evidence']=='statement_only']
            parts.append(f"Prescribed-only courses: {e['prescribed_only']['value']}; scheduled: {e['scheduled']['value']}; confirmed administered: {e['confirmed_administered']['value']}"+(f" (of which {len(stated)} rest on a hospital statement that the drug was given, with no dated administration chart)" if stated else '')+f"; unknown administration status: {e['unknown']['value']}; culture-only or not received: {e['not_received']['value']}. Confirmed exposure days: {m['C_exposure_days']['overall_calendar_days']}. A prescription does not prove the medicine was given, and a statement without dates cannot be counted as exposure days.")
        if month:
            adms={a.id:a for a in db.query(Admission).filter_by(patient_id=pid)}
            def course_month(c):
                when=c['actual_start'] or c['prescribed_on'] or c['planned_start']
                if not when and c['admission_id'] in adms:when=adms[c['admission_id']].admitted_on.isoformat()
                return (when or '')[5:7]
            sel=[c for c in courses if course_month(c)==f'{month:02d}' and not c.get('unclassified')]
            if 'hospital' in q:sel=[c for c in sel if c['setting']=='inpatient' or c['admission_id']]
            if 'administer' in q or 'given' in q:
                given=[c for c in sel if c['administration_status']=='confirmed_administered']
                parts.append('Antibiotics documented as administered in hospital that month: '+(', '.join(f"{c['generic_name']} ({c['route']}; dates {'documented' if c['exposure_days'] else 'not documented, listed in the discharge summary only'})" for c in given) or 'none documented')+'. Also prescribed that month but not confirmed given: '+(', '.join(f"{c['generic_name']} ({c['route']}, {c['administration_status'].replace('_',' ')})" for c in sel if c['administration_status']!='confirmed_administered') or 'none')+'.')
                courses=sel
                month=None
        if month:
            parts.append(f"Antibiotics documented for that month: "+(', '.join(f"{c['generic_name']} ({c['route']}, {c['administration_status'].replace('_',' ')})" for c in sel) or 'none documented'))
            courses=sel
        if about_ecoli:
            ec=ecoli_report(db,pid)
            for item in ec['laboratory_findings']:
                o=item['observation']
                cite('observation',o['id'],f"{o['record_date']} {item['specimen_type']} specimen: {o['organism'] or o['test_name']}; {o['culture_findings'] or o['test_name']}; laboratory finding, not a clinical diagnosis. Antibiotics within window: "+(', '.join(f"{c['generic_name']} ({c['administration_status'].replace('_',' ')}; report says {c['laboratory_report_says']})" for c in item['antibiotics_prescribed_or_administered_within_window']) or 'none documented')+'. Fever episodes within 14 days: '+(', '.join(f['title'] for f in item['fever_episodes_within_14_days']) or 'none documented')+'.',o['record_date'],o['verified'],o['source_document_id'],o['source_page'])
            warnings.append('E. coli findings in different specimens or episodes do not establish the same disease or reinfection. Susceptibility is shown only as printed on the laboratory report and is not a treatment recommendation.')
            if not parts:parts.append(f"E. coli-related laboratory findings documented: {len(ec['laboratory_findings'])}; clinician-documented diagnoses mentioning E. coli: {len(ec['clinician_documented_diagnoses_mentioning_ecoli'])}.")
        if not parts:parts.append(f"Antibiotic courses documented: {m['B_antibiotic_courses']['value']} across {m['A_distinct_antibiotics']['value']} active ingredient(s); confirmed administered {m['E_status_breakdown']['confirmed_administered']['value']}; confirmed exposure days {m['C_exposure_days']['overall_calendar_days']}.")
        for c in courses:
            cite('antibiotic_course',c['id'],f"{c['generic_name']}{' ('+c['brand_name']+')' if c['brand_name'] else ''}: {c['route']}, {c['setting'].replace('_',' ')}; prescribed {c['prescribed_on'] or 'date not documented'}; prescribed duration {c['prescribed_duration_text'] or c['prescribed_duration_days'] or 'not documented'}; actual {c['actual_start'] or 'start unknown'} → {c['actual_stop'] or 'stop unknown'}; status {c['administration_status'].replace('_',' ')}; documented exposure days {len(c['exposure_days'])}; illness {c['illness'] or c['indication'] or 'not linked'}; {c['origin_label']}.",c['prescribed_on'] or c['actual_start'],c['verification_status']=='verified',c['source_document_id'],c['source_page'])
            for ev in c['evidence'][:3]:cite('evidence',ev['id'],f"Original wording ({ev['source_ref'] or 'page'}): “{ev['quoted_text']}”",ev['observed_on'],False,ev['source_document_id'],ev['source_page'])
    if about_fever and about_abx:
        corr=correlation_report(db,pid)
        parts.append(f"Fever episodes without a documented antibiotic: {len(corr['fever_episodes_without_documented_antibiotic'])}; antibiotic courses without a documented fever: {len(corr['antibiotic_courses_without_documented_fever'])}. Timing alone does not show cause or need.")
    if not parts:return None
    if any(x['verified'] is False for x in evidence):warnings.append('Entries marked unverified were transcribed from the original pages and still need your verification against the scan.')
    return {'question':question,'mode':'lifetime_health_structured','answer':'\n\n'.join(parts),'evidence':evidence[:150],'warnings':warnings}
