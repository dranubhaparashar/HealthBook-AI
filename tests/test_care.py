"""Daily Care tests: quick-add, idempotent submissions, diaper counts, stool photo observations (never a diagnosis),
GIVEN versus scheduled, calendar views, timezone boundaries, corrections, export, authorization and Ask."""
import io
import json
import uuid
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
import pytest
from PIL import Image
from tests.test_api import api, make_user, make_child, make_doc, client
from api.database import SessionLocal
from api.models import RateWindow
from api import care_vision

@pytest.fixture(autouse=True)
def clear_rate_window():
    with SessionLocal() as db:db.query(RateWindow).delete();db.commit()

@pytest.fixture(autouse=True)
def late_evening_clock(monkeypatch):
    """Freeze 'now' at 23:50 local time today so fixture entries (07:00-15:00) are never 'in the future' whatever the
    wall clock or CI timezone, while 'today' keeps meaning today."""
    from api import care, care_api
    frozen=datetime.combine(datetime.now(ZoneInfo('Asia/Kolkata')).date(),datetime.min.time(),ZoneInfo('Asia/Kolkata')).replace(hour=23,minute=50)
    fixed=lambda:frozen.astimezone(ZoneInfo('UTC'))
    monkeypatch.setattr(care,'utcnow',fixed);monkeypatch.setattr(care_api,'utcnow',fixed)

def family():
    t=make_user('care'+uuid.uuid4().hex[:10]);p=make_child(t);return t,p

def today():return datetime.now(ZoneInfo('Asia/Kolkata')).date()

def jpeg(colour,size=(240,240)):
    im=Image.new('RGB',size,colour);b=io.BytesIO();im.save(b,format='JPEG');return b.getvalue()

def post(t,p,kind,body,expect=201):
    r=api('POST',f'/api/patients/{p}/care/{kind}',t,json=body);assert r.status_code==expect,r.text;return r.json()

def test_quick_add_water_and_duplicate_submission_is_idempotent():
    t,p=family();d=today().isoformat();key='offline-'+uuid.uuid4().hex[:8]
    first=post(t,p,'fluid',{'at':f'{d}T08:30','amount_ml':100,'fluid_type':'water','client_key':key})
    second=post(t,p,'fluid',{'at':f'{d}T08:30','amount_ml':100,'fluid_type':'water','client_key':key})
    assert second['id']==first['id'] and second.get('existing')
    post(t,p,'fluid',{'at':f'{d}T11:00','amount_ml':60,'fluid_type':'ors'})
    post(t,p,'fluid',{'at':f'{d}T12:00','fluid_type':'breastmilk','duration_minutes':12})
    post(t,p,'fluid',{'at':f'{d}T13:00','custom_amount':'a few sips','fluid_type':'water'})
    assert post(t,p,'fluid',{'at':f'{d}T14:00','fluid_type':'water'},422)
    day=api('GET',f'/api/patients/{p}/care/day',t).json()
    assert day['fluids']['total_ml']==160 and day['fluids']['events']==4 and day['fluids']['unknown_amount_events']==2 and day['fluids']['by_type']=={'water':100,'ors':60}
    assert 'hydration' in day['warnings'][0]
    assert api('POST',f'/api/patients/{p}/care/fluid',t,json={'at':'2099-01-01T08:00','amount_ml':10}).status_code==422

def test_diaper_counts_time_since_and_threshold_language():
    t,p=family();d=today().isoformat()
    post(t,p,'diaper',{'at':f'{d}T07:00','kind':'wet','urine_amount':'medium'})
    post(t,p,'diaper',{'at':f'{d}T09:30','kind':'mixed'})
    post(t,p,'diaper',{'at':f'{d}T10:15','kind':'stool'})
    post(t,p,'diaper',{'at':f'{d}T11:00','kind':'dry'})
    day=api('GET',f'/api/patients/{p}/care/day',t).json()['diapers']
    assert day['wet']==2 and day['stool']==2 and day['mixed']==1 and day['dry_checks']==1 and day['total_changes']==3
    assert day['minutes_since_last_wet'] is not None and day['minutes_since_last_stool'] is not None and day['threshold'] is None
    assert api('PUT',f'/api/patients/{p}/care/settings',t,json={'wet_diaper_threshold_per_day':6}).status_code==422  # a source is required; the app never invents thresholds
    assert api('PUT',f'/api/patients/{p}/care/settings',t,json={'wet_diaper_threshold_per_day':6,'threshold_source':'Dr A, 2 Oct 2026'}).status_code==200
    thr=api('GET',f'/api/patients/{p}/care/day',t).json()['diapers']['threshold']
    assert thr['value']==6 and 'Review requested' in thr['message'] and 'dehydrat' not in json.dumps(thr).casefold()

def test_stool_photo_observation_is_descriptive_and_never_a_diagnosis():
    t,p=family();d=today().isoformat()
    s=post(t,p,'stool',{'at':f'{d}T11:40','consistency':'loose','amount':'medium','mucus_observed':False,'create_diaper':True})
    r=api('POST',f'/api/care/stool/{s["id"]}/photos',t,files={'file':('potty.jpg',jpeg((120,80,40)),'image/jpeg')},data={'analyse':'true'})
    assert r.status_code==201,r.text;obs=r.json()['ai_observation']
    assert obs['disclaimer']=='Image-based observation only — not a diagnosis.' and obs['apparent_colour'] in ('brown','yellow')
    text=json.dumps({k:v for k,v in obs.items() if k!='disclaimer'}).casefold()
    for word in care_vision.FORBIDDEN_WORDS:assert word not in text,word
    red=post(t,p,'stool',{'at':f'{d}T15:00'})
    r2=api('POST',f'/api/care/stool/{red["id"]}/photos',t,files={'file':('red.jpg',jpeg((200,30,30)),'image/jpeg')}).json()
    assert r2['ai_observation']['consider_clinical_review'] and r2['ai_observation']['blood_like_colour']=='possible'
    entry=next(x for x in api('GET',f'/api/patients/{p}/care/day',t).json()['stool']['items'] if x['id']==red['id'])
    assert entry['review_message']=='Consider clinical review' and entry['ai_review_status']=='ai_observed' and len(entry['photos'])==1
    photo=api('GET',f'/api/care/stool-photos/{entry["photos"][0]["id"]}',t);assert photo.status_code==200 and photo.content==jpeg((200,30,30))
    fixed=api('POST',f'/api/care/stool/{red["id"]}/observation-review',t,json={'action':'correct','corrections':{'colour':'brown','blood_observed':False}}).json()
    assert fixed['ai_review_status']=='parent_corrected' and fixed['colour']=='brown' and fixed['clinical_review_suggested'] is False
    assert api('GET',f'/api/care/stool/{red["id"]}/history',t).json()['revisions'][0]['previous_values']['ai_review_status']=='ai_observed'
    day=api('GET',f'/api/patients/{p}/care/day',t).json();assert day['diapers']['stool']==1 and day['stool']['entries']==2
    assert 'coli' not in json.dumps(day).casefold()

def test_scheduled_medicine_is_never_counted_as_given():
    t,p=family();doc=make_doc(t,p);d=today().isoformat()
    m=api('POST',f'/api/patients/{p}/medicines',t,json={'name':'Zinc drops','instructions':'1 ml once daily','start_date':(today()-timedelta(days=3)).isoformat(),'end_date':(today()+timedelta(days=5)).isoformat(),'schedule_times':'10:00','dosage':'1','dose_unit':'ml','source_document_id':doc}).json()
    assert api('POST',f'/api/medicines/{m["id"]}/confirm',t,json={'checked_against_prescription':True}).status_code==200
    plan=api('GET',f'/api/patients/{p}/care/medicines/today',t).json()
    assert plan['counts']['due']==1 and plan['counts']['given']==0 and plan['due'][0]['status']=='no_record' and plan['due'][0]['scheduled_local']=='10:00'
    a=api('GET',f'/api/patients/{p}/care/analytics?range=today',t).json()
    assert a['medicine_adherence'][0]['given']==0 and a['medicine_adherence'][0]['due_slots']==1
    given=api('POST',f'/api/patients/{p}/care/medicine/quick',t,json={'medicine_id':m['id'],'action':'given','scheduled_for':plan['due'][0]['scheduled_for'],'at':f'{d}T10:05'})
    assert given.status_code==201,given.text and given.json()['medicine_name']=='Zinc drops' and given.json()['dose']=='1'
    plan=api('GET',f'/api/patients/{p}/care/medicines/today',t).json()
    assert plan['counts']['given']==1 and plan['due'][0]['status']=='given'
    api('POST',f'/api/patients/{p}/care/medicine/quick',t,json={'medicine_name':'Vitamin D','action':'skipped','at':f'{d}T09:00','note':'asleep'})
    api('POST',f'/api/patients/{p}/care/medicine/quick',t,json={'medicine_name':'Calcium','action':'later','at':f'{d}T12:00'})
    counts=api('GET',f'/api/patients/{p}/care/medicines/today',t).json()['counts'];assert counts['skipped']==1 and counts['later']==1 and counts['given']==1
    assert api('POST',f'/api/patients/{p}/care/medicine',t,json={'at':f'{d}T13:00','administration_status':'given'}).status_code==422
    tl=api('GET',f'/api/patients/{p}/care/timeline?filters=medicine',t).json()['items']
    assert [x['status'] for x in tl]==['skipped','given','later']
    ask=api('POST',f'/api/patients/{p}/care/ask',t,json={'question':'Which medicines did I actually give today?'}).json()
    assert ask['mode']=='daily_care_structured' and 'Zinc drops at 10:05' in ask['answer'] and 'Vitamin D skipped' in ask['answer']

def test_timezone_boundaries_group_by_child_local_date():
    t,p=family();d=today()-timedelta(days=1)
    late=post(t,p,'fluid',{'at':f'{d.isoformat()}T23:30','amount_ml':50})
    utc_next=post(t,p,'fluid',{'at':f'{d.isoformat()}T19:30:00+00:00','amount_ml':40})  # 01:00 next day in Asia/Kolkata
    assert late['local_date']==d.isoformat() and utc_next['local_date']==(d+timedelta(days=1)).isoformat()
    rows=api('GET',f'/api/patients/{p}/care/fluid?start={d}&end={d}',t).json();assert [r['id'] for r in rows]==[late['id']]
    tl=api('GET',f'/api/patients/{p}/care/timeline?day={d}',t).json()['items'];assert tl[0]['time']=='23:30'

def test_calendar_views_and_everything_recorded_that_day():
    t,p=family();d=today();doc=make_doc(t,p,'discharge')
    post(t,p,'fluid',{'at':f'{d}T09:00','amount_ml':80});post(t,p,'diaper',{'at':f'{d}T10:00','kind':'wet'});post(t,p,'stool',{'at':f'{d}T12:00','consistency':'formed'})
    post(t,p,'activity',{'at':f'{d}T10:20','ended_at':f'{d}T10:50','kind':'playing'});post(t,p,'sleep',{'at':f'{d}T13:30','ended_at':f'{d}T14:45','kind':'nap'})
    post(t,p,'symptom',{'at':f'{d}T16:00','kind':'cough','severity':'mild'})
    api('POST',f'/api/patients/{p}/admissions',t,json={'facility':'Fictional hospital','admitted_on':(d-timedelta(days=3)).isoformat(),'discharged_on':(d-timedelta(days=1)).isoformat(),'source_document_id':doc})
    month=api('GET',f'/api/patients/{p}/care/calendar/month?year={d.year}&month={d.month}',t).json()
    day=month['days'][d.isoformat()]
    assert day['fluid']==1 and day['diaper']==1 and day['wet']==1 and day['stool']==1 and day['activity']==1 and day['sleep']==1 and day['symptom']==1 and day['fluid_ml']==80
    assert month['days'][(d-timedelta(days=2)).isoformat()]['admission']==1
    week=api('GET',f'/api/patients/{p}/care/calendar/week?start={d-timedelta(days=d.weekday())}',t).json()
    assert len(week['days'])==7 and any(x['date']==d.isoformat() and len(x['items'])>=6 for x in week['days'])
    full=api('GET',f'/api/patients/{p}/care/day?day={d}',t).json()
    kinds=[x['kind'] for x in full['timeline']];assert kinds==sorted(kinds,key=lambda k:[i['kind'] for i in full['timeline']].index(k)) and {'fluid','diaper','stool','activity','sleep','symptom'}<=set(kinds)
    assert full['activity']['active_minutes']==30 and full['sleep']['sleep_minutes']==75
    only=api('GET',f'/api/patients/{p}/care/timeline?day={d}&filters=sleep,symptom',t).json()['items'];assert {x['kind'] for x in only}=={'sleep','symptom'}

def test_analytics_illness_comparison_and_export_use_observational_language():
    t,p=family();d=today();doc=make_doc(t,p)
    for i in range(6):
        day=(d-timedelta(days=5-i)).isoformat()
        post(t,p,'fluid',{'at':f'{day}T09:00','amount_ml':100+i*10});post(t,p,'diaper',{'at':f'{day}T08:00','kind':'wet'})
        if i<4:post(t,p,'stool',{'at':f'{day}T11:00','consistency':'loose','watery':i%2==0})
        post(t,p,'medicine',{'at':f'{day}T10:00','medicine_name':'Zinc','administration_status':'given' if i!=2 else 'skipped'})
    ep=api('POST',f'/api/patients/{p}/episodes',t,json={'title':'Fictional E. coli-related episode','started_on':(d-timedelta(days=4)).isoformat(),'ended_on':(d-timedelta(days=2)).isoformat(),'source_document_id':doc}).json()
    a=api('GET',f'/api/patients/{p}/care/analytics?range=7d',t).json()
    assert a['totals']['loose_or_watery_days']==4 and a['totals']['wet']==6 and a['totals']['fluid_ml']==750
    assert any('Loose or watery stool entries were recorded on 4 of 6' in s for s in a['statements']) and any('Zinc: recorded as given on 5 of 7 days' in s for s in a['statements'])
    text=json.dumps(a).casefold();assert 'cured' not in text and 'caused' not in text.replace('caused a change','') and 'dehydrat' not in text
    choices=api('GET',f'/api/patients/{p}/care/illnesses',t).json();assert choices[0]['ecoli']
    c=api('GET',f'/api/patients/{p}/care/compare?kind=episode&ref_id={ep["id"]}&window_days=3',t).json()
    assert c['during']['days']==3 and c['during']['stool_entries']==3 and c['before']['recorded_days']>=1 and len(c['statements'])==3 and 'observations' in c['warnings'][0]
    assert any('before' in s.casefold() for s in c['statements'])
    exp=api('GET',f'/api/patients/{p}/care/export?preset=illness&kind=episode&ref_id={ep["id"]}&format=txt',t)
    assert exp.status_code==200 and 'DAILY CARE SUMMARY' in exp.text and 'Zinc given' in exp.text and 'not included' in exp.text
    js=api('GET',f'/api/patients/{p}/care/export?preset=7d&include_photos=true',t).json();assert js['photos_included'] and js['photo_document_ids']==[] and js['sections']
    ask=api('POST',f'/api/patients/{p}/care/ask',t,json={'question':'Compare his daily care before and during his E. coli-related episode'}).json()
    assert ask['mode']=='daily_care_structured' and 'Before' in ask['answer'] and 'During' in ask['answer']
    ask=api('POST',f'/api/patients/{p}/care/ask',t,json={'question':'How many days did I record loose stools?'}).json();assert 'recorded on 4 day(s)' in ask['answer']
    ask=api('POST',f'/api/patients/{p}/care/ask',t,json={'question':'Did I give zinc every day this week?'}).json();assert 'Zinc was recorded as given on' in ask['answer']

def test_corrections_keep_history_withdraw_and_authorization():
    t,p=family();d=today().isoformat();other=make_user('care'+uuid.uuid4().hex[:10])
    f=post(t,p,'fluid',{'at':f'{d}T08:00','amount_ml':100})
    fixed=api('PATCH',f'/api/care/fluid/{f["id"]}',t,json={'amount_ml':120}).json();assert fixed['amount_ml']==120 and fixed['version']==2
    hist=api('GET',f'/api/care/fluid/{f["id"]}/history',t).json();assert hist['revisions'][0]['previous_values']['amount_ml']==100
    assert api('POST',f'/api/care/fluid/{f["id"]}/withdraw',t).json()['status']=='withdrawn'
    assert api('GET',f'/api/patients/{p}/care/day',t).json()['fluids']['events']==0
    for path in ('care/day','care/timeline','care/calendar/month?year=2026&month=1','care/analytics','care/export','care/settings','care/illnesses','care/fluid'):
        assert api('GET',f'/api/patients/{p}/{path}',other).status_code==404,path
    assert api('PATCH',f'/api/care/fluid/{f["id"]}',other,json={'amount_ml':1}).status_code==404
    assert api('POST',f'/api/patients/{p}/care/fluid',other,json={'at':f'{d}T08:00','amount_ml':1}).status_code==404
    assert api('GET',f'/api/patients/{p}/care/day').status_code==401

def test_ask_routes_through_the_shared_answerer_and_other_questions_fall_back():
    t,p=family();d=today().isoformat()
    post(t,p,'fluid',{'at':f'{d}T08:00','amount_ml':90});post(t,p,'diaper',{'at':f'{d}T09:00','kind':'wet'});post(t,p,'stool',{'at':f'{d}T10:00','consistency':'soft'})
    post(t,p,'activity',{'at':f'{(today()-timedelta(days=1)).isoformat()}T10:00','kind':'playing','duration_minutes':25})
    shared=lambda q:api('POST',f'/api/patients/{p}/ask',t,json={'question':q}).json()
    assert 'Fluids recorded today: 90 ml' in shared('How much water did he drink today?')['answer']
    assert 'Wet diapers recorded today: 1' in shared('How many wet diapers today?')['answer']
    assert 'Last recorded stool' in shared('When was his last stool?')['answer']
    assert '25 min recorded' in shared('Show his activity yesterday.')['answer']
    assert shared('Which vaccines were given?')['mode']!='daily_care_structured'
    assert shared('How many times has my child had fever since birth?')['mode']=='lifetime_health_structured'
