"""Intelligent stool-photo intake: when was the photo really taken, and what does it visibly show.

Timestamp priority (first trustworthy hit wins, each candidate is kept for the audit trail):
  1. EXIF DateTimeOriginal        -> source 'exif',         confidence high
  2. EXIF DateTimeDigitized/DateTime -> source 'exif_created', confidence high
  3. date AND time in the file name (WhatsApp, IMG_, PXL_, Screenshot_, signal-, plain) -> 'filename', medium
  4. XMP / PNG creation metadata  -> source 'metadata',     confidence medium
  5. date only in the file name   -> source 'filename_date', confidence low (time unknown, needs confirmation)
  6. file-system modified time sent by the client -> 'filesystem', confidence low, clearly weak
  7. nothing                      -> source 'none': the parent must enter the time
Upload time is never used as the event time. Candidates in the future or before the person's birth are rejected.

Suggestions describe appearance only (colour family, consistency hints, mucus-like / blood-like / watery appearance)
with a confidence each; they are presented as "AI suggested" and never saved without parent confirmation. The
fixed disclaimer applies and no organism, infection or disease is ever named.
"""
import io
import re
import colorsys
from collections import Counter
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import care_vision

DISCLAIMER = care_vision.DISCLAIMER
SOURCE_LABEL = {'exif': 'EXIF (camera time)', 'exif_created': 'EXIF (creation date)', 'filename': 'file name', 'metadata': 'image metadata',
                'filename_date': 'file name (date only)', 'filesystem': 'file modified time (weak)', 'manual': 'entered by parent', 'none': 'not detected'}
CONFIDENCE = {'exif': 'high', 'exif_created': 'high', 'filename': 'medium', 'metadata': 'medium', 'filename_date': 'low', 'filesystem': 'low', 'none': 'none', 'manual': 'confirmed'}
NEEDS_CONFIRMATION = {'filename_date', 'filesystem', 'none'}

_EXIF_DT = re.compile(r'^(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})')
_OFFSET = re.compile(r'^([+-])(\d{2}):?(\d{2})$')

# ----------------------------------------------------------------------------------------------- file-name patterns
# Each pattern yields named groups y m d and optionally H M S, ampm. 'utc' marks file names written in UTC (Pixel).
_FILENAME_PATTERNS = [
    # WhatsApp Image 2026-10-06 at 8.35.59 AM.jpg  |  WhatsApp Image 2026-10-05 at 20.52.01.jpeg
    (re.compile(r'WhatsApp (?:Image|Video)[ _](?P<y>\d{4})-(?P<m>\d{2})-(?P<d>\d{2}) at (?P<H>\d{1,2})\.(?P<M>\d{2})\.(?P<S>\d{2})(?:[ _]?(?P<ampm>[AaPp][Mm]))?', re.I), False),
    # PXL_20261006_030559123.jpg (Pixel camera, UTC)
    (re.compile(r'PXL_(?P<y>\d{4})(?P<m>\d{2})(?P<d>\d{2})_(?P<H>\d{2})(?P<M>\d{2})(?P<S>\d{2})'), True),
    # IMG_20261006_083559.jpg | VID_20261006_083559 | Screenshot_20261006-083559 | 20261006_083559.jpg | IMG20261006083559.jpg
    (re.compile(r'(?<!\d)(?P<y>20\d{2})(?P<m>\d{2})(?P<d>\d{2})[_\-T ]?(?P<H>\d{2})(?P<M>\d{2})(?P<S>\d{2})(?!\d)'), False),
    # signal-2026-10-06-083559.jpg | 2026-10-06 08.35.59.jpg | 2026-10-06_08-35-59 | photo_2026-10-06_08-35-59
    (re.compile(r'(?P<y>20\d{2})-(?P<m>\d{2})-(?P<d>\d{2})[ _\-T](?P<H>\d{2})[.\-:_]?(?P<M>\d{2})[.\-:_]?(?P<S>\d{2})'), False),
]
# Date-only patterns: IMG-20261006-WA0012.jpg (WhatsApp Android), 2026-10-06.jpg
_DATE_ONLY_PATTERNS = [
    re.compile(r'IMG-(?P<y>20\d{2})(?P<m>\d{2})(?P<d>\d{2})-WA\d+', re.I),
    re.compile(r'(?<![\d-])(?P<y>20\d{2})-(?P<m>\d{2})-(?P<d>\d{2})(?![\d-])'),
    re.compile(r'(?<!\d)(?P<y>20\d{2})(?P<m>\d{2})(?P<d>\d{2})(?!\d)'),
]


def _valid_date(y, m, d, H=0, M=0, S=0):
    try:
        return datetime(int(y), int(m), int(d), int(H), int(M), int(S))
    except ValueError:
        return None


def parse_filename(name: str, tz: ZoneInfo):
    """Return (aware datetime | None, source) from a photo file name. Date-only names give source 'filename_date'."""
    stem = (name or '').strip()
    for pattern, is_utc in _FILENAME_PATTERNS:
        m = pattern.search(stem)
        if not m:
            continue
        g = m.groupdict()
        H = int(g['H']); ampm = (g.get('ampm') or '').upper()
        if ampm == 'PM' and H < 12: H += 12
        if ampm == 'AM' and H == 12: H = 0
        if H > 23: continue
        naive = _valid_date(g['y'], g['m'], g['d'], H, g['M'], g['S'])
        if not naive:
            continue
        if is_utc:
            return naive.replace(tzinfo=timezone.utc).astimezone(tz), 'filename'
        return naive.replace(tzinfo=tz), 'filename'
    for pattern in _DATE_ONLY_PATTERNS:
        m = pattern.search(stem)
        if m:
            naive = _valid_date(m['y'], m['m'], m['d'], 12, 0, 0)
            if naive:
                return naive.replace(tzinfo=tz), 'filename_date'
    return None, 'none'


def _parse_exif_value(value, offset, tz):
    m = _EXIF_DT.match(str(value or '').strip())
    if not m:
        return None
    naive = _valid_date(*m.groups())
    if not naive or naive.year < 2000:
        return None
    om = _OFFSET.match(str(offset or '').strip()) if offset else None
    if om:
        sign = 1 if om.group(1) == '+' else -1
        return naive.replace(tzinfo=timezone(sign * timedelta(hours=int(om.group(2)), minutes=int(om.group(3))))).astimezone(tz)
    return naive.replace(tzinfo=tz)


def read_exif(raw: bytes, tz: ZoneInfo):
    """Return ({'original': dt|None, 'created': dt|None, 'make': str, 'model': str}) using Pillow; HEIC via pillow-heif."""
    out = {'original': None, 'created': None, 'make': '', 'model': ''}
    try:
        from PIL import Image
        import pillow_heif
        pillow_heif.register_heif_opener()
        with Image.open(io.BytesIO(raw)) as im:
            exif = im.getexif()
            if not exif:
                return out
            ifd = exif.get_ifd(0x8769) if hasattr(exif, 'get_ifd') else {}
            out['make'] = str(exif.get(271, '') or '').strip()[:40]
            out['model'] = str(exif.get(272, '') or '').strip()[:60]
            # 36867 DateTimeOriginal, 36868 DateTimeDigitized, 306 DateTime; 36881/36882 OffsetTimeOriginal/Digitized, 36880 OffsetTime
            original = ifd.get(36867) or exif.get(36867)
            digitized = ifd.get(36868) or exif.get(36868)
            modified = exif.get(306)
            out['original'] = _parse_exif_value(original, ifd.get(36881) or exif.get(36881), tz) if original else None
            out['created'] = (_parse_exif_value(digitized, ifd.get(36882) or exif.get(36882), tz) if digitized else None) or \
                             (_parse_exif_value(modified, ifd.get(36880) or exif.get(36880), tz) if modified else None)
    except Exception:
        pass
    return out


_XMP_DATE = re.compile(rb'(?:xmp:CreateDate|photoshop:DateCreated|exif:DateTimeOriginal|xmp:ModifyDate)\s*(?:=\s*"|>)\s*(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:\d{2})?)')
_PNG_TIME = re.compile(rb'(?:Creation Time|create-date|date:create)\x00([^\x00]{10,40})')


def read_metadata(raw: bytes, tz: ZoneInfo):
    """XMP (JPEG/HEIC/PNG) or PNG text chunk creation dates."""
    for m in _XMP_DATE.finditer(raw[:2_000_000]):
        try:
            text = m.group(1).decode('ascii')
            dt = datetime.fromisoformat(text.replace('Z', '+00:00'))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=tz)
            return dt.astimezone(tz)
        except ValueError:
            continue
    m = _PNG_TIME.search(raw[:200_000])
    if m:
        text = m.group(1).decode('ascii', 'ignore').strip()
        for fmt in ('%Y-%m-%dT%H:%M:%S%z', '%Y-%m-%dT%H:%M:%S', '%Y:%m:%d %H:%M:%S', '%d %b %Y %H:%M:%S %z', '%Y-%m-%d %H:%M:%S'):
            try:
                dt = datetime.strptime(text[:len(fmt) + 10], fmt)
                return (dt if dt.tzinfo else dt.replace(tzinfo=tz)).astimezone(tz)
            except ValueError:
                continue
    return None


def detect_timestamp(raw: bytes, filename: str, tz_name: str, *, client_modified=None, client_exif=None, now=None, dob=None):
    """Choose the most trustworthy capture time. Returns a dict that is safe to store and show.

    client_modified: ISO string of the file-system modified time reported by the browser/phone (weak fallback).
    client_exif:     ISO or EXIF-formatted DateTimeOriginal reported by the phone's picker when the upload itself
                     was re-encoded (treated like EXIF but confidence medium because it did not travel in the file).
    """
    tz = ZoneInfo(tz_name or 'UTC')
    now = now or datetime.now(timezone.utc)
    candidates = []
    exif = read_exif(raw, tz)
    if exif['original']: candidates.append(('exif', exif['original']))
    if exif['created']: candidates.append(('exif_created', exif['created']))
    if client_exif:
        dt = _parse_exif_value(client_exif, None, tz)
        if dt is None:
            try:
                parsed = datetime.fromisoformat(str(client_exif).replace('Z', '+00:00'))
                dt = (parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)).astimezone(tz)
            except ValueError:
                dt = None
        if dt: candidates.append(('exif_client', dt))
    fn_dt, fn_source = parse_filename(filename, tz)
    if fn_dt and fn_source == 'filename': candidates.append(('filename', fn_dt))
    meta = read_metadata(raw, tz)
    if meta: candidates.append(('metadata', meta))
    if fn_dt and fn_source == 'filename_date': candidates.append(('filename_date', fn_dt))
    if client_modified:
        try:
            cm = datetime.fromisoformat(str(client_modified).replace('Z', '+00:00'))
            candidates.append(('filesystem', (cm if cm.tzinfo else cm.replace(tzinfo=tz)).astimezone(tz)))
        except ValueError:
            pass
    chosen = None; rejected = []
    for source, dt in candidates:
        if dt > now + timedelta(minutes=5):
            rejected.append({'source': source, 'at': dt.isoformat(), 'reason': 'in the future'}); continue
        if dob and dt.date() < dob - timedelta(days=1):
            rejected.append({'source': source, 'at': dt.isoformat(), 'reason': 'before birth'}); continue
        chosen = (source, dt); break
    if not chosen:
        source, dt = 'none', None
    else:
        source, dt = chosen
    stored_source = 'exif' if source == 'exif_client' else source
    confidence = CONFIDENCE.get(stored_source, 'none') if source != 'exif_client' else 'medium'
    return {
        'detected_at': dt, 'tz': tz_name, 'source': stored_source, 'confidence': confidence,
        'source_label': SOURCE_LABEL.get(stored_source, stored_source) + (' (reported by the phone)' if source == 'exif_client' else ''),
        'needs_confirmation': stored_source in NEEDS_CONFIRMATION, 'time_unknown': stored_source == 'filename_date',
        'original_filename': (filename or '')[:200], 'camera': ' '.join(x for x in (exif['make'], exif['model']) if x),
        'candidates': [{'source': s, 'at': d.isoformat()} for s, d in candidates], 'rejected': rejected,
    }


# ----------------------------------------------------------------------------------------------- visual suggestions
COLOUR_OPTIONS = ('brown', 'yellow', 'green', 'dark_green', 'black', 'red', 'pale', 'white', 'other')
CONSISTENCY_OPTIONS = ('watery', 'loose', 'soft', 'formed', 'hard')


def _texture(raw: bytes):
    """Spread, edge density and sheen of the stool-coloured region on a 96px copy. Deliberately coarse."""
    from PIL import Image, ImageOps, ImageFilter
    import pillow_heif
    pillow_heif.register_heif_opener()
    with Image.open(io.BytesIO(raw)) as im:
        im = ImageOps.exif_transpose(im).convert('RGB'); im.thumbnail((96, 96))
        w, h = im.size; px = list(im.getdata())
        edges = list(im.convert('L').filter(ImageFilter.FIND_EDGES).getdata())
    region = []; sheen = 0; dark_green = 0
    for i, (r, g, b) in enumerate(px):
        hh, ss, vv = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255); hd = hh * 360
        fam = care_vision._family(hd, ss, vv)
        if fam in ('brown', 'yellow', 'green', 'black', 'red', 'pale'):
            region.append(i)
            if ss < 0.25 and vv > 0.8: sheen += 1
            if fam == 'green' and vv < 0.45: dark_green += 1
    n = len(px) or 1
    if not region:
        return {'region_share': 0.0, 'edge_density': 0.0, 'sheen_share': 0.0, 'dark_green_share': 0.0, 'spread': 0.0}
    xs = [i % w for i in region]; ys = [i // w for i in region]
    bbox_area = max(1, (max(xs) - min(xs) + 1) * (max(ys) - min(ys) + 1))
    edge_density = sum(1 for i in region if edges[i] > 60) / len(region)
    return {'region_share': len(region) / n, 'edge_density': edge_density, 'sheen_share': sheen / len(region),
            'dark_green_share': dark_green / len(region), 'spread': len(region) / bbox_area}


def suggest(raw: bytes):
    """Form suggestions with confidences from the photograph. Never diagnostic."""
    observation = care_vision.observe(raw)
    try:
        tex = _texture(raw)
    except Exception:
        tex = {'region_share': 0.0, 'edge_density': 0.0, 'sheen_share': 0.0, 'dark_green_share': 0.0, 'spread': 0.0}
    colour = observation['apparent_colour']
    if colour == 'green' and tex['dark_green_share'] > 0.6: colour = 'dark_green'
    if colour not in COLOUR_OPTIONS: colour = 'other'
    colour_conf = observation['colour_confidence'] if colour != 'other' else 'low'
    # Consistency: thin, widely spread, low-edge regions read as watery/loose; compact high-edge regions as formed.
    consistency, cons_conf = 'unable_to_determine', 'none'
    if tex['region_share'] > 0.08:
        if tex['spread'] > 0.75 and tex['edge_density'] < 0.08: consistency, cons_conf = 'watery', 'low'
        elif tex['spread'] > 0.6 and tex['edge_density'] < 0.16: consistency, cons_conf = 'loose', 'low'
        elif tex['edge_density'] > 0.35 and tex['spread'] < 0.5: consistency, cons_conf = 'formed', 'low'
        elif tex['edge_density'] > 0.18: consistency, cons_conf = 'soft', 'low'
    watery = 'yes' if consistency == 'watery' else 'uncertain' if consistency in ('loose', 'unable_to_determine') else 'no'
    mucus = 'uncertain' if tex['sheen_share'] > 0.08 else 'no' if tex['region_share'] > 0.08 else 'uncertain'
    blood = 'uncertain' if observation['blood_like_colour'] == 'possible' else 'no'
    blood_conf = 'medium' if blood == 'uncertain' else 'low'
    amount = 'large' if tex['region_share'] > 0.55 else 'not_determined'
    review = colour in ('red', 'black', 'pale', 'white') or blood == 'uncertain'
    share = (observation.get('colour_shares') or {}).get(observation['apparent_colour'], 0)
    pct = lambda word, measured=None: int(round(measured * 100)) if measured else {'high': 85, 'medium': 65, 'low': 40}.get(word, 0)
    out = {
        'colour': {'value': colour, 'confidence': colour_conf, 'confidence_pct': pct(colour_conf, share if colour != 'other' else None)},
        'consistency': {'value': consistency, 'confidence': cons_conf, 'confidence_pct': pct(cons_conf)},
        'mucus_like_material': {'value': mucus, 'confidence': 'low', 'confidence_pct': pct('low')},
        'blood_like_colour': {'value': blood, 'confidence': blood_conf, 'confidence_pct': pct(blood_conf)},
        'watery_appearance': {'value': watery, 'confidence': cons_conf if consistency != 'unable_to_determine' else 'low', 'confidence_pct': pct(cons_conf if consistency != 'unable_to_determine' else 'low')},
        'amount': {'value': amount, 'confidence': 'low' if amount != 'not_determined' else 'none', 'confidence_pct': pct('low' if amount != 'not_determined' else 'none')},
        'consider_clinical_review': review,
        'review_message': 'Consider clinical review' if review else '',
        'observation': observation, 'texture': {k: round(v, 3) for k, v in tex.items()}, 'disclaimer': DISCLAIMER,
    }
    text = ' '.join(str(v) for v in (colour, consistency, mucus, blood, watery, amount, out['review_message'])).casefold()
    if any(word in text for word in care_vision.FORBIDDEN_WORDS):
        raise RuntimeError('Suggestion text must not contain diagnostic terms')
    return out


def form_prefill(suggestion):
    """Map suggestions onto StoolIn field values (None/'' where undetermined)."""
    if not suggestion:
        return {}
    tri = {'yes': True, 'no': False}
    cons = suggestion['consistency']['value']
    return {
        'colour': suggestion['colour']['value'] if suggestion['colour']['value'] in COLOUR_OPTIONS else '',
        'consistency': cons if cons in CONSISTENCY_OPTIONS else '',
        'amount': suggestion['amount']['value'] if suggestion['amount']['value'] in ('small', 'medium', 'large') else '',
        'mucus_observed': tri.get(suggestion['mucus_like_material']['value']),
        'blood_observed': tri.get(suggestion['blood_like_colour']['value']),
        'watery': suggestion['watery_appearance']['value'] == 'yes',
    }
