"""Premium PDF export: structure, selectivity, appended originals (images and multi-page PDFs in order), medical-safety
labels (prescribed is not administered; susceptibility is not administration; due is not given), authorization,
determinism and no temp files. Synthetic families and synthetic documents only."""
import csv
import io
import tempfile
import uuid
import zipfile
from datetime import date, datetime
from zoneinfo import ZoneInfo
import fitz
import pytest
from tests.test_api import api, make_user, make_child, client
from tests.test_share import seed, make_doc, png
from api.database import SessionLocal
from api.models import Patient
from api import export_model, export_pdf

def family():
    t=make_user('exp'+uuid.uuid4().hex[:10]);p=make_child(t,'Fictional Export Child');return t,p

def pdf_text(data):
    """Page texts with line wraps collapsed so phrases can be asserted."""
    with fitz.open(stream=data,filetype='pdf') as d:return [' '.join(pg.get_text().split()) for pg in d]

def three_page_pdf():
    d=fitz.open()
    for i in range(1,4):
        pg=d.new_page(width=595,height=842);pg.insert_text((72,100),f'SYNTHETIC SOURCE PAGE {i} OF 3',fontsize=18);pg.insert_text((72,140),f'marker-{"alpha" if i==1 else "beta" if i==2 else "gamma"}',fontsize=12)
    return d.tobytes()

def download(t,p,**spec):
    r=api('POST',f'/api/patients/{p}/export/download',t,json=spec);assert r.status_code==200,r.text;return r

def test_complete_pdf_structure_bookmarks_footer_and_name():
    t,p=family();s=seed(t,p)
    r=download(t,p,preset='complete',mode='with_sources')
    assert r.content[:5]==b'%PDF-' and r.headers['content-type']=='application/pdf'
    assert 'Fictional_Export_Child_Medical_Record_' in r.headers['content-disposition'] and r.headers['cache-control'].startswith('no-store')
    pages=pdf_text(r.content)
    assert 'Fictional Export Child' in pages[0] and 'Complete Medical Record' in pages[0]
    assert 'Executive clinical summary' in pages[1] and 'Contents' in ''.join(pages[1:4])
    with fitz.open(stream=r.content,filetype='pdf') as d:
        toc=d.get_toc();titles=[x[1] for x in toc]
        assert 'Executive clinical summary' in titles and 'Appendix: original documents' in titles and any(x.startswith('Appendix A') for x in titles)
        assert f'Page 2 of {d.page_count}' in d[1].get_text() and f'Page {d.page_count} of {d.page_count}' in d[-1].get_text()
        # every source reference resolves to a GOTO link inside the document, none is left as a private URI
        kinds=[l['kind'] for pg in d for l in pg.get_links()]
        assert fitz.LINK_GOTO in kinds and not any(l.get('uri','').startswith('bcsrc:') for pg in d for l in pg.get_links())
    joined='\n'.join(pages)
    assert 'Synthetic culture sheet 1' in joined and 'Synthetic prescription sheet 3' in joined  # complete export appends everything
    assert 'Rx id' not in joined and '/documents/' not in joined

def test_ecoli_export_includes_related_and_excludes_unrelated_records():
    t,p=family();s=seed(t,p)
    pv=api('POST',f'/api/patients/{p}/export/preview',t,json={'preset':'ecoli','mode':'with_sources'}).json()
    titles=[x['title'] for x in pv['sources']]
    assert 'Synthetic culture sheet 1' in titles and 'Synthetic prescription sheet 2' in titles
    assert 'Synthetic prescription sheet 3' not in titles and 'Synthetic vaccination_card sheet 4' not in titles
    assert pv['counts']['prescriptions']==1 and pv['counts']['cultures']==1 and pv['counts']['episodes']==1 and pv['counts']['vaccinations']==0
    r=download(t,p,preset='ecoli',mode='with_sources');joined='\n'.join(pdf_text(r.content))
    assert 'Cefixime syrup' in joined and 'Escherichia coli' in joined and 'Paracetamol drops' not in joined and 'Viral fever Feb 2026' not in joined
    assert 'Ecoli_History' in r.headers['content-disposition']

def test_original_images_are_appended_after_divider_pages_in_index_order():
    t,p=family();s=seed(t,p)
    r=download(t,p,preset='complete',mode='with_sources')
    with fitz.open(stream=r.content,filetype='pdf') as d:
        pv=api('POST',f'/api/patients/{p}/export/preview',t,json={'preset':'complete'}).json()
        body=d.page_count-2*len(pv['sources'])  # each image document = divider + one page
        assert body>=4
        for i,src in enumerate(pv['sources']):
            divider=d[body+2*i];page=d[body+2*i+1]
            assert f"APPENDIX {src['appendix']}" in divider.get_text() and src['title'] in ' '.join(divider.get_text().split())
            assert page.get_images(), 'original image missing on appendix page'
            assert '‹ Back to summary' in page.get_text() and f"Appendix {src['appendix']}" in page.get_text()
            back=[l for l in page.get_links() if l['kind']==fitz.LINK_GOTO and l['page']<body]
            assert back, 'back link missing'
        # Summary-only mode appends nothing
    r2=download(t,p,preset='complete',mode='summary')
    with fitz.open(stream=r2.content,filetype='pdf') as d2:assert d2.page_count<=body and not any(pg.get_images() for pg in d2)

def test_multi_page_pdf_source_is_embedded_in_order_without_rasterizing():
    t,p=family()
    r=api('POST',f'/api/patients/{p}/documents',t,data={'category':'discharge','title':'Synthetic three page discharge','record_date':'2026-09-01'},files={'file':('discharge.pdf',three_page_pdf(),'application/pdf')})
    assert r.status_code==201,r.text
    out=download(t,p,preset='complete',mode='with_sources')
    with fitz.open(stream=out.content,filetype='pdf') as d:
        texts=[pg.get_text() for pg in d]
        idx=[i for i,x in enumerate(texts) if 'SYNTHETIC SOURCE PAGE' in x]
        assert len(idx)==3 and idx==list(range(idx[0],idx[0]+3)),'pages missing or out of order'
        assert 'marker-alpha' in texts[idx[0]] and 'marker-beta' in texts[idx[1]] and 'marker-gamma' in texts[idx[2]]
        assert not any(d[i].get_images() for i in idx),'PDF pages must be embedded as vector pages, not rasterized'
        assert 'page 2 of 3' in texts[idx[1]]

def test_prescribed_is_never_shown_as_administered_and_susceptibility_is_not_medication():
    t,p=family();s=seed(t,p)
    with SessionLocal() as db:
        patient=db.get(Patient,p);model=export_model.build_model(db,patient,'ecoli')
    meds=model['medicines'];assert meds and all(m['status'].startswith('PRESCRIBED') for m in meds)
    assert not any('GIVEN' in m['status'] for m in meds)
    names={m['name'].casefold() for m in meds}|{r['drug'].casefold() for r in model['antibiotics']['rows']}
    assert 'amikacin' not in names,'culture susceptibility antibiotic must not appear as a medicine or course'
    mx=model['susceptibility_matrix'];assert mx and {r['drug'] for r in mx['rows']}>={'cefixime','amikacin'}
    assert next(r for r in mx['rows'] if r['drug']=='amikacin')['prescribed_or_given'] is False
    joined='\n'.join(pdf_text(download(t,p,preset='ecoli',mode='summary').content))
    assert 'PRESCRIBED' in joined and 'CONFIRMED GIVEN' not in joined.replace('CONFIRMED GIVEN means','').replace('CONFIRMED GIVEN = administration','').replace('marked CONFIRMED GIVEN rest','')
    assert 'not a record of medicines given' in joined

def test_vaccination_due_date_is_not_a_given_date():
    t,p=family();card=make_doc(t,p,category='vaccination_card',seed=7)
    api('POST',f'/api/patients/{p}/vaccines',t,json={'name':'Synthetic vaccine','dose_label':'1','given_date':'2026-01-05','source_document_id':card,'source_type':'vaccination_card'})
    r=api('POST',f'/api/patients/{p}/vaccines',t,json={'name':'Synthetic vaccine','dose_label':'2','due_date':'2027-03-01','source_document_id':card,'source_type':'vaccination_card'});assert r.status_code==201,r.text
    with SessionLocal() as db:model=export_model.build_model(db,db.get(Patient,p),'vaccination')
    rows={v['dose']:v for v in model['vaccinations']}
    assert rows['1']['given']=='2026-01-05' and rows['2']['given'] is None and rows['2']['due']=='2027-03-01'
    assert model['summary']['vaccination']['given_verified']+model['summary']['vaccination']['given_pending']==1
    joined='\n'.join(pdf_text(download(t,p,preset='vaccination',mode='summary').content))
    assert 'not recorded' in joined and '01 Mar 2027' in joined

def test_other_family_cannot_export_and_bad_sections_are_rejected():
    t,p=family();seed(t,p);t2,_=family()
    assert api('POST',f'/api/patients/{p}/export/download',t2,json={'preset':'complete'}).status_code==404
    assert api('POST',f'/api/patients/{p}/export/preview',t2,json={'preset':'ecoli'}).status_code==404
    assert client.post(f'/api/patients/{p}/export/download',json={'preset':'complete'}).status_code==401
    assert api('POST',f'/api/patients/{p}/export/preview',t,json={'preset':'custom','sections':['summary','passwords']}).status_code==422
    assert api('POST',f'/api/patients/{p}/export/preview',t,json={'preset':'custom','date_from':'2026-09-01','date_to':'2026-01-01'}).status_code==422
    ok=api('POST',f'/api/patients/{p}/export/preview',t,json={'preset':'custom','sections':['summary','prescriptions','labs','stool_photos','sources'],'date_from':'2026-08-01','date_to':'2026-08-31'});assert ok.status_code==200,ok.text
    assert set(ok.json()['sections'])=={'summary','medicines','labs','sources'} and ok.json()['counts']['sources']==2

def test_zip_archive_has_pdf_originals_and_manifest_without_database_ids():
    t,p=family();s=seed(t,p)
    r=download(t,p,preset='complete',mode='archive');assert r.content[:2]==b'PK' and r.headers['content-type']=='application/zip'
    z=zipfile.ZipFile(io.BytesIO(r.content));names=z.namelist()
    assert 'Medical_Record.pdf' in names and 'manifest.csv' in names and any(n.startswith('Original_Reports/') for n in names)
    assert z.read('Medical_Record.pdf')[:5]==b'%PDF-'
    rows=list(csv.DictReader(io.StringIO(z.read('manifest.csv').decode())))
    assert len(rows)==4 and all(r['file'].startswith('Original_Reports/') and r['file'] in names for r in rows)
    assert not any(f"_{s['ecoli_lab']}." in n or f"/{s['ecoli_lab']}_" in n for n in names)

def test_generation_is_in_memory_and_deterministic(monkeypatch):
    t,p=family();seed(t,p)
    def boom(*a,**k):raise AssertionError('export must not write temporary files')
    monkeypatch.setattr(tempfile,'mkstemp',boom);monkeypatch.setattr(tempfile,'NamedTemporaryFile',boom);monkeypatch.setattr(tempfile,'TemporaryFile',boom);monkeypatch.setattr(tempfile,'mkdtemp',boom)
    gen=datetime(2026,10,6,9,0,tzinfo=ZoneInfo('Asia/Kolkata'))
    with SessionLocal() as db:
        patient=db.get(Patient,p)
        a,fa,ia=export_pdf.render(db,patient,'complete',None,'with_sources',gen);b,fb,ib=export_pdf.render(db,patient,'complete',None,'with_sources',gen)
    assert a==b and fa==fb=='Fictional_Export_Child_Medical_Record_2026-10-06.pdf' and ia['pages']==ib['pages']>4

def test_text_export_comes_from_the_same_model():
    t,p=family();seed(t,p)
    r=api('GET',f'/api/patients/{p}/export/debug.txt?preset=ecoli',t);assert r.status_code==200
    body=r.text;assert 'HEALTHBOOK AI' in body and 'Cefixime syrup' in body and 'PRESCRIBED' in body and 'Paracetamol' not in body and 'SOURCE DOCUMENTS' in body
