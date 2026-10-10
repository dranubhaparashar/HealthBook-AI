"""Reference-only India UIP age chart; never a personalized catch-up order."""
import re
from datetime import timedelta
from dateutil.relativedelta import relativedelta

SOURCE = 'https://mohfw.gov.in/sites/default/files/Final%20Printed%20English%20AR%202024-25.pdf'
IAP_SOURCE = 'https://www.indianpediatrics.net/may2026/311.pdf'
# JE applies only where indicated by the programme; confirm with local clinician.
# Do not automatically activate reminders from the reference chart.
SCHEDULE = [
    (0,'Birth','BCG','Birth dose'),(0,'Birth','OPV','0'),(0,'Birth','Hepatitis B','Birth dose'),
    (6,'Weeks','OPV','1'),(6,'Weeks','Rotavirus','1'),(6,'Weeks','fIPV','1'),(6,'Weeks','Pentavalent','1'),(6,'Weeks','PCV','1'),
    (10,'Weeks','OPV','2'),(10,'Weeks','Rotavirus','2'),(10,'Weeks','Pentavalent','2'),
    (14,'Weeks','OPV','3'),(14,'Weeks','Rotavirus','3'),(14,'Weeks','fIPV','2'),(14,'Weeks','Pentavalent','3'),(14,'Weeks','PCV','2'),
    (9,'Months','Measles-Rubella (MR)','1'),(9,'Months','PCV','Booster'),(9,'Months','fIPV','3'),
    (9,'Months','Japanese encephalitis (JE) — eligible areas only','1'),
    (16,'Months','Measles-Rubella (MR)','2'),(16,'Months','DPT','Booster 1'),
    (16,'Months','OPV','Booster'),(16,'Months','Japanese encephalitis (JE) — eligible areas only','2'),
    (5,'Years','DPT','Booster 2'),(10,'Years','Td','10 years'),(16,'Years','Td','16 years')
]

def reference(dob, schedule='India_UIP'):
    result=[]
    for amount, unit, name, dose in (IAP_SCHEDULE if schedule=='IAP_private' else SCHEDULE):
        when=dob+(timedelta(weeks=amount) if unit=='Weeks' else relativedelta(months=amount) if unit=='Months' else relativedelta(years=amount) if unit=='Years' else relativedelta())
        result.append({'name':name,'dose_label':dose,'age_window': f'{amount} {unit.lower()}' if amount else 'birth', 'reference_date':when.isoformat(), 'reference_window_end':(dob+relativedelta(months=12)).isoformat() if schedule=='India_UIP' and unit=='Months' and amount==9 else (dob+relativedelta(months=24)).isoformat() if schedule=='India_UIP' and unit=='Months' and amount==16 else (dob+relativedelta(years=6)).isoformat() if unit=='Years' and amount in (4,5) else (dob+relativedelta(months=18)).isoformat() if schedule=='IAP_private' and unit=='Months' and amount==16 else None, 'schedule':schedule,'reference_version':'IAP ACVIP 2025 (published April 2026)' if schedule=='IAP_private' else 'MoHFW annual report 2024-25','source':IAP_SOURCE if schedule=='IAP_private' else SOURCE,'warning':'REFERENCE ONLY: schedule varies; not an appointment or catch-up instruction.'})
    return result

# Conservative card extraction: only explicit "given" or "next/due" labels produce type suggestions;
# unlabeled dates are NOT assumed to be due dates. All candidates remain unverified.
VACCINE_RE = re.compile(r'\b(?:BCG|OPV(?:\s*[-–]?\s*\d+)?|IPV|fIPV|Penta(?:valent)?|Hep(?:atitis)?\s*B|MR(?:\s*[-–]?\s*\d+)?|MMR|PCV|DPT|DTaP|Td|JE|Rotavirus|Typhoid|Influenza|Hib|Varicella|Hep\s*A)\b', re.I)
DATE_RE = re.compile(r'(?<!\d)(\d{1,2}[/-]\d{1,2}[/-]\d{4}|\d{4}-\d{2}-\d{2})(?!\d)')

def card_candidates(text: str):
    out=[]
    page=1
    for line in text.splitlines():
        marker=re.match(r'=== PAGE (\d+) ===',line)
        if marker:page=int(marker[1]);continue
        match=VACCINE_RE.search(line)
        if not match:continue
        dates=DATE_RE.findall(line)
        label='unspecified'
        if re.search(r'\b(next|due|scheduled|appointment)\b',line,re.I):label='possible_due_date'
        elif re.search(r'\b(given|administered|received|done|vaccinated)\b',line,re.I):label='possible_given_date'
        if len(dates)>1:label='ambiguous_multiple_dates'
        out.append({'vaccine_candidate':' + '.join(m.group(0) for m in VACCINE_RE.finditer(line)),'source_page':page,'date_candidates':dates,'interpretation':label,'raw_line':line[:260], 'review_required':True})
    return out[:100]

# Separate IAP reference milestones, not individualized orders. Variant/brand rules require review.
IAP_SCHEDULE = [
    (0,'Birth','BCG / OPV / Hepatitis B','Birth'),
    (6,'Weeks','DTwP/DTaP, IPV, Hib, Hepatitis B, Rotavirus, PCV','Primary visit 1'),
    (10,'Weeks','DTwP/DTaP, IPV, Hib, Hepatitis B, Rotavirus, PCV','Primary visit 2'),
    (14,'Weeks','DTwP/DTaP, IPV, Hib, Hepatitis B, Rotavirus, PCV','Primary visit 3; product-dependent'),
    (6,'Months','Influenza','1; clinician determines annual plan'),
    (7,'Months','Influenza','2'),
    (6,'Months','Typhoid conjugate','6-9 month window'),
    (9,'Months','MMR','1'),
    (12,'Months','Hepatitis A','1; product-dependent'),
    (15,'Months','MMR, Varicella, PCV','MMR 2 / Varicella 1 / PCV booster'),
    (16,'Months','DTwP/DTaP, Hib, IPV','Booster 1; 16-18 months'),
    (18,'Months','Hepatitis A, Varicella','18-19 months; Hep A 2 only for inactivated product'),
    (4,'Years','DTwP/DTaP, IPV, MMR','4-6 years'),
    (9,'Years','HPV','9 to under 15 years; clinician review'),
    (10,'Years','Tdap','Adolescent milestone'),
    (16,'Years','Td/Tdap','16-18 years'),
]
