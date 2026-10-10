import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone
from fastapi import Depends, HTTPException, Header, Request
from sqlalchemy.orm import Session as DbSession
from .database import get_db
from .models import User, Session, Patient, Document, Medicine, HospitalVisit, Vaccine, CaregiverAccess

SESSION_DAYS = 14

def hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac('sha256',password.encode(), bytes.fromhex(salt),390_000).hex()

def create_password(password: str):
    salt = os.urandom(16).hex()
    return salt, hash_password(password,salt)

def issue_session(db: DbSession, user: User):
    token=secrets.token_urlsafe(36)
    db.add(Session(user_id=user.id,token_hash=hashlib.sha256(token.encode()).hexdigest(), expires_at=datetime.now(timezone.utc)+timedelta(days=SESSION_DAYS)))
    db.commit()
    return token

def require_user(request: Request, authorization: str | None=Header(default=None), db: DbSession=Depends(get_db)) -> User:
    cookie = request.cookies.get('babycare_session')
    if not authorization and not cookie:
        raise HTTPException(401, 'Sign in required')
    if authorization and not authorization.startswith('Bearer '):
        raise HTTPException(401, 'Invalid authorization')
    if not authorization and request.method not in ('GET', 'HEAD', 'OPTIONS'):
        if request.headers.get('X-Requested-With') != 'BabyCare':
            raise HTTPException(403, 'CSRF check failed')
    token=authorization[7:] if authorization else cookie
    row=db.query(Session).filter(Session.token_hash==hashlib.sha256(token.encode()).hexdigest()).first()
    if not row:
        raise HTTPException(401, 'Invalid session')
    expiry = row.expires_at.replace(tzinfo=timezone.utc) if row.expires_at.tzinfo is None else row.expires_at
    if expiry < datetime.now(timezone.utc):
        raise HTTPException(401, 'Session expired')
    user=db.get(User,row.user_id)
    if not user: raise HTTPException(401,'Account unavailable')
    user._write_access = request.method not in ('GET','HEAD','OPTIONS') and not request.url.path.endswith(('/ask','/signed-access'))
    user._session_hash = row.token_hash
    return user

def own_patient(db, user, patient_id: int):
    patient=db.get(Patient,patient_id)
    if not patient:raise HTTPException(404,'Not found')
    if patient.user_id != user.id:
        grant=db.query(CaregiverAccess).filter_by(patient_id=patient_id,user_id=user.id).first()
        if not grant:raise HTTPException(404,'Not found')
        if getattr(user,'_write_access',False) and grant.role != 'editor':
            raise HTTPException(403,'Read-only caregiver access')
    return patient

def own_resource(db, user, model, resource_id: int):
    row=db.get(model,resource_id)
    if not row: raise HTTPException(404,'Not found')
    if not hasattr(row,'patient_id'): raise HTTPException(500,'Unsupported resource')
    pid=row.patient_id
    own_patient(db,user,pid)
    return row
