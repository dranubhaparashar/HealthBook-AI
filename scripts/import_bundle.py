#!/usr/bin/env python3
"""Import a reviewed scan bundle into a running local BabyCare AI backend through its authenticated API.

The bundle directory must contain `manifest.json` (source_id, filename, sha256, category, record_date, title) and the
scanned files. An optional `curated_records.json` (produced after reviewing every page against the originals) adds
document metadata, vaccination/medication drafts, laboratory observations, admissions, growth entries, illness episodes
and handwritten-page review drafts. Everything created stays UNCONFIRMED: no vaccine is confirmed, no medicine is
verified, no reminder is activated and no administration event is recorded by this tool.

Idempotent: re-running reuses existing records (matched by source reference, document hash or natural keys) instead
of duplicating them. Only localhost HTTP targets are accepted. Credentials are never written to disk.
"""
from __future__ import annotations
import argparse
import getpass
import hashlib
import json
import os
import re
import secrets
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

class APIError(Exception):
    def __init__(self,status,detail):
        self.status,self.detail=status,detail
        super().__init__(f'{status}: {detail}')

class Client:
    def __init__(self,base,token=None):
        self.base=base.rstrip('/')
        parts=urlsplit(self.base)
        if parts.scheme!='http' or parts.hostname not in ('127.0.0.1','localhost','::1'):
            raise ValueError('For safety this importer connects only to a localhost HTTP server.')
        self.token=token
    def call(self,method,endpoint,payload=None,*,upload=None,fields=None,retries=3):
        for attempt in range(retries):
            try:return self._call(method,endpoint,payload,upload=upload,fields=fields)
            except APIError as e:
                if e.status==429 and attempt<retries-1:time.sleep(20);continue
                raise
    def _call(self,method,endpoint,payload=None,*,upload=None,fields=None):
        hdr={'Accept':'application/json','X-Requested-With':'BabyCare'}
        if self.token:hdr['Authorization']='Bearer '+self.token
        data=None
        if upload is not None:
            boundary='----babycare-'+secrets.token_hex(12)
            hdr['Content-Type']='multipart/form-data; boundary='+boundary
            chunks=[]
            for name,value in (fields or {}).items():
                if value is None or value=='':continue
                chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode('utf-8'))
            filename=upload.name.replace('"','_')
            mime='application/pdf' if filename.lower().endswith('.pdf') else 'image/png' if filename.lower().endswith('.png') else 'image/jpeg'
            chunks.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n'.encode('utf-8'))
            chunks.append(upload.read_bytes());chunks.append(b'\r\n--'+boundary.encode('ascii')+b'--\r\n')
            data=b''.join(chunks)
        elif payload is not None:
            hdr['Content-Type']='application/json';data=json.dumps(payload,ensure_ascii=False).encode('utf-8')
        req=Request(self.base+endpoint,data=data,method=method,headers=hdr)
        try:
            with urlopen(req,timeout=180) as r:
                body=r.read();return json.loads(body.decode('utf-8')) if body else None
        except HTTPError as e:
            try:v=json.loads(e.read().decode('utf-8'))
            except Exception:v={}
            detail=v.get('detail','Unknown server response') if isinstance(v,dict) else 'Unknown server response'
            raise APIError(e.code,detail if isinstance(detail,str) else json.dumps(detail)[:600]) from None
        except URLError:
            raise APIError(0,'Cannot reach BabyCare API. Start the backend first.') from None

def load_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))

def main():
    ap=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--bundle',required=True,help='Extracted bundle directory containing manifest.json and records/')
    ap.add_argument('--url',default='http://127.0.0.1:8000')
    ap.add_argument('--patient-id',type=int,help='Target child profile id (prompted if omitted)')
    ap.add_argument('--dob',default=None,help='Expected date of birth safeguard (YYYY-MM-DD); taken from curated_records.json expected_dob when omitted')
    ap.add_argument('--dry-run',action='store_true',help='Validate files and show what would be created; no network writes')
    ap.add_argument('--token-env',default='BABYCARE_IMPORT_TOKEN',help='Environment variable holding a session token (otherwise email/password prompt)')
    ap.add_argument('--yes',action='store_true',help='Skip interactive confirmations (for operator-run imports)')
    ap.add_argument('--documents-only',action='store_true')
    ap.add_argument('--skip-labs',action='store_true',help='Do not create laboratory observations')
    ap.add_argument('--ocr',action='store_true',help='Run local OCR extraction for every imported document that has not been extracted')
    args=ap.parse_args()
    root=Path(args.bundle).resolve()
    manifest=load_json(root/'manifest.json')
    curated=load_json(root/'curated_records.json') if (root/'curated_records.json').exists() else {}
    for row in manifest:
        file=root/row['filename']
        if not file.is_file():raise SystemExit(f'Missing bundled source scan: {row["source_id"]}')
        if hashlib.sha256(file.read_bytes()).hexdigest()!=row['sha256']:raise SystemExit(f'Checksum mismatch for {row["source_id"]}; refusing to import a modified scan')
    docs_meta=curated.get('documents',{});batch=curated.get('import_batch') or ('bundle-'+root.name)
    counts={k:len(curated.get(k,[])) for k in ('vaccines','medicines','observations','admissions','growth','episodes')}
    counts['review_drafts']=len(curated.get('review_drafts',{}))
    print(f'Validated {len(manifest)} scans (all checksums match). Curated records: {counts}. Import batch label: {batch}')
    print('Everything created is an UNCONFIRMED draft: no vaccine confirmation, no medication verification, no reminder activation, no administration events.')
    if args.dry_run:
        for row in manifest[:5]:
            meta=docs_meta.get(row['source_id'],{});print(f'  would upload {row["source_id"]}: category={meta.get("category",row["category"])} date={meta.get("record_date",row["record_date"]) or "unknown"} title={meta.get("title",row["title"])!r}')
        print(f'  … and {len(manifest)-5} more scans. Dry run complete; nothing was sent.');return 0
    if not args.yes and input('Type IMPORT to proceed: ').strip()!='IMPORT':print('Cancelled.');return 1
    token=os.environ.get(args.token_env)
    cli=Client(args.url,token)
    if not token:
        email=input('BabyCare AI login email: ').strip();password=getpass.getpass('BabyCare AI password (hidden): ')
        auth=cli.call('POST','/api/auth/login',{'email':email,'password':password,'client':'mobile'});cli.token=auth.get('token');del password
        if not cli.token:raise SystemExit('Login did not return a token')
    patients=cli.call('GET','/api/patients')
    if args.patient_id is None:
        for p in patients:print(' ',p['id'],p['name'],'DOB',p['dob'])
        args.patient_id=int(input('Enter exact child profile id: ').strip())
    patient=next((p for p in patients if p['id']==args.patient_id),None)
    if not patient:raise SystemExit('Patient id not found in this account')
    expected_dob=args.dob or curated.get('expected_dob')
    if not expected_dob:raise SystemExit('Pass --dob YYYY-MM-DD (or set expected_dob in curated_records.json); the importer refuses to guess the child identity.')
    if patient['dob']!=expected_dob:raise SystemExit(f'STOP: profile DOB {patient["dob"]} does not match expected {expected_dob}. Fix identity first; nothing uploaded.')
    pid=args.patient_id;errors=[]
    # ---------------------------------------------------------------- documents
    existing={d.get('source_ref'):d for d in cli.call('GET',f'/api/patients/{pid}/documents') if d.get('source_ref')}
    doc_ids={}
    print('\nUploading original scans…')
    for ix,row in enumerate(manifest,1):
        rid=row['source_id'];meta=docs_meta.get(rid,{})
        if rid in existing:doc_ids[rid]=existing[rid]['id'];print(f'[{ix}/{len(manifest)}] {rid}: already imported as document #{existing[rid]["id"]}');continue
        fields={'category':meta.get('category',row['category']),'title':(meta.get('title') or row['title'])[:180],'record_date':meta.get('record_date',row['record_date']) or '',
                'source_ref':rid,'import_batch':batch,'notes':meta.get('notes','')}
        try:
            result=cli.call('POST',f'/api/patients/{pid}/documents',upload=root/row['filename'],fields=fields)
            doc_ids[rid]=result['id'];print(f'[{ix}/{len(manifest)}] {rid}: uploaded document #{result["id"]}')
        except APIError as e:
            found=re.search(r'document #(\d+)',e.detail) if e.status==409 else None
            if found:doc_ids[rid]=int(found.group(1));print(f'[{ix}/{len(manifest)}] {rid}: existing file, reusing document #{found.group(1)}');continue
            errors.append(f'{rid}: {e.status} {e.detail}');print(f'[{ix}/{len(manifest)}] {rid}: FAILED {e.status} {e.detail}')
    print('Originals linked:',len(doc_ids),'/',len(manifest))
    created={'vaccines':0,'medicines':0,'observations':0,'admissions':0,'growth':0,'episodes':0,'review_drafts':0,'ocr':0}
    if not args.documents_only:
        # ------------------------------------------------------------ vaccines (unconfirmed)
        have=cli.call('GET',f'/api/patients/{pid}/vaccines')
        for rec in curated.get('vaccines',[]):
            doc=doc_ids.get(rec['source_id'])
            if not doc:continue
            if any(x.get('source_document_id')==doc and x['name']==rec['name'] and x['dose_label']==rec['dose_label'] for x in have):continue
            body={'name':rec['name'],'dose_label':rec['dose_label'],'given_date':rec.get('given_date'),'due_date':rec.get('due_date'),'source_type':rec['source_type'],'source_document_id':doc,
                  'note':('UNCONFIRMED transcription from the original; review before confirming. '+rec.get('note',''))[:1000],'duplicate_confirmed':True}
            try:r=cli.call('POST',f'/api/patients/{pid}/vaccines',body);have.append(r);created['vaccines']+=1
            except APIError as e:errors.append(f'Vaccine {rec["name"]} {rec["dose_label"]}: {e.status} {e.detail}')
        # ------------------------------------------------------------ medicines (drafts)
        have=cli.call('GET',f'/api/patients/{pid}/medicines');med_ids={}
        for rec in curated.get('medicines',[]):
            doc=doc_ids.get(rec['source_id'])
            if not doc:continue
            match=next((x for x in have if x.get('source_document_id')==doc and x['name']==rec['name'] and x.get('course_type',rec['course_type'])==rec['course_type']),None)
            if match:med_ids[(rec['name'],rec['course_type'])]=match['id'];continue
            body={k:rec.get(k,'') for k in ('name','instructions','dosage','dose_unit','route','frequency','prescribing_doctor','indication')}
            body.update(course_type=rec['course_type'],prescription_date=rec.get('prescription_date'),start_date=None,end_date=None,schedule_times='',source_document_id=doc)
            try:r=cli.call('POST',f'/api/patients/{pid}/medicines',body);have.append(r);med_ids[(rec['name'],rec['course_type'])]=r['id'];created['medicines']+=1
            except APIError as e:errors.append(f'Medicine {rec["name"]}: {e.status} {e.detail}')
        # ------------------------------------------------------------ observations
        obs_ids_by_date={}
        if not args.skip_labs:
            have=cli.call('GET',f'/api/patients/{pid}/observations')
            for rec in curated.get('observations',[]):
                doc=doc_ids.get(rec['source_id'])
                if not doc:continue
                match=next((x for x in have if x.get('source_document_id')==doc and x['test_name']==rec['test_name'] and x['record_date']==rec['record_date'] and x['value']==rec['value']),None)
                if not match:
                    body={k:rec[k] for k in ('test_name','value','unit','specimen','record_date','reference_range','verified','organism','culture_findings','susceptibility')}
                    body['source_document_id']=doc
                    try:match=cli.call('POST',f'/api/patients/{pid}/observations',body);have.append(match);created['observations']+=1
                    except APIError as e:errors.append(f'Lab {rec["test_name"]} {rec["record_date"]}: {e.status} {e.detail}');continue
                obs_ids_by_date.setdefault(rec['record_date'],[]).append(match['id'])
        else:
            for x in cli.call('GET',f'/api/patients/{pid}/observations'):obs_ids_by_date.setdefault(x['record_date'],[]).append(x['id'])
        # ------------------------------------------------------------ admissions
        have=cli.call('GET',f'/api/patients/{pid}/admissions');adm_ids={}
        for rec in curated.get('admissions',[]):
            match=next((x for x in have if x['facility']==rec['facility'] and x['admitted_on']==rec['admitted_on']),None)
            if not match:
                body={k:rec[k] for k in ('facility','doctor','diagnosis','treatment_plan','admitted_on','discharged_on')}
                body.update(source_document_id=doc_ids.get(rec['source_id']),discharge_document_id=doc_ids.get(rec.get('discharge_source_id')))
                try:match=cli.call('POST',f'/api/patients/{pid}/admissions',body);have.append(match);created['admissions']+=1
                except APIError as e:errors.append(f'Admission {rec["facility"]}: {e.status} {e.detail}');continue
            adm_ids[rec['key']]=match['id']
        # ------------------------------------------------------------ growth
        have=cli.call('GET',f'/api/patients/{pid}/growth')
        for rec in curated.get('growth',[]):
            if any(x['record_date']==rec['record_date'] and x.get('weight_kg')==rec.get('weight_kg','') and x.get('source_document_id')==doc_ids.get(rec['source_id']) for x in have):continue
            body={'record_date':rec['record_date'],'weight_kg':rec.get('weight_kg',''),'length_cm':rec.get('length_cm',''),'head_cm':rec.get('head_cm',''),'milestone':rec.get('milestone',''),'note':rec.get('note',''),'source_document_id':doc_ids.get(rec['source_id'])}
            try:cli.call('POST',f'/api/patients/{pid}/growth',body);created['growth']+=1
            except APIError as e:errors.append(f'Growth {rec["record_date"]}: {e.status} {e.detail}')
        # ------------------------------------------------------------ episodes with explicit links
        have=cli.call('GET',f'/api/patients/{pid}/episodes')
        for rec in curated.get('episodes',[]):
            if any(x['title']==rec['title'] and x['started_on']==rec['started_on'] for x in have):continue
            links=[{'kind':'document','id':doc_ids[s]} for s in rec.get('documents',[]) if s in doc_ids]
            if rec.get('admission') in adm_ids:links.append({'kind':'admission','id':adm_ids[rec['admission']]})
            for name in rec.get('medicines',[]):
                for (n,ct),mid in med_ids.items():
                    if n==name:links.append({'kind':'medicine','id':mid})
            for d in rec.get('observation_dates',[]):
                for oid in obs_ids_by_date.get(d,[]):links.append({'kind':'observation','id':oid})
            body={'title':rec['title'],'started_on':rec['started_on'],'ended_on':rec.get('ended_on'),'symptoms':rec.get('symptoms',''),'documented_diagnosis':rec.get('documented_diagnosis',''),'notes':rec.get('notes',''),
                  'source_document_id':doc_ids.get(rec.get('source_id')),'links':links[:100]}
            try:cli.call('POST',f'/api/patients/{pid}/episodes',body);created['episodes']+=1
            except APIError as e:errors.append(f'Episode {rec["title"]}: {e.status} {e.detail}')
        # ------------------------------------------------------------ review drafts for handwritten pages
        for rid,draft in curated.get('review_drafts',{}).items():
            doc=doc_ids.get(rid)
            if not doc:continue
            try:
                current=cli.call('GET',f'/api/documents/{doc}/review')
                if current.get('status')=='confirmed' or str(current.get('text','')).startswith('[REVIEWER TRANSCRIPTION'):continue
                cli.call('PUT',f'/api/documents/{doc}/review',{'text':draft['text'],'items':draft['items'],'version':current.get('version',1),'checked_against_original':False});created['review_drafts']+=1
            except APIError as e:errors.append(f'Review draft {rid}: {e.status} {e.detail}')
    if args.ocr:
        print('\nRunning local OCR (unverified text for search only)…')
        for rid,doc in doc_ids.items():
            try:
                state=cli.call('GET',f'/api/documents/{doc}/extraction')
                if state.get('text'):continue
                cli.call('POST',f'/api/documents/{doc}/extract');created['ocr']+=1;print(f'  OCR {rid} → #{doc}')
            except APIError as e:errors.append(f'OCR {rid}: {e.status} {e.detail}')
    print('\nSUMMARY: documents linked',len(doc_ids),'| created this run:',created)
    if errors:
        print(f'{len(errors)} item(s) failed; originals were not deleted. Fix and re-run (idempotent):')
        for e in errors[:40]:print(' -',e)
        return 2
    print('All vaccine, medicine and lab entries remain drafts/unverified until a caregiver confirms them in the app. No reminders were activated.')
    return 0

if __name__=='__main__':
    try:sys.exit(main())
    except (APIError,ValueError,KeyboardInterrupt) as e:print(f'Import stopped: {e}',file=sys.stderr);sys.exit(1)
