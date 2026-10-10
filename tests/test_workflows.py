"""Integration tests use synthetic records; provider calls are tested via explicit fakes."""
import io
import json
import os
import subprocess
import sys
import uuid
from datetime import date,datetime,timedelta,timezone
from pathlib import Path
from zoneinfo import ZoneInfo
import pytest
from PIL import Image,ImageDraw
from tests.test_api import api,make_user,make_child,make_doc,client
from api.database import SessionLocal
from api.models import RateWindow,Reminder,Notification,Medicine,Document,PushDevice,Patient
from api.crypto import decrypt
from api.reminders import reminder_items,reconcile,local_time

@pytest.fixture(autouse=True)
def clear_rate_window():
    with SessionLocal() as db:db.query(RateWindow).delete();db.commit()

def setup_family():
    t=make_user(uuid.uuid4().hex);p=make_child(t);d=make_doc(t,p,'vaccination_card');return t,p,d

def review_body(due=None,given=None):
    return {'version':1,'text':'Synthetic verified transcript','checked_against_original':True,'items':[{'kind':'vaccine','page':1,'verified':True,'activate_reminders':bool(due),'fields':{'name':'Fictional vaccine','dose_label':'1','due_date':due,'given_date':given}}]}

def test_review_is_atomic_idempotent_and_requires_human_check():
    t,p,d=setup_family();due=(date.today()+timedelta(days=10)).isoformat();body=review_body(due)
    body['items'][0]['verified']=False
    assert api('POST',f'/api/documents/{d}/review/confirm',t,json=body).status_code==422
    assert api('GET',f'/api/patients/{p}/vaccines',t).json()==[]
    body['items'][0]['verified']=True
    r=api('POST',f'/api/documents/{d}/review/confirm',t,json=body)
    assert r.status_code==200,r.text
    assert api('POST',f'/api/documents/{d}/review/confirm',t,json=body).status_code==409
    v=api('GET',f'/api/patients/{p}/vaccines',t).json()[0]
    assert v['confirmed'] and v['given_date'] is None and v['source_page']==1
    assert len(api('GET',f'/api/patients/{p}/reminders?days=20',t).json()['items'])==3

def test_reviewed_prescription_never_auto_activates_reminders():
    t,p,d=setup_family();when=(date.today()+timedelta(days=1)).isoformat()
    body={'checked_against_original':True,'items':[{'kind':'medicine','page':1,'verified':True,'fields':{'name':'Fictional Vitamin D','instructions':'As written by clinician','start_date':when,'end_date':when,'schedule_times':'09:00'}}]}
    r=api('POST',f'/api/documents/{d}/review/confirm',t,json=body)
    assert r.status_code==200,r.text
    assert not api('GET',f'/api/patients/{p}/reminders',t).json()['items']


def test_completed_card_dose_and_next_appointment_are_separate():
    from api.extraction import structured_candidates
    due=(date.today()+timedelta(days=10)).isoformat()
    candidates=structured_candidates(f'MMR dose 1 given: 2026-09-15 next: {due}; batch: B123; clinic: Fictional clinic','vaccination_card')
    assert candidates[0]['fields']['given_date']=='2026-09-15'
    assert candidates[0]['fields']['next_due_date']==due
    assert candidates[0]['fields']['batch_number']=='B123'
    assert candidates[0]['fields']['dose_label']=='1'
    t,p,d=setup_family()
    candidates[0].update(verified=True,activate_reminders=True)
    result=api('POST',f'/api/documents/{d}/review/confirm',t,json={'items':candidates,'checked_against_original':True})
    assert result.status_code==200,result.text
    rows=api('GET',f'/api/patients/{p}/vaccines',t).json()
    assert len(rows)==2
    completed=next(v for v in rows if v['given_date']);upcoming=next(v for v in rows if not v['given_date'])
    assert completed['confirmed'] and upcoming['confirmed']
    assert upcoming['dose_label']=='' and upcoming['batch_number']=='' # Do not invent the next dose number/batch.
    assert len(api('GET',f'/api/patients/{p}/reminders?days=20',t).json()['items'])==3
    assert 'Verified administration record' in api('GET',f'/api/patients/{p}/doctor-summary',t).text
    revised=(date.today()+timedelta(days=13)).isoformat()
    assert api('PATCH',f'/api/vaccines/{upcoming["id"]}',t,json={'due_date':revised}).status_code==200
    history=api('GET',f'/api/patients/{p}/record-revisions',t).json()
    assert history[0]['previous_values']['due_date']==due
    assert not api('GET',f'/api/patients/{p}/reminders?days=20',t).json()['items']

def test_review_wrong_page_and_cross_family_are_rejected():
    t,p,d=setup_family();body=review_body(given='2026-01-05');body['items'][0]['page']=2
    assert api('POST',f'/api/documents/{d}/review/confirm',t,json=body).status_code==422
    other=make_user(uuid.uuid4().hex)
    for endpoint in ('review','pages/1','extraction','download'):
        assert api('GET',f'/api/documents/{d}/{endpoint}',other).status_code==404

def test_duplicate_vaccine_requires_explicit_acknowledgment():
    t,p,d=setup_family();body={'name':'Fictional vaccine','dose_label':'1','source_document_id':d}
    assert api('POST',f'/api/patients/{p}/vaccines',t,json=body).status_code==201
    assert api('POST',f'/api/patients/{p}/vaccines',t,json=body).status_code==409
    assert api('POST',f'/api/patients/{p}/vaccines',t,json={**body,'duplicate_confirmed':True}).status_code==201

def test_unknown_end_date_and_multiple_medication_courses_remain_distinct():
    t,p,d=setup_family()
    for start in ('2026-01-01','2026-03-01','2026-06-01'):
        r=api('POST',f'/api/patients/{p}/medicines',t,json={'name':'Calcium','instructions':'Copied from doctor','start_date':start,'source_document_id':d})
        assert r.status_code==201
        assert api('POST',f'/api/medicines/{r.json()["id"]}/confirm',t,json={'checked_against_prescription':True}).status_code==200
    reply=api('POST',f'/api/patients/{p}/ask',t,json={'question':'When was calcium given?'}).json()
    assert len(reply['evidence'])==3
    assert all('No actual given events' in e['statement'] and 'not established' in e['statement'] for e in reply['evidence'])


def test_partial_prescription_revision_retains_prior_optional_fields():
    t,p,d=setup_family()
    m=api('POST',f'/api/patients/{p}/medicines',t,json={'name':'Fictional course','instructions':'Original text','indication':'As documented','actual_start_date':'2026-01-02','source_document_id':d}).json()
    changed=api('PATCH',f'/api/medicines/{m["id"]}',t,json={'name':'Fictional course','instructions':'Revised text'}).json()
    assert changed['indication']=='As documented' and changed['actual_start_date']=='2026-01-02'
    assert changed['source_document_id']==d and changed['end_date'] is None and changed['status']=='draft'
    assert api('GET',f'/api/medicines/{m["id"]}/revisions',t).json()[0]['snapshot']['instructions']=='Original text'

def test_persistent_worker_dedupe_and_acknowledgement_do_not_administer():
    t,p,d=setup_family();when=date.today()+timedelta(days=10)
    assert api('POST',f'/api/documents/{d}/review/confirm',t,json=review_body(when.isoformat())).status_code==200
    now=datetime.combine(when-timedelta(days=7),datetime.min.time(),ZoneInfo('Asia/Kolkata')).replace(hour=9,minute=1).astimezone(timezone.utc)
    with SessionLocal() as db:
        person=db.get(Patient,p);reconcile(db,person,now);reconcile(db,person,now)
        assert db.query(Notification).join(Reminder).filter(Reminder.patient_id==p).count()==1
    ns=api('GET',f'/api/patients/{p}/notifications',t).json()['items'];assert len(ns)==1
    assert api('POST',f'/api/notifications/{ns[0]["id"]}/action',t,json={'action':'acknowledge'}).status_code==200
    assert api('GET',f'/api/patients/{p}/vaccines',t).json()[0]['given_date'] is None

def test_timezone_offsets_and_dst_rejection():
    assert local_time(datetime(2026,10,3,9), 'Asia/Kolkata').utcoffset()==timedelta(hours=5,minutes=30)
    with pytest.raises(ValueError):local_time(datetime(2026,3,8,2,30),'America/New_York')
    with pytest.raises(ValueError):local_time(datetime(2026,11,1,1,30),'America/New_York')
    from api.vaccination import reference
    r=reference(date(2024,2,29));assert next(x for x in r if x['name']=='Td')['reference_date']=='2034-02-28'
    assert all(x['schedule']=='IAP_private' for x in reference(date(2025,1,1),'IAP_private'))

def test_lab_trends_separate_units_specimens_and_unverified_values():
    t,p,d=setup_family()
    for unit,specimen,verified in [('mg/L','blood',True),('mg/dL','blood',True),('mg/L','urine',True),('mg/L','blood',False)]:
        r=api('POST',f'/api/patients/{p}/observations',t,json={'test_name':'CRP','value':'4','unit':unit,'specimen':specimen,'verified':verified,'record_date':'2026-01-01','source_document_id':d})
        assert r.status_code==201
    result=api('GET',f'/api/patients/{p}/lab-trends',t).json()
    assert len(result['groups'])==3
    assert sum(x['plot_eligible'] for g in result['groups'] for x in g['points'])==3

def test_read_only_invitation_and_immediate_revocation():
    owner,p,d=setup_family();reader=make_user(uuid.uuid4().hex);email=api('GET','/api/me',reader).json()['email']
    inv=api('POST',f'/api/patients/{p}/invitations',owner,json={'email':email,'role':'viewer','guardian_permission':True}).json()
    r=api('POST','/api/invitations/accept',reader,json={'invitation_code':inv['invitation_code']});assert r.status_code==200,r.text
    assert api('GET',f'/api/documents/{d}/download',reader).status_code==200
    assert api('POST',f'/api/patients/{p}/medicines',reader,json={'name':'No permission'}).status_code==403
    assert api('POST',f'/api/patients/{p}/ask',reader,json={'question':'What vaccines are documented?'}).status_code==200
    access=api('GET',f'/api/patients/{p}/family',owner).json()['members'][0]['id']
    assert api('DELETE',f'/api/patients/{p}/family/{access}',owner).status_code==200
    assert api('GET',f'/api/documents/{d}/download',reader).status_code==404

def test_signed_file_expiry_and_session_revocation():
    t,p,d=setup_family();url=api('POST',f'/api/documents/{d}/signed-access',t).json()['url']
    assert client.get(url).status_code==200
    assert api('POST','/api/auth/logout',t).status_code==200
    assert client.get(url).status_code==404

def test_web_cookie_csrf_and_session_not_exposed():
    email=uuid.uuid4().hex+'@test.invalid'
    r=api('POST','/api/auth/register',json={'email':email,'password':'safe-fictional-password','client':'web'})
    assert r.status_code==201 and 'token' not in r.json()
    assert 'HttpOnly' in r.headers['set-cookie'] and 'SameSite=strict' in r.headers['set-cookie']
    assert api('POST','/api/patients',json={'name':'Fictional','dob':'2025-01-01'}).status_code==403
    assert api('POST','/api/patients',json={'name':'Fictional','dob':'2025-01-01'},headers={'X-Requested-With':'BabyCare'}).status_code==201
    client.cookies.clear()

def test_admission_series_attendance_and_actual_treatment_are_separate():
    t,p,d=setup_family()
    admission=api('POST',f'/api/patients/{p}/admissions',t,json={'facility':'Fictional Hospital','admitted_on':'2026-01-01','source_document_id':d}).json()
    visits=[{'facility':'Fictional Hospital','purpose':'Documented review','planned_at':f'2026-01-0{i+1}T09:00','source_document_id':d,'admission_id':admission['id'],'confirmed':True} for i in range(5)]
    r=api('POST',f'/api/patients/{p}/hospital-visit-series',t,json={'visits':visits,'checked_against_hospital_plan':True})
    assert r.status_code==201,r.text
    vid=r.json()[0]['id'];assert api('PATCH',f'/api/hospital-visits/{vid}',t,json={'status':'attended'}).status_code==200
    assert api('GET',f'/api/patients/{p}/administrations',t).json()==[]
    body={'medication':'Synthetic IV record','occurred_at':'2026-01-01T09:30','source_document_id':d,'checked_administration_record':True,'idempotency_key':'hospital-'+str(p)}
    assert api('POST',f'/api/hospital-visits/{vid}/administrations',t,json=body).status_code==201
    assert api('POST',f'/api/hospital-visits/{vid}/administrations',t,json=body).status_code==201
    assert len(api('GET',f'/api/patients/{p}/administrations',t).json())==1

def test_episode_comparison_and_export_have_sources_not_inferred_reinfection():
    import zipfile
    t,p,d=setup_family()
    for specimen in ['urine','stool']:
        api('POST',f'/api/patients/{p}/observations',t,json={'test_name':'Culture','value':'Reported growth','specimen':specimen,'organism':'E. coli','record_date':'2026-01-01','source_document_id':d,'verified':True})
    reply=api('GET',f'/api/patients/{p}/episode-comparison',t).json()
    assert len(reply['evidence'])==2
    assert all(x['source_document_id']==d for x in reply['evidence'])
    assert any('do not establish' in w for w in reply['warnings'])
    export=api('GET',f'/api/patients/{p}/export',t)
    with zipfile.ZipFile(io.BytesIO(export.content)) as archive:
        assert 'records.json' in archive.namelist()
        assert f'originals/document-{d}.png' in archive.namelist()

def test_vision_requires_explicit_consent_without_network(monkeypatch):
    t,p,d=setup_family()
    def no_network(*a,**kw):raise AssertionError('No document should leave the server')
    monkeypatch.setattr('httpx.post',no_network)
    assert api('POST',f'/api/documents/{d}/vision-extract',t,json={'provider_host':'example.invalid'}).status_code==422

def test_card_parser_tracks_pages_and_never_guesses_ambiguous_dates():
    from api.extraction import structured_candidates
    rows=structured_candidates('=== PAGE 2 ===\nMMR given 05/06/2026\nPCV next due 2026-12-03','vaccination_card')
    assert rows[0]['page']==2 and 'given_date' not in rows[0]['fields']
    assert rows[1]['fields']['due_date']=='2026-12-03'
    assert not any(r['verified'] for r in rows)

def test_rate_limit_persists():
    from api.guardrails import rate_allowed
    key='test-'+uuid.uuid4().hex
    assert rate_allowed(key,limit=2)
    assert rate_allowed(key,limit=2)
    assert not rate_allowed(key,limit=2)
