import io
import json
import uuid
from datetime import date,datetime,timedelta,timezone
from zoneinfo import ZoneInfo
import httpx
from cryptography.fernet import Fernet
from tests.test_api import api,make_user,make_child,make_doc
from api.database import SessionLocal
from api.models import RateWindow,Patient,Reminder,Notification,PushDevice,Vaccine
from api.reminders import reconcile
from api.worker import deliver

def test_real_worker_adapter_retries_and_checks_receipts(monkeypatch):
    with SessionLocal() as db:db.query(RateWindow).delete();db.commit()
    t=make_user(uuid.uuid4().hex);pid=make_child(t);doc=make_doc(t,pid,'vaccination_card')
    due=(date.today()+timedelta(days=7)).isoformat()
    v=api('POST',f'/api/patients/{pid}/vaccines',t,json={'name':'Synthetic vaccine','due_date':due,'source_document_id':doc}).json()
    assert api('PATCH',f'/api/vaccines/{v["id"]}',t,json={'confirmed':True}).status_code==200
    assert api('POST','/api/devices',t,json={'token':'ExpoPushToken[synthetic_token_'+str(pid)+']','consent_generic_push':True}).status_code==200
    now=datetime.combine(date.today(),datetime.min.time(),ZoneInfo('Asia/Kolkata')).replace(hour=9,minute=1).astimezone(timezone.utc)
    monkeypatch.setenv('BABYCARE_PUSH_ENABLED','true')
    class FakeTransport:
        def __init__(self):self.fail=True;self.bodies=[]
        def post(self,url,json,headers):
            self.bodies.append(json)
            request=httpx.Request('POST',url)
            if self.fail:raise httpx.ConnectError('Synthetic outage',request=request)
            if url.endswith('getReceipts'):return httpx.Response(200,json={'data':{'ticket-1':{'status':'ok'}}},request=request)
            return httpx.Response(200,json={'data':{'status':'ok','id':'ticket-1'}},request=request)
    transport=FakeTransport()
    with SessionLocal() as db:
        reconcile(db,db.get(Patient,pid),now)
        n=db.query(Notification).join(Reminder).filter(Reminder.patient_id==pid,Notification.channel=='expo').one()
        deliver(db,n,transport,now);db.commit();assert n.status=='retry' and n.attempts==1
        transport.fail=False
        deliver(db,n,transport,now+timedelta(minutes=2));db.commit();assert n.status=='accepted'
        assert 'Synthetic vaccine' not in json.dumps(transport.bodies)
        transport.fail=True
        deliver(db,n,transport,now+timedelta(minutes=18));db.commit();assert n.status=='accepted'
        transport.fail=False
        deliver(db,n,transport,now+timedelta(minutes=34));db.commit();assert n.status=='provider_received'
        assert sum('to' in b for b in transport.bodies)==2 # First send failed; receipt retries never resend.
        assert db.get(Vaccine,v['id']).given_date is None

def test_backup_restore_never_overwrites_and_preserves_key(tmp_path,monkeypatch):
    import pytest
    from api.database import engine
    if engine.dialect.name!='sqlite':pytest.skip('api.backup is the SQLite snapshot tool; PostgreSQL uses scripts/cloud_backup.py (covered in tests/test_cloud.py)')
    from api.backup import backup,restore
    monkeypatch.setenv('BABYCARE_BACKUP_KEY',Fernet.generate_key().decode())
    target=tmp_path/'snapshot.enc';backup(target)
    restored=tmp_path/'new-location';restore(target,restored)
    assert (restored/'babycare.sqlite3').read_bytes().startswith(b'SQLite format 3')
    assert len((restored/'file_crypt.key').read_bytes())==44
    import pytest
    with pytest.raises(RuntimeError):restore(target,restored)


def test_missing_application_key_never_generates_replacement_for_existing_files(tmp_path,monkeypatch):
    import pytest
    from api.crypto import crypto
    monkeypatch.setattr('api.crypto.DATA_DIR',tmp_path)
    monkeypatch.delenv('BABYCARE_FERNET_KEY',raising=False)
    folder=tmp_path/'encrypted_files';folder.mkdir();(folder/'synthetic.enc').write_bytes(b'encrypted-original')
    with pytest.raises(RuntimeError,match='Restore the original key'):crypto()
    assert not (tmp_path/'file_crypt.key').exists()

def test_account_deletion_removes_owned_records_but_not_other_family():
    with SessionLocal() as db:db.query(RateWindow).delete();db.commit()
    t=make_user(uuid.uuid4().hex);pid=make_child(t);doc=make_doc(t,pid)
    b=make_user(uuid.uuid4().hex);other=make_child(b)
    r=api('POST','/api/account/delete',t,json={'password':'correct-horse-battery-2026','confirmation':'DELETE MY ACCOUNT AND OWNED CHILD RECORDS'})
    assert r.status_code==200,r.text
    assert api('GET','/api/me',t).status_code==401
    assert api('GET',f'/api/documents/{doc}/download',b).status_code==404
    assert api('GET',f'/api/patients/{other}/documents',b).status_code==200

def test_invalid_file_and_future_administration_rejected():
    with SessionLocal() as db:db.query(RateWindow).delete();db.commit()
    t=make_user(uuid.uuid4().hex);pid=make_child(t)
    assert api('POST',f'/api/patients/{pid}/documents',t,files={'file':('evil.png',b'<script>alert(1)</script>','image/png')}).status_code==415
    assert api('POST',f'/api/patients/{pid}/vaccines',t,json={'name':'Synthetic vaccine','given_date':(date.today()+timedelta(days=1)).isoformat()}).status_code==422


def test_independent_worker_process_persists_notifications_without_browser():
    import os,subprocess,sys
    with SessionLocal() as db:db.query(RateWindow).delete();db.commit()
    t=make_user(uuid.uuid4().hex);pid=make_child(t);doc=make_doc(t,pid)
    # Yesterday remains in the worker recovery window at any time of day.
    due=(datetime.now(ZoneInfo('Asia/Kolkata')).date()+timedelta(days=6)).isoformat()
    v=api('POST',f'/api/patients/{pid}/vaccines',t,json={'name':'Synthetic background reminder','due_date':due,'source_document_id':doc}).json()
    assert api('PATCH',f'/api/vaccines/{v["id"]}',t,json={'confirmed':True}).status_code==200
    result=subprocess.run([sys.executable,'-m','api.worker'],env={**os.environ,'BABYCARE_WORKER_ONCE':'1','BABYCARE_PUSH_ENABLED':'false'},capture_output=True,text=True,timeout=30)
    assert result.returncode==0,result.stderr
    status=api('GET',f'/api/patients/{pid}/notifications',t).json()
    assert status['worker_running'] and any(n['channel']=='in_app' for n in status['items'])
    assert api('GET',f'/api/patients/{pid}/vaccines',t).json()[0]['given_date'] is None
