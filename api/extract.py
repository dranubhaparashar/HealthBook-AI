"""Local-only OCR, no third-party medical-data transmission."""
import io
import os
from pathlib import Path
from cryptography.fernet import Fernet
from .database import DATA_DIR

from .crypto import crypto, encrypt, decrypt

def heif_support():
    """HEIC decoding is optional: the native plugin may be blocked by OS policy. JPEG/PNG/PDF never depend on it."""
    try:
        import pillow_heif
        pillow_heif.register_heif_opener()
        return True
    except Exception:
        return False

def configure_tesseract():
    import pytesseract
    configured=os.getenv('TESSERACT_CMD')
    windows_default=Path('C:/Program Files/Tesseract-OCR/tesseract.exe')
    if configured:pytesseract.pytesseract.tesseract_cmd=configured
    elif os.name=='nt' and windows_default.exists():pytesseract.pytesseract.tesseract_cmd=str(windows_default)

def extract_text(raw:bytes,mime:str) -> str:
    configure_tesseract()
    if mime=='application/pdf':
        import fitz
        with fitz.open(stream=raw,filetype='pdf') as pdf:
            if pdf.page_count>35:raise ValueError('PDF exceeds 35 pages for one extraction; split it into parts')
            pages=[]
            for i,p in enumerate(pdf):
                t=p.get_text('text').strip()
                if len(t)<40:
                    try:
                        import pytesseract
                        from PIL import Image
                        png=p.get_pixmap(matrix=fitz.Matrix(1.5,1.5)).tobytes('png')
                        t=pytesseract.image_to_string(Image.open(io.BytesIO(png)),config='--psm 3',timeout=30)
                    except (ImportError, RuntimeError, OSError):
                        t+='\n[OCR unavailable; review this scanned page manually]'
                pages.append(f'=== PAGE {i+1} ===\n{t}')
            return '\n'.join(pages)[:200_000]
    from PIL import Image
    heif_support()
    import pytesseract
    image=Image.open(io.BytesIO(raw))
    image.verify()
    image=Image.open(io.BytesIO(raw)).convert('RGB')
    return '=== PAGE 1 ===\n'+pytesseract.image_to_string(image,config='--psm 3',timeout=30)[:200_000]


def medication_candidates(text: str):
    """Potential prescription lines ONLY; does not parse or authorize medication doses."""
    import re
    candidate_terms = re.compile(r'\b(?:vit(?:amin)?\s*[a-dk]\d?|calci(?:um|rol)|iron|ferrous|zinc|paracetamol|acetaminophen|ibuprofen|amoxi(?:cillin|clav)|cef(?:ixime|triaxone|podoxime)|azithromycin|probiotic|lactobacillus|antibiotic)\b', re.I)
    lines=[]
    for line in text.splitlines():
        match=candidate_terms.search(line)
        if match:
            lines.append({'potential_name':match.group(0),'raw_line':line.strip()[:240],
                'review_required':True,'dose_and_frequency':'not_parsed'})
    return lines[:80]
