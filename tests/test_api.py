"""Safe-workflow regression tests. Synthetic information only, never real patient data."""
import io
import os
import tempfile
from datetime import date, datetime, timedelta
from PIL import Image

# Set data root BEFORE importing the API; no real user data is accessed.
os.environ['BABYCARE_DATA_DIR']=tempfile.mkdtemp(prefix='babycare_test_')
from fastapi.testclient import TestClient
from api.main import app

client=TestClient(app)

def api(method,path,token=None,**kwargs):
    headers=kwargs.pop('headers',{})
    if token:headers['Authorization']='Bearer '+token
    return client.request(method,path,headers=headers,**kwargs)

def make_user(n):
    r=api('POST','/api/auth/register',json={'email':f'guardian{n}@test.invalid','password':'correct-horse-battery-2026'})
    assert r.status_code==201,r.text
    return r.json()['token']

def make_child(token,name='Fictional Child'):
    r=api('POST','/api/patients',token,json={'name':name,'dob':'2025-06-15','tz':'Asia/Kolkata'})
    assert r.status_code==201,r.text
    return r.json()['id']

def fake_png():
    im=Image.new('RGB',(500,120),'white');b=io.BytesIO();im.save(b,format='PNG');return b.getvalue()

def make_doc(token,pid,category='prescription'):
    r=api('POST',f'/api/patients/{pid}/documents',token,
         data={'category':category,'title':'Synthetic test sheet'},files={'file':('test.png',fake_png(),'image/png')})
    assert r.status_code==201,r.text
    return r.json()['id']

def test_authentication_and_family_isolation():
    a=make_user('family_a');b=make_user('family_b');p=make_child(a,'A');other=make_child(b,'B')
    assert api('GET',f'/api/patients/{p}/documents',b).status_code==404
    assert api('GET',f'/api/patients/{other}/vaccines',a).status_code==404
    assert api('GET',f'/api/patients/{other}/reminders',a).status_code==404
    assert api('GET',f'/api/patients/{p}/documents').status_code==401
    assert len(api('GET','/api/patients',a).json())==1

def test_ocr_has_no_side_effects_or_reminders():
    t=make_user('ocr');p=make_child(t)
    d=make_doc(t,p,category='vaccination_card')
    out=api('POST',f'/api/documents/{d}/extract',t)
    assert out.status_code==200,out.text
    assert out.json()['status']=='needs_review'
    assert api('GET',f'/api/patients/{p}/vaccines',t).json()==[]
    assert api('GET',f'/api/patients/{p}/reminders',t).json()['items']==[]
    assert api('GET',f'/api/documents/{d}/download',t).content==fake_png()

def test_vaccine_reference_never_sets_verified_or_due_alert():
    t=make_user('vax_ref');p=make_child(t)
    r=api('POST',f'/api/patients/{p}/vaccine-reference/import',t)
    assert r.status_code==200 and r.json()['created']>20
    entries=api('GET',f'/api/patients/{p}/vaccines',t).json()
    assert all(v['confirmed'] is False for v in entries)
    assert not api('GET',f'/api/patients/{p}/reminders?days=120',t).json()['items']
    x=api('PATCH',f'/api/vaccines/{entries[0]["id"]}',t,json={'confirmed':True})
    assert x.status_code==422

def test_confirmed_card_date_creates_reminder_but_given_date_removes_it():
    t=make_user('vax_due');p=make_child(t);d=make_doc(t,p,category='vaccination_card')
    tomorrow=(datetime.now().date()+timedelta(days=2)).isoformat()
    r=api('POST',f'/api/patients/{p}/vaccines',t,json={'name':'Example vaccine','dose_label':'1','due_date':tomorrow,'source_document_id':d,'source_type':'vaccination_card'})
    assert r.status_code==201,r.text
    item=r.json();assert item['confirmed'] is False
    assert not api('GET',f'/api/patients/{p}/reminders?days=20',t).json()['items']
    assert api('PATCH',f'/api/vaccines/{item["id"]}',t,json={'confirmed':True}).status_code==200
    r=api('GET',f'/api/patients/{p}/reminders?days=20',t).json()['items']
    assert len(r)>=1 and all(x['kind']=='vaccine' and x['source_document_id']==d for x in r)
    assert any(x['key'].endswith('1d') for x in r) and any(x['key'].endswith('0d') for x in r)
    assert api('PATCH',f'/api/vaccines/{item["id"]}',t,json={'given_date':datetime.now().date().isoformat()}).status_code==200
    assert not api('GET',f'/api/patients/{p}/reminders?days=20',t).json()['items']

def test_medicine_requires_original_and_revision_disables_reminders():
    t=make_user('med');p=make_child(t);d=make_doc(t,p)
    tomorrow=(datetime.now().date()+timedelta(days=2)).isoformat()
    data={'name':'Example clinician medicine','instructions':'Synthetic doctor instructions only','start_date':tomorrow,'end_date':tomorrow,'schedule_times':'09:00'}
    no_source=api('POST',f'/api/patients/{p}/medicines',t,json=data).json()
    assert api('POST',f'/api/medicines/{no_source["id"]}/confirm',t,json={'checked_against_prescription':True}).status_code==422
    data['source_document_id']=d
    med=api('POST',f'/api/patients/{p}/medicines',t,json=data).json()
    assert not api('GET',f'/api/patients/{p}/reminders?days=20',t).json()['items']
    assert api('POST',f'/api/medicines/{med["id"]}/confirm',t,json={'checked_against_prescription':True}).status_code==200
    assert any(r['kind']=='medicine' for r in api('GET',f'/api/patients/{p}/reminders?days=20',t).json()['items'])
    r=api('PATCH',f'/api/medicines/{med["id"]}',t,json={**data,'instructions':'Revised prescription; requires reconfirmation'})
    assert r.status_code==200 and r.json()['status']=='draft' and r.json()['version']==2
    assert not api('GET',f'/api/patients/{p}/reminders?days=20',t).json()['items']
    history=api('GET',f'/api/medicines/{med["id"]}/revisions',t).json()
    assert history[0]['snapshot']['instructions']=='Synthetic doctor instructions only'
    assert api('POST',f'/api/medicines/{med["id"]}/events',t,json={'event_type':'given','occurred_at':datetime.now().strftime('%Y-%m-%dT%H:%M:%S'),'idempotency_key':'medicine-one'}).status_code==409

def test_hospital_visit_does_not_create_administration_event():
    t=make_user('hospital');p=make_child(t);d=make_doc(t,p,category='hospital')
    next_day=(datetime.now()+timedelta(days=1)).strftime('%Y-%m-%dT10:00:00')
    visit=api('POST',f'/api/patients/{p}/hospital-visits',t,json={'facility':'Fictional Hospital','planned_at':next_day,'purpose':'Clinician-ordered follow-up','confirmed':True,'source_document_id':d}).json()
    due=api('GET',f'/api/patients/{p}/reminders?days=5',t).json()['items']
    assert any(i['kind']=='hospital' for i in due)
    assert api('PATCH',f'/api/hospital-visits/{visit["id"]}',t,json={'status':'attended','note':'Caregiver reports hospital attendance only'}).status_code==200
    assert not api('GET',f'/api/patients/{p}/reminders?days=5',t).json()['items']
    assert api('GET',f'/api/patients/{p}/medicines',t).json()==[]
    summary=api('GET',f'/api/patients/{p}/doctor-summary',t).text
    assert 'does NOT mean treatment administered' in summary


def test_cross_family_source_document_is_rejected():
    a=make_user('cross_a');b=make_user('cross_b');p1=make_child(a);p2=make_child(b)
    doc=make_doc(a,p1)
    r=api('POST',f'/api/patients/{p2}/medicines',b,json={'name':'Test','instructions':'From another family','source_document_id':doc})
    assert r.status_code==400
    assert api('GET',f'/api/documents/{doc}/download',b).status_code==404


def test_vaccine_card_date_regex_never_auto_decides_ambiguous_dates():
    from api.vaccination import card_candidates
    text='BCG given 15/08/2025\nMR-2 next due 06/07/2027\nPCV 1 03/04/2026 06/09/2026'
    rows=card_candidates(text)
    assert len(rows)==3
    assert rows[0]['interpretation']=='possible_given_date'
    assert rows[1]['interpretation']=='possible_due_date'
    assert rows[2]['interpretation']=='ambiguous_multiple_dates'
    assert all(row['review_required'] for row in rows)
