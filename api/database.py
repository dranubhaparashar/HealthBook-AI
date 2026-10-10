import os
from pathlib import Path
from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .config import DATA_DIR, DATABASE_URL
DATA_DIR.mkdir(parents=True, exist_ok=True)
try:
    DATA_DIR.chmod(0o700)
except OSError:
    pass
def _engine_options(url):
    if url.startswith('sqlite:'):
        return {'connect_args': {'check_same_thread': False}}
    if url.startswith('postgresql'):
        # Free-tier pooler friendly (Supabase Supavisor session mode, IPv4): at most 10 connections from one instance (the
        # free pooler allows 15 per project), recycled, TLS, no server-side prepared statements; fail fast instead of hanging.
        connect_args = {'prepare_threshold': None}
        if 'sslmode=' not in url:
            connect_args['sslmode'] = os.getenv('BABYCARE_DB_SSLMODE', 'require')
        return {'connect_args': connect_args, 'pool_size': int(os.getenv('BABYCARE_DB_POOL_SIZE', '5')), 'max_overflow': int(os.getenv('BABYCARE_DB_MAX_OVERFLOW', '5')), 'pool_recycle': 900, 'pool_timeout': int(os.getenv('BABYCARE_DB_POOL_TIMEOUT', '10'))}
    return {}

engine = create_engine(DATABASE_URL, pool_pre_ping=True, hide_parameters=True, **_engine_options(DATABASE_URL))
if DATABASE_URL.startswith('sqlite:'):
    @event.listens_for(engine, 'connect')
    def enforce_fk(dbapi_conn, _):
        dbapi_conn.execute('PRAGMA foreign_keys=ON')
        dbapi_conn.execute('PRAGMA busy_timeout=10000')
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

class Base(DeclarativeBase):
    pass

def get_db():
    with SessionLocal() as db:
        yield db
