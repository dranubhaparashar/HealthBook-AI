"""Family workspace: profiles of any age, age-aware module defaults, per-profile module settings, vitals journal,
strict separation between profiles, Ask scoping, and HealthBook AI branding. Synthetic data only."""
import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import pytest
from tests.test_api import api, make_user, make_child, client
from api import profiles as PR

def user():return make_user('fam'+uuid.uuid4().hex[:10])
def profile(t,name,dob,**extra):
    r=api('POST','/api/patients',t,json={'name':name,'dob':dob,**extra});assert r.status_code==201,r.text;return r.json()

def test_age_groups_and_default_modules():
    today=date(2026,10,6)
    assert PR.age_group(date(2026,3,1),today)=='infant' and PR.age_group(date(2025,1,1),today)=='toddler' and PR.age_group(date(2018,1,1),today)=='child'
    assert PR.age_group(date(2010,1,1),today)=='teen' and PR.age_group(date(1990,1,1),today)=='adult' and PR.age_group(date(1950,1,1),today)=='senior'
    assert {'diapers','vaccines','growth'}<=PR.DEFAULT_MODULES['infant'] and 'vitals' not in PR.DEFAULT_MODULES['infant']
    assert {'vitals','medicines','labs','share','export'}<=PR.DEFAULT_MODULES['adult'] and 'diapers' not in PR.DEFAULT_MODULES['adult']

def test_profiles_of_any_age_with_relationship_and_modules():
    t=user()
    kid=profile(t,'Fictional Infant','2026-05-01',relationship='child',sex='male')
    adult=profile(t,'Fictional Adult','1988-02-14',relationship='self')
    assert kid['age_group']=='infant' and 'diapers' in kid['modules'] and 'diaper' in kid['care_kinds'] and kid['relationship']=='child' and kid['sex']=='male'
    assert adult['age_group']=='adult' and 'vitals' in adult['modules'] and 'diaper' not in adult['care_kinds'] and adult['modules_custom'] is False
    assert api('POST','/api/patients',t,json={'name':'X','dob':'2000-01-01','relationship':'robot'}).status_code==422
    listed=api('GET','/api/patients',t).json();assert [p['name'] for p in listed]==['Fictional Infant','Fictional Adult'] and all('available_modules' in p for p in listed)
    # per-profile module configuration
    r=api('PUT',f"/api/patients/{adult['id']}/modules",t,json={'enabled':['records','medicines','vitals','labs','export']});assert r.status_code==200,r.text
    assert r.json()['modules']==['records','medicines','labs','vitals','export'] and r.json()['modules_custom'] is True
    assert api('PUT',f"/api/patients/{adult['id']}/modules",t,json={'enabled':['records','teleport']}).status_code==422
    assert api('GET',f"/api/patients/{kid['id']}/profile",t).json()['modules_custom'] is False  # untouched profile keeps defaults
    reset=api('PUT',f"/api/patients/{adult['id']}/modules",t,json={'enabled':None}).json();assert reset['modules_custom'] is False and 'share' in reset['modules']
    meta=api('PATCH',f"/api/patients/{adult['id']}/profile",t,json={'relationship':'parent','sex':'female'}).json();assert meta['relationship']=='parent' and meta['sex']=='female'
    assert api('PATCH',f"/api/patients/{adult['id']}/profile",t,json={'sex':'unknown-thing'}).status_code==422
    # legacy PATCH /patients/{pid} still works and keeps the new fields
    r=api('PATCH',f"/api/patients/{adult['id']}",t,json={'name':'Fictional Adult Renamed','dob':'1988-02-14','tz':'Asia/Kolkata','relationship':'parent'});assert r.status_code==200,r.text
    assert api('GET',f"/api/patients/{adult['id']}/profile",t).json()['name']=='Fictional Adult Renamed'

def test_vitals_journal_is_per_profile_validated_and_answerable():
    t=user();a=profile(t,'Fictional Adult A','1980-06-01',relationship='self');b=profile(t,'Fictional Adult B','1975-03-03',relationship='partner')
    d=datetime.now(ZoneInfo('Asia/Kolkata')).date().isoformat()
    r=api('POST',f"/api/patients/{a['id']}/vitals",t,json={'at':f'{d}T07:30','systolic':122,'diastolic':78,'heart_rate':70,'spo2':98,'client_key':'k1'});assert r.status_code==201,r.text
    again=api('POST',f"/api/patients/{a['id']}/vitals",t,json={'at':f'{d}T07:30','systolic':122,'diastolic':78,'client_key':'k1'}).json();assert again['id']==r.json()['id'] and again['created'] is False
    api('POST',f"/api/patients/{a['id']}/vitals",t,json={'at':f'{d}T08:00','glucose_mg_dl':96,'glucose_context':'fasting','weight_kg':71.4,'meal':'oats and tea'})
    assert api('POST',f"/api/patients/{a['id']}/vitals",t,json={'at':f'{d}T08:10','systolic':120}).status_code==422          # needs both BP values
    assert api('POST',f"/api/patients/{a['id']}/vitals",t,json={'at':f'{d}T08:10','spo2':120}).status_code==422              # out of range
    assert api('POST',f"/api/patients/{a['id']}/vitals",t,json={'at':'2099-01-01T08:00','heart_rate':70}).status_code==422   # future
    assert api('POST',f"/api/patients/{a['id']}/vitals",t,json={'at':f'{d}T08:10'}).status_code==422                         # empty
    lst=api('GET',f"/api/patients/{a['id']}/vitals?days=7",t).json();assert len(lst['entries'])==2 and lst['entries'][0]['summary'].startswith('BP 122/78')
    summ=api('GET',f"/api/patients/{a['id']}/vitals/summary?days=7",t).json()
    assert summ['latest']['glucose_mg_dl']==96 and any(s.startswith('Blood pressure recorded on 1 of 7 days') for s in summ['statements'])
    assert not any(w in ' '.join(summ['statements']).casefold() for w in ('hypertension','diabetes','normal','abnormal','diagnos'))
    # strict separation: profile B sees nothing of A, another family sees nothing at all
    assert api('GET',f"/api/patients/{b['id']}/vitals?days=7",t).json()['entries']==[]
    t2=user();assert api('GET',f"/api/patients/{a['id']}/vitals",t2).status_code==404 and api('POST',f"/api/patients/{a['id']}/vitals",t2,json={'heart_rate':70}).status_code==404
    # Ask is scoped to the selected profile
    ans=api('POST',f"/api/patients/{a['id']}/lifetime/ask",t,json={'question':'What was my blood pressure today?'})
    assert ans.status_code==200,ans.text;body=ans.json();text=body.get('answer','')
    assert '122' in text and '78' in text and body.get('mode')=='daily_care_structured'
    other=api('POST',f"/api/patients/{b['id']}/lifetime/ask",t,json={'question':'What was my blood pressure today?'})
    assert '122/78' not in other.text
    # edit and withdraw keep history
    eid=r.json()['id'];ed=api('PATCH',f'/api/vitals/{eid}',t,json={'heart_rate':72}).json();assert ed['heart_rate']==72 and ed['systolic']==122 and ed['version']==2
    assert api('POST',f'/api/vitals/{eid}/withdraw',t).json()['status']=='withdrawn'
    assert len(api('GET',f"/api/patients/{a['id']}/vitals?days=7",t).json()['entries'])==1
    assert api('PATCH',f'/api/vitals/{eid}',t2,json={'heart_rate':1}).status_code==404

def test_branding_is_healthbook_everywhere_visible():
    html=client.get('/').text;assert 'HealthBook AI' in html and 'BabyCare' not in html
    man=client.get('/manifest.webmanifest').json();assert man['name'].startswith('HealthBook AI') and man['short_name']=='HealthBook AI'
    opts=api('GET','/api/profiles/options').json();assert opts['brand']=='HealthBook AI' and opts['tagline']=='Your private family health workspace.'
    from api import push_web;assert 'HealthBook AI' in push_web.GENERIC_BODY
    for f in ('web/app.js','web/features.js','web/history.js','web/share.js','web/exports.js','web/analytics.js','web/sw.js','web/doctor.html'):
        txt=open(f,encoding='utf-8').read()
        import re;visible=re.sub(r"babycare_[a-z_]+|babycare-shell-v\d+|X-Requested-With[^,]*","",txt)
        assert 'BabyCare' not in visible,f
