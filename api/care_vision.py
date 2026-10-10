"""Image-based observations for stool photographs. Local, deterministic, descriptive only.

The output describes visible characteristics (apparent colour family, saturated red-like areas, very dark areas,
very pale areas). It never names an organism, an infection or a disease; every result carries the fixed
disclaimer. Pixel statistics are computed on a downscaled copy; the original is never modified.
"""
import io
import colorsys
from collections import Counter

DISCLAIMER='Image-based observation only — not a diagnosis.'
FORBIDDEN_WORDS=('coli','bacteria','bacterial','viral','virus','parasite','infection','disease','diagnos','rotavirus','salmonella','shigella','giardia','ibd','crohn')
REVIEW_COLOURS={'black':'very dark / black appearance','red':'red-like colour visible','pale':'very pale appearance','white':'white / chalky appearance'}

def _family(h,s,v):
    """Map HSV (h in degrees) to a coarse colour family. Thresholds are deliberately conservative."""
    if v<0.18:return 'black'
    if s<0.12 and v>0.85:return 'white'
    if s<0.22 and v>0.6:return 'pale'
    if (h<15 or h>=345) and s>0.45 and v>0.3:return 'red'
    if 15<=h<45:return 'brown' if v<0.65 else 'yellow'
    if 45<=h<70:return 'yellow'
    if 70<=h<170:return 'green'
    if 170<=h<345 and s<0.35:return 'brown'
    return 'other'

def observe(raw:bytes):
    """Return a descriptive observation dict. Raises ValueError for unreadable images."""
    from PIL import Image, ImageOps
    import pillow_heif
    pillow_heif.register_heif_opener()
    with Image.open(io.BytesIO(raw)) as im:
        im.verify()
    with Image.open(io.BytesIO(raw)) as im:
        im=ImageOps.exif_transpose(im).convert('RGB')
        width,height=im.size
        im.thumbnail((160,160))
        pixels=list(im.getdata())
    if not pixels:raise ValueError('Empty image')
    # Focus on the central region (typical framing); ignore near-white borders such as nappy liners.
    w,h=im.size
    centre=[p for i,p in enumerate(pixels) if (w*0.15<=i%w<=w*0.85) and (h*0.15<=i//w<=h*0.85)] or pixels
    families=Counter();red_like=0;dark=0;pale=0;sat_total=0.0;val_total=0.0
    for r,g,b in centre:
        hh,ss,vv=colorsys.rgb_to_hsv(r/255,g/255,b/255);hd=hh*360
        fam=_family(hd,ss,vv);families[fam]+=1
        if fam=='red':red_like+=1
        if vv<0.18:dark+=1
        if ss<0.2 and vv>0.75:pale+=1
        sat_total+=ss;val_total+=vv
    n=len(centre)
    ranked=[(k,c/n) for k,c in families.most_common() if k!='other']
    dominant=ranked[0][0] if ranked else 'other';share=ranked[0][1] if ranked else 0
    confidence='high' if share>0.6 else 'medium' if share>0.4 else 'low'
    notes=[]
    red_share=red_like/n;dark_share=dark/n;pale_share=pale/n
    if red_share>0.04:notes.append(f'red-like colour visible in about {round(red_share*100)}% of the sampled area')
    if dark_share>0.5:notes.append('predominantly very dark / black appearance')
    if pale_share>0.5:notes.append('predominantly very pale appearance')
    review=[REVIEW_COLOURS[k] for k in ('black','red','pale','white') if (k==dominant and share>0.4) or (k=='red' and red_share>0.04)]
    result={'apparent_colour':dominant,'colour_confidence':confidence,'colour_shares':{k:round(v,3) for k,v in ranked[:4]},
        'apparent_consistency':'not assessed from a photograph','mucus_like_material':'not assessed','blood_like_colour':'possible' if red_share>0.04 else 'not evident in sampled area',
        'notes':notes,'consider_clinical_review':bool(review),'review_reasons':review,'image_size':[width,height],'sampled_pixels':n,'disclaimer':DISCLAIMER}
    for text in [result['apparent_colour']]+notes+review:
        low=text.casefold()
        if any(word in low for word in FORBIDDEN_WORDS):raise RuntimeError('Observation text must not contain diagnostic terms')
    return result
