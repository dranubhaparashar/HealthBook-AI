"""Intelligent stool-photo intake: capture-time detection (EXIF, WhatsApp file names, metadata, weak fallbacks),
batch upload with chronological review cards, hash-based duplicate detection, AI suggestions that stay suggestions
until the parent confirms, audit trail of suggestion vs confirmed values, historical calendar placement, atomic
stool + diaper creation, timezone handling, no diagnosis language, cross-family isolation."""
import io
import json
import uuid
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pytest
from PIL import Image
from tests.test_api import api, make_user, make_child
from api import care_vision
from api import stool_intake as SI

TZ=ZoneInfo('Asia/Kolkata')

@pytest.fixture(autouse=True)
def clear_rate_window():
    from api.database import SessionLocal
    from api.models import RateWindow
    with SessionLocal() as db:db.query(RateWindow).delete();db.commit()

@pytest.fixture(autouse=True)
def late_evening_clock(monkeypatch):
    """Freeze 'now' at 23:50 local today so fixture times earlier today are never 'in the future' in any CI timezone."""
    from api import care, care_api
    frozen=datetime.combine(datetime.now(TZ).date(),datetime.min.time(),TZ).replace(hour=23,minute=50)
    fixed=lambda:frozen.astimezone(ZoneInfo('UTC'))
    monkeypatch.setattr(care,'utcnow',fixed);monkeypatch.setattr(care_api,'utcnow',fixed)

def family():
    t=make_user('intake'+uuid.uuid4().hex[:10]);p=make_child(t);return t,p

def today():return datetime.now(TZ).date()

def jpeg(colour,exif=None,size=(240,240),noise=0):
    if noise:colour=(min(255,colour[0]+noise*6),min(255,colour[1]+noise*2),colour[2])  # distinct bytes per photo so hashes differ
    im=Image.new('RGB',size,colour)
    b=io.BytesIO()
    if exif is not None:im.save(b,format='JPEG',exif=exif)
    else:im.save(b,format='JPEG')
    return b.getvalue()

def exif_bytes(original=None,offset=None,digitized=None,make='FictionalCam'):
    ex=Image.Exif();ex[271]=make
    ifd=ex.get_ifd(0x8769)
    if original:ifd[36867]=original
    if offset:ifd[36881]=offset
    if digitized:ifd[36868]=digitized
    return ex.tobytes()

def intake(t,p,files,**fields):
    r=api('POST',f'/api/patients/{p}/care/stool/intake',t,files=[('files',f) for f in files],data={k:list(v) for k,v in fields.items()} or None)
    assert r.status_code==201,r.text;return r.json()

def confirm(t,iid,body=None,expect=201):
    r=api('POST',f'/api/care/stool-intake/{iid}/confirm',t,json=body or {});assert r.status_code==expect,r.text;return r.json()

def day_view(t,p,d):return api('GET',f'/api/patients/{p}/care/day?day={d}',t).json()


def test_exif_timestamp_places_a_historical_photo_on_its_real_day():
    t,p=family();d=today()-timedelta(days=2)
    raw=jpeg((120,80,40),exif=exif_bytes(original=f'{d:%Y:%m:%d} 14:05:09'))
    r=intake(t,p,[('IMG_random_name.jpg',raw,'image/jpeg')],file_modified=[f'{today()}T09:00:00'])
    assert r['count']==1 and r['pending']==1
    card=r['items'][0];det=card['detected']
    assert det['source']=='exif' and det['confidence']=='high' and det['needs_confirmation'] is False
    assert det['at'].startswith(f'{d}T14:05:09') and det['date']==d.isoformat() and det['camera']=='FictionalCam'
    assert '2:05 PM' in det['label'] and 'EXIF' in det['source_label']
    assert r['by_date']==[{'date':d.isoformat(),'count':1}]
    # the upload day was NOT used even though the browser reported a file-modified time of today
    assert all(c['source']!='filesystem' or not det['at'].startswith(str(today())) for c in det['candidates'])
    entry=confirm(t,card['id'])
    assert entry['at'].startswith(f'{d}T14:05') and entry['local_date']==d.isoformat() and entry['time_source']=='exif'
    assert entry['photos'][0]['taken_at_source']=='exif' and entry['photos'][0]['original_filename']=='IMG_random_name.jpg'
    ph=entry['photos'][0]
    assert ph['stool_entry_id']==entry['id'] and ph['thumbnail_url']==f"/api/care/stool-photos/{ph['id']}" and ph['captured_at'].startswith(f'{d}T14:05') and ph['captured_at_source']=='exif'
    assert ph['uploaded_at'] and ph['uploaded_at'][:10]!=ph['captured_at'][:10] and ph['review_status']==entry['ai_review_status'] and ph['ai_observation']['apparent_colour'] in ('brown','yellow')
    dv=day_view(t,p,d);assert dv['stool']['entries']==1 and dv['stool']['items'][0]['id']==entry['id'] and dv['stool']['with_photo']==1
    assert day_view(t,p,today())['stool']['entries']==0
    month=api('GET',f'/api/patients/{p}/care/calendar/month?year={d.year}&month={d.month}',t).json()
    assert month['days'][d.isoformat()]['stool']==1
    tl=api('GET',f'/api/patients/{p}/care/timeline?day={d}',t).json()
    assert any(i['kind']=='stool' and i['time']=='14:05' for i in tl['items'])
    img=api('GET',card['image_url'],t);assert img.status_code==200 and img.content==raw


def test_whatsapp_filename_without_exif_gives_the_filename_time_not_the_download_time():
    t,p=family();d=today()-timedelta(days=1)
    r=intake(t,p,[(f'WhatsApp Image {d} at 8.35.59 AM.jpg',jpeg((150,110,60)),'image/jpeg')],file_modified=[f'{today()}T18:22:00'])
    det=r['items'][0]['detected']
    assert det['source']=='filename' and det['confidence']=='medium' and det['at'].startswith(f'{d}T08:35:59') and det['needs_confirmation'] is False
    assert det['label']==f'{d:%d %b %Y} · 8:35 AM'.replace(' 0',' ') and det['source_label']=='file name'
    r2=intake(t,p,[(f'WhatsApp Image {d} at 8.52.01 PM.jpeg',jpeg((150,110,60),noise=3),'image/jpeg')])
    assert r2['items'][0]['detected']['at'].startswith(f'{d}T20:52:01')
    e=confirm(t,r['items'][0]['id']);assert e['time_source']=='filename' and e['local_date']==d.isoformat()


def test_no_usable_timestamp_requires_the_parent_to_enter_the_time():
    t,p=family();d=today()-timedelta(days=3)
    r=intake(t,p,[('random.jpg',jpeg((130,90,50)),'image/jpeg')])
    det=r['items'][0]['detected']
    assert det['source']=='none' and det['at'] is None and det['needs_confirmation'] and det['label']=='Time not detected'
    assert r['by_date']==[{'date':'unknown','count':1}]
    confirm(t,r['items'][0]['id'],{},expect=422)
    e=confirm(t,r['items'][0]['id'],{'at':f'{d}T07:15'})
    assert e['time_source']=='parent_corrected' and e['at'].startswith(f'{d}T07:15') and e['photos'][0]['taken_at_source']=='manual'
    # file-system time only: clearly weak, must be confirmed
    r=intake(t,p,[('photo.jpg',jpeg((130,90,50),noise=5),'image/jpeg')],file_modified=[f'{d}T10:00:00'])
    det=r['items'][0]['detected'];assert det['source']=='filesystem' and det['confidence']=='low' and det['needs_confirmation'] and 'weak' in det['source_label']
    confirm(t,r['items'][0]['id'],{},expect=422)
    e=confirm(t,r['items'][0]['id'],{'at':det['at']});assert e['local_date']==d.isoformat()
    # date-only WhatsApp Android name: date known, time unknown
    r=intake(t,p,[(f'IMG-{d:%Y%m%d}-WA0012.jpg',jpeg((130,90,50),noise=9),'image/jpeg')])
    det=r['items'][0]['detected'];assert det['source']=='filename_date' and det['time_unknown'] and det['needs_confirmation'] and det['date']==d.isoformat()


def test_future_filename_date_is_rejected_and_never_used():
    t,p=family()
    r=intake(t,p,[('WhatsApp Image 2031-01-01 at 8.35.59 AM.jpg',jpeg((130,90,50)),'image/jpeg')])
    det=r['items'][0]['detected']
    assert det['source']=='none' and det['rejected'][0]['reason']=='in the future'


def test_batch_upload_creates_one_sorted_card_per_photo_with_per_day_summary():
    t,p=family();d1=today()-timedelta(days=1);d2=today()
    files=[(f'WhatsApp Image {d2} at 8.35.59 AM.jpg',jpeg((120,80,40),noise=1),'image/jpeg'),
           (f'WhatsApp Image {d1} at 8.52.01 PM.jpeg',jpeg((120,80,40),noise=2),'image/jpeg'),
           ('IMG_%s_071010.jpg'%d1.strftime('%Y%m%d'),jpeg((120,80,40),noise=3),'image/jpeg'),
           ('IMG_%s_063000.jpg'%d2.strftime('%Y%m%d'),jpeg((120,80,40),noise=4),'image/jpeg')]
    r=intake(t,p,files)
    assert r['count']==4 and r['pending']==4 and r['duplicates']==0
    ats=[c['detected']['at'] for c in r['items']];assert ats==sorted(ats)
    assert [c['original_filename'] for c in r['items']][0].startswith('IMG_') and r['items'][0]['detected']['at'].startswith(f'{d1}T07:10')
    assert r['by_date']==[{'date':d1.isoformat(),'count':2},{'date':d2.isoformat(),'count':2}]
    pending=api('GET',f'/api/patients/{p}/care/stool/intake',t).json();assert pending['pending']==4
    for c in r['items']:confirm(t,c['id'],{'create_diaper':False})
    assert day_view(t,p,d1)['stool']['entries']==2 and day_view(t,p,d2)['stool']['entries']==2
    assert api('GET',f'/api/patients/{p}/care/stool/intake',t).json()['pending']==0
    # four photos were never merged into one event
    assert len(api('GET',f'/api/patients/{p}/care/stool?start={d1}&end={d2}',t).json())==4


def test_duplicate_photo_is_detected_by_hash_and_never_saved_twice():
    t,p=family();d=today()-timedelta(days=1)
    raw=jpeg((120,80,40));name=f'WhatsApp Image {d} at 8.35.59 AM.jpg'
    r=intake(t,p,[(name,raw,'image/jpeg'),(name,raw,'image/jpeg')])
    assert r['count']==1,'the same file picked twice in one batch is one card'
    entry=confirm(t,r['items'][0]['id'])
    again=intake(t,p,[('copy of photo.jpg',raw,'image/jpeg')])
    card=again['items'][0]
    assert card['status']=='duplicate' and card['duplicate_of']['entry_id']==entry['id'] and again['pending']==0 and again['duplicates']==1
    assert '8:35 AM' in card['duplicate_of']['label']
    r=api('POST',f'/api/care/stool-intake/{card["id"]}/confirm',t,json={});assert r.status_code==409 and 'already recorded' in r.text
    assert len(api('GET',f'/api/patients/{p}/care/stool?start={d}&end={today()}',t).json())==1
    assert api('DELETE',f'/api/care/stool-intake/{card["id"]}',t).json()['status']=='discarded'


def test_ai_values_are_suggestions_until_the_parent_confirms_and_corrections_are_audited():
    t,p=family();d=today()-timedelta(days=1)
    r=intake(t,p,[(f'IMG_{d:%Y%m%d}_101500.jpg',jpeg((120,80,40)),'image/jpeg')])
    card=r['items'][0];sug=card['suggestions']
    assert sug['colour']['value']=='brown' and sug['colour']['confidence'] in ('high','medium','low')
    assert isinstance(sug['colour']['confidence_pct'],int) and 0<sug['colour']['confidence_pct']<=100 and all('confidence_pct' in sug[k] for k in ('consistency','mucus_like_material','blood_like_colour','watery_appearance','amount'))
    assert {'value','confidence','confidence_pct'}<=set(sug['consistency']) and sug['disclaimer']==care_vision.DISCLAIMER
    assert card['prefill']['colour']=='brown'
    # nothing was saved yet
    assert day_view(t,p,d)['stool']['entries']==0
    # parent corrects colour to green and marks mucus; the suggestion and the parent's values are both kept
    e=confirm(t,card['id'],{**card['prefill'],'colour':'green','mucus_observed':True,'parent_concern':'mild'})
    assert e['colour']=='green' and e['mucus_observed'] is True and e['ai_review_status']=='parent_corrected'
    assert e['ai_suggestion']['colour']['value']=='brown' and e['parent_values']['colour']=='green' and e['parent_values']['mucus_observed'] is True
    assert e['parent_values']['time_source']=='filename' and e['ai_observation']['apparent_colour']=='brown'
    # a card confirmed exactly as suggested is 'parent_confirmed'
    r2=intake(t,p,[(f'IMG_{d:%Y%m%d}_111500.jpg',jpeg((120,80,40),noise=2),'image/jpeg')])
    e2=confirm(t,r2['items'][0]['id'],r2['items'][0]['prefill'])
    assert e2['ai_review_status']=='parent_confirmed'
    hist=api('GET',f'/api/care/stool/{e["id"]}/history',t).json();assert hist['current']['ai_review_status']=='parent_corrected'


def test_timezone_offsets_in_exif_and_utc_filenames_are_converted_to_the_childs_local_time():
    t,p=family();d=today()-timedelta(days=1)
    raw=jpeg((120,80,40),exif=exif_bytes(original=f'{d:%Y:%m:%d} 09:05:00',offset='+00:00'))
    det=intake(t,p,[('cam.jpg',raw,'image/jpeg')])['items'][0]['detected']
    assert det['at'].startswith(f'{d}T14:35:00+05:30'),det   # 09:05 UTC is 14:35 in Asia/Kolkata
    det=intake(t,p,[(f'PXL_{d:%Y%m%d}_030559123.jpg',jpeg((120,80,40),noise=2),'image/jpeg')])['items'][0]['detected']
    assert det['at'].startswith(f'{d}T08:35:59+05:30'),det   # Pixel names are UTC
    # EXIF without an offset is the camera's local clock = the child's local time
    raw=jpeg((120,80,40),exif=exif_bytes(original=f'{d:%Y:%m:%d} 23:40:00'),noise=3)
    e=confirm(t,intake(t,p,[('late.jpg',raw,'image/jpeg')])['items'][0]['id'])
    assert e['local_date']==d.isoformat() and e['at'].startswith(f'{d}T23:40')


def test_stool_and_diaper_are_created_together_and_only_once():
    t,p=family();d=today()-timedelta(days=1)
    r=intake(t,p,[(f'IMG_{d:%Y%m%d}_064500.jpg',jpeg((120,80,40)),'image/jpeg')])
    e=confirm(t,r['items'][0]['id'],{'create_diaper':True,'client_key':'intake-'+uuid.uuid4().hex[:8]})
    dv=day_view(t,p,d)
    assert dv['stool']['entries']==1 and dv['diapers']['stool']==1
    diapers=api('GET',f'/api/patients/{p}/care/diaper?start={d}&end={d}',t).json()
    assert len(diapers)==1 and diapers[0]['stool_entry_id']==e['id'] and diapers[0]['at']==e['at']
    tl=api('GET',f'/api/patients/{p}/care/timeline?day={d}',t).json()
    # one combined item: the diaper logged with the photo is shown inside the stool entry, never as a second event
    stool_items=[i for i in tl['items'] if i['kind']=='stool']
    assert len(stool_items)==1 and sum(1 for i in tl['items'] if i['kind']=='diaper')==0
    it=stool_items[0];assert it['diaper_logged'] and it['linked_diaper_id']==diapers[0]['id'] and 'diaper changed' in it['title'] and it['photo_id']==e['photos'][0]['id'] and it['time_source']=='filename'
    # the day summary and the photo history both list the photograph with its provenance
    ph=day_view(t,p,d)['stool']['photos'];assert len(ph)==1 and ph[0]['entry_id']==e['id'] and ph[0]['taken_at_source']=='filename' and ph[0]['original_filename'].startswith('IMG_')
    hist=api('GET',f'/api/patients/{p}/care/stool/photos?start={d}&end={today()}',t).json()
    assert hist['total']==1 and hist['days'][0]['date']==d.isoformat() and hist['days'][0]['photos'][0]['url']==f"/api/care/stool-photos/{ph[0]['id']}"
    assert api('GET',f'/api/patients/{p}/care/stool/photos',make_user('hist'+uuid.uuid4().hex[:6])).status_code==404
    # re-confirming the same card returns the existing entry
    again=confirm(t,r['items'][0]['id'],{'create_diaper':True});assert again.get('existing') and again['id']==e['id']
    assert day_view(t,p,d)['diapers']['stool']==1
    # analytics counts the photo entry
    an=api('GET',f'/api/patients/{p}/care/analytics?start={d}&end={today()}',t).json()
    assert json.dumps(an) and 'stool' in json.dumps(an).casefold()


def test_red_or_pale_appearance_only_suggests_clinical_review_and_never_a_diagnosis():
    t,p=family();d=today()-timedelta(days=1)
    r=intake(t,p,[(f'IMG_{d:%Y%m%d}_064500.jpg',jpeg((200,30,30)),'image/jpeg')])
    card=r['items'][0];sug=card['suggestions']
    assert sug['colour']['value']=='red' and sug['blood_like_colour']['value']=='uncertain' and sug['consider_clinical_review'] and sug['review_message']=='Consider clinical review'
    assert care_vision.DISCLAIMER in json.dumps(r,ensure_ascii=False)
    text=json.dumps(r,ensure_ascii=False).casefold().replace(care_vision.DISCLAIMER.casefold(),'')
    for word in care_vision.FORBIDDEN_WORDS:assert word not in text,word
    e=confirm(t,card['id'],card['prefill'])
    assert e['clinical_review_suggested'] is True and e['review_message']=='Consider clinical review'
    assert 'coli' not in json.dumps(e).casefold()


def test_intake_is_private_to_the_family():
    t,p=family();other=make_user('intruder'+uuid.uuid4().hex[:8]);make_child(other)
    r=intake(t,p,[('x.jpg',jpeg((120,80,40)),'image/jpeg')]);iid=r['items'][0]['id']
    assert api('GET',f'/api/care/stool-intake/{iid}/image',other).status_code==404
    assert api('POST',f'/api/care/stool-intake/{iid}/confirm',other,json={}).status_code==404
    assert api('DELETE',f'/api/care/stool-intake/{iid}',other).status_code==404
    assert api('GET',f'/api/patients/{p}/care/stool/intake',other).status_code==404
    assert api('POST',f'/api/patients/{p}/care/stool/intake',other,files=[('files',('x.jpg',jpeg((120,80,40)),'image/jpeg'))]).status_code==404


def test_filename_parser_covers_common_phone_patterns():
    tz=ZoneInfo('Asia/Kolkata')
    cases={'WhatsApp Image 2026-10-06 at 8.35.59 AM.jpg':('filename','2026-10-06T08:35:59'),
           'WhatsApp Image 2026-10-05 at 8.52.01 PM.jpeg':('filename','2026-10-05T20:52:01'),
           'WhatsApp Image 2026-10-05 at 20.52.01.jpeg':('filename','2026-10-05T20:52:01'),
           'IMG_20261006_083559.jpg':('filename','2026-10-06T08:35:59'),
           'Screenshot_20261006-083559.png':('filename','2026-10-06T08:35:59'),
           'signal-2026-10-06-083559.jpg':('filename','2026-10-06T08:35:59'),
           'PXL_20261006_030559123.jpg':('filename','2026-10-06T08:35:59'),
           'IMG-20261006-WA0012.jpg':('filename_date','2026-10-06T12:00:00'),
           'random.jpg':('none',None),'IMG_1234.HEIC':('none',None)}
    for name,(source,when) in cases.items():
        dt,src=SI.parse_filename(name,tz)
        assert src==source,(name,src)
        assert (dt.strftime('%Y-%m-%dT%H:%M:%S') if dt else None)==when,(name,dt)
