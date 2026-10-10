"""Generate a clearly fictional PDF for browser tests."""
from pathlib import Path
import pymupdf
root=Path(__file__).resolve().parents[1]/'.cache'/'fixtures'
root.mkdir(parents=True,exist_ok=True)
with pymupdf.open() as document:
    page=document.new_page()
    page.insert_text((50,80),'FICTIONAL TEST VACCINATION CARD\nMMR given 2026-01-05\nPCV next due 2026-12-03\nClinic: Synthetic Test Clinic',fontsize=14)
    document.save(root/'vaccination.pdf')

# Synthetic stool photographs for tests/browser/stool_intake.spec.js: one with an EXIF capture time two days ago,
# one named like a WhatsApp download from yesterday (no EXIF), one with no usable timestamp at all.
import io
from datetime import date, timedelta
from PIL import Image
d2=date.today()-timedelta(days=2); d1=date.today()-timedelta(days=1)
def _jpeg(colour,path,exif=None):
    im=Image.new('RGB',(320,240),colour); b=io.BytesIO()
    im.save(b,format='JPEG',**({'exif':exif} if exif else {})); (root/path).write_bytes(b.getvalue())
ex=Image.Exif(); ex[271]='FictionalCam'; ex.get_ifd(0x8769)[36867]=f'{d2:%Y:%m:%d} 14:05:09'
_jpeg((120,80,40),'exif-stool.jpg',ex.tobytes())
_jpeg((150,110,60),f'WhatsApp Image {d1} at 8.35.59 AM.jpg')
_jpeg((135,95,55),'no-timestamp.jpg')
_jpeg((160,125,60),f'WhatsApp Image {d1} at 8.52.01 PM.jpg')
