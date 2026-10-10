"""Document extraction review → "Confirm and save to history": only included rows are validated and saved, errors are
field-level with page numbers, excluded/removed/empty rows and raw OCR never block, transcription-only documents can be
archived, and a double submit never duplicates history. Synthetic data only."""
import io
import uuid
import fitz
from tests.test_api import api, make_user, make_child
import pytest
from api.database import SessionLocal
from api.models import RateWindow

@pytest.fixture(autouse=True)
def clear_rate_window():
    """Each test signs up fresh synthetic accounts; reset the shared signup rate window before and after so this file
    never exhausts it for the files that run later."""
    with SessionLocal() as db:db.query(RateWindow).delete();db.commit()
    yield
    with SessionLocal() as db:db.query(RateWindow).delete();db.commit()

def family():
    t=make_user('rev'+uuid.uuid4().hex[:10]);p=make_child(t);return t,p

def pdf(pages,seed):
    d=fitz.open()
    for i in range(pages):
        pg=d.new_page();pg.insert_text((72,100),f'SYNTHETIC REPORT {seed} PAGE {i+1}',fontsize=14)
    return d.tobytes()

def upload(t,p,pages=1,category='prescription'):
    seed=uuid.uuid4().hex[:8]
    r=api('POST',f'/api/patients/{p}/documents',t,data={'category':category,'title':f'Synthetic {category} {seed}'},files={'file':(f'{seed}.pdf',pdf(pages,seed),'application/pdf')})
    assert r.status_code==201,r.text;return r.json()['id']

def med(page=1,included=True,**fields):
    return {'kind':'medicine','page':page,'included':included,'verified':included,'fields':{'name':'Synthetic syrup','dosage':'2.5','dose_unit':'ml','frequency':'twice daily',**fields}}

def confirm(t,d,items,text='raw OCR text',ack=True):
    return api('POST',f'/api/documents/{d}/review/confirm',t,json={'items':items,'text':text,'version':1,'checked_against_original':ack})

def test_A_clean_document_confirms_and_appears_in_history_with_source_page():
    t,p=family();d=upload(t,p,pages=2)
    r=confirm(t,d,[med(page=2)]);assert r.status_code==200,r.text
    body=r.json();assert body['status']=='confirmed' and len(body['created'])==1 and body['transcription_only'] is False
    meds=api('GET',f'/api/patients/{p}/medicines',t).json()
    m=next(x for x in meds if x['name']=='Synthetic syrup');assert m['source_document_id']==d and m['source_page']==2 and m['status']=='draft'
    doc=next(x for x in api('GET',f'/api/patients/{p}/documents',t).json() if x['id']==d);assert doc['extraction_status']=='reviewed'

def test_B_unreadable_dose_is_reported_on_the_exact_field():
    t,p=family();d=upload(t,p)
    r=confirm(t,d,[med(),med(dosage='2.?')]);assert r.status_code==422
    det=r.json()['detail'];errs=det['errors']
    assert det['message']=='1 item needs review before this document can be confirmed.'
    assert errs==[{**errs[0],'index':1,'entry_type':'medicine','row_number':2,'field':'dosage','reason':'unreadable','page':1}]
    assert errs[0]['message'].startswith('Page 1 · Medicine row 2: Dose is marked unreadable')
    assert api('GET',f'/api/patients/{p}/medicines',t).json()==[]   # nothing partially saved

def test_C_ambiguous_date_is_reported_and_never_guessed():
    t,p=family();d=upload(t,p)
    r=confirm(t,d,[{'kind':'observation','page':1,'included':True,'verified':True,'fields':{'test_name':'Haemoglobin','value':'11.2','unit':'g/dL','record_date':'03/04/26'}}])
    e=r.json()['detail']['errors'];assert r.status_code==422 and e[0]['field']=='record_date' and e[0]['reason']=='invalid_date' and 'Observation row 1' in e[0]['message']

def test_D_E_excluded_incomplete_rows_and_removed_rows_do_not_block():
    t,p=family();d=upload(t,p)
    incomplete={'kind':'vaccine','page':1,'included':False,'verified':False,'fields':{'name':'mr','note':'434 | mr sam SY'},'raw_line':'434 | mr sam SY'}
    r=confirm(t,d,[med(),incomplete]);assert r.status_code==200,r.text   # the removed rows simply are not in the list
    assert r.json()['skipped']['excluded']==1 and api('GET',f'/api/patients/{p}/vaccines',t).json()==[]

def test_F_raw_ocr_garbage_and_empty_included_rows_do_not_block():
    t,p=family();d=upload(t,p,category='lab')
    garbage='Ref range 11.0-14.0 ??\nDr. ____ signature ...\nPage 1 of 3 | ||| ~~~'
    empty={'kind':'observation','page':1,'included':True,'verified':False,'fields':{}}
    r=confirm(t,d,[empty],text=garbage);assert r.status_code==200,r.text
    assert r.json()['transcription_only'] is True and r.json()['skipped']['empty']==1

def test_G_transcription_only_document_is_saved_to_the_library():
    t,p=family();d=upload(t,p,category='bill')
    r=confirm(t,d,[],text='Hospital bill, synthetic');assert r.status_code==200,r.text
    assert r.json()['created']==[] and r.json()['document_status']=='reviewed'
    rv=api('GET',f'/api/documents/{d}/review',t).json();assert rv['status']=='confirmed' and rv['text']=='Hospital bill, synthetic'

def test_H_problem_on_another_page_carries_that_page_and_multiple_errors_are_all_listed():
    t,p=family();d=upload(t,p,pages=3)
    r=confirm(t,d,[med(page=1),med(page=3,name='',dosage='unclear')],ack=False);assert r.status_code==422
    det=r.json()['detail'];errs=det['errors']
    assert det['message']=='3 items need review before this document can be confirmed.'
    assert {(e['page'],e['field'],e['reason']) for e in errs}=={(3,'name','required'),(3,'dosage','unreadable'),(None,'checked_against_original','acknowledgement_missing')}
    assert all(e['message'].startswith('Page 3 · Medicine row 2') for e in errs if e['page']==3)

def test_included_row_must_be_checked_and_undated_vaccine_is_never_assumed_given():
    t,p=family();d=upload(t,p,category='vaccination_card')
    unchecked={**med(),'verified':False}
    vax={'kind':'vaccine','page':1,'included':True,'verified':True,'fields':{'name':'Synthetic vaccine','dose_label':'1'}}
    errs=confirm(t,d,[unchecked,vax]).json()['detail']['errors']
    assert {(e['entry_type'],e['reason']) for e in errs}=={('medicine','unverified'),('vaccine','date_required')}

def test_double_confirm_never_duplicates_history():
    t,p=family();d=upload(t,p)
    assert confirm(t,d,[med()]).status_code==200
    assert confirm(t,d,[med()]).status_code==409
    assert len(api('GET',f'/api/patients/{p}/medicines',t).json())==1

def test_other_family_cannot_confirm():
    t,p=family();d=upload(t,p);t2,_=family()
    assert confirm(t2,d,[med()]).status_code==404
