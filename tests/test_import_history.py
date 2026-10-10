"""Bundle import, longitudinal history views and Ask Records grounding — synthetic data only, isolated database."""
import io
import json
import os
import subprocess
import sys
import uuid
import hashlib
from datetime import date, timedelta
from pathlib import Path
import pytest
from PIL import Image, ImageDraw
from tests.test_api import api, make_user, make_child, client
from api.database import SessionLocal
from api.models import RateWindow

@pytest.fixture(autouse=True)
def clear_rate_window():
    with SessionLocal() as db:db.query(RateWindow).delete();db.commit()

def page(text):
    im=Image.new('RGB',(600,300),'white');ImageDraw.Draw(im).text((20,20),text,fill='black');b=io.BytesIO();im.save(b,format='JPEG');return b.getvalue()

def family():
    t=make_user(uuid.uuid4().hex);p=make_child(t);return t,p

def upload(t,p,ref,category='lab',record_date='2026-01-05',title='Synthetic page',notes=''):
    data={'category':category,'title':title,'source_ref':ref,'import_batch':'synthetic-batch','notes':notes}
    if record_date:data['record_date']=record_date
    return api('POST',f'/api/patients/{p}/documents',t,data=data,files={'file':(ref+'.jpg',page(ref),'image/jpeg')})

def test_source_ref_provenance_blocks_duplicates_and_keeps_metadata():
    t,p=family()
    r=upload(t,p,'SYN_01',notes='Date inferred from a bill');assert r.status_code==201,r.text
    assert r.json()['source_ref']=='SYN_01' and r.json()['import_batch']=='synthetic-batch' and r.json()['notes']=='Date inferred from a bill'
    again=api('POST',f'/api/patients/{p}/documents',t,data={'category':'lab','title':'other title','source_ref':'SYN_01'},files={'file':('x.jpg',page('different bytes'),'image/jpeg')})
    assert again.status_code==409 and 'document #' in again.json()['detail']
    assert api('POST',f'/api/patients/{p}/documents',t,data={'category':'lab','source_ref':'bad ref!'},files={'file':('x.jpg',page('z'),'image/jpeg')}).status_code==422
    assert api('POST',f'/api/patients/{p}/documents',t,data={'category':'not-a-category'},files={'file':('x.jpg',page('q'),'image/jpeg')}).status_code==422
    culture=upload(t,p,'SYN_02',category='culture');assert culture.status_code==201
    meta=api('PATCH',f'/api/documents/{culture.json()["id"]}',t,json={'record_date':'2026-02-01','notes':'Reviewed'})
    assert meta.status_code==200 and meta.json()['record_date']=='2026-02-01'
    history=api('GET',f'/api/patients/{p}/record-revisions',t).json()
    assert history and history[0]['entity']=='documents'

def seed_history(t,p):
    docs={ref:upload(t,p,ref,cat,dt,title).json()['id'] for ref,cat,dt,title in [
        ('D_DIS','discharge','2026-03-05','Discharge summary'),('D_LAB1','lab','2026-01-20','CBC lab A'),('D_LAB2','lab','2026-01-20','CBC lab B'),
        ('D_CUL1','culture','2026-01-22','Stool culture January'),('D_CUL2','culture','2026-03-02','Stool culture March'),('D_CARD','vaccination_card',None,'Card')]}
    adm=api('POST',f'/api/patients/{p}/admissions',t,json={'facility':'Fictional Children Hospital','admitted_on':'2026-03-01','discharged_on':'2026-03-05','diagnosis':'Documented diagnosis text','treatment_plan':'Inpatient list: Drug A, Drug B','source_document_id':docs['D_DIS'],'discharge_document_id':docs['D_DIS']}).json()
    meds=[]
    for name,ctype in [('Drug A','inpatient_treatment'),('Drug A','discharge_prescription'),('Drug B','discharge_prescription'),('Historic drops','historical_prescription')]:
        r=api('POST',f'/api/patients/{p}/medicines',t,json={'name':name,'instructions':'As written','course_type':ctype,'prescription_date':'2026-03-05' if ctype!='historical_prescription' else '2025-09-01','source_document_id':docs['D_DIS']})
        assert r.status_code==201,r.text;meds.append(r.json())
    obs=[]
    for dt,test,value,unit,doc in [('2026-01-20','Haemoglobin','10.2','gm%','D_LAB1'),('2026-01-20','Haemoglobin (Hb)','11.9','gm/dL','D_LAB2'),('2026-01-20','Total WBC Count','7600','/cumm','D_LAB1'),('2026-01-20','Total Leukocyte Count (TLC)','7.20','X10^3/uL','D_LAB2'),('2026-01-20','CRP','14.8','mg/L','D_LAB1'),('2026-03-01','CRP','4.5','mg/L','D_LAB1')]:
        r=api('POST',f'/api/patients/{p}/observations',t,json={'test_name':test,'value':value,'unit':unit,'specimen':'blood' if test!='CRP' else 'serum','record_date':dt,'verified':True,'source_document_id':docs[doc]});assert r.status_code==201,r.text;obs.append(r.json())
    for dt,doc,sus in [('2026-01-22','D_CUL1','Meropenem: R; Amikacin: S'),('2026-03-02','D_CUL2','Meropenem: S; Amikacin: S')]:
        r=api('POST',f'/api/patients/{p}/observations',t,json={'test_name':'Stool culture and sensitivity','value':'Escherichia coli isolated','specimen':'stool','record_date':dt,'verified':True,'organism':'Escherichia coli','susceptibility':sus,'source_document_id':docs[doc]});assert r.status_code==201;obs.append(r.json())
    ep1=api('POST',f'/api/patients/{p}/episodes',t,json={'title':'January gastro episode','started_on':'2026-01-19','ended_on':'2026-01-25','documented_diagnosis':'Not documented','links':[{'kind':'document','id':docs['D_CUL1']},{'kind':'observation','id':obs[-2]['id']}]}).json()
    ep2=api('POST',f'/api/patients/{p}/episodes',t,json={'title':'March hospitalization','started_on':'2026-03-01','documented_diagnosis':'Documented diagnosis text','links':[{'kind':'admission','id':adm['id']},{'kind':'document','id':docs['D_DIS']},{'kind':'medicine','id':meds[1]['id']},{'kind':'observation','id':obs[-1]['id']}]}).json()
    vax=[]
    for name,dose,given,due in [('PCV','Booster 1',None,'2026-11-03'),('MMR','Dose 2',None,'2026-11-03'),('Hib','Dose 1','2025-09-15',None)]:
        r=api('POST',f'/api/patients/{p}/vaccines',t,json={'name':name,'dose_label':dose,'given_date':given,'due_date':due,'source_type':'vaccination_card','source_document_id':docs['D_CARD']});assert r.status_code==201,r.text;vax.append(r.json())
    return docs,adm,meds,obs,(ep1,ep2),vax

def test_dashboard_history_and_series_read_real_records_without_inference():
    t,p=family();docs,adm,meds,obs,eps,vax=seed_history(t,p)
    d=api('GET',f'/api/patients/{p}/dashboard',t).json()
    assert d['documents']['total']==6 and d['documents']['by_category']['culture']==2 and d['documents']['undated']==1
    assert d['episodes']['total']==2 and d['admissions']['total']==1
    assert d['medications']['verified_current']==0 and d['medications']['drafts']==4 and d['medications']['by_type']['inpatient_treatment']==1
    assert d['vaccination']['next_confirmed'] is None and d['vaccination']['pending_verification']==3
    assert d['vaccination']['pending_due_dates']==[{'due_date':'2026-11-03','vaccines':['MMR Dose 2','PCV Booster 1']}]
    assert d['reminders_next_30_days']==0 and d['labs']['verified']==8
    h=api('GET',f'/api/patients/{p}/history',t).json()
    titles={e['title']:e for e in h['episodes']}
    assert set(titles)=={'January gastro episode','March hospitalization'}
    march=titles['March hospitalization']
    assert any(ev['kind']=='admission' and ev['association']=='explicit_link' for ev in march['events'])
    assert any(ev['kind']=='culture' and ev['organism']=='Escherichia coli' for ev in march['events'])
    assert any(ev['kind']=='medicine_prescribed' and ev['course_type']=='discharge_prescription' for ev in march['events'])
    assert len(h['undated_documents'])==1 and any(g['days']>=60 for g in h['gaps'])
    assert all(ev['source_ref'] for ep in h['episodes'] for ev in ep['events'] if ev['source_document_id'])
    s=api('GET',f'/api/patients/{p}/lab-series',t).json()
    hb=next(g for g in s['series'] if g['test_name']=='Haemoglobin')
    assert hb['unit']=='g/dL' and len(hb['points'])==2 and all(pt['same_day_separate_reports'] for pt in hb['points'])
    wbc=next(g for g in s['series'] if g['test_name'].startswith('WBC'))
    assert wbc['unit']=='/µL' and sorted(pt['numeric_value'] for pt in wbc['points'])==[7200.0,7600.0] and wbc['unit_conversions_applied']
    chart=api('GET',f'/api/patients/{p}/vaccination-chart',t).json()
    assert chart['by_status']=={'due_pending_verification':2,'documented_given_pending_verification':1} and chart['next_confirmed'] is None
    assert all(r['reminders_scheduled']==[] for r in chart['rows'])
    mh=api('GET',f'/api/patients/{p}/medication-history?q=drug a',t).json()
    assert mh['summary']['total']==2 and {c['course_type'] for c in mh['courses']}=={'inpatient_treatment','discharge_prescription'}
    assert all('no administration events recorded' in c['missing'] for c in mh['courses'] if c['course_type']!='inpatient_treatment')
    links=api('GET',f'/api/documents/{docs["D_DIS"]}/links',t).json()
    assert len(links['medicines'])==4 and len(links['admissions'])==1 and len(links['episodes'])==1
    thumb=api('GET',f'/api/documents/{docs["D_DIS"]}/thumbnail?w=200',t)
    assert thumb.status_code==200 and thumb.headers['content-type'].startswith('image/')

def test_confirming_a_due_date_schedules_7_1_0_reminders_and_unverified_rows_never_do():
    t,p=family();docs,adm,meds,obs,eps,vax=seed_history(t,p)
    due=(date.today()+timedelta(days=20)).isoformat()
    pcv=next(v for v in vax if v['name']=='PCV')
    assert api('PATCH',f'/api/vaccines/{pcv["id"]}',t,json={'due_date':due}).status_code==200
    assert api('GET',f'/api/patients/{p}/reminders?days=60',t).json()['items']==[]
    assert api('PATCH',f'/api/vaccines/{pcv["id"]}',t,json={'confirmed':True}).status_code==200
    items=api('GET',f'/api/patients/{p}/reminders?days=60',t).json()['items']
    assert sorted(i['key'].rsplit('-',1)[1] for i in items)==['0d','1d','7d'] and all(i['item_id']==pcv['id'] for i in items)
    chart=api('GET',f'/api/patients/{p}/vaccination-chart',t).json()
    row=next(r for r in chart['rows'] if r['id']==pcv['id'])
    assert row['status']=='upcoming_confirmed' and len(row['reminders_scheduled'])==3
    assert api('GET',f'/api/patients/{p}/dashboard',t).json()['vaccination']['next_confirmed']['id']==pcv['id']
    other=next(v for v in vax if v['name']=='MMR')
    assert api('GET',f'/api/patients/{p}/vaccination-chart',t).json()['rows'] and all(r['reminders_scheduled']==[] for r in chart['rows'] if r['id']==other['id'])

def test_ask_records_cites_sources_and_never_invents_administration():
    t,p=family();docs,adm,meds,obs,eps,vax=seed_history(t,p)
    a=api('POST',f'/api/patients/{p}/ask',t,json={'question':'Which medicines were prescribed at hospital discharge?'}).json()
    names={e['statement'].split(' [')[0] for e in a['evidence'] if e['record_type']=='medicine'}
    assert names=={'Drug A','Drug B'} and all(e['source_document_id']==docs['D_DIS'] for e in a['evidence'] if e['record_type']=='medicine')
    a=api('POST',f'/api/patients/{p}/ask',t,json={'question':'Which medications were actually administered?'}).json()
    assert any('No actual administration events are documented' in s for s in a['summary'])
    assert not any(e['record_type']=='medication_event' for e in a['evidence'])
    a=api('POST',f'/api/patients/{p}/ask',t,json={'question':'Compare the January and March E. coli culture reports'}).json()
    assert a['comparison'] and a['comparison']['differences']==['Meropenem'] and len(a['comparison']['reports'])==2
    assert any('do not establish' in w for w in a['warnings'])
    a=api('POST',f'/api/patients/{p}/ask',t,json={'question':'What happened during his March hospitalization?'}).json()
    kinds={e['record_type'] for e in a['evidence']}
    assert 'admission' in kinds and 'episode' in kinds and 'observation_group' in kinds
    a=api('POST',f'/api/patients/{p}/ask',t,json={'question':'When is his next confirmed vaccination appointment?'}).json()
    assert any('No caregiver-confirmed upcoming vaccination appointment' in s for s in a['summary'])
    a=api('POST',f'/api/patients/{p}/ask',t,json={'question':'How did his CRP change?'}).json()
    assert [e['date'] for e in a['evidence']]==['2026-01-20','2026-03-01'] and a['summary'] and '14.8' in a['summary'][0] and '4.5' in a['summary'][0]
    a=api('POST',f'/api/patients/{p}/ask',t,json={'question':'When did my child take Vitamin D?'}).json()
    assert any('No medication course matching' in s for s in a['summary']) and a['evidence']==[]

def test_cross_family_access_to_history_views_is_rejected():
    t,p=family();seed_history(t,p);other=make_user(uuid.uuid4().hex)
    for path in ('dashboard','history','medication-history','vaccination-chart','lab-series'):
        assert api('GET',f'/api/patients/{p}/{path}',other).status_code==404
    doc=api('GET',f'/api/patients/{p}/documents',t).json()[0]['id']
    for path in ('thumbnail','links'):
        assert api('GET',f'/api/documents/{doc}/{path}',other).status_code==404
    assert api('PATCH',f'/api/documents/{doc}',other,json={'title':'x'}).status_code==404

def write_bundle(root):
    records=root/'records';records.mkdir(parents=True)
    manifest=[]
    for i,(cat,dt) in enumerate([('lab','2026-01-20'),('discharge','2026-03-05'),('vaccination_card',None)],1):
        ref=f'SYN_{i:02d}';raw=page(ref);(records/f'{ref}.jpg').write_bytes(raw)
        manifest.append({'source_id':ref,'filename':f'records/{ref}.jpg','sha256':hashlib.sha256(raw).hexdigest(),'category':cat,'record_date':dt,'title':f'Synthetic {ref}'})
    (root/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
    curated={'import_batch':'synthetic-bundle','expected_dob':'2025-06-15',
        'documents':{'SYN_03':{'category':'vaccination_card','record_date':None,'title':'Synthetic card','notes':'Undated card'}},
        'vaccines':[{'name':'PCV','dose_label':'Booster 1','given_date':None,'due_date':'2026-11-03','source_id':'SYN_03','source_type':'vaccination_card','note':'due only'}],
        'medicines':[{'name':'Drug Z','course_type':'discharge_prescription','source_id':'SYN_02','instructions':'As typed','prescription_date':'2026-03-05'}],
        'observations':[{'record_date':'2026-01-20','test_name':'CRP','value':'3.2','unit':'mg/L','specimen':'serum','reference_range':'0 - 6','source_id':'SYN_01','verified':True,'organism':'','culture_findings':'','susceptibility':''}],
        'admissions':[{'key':'a1','facility':'Fictional Hospital','doctor':'','diagnosis':'As typed','treatment_plan':'As typed','admitted_on':'2026-03-01','discharged_on':'2026-03-05','source_id':'SYN_02','discharge_source_id':'SYN_02'}],
        'growth':[{'record_date':'2026-03-05','weight_kg':'9.5','milestone':'Discharge weight','source_id':'SYN_02','note':'typed'}],
        'episodes':[{'title':'Synthetic hospitalization','started_on':'2026-03-01','ended_on':'2026-03-05','source_id':'SYN_02','symptoms':'s','documented_diagnosis':'d','notes':'n','documents':['SYN_02'],'admission':'a1','medicines':['Drug Z'],'observation_dates':['2026-01-20']}],
        'review_drafts':{'SYN_02':{'text':'[REVIEWER TRANSCRIPTION – UNVERIFIED HANDWRITING]\nSyp Example 2.5 ml BD','items':[{'kind':'medicine','page':1,'fields':{'name':'Example','instructions':'[handwriting – verify] 2.5 ml BD'},'verified':False,'activate_reminders':False}]}}}
    (root/'curated_records.json').write_text(json.dumps(curated),encoding='utf-8')

@pytest.mark.skipif(os.name!='nt' and not os.environ.get('BABYCARE_RUN_IMPORT_TEST'),reason='runs a live uvicorn subprocess')
def test_import_bundle_script_is_idempotent_and_activates_nothing(tmp_path):
    import socket,time,urllib.request
    root=Path(__file__).resolve().parents[1]
    with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
    env={**os.environ,'BABYCARE_DATA_DIR':str(tmp_path/'data'),'PYTHONPATH':str(root)};env.pop('DATABASE_URL',None)
    server=subprocess.Popen([sys.executable,'-m','uvicorn','api.main:app','--host','127.0.0.1','--port',str(port),'--no-access-log'],cwd=root,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    try:
        for _ in range(60):
            try:urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health',timeout=1);break
            except Exception:time.sleep(0.5)
        def call(method,path,body=None,token=None):
            req=urllib.request.Request(f'http://127.0.0.1:{port}'+path,data=json.dumps(body).encode() if body is not None else None,method=method,headers={'Content-Type':'application/json','X-Requested-With':'BabyCare',**({'Authorization':'Bearer '+token} if token else {})})
            with urllib.request.urlopen(req) as r:return json.loads(r.read())
        tok=call('POST','/api/auth/register',{'email':'bundle@test.invalid','password':'fictional-bundle-password'})['token']
        pid=call('POST','/api/patients',{'name':'Bundle Child','dob':'2025-06-15'},tok)['id']
        wrong=call('POST','/api/patients',{'name':'Other Child','dob':'2024-01-01'},tok)['id']
        bundle=tmp_path/'bundle';write_bundle(bundle)
        base=[sys.executable,str(root/'scripts'/'import_bundle.py'),'--bundle',str(bundle),'--url',f'http://127.0.0.1:{port}','--yes']
        env2={**env,'BABYCARE_IMPORT_TOKEN':tok,'PYTHONIOENCODING':'utf-8'}
        dry=subprocess.run(base+['--patient-id',str(pid),'--dry-run'],capture_output=True,text=True,env=env2,timeout=120)
        assert dry.returncode==0 and 'Dry run complete' in dry.stdout,dry.stdout+dry.stderr
        assert call('GET',f'/api/patients/{pid}/documents',token=tok)==[]
        blocked=subprocess.run(base+['--patient-id',str(wrong)],capture_output=True,text=True,env=env2,timeout=120)
        assert blocked.returncode!=0 and 'DOB' in (blocked.stdout+blocked.stderr) and call('GET',f'/api/patients/{wrong}/documents',token=tok)==[]
        first=subprocess.run(base+['--patient-id',str(pid)],capture_output=True,text=True,env=env2,timeout=240)
        assert first.returncode==0,first.stdout+first.stderr
        second=subprocess.run(base+['--patient-id',str(pid)],capture_output=True,text=True,env=env2,timeout=240)
        assert second.returncode==0 and "'vaccines': 0, 'medicines': 0, 'observations': 0, 'admissions': 0, 'growth': 0, 'episodes': 0, 'review_drafts': 0" in second.stdout,second.stdout
        docs=call('GET',f'/api/patients/{pid}/documents',token=tok)
        assert len(docs)==3 and {d['source_ref'] for d in docs}=={'SYN_01','SYN_02','SYN_03'} and all(d['import_batch']=='synthetic-bundle' for d in docs)
        assert len(call('GET',f'/api/patients/{pid}/vaccines',token=tok))==1 and len(call('GET',f'/api/patients/{pid}/medicines',token=tok))==1
        assert len(call('GET',f'/api/patients/{pid}/observations',token=tok))==1 and len(call('GET',f'/api/patients/{pid}/episodes',token=tok))==1
        vax=call('GET',f'/api/patients/{pid}/vaccines',token=tok)[0];med=call('GET',f'/api/patients/{pid}/medicines',token=tok)[0]
        assert vax['confirmed'] is False and med['status']=='draft' and med['course_type']=='discharge_prescription'
        assert call('GET',f'/api/patients/{pid}/reminders?days=120',token=tok)['items']==[]
        card=next(d for d in docs if d['source_ref']=='SYN_02')
        review=call('GET',f'/api/documents/{card["id"]}/review',token=tok)
        assert review['status']=='needs_review' and review['items'][0]['verified'] is False
        assert call('GET',f'/api/patients/{pid}/dashboard',token=tok)['documents']['total']==3
    finally:
        server.terminate();server.wait(timeout=20)
