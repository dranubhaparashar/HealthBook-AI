"""Transport and abuse protections; never log request bodies or medical data."""
import hashlib
from datetime import timedelta
from fastapi.responses import JSONResponse
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from .database import SessionLocal
from .models import RateWindow, utcnow
from .config import PRODUCTION

def utc(value):
    from datetime import timezone
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value

import os, threading, time as _time
_MEM={}; _MEM_LOCK=threading.Lock()

def _rate_allowed_memory(key, limit, seconds):
    """Fixed window per key held in process memory: no database round trip per request. Right for one instance
    (Render free); counts reset on restart, which only makes the limiter more lenient."""
    now=_time.monotonic()
    with _MEM_LOCK:
        start,count=_MEM.get(key,(now,0))
        if now-start>=seconds: start,count=now,0
        if count>=limit: _MEM[key]=(start,count); return False
        _MEM[key]=(start,count+1)
        if len(_MEM)>50000: _MEM.clear()
        return True

def rate_allowed(key, limit=30, seconds=60):
    if os.getenv('BABYCARE_RATE_LIMIT_STORE','db').lower()=='memory':
        return _rate_allowed_memory(hashlib.sha256(key.encode()).hexdigest(),limit,seconds)
    raw_key=key
    key=hashlib.sha256(key.encode()).hexdigest()
    now=utcnow()
    with SessionLocal() as db:
        row=db.get(RateWindow,key)
        if not row:
            db.add(RateWindow(key=key,started_at=now,count=1))
            try: db.commit(); return True
            except IntegrityError: db.rollback(); return rate_allowed(raw_key,limit,seconds)
        if utc(row.started_at) < now-timedelta(seconds=seconds):
            db.execute(update(RateWindow).where(RateWindow.key==key).values(count=1,started_at=now));db.commit();return True
        result=db.execute(update(RateWindow).where(RateWindow.key==key,RateWindow.count<limit).values(count=RateWindow.count+1))
        db.commit()
        return result.rowcount == 1

async def secure_headers(request, call_next):
    if request.url.path.startswith('/api/'):
        origin=request.headers.get('origin')
        if request.method not in ('GET','HEAD','OPTIONS') and origin:
            from .config import os
            allowed={str(request.base_url).rstrip('/'),*os.getenv('BABYCARE_CORS_ORIGINS','').split(',')}
            if origin not in allowed:
                return JSONResponse({'detail':'Origin not allowed'},status_code=403)
        if request.url.path=='/api/health':return await call_next(request)  # liveness probe: no database round trip
        ip=request.client.host if request.client else 'unknown'
        auth=request.url.path.startswith('/api/auth/')
        # The rate window is a database write; run it in the threadpool so a busy connection pool can never freeze the event loop.
        from starlette.concurrency import run_in_threadpool
        if not await run_in_threadpool(rate_allowed,('auth:' if auth else 'api:')+ip,30 if auth else 600):
            return JSONResponse({'detail':'Too many requests. Try again shortly.'},status_code=429,headers={'Retry-After':'60'})
        try: length=int(request.headers.get('content-length','0') or 0)
        except ValueError: return JSONResponse({'detail':'Invalid content length'},status_code=400)
        if length>13*1024*1024:
            return JSONResponse({'detail':'Request too large'},status_code=413)
    response=await call_next(request)
    response.headers.update({'X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer','Cache-Control':'no-store','X-Frame-Options':'DENY','Permissions-Policy':'camera=(self), microphone=(), geolocation=()',
        'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; frame-src 'self' blob:; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"})
    if PRODUCTION: response.headers['Strict-Transport-Security']='max-age=31536000; includeSubDomains'
    return response
