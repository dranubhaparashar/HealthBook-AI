"""Share with Doctor: selective scopes, package contents, PDF summary, expiring/revocable doctor links. Synthetic data only."""
import io
import csv
import json
import zipfile
from datetime import date, datetime, timedelta, timezone

import pytest

from tests.test_api import api, make_user, make_child, client


def png(seed):
    """Distinct synthetic image per document (the API rejects duplicate file hashes)."""
    from PIL import Image
    im = Image.new('RGB', (400 + seed, 120), (20 * seed % 255, 90, 120))
    b = io.BytesIO(); im.save(b, format='PNG'); return b.getvalue()


def make_doc(t, p, category='prescription', seed=1, record_date=None):
    data = {'category': category, 'title': f'Synthetic {category} sheet {seed}'}
    if record_date:
        data['record_date'] = record_date
    r = api('POST', f'/api/patients/{p}/documents', t, data=data, files={'file': (f'test{seed}.png', png(seed), 'image/png')})
    assert r.status_code == 201, r.text
    return r.json()['id']


def seed(t, p):
    """Two illnesses: an E. coli stool-culture episode with a prescription, and an unrelated viral illness."""
    ecoli_lab = make_doc(t, p, category='culture', seed=1, record_date='2026-08-20')
    ecoli_rx = make_doc(t, p, category='prescription', seed=2)
    unrelated_rx = make_doc(t, p, category='prescription', seed=3)
    card = make_doc(t, p, category='vaccination_card', seed=4)
    culture = api('POST', f'/api/patients/{p}/observations', t, json={'test_name': 'Stool culture', 'value': 'growth', 'unit': '', 'specimen': 'stool', 'record_date': '2026-08-20', 'organism': 'Escherichia coli',
                                                                      'culture_findings': 'Significant growth of E. coli', 'susceptibility': 'cefixime: S; amikacin: S', 'source_document_id': ecoli_lab, 'verified': True}).json()
    rx = api('POST', f'/api/patients/{p}/medicines', t, json={'name': 'Cefixime syrup', 'generic_name': 'cefixime', 'instructions': 'Synthetic 5 day course', 'prescription_date': '2026-08-21', 'source_document_id': ecoli_rx, 'course_type': 'prescription'}).json()
    other = api('POST', f'/api/patients/{p}/medicines', t, json={'name': 'Paracetamol drops', 'instructions': 'Synthetic SOS', 'prescription_date': '2026-02-24', 'source_document_id': unrelated_rx}).json()
    ep = api('POST', f'/api/patients/{p}/episodes', t, json={'title': 'Acute gastroenteritis with E. coli on stool culture', 'started_on': '2026-08-19', 'ended_on': '2026-08-26', 'symptoms': 'loose stools', 'documented_diagnosis': 'AGE',
                                                              'links': [{'kind': 'observation', 'id': culture['id']}, {'kind': 'medicine', 'id': rx['id']}, {'kind': 'document', 'id': ecoli_lab}, {'kind': 'document', 'id': ecoli_rx}], 'source_document_id': ecoli_lab})
    assert ep.status_code == 201, ep.text
    viral = api('POST', f'/api/patients/{p}/episodes', t, json={'title': 'Viral fever Feb 2026', 'started_on': '2026-02-24', 'ended_on': '2026-02-27', 'symptoms': 'cough', 'documented_diagnosis': 'viral illness',
                                                                 'links': [{'kind': 'medicine', 'id': other['id']}, {'kind': 'document', 'id': unrelated_rx}], 'source_document_id': unrelated_rx})
    assert viral.status_code == 201, viral.text
    vax = api('POST', f'/api/patients/{p}/vaccines', t, json={'name': 'Example vaccine', 'dose_label': '1', 'given_date': '2026-01-05', 'source_document_id': card, 'source_type': 'vaccination_card'}).json()
    return {'ecoli_lab': ecoli_lab, 'ecoli_rx': ecoli_rx, 'unrelated_rx': unrelated_rx, 'card': card, 'culture': culture, 'rx': rx, 'other': other, 'episode': ep.json(), 'viral': viral.json(), 'vax': vax}


def test_ecoli_preset_excludes_unrelated_records_and_lists_documents_first():
    t = make_user('share_ecoli'); p = make_child(t); s = seed(t, p)
    preview = api('POST', f'/api/patients/{p}/share/preview', t, json={'mode': 'preset', 'preset': 'ecoli'})
    assert preview.status_code == 200, preview.text
    pv = preview.json()
    ids = {d['id'] for d in pv['documents']}
    assert s['ecoli_lab'] in ids and s['ecoli_rx'] in ids
    assert s['unrelated_rx'] not in ids and s['card'] not in ids, 'unrelated prescription and vaccination card must not leak into a condition export'
    assert pv['counts']['observations'] == 1 and pv['counts']['medicines'] == 1 and pv['counts']['episodes'] == 1 and pv['counts']['vaccines'] == 0
    assert pv['reason'].startswith('E. coli')
    assert all(d['file'].split('/')[0] in ('Labs', 'Prescriptions') for d in pv['documents'])
    import re
    assert not any(re.search(r'(document|doc)[-_]?\d+|#\d+|\.enc', d['file']) for d in pv['documents']), 'package file names must not embed database ids or storage names'


def test_zip_package_contains_only_selected_originals_plus_summary_index_manifest():
    t = make_user('share_zip'); p = make_child(t); s = seed(t, p)
    r = api('POST', f'/api/patients/{p}/share/package.zip', t, json={'mode': 'preset', 'preset': 'ecoli', 'reason': 'E. coli history for the gastroenterologist'})
    assert r.status_code == 200 and r.headers['content-type'] == 'application/zip'
    assert r.headers['content-disposition'].startswith('attachment; filename="HealthBook_Share_')
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        names = z.namelist()
        root = names[0].split('/')[0]
        assert root.startswith('HealthBook_Share_')
        assert f'{root}/Doctor_Summary.pdf' in names and f'{root}/INDEX.html' in names and f'{root}/manifest.csv' in names
        originals = [n for n in names if n.split('/')[1] in ('Labs', 'Prescriptions', 'Hospital', 'Vaccinations', 'Imaging', 'Other')]
        assert len(originals) == 2, originals
        assert {z.read(n) for n in originals} == {png(1), png(2)}, 'originals must be the decrypted source files of the selected documents only'
        rows = list(csv.DictReader(io.StringIO(z.read(f'{root}/manifest.csv').decode('utf-8-sig'))))
        assert {row['folder'] for row in rows} == {'Labs', 'Prescriptions'} and all(len(row['sha256']) == 64 for row in rows)
        pdf = z.read(f'{root}/Doctor_Summary.pdf')
        assert pdf.startswith(b'%PDF')
        import fitz
        text = ''.join(page.get_text() for page in fitz.open(stream=pdf, filetype='pdf'))
        assert 'Medical Summary for Clinician' in text and 'E. coli history for the gastroenterologist' in text
        assert 'Stool culture' in text and 'Cefixime syrup' in text and 'PRESCRIBED' in text
        assert 'Paracetamol' not in text and 'Example vaccine' not in text, 'unrelated records must not appear in the summary'
        assert 'Labs/2026-08-20_' in text, 'statements must reference package files'
        assert 'babycare_session' not in text and '.enc' not in text
        index = z.read(f'{root}/INDEX.html').decode()
        assert 'noindex' in index and 'Doctor_Summary.pdf' in index


def test_manual_and_category_and_date_scopes():
    t = make_user('share_scopes'); p = make_child(t); s = seed(t, p)
    manual = api('POST', f'/api/patients/{p}/share/preview', t, json={'mode': 'manual', 'document_ids': [s['card']]}).json()
    assert [d['id'] for d in manual['documents']] == [s['card']] and manual['counts']['vaccines'] == 1 and manual['counts']['medicines'] == 0
    cats = api('POST', f'/api/patients/{p}/share/preview', t, json={'mode': 'categories', 'categories': ['vaccinations']}).json()
    assert {d['id'] for d in cats['documents']} == {s['card']} and cats['counts']['vaccines'] == 1
    dated = api('POST', f'/api/patients/{p}/share/preview', t, json={'mode': 'dates', 'date_from': '2026-02-01', 'date_to': '2026-03-01'}).json()
    assert dated['counts']['medicines'] == 1 and dated['counts']['episodes'] == 1 and s['ecoli_rx'] not in {d['id'] for d in dated['documents']}
    excluded = api('POST', f'/api/patients/{p}/share/preview', t, json={'mode': 'preset', 'preset': 'complete', 'exclude_document_ids': [s['card']]}).json()
    assert s['card'] not in {d['id'] for d in excluded['documents']} and len(excluded['documents']) == 3
    assert api('POST', f'/api/patients/{p}/share/preview', t, json={'mode': 'categories', 'categories': ['nonsense']}).status_code == 422
    assert api('POST', f'/api/patients/{p}/share/preview', t, json={'mode': 'preset', 'preset': 'latest_hospitalization'}).status_code == 422  # nothing recorded yet
    pdf = api('POST', f'/api/patients/{p}/share/summary.pdf', t, json={'mode': 'preset', 'preset': 'vaccination'})
    assert pdf.status_code == 200 and pdf.content.startswith(b'%PDF')


def test_doctor_link_lifecycle_pin_expiry_revoke_and_isolation(monkeypatch):
    t = make_user('share_link'); p = make_child(t); s = seed(t, p)
    outsider = make_user('share_outsider')
    created = api('POST', f'/api/patients/{p}/share/links', t, json={'spec': {'mode': 'preset', 'preset': 'ecoli'}, 'label': 'GI clinic', 'expiry': '24h', 'pin': '4321', 'view_only': False, 'allow_download': True})
    assert created.status_code == 201, created.text
    link = created.json()
    token = link['token']
    assert len(token) >= 40 and link['path'] == f'/doctor.html#t={token}' and link['status'] == 'active' and link['pin_protected'] is True and link['document_count'] == 2
    listing = api('GET', f'/api/patients/{p}/share/links', t).json()['items']
    assert len(listing) == 1 and 'token' not in listing[0] and listing[0]['label'] == 'GI clinic'
    # public access: PIN required, wrong PIN counted, right PIN opens only the scoped package
    assert client.get(f'/api/share/public/{token}').status_code == 401
    assert client.get(f'/api/share/public/{token}', headers={'X-Share-Pin': '0000'}).status_code == 401
    opened = client.get(f'/api/share/public/{token}', headers={'X-Share-Pin': '4321'})
    assert opened.status_code == 200 and opened.headers['x-robots-tag'].startswith('noindex')
    body = opened.json()
    assert body['child']['name'] == 'Fictional Child' and len(body['documents']) == 2 and body['counts']['medicines'] == 1
    assert 'id' not in body['documents'][0] and 'storage_name' not in opened.text and 'token' not in body
    doc = client.get(f'/api/share/public/{token}/document/1', headers={'X-Share-Pin': '4321'})
    assert doc.status_code == 200 and doc.content in (png(1), png(2))
    assert client.get(f'/api/share/public/{token}/document/99', headers={'X-Share-Pin': '4321'}).status_code == 404
    summary = client.get(f'/api/share/public/{token}/summary.pdf', headers={'X-Share-Pin': '4321'})
    assert summary.status_code == 200 and summary.content.startswith(b'%PDF')
    package = client.get(f'/api/share/public/{token}/package.zip', headers={'X-Share-Pin': '4321'})
    assert package.status_code == 200 and package.headers['content-type'] == 'application/zip'
    # the doctor cannot reach the rest of the account
    assert client.get(f'/api/patients/{p}/documents').status_code == 401
    assert client.get(f'/api/documents/{s["card"]}/download').status_code == 401
    # access accounting and log
    info = api('GET', f'/api/share/links/{link["id"]}/log', t).json()
    assert info['package']['access_count'] >= 4 and info['package']['last_accessed_at']
    events = [e['event'] for e in info['events']]
    assert 'created' in events and 'pin_failed' in events and 'opened' in events and 'document' in events
    assert not any('Fictional' in (e['detail'] or '') or 'coli' in (e['detail'] or '').lower() for e in info['events']), 'access log never contains clinical content'
    # cross-family: outsider can neither list nor revoke
    assert api('GET', f'/api/patients/{p}/share/links', outsider).status_code == 404
    assert api('DELETE', f'/api/share/links/{link["id"]}', outsider).status_code == 404
    # revoke
    revoked = api('DELETE', f'/api/share/links/{link["id"]}', t)
    assert revoked.status_code == 200 and revoked.json()['status'] == 'revoked'
    assert client.get(f'/api/share/public/{token}', headers={'X-Share-Pin': '4321'}).status_code == 410
    # expiry
    from api.database import SessionLocal
    from api.share_models import SharePackage
    second = api('POST', f'/api/patients/{p}/share/links', t, json={'spec': {'mode': 'manual', 'document_ids': [s['card']]}, 'expiry': '1h', 'view_only': True}).json()
    with SessionLocal() as db:
        row = db.get(SharePackage, second['id'])
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
    assert client.get(f'/api/share/public/{second["token"]}').status_code == 410
    assert api('GET', f'/api/patients/{p}/share/links', t).json()['items'][0]['status'] == 'expired'
    # view-only share: downloads disabled, documents served inline
    third = api('POST', f'/api/patients/{p}/share/links', t, json={'spec': {'mode': 'manual', 'document_ids': [s['card']]}, 'expiry': '3d', 'view_only': True}).json()
    assert third['allow_download'] is False
    assert client.get(f'/api/share/public/{third["token"]}/package.zip').status_code == 403
    inline = client.get(f'/api/share/public/{third["token"]}/document/1')
    assert inline.status_code == 200 and inline.headers['content-disposition'].startswith('inline')
    # garbage / unknown tokens
    assert client.get('/api/share/public/not-a-real-token-at-all-xxxxxxxx').status_code == 404
    assert client.get('/api/share/public/short').status_code == 404


def test_unverified_rows_are_labelled_and_culture_entries_are_not_counted_as_given():
    t = make_user('share_labels'); p = make_child(t); s = seed(t, p)
    pending = api('POST', f'/api/patients/{p}/share/summary.pdf', t, json={'mode': 'preset', 'preset': 'ecoli'})
    import fitz
    text = ''.join(page.get_text() for page in fitz.open(stream=pending.content, filetype='pdf'))
    assert 'PRESCRIBED / OCR UNVERIFIED (draft)' in text, 'draft prescription must be labelled as unverified'
    assert 'DOCUMENTED ADMINISTERED MEDICATION (0)' in text, 'no administration record exists, so nothing may be counted as given'
    assert 'cefixime: S' in text and 'CONFIRMED ADMINISTERED' not in text.split('DOCUMENTED ADMINISTERED MEDICATION')[1].split('PENDING OR UNVERIFIED')[0], 'a susceptibility entry must never appear as an administered medicine'
    verified_only = api('POST', f'/api/patients/{p}/share/preview', t, json={'mode': 'preset', 'preset': 'ecoli', 'include_pending': False}).json()
    assert verified_only['counts']['medicines'] == 0 and verified_only['counts']['observations'] == 1  # the culture was verified, the prescription is a draft


def test_mobile_get_downloads_accept_base64_spec_and_reject_garbage():
    import base64
    t = make_user('share_mobile'); p = make_child(t); seed(t, p)
    spec = base64.urlsafe_b64encode(json.dumps({'mode': 'preset', 'preset': 'vaccination'}).encode()).decode().rstrip('=')
    z = api('GET', f'/api/patients/{p}/share/package.zip?spec={spec}', t)
    assert z.status_code == 200 and z.headers['content-type'] == 'application/zip' and z.headers['X-Document-Count'] == '1'
    pdf = api('GET', f'/api/patients/{p}/share/summary.pdf?spec={spec}', t)
    assert pdf.status_code == 200 and pdf.content.startswith(b'%PDF')
    assert api('GET', f'/api/patients/{p}/share/package.zip?spec=not-base64!!', t).status_code == 422
    other = make_user('share_mobile_other')
    assert api('GET', f'/api/patients/{p}/share/package.zip?spec={spec}', other).status_code == 404
