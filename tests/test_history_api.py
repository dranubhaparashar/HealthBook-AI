"""Regression tests for the read-only history router (api/history_api.py). Synthetic information only.

Covers the medical timeline, medication history (antibiotic word-list match), vaccination chart, laboratory
series and document back-links for an authenticated child profile, plus family isolation and read-only behaviour.
"""
from datetime import date, timedelta

from tests.test_api import api, make_user, make_child, make_doc

HISTORY_PATHS=('dashboard','history','medication-history','vaccination-chart','lab-series')

def seed_child(token,pid,doc):
    future=(date.today()+timedelta(days=2)).isoformat()
    med=api('POST',f'/api/patients/{pid}/medicines',token,json={'name':'Cefixime (Zifi) syrup','instructions':'Synthetic discharge prescription text',
        'source_document_id':doc,'course_type':'discharge_prescription','prescription_date':'2026-09-01'})
    assert med.status_code==201,med.text
    vac=api('POST',f'/api/patients/{pid}/vaccines',token,json={'name':'Example vaccine','dose_label':'1','due_date':future,'source_document_id':doc,'source_type':'vaccination_card'})
    assert vac.status_code==201,vac.text
    # Two haemoglobin results on the same day from different laboratories, with different unit spellings.
    for name,value,unit in (('Hb','10.2','g/dl'),('Haemoglobin','11.9','gm%')):
        r=api('POST',f'/api/patients/{pid}/observations',token,json={'test_name':name,'value':value,'unit':unit,'specimen':'blood','record_date':'2026-09-30','source_document_id':doc})
        assert r.status_code==201,r.text
    return med.json(),vac.json(),future

def test_history_router_is_registered_and_requires_authentication():
    for path in HISTORY_PATHS:
        assert api('GET',f'/api/patients/1/{path}').status_code==401,path
    assert api('GET','/api/documents/1/links').status_code==401
    assert api('GET','/api/documents/1/thumbnail').status_code==401

def test_history_views_only_reflect_stored_records():
    t=make_user('history_a');p=make_child(t);d=make_doc(t,p)
    med,vac,future=seed_child(t,p,d)

    dash=api('GET',f'/api/patients/{p}/dashboard',t)
    assert dash.status_code==200,dash.text
    dash=dash.json()
    assert dash['patient']['id']==p and dash['documents']=={'total':1,'by_category':{'prescription':1},'undated':1,'pending_review':1}
    assert dash['medications']['courses']==1 and dash['medications']['drafts']==1 and dash['medications']['verified_current']==0
    assert dash['medications']['by_type']['discharge_prescription']==1
    assert dash['vaccination']['pending_verification']==1 and dash['vaccination']['next_confirmed'] is None and dash['vaccination']['documented_given']==0
    assert dash['vaccination']['pending_due_dates']==[{'due_date':future,'vaccines':['Example vaccine 1']}]
    assert dash['labs']=={**dash['labs'],'total':2,'verified':0,'unverified':2} and len(dash['labs']['recent'])==2
    assert dash['reviews_pending']==5 and dash['reminders_next_30_days']==0

    hist=api('GET',f'/api/patients/{p}/history',t)
    assert hist.status_code==200,hist.text
    hist=hist.json()
    assert hist['birth']=='2025-06-15' and hist['episodes']==[] and hist['gaps']==[] and hist['all_events_count']==3
    by_kind={e['kind']:e for e in hist['events']}
    assert set(by_kind)=={'medicine_prescribed','vaccine_due','laboratory'}
    assert by_kind['medicine_prescribed']['certainty']=='unverified_draft' and by_kind['medicine_prescribed']['record_id']==med['id']
    assert by_kind['medicine_prescribed']['source_document_id']==d and by_kind['medicine_prescribed']['date']=='2026-09-01'
    assert by_kind['vaccine_due']['status']=='due_pending_verification' and by_kind['vaccine_due']['certainty']=='unverified_transcription'
    assert len(by_kind['laboratory']['results'])==2 and by_kind['laboratory']['certainty']=='unverified_transcription' and by_kind['laboratory']['date']=='2026-09-30'
    assert [u['id'] for u in hist['undated_documents']]==[d]
    assert any('does not establish the same infection' in w for w in hist['warnings'])

    meds=api('GET',f'/api/patients/{p}/medication-history',t).json()
    assert meds['summary']=={'total':1,'verified':0,'drafts':1,'with_administration_events':0}
    course=meds['courses'][0]
    assert course['id']==med['id'] and course['antibiotic_list_match'] is True and course['events']==[]
    assert 'prescription not yet verified by caregiver against the original' in course['missing']
    assert 'no administration events recorded' in course['missing'] and 'prescription date not documented' not in course['missing']
    assert course['display_period']['prescribed']=='2026-09-01' and course['display_period']['actual_start'] is None
    assert meds['groups']==[{'name':'cefixime zifi syrup','course_ids':[med['id']]}]
    assert len(api('GET',f'/api/patients/{p}/medication-history?q=cefixime',t).json()['courses'])==1
    assert api('GET',f'/api/patients/{p}/medication-history?q=paracetamol',t).json()['courses']==[]

    chart=api('GET',f'/api/patients/{p}/vaccination-chart',t).json()
    assert len(chart['rows'])==1 and chart['by_status']=={'due_pending_verification':1} and chart['next_confirmed'] is None
    row=chart['rows'][0]
    assert row['id']==vac['id'] and row['reminders_scheduled']==[] and row['anchor_date']==future and row['source_document_id']==d
    assert chart['pending_due_dates'][0]['due_date']==future

    labs=api('GET',f'/api/patients/{p}/lab-series',t).json()
    assert len(labs['series'])==1
    series=labs['series'][0]
    assert series['test_name']=='Haemoglobin' and series['unit']=='g/dL' and len(series['points'])==2
    assert series['original_units']==['g/dl','gm%'] and series['original_names']==['Haemoglobin','Hb']
    assert series['unit_conversions_applied'] is False and series['plot_points']==0  # unverified values are never plotted
    assert sorted(pt['numeric_value'] for pt in series['points'])==[10.2,11.9]
    assert all(len(pt['same_day_separate_reports'])==1 and pt['plot_eligible'] is False for pt in series['points'])

    links=api('GET',f'/api/documents/{d}/links',t).json()
    assert links['document']['id']==d and links['total_links']==4 and links['review'] is None
    assert [m['id'] for m in links['medicines']]==[med['id']] and [v['id'] for v in links['vaccines']]==[vac['id']] and len(links['observations'])==2

    thumb=api('GET',f'/api/documents/{d}/thumbnail?w=120',t)
    assert thumb.status_code==200 and thumb.headers['content-type']=='image/jpeg' and len(thumb.content)>0

    # Reading history never verifies, confirms or schedules anything.
    assert api('GET',f'/api/patients/{p}/reminders?days=30',t).json()['items']==[]
    assert all(v['confirmed'] is False for v in api('GET',f'/api/patients/{p}/vaccines',t).json())
    assert all(m['status']=='draft' for m in api('GET',f'/api/patients/{p}/medicines',t).json())

def test_history_views_are_isolated_per_family():
    a=make_user('history_iso_a');b=make_user('history_iso_b');p=make_child(a);d=make_doc(a,p)
    seed_child(a,p,d)
    for path in HISTORY_PATHS:
        assert api('GET',f'/api/patients/{p}/{path}',b).status_code==404,path
        assert api('GET',f'/api/patients/{p}/{path}',a).status_code==200,path
    assert api('GET',f'/api/documents/{d}/links',b).status_code==404
    assert api('GET',f'/api/documents/{d}/thumbnail',b).status_code==404
