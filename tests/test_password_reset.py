"""Forgot / reset password: generic response, hashed single-use expiring tokens, sessions invalidated, data preserved,
no token in logs. Synthetic accounts only."""
import hashlib
import logging
import re
import uuid
from datetime import timedelta
import pytest
from tests.test_api import api, make_child, client
from api import mailer
from api.database import SessionLocal
from api.models import RateWindow, utcnow
from api.password_reset import PasswordResetToken, GENERIC

PW = 'correct-horse-battery-2026'

@pytest.fixture(autouse=True)
def outbox(monkeypatch):
    with SessionLocal() as db:db.query(RateWindow).delete();db.commit()
    box=[];monkeypatch.setattr(mailer,'OUTBOX',box);yield box
    with SessionLocal() as db:db.query(RateWindow).delete();db.commit()

def account():
    email=f'reset{uuid.uuid4().hex[:10]}@test.invalid'
    r=api('POST','/api/auth/register',json={'email':email,'password':PW});assert r.status_code==201,r.text
    return email,r.json()['token']

def token_from(box):
    m=re.search(r'#reset-password=([A-Za-z0-9_\-]+)',box[-1]['text']);assert m,box[-1]['text'];return m[1]

def login(email,pw):return api('POST','/api/auth/login',json={'email':email,'password':pw})

def test_full_reset_flow_preserves_data_and_signs_out_old_sessions(outbox):
    email,old_token=account();pid=make_child(old_token,'Fictional Reset Child')
    r=api('POST','/api/auth/password-reset/request',json={'email':email.upper()});assert r.status_code==202 and r.json()['message']==GENERIC
    assert len(outbox)==1 and outbox[0]['to']==email and outbox[0]['subject']=='Reset your HealthBook AI password'
    tok=token_from(outbox)
    assert api('POST','/api/auth/password-reset/check',json={'token':tok}).json()['valid'] is True
    new='a-brand-new-password-2026'
    r=api('POST','/api/auth/password-reset/confirm',json={'token':tok,'password':new});assert r.status_code==200 and r.json()['signed_out_sessions']>=1
    assert api('GET','/api/me',old_token).status_code==401                     # old sessions signed out
    assert login(email,PW).status_code==401                                    # old password no longer works
    r=login(email,new);assert r.status_code==200;t=r.json()['token']
    assert [p['name'] for p in api('GET','/api/patients',t).json()]==['Fictional Reset Child']  # data preserved
    assert api('POST','/api/auth/password-reset/confirm',json={'token':tok,'password':'yet-another-password-1'}).status_code==400  # single use

def test_unknown_email_gets_the_same_generic_answer_and_no_email(outbox):
    r=api('POST','/api/auth/password-reset/request',json={'email':'nobody-here@test.invalid'})
    assert r.status_code==202 and r.json()['message']==GENERIC and outbox==[]

def test_token_is_stored_hashed_expires_and_is_replaced_by_a_newer_request(outbox):
    email,_=account()
    api('POST','/api/auth/password-reset/request',json={'email':email});first=token_from(outbox)
    with SessionLocal() as db:
        rows=db.query(PasswordResetToken).all();hashes={r.token_hash for r in rows}
        assert first not in hashes and hashlib.sha256(first.encode()).hexdigest() in hashes   # only the hash is stored
    api('POST','/api/auth/password-reset/request',json={'email':email});second=token_from(outbox)
    assert api('POST','/api/auth/password-reset/check',json={'token':first}).status_code==400   # replaced
    with SessionLocal() as db:
        row=db.query(PasswordResetToken).filter_by(token_hash=hashlib.sha256(second.encode()).hexdigest()).one();row.expires_at=utcnow()-timedelta(minutes=1);db.commit()
    assert api('POST','/api/auth/password-reset/confirm',json={'token':second,'password':'a-brand-new-password-2026'}).status_code==400  # expired
    assert login(email,PW).status_code==200

def test_weak_password_and_garbage_tokens_are_rejected(outbox):
    email,_=account();api('POST','/api/auth/password-reset/request',json={'email':email});tok=token_from(outbox)
    assert api('POST','/api/auth/password-reset/confirm',json={'token':tok,'password':'short'}).status_code==422
    assert api('POST','/api/auth/password-reset/confirm',json={'token':'x'*40,'password':'a-brand-new-password-2026'}).status_code==400
    assert api('POST','/api/auth/password-reset/check',json={'token':tok}).status_code==200   # still valid after a rejected attempt

def test_per_email_rate_limit_without_revealing_anything(outbox):
    email,_=account()
    for _ in range(5):assert api('POST','/api/auth/password-reset/request',json={'email':email}).json()['message']==GENERIC
    assert len(outbox)==3

def test_token_and_password_never_reach_the_logs(outbox,caplog):
    caplog.set_level(logging.DEBUG)
    email,_=account();api('POST','/api/auth/password-reset/request',json={'email':email});tok=token_from(outbox)
    api('POST','/api/auth/password-reset/confirm',json={'token':tok,'password':'a-brand-new-password-2026'})
    assert tok not in caplog.text and 'a-brand-new-password-2026' not in caplog.text

def test_reset_link_uses_the_public_web_origin_and_url_fragment(outbox,monkeypatch):
    monkeypatch.setenv('BABYCARE_PUBLIC_WEB_ORIGIN','https://example-healthbook.web.app')
    email,_=account();api('POST','/api/auth/password-reset/request',json={'email':email})
    assert re.search(r'https://example-healthbook\.web\.app/#reset-password=[A-Za-z0-9_\-]{40,}',outbox[-1]['text'])
