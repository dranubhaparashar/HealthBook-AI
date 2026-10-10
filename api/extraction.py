"""Conservative, page-linked candidates. No extracted item is a verified fact."""
import re
from datetime import date
from .vaccination import card_candidates, DATE_RE
from .extract import medication_candidates

def parse_unambiguous_date(value):
    try:
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):return date.fromisoformat(value).isoformat()
        day,month,year=map(int,re.split(r'[/-]',value))
        if day<=12 and month<=12:return None
        return date(year,month,day).isoformat()
    except (ValueError,TypeError):return None

def pages(text):
    result=[];page=1;lines=[]
    for line in text.splitlines():
        match=re.fullmatch(r'=== PAGE (\d+) ===',line.strip())
        if match:
            if lines:result.append((page,'\n'.join(lines)))
            page=int(match[1]);lines=[]
        else:lines.append(line)
    if lines:result.append((page,'\n'.join(lines)))
    return result

def classify(text):
    t=text.casefold()
    for kind,terms in [('vaccination_card',['vaccination','immunization','immunisation']),('discharge',['discharge summary']),('lab',['reference range','specimen','haemoglobin','hemoglobin']),('prescription',['prescription','rx','dosage']),('hospital',['admission','hospital'])]:
        if any(term in t for term in terms):return kind
    return 'other'

def structured_candidates(text,category):
    rows=[]
    for page,body in pages(text):
        if category=='vaccination_card':
            for c in card_candidates(body):
                fields={'name':c['vaccine_candidate'],'dose_label':c.get('dose_label',''),'note':c['raw_line'],'source_type':'vaccination_card'}
                dates=c['date_candidates']
                line=c['raw_line']
                given=re.search(r'\b(?:given|administered|received|done|vaccinated)\s*(?:on)?\s*[:=-]?\s*'+DATE_RE.pattern,line,re.I)
                next_due=re.search(r'\b(?:next(?:\s+dose)?|due|scheduled|appointment)\s*(?:on)?\s*[:=-]?\s*'+DATE_RE.pattern,line,re.I)
                if given and next_due:
                    actual=parse_unambiguous_date(given[1]);upcoming=parse_unambiguous_date(next_due[1])
                    if actual:fields['given_date']=actual
                    if upcoming:fields['next_due_date']=upcoming
                for key,pattern in [('dose_label',r'\bdose\s*[:#-]?\s*(\d+|booster)\b'),('batch_number',r'\bbatch\s*(?:no\.?)?\s*[:#-]\s*([^;|,]+)'),('facility',r'\b(?:clinic|hospital)\s*:\s*([^;|]+)'),('doctor',r'\bdoctor\s*:\s*([^;|]+)')]:
                    match=re.search(pattern,line,re.I)
                    if match:fields[key]=match[1].strip()
                if len(dates)==1:
                    value=parse_unambiguous_date(dates[0])
                    if value and c['interpretation']=='possible_given_date':fields['given_date']=value
                    if value and c['interpretation']=='possible_due_date':fields['due_date']=value
                rows.append(dict(kind='vaccine',page=page,fields=fields,verified=False,activate_reminders=False,raw_line=c['raw_line'],uncertain_fields=['name','dose_label','given_date','due_date'],date_candidates=dates))
        if category=='prescription':
            for c in medication_candidates(body):
                rows.append(dict(kind='medicine',page=page,fields={'name':c['potential_name'],'instructions':c['raw_line']},verified=False,activate_reminders=False,raw_line=c['raw_line'],uncertain_fields=['name','dosage','frequency','dates']))
        if category=='lab':
            # Deliberately requires a numeric value and printed unit. Unknown dates remain blank.
            pattern=r'^([A-Za-z][A-Za-z ()\-/]{1,70}?)\s{2,}([<>]?\d+(?:\.\d+)?)\s+([A-Za-z%µ/\^\d.]+)(?:\s{2,}(.+))?$'
            for line in body.splitlines():
                m=re.match(pattern,line.strip())
                if m:rows.append(dict(kind='observation',page=page,fields={'test_name':m[1].strip(),'value':m[2],'unit':m[3],'reference_range':m[4] or ''},verified=False,activate_reminders=False,raw_line=line,uncertain_fields=['test_name','value','unit','reference_range','record_date','specimen']))
    return rows[:300]
