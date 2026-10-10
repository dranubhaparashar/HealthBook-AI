"""Zero-cost cloud layer: storage backends, scheduler trigger, web push, data copy, PostgreSQL hardening. Synthetic data only."""
import hashlib
import json
import os
import re
from types import SimpleNamespace
from datetime import datetime, timezone, timedelta

import httpx
import pytest

from tests.test_api import api, make_user, make_child, make_doc, fake_png
from api import storage as storage_mod
from api.storage import LocalStorage, SupabaseStorage, MissingObject, StorageError
from api import push_web

PG_URL = os.environ.get('BABYCARE_TEST_PG_URL')  # throwaway PostgreSQL for staging checks (see docs)


# ---------------------------------------------------------------- storage backends
def test_local_storage_never_overwrites_and_reports_missing(tmp_path):
    store = LocalStorage(tmp_path / 'enc')
    store.write('a.enc', b'cipher-1')
    assert store.read('a.enc') == b'cipher-1' and store.exists('a.enc') and store.list() == ['a.enc']
    with pytest.raises(FileExistsError):
        store.write('a.enc', b'cipher-2')
    with pytest.raises(MissingObject):
        store.read('missing.enc')
    for bad in ('../x.enc', 'dir/x.enc', '.hidden'):
        with pytest.raises(StorageError):
            store.read(bad)
    store.delete('a.enc')
    assert not store.exists('a.enc')


class FakeSupabase:
    """Minimal in-memory stand-in for the Supabase Storage REST API used by SupabaseStorage."""
    def __init__(self, public=False):
        self.objects, self.buckets, self.calls = {}, {'medical-documents': {'id': 'medical-documents', 'public': public}}, []

    def handler(self, request: httpx.Request):
        self.calls.append((request.method, request.url.path, dict(request.headers)))
        assert request.headers.get('authorization') == 'Bearer service-key' and request.headers.get('apikey') == 'service-key'
        path = request.url.path
        if path.startswith('/storage/v1/bucket/'):
            bucket = path.rsplit('/', 1)[1]
            return httpx.Response(200, json=self.buckets[bucket]) if bucket in self.buckets else httpx.Response(404, json={'error': 'not found'})
        if path == '/storage/v1/bucket' and request.method == 'POST':
            body = json.loads(request.content)
            assert body['public'] is False
            self.buckets[body['id']] = body
            return httpx.Response(200, json={'name': body['id']})
        m = re.fullmatch(r'/storage/v1/object/list/([^/]+)', path)
        if m and request.method == 'POST':
            body = json.loads(request.content)
            names = sorted(k for k in self.objects if k.startswith(body['prefix']) and body.get('search', '') in k)
            page = names[body.get('offset', 0):body.get('offset', 0) + body.get('limit', 100)]
            return httpx.Response(200, json=[{'name': n[len(body['prefix']):]} for n in page])
        m = re.fullmatch(r'/storage/v1/object/([^/]+)/(.+)', path)
        if m:
            key = m.group(2)
            if request.method == 'POST':
                if key in self.objects:
                    return httpx.Response(400, json={'statusCode': '409', 'error': 'Duplicate', 'message': 'The resource already exists'})
                self.objects[key] = request.content
                return httpx.Response(200, json={'Key': key})
            if request.method == 'GET':
                return httpx.Response(200, content=self.objects[key]) if key in self.objects else httpx.Response(400, json={'error': 'Object not found'})
            if request.method == 'DELETE':
                self.objects.pop(key, None)
                return httpx.Response(200, json={'message': 'Successfully deleted'})
        return httpx.Response(500, json={'error': 'unexpected route ' + path})


def make_supabase(fake):
    return SupabaseStorage(url='https://example.supabase.co', service_key='service-key', bucket='medical-documents', transport=httpx.MockTransport(fake.handler))


def test_supabase_storage_contract_private_bucket_only():
    fake = FakeSupabase()
    store = make_supabase(fake)
    assert store.ensure_bucket() is False
    store.write('doc1.enc', b'encrypted-bytes')
    assert store.read('doc1.enc') == b'encrypted-bytes'
    assert store.exists('doc1.enc') and not store.exists('doc2.enc')
    with pytest.raises(StorageError):
        store.write('doc1.enc', b'other')  # originals are immutable
    with pytest.raises(MissingObject):
        store.read('doc2.enc')
    store.write('doc2.enc', b'x')
    assert store.list() == ['doc1.enc', 'doc2.enc']
    store.delete('doc1.enc')
    assert store.list() == ['doc2.enc']
    assert all(p.startswith('/storage/v1/object/medical-documents/documents/') for _, p, _ in fake.calls if '/object/medical-documents/' in p)
    assert store.describe() == {'backend': 'supabase', 'bucket': 'medical-documents', 'public': False}


def test_supabase_storage_refuses_public_bucket_and_creates_private_one():
    with pytest.raises(StorageError):
        make_supabase(FakeSupabase(public=True)).ensure_bucket()
    fake = FakeSupabase()
    del fake.buckets['medical-documents']
    assert make_supabase(fake).ensure_bucket() is True and fake.buckets['medical-documents']['public'] is False


def test_supabase_backend_requires_https_and_service_key(monkeypatch):
    with pytest.raises(StorageError):
        SupabaseStorage(url='http://example.supabase.co', service_key='k')
    with pytest.raises(StorageError):
        SupabaseStorage(url='https://example.supabase.co', service_key='')


def test_upload_download_roundtrip_through_storage_layer(monkeypatch):
    """Documents written by the API go through api.storage; a document whose object vanished yields 404, never a crash."""
    t = make_user('cloud_store')
    p = make_child(t)
    d = make_doc(t, p)
    assert api('GET', f'/api/documents/{d}/download', t).content == fake_png()
    from api.models import Document
    from api.database import SessionLocal
    with SessionLocal() as db:
        name = db.get(Document, d).storage_name
    assert storage_mod.document_exists(name)
    storage_mod.delete_document(name)
    assert api('GET', f'/api/documents/{d}/download', t).status_code == 404
    assert api('GET', f'/api/documents/{d}/pages/1', t).status_code == 404
    assert api('GET', f'/api/documents/{d}/thumbnail', t).status_code == 404


def test_api_roundtrip_with_supabase_backend():
    """Upload, download, page render, thumbnail, export and account deletion all go through the private bucket."""
    import io
    import zipfile
    fake = FakeSupabase()
    previous = storage_mod._instance
    storage_mod._instance = make_supabase(fake)
    try:
        t = make_user('cloud_bucket')
        p = make_child(t)
        d = make_doc(t, p)
        assert len(fake.objects) == 1 and all(k.startswith('documents/') for k in fake.objects)
        assert all(v != fake_png() for v in fake.objects.values())  # only Fernet ciphertext is stored
        assert api('GET', f'/api/documents/{d}/download', t).content == fake_png()
        assert api('GET', f'/api/documents/{d}/pages/1', t).headers['content-type'] == 'image/png'
        assert api('GET', f'/api/documents/{d}/thumbnail?w=100', t).headers['content-type'] == 'image/jpeg'
        ticket = api('POST', f'/api/documents/{d}/signed-access', t).json()['url']
        assert api('GET', ticket).content == fake_png()
        export = api('GET', f'/api/patients/{p}/export', t)
        with zipfile.ZipFile(io.BytesIO(export.content)) as archive:
            assert archive.read(f'originals/document-{d}.png') == fake_png()
        r = api('POST', '/api/account/delete', t, json={'password': 'correct-horse-battery-2026', 'confirmation': 'DELETE MY ACCOUNT AND OWNED CHILD RECORDS'})
        assert r.status_code == 200 and fake.objects == {}
    finally:
        storage_mod._instance = previous


# ---------------------------------------------------------------- scheduler trigger
def test_scheduler_tick_is_hidden_without_key_and_guarded_with_key(monkeypatch):
    monkeypatch.delenv('BABYCARE_SCHEDULER_KEY', raising=False)
    assert api('POST', '/api/internal/scheduler/tick', headers={'X-Scheduler-Key': 'anything-at-all-long-enough'}).status_code == 404
    monkeypatch.setenv('BABYCARE_SCHEDULER_KEY', 'short')
    assert api('POST', '/api/internal/scheduler/tick', headers={'X-Scheduler-Key': 'short'}).status_code == 404
    key = 'k' * 40
    monkeypatch.setenv('BABYCARE_SCHEDULER_KEY', key)
    assert api('POST', '/api/internal/scheduler/tick').status_code == 403
    assert api('POST', '/api/internal/scheduler/tick', headers={'X-Scheduler-Key': 'k' * 39 + 'x'}).status_code == 403
    r = api('POST', '/api/internal/scheduler/tick', headers={'X-Scheduler-Key': key})
    assert r.status_code == 200 and r.json()['ok'] is True and 'notifications_awaiting_delivery' in r.json()
    status = api('GET', '/api/internal/status', headers={'X-Scheduler-Key': key})
    assert status.status_code == 200
    body = status.json()
    assert body['worker_recent'] is True and body['storage']['backend'] == 'local' and 'schema_version' in body
    assert not any(k in json.dumps(body).lower() for k in ('token', 'password', 'name'))  # operational data only


def test_scheduler_tick_never_activates_unconfirmed_reminders(monkeypatch):
    t = make_user('cloud_tick')
    p = make_child(t)
    d = make_doc(t, p, category='vaccination_card')
    due = (datetime.now().date() + timedelta(days=1)).isoformat()
    api('POST', f'/api/patients/{p}/vaccines', t, json={'name': 'Example vaccine', 'dose_label': '1', 'due_date': due, 'source_document_id': d, 'source_type': 'vaccination_card'})
    monkeypatch.setenv('BABYCARE_SCHEDULER_KEY', 'z' * 40)
    assert api('POST', '/api/internal/scheduler/tick', headers={'X-Scheduler-Key': 'z' * 40}).status_code == 200
    assert api('GET', f'/api/patients/{p}/notifications', t).json()['items'] == []
    assert api('GET', f'/api/patients/{p}/reminders?days=10', t).json()['items'] == []


# ---------------------------------------------------------------- web push
SUB = {'endpoint': 'https://fcm.googleapis.com/fcm/send/abc123', 'keys': {'p256dh': 'B' * 87, 'auth': 'a' * 22}}


def test_web_push_config_and_subscription_lifecycle(monkeypatch):
    t = make_user('cloud_push')
    monkeypatch.delenv('BABYCARE_VAPID_PUBLIC_KEY', raising=False)
    monkeypatch.delenv('BABYCARE_VAPID_PRIVATE_KEY', raising=False)
    assert api('GET', '/api/push/web/config', t).json() == {**api('GET', '/api/push/web/config', t).json(), 'enabled': False, 'public_key': None}
    assert api('POST', '/api/push/web/subscribe', t, json={'subscription': SUB, 'consent_generic_push': True}).status_code == 503
    keys = push_web.generate_keys()
    monkeypatch.setenv('BABYCARE_VAPID_PUBLIC_KEY', keys['public_key'])
    monkeypatch.setenv('BABYCARE_VAPID_PRIVATE_KEY', keys['private_key'])
    cfg = api('GET', '/api/push/web/config', t).json()
    assert cfg['enabled'] is True and cfg['public_key'] == keys['public_key']
    assert api('POST', '/api/push/web/subscribe', t, json={'subscription': SUB, 'consent_generic_push': False}).status_code == 422
    assert api('POST', '/api/push/web/subscribe', t, json={'subscription': {**SUB, 'endpoint': 'http://insecure.example/x'}, 'consent_generic_push': True}).status_code == 422
    r = api('POST', '/api/push/web/subscribe', t, json={'subscription': SUB, 'consent_generic_push': True})
    assert r.status_code == 201 and r.json()['channel'] == 'web_push'
    other = make_user('cloud_push_other')
    assert api('POST', '/api/push/web/subscribe', other, json={'subscription': SUB, 'consent_generic_push': True}).status_code == 409
    assert api('GET', '/api/push/web/config').status_code == 401
    from api.models import PushDevice
    from api.database import SessionLocal
    with SessionLocal() as db:
        row = db.get(PushDevice, r.json()['id'])
        assert row.token.startswith(push_web.WEBPUSH_PREFIX) and push_web.parse_subscription(row.token)['endpoint'] == SUB['endpoint']
    assert api('DELETE', '/api/push/web/subscribe', t, json={'endpoint': SUB['endpoint']}).status_code == 200
    with SessionLocal() as db:
        assert db.get(PushDevice, r.json()['id']).enabled is False


def test_web_push_payload_is_generic_and_outcomes_are_classified(monkeypatch):
    import pywebpush
    keys = push_web.generate_keys()
    monkeypatch.setenv('BABYCARE_VAPID_PUBLIC_KEY', keys['public_key'])
    monkeypatch.setenv('BABYCARE_VAPID_PRIVATE_KEY', keys['private_key'])
    monkeypatch.setenv('BABYCARE_VAPID_CONTACT', 'parent@example.invalid')
    seen = {}

    def fake_webpush(**kwargs):
        seen.update(kwargs)
        return SimpleNamespace(status_code=201)
    monkeypatch.setattr(pywebpush, 'webpush', fake_webpush)
    assert push_web.send(SUB, 77) == ('delivered', None)
    payload = json.loads(seen['data'])
    assert payload == {'title': 'HealthBook AI', 'body': push_web.GENERIC_BODY, 'notification_id': 77}
    assert seen['vapid_claims'] == {'sub': 'mailto:parent@example.invalid'} and seen['ttl'] == 3600

    def gone(**kwargs):
        raise pywebpush.WebPushException('gone', response=SimpleNamespace(status_code=410))
    monkeypatch.setattr(pywebpush, 'webpush', gone)
    assert push_web.send(SUB, 1) == ('gone', 'subscription_gone')

    def busy(**kwargs):
        raise pywebpush.WebPushException('busy', response=SimpleNamespace(status_code=503))
    monkeypatch.setattr(pywebpush, 'webpush', busy)
    assert push_web.send(SUB, 1) == ('retry', 'push_service_503')
    monkeypatch.delenv('BABYCARE_VAPID_PRIVATE_KEY')
    assert push_web.send(SUB, 1) == ('retry', 'webpush_not_configured')


def test_worker_web_push_branch_updates_notification_and_device(monkeypatch):
    from api import worker
    now = datetime.now(timezone.utc)
    monkeypatch.setenv('BABYCARE_VAPID_PUBLIC_KEY', 'pub')
    monkeypatch.setenv('BABYCARE_VAPID_PRIVATE_KEY', 'priv')
    device = SimpleNamespace(token=push_web.WEBPUSH_PREFIX + json.dumps(SUB), enabled=True)

    def run(outcome, code=None, attempts=0):
        n = SimpleNamespace(status='pending', attempts=attempts, error_code=None, next_attempt_at=now, id=5)
        monkeypatch.setattr(push_web, 'send', lambda sub, nid: (outcome, code))
        worker.deliver_web_push(None, n, device, now)
        return n
    assert run('delivered').status == 'provider_received'
    n = run('retry', 'push_service_503')
    assert n.status == 'retry' and n.error_code == 'push_service_503' and n.next_attempt_at > now
    assert run('retry', 'push_service_503', attempts=6).status == 'failed'
    n = run('gone', 'subscription_gone')
    assert n.status == 'failed' and device.enabled is False
    monkeypatch.delenv('BABYCARE_VAPID_PRIVATE_KEY')
    n = SimpleNamespace(status='pending', attempts=0, error_code=None, next_attempt_at=now, id=6)
    worker.deliver_web_push(None, n, device, now)
    assert n.error_code == 'webpush_not_configured' and n.status == 'pending' and n.attempts == 0


def test_vapid_keys_are_urlsafe_base64():
    keys = push_web.generate_keys()
    assert re.fullmatch(r'[A-Za-z0-9_-]{43}', keys['private_key']) and re.fullmatch(r'[A-Za-z0-9_-]{87}', keys['public_key'])


# ---------------------------------------------------------------- raw data copy (used by migration + backup)
def test_datasync_copies_encrypted_values_verbatim(tmp_path):
    from sqlalchemy import create_engine, text
    from api.database import Base
    from api.models import User, Patient
    from api.datasync import all_tables, dump_rows, insert_rows, row_counts
    src = create_engine(f"sqlite:///{(tmp_path / 'src.sqlite3').as_posix()}")
    dst = create_engine(f"sqlite:///{(tmp_path / 'dst.sqlite3').as_posix()}")
    Base.metadata.create_all(src)
    Base.metadata.create_all(dst)
    from sqlalchemy.orm import Session
    with Session(src) as s:
        u = User(email='copy@test.invalid', password_salt='s', password_hash='h')
        s.add(u)
        s.flush()
        s.add(Patient(user_id=u.id, name='Synthetic Child', dob=datetime(2025, 8, 3).date(), tz='Asia/Kolkata'))
        s.commit()
    tables = all_tables()
    with src.connect() as a, dst.begin() as b:
        for table in tables:
            insert_rows(b, table, dump_rows(a, table))
    with src.connect() as a, dst.connect() as b:
        assert row_counts(a, tables) == row_counts(b, tables)
        raw_src = a.execute(text('SELECT name FROM patients')).scalar()
        raw_dst = b.execute(text('SELECT name FROM patients')).scalar()
    assert raw_src.startswith('enc:') and raw_src == raw_dst  # no decrypt/re-encrypt happened
    with Session(dst) as s:
        assert s.query(Patient).one().name == 'Synthetic Child'


def test_harden_database_matches_dialect():
    from api.database import engine
    from api.cloud import harden_database, verify_hardening
    if engine.dialect.name == 'sqlite':
        assert harden_database(engine) == {'applied': False, 'reason': 'not postgresql'}
        assert verify_hardening(engine) == {'postgresql': False}
    else:  # staging run against PostgreSQL: every table ends up with RLS enabled
        result = harden_database(engine)
        assert result['applied'] is True and result['tables'] > 20
        assert verify_hardening(engine)['tables_without_rls'] == []


def test_full_backup_create_verify_restore_cycle(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet
    import importlib
    monkeypatch.setenv('BABYCARE_BACKUP_KEY', Fernet.generate_key().decode())
    import scripts.cloud_backup as cb
    importlib.reload(cb)
    t = make_user('cloud_backup')
    p = make_child(t)
    make_doc(t, p)
    target, manifest = cb.create(tmp_path / 'full.enc')
    assert target.exists() and manifest['tables']['documents'] >= 1 and len(manifest['objects']) >= 1 and manifest['includes_app_key'] is False
    verified, problems = cb.verify(target)
    assert verified['schema'] == 'babycare-full-backup-1'
    # Other tests in this suite deliberately delete an original behind a document row; verify must report exactly that and nothing else.
    assert all('no stored original' in p for p in problems), problems
    with pytest.raises(SystemExit):
        cb.restore(target)  # the live test database is not empty: restore must refuse


# ---------------------------------------------------------------- PostgreSQL staging checks (skipped unless a throwaway server is provided)
@pytest.mark.skipif(not PG_URL, reason='set BABYCARE_TEST_PG_URL to a throwaway PostgreSQL to run staging checks')
def test_postgres_hardening_locks_out_supabase_client_roles():
    from sqlalchemy import create_engine, text
    from api.database import Base
    from api.cloud import harden_database, verify_hardening
    engine = create_engine(PG_URL, connect_args={'prepare_threshold': None})
    with engine.begin() as c:
        for role in ('anon', 'authenticated'):
            c.execute(text(f"DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='{role}') THEN CREATE ROLE {role} NOLOGIN; END IF; END $$"))
        c.execute(text('GRANT USAGE ON SCHEMA public TO anon, authenticated'))
        c.execute(text('GRANT SELECT ON ALL TABLES IN SCHEMA public TO anon, authenticated'))
    Base.metadata.create_all(engine)
    result = harden_database(engine)
    assert result['applied'] is True and set(result['client_roles_revoked']) == {'anon', 'authenticated'}
    check = verify_hardening(engine)
    assert check['tables_without_rls'] == [] and check['client_role_grants'] == {}
    with engine.connect() as c:
        c.execute(text('SET ROLE anon'))
        with pytest.raises(Exception):
            c.execute(text('SELECT count(*) FROM patients')).scalar()


@pytest.mark.skipif(not PG_URL, reason='set BABYCARE_TEST_PG_URL to a throwaway PostgreSQL to run staging checks')
def test_sqlite_to_postgres_copy_keeps_ids_and_advances_sequences(tmp_path):
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session
    from api.database import Base
    from api.models import User, Patient, Document
    from api.datasync import all_tables, dump_rows, insert_rows, reset_sequences, row_counts
    src = create_engine(f"sqlite:///{(tmp_path / 'src.sqlite3').as_posix()}")
    Base.metadata.create_all(src)
    with Session(src) as s:
        u = User(email='pg@test.invalid', password_salt='s', password_hash='h')
        s.add(u)
        s.flush()
        s.add(Patient(id=7, user_id=u.id, name='Synthetic', dob=datetime(2025, 8, 3).date(), tz='Asia/Kolkata'))
        s.commit()
    pg = create_engine(PG_URL, connect_args={'prepare_threshold': None})
    Base.metadata.drop_all(pg)
    Base.metadata.create_all(pg)
    tables = all_tables()
    with src.connect() as a, pg.begin() as b:
        for table in tables:
            insert_rows(b, table, dump_rows(a, table))
        reset_sequences(b, tables)
    with src.connect() as a, pg.connect() as b:
        assert row_counts(a, tables) == row_counts(b, tables)
    with Session(pg) as s:
        assert s.query(Patient).one().id == 7 and s.query(Patient).one().name == 'Synthetic'
        s.add(Patient(user_id=1, name='Second', dob=datetime(2025, 8, 3).date(), tz='Asia/Kolkata'))
        s.commit()  # would fail with a duplicate key if the sequence had not been advanced
        assert sorted(p.id for p in s.query(Patient)) == [7, 8]
