"""Zero-cost cloud operations: database lock-down, scheduler trigger, web push registration, status.

Nothing here reads or returns clinical content. The scheduler endpoint exists because free hosting sleeps:
an external free scheduler (cron-job.org, Supabase pg_cron) calls it, which wakes the API and runs one
reminder worker tick. Delivery is only ever the fixed generic sentence defined in api.push_web.
"""
import hashlib
import hmac
import json
import logging
import os
from datetime import timedelta
from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel, Field, HttpUrl, field_validator
from sqlalchemy import text, func
from sqlalchemy.orm import Session as DbSession
from .config import PRODUCTION
from .database import get_db
from .models import User, PushDevice, Notification, WorkerHeartbeat, utcnow
from .security import require_user
from . import push_web
from .reminders import aware

log = logging.getLogger('babycare.cloud')
router = APIRouter(prefix='/api')

SUPABASE_CLIENT_ROLES = ('anon', 'authenticated')


# ------------------------------------------------------------------ database hardening (runs at startup)
def harden_database(engine):
    """On PostgreSQL: enable row level security on every table and strip the Supabase client roles.

    The API connects as the table owner (RLS does not apply to the owner), so the application keeps working
    while PostgREST/GraphQL callers holding the anon or authenticated keys can read nothing. Idempotent.
    """
    if engine.dialect.name != 'postgresql':
        return {'applied': False, 'reason': 'not postgresql'}
    try:
        with engine.begin() as connection:
            roles = {r[0] for r in connection.execute(text("SELECT rolname FROM pg_roles WHERE rolname = ANY(:names)"), {'names': list(SUPABASE_CLIENT_ROLES)})}
            tables = [r[0] for r in connection.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public'"))]
            for table in tables:
                connection.execute(text(f'ALTER TABLE public."{table}" ENABLE ROW LEVEL SECURITY'))
            if roles:
                role_list = ', '.join(sorted(roles))
                connection.execute(text(f'REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {role_list}'))
                connection.execute(text(f'REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {role_list}'))
                connection.execute(text(f'REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM {role_list}'))
                connection.execute(text(f'REVOKE USAGE, CREATE ON SCHEMA public FROM {role_list}'))
                connection.execute(text(f'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON TABLES FROM {role_list}'))
                connection.execute(text(f'ALTER DEFAULT PRIVILEGES IN SCHEMA public REVOKE ALL ON SEQUENCES FROM {role_list}'))
        return {'applied': True, 'tables': len(tables), 'client_roles_revoked': sorted(roles)}
    except Exception as exc:
        if PRODUCTION:
            raise RuntimeError('Database hardening failed; refusing to start with potentially exposed tables') from exc
        log.warning('database hardening skipped: %s', type(exc).__name__)
        return {'applied': False, 'reason': type(exc).__name__}


def verify_hardening(engine):
    """Read-only check used by the status endpoint and the deployment runbook."""
    if engine.dialect.name != 'postgresql':
        return {'postgresql': False}
    with engine.connect() as connection:
        unprotected = [r[0] for r in connection.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public' AND NOT rowsecurity"))]
        grants = connection.execute(text("SELECT grantee, COUNT(*) FROM information_schema.role_table_grants WHERE table_schema='public' AND grantee = ANY(:names) GROUP BY grantee"),
                                    {'names': list(SUPABASE_CLIENT_ROLES)}).all()
        return {'postgresql': True, 'tables_without_rls': unprotected, 'client_role_grants': {g: n for g, n in grants}}


def prepare_storage():
    """Create the private bucket when the Supabase backend is selected. Fails closed if the bucket is public."""
    from .storage import get_storage, SupabaseStorage
    store = get_storage()
    if isinstance(store, SupabaseStorage):
        created = store.ensure_bucket()
        return {'backend': 'supabase', 'bucket_created': created}
    return store.describe()


def install_handlers(app):
    """Map document-store failures to safe HTTP errors (no object names or stack traces leave the server)."""
    from fastapi.responses import JSONResponse
    from .storage import MissingObject, StorageError

    @app.exception_handler(MissingObject)
    async def _missing(request, exc):
        return JSONResponse({'detail': 'Encrypted file is missing'}, status_code=404)

    @app.exception_handler(StorageError)
    async def _unavailable(request, exc):
        log.error('document store error: %s', type(exc).__name__)
        return JSONResponse({'detail': 'Encrypted document store unavailable; try again shortly'}, status_code=503)


# ------------------------------------------------------------------ scheduler trigger
def _scheduler_key():
    return os.getenv('BABYCARE_SCHEDULER_KEY', '').strip()


def require_scheduler(x_scheduler_key: str | None = Header(default=None)):
    key = _scheduler_key()
    if not key or len(key) < 24:
        raise HTTPException(404, 'Not found')  # feature disabled until a strong key is configured
    if not x_scheduler_key or not hmac.compare_digest(x_scheduler_key, key):
        raise HTTPException(403, 'Scheduler key rejected')
    return True


@router.post('/internal/scheduler/tick')
def scheduler_tick(_: bool = Depends(require_scheduler), db: DbSession = Depends(get_db)):
    """Run one reminder worker tick (reconcile confirmed plans, deliver generic pushes)."""
    from .worker import tick
    now = utcnow()
    tick(now)
    pending = db.query(Notification).filter(Notification.status.in_(['pending', 'retry', 'accepted'])).count()
    return {'ok': True, 'at': now.isoformat(), 'notifications_awaiting_delivery': pending}


@router.get('/internal/status')
def internal_status(_: bool = Depends(require_scheduler), db: DbSession = Depends(get_db)):
    """Operational status without any clinical content."""
    from .database import engine
    from .storage import get_storage
    beat = db.get(WorkerHeartbeat, 1)
    version = db.execute(text('SELECT version_num FROM alembic_version')).scalar()
    return {'ok': True, 'database': engine.dialect.name, 'schema_version': version, 'hardening': verify_hardening(engine), 'storage': get_storage().describe(),
            'web_push_configured': push_web.configured(), 'email': __import__('api.password_reset',fromlist=['status']).status(), 'last_worker_tick': aware(beat.at).isoformat() if beat else None,
            'worker_recent': bool(beat and aware(beat.at) > utcnow() - timedelta(minutes=20)),
            'notifications': {s: n for s, n in db.query(Notification.status, func.count()).group_by(Notification.status).all()}}


# ------------------------------------------------------------------ web push subscriptions
class WebPushKeys(BaseModel):
    p256dh: str = Field(min_length=20, max_length=200, pattern=r'^[A-Za-z0-9_\-=]+$')
    auth: str = Field(min_length=10, max_length=100, pattern=r'^[A-Za-z0-9_\-=]+$')


class WebPushSubscription(BaseModel):
    endpoint: HttpUrl
    keys: WebPushKeys
    expirationTime: int | None = None

    @field_validator('endpoint')
    @classmethod
    def https_only(cls, value):
        if value.scheme != 'https' or len(str(value)) > 1500:
            raise ValueError('Push endpoint must be an https URL')
        return value


class WebPushIn(BaseModel):
    subscription: WebPushSubscription
    consent_generic_push: bool

    @field_validator('consent_generic_push')
    @classmethod
    def must_consent(cls, value):
        if value is not True:
            raise ValueError('Consent to generic push notifications is required')
        return value


class WebPushOut(BaseModel):
    endpoint: HttpUrl


def _endpoint_hash(endpoint: str) -> str:
    return hashlib.sha256(('webpush|' + endpoint).encode()).hexdigest()


@router.get('/push/web/config')
def web_push_config(user: User = Depends(require_user)):
    return {'enabled': push_web.configured(), 'public_key': push_web.public_key() if push_web.configured() else None,
            'body_policy': 'Push text is always the same generic sentence; details stay inside the signed-in app.'}


@router.post('/push/web/subscribe', status_code=201)
def web_push_subscribe(body: WebPushIn, user: User = Depends(require_user), db: DbSession = Depends(get_db)):
    if not push_web.configured():
        raise HTTPException(503, 'Web push is not configured on this server')
    endpoint = str(body.subscription.endpoint)
    digest = _endpoint_hash(endpoint)
    row = db.query(PushDevice).filter_by(token_hash=digest).first()
    if row and row.user_id != user.id:
        raise HTTPException(409, 'This browser is registered to another account; sign out there first')
    token = push_web.WEBPUSH_PREFIX + json.dumps({'endpoint': endpoint, 'keys': body.subscription.keys.model_dump()})
    if not row:
        row = PushDevice(user_id=user.id, token=token, token_hash=digest)
    row.token = token
    row.enabled = True
    db.add(row)
    db.commit()
    return {'id': row.id, 'enabled': True, 'channel': 'web_push'}


@router.delete('/push/web/subscribe')
def web_push_unsubscribe(body: WebPushOut, user: User = Depends(require_user), db: DbSession = Depends(get_db)):
    row = db.query(PushDevice).filter_by(token_hash=_endpoint_hash(str(body.endpoint)), user_id=user.id).first()
    if row:
        row.enabled = False
        db.commit()
    return {'ok': True}
