"""Forgot password / reset password.

Request: always answers with the same generic message and status, whether or not the email is registered; the email is
sent in the background so response time does not reveal it either. Rate limited per IP (all /api/auth/ routes) and per
email address.

Token: 32 random bytes (secrets.token_urlsafe), only its SHA-256 hash is stored, valid for 30 minutes, single use; a new
request replaces earlier unused tokens. The link carries the token in the URL fragment (#reset-password=…), which
browsers never send to a server, so it cannot appear in hosting or API access logs.

Confirm: sets the new password (same policy as sign-up), marks the token used, invalidates every other reset token and
signs out every existing session of that account. Profiles and medical data are untouched.
"""
import hashlib
import hmac
import os
import secrets
from datetime import timedelta
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, Session as DbSession
from .database import Base, get_db
from .models import User, Session as AuthSession, Audit, utcnow
from .security import create_password
from . import mailer

TOKEN_MINUTES = 30
GENERIC = 'If an account exists for this email, a reset link has been sent. The link is valid for 30 minutes.'
INVALID = 'This reset link is invalid or has expired. Request a new one from the sign-in page.'
router = APIRouter(prefix='/api/auth/password-reset', tags=['auth'])


class PasswordResetToken(Base):
    __tablename__ = 'password_reset_tokens'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[object] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[object] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[object | None] = mapped_column(DateTime(timezone=True), nullable=True)


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def _aware(dt):
    from datetime import timezone
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


def _clean_email(v):
    v = v.strip().lower()
    if '@' not in v or '.' not in v.split('@')[-1]:
        raise ValueError('Enter a valid email address')
    return v


class ResetRequest(BaseModel):
    email: str = Field(min_length=4, max_length=254)
    _e = field_validator('email')(classmethod(lambda cls, v: _clean_email(v)))


class ResetConfirm(BaseModel):
    token: str = Field(min_length=20, max_length=200)
    password: str = Field(min_length=12, max_length=200)


class ResetCheck(BaseModel):
    token: str = Field(min_length=20, max_length=200)


def web_origin():
    origin = os.getenv('BABYCARE_PUBLIC_WEB_ORIGIN', '').rstrip('/')
    if origin:
        return origin
    first = os.getenv('BABYCARE_CORS_ORIGINS', '').split(',')[0].strip().rstrip('/')
    return first or 'http://127.0.0.1:8000'


def _message(link):
    text = (f'Someone asked to reset the password of your HealthBook AI account.\n\n'
            f'Set a new password here (valid for {TOKEN_MINUTES} minutes, usable once):\n{link}\n\n'
            'If you did not ask for this, ignore this email; your password stays the same.\n\nHealthBook AI')
    html = (f'<p>Someone asked to reset the password of your <b>HealthBook AI</b> account.</p>'
            f'<p><a href="{link}" style="background:#1f5f6b;color:#fff;padding:10px 16px;border-radius:8px;text-decoration:none">Set a new password</a></p>'
            f'<p>The link is valid for {TOKEN_MINUTES} minutes and can be used once. If you did not ask for this, ignore this email; your password stays the same.</p>'
            '<p style="color:#66727c">HealthBook AI · Your private family health workspace.</p>')
    return 'Reset your HealthBook AI password', text, html


def _deliver(email, link):
    subject, text, html = _message(link)
    mailer.send(email, subject, text, html)


def _per_email_allowed(email):
    from .guardrails import rate_allowed
    return rate_allowed('pwreset:' + hashlib.sha256(email.encode()).hexdigest()[:32], 3, 3600)


@router.post('/request', status_code=202)
def request_reset(body: ResetRequest, background: BackgroundTasks, db: DbSession = Depends(get_db)):
    user = db.query(User).filter(User.email == body.email).first()
    if user and _per_email_allowed(body.email):
        now = utcnow()
        for old in db.query(PasswordResetToken).filter(PasswordResetToken.user_id == user.id, PasswordResetToken.used_at.is_(None)):
            old.used_at = now  # only the newest link works
        token = secrets.token_urlsafe(32)
        db.add(PasswordResetToken(user_id=user.id, token_hash=_hash(token), created_at=now, expires_at=now + timedelta(minutes=TOKEN_MINUTES)))
        db.add(Audit(user_id=user.id, patient_id=None, action='password_reset_requested', entity='user', entity_id=user.id))
        db.commit()
        background.add_task(_deliver, user.email, f'{web_origin()}/#reset-password={token}')
    return {'message': GENERIC}


def _valid_token(db, token):
    row = db.query(PasswordResetToken).filter(PasswordResetToken.token_hash == _hash(token)).first()
    if not row or row.used_at is not None or _aware(row.expires_at) < utcnow() or not hmac.compare_digest(row.token_hash, _hash(token)):
        return None
    return row


@router.post('/check')
def check_reset(body: ResetCheck, db: DbSession = Depends(get_db)):
    """Lets the reset page tell the user up front whether the link still works. Reveals no account details."""
    if not _valid_token(db, body.token):
        raise HTTPException(400, INVALID)
    return {'valid': True, 'minutes_valid': TOKEN_MINUTES}


@router.post('/confirm')
def confirm_reset(body: ResetConfirm, db: DbSession = Depends(get_db)):
    row = _valid_token(db, body.token)
    if not row:
        raise HTTPException(400, INVALID)
    user = db.get(User, row.user_id)
    if not user:
        raise HTTPException(400, INVALID)
    now = utcnow()
    user.password_salt, user.password_hash = create_password(body.password)
    row.used_at = now
    for other in db.query(PasswordResetToken).filter(PasswordResetToken.user_id == user.id, PasswordResetToken.used_at.is_(None)):
        other.used_at = now
    signed_out = db.query(AuthSession).filter(AuthSession.user_id == user.id).delete()
    db.add(Audit(user_id=user.id, patient_id=None, action='password_reset_completed', entity='user', entity_id=user.id))
    db.commit()
    return {'ok': True, 'signed_out_sessions': signed_out, 'message': 'Your password has been changed. Sign in with the new password.'}


def status():
    return {'email_provider': mailer.provider() or 'none', 'email_configured': mailer.configured(), 'reset_link_minutes': TOKEN_MINUTES}
