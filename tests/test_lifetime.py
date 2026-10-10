"""Lifetime Health regression tests. Synthetic records only; metric rules are exercised one by one."""
import uuid
from datetime import date,timedelta
from tests.test_api import api,make_user,make_child,fake_png,client
from api.main import app
from api.database import SessionLocal
from api.models import RateWindow,Document
from api.crypto import encrypt
from api import lifetime as L

if not any(getattr(r,'path','').endswith('/lifetime/dashboard') for r in app.routes):
    # Until main.py includes the router, register it ahead of the static web mount.
    from api.lifetime_api import router
    app.include_router(router)
    for m in [r for r in app.router.routes if getattr(r,'name','')=='web']:
        app.router.routes.remove(m);app.router.routes.append(m)

import pytest
@pytest.fixture(autouse=True)
def clear_rate_window():
    with SessionLocal() as db:db.query(RateWindow).delete();db.commit()

def doc(token,pid,ref,category='prescription',text=None,record_date=None):
    data={'category':category,'title':f'Synthetic {ref}','source_ref':ref}
    if record_date:data['record_date']=record_date
    r=api('POST',f'/api/patients/{pid}/documents',token,data=data,files={'file':(f'{ref}.png',fake_png()+ref.encode(),'image/png')})
    assert r.status_code==201,r.text
    if text:
        with SessionLocal() as db:
            d=db.get(Document,r.json()['id']);d.extracted_cipher=encrypt(('=== PAGE 1 ===\n'+text).encode()).decode();db.commit()
    return r.json()['id']

def family():
    t=make_user('lh'+uuid.uuid4().hex[:10]);p=make_child(t);return t,p

def course(t,p,**kw):
    body={'generic_name':'cefixime','route':'oral','administration_status':'prescribed_only','setting':'outpatient'};body.update(kw)
    r=api('POST',f'/api/patients/{p}/lifetime/antibiotics',t,json=body);assert r.status_code==201,r.text;return r.json()

def test_drug_classification_against_active_ingredients():
    assert L.classify_drug('Syp Omnix-100')['generic']=='cefixime'
    assert L.classify_drug('Inj PIPZO')['generic']=='piperacillin-tazobactam'
    assert L.classify_drug('Augmentin Duo')['generic']=='amoxicillin-clavulanate'
    assert L.classify_drug('Tab O2')['generic']=='ofloxacin-ornidazole'
    for name in ('Zn 20 drop','Tuffpro Fast','Syp Meftal-P','Inj PCM','WHO-ORS','Crocin drops','Emeset'):
        assert L.classify_drug(name)['is_antibiotic'] is False,name
    assert L.classify_drug('Crocin drops')['is_antipyretic'] and not L.classify_drug('Emeset')['is_antipyretic']
    assert L.classify_drug('Unknownamycin')['is_antibiotic'] is None
    mentions=L.antibiotic_mentions('Meropenem   S\nAmikacin  R\nInj Meropenem 250 mg IV TDS x 5 days')
    assert mentions[0]['context']=='culture_susceptibility_list' and mentions[2]['context']=='prescription_or_note'
    fev=L.fever_mentions('c/o fever x 3 days\nTemp 101.2 F\nSyp Crocin SOS')
    assert fev[0]['duration']['days']==3 and fev[1]['temperatures']==['101.2 F'] and len(fev)==2

def test_distinct_courses_exposure_days_and_exclusions():
    t,p=family();d=doc(t,p,'NEW_62',category='discharge');rx=doc(t,p,'NEW_21')
    # same ingredient, two illnesses -> 1 distinct, 2 courses
    c1=course(t,p,generic_name='Omnix-100',prescribed_on='2026-06-29',source_document_id=rx)
    assert c1['generic_name']=='cefixime' and c1['brand_name']=='Omnix-100'
    course(t,p,generic_name='cefixime',prescribed_on='2026-08-19',source_document_id=rx)
    # overlapping confirmed IV courses: per-drug days kept, overall calendar days counted once
    m=course(t,p,generic_name='meropenem',route='IV',setting='inpatient',administration_status='confirmed_administered',actual_start='2026-09-27',actual_stop='2026-10-01',source_document_id=d)
    course(t,p,generic_name='amikacin',route='IV',setting='inpatient',administration_status='confirmed_administered',actual_start='2026-09-29',actual_stop='2026-10-02',source_document_id=d)
    # IV -> oral switch of the same agent: a continuation, not a new course
    course(t,p,generic_name='meropenem',route='oral',setting='discharge_prescription',administration_status='prescribed_only',prescribed_on='2026-10-02',continuation_of_id=m['id'],source_document_id=d)
    # culture-only entry and an unknown-status prescription
    course(t,p,generic_name='gentamicin',administration_status='not_received',source_document_id=d,notes='susceptibility table only')
    course(t,p,generic_name='azithromycin',administration_status='unknown',prescribed_on='2026-03-01',source_document_id=rx)
    # rejected: not an antibiotic / unrecognised without override
    assert api('POST',f'/api/patients/{p}/lifetime/antibiotics',t,json={'generic_name':'Zn 20','source_document_id':rx}).status_code==422
    assert api('POST',f'/api/patients/{p}/lifetime/antibiotics',t,json={'generic_name':'Mysterycillin','source_document_id':rx}).status_code==422
    r=api('GET',f'/api/patients/{p}/lifetime/antibiotics',t).json();m_=r['metrics']
    assert m_['A_distinct_antibiotics']['value']==4  # cefixime, meropenem, amikacin, azithromycin (gentamicin excluded)
    assert m_['B_antibiotic_courses']['value']==5 and len(m_['B_antibiotic_courses']['continuations_merged'])==1
    assert m_['C_exposure_days']['per_antibiotic']['meropenem']['days']==5 and m_['C_exposure_days']['per_antibiotic']['amikacin']['days']==4
    assert m_['C_exposure_days']['overall_calendar_days']==6  # 27 Sep .. 2 Oct
    assert m_['E_status_breakdown']['prescribed_only']['value']==3 and m_['E_status_breakdown']['confirmed_administered']['value']==2 and m_['E_status_breakdown']['unknown']['value']==1 and m_['E_status_breakdown']['not_received']['value']==1
    assert m_['courses_per_antibiotic']['cefixime']==2 and m_['prescribed_more_than_once']==['cefixime']
    assert set(m_['C_exposure_days']['courses_without_documented_days'])>={c['id'] for c in r['courses'] if c['administration_status'] in ('prescribed_only','unknown')}
    assert m_['route_split']['IV']['courses']==2
    # Metric D: documented administrations with nursing chart source need a page
    assert api('POST',f'/api/lifetime/antibiotics/{m["id"]}/administrations',t,json={'administered_on':'2026-09-28','source':'nursing_chart'}).status_code==422
    r2=api('POST',f'/api/lifetime/antibiotics/{m["id"]}/administrations',t,json={'administered_on':'2026-09-28','administered_time':'08:00','source':'nursing_chart','source_document_id':d,'idempotency_key':'synthetic-adm-1'});assert r2.status_code==201,r2.text
    assert api('POST',f'/api/lifetime/antibiotics/{m["id"]}/administrations',t,json={'administered_on':'2026-09-28','administered_time':'08:00','source':'nursing_chart','source_document_id':d,'idempotency_key':'synthetic-adm-1'}).json()['id']==r2.json()['id']
    assert api('GET',f'/api/patients/{p}/lifetime/antibiotics',t).json()['metrics']['D_documented_administrations']['value']==1
    dash=api('GET',f'/api/patients/{p}/lifetime/dashboard',t).json()
    tiles={x['key']:x for x in dash['tiles']}
    assert tiles['distinct_antibiotics']['value']==4 and tiles['distinct_antibiotics']['verified_value']==0 and tiles['antibiotic_courses']['contributors']
    assert tiles['exposure_days']['value']==6 and len(tiles['exposure_days']['contributors'])==6

def test_duplicates_unknown_dates_and_source_link_validation():
    t,p=family();rx=doc(t,p,'NEW_10')
    a=course(t,p,generic_name='cefpodoxime',prescribed_on='2025-10-15',source_document_id=rx)
    course(t,p,generic_name='Cefoprox',prescribed_on='2025-10-15',source_document_id=rx)
    course(t,p,generic_name='amoxicillin',source_document_id=rx,uncertain_fields='dose,duration')
    course(t,p,generic_name='azithromycin',administration_status='confirmed_administered',source_document_id=rx)
    flags=api('GET',f'/api/patients/{p}/lifetime/antibiotics',t).json()['review_flags'];kinds={f['type'] for f in flags}
    assert {'possible_duplicate_prescription','undated_prescription','confirmed_without_dates','uncertain_transcription'}<=kinds
    assert api('POST',f'/api/patients/{p}/lifetime/antibiotics',t,json={'generic_name':'cefixime','source_document_id':rx,'source_page':3}).status_code==422
    tables=api('GET',f'/api/patients/{p}/lifetime/review-tables',t).json()
    assert all(r['source_link_valid'] for r in tables['antibiotic_review_table']) and tables['source_links']['invalid']==[]
    # verification requires an explicit check; revision resets it and keeps history
    assert api('POST',f'/api/lifetime/antibiotics/{a["id"]}/verify',t,json={'status':'verified'}).status_code==422
    assert api('POST',f'/api/lifetime/antibiotics/{a["id"]}/verify',t,json={'status':'verified','checked_against_original':True}).json()['verification_status']=='verified'
    assert api('GET',f'/api/patients/{p}/lifetime/antibiotics?scope=verified',t).json()['metrics']['B_antibiotic_courses']['value']==1
    changed=api('PATCH',f'/api/lifetime/antibiotics/{a["id"]}',t,json={'generic_name':'cefpodoxime','prescribed_on':'2025-10-16','source_document_id':rx}).json()
    assert changed['verification_status']=='needs_verification' and changed['version']==2
    history=api('GET',f'/api/patients/{p}/record-revisions',t).json()
    assert any(h['entity']=='antibiotic_courses' and h['previous_values']['prescribed_on']=='2025-10-15' for h in history)

def test_fever_is_documented_not_inferred_and_durations_are_preserved():
    t,p=family()
    rx=doc(t,p,'NEW_29',text='H/o Fever x 1 day\nSyp Meftal-P SOS\nOmnix-100 4 ml BD x 5 day',record_date='2026-07-27')
    crocin=doc(t,p,'NEW_07',text='Crocin drops SOS after vaccination',record_date='2025-09-15')
    # no fever episode exists yet although paracetamol and an antibiotic are written
    assert api('GET',f'/api/patients/{p}/lifetime/fever',t).json()['metrics']['documented_episodes']['value']==0
    scan=api('GET',f'/api/patients/{p}/lifetime/scan',t).json()['pages']
    assert any(pg['document_id']==rx and pg['fever_mentions'] for pg in scan) and not any(pg['document_id']==crocin and pg['fever_mentions'] for pg in scan)
    body={'title':'Fever July 2026','first_documented_on':'2026-07-27','duration_text':'H/o Fever x 1 day','date_certainty':'approximate','documented_diagnosis':'not documented','source_document_id':rx,'evidence':[{'source_document_id':rx,'source_page':1,'quoted_text':'H/o Fever x 1 day','evidence_kind':'fever_mention','observed_on':'2026-07-27','certainty':'high'}]}
    f=api('POST',f'/api/patients/{p}/lifetime/fever',t,json=body);assert f.status_code==201,f.text
    assert api('POST',f'/api/patients/{p}/lifetime/fever',t,json={**body,'first_documented_on':'2025-01-01'}).status_code==422  # before birth
    assert api('POST',f'/api/patients/{p}/lifetime/fever',t,json={**body,'highest_temperature':'101'}).status_code==422  # unit missing
    rep=api('GET',f'/api/patients/{p}/lifetime/fever',t).json()
    ep=rep['episodes'][0]
    assert ep['documented_duration_days'] is None and ep['duration_text']=='H/o Fever x 1 day' and rep['metrics']['episodes_with_uncertain_dates']['value']==1
    assert ep['evidence'][0]['quoted_text']=='H/o Fever x 1 day' and rep['calendar_days']==['2026-07-27']
    # an antibiotic prescribed the same day appears as date-window linked, never as proof of cause
    c=course(t,p,generic_name='Omnix-100',prescribed_on='2026-07-27',source_document_id=rx,uncertain_fields='name')
    ep=api('GET',f'/api/patients/{p}/lifetime/fever',t).json()['episodes'][0]
    assert ep['antibiotics_during_episode'][0]['link']=='date_window_only'
    corr=api('GET',f'/api/patients/{p}/lifetime/correlation',t).json()
    assert corr['fever_episodes_without_documented_antibiotic']==[] and corr['antibiotic_courses_without_documented_fever']==[]
    steps=[e['step'] for e in corr['events']];assert 'fever_onset' in steps and 'medication_prescribed' in steps and 'doctor_consultation' in steps

def test_parent_entries_are_labelled_and_corrections_keep_history():
    t,p=family()
    e=api('POST',f'/api/patients/{p}/lifetime/parent-entries',t,json={'kind':'fever','started_on':'2026-04-10','ended_on':'2026-04-12','temperature':'101.4','temperature_unit':'F','symptoms':'runny nose','medicine_name':'Crocin drops','notes':'home thermometer'})
    assert e.status_code==201,e.text;eid=e.json()['id']
    rep=api('GET',f'/api/patients/{p}/lifetime/fever',t).json()
    assert rep['metrics']['documented_episodes']['value']==1 and rep['episodes'][0]['origin']=='parent_report' and rep['episodes'][0]['origin_label']=='Parent-reported' and rep['episodes'][0]['documented_duration_days']==3 and rep['episodes'][0]['antipyretics']=='Crocin drops'
    assert api('GET',f'/api/patients/{p}/lifetime/fever?scope=verified',t).json()['metrics']['documented_episodes']['value']==0
    fixed=api('PATCH',f'/api/lifetime/parent-entries/{eid}',t,json={'kind':'fever','started_on':'2026-04-10','ended_on':'2026-04-11','temperature':'101.4','temperature_unit':'F','symptoms':'runny nose','notes':'corrected end date'})
    assert fixed.status_code==200,fixed.text and fixed.json()['version']==2
    assert fixed.json()['revisions'][0]['previous_values']['ended_on']=='2026-04-12'
    assert api('GET',f'/api/patients/{p}/lifetime/fever',t).json()['episodes'][0]['documented_duration_days']==2
    # parent-reported antibiotic dose becomes a caregiver-reported administration (Metric D) and a parent-origin course
    m=api('POST',f'/api/patients/{p}/lifetime/parent-entries',t,json={'kind':'medicine','medicine_name':'Augmentin','medicine_dose':'3 ml','administered_on':'2026-05-02','administered_time':'09:00'})
    assert m.status_code==201,m.text
    abx=api('GET',f'/api/patients/{p}/lifetime/antibiotics',t).json()
    assert abx['courses'][0]['origin']=='parent_report' and abx['courses'][0]['generic_name']=='amoxicillin-clavulanate' and abx['metrics']['D_documented_administrations']['value']==1 and abx['metrics']['C_exposure_days']['overall_calendar_days']==1
    assert api('POST',f'/api/patients/{p}/lifetime/parent-entries',t,json={'kind':'fever','started_on':'2099-01-01'}).status_code==422
    api('POST',f'/api/lifetime/parent-entries/{eid}/withdraw',t)
    assert api('GET',f'/api/patients/{p}/lifetime/fever',t).json()['metrics']['documented_episodes']['value']==0
    assert len(api('GET',f'/api/patients/{p}/lifetime/parent-entries',t).json())==2  # nothing deleted

def test_cross_family_isolation_and_viewer_rules():
    t,p=family();other=make_user('lh'+uuid.uuid4().hex[:10]);rx=doc(t,p,'NEW_21')
    c=course(t,p,source_document_id=rx)
    for path in ('lifetime/dashboard','lifetime/fever','lifetime/antibiotics','lifetime/illnesses','lifetime/ecoli','lifetime/correlation','lifetime/review-tables','lifetime/summary','lifetime/parent-entries','lifetime/scan'):
        assert api('GET',f'/api/patients/{p}/{path}',other).status_code==404,path
    assert api('PATCH',f'/api/lifetime/antibiotics/{c["id"]}',other,json={'generic_name':'cefixime','source_document_id':rx}).status_code==404
    assert api('POST',f'/api/patients/{p}/lifetime/antibiotics',other,json={'generic_name':'cefixime','source_document_id':rx}).status_code==404
    # a document from another family cannot be cited
    op=make_child(other);od=doc(other,op,'NEW_21')
    assert api('POST',f'/api/patients/{p}/lifetime/antibiotics',t,json={'generic_name':'cefixime','source_document_id':od}).status_code==400

def test_ask_lifetime_questions_cite_evidence():
    t,p=family();rx=doc(t,p,'NEW_34',text='fever today\nOmnix-100 4 ml BD x5 day',record_date='2026-08-19');dis=doc(t,p,'NEW_62',category='discharge',record_date='2026-10-02')
    api('POST',f'/api/patients/{p}/lifetime/fever',t,json={'title':'Fever Aug 2026','first_documented_on':'2026-08-19','last_documented_on':'2026-08-21','documented_fever_days':3,'date_certainty':'exact','highest_temperature':'99.6','temperature_unit':'F','source_document_id':rx,'evidence':[{'source_document_id':rx,'quoted_text':'fever today','evidence_kind':'fever_mention'}]})
    api('POST',f'/api/patients/{p}/lifetime/fever',t,json={'title':'Fever Sep 2026 admission','first_documented_on':'2026-09-27','duration_text':'FEVER SINCE 1 DAY','date_certainty':'approximate','hospitalized':True,'source_document_id':dis})
    course(t,p,generic_name='Omnix-100',prescribed_on='2026-08-19',source_document_id=rx)
    course(t,p,generic_name='meropenem',route='IV',setting='inpatient',administration_status='confirmed_administered',actual_start='2026-09-27',actual_stop='2026-10-01',source_document_id=dis)
    course(t,p,generic_name='cefixime',prescribed_on='2026-06-29',source_document_id=rx)
    q=lambda s:api('POST',f'/api/patients/{p}/lifetime/ask',t,json={'question':s}).json()
    a=q('How many times has my child had fever since birth?')
    assert a['mode']=='lifetime_health_structured' and 'fever episodes since birth: 2' in a['answer'].casefold() and any(e['record_type']=='evidence' and e['source_document_id']==rx for e in a['evidence'])
    a=q('What was his longest recorded fever episode?');assert 'Longest documented fever episode: Fever Aug 2026 with 3' in a['answer']
    a=q('How many different antibiotics has my child received?');assert 'Distinct antibiotic active ingredients documented: 2' in a['answer'] and 'Separate courses: 3' in a['answer']
    a=q('Which antibiotics were prescribed more than once?');assert 'cefixime (2 courses)' in a['answer']
    a=q('How many days of confirmed antibiotic exposure are recorded?');assert 'Confirmed antibiotic-exposure days: 5' in a['answer']
    a=q('Which antibiotics were administered during his September hospitalization?');assert 'meropenem (IV; dates documented)' in a['answer'] and 'cefixime' not in a['answer'].split('administered in hospital that month')[1]
    a=q('What is the difference between his prescribed and actually administered antibiotic history?');assert 'Prescribed-only courses: 2' in a['answer'] and 'confirmed administered: 1' in a['answer']
    a=q('Compare the fever and antibiotic history across his E. coli-related episodes.');assert a['mode']=='lifetime_health_structured' and any('reinfection' in w for w in a['warnings'])
    for other in ('Which vaccines were given?','What happened during his September hospitalization?','Compare all E. coli-related reports and episodes','Which medicines were prescribed at discharge?'):
        assert api('POST',f'/api/patients/{p}/lifetime/ask',t,json={'question':other}).json()['mode']!='lifetime_health_structured',other
    text=api('GET',f'/api/patients/{p}/lifetime/summary',t).text
    assert 'FEVER EPISODES DOCUMENTED: 2' in text and 'DISTINCT ANTIBIOTICS DOCUMENTED: 2' in text and 'CANNOT BE ESTABLISHED' in text

def test_transcription_import_is_idempotent_and_resolves_bundle_pages():
    t,p=family();doc(t,p,'NEW_39',category='discharge',record_date='2026-10-02');doc(t,p,'NEW_62',category='discharge',record_date='2026-10-02')
    bundle={'batch':'synthetic-bundle','checked_against_originals':True,
        'fever_episodes':[{'key':'sep','title':'Fever at admission','first_documented_on':'2026-09-27','duration_text':'FEVER SINCE 1 DAY','date_certainty':'approximate','hospitalized':True,'source_ref':'NEW_39','evidence':[{'source_ref':'NEW_39','quoted_text':'FEVER SINCE 1 DAY','evidence_kind':'fever_mention'}]}],
        'antibiotic_courses':[{'key':'mero-ip','generic_name':'Inj Meropenem','route':'IV','setting':'inpatient','administration_status':'confirmed_administered','source_ref':'NEW_62','fever_key':'sep'},
            {'key':'mero-dc','generic_name':'meropenem','route':'IV','setting':'discharge_prescription','administration_status':'prescribed_only','prescribed_on':'2026-10-02','prescribed_duration_days':5,'source_ref':'NEW_62','continuation_of_key':'mero-ip'}],
        'administrations':[{'course_key':'mero-ip','administered_on':'2026-10-01','source':'discharge_summary','source_ref':'NEW_62','note':'statement only'}]}
    first=api('POST',f'/api/patients/{p}/lifetime/import-transcription',t,json=bundle);assert first.status_code==200,first.text
    assert len(first.json()['fever_episodes'])==1 and len(first.json()['antibiotic_courses'])==2 and len(first.json()['administrations'])==1
    second=api('POST',f'/api/patients/{p}/lifetime/import-transcription',t,json=bundle).json()
    assert second['fever_episodes']==[] and second['antibiotic_courses']==[] and len(second['skipped_existing'])==3
    abx=api('GET',f'/api/patients/{p}/lifetime/antibiotics',t).json()
    assert abx['metrics']['B_antibiotic_courses']['value']==1 and abx['metrics']['A_distinct_antibiotics']['value']==1 and abx['metrics']['D_documented_administrations']['value']==1
    assert any(c['fever_episode_id'] for c in abx['courses'] if c['setting']=='inpatient')
    missing={**bundle,'batch':'other','fever_episodes':[{**bundle['fever_episodes'][0],'source_ref':'NEW_99'}]}
    assert api('POST',f'/api/patients/{p}/lifetime/import-transcription',t,json=missing).status_code==422
